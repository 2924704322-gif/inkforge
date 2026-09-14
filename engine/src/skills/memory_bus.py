"""记忆总线（M3 / T3.3）：核心流程与 Skill 之间的事件解耦通道。

核心节点（如章节回写、角色建档）在关键时刻 publish 事件；订阅了该事件的 Skill
被回调。总线对发布方零侵入——没有订阅者时 publish 为廉价 no-op，单个 Skill 回调
异常被隔离（记录日志，不影响主流程与其他订阅者）。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable

from src.utils.logger import get_logger

logger = get_logger(__name__)

# 标准事件名（约定，供核心节点发布 / Skill 订阅）
EVENT_CHAPTER_COMMITTED = "chapter_committed"
EVENT_CHARACTER_UPDATED = "character_updated"
EVENT_OUTLINE_APPROVED = "outline_approved"

Handler = Callable[[str, dict], None]


class MemoryBus:
    """极简同步事件总线。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event: str, handler: Handler) -> None:
        self._subs[event].append(handler)

    def publish(self, event: str, payload: dict) -> None:
        handlers = self._subs.get(event)
        if not handlers:
            return
        for handler in handlers:
            try:
                handler(event, payload)
            except Exception as e:  # 隔离：Skill 异常不得影响主流程
                logger.error("记忆总线事件[%s]回调异常，已隔离: %s", event, e)

    def subscriber_count(self, event: str) -> int:
        return len(self._subs.get(event, []))
