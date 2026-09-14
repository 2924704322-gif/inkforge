"""Skill 扩展框架基类（M3 / T3.3）。

设计目标：让新功能（如立绘生成、翻译、导出）以插件式接入，不侵入核心生成流程。
- SkillSettings：每个 Skill 的可配置项基类（pydantic 校验，支持 enabled 开关）。
- SkillContext：注入给 Skill 的运行时依赖（MD 存储 / 记忆 / 模型 / 小说 id）。
- Skill：抽象基类，声明 name / SettingsModel / run()，并可订阅记忆总线事件。

记忆总线钩子：Skill 通过 subscribed_events() 声明关心的事件（如 chapter_committed /
character_updated），由 SkillRegistry 绑定到 MemoryBus，事件发生时回调 handle_event()。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

if TYPE_CHECKING:  # 仅类型标注，避免运行时耦合
    from src.llm.registry import ModelRegistry
    from src.memory.md_store import MdStore
    from src.memory.memory_manager import MemoryManager


class SkillSettings(BaseModel):
    """Skill 配置基类；子类追加自有字段。"""

    enabled: bool = True


@dataclass
class SkillContext:
    """Skill 运行时依赖，由装配层注入。"""

    novel_id: str
    store: "MdStore"
    memory: "MemoryManager"
    registry: "ModelRegistry"


class Skill(ABC):
    """Skill 抽象基类。子类须声明 name 与 SettingsModel 并实现 run()。"""

    name: ClassVar[str] = ""
    SettingsModel: ClassVar[type[SkillSettings]] = SkillSettings

    def __init__(self, settings: SkillSettings, context: SkillContext):
        self.settings = settings
        self.context = context

    @classmethod
    def build(cls, raw_settings: dict, context: SkillContext) -> "Skill":
        """由原始配置字典构造：pydantic 校验（Fail-Fast），再实例化。"""
        if not cls.name:
            raise ValueError(f"{cls.__name__} 未声明 name")
        settings = cls.SettingsModel.model_validate(raw_settings or {})
        return cls(settings, context)

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @abstractmethod
    def run(self, **kwargs: Any) -> dict:
        """执行 Skill 主逻辑，返回结构化结果。"""

    # ---------- 记忆总线钩子（可选） ----------

    def subscribed_events(self) -> list[str]:
        """声明订阅的记忆总线事件；默认不订阅。"""
        return []

    def handle_event(self, event: str, payload: dict) -> None:
        """事件回调；默认 no-op，订阅事件的子类覆盖。"""
