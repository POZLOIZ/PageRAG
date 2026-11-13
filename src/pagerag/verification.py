"""回答生成后的引用核验与局部修订。

核验输入是本次回答实际发送给模型的证据条目；输出是逐条结论的判定，
不是新的检索步骤。核验失败只记录 unavailable，不改写回答；只有明确
存在不支持内容且开启修订时，才调用一次局部修订并重新核验。
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

VERIFY_PROMPT = """You verify whether cited evidence supports the factual claims in an answer.
Return JSON only. Do not add commentary or markdown fences."""

REPAIR_PROMPT = """You repair an answer using only the numbered evidence.
Keep supported content and citation numbers. For unsupported details, either cite
the evidence that actually supports them or remove them. If the evidence cannot
confirm something, say so explicitly. Do not add facts that are not in the evidence.
Answer in the same language as the original answer. Return only the corrected answer."""


class ClaimCheck(BaseModel):
    claim: str
    citations: list[int] = Field(default_factory=list)
    verdict: Literal["supported", "partial", "unsupported", "uncited"]
    reason: str = ""
    # 该结论是否在局部修订后的复核里重新判定过；只用于页面展示。
    repaired: bool = False

    @field_validator("citations", mode="before")
    @classmethod
    def _coerce_citations(cls, value):
        # 模型常把引用写成 "[1]" 或 "1, 2"，这里统一成整数列表；无法解析的
        # 项直接丢弃，后续按越界引用处理。
        if value is None:
            return []
        if isinstance(value, (int, str)):
            value = [value]
        if not isinstance(value, list):
            return []
        numbers: list[int] = []
        for item in value:
            if isinstance(item, int):
                numbers.append(item)
                continue
            match = re.search(r"\d+", str(item))
            if match:
                numbers.append(int(match.group()))
        return numbers

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, value):
        text = str(value or "").strip().lower()
        aliases = {
            "supported by evidence": "supported",
            "partially supported": "partial",
            "not supported": "unsupported",
            "no citation": "uncited",
        }
        return aliases.get(text, text)


class ClaimResponse(BaseModel):
    claims: list[ClaimCheck] = Field(default_factory=list)


@dataclass
class VerificationResult:
    status: str
    claims: list[dict] = field(default_factory=list)
    issues: dict = field(default_factory=dict)
    original_answer: str = ""
    final_answer: str = ""
    repair_count: int = 0
    timings: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    error: str | None = None


class VerificationUnavailable(Exception):
    pass


class AnswerVerifier:
    def __init__(self, settings, llm) -> None:
        self._settings = settings
        self._llm = llm
        self._timeout = float(getattr(
            settings, "answer_verification_timeout_seconds", 45.0))
        self._max_claims = max(1, int(getattr(
            settings, "answer_verification_max_claims", 12)))
        # 解析失败与调用失败都只重试一次，避免端点异常时拖长查询。
        self._attempts = 2

    async def run(
        self,
        question: str,
        answer: str,
        items: list[dict],
        *,
        insufficient: bool = False,
    ) -> VerificationResult:
        started = time.monotonic()
        if insufficient:
            return VerificationResult(
                status="skipped",
                original_answer=answer,
                final_answer=answer,
                issues=_count_issues([]),
                timings={"verify": 0.0, "total": round(time.monotonic() - started, 3)},
            )
        if not items:
            return VerificationResult(
                status="unavailable",
                original_answer=answer,
                final_answer=answer,
                issues=_count_issues([]),
                error="no_evidence",
                timings={"verify": 0.0, "total": round(time.monotonic() - started, 3)},
            )

        timings: dict[str, float] = {}
        usage: dict[str, int] = {}
        try:
            check_started = time.monotonic()
            claims, call_usage = await self._check(question, answer, items)
            timings["verify"] = round(time.monotonic() - check_started, 3)
            _merge_usage(usage, call_usage)
        except Exception as exc:  # noqa: BLE001 - 核验失败不阻断回答
            timings["total"] = round(time.monotonic() - started, 3)
            return VerificationResult(
                status="unavailable",
                original_answer=answer,
                final_answer=answer,
                issues=_count_issues([]),
                error=f"{type(exc).__name__}: {exc}"[:1000],
                timings=timings,
                usage=usage,
            )

        issues = _count_issues(claims)
        final_answer = answer
        repair_count = 0
        status = "succeeded" if _is_clean(issues) else "partial"
        error = None
        if not _is_clean(issues) and self._settings.answer_verification_repair_enabled:
            repair_started = time.monotonic()
            try:
                repaired, repair_usage = await self._repair(
                    question, answer, items, claims)
                repair_count = 1
                _merge_usage(usage, repair_usage)
                final_answer = repaired or answer
                timings["repair"] = round(time.monotonic() - repair_started, 3)
            except Exception as exc:  # noqa: BLE001 - 修订失败保留原答案
                timings["repair"] = round(time.monotonic() - repair_started, 3)
                error = f"repair_failed: {type(exc).__name__}: {exc}"[:1000]
            if final_answer != answer:
                verify_started = time.monotonic()
                try:
                    rechecked, recheck_usage = await self._check(
                        question, final_answer, items)
                    timings["reverify"] = round(time.monotonic() - verify_started, 3)
                    _merge_usage(usage, recheck_usage)
                    claims = [
                        claim.model_copy(update={"repaired": True})
                        for claim in rechecked
                    ]
                    issues = _count_issues(claims)
                    status = "succeeded" if _is_clean(issues) else "partial"
                except Exception as exc:  # noqa: BLE001 - 修订结果仍可展示
                    timings["reverify"] = round(time.monotonic() - verify_started, 3)
                    error = f"reverify_failed: {type(exc).__name__}: {exc}"[:1000]
                    status = "partial"
        timings["total"] = round(time.monotonic() - started, 3)
        return VerificationResult(
            status=status,
            claims=[_claim_payload(claim) for claim in claims],
            issues=issues,
            original_answer=answer,
            final_answer=final_answer,
            repair_count=repair_count,
            timings=timings,
            usage=usage,
            error=error,
        )

    async def _check(
        self, question: str, answer: str, items: list[dict]
    ) -> tuple[list[ClaimCheck], dict]:
        prompt = _verify_prompt(question, answer, items, self._max_claims)
        last_error: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                content, usage = await self._invoke([
                    ("system", VERIFY_PROMPT), ("human", prompt)])
                payload = ClaimResponse.model_validate_json(_extract_json(content))
                claims = self._validate_claims(payload.claims, len(items))
                return claims, usage
            except (ValueError, ValidationError, json.JSONDecodeError) as exc:
                last_error = exc
                prompt = _verify_prompt(
                    question, answer, items, self._max_claims,
                    retry_note=f"The previous reply was not valid JSON: {exc}. "
                               "Return valid JSON only.")
            except Exception as exc:  # noqa: BLE001 - 端点错误按同一上限重试
                last_error = exc
        raise VerificationUnavailable(
            f"{type(last_error).__name__}: {last_error}" if last_error else "unknown")

    def _validate_claims(
        self, claims: list[ClaimCheck], evidence_count: int
    ) -> list[ClaimCheck]:
        valid: list[ClaimCheck] = []
        for claim in claims[: self._max_claims]:
            text = claim.claim.strip()
            if not text or _is_evidence_meta(text):
                continue
            citations = [n for n in claim.citations if 1 <= n <= evidence_count]
            claim = claim.model_copy(update={"claim": text, "citations": citations})
            if claim.verdict != "uncited" and not citations:
                claim = claim.model_copy(update={
                    "verdict": "uncited",
                    "reason": claim.reason or "引用编号缺失或越界",
                })
            elif claim.verdict == "uncited" and citations:
                claim = claim.model_copy(update={"verdict": "supported"})
            valid.append(claim)
        return valid

    async def _repair(
        self, question: str, answer: str, items: list[dict],
        claims: list[ClaimCheck],
    ) -> tuple[str, dict]:
        flagged = [claim for claim in claims if claim.verdict != "supported"]
        lines = [
            f"- {claim.claim} | citations={claim.citations} | "
            f"verdict={claim.verdict} | reason={claim.reason}"
            for claim in flagged
        ]
        prompt = (
            f"Question: {question}\n\n"
            f"Original answer:\n{answer}\n\n"
            "Claims that need repair:\n" + "\n".join(lines) +
            f"\n\nNumbered evidence:\n{_render_items(items)}"
        )
        last_error: Exception | None = None
        for _ in range(self._attempts):
            try:
                content, usage = await self._invoke([
                    ("system", REPAIR_PROMPT), ("human", prompt)])
                text = (content or "").strip()
                if text:
                    return text, usage
                last_error = ValueError("empty repair")
            except Exception as exc:  # noqa: BLE001 - 失败保留原答案
                last_error = exc
        raise VerificationUnavailable(
            f"{type(last_error).__name__}: {last_error}" if last_error else "unknown")

    async def _invoke(self, messages) -> tuple[str, dict]:
        message = await asyncio.wait_for(
            self._llm.ainvoke(messages), timeout=self._timeout)
        usage = dict(getattr(message, "usage_metadata", None) or {})
        return message.content or "", usage


def _verify_prompt(
    question: str, answer: str, items: list[dict], max_claims: int,
    retry_note: str = "",
) -> str:
    note = f"\n\n{retry_note}" if retry_note else ""
    return (
        f"Question: {question}\n\n"
        f"Answer:\n{answer}\n\n"
        f"Numbered evidence:\n{_render_items(items)}\n\n"
        f"Extract at most {max_claims} factual claims that matter for the answer. "
        "For each claim, list the evidence numbers it relies on and judge support:\n"
        "- supported: the cited evidence states the claim, including its numbers, "
        "conditions and objects.\n"
        "- partial: the evidence supports part of the claim but a condition, number "
        "or object differs or is missing.\n"
        "- unsupported: the cited evidence does not support the claim or contradicts it.\n"
        "- uncited: the claim is factual and has no citation.\n"
        "Ignore purely stylistic or meta sentences. In particular, ignore statements "
        "about the evidence itself: what the evidence does or does not contain, which "
        "source is missing, or what cannot be verified from the provided evidence. "
        "Those are descriptions of the evidence, not factual claims about the world. "
        "If the answer only says the evidence is insufficient, return an empty claims list.\n"
        'Return exactly: {"claims":[{"claim":"...","citations":[1],'
        '"verdict":"supported","reason":"..."}]}'
        f"{note}"
    )


def _render_items(items: list[dict]) -> str:
    parts: list[str] = []
    for item in items:
        header = str(item.get("header") or f"[{item.get('rank', 0)}]")
        text = str(item.get("text") or "").strip()
        parts.append(f"{header}\n{text}")
    return "\n\n".join(parts)


def _extract_json(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:] if lines else []
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in reply")
    return cleaned[start:end + 1]


def _is_evidence_meta(text: str) -> bool:
    """判断一句话是否只是在描述证据集合本身，而不是关于事实的结论。

    模型偶尔会把"证据里没有某篇报道"当成待核验结论。这类句子没有可引用
    的原文，也不属于回答内容，留在核验里会制造假的引用问题。
    """
    lowered = text.lower()
    markers = (
        "in the evidence", "in the provided evidence", "among the provided evidence",
        "evidence does not", "evidence lacks", "evidence only contains",
        "not included in the evidence", "not among the provided",
        "cannot be verified from", "cannot be determined from the evidence",
    )
    return any(marker in lowered for marker in markers)


def _count_issues(claims: list[ClaimCheck]) -> dict:
    counts = {"supported": 0, "partial": 0, "unsupported": 0, "uncited": 0}
    for claim in claims:
        counts[claim.verdict] = counts.get(claim.verdict, 0) + 1
    counts["total"] = len(claims)
    return counts


def _is_clean(issues: dict) -> bool:
    return not (issues.get("partial") or issues.get("unsupported")
                or issues.get("uncited"))


def _claim_payload(claim: ClaimCheck) -> dict:
    return {
        "claim": claim.claim,
        "citations": claim.citations,
        "verdict": claim.verdict,
        "reason": claim.reason,
        "repaired": claim.repaired,
    }


def _merge_usage(total: dict, usage: dict) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, (int, float)):
            total[key] = total.get(key, 0) + int(value)
