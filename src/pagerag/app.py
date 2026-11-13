"""应用装配：HTTP 接口与评测入口共用同一服务层。

接口覆盖首轮需要：创建集合、导入文档、查询、查看状态与引用回读。
状态页与控制台共用 app.state 里的服务，不复制数据库会话工厂。
"""

import asyncio
import base64
import binascii
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag import __version__
from pagerag.answering import AnsweringService
from pagerag.auth import (
    SESSION_COOKIE,
    can_access_collection,
    can_access_conversation,
    resolve_session,
    set_collection_owner,
    set_conversation_owner,
    visible_collections_statement,
)
from pagerag.auth_api import router as auth_router
from pagerag.conversations import (
    append_turn,
    create_conversation,
    delete_conversation,
    get_conversation,
    list_conversations,
    recent_history,
    rename_conversation,
)
from pagerag.db import Database
from pagerag.deps import get_session
from pagerag.embeddings import EmbeddingClient
from pagerag.ingest import IngestError, IngestService
from pagerag.milvus_store import MilvusStore
from pagerag.models import (
    AnswerCheck,
    Chunk,
    Citation,
    Collection,
    Conversation,
    ConversationTurn,
    Document,
    IndexVersion,
    QueryRun,
    Revision,
)
from pagerag.rebuild import RebuildManager
from pagerag.retrieval import IndexRef, RetrievalService
from pagerag.settings import Settings
from pagerag.status_api import router as status_router
from pagerag.status_api import total_duration_ms

STATIC_DIR = Path(__file__).parent / "static"


def _iso(value) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _resource_allowed(session: AsyncSession, path: str,
                            user_id: int) -> bool:
    """按资源路径做权限检查；只检查能识别的单个资源，列表接口在内部过滤。"""
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "api":
        parts = parts[1:]
    if not parts:
        return True
    head = parts[0]
    if head == "collections" and len(parts) >= 2:
        collection = (await session.execute(
            select(Collection).where(Collection.public_id == parts[1])
        )).scalar_one_or_none()
        return collection is None or await can_access_collection(
            session, collection, user_id)
    if head == "conversations" and len(parts) >= 2:
        conversation = (await session.execute(
            select(Conversation).where(Conversation.public_id == parts[1])
        )).scalar_one_or_none()
        return conversation is None or await can_access_conversation(
            session, conversation, user_id)
    if head == "queries" and len(parts) >= 2:
        run = (await session.execute(
            select(QueryRun).where(QueryRun.public_id == parts[1])
        )).scalar_one_or_none()
        if run is None:
            return True
        collection = await session.get(Collection, run.collection_id)
        return collection is not None and await can_access_collection(
            session, collection, user_id)
    if head == "evidence" and len(parts) >= 2:
        try:
            chunk_id = int(parts[1])
        except ValueError:
            return True
        collection = (await session.execute(
            select(Collection)
            .select_from(Chunk)
            .join(Revision, Revision.id == Chunk.revision_row_id)
            .join(Document, Document.id == Revision.document_id)
            .join(Collection, Collection.id == Document.collection_id)
            .where(Chunk.id == chunk_id)
        )).scalar_one_or_none()
        return collection is None or await can_access_collection(
            session, collection, user_id)
    if head == "documents" and len(parts) >= 2:
        collection = (await session.execute(
            select(Collection)
            .select_from(Document)
            .join(Collection, Collection.id == Document.collection_id)
            .where(Document.public_id == parts[1])
        )).scalar_one_or_none()
        return collection is None or await can_access_collection(
            session, collection, user_id)
    return True


def _decode_upload(content_b64: str, filename: str, settings: Settings) -> bytes:
    """解码并校验上传内容，把文件问题变成可读的 4xx，而不是裸 500。"""
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "文件内容不是有效的 base64 编码") from exc
    if not data:
        raise HTTPException(400, "文件内容为空，请选择有内容的文件")
    if len(data) > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes // (1024 * 1024)
        raise HTTPException(413, f"文件超过 {limit_mb} MB 上限，请拆分或压缩后再上传")
    suffix = Path(filename or "").suffix.lower()
    if suffix not in {".md", ".markdown", ".txt", ".pdf"}:
        raise HTTPException(
            400, "只支持 Markdown、TXT 和带文本层的 PDF 文件")
    return data


def build_app(settings: Settings, database: Database, store: MilvusStore,
              embedder: EmbeddingClient) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 新表（会话与轮次）由模型声明补齐；已有表不受 create_all 影响。
        from pagerag.models import Base

        async with database.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        # 单进程演示环境：进程重启时不可能还有活跃的流式请求，统一把遗留的
        # running 查询标成失败，避免页面永远显示进行中。
        async with database.sessions() as session:
            await session.execute(
                update(QueryRun).where(QueryRun.status == "running")
                .values(status="failed", failure_reason="服务重启，生成已中断")
            )
            await session.commit()
        yield

    app = FastAPI(title="PageRAG", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        if not settings.auth_enabled:
            request.state.user = None
            return await call_next(request)
        path = request.url.path
        public = (
            path == "/" or path == "/healthz" or path.startswith("/static")
            or path.startswith("/docs") or path in (
                "/openapi.json", "/redoc", "/auth/status", "/auth/session",
                "/auth/me", "/auth/login", "/auth/register", "/auth/logout",
                "/favicon.ico")
        )
        token = request.cookies.get(SESSION_COOKIE)
        user = None
        if token:
            async with database.sessions() as auth_session:
                user = await resolve_session(auth_session, token)
        request.state.user = user
        if not public and user is None:
            return JSONResponse({"detail": "请先登录"}, status_code=401)
        if user is not None:
            async with database.sessions() as auth_session:
                allowed = await _resource_allowed(auth_session, path, user.id)
            if not allowed:
                return JSONResponse({"detail": "没有访问这个资料的权限"},
                                    status_code=403)
        return await call_next(request)

    retrieval = RetrievalService(store, embedder, settings)
    answering = AnsweringService(settings, retrieval)
    ingest = IngestService(settings, store, embedder)
    app.state.settings = settings
    app.state.database = database
    app.state.store = store
    app.state.embedder = embedder
    app.state.retrieval = retrieval
    app.state.answering = answering
    app.state.ingest = ingest
    app.state.rebuild_manager = RebuildManager(database, ingest)
    app.include_router(status_router)
    app.include_router(auth_router)

    class CollectionIn(BaseModel):
        name: str

    class DocumentIn(BaseModel):
        title: str
        filename: str
        content_b64: str
        revision_id: str = "r1"
        source_kind: str = "markdown"
        # 文档级溯源元数据（按语料/来源实际有的填，如 author/published_at/path；
        # 没有就不传，不虚构字段）。
        meta: dict | None = None

    class RevisionIn(BaseModel):
        # 同一文档的新版本。
        # 与当前修订相同会被拒（同号无意义且会盖住 current 指针）。
        filename: str
        content_b64: str
        revision_id: str

    class HistoryTurn(BaseModel):
        # 连续追问的轻量历史：只带问题与上一轮回答，客户端负责给出最近几轮。
        # 服务端再截到 6 轮/500 字符，超出部分不参与指代消解。
        question: str
        answer: str = ""

    class QueryIn(BaseModel):
        question: str
        history: list[HistoryTurn] = []
        # 服务端会话：传入后历史由服务端读取，客户端 history 不再使用。
        conversation_id: str | None = None

    class ConversationIn(BaseModel):
        collection_id: str
        question: str = ""

    class FederatedQueryIn(BaseModel):
        """跨集合问答：不指定集合时检索当前用户可见的全部已建索引集合。"""
        question: str
        collection_ids: list[str] | None = None

    class ConversationPatch(BaseModel):
        title: str

    async def _collection_or_404(session: AsyncSession,
                                 public_id: str) -> Collection:
        collection = (await session.execute(
            select(Collection).where(Collection.public_id == public_id)
        )).scalar_one_or_none()
        if collection is None:
            raise HTTPException(404, "集合不存在")
        return collection

    async def _conversation_or_404(session: AsyncSession,
                                   public_id: str) -> Conversation:
        conversation = await get_conversation(session, public_id)
        if conversation is None:
            raise HTTPException(404, "会话不存在")
        return conversation

    async def _conversation_history(
        session: AsyncSession, collection: Collection, payload: QueryIn,
        user,
    ) -> tuple[Conversation | None, list[dict]]:
        if not payload.conversation_id:
            return None, [turn.model_dump() for turn in payload.history]
        conversation = await _conversation_or_404(
            session, payload.conversation_id)
        if conversation.collection_id != collection.id:
            raise HTTPException(409, "会话不属于这个集合")
        if user is not None and not await can_access_conversation(
                session, conversation, user.id):
            raise HTTPException(403, "没有访问这个对话的权限")
        return conversation, await recent_history(session, conversation.id)

    async def _conversation_summary(conversation: Conversation) -> dict:
        return {
            "conversation_id": conversation.public_id,
            "title": conversation.title,
            "created_at": _iso(conversation.created_at),
            "updated_at": _iso(conversation.updated_at),
        }

    @app.post("/conversations")
    async def open_conversation(payload: ConversationIn, request: Request,
                                session: AsyncSession = Depends(get_session)):
        collection = await _collection_or_404(session, payload.collection_id)
        user = getattr(request.state, "user", None)
        if user is not None and not await can_access_collection(
                session, collection, user.id):
            raise HTTPException(403, "没有访问这个资料的权限")
        conversation = await create_conversation(
            session, collection, payload.question)
        await set_conversation_owner(
            session, conversation, getattr(request.state, "user", None))
        await session.commit()
        return {
            "conversation_id": conversation.public_id,
            "title": conversation.title,
            "collection_id": collection.public_id,
        }

    @app.get("/conversations")
    async def conversations(
        collection_id: str, request: Request, q: str = "", limit: int = 40,
        session: AsyncSession = Depends(get_session),
    ):
        collection = await _collection_or_404(session, collection_id)
        user = getattr(request.state, "user", None)
        if user is not None and not await can_access_collection(
                session, collection, user.id):
            raise HTTPException(403, "没有访问这个资料的权限")
        rows = await list_conversations(
            session, collection.id, q, limit, user.id if user else None)
        return {
            "collection_id": collection.public_id,
            "conversations": [
                {
                    "conversation_id": row["conversation_id"],
                    "title": row["title"],
                    "turn_count": row["turn_count"],
                    "last_question": row["last_question"],
                    "created_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"]),
                }
                for row in rows
            ],
        }

    @app.get("/conversations/{conversation_public_id}")
    async def conversation_detail(
        conversation_public_id: str,
        session: AsyncSession = Depends(get_session),
    ):
        conversation = await _conversation_or_404(
            session, conversation_public_id)
        collection = await session.get(Collection, conversation.collection_id)
        rows = (await session.execute(
            select(ConversationTurn, QueryRun)
            .join(QueryRun, QueryRun.id == ConversationTurn.query_run_id)
            .where(ConversationTurn.conversation_id == conversation.id)
            .order_by(ConversationTurn.position)
        )).all()
        return {
            **(await _conversation_summary(conversation)),
            "collection_id": collection.public_id if collection else None,
            "turns": [
                {"position": row.ConversationTurn.position,
                 "query": await _query_detail(session, row.QueryRun)}
                for row in rows
            ],
        }

    @app.patch("/conversations/{conversation_public_id}")
    async def update_conversation(
        conversation_public_id: str, payload: ConversationPatch,
        session: AsyncSession = Depends(get_session),
    ):
        conversation = await _conversation_or_404(
            session, conversation_public_id)
        try:
            await rename_conversation(session, conversation, payload.title)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        await session.commit()
        return await _conversation_summary(conversation)

    @app.delete("/conversations/{conversation_public_id}")
    async def remove_conversation(
        conversation_public_id: str,
        session: AsyncSession = Depends(get_session),
    ):
        conversation = await _conversation_or_404(
            session, conversation_public_id)
        await delete_conversation(session, conversation)
        await session.commit()
        return {"deleted": True}

    @app.post("/collections")
    async def create_collection(payload: CollectionIn, request: Request,
                                session: AsyncSession = Depends(get_session)):
        collection = await ingest.create_collection(session, payload.name)
        await set_collection_owner(
            session, collection, getattr(request.state, "user", None))
        await session.commit()
        return {"collection_id": collection.public_id, "name": collection.name}

    @app.post("/collections/{collection_public_id}/documents")
    async def add_document(collection_public_id: str, payload: DocumentIn,
                           session: AsyncSession = Depends(get_session)):

        collection = (await session.execute(
            select(Collection).where(Collection.public_id == collection_public_id)
        )).scalar_one_or_none()
        if collection is None:
            raise HTTPException(404, "集合不存在")

        data = _decode_upload(payload.content_b64, payload.filename, settings)
        try:
            document = await ingest.add_document(
                session, collection, payload.title, data, payload.filename,
                payload.revision_id, payload.source_kind, meta=payload.meta,
            )
        except IngestError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, f"文件无法解析：{exc}") from exc
        await session.commit()
        return {"document_id": document.public_id, "revision_id": payload.revision_id,
                "status": "received"}

    @app.post("/documents/{document_public_id}/revisions")
    async def add_revision(document_public_id: str, payload: RevisionIn,
                           session: AsyncSession = Depends(get_session)):
        """同一文档增加修订：登记新快照并前移 current 指针，旧修订块行不动
        （旧引用可回查）；新内容要在随后的 rebuild 之后进入新答案。"""
        document = (await session.execute(
            select(Document).where(Document.public_id == document_public_id)
        )).scalar_one_or_none()
        if document is None:
            raise HTTPException(404, "文档不存在")
        collection = await session.get(Collection, document.collection_id)
        data = _decode_upload(payload.content_b64, payload.filename, settings)
        try:
            revision = await ingest.add_revision(
                session, document, collection, data, payload.filename,
                payload.revision_id,
            )
        except IngestError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, f"文件无法解析：{exc}") from exc
        await session.commit()
        return {"document_id": document.public_id,
                "revision_id": revision.revision_id, "status": revision.status}

    @app.post("/collections/{collection_public_id}/rebuild")
    async def rebuild(collection_public_id: str,
                      session: AsyncSession = Depends(get_session)):

        collection = (await session.execute(
            select(Collection).where(Collection.public_id == collection_public_id)
        )).scalar_one_or_none()
        if collection is None:
            raise HTTPException(404, "集合不存在")
        try:
            index = await ingest.rebuild_index(session, collection)
        except IngestError as exc:
            await session.commit()
            raise HTTPException(502, f"索引构建失败：{exc}") from exc
        await session.commit()
        return {"index_version": index.version, "status": index.status}

    async def _create_run(session: AsyncSession, collection: Collection,
                          question: str) -> tuple[QueryRun, object]:
        if collection.active_index_version_id is None:
            raise HTTPException(409, "集合尚无可用的已发布索引")
        index = await session.get(IndexVersion, collection.active_index_version_id)
        if index is None:
            raise HTTPException(409, "集合的当前索引不存在，请重新构建")
        run = QueryRun(
            public_id=secrets.token_hex(16),
            collection_id=collection.id,
            question=question,
            index_version_id=index.id,
        )
        session.add(run)
        await session.flush()
        return run, index

    @app.post("/collections/{collection_public_id}/queries")
    async def query(collection_public_id: str, payload: QueryIn, request: Request,
                    session: AsyncSession = Depends(get_session)):
        collection = await _collection_or_404(session, collection_public_id)
        conversation, history = await _conversation_history(
            session, collection, payload, getattr(request.state, "user", None))
        run, _index = await _create_run(session, collection, payload.question)
        if conversation is not None:
            await append_turn(session, conversation, run)
        try:
            await answering.answer(session, run, payload.question, history)
        except Exception as exc:  # noqa: BLE001
            run.status = "failed"
            run.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
            await session.commit()
            raise HTTPException(502, f"查询失败：{run.failure_reason}") from exc
        await session.commit()
        return await _query_detail(session, run)

    async def _interrupt_run(run_id: int, reason: str) -> None:
        """请求被取消时用独立会话收尾，避免遗留永久 running 的查询记录。"""
        async with database.sessions() as fresh:
            await fresh.execute(
                update(QueryRun)
                .where(QueryRun.id == run_id, QueryRun.status == "running")
                .values(status="failed", failure_reason=reason)
            )
            await fresh.commit()

    @app.post("/collections/{collection_public_id}/queries/stream")
    async def query_stream(collection_public_id: str, payload: QueryIn,
                           request: Request,
                           session: AsyncSession = Depends(get_session)):
        """SSE 流式问答：检索、生成、核验三个阶段，正文逐段返回。"""
        collection = await _collection_or_404(session, collection_public_id)
        conversation, history = await _conversation_history(
            session, collection, payload, getattr(request.state, "user", None))
        run, _index = await _create_run(session, collection, payload.question)
        if conversation is not None:
            await append_turn(session, conversation, run)
        # 先把运行记录与会话轮次落库，页面刷新后仍能看到这条正在生成的会话。
        await session.commit()

        async def events():
            try:
                async for item in answering.stream_answer(
                        session, run, payload.question, history):
                    yield _sse(item["event"], item["data"])
                detail = await _query_detail(session, run)
                await session.commit()
                yield _sse("done", detail)
            except (asyncio.CancelledError, GeneratorExit):
                # 浏览器关闭或点了停止：请求作用域会话会随连接一起丢弃，
                # 必须用新会话把已落库的 running 行改成失败。
                await _interrupt_run(run.id, "生成被中断，未产出完整回答")
                raise
            except Exception as exc:  # noqa: BLE001
                run.status = "failed"
                run.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
                await session.commit()
                yield _sse("error", {"message": f"查询失败：{run.failure_reason}"})

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/queries/stream")
    async def federated_query_stream(payload: FederatedQueryIn, request: Request,
                                     session: AsyncSession = Depends(get_session)):
        """跨集合 SSE 问答。

        每次只嵌入一次、只重排一次：各集合各自召回候选，合并后统一重排。
        查询记录挂在第一个集合上便于审计，不写入任何对话；跨集合会话的
        持久化留到后续版本。
        """
        question = str(payload.question or "").strip()
        if not question:
            raise HTTPException(422, "问题不能为空")
        user = getattr(request.state, "user", None)
        if payload.collection_ids:
            wanted = list(dict.fromkeys(payload.collection_ids))
            rows = (await session.execute(
                select(Collection).where(Collection.public_id.in_(wanted))
            )).scalars().all()
            by_public = {row.public_id: row for row in rows}
            collections = []
            for public_id in wanted:
                collection = by_public.get(public_id)
                if collection is None:
                    raise HTTPException(404, f"集合不存在：{public_id}")
                if user is not None and not await can_access_collection(
                        session, collection, user.id):
                    raise HTTPException(403, "没有访问这个资料的权限")
                collections.append(collection)
        else:
            statement = (visible_collections_statement(user.id)
                         if user is not None else select(Collection))
            collections = list((await session.execute(
                statement.where(Collection.active_index_version_id.is_not(None))
                .order_by(Collection.id.desc())
            )).scalars().all())
        usable = [item for item in collections
                  if item.active_index_version_id is not None]
        if not usable:
            raise HTTPException(409, "没有可检索的资料集合，请先导入资料并构建索引")
        index_rows = (await session.execute(
            select(IndexVersion.id, IndexVersion.version,
                   IndexVersion.milvus_collection)
            .where(IndexVersion.id.in_(
                [item.active_index_version_id for item in usable]))
        )).all()
        by_index = {row.id: row for row in index_rows}
        indexes = [
            IndexRef(id=row.id, version=row.version,
                     milvus_collection=row.milvus_collection)
            for row in (by_index.get(item.active_index_version_id)
                        for item in usable)
            if row is not None
        ]
        if not indexes:
            raise HTTPException(409, "资料集合的索引记录缺失，请重新构建")
        anchor = usable[0]
        run, _index = await _create_run(session, anchor, question)
        await session.commit()

        async def events():
            try:
                async for item in answering.stream_answer(
                        session, run, question, None, indexes):
                    yield _sse(item["event"], item["data"])
                detail = await _query_detail(session, run)
                await session.commit()
                yield _sse("done", detail)
            except (asyncio.CancelledError, GeneratorExit):
                await _interrupt_run(run.id, "生成被中断，未产出完整回答")
                raise
            except Exception as exc:  # noqa: BLE001
                run.status = "failed"
                run.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
                await session.commit()
                yield _sse("error", {"message": f"查询失败：{run.failure_reason}"})

        return StreamingResponse(
            events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _scope_payload(session: AsyncSession, scope: dict | None) -> dict | None:
        """把快照里的索引编号补成集合名，跨集合查询详情才能读懂。"""
        if not scope:
            return None
        indexes = scope.get("indexes") or []
        ids = [int(item["index_id"]) for item in indexes
               if item.get("index_id") is not None]
        if not ids:
            return scope
        rows = (await session.execute(
            select(IndexVersion.id, IndexVersion.version,
                   Collection.public_id, Collection.name)
            .join(Collection, Collection.id == IndexVersion.collection_id)
            .where(IndexVersion.id.in_(ids))
        )).all()
        names = {
            int(row.id): {"collection_id": row.public_id,
                          "collection_name": row.name,
                          "version": row.version}
            for row in rows
        }
        return {
            **scope,
            "indexes": [
                {**item, **names.get(int(item["index_id"]), {})}
                for item in indexes
            ],
        }

    async def _run_payload(session: AsyncSession, run: QueryRun) -> dict:
        check = (await session.execute(
            select(AnswerCheck).where(AnswerCheck.query_run_id == run.id)
        )).scalar_one_or_none()
        verification = None
        if check is not None:
            verification = {
                "status": check.status,
                "claims": check.claims,
                "issues": check.issues,
                "original_answer": check.original_answer,
                "final_answer": check.final_answer,
                "repair_count": check.repair_count,
                "timings": check.timings,
                "usage": check.usage,
                "error": check.error,
            }
        index = await session.get(IndexVersion, run.index_version_id)
        conversation_public_id = (await session.execute(
            select(Conversation.public_id)
            .join(ConversationTurn, ConversationTurn.conversation_id == Conversation.id)
            .where(ConversationTurn.query_run_id == run.id)
        )).scalar_one_or_none()
        created_at = run.created_at
        if created_at is not None and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return {
            "query_id": run.public_id,
            "scope": await _scope_payload(
                session, (run.evidence_snapshot or {}).get("scope")),
            "conversation_id": conversation_public_id,
            "status": run.status,
            "answer": run.answer,
            "insufficient": run.insufficient,
            # 对外的版本号是人可读的 v1、v2；数据库行号单独返回，避免页面把
            # 行号当成版本号显示。
            "index_version": index.version if index is not None else None,
            "index_version_id": run.index_version_id,
            # 连续追问：改写后的独立检索问题（无历史或退回原句时为空），
            # 与 question 一起返回，便于确认这次实际检索了什么。
            "resolved_question": run.resolved_question,
            "timings": run.timings,
            "duration_ms": total_duration_ms(run.timings),
            "usage": run.usage,
            "failure_reason": run.failure_reason,
            "created_at": created_at.isoformat() if created_at else None,
            "verification": verification,
        }

    async def _query_detail(session: AsyncSession, run: QueryRun) -> dict:
        """单次查询的完整页面数据：回答、核验、证据快照与逐条引用。"""
        citations = (await session.execute(
            select(Chunk, Citation, Document, Revision)
            .join(Citation, Citation.chunk_id == Chunk.id)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Citation.query_run_id == run.id)
            .order_by(Citation.rank)
        )).all()
        snapshot = run.evidence_snapshot or {}
        return {
            **await _run_payload(session, run),
            "question": run.question,
            # 本次实际生效的对话摘要。历史回答只用于理解指代，不是证据。
            "history": run.history,
            "evidence": {
                "render_version": snapshot.get("render_version"),
                "items": snapshot.get("items", []),
                "candidates": snapshot.get("candidates", []),
                "pool": snapshot.get("pool", []),
                "context": snapshot.get("context", ""),
                "subqueries": snapshot.get("subqueries", []),
                "retrieval_status": snapshot.get("retrieval_status"),
                "retrieval_strategy": snapshot.get("retrieval_strategy"),
                "retrieval_errors": snapshot.get("retrieval_errors", []),
            },
            "citations": [
                {
                    "rank": row.Citation.rank,
                    # 块 id：引用回读 /evidence/{chunk_id} 的入口（此前只给行号，
                    # 前端无法顺着回读原文）。
                    "chunk_id": row.Chunk.id,
                    "document_title": row.Document.title,
                    "revision_id": row.Revision.revision_id,
                    "line_start": row.Chunk.line_start,
                    "line_end": row.Chunk.line_end,
                    "page": row.Chunk.page,
                    "context": row.Chunk.context,
                    "score": row.Citation.score,
                    # 删除后的资料只保留引用记录，不再把正文返回给页面。
                    "available": row.Document.available,
                    "text": row.Chunk.text if row.Document.available else None,
                }
                for row in citations
            ],
        }

    @app.get("/queries/{query_public_id}")
    async def get_query(query_public_id: str, session: AsyncSession = Depends(get_session)):
        run = (await session.execute(
            select(QueryRun).where(QueryRun.public_id == query_public_id)
        )).scalar_one_or_none()
        if run is None:
            raise HTTPException(404, "查询不存在")
        return await _query_detail(session, run)

    @app.get("/evidence/{chunk_id}")
    async def get_evidence(chunk_id: int, session: AsyncSession = Depends(get_session)):
        """引用回读：按块定位返回原文片段与可用状态（删除的资料不暴露正文）。"""
        row = (await session.execute(
            select(Chunk, Revision, Document)
            .join(Revision, Chunk.revision_row_id == Revision.id)
            .join(Document, Revision.document_id == Document.id)
            .where(Chunk.id == chunk_id)
        )).first()
        if row is None:
            raise HTTPException(404, "证据不存在")
        return {
            "document_title": row.Document.title,
            "revision_id": row.Revision.revision_id,
            "available": row.Document.available,
            "text": row.Chunk.text if row.Document.available else None,
            "context": row.Chunk.context,
            "line_start": row.Chunk.line_start,
            "line_end": row.Chunk.line_end,
            "page": row.Chunk.page,
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "stage": "implemented", "version": __version__}

    @app.get("/", include_in_schema=False)
    async def console() -> FileResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            raise HTTPException(404, "控制台尚未安装")
        return FileResponse(index)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
