"""OpenAICompatProvider：覆盖一切 OpenAI 兼容 API（DeepSeek/GPT/通义/Kimi/Ollama/vLLM...）。"""

from __future__ import annotations

import time

from openai import APIError, OpenAI

from src.llm.base import ChatMessage, ChatResult, ModelProvider, ProviderError
from src.utils.logger import get_logger

logger = get_logger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OpenAICompatProvider(ModelProvider):
    """只需 base_url + api_key + model 即可挂载任意 OpenAI 兼容端点。"""

    def __init__(self, name: str, base_url: str, api_key: str = "none"):
        super().__init__(name)
        self._client = OpenAI(base_url=base_url, api_key=api_key or "none", max_retries=0)
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
        for attempt in range(3):  # 指数退避重试（R7）
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
                if status in _RETRYABLE_STATUS and attempt < 2:
                    wait = 2**attempt
                    logger.warning(
                        "[%s] 请求失败(status=%s)，%ds 后重试", self.name, status, wait
                    )
                    time.sleep(wait)
                    continue
                break
            except Exception as e:  # 网络层异常
                last_err = e
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
                break
        raise ProviderError(self.name, f"chat 调用失败: {last_err}")

    def probe(self, model: str) -> None:
        try:
            self._client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
        except Exception as e:
            raise ProviderError(self.name, f"探活失败（model={model}）: {e}") from e

    def supports_json_mode(self) -> bool:
        return self._json_mode_ok is not False
