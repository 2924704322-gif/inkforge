"""AnthropicProvider：Claude 系列接入。"""

from __future__ import annotations

import time

import anthropic

from src.llm.base import ChatMessage, ChatResult, ModelProvider, ProviderError
from src.llm.openai_compat import (  # 网络错误判定/文案归一：单一事实源，勿复制
    CHAT_TIMEOUT_SECONDS,
    NETWORK_ATTEMPTS,
    PROBE_TIMEOUT_SECONDS,
    _describe_network_error,
    _is_network_error,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


class AnthropicProvider(ModelProvider):
    """Anthropic Messages API 接入。system 消息单独提升为 system 参数。"""

    def __init__(self, name: str, api_key: str, base_url: str | None = None):
        super().__init__(name)
        # max_retries=0：重试节奏由本类统一控制；timeout 显式设置，避免网络黑洞下
        # 界面被 SDK 默认的 10 分钟超时拖成「假死」。
        kwargs: dict = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": CHAT_TIMEOUT_SECONDS,
        }
        if base_url:
            kwargs["base_url"] = base_url
        self._client = anthropic.Anthropic(**kwargs)

    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        system_parts = [m.content for m in messages if m.role == "system"]
        chat_msgs = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role != "system"
        ]
        # Anthropic 无原生 JSON mode，走提示词约束（能力自适应由 structured 模块兜底）
        if json_mode:
            chat_msgs.append(
                {"role": "assistant", "content": "{"}  # 预填引导 JSON 输出
            )

        last_err: Exception | None = None
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                resp = self._client.messages.create(
                    model=model,
                    system="\n\n".join(system_parts) or anthropic.NOT_GIVEN,
                    messages=chat_msgs,
                    temperature=temperature,
                    max_tokens=max_tokens or 8192,
                )
                content = "".join(
                    b.text for b in resp.content if b.type == "text"
                )
                if json_mode:
                    content = "{" + content  # 补回预填的开括号
                return ChatResult(
                    content=content,
                    model=model,
                    provider_name=self.name,
                    usage={
                        "prompt_tokens": resp.usage.input_tokens,
                        "completion_tokens": resp.usage.output_tokens,
                    },
                )
            except anthropic.APIStatusError as e:
                last_err = e
                if e.status_code in (429, 500, 502, 503, 529) and attempt < 2:
                    wait = 2**attempt
                    logger.warning(
                        "[%s] 请求失败(status=%s)，%ds 后重试",
                        self.name,
                        e.status_code,
                        wait,
                    )
                    time.sleep(wait)
                    continue
                break
            except Exception as e:
                last_err = e
                if attempt < NETWORK_ATTEMPTS - 1:
                    time.sleep(2**attempt)
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
        """启动探活：短超时 + 网络类重试（探活失败在 probe_all 里是 Fail-Fast 的）。"""
        last_err: Exception | None = None
        for attempt in range(NETWORK_ATTEMPTS):
            try:
                self._client.messages.create(
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
        return False  # 无原生 JSON mode，统一走提示词约束
