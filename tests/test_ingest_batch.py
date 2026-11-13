"""两态写入顺序：块行必须先提交，再编码与写远端。

若远端已写入而本地事务回滚，重跑会生成新主键，旧向量成为无法核对的残留。这里用记录调用
顺序的假对象验证顺序，不连 MySQL/Milvus/模型服务。
"""

from types import SimpleNamespace

import pytest

from pagerag.ingest import IngestService, _Batch
from pagerag.settings import Settings


def make_settings() -> Settings:
    return Settings(_env_file=None, database_url="", llm_base_url="", llm_model="",
                    retrieval_strategy="dense")


class RecordingSession:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def commit(self) -> None:
        self.events.append("commit")

    async def execute(self, statement) -> None:  # noqa: ANN001 - 只记录调用
        self.events.append("execute")


class RecordingStore:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def upsert(self, collection: str, rows: list[tuple]) -> None:
        assert collection == "pagerag_x_v1"
        assert [row[0] for row in rows] == [11, 12]
        self.events.append("upsert")


class RecordingEmbedder:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        assert texts == ["甲", "乙"]
        self.events.append("embed")
        return [[0.0], [0.0]]


@pytest.mark.asyncio
async def test_write_batch_commits_chunk_rows_before_remote_write() -> None:
    events: list[str] = []
    service = IngestService(make_settings(), RecordingStore(events),
                            RecordingEmbedder(events))
    batch = _Batch(make_settings())
    batch.revisions = [SimpleNamespace(id=1)]
    batch.chunks = [SimpleNamespace(id=11, text="甲"), SimpleNamespace(id=12, text="乙")]
    index = SimpleNamespace(version=1, milvus_collection="pagerag_x_v1")

    await service._write_batch(RecordingSession(events), index, batch)

    # 第一次提交（块行落库并分配主键）必须早于编码与远端写入；
    # 两次 execute 分别更新块的确认状态与修订状态，最后再提交。
    assert events == ["commit", "embed", "upsert", "execute", "execute", "commit"]
    assert batch.chunks == []
