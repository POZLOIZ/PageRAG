"""后台重建任务单元测试：冲突、成功、失败与未知集合。"""

import asyncio
from types import SimpleNamespace

import pytest

from pagerag.rebuild import RebuildConflict, RebuildManager


class FakeSession:
    def __init__(self, collection) -> None:
        self.collection = collection
        self.commits = 0

    async def execute(self, statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: self.collection)

    async def commit(self) -> None:
        self.commits += 1


class FakeSessions:
    def __init__(self, session) -> None:
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


class FakeDatabase:
    def __init__(self, collection) -> None:
        self.session = FakeSession(collection)

    def sessions(self):
        return FakeSessions(self.session)


class FakeIngest:
    def __init__(self, gate: asyncio.Event | None = None,
                 error: Exception | None = None, version: int = 7) -> None:
        self.gate = gate
        self.error = error
        self.version = version
        self.calls = 0

    async def rebuild_index(self, session, collection):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return SimpleNamespace(version=self.version)


@pytest.mark.asyncio
async def test_start_rejects_same_collection_while_running():
    gate = asyncio.Event()
    manager = RebuildManager(FakeDatabase(SimpleNamespace(public_id="c1")),
                             FakeIngest(gate=gate))
    task = await manager.start("c1")
    with pytest.raises(RebuildConflict):
        await manager.start("c1")
    gate.set()
    await manager._futures[task.task_id]
    assert task.status == "succeeded"
    assert task.index_version == 7
    assert manager.get(task.task_id) is task


@pytest.mark.asyncio
async def test_rebuild_marks_failed_and_keeps_error():
    manager = RebuildManager(FakeDatabase(SimpleNamespace(public_id="c1")),
                             FakeIngest(error=ValueError("磁盘满了")))
    task = await manager.start("c1")
    await manager._futures[task.task_id]
    assert task.status == "failed"
    assert "磁盘满了" in task.error
    assert task.finished_at is not None


@pytest.mark.asyncio
async def test_missing_collection_fails_with_clear_error():
    manager = RebuildManager(FakeDatabase(None), FakeIngest())
    task = await manager.start("missing")
    await manager._futures[task.task_id]
    assert task.status == "failed"
    assert "集合不存在" in task.error
    assert task.index_version is None


@pytest.mark.asyncio
async def test_list_is_newest_first_and_prune_keeps_recent():
    manager = RebuildManager(FakeDatabase(SimpleNamespace(public_id="c1")),
                             FakeIngest())
    first = await manager.start("c1")
    await manager._futures[first.task_id]
    second = await manager.start("c1")
    await manager._futures[second.task_id]
    listed = manager.list()
    assert [item.task_id for item in listed] == [second.task_id, first.task_id]
    assert manager.get("not-a-task") is None
