"""ModelProvider 统一抽象：Agent 逻辑与模型厂商完全解耦。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChatMessage:
    """统一消息结构。role: system / user / assistant"""

    role: str
    content: str


@dataclass
class ChatResult:
    """统一返回结构。"""

    content: str
    model: str
    provider_name: str
    usage: dict = field(default_factory=dict)  # prompt_tokens / completion_tokens
    used_fallback: bool = False  # 是否由主接入点降级到备用接入点产出


class ModelProvider(ABC):
    """模型接入点统一调用面。"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        model: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """发起一次对话补全。json_mode=True 时尽力要求 JSON 输出（能力自适应）。"""

    @abstractmethod
    def probe(self, model: str) -> None:
        """启动探活：发送最小请求验证连通性与 Key 有效性，失败抛 ProviderError。"""

    def supports_json_mode(self) -> bool:
        """是否原生支持 JSON mode（探测失败后由 registry 缓存降级标记）。"""
        return True


class ProviderError(RuntimeError):
    """接入点调用/探活失败。"""

    def __init__(self, provider_name: str, detail: str):
        self.provider_name = provider_name
        super().__init__(f"接入点 [{provider_name}] 错误: {detail}")
