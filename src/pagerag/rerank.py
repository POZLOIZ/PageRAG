"""远程重排客户端：把候选池交给重排模型按问题重新打分。

接口形状为 POST {model, query, documents, top_n}，返回
{"results": [{"index", "relevance_score"}, ...]}，按分数降序。重排输入目前使用
块正文，标题和元数据随结构分块进入检索单元后再并入。分数只用于排序和快照记录，
不与稠密或 BM25 分数互相比较。
"""

import httpx

from pagerag.settings import Settings


def parse_rerank_results(payload: dict, document_count: int) -> list[float]:
    """把接口返回展开成与输入 documents 一一对应的分数列表。

    接口按 top_n 截断返回；未出现的文档记 -1.0（排序时落在末段，输入序稳定）。
    """
    scores = [-1.0] * document_count
    for row in payload.get("results") or []:
        index = row.get("index")
        if isinstance(index, int) and 0 <= index < document_count:
            scores[index] = float(row.get("relevance_score", -1.0))
    return scores


class RerankClient:
    """调用 /rerank 接口；失败原样抛出（单题失败由评测记录，不静默吞掉）。"""

    # 重排服务单次最多 64 段，邻块扩展后的候选可能超过这个数。客户端按 60
    # 分批调用再拼回顺序。逐对分数与批次组成无关，分批不改变结果。
    _BATCH = 60

    def __init__(self, settings: Settings,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = settings.rerank_url
        self._model = settings.rerank_model
        self._api_key = settings.rerank_api_key.get_secret_value()
        self._timeout = 60.0
        # 测试注入用；生产为 None，走 httpx 默认传输。
        self._transport = transport

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """返回与 documents 等长的相关度分数列表（越大越相关）。"""
        if not documents:
            return []
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        scores: list[float] = []
        async with httpx.AsyncClient(trust_env=False, timeout=self._timeout,
                                     transport=self._transport) as http:
            for start in range(0, len(documents), self._BATCH):
                batch = documents[start:start + self._BATCH]
                body = {"model": self._model, "query": query, "documents": batch,
                        "top_n": len(batch)}
                response = await http.post(self._url, json=body, headers=headers)
                response.raise_for_status()
                scores.extend(parse_rerank_results(response.json(), len(batch)))
        return scores
