"""Summarizer：人审通过后生成结构化摘要 + 记忆更新抽取（复用 editor 角色低温模型）。"""

from __future__ import annotations

from src.agents.prompt_loader import render_prompt
from src.agents.schemas import SummaryOutput
from src.agents.writer import _fmt_foreshadowing, _fmt_state_board
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "editor"  # 摘要要求精确低温，复用 editor 绑定


class Summarizer:
    def __init__(self, registry: ModelRegistry):
        self._registry = registry

    def summarize(
        self,
        chapter: int,
        chapter_text: str,
        unresolved_foreshadowing: list[dict],
        total_chapters: int = 0,
        is_finale: bool = False,
        state_board: list[dict] | None = None,
    ) -> SummaryOutput:
        logger.info("Summarizer: 生成第 %d 章摘要 ...", chapter)
        if is_finale:
            finale_directive = (
                "\n## ⚠ 完结章特别要求\n"
                "本章是全书最后一章。上面【已知未回收伏笔】中每一条都必须在本章正文中"
                "得到交代——请为每条给出对应的 resolve 操作（附其 id）。除非该伏笔正文确实"
                "完全未触及，否则不得遗漏；本章不要再 plant 任何新伏笔。"
            )
        else:
            plant_budget = (
                "\n## 埋设纪律\n"
                "- 只 plant 真正贯穿多章的叙事悬念（身世、阴谋、未解之谜、关键物品去向）；"
                "数值/状态变化（如业力值、修为层数）属于角色状态更新，不要当作伏笔 plant；\n"
                "- plant 时给出的 resolve_ch 必须晚于本章且不得超过全书总章数"
                f"（共 {total_chapters} 章）；临近结尾不要再埋新坑；\n"
                "- 优先输出 resolve：只要本章正文推进/兑现了某条已知未回收伏笔，就 resolve 它。"
            )
            finale_directive = plant_budget
        prompt = render_prompt(
            "summarizer",
            chapter=chapter,
            chapter_text=chapter_text,
            foreshadowing=_fmt_foreshadowing(unresolved_foreshadowing),
            state_board=_fmt_state_board(state_board or []),
            finale_directive=finale_directive,
        )
        out = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], SummaryOutput
        )
        logger.info(
            "Summarizer: 第 %d 章摘要完成（%d 字，%d 角色更新，%d 伏笔操作，%d 状态板变更）",
            chapter,
            len(out.summary),
            len(out.character_updates),
            len(out.foreshadow_ops),
            len(out.state_ops),
        )
        return out
