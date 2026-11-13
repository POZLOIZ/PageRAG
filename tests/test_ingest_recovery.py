"""写入批故障注入：编码失败后块行保留，重跑按同一主键覆盖写入。"""

from types import SimpleNamespace

import pytest

from pagerag.ingest import IngestError, IngestService, _Batch


class FakeEmbedder:
    def __init__(self, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def embed_documents(self, texts):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("embedding endpoint down")
        return [[1.0, 0.0] for _ in texts]


class FakeStore:
    def __init__(self) -> None:
        self.upserts = []

    def upsert(self, collection, rows):
        self.upserts.append([row[0] for row in rows])


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace()

    async def commit(self) -> None:
        self.commits += 1


def _settings():
    return SimpleNamespace(ingest_max_attempts=2)


def _index():
    return SimpleNamespace(version=1, milvus_collection="c")


def _batch() -> _Batch:
    batch = _Batch(_settings())
    batch.revisions = [SimpleNamespace(id=11)]
    batch.chunks = [SimpleNamespace(id=1, text="第一块"),
                    SimpleNamespace(id=2, text="第二块")]
    return batch


@pytest.mark.asyncio
async def test_embedding_failure_keeps_prepared_chunks_and_marks_revision():
    session = FakeSession()
    store = FakeStore()
    ingest = IngestService(_settings(), store, FakeEmbedder(fail_times=2))
    with pytest.raises(IngestError, match="向量编码"):
        await ingest._write_batch(session, _index(), _batch())
    # 第一次提交是"块已准备"，第二次提交是失败状态；远端没有写入。
    assert session.commits == 2
    assert store.upserts == []


@pytest.mark.asyncio
async def test_retry_uses_same_chunk_primary_keys():
    session = FakeSession()
    store = FakeStore()
    ingest = IngestService(_settings(), store, FakeEmbedder())
    await ingest._write_batch(session, _index(), _batch())
    assert store.upserts == [[1, 2]]
    assert session.commits == 2
