"""Writer：基于 RAG 上下文生成章节正文（自由文本，非结构化）。"""

from __future__ import annotations

from src.agents.prompt_loader import render_prompt
from src.agents.schemas import NegotiationOutput, ReviewOutput
from src.llm.base import ChatMessage, ChatResult
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.memory.memory_manager import ChapterContext
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "writer"
DEFAULT_TARGET_WORDS = 5000
DEFAULT_TOLERANCE = 500
DEFAULT_MAX_CONTINUATION_ATTEMPTS = 1


def chapter_length(text: str) -> int:
    """章节字数（中文按字符计）。全书统一口径。"""
    return len(text)


def length_deviation(actual: int, target: int) -> int:
    """字数偏差绝对值。"""
    return abs(actual - target)


def length_revision_note(target: int, actual: int) -> str:
    """字数门禁打回指令（客观约束，不协商）。"""
    if actual < target:
        return (
            f"【字数修正】当前正文 {actual} 字，少于目标 {target} 字（允许误差 ±500 字）。"
            f"请在原稿基础上自然扩充约 {target - actual} 字：深化场景细节、补足对话与心理层次，"
            f"不得注水或重复，保持结尾钩子不变。"
        )
    return (
        f"【字数修正】当前正文 {actual} 字，超出目标 {target} 字约 {actual - target} 字。"
        f"请删减冗余描写与无关枝节，压缩至目标附近（允许误差 ±500 字），"
        f"保留全部核心事件、关键对话与结尾钩子。"
    )

# 人工打回意见固定前缀（供编排层复用；下游断言依赖此文本）
HUMAN_REVISION_HEADER = "【人工审阅打回意见，必须逐条落实】"


def human_revision_notes(feedback: str, mode: str = "targeted") -> str:
    """把人工自然语言意见组装成 Writer 重写指令（P3）。

    mode="targeted"（定向修订，默认）：严格保留未提及内容，只改意见涉及之处；
    mode="rewrite"（整体重写）：允许对全章大幅调整以落实意见。
    """
    if mode == "rewrite":
        directive = (
            "\n\n【修订模式：整体重写】可对全章结构与措辞大幅调整以落实上述意见，"
            "但须保持本章大纲核心事件、结尾钩子与既有设定不变。"
        )
    else:  # targeted
        directive = (
            "\n\n【修订模式：定向修订】只修改上述意见明确涉及之处；"
            "未被提及的段落必须逐字保留，不得改写、删减或调整其措辞与情节。"
        )
    return f"{HUMAN_REVISION_HEADER}\n{feedback}{directive}"


def _fmt_list(items: list[str], empty: str = "（无）") -> str:
    return "\n\n".join(items) if items else empty


def _fmt_characters(states: dict[str, str]) -> str:
    if not states:
        return "（无）"
    return "\n\n".join(f"### {name}\n{state}" for name, state in states.items())


def _fmt_foreshadowing(items: list[dict]) -> str:
    if not items:
        return "（无）"
    return "\n".join(
        f"- [{i.get('id')}] {i.get('desc')}（埋设于第{i.get('planted_ch')}章，"
        f"预期第{i.get('resolve_ch')}章回收）"
        for i in items
    )


def _fmt_state_board(items: list[dict]) -> str:
    if not items:
        return "（无）"
    return "\n".join(
        f"- 【{i.get('entity')}】{i.get('fact')}（第{i.get('chapter')}章确立）"
        for i in items
    )


def fmt_review_issues(review: ReviewOutput) -> str:
    """审查问题清单 → 带序号的文本（协商/仲裁提示词共用，序号与 index 对齐）。"""
    lines = [
        f"{i}. [{issue.severity}/{issue.dimension}] {issue.description}"
        + (f"（原文「{issue.quote}」）" if issue.quote else "")
        + f" → 建议：{issue.suggestion}"
        for i, issue in enumerate(review.issues, 1)
    ]
    return "\n".join(lines) if lines else "（无）"


class Writer:
    """章节正文生成；支持携带 Editor 问题清单 / 人工意见的重写。"""

    def __init__(
        self,
        registry: ModelRegistry,
        target_words: int = DEFAULT_TARGET_WORDS,
        tolerance: int = DEFAULT_TOLERANCE,
        max_continuation_attempts: int = DEFAULT_MAX_CONTINUATION_ATTEMPTS,
    ):
        self._registry = registry
        self._target_words = target_words
        self._tolerance = tolerance
        self._max_continuation_attempts = max_continuation_attempts

    @property
    def target_words(self) -> int:
        return self._target_words

    def write_chapter(
        self,
        ctx: ChapterContext,
        revision_notes: str | None = None,
        previous_text: str | None = None,
        target_words_override: int | None = None,
    ) -> ChatResult:
        """生成/重写章节。revision_notes 非空时为重写模式。
        target_words_override 非空时覆盖实例默认 target_words，用于互动模式按章自定义字数。"""
        target = target_words_override if target_words_override is not None else self._target_words
        revision_section = ""
        if revision_notes:
            revision_section = (
                "## 重写指令（必须逐条落实）\n"
                f"{revision_notes}\n"
                "\n## ⚠ 重写约束提醒（刚性，与人审意见同等优先级）\n"
                f"- 字数目标：重写后正文仍须达到 {target} 字（允许误差 ±{self._tolerance} 字），不得因修订而缩水；\n"
                "- 剧情卡遵循：重写不得偏离【本章大纲】中剧情卡已确定的人物、场景、核心事件与结尾钩子；\n"
                "- 自定义约束：若【自定义创作约束】非空，其全部规则在重写中仍然生效，为最高优先级；\n"
                '- 新增描写必须在剧情卡框架内展开，不得借「丰富细节」之名自行添加剧情卡未提及的场景或角色。\n'
            )
            if previous_text:
                revision_section += (
                    "\n## 上一稿正文（在此基础上按上述修订指令修改）\n"
                    f"{previous_text}\n"
                )
        prompt = render_prompt(
            "writer_chapter",
            target_words=target,
            tolerance=self._tolerance,
            revision_section=revision_section,
            chapter=ctx.chapter,
            outline=ctx.outline,
            recent_summaries=_fmt_list(ctx.recent_summaries),
            related_summaries=_fmt_list(ctx.related_summaries),
            character_states=_fmt_characters(ctx.character_states),
            foreshadowing=_fmt_foreshadowing(ctx.unresolved_foreshadowing),
            due_foreshadowing=_fmt_foreshadowing(ctx.due_foreshadowing),
            worldview_rules=_fmt_list(ctx.worldview_rules),
            state_board=_fmt_state_board(ctx.state_board),
            style_guide=ctx.style_guide.strip() or "（无）",
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
        )
        mode = "重写" if revision_notes else "初稿"
        logger.info("Writer: 第 %d 章%s生成中 ...", ctx.chapter, mode)
        result = self._registry.chat_as(ROLE, [ChatMessage("user", prompt)])

        # Layer 2: 后处理字数核验 —— 超差时追加续写/压缩（上限可控）
        draft = result.content
        for cont_i in range(self._max_continuation_attempts):
            actual = chapter_length(draft)
            if length_deviation(actual, target) <= self._tolerance:
                break
            if actual < target:
                logger.info(
                    "Writer: 第 %d 章第 %d 稿 %d 字不足目标 %d 字，追加续写...",
                    ctx.chapter, cont_i + 1, actual, target,
                )
                note = (
                    f"【字数补充】上一稿 {actual} 字，距目标 {target} 字还差约 {target - actual} 字。"
                    f"紧接上一稿末尾自然续写以推进剧情，只输出新增正文，不得重复已有内容。"
                )
                extra = self._registry.chat_as(
                    ROLE,
                    [ChatMessage("user", f"{prompt}\n\n{note}\n\n## 上一稿正文（仅作续写衔接）\n{draft}")],
                )
                draft = draft + "\n\n" + extra.content
            else:
                logger.info(
                    "Writer: 第 %d 章第 %d 稿 %d 字超出目标 %d 字，压缩中...",
                    ctx.chapter, cont_i + 1, actual, target,
                )
                note = (
                    f"【字数压缩】上一稿 {actual} 字，超出目标 {target} 字约 {actual - target} 字。"
                    f"删减冗余描写、重复表达与无关枝节，输出压缩后的完整正文，"
                    f"保留核心事件、关键对话与结尾钩子，不得省略情节。"
                )
                trimmed = self._registry.chat_as(
                    ROLE,
                    [ChatMessage("user", f"{prompt}\n\n{note}\n\n## 上一稿正文（在此基础上压缩）\n{draft}")],
                )
                draft = trimmed.content
        result.content = draft

        logger.info(
            "Writer: 第 %d 章%s完成（%d 字 / 目标 %d 字，偏差 %+d，tokens=%s）",
            ctx.chapter,
            mode,
            chapter_length(result.content),
            target,
            chapter_length(result.content) - target,
            result.usage.get("completion_tokens", "?"),
        )
        return result

    def respond_issues(
        self, ctx: ChapterContext, review: ReviewOutput, chapter_text: str
    ) -> NegotiationOutput:
        """对话协商（P4-A）：对 Editor 问题清单逐条回应（采纳/申辩）。"""
        prompt = render_prompt(
            "writer_negotiate",
            chapter=ctx.chapter,
            outline=ctx.outline,
            issues=fmt_review_issues(review),
            comment=review.comment or "（无）",
            chapter_text=chapter_text,
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
        )
        logger.info(
            "Writer: 第 %d 章对 %d 条审查问题逐条回应（协商）...",
            ctx.chapter, len(review.issues),
        )
        return chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], NegotiationOutput
        )
