"""章节定稿（人审通过后）：摘要生成 → 记忆回写 → frontmatter 终态 → 记忆总线事件。

供串行主图（graph.writeback_node）与卷级并行驱动器（parallel_runner）共用，
保证两条执行路径的回写语义完全一致。

记忆总线（M3 / T3.3）：定稿时发布 chapter_committed / character_updated 事件，
订阅了这些事件的 Skill（如 ImageGenSkill 自动补图）被回调；无订阅者时为廉价 no-op。
"""

from __future__ import annotations

from src.agents.schemas import ReviewOutput
from src.memory.memory_manager import ChapterContext
from src.skills.memory_bus import EVENT_CHAPTER_COMMITTED, EVENT_CHARACTER_UPDATED
from src.utils.logger import get_logger

logger = get_logger(__name__)


def finalize_chapter(
    pipe,
    chapter: int,
    volume: int,
    draft_text: str,
    ctx: ChapterContext,
    review: dict | None,
    first_review_passed: bool | None,
    total_chapters: int,
) -> None:
    """人审通过后的完整定稿流程（摘要 / 回写 / 终态 / 事件）。"""
    summary = pipe.summarizer.summarize(
        chapter, draft_text, ctx.unresolved_foreshadowing,
        total_chapters=total_chapters,
        is_finale=chapter >= total_chapters,
        state_board=ctx.state_board,
    )
    pipe.memory.writeback_chapter(
        chapter=chapter,
        volume=volume,
        summary_content=summary.summary,
        character_updates={u.name: u.updates for u in summary.character_updates},
        foreshadow_ops=[op.model_dump() for op in summary.foreshadow_ops],
        total_chapters=total_chapters,
        state_ops=[op.model_dump() for op in summary.state_ops],
    )
    # 章节 frontmatter 终态更新
    review_out = ReviewOutput.model_validate(review) if review else None
    rel = pipe.store.chapter_rel_path(volume, chapter)
    pipe.store.update_metadata(
        rel,
        {
            "status": "approved",
            "characters": summary.characters_present,
            "score": review_out.overall if review_out else None,
            "first_review_passed": first_review_passed,
            "foreshadow_planted": [
                op.desc for op in summary.foreshadow_ops if op.action == "plant"
            ],
            "foreshadow_resolved": [
                op.id for op in summary.foreshadow_ops if op.action == "resolve"
            ],
        },
        commit_message=f"ch-{chapter:03d} 人审通过，记忆回写",
    )
    # 记忆总线事件：核心流程零依赖 Skill，异常已由总线隔离
    bus = getattr(pipe, "bus", None)
    if bus is not None:
        for u in summary.character_updates:
            bus.publish(EVENT_CHARACTER_UPDATED, {
                "name": u.name, "updates": u.updates, "chapter": chapter,
            })
        bus.publish(EVENT_CHAPTER_COMMITTED, {
            "chapter": chapter,
            "volume": volume,
            "score": review_out.overall if review_out else None,
            "total_chapters": total_chapters,
            "is_finale": chapter >= total_chapters,
        })
    logger.info("第 %d 章完成（%d/%d）", chapter, chapter, total_chapters)
