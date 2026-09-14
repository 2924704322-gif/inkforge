"""翻译 Skill（C 阶段扩展）：将定稿章节译为目标语言并落盘为 MD。

作为 Skill 扩展框架（T3.3）中「消费 ModelRegistry」的示范：
- 通过 SkillContext.registry.chat_as(role) 调用模型翻译，复用降级容错（C1）。
- 逐章翻译，保留分段结构；结果写入 translations/<lang>/ch-NNN.md。
- 默认关闭（enabled=False），因翻译会产生 LLM 调用成本。
"""

from __future__ import annotations

from typing import Any, ClassVar, Optional

from src.llm.base import ChatMessage
from src.memory.md_store import slugify
from src.skills.base import Skill, SkillContext, SkillSettings
from src.skills.memory_bus import EVENT_CHAPTER_COMMITTED
from src.skills.registry import register
from src.utils.logger import get_logger

logger = get_logger(__name__)


class TranslateSettings(SkillSettings):
    enabled: bool = False                # 翻译有 LLM 成本，基线默认关闭
    target_lang: str = "English"         # 目标语言（自然语言名，直接进提示词）
    role: str = "editor"                 # 使用的模型角色（editor 温度低，忠实度高）
    output_subdir: str = "translations"  # 输出子目录（其下再按语言分目录）
    include_unapproved: bool = False
    auto_on_commit: bool = False         # 订阅记忆总线：章节定稿后自动翻译该章


@register
class TranslateSkill(Skill):
    name: ClassVar[str] = "translate"
    SettingsModel: ClassVar[type[SkillSettings]] = TranslateSettings

    def _collect(self, chapter: Optional[int], chapters: Optional[list[int]],
                 include_unapproved: bool) -> list[dict]:
        want: Optional[set[int]] = None
        if chapter is not None:
            want = {int(chapter)}
        elif chapters:
            want = {int(c) for c in chapters}

        picked: list[dict] = []
        for doc in self.context.store.list_chapters():
            meta = doc.metadata
            num = int(meta.get("chapter", 0) or 0)
            if want is not None and num not in want:
                continue
            if want is None and not include_unapproved and str(meta.get("status")) != "approved":
                continue
            picked.append(
                {
                    "chapter": num,
                    "title": str(meta.get("title", "")).strip(),
                    "content": doc.content or "",
                }
            )
        picked.sort(key=lambda c: c["chapter"])
        return picked

    def _translate_text(self, text: str, target_lang: str) -> str:
        messages = [
            ChatMessage(
                role="system",
                content=(
                    f"你是专业文学译者。请把用户提供的小说正文忠实翻译为{target_lang}，"
                    "保留分段与语气，不要增删情节，不要输出除译文外的任何说明。"
                ),
            ),
            ChatMessage(role="user", content=text),
        ]
        result = self.context.registry.chat_as(self.settings.role, messages)
        return result.content.strip()

    def run(
        self,
        chapter: Optional[int] = None,
        chapters: Optional[list[int]] = None,
        target_lang: Optional[str] = None,
        include_unapproved: Optional[bool] = None,
        **kwargs: Any,
    ) -> dict:
        lang = target_lang or self.settings.target_lang
        inc = self.settings.include_unapproved if include_unapproved is None else include_unapproved
        picked = self._collect(chapter, chapters, inc)
        if not picked:
            raise ValueError("没有匹配到可翻译的章节")

        lang_dir = f"{self.settings.output_subdir}/{slugify(lang)}"
        done: list[int] = []
        for ch in picked:
            translated = self._translate_text(ch["content"], lang)
            rel = f"{lang_dir}/ch-{ch['chapter']:03d}.md"
            self.context.store.write(
                rel,
                translated,
                metadata={"chapter": ch["chapter"], "lang": lang, "source_title": ch["title"]},
                commit_message=f"翻译 ch-{ch['chapter']:03d} → {lang}",
            )
            done.append(ch["chapter"])
            logger.info("翻译完成 ch-%03d → %s", ch["chapter"], lang)

        return {
            "target_lang": lang,
            "output_dir": lang_dir,
            "translated_chapters": done,
            "count": len(done),
        }

    # ---------- 记忆总线钩子（可选自动翻译） ----------

    def subscribed_events(self) -> list[str]:
        """仅当 auto_on_commit 开启时才订阅，否则零开销。"""
        return [EVENT_CHAPTER_COMMITTED] if self.settings.auto_on_commit else []

    def handle_event(self, event: str, payload: dict) -> None:
        """章节定稿后自动翻译该章（异常由记忆总线隔离，不影响主流程）。"""
        if event != EVENT_CHAPTER_COMMITTED:
            return
        ch = payload.get("chapter")
        if ch is None:
            return
        logger.info("记忆总线触发自动翻译：ch-%03d → %s", ch, self.settings.target_lang)
        self.run(chapter=ch)
