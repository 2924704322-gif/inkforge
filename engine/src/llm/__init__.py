"""模型接入层：ModelProvider 抽象 + 多厂商实现 + 角色注册表。"""

from src.llm.base import ChatMessage, ChatResult, ModelProvider
from src.llm.registry import ModelRegistry

__all__ = ["ChatMessage", "ChatResult", "ModelProvider", "ModelRegistry"]
