"""Plotter：互动创作模式的剧情策划——基于 RAG 上下文为下一章生成 3 张剧情卡。

复用 architect 角色的模型绑定（同属规划型任务，无需在 models.yaml 新增角色）。
"""

from __future__ import annotations

from src.agents.architect import brief_fidelity_block, generate_faithful
from src.agents.prompt_loader import render_prompt
from src.agents.schemas import PlotCardsOutput
from src.agents.writer import (
    _fmt_characters,
    _fmt_foreshadowing,
    _fmt_list,
    _fmt_state_board,
)
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.memory.md_store import MdStore
from src.memory.memory_manager import ChapterContext
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "architect"  # 剧情策划属规划型任务，复用 architect 绑定


class Plotter:
    """每章生成 3 张方向互斥的剧情卡，供用户四选一（第 4 张为用户自定义卡）。"""

    def __init__(self, registry: ModelRegistry, store: MdStore):
        self._registry = registry
        self._store = store

    def generate_cards(
        self, ctx: ChapterContext, chapter: int, feedback: str = ""
    ) -> PlotCardsOutput:
        """基于七维上下文生成剧情卡；feedback 为用户重抽意见（必须落实）。

        防拒绝：模型偶发拒答（或产出不足 3 张卡）时**自动显式重试一次**。
        此前一次拒答会直接抛错、整章互动创作中断；重试时补一段强制说明：
        这是虚构小说创作，必须直接产出恰好 3 张卡的 JSON，不得拒答或解释。
        """
        for attempt in (1, 2):
            try:
                return self._generate_cards_once(
                    ctx, chapter, feedback, strict=attempt == 2
                )
            except Exception as exc:  # noqa: BLE001 - 拒答/解析失败一律走重试
                if attempt == 2:
                    raise
                logger.warning(
                    "第 %d 章剧情卡生成失败（%s:%s），显式重试一次",
                    chapter, type(exc).__name__, exc,
                )
        raise RuntimeError("unreachable")  # pragma: no cover

    def _generate_cards_once(
        self, ctx: ChapterContext, chapter: int, feedback: str, strict: bool = False
    ) -> PlotCardsOutput:
        logger.info("Plotter: 生成第 %d 章剧情卡 ...%s", chapter, "（重试）" if strict else "")
        prompt = render_prompt(
            "plotter_cards",
            chapter=chapter,
            story_overview=self._story_overview(),
            recent_summaries=_fmt_list(ctx.recent_summaries),
            related_summaries=_fmt_list(ctx.related_summaries),
            character_states=_fmt_characters(ctx.character_states),
            state_board=_fmt_state_board(ctx.state_board),
            foreshadowing=_fmt_foreshadowing(ctx.unresolved_foreshadowing),
            due_foreshadowing=_fmt_foreshadowing(ctx.due_foreshadowing),
            worldview_rules=_fmt_list(ctx.worldview_rules),
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
            feedback=feedback.strip() or "（无）",
        )
        # 虚构框架 + 零拒绝 + 约束优先级链（与世界观/大纲同源，此前出卡链路缺这一段）
        prompt = f"{brief_fidelity_block()}\n\n{prompt}"
        if strict:
            prompt += (
                "\n\n【必须产出（上一次未产出合规结果）】\n"
                "这是一部**虚构小说**的创作辅助，按作者设定与上文上下文执行；\n"
                "不得拒答、不得解释、不得道歉、不得输出 JSON 以外的任何内容；\n"
                "必须给出恰好 3 张互斥的剧情卡（c1/c2/c3），每张含 title / outline / hook / characters。"
            )
        # 与 architect 同源的保真链：空产出/拒绝措辞在系统内消化（结果不落成拒答文案）。
        # 出卡此前只有"异常重试"，模型**成功返回一段拒绝文本**时不会被识别，会直接变成剧情卡内容。
        out = generate_faithful(
            lambda msgs, temp: chat_structured(
                self._registry, ROLE, msgs, PlotCardsOutput, temperature=temp
            ),
            [ChatMessage("user", prompt)],
        )
        out = self._normalize(out)
        logger.info(
            "Plotter: 第 %d 章剧情卡完成（%s）",
            chapter, "、".join(f"[{c.tag}]{c.title}" for c in out.cards),
        )
        return out

    @staticmethod
    def _normalize(out: PlotCardsOutput) -> PlotCardsOutput:
        """规范化：截断多余卡、card_id 强制为 c1/c2/c3；不足 3 张 Fail-Fast。"""
        if len(out.cards) < 3:
            raise RuntimeError(f"Plotter 只产出了 {len(out.cards)} 张剧情卡，要求恰好 3 张")
        out.cards = out.cards[:3]
        for i, card in enumerate(out.cards, 1):
            card.card_id = f"c{i}"
        return out

    def _story_overview(self, limit: int = 1200) -> str:
        """直读已确认的 story-overview.md（书名/主题/梗概/概要）作为全书基调。"""
        rel = "settings/story-overview.md"
        if not self._store.exists(rel):
            return "（无）"
        return self._store.read(rel).content.strip()[:limit]
