from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 解析器与分块算法的版本号：原文偏移与块结构的可解释性依赖它，改动规则时必须更新。
PARSER_VERSION = "md-txt-pdf-v1"
CHUNKER_VERSION = "structural-v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PAGERAG_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = ""
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: SecretStr = SecretStr("")
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: SecretStr = SecretStr("")
    # 可选备援通道。主通道限流或不可用时切换；三项留空表示不启用。
    # llm_session 是部分兼容服务用于复用提示缓存的会话标识。
    llm_primary_base_url: str = ""
    llm_primary_model: str = ""
    llm_primary_api_key: SecretStr = SecretStr("")
    llm_session: str = "pagerag"
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    embedding_dimensions: int | None = None
    rerank_url: str = ""
    rerank_model: str = ""
    rerank_api_key: SecretStr = SecretStr("")
    mineru_base_url: str = ""

    # 原文件快照与日志的保存位置；数据库只存路径与哈希。
    data_dir: Path = Path("data")
    # 单次上传的原始文件大小上限。演示入口使用 JSON base64，过大的文件会先
    # 进入应用内存，因此必须在服务端拒绝，而不是只靠前端提示。
    max_upload_bytes: int = 50 * 1024 * 1024

    # 分块与检索参数均可配置，默认值是当前实现的选择，不是通用最优。
    # chunk_chars 是相邻完整段落的合并软目标；chunk_max_chars 是单个段落的硬上限。
    # 超过硬上限先按句末切，单句仍然超长才允许句中切。嵌入和重排服务通常有
    # 输入长度限制，部署时应确认实际值，避免静默截断。
    chunk_chars: int = 500
    chunk_max_chars: int = 3000
    retrieval_top_k: int = 30
    # dense 为纯向量；hybrid-rrf 对两路候选做 RRF 后截断；
    # hybrid-rerank 对两路候选去重后交给重排模型。
    retrieval_strategy: str = "hybrid-rerank"
    rrf_k: int = 60
    bm25_top_n: int = 30
    # hybrid-rerank 送入重排的并集上限。两路各 30 条，去重后最多 60 条。
    rerank_pool_size: int = 60
    # 融合权重。1.0 为对称融合；小于 1 时降低关键词单路前段项对向量榜
    # 中后段项的挤出。
    bm25_weight: float = 1.0
    # 证据预算约束最终送入生成模型的证据单元，不是召回规模。单元由命中块和
    # 同章节相邻块组成，token 按 cl100k 近似并包含渲染头，不含系统提示和问题。
    # 每篇文档的第一轮配额由 evidence_document_limit 控制，剩余名额按名次回填。
    evidence_unit_limit: int = 20
    context_token_budget: int = 6000
    # 邻块扩展会把候选块同修订前后各 N 块加入候选并重新排序，再按分数与
    # 原名次合并。只扩展当前索引版本的块；window 模式下不生效。
    evidence_neighbor_expansion: int = 1
    # 同一篇文档最多占多少个证据单元，0 表示不限制。第一轮按配额填，
    # 剩余名额按名次回填。
    evidence_document_limit: int = 3
    # 证据组装方式：
    # - window：命中块前后各补一块组成窗口，邻块不再单独重排；
    # - neighbor-rerank：邻块加入候选并单独重排，按分数与原名次合并。
    # 两种方式不叠加。
    evidence_context_mode: str = "window"
    # 查询分解默认关闭。开启后模型把问题拆成至多 N 条子查询，各走一路召回，
    # 合并候选后仍按原问题重排。子查询只补召回，不参与排序；失败退回单查询。
    query_decomposition_enabled: bool = False
    query_decomposition_max_subqueries: int = 3
    # 多查询合并候选池的封顶。120 是重排客户端两批的整数倍，避免按原问题
    # 名次过早截断只有子查询能召回的块。
    decomposition_pool_limit: int = 120
    # 可选的子查询缓存，JSON 文件格式为 {问题文本: [子查询]}。只缓存调用成功
    # 的结果，失败不缓存；留空表示不启用。
    decomposition_cache: str = ""

    # 生成后逐条判断引用原文是否支持结论，发现问题时最多做一次局部修订。
    # 核验失败不阻断回答，结果单独落表。
    answer_verification_enabled: bool = True
    answer_verification_repair_enabled: bool = True
    answer_verification_timeout_seconds: float = 45.0
    answer_verification_max_claims: int = 12
    # 子问题证据分组：每个子问题第一轮最多占几条证据，剩余名额按原名次回填。
    # 只在查询分解开启且确实产生子查询时生效；0 表示不限制。
    evidence_subquery_group_limit: int = 6

    # 查询链路的有界重试与超时。向量/关键词/重排失败时保留另一路结果，
    # 主模型超时或连接失败时切备援；重试次数是上限，不无限循环。
    query_channel_max_attempts: int = 2
    query_channel_backoff_seconds: float = 0.5
    llm_request_timeout_seconds: float = 60.0
    llm_max_attempts: int = 2

    # 多用户登录：默认关闭，保持已有脚本与本地单用户工作流不变。开启后
    # /api 与问答接口需要登录，集合与对话按创建者隔离，未登记归属的旧集合
    # 作为共享资料继续可见。
    auth_enabled: bool = False

    # 入库失败重试上限，只做有限重试，不无限循环。
    ingest_max_attempts: int = 3
    # 可恢复导入的分批上限。篇数、块量或字符输入量任一到达上限就封批写入：
    # 块行落库，编码并幂等写入 Milvus，然后记录确认状态。中断后最多重做一批。
    ingest_batch_docs: int = 1000
    ingest_batch_chunks: int = 1000
    ingest_batch_chars: int = 2_000_000

    @property
    def dim(self) -> int:
        return self.embedding_dimensions or 1024

    @field_validator("retrieval_strategy")
    @classmethod
    def _check_strategy(cls, value: str) -> str:
        # 拼错的策略名必须在启动时失败：否则对照实验会拿 dense 结果冒充 hybrid。
        allowed = ("dense", "hybrid-rrf", "hybrid-rerank")
        if value not in allowed:
            raise ValueError(f"retrieval_strategy 必须是 {' 或 '.join(allowed)}，收到 {value!r}")
        return value

    @field_validator("evidence_context_mode")
    @classmethod
    def _check_context_mode(cls, value: str) -> str:
        allowed = ("window", "neighbor-rerank")
        if value not in allowed:
            raise ValueError(f"evidence_context_mode 必须是 {' 或 '.join(allowed)}，收到 {value!r}")
        return value

    @model_validator(mode="after")
    def _check_rerank_config(self) -> "Settings":
        # hybrid-rerank 没配重排服务时启动即失败，不静默退回无重排行为。
        if self.retrieval_strategy == "hybrid-rerank" and not (
            self.rerank_url and self.rerank_model
        ):
            raise ValueError("hybrid-rerank 需要 rerank_url 与 rerank_model")
        return self


def index_build_config(settings: Settings) -> dict:
    """索引身份中除语料内容外的构建配置：分块、嵌入与解析版本。

    复用索引前必须比较这些字段。任何一项变化都意味着不同的索引，不能沿用。
    """
    return {
        "parser_version": PARSER_VERSION,
        "chunker_version": CHUNKER_VERSION,
        "chunk_chars": settings.chunk_chars,
        "chunk_max_chars": settings.chunk_max_chars,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.dim,
        # 关键词通道属于索引身份：实现或 analyzer 变化后，同一语料的关键词
        # 检索结果也会变化，必须重建而不是沿用旧集合。
        "keyword_channel": {"type": "milvus-bm25", "analyzer": "chinese"},
    }
