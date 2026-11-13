"""生成模型客户端：可选主备双通道。

主通道限流、超时、连接失败或返回 5xx 时切换到备援通道。未配置主通道时退回单端点。
`llm_session` 作为会话标识随请求发送，供需要稳定会话的服务复用提示缓存。
`last_endpoint` 记录每次调用实际使用的端点，便于排查和记录调用链。
两端 temperature=0 并关闭 thinking，保证结果可复现。
"""

import asyncio
from typing import Any
from urllib.parse import urlparse

from langchain_openai import ChatOpenAI

from pagerag.settings import Settings

# 同端点重试的状态：限流、请求超时、服务端临时故障。400/422 是请求本身
# 有问题，重试不会变好；401/403/404 可能与端点绑定，交给备援通道。
RETRYABLE_STATUS = {408, 409, 425, 429}
NO_FALLBACK_STATUS = {400, 422}


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def is_rate_limited(exc: Exception) -> bool:
    """429 判定：openai 异常的 status_code，或错误串里的 429 / rate limit。"""
    status = _status_code(exc)
    if status == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "rate limit" in text


def is_transient(exc: Exception) -> bool:
    """同端点值得再试一次：限流、超时、连接失败、5xx。"""
    status = _status_code(exc)
    if status is not None:
        return status in RETRYABLE_STATUS or 500 <= status < 600
    name = type(exc).__name__.lower()
    if any(token in name for token in
           ("timeout", "connect", "ratelimit", "internalserver")):
        return True
    text = str(exc).lower()
    return any(token in text for token in
               ("timed out", "timeout", "connection", "temporarily unavailable",
                "overloaded", "rate limit", "429"))


def can_fallback(exc: Exception) -> bool:
    """异常后是否值得换备援端点。

    请求格式错误换端点也没用；没有状态码的普通异常视为调用方缺陷，不掩盖。
    只有带状态码的端点错误或可重试的临时故障才切备援。
    """
    status = _status_code(exc)
    if status is not None:
        return status not in NO_FALLBACK_STATUS
    return is_transient(exc)


def endpoint_label(base_url: str, model: str) -> str:
    host = urlparse(base_url).hostname or base_url
    return f"{model}@{host}"


class FallbackChatLLM:
    """包装主备两个 ChatOpenAI：主用临时故障时重试，仍失败切备援。

    timeout 是单次调用上限；attempts 是每个通道的最大尝试次数。重试只针对
    限流、超时、连接失败和 5xx；400/422 直接抛出。last_attempts 供评测记录
    实际调用次数，便于区分"模型慢"和"网络抖"。
    """

    def __init__(self, primary: ChatOpenAI, backup: ChatOpenAI | None,
                 primary_label: str, backup_label: str | None,
                 timeout: float = 60.0, attempts: int = 2,
                 backoff: float = 0.5) -> None:
        self._primary = primary
        self._backup = backup
        self._primary_label = primary_label
        self._backup_label = backup_label
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._backoff = max(0.0, backoff)
        self.last_endpoint = primary_label
        self.last_attempts = 0

    @property
    def endpoints(self) -> dict:
        """血缘用：主备端点标识（备份为空表示单端点）。"""
        return {"primary": self._primary_label, "backup": self._backup_label}

    async def ainvoke(self, messages: list, **kwargs: Any):
        last_error: Exception | None = None
        self.last_attempts = 0
        channels: list[tuple[ChatOpenAI, str]] = [
            (self._primary, self._primary_label)
        ]
        if self._backup is not None:
            channels.append((self._backup, self._backup_label or "backup"))
        for chat, label in channels:
            for attempt in range(1, self._attempts + 1):
                self.last_attempts += 1
                try:
                    result = await asyncio.wait_for(
                        chat.ainvoke(messages, **kwargs), timeout=self._timeout)
                except Exception as exc:  # noqa: BLE001 - 分类后重试或切备援
                    last_error = exc
                    if attempt < self._attempts and is_transient(exc):
                        await asyncio.sleep(self._backoff * attempt)
                        continue
                    if not can_fallback(exc):
                        raise
                    break
                self.last_endpoint = label
                return result
        assert last_error is not None
        raise last_error

    async def astream(self, messages: list, **kwargs: Any):
        """流式调用；只在还没有输出任何内容前重试或切换备援。

        一旦已经向调用方送出过 token，再换端点会拼接出两段不同的回答，
        因此此时直接抛出，由上层按失败处理。
        """
        last_error: Exception | None = None
        self.last_attempts = 0
        channels: list[tuple[ChatOpenAI, str]] = [
            (self._primary, self._primary_label)
        ]
        if self._backup is not None:
            channels.append((self._backup, self._backup_label or "backup"))
        for chat, label in channels:
            for attempt in range(1, self._attempts + 1):
                self.last_attempts += 1
                emitted = False
                self.last_endpoint = label
                try:
                    iterator = chat.astream(messages, **kwargs)
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                anext(iterator), timeout=self._timeout)
                        except StopAsyncIteration:
                            return
                        emitted = True
                        yield chunk
                except Exception as exc:  # noqa: BLE001 - 同上
                    last_error = exc
                    if emitted:
                        raise
                    if attempt < self._attempts and is_transient(exc):
                        await asyncio.sleep(self._backoff * attempt)
                        continue
                    if not can_fallback(exc):
                        raise
                    break
        assert last_error is not None
        raise last_error


def chunk_text(chunk: Any) -> str:
    """从流式消息块里取文本。OpenAI 兼容端点通常给字符串；部分网关会把
    内容拆成多段对象，这里只拼接 text 字段。"""
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return ""


def _chat(base_url: str, model: str, api_key: str,
          extra_headers: dict[str, str] | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=base_url, model=model, api_key=api_key, temperature=0,
        extra_body={"thinking": {"type": "disabled"}},
        default_headers=extra_headers,
        # 重试集中在外层：避免 SDK 内建重试与通道切换叠加，次数不可见。
        max_retries=0,
    )


def build_chat_llm(settings: Settings) -> FallbackChatLLM:
    """有主入口（Go）配置则双通道，否则单端点（官方）。"""
    backup = _chat(settings.llm_base_url, settings.llm_model,
                   settings.llm_api_key.get_secret_value())
    backup_label = endpoint_label(settings.llm_base_url, settings.llm_model)
    if not (settings.llm_primary_base_url and settings.llm_primary_model
            and settings.llm_primary_api_key.get_secret_value()):
        return FallbackChatLLM(
            backup, None, backup_label, None,
            timeout=settings.llm_request_timeout_seconds,
            attempts=settings.llm_max_attempts,
            backoff=settings.query_channel_backoff_seconds,
        )
    primary = _chat(
        settings.llm_primary_base_url, settings.llm_primary_model,
        settings.llm_primary_api_key.get_secret_value(),
        # Go 入口按其要求携带稳定会话标识
        extra_headers={"x-opencode-session": settings.llm_session},
    )
    return FallbackChatLLM(
        primary, backup,
        endpoint_label(settings.llm_primary_base_url, settings.llm_primary_model),
        backup_label,
        timeout=settings.llm_request_timeout_seconds,
        attempts=settings.llm_max_attempts,
        backoff=settings.query_channel_backoff_seconds,
    )
