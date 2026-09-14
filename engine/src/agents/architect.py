"""Architect：世界观 / 角色档案 / 大纲 / 初始伏笔表生成，产物落盘 settings/。"""

from __future__ import annotations

import re

from src.agents.prompt_loader import render_prompt
from src.agents.schemas import (
    CharacterProfile,
    CharactersOutput,
    DemoOutput,
    OutlineOutput,
    WorldviewDoc,
    WorldviewOutput,
)
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.memory.md_store import MdStore, slugify
from src.memory.memory_manager import STYLE_REL
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "architect"

# 设定 Demo 确认后的落盘标记文件（存在即视为世界观/角色已人工确认）
DEMO_OVERVIEW_REL = "settings/story-overview.md"

_SECTION_RE = r"##\s*{title}\s*\n(.*?)(?=\n##\s|\Z)"


def _section_of(content: str, title: str) -> str:
    """从角色档案 MD 正文提取 `## <title>` 小节文本（回读已确认设定用）。"""
    m = re.search(_SECTION_RE.format(title=re.escape(title)), content, re.S)
    return m.group(1).strip() if m else ""


def maybe_generate_style(pipe, brief: str, outline: dict) -> None:
    """大纲人审通过后的文风指纹挂接点（graph / parallel_runner 共用）。

    style.md 不存在才生成（幂等由 generate_style 保证）；mock 管线无此方法
    或 LLM 异常时静默跳过，不阻塞章节生成主流程。
    """
    gen = getattr(pipe.architect, "generate_style", None)
    if not callable(gen):
        return
    try:
        gen(brief or "", outline or {})
    except Exception as exc:  # noqa: BLE001 - 文风指纹失败不阻塞主流程
        logger.warning("文风指纹生成失败（跳过，不阻塞主流程）：%s", exc)


class Architect:
    """三步生成：世界观 → 角色 → 大纲+伏笔，逐步落盘为 settings/ 下 MD。"""

    def __init__(self, registry: ModelRegistry, store: MdStore):
        self._registry = registry
        self._store = store

    def generate_settings(self, brief: str, total_chapters: int) -> OutlineOutput:
        """完整生成流程，返回大纲（供图状态使用）。

        若资料库中存在已确认的设定 Demo（story-overview.md 标记），
        则沿用其世界观/角色，不再推翻重来，只生成大纲+伏笔。
        """
        confirmed = self._load_confirmed_settings()
        if confirmed is not None:
            worldview, characters = confirmed
            logger.info("Architect: 检测到已确认的设定 Demo，沿用世界观/角色，仅生成大纲")
            return self._generate_outline(brief, worldview, characters, total_chapters)
        worldview = self._generate_worldview(brief)
        characters = self._generate_characters(brief, worldview)
        outline = self._generate_outline(brief, worldview, characters, total_chapters)
        return outline

    # ---------- 设定 Demo（创作向导先审后入库） ----------

    def generate_demo(
        self, brief: str, total_chapters: int, feedback: str = ""
    ) -> DemoOutput:
        """生成设定 Demo（梗概/概要/世界观/主题/主要人物），不落盘，供人工审核。"""
        logger.info("Architect: 生成设定 Demo ...")
        prompt = render_prompt(
            "architect_demo",
            brief=brief,
            total_chapters=total_chapters,
            feedback=feedback or "（无）",
        )
        return chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], DemoOutput
        )

    def save_demo(self, demo: DemoOutput) -> None:
        """人工确认后将 Demo 写入资料库 settings/（与三步生成落盘格式一致）。"""
        lines = [
            f"# {demo.book_title}\n",
            f"## 核心主题\n{demo.theme}\n",
            f"## 故事梗概\n{demo.synopsis}\n",
            f"## 整体概要\n{demo.overview}\n",
        ]
        self._store.write(
            DEMO_OVERVIEW_REL,
            "\n".join(lines),
            metadata={
                "title": demo.book_title,
                "theme": demo.theme,
                "demo_confirmed": True,
            },
            commit_message="设定 Demo 确认：故事梗概/整体概要/核心主题",
        )
        logger.info("  已落盘 %s", DEMO_OVERVIEW_REL)
        self._save_worldview_docs(demo.worldview)
        self._save_character_profiles(demo.characters)

    def _load_confirmed_settings(
        self,
    ) -> "tuple[WorldviewOutput, CharactersOutput] | None":
        """从资料库回读已确认的世界观/角色；无确认标记返回 None。"""
        if not self._store.exists(DEMO_OVERVIEW_REL):
            return None
        docs: list[WorldviewDoc] = []
        chars: list[CharacterProfile] = []
        for doc in self._store.iter_documents():
            if doc.doc_id.startswith("settings/worldview"):
                stem = doc.doc_id.rsplit("/", 1)[-1].removesuffix(".md")
                docs.append(WorldviewDoc(
                    filename=stem,
                    title=doc.metadata.get("title", stem),
                    content=doc.content,
                ))
            elif doc.doc_id.startswith("settings/characters"):
                meta = doc.metadata
                chars.append(CharacterProfile(
                    name=meta.get("title", ""),
                    role=meta.get("role", ""),
                    appearance=_section_of(doc.content, "外貌"),
                    personality=_section_of(doc.content, "性格"),
                    background=_section_of(doc.content, "背景"),
                    level=meta.get("level") or "",
                    location=meta.get("location") or "",
                    items=meta.get("items") or [],
                    relations=meta.get("relations") or {},
                ))
        if not docs or not chars:
            return None
        return WorldviewOutput(docs=docs), CharactersOutput(characters=chars)

    # ---------- 世界观 ----------

    def _generate_worldview(self, brief: str) -> WorldviewOutput:
        logger.info("Architect: 生成世界观 ...")
        prompt = render_prompt("architect_worldview", brief=brief)
        out = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], WorldviewOutput
        )
        self._save_worldview_docs(out.docs)
        return out

    def _save_worldview_docs(self, docs: list[WorldviewDoc]) -> None:
        for doc in docs:
            rel = f"settings/worldview/{slugify(doc.filename)}.md"
            self._store.write(
                rel,
                doc.content,
                metadata={"title": doc.title},
                commit_message=f"Architect 世界观: {doc.title}",
            )
            logger.info("  已落盘 %s", rel)

    # ---------- 角色 ----------

    def _generate_characters(
        self, brief: str, worldview: WorldviewOutput
    ) -> CharactersOutput:
        logger.info("Architect: 生成角色档案 ...")
        prompt = render_prompt(
            "architect_characters",
            brief=brief,
            worldview_digest=self._worldview_digest(worldview),
        )
        out = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], CharactersOutput
        )
        self._save_character_profiles(out.characters)
        return out

    def _save_character_profiles(self, characters: list[CharacterProfile]) -> None:
        for c in characters:
            rel = f"settings/characters/{slugify(c.name)}.md"
            content = (
                f"# {c.name}\n\n"
                f"## 定位\n{c.role}\n\n"
                f"## 外貌\n{c.appearance}\n\n"
                f"## 性格\n{c.personality}\n\n"
                f"## 背景\n{c.background}\n"
            )
            self._store.write(
                rel,
                content,
                metadata={
                    "title": c.name,
                    "role": c.role,
                    "level": c.level,
                    "location": c.location,
                    "items": c.items,
                    "relations": c.relations,
                    "characters": [c.name],
                },
                commit_message=f"Architect 角色建档: {c.name}",
            )
            logger.info("  已落盘 %s", rel)

    # ---------- 大纲 + 伏笔 ----------

    def _generate_outline(
        self,
        brief: str,
        worldview: WorldviewOutput,
        characters: CharactersOutput,
        total_chapters: int,
    ) -> OutlineOutput:
        logger.info("Architect: 生成大纲与伏笔表 ...")
        char_digest = "\n".join(
            f"- {c.name}（{c.role}）：{c.personality[:60]}"
            for c in characters.characters
        )
        prompt = render_prompt(
            "architect_outline",
            brief=brief,
            worldview_digest=self._worldview_digest(worldview),
            character_digest=char_digest,
            total_chapters=total_chapters,
        )
        out = chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], OutlineOutput
        )
        out = self._ensure_chapter_count(out, prompt, total_chapters)
        self.save_outline(out)
        return out

    @staticmethod
    def _chapter_count(out: OutlineOutput) -> int:
        return sum(len(v.chapters) for v in out.volumes)

    def _ensure_chapter_count(
        self, out: OutlineOutput, base_prompt: str, total_chapters: int
    ) -> OutlineOutput:
        """校验大纲章节总数是否等于 total_chapters；不符则携差异重生成一次，仍不符则报错。

        LLM 有时无视章节数要求（如只产出 5 章），会导致后续 assemble_context 因缺章崩溃。
        """
        got = self._chapter_count(out)
        if got == total_chapters:
            return out
        logger.warning(
            "Architect 大纲章节数=%d，与要求 %d 不符，携差异重新生成一次", got, total_chapters
        )
        repair = (
            f"{base_prompt}\n\n## 修正要求（上一次生成不合格）\n"
            f"上次只产出了 {got} 章，但全书必须恰好 {total_chapters} 章。"
            f"请重新输出完整大纲：章号从 1 连续编号到 {total_chapters}，"
            f"各卷章节数之和严格等于 {total_chapters}，不得跳号或省略。"
        )
        out = chat_structured(
            self._registry, ROLE, [ChatMessage("user", repair)], OutlineOutput
        )
        got = self._chapter_count(out)
        if got != total_chapters:
            raise RuntimeError(
                f"Architect 两次生成均未满足章节数要求：得到 {got} 章，要求 {total_chapters} 章"
            )
        logger.info("Architect 大纲重生成成功：%d 章", got)
        return out

    def save_outline(self, out: OutlineOutput) -> None:
        """大纲与伏笔表落盘（人审打回重新生成后也走此方法）。"""
        lines = [f"# {out.book_title}\n", f"> 主题：{out.theme}\n"]
        for vol in out.volumes:
            dep = f"（依赖卷 {vol.depends_on}）" if vol.depends_on else ""
            lines.append(f"\n## 第{vol.volume}卷 {vol.title}{dep}\n")
            for ch in vol.chapters:
                lines.append(
                    f"### 第{ch.chapter}章 {ch.title}\n\n"
                    f"出场角色：{'、'.join(ch.characters)}\n\n{ch.outline}\n"
                )
        self._store.write(
            "settings/outline.md",
            "\n".join(lines),
            metadata={
                "title": out.book_title,
                "theme": out.theme,
                "volumes": [v.model_dump() for v in out.volumes],
            },
            commit_message="Architect 大纲",
        )
        self._store.write(
            "settings/foreshadowing.md",
            "# 伏笔表\n\n结构化条目见 frontmatter items。",
            metadata={
                "items": [
                    {**f.model_dump(), "status": "open"} for f in out.foreshadowing
                ]
            },
            commit_message="Architect 初始伏笔表",
        )
        logger.info("  已落盘 settings/outline.md 与 settings/foreshadowing.md")

    @staticmethod
    def _worldview_digest(worldview: WorldviewOutput, per_doc: int = 500) -> str:
        return "\n\n".join(
            f"### {d.title}\n{d.content[:per_doc]}" for d in worldview.docs
        )

    # ---------- 文风指纹（v2.0 P4-D） ----------

    def generate_style(self, brief: str, outline: dict) -> bool:
        """生成文风指纹 settings/style.md；已存在则跳过（幂等，人工可编辑）。

        返回是否实际生成。大纲人审通过后挂接（graph / parallel_runner）。
        """
        if self._store.exists(STYLE_REL):
            logger.info("Architect: 检测到既有文风指纹 style.md，跳过生成")
            return False
        logger.info("Architect: 生成文风指纹 ...")
        outline_digest = "\n".join(
            [f"《{outline.get('book_title', '')}》主题：{outline.get('theme', '')}"]
            + [
                f"- 第{v.get('volume')}卷 {v.get('title', '')}（{len(v.get('chapters', []))} 章）"
                for v in outline.get("volumes", [])
            ]
        )
        prompt = render_prompt(
            "architect_style",
            brief=brief or "（无）",
            outline_digest=outline_digest,
            worldview_digest=self._worldview_digest_from_store(),
        )
        result = self._registry.chat_as(ROLE, [ChatMessage("user", prompt)])
        self._store.write(
            STYLE_REL,
            result.content.strip(),
            metadata={"title": "文风指纹"},
            commit_message="Architect 文风指纹",
        )
        logger.info("  已落盘 %s", STYLE_REL)
        return True

    def _worldview_digest_from_store(self, per_doc: int = 500) -> str:
        """从事实源读世界观摘要（挂接点在大纲人审后，内存中已无 WorldviewOutput）。"""
        parts: list[str] = []
        for doc in self._store.iter_documents():
            if not doc.doc_id.startswith("settings/worldview"):
                continue
            title = doc.metadata.get("title", doc.doc_id)
            parts.append(f"### {title}\n{doc.content.strip()[:per_doc]}")
        return "\n\n".join(parts) if parts else "（无）"
