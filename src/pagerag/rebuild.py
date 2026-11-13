"""后台重建任务：HTTP 请求只负责登记任务，构建在独立数据库会话里执行。

构建状态本身已经落在 index_versions 和 ingest_jobs 两张表里；这里的任务
句柄只解决请求不阻塞和同一集合不并发重建。进程重启后句柄丢失，但数据库
里的 building 版本仍在，页面可以显示并再次触发续建。
"""

import asyncio
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pagerag.db import Database
from pagerag.ingest import IngestService
from pagerag.models import Collection, utcnow


class RebuildConflict(Exception):
    """同一集合已有重建任务在跑。"""


@dataclass
class RebuildTask:
    task_id: str
    collection_public_id: str
    status: str = "queued"
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    index_version: int | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "collection_id": self.collection_public_id,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "index_version": self.index_version,
            "error": self.error,
        }


class RebuildManager:
    def __init__(self, database: Database, ingest: IngestService) -> None:
        self._database = database
        self._ingest = ingest
        self._tasks: dict[str, RebuildTask] = {}
        self._running: set[str] = set()
        self._lock = asyncio.Lock()
        self._futures: dict[str, asyncio.Task] = {}

    async def start(self, collection_public_id: str) -> RebuildTask:
        async with self._lock:
            if collection_public_id in self._running:
                raise RebuildConflict("该集合已有重建任务在运行")
            self._running.add(collection_public_id)
            self._prune()
        task = RebuildTask(
            task_id=secrets.token_hex(8),
            collection_public_id=collection_public_id,
        )
        self._tasks[task.task_id] = task
        # create_task 的返回值必须保留强引用，否则事件循环只保留弱引用，
        # 任务可能在运行中被回收。
        self._futures[task.task_id] = asyncio.create_task(self._run(task))
        return task

    def get(self, task_id: str) -> RebuildTask | None:
        return self._tasks.get(task_id)

    def list(self) -> list[RebuildTask]:
        return sorted(self._tasks.values(), key=lambda item: item.created_at,
                      reverse=True)

    async def _run(self, task: RebuildTask) -> None:
        task.status = "running"
        task.started_at = utcnow()
        try:
            async with self._database.sessions() as session:
                collection = (await session.execute(
                    select(Collection).where(
                        Collection.public_id == task.collection_public_id)
                )).scalar_one_or_none()
                if collection is None:
                    raise ValueError("集合不存在")
                index = await self._ingest.rebuild_index(session, collection)
                await session.commit()
                task.index_version = index.version
            task.status = "succeeded"
        except Exception as exc:  # noqa: BLE001 - 失败原因必须回到任务和页面
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"[:2000]
        finally:
            task.finished_at = utcnow()
            async with self._lock:
                self._running.discard(task.collection_public_id)

    def _prune(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(hours=1)
        finished = [
            task for task in self._tasks.values()
            if task.finished_at is not None and task.finished_at < cutoff
        ]
        for task in finished:
            self._tasks.pop(task.task_id, None)
            self._futures.pop(task.task_id, None)
        if len(self._tasks) > 50:
            ordered = sorted(self._tasks.values(), key=lambda item: item.created_at)
            for task in ordered[: len(self._tasks) - 50]:
                if task.finished_at is not None:
                    self._tasks.pop(task.task_id, None)
                    self._futures.pop(task.task_id, None)
