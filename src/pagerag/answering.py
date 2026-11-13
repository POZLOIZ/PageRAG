"""回答层：检索一次，生成带引用编号的回答。

证据不足时必须明确说明缺什么，不允许编造。引用编号与本次检索证据一一对应，
随 QueryRun 落库。实际发送给模型的证据和渲染头整体快照进
`QueryRun.evidence_snapshot`，评分与回读都以它为准，不能事后重新组装。

`dense` 和 `hybrid-rrf` 使用平铺截取的上下文。`hybrid-rerank` 按证据单元组装：
按重排名次取块，在断句处补邻块，跨单元去重，并在 token 预算内填充。提示和
渲染头保持语言中性，回答语言跟随问题。

连续追问可以带轻量历史。系统先用一次短调用把指代和省略消解成独立问题，再走
同一条检索与生成链路。原问题保留在 `QueryRun.question`，改写结果落在
`resolved_question`，本次使用的对话摘要落在 `history`。改写只用于检索，生成
提示仍然回答用户原话，并带最近两轮对话，避免把"太复杂了，简单说"这类反馈
本身当成要检索的知识点。改写失败退回原问题，不做意图分类、长期记忆或多轮规划。

查询分解默认关闭。子查询只作为追加召回通道交给检索层，合并候选后仍按原问题
重排；失败或不可拆时行为与不开启一致。实际使用的子查询随证据快照落盘。
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag.evidence import EvidenceUnit, UnitChunk, count_tokens, select_units
from pagerag.llm import build_chat_llm, chunk_text
from pagerag.models import AnswerCheck, Chunk, Citation, IndexVersion, QueryRun
from pagerag.retrieval import Evidence, IndexRef, RetrievalService, SearchResult
from pagerag.settings import Settings
from pagerag.verification import AnswerVerifier, VerificationResult

# 证据渲染规则版本：v3 使用中性渲染头并附带结构上下文，章节路径和表头取自
# 切块时记录的 Chunk.context，与正文分开保存。引用仍指向原文区间。
EVIDENCE_RENDER_VERSION = "evidence-head-v3"

# 中性系统提示：指令用英文写（对中英问题同样稳定），回答语言由第 4 条跟随问题。
SYSTEM_PROMPT = """You answer the current question using only the numbered evidence provided.
1. When you use information from the evidence, end the sentence with its number, e.g. [1].
2. If the evidence does not contain enough information to answer, say so directly and state what is missing. Do not guess or fabricate.
3. Be precise about conditions, numbers and names; when evidence items disagree, cite each source separately.
4. Answer in the same language as the current question.
5. The current question may refer to the previous conversation, for example by asking what your last answer meant or by asking for a simpler explanation. Use the conversation only to identify what the user refers to. The previous assistant answer is not evidence: every factual claim still needs a numbered citation from the evidence.
6. Sub-queries, when present, are only for retrieving evidence. They are not separate questions and may omit the user's intent."""

# 连续追问只做一次有界的指代和省略消解，把追问改写成可独立检索的问题，同时
# 保留原问题落库。反馈类追问要改写成上一轮的主题，不能把"太复杂""啥意思"
# 本身当成知识点。
RESOLVE_PROMPT = """You rewrite a follow-up question into a standalone search query.
Use the conversation only to resolve pronouns and omitted references.
Keep the user's language and all names, dates and numbers from the follow-up.
If the follow-up comments on the previous answer, for example by asking what it
meant, saying it is too complicated, or asking for a simpler explanation,
rewrite it as a query for the topic being discussed. Do not turn the comment
itself into search keywords.
Do not answer the question. Output only the rewritten query on one line,
without quotes or explanation."""

# 查询分解：比较、推理和时间问题里的次要实体常被压进同一个查询，导致相关
# 文档进不了候选。这里只负责拆出可独立检索的子查询，召回与选择仍在 retrieval
# 层完成，合并候选后按原问题重排。语言跟随问题，判断是否拆分完全由模型看
# 问题文本决定，不读取题目标签。
DECOMPOSE_PROMPT = """You split a question into standalone search queries.
Split only when the question compares several entities or needs several distinct
facts or conditions; then output one query per entity, aspect or condition, each
searchable on its own. Otherwise output an empty list.
Keep the question's language and all names, dates and numbers.
Do not answer the question. Output only a JSON array of at most 3 strings,
no explanation."""

# 子查询长度上限：与追问改写同量级；超过说明模型在复述问题或写句子，
# 不是可独立检索的短查询。
DECOMPOSE_QUERY_MAX_CHARS = 200
# 分解调用上限（秒）：短提示通常几秒内返回，端点排队时可能明显变慢。超过
# 时限按失败处理并退回单查询，不让一次挂起的调用拖住整题。
DECOMPOSE_TIMEOUT_SECONDS = 60.0

# 对话摘要上限：最多 6 轮，答案各截 500 字符。改写调用是短提示，成本有界；
# 超出的旧轮次不参与指代消解（更早的上下文对单个追问通常已无必要）。
HISTORY_MAX_TURNS = 6
HISTORY_ANSWER_CHARS = 500
HISTORY_QUESTION_CHARS = 500

# 生成提示只带最近两轮对话：用户常用“啥意思”“太复杂了”指代上一轮回答。
# 更早的轮次对一次追问帮助有限，全部塞进提示会挤占证据预算。
PROMPT_HISTORY_TURNS = 2


def _prompt_history(digest: list[dict]) -> str:
    """把最近两轮对话排成生成提示里的上下文，明确区分用户和助手。

    上一轮回答里的引用编号是上一轮的证据编号，直接带进新提示会诱导模型
    复用错误编号；这里只保留文字，本次引用必须重新从当前证据选择。
    """
    lines: list[str] = []
    for turn in digest[-PROMPT_HISTORY_TURNS:]:
        question = str(turn.get("question") or "").strip()
        answer = re.sub(r"\[\d+\]", "", str(turn.get("answer") or "")).strip()
        if not question:
            continue
        lines.append(f"User: {question}")
        if answer:
            lines.append(f"Assistant: {answer}")
    return "\n".join(lines)


@dataclass
class PreparedAnswer:
    """检索与上下文组装完成后的中间结果。生成、核验与落引用共用这一份，
    避免普通请求与流式请求各走一套组装逻辑。"""

    question: str
    resolved: str
    digest: list[dict]
    timings: dict[str, float]
    result: SearchResult
    subqueries: list[str]
    selected: list[Evidence] = field(default_factory=list)
    units: list[EvidenceUnit] = field(default_factory=list)
    context: str = ""
    items: list[dict] = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)
    refusal_reason: str | None = None

    @property
    def refused(self) -> bool:
        return self.refusal_reason is not None

    def prompt(self) -> str:
        """生成提示回答用户原话，不用改写后的检索问题替代它。

        改写问题只负责召回，可能把“解释过于复杂”这类反馈压成关键词；如果
        直接拿它当问题，模型就会去解释这句话本身，而不是简化上一轮回答。
        """
        parts: list[str] = []
        history = _prompt_history(self.digest)
        if history:
            parts.append("Conversation so far:\n" + history)
        parts.append(f"Current question: {self.question}")
        if self.subqueries:
            lines = "\n".join(
                f"Q{i}: {query}" for i, query in enumerate(self.subqueries, start=1))
            parts.append("Sub-queries used for retrieval:\n" + lines)
        parts.append("Numbered evidence:\n" + self.context)
        return "\n\n".join(parts)


def _context_parts(context: dict | None) -> list[str]:
    """结构上下文 → 渲染头字段（context 与正文分开保存，只进头部，
    不进引用区间）。章节路径用 > 连接各级标题；表头取表格首行并把内部竖线
    压成 /（避免与字段分隔符 | 混淆），截断 60 字符。值全部来自语料，连接
    只用符号，保持头部语言中性。"""
    if not context:
        return []
    parts: list[str] = []
    sections = [s.lstrip("#").strip() for s in (context.get("sections") or []) if s]
    if sections:
        parts.append(" > ".join(sections))
    table_header = str(context.get("table_header") or "").strip()
    if table_header:
        # 去掉首尾竖线（markdown 表格外框），内部竖线换成 /，避免与字段分隔符混淆。
        cells = table_header.strip("|").replace("|", "/")
        parts.append(cells.strip()[:60])
    return parts


def _header(number: int, title: str, source: str, published_at: str,
            line_start: int, line_end: int, page: int | None,
            context: dict | None = None) -> str:
    # 渲染头 v3：v2 的语料字段（标题/来源/日期/行号/页码）之上，插入切块时
    # 记录的结构上下文（章节路径、表头）——字段值全部来自语料自身，分隔只用
    # 符号；固定语言词（如中文"来源："）会把英文问题的回答带偏成中文。
    parts = [f"[{number}]", title]
    parts.extend(_context_parts(context))
    if source:
        parts.append(source)
    if published_at:
        parts.append(published_at[:10])
    parts.append(f"L{line_start}-{line_end}")
    if page:
        parts.append(f"P{page}")
    return " | ".join(parts)


def _unit_header_builder(by_chunk: dict[int, Evidence],
                         subqueries: list[str] | None = None):
    """单元渲染头：溯源与结构上下文取命中块所属文档/块记录，行号区间取单元
    全体成员（邻块补全可能拉宽区间）；页码用命中块的（成员跨页极少，溯源以
    命中为准）。

    有子查询时在编号后插入 Q1/Q2 标记，引用编号仍保持在最前，模型照常按
    [n] 引用；子查询原文在问题后面的 Sub-queries 段落里给出。
    """

    def build(number: int, hit: UnitChunk, members: list[UnitChunk]) -> str:
        item = by_chunk[hit.chunk_id]
        header = _header(number, item.document_title, item.source_name,
                         item.published_at,
                         min(m.line_start for m in members),
                         max(m.line_end for m in members), item.page, item.context)
        if subqueries and hit.source_subquery:
            header = header.replace(
                f"[{number}]", f"[{number}] Q{hit.source_subquery}", 1)
        return header

    return build


async def _load_neighbors(
    session: AsyncSession, hits: list[UnitChunk], index_id: int
) -> dict[tuple[int, int], UnitChunk]:
    """取命中块的前后邻块，同修订、chunk_index±1、同索引版本，供断句补全。

    命中块本身也进映射：相邻两块都被检索到时，切断的句子同样需要对方补全。
    单元组装按 chunk_id 去重，不会重复计数。

    版本过滤：重建不删旧行，旧引用要能回查，因此同一修订会累积多个版本块。
    修订在本索引有自己的标记块时，邻块只取本版本行。分块改变后旧块序号不再
    对应同一片原文，按序号兜底会把别的版本文本拼进证据。NULL 历史行只服务
    迁移前建库、整个修订都没有标记块的索引。"""
    by_key = {(h.revision_row_id, h.chunk_index): h for h in hits}
    wanted = {(rid, idx + offset)
              for rid, idx in by_key for offset in (-1, 1)} - set(by_key)
    if wanted:
        hit_revisions = {h.revision_row_id for h in hits}
        tagged_rows = (await session.execute(
            select(Chunk.revision_row_id).where(
                Chunk.index_version_id == index_id,
                Chunk.revision_row_id.in_(sorted(hit_revisions)),
            ).distinct()
        )).all()
        tagged = {row[0] for row in tagged_rows}
        legacy_ok = Chunk.index_version_id.is_(None)
        if tagged:
            legacy_ok = and_(legacy_ok, Chunk.revision_row_id.not_in(tagged))
        rows = (await session.execute(
            select(Chunk).where(
                tuple_(Chunk.revision_row_id, Chunk.chunk_index).in_(sorted(wanted)),
                or_(Chunk.index_version_id == index_id, legacy_ok),
            )
        )).scalars().all()
        for row in rows:
            if row.index_version_id is None and row.revision_row_id in tagged:
                continue  # SQL 过滤之外的同规则双保险
            by_key.setdefault((row.revision_row_id, row.chunk_index),
                              UnitChunk.from_source(row, False))
    return by_key


class QueryDecomposer:
    """一次有界调用把问题拆成至多 N 条子查询；任何失败都退回空表。

    子查询只是追加召回通道（retrieval 合并候选池后仍按原问题重排），因此
    解析从保守：非 JSON、非字符串数组、空串、超长或与原问题相同的项一律
    丢弃——宁可少一路召回，也不把坏查询放进检索。调用失败计入 failed
    计数器（不抛出）："模型判断不拆"与"调用失败"必须能区分——否则评估会把
    端点故障当成"分解无收益"。

    可选缓存（评测用）：键 = 问题文本，值 = 成功分解的子查询列表。只缓存
    调用成功的结果（含模型判断"不拆"的空表——调用已成功，结果可复用）；
    超时/异常不缓存，下次照常重试。同问题在探针与正式运行间复用，前提是
    分解模型与提示未变（DECOMPOSE_PROMPT 的版本随代码提交可查）。
    """

    def __init__(self, llm, max_subqueries: int = 3,
                 cache: dict[str, list[str]] | None = None,
                 cache_path: Path | None = None) -> None:
        self._llm = llm
        self._max = max(1, int(max_subqueries))
        self.failed = 0
        self.cache_hits = 0
        self._cache = cache if cache is not None else {}
        self._cache_path = cache_path

    async def decompose(self, question: str) -> list[str]:
        key = question.strip()
        cached = self._cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return list(cached)
        prompt = f"Question: {question}\n\nSub-queries (JSON array):"
        try:
            message = await asyncio.wait_for(
                self._llm.ainvoke([("system", DECOMPOSE_PROMPT), ("human", prompt)]),
                timeout=DECOMPOSE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 - 分解失败＝只用原问题（见 docstring）
            self.failed += 1
            return []
        try:
            items = json.loads((message.content or "").strip())
        except ValueError:
            return []
        if not isinstance(items, list):
            return []
        picked: list[str] = []
        seen = {question.strip().casefold()}
        for item in items:
            text = item.strip() if isinstance(item, str) else ""
            key_ = text.casefold()
            if not text or len(text) > DECOMPOSE_QUERY_MAX_CHARS or key_ in seen:
                continue
            seen.add(key_)
            picked.append(text)
            if len(picked) >= self._max:
                break
        # 调用成功才落缓存（含空表＝模型判断不拆）；失败路径在上面已提前返回。
        self._cache[key] = list(picked)
        if self._cache_path is not None:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=1),
                encoding="utf-8")
        return picked


class AnsweringService:
    def __init__(self, settings: Settings, retrieval: RetrievalService) -> None:
        self._settings = settings
        self._retrieval = retrieval
        # 生成模型客户端。配置主通道时启用双通道备援。
        self._llm = build_chat_llm(settings)
        # 可选的子查询缓存，文件存在则预载，新的分解结果逐次追加。
        cache_path = (Path(settings.decomposition_cache)
                      if settings.decomposition_cache else None)
        cache = None
        if cache_path is not None and cache_path.exists():
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = {k: list(v) for k, v in loaded.items()}
        self._decomposer = QueryDecomposer(
            self._llm, settings.query_decomposition_max_subqueries,
            cache=cache, cache_path=cache_path)
        # 引用核验：默认开启，失败只记 unavailable，不影响回答。
        self._verifier = AnswerVerifier(settings, self._llm)

    async def answer(
        self, session: AsyncSession, run: QueryRun, question: str,
        history: list[dict] | None = None,
        indexes: list[IndexRef] | None = None,
    ) -> QueryRun:
        """一次性完成检索、生成、核验与引用落库。"""
        prepared = await self._prepare(session, run, question, history, indexes)
        if prepared.refused:
            return await self._complete_refusal(session, run, prepared)
        started = time.monotonic()
        message = await self._llm.ainvoke(
            [("system", SYSTEM_PROMPT), ("human", prepared.prompt())])
        prepared.timings["generate"] = time.monotonic() - started
        prepared.timings["llm_endpoint"] = self._llm.last_endpoint
        prepared.timings["llm_attempts"] = getattr(self._llm, "last_attempts", 1)
        run.status = "succeeded"
        run.answer = message.content
        run.timings = prepared.timings
        usage = getattr(message, "usage_metadata", None)
        run.usage = dict(usage) if usage else {}
        await self._verify_answer(session, run, prepared.question, prepared.items)
        await self._add_citations(session, run, prepared)
        await session.flush()
        return run

    async def stream_answer(
        self, session: AsyncSession, run: QueryRun, question: str,
        history: list[dict] | None = None,
        indexes: list[IndexRef] | None = None,
    ):
        """流式完成同一链路，事件为 status / delta。

        服务端仍先完成检索与证据组装，再开始生成；生成期间逐段送出正文。
        回答结束后照常做引用核验与引用落库，最终结果由调用方查询数据库。
        """
        yield {"event": "status", "data": {"stage": "retrieving"}}
        prepared = await self._prepare(session, run, question, history, indexes)
        if prepared.refused:
            yield {"event": "status", "data": {
                "stage": "generating", "insufficient": True,
                "reason": prepared.refusal_reason}}
            async for piece in self._stream_generate(run, prepared):
                yield {"event": "delta", "data": {"text": piece}}
            run.status = "succeeded"
            run.insufficient = True
            run.failure_reason = prepared.refusal_reason
            run.evidence_snapshot = prepared.snapshot
            await session.flush()
            yield {"event": "status", "data": {"stage": "done", "insufficient": True}}
            return
        yield {"event": "status", "data": {"stage": "generating"}}
        async for piece in self._stream_generate(run, prepared):
            yield {"event": "delta", "data": {"text": piece}}
        run.status = "succeeded"
        if self._settings.answer_verification_enabled:
            yield {"event": "status", "data": {"stage": "verifying"}}
        await self._verify_answer(session, run, prepared.question, prepared.items)
        await self._add_citations(session, run, prepared)
        await session.flush()
        yield {"event": "status", "data": {"stage": "done"}}

    async def _prepare(
        self, session: AsyncSession, run: QueryRun, question: str,
        history: list[dict] | None = None,
        indexes: list[IndexRef] | None = None,
    ) -> PreparedAnswer:
        # 只取检索需要的三列（见 retrieval.IndexRef）：revisions 映射在全量语料
        # 下是一行十 MB 的 JSON，整行载入只为了 id/版本号/集合名是纯浪费。
        row = (await session.execute(
            select(IndexVersion.id, IndexVersion.version, IndexVersion.milvus_collection)
            .where(IndexVersion.id == run.index_version_id)
        )).one()
        index = IndexRef(id=row.id, version=row.version,
                         milvus_collection=row.milvus_collection)
        # 连续追问：先做一次有界改写，检索与生成都用独立问题；原问题留在
        # run.question 供回显。改写失败或输出不像话时退回原问题，不阻断回答。
        digest = self._history_digest(history)
        run.history = digest or None
        timings: dict[str, float] = {}
        started = time.monotonic()
        resolved = await self._resolve_question(question, digest) if digest else question
        if digest:
            timings["resolve"] = time.monotonic() - started
        run.resolved_question = resolved if resolved != question else None
        # 查询分解（默认关）：拆的是检索用的独立问题（追问场景下已是 resolved），
        # 失败/不拆得到空表，检索行为与不开启时完全一致。
        subqueries: list[str] = []
        if self._settings.query_decomposition_enabled:
            started = time.monotonic()
            subqueries = await self._decomposer.decompose(resolved)
            timings["decompose"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        federated = bool(indexes and len(indexes) > 1)
        if federated:
            # 跨集合：各集合先召回，合并候选后统一重排；子查询通道暂不叠加，
            # 保持一次嵌入一次重排的成本边界。
            result = await self._retrieval.search_collections(
                session, indexes or [], resolved)
        else:
            result = await self._retrieval.search(session, index, resolved,
                                                  subqueries=subqueries)
        timings.update(result.stage_timings)
        snapshot_extra = {
            "subqueries": subqueries,
            "retrieval_status": result.status,
            # 历史分数来自哪套排序：dense 是余弦相似度，hybrid-rrf 是名次
            # 融合分，hybrid-rerank 是重排分。页面据此解释分数，不混用。
            "retrieval_strategy": self._settings.retrieval_strategy,
            "retrieval_errors": result.errors,
            "scope": {
                "mode": "federated" if federated else "collection",
                "indexes": [
                    {"index_id": item.id, "version": item.version}
                    for item in (indexes or [index])
                ],
            },
        }
        if not result.evidence:
            reason = "retrieval_empty" if result.status == "empty" else result.status
            snapshot = {
                "render_version": EVIDENCE_RENDER_VERSION,
                "context": "", "items": [], "candidates": [], "pool": result.pool,
                **snapshot_extra,
            }
            run.evidence_snapshot = snapshot
            return PreparedAnswer(question=question, resolved=resolved, digest=digest,
                                  timings=timings, result=result,
                                  subqueries=subqueries, snapshot=snapshot,
                                  refusal_reason=reason)
        # 所有检索策略共用同一套上下文组装：窗口和断句补全、每篇配额、
        # 单元数与 token 预算都含渲染头。策略差异只在上游的候选与重排，
        # 这样比较检索策略时不会混入上下文预算的差异。
        selected, context, units = await self._assemble_units(
            session, result, index, subqueries)
        if not selected:
            # 预算内放不下任何单元：如实记为不足，不静默回退平铺路径。
            snapshot = {
                "render_version": EVIDENCE_RENDER_VERSION,
                "context": "", "items": [], "candidates": [], "pool": result.pool,
                **snapshot_extra,
            }
            run.evidence_snapshot = snapshot
            return PreparedAnswer(question=question, resolved=resolved, digest=digest,
                                  timings=timings, result=result,
                                  subqueries=subqueries, snapshot=snapshot,
                                  refusal_reason="context_budget_empty")
        # 快照在生成前固化：之后无论 meta/渲染规则怎么变，这次回答的依据可复查。
        # 三个检查点齐全：pool（融合前两路名次）→ candidates（重排后全量候选）
        # → items（最终进入上下文的单元/块）。
        items = [
            {"rank": i, "chunk_id": item.chunk_id,
             "revision_id": item.revision_id,
             "header": unit.header, "text": unit.text,
             "source_subquery": item.source_subquery,
             "completion_chunk_ids": unit.completion_chunk_ids}
            for i, (item, unit) in enumerate(zip(selected, units), start=1)
        ]
        snapshot = {
            "render_version": EVIDENCE_RENDER_VERSION,
            "context": context, "items": items,
            "candidates": [
                {"rank": item.rank, "chunk_id": item.chunk_id,
                 "revision_id": item.revision_id, "score": item.score,
                 "document_title": item.document_title}
                for item in result.evidence
            ],
            # 完整候选（预算截断前），供"候选召回"与"最终上下文覆盖"分开统计。
            # pool 仅 hybrid-rerank 有值：重排选择之前的两路名次快照。
            "pool": result.pool,
            # 子查询、检索状态与本次范围（单集合或跨集合）。
            **snapshot_extra,
        }
        run.evidence_snapshot = snapshot
        return PreparedAnswer(question=question, resolved=resolved, digest=digest,
                              timings=timings, result=result,
                              subqueries=subqueries, selected=selected,
                              units=units or [], context=context, items=items,
                              snapshot=snapshot)

    async def _stream_generate(self, run: QueryRun, prepared: PreparedAnswer):
        """流式生成正文，同时把完整回答与耗时写回 QueryRun。"""
        prompt = prepared.prompt()
        if prepared.refused:
            # 无证据时不带对话，避免模型拿上一轮回答当资料继续答。
            prompt = f"Current question: {prepared.question}\n\nNumbered evidence:\n(none)"
        buffer: list[str] = []
        usage = None
        started = time.monotonic()
        async for chunk in self._llm.astream(
                [("system", SYSTEM_PROMPT), ("human", prompt)]):
            text = chunk_text(chunk)
            if text:
                buffer.append(text)
                yield text
            chunk_usage = getattr(chunk, "usage_metadata", None)
            if chunk_usage:
                usage = chunk_usage
        prepared.timings["generate"] = time.monotonic() - started
        prepared.timings["llm_endpoint"] = self._llm.last_endpoint
        prepared.timings["llm_attempts"] = getattr(self._llm, "last_attempts", 1)
        run.answer = "".join(buffer)
        run.timings = prepared.timings
        run.usage = dict(usage) if usage else {}

    async def _complete_refusal(self, session: AsyncSession, run: QueryRun,
                                prepared: PreparedAnswer) -> QueryRun:
        """无证据（或预算内放不下单元）：按同一提示让模型生成"资料不足"回复，
        拒答语言随问题——不再注入固定中文短语（语言诊断泄漏源之一）。
        结构化标志（insufficient/failure_reason）照记，不依赖措辞。"""
        run.insufficient = True
        run.failure_reason = prepared.refusal_reason
        prompt = f"Current question: {prepared.question}\n\nNumbered evidence:\n(none)"
        started = time.monotonic()
        message = await self._llm.ainvoke(
            [("system", SYSTEM_PROMPT), ("human", prompt)])
        prepared.timings["generate"] = time.monotonic() - started
        prepared.timings["llm_endpoint"] = self._llm.last_endpoint
        prepared.timings["llm_attempts"] = getattr(self._llm, "last_attempts", 1)
        run.status = "succeeded"
        run.answer = message.content
        run.timings = prepared.timings
        usage = getattr(message, "usage_metadata", None)
        run.usage = dict(usage) if usage else {}
        run.evidence_snapshot = prepared.snapshot
        return run

    async def _add_citations(self, session: AsyncSession, run: QueryRun,
                             prepared: PreparedAnswer) -> None:
        """引用与最终上下文一致：平铺 = 选中块；单元 = 命中块代表单元（rank 即
        单元号），补全块以同 rank 一并落 Citation——评分的"最终上下文"以
        模型实际所见为准，补全块同样是模型看过的正文。"""
        for i, item in enumerate(prepared.selected, start=1):
            session.add(Citation(
                query_run_id=run.id, rank=i, chunk_id=item.chunk_id, score=item.score
            ))
        for i, unit in enumerate(prepared.units or [], start=1):
            for chunk_id in unit.completion_chunk_ids:
                session.add(Citation(
                    query_run_id=run.id, rank=i, chunk_id=chunk_id, score=0.0
                ))
        await session.flush()

    async def _verify_answer(
        self, session: AsyncSession, run: QueryRun, question: str,
        items: list[dict],
    ) -> None:
        """核验当前回答并落 answer_checks；任何异常都不外抛。"""
        if not self._settings.answer_verification_enabled:
            return
        try:
            result = await self._verifier.run(
                question, run.answer or "", items, insufficient=run.insufficient)
        except Exception as exc:  # noqa: BLE001 - 核验失败不能拖垮回答
            result = VerificationResult(
                status="unavailable",
                original_answer=run.answer or "",
                final_answer=run.answer or "",
                issues={"supported": 0, "partial": 0, "unsupported": 0,
                        "uncited": 0, "total": 0},
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        timings = dict(run.timings or {})
        for key, value in result.timings.items():
            if isinstance(value, (int, float)):
                timings[f"verify_{key}"] = value
        run.timings = timings
        usage = dict(run.usage or {})
        for key, value in result.usage.items():
            if isinstance(value, (int, float)):
                usage[f"verify_{key}"] = int(value)
        run.usage = usage
        if result.final_answer:
            run.answer = result.final_answer
        existing = (await session.execute(
            select(AnswerCheck).where(AnswerCheck.query_run_id == run.id)
        )).scalar_one_or_none()
        check = existing or AnswerCheck(query_run_id=run.id)
        check.status = result.status
        check.claims = result.claims
        check.issues = result.issues
        check.original_answer = result.original_answer
        check.final_answer = result.final_answer
        check.repair_count = result.repair_count
        check.timings = result.timings
        check.usage = result.usage
        check.error = result.error
        if existing is None:
            session.add(check)
        await session.flush()

    def _history_digest(self, history: list[dict] | None) -> list[dict]:
        """把客户端历史截成可落库、可发送的有界摘要（最近 6 轮）。

        只保留问题与非空答案；答案截 500 字符——追问消解只需要实体与话题，
        不需要完整上一轮回答。返回值同时写进 QueryRun.history，便于复查这次
        回答实际用了哪段对话。"""
        digest: list[dict] = []
        for turn in (history or [])[-HISTORY_MAX_TURNS:]:
            if not isinstance(turn, dict):
                continue
            question = str(turn.get("question") or "").strip()
            answer = str(turn.get("answer") or "").strip()
            if not question:
                continue
            digest.append({
                "question": question[:HISTORY_QUESTION_CHARS],
                "answer": answer[:HISTORY_ANSWER_CHARS],
            })
        return digest

    async def _resolve_question(self, question: str, digest: list[dict]) -> str:
        """一次短调用把追问改写成独立检索问题；失败/空/多行/过长都退回原句。

        这是增益步骤不是必需步骤：改写模型不可用或输出不合格时，行为与没有
        历史时一致，回答仍会基于原问题检索。"""
        lines: list[str] = []
        for turn in digest:
            lines.append(f"Q: {turn['question']}")
            if turn["answer"]:
                lines.append(f"A: {turn['answer']}")
        prompt = ("Conversation:\n" + "\n".join(lines)
                  + f"\n\nFollow-up question: {question}\n\nStandalone search query:")
        try:
            message = await self._llm.ainvoke(
                [("system", RESOLVE_PROMPT), ("human", prompt)])
        except Exception:  # noqa: BLE001 - 改写失败退回原问题（见 docstring）
            return question
        resolved = (message.content or "").strip().strip('"').strip()
        if not resolved or "\n" in resolved or len(resolved) > 500:
            return question
        return resolved

    async def _assemble_units(
        self, session: AsyncSession, result, index: IndexRef,
        subqueries: list[str] | None = None,
    ) -> tuple[list[Evidence], str, list[EvidenceUnit]]:
        """hybrid-rerank 的上下文组装：重排名次 → 断句补邻块 → 跨单元去重 →
        token 预算（含渲染头）内填充。返回 (命中块列表, 上下文, 单元列表)。

        跨集合时证据来自不同索引版本，邻块按证据自己的 index_id 分组取，
        不能拿主集合的版本号去匹配其他集合的块。
        """
        hits = [UnitChunk.from_source(e, is_hit=True) for e in result.evidence]
        neighbors: dict[tuple[int, int], UnitChunk] = {}
        grouped: dict[int, list[UnitChunk]] = {}
        for evidence, hit in zip(result.evidence, hits):
            grouped.setdefault(evidence.index_id or index.id, []).append(hit)
        for index_id, group in grouped.items():
            neighbors.update(await _load_neighbors(session, group, index_id))
        by_chunk = {e.chunk_id: e for e in result.evidence}
        units = select_units(
            hits, neighbors,
            unit_limit=self._settings.evidence_unit_limit,
            token_budget=self._settings.context_token_budget,
            header_builder=_unit_header_builder(by_chunk, subqueries),
            token_counter=count_tokens,
            document_limit=self._settings.evidence_document_limit,
            window=self._settings.evidence_context_mode == "window",
            group_limit=self._settings.evidence_subquery_group_limit)
        selected = [by_chunk[u.hit_chunk_ids[0]] for u in units if u.hit_chunk_ids]
        context = "\n\n".join(f"{u.header}\n{u.text}" for u in units)
        return selected, context, units
