"""Editor：章节审查、分维度评分、问题标注；审查报告落盘 reviews/。"""

from __future__ import annotations

from src.agents.prompt_loader import render_prompt
from src.agents.schemas import (
    ArbitrationOutput,
    CrossVolumeReviewOutput,
    NegotiationOutput,
    ReviewIssue,
    ReviewOutput,
)
from src.agents.writer import (
    _fmt_characters,
    _fmt_foreshadowing,
    _fmt_list,
    _reality_policy_of,
    chapter_length,
    fmt_review_issues,
    resolve_bounds,
)
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.memory.md_store import MdStore
from src.memory.memory_manager import ChapterContext
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "editor"

# 分级打回阈值（D6）
PASS_THRESHOLD = 8.0
PARTIAL_THRESHOLD = 6.0
DEFAULT_TOLERANCE = 500


def verdict_of(score: float) -> str:
    """pass / partial_rewrite / full_rewrite"""
    if score >= PASS_THRESHOLD:
        return "pass"
    if score >= PARTIAL_THRESHOLD:
        return "partial_rewrite"
    return "full_rewrite"


def _fmt_responses(negotiation: NegotiationOutput) -> str:
    """Writer 逐条回应 → 仲裁提示词文本。"""
    lines = [
        f"{r.index}. [{'采纳' if r.stance == 'accept' else '申辩'}] {r.reply}"
        for r in negotiation.responses
    ]
    return "\n".join(lines) if lines else "（无）"


def negotiate_revision(pipe, ctx, review: ReviewOutput, chapter_text: str):
    """对话协商编排（P4-A）：Writer 逐条回应 → Editor 仲裁 → 定向修改清单。

    graph.py 与 scheduler.draft_chapter 共用。返回值语义：
    - 非空 str：仲裁后的定向修改指令（替代 issues_digest）
    - ""：全部问题被豁免，调用方应跳过重写直接送人审
    - None：协商链路异常/无可协商项，调用方回退 issues_digest
    """
    if not review.issues:
        return None
    try:
        negotiation = pipe.writer.respond_issues(ctx, review, chapter_text)
        arb = pipe.editor.arbitrate(ctx, review, negotiation)
    except Exception as exc:  # noqa: BLE001 - 协商失败不阻断主流程
        logger.warning("第 %d 章协商链路异常，回退问题清单直接重写：%s", ctx.chapter, exc)
        return None
    if not arb.items:
        # 仲裁结果缺失视为协商无效，不能误判为全部豁免
        logger.warning("第 %d 章仲裁结果为空，回退问题清单直接重写", ctx.chapter)
        return None
    return Editor.arbitration_digest(review, arb)


class Editor:
    """Editor Agent：temperature 0 审查。"""

    def __init__(self, registry: ModelRegistry, store: MdStore, tolerance: int = DEFAULT_TOLERANCE):
        self._registry = registry
        self._store = store
        self._tolerance = tolerance

    def review_chapter(
        self, ctx: ChapterContext, chapter_text: str, attempt: int,
        target_words: int | None = None,
        length_bounds: tuple[int, int] | None = None,
    ) -> ReviewOutput:
        logger.info("Editor: 审查第 %d 章（第 %d 稿）...", ctx.chapter, attempt)
        actual = chapter_length(chapter_text)
        # 字数区间必须与实际门禁同源：调用方已算好就传进来；**没传时按非对称口径现算**
        # （真机踩到：无条件 `*length_bounds` 展开 None 直接 TypeError，
        #  凡是不传该参数的调用点——含外部脚本/子进程直调——都会炸）。
        if target_words is not None:
            floor, ceiling = (
                resolve_bounds(int(target_words), *length_bounds)
                if length_bounds is not None
                else resolve_bounds(int(target_words))
            )
        else:
            floor = ceiling = DEFAULT_TOLERANCE
        prompt = render_prompt(
            "editor_review",
            chapter=ctx.chapter,
            outline=ctx.outline,
            recent_summaries=_fmt_list(ctx.recent_summaries),
            character_states=_fmt_characters(ctx.character_states),
            worldview_rules=_fmt_list(ctx.worldview_rules),
            foreshadowing=_fmt_foreshadowing(ctx.unresolved_foreshadowing),
            style_guide=ctx.style_guide.strip() or "（无）",
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
            # 作者创作需求：审查侧必须能看到"作者到底要什么"，否则无法判断约束是否落实
            brief=ctx.brief.strip() or "（未提供）",
            # 现实性口径（用户要求）：默认以作者创作目标为唯一基准，
            # 不得因"不符合现实"扣分或提建议；与写作链路同源同一份文本。
            reality_policy=_reality_policy_of(ctx),
            chapter_text=chapter_text,
            target_words=target_words if target_words is not None else "未指定",
            actual_length=actual,
            tolerance=floor,
            length_floor=floor,
            length_ceiling=ceiling,
        )
        review = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], ReviewOutput
        )
        if target_words is not None:
            review = self._reconcile_length(review, target_words, actual, floor, ceiling)
        logger.info(
            "Editor: 第 %d 章总分 %.1f（一致性%.1f/大纲%.1f/衔接%.1f/文笔%.1f/字数%.1f）→ %s",
            ctx.chapter,
            review.overall,
            review.consistency,
            review.plot,
            review.continuity,
            review.prose,
            review.length,
            verdict_of(review.overall),
        )
        self._save_report(ctx.chapter, review, attempt)
        return review

    @staticmethod
    def _reconcile_length(
        review: ReviewOutput, target: int, actual: int, floor: int, ceiling: int
    ) -> ReviewOutput:
        """字数维度的**确定性兜底**：把模型自评分与客观字数对齐。

        由来（用户实测）：字数是否达标原先完全依赖模型自评——模型经常给 length 满分、
        也常常漏掉 length issue，于是"实际远小于要求"的稿子照样 pass 送人审。
        字数是可以客观计算的，不该交给模型判断：这里按客观计数覆写 length 分，
        并在欠字数且模型没标注时补一条可执行的 length issue（谁写的正文都适用）。
        """
        if actual < floor:
            gap = floor - actual
            review.length = max(0.0, round(10.0 - (gap / 500.0) * 2, 1))
            if not any(i.dimension == "length" for i in review.issues):
                review.issues.append(ReviewIssue(
                    dimension="length",
                    severity="major" if gap >= 1000 else "minor",
                    description=f"正文 {actual} 字，低于本章下限 {floor} 字（目标 {target} 字），缺口 {gap} 字",
                    quote="",
                    suggestion=(
                        f"扩充至约 {target} 字以上（至少 {floor} 字）："
                        "补足场景过程与对话轮次、写出配角的具体反应、把主要冲突多推进一层，"
                        "不得用抽象概括或复述前文凑字数"
                    ),
                ))
        elif actual > ceiling:
            over = actual - ceiling
            review.length = max(0.0, round(10.0 - (over / 500.0) * 1, 1))
            if not any(i.dimension == "length" for i in review.issues):
                review.issues.append(ReviewIssue(
                    dimension="length",
                    severity="minor",
                    description=f"正文 {actual} 字，超过本章上限 {ceiling} 字（目标 {target} 字），超出 {over} 字",
                    quote="",
                    suggestion=f"压缩至 {target} 字附近（不超过 {ceiling} 字），保留核心事件与结尾钩子",
                ))
        else:
            # 落在非对称区间内 = 合格：模型若因"超出目标"而扣了分，这里纠正回来
            review.length = 10.0
            review.issues = [i for i in review.issues if i.dimension != "length"]
        return review

    def _save_report(self, chapter: int, review: ReviewOutput, attempt: int) -> None:
        lines = [
            f"# 第 {chapter} 章审查报告（第 {attempt} 稿）\n",
            f"**总分：{review.overall}** → {verdict_of(review.overall)}\n",
            "| 维度 | 得分 |\n|------|------|",
            f"| 设定一致性 | {review.consistency} |",
            f"| 大纲符合度 | {review.plot} |",
            f"| 衔接连贯性 | {review.continuity} |",
            f"| 文笔质量 | {review.prose} |",
            f"| 字数符合度 | {review.length} |",
            f"\n**总评**：{review.comment}\n",
        ]
        if review.issues:
            lines.append("## 问题标注\n")
            for i, issue in enumerate(review.issues, 1):
                lines.append(
                    f"{i}. **[{issue.severity}/{issue.dimension}]** {issue.description}\n"
                    f"   - 原文：「{issue.quote}」\n"
                    f"   - 建议：{issue.suggestion}\n"
                )
        self._store.write(
            self._store.review_rel_path(chapter),
            "\n".join(lines),
            metadata={
                "chapter": chapter,
                "attempt": attempt,
                "overall": review.overall,
                "verdict": verdict_of(review.overall),
                "scores": {
                    "consistency": review.consistency,
                    "plot": review.plot,
                    "continuity": review.continuity,
                    "prose": review.prose,
                    "length": review.length,
                },
            },
            commit_message=f"ch-{chapter:03d} 审查报告（第 {attempt} 稿）",
        )

    @staticmethod
    def issues_digest(review: ReviewOutput) -> str:
        """转为 Writer 重写指令文本。"""
        lines = [f"Editor 总评：{review.comment}（总分 {review.overall}）"]
        for i, issue in enumerate(review.issues, 1):
            lines.append(
                f"{i}. [{issue.severity}] {issue.description}"
                + (f"（原文「{issue.quote}」）" if issue.quote else "")
                + f" → {issue.suggestion}"
            )
        return "\n".join(lines)

    # ---------- 对话协商（v2.0 P4-A：partial_rewrite 时 Writer↔Editor 磋稿） ----------

    def arbitrate(
        self, ctx: ChapterContext, review: ReviewOutput, negotiation: NegotiationOutput
    ) -> ArbitrationOutput:
        """对 Writer 的逐条回应做最终仲裁（revise 坚持修改 / waive 接受申辩豁免）。"""
        logger.info("Editor: 第 %d 章仲裁 Writer 回应（协商）...", ctx.chapter)
        prompt = render_prompt(
            "editor_arbitrate",
            chapter=ctx.chapter,
            issues=fmt_review_issues(review),
            responses=_fmt_responses(negotiation),
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
        )
        arb = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], ArbitrationOutput
        )
        revise = sum(1 for it in arb.items if it.final == "revise")
        logger.info(
            "Editor: 第 %d 章仲裁完成（坚持修改 %d / 豁免 %d）",
            ctx.chapter, revise, len(arb.items) - revise,
        )
        return arb

    @staticmethod
    def arbitration_digest(review: ReviewOutput, arb: ArbitrationOutput) -> str:
        """仲裁清单 → Writer 定向修改指令（仅 final=revise 项；全部豁免返回空串）。"""
        issue_map = dict(enumerate(review.issues, 1))
        lines = []
        for item in arb.items:
            if item.final != "revise":
                continue
            issue = issue_map.get(item.index)
            severity = issue.severity if issue else "minor"
            directive = item.directive.strip() or (issue.suggestion if issue else "")
            quote = f"（原文「{issue.quote}」）" if issue and issue.quote else ""
            lines.append(f"{len(lines) + 1}. [{severity}] {directive}{quote}")
        if not lines:
            return ""
        return (
            f"Editor 总评：{review.comment}（总分 {review.overall}）\n"
            "经协商仲裁后的定向修改清单（其余问题已豁免，未提及处不要改动）：\n"
            + "\n".join(lines)
        )

    # ---------- 跨卷一致性合并审查（R2，卷级并行波次完成后执行） ----------

    def cross_volume_review(
        self,
        wave: int,
        volume_summaries: dict[int, list[str]],
        outline_digest: str,
        worldview_rules: list[str],
    ) -> CrossVolumeReviewOutput:
        """对同一并行波次的多个卷做跨卷一致性合并审查，报告落盘 reviews/。

        volume_summaries: {卷号: [该卷各章摘要文本...]}。
        """
        vols = sorted(volume_summaries)
        logger.info("Editor: 跨卷一致性审查（波次 %d，卷 %s）...", wave, vols)
        sections = []
        for vid in vols:
            body = "\n".join(volume_summaries[vid]) or "（无摘要）"
            sections.append(f"### 卷 {vid}\n{body}")
        prompt = render_prompt(
            "editor_cross_volume",
            outline_digest=outline_digest,
            worldview_rules=_fmt_list(worldview_rules),
            volume_summaries="\n\n".join(sections),
        )
        review = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], CrossVolumeReviewOutput
        )
        logger.info(
            "Editor: 跨卷审查完成（consistent=%s，%d 个问题）",
            review.consistent, len(review.issues),
        )
        self._save_cross_volume_report(wave, vols, review)
        return review

    def _save_cross_volume_report(
        self, wave: int, volumes: list[int], review: CrossVolumeReviewOutput
    ) -> None:
        lines = [
            f"# 跨卷一致性合并审查（波次 {wave}：卷 {'、'.join(map(str, volumes))}）\n",
            f"**结论：{'一致 ✓' if review.consistent else '存在 major 冲突 ✗'}**\n",
            f"**总评**：{review.comment}\n",
        ]
        if review.issues:
            lines.append("## 冲突标注\n")
            for i, issue in enumerate(review.issues, 1):
                lines.append(
                    f"{i}. **[{issue.severity}/卷{','.join(map(str, issue.volumes))}]** "
                    f"{issue.description}\n   - 建议：{issue.suggestion}\n"
                )
        self._store.write(
            f"reviews/cross-vol-wave-{wave:02d}.review.md",
            "\n".join(lines),
            metadata={
                "wave": wave,
                "volumes": volumes,
                "consistent": review.consistent,
                "issue_count": len(review.issues),
            },
            commit_message=f"波次 {wave} 跨卷一致性审查报告",
        )
