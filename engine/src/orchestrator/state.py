"""图状态定义（必须 JSON 可序列化，供 SqliteSaver 持久化）。"""

from __future__ import annotations

from typing import TypedDict


class NovelState(TypedDict, total=False):
    """核心生成流程的图状态。"""

    # 项目基本信息
    novel_id: str
    brief: str                      # 创作需求
    total_chapters: int
    target_words: int               # 单章目标字数

    # 大纲阶段
    outline: dict                   # OutlineOutput.model_dump()
    outline_feedback: str           # 大纲人审打回意见
    outline_feedback_mode: str      # 打回模式：targeted（定向修订，默认）/ rewrite（整体重写）

    # 章节循环
    current_chapter: int            # 当前生成章节号（从 1 开始）
    current_volume: int
    chapter_target_words: int       # 本章预期字数（大纲预算 → 人审可改；写作/评分/门禁共用）
    chapter_length_floor: int       # 本章最低可接受字数（目标 - 下浮上限，硬线）
    chapter_length_ceiling: int     # 本章最高可接受字数（目标 + 上浮上限，内容完整性优先）
    chapter_ctx: dict               # ChapterContext 序列化（供 Editor 复用）
    draft_text: str                 # 当前稿正文
    attempt: int                    # 当前章第几稿（从 1 开始）
    partial_retries: int            # 局部重写计数（上限 2，D6）
    full_retries: int               # 整章重写计数（上限 2，D6）
    length_retries: int             # 字数门禁打回计数（上限 max_length_retries）
    review: dict                    # 最近一次 Editor 审查结果
    verdict: str                    # pass / partial_rewrite / full_rewrite
    retry_exceeded: bool            # 重试超限强制送人审标记
    revision_notes: str | None   # 下一稿重写指令（Editor 问题清单或人工意见）
    first_review_passed: bool | None  # 本章首次送人审是否通过（验收指标③）
    model: str                      # 当前稿实际生成使用的 provider/model
    used_fallback: bool             # 当前稿是否触发降级备用接入点（可追溯）

    # 全局
    done: bool


def chapter_plans(state: NovelState) -> list[dict]:
    """从大纲状态展开有序章节计划列表 [{chapter,title,outline,characters,volume}]。"""
    plans: list[dict] = []
    for vol in state["outline"].get("volumes", []):
        for ch in vol.get("chapters", []):
            plans.append({**ch, "volume": vol["volume"]})
    plans.sort(key=lambda c: c["chapter"])
    return plans


def plan_for_chapter(state: NovelState, chapter: int) -> dict | None:
    for plan in chapter_plans(state):
        if plan["chapter"] == chapter:
            return plan
    return None


def resolve_chapter_target(plan: dict | None, fallback: int | None) -> int | None:
    """解析本章预期字数：大纲预算优先，缺失/非法则回落全局默认（问题3）。

    为什么单一入口：写作（writer）、评分（editor）、字数门禁（graph/scheduler）必须
    用**同一个数**，否则会出现"模型按 3000 写、门禁按 5000 判"的互相打架。
    非法值（<=0、非整数）一律当"未设置"，不抛错——旧大纲没有该字段是正常情况。
    """
    if plan:
        raw = plan.get("target_words")
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    try:
        fb = int(fallback)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return fb if fb > 0 else None
