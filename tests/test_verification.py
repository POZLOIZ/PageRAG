"""引用核验单元测试：解析、拒答跳过、超时、非法 JSON、修订后再核验。"""

import asyncio
from types import SimpleNamespace

import pytest

from pagerag.verification import AnswerVerifier


def _settings(repair: bool = False, timeout: float = 1.0) -> SimpleNamespace:
    return SimpleNamespace(
        answer_verification_timeout_seconds=timeout,
        answer_verification_max_claims=12,
        answer_verification_repair_enabled=repair,
    )


class ScriptedLLM:
    def __init__(self, replies: list, delay: float = 0.0) -> None:
        self.replies = replies
        self.delay = delay
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply,
                               usage_metadata={"input_tokens": 10, "output_tokens": 5})


def _items(count: int = 1) -> list[dict]:
    return [{"header": f"[{index}] 文档", "text": f"证据正文 {index}"}
            for index in range(1, count + 1)]


def _claim(verdict: str, citations: list[int] | None = None,
           claim: str = "结论") -> dict:
    return {"claims": [{"claim": claim,
                        "citations": citations if citations is not None else [1],
                        "verdict": verdict, "reason": "理由"}]}


@pytest.mark.asyncio
async def test_supported_claim_parses_and_skips_repair():
    llm = ScriptedLLM(['{"claims":[{"claim":"期限是三年","citations":[1],'
                       '"verdict":"supported","reason":"原文写明"}]}'])
    verifier = AnswerVerifier(_settings(), llm)
    result = await verifier.run("期限多久", "期限是三年 [1]", _items())
    assert result.status == "succeeded"
    assert result.issues["supported"] == 1
    assert result.repair_count == 0
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_code_fence_reply_is_parsed():
    reply = ('```json\n{"claims":[{"claim":"结论","citations":[1],'
             '"verdict":"supported"}]}\n```')
    verifier = AnswerVerifier(_settings(), ScriptedLLM([reply]))
    result = await verifier.run("问题", "回答 [1]", _items())
    assert result.status == "succeeded"
    assert result.claims[0]["verdict"] == "supported"


@pytest.mark.asyncio
async def test_invalid_json_retries_then_unavailable():
    llm = ScriptedLLM(["这不是 JSON", "仍然不是"])
    verifier = AnswerVerifier(_settings(), llm)
    result = await verifier.run("问题", "回答 [1]", _items())
    assert result.status == "unavailable"
    assert llm.calls == 2
    assert result.error
    assert result.final_answer == "回答 [1]"


@pytest.mark.asyncio
async def test_timeout_marks_unavailable_without_changing_answer():
    llm = ScriptedLLM([_claim("supported")], delay=0.2)
    verifier = AnswerVerifier(_settings(timeout=0.02), llm)
    result = await verifier.run("问题", "原回答 [1]", _items())
    assert result.status == "unavailable"
    assert result.final_answer == "原回答 [1]"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_unsupported_triggers_repair_and_reverify():
    llm = ScriptedLLM([
        '{"claims":[{"claim":"错误结论","citations":[1],'
        '"verdict":"unsupported","reason":"原文不支持"}]}',
        "修订后的回答 [1]",
        '{"claims":[{"claim":"修订后的结论","citations":[1],'
        '"verdict":"supported","reason":"原文支持"}]}',
    ])
    verifier = AnswerVerifier(_settings(repair=True), llm)
    result = await verifier.run("问题", "错误回答 [1]", _items())
    assert result.status == "succeeded"
    assert result.repair_count == 1
    assert result.final_answer == "修订后的回答 [1]"
    assert result.issues["supported"] == 1
    assert result.claims[0]["repaired"] is True
    assert llm.calls == 3


@pytest.mark.asyncio
async def test_missing_citation_becomes_uncited():
    llm = ScriptedLLM([json_reply(_claim("supported", citations=[]))])
    verifier = AnswerVerifier(_settings(), llm)
    result = await verifier.run("问题", "没有标注的回答", _items())
    assert result.status == "partial"
    assert result.issues["uncited"] == 1


@pytest.mark.asyncio
async def test_out_of_range_citation_becomes_uncited():
    llm = ScriptedLLM([json_reply(_claim("supported", citations=[9]))])
    verifier = AnswerVerifier(_settings(), llm)
    result = await verifier.run("问题", "回答 [9]", _items(1))
    assert result.issues["uncited"] == 1
    assert result.claims[0]["citations"] == []


@pytest.mark.asyncio
async def test_evidence_meta_claim_is_ignored():
    reply = json_reply({"claims": [
        {"claim": "No Cnbc article in the evidence discusses the board plan",
         "citations": [], "verdict": "uncited", "reason": "描述证据集合"},
        {"claim": "期限是三年", "citations": [1], "verdict": "supported",
         "reason": "原文写明"},
    ]})
    verifier = AnswerVerifier(_settings(), ScriptedLLM([reply]))
    result = await verifier.run("问题", "回答 [1]", _items())
    assert result.status == "succeeded"
    assert result.issues["total"] == 1
    assert result.claims[0]["claim"] == "期限是三年"


@pytest.mark.asyncio
async def test_insufficient_skips_model_call():
    llm = ScriptedLLM([_claim("supported")])
    verifier = AnswerVerifier(_settings(), llm)
    result = await verifier.run("问题", "资料不足", _items(), insufficient=True)
    assert result.status == "skipped"
    assert llm.calls == 0
    assert result.final_answer == "资料不足"


def json_reply(payload: dict) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False)
