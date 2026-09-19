"""OpenAICompatProvider：覆盖一切 OpenAI 兼容 API（DeepSeek/GPT/通义/Kimi/Ollama/vLLM...）。"""

from __future__ import annotations

import time

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from src.llm.base import ChatMessage, ChatResult, ModelProvider, ProviderError
from src.utils.logger import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

#: 单次请求超时（秒）。长文写作一章可能跑几分钟，故给足；用于把
#: 「无限等待」变成「有界失败 + 可恢复」。
CHAT_TIMEOUT_SECONDS = 600.0
#: 探活请求必须短：启动期不能因为网络黑洞卡住 10 分钟。
PROBE_TIMEOUT_SECONDS = 20.0
#: 网络类异常的重试次数（含首次尝试）。与 APIError 的状态码重试相互独立。
NETWORK_ATTEMPTS = 3

#: SDK 网络异常在部分版本里字符串化就是英文 "Connection error."，直接透给用户等于
#: 什么都没说。统一归一为可读中文（原始异常仍附在末尾，便于排障）。
_NETWORK_HINTS = (
    ("nodename nor servname", "DNS 解析失败"),
    ("name or service not known", "DNS 解析失败"),
    ("getaddrinfo", "DNS 解析失败"),
    ("certificate", "TLS 证书校验失败"),
    ("ssl", "TLS 握手失败"),
    ("proxy", "代理不可用"),
    ("timed out", "连接超时"),
    ("timeout", "连接超时"),
    ("connection refused", "目标端口拒绝连接"),
    ("connection reset", "连接被对端重置"),
    ("eof occurred", "连接被对端提前关闭"),
    ("temporarily unavailable", "网络暂时不可用"),
)


def _is_network_error(exc: BaseException) -> bool:
    """判断异常是否为网络层（与 HTTP 状态码无关）。"""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIError):
        # 无 status_code 的 APIError 只可能来自传输层
        return getattr(exc, "status_code", None) is None
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _describe_network_error(exc: BaseException) -> str:
    """把网络异常说成人话：查因提示 + 原始异常原文（保留可检索性）。"""
    text = f"{type(exc).__name__}: {exc}"
    low = text.lower()
    hint = next((h for kw, h in _NETWORK_HINTS if kw in low), "网络连接中断")
    return f"{hint}（{text}）"


class OpenAICompatProvider(ModelProvider):
    """只需 base_url + api_key + model 即可挂载任意 OpenAI 兼容端点。"""

    def __init__(self, name: str, base_url: str, api_key: str = "none"):
        super().__init__(name)
        # max_retries=0：SDK 自带重试与本类的退避重试会叠加成倍数等待，故此处统一关掉，
        # 由本类显式控制重试节奏（见 chat()）。timeout 必须显式设置：SDK 默认 10 分钟，
        # 网络黑洞下表现为「界面无响应」而非可恢复的失败。
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key or "none",
            max_retries=0,
            timeout=CHAT_TIMEOUT_SECONDS,
        )
        self._json_mode_ok: bool | None = None  # None=未探测

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        kwargs: dict = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if json_mode and self._json_mode_ok is not False:
            kwargs["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(NETWORK_ATTEMPTS):  # 指数退避重试（R7）
            try:
                resp = self._client.chat.completions.create(**kwargs)
                usage = {}
                if resp.usage:
                    usage = {
                        "prompt_tokens": resp.usage.prompt_tokens,
                        "completion_tokens": resp.usage.completion_tokens,
                    }
                if json_mode and self._json_mode_ok is None:
                    self._json_mode_ok = True
                return ChatResult(
                    content=resp.choices[0].message.content or "",
                    model=model,
                    provider_name=self.name,
                    usage=usage,
                )
            except Exception as e:  # noqa: BLE001 - 统一判网络类，见 _is_network_error
                # 顺序至关重要：在 openai>=1.x/2.x 中 APIConnectionError / APITimeoutError
                # 都是 APIError 的子类。若把本分支写在 `except APIError` 之后，网络异常会
                # 被那个分支接住（status_code=None 不在 _RETRYABLE_STATUS）→ 一次抖动即失败。
                if not _is_network_error(e):
                    raise
                last_err = e
                logger.warning(
                    "[%s] 网络异常（第 %d/%d 次）：%s",
                    self.name, attempt + 1, NETWORK_ATTEMPTS, e,
                )
                if attempt < NETWORK_ATTEMPTS - 1:
                    time.sleep(2**attempt)
                    continue
                break
            except APIError as e:
                status = getattr(e, "status_code", None)
                # 能力自适应：JSON mode 不被支持时去掉参数降级重试（R4）
                if json_mode and "response_format" in kwargs and status in (400, 422):
                    logger.warning(
                        "[%s] 不支持 JSON mode，降级为提示词约束模式", self.name
                    )
                    self._json_mode_ok = False
                    kwargs.pop("response_format", None)
                    continue
                last_err = e
                if status in _RETRYABLE_STATUS and attempt < NETWORK_ATTEMPTS - 1:
                    wait = 2**attempt
                    logger.warning(
                        "[%s] 请求失败(status=%s)，%ds 后重试", self.name, status, wait
                    )
                    time.sleep(wait)
                    continue
                break
        if last_err is not None and _is_network_error(last_err):
            raise ProviderError(
                self.name,
                f"网络连接失败（已重试 {NETWORK_ATTEMPTS} 次）: "
                f"{_describe_network_error(last_err)}",
            ) from last_err
        raise ProviderError(self.name, f"chat 调用失败: {last_err}")

    def probe(self, model: str) -> None:
        """启动探活：短超时 + 网络类重试，避免启动期被网络黑洞拖死。

        注意：探活在 ``ModelRegistry.probe_all`` 里是 Fail-Fast 的（失败即抛），
        因此这里的重试直接决定「一次网络抖动会不会让整本书开不了工」。
        """
        last_err: Exception | None = None
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                self._client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
                return
            except Exception as e:  # noqa: BLE001
                if not _is_network_error(e):
                    raise ProviderError(
                        self.name, f"探活失败（model={model}）: {e}"
                    ) from e
                last_err = e
                if attempt < NETWORK_ATTEMPTS - 1:
                    time.sleep(2**attempt)
        raise ProviderError(
            self.name,
            f"探活失败（model={model}，已重试 {NETWORK_ATTEMPTS} 次）: "
            f"{_describe_network_error(last_err) if last_err else '未知网络错误'}",
        ) from last_err

    def supports_json_mode(self) -> bool:
        return self._json_mode_ok is not False
