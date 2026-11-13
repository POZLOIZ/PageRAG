"""导入与索引发布：上传只是收到文件；解析、编码、建索引、发布各有状态。

可恢复构建：

- 两态确认：块行落库并先提交，此时 `write_confirmed=False`，表示块已准备；
  然后编码并按主键幂等写入 Milvus；最后把 `write_confirmed` 设为 True，表示
  索引写入已确认。MySQL 与 Milvus 无法原子提交，块行先提交拿到稳定主键，
  再用覆盖式 upsert，保证任意中断位置都能重跑。主键不会因回滚而改变，已确认
  的块不会重做。
- 分批：篇数、块量、字符输入量三个上限任一到达就封批。只看篇数无法限制内存，
  因为条目长短差异很大。
- 恢复：同集合存在 building 版本且构建配置与当前一致时，复用该版本和同名
  集合续跑。配置不一致时报告错误，不混建；确实要放弃时调用 `abandon_index`。
  failed 版本不续跑，自动放弃后按当前文档新开版本，因为它的修订映射已经冻结，
  后来上传的文档不在其中。
- 发布：全部块确认后核对行数，并用稠密和全文两路探针查询验证，通过后才切换
  active 指针。失败时旧 active 版本保持不变。
"""

import hashlib
import logging
import secrets
import time
from pathlib import Path

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from pagerag import chunking, parsing
from pagerag.chunking import ChunkRecord
from pagerag.embeddings import EmbeddingClient
from pagerag.milvus_store import MilvusStore
from pagerag.models import Chunk, Collection, Document, IndexVersion, IngestJob, Revision, utcnow
from pagerag.settings import Settings, index_build_config

logger = logging.getLogger(__name__)

# 文档查询窗口：一次取多少篇（封批另按体积算，窗口只保证单次查询有界）。
_DOC_WINDOW = 200


class IngestError(RuntimeError):
    pass


def _new_id() -> str:
    return secrets.token_hex(16)


class _Batch:
    """一个写入批：篇数/块量/字符量任一到达上限即封批。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.revisions: list[Revision] = []
        self.chunks: list[Chunk] = []
        self._chars = 0

    def add(self, revision: Revision, rows: list[Chunk]) -> None:
        self.revisions.append(revision)
        self.chunks.extend(rows)
        self._chars += sum(len(row.text) for row in rows)

    def __bool__(self) -> bool:
        return bool(self.revisions)

    def full(self) -> bool:
        s = self._settings
        return (len(self.revisions) >= s.ingest_batch_docs
                or len(self.chunks) >= s.ingest_batch_chunks
                or self._chars >= s.ingest_batch_chars)

    def clear(self) -> None:
        self.revisions.clear()
        self.chunks.clear()
        self._chars = 0


class IngestService:
    def __init__(self, settings: Settings, store: MilvusStore, embedder: EmbeddingClient) -> None:
        self._settings = settings
        self._store = store
        self._embedder = embedder

    async def create_collection(self, session: AsyncSession, name: str) -> Collection:
        collection = Collection(public_id=_new_id(), name=name)
        session.add(collection)
        await session.flush()
        return collection

    def _snapshot_path(self, collection: Collection, document_public_id: str,
                       title: str, revision_id: str, filename: str) -> Path:
        # 快照文件名带文档与标题标识：不同文档可能共用同一修订号，不能互相覆盖。
        # revision_id 可能是 URL 等含路径分隔符的标识，必须同样净化，否则写出失败。
        def _safe_part(text: str, limit: int) -> str:
            return "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in text
            )[:limit]

        snapshot_dir = self._settings.data_dir / "snapshots" / collection.public_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        return (snapshot_dir / f"{document_public_id}-{_safe_part(title, 50)}-"
                f"{_safe_part(revision_id, 80)}{Path(filename).suffix}")

    async def _register_revision(
        self, session: AsyncSession, document: Document, collection: Collection,
        data: bytes, filename: str, revision_id: str,
        parsed: parsing.ParsedDocument,
    ) -> Revision:
        path = self._snapshot_path(collection, document.public_id,
                                   document.title, revision_id, filename)
        path.write_bytes(data)
        revision = Revision(
            document_id=document.id,
            revision_id=revision_id,
            snapshot_path=str(path),
            sha256=hashlib.sha256(data).hexdigest(),
            parser_version=parsed.parser_version,
            status="received",
        )
        session.add(revision)
        await session.flush()
        document.current_revision_id = revision.revision_id
        await session.flush()
        return revision

    async def add_document(
        self,
        session: AsyncSession,
        collection: Collection,
        title: str,
        data: bytes,
        filename: str,
        revision_id: str,
        source_kind: str = "markdown",
        meta: dict | None = None,
    ) -> Document:
        """保存原文快照并登记文档修订；索引构建由 rebuild_index 负责。"""
        parsed = parsing.parse_bytes(data, filename)
        document = Document(
            public_id=_new_id(),
            collection_id=collection.id,
            title=title,
            source_kind=parsed.source_kind or source_kind,
            meta=meta or None,
            current_revision_id=revision_id,
        )
        session.add(document)
        await session.flush()
        await self._register_revision(
            session, document, collection, data, filename, revision_id, parsed)
        return document

    async def add_revision(
        self,
        session: AsyncSession,
        document: Document,
        collection: Collection,
        data: bytes,
        filename: str,
        revision_id: str,
    ) -> Revision:
        """同一文档增加修订。

        只登记新快照并把 current 指针前移，旧修订的块行不动，旧答案的引用仍指向
        当时的块。配合 `rebuild_index`，新索引只含新 current 修订，更新后新答案
        使用新资料，旧引用仍可回查。
        """
        if revision_id == document.current_revision_id:
            raise IngestError(f"revision_id {revision_id!r} 已是该文档的当前修订")
        parsed = parsing.parse_bytes(data, filename)
        return await self._register_revision(
            session, document, collection, data, filename, revision_id, parsed)

    async def rebuild_index(self, session: AsyncSession, collection: Collection,
                            *, resume: bool = True) -> IndexVersion:
        """为集合全部有效文档的当前修订构建新索引并发布；中断后重跑自动续建。

        resume=True 时复用同集合未完成版本（building/failed）——已确认的块
        不重做，未确认的按主键重写；配置不一致则报错，避免混出两种索引。
        """
        if collection.active_index_version_id is not None:
            old = await session.get(IndexVersion, collection.active_index_version_id)
        else:
            old = None
        # 只取主键再按主键载入：revisions 映射随语料规模增长（16.5 万篇约十 MB
        # 一行），带 ORDER BY 的整体取行会连 JSON 一起排序，实测撑爆 MySQL 排序
        # 缓冲（1038 Out of sort memory）。排序只排小列，行内容按主键单独取。
        unfinished_id = (await session.execute(
            select(IndexVersion.id).where(
                IndexVersion.collection_id == collection.id,
                IndexVersion.status == "building",
            ).order_by(IndexVersion.version.desc()).limit(1)
        )).scalar_one_or_none()
        unfinished = (await session.get(IndexVersion, unfinished_id)
                      if unfinished_id is not None else None)
        expected_config = index_build_config(self._settings)
        if unfinished is not None:
            if not resume:
                raise IngestError(
                    f"存在未完成的索引版本 v{unfinished.version}；"
                    "续跑请保持 resume=True，确要放弃先 abandon_index")
            if (unfinished.build_config or None) != expected_config:
                raise IngestError(
                    f"未完成版本 v{unfinished.version} 的构建配置与当前设置不一致，"
                    "续跑会混出两种索引；请恢复原设置续跑，或 abandon_index 后重建")
            index = unfinished
        else:
            # 失败版本不再续跑。它的修订映射在创建时已冻结，之后上传的文档
            # 不在映射里，会被永久挡在索引外；它也可能留下无法嵌入的空块。
            # 自动放弃只改状态，块行与 Milvus 集合保留，旧引用仍可回查。
            failed = list((await session.execute(
                select(IndexVersion).where(
                    IndexVersion.collection_id == collection.id,
                    IndexVersion.status == "failed",
                )
            )).scalars().all())
            for version in failed:
                version.status = "abandoned"
            rows = (await session.execute(
                select(Document.id, Document.public_id, Revision.id, Revision.revision_id)
                .join(Revision, and_(Revision.document_id == Document.id,
                                     Revision.revision_id == Document.current_revision_id))
                .where(Document.collection_id == collection.id,
                       Document.available.is_(True))
                .order_by(Document.id)
            )).all()
            if not rows:
                raise IngestError("集合中没有可导入的文档")
            # 版本号取历史最大值 + 1：被放弃的版本可能比当前 active 版本还新，
            # 只按 active 递增会撞号。
            max_version = (await session.execute(
                select(func.max(IndexVersion.version)).where(
                    IndexVersion.collection_id == collection.id)
            )).scalar_one_or_none() or 0
            next_version = max_version + 1
            index = IndexVersion(
                collection_id=collection.id,
                version=next_version,
                milvus_collection=self._store.collection_name(collection.public_id, next_version),
                status="building",
                # 构建配置随版本固化：索引身份 = 语料内容 + 这些配置。
                build_config=expected_config,
                revisions={public_id: revision_id
                           for _, public_id, _, revision_id in rows},
            )
            session.add(index)
            await session.flush()
        # IngestJob 的外键要真实行：取本集合第一个可导入文档及其当前修订。
        first = (await session.execute(
            select(Document.id, Revision.id)
            .join(Revision, and_(Revision.document_id == Document.id,
                                 Revision.revision_id == Document.current_revision_id))
            .where(Document.collection_id == collection.id,
                   Document.available.is_(True))
            .order_by(Document.id).limit(1)
        )).first()
        job = IngestJob(
            collection_id=collection.id,
            document_id=first[0],
            revision_row_id=first[1],
            index_version_id=index.id,
            status="running",
        )
        session.add(job)
        # 立即提交：构建可能中断，building 版本与任务行必须先于写入落库，
        # 恢复路径才能找到它们（批间提交见 _write_batch）。
        await session.commit()
        index_id, job_id = index.id, job.id
        try:
            await self._build(session, collection, index)
            await self._verify_index(session, index)
        except Exception as exc:  # noqa: BLE001 - 状态必须落库，具体原因记入 error
            await session.rollback()
            index = await session.get(IndexVersion, index_id)
            job = await session.get(IngestJob, job_id)
            if index is not None:
                index.status = "failed"
                index.error = str(exc)[:2000]
            if job is not None:
                job.status = "failed"
                job.error = str(exc)[:2000]
            await session.commit()
            raise IngestError(str(exc)) from exc
        # 完整性核对通过后才发布；旧版本退役。晚完成的旧任务不能覆盖新版的保证
        # 由第二轮的版本校验补齐，当前同步路径不存在并发发布。
        index.status = "active"
        index.activated_at = utcnow()
        if old is not None:
            old.status = "retired"
        collection.active_index_version_id = index.id
        job.status = "succeeded"
        await session.flush()
        return index

    async def abandon_index(self, session: AsyncSession, index: IndexVersion) -> None:
        """放弃一个未完成版本：只改状态，块行与 Milvus 集合一律保留（旧引用
        可回查的保证不因放弃破坏）。被放弃版本不可再续跑。"""
        if index.status not in ("building", "failed"):
            raise IngestError("只能放弃 building/failed 状态的版本")
        index.status = "abandoned"
        await session.flush()

    async def _build(
        self, session: AsyncSession, collection: Collection, index: IndexVersion
    ) -> None:
        created = self._store.ensure(index.milvus_collection, self._settings.dim)
        logger.info("索引 v%s：%s集合 %s", index.version,
                    "新建" if created else "复用续跑 ", index.milvus_collection)
        batch = _Batch(self._settings)
        last_doc_id = 0
        while True:
            documents = (await session.execute(
                select(Document).where(
                    Document.collection_id == collection.id,
                    Document.available.is_(True),
                    Document.id > last_doc_id,
                ).order_by(Document.id).limit(_DOC_WINDOW)
            )).scalars().all()
            if not documents:
                break
            for document in documents:
                last_doc_id = document.id
                revision = await self._current_revision(session, document)
                if revision is None:
                    continue
                if index.revisions.get(document.public_id) != revision.revision_id:
                    continue  # 版本映射冻结后才变的修订不进本版本（下次重建再进）
                rows = await self._prepare_chunks(session, index, revision)
                if rows:
                    batch.add(revision, rows)
                if batch.full():
                    await self._write_batch(session, index, batch)
        if batch:
            await self._write_batch(session, index, batch)

    async def _prepare_chunks(
        self, session: AsyncSession, index: IndexVersion, revision: Revision
    ) -> list[Chunk]:
        """两态第一态：确保该修订在本索引有块行，返回待写入的未确认块。

        - 有未确认行（上次构建备好但没确认写入）→ 直接复用，不重切；
        - 没有未确认行但已有确认行 → 整篇已完成，返回空表跳过；
        - 一行都没有 → 重新解析切块（块行落库即"块已准备"）。
        """
        pending = (await session.execute(
            select(Chunk).where(
                Chunk.revision_row_id == revision.id,
                Chunk.index_version_id == index.id,
                Chunk.write_confirmed.is_(False),
            ).order_by(Chunk.chunk_index)
        )).scalars().all()
        if pending:
            return list(pending)
        done = (await session.execute(
            select(Chunk.id).where(
                Chunk.revision_row_id == revision.id,
                Chunk.index_version_id == index.id,
            ).limit(1)
        )).scalar_one_or_none()
        if done is not None:
            return []
        parsed = self._reparse(revision)
        records = chunking.chunk_parsed(parsed, self._settings)
        rows: list[Chunk] = []
        for record in records:
            # 重建不删旧块，旧答案的引用指向当时的行。新行标记所属索引版本，
            # 检索按版本取块。
            values = record_as_dict(record)
            # context 为空时不设置属性，INSERT 不带该列，存 SQL NULL。JSON 列会
            # 把 Python None 序列化成 JSON null 字面量，统计和过滤查询会把 JSON
            # null 当成非空值。
            context = values.pop("context", None)
            row = Chunk(revision_row_id=revision.id, index_version_id=index.id,
                        **values)
            if context:
                row.context = context
            session.add(row)
            rows.append(row)
        revision.status = "chunked"
        await session.flush()  # 分配数据库主键，Milvus 主键与之一一对应
        return rows

    async def _write_batch(
        self, session: AsyncSession, index: IndexVersion, batch: _Batch
    ) -> None:
        """两态第二态：块行先持久化，再编码、幂等 upsert、记录确认状态。

        本批块行先提交，块已准备状态和稳定主键必须在写入 Milvus 之前落库。
        然后编码、按主键 upsert、标记确认并提交。即使 Milvus 写入成功而确认
        事务回滚，重跑仍按同一主键覆盖写入，不会生成新主键，也不会留下无法核对
        的远端残留。代价是每批多一次提交，换来任意中断位置都可恢复。

        批内中断时，块行已落库但未确认，续跑按主键重写。若在块行提交之前中断，
        该批整体回滚，重切一遍且内容相同，同样安全。
        """
        rows = batch.chunks
        # 分段计时：块行落库、编码、远端写入、确认提交分开记录。
        started = time.monotonic()
        pairs = [(row.id, row.text) for row in rows]
        revision_ids = [revision.id for revision in batch.revisions]
        # 先把"块已准备"提交：块主键早于 Milvus 写入持久化（见 docstring）。
        await session.commit()
        prepared = time.monotonic()
        vectors = await self._embed_with_retry([text for _, text in pairs])
        embedded = time.monotonic()
        if vectors is None:
            await session.execute(
                update(Revision).where(Revision.id.in_(revision_ids))
                .values(status="failed", error="embedding failed after retries"))
            await session.commit()
            raise IngestError(
                f"向量编码重试 {self._settings.ingest_max_attempts} 次后仍失败")
        self._store.upsert(index.milvus_collection, [
            (row_id, vector, text)
            for (row_id, text), vector in zip(pairs, vectors, strict=True)])
        upserted = time.monotonic()
        await session.execute(
            update(Chunk).where(Chunk.id.in_([row_id for row_id, _ in pairs]))
            .values(write_confirmed=True))
        await session.execute(
            update(Revision).where(Revision.id.in_(revision_ids))
            .values(status="indexed", error=None))
        await session.commit()
        logger.info(
            "索引 v%s：写入确认 %d 修订 / %d 块（块行落库 %.1fs，编码 %.1fs，"
            "upsert %.1fs，确认提交 %.1fs）",
            index.version, len(revision_ids), len(pairs),
            prepared - started, embedded - prepared,
            upserted - embedded, time.monotonic() - upserted)
        batch.clear()

    async def _embed_with_retry(self, texts: list[str]) -> list[list[float]] | None:
        attempts = 0
        last_error: Exception | None = None
        while attempts < self._settings.ingest_max_attempts:
            attempts += 1
            try:
                return await self._embedder.embed_documents(texts)
            except Exception as exc:  # noqa: BLE001 - 网络抖动/超时也重试
                last_error = exc
                logger.warning("向量编码失败（第 %d 次）：%s", attempts, exc)
        logger.error("向量编码最终失败：%s", last_error)
        return None

    async def _verify_index(self, session: AsyncSession, index: IndexVersion) -> None:
        """发布前完整性核对（新索引确认完整可查询后才发布）：

        ① 未确认块必须为零；② flush 后 Milvus 行数＝数据库本索引块行数；
        ③ 稠密与全文两路各跑一次探针查询（用库内向量与块自身文本），都能返回。
        """
        unconfirmed = (await session.execute(
            select(func.count()).select_from(Chunk).where(
                Chunk.index_version_id == index.id,
                Chunk.write_confirmed.is_(False))
        )).scalar_one()
        if unconfirmed:
            raise IngestError(f"仍有 {unconfirmed} 个未确认块，不应进入发布核对")
        expected = (await session.execute(
            select(func.count()).select_from(Chunk).where(
                Chunk.index_version_id == index.id)
        )).scalar_one()
        if expected == 0:
            raise IngestError("集合中没有可索引的内容")
        self._store.flush(index.milvus_collection)
        actual = self._store.row_count(index.milvus_collection)
        if actual != expected:
            raise IngestError(
                f"完整性核对失败：数据库 {expected} 块，Milvus {actual} 行")
        probes = (await session.execute(
            select(Chunk.id, Chunk.text).where(Chunk.index_version_id == index.id)
            .order_by(Chunk.id).limit(5)
        )).all()
        probe = next((row for row in probes if (row.text or "").strip()), None)
        if probe is None:
            raise IngestError("找不到可用的探针块")
        vectors = self._store.fetch_embeddings(index.milvus_collection, [probe.id])
        if probe.id not in vectors:
            raise IngestError("探针失败：取不回样例块的向量")
        dense_hits = self._store.search(index.milvus_collection, vectors[probe.id], 1)
        sparse_hits = self._store.search_sparse(
            index.milvus_collection, probe.text[:64], 1)
        if not dense_hits or not sparse_hits:
            raise IngestError(
                f"探针查询失败：稠密 {len(dense_hits)} 命中、全文 {len(sparse_hits)} 命中")

    def _reparse(self, revision: Revision) -> parsing.ParsedDocument:
        path = Path(revision.snapshot_path)
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return parsing.parse_bytes(path.read_bytes(), path.name)
        text = path.read_text(encoding="utf-8")
        kind = "md" if suffix in (".md", ".markdown") else "txt"
        return parsing.ParsedDocument(
            text, [parsing.Segment(text, 0)],
            parser_version=revision.parser_version, source_kind=kind,
        )

    async def _current_revision(self, session: AsyncSession, document: Document) -> Revision | None:
        if document.current_revision_id is None:
            return None
        return (await session.execute(
            select(Revision).where(
                Revision.document_id == document.id,
                Revision.revision_id == document.current_revision_id,
            )
        )).scalar_one_or_none()


def record_as_dict(record: ChunkRecord) -> dict:
    return {
        "chunk_index": record.chunk_index,
        "text": record.text,
        "content_hash": record.content_hash,
        "char_start": record.char_start,
        "char_end": record.char_end,
        "line_start": record.line_start,
        "line_end": record.line_end,
        "page": record.page,
        "start_cut": record.start_cut,
        "end_cut": record.end_cut,
        "context": record.context,
    }
