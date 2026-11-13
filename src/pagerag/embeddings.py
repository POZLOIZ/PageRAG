"""远程向量模型适配层：问句传 `input_type=query`，资料传 `document`。

对超长输入做显式记录，不静默截断。子批并发发送，结果按子批下标回收，顺序与
输入一致；任一子批失败则整次调用抛错，重试交给调用方。并发度默认 8，是在
吞吐与排队之间取的折中。
"""

import asyncio
import hashlib
import logging

import httpx

from pagerag.settings import Settings

logger = logging.getLogger(__name__)

BATCH_SIZE = 16
CONCURRENCY = 8


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(timeout=120)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self._embed(texts, "document")

    async def embed_query(self, text: str) -> list[float]:
        vectors = await self._embed([text], "query")
        return vectors[0]

    async def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        batches = [texts[start:start + BATCH_SIZE]
                   for start in range(0, len(texts), BATCH_SIZE)]
        for batch in batches:
            for text in batch:
                if len(text) > 4000:
                    logger.warning(
                        "输入过长（%d 字符），记录哈希 %s，实际是否截断由服务端决定",
                        len(text), hashlib.sha256(text.encode()).hexdigest()[:12])
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def fetch(batch: list[str]) -> list[list[float]]:
            async with semaphore:
                payload = {
                    "model": self._settings.embedding_model,
                    "input": batch,
                    "input_type": input_type,
                }
                if self._settings.embedding_dimensions:
                    payload["dimensions"] = self._settings.embedding_dimensions
                response = await self._client.post(
                    f"{self._settings.embedding_base_url.rstrip('/')}/embeddings",
                    json=payload)
                if response.status_code != 200:
                    raise EmbeddingError(
                        f"向量服务返回 {response.status_code}：{response.text[:200]}")
                return [item["embedding"] for item in response.json()["data"]]

        vectors = await asyncio.gather(*(fetch(batch) for batch in batches))
        return [vector for batch_vectors in vectors for vector in batch_vectors]
