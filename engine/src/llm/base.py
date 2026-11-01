"""ModelProvider 统一抽象：Agent 逻辑与模型厂商完全解耦。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
    #: 厂商侧的收尾原因（openai_compat: `stop`/`length`/…；anthropic: `end_turn`/`max_tokens`/…）。
    #: 2026-09-22 补（真机冒烟发现）：此前全链不透出，于是"产出被截断"与"模型不肯写长"
    #: 无法区分——实测 max_tokens=8000 时正文被硬切、completion_tokens 恒等于 8000，
    #: 只能靠 `completion_tokens ≥ max_tokens` 近似猜。有它才谈得上硬判定。
    finish_reason: str = ""


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
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        """发起一次对话补全。json_mode=True 时尽力要求 JSON 输出（能力自适应）。"""

    @abstractmethod
    def probe(self, model: str) -> None:
        """启动探活：发送最小请求验证连通性与 Key 有效性，失败抛 ProviderError。"""

    def list_models(self) -> list[str]:
        """拉取该接入点当前可用的模型名清单（供「获取模型」下拉选择目标模型）。

        默认实现**显式报错**而不是返回空列表：空列表会被 UI 读成「这个接入点没有
        可用模型」，而真相是「这个接入点类型没实现拉取」——两者必须能分辨。
        """
        raise ProviderError(
            self.name, f"接入点类型 [{type(self).__name__}] 未实现模型列表拉取"
        )

    def supports_json_mode(self) -> bool:
        """是否原生支持 JSON mode（探测失败后由 registry 缓存降级标记）。"""
        return True


class ProviderError(RuntimeError):
    """接入点调用/探活失败。"""

    def __init__(self, provider_name: str, detail: str):
        self.provider_name = provider_name
        super().__init__(f"接入点 [{provider_name}] 错误: {detail}")
