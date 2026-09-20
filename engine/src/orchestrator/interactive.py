"""互动创作模式驱动器（剧情卡选择）：逐章"出卡 → 选卡 → 写章 → 人审 → 入库"。

设计要点：
- 独立于 LangGraph（无预生成全书大纲，逐章由用户选卡驱动），不污染 checkpoint；
- 完全复用 Pipeline 组件：retrieve_context 七维检索保证上下文连贯，
  Writer/Editor 完成创作与参考审查，finalize_chapter 保证"人审通过才入库"；
- 剧情卡与用户选择落盘为 MD（interactive/ch-NNN.cards.md，唯一事实源），
  服务重启后可从 MD 恢复到选卡/审核断点；
- 逐章追加渐进式大纲 settings/interactive-outline.md（与 outline.md 分离，
  避免书架把"已写章数==计划章数"误判为完结）。
"""

from __future__ import annotations

from src.agents.plotter import Plotter
from src.agents.writer import (
    chapter_length,
    human_revision_notes,
    length_assessment,
    length_revision_note,
)
from src.memory.memory_manager import ChapterContext
from src.orchestrator.finalize import finalize_chapter
from src.orchestrator.scheduler import generation_config
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 互动模式统一单卷
VOLUME = 1
# 开放式连载无预设总章数；finalize/Summarizer 需要 total_chapters 判定完结章与
# 伏笔回收窗口，用"当前章 + 滚动地平线"代替，保证永不触发完结语义。
HORIZON = 20

INTERACTIVE_OUTLINE_REL = "settings/interactive-outline.md"

# 新角色隔离约束标头（注入 custom_constraints，Writer 写作遵循、Editor 审查校验；
# 下游断言依赖此文本）
NEW_CHARACTER_RULE_HEADER = "【新角色隔离约束】"


def cards_rel_path(chapter: int) -> str:
    return f"interactive/ch-{chapter:03d}.cards.md"


def is_interactive_book(store) -> bool:
    """互动模式书籍识别：interactive/ 目录存在即视为互动书。"""
    return (store.root / "interactive").is_dir()


class InteractiveRunner:
    """单章循环的纯逻辑封装（无线程/会话状态，便于单测）。"""

    def __init__(self, pipe, plotter: Plotter | None = None):
        self.pipe = pipe
        self.plotter = plotter or Plotter(pipe.registry, pipe.store)

    # ---------- 进度恢复（MD 事实源自证） ----------

    def next_chapter(self) -> int:
        """下一待创作章号 = 已定稿最大章号 + 1。"""
        approved = [
            c.metadata.get("chapter", 0)
            for c in self.pipe.store.list_chapters()
            if c.metadata.get("status") == "approved"
        ]
        return (max(approved) + 1) if approved else 1

    def approved_count(self) -> int:
        return sum(
            1 for c in self.pipe.store.list_chapters()
            if c.metadata.get("status") == "approved"
        )

    def load_cards(self, chapter: int) -> dict | None:
        """回读某章剧情卡记录：{cards, chosen, custom_text, target_words}；无卡返回 None。"""
        rel = cards_rel_path(chapter)
        if not self.pipe.store.exists(rel):
            return None
        meta = self.pipe.store.read(rel).metadata
        return {
            "cards": meta.get("cards") or [],
            "chosen": meta.get("chosen"),
            "custom_text": meta.get("custom_text", ""),
            # 生成前设定的本章预期字数（落卡文件 = 事实源：重启/断点续写都读它）
            "target_words": meta.get("target_words"),
        }

    def draft_snapshot(self, chapter: int) -> dict | None:
        """回读某章未定稿草稿（断点恢复到审核阶段用）。"""
        rel = self.pipe.store.chapter_rel_path(VOLUME, chapter)
        if not self.pipe.store.exists(rel):
            return None
        doc = self.pipe.store.read(rel)
        if doc.metadata.get("status") != "draft":
            return None
        words = doc.metadata.get("words")
        if words is None:
            words = len(doc.content)
        target = doc.metadata.get("target_words")
        return {
            "draft_text": doc.content,
            "attempt": doc.metadata.get("attempt", 1),
            "title": doc.metadata.get("title", ""),
            "review": self.load_review(chapter),
            "words": words,
            "target_words": target,
            # 字数区间（非对称）：审阅卡显示"合格线"用，与门禁同一对数
            "length_floor": self.length_bounds(target)[0] if target else None,
            "length_ceiling": self.length_bounds(target)[1] if target else None,
            "length_deviation": (words - target) if target else None,
        }

    def length_bounds(self, target: int | None) -> tuple[int | None, int | None]:
        """本章可接受字数区间（最低, 最高）；无目标时返回 (None, None)。"""
        if not target:
            return None, None
        return generation_config(self.pipe).length_bounds(int(target))

    def load_review(self, chapter: int) -> dict | None:
        """从审查报告 frontmatter 重建评分 dict（服务重启后恢复展示/定稿用）。"""
        rel = self.pipe.store.review_rel_path(chapter)
        if not self.pipe.store.exists(rel):
            return None
        meta = self.pipe.store.read(rel).metadata
        scores = meta.get("scores") or {}
        if not all(k in scores for k in ("consistency", "plot", "continuity", "prose")):
            return None
        return {**scores, "issues": [], "comment": ""}

    # ---------- 出卡 ----------

    def generate_cards(self, chapter: int, feedback: str = "") -> list[dict]:
        """每章开始：同步人工编辑 → 七维检索 → Plotter 出 3 张卡 → 落盘。"""
        self.pipe.memory.sync_changed()
        ctx = self._plot_context(chapter)
        out = self.plotter.generate_cards(ctx, chapter, feedback=feedback)
        cards = [c.model_dump() for c in out.cards]
        self._save_cards(chapter, cards)
        return cards

    def _plot_context(self, chapter: int) -> ChapterContext:
        """出卡用上下文：以上一章摘要（或全书概要）为 Query，主要角色全员参与。"""
        query = ""
        prev_rel = self.pipe.store.summary_rel_path(chapter - 1)
        if chapter > 1 and self.pipe.store.exists(prev_rel):
            query = self.pipe.store.read(prev_rel).content.strip()[:300]
        if not query and self.pipe.store.exists("settings/story-overview.md"):
            query = self.pipe.store.read("settings/story-overview.md").content.strip()[:300]
        return self.pipe.memory.retrieve_context(
            chapter=chapter,
            chapter_outline=query or f"第{chapter}章剧情规划",
            characters=self.main_characters(),
            total_chapters=chapter + HORIZON,
        )

    def main_characters(self) -> list[str]:
        """主要角色清单（settings/characters/ 档案标题）。"""
        names = []
        for doc in self.pipe.store.iter_documents("settings/characters"):
            name = doc.metadata.get("title")
            if name:
                names.append(name)
        return names

    def _save_cards(self, chapter: int, cards: list[dict],
                    chosen: str | None = None, custom_text: str = "",
                    target_words: int | None = None) -> None:
        lines = [f"# 第 {chapter} 章剧情卡\n"]
        for c in cards:
            lines.append(
                f"## [{c.get('card_id')}] {c.get('title')}（{c.get('tag')}）\n\n"
                f"{c.get('outline', '')}\n\n"
                f"- 结尾钩子：{c.get('hook', '')}\n"
                f"- 出场角色：{'、'.join(c.get('characters', []) or [])}\n"
            )
        if chosen:
            label = "用户自定义卡" if chosen == "custom" else chosen
            lines.append(f"\n> ✅ 用户选择：{label}\n")
            if custom_text:
                lines.append(f"\n{custom_text}\n")
        # 目标字数与选择同一次落盘：断点续写/重启后要能取回用户生成前设的那个数
        prev_target = self.load_cards(chapter) or {}
        meta_target = target_words or prev_target.get("target_words")
        if meta_target:
            lines.append(f"\n> 🎯 本章预期字数：{meta_target} 字\n")
        self.pipe.store.write(
            cards_rel_path(chapter),
            "\n".join(lines),
            metadata={
                "chapter": chapter,
                "cards": cards,
                "chosen": chosen,
                "custom_text": custom_text,
                "target_words": meta_target,
            },
            commit_message=f"ch-{chapter:03d} 剧情卡"
                           + ("（已选卡）" if chosen else ""),
        )

    # ---------- 选卡 ----------

    def choose_card(self, chapter: int, card_id: str, custom_text: str = "",
                    target_words: int | None = None) -> dict:
        """记录用户选择并返回本章计划 {title, outline, characters}。

        target_words：用户在选卡页设定的本章预期字数（生成前设定）——与选择同一次落盘，
        断点续写/重启都读得回，避免"设了 5000、中断后续写变成默认值"。
        """
        record = self.load_cards(chapter)
        if record is None:
            raise RuntimeError(f"第 {chapter} 章尚未生成剧情卡")
        cards = record["cards"]
        if card_id == "custom":
            text = custom_text.strip()
            if not text:
                raise ValueError("自定义卡剧情内容不能为空")
            title = text.splitlines()[0].strip()[:15]
            plan = {"title": title, "outline": text,
                    "characters": self.main_characters()}
        else:
            card = next((c for c in cards if c.get("card_id") == card_id), None)
            if card is None:
                raise ValueError(f"剧情卡不存在：{card_id}")
            plan = {"title": card.get("title", ""),
                    "outline": card.get("outline", ""),
                    "characters": card.get("characters", []) or []}
            custom_text = ""
        self._save_cards(chapter, cards, chosen=card_id, custom_text=custom_text,
                         target_words=target_words)
        return plan

    def chosen_plan(self, chapter: int) -> dict | None:
        """回读已选卡对应的本章计划（断点恢复/定稿用）。"""
        record = self.load_cards(chapter)
        if record is None or not record["chosen"]:
            return None
        chosen = record["chosen"]
        if chosen == "custom":
            text = (record["custom_text"] or "").strip()
            return {"title": text.splitlines()[0].strip()[:15] if text else "",
                    "outline": text, "characters": self.main_characters()}
        card = next(
            (c for c in record["cards"] if c.get("card_id") == chosen), None
        )
        if card is None:
            return None
        return {"title": card.get("title", ""),
                "outline": card.get("outline", ""),
                "characters": card.get("characters", []) or []}

    # ---------- 写章（选卡后 / 打回重写） ----------

    def write_chapter(
        self,
        chapter: int,
        plan: dict,
        feedback: str = "",
        revision_mode: str = "targeted",
        target_words: int | None = None,
    ) -> dict:
        """选中卡 → ChapterContext → Writer 落盘草稿 → Editor 审查报告（仅供参考）。

        feedback 非空时为人工打回重写：携意见与上一稿走 Writer 重写链路。
        target_words 非空时覆盖 Writer 默认字数目标，用于互动模式按章自定义。

        **字数门禁（非对称 · 用户要求）**：互动路径此前是"写一遍、超差只打一条日志
        就算了"（`Layer 2 已达上限，请人工注意`）——用户实测"要求 5000 字、实际远小于"
        的最直接原因就在这里。现在与自由创作走同一套门禁：**低于下限就带差额打回重写**
        （上限 `max_length_retries`），上浮在 ceiling 以内视为合格，不再逼模型压缩。
        """
        ctx = self._chapter_context(chapter, plan)
        # 目标优先取调用方显式传入；未传则沿用本章已落盘的目标（打回重写时不再退回默认值，
        # 否则"生成前设的 5000 字"会在第一次打回后静默变成别的数）。
        prev = self.draft_snapshot(chapter)
        if target_words is None and prev and prev.get("target_words"):
            target_words = int(prev["target_words"])
        floor, ceiling = self.length_bounds(target_words)

        revision_notes, previous_text = None, None
        if feedback:
            revision_notes = human_revision_notes(feedback, revision_mode)
            # 整章重写不携带上一稿（与 graph 的 full_rewrite 分支同语义）；定向修订必须带，
            # 否则"未点名处逐字保留"这条约束无从落地。
            if revision_mode == "targeted" and prev:
                previous_text = prev["draft_text"]

        gen = generation_config(self.pipe)
        max_attempts = max(1, int(gen.max_length_retries) + 1)
        attempt = (prev["attempt"] + 1) if prev else 1
        result = None
        note = revision_notes
        words = 0
        length_note = ""
        for round_i in range(max_attempts):
            result = self.pipe.writer.write_chapter(
                ctx,
                revision_notes=note,
                previous_text=previous_text if note else None,
                target_words_override=target_words,
            )
            words = chapter_length(result.content)
            if target_words is None or not gen.length_gate_enabled:
                break
            state = length_assessment(target_words, words, floor, ceiling)
            if state == "pass":
                break
            if round_i + 1 >= max_attempts:
                logger.warning(
                    "第 %d 章字数仍未达标：实际 %d 字 / 目标 %d 字（可接受 %d-%d），"
                    "已重写 %d 轮，送人工裁决",
                    chapter, words, target_words, floor, ceiling, round_i + 1,
                )
                break
            length_note = length_revision_note(target_words, words, floor, ceiling)
            logger.info(
                "第 %d 章字数门禁触发（第 %d 轮）：实际 %d 字 / 目标 %d 字（可接受 %d-%d）→ 打回重写",
                chapter, round_i + 1, words, target_words, floor, ceiling,
            )
            # 重写必须在上一稿基础上改：定向带原文，超上限压缩时也带原文
            previous_text = result.content
            # 第 2 轮起仍要带上**原始打回意见**，否则"按意见改"只发生在第一轮，
            # 后续轮次会退化成"只补字数"（用户实测的"改了字数又丢了建议"）。
            note = "\n\n".join(p for p in (revision_notes, length_note) if p)
            attempt += 1


        rel = self.pipe.store.chapter_rel_path(VOLUME, chapter)
        self.pipe.store.write(
            rel,
            result.content,
            metadata={
                "chapter": chapter,
                "volume": VOLUME,
                "title": plan.get("title", ""),
                "characters": plan.get("characters", []),
                "status": "draft",
                "attempt": attempt,
                "model": f"{result.provider_name}/{result.model}",
                "used_fallback": result.used_fallback,
                "words": words,
                "target_words": target_words,
            },
            commit_message=f"ch-{chapter:03d} 第 {attempt} 稿（互动模式）",
        )
        review = self.pipe.editor.review_chapter(
            ctx, result.content, attempt, target_words=target_words,
            length_bounds=(floor, ceiling) if target_words else None,
        )
        in_band = (
            length_assessment(target_words, words, floor, ceiling) == "pass"
            if target_words is not None else None
        )
        if target_words is not None and in_band is False:
            logger.warning(
                "第 %d 章字数落在可接受区间外：%d 字（目标 %d，可接受 %d-%d），请人工注意",
                chapter, words, target_words, floor, ceiling,
            )
        return {
            "draft_text": result.content,
            "attempt": attempt,
            "review": review.model_dump(),
            "model": f"{result.provider_name}/{result.model}",
            "words": words,
            "target_words": target_words,
            "length_deviation": (words - target_words) if target_words else None,
            "length_floor": floor,
            "length_ceiling": ceiling,
            "length_ok": in_band,
        }

    def author_directive(self, chapter: int) -> str:
        """作者自拟卡的原话（本章最高优先级约束）。

        断点恢复同样成立：原话存在剧情卡记录里，不依赖调用方传参。
        """
        record = self.load_cards(chapter)
        if not record or record.get("chosen") != "custom":
            return ""
        return (record.get("custom_text") or "").strip()

    def _chapter_context(self, chapter: int, plan: dict) -> ChapterContext:
        self.pipe.memory.sync_changed()
        ctx = self.pipe.memory.retrieve_context(
            chapter=chapter,
            chapter_outline=f"《{plan.get('title', '')}》{plan.get('outline', '')}",
            characters=plan.get("characters", []) or [],
            total_chapters=chapter + HORIZON,
        )
        # 互动模式专属：新角色未经剧情卡明确说明，禁止与资料库已有角色产生既有关系
        rule = self._new_character_rule(plan)
        base = ctx.custom_constraints.strip()
        ctx.custom_constraints = f"{base}\n\n{rule}" if base else rule
        # 作者自拟剧情卡：原话逐字注入最高优先级通道（与自定义 Skill 同级）
        directive = self.author_directive(chapter)
        if directive:
            block = (
                "【作者钦定的本章走向（最高优先级，必须逐条落实）】\n"
                f"{directive}\n"
                "要求：本章必须按上述走向推进；不得替换成其它方向、不得省略其中任一条、"
                "不得提前或延后到其它章节。若与既有上下文冲突，以上述走向为准并做最小改写。"
            )
            ctx.custom_constraints = f"{ctx.custom_constraints}\n\n{block}"
        return ctx

    def _new_character_rule(self, plan: dict) -> str:
        """新角色隔离约束：剧情卡未明确写明的关系一律视为不存在。"""
        known = self.main_characters()
        newcomers = [c for c in (plan.get("characters") or []) if c not in known]
        return (
            f"{NEW_CHARACTER_RULE_HEADER}\n"
            f"- 资料库已有角色档案：{'、'.join(known) or '（无）'}\n"
            f"- 本章剧情卡明确引入的新角色：{'、'.join(newcomers) or '（无）'}\n"
            "- 规则：凡角色档案之外的新角色（含正文临时出现者），"
            "除非本章剧情卡大纲中明确写明其与已有角色的关系，"
            "一律视为与已有角色素不相识——禁止虚构亲缘、旧识、师承、恩怨、"
            "组织从属等任何既有联系；新角色与已有角色的互动只能从本章"
            "首次接触开始，不得暗示过往渊源。"
        )

    # ---------- 定稿入库（人审通过后） ----------

    def commit(self, chapter: int) -> None:
        """人审通过：finalize（摘要→回写→approved→事件）+ 渐进式大纲追加。"""
        plan = self.chosen_plan(chapter)
        if plan is None:
            raise RuntimeError(f"第 {chapter} 章无已选剧情卡，无法定稿")
        rel = self.pipe.store.chapter_rel_path(VOLUME, chapter)
        if not self.pipe.store.exists(rel):
            raise RuntimeError(f"第 {chapter} 章草稿不存在，无法定稿")
        doc = self.pipe.store.read(rel)
        ctx = self._chapter_context(chapter, plan)
        finalize_chapter(
            self.pipe,
            chapter=chapter,
            volume=VOLUME,
            draft_text=doc.content,
            ctx=ctx,
            review=self.load_review(chapter),
            first_review_passed=doc.metadata.get("attempt", 1) == 1,
            total_chapters=chapter + HORIZON,
        )
        self._append_outline(chapter, plan)

    def _append_outline(self, chapter: int, plan: dict) -> None:
        """把本章计划追加进渐进式大纲（frontmatter chapters + 人类可读正文）。"""
        rel = INTERACTIVE_OUTLINE_REL
        if self.pipe.store.exists(rel):
            doc = self.pipe.store.read(rel)
            meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
            content = doc.content
        else:
            meta = {"title": "互动创作渐进式大纲"}
            content = "# 互动创作渐进式大纲\n"
        chapters = [c for c in (meta.get("chapters") or [])
                    if isinstance(c, dict) and c.get("chapter") != chapter]
        chapters.append({"chapter": chapter, **plan})
        chapters.sort(key=lambda c: c.get("chapter", 0))
        meta["chapters"] = chapters
        content += (
            f"\n### 第{chapter}章 {plan.get('title', '')}\n\n"
            f"出场角色：{'、'.join(plan.get('characters', []) or [])}\n\n"
            f"{plan.get('outline', '')}\n"
        )
        self.pipe.store.write(
            rel, content, meta,
            commit_message=f"ch-{chapter:03d} 渐进式大纲追加",
        )
