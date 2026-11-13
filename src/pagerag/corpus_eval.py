"""语料级评测：一个语料版本建一次检索库，全部题目复用（替代逐题建集合的首轮入口）。

评分只做确定性检查：证据文档命中（以进入最终上下文的引用为准）、标注事实的
字面覆盖、资料不足题的拒答判断。语义判分（Ragas）另行接入，不在本模块冒充。

语料适配目前支持 MultiHop-RAG（corpus.json + MultiHopRAG.json）。
OmniEval 整理版的全量接入在金融索引阶段补充流式适配。
"""

import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from pagerag.answering import EVIDENCE_RENDER_VERSION, AnsweringService
from pagerag.db import Database
from pagerag.embeddings import EmbeddingClient
from pagerag.evidence import count_tokens
from pagerag.ingest import IngestService
from pagerag.llm import build_chat_llm
from pagerag.milvus_store import MilvusStore
from pagerag.models import Chunk, Citation, Collection, Document, IndexVersion, QueryRun, Revision
from pagerag.retrieval import RetrievalService
from pagerag.settings import CHUNKER_VERSION, PARSER_VERSION, Settings, index_build_config

_REFUSAL_MARKERS = (
    "资料中没有写", "资料中没有找到", "无法判断", "无法确认", "无法回答",
    "does not contain", "not contain", "no information", "not mentioned",
    "cannot be determined", "INSUFFICIENT",
)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def url_revision_id(url: str) -> str:
    """证据 URL 超出 revision_id 列宽（64），用确定性短哈希代替原始 URL；
    评分时证据命中按同一函数映射，标识空间仍然一致。"""
    return "u-" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass
class QuestionResult:
    row_index: int
    question_type: str
    question: str
    gold_answer: str
    answer: str | None
    refused: bool
    citations: list[dict] = field(default_factory=list)
    # 标注侧证据（URL + fact）随结果落盘：Ragas 短参考合成的完整语义参考要用，
    # 且只在标注侧取（独立于系统输出）。
    gold_evidence: list[dict] = field(default_factory=list)
    # 两个检查点分开记录：候选召回与最终上下文覆盖不能混为一谈。
    # candidates = 检索 top-k 全量候选；model_context = 预算截断后模型实际所见
    # （含渲染头）；retrieved_contexts = 最终上下文的正文与 URL（确定性覆盖计算用）。
    candidates: list[dict] = field(default_factory=list)
    model_context: list[dict] = field(default_factory=list)
    retrieved_contexts: list[dict] = field(default_factory=list)
    # 前十名候选的块正文（MRR@10 段级命中用；只取前十，避免整表正文随结果膨胀）。
    candidate_texts: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    timings: dict = field(default_factory=dict)
    error: str | None = None


class CorpusEvalRunner:
    def __init__(self, settings: Settings, database: Database, store: MilvusStore,
                 embedder: EmbeddingClient) -> None:
        self._settings = settings
        self._database = database
        self._ingest = IngestService(settings, store, embedder)
        self._answering = AnsweringService(settings, RetrievalService(store, embedder, settings))
        self._urls_by_rev: dict[str, str] = {}

    async def ensure_collection(self, corpus_path: Path, corpus_sha: str):
        """索引身份 = 语料哈希 + 构建配置。只凭语料哈希复用会在改参数后
        仍用旧索引；配置不匹配时旧集合（含其历史引用）不动，
        换带配置哈希后缀的新集合名重建；同配置才复用。"""
        entries = json.loads(corpus_path.read_text(encoding="utf-8"))
        self._urls_by_rev = {url_revision_id(e["url"]): e["url"] for e in entries}
        expected = index_build_config(self._settings)
        name = f"multihop-{corpus_sha[:8]}"
        async with self._database.sessions() as session:
            existing = (await session.execute(
                select(Collection).where(Collection.name == name)
            )).scalar_one_or_none()
            if existing is not None and await self._config_matches(session, existing, expected):
                return existing, name, 0.0, True
            if existing is not None:
                # 同语料、不同构建配置：旧集合保留作历史，用带配置后缀的集合名。
                config_tag = hashlib.sha256(
                    json.dumps(expected, sort_keys=True).encode("utf-8")
                ).hexdigest()[:8]
                name = f"{name}-{config_tag}"
                existing = (await session.execute(
                    select(Collection).where(Collection.name == name)
                )).scalar_one_or_none()
                if existing is not None and await self._config_matches(session, existing, expected):
                    return existing, name, 0.0, True
            collection = existing or await self._ingest.create_collection(session, name)
            started = time.monotonic()
            for index, entry in enumerate(entries):
                await self._ingest.add_document(
                    session, collection,
                    title=entry.get("title") or f"doc-{index}",
                    data=(entry.get("body") or "").encode("utf-8"),
                    filename=f"doc-{index:04d}.txt",
                    # revision_id 用 URL 的确定性短哈希（URL 本身超列宽）：
                    # 与题目标注 evidence_list 的 URL 经 url_revision_id 映射后同一空间。
                    revision_id=url_revision_id(entry["url"]), source_kind="news",
                    meta={"source": entry.get("source") or "",
                          "published_at": entry.get("published_at") or "",
                          "url": entry["url"]},
                )
            await session.commit()
            import_seconds = time.monotonic() - started
            started = time.monotonic()
            await self._ingest.rebuild_index(session, collection)
            await session.commit()
            return collection, name, import_seconds + time.monotonic() - started, False

    async def _config_matches(self, session, collection: Collection, expected: dict) -> bool:
        """active 索引的构建配置与当前配置完全一致才可复用（无记录按不匹配处理）。"""
        if collection.active_index_version_id is None:
            return False
        active = await session.get(IndexVersion, collection.active_index_version_id)
        return active is not None and (active.build_config or None) == expected

    async def answer_one(self, collection: Collection, row: dict) -> QuestionResult:
        result = QuestionResult(
            row_index=row["row_index"], question_type=row["question_type"],
            question=row["query"], gold_answer=row["answer"], answer=None, refused=False,
            gold_evidence=[
                {"url": item["url"], "fact": item.get("fact") or ""}
                for item in row.get("evidence_list") or []
            ],
        )
        started = time.monotonic()
        try:
            async with self._database.sessions() as session:
                collection = await session.get(Collection, collection.id)
                run = QueryRun(
                    public_id=secrets.token_hex(16), collection_id=collection.id,
                    question=row["query"], index_version_id=collection.active_index_version_id,
                )
                session.add(run)
                await session.flush()
                await self._answering.answer(session, run, row["query"])
                await session.commit()
                result.answer = run.answer
                result.usage = run.usage or {}
                result.timings = run.timings or {}
                result.citations, result.retrieved_contexts = await self._load_evidence(
                    session, run)
                snapshot = run.evidence_snapshot or {}
                result.model_context = [
                    {"rank": item["rank"], "header": item["header"], "text": item["text"],
                     "url": self._urls_by_rev.get(item["revision_id"], item["revision_id"])}
                    for item in snapshot.get("items") or []
                ]
                result.candidates = [
                    {"rank": c["rank"], "chunk_id": c.get("chunk_id"), "score": c["score"],
                     "document_title": c["document_title"],
                     "url": self._urls_by_rev.get(c["revision_id"], c["revision_id"])}
                    for c in snapshot.get("candidates") or []
                ]
                # MRR@10 只看前十名候选的段级命中：正文按块主键取回，供 _score 判段。
                top_ids = [c["chunk_id"] for c in result.candidates[:10]
                           if c["chunk_id"] is not None]
                if top_ids:
                    texts = (await session.execute(
                        select(Chunk.id, Chunk.text).where(Chunk.id.in_(top_ids))
                    )).all()
                    result.candidate_texts = {row.id: row.text for row in texts}
        except Exception as exc:  # noqa: BLE001 - 单题失败记录后继续
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        result.timings["total"] = round(time.monotonic() - started, 3)
        result.refused = self._is_refusal(result)
        result.metrics = self._score(row, result)
        return result

    async def _load_evidence(self, session, run: QueryRun) -> tuple[list[dict], list[dict]]:
        rows_ = (await session.execute(
            select(Chunk, Citation, Document, Revision)
            .join(Citation, Citation.chunk_id == Chunk.id)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Citation.query_run_id == run.id)
            .order_by(Citation.rank)
        )).all()
        citations, contexts = [], []
        for row in rows_:
            url = self._urls_by_rev.get(
                row.Revision.revision_id, row.Revision.revision_id)
            citations.append({
                "rank": row.Citation.rank, "url": url,
                "title": row.Document.title, "lines": [row.Chunk.line_start, row.Chunk.line_end],
            })
            contexts.append({"url": url, "text": row.Chunk.text})
        return citations, contexts

    def _is_refusal(self, result: QuestionResult) -> bool:
        """启发式标志，不是质量结论：英文句号不参与切分、未列出的表达会漏判，
        实测既漏判过部分作答也误判过带保留回答。
        结果只作为 refused 标志落盘；正误判断以人工标注为准。"""
        answer = (result.answer or "").strip().lower()
        first_sentence = answer.split("。", 1)[0]
        return any(marker.lower() in first_sentence for marker in _REFUSAL_MARKERS)

    def _score(self, row: dict, result: QuestionResult) -> dict:
        # 两个检查点：预算只影响最终上下文，不影响候选。
        # - candidate_doc_recall：gold 文档是否进入检索 top-k 候选（检索能力）；
        # - doc_recall：gold 文档是否进入最终上下文（候选 × 预算截断后的覆盖）。
        # retrieved_contexts / candidates 的 url 均已映射回原始 URL。
        gold_urls = {item["url"] for item in row.get("evidence_list") or []}
        gold_facts = [item["fact"] for item in row.get("evidence_list") or []]
        retrieved_urls = {item["url"] for item in result.retrieved_contexts}
        candidate_urls = {item["url"] for item in result.candidates}
        context_blob = _normalize_text(" ".join(
            item["text"] for item in result.retrieved_contexts))
        metrics: dict = {"retrieved_docs": len(retrieved_urls),
                         "candidate_docs": len(candidate_urls),
                         # 模型实际输入量：与预算同口径（tiktoken cl100k，含渲染头），
                         # 供最终比较核对两臂是否同预算、是否出现截断。
                         "context_units": len(result.model_context),
                         "context_tokens": sum(
                             count_tokens(item["header"]) + count_tokens(item["text"])
                             for item in result.model_context)}
        if row["question_type"] == "null_query":
            # 资料不足题：该拒答而拒答 = 正确；不该拒而拒 = 误拒（其他题在汇总里算）。
            metrics["correct_refusal"] = bool(result.refused)
            metrics["doc_recall"] = None
            metrics["fact_coverage"] = None
            return metrics
        if gold_urls and candidate_urls:
            metrics["candidate_doc_recall"] = round(
                len(gold_urls & candidate_urls) / len(gold_urls), 4)
        if gold_urls:
            metrics["doc_recall"] = round(
                len(gold_urls & retrieved_urls) / len(gold_urls), 4)
        if gold_facts:
            covered = sum(
                1 for fact in gold_facts
                if _normalize_text(fact) in context_blob
            )
            metrics["fact_coverage"] = round(covered / len(gold_facts), 4)
            metrics["all_facts_covered"] = covered == len(gold_facts)
            mrr = self._mrr_at_10(result, gold_facts)
            if mrr is not None:
                metrics["mrr_at_10"] = mrr
        metrics["wrong_refusal"] = bool(result.refused)
        return metrics

    def _mrr_at_10(self, result: QuestionResult, gold_facts: list[str]) -> float | None:
        """段级 MRR@10：按候选名次逐段看正文（归一化字面）是否包含任一 gold
        事实，首个命中的名次 r → 1/r，前十无命中 → 0。只衡量检索名次质量，
        与预算截断/上下文组装无关；MultiHop 才有 gold fact 标注，本 runner
        只跑 MultiHop（金融线无事实标注，不在此编造）。"""
        if not result.candidate_texts:
            return None
        for cand in result.candidates[:10]:
            text = result.candidate_texts.get(cand.get("chunk_id"))
            if not text:
                continue
            blob = _normalize_text(text)
            if any(_normalize_text(fact) in blob for fact in gold_facts):
                return round(1.0 / int(cand["rank"]), 4)
        return 0.0


def write_corpus_run(out_root: Path, suite_label: str, corpus_path: Path,
                     split_path: Path, settings: Settings, results: list[QuestionResult],
                     build_seconds: float, reused: bool,
                     index_version_id: int | None = None) -> Path:
    run_id = time.strftime("%Y%m%d-%H%M%S")
    out = out_root / run_id
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "suite": suite_label,
        "corpus": {"path": str(corpus_path), "sha256": sha256_of(corpus_path)},
        "split": {"path": str(split_path), "sha256": sha256_of(split_path)},
        "parser_version": PARSER_VERSION,
        "chunker": {"version": CHUNKER_VERSION, "chars": settings.chunk_chars,
                    "max_chars": settings.chunk_max_chars},
        "retrieval": {
            "strategy": settings.retrieval_strategy, "top_k": settings.retrieval_top_k,
            # 查询分解参数随运行落盘（默认关；开启时 results.jsonl 的
            # evidence_snapshot.subqueries 记录逐题实际使用的子查询）。
            # cache 记录缓存文件血缘：子查询复用自探针还是本次新生成。
            "query_decomposition": {
                "enabled": settings.query_decomposition_enabled,
                "max_subqueries": settings.query_decomposition_max_subqueries,
                "pool_limit": settings.decomposition_pool_limit,
                "cache": settings.decomposition_cache or None},
            **({"rrf_k": settings.rrf_k, "bm25_top_n": settings.bm25_top_n,
                "bm25_weight": settings.bm25_weight}
               if settings.retrieval_strategy == "hybrid-rrf" else {}),
            **({"bm25_top_n": settings.bm25_top_n,
                "rerank_model": settings.rerank_model,
                "rerank_pool_size": settings.rerank_pool_size,
                "context_mode": settings.evidence_context_mode,
                "neighbor_expansion": (
                    settings.evidence_neighbor_expansion
                    if settings.evidence_context_mode == "neighbor-rerank" else 0)}
               if settings.retrieval_strategy == "hybrid-rerank" else {}),
        },
        "answering": {"strategy": "G0-single", "model": settings.llm_model,
                      "evidence_unit_limit": settings.evidence_unit_limit,
                      "context_token_budget": settings.context_token_budget,
                      "evidence_document_limit": settings.evidence_document_limit,
                      "evidence_context_mode": settings.evidence_context_mode,
                      "token_counter": "tiktoken-cl100k（含渲染头）",
                      "evidence_render": EVIDENCE_RENDER_VERSION,
                      "index_version_id": index_version_id},
        # LLM 双通道：主端点失败后切换到备援端点（逐题实际
        # 端点在 results.jsonl 的 timings.llm_endpoint）。
        "llm": build_chat_llm(settings).endpoints,
        # 索引身份 = 语料哈希 + 构建配置；复用前已比对。
        "index_build_config": index_build_config(settings),
        "embedding": {"model": settings.embedding_model},
        "grader": "deterministic-v1（候选/最终上下文双检查点文档覆盖、事实字面覆盖；"
                  "refused 仅为启发式标志，正误以人工标注为准；语义判分另行落盘）",
        "revision_id_rule": "u- + sha256(url)[:16]（URL 超出列宽，证据命中按同一规则映射）",
        "index_build_seconds": round(build_seconds, 1), "index_reused": reused,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    (out / "summary.md").write_text(_summarize(results, manifest), encoding="utf-8")
    return out


def _summarize(results: list[QuestionResult], manifest: dict) -> str:
    scored = [r for r in results if r.error is None]
    errors = [r for r in results if r.error is not None]
    candidate_recall = [r.metrics["candidate_doc_recall"] for r in scored
                        if r.metrics.get("candidate_doc_recall") is not None]
    recall = [r.metrics["doc_recall"] for r in scored
              if r.metrics.get("doc_recall") is not None]
    coverage = [r.metrics["fact_coverage"] for r in scored
                if r.metrics.get("fact_coverage") is not None]
    mrr = [r.metrics["mrr_at_10"] for r in scored
           if r.metrics.get("mrr_at_10") is not None]
    full = [r for r in scored if r.metrics.get("all_facts_covered")]
    nulls = [r for r in scored if r.question_type == "null_query"]
    flagged = [r for r in scored if r.metrics.get("wrong_refusal")]

    def _mean(values: list) -> float:
        return round(sum(values) / len(values), 4) if values else None

    answering_cfg = manifest["answering"]
    if "context_token_budget" in answering_cfg:
        budget_desc = (f"{answering_cfg['evidence_unit_limit']} 单元 / "
                       f"{answering_cfg['context_token_budget']} token（含渲染头）预算")
    else:
        budget_desc = f"{answering_cfg['evidence_budget_chars']} 字符预算"

    lines = [
        f"# 语料评测 {manifest['run_id']}（{manifest['suite']}）",
        "",
        f"- 题数 {len(results)}（成功 {len(scored)}，出错 {len(errors)}）；"
        f"索引{'复用' if manifest['index_reused'] else '新建'}，"
        f"建库耗时 {manifest['index_build_seconds']}s。",
        f"- 候选文档覆盖（top-{manifest['retrieval']['top_k']} 内）：均值 "
        f"{_mean(candidate_recall)}（n={len(candidate_recall)}）—— 检索检查点",
        f"- 段级 MRR@10：均值 {_mean(mrr)}（n={len(mrr)}）—— 候选名次的 gold "
        f"事实在前十段的首次命中（与预算/组装无关）",
        f"- 最终上下文文档覆盖：均值 {_mean(recall)}（n={len(recall)}）—— "
        f"候选经 {budget_desc}截断后",
        f"- 事实覆盖率：均值 {_mean(coverage)}（n={len(coverage)}），"
        f"全覆盖题 {len(full)}/{len(coverage)}",
    ]
    if nulls:
        correct = sum(1 for r in nulls if r.metrics.get("correct_refusal"))
        lines.append(f"- 资料不足题正确拒答（规则标志）{correct}/{len(nulls)}")
    if flagged:
        lines.append(f"- 规则标记拒答（非结论，正误以人工标注为准）{len(flagged)} 题："
                     + ", ".join(str(r.row_index) for r in flagged[:10]))
    lines.append(f"- 评分器：{manifest['grader']}")
    lines.append("")
    return "\n".join(lines)
