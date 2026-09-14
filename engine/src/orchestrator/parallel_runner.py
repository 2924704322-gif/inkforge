"""卷级并行主流程驱动器（T2.2 接入主执行链路）。

与串行主图（graph.py）互补的第二条执行路径，由 CLI `create/resume --parallel` 启用：

  大纲阶段（含人审打回循环）
    → 按卷依赖拓扑分波（compute_waves）
    → 波次内独立卷并行起草（writer→editor 自动分级重写，D6）
    → 合并后按章节号升序逐章人审（D4 逐章确认不妥协）
    → 人审通过即定稿（finalize_chapter：摘要/回写/事件）
    → 波次含多卷时执行跨卷一致性合并审查（R2）
    → 下一波次

断点续跑：不依赖图检查点，以 MD 事实源自证——status=approved 的章节直接跳过，
已存在的大纲直接复用，已生成的跨卷审查报告不重复执行；任意中断后重跑同一命令即恢复。
"""

from __future__ import annotations

from typing import Callable, Optional

from src.agents.architect import maybe_generate_style
from src.agents.writer import human_revision_notes
from src.memory.memory_manager import ChapterContext
from src.orchestrator.finalize import finalize_chapter
from src.orchestrator.scheduler import (
    chapters_of_volume,
    compute_waves,
    draft_chapter,
    merge_draft_records,
    parallel_draft_wave,
)
from src.skills.memory_bus import EVENT_OUTLINE_APPROVED
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 人审回调：payload -> {"action": "approve"|"reject", "feedback": str}
DecisionCallback = Callable[[dict], dict]
# 跨卷审查报告回调：(wave_no, volumes, CrossVolumeReviewOutput) -> None
WaveReportCallback = Callable[[int, list[int], object], None]

OUTLINE_REL = "settings/outline.md"


# ---------- 大纲阶段 ----------

def load_outline(store) -> Optional[dict]:
    """从 MD 事实源恢复大纲（断点续跑）；无大纲返回 None。"""
    if not store.exists(OUTLINE_REL):
        return None
    meta = store.read(OUTLINE_REL).metadata
    volumes = meta.get("volumes") or []
    if not volumes:
        return None
    return {
        "book_title": meta.get("title", ""),
        "theme": meta.get("theme", ""),
        "volumes": volumes,
    }


def ensure_outline(pipe, brief: str, total_chapters: int,
                   outline_cb: DecisionCallback, novel_id: str = "") -> dict:
    """已有大纲直接复用；否则 Architect 生成 + 大纲人审循环（与主图语义一致）。"""
    existing = load_outline(pipe.store)
    if existing is not None:
        logger.info("检测到既有大纲《%s》，跳过 Architect 阶段", existing["book_title"])
        return existing

    feedback = ""
    while True:
        effective_brief = brief
        if feedback:
            effective_brief = f"{brief}\n\n【人工审阅打回意见，必须落实】\n{feedback}"
        outline_out = pipe.architect.generate_settings(effective_brief, total_chapters)
        pipe.memory.rebuild_index()
        outline = outline_out.model_dump()
        decision = outline_cb({"type": "outline_review", "outline": outline})
        if decision.get("action") == "approve":
            logger.info("大纲人审通过，进入卷级并行章节循环")
            if pipe.bus is not None:
                pipe.bus.publish(EVENT_OUTLINE_APPROVED, {
                    "novel_id": novel_id,
                    "book_title": outline.get("book_title", ""),
                })
            return outline
        feedback = decision.get("feedback", "请改进大纲")
        logger.info("大纲被人工打回：%s", feedback)


# ---------- 跨卷一致性审查（R2） ----------

def _outline_digest(outline: dict) -> str:
    lines = []
    for vol in outline.get("volumes", []):
        chs = vol.get("chapters", [])
        span = f"第{chs[0]['chapter']}-{chs[-1]['chapter']}章" if chs else "（空）"
        dep = f"，依赖卷 {vol.get('depends_on')}" if vol.get("depends_on") else "，无依赖"
        lines.append(f"- 卷{vol['volume']}《{vol.get('title', '')}》{span}{dep}")
    return "\n".join(lines)


def _worldview_rules(store, per_doc: int = 600) -> list[str]:
    return [
        doc.content.strip()[:per_doc]
        for doc in store.iter_documents("settings/worldview")
    ]


def cross_volume_check(pipe, outline: dict, wave_no: int, volumes: list[int],
                       wave_report_cb: Optional[WaveReportCallback] = None) -> None:
    """波次含多卷时执行跨卷一致性合并审查；报告已存在则跳过（断点续跑）。"""
    if len(volumes) < 2:
        return
    report_rel = f"reviews/cross-vol-wave-{wave_no:02d}.review.md"
    if pipe.store.exists(report_rel):
        logger.info("波次 %d 跨卷审查报告已存在，跳过", wave_no)
        return
    volume_summaries: dict[int, list[str]] = {}
    for vid in volumes:
        summaries = []
        for plan in chapters_of_volume(outline, vid):
            rel = pipe.store.summary_rel_path(plan["chapter"])
            if pipe.store.exists(rel):
                summaries.append(
                    f"【第{plan['chapter']}章】{pipe.store.read(rel).content.strip()[:500]}"
                )
        volume_summaries[vid] = summaries
    review = pipe.editor.cross_volume_review(
        wave_no, volume_summaries, _outline_digest(outline),
        _worldview_rules(pipe.store),
    )
    if wave_report_cb is not None:
        wave_report_cb(wave_no, volumes, review)


# ---------- 主循环 ----------

def _approved_chapters(store) -> set[int]:
    return {
        c.metadata.get("chapter")
        for c in store.list_chapters()
        if c.metadata.get("status") == "approved" and c.metadata.get("chapter")
    }


def run_parallel(
    pipe,
    novel_id: str,
    brief: str,
    total_chapters: int,
    outline_cb: DecisionCallback,
    review_cb: DecisionCallback,
    max_workers: int = 0,
    wave_report_cb: Optional[WaveReportCallback] = None,
) -> dict:
    """卷级并行全流程。返回 {"chapters_done": int, "waves": int}。"""
    outline = ensure_outline(pipe, brief, total_chapters, outline_cb, novel_id)
    # 文风指纹（P4-D）：大纲就绪后幂等生成（style.md 已存在则跳过）
    maybe_generate_style(pipe, brief, outline)
    state = {"outline": outline, "total_chapters": total_chapters}
    waves = compute_waves(outline)
    approved = _approved_chapters(pipe.store)
    logger.info(
        "卷级并行启动：%d 个波次，已完成 %d/%d 章", len(waves), len(approved), total_chapters
    )

    for wave_no, wave in enumerate(waves, start=1):
        pending_vols = [
            vid for vid in wave
            if any(p["chapter"] not in approved for p in chapters_of_volume(outline, vid))
        ]
        if pending_vols:
            # 人工可能直接编辑过设定/摘要 MD：并行起草前检测并重嵌入
            pipe.memory.sync_changed()
            logger.info("波次 %d：并行起草卷 %s", wave_no, pending_vols)
            wave_res = parallel_draft_wave(
                pipe, state, pending_vols, max_workers=max_workers,
                skip_chapters=approved,
            )
            # 合并为章节号升序队列，逐章人审（串行）
            for rec in merge_draft_records(wave_res):
                _review_until_approved(pipe, state, rec, review_cb, approved)
        cross_volume_check(pipe, outline, wave_no, wave, wave_report_cb)

    return {"chapters_done": len(approved), "waves": len(waves)}


def _review_until_approved(pipe, state: dict, rec: dict,
                           review_cb: DecisionCallback, approved: set[int]) -> None:
    """单章人审循环：打回则携人工意见重稿（重置自动重试计数），直至通过并定稿。"""
    first_review_passed: Optional[bool] = None
    while True:
        decision = review_cb({
            "type": "chapter_review",
            "chapter": rec["chapter"],
            "volume": rec["volume"],
            "draft_text": rec["draft_text"],
            "review": rec.get("review", {}),
            "retry_exceeded": rec.get("retry_exceeded", False),
            "attempt": rec.get("attempt", 1),
            "model": rec.get("model"),
            "used_fallback": rec.get("used_fallback", False),
        })
        if decision.get("action") == "approve":
            if first_review_passed is None:
                first_review_passed = True
            logger.info("第 %d 章人审通过", rec["chapter"])
            finalize_chapter(
                pipe,
                chapter=rec["chapter"],
                volume=rec["volume"],
                draft_text=rec["draft_text"],
                ctx=ChapterContext(**rec["ctx"]),
                review=rec.get("review") or None,
                first_review_passed=first_review_passed,
                total_chapters=state["total_chapters"],
            )
            approved.add(rec["chapter"])
            return
        first_review_passed = False
        feedback = decision.get("feedback", "")
        mode = decision.get("revision_mode", "targeted")
        logger.info("第 %d 章被人工打回（%s）：%s", rec["chapter"], mode, feedback)
        plan = {"chapter": rec["chapter"], "volume": rec["volume"], "title": rec["title"]}
        # 重稿前刷新状态板：波次内前面章节定稿新增的硬事实要让重稿看到
        ctx = ChapterContext(**rec["ctx"])
        fresh = pipe.memory.retrieve_context(
            chapter=ctx.chapter,
            chapter_outline=ctx.outline,
            characters=list(ctx.character_states.keys()),
            total_chapters=state["total_chapters"],
            is_finale=ctx.chapter >= state["total_chapters"],
        )
        ctx.state_board = fresh.state_board
        rec = draft_chapter(
            pipe, plan, ctx,
            revision_notes=human_revision_notes(feedback, mode),
            previous_text=rec["draft_text"],
            start_attempt=rec.get("attempt", 1),
        )
