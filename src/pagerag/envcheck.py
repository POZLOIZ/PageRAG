import argparse
import asyncio
import importlib
import sys

from fastapi.testclient import TestClient
from pymilvus import MilvusClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from pagerag.settings import Settings


async def check_mysql(url: str) -> None:
    if not url:
        raise ValueError("缺少数据库配置")
    engine = create_async_engine(url, connect_args={"connect_timeout": 5})
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TEMPORARY TABLE env_probe (id INT PRIMARY KEY)"))
            await conn.execute(text("INSERT INTO env_probe (id) VALUES (1)"))
            assert (await conn.execute(text("SELECT id FROM env_probe"))).scalar_one() == 1
            await conn.execute(text("DROP TEMPORARY TABLE env_probe"))
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 PageRAG 的本地开发环境")
    parser.add_argument("--services", action="store_true", help="同时连接 MySQL 与 Milvus")
    args = parser.parse_args()
    failures = 0

    def check(label, operation):
        nonlocal failures
        try:
            operation()
            print(f"通过：{label}")
        except Exception as exc:
            failures += 1
            # 驱动异常可能含连接参数，因此不输出原始异常文本。
            print(f"失败：{label}（{type(exc).__name__}）")

    def check_venv():
        if sys.prefix == sys.base_prefix:
            raise RuntimeError("请使用项目虚拟环境")

    check("独立 Python 虚拟环境", check_venv)
    for module in ("langchain_openai", "asyncmy", "pymupdf", "tiktoken"):
        check(f"依赖 {module}", lambda module=module: importlib.import_module(module))

    def check_api():
        # 延迟导入：模块导入时会装配数据库、Milvus 与模型客户端，
        # 配置不完整时应该在这里报错，而不是让检查命令本身启动失败。
        from pagerag.api import app
        with TestClient(app) as client:
            response = client.get("/healthz")
            assert response.status_code == 200
            assert response.json()["stage"] == "implemented"

    check("FastAPI 健康接口", check_api)
    settings = Settings()
    if args.services:
        check("MySQL 临时表写入与读取", lambda: asyncio.run(check_mysql(settings.database_url)))

        def check_milvus():
            client = MilvusClient(
                uri=settings.milvus_uri,
                token=settings.milvus_token.get_secret_value(),
                timeout=10,
            )
            try:
                print(f"  Milvus 服务版本：{client.get_server_version()}")
            finally:
                client.close()

        check("Milvus 服务连接", check_milvus)
    else:
        print("未检查：数据库服务（添加 --services 可检查）")
    print("模型连通性请在启动服务后访问 /api/system 查看。")
    return 1 if failures else 0
