"""蒸馏 LangGraph 状态图：chunk → extract → merge → (loop|finalize)。

复用 langgraph-checkpoint-sqlite 做断点续跑，状态图独立于核心生成流程。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from src.distillation.chunker import Chunk
from src.distillation.merger import build_cumulative_summary, merge_increment
from src.distillation.prompts import render_prompt
from src.distillation.schemas import ChunkExtraction, FullReport
from src.llm.base import ChatMessage
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.llm.registry import ModelRegistry

logger = get_logger(__name__)

# 蒸馏使用的 LLM 角色名（在 models.yaml 中绑定）
DISTILLER_ROLE = "distiller"


class DistillState(TypedDict, total=False):
    """蒸馏流程的 LangGraph 状态（JSON 可序列化，SqliteSaver 持久化）。"""

    skill_id: str
    book_title: str
    total_chunks: int
    current_chunk_index: int       # 从 1 开始
    chunks_json: str               # Chunk 列表的 JSON 序列化（TypedDict 不支持非基础类型）
    cumulative_json: str           # FullReport 的 JSON 序列化
    last_extraction_json: str      # 最近一次 ChunkExtraction 的 JSON
    done: bool
    provider: str
    model: str


@dataclass
class DistillPipeline:
    """蒸馏节点的共享运行时依赖（轻量，不需要 memory/md_store）。"""

    registry: ModelRegistry


def _dump_chunks(chunks: list[Chunk]) -> str:
    """序列化 Chunk 列表为 JSON。"""
    return json.dumps(
        [{"index": c.index, "text": c.text, "chapter_range": c.chapter_range} for c in chunks],
        ensure_ascii=False,
    )


def _load_chunks(json_str: str) -> list[dict]:
    """从 JSON 反序列化 Chunk 列表。"""
    return json.loads(json_str)


def build_distill_graph(pipe: DistillPipeline, checkpoint_db: Path):
    """构建蒸馏状态图。"""

    # ── 节点 ──

    def chunk_node(state: DistillState) -> dict:
        """准备当前块：无外部输入，仅推进状态。"""
        idx = state.get("current_chunk_index", 1)
        logger.info("蒸馏进度：第 %d / %d 块", idx, state["total_chunks"])
        return {}

    def extract_node(state: DistillState) -> dict:
        """对当前块调用 LLM 提取增量。"""
        chunks = _load_chunks(state["chunks_json"])
        idx = state.get("current_chunk_index", 1)
        chunk = chunks[idx - 1]

        # 构建累积摘要
        cumulative = FullReport.model_validate_json(state.get("cumulative_json", "{}"))
        summary = build_cumulative_summary(cumulative)

        # 渲染提示词
        prompt = render_prompt(
            "distill_extract",
            book_title=state.get("book_title", ""),
            current_chunk_index=str(idx),
            total_chunks=str(state["total_chunks"]),
            cumulative_summary=summary or "（尚无累积知识，这是第一块）",
            new_text_content=chunk["text"],
        )

        messages = [
            ChatMessage(role="system", content="你是一个极致的文学结构化分析引擎。只输出合法 JSON。"),
            ChatMessage(role="user", content=prompt),
        ]

        result = pipe.registry.chat_as(
            DISTILLER_ROLE,
            messages,
            json_mode=True,
            temperature=0.3,
        )

        # 解析 JSON
        raw = result.content.strip()
        if raw.startswith("```"):
            # 去除可能的 markdown 代码块标记
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        try:
            extraction = ChunkExtraction.model_validate_json(raw)
        except Exception as e:
            logger.error("LLM 返回的 JSON 解析失败（块 %d）: %s", idx, e)
            # 尝试修复：截取第一个 { 到最后一个 }
            try:
                start = raw.index("{")
                end = raw.rindex("}") + 1
                extraction = ChunkExtraction.model_validate_json(raw[start:end])
            except Exception:
                # 返回空增量，不阻断流程
                logger.warning("JSON 修复失败，返回空增量")
                extraction = ChunkExtraction(chunk_index=idx, chapter_range=chunk.get("chapter_range", ""))

        logger.info(
            "块 %d 提取完成：provider=%s model=%s",
            idx, result.provider_name, result.model,
        )

        return {
            "last_extraction_json": extraction.model_dump_json(),
            "provider": f"{result.provider_name}/{result.model}",
            "model": result.model,
        }

    def merge_node(state: DistillState) -> dict:
        """将本块增量合并入累积报告，并推进到下一块。"""
        cumulative = FullReport.model_validate_json(state.get("cumulative_json", "{}"))
        extraction = ChunkExtraction.model_validate_json(state["last_extraction_json"])

        # 首次合并设置元信息
        if not cumulative.book_title:
            cumulative.book_title = state.get("book_title", "")
            cumulative.total_chunks = state.get("total_chunks", 0)

        cumulative = merge_increment(cumulative, extraction)
        idx = state.get("current_chunk_index", 1)
        logger.info("块 %d/%d 合并完成", idx, state.get("total_chunks", 0))

        return {
            "cumulative_json": cumulative.model_dump_json(),
            "current_chunk_index": idx + 1,  # 推进到下一块
        }

    def finalize_node(state: DistillState) -> dict:
        """生成最终报告，标记完成。"""
        cumulative = FullReport.model_validate_json(state["cumulative_json"])
        cumulative.provider = state.get("provider", "")
        cumulative.model = state.get("model", "")
        logger.info("蒸馏完成：《%s》%d 块 %d 维分析已纳入报告",
                     cumulative.book_title, state["total_chunks"], 16)
        return {
            "cumulative_json": cumulative.model_dump_json(),
            "done": True,
        }

    # ── 路由 ──

    def route_after_merge(state: DistillState) -> str:
        idx = state.get("current_chunk_index", 1)
        if idx >= state.get("total_chunks", 0):
            return "finalize"
        return "chunk"

    def route_after_chunk(_state: DistillState) -> str:
        return "extract"

    # ── 图装配 ──

    graph = StateGraph(DistillState)
    graph.add_node("chunk", chunk_node)
    graph.add_node("extract", extract_node)
    graph.add_node("merge", merge_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "chunk")
    graph.add_conditional_edges("chunk", route_after_chunk, ["extract"])
    graph.add_edge("extract", "merge")
    graph.add_conditional_edges("merge", route_after_merge, {
        "chunk": "chunk",
        "finalize": "finalize",
    })
    graph.add_edge("finalize", END)

    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(checkpoint_db), check_same_thread=False)
    return graph.compile(checkpointer=SqliteSaver(conn))


def run_distill(
    pipe: DistillPipeline,
    skill_id: str,
    book_title: str,
    chunks: list[Chunk],
    checkpoint_db: Path,
) -> FullReport:
    """同步执行完整蒸馏流程（CLI / 后台线程调用）。

    返回最终的 FullReport。
    """
    graph = build_distill_graph(pipe, checkpoint_db)

    state: DistillState = {
        "skill_id": skill_id,
        "book_title": book_title,
        "total_chunks": len(chunks),
        "current_chunk_index": 1,
        "chunks_json": _dump_chunks(chunks),
        "cumulative_json": "{}",
        "last_extraction_json": "{}",
        "done": False,
        "provider": "",
        "model": "",
    }

    config = {"configurable": {"thread_id": skill_id}, "recursion_limit": 500}

    result = graph.invoke(state, config)
    return FullReport.model_validate_json(result["cumulative_json"])


def resume_distill(
    pipe: DistillPipeline,
    skill_id: str,
    checkpoint_db: Path,
) -> FullReport:
    """从 checkpoint 断点续跑蒸馏。"""
    graph = build_distill_graph(pipe, checkpoint_db)
    config = {"configurable": {"thread_id": skill_id}, "recursion_limit": 500}

    # 从断点恢复：传入 None 让 LangGraph 从 checkpoint 回放
    result = graph.invoke(None, config)

    # 检查是否已跑完
    if result.get("done"):
        logger.info("蒸馏 %s 已完成，直接返回缓存报告", skill_id)
    else:
        logger.info("蒸馏 %s 从断点恢复，完成剩余块", skill_id)

    return FullReport.model_validate_json(result["cumulative_json"])
