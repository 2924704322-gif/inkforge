"""Skill 注册器与路由表（M3 / T3.3）。

- register()：按 name 注册 Skill 类（重名 Fail-Fast），构成路由映射表。
- build_enabled()：读取 skills 配置字典，为每个已注册且 enabled 的 Skill 构造实例，
  并把其订阅的记忆总线事件挂到 MemoryBus。
- dispatch()：按 name 路由到对应 Skill 实例执行 run()。

内置 Skill（如 ImageGenSkill/T3.4）在模块导入时通过 @register 自动登记。
"""

from __future__ import annotations

from typing import Optional

from src.skills.base import Skill, SkillContext
from src.skills.memory_bus import MemoryBus
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 路由映射表：name -> Skill 子类
_REGISTRY: dict[str, type[Skill]] = {}


def register(cls: type[Skill]) -> type[Skill]:
    """类装饰器：登记 Skill。重名抛错（Fail-Fast）。"""
    name = cls.name
    if not name:
        raise ValueError(f"{cls.__name__} 未声明 name，无法注册")
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"Skill 名冲突: {name!r} 已被 {_REGISTRY[name].__name__} 占用")
    _REGISTRY[name] = cls
    return cls


def registered_names() -> list[str]:
    return sorted(_REGISTRY)


def get_skill_class(name: str) -> type[Skill]:
    if name not in _REGISTRY:
        raise KeyError(f"未注册的 Skill: {name!r}（已注册: {registered_names()}）")
    return _REGISTRY[name]


class SkillRegistry:
    """运行时 Skill 容器：构造已启用实例 + 绑定记忆总线 + 路由分发。"""

    def __init__(self, context: SkillContext, bus: Optional[MemoryBus] = None):
        self._context = context
        self.bus = bus or MemoryBus()
        self._instances: dict[str, Skill] = {}

    def build_enabled(self, skills_config: dict) -> "SkillRegistry":
        """skills_config: {name: {enabled: bool, ...}}。仅构造已注册且 enabled 的。"""
        for name, cls in _REGISTRY.items():
            raw = (skills_config or {}).get(name, {})
            skill = cls.build(raw, self._context)
            if not skill.enabled:
                logger.info("Skill[%s] 未启用，跳过", name)
                continue
            self._instances[name] = skill
            for event in skill.subscribed_events():
                self.bus.subscribe(event, skill.handle_event)
            logger.info("Skill[%s] 已启用，订阅事件: %s", name, skill.subscribed_events())
        return self

    def dispatch(self, name: str, **kwargs) -> dict:
        """路由到指定 Skill 执行。未启用/未注册抛错。"""
        if name not in self._instances:
            raise KeyError(f"Skill[{name}] 未启用或未注册（已启用: {list(self._instances)}）")
        return self._instances[name].run(**kwargs)

    def active_names(self) -> list[str]:
        return sorted(self._instances)
