"""默认应用入口：按当前配置装配全部服务（uvicorn pagerag.api:app）。"""

from pagerag.app import build_app
from pagerag.db import Database
from pagerag.embeddings import EmbeddingClient
from pagerag.milvus_store import MilvusStore
from pagerag.settings import Settings


def create_default_app():
    settings = Settings()
    database = Database(settings)
    store = MilvusStore(settings)
    embedder = EmbeddingClient(settings)
    return build_app(settings, database, store, embedder)


app = create_default_app()
