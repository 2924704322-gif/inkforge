"""LangGraph 状态图：大纲阶段 → 大纲人审 → 章节循环（生成/审查/分级打回/逐章人审/回写）。

- 人审暂停点用 interrupt() 实现，CLI 以 Command(resume=...) 恢复。
- 全流程状态由 SqliteSaver 持久化，支持断点续写（D4 / R7）。
- 局部/整章重写各计上限 2 次，超限强制携带标注送人审（D6）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from src.agents.architect import Architect, maybe_generate_style
from src.agents.editor import Editor, negotiate_revision, verdict_of
from src.agents.summarizer import Summarizer
from src.agents.writer import (
    Writer,
    chapter_length,
    human_revision_notes,
    length_deviation,
    length_revision_note,
)
from src.config.app_config import GenerationConfig
from src.llm.registry import ModelRegistry
from src.memory.md_store import MdStore
from src.memory.memory_manager import (
    ChapterContext,
    MemoryManager,
    read_custom_constraints,
)
from src.orchestrator.finalize import finalize_chapter
from src.orchestrator.scheduler import generation_config
from src.orchestrator.state import NovelState, plan_for_chapter
from src.skills.memory_bus import EVENT_OUTLINE_APPROVED, MemoryBus
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.skills.registry import SkillRegistry

logger = get_logger(__name__)

# 默认重试上限（D6）；实际生效值经 generation_config 从分层配置读取（P4-A 接线）
MAX_PARTIAL_RETRIES = 2
MAX_FULL_RETRIES = 2


def _load_outline_md(store: MdStore) -> dict | None:
    """从 settings/outline.md 的 frontmatter 恢复大纲（MD 唯一事实源）；无有效大纲返回 None。

    与 parallel_runner.load_outline 语义一致：使人工在资料库页编辑保存的大纲
    成为串行图后续正文生成的依据。
    """
    rel = "settings/outline.md"
    if not store.exists(rel):
        return None
    meta = store.read(rel).metadata
    volumes = meta.get("volumes") or []
    if not volumes:
        return None
    return {
        "book_title": meta.get("title", ""),
        "theme": meta.get("theme", ""),
        "volumes": volumes,
    }


@dataclass
class Pipeline:
    """节点共享的运行时依赖。"""

    registry: ModelRegistry
    store: MdStore
    memory: MemoryManager
    architect: Architect
    writer: Writer
    editor: Editor
    summarizer: Summarizer
    # Skill 扩展（M3 / T3.3）：记忆总线 + 已启用 Skill 容器（可缺省，核心流程零依赖）
    bus: MemoryBus | None = None
    skills: SkillRegistry | None = None
    # 生成流程参数（P4-A）：缺省时 generation_config 回退全局分层配置
    gen_config: GenerationConfig | None = None


def build_graph(pipe: Pipeline, checkpoint_db: Path):
    """构建并编译核心生成流程状态图。"""

    # ---------- 节点 ----------

    def architect_node(state: NovelState) -> dict:
        feedback = state.get("outline_feedback", "")
        # 无打回意见时优先复用既有 outline.md（含人工在资料库页编辑保存的版本），
        # 与并行模式 ensure_outline 语义一致，避免覆盖用户改稿。
        if not feedback:
            existing = _load_outline_md(pipe.store)
            if existing is not None:
                logger.info("检测到既有大纲《%s》，复用（跳过 Architect 重新生成）",
                            existing.get("book_title", ""))
                pipe.memory.rebuild_index()
                return {"outline": existing, "outline_feedback": ""}
        brief = state["brief"]
        if feedback:
            brief = f"{brief}\n\n【人工审阅打回意见，必须落实】\n{feedback}"
        # 项目级约束（settings/custom-skills.md）与 brief 同处送达设定/大纲渲染（W5）
        outline = pipe.architect.generate_settings(
            brief, state["total_chapters"],
            custom_constraints=read_custom_constraints(pipe.store),
        )
        # 设定落盘后全量建立索引
        pipe.memory.rebuild_index()
        return {"outline": outline.model_dump(), "outline_feedback": ""}

    def outline_review_node(state: NovelState) -> dict:
        decision = interrupt(
            {
                "type": "outline_review",
                "outline": state["outline"],
            }
        )
        if decision.get("action") == "approve":
            logger.info("大纲人审通过，进入章节循环")
            maybe_generate_style(pipe, state.get("brief", ""), state["outline"],
                                 read_custom_constraints(pipe.store))
            if pipe.bus is not None:
                pipe.bus.publish(EVENT_OUTLINE_APPROVED, {
                    "novel_id": state.get("novel_id", ""),
                    "book_title": state["outline"].get("book_title", ""),
                })
            return {
                "outline_feedback": "",
                "current_chapter": 1,
            }
        logger.info("大纲被人工打回：%s", decision.get("feedback", ""))
        return {"outline_feedback": decision.get("feedback", "请改进大纲")}

    def assemble_context_node(state: NovelState) -> dict:
        chapter = state["current_chapter"]
        # MD 唯一事实源：优先采用最新 outline.md（人工可能在资料库页编辑过大纲），
        # 使后续正文以修改后的大纲生成；缺失则沿用图状态中的大纲。
        outline_update: dict = {}
        latest = _load_outline_md(pipe.store)
        if latest is not None:
            merged = {**state.get("outline", {}), **latest}
            state = {**state, "outline": merged}
            outline_update = {"outline": merged}
        plan = plan_for_chapter(state, chapter)
        if plan is None:
            raise RuntimeError(f"大纲中不存在第 {chapter} 章的计划")
        # 人工可能直接编辑过设定/摘要 MD：检测并重嵌入
        pipe.memory.sync_changed()
        total = state["total_chapters"]
        ctx = pipe.memory.retrieve_context(
            chapter=chapter,
            chapter_outline=f"《{plan['title']}》{plan['outline']}",
            characters=plan.get("characters", []),
            total_chapters=total,
            is_finale=chapter >= total,
        )
        logger.info(
            "第 %d 章上下文组装完成（近期摘要%d/远期%d/角色%d/伏笔%d/世界观%d）",
            chapter,
            len(ctx.recent_summaries),
            len(ctx.related_summaries),
            len(ctx.character_states),
            len(ctx.unresolved_foreshadowing),
            len(ctx.worldview_rules),
        )
        return {
            "chapter_ctx": asdict(ctx),
            "current_volume": plan["volume"],
            "attempt": 0,
            "partial_retries": 0,
            "full_retries": 0,
            "length_retries": 0,
            "retry_exceeded": False,
            "revision_notes": None,
            "first_review_passed": None,
            "draft_text": "",
            **outline_update,
        }

    def writer_node(state: NovelState) -> dict:
        ctx = ChapterContext(**state["chapter_ctx"])
        revision_notes = state.get("revision_notes")
        result = pipe.writer.write_chapter(
            ctx,
            revision_notes=revision_notes,
            previous_text=state.get("draft_text") or None,
        )
        attempt = state.get("attempt", 0) + 1
        # 每稿即落盘（人工可随时查看），状态 draft
        plan = plan_for_chapter(state, ctx.chapter) or {}
        rel = pipe.store.chapter_rel_path(state["current_volume"], ctx.chapter)
        pipe.store.write(
            rel,
            result.content,
            metadata={
                "chapter": ctx.chapter,
                "volume": state["current_volume"],
                "title": plan.get("title", ""),
                "characters": plan.get("characters", []),
                "status": "draft",
                "attempt": attempt,
                "model": f"{result.provider_name}/{result.model}",
                "used_fallback": result.used_fallback,
            },
            commit_message=f"ch-{ctx.chapter:03d} 第 {attempt} 稿",
        )
        return {
            "draft_text": result.content,
            "attempt": attempt,
            "revision_notes": None,
            "model": f"{result.provider_name}/{result.model}",
            "used_fallback": result.used_fallback,
        }

    def editor_node(state: NovelState) -> dict:
        ctx = ChapterContext(**state["chapter_ctx"])
        gen = generation_config(pipe)
        target = getattr(pipe.writer, "target_words", None)
        review = pipe.editor.review_chapter(
            ctx, state["draft_text"], state["attempt"], target_words=target,
        )
        verdict = verdict_of(review.overall)
        updates: dict = {"review": review.model_dump(), "verdict": verdict}

        # 字数门禁：客观偏差超容差 → 强制修正（客观约束，不走协商）
        length_note = ""
        if target is not None and gen.length_gate_enabled:
            actual = chapter_length(state["draft_text"])
            if length_deviation(actual, target) > gen.word_count_tolerance:
                length_note = length_revision_note(target, actual)

        if verdict == "partial_rewrite":
            if state.get("partial_retries", 0) < gen.max_partial_retries:
                notes = (
                    negotiate_revision(pipe, ctx, review, state["draft_text"])
                    if gen.negotiation_enabled else None
                )
                if notes == "":
                    # 协商后全部问题被豁免：不设 revision_notes，路由将直接送人审
                    logger.info(
                        "第 %d 章协商后全部问题豁免，直接送人审", state["current_chapter"]
                    )
                else:
                    updates["partial_retries"] = state.get("partial_retries", 0) + 1
                    base = notes or Editor.issues_digest(review)
                    updates["revision_notes"] = (
                        "【局部重写】" + base
                        + (("\n\n" + length_note) if length_note else "")
                    )
            else:
                updates["retry_exceeded"] = True
        elif verdict == "full_rewrite":
            if state.get("full_retries", 0) < gen.max_full_retries:
                updates["full_retries"] = state.get("full_retries", 0) + 1
                updates["revision_notes"] = (
                    "【整章重写】上一稿总分过低，请抛弃原稿结构重新创作。问题清单：\n"
                    + Editor.issues_digest(review)
                    + (("\n\n" + length_note) if length_note else "")
                )
                updates["draft_text"] = ""  # 整章重写不携带上一稿
            else:
                updates["retry_exceeded"] = True
        else:  # pass
            # 字数门禁：质量 pass 但字数超差 → 强制打回修正
            if length_note and state.get("length_retries", 0) < gen.max_length_retries:
                logger.info(
                    "第 %d 章字数门禁触发：偏差 %+d 字 → 强制 partial_rewrite",
                    state["current_chapter"],
                    chapter_length(state["draft_text"]) - target,
                )
                updates["length_retries"] = state.get("length_retries", 0) + 1
                updates["verdict"] = "partial_rewrite"
                updates["revision_notes"] = length_note
        return updates

    def route_after_editor(state: NovelState) -> str:
        if state["verdict"] == "pass" or state.get("retry_exceeded"):
            if state.get("retry_exceeded"):
                logger.warning(
                    "第 %d 章自动重试超限，携带问题标注强制送人审",
                    state["current_chapter"],
                )
            return "human_review"
        if not state.get("revision_notes"):
            # 协商豁免全部问题：无修改指令即送人审（P4-A）
            return "human_review"
        return "writer"

    def human_review_node(state: NovelState) -> dict:
        first_time = state.get("first_review_passed") is None
        decision = interrupt(
            {
                "type": "chapter_review",
                "chapter": state["current_chapter"],
                "volume": state["current_volume"],
                "draft_text": state["draft_text"],
                "review": state.get("review", {}),
                "retry_exceeded": state.get("retry_exceeded", False),
                "attempt": state.get("attempt", 1),
                "model": state.get("model"),
                "used_fallback": state.get("used_fallback", False),
            }
        )
        if decision.get("action") == "approve":
            logger.info("第 %d 章人审通过", state["current_chapter"])
            return {
                "first_review_passed": True if first_time else state["first_review_passed"],
            }
        feedback = decision.get("feedback", "")
        mode = decision.get("revision_mode", "targeted")
        logger.info("第 %d 章被人工打回（%s）：%s", state["current_chapter"], mode, feedback)
        return {
            "first_review_passed": False if first_time else state["first_review_passed"],
            "revision_notes": human_revision_notes(feedback, mode),
            # 人工打回重置自动重试计数，给 Writer 完整的修改窗口
            "partial_retries": 0,
            "full_retries": 0,
            "retry_exceeded": False,
        }

    def route_after_human(state: NovelState) -> str:
        return "writer" if state.get("revision_notes") else "writeback"

    def writeback_node(state: NovelState) -> dict:
        chapter = state["current_chapter"]
        volume = state["current_volume"]
        ctx = ChapterContext(**state["chapter_ctx"])
        finalize_chapter(
            pipe,
            chapter=chapter,
            volume=volume,
            draft_text=state["draft_text"],
            ctx=ctx,
            review=state.get("review"),
            first_review_passed=state.get("first_review_passed"),
            total_chapters=state["total_chapters"],
        )
        if chapter >= state["total_chapters"]:
            return {"done": True}
        return {"current_chapter": chapter + 1, "done": False}

    def chapter_gate_node(state: NovelState) -> dict:
        """逐章确认关卡：上一章定稿后**停下来**，由用户在对话框发指令才写下一章。

        与 human_review 用同一套 interrupt/resume 机制（前端通过 /api/decision
        或对话框里的「继续」指令放行）。这是刻意的产品行为：一章一章来，
        不让流水线自己往下跑。
        """
        decision = interrupt(
            {
                "type": "chapter_gate",
                "approved_chapter": state["current_chapter"] - 1,
                "next_chapter": state["current_chapter"],
            }
        )
        if decision.get("action") == "approve":
            logger.info("用户指示继续：开始生成第 %d 章", state["current_chapter"])
            return {}
        logger.info("用户选择暂不继续：第 %d 章待写", state["current_chapter"])
        return {"done": True}

    def route_after_writeback(state: NovelState) -> str:
        return END if state.get("done") else "chapter_gate"

    def route_after_gate(state: NovelState) -> str:
        return END if state.get("done") else "assemble_context"

    def route_after_outline(state: NovelState) -> str:
        return "architect" if state.get("outline_feedback") else "assemble_context"

    # ---------- 图装配 ----------

    graph = StateGraph(NovelState)
    graph.add_node("architect", architect_node)
    graph.add_node("outline_review", outline_review_node)
    graph.add_node("assemble_context", assemble_context_node)
    graph.add_node("writer", writer_node)
    graph.add_node("editor", editor_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("writeback", writeback_node)
    graph.add_node("chapter_gate", chapter_gate_node)

    graph.add_edge(START, "architect")
    graph.add_edge("architect", "outline_review")
    graph.add_conditional_edges(
        "outline_review", route_after_outline, ["architect", "assemble_context"]
    )
    graph.add_edge("assemble_context", "writer")
    graph.add_edge("writer", "editor")
    graph.add_conditional_edges("editor", route_after_editor, ["writer", "human_review"])
    graph.add_conditional_edges(
        "human_review", route_after_human, ["writer", "writeback"]
    )
    graph.add_conditional_edges(
        "writeback", route_after_writeback, ["chapter_gate", END]
    )
    graph.add_conditional_edges(
        "chapter_gate", route_after_gate, ["assemble_context", END]
    )

    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))
