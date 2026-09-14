"""Skill 扩展框架（M3 / T3.3-T3.4）。

导入本包即触发内置 Skill 的自动注册（通过 @register 装饰器）。
"""

from src.skills.base import Skill, SkillContext, SkillSettings
from src.skills.memory_bus import (
    EVENT_CHAPTER_COMMITTED,
    EVENT_CHARACTER_UPDATED,
    EVENT_OUTLINE_APPROVED,
    MemoryBus,
)
from src.skills.registry import SkillRegistry, register, registered_names

# 触发内置 Skill 注册（副作用导入）
from src.skills import image_gen  # noqa: E402,F401
from src.skills import export  # noqa: E402,F401
from src.skills import translate  # noqa: E402,F401

__all__ = [
    "Skill",
    "SkillContext",
    "SkillSettings",
    "SkillRegistry",
    "register",
    "registered_names",
    "MemoryBus",
    "EVENT_CHAPTER_COMMITTED",
    "EVENT_CHARACTER_UPDATED",
    "EVENT_OUTLINE_APPROVED",
]
