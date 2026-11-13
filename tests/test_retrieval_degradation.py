"""检索降级单元测试：单路失败、双路失败、重排失败与重试恢复。"""

from types import SimpleNamespace

import pytest

from pagerag.retrieval import IndexRef, RetrievalService

_INDEX = IndexRef(id=9, version=1, milvus_collection="c")


def _settings(**overrides):
    base = {
        "retrieval_strategy": "hybrid-rerank", "retrieval_top_k": 30,
        "bm25_top_n": 30, "rerank_pool_size": 60,
        "decomposition_pool_limit": 120, "evidence_context_mode": "window",
        "evidence_neighbor_expansion": 0, "query_channel_max_attempts": 2,
        "query_channel_backoff_seconds": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeEmbedder:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def embed_query(self, query: str):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [1.0, 0.0]


class Outcome:
    def __init__(self, *values) -> None:
        self.values = list(values)
        self.calls = 0

    def next(self):
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value


class FakeStore:
    def __init__(self, dense=None, sparse=None) -> None:
        self.dense = Outcome(*(dense or [[]]))
        self.sparse = Outcome(*(sparse or [[]]))

    def search(self, collection, vector, top_k):
        return self.dense.next()

    def search_sparse(self, collection, query, top_n):
        return self.sparse.next()


class FakeReranker:
    def __init__(self, scores=None, error: Exception | None = None) -> None:
        self.scores = scores
        self.error = error
        self.calls = 0

    async def rerank(self, query, texts):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.scores is not None:
            return self.scores
        return [1.0 - index * 0.1 for index in range(len(texts))]


def _session(chunk_ids: list[int]):
    rows = []
    for cid in chunk_ids:
        rows.append(SimpleNamespace(
            Chunk=SimpleNamespace(
                id=cid, index_version_id=9, text=f"正文{cid}", char_start=0,
                char_end=3, line_start=1, line_end=1, page=None,
                content_hash=f"h{cid}", chunk_index=cid, context=None,
                start_cut=False, end_cut=False),
            Revision=SimpleNamespace(id=cid + 100, revision_id=f"r{cid}"),
            Document=SimpleNamespace(public_id=f"d{cid}", title=f"文档{cid}",
                                     meta={}, available=True),
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


def _service(store, embedder=None, reranker=None, settings=None):
    return RetrievalService(store, embedder or FakeEmbedder(),
                            settings or _settings(),
                            reranker=reranker or FakeReranker())


@pytest.mark.asyncio
async def test_dense_failure_falls_back_to_sparse():
    store = FakeStore(dense=[RuntimeError("dense down")],
                      sparse=[[(1, 0.8)]])
    result = await _service(store).search(_session([1]), _INDEX, "问题")
    assert result.status == "partial"
    assert [item.chunk_id for item in result.evidence] == [1]
    assert any("dense_search" in error for error in result.errors)


@pytest.mark.asyncio
async def test_both_channels_fail_returns_failed():
    store = FakeStore(dense=[RuntimeError("dense down")],
                      sparse=[RuntimeError("sparse down")])
    result = await _service(store).search(_session([1]), _INDEX, "问题")
    assert result.status == "failed"
    assert result.evidence == []
    assert len(result.errors) == 2


@pytest.mark.asyncio
async def test_embed_failure_still_uses_sparse_channel():
    store = FakeStore(sparse=[[(1, 0.8)]])
    result = await _service(store, embedder=FakeEmbedder(RuntimeError("embed down"))) \
        .search(_session([1]), _INDEX, "问题")
    assert result.status == "partial"
    assert [item.chunk_id for item in result.evidence] == [1]
    assert any("embed_query" in error for error in result.errors)


@pytest.mark.asyncio
async def test_rerank_failure_keeps_pre_rerank_order():
    store = FakeStore(dense=[[(1, 0.9), (2, 0.8)]])
    reranker = FakeReranker(error=RuntimeError("rerank down"))
    result = await _service(store, reranker=reranker) \
        .search(_session([1, 2]), _INDEX, "问题")
    assert result.status == "partial"
    assert [item.chunk_id for item in result.evidence] == [1, 2]
    assert any("rerank" in error for error in result.errors)


@pytest.mark.asyncio
async def test_transient_dense_error_recovers_on_retry():
    store = FakeStore(dense=[RuntimeError("blip"), [(1, 0.9)]])
    result = await _service(store).search(_session([1]), _INDEX, "问题")
    assert result.status == "ok"
    assert store.dense.calls == 2
    assert result.errors == []


@pytest.mark.asyncio
async def test_rerank_wrong_count_falls_back_to_candidate_order():
    store = FakeStore(dense=[[(1, 0.9), (2, 0.8)]])
    reranker = FakeReranker(scores=[0.5])
    result = await _service(store, reranker=reranker) \
        .search(_session([1, 2]), _INDEX, "问题")
    assert result.status == "partial"
    assert [item.chunk_id for item in result.evidence] == [1, 2]
    assert any("返回数量" in error for error in result.errors)
