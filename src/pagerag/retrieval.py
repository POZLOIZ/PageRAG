"""检索层：纯向量、混合 RRF 与混合重排三种策略。

策略由 `settings.retrieval_strategy` 决定。`dense` 只走向量召回；`hybrid-rrf`
对向量和关键词两路候选做 RRF 融合后截断；`hybrid-rerank` 把两路候选去重后交给
重排模型按内容选择，避免融合截断把单路中后段项挤出。

关键词通道使用与稠密检索同一个 Milvus collection 的全文检索，带中文 analyzer
和 BM25 稀疏字段，保证同版本同块集合。

查询分解默认关闭。开启后模型把比较或推理问题拆成子查询，各走一路召回，合并
候选后仍按原问题重排；子查询只补召回，排序与选择不变。

查询开始时固定索引版本；返回的证据带原文定位，供引用回读。
"""

import asyncio
import time
from dataclasses import dataclass, field

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag.bm25 import build_candidate_pool, merge_candidate_pools, rrf_fuse
from pagerag.chunking import ChunkRecord  # noqa: F401  （类型约定与 chunking 保持一致）
from pagerag.embeddings import EmbeddingClient
from pagerag.milvus_store import MilvusStore
from pagerag.models import Chunk, Document, Revision
from pagerag.rerank import RerankClient


@dataclass
class Evidence:
    chunk_id: int
    document_public_id: str
    document_title: str
    revision_id: str
    revision_row_id: int
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    line_start: int
    line_end: int
    page: int | None
    score: float
    rank: int
    content_hash: str
    # 文档级溯源（documents.meta），缺失时为空串，呈现层据此退回标题+行号。
    source_name: str = ""
    published_at: str = ""
    # 块的结构上下文（章节路径、表头）——与原文位置分开保存，渲染头用；
    # 不进 text，引用仍指 char_start/char_end 的原文区间。
    context: dict | None = None
    # 切块时记录的真实边界：窗口补全用（None＝旧行，读取侧才回退标点）。
    start_cut: bool | None = None
    end_cut: bool | None = None
    source_text: str = field(default="", repr=False)
    # 这块由哪条查询召回：0＝原问题，1..＝查询分解的子查询。仅用于证据
    # 分组与页面展示，不参与排序。
    source_subquery: int = 0
    # 块所属索引版本主键。单集合检索由调用方补，跨集合检索用它按版本
    # 取邻块与核对归属；旧数据为空时回退调用方传入的主索引。
    index_id: int | None = None


@dataclass
class IndexRef:
    """检索只需要索引版本的三列（id、版本号、集合名）。刻意不取整个 ORM 行：
    revisions 映射随语料规模增长（16.5 万篇约十 MB 一行），每题整行载入是纯浪费；
    IndexVersion 实例有同名属性，可直接传入（鸭子类型）。"""

    id: int
    version: int
    milvus_collection: str


@dataclass
class SearchResult:
    status: str  # ok | empty | partial | failed
    evidence: list[Evidence]
    index_version: int
    stage_timings: dict[str, float]
    errors: list[str]
    # hybrid-rerank：候选池的各路名次（融合前），快照与诊断用；其余策略为空。
    pool: list[dict] = field(default_factory=list)


class RetrievalService:
    def __init__(self, store: MilvusStore, embedder: EmbeddingClient, settings,
                 reranker: RerankClient | None = None) -> None:
        self._store = store
        self._embedder = embedder
        self._settings = settings
        # reranker 可注入：离线测试替身用，不改变生产行为。
        self._reranker = reranker or RerankClient(settings)

    async def search(
        self, session: AsyncSession, index: IndexRef, question: str,
        subqueries: list[str] | None = None,
    ) -> SearchResult:
        """按问题检索；subqueries 是查询分解产生的追加召回通道。

        子查询只在 hybrid-rerank 生效：其他策略没有"去重并集交给重排"的
        选择阶段，多查询各自截断会互相挤出（正是 R1 对照记录过的结构问题）。
        每条查询各走一路稠密＋关键词召回，逐查询建池后合并；重排与最终
        选择始终按原问题（question）——子查询只负责把比较对象/条件的文档
        带进候选，不参与排序。queries[0] 恒为原问题。
        """
        timings: dict[str, float] = {}
        errors: list[str] = []
        strategy = self._settings.retrieval_strategy
        queries = [question]
        if strategy == "hybrid-rerank":
            queries += [q for q in (subqueries or []) if q and q != question]

        started = time.monotonic()
        # 多查询时 embed/milvus/sparse 计时是各查询之和，键名不变（单查询
        # 语义不变）；快照里的 subqueries 列表说明当次用了几条查询。
        vectors: list[list[float] | None] = []
        for query in queries:
            vector, error = await self._retry_async(
                "embed_query", lambda q=query: self._embedder.embed_query(q))
            if error:
                errors.append(error)
            vectors.append(vector)
        timings["embed_query"] = time.monotonic() - started

        started = time.monotonic()
        dense_lists: list[list[tuple[int, float]]] = []
        for vector in vectors:
            if vector is None:
                dense_lists.append([])
                continue
            dense, error = self._retry_sync(
                "dense_search",
                lambda v=vector: self._store.search(
                    index.milvus_collection, v, self._settings.retrieval_top_k))
            if error:
                errors.append(error)
            dense_lists.append(dense or [])
        timings["milvus_search"] = time.monotonic() - started

        hits = dense_lists[0]
        pool: list[dict] = []
        sparse_lists: list[list[tuple[int, float]]] = []
        sparse_supported = True
        probe = getattr(self._store, "has_sparse", None)
        if probe is not None:
            sparse_supported = bool(probe(index.milvus_collection))
        if strategy in ("hybrid-rrf", "hybrid-rerank") and sparse_supported:
            # 关键词路＝同一集合的全文检索；块集合与稠密严格同源（同版本同批
            # 块），版本隔离由集合本身保证——旧版本块在别的集合，不在这里过滤。
            started = time.monotonic()
            for query in queries:
                sparse, error = self._retry_sync(
                    "keyword_search",
                    lambda q=query: self._store.search_sparse(
                        index.milvus_collection, q, self._settings.bm25_top_n))
                if error:
                    errors.append(error)
                sparse_lists.append(sparse or [])
            timings["sparse_search"] = time.monotonic() - started
        elif strategy in ("hybrid-rrf", "hybrid-rerank"):
            # 老索引没有 sparse 字段：走纯向量，不把"缺字段"记成降级。
            sparse_lists = [[] for _ in queries]
        if strategy == "hybrid-rrf":
            # 两路只贡献排名，RRF 融合后按名次取 top_k 作为本策略的候选列表；
            # bm25_weight<1 时保护稠密榜中后段项（调参轴，见 settings 注释）。
            hits = rrf_fuse([[pk for pk, _ in hits], [pk for pk, _ in sparse_lists[0]]],
                            top_k=self._settings.retrieval_top_k,
                            k=self._settings.rrf_k,
                            weights=[1.0, self._settings.bm25_weight])
        elif strategy == "hybrid-rerank":
            # 只合并不选择：两路全部命中项去重进池（每项带各路名次），
            # 选择交给看过问题与内容的重排，单路中后段项不再被挤掉。
            pools = [build_candidate_pool(
                [pk for pk, _ in dense], [pk for pk, _ in sparse],
                pool_size=self._settings.rerank_pool_size)
                for dense, sparse in zip(dense_lists, sparse_lists)]
            # 查询分解：逐查询建池后合并（去重、按各池最好名次封顶），
            # 池上限放宽到 decomposition_pool_limit——按原问题名次截 60 会
            # 把"只有子查询能召回"的块挡在重排之外，失去分解的意义。
            pool = (pools[0] if len(pools) == 1 else merge_candidate_pools(
                pools, self._settings.decomposition_pool_limit))
            hits = [(entry["chunk_id"], 0.0) for entry in pool]
        if not hits:
            status = "failed" if errors else "empty"
            return SearchResult(status, [], index.version, timings, errors, pool)
        subquery_by_chunk = {
            entry["chunk_id"]: int(entry.get("subquery", 0))
            for entry in pool
        }
        started = time.monotonic()
        try:
            evidence = await self._to_evidence(
                session, index, hits, subquery_by_chunk=subquery_by_chunk)
        except Exception as exc:  # noqa: BLE001 - 证据回载失败按整次失败返回
            timings["load_evidence"] = time.monotonic() - started
            errors.append(f"load_evidence: {type(exc).__name__}: {exc}"[:500])
            return SearchResult("failed", [], index.version, timings, errors, pool)
        timings["load_evidence"] = time.monotonic() - started
        if strategy == "hybrid-rerank" and evidence:
            started = time.monotonic()
            scores, error = await self._retry_async(
                "rerank", lambda: self._reranker.rerank(
                    question, [e.text for e in evidence]))
            timings["rerank"] = time.monotonic() - started
            if error:
                # 重排不可用时保留重排前的候选顺序，回答仍可生成。
                errors.append(error)
            elif scores is None or len(scores) != len(evidence):
                errors.append("rerank: 返回数量与候选不一致，使用重排前顺序")
            else:
                evidence = _apply_rerank(evidence, scores)
                surviving = {e.chunk_id for e in evidence}
                pool = [entry for entry in pool if entry["chunk_id"] in surviving]
            if (self._settings.evidence_context_mode == "neighbor-rerank"
                    and self._settings.evidence_neighbor_expansion > 0):
                started = time.monotonic()
                try:
                    evidence = await self._expand_neighbors(
                        session, index, question, evidence,
                        self._settings.evidence_neighbor_expansion)
                except Exception as exc:  # noqa: BLE001 - 邻块扩展失败保留原候选
                    errors.append(f"expand_neighbors: {type(exc).__name__}: {exc}"[:500])
                timings["expand_neighbors"] = time.monotonic() - started
        if not evidence:
            status = "failed" if errors else "empty"
        else:
            status = "partial" if errors else "ok"
        return SearchResult(status, evidence, index.version, timings, errors, pool)

    async def search_collections(
        self, session: AsyncSession, indexes: list[IndexRef], question: str,
    ) -> SearchResult:
        """跨集合检索：一次嵌入，各集合各自召回，合并候选后统一重排。

        只在默认的 hybrid-rerank 策略下走这条合并路径：每集合的候选池只贡献
        名次，不先做集合内的重排与截断；合并按各池名次轮流取，避免某个集合
        的六十条候选挤掉其他集合的第一条。重排与最终选择仍只按原问题做一次。
        其他策略退回逐集合完整检索后按分数合并，语义保持但成本更高。
        """
        indexes = list(indexes)
        if not indexes:
            return SearchResult("empty", [], 0, {}, [], [])
        if len(indexes) == 1:
            return await self.search(session, indexes[0], question)
        if self._settings.retrieval_strategy != "hybrid-rerank":
            return await self._merge_searches(session, indexes, question)

        timings: dict[str, float] = {}
        errors: list[str] = []
        started = time.monotonic()
        vector, error = await self._retry_async(
            "embed_query", lambda: self._embedder.embed_query(question))
        timings["embed_query"] = time.monotonic() - started
        if error:
            errors.append(error)

        started = time.monotonic()
        pools: list[list[dict]] = []
        origins: dict[int, int] = {}
        for index in indexes:
            dense: list[tuple[int, float]] = []
            sparse: list[tuple[int, float]] = []
            if vector is not None:
                hits, error = self._retry_sync(
                    "dense_search",
                    lambda i=index, v=vector: self._store.search(
                        i.milvus_collection, v, self._settings.retrieval_top_k))
                if error:
                    errors.append(f"{index.milvus_collection}: {error}")
                dense = hits or []
            probe = getattr(self._store, "has_sparse", None)
            if probe is None or probe(index.milvus_collection):
                hits, error = self._retry_sync(
                    "keyword_search",
                    lambda i=index: self._store.search_sparse(
                        i.milvus_collection, question, self._settings.bm25_top_n))
                if error:
                    errors.append(f"{index.milvus_collection}: {error}")
                sparse = hits or []
            pool = build_candidate_pool(
                [pk for pk, _ in dense], [pk for pk, _ in sparse],
                pool_size=self._settings.rerank_pool_size)
            for entry in pool:
                origins.setdefault(entry["chunk_id"], index.id)
            pools.append(pool)
        timings["milvus_search"] = time.monotonic() - started

        pool = _merge_pools_round_robin(pools, self._settings.rerank_pool_size)
        if not pool:
            status = "failed" if errors else "empty"
            return SearchResult(status, [], indexes[0].version, timings, errors, [])
        started = time.monotonic()
        try:
            evidence = await self._to_evidence_many(
                session, indexes, pool, origins)
        except Exception as exc:  # noqa: BLE001 - 证据回载失败按整次失败返回
            timings["load_evidence"] = time.monotonic() - started
            errors.append(f"load_evidence: {type(exc).__name__}: {exc}"[:500])
            return SearchResult("failed", [], indexes[0].version, timings,
                                errors, pool)
        timings["load_evidence"] = time.monotonic() - started
        if evidence:
            started = time.monotonic()
            scores, error = await self._retry_async(
                "rerank", lambda: self._reranker.rerank(
                    question, [e.text for e in evidence]))
            timings["rerank"] = time.monotonic() - started
            if error:
                errors.append(error)
            elif scores is None or len(scores) != len(evidence):
                errors.append("rerank: 返回数量与候选不一致，使用重排前顺序")
            else:
                evidence = _apply_rerank(evidence, scores)
                surviving = {e.chunk_id for e in evidence}
                pool = [entry for entry in pool if entry["chunk_id"] in surviving]
        if not evidence:
            status = "failed" if errors else "empty"
        else:
            status = "partial" if errors else "ok"
        return SearchResult(status, evidence, indexes[0].version, timings,
                            errors, pool)

    async def _merge_searches(
        self, session: AsyncSession, indexes: list[IndexRef], question: str,
    ) -> SearchResult:
        """非默认策略的跨集合退路：各集合完整检索后按分数合并。"""
        merged: list[Evidence] = []
        timings: dict[str, float] = {}
        errors: list[str] = []
        statuses: list[str] = []
        pool: list[dict] = []
        for index in indexes:
            result = await self.search(session, index, question)
            statuses.append(result.status)
            for key, value in result.stage_timings.items():
                timings[key] = timings.get(key, 0.0) + value
            errors.extend(
                f"{index.milvus_collection}: {item}" for item in result.errors)
            pool.extend(result.pool)
            merged.extend(result.evidence)
        by_chunk = {item.chunk_id: item for item in merged}
        evidence = sorted(by_chunk.values(), key=lambda item: (-item.score,
                                                              item.chunk_id))
        for rank, item in enumerate(evidence, start=1):
            item.rank = rank
        if not evidence:
            status = "failed" if errors else "empty"
        elif errors or any(value == "partial" for value in statuses):
            status = "partial"
        elif all(value == "empty" for value in statuses):
            status = "empty"
        else:
            status = "ok"
        return SearchResult(status, evidence, indexes[0].version, timings,
                            errors, pool)

    async def _retry_async(self, label: str, factory):
        attempts = max(1, int(getattr(
            self._settings, "query_channel_max_attempts", 2)))
        backoff = float(getattr(
            self._settings, "query_channel_backoff_seconds", 0.5))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await factory(), None
            except Exception as exc:  # noqa: BLE001 - 单路失败由调用方降级
                last_error = exc
                if attempt < attempts:
                    await asyncio.sleep(backoff * attempt)
        return None, f"{label}: {type(last_error).__name__}: {last_error}"[:500]

    def _retry_sync(self, label: str, factory):
        attempts = max(1, int(getattr(
            self._settings, "query_channel_max_attempts", 2)))
        backoff = float(getattr(
            self._settings, "query_channel_backoff_seconds", 0.5))
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return factory(), None
            except Exception as exc:  # noqa: BLE001 - 单路失败由调用方降级
                last_error = exc
                if attempt < attempts:
                    time.sleep(backoff * attempt)
        return None, f"{label}: {type(last_error).__name__}: {last_error}"[:500]

    async def _expand_neighbors(
        self, session: AsyncSession, index: IndexRef, question: str,
        evidence: list[Evidence], radius: int,
    ) -> list[Evidence]:
        """把已重排候选的同修订邻块加入候选，再重排合并。

        块级检索只带回最像问题的一批块，同一文档里相邻的事实块可能没有机会被
        重排看到。这里只补同修订、同索引版本的邻块，旧版本块序号不对应，不能拼。
        新块与原名次一起按重排分数排序。分数是逐对 softmax，单独一批重排与混在
        原名次里等价。

        只改候选与名次，不改变最终单元数和预算，是否选入仍由 `select_units`
        决定。
        """
        existing_keys = {(e.revision_row_id, e.chunk_index) for e in evidence}
        existing_ids = {e.chunk_id for e in evidence}
        wanted: set[tuple[int, int]] = set()
        origin: dict[tuple[int, int], int] = {}
        for item in evidence:
            for offset in range(-radius, radius + 1):
                if offset == 0:
                    continue
                key = (item.revision_row_id, item.chunk_index + offset)
                if key not in existing_keys:
                    wanted.add(key)
                    origin.setdefault(key, item.source_subquery)
        if not wanted:
            return evidence
        keys = sorted(wanted)
        new_rows: dict[int, tuple[int, int]] = {}
        for start in range(0, len(keys), 800):
            rows = (await session.execute(
                select(Chunk.id, Chunk.revision_row_id, Chunk.chunk_index).where(
                    tuple_(Chunk.revision_row_id, Chunk.chunk_index).in_(
                        keys[start:start + 800]),
                    Chunk.index_version_id == index.id,
                )
            )).all()
            for chunk_id, revision_row_id, chunk_index in rows:
                new_rows[chunk_id] = (revision_row_id, chunk_index)
        new_ids = [cid for cid in new_rows if cid not in existing_ids]
        if not new_ids:
            return evidence
        added = await self._to_evidence(
            session, index, [(chunk_id, 0.0) for chunk_id in new_ids],
            subquery_by_chunk={
                chunk_id: origin.get(new_rows[chunk_id], 0)
                for chunk_id in new_ids
            })
        if not added:
            return evidence
        scores = await self._reranker.rerank(question, [item.text for item in added])
        for item, score in zip(added, scores):
            item.score = round(float(score), 4)
        merged = evidence + added
        merged.sort(key=lambda item: (-item.score, item.chunk_id))
        for rank, item in enumerate(merged, start=1):
            item.rank = rank
        return merged

    async def _to_evidence(
        self, session: AsyncSession, index: IndexRef, hits: list[tuple[int, float]],
        subquery_by_chunk: dict[int, int] | None = None,
    ) -> list[Evidence]:
        # chunk_pk 即块写入时的行主键；直接按主键取回并核对块属于本索引版本。
        # 归属校验用块行自己的 index_version_id（已在行内，零额外查询）：比逐题
        # 载入 revisions 映射便宜，且严格等价——建库时只有冻结映射内的修订会
        # 落下带本版本标记的块。NULL 是加标记列之前的历史行，仍按旧索引读。
        chunk_rows = (await session.execute(
            select(Chunk, Revision, Document)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Chunk.id.in_([pk for pk, _ in hits]))
        )).all()
        by_id = {row.Chunk.id: row for row in chunk_rows}
        evidence: list[Evidence] = []
        for rank, (chunk_pk, score) in enumerate(hits, start=1):
            row = by_id.get(chunk_pk)
            if row is None:
                continue
            if row.Chunk.index_version_id not in (None, index.id):
                continue
            if not row.Document.available:
                continue
            meta = row.Document.meta or {}
            evidence.append(
                Evidence(
                    chunk_id=row.Chunk.id,
                    document_public_id=row.Document.public_id,
                    document_title=row.Document.title,
                    revision_id=row.Revision.revision_id,
                    revision_row_id=row.Revision.id,
                    chunk_index=row.Chunk.chunk_index,
                    text=row.Chunk.text,
                    char_start=row.Chunk.char_start,
                    char_end=row.Chunk.char_end,
                    line_start=row.Chunk.line_start,
                    line_end=row.Chunk.line_end,
                    page=row.Chunk.page,
                    score=round(float(score), 4),
                    rank=rank,
                    content_hash=row.Chunk.content_hash,
                    source_name=str(meta.get("source") or ""),
                    published_at=str(meta.get("published_at") or ""),
                    context=row.Chunk.context,
                    start_cut=row.Chunk.start_cut,
                    end_cut=row.Chunk.end_cut,
                    source_subquery=(subquery_by_chunk or {}).get(row.Chunk.id, 0),
                    index_id=index.id,
                )
            )
        return evidence

    async def _to_evidence_many(
        self, session: AsyncSession, indexes: list[IndexRef],
        pool: list[dict], origins: dict[int, int],
    ) -> list[Evidence]:
        """跨集合候选回载：只接受块行标记属于候选来源索引的块。

        候选池里的 chunk_id 由某个集合的检索返回，回载时逐块核对
        Chunk.index_version_id 与来源索引一致——不同集合的 Milvus 主键理论上
        不会冲突，但核对一次可以把旧版本、越权或残留块挡在证据之外。
        没有标记的历史块无法判断归属，跨集合场景一律跳过。
        """
        allowed = {index.id: index for index in indexes}
        wanted = [entry["chunk_id"] for entry in pool]
        if not wanted:
            return []
        rows = (await session.execute(
            select(Chunk, Revision, Document)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Chunk.id.in_(wanted))
        )).all()
        by_id = {row.Chunk.id: row for row in rows}
        evidence: list[Evidence] = []
        for rank, entry in enumerate(pool, start=1):
            chunk_pk = entry["chunk_id"]
            row = by_id.get(chunk_pk)
            if row is None or not row.Document.available:
                continue
            origin = origins.get(chunk_pk)
            if row.Chunk.index_version_id is None or origin is None:
                continue
            if row.Chunk.index_version_id != origin:
                continue
            if origin not in allowed:
                continue
            meta = row.Document.meta or {}
            evidence.append(
                Evidence(
                    chunk_id=row.Chunk.id,
                    document_public_id=row.Document.public_id,
                    document_title=row.Document.title,
                    revision_id=row.Revision.revision_id,
                    revision_row_id=row.Revision.id,
                    chunk_index=row.Chunk.chunk_index,
                    text=row.Chunk.text,
                    char_start=row.Chunk.char_start,
                    char_end=row.Chunk.char_end,
                    line_start=row.Chunk.line_start,
                    line_end=row.Chunk.line_end,
                    page=row.Chunk.page,
                    score=0.0,
                    rank=rank,
                    content_hash=row.Chunk.content_hash,
                    source_name=str(meta.get("source") or ""),
                    published_at=str(meta.get("published_at") or ""),
                    context=row.Chunk.context,
                    start_cut=row.Chunk.start_cut,
                    end_cut=row.Chunk.end_cut,
                    source_subquery=0,
                    index_id=origin,
                )
            )
        return evidence


def _merge_pools_round_robin(pools: list[list[dict]], limit: int) -> list[dict]:
    """按各集合候选池的名次轮流取，直到达到总上限。

    池内名次只在本集合可比；轮流取保证每个集合的最高名次候选先进入重排，
    而不是让先遍历的集合把上限占满。
    """
    merged: list[dict] = []
    seen: set[int] = set()
    position = 0
    while len(merged) < limit:
        progressed = False
        for pool in pools:
            if position >= len(pool):
                continue
            progressed = True
            entry = pool[position]
            key = entry["chunk_id"]
            if key in seen:
                continue
            seen.add(key)
            merged.append(entry)
            if len(merged) >= limit:
                break
        if not progressed:
            break
        position += 1
    return merged


def _apply_rerank(evidence: list[Evidence], scores: list[float]) -> list[Evidence]:
    """按重排分数重排候选并重编名次；分数并列时按块主键稳定。"""
    scored = list(zip(evidence, scores))
    scored.sort(key=lambda pair: (-pair[1], pair[0].chunk_id))
    ordered: list[Evidence] = []
    for rank, (item, score) in enumerate(scored, start=1):
        item.rank = rank
        item.score = round(float(score), 4)
        ordered.append(item)
    return ordered
