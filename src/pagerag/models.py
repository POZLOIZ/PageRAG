"""MySQL 数据模型：资料、修订、块、索引版本、任务与查询记录。

状态全部外置：导入、发布、查询各自有独立状态字段，进程重启后可从数据库恢复。
"""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # 当前对外可用的索引版本；NULL 表示尚无任何可查内容。
    active_index_version_id: Mapped[int | None] = mapped_column(ForeignKey("index_versions.id"),
                                                                 nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class IndexVersion(Base):
    """每个资料集合的一个可发布索引快照。查询开始时固定使用一个版本。"""

    __tablename__ = "index_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    version: Mapped[int] = mapped_column(Integer)
    # 该版本包含的修订（document public_id -> revision_id），发布前固定。
    revisions: Mapped[dict] = mapped_column(JSON, default=dict)
    # building / active / retired / failed / abandoned（放弃的未完成版本，
    # 块行与集合保留但不可续跑）；同一集合同时至多一个 active。
    status: Mapped[str] = mapped_column(String(20), default="building")
    milvus_collection: Mapped[str] = mapped_column(String(200))
    # 构建配置快照（分块/嵌入/解析版本，settings.index_build_config）：索引身份 =
    # 语料内容 + 这些配置。复用前必须比对，防止改参数后沿用旧索引。
    # 早期索引无此列数据（NULL），按不匹配处理，除非经核实后回填。
    build_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    activated_at: Mapped[datetime | None] = mapped_column(nullable=True)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    title: Mapped[str] = mapped_column(String(300))
    source_kind: Mapped[str] = mapped_column(String(30), default="markdown")
    # 文档级溯源元数据（来源机构/媒体、发布日期、原始 URL 等，按语料提供情况填）。
    # 证据呈现与按元数据过滤都从这里取；缺失时为 NULL，呈现层退回标题+行号。
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 资料是否可用；删除只改标记，不做物理删除。
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    current_revision_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Revision(Base):
    __tablename__ = "revisions"
    __table_args__ = (Index("ix_revisions_doc", "document_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    revision_id: Mapped[str] = mapped_column(String(64))
    snapshot_path: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(40))
    # received / parsing / chunked / embedded / indexed / failed
    status: Mapped[str] = mapped_column(String(20), default="received")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_revision", "revision_row_id"),
        Index("ix_chunks_index_version", "index_version_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    revision_row_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    # 块属于构建它的索引版本。重建不删旧行，旧答案的引用仍能回查当时的依据。
    # NULL 表示加列迁移前的历史行。
    index_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("index_versions.id"), nullable=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    # 原文定位：解析快照中的 Unicode 字符区间（终点不含）与行号；PDF 另有 1-based 页码。
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 切块时记录的真实边界：True 表示该端落在句子、围栏行或表格行中间，补邻块
    # 有意义；False 表示结构边界，例如段落、标题或句末。NULL 表示该列出现之前
    # 的旧行，读取侧回退到标点启发式。
    start_cut: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    end_cut: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 结构上下文（章节路径、表头），与原文位置分开保存：检索/展示需要补信息
    # 时用；引用仍指向 char_start/char_end 的原文区间，不把合成内容混进原文。
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 可恢复导入的两态：False 表示块已准备，行已落库但 Milvus 写入未确认；
    # True 表示已确认写入本索引的 Milvus 集合。主键就是 Milvus 主键，重跑按
    # 主键覆盖式 upsert，不重不漏。
    write_confirmed: Mapped[bool] = mapped_column(Boolean, default=False,
                                                  nullable=False)


class IngestJob(Base):
    __tablename__ = "ingest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"))
    revision_row_id: Mapped[int] = mapped_column(ForeignKey("revisions.id"))
    index_version_id: Mapped[int] = mapped_column(ForeignKey("index_versions.id"))
    # pending / running / succeeded / failed / stale（晚完成的旧版本任务）
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class User(Base):
    """最小登录账号。密码只保存 scrypt 派生值，不保存明文。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class UserSession(Base):
    """浏览器会话。数据库只保存令牌哈希，Cookie 里才是原始令牌。"""

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_token", "token_hash", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime] = mapped_column()


class CollectionOwner(Base):
    """集合归属。独立成表，避免给已有 collections 表加列。"""

    __tablename__ = "collection_owners"

    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ConversationOwner(Base):
    __tablename__ = "conversation_owners"

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id"), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Conversation(Base):
    """一次多轮对话。会话属于一个集合；删除会话只删会话与轮次映射，
    查询记录本身保留，便于审计与统计。"""

    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ConversationTurn(Base):
    """会话中的一轮问答。轮次只做排序与关联，问题与回答仍读 QueryRun，
    不复制正文，避免同一份回答出现两个版本。"""

    __tablename__ = "conversation_turns"
    __table_args__ = (
        Index("ix_conversation_turns_conversation", "conversation_id"),
        Index("ix_conversation_turns_run", "query_run_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    query_run_id: Mapped[int] = mapped_column(ForeignKey("query_runs.id"))
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class QueryRun(Base):
    __tablename__ = "query_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(32), unique=True)
    collection_id: Mapped[int] = mapped_column(ForeignKey("collections.id"))
    question: Mapped[str] = mapped_column(Text)
    # 连续追问：原始问题原样保存，检索使用消解指代后的独立问题。没有历史或
    # 改写不可用时为空。history 记录本次实际生效的对话摘要，已截断，避免事后
    # 按客户端最新状态重放。
    resolved_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    history: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # 本次查询固定的索引版本；回答与引用都基于它。
    index_version_id: Mapped[int] = mapped_column(ForeignKey("index_versions.id"))
    # running / succeeded / failed
    status: Mapped[str] = mapped_column(String(20), default="running")
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    insufficient: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    # 回答当时实际发送给模型的证据快照：渲染后的完整上下文、逐条头部（含来源/日期）
    # 与正文、以及完整 top-k 候选。随查询固化，后续修改 Document.meta 不改写历史；
    # 评分侧（确定性覆盖与 Ragas）必须用这里的上下文，而不是重新组装。
    evidence_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Citation(Base):
    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_run_id: Mapped[int] = mapped_column(ForeignKey("query_runs.id"))
    rank: Mapped[int] = mapped_column(Integer)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"))
    score: Mapped[float] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AnswerCheck(Base):
    """回答生成后的引用核验记录：逐条结论、引用编号、判定与修订结果。

    一次查询至多一条。核验失败不影响回答，status 记为 unavailable；
    资料不足的回答不调用模型，status 记为 skipped。
    """

    __tablename__ = "answer_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_run_id: Mapped[int] = mapped_column(
        ForeignKey("query_runs.id"), unique=True, nullable=False
    )
    # succeeded / partial / unavailable / skipped
    status: Mapped[str] = mapped_column(String(20), default="succeeded")
    # [{claim, citations, verdict, reason, repaired}]
    claims: Mapped[list] = mapped_column(JSON, default=list)
    # 四类判定计数与总数，页面和脚本共用
    issues: Mapped[dict] = mapped_column(JSON, default=dict)
    original_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    repair_count: Mapped[int] = mapped_column(Integer, default=0)
    timings: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
