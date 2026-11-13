"""查询分解离线单元测试：子查询解析、多查询候选池合并与检索接线。

不依赖 MySQL/Milvus/模型服务；分解器、嵌入、存储、重排都用假对象注入
（向量＝查询文本本身，假存储按它区分各查询的召回）。
"""

import json
from types import SimpleNamespace

import pytest

from pagerag.answering import QueryDecomposer
from pagerag.bm25 import build_candidate_pool, merge_candidate_pools
from pagerag.retrieval import IndexRef, RetrievalService

_INDEX = IndexRef(id=9, version=1, milvus_collection="c")


class TestMergeCandidatePools:
    def test_collision_keeps_primary_entry(self):
        # 块 3 在池 0 与池 1 都出现：以原问题条目为准（subquery=0）。
        pool0 = build_candidate_pool([1, 3], [], pool_size=10)
        pool1 = build_candidate_pool([3, 4], [], pool_size=10)
        merged = merge_candidate_pools([pool0, pool1], pool_size=10)
        by_id = {e["chunk_id"]: e for e in merged}
        assert by_id[3]["subquery"] == 0
        assert by_id[4]["subquery"] == 1

    def test_primary_pool_never_crowded_out(self):
        # 不变式：池 0 条目全保留——分解只能加候选，不能挤掉原问题的召回。
        pool0 = build_candidate_pool(list(range(1, 6)), [], pool_size=10)
        pool1 = build_candidate_pool(list(range(6, 26)), [], pool_size=10)
        merged = merge_candidate_pools([pool0, pool1], pool_size=8)
        ids = {e["chunk_id"] for e in merged}
        assert set(range(1, 6)) <= ids
        assert len(merged) == 8

    def test_union_across_pools(self):
        # 各池独有的块全部保留：合并只去重，不按原问题名次丢子查询独有项。
        pool0 = build_candidate_pool([1], [2], pool_size=10)
        pool1 = build_candidate_pool([3], [4], pool_size=10)
        merged = merge_candidate_pools([pool0, pool1], pool_size=10)
        assert {e["chunk_id"] for e in merged} == {1, 2, 3, 4}

    def test_pool_size_caps(self):
        pools = [build_candidate_pool(list(range(i * 10, i * 10 + 10)), [],
                                      pool_size=10) for i in range(3)]
        assert len(merge_candidate_pools(pools, pool_size=15)) == 15


class _FakeLLM:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self._content = content
        self._error = error
        self.last_endpoint = "fake"

    async def ainvoke(self, messages, **kwargs):
        if self._error is not None:
            raise self._error
        return SimpleNamespace(content=self._content)


class TestDecompose:
    @pytest.mark.asyncio
    async def test_valid_split(self):
        llm = _FakeLLM('["甲公司的营收", "乙公司的营收"]')
        decomposer = QueryDecomposer(llm, max_subqueries=3)
        assert await decomposer.decompose("比较甲与乙的营收") == [
            "甲公司的营收", "乙公司的营收"]

    @pytest.mark.asyncio
    async def test_empty_list_means_no_split(self):
        # 单一焦点题：模型输出空表＝不拆，检索保持单查询。
        assert await QueryDecomposer(_FakeLLM("[]")).decompose("问题") == []

    @pytest.mark.asyncio
    async def test_garbage_falls_back_to_empty(self):
        for content in ("not json", '{"a": 1}', "[1, 2]", ""):
            assert await QueryDecomposer(_FakeLLM(content)).decompose("问题") == []

    @pytest.mark.asyncio
    async def test_error_falls_back_to_empty_and_counts(self):
        # 失败计数器：把"端点故障"与"模型判断不拆"区分开，评估才能如实报告。
        decomposer = QueryDecomposer(_FakeLLM(error=RuntimeError("down")), 3)
        assert await decomposer.decompose("问题") == []
        assert decomposer.failed == 1
        # 正常返回不增加失败计数。
        assert await QueryDecomposer(_FakeLLM("[]")).decompose("问题") == []
        assert QueryDecomposer(_FakeLLM("[]")).failed == 0

    @pytest.mark.asyncio
    async def test_hanging_call_bounded_by_timeout(self):
        # 挂起的调用在时限内被取消并计失败，不拖住整题（时限可注入缩短）。
        import asyncio

        class _HangingLLM:
            last_endpoint = "fake"

            async def ainvoke(self, messages, **kwargs):
                await asyncio.sleep(30)

        decomposer = QueryDecomposer(_HangingLLM(), 3)
        import pagerag.answering as answering
        original = answering.DECOMPOSE_TIMEOUT_SECONDS
        answering.DECOMPOSE_TIMEOUT_SECONDS = 0.05
        try:
            assert await decomposer.decompose("问题") == []
            assert decomposer.failed == 1
        finally:
            answering.DECOMPOSE_TIMEOUT_SECONDS = original

    @pytest.mark.asyncio
    async def test_original_duplicate_and_junk_dropped(self):
        # 与原问题相同的项、空串、非字符串都丢：宁少一路召回。
        llm = _FakeLLM('["比较甲与乙", "", "乙公司", 7]')
        assert await QueryDecomposer(llm, 3).decompose("比较甲与乙") == ["乙公司"]

    @pytest.mark.asyncio
    async def test_caps_at_max(self):
        llm = _FakeLLM('["a", "b", "c"]')
        assert await QueryDecomposer(llm, 2).decompose("q") == ["a", "b"]

    @pytest.mark.asyncio
    async def test_overlong_query_dropped(self):
        llm = _FakeLLM(f'["{"x" * 201}"]')
        assert await QueryDecomposer(llm, 3).decompose("q") == []


class _CountingLLM:
    """按次返回不同输出的假 LLM：调用次数即端点开销的代理。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.last_endpoint = "fake"

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=self._replies.pop(0))


class TestDecomposeCache:
    @pytest.mark.asyncio
    async def test_hit_returns_cached_without_llm_call(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        decomposer = QueryDecomposer(
            _CountingLLM(['["新拆的子查询"]']), 3,
            cache={"缓存里的问题": ["甲公司营收", "乙公司营收"]},
            cache_path=cache_path)
        assert await decomposer.decompose("缓存里的问题") == [
            "甲公司营收", "乙公司营收"]
        assert decomposer.cache_hits == 1
        assert decomposer._llm.calls == 0  # noqa: SLF001 - 计数即断言目的

    @pytest.mark.asyncio
    async def test_success_stored_and_persisted(self, tmp_path):
        cache_path = tmp_path / "cache.json"
        decomposer = QueryDecomposer(
            _CountingLLM(['["甲公司营收", "乙公司营收"]']), 3,
            cache_path=cache_path)
        assert await decomposer.decompose("比较甲与乙") == ["甲公司营收", "乙公司营收"]
        stored = json.loads(cache_path.read_text(encoding="utf-8"))
        assert stored == {"比较甲与乙": ["甲公司营收", "乙公司营收"]}
        # 第二次同题命中缓存，不再调端点。
        assert await decomposer.decompose("比较甲与乙") == [
            "甲公司营收", "乙公司营收"]
        assert decomposer.cache_hits == 1
        assert decomposer._llm.calls == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_no_split_empty_result_is_cached(self, tmp_path):
        # 调用成功且模型判断不拆（空表）＝可复用结果，二次同题不再调端点。
        decomposer = QueryDecomposer(
            _CountingLLM(["[]"]), 3, cache_path=tmp_path / "cache.json")
        assert await decomposer.decompose("单一问题") == []
        assert await decomposer.decompose("单一问题") == []
        assert decomposer.cache_hits == 1
        assert decomposer._llm.calls == 1  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_failure_not_cached_and_retried(self, tmp_path):
        class _FlakyLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.last_endpoint = "fake"

            async def ainvoke(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("endpoint down")
                return SimpleNamespace(content='["甲公司营收"]')

        cache_path = tmp_path / "cache.json"
        llm = _FlakyLLM()
        decomposer = QueryDecomposer(llm, 3, cache_path=cache_path)
        assert await decomposer.decompose("比较甲与乙") == []  # 失败退回空表
        assert decomposer.failed == 1
        assert not cache_path.exists()  # 失败不落缓存
        assert await decomposer.decompose("比较甲与乙") == ["甲公司营收"]  # 重试成功
        assert json.loads(cache_path.read_text(encoding="utf-8")) == {
            "比较甲与乙": ["甲公司营收"]}

    @pytest.mark.asyncio
    async def test_parse_garbage_not_cached(self, tmp_path):
        # 非 JSON 输出视同失败：不缓存，下次照常调端点。
        decomposer = QueryDecomposer(
            _CountingLLM(["not json", "not json"]), 3,
            cache_path=tmp_path / "cache.json")
        assert await decomposer.decompose("问题") == []
        assert await decomposer.decompose("问题") == []
        assert decomposer._llm.calls == 2  # noqa: SLF001
        assert not (tmp_path / "cache.json").exists()


class _FakeEmbedder:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def embed_query(self, query: str) -> str:
        self.queries.append(query)
        return query  # 向量＝查询文本，假存储按它返回该查询的召回


class _FakeStore:
    def __init__(self, dense: dict, sparse: dict) -> None:
        self._dense = dense
        self._sparse = sparse
        self.dense_calls = 0

    def search(self, _collection, vector, top_k):
        self.dense_calls += 1
        return [(pk, 0.5) for pk in self._dense.get(vector, [])[:top_k]]

    def search_sparse(self, _collection, query, top_n):
        return [(pk, 0.5) for pk in self._sparse.get(query, [])[:top_n]]


class _FakeReranker:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def rerank(self, query, documents):
        self.queries.append(query)
        return [1.0 - 0.01 * i for i in range(len(documents))]


def _settings(**overrides) -> SimpleNamespace:
    base = {"retrieval_strategy": "hybrid-rerank", "retrieval_top_k": 30,
            "bm25_top_n": 30, "rrf_k": 60, "bm25_weight": 1.0,
            "rerank_pool_size": 60, "decomposition_pool_limit": 120,
            "evidence_context_mode": "window", "evidence_neighbor_expansion": 0}
    base.update(overrides)
    return SimpleNamespace(**base)


def _session(chunk_ids: list[int]):
    rows = []
    for cid in chunk_ids:
        rows.append(SimpleNamespace(
            Chunk=SimpleNamespace(id=cid, index_version_id=9, text=f"正文{cid}",
                                  char_start=0, char_end=3, line_start=1, line_end=1,
                                  page=None, content_hash=f"h{cid}", chunk_index=cid,
                                  context=None, start_cut=None, end_cut=None),
            Revision=SimpleNamespace(id=cid + 100, revision_id=f"r{cid}",
                                     document_id=1),
            Document=SimpleNamespace(available=True, public_id="d",
                                     title=f"t{cid}", meta={}),
        ))

    class _Result:
        @staticmethod
        def all():
            return rows

    class _Session:
        @staticmethod
        async def execute(_statement):
            return _Result()

    return _Session()


class TestSearchSubqueries:
    """检索接线：子查询各走一路召回、合并进池，重排与最终排序只看原问题。"""

    QUESTION = "比较甲公司与乙公司的营收"

    def _service(self, dense: dict, sparse: dict, settings=None):
        store = _FakeStore(dense, sparse)
        embedder = _FakeEmbedder()
        reranker = _FakeReranker()
        retrieval = RetrievalService(store, embedder, settings or _settings(),
                                     reranker=reranker)
        return retrieval, store, embedder, reranker

    @pytest.mark.asyncio
    async def test_subqueries_add_recall_rerank_keeps_original(self):
        dense = {self.QUESTION: [1, 2], "甲公司营收": [4, 5], "乙公司营收": [6]}
        sparse = {self.QUESTION: [1, 3], "甲公司营收": [4], "乙公司营收": [6, 7]}
        retrieval, _store, _embedder, reranker = self._service(dense, sparse)
        result = await retrieval.search(
            _session(list(range(1, 8))), _INDEX, self.QUESTION,
            subqueries=["甲公司营收", "乙公司营收"])
        # 子查询独有的块（4、5、6、7）进入候选；重排只按原问题调用一次。
        assert {e.chunk_id for e in result.evidence} == set(range(1, 8))
        assert reranker.queries == [self.QUESTION]
        # 池条目带来源查询下标（0＝原问题）；名次重排后 1..7 全部在列。
        by_id = {e["chunk_id"]: e for e in result.pool}
        assert by_id[1]["subquery"] == 0 and by_id[4]["subquery"] == 1
        assert [e.rank for e in result.evidence] == list(range(1, 8))

    @pytest.mark.asyncio
    async def test_without_subqueries_pool_format_unchanged(self):
        # 单查询路径：所有池条目标记为原问题（subquery=0），快照字段统一。
        dense = {self.QUESTION: [1, 2]}
        sparse = {self.QUESTION: [1, 3]}
        retrieval, _store, _embedder, reranker = self._service(dense, sparse)
        result = await retrieval.search(_session([1, 2, 3]), _INDEX, self.QUESTION)
        assert {e.chunk_id for e in result.evidence} == {1, 2, 3}
        assert reranker.queries == [self.QUESTION]
        assert all(entry["subquery"] == 0 for entry in result.pool)

    @pytest.mark.asyncio
    async def test_duplicate_subquery_ignored(self):
        # 与原问题相同的子查询不产生第二路召回（接线侧防御，分解器已过滤）。
        dense = {self.QUESTION: [1]}
        sparse = {self.QUESTION: [1]}
        retrieval, store, _embedder, _reranker = self._service(dense, sparse)
        await retrieval.search(_session([1]), _INDEX, self.QUESTION,
                               subqueries=[self.QUESTION, "", "乙公司营收"])
        assert store.dense_calls == 2  # 原问题＋1 条有效子查询

    @pytest.mark.asyncio
    async def test_merged_pool_capped_by_limit(self):
        # 合并池按 decomposition_pool_limit 封顶：超出上限的低名次项进不了重排。
        # 两查询各 30 稠密＋15 关键词，去重后 90 > 70（每路先按 top_k/top_n 截）。
        dense = {self.QUESTION: list(range(1, 31)),
                 "乙公司营收": list(range(31, 61))}
        sparse = {self.QUESTION: list(range(61, 76)),
                  "乙公司营收": list(range(76, 91))}
        settings = _settings(decomposition_pool_limit=70)
        retrieval, _store, _embedder, _reranker = self._service(dense, sparse,
                                                                settings)
        result = await retrieval.search(_session(list(range(1, 91))), _INDEX,
                                        self.QUESTION, subqueries=["乙公司营收"])
        assert len(result.pool) == 70

    @pytest.mark.asyncio
    async def test_dense_strategy_ignores_subqueries(self):
        # 非 hybrid-rerank 不接入分解：多查询各自截断会互相挤出（R1 教训）。
        dense = {self.QUESTION: [1, 2], "乙公司营收": [3]}
        sparse = {self.QUESTION: [1], "乙公司营收": [3]}
        settings = _settings(retrieval_strategy="dense")
        retrieval, store, _embedder, _reranker = self._service(dense, sparse,
                                                               settings)
        result = await retrieval.search(_session([1, 2, 3]), _INDEX,
                                        self.QUESTION, subqueries=["乙公司营收"])
        assert store.dense_calls == 1
        assert {e.chunk_id for e in result.evidence} == {1, 2}
