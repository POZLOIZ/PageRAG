"""评测入口：直接调用服务层（与 HTTP 同一套代码），按根目录约定写运行记录。

首轮自动检查只做能机器判定的部分（答案非空、引用存在、拒答场景确实拒答）；
标准事实覆盖与语义判分按评测套件约定另行校准，不在这里假装已通过。
"""

import json
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from pagerag.answering import AnsweringService
from pagerag.db import Database
from pagerag.embeddings import EmbeddingClient
from pagerag.ingest import IngestService
from pagerag.milvus_store import MilvusStore
from pagerag.models import Chunk, Citation, Document, QueryRun, Revision
from pagerag.retrieval import RetrievalService
from pagerag.settings import CHUNKER_VERSION, PARSER_VERSION, Settings


@dataclass
class TaskResult:
    task_id: str
    status: str  # passed / failed / blocked / not_run
    question: str
    answer: str | None = None
    insufficient: bool = False
    citations: list[dict] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    error: str | None = None
    cost: dict = field(default_factory=dict)


class EvalRunner:
    def __init__(self, settings: Settings, database: Database, store: MilvusStore,
                 embedder: EmbeddingClient) -> None:
        self._settings = settings
        self._database = database
        self._ingest = IngestService(settings, store, embedder)
        retrieval = RetrievalService(store, embedder, settings)
        self._answering = AnsweringService(settings, retrieval)

    async def run_task(self, task: dict, package_dir: Path) -> TaskResult:
        result = TaskResult(task_id=task["task_id"], status="failed",
                            question=task.get("user_request", ""))
        started = time.monotonic()
        try:
            async with self._database.sessions() as session:
                collection = await self._ingest.create_collection(
                    session, f"eval-{task['task_id']}-{secrets.token_hex(4)}"
                )
                for document_id in task.get("input_documents", []):
                    await self._import_document(session, collection, document_id, package_dir)
                index = await self._ingest.rebuild_index(session, collection)
                run = QueryRun(
                    public_id=secrets.token_hex(16),
                    collection_id=collection.id,
                    question=task["user_request"],
                    index_version_id=index.id,
                )
                session.add(run)
                await session.flush()
                await self._answering.answer(session, run, task["user_request"])
                await session.commit()
                result.answer = run.answer
                result.insufficient = run.insufficient
                result.citations = await self._load_citations(session, run)
        except Exception as exc:  # noqa: BLE001 - 评测必须记录失败而不是中断
            result.status = "blocked"
            result.error = f"{type(exc).__name__}: {exc}"
            return result
        result.cost = {"wall_seconds": round(time.monotonic() - started, 2),
                       "usage": _usage_of(run)}
        result.checks = self._basic_checks(task, result)
        result.status = "passed" if all(c["passed"] for c in result.checks) else "failed"
        return result

    async def _import_document(self, session, collection, document_id: str,
                               package_dir: Path) -> None:
        sources = json.loads((package_dir / "sources.json").read_text(encoding="utf-8"))
        info = next(d for d in sources["documents"] if d["document_id"] == document_id)
        # sources.json 里的 path 相对评测套件根目录，不是 pagerag 子目录。
        path = package_dir.parent / info["path"]
        if not path.is_file():
            path = package_dir / info["path"]
        await self._ingest.add_document(
            session, collection, title=Path(info["path"]).stem,
            data=path.read_bytes(), filename=path.name,
            revision_id=info["revision_id"], source_kind=info.get("source_kind", "markdown"),
        )

    async def _load_citations(self, session, run: QueryRun) -> list[dict]:
        rows = (await session.execute(
            select(Chunk, Citation, Document, Revision)
            .join(Citation, Citation.chunk_id == Chunk.id)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Citation.query_run_id == run.id)
            .order_by(Citation.rank)
        )).all()
        return [
            {
                "rank": row.Citation.rank,
                "document": row.Document.title,
                "revision_id": row.Revision.revision_id,
                "lines": [row.Chunk.line_start, row.Chunk.line_end],
                "page": row.Chunk.page,
            }
            for row in rows
        ]

    def _basic_checks(self, task: dict, result: TaskResult) -> list[dict]:
        expect_refusal = ("无法回答" in task.get("title", "")
                          or task.get("expect_refusal") is True)
        checks = [
            {"name": "answer_present", "passed": bool(result.answer)}
        ]
        if expect_refusal:
            checks.append({"name": "refused_when_insufficient",
                           "passed": result.insufficient})
        else:
            checks.append({"name": "citations_present",
                           "passed": len(result.citations) > 0 and not result.insufficient})
        return checks


def _usage_of(run: QueryRun) -> dict:
    return run.usage or {}


def write_run(directory: Path, task_results: list[TaskResult], settings: Settings,
              suite_path: Path) -> Path:
    """按 evaluation/runs/<run_id>/ 约定落盘。"""
    run_id = time.strftime("%Y%m%d-%H%M%S")
    out = directory / run_id
    (out / "artifacts").mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "suite": str(suite_path),
        "parser_version": PARSER_VERSION,
        "chunker": {"version": CHUNKER_VERSION, "chars": settings.chunk_chars,
                    "max_chars": settings.chunk_max_chars},
        "retrieval": {"strategy": "R0-dense", "top_k": settings.retrieval_top_k},
        "answering": {"strategy": "G0-single", "model": settings.llm_model},
        "embedding": {"model": settings.embedding_model, "dimensions": settings.dim},
        "grader": "basic-v1（非空、引用、拒答；语义判分未实现）",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    with (out / "results.jsonl").open("w", encoding="utf-8") as handle:
        for item in task_results:
            handle.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    passed = sum(1 for r in task_results if r.status == "passed")
    lines = [f"# 评测运行 {run_id}", "",
             f"- 预定任务 {len(task_results)} 项，基本检查通过 {passed} 项。",
             "- 说明：本轮自动检查只覆盖答案非空、引用存在与拒答场景；标准事实覆盖与语义判分待校准。", ""]
    for r in task_results:
        lines.append(f"## {r.task_id}：{r.status}")
        if r.error:
            lines.append(f"- 错误：{r.error}")
        lines.append(f"- 引用 {len(r.citations)} 条；耗时 {r.cost.get('wall_seconds')}s")
        lines.append("")
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return out
