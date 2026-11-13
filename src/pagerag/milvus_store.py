"""Milvus 读写：每个索引版本一个独立 collection，发布即切换 active 指针。

完整快照发布的代价是未变化的记录也要写入新 collection；规模增长后再考虑差量方案。

集合结构包含主键、文本字段、BM25 稀疏字段和稠密向量。关键词与稠密检索同库同版本，
中文关键词走 Milvus 全文检索。恢复语义：ensure 对已存在的同名集合只复用、不删除，
上次写入的行就是断点进度；写入一律按主键覆盖式 upsert，重跑重复写安全。MySQL 与
Milvus 无法原子提交，靠幂等写入和两态确认补齐，见 `ingest.py`。
"""

from pymilvus import DataType, Function, FunctionType, MilvusClient

from pagerag.settings import Settings

# 单次 upsert 的行数上限：与导入分批解耦（这里只是传输尺寸保险）。
_UPSERT_ROWS = 1000


class MilvusStore:
    def __init__(self, settings: Settings, client: MilvusClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._own_client = client is None
        # 集合是否带 sparse 字段：老索引建库时还没有关键词通道。查询前探一次
        # 并缓存，避免每题都向 Milvus 要 schema，也避免对老集合反复报错。
        self._sparse_cache: dict[str, bool] = {}

    @property
    def _connection(self) -> MilvusClient:
        # 懒连接：隧道未开时不因为导入模块失败。
        if self._client is None:
            self._client = MilvusClient(
                uri=self._settings.milvus_uri,
                token=self._settings.milvus_token.get_secret_value(),
                timeout=30,
            )
        return self._client

    def has_sparse(self, name: str) -> bool:
        cached = self._sparse_cache.get(name)
        if cached is not None:
            return cached
        try:
            described = self._connection.describe_collection(name)
            fields = {item.get("name") for item in described.get("fields", [])}
        except Exception:  # noqa: BLE001 - 探测失败交给正常检索路径报错
            return True
        result = "sparse" in fields
        self._sparse_cache[name] = result
        return result

    def close(self) -> None:
        if self._client is not None and self._own_client:
            self._client.close()

    def collection_name(self, collection_public_id: str, version: int) -> str:
        return f"pagerag_{collection_public_id}_v{version}"

    def ensure(self, name: str, dim: int) -> bool:
        """确保集合存在且两路字段齐全；已存在即复用（断点续跑），返回是否新建。

        不做删除：同名集合意味着上次构建写过数据，删了会把可恢复进度毁掉；
        真正要废弃时由调用方显式 drop。
        """
        conn = self._connection
        if conn.has_collection(name):
            conn.load_collection(name)  # 幂等；恢复后保证可查
            return False
        schema = conn.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("chunk_pk", DataType.INT64, is_primary=True)
        # 字段顺序沿用实测探针：text → 稀疏（函数紧随其后）→ 稠密。
        schema.add_field("text", DataType.VARCHAR, max_length=65535,
                         enable_analyzer=True, analyzer_params={"type": "chinese"})
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_function(Function(
            name="text_bm25", input_field_names=["text"], output_field_names=["sparse"],
            function_type=FunctionType.BM25,
        ))
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=dim)
        index_params = conn.prepare_index_params()
        index_params.add_index(field_name="sparse", index_type="SPARSE_INVERTED_INDEX",
                               metric_type="BM25")
        index_params.add_index("embedding", metric_type="COSINE",
                               index_type="AUTOINDEX", params={})
        conn.create_collection(name, schema=schema, index_params=index_params)
        return True

    def upsert(self, name: str, rows: list[tuple[int, list[float], str]]) -> None:
        """按主键覆盖式写入 (chunk_pk, 向量, 文本)。重复写同一主键安全——
        重跑不会产生重复行，只会覆盖为相同内容。"""
        if not rows:
            return
        conn = self._connection
        for start in range(0, len(rows), _UPSERT_ROWS):
            batch = rows[start : start + _UPSERT_ROWS]
            conn.upsert(name, [
                {"chunk_pk": pk, "text": text, "embedding": vector}
                for pk, vector, text in batch
            ])

    def search(self, name: str, query_vector: list[float], top_k: int) -> list[tuple[int, float]]:
        if not self._connection.has_collection(name):
            return []
        results = self._connection.search(
            name, data=[query_vector], anns_field="embedding", limit=top_k,
            output_fields=[]
        )
        hits = results[0] if results else []
        return [(hit["chunk_pk"], hit["distance"]) for hit in hits]

    def search_sparse(self, name: str, query_text: str, top_k: int) -> list[tuple[int, float]]:
        """全文检索：BM25 函数在查询侧把原问题文本转成稀疏向量。中文文本
        使用 Milvus 的 chinese analyzer。关键词通道使用 BM25 函数生成的稀疏
        字段，与稠密向量同集合、同索引版本。"""
        if not self._connection.has_collection(name):
            return []
        results = self._connection.search(
            name, data=[query_text], anns_field="sparse", limit=top_k,
            output_fields=[]
        )
        hits = results[0] if results else []
        return [(hit["chunk_pk"], hit["distance"]) for hit in hits]

    def fetch_embeddings(self, name: str, pks: list[int]) -> dict[int, list[float]]:
        """按主键取回向量（发布前探针用：用库内向量验证稠密路可查）。"""
        if not pks:
            return {}
        found: dict[int, list[float]] = {}
        for start in range(0, len(pks), _UPSERT_ROWS):
            batch = pks[start : start + _UPSERT_ROWS]
            rows = self._connection.query(
                name, filter=f"chunk_pk in {[int(pk) for pk in batch]}",
                output_fields=["chunk_pk", "embedding"])
            for row in rows:
                found[int(row["chunk_pk"])] = list(row["embedding"])
        return found

    def row_count(self, name: str) -> int:
        stats = self._connection.get_collection_stats(name)
        return int(stats.get("row_count", 0))

    def flush(self, name: str) -> None:
        # 全库量级的 flush 可能超过默认 RPC 超时，单独放宽。
        self._connection.flush(name, timeout=600)

    def drop(self, name: str) -> None:
        if self._connection.has_collection(name):
            self._connection.drop_collection(name)
