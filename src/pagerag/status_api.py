"""只读状态接口、后台重建入口与最小统计。

这些接口只读现有表，不复制状态：索引版本、导入任务、查询记录和引用都来自
原来的表。统计在 Python 里对最近 200 次查询汇总，避免在 MySQL 上写窗口函数。
"""

import math
import re
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag import __version__
from pagerag.auth import can_access_collection, visible_collections_statement
from pagerag.deps import get_session
from pagerag.models import (
    AnswerCheck,
    Chunk,
    Citation,
    Collection,
    Conversation,
    ConversationTurn,
    Document,
    IndexVersion,
    IngestJob,
    QueryRun,
    Revision,
)
from pagerag.rebuild import RebuildConflict
from pagerag.settings import (
    CHUNKER_VERSION,
    PARSER_VERSION,
    index_build_config,
)

router = APIRouter(prefix="/api", tags=["status"])

RETRIEVAL_TIMING_KEYS = {
    "embed_query", "milvus_search", "sparse_search", "load_evidence",
    "rerank", "expand_neighbors",
}


def _iso(value) -> str | None:
    # MySQL DATETIME 读回来没有时区，直接给前端会被当成本地时间。
    # 统一补上 UTC 标记，浏览器再按本地时区显示。
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _elapsed_seconds(start, end=None) -> float | None:
    if start is None:
        return None
    if end is None:
        from pagerag.models import utcnow
        end = utcnow()
    # MySQL DATETIME 读回来没有时区，按 UTC 补齐后再相减。
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return round((end - start).total_seconds(), 1)


async def _active_index(session: AsyncSession,
                        collection: Collection) -> IndexVersion | None:
    """优先查状态为 active 的版本，集合指针过期时仍能显示实际在用版本。"""
    index = (await session.execute(
        select(IndexVersion).where(
            IndexVersion.collection_id == collection.id,
            IndexVersion.status == "active",
        ).order_by(IndexVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    if index is not None:
        return index
    if collection.active_index_version_id is not None:
        return await session.get(IndexVersion, collection.active_index_version_id)
    return None


async def _collection_or_404(session: AsyncSession, public_id: str) -> Collection:
    collection = (await session.execute(
        select(Collection).where(Collection.public_id == public_id)
    )).scalar_one_or_none()
    if collection is None:
        raise HTTPException(404, "集合不存在")
    return collection


async def _latest_job(session: AsyncSession, collection_id: int) -> IngestJob | None:
    return (await session.execute(
        select(IngestJob).where(IngestJob.collection_id == collection_id)
        .order_by(IngestJob.id.desc()).limit(1)
    )).scalar_one_or_none()


async def _index_counts(session: AsyncSession, index_id: int) -> tuple[int, int]:
    total = (await session.execute(
        select(func.count()).select_from(Chunk).where(
            Chunk.index_version_id == index_id)
    )).scalar_one()
    confirmed = (await session.execute(
        select(func.count()).select_from(Chunk).where(
            Chunk.index_version_id == index_id,
            Chunk.write_confirmed.is_(True))
    )).scalar_one()
    return int(total), int(confirmed)


async def _collection_payload(session: AsyncSession, collection: Collection) -> dict:
    documents = (await session.execute(
        select(func.count()).select_from(Document).where(
            Document.collection_id == collection.id)
    )).scalar_one()
    available = (await session.execute(
        select(func.count()).select_from(Document).where(
            Document.collection_id == collection.id,
            Document.available.is_(True))
    )).scalar_one()
    active = await _active_index(session, collection)
    job = await _latest_job(session, collection.id)
    queries = (await session.execute(
        select(func.count()).select_from(QueryRun).where(
            QueryRun.collection_id == collection.id)
    )).scalar_one()
    conversations = (await session.execute(
        select(func.count()).select_from(Conversation).where(
            Conversation.collection_id == collection.id)
    )).scalar_one()
    chunks_total = 0
    chunks_confirmed = 0
    if active is not None:
        chunks_total, chunks_confirmed = await _index_counts(session, active.id)
    return {
        "collection_id": collection.public_id,
        "name": collection.name,
        "created_at": _iso(collection.created_at),
        "documents_total": int(documents),
        "documents_available": int(available),
        "conversations_total": int(conversations),
        "chunks_total": chunks_total,
        "chunks_confirmed": chunks_confirmed,
        "active_index_version": active.version if active else None,
        "active_index_status": active.status if active else None,
        "last_job_status": job.status if job else None,
        "last_job_error": job.error if job else None,
        "last_job_at": _iso(job.updated_at) if job else None,
        "query_count": int(queries),
    }


def _user_id(request: Request) -> int | None:
    user = getattr(request.state, "user", None)
    return user.id if user is not None else None


@router.get("/collections")
async def list_collections(request: Request,
                           session: AsyncSession = Depends(get_session)) -> dict:
    user_id = _user_id(request)
    statement = (visible_collections_statement(user_id) if user_id is not None
                 else select(Collection))
    collections = (await session.execute(
        statement.order_by(Collection.id.desc()).limit(100)
    )).scalars().all()
    return {"collections": [
        await _collection_payload(session, collection) for collection in collections
    ]}


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: str,
                         session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    return await _collection_payload(session, collection)


@router.get("/collections/{collection_id}/documents")
async def list_documents(collection_id: str,
                         session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    active = await _active_index(session, collection)
    rows = (await session.execute(
        select(Document, Revision)
        .outerjoin(Revision, and_(
            Revision.document_id == Document.id,
            Revision.revision_id == Document.current_revision_id))
        .where(Document.collection_id == collection.id)
        .order_by(Document.id)
        .limit(500)
    )).all()
    documents = []
    for document, revision in rows:
        chunk_count = 0
        if revision is not None and active is not None:
            chunk_count = (await session.execute(
                select(func.count()).select_from(Chunk).where(
                    Chunk.revision_row_id == revision.id,
                    Chunk.index_version_id == active.id)
            )).scalar_one()
        documents.append({
            "document_id": document.public_id,
            "title": document.title,
            "source_kind": document.source_kind,
            "available": document.available,
            "current_revision_id": document.current_revision_id,
            "revision_status": revision.status if revision else None,
            "revision_error": revision.error if revision else None,
            "chunk_count": int(chunk_count),
            "meta": document.meta,
            "created_at": _iso(document.created_at),
        })
    return {"collection_id": collection.public_id, "documents": documents}


@router.get("/collections/{collection_id}/index-versions")
async def list_index_versions(collection_id: str,
                              session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    versions = (await session.execute(
        select(IndexVersion).where(IndexVersion.collection_id == collection.id)
        .order_by(IndexVersion.version.desc()).limit(50)
    )).scalars().all()
    items = []
    for index in versions:
        total, confirmed = await _index_counts(session, index.id)
        items.append({
            "index_version_id": index.id,
            "version": index.version,
            "status": index.status,
            "milvus_collection": index.milvus_collection,
            "documents_total": len(index.revisions or {}),
            "chunks_total": total,
            "chunks_confirmed": confirmed,
            "error": index.error,
            "created_at": _iso(index.created_at),
            "activated_at": _iso(index.activated_at),
        })
    return {"collection_id": collection.public_id, "index_versions": items}


@router.get("/collections/{collection_id}/ingest-jobs")
async def list_ingest_jobs(collection_id: str,
                           session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    rows = (await session.execute(
        select(IngestJob, IndexVersion.version)
        .join(IndexVersion, IngestJob.index_version_id == IndexVersion.id)
        .where(IngestJob.collection_id == collection.id)
        .order_by(IngestJob.id.desc()).limit(50)
    )).all()
    return {"collection_id": collection.public_id, "jobs": [
        {
            "job_id": job.id,
            "index_version": version,
            "status": job.status,
            "attempts": job.attempts,
            "error": job.error,
            "created_at": _iso(job.created_at),
            "updated_at": _iso(job.updated_at),
        }
        for job, version in rows
    ]}


@router.get("/collections/{collection_id}/progress")
async def get_progress(collection_id: str,
                       session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    index = (await session.execute(
        select(IndexVersion).where(
            IndexVersion.collection_id == collection.id,
            IndexVersion.status.in_(["building", "failed"]),
        ).order_by(IndexVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    if index is None:
        index = await _active_index(session, collection)
    if index is None:
        return {
            "collection_id": collection.public_id,
            "state": "empty",
            "percent": 0,
            "documents_total": 0,
            "documents_done": 0,
            "chunks_total": 0,
            "chunks_confirmed": 0,
            "error": None,
        }
    revisions = index.revisions or {}
    documents_total = len(revisions)
    documents_done = 0
    wanted = set(revisions.values())
    if wanted:
        rows = (await session.execute(
            select(Document.public_id, Revision.revision_id, Revision.status)
            .join(Revision, Revision.document_id == Document.id)
            .where(Document.collection_id == collection.id,
                   Revision.revision_id.in_(wanted))
        )).all()
        documents_done = sum(
            1 for public_id, revision_id, status in rows
            if revisions.get(public_id) == revision_id and status == "indexed"
        )
    chunks_total, chunks_confirmed = await _index_counts(session, index.id)
    if index.status == "active":
        percent = 100.0
    elif chunks_total:
        percent = round(chunks_confirmed * 100.0 / chunks_total, 1)
    else:
        percent = 0.0
    job = (await session.execute(
        select(IngestJob).where(IngestJob.index_version_id == index.id)
        .order_by(IngestJob.id.desc()).limit(1)
    )).scalar_one_or_none()
    if index.status == "building":
        elapsed = _elapsed_seconds(index.created_at)
    elif job is not None:
        elapsed = _elapsed_seconds(job.created_at, job.updated_at)
    else:
        elapsed = None
    return {
        "collection_id": collection.public_id,
        "state": index.status,
        "index_version_id": index.id,
        "version": index.version,
        "percent": percent,
        "documents_total": documents_total,
        "documents_done": documents_done,
        "chunks_total": chunks_total,
        "chunks_confirmed": chunks_confirmed,
        "resume_available": index.status in ("building", "failed"),
        "error": index.error or (job.error if job else None),
        "job_status": job.status if job else None,
        "elapsed_seconds": elapsed,
        "created_at": _iso(index.created_at),
    }


def _verification_issue_count(check: AnswerCheck | None) -> int:
    issues = (check.issues if check is not None else None) or {}
    return sum(int(issues.get(key) or 0)
               for key in ("partial", "unsupported", "uncited"))


def total_duration_ms(timings: dict | None) -> float | None:
    """一次查询的总耗时。核验的 total 已包含 verify、repair、reverify，
    不能再把各分项相加，否则同一段核验会被计算两遍。"""
    if not timings:
        return None
    total = 0.0
    seen = False
    for key, value in timings.items():
        if not isinstance(value, (int, float)):
            continue
        if key in {"llm_attempts", "llm_endpoint"}:
            continue
        if key.startswith("verify_"):
            continue
        total += float(value)
        seen = True
    verify_total = timings.get("verify_total")
    if isinstance(verify_total, (int, float)):
        total += float(verify_total)
        seen = True
    else:
        for key, value in timings.items():
            if key.startswith("verify_") and isinstance(value, (int, float)):
                total += float(value)
                seen = True
    return round(total * 1000, 1) if seen else None


async def _citation_counts(session: AsyncSession,
                           run_ids: list[int]) -> dict[int, int]:
    if not run_ids:
        return {}
    rows = (await session.execute(
        select(Citation.query_run_id, func.count())
        .where(Citation.query_run_id.in_(run_ids))
        .group_by(Citation.query_run_id)
    )).all()
    return {int(run_id): int(count) for run_id, count in rows}


def _plain_excerpt(text: str, limit: int = 200) -> str:
    """把回答压成一行摘要，去掉标题、列表、引用和代码围栏等 Markdown 记号。"""
    if not text:
        return ""
    value = re.sub(r"```.*?```", " ", text, flags=re.S)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.M)
    value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.M)
    value = re.sub(r"^\s*\d+[.)]\s+", "", value, flags=re.M)
    value = re.sub(r"^\s*>\s?", "", value, flags=re.M)
    value = re.sub(r"\*\*|__|~~", "", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", value)
    value = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"\1", value)
    value = re.sub(r"\[\d+\]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    # 去掉引用角标后常留下中文标点前的空格，摘要里读起来会断开。
    value = re.sub(r"\s+([，。、；：！？）】》」』])", r"\1", value)
    value = re.sub(r"([（【《「『])\s+", r"\1", value)
    if len(value) > limit:
        value = value[:limit].rstrip() + "…"
    return value


def _query_item(run: QueryRun, check: AnswerCheck | None,
                collection_public_id: str | None,
                collection_name: str | None, index_version: int | None,
                conversation_id: str | None,
                citation_count: int) -> dict:
    snapshot = run.evidence_snapshot or {}
    return {
        "query_id": run.public_id,
        "question": run.question,
        "collection_id": collection_public_id,
        "collection_name": collection_name,
        "conversation_id": conversation_id,
        "status": run.status,
        "insufficient": run.insufficient,
        "verification_status": check.status if check is not None else None,
        "verification_issue_count": _verification_issue_count(check),
        "answer_excerpt": _plain_excerpt(run.answer or ""),
        "failure_reason": run.failure_reason,
        "index_version": index_version,
        "evidence_count": len(snapshot.get("items") or []),
        "citation_count": citation_count,
        "timings": run.timings or {},
        "usage": run.usage or {},
        "duration_ms": total_duration_ms(run.timings),
        "created_at": _iso(run.created_at),
    }


def _query_statement(collection_id: str = ""):
    return (
        select(QueryRun, AnswerCheck,
               Collection.public_id.label("collection_public_id"),
               Collection.name.label("collection_name"),
               IndexVersion.version.label("index_version"),
               Conversation.public_id.label("conversation_public_id"))
        .join(Collection, Collection.id == QueryRun.collection_id)
        .outerjoin(AnswerCheck, AnswerCheck.query_run_id == QueryRun.id)
        .outerjoin(IndexVersion, IndexVersion.id == QueryRun.index_version_id)
        .outerjoin(ConversationTurn,
                   ConversationTurn.query_run_id == QueryRun.id)
        .outerjoin(Conversation,
                   Conversation.id == ConversationTurn.conversation_id)
    )


@router.get("/queries")
async def list_queries(request: Request, collection_id: str = "",
                       limit: int = 50,
                       session: AsyncSession = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 200))
    statement = _query_statement()
    user_id = _user_id(request)
    if collection_id:
        collection = await _collection_or_404(session, collection_id)
        if user_id is not None and not await can_access_collection(
                session, collection, user_id):
            raise HTTPException(403, "没有访问这个资料的权限")
        statement = statement.where(QueryRun.collection_id == collection.id)
    elif user_id is not None:
        visible = visible_collections_statement(user_id).with_only_columns(
            Collection.id)
        statement = statement.where(QueryRun.collection_id.in_(visible))
    rows = (await session.execute(
        statement.order_by(QueryRun.id.desc()).limit(limit)
    )).all()
    counts = await _citation_counts(session, [row[0].id for row in rows])
    return {"queries": [
        {
            **_query_item(row[0], row[1], row.collection_public_id,
                          row.collection_name, row.index_version,
                          row.conversation_public_id,
                          counts.get(row[0].id, 0)),
        }
        for row in rows
    ]}


def _stats_from_runs(runs: list[QueryRun]) -> dict:
    total = len(runs)
    answered = sum(1 for run in runs
                   if run.status == "succeeded" and not run.insufficient)
    insufficient = sum(1 for run in runs
                       if run.status == "succeeded" and run.insufficient)
    failed = sum(1 for run in runs if run.status == "failed")
    generate_ms = [_seconds_to_ms(run.timings.get("generate"))
                   for run in runs if run.timings]
    generate_ms = [value for value in generate_ms if value is not None]
    retrieval_ms = []
    for run in runs:
        total_seconds = sum(
            float(value) for key, value in (run.timings or {}).items()
            if key in RETRIEVAL_TIMING_KEYS and isinstance(value, (int, float))
        )
        if total_seconds:
            retrieval_ms.append(round(total_seconds * 1000, 1))
    input_tokens = [int(run.usage.get("input_tokens", 0)) for run in runs
                    if run.usage]
    output_tokens = [int(run.usage.get("output_tokens", 0)) for run in runs
                     if run.usage]
    verification_tokens = [
        int(value) for run in runs for key, value in (run.usage or {}).items()
        if key.startswith("verify_") and "tokens" in key
        and isinstance(value, (int, float))
    ]
    return {
        "query_count": total,
        "answered_count": answered,
        "insufficient_count": insufficient,
        "failed_count": failed,
        "insufficient_rate": round(insufficient / total, 4) if total else 0.0,
        "failure_rate": round(failed / total, 4) if total else 0.0,
        "generate_ms_p50": _percentile(generate_ms, 50),
        "generate_ms_p95": _percentile(generate_ms, 95),
        "retrieval_ms_p50": _percentile(retrieval_ms, 50),
        "retrieval_ms_p95": _percentile(retrieval_ms, 95),
        "avg_input_tokens": round(sum(input_tokens) / len(input_tokens), 1)
        if input_tokens else 0.0,
        "avg_output_tokens": round(sum(output_tokens) / len(output_tokens), 1)
        if output_tokens else 0.0,
        "verification_tokens_total": sum(verification_tokens),
    }


@router.get("/stats")
async def get_stats(request: Request, collection_id: str = "",
                    session: AsyncSession = Depends(get_session)) -> dict:
    statement = select(QueryRun)
    user_id = _user_id(request)
    if collection_id:
        collection = await _collection_or_404(session, collection_id)
        if user_id is not None and not await can_access_collection(
                session, collection, user_id):
            raise HTTPException(403, "没有访问这个资料的权限")
        statement = statement.where(QueryRun.collection_id == collection.id)
    elif user_id is not None:
        visible = visible_collections_statement(user_id).with_only_columns(
            Collection.id)
        statement = statement.where(QueryRun.collection_id.in_(visible))
    runs = (await session.execute(
        statement.order_by(QueryRun.id.desc()).limit(200)
    )).scalars().all()
    return {"collection_id": collection_id or None, **_stats_from_runs(runs)}


async def _query_trend(session: AsyncSession,
                       user_id: int | None) -> list[dict]:
    days = 14
    # MySQL DATETIME 存的是无时区 UTC，过滤值也保持 naive，避免驱动在
    # 绑定时带偏移量。
    today = datetime.now(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    start = today - timedelta(days=days - 1)
    statement = select(QueryRun.created_at, QueryRun.status,
                       QueryRun.insufficient).where(
        QueryRun.created_at >= start)
    if user_id is not None:
        visible = visible_collections_statement(user_id).with_only_columns(
            Collection.id)
        statement = statement.where(QueryRun.collection_id.in_(visible))
    rows = (await session.execute(statement)).all()
    buckets = {}
    for offset in range(days):
        key = (start + timedelta(days=offset)).date().isoformat()
        buckets[key] = {"date": key, "answered": 0, "insufficient": 0,
                        "failed": 0, "other": 0, "total": 0}
    for created_at, status, insufficient in rows:
        if created_at is None:
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        bucket = buckets.get(created_at.date().isoformat())
        if bucket is None:
            continue
        bucket["total"] += 1
        if status == "failed":
            bucket["failed"] += 1
        elif status == "succeeded" and insufficient:
            bucket["insufficient"] += 1
        elif status == "succeeded":
            bucket["answered"] += 1
        else:
            bucket["other"] += 1
    return list(buckets.values())


@router.get("/overview")
async def get_overview(request: Request,
                       session: AsyncSession = Depends(get_session)) -> dict:
    user_id = _user_id(request)
    statement = (visible_collections_statement(user_id) if user_id is not None
                 else select(Collection))
    collections = (await session.execute(
        statement.order_by(Collection.id.desc()).limit(100)
    )).scalars().all()
    collection_payloads = [
        await _collection_payload(session, collection)
        for collection in collections
    ]
    query_statement = _query_statement()
    if user_id is not None:
        visible = visible_collections_statement(user_id).with_only_columns(
            Collection.id)
        query_statement = query_statement.where(QueryRun.collection_id.in_(visible))
    rows = (await session.execute(
        query_statement.order_by(QueryRun.id.desc()).limit(200)
    )).all()
    runs = [row[0] for row in rows]
    checks = [row[1] for row in rows]
    counts = await _citation_counts(session, [row[0].id for row in rows])
    recent = [
        {
            **_query_item(row[0], row[1], row.collection_public_id,
                          row.collection_name, row.index_version,
                          row.conversation_public_id,
                          counts.get(row[0].id, 0)),
        }
        for row in rows[:8]
    ]
    checked = [check for check in checks if check is not None]
    issue_claims = sum(_verification_issue_count(check) for check in checked)
    tasks = request.app.state.rebuild_manager.list()
    visible_ids = {item["collection_id"] for item in collection_payloads}
    tasks = [task.as_dict() for task in tasks
             if task.collection_public_id in visible_ids]
    return {
        "generated_at": _iso(datetime.now(UTC)),
        "window": {"label": "最近 200 次查询", "query_sample": len(runs)},
        "totals": {
            "collections": len(collection_payloads),
            "documents": sum(item["documents_total"]
                             for item in collection_payloads),
            "documents_available": sum(item["documents_available"]
                                       for item in collection_payloads),
            "chunks": sum(item["chunks_total"] for item in collection_payloads),
            "queries": sum(item["query_count"] for item in collection_payloads),
            "conversations": sum(item["conversations_total"]
                                 for item in collection_payloads),
        },
        "stats": _stats_from_runs(runs),
        "verification": {
            "checked": len(checked),
            "clean": sum(1 for check in checked if check.status == "succeeded"),
            "partial": sum(1 for check in checked if check.status == "partial"),
            "unavailable": sum(1 for check in checked
                               if check.status == "unavailable"),
            "issue_claims": issue_claims,
        },
        "trend": await _query_trend(session, user_id),
        "collections": collection_payloads,
        "recent_queries": recent,
        "active_tasks": tasks,
        "system": {
            "version": __version__,
            "auth_enabled": bool(request.app.state.settings.auth_enabled),
            "active_tasks": len(tasks),
        },
    }


@router.post("/collections/{collection_id}/rebuild", status_code=202)
async def start_rebuild(collection_id: str, request: Request,
                        session: AsyncSession = Depends(get_session)) -> dict:
    collection = await _collection_or_404(session, collection_id)
    manager = request.app.state.rebuild_manager
    try:
        task = await manager.start(collection.public_id)
    except RebuildConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return task.as_dict()


async def _visible_public_ids(session: AsyncSession,
                              user_id: int | None) -> set[str] | None:
    if user_id is None:
        return None
    rows = (await session.execute(
        visible_collections_statement(user_id).with_only_columns(
            Collection.public_id)
    )).scalars().all()
    return set(rows)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request,
                   session: AsyncSession = Depends(get_session)) -> dict:
    task = request.app.state.rebuild_manager.get(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    visible = await _visible_public_ids(session, _user_id(request))
    if visible is not None and task.collection_public_id not in visible:
        raise HTTPException(403, "没有访问这个资料的权限")
    return task.as_dict()


@router.get("/tasks")
async def list_tasks(request: Request,
                     session: AsyncSession = Depends(get_session)) -> dict:
    visible = await _visible_public_ids(session, _user_id(request))
    tasks = [task.as_dict()
             for task in request.app.state.rebuild_manager.list()
             if visible is None or task.collection_public_id in visible]
    return {"tasks": tasks}


@router.get("/documents/{document_id}")
async def get_document(document_id: str, request: Request,
                       revision_id: str = "",
                       index_version_id: int | None = None,
                       offset: int = 0, limit: int = 200,
                       session: AsyncSession = Depends(get_session)) -> dict:
    """文档详情：当前修订、索引版本与分页块列表，供知识库详情抽屉使用。"""
    document = (await session.execute(
        select(Document).where(Document.public_id == document_id)
    )).scalar_one_or_none()
    if document is None:
        raise HTTPException(404, "文档不存在")
    collection = await session.get(Collection, document.collection_id)
    if collection is None:
        raise HTTPException(404, "集合不存在")
    user_id = _user_id(request)
    if user_id is not None and not await can_access_collection(
            session, collection, user_id):
        raise HTTPException(403, "没有访问这个资料的权限")
    revisions = (await session.execute(
        select(Revision).where(Revision.document_id == document.id)
        .order_by(Revision.id.desc())
    )).scalars().all()
    selected_revision = None
    if revision_id:
        selected_revision = next(
            (item for item in revisions if item.revision_id == revision_id),
            None)
    else:
        selected_revision = next(
            (item for item in revisions
             if item.revision_id == document.current_revision_id), None)
    if selected_revision is None and revisions:
        selected_revision = revisions[0]

    active = await _active_index(session, collection)
    index = active
    if index_version_id is not None:
        index = await session.get(IndexVersion, index_version_id)
        if index is None or index.collection_id != collection.id:
            raise HTTPException(404, "索引版本不存在")

    offset = max(0, offset)
    limit = max(1, min(limit, 500))
    chunks_total = 0
    chunk_rows = []
    if selected_revision is not None and index is not None:
        chunks_total = (await session.execute(
            select(func.count()).select_from(Chunk).where(
                Chunk.revision_row_id == selected_revision.id,
                Chunk.index_version_id == index.id)
        )).scalar_one()
        chunk_rows = (await session.execute(
            select(Chunk).where(
                Chunk.revision_row_id == selected_revision.id,
                Chunk.index_version_id == index.id)
            .order_by(Chunk.chunk_index)
            .offset(offset).limit(limit)
        )).scalars().all()

    revision_counts = {}
    if index is not None and revisions:
        rows = (await session.execute(
            select(Chunk.revision_row_id, func.count())
            .where(Chunk.revision_row_id.in_([item.id for item in revisions]),
                   Chunk.index_version_id == index.id)
            .group_by(Chunk.revision_row_id)
        )).all()
        revision_counts = {int(row_id): int(count) for row_id, count in rows}

    return {
        "document_id": document.public_id,
        "collection_id": collection.public_id,
        "collection_name": collection.name,
        "title": document.title,
        "source_kind": document.source_kind,
        "available": document.available,
        "meta": document.meta,
        "created_at": _iso(document.created_at),
        "current_revision_id": document.current_revision_id,
        "active_index_version": active.version if active else None,
        "index_version": index.version if index is not None else None,
        "index_version_id": index.id if index is not None else None,
        "revisions": [
            {
                "revision_id": item.revision_id,
                "status": item.status,
                "sha256": item.sha256,
                "parser_version": item.parser_version,
                "error": item.error,
                "created_at": _iso(item.created_at),
                "is_current": item.revision_id == document.current_revision_id,
                "chunk_count": revision_counts.get(item.id, 0),
            }
            for item in revisions
        ],
        "selected_revision_id": (selected_revision.revision_id
                                 if selected_revision is not None else None),
        "chunks_total": int(chunks_total),
        "offset": offset,
        "limit": limit,
        "chunks": [
            {
                "chunk_id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "text": chunk.text if document.available else None,
                "context": chunk.context,
                "char_start": chunk.char_start,
                "char_end": chunk.char_end,
                "char_count": chunk.char_end - chunk.char_start,
                "line_start": chunk.line_start,
                "line_end": chunk.line_end,
                "page": chunk.page,
                "content_hash": chunk.content_hash,
                "write_confirmed": chunk.write_confirmed,
                "start_cut": chunk.start_cut,
                "end_cut": chunk.end_cut,
            }
            for chunk in chunk_rows
        ],
    }


@router.get("/system")
async def get_system(request: Request,
                     session: AsyncSession = Depends(get_session)) -> dict:
    """只读运行配置：显式白名单，不返回密钥、内部地址或文件路径。"""
    settings = request.app.state.settings
    user_id = _user_id(request)
    index_statement = (
        select(IndexVersion, Collection)
        .join(Collection, Collection.id == IndexVersion.collection_id)
        .where(IndexVersion.status == "active")
    )
    if user_id is not None:
        visible = visible_collections_statement(user_id).with_only_columns(
            Collection.id)
        index_statement = index_statement.where(
            IndexVersion.collection_id.in_(visible))
    index_rows = (await session.execute(
        index_statement.order_by(Collection.id)
    )).all()
    visible_ids = await _visible_public_ids(session, user_id)
    tasks = [task.as_dict()
             for task in request.app.state.rebuild_manager.list()
             if visible_ids is None or task.collection_public_id in visible_ids]
    generation_configured = bool(settings.llm_base_url and settings.llm_model)
    fallback_configured = bool(
        settings.llm_primary_base_url and settings.llm_primary_model)
    return {
        "service": {
            "name": "PageRAG",
            "version": __version__,
            "status": "running",
            "auth_enabled": bool(settings.auth_enabled),
        },
        "models": {
            "generation": {
                "model": settings.llm_model or None,
                "configured": generation_configured,
            },
            "generation_fallback": {
                "model": settings.llm_primary_model or None,
                "configured": fallback_configured,
            },
            "embedding": {
                "model": settings.embedding_model or None,
                "dimensions": settings.dim,
                "configured": bool(
                    settings.embedding_base_url and settings.embedding_model),
            },
            "rerank": {
                "model": settings.rerank_model or None,
                "configured": bool(settings.rerank_url and settings.rerank_model),
            },
        },
        "configured": {
            "database": bool(settings.database_url),
            "milvus": bool(settings.milvus_uri),
            "generation": generation_configured or fallback_configured,
            "embedding": bool(
                settings.embedding_base_url and settings.embedding_model),
            "rerank": bool(settings.rerank_url and settings.rerank_model),
        },
        "retrieval": {
            "strategy": settings.retrieval_strategy,
            "top_k": settings.retrieval_top_k,
            "rrf_k": settings.rrf_k,
            "bm25_top_n": settings.bm25_top_n,
            "rerank_pool_size": settings.rerank_pool_size,
            "bm25_weight": settings.bm25_weight,
        },
        "chunking": {
            "parser_version": PARSER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "chunk_chars": settings.chunk_chars,
            "chunk_max_chars": settings.chunk_max_chars,
        },
        "evidence": {
            "unit_limit": settings.evidence_unit_limit,
            "context_token_budget": settings.context_token_budget,
            "neighbor_expansion": settings.evidence_neighbor_expansion,
            "document_limit": settings.evidence_document_limit,
            "context_mode": settings.evidence_context_mode,
            "subquery_group_limit": settings.evidence_subquery_group_limit,
        },
        "query": {
            "decomposition_enabled": settings.query_decomposition_enabled,
            "max_subqueries": settings.query_decomposition_max_subqueries,
            "channel_max_attempts": settings.query_channel_max_attempts,
            "channel_backoff_seconds": settings.query_channel_backoff_seconds,
            "llm_timeout_seconds": settings.llm_request_timeout_seconds,
            "llm_max_attempts": settings.llm_max_attempts,
        },
        "verification": {
            "enabled": settings.answer_verification_enabled,
            "repair_enabled": settings.answer_verification_repair_enabled,
            "timeout_seconds": settings.answer_verification_timeout_seconds,
            "max_claims": settings.answer_verification_max_claims,
        },
        "runtime": {
            "auth_enabled": bool(settings.auth_enabled),
            "max_upload_bytes": settings.max_upload_bytes,
            "max_upload_mb": round(settings.max_upload_bytes / 1024 / 1024, 1),
        },
        "index_build_config": index_build_config(settings),
        "active_indexes": [
            {
                "collection_id": collection.public_id,
                "collection_name": collection.name,
                "version": index.version,
                "status": index.status,
                "build_config": index.build_config,
                "activated_at": _iso(index.activated_at),
            }
            for index, collection in index_rows
        ],
        "active_tasks": tasks,
    }


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str,
                          session: AsyncSession = Depends(get_session)) -> dict:
    document = (await session.execute(
        select(Document).where(Document.public_id == document_id)
    )).scalar_one_or_none()
    if document is None:
        raise HTTPException(404, "文档不存在")
    document.available = False
    await session.commit()
    return {"document_id": document.public_id, "available": False}


def _seconds_to_ms(value) -> float | None:
    return round(float(value) * 1000, 1) if isinstance(value, (int, float)) else None


def _percentile(values: list[float], percent: int) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1,
                          math.ceil(percent / 100 * len(ordered)) - 1))
    return round(ordered[position], 1)
