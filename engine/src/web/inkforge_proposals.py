"""Inkforge 提案-审阅流（照抄 DeepWrite 的 proposal-first 逻辑）。

对话智能体的改稿动作不直接写盘：产出「变更提案」（原文快照 + 提案全文 + 行级
hunk diff + 增删统计），由用户接受或拒绝；接受时校验基础版本（文档若已被人工
改动则返回 conflict，不覆盖最新内容）。提案状态机与 DeepWrite 对齐：
pending → accepting → accepted / rejected / conflict / error。

审批模式：request-approval（每次审批）与 auto-approve（替我审批——接受动作
自动触发，但仍走冲突校验）。
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

_NOVEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DIFF_CONTEXT = 3
_MAX_DIFF_LINES = 400

# ---------- 请求体模型（模块级） ----------


class ProposeBody(BaseModel):
    instruction: str
    target: dict  # {kind: 'chapter'|'settings', key: 'ch-3'|'settings/xxx.md'}
    agent: str = "chat"


class DecideBody(BaseModel):
    decision: str  # accept / reject


# ---------- 行级 diff（DeepWrite 的 hunk 结构） ----------


def _build_hunks(original: str, proposed: str) -> tuple[list[dict], int, int, bool]:
    old_lines = original.splitlines()
    new_lines = proposed.splitlines()
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    ops = [op for op in sm.get_opcodes() if op[0] != "equal"]

    additions = sum(new2 - new1 for tag, _i1, _i2, new1, new2 in ops)
    deletions = sum(old2 - old1 for tag, old1, old2, _n1, _n2 in ops)

    hunks: list[dict] = []
    truncated = False
    total_lines = 0
    for tag, i1, i2, j1, j2 in ops:
        ctx_start = max(0, i1 - _DIFF_CONTEXT)
        ctx_end = min(len(old_lines), i2 + _DIFF_CONTEXT)
        lines: list[dict] = []
        for idx in range(ctx_start, i1):
            lines.append({"type": "context", "old": idx + 1, "new": "", "text": old_lines[idx]})
        for idx in range(i1, i2):
            lines.append({"type": "deletion", "old": idx + 1, "new": "", "text": old_lines[idx]})
        for idx in range(j1, j2):
            lines.append({"type": "addition", "old": "", "new": idx + 1, "text": new_lines[idx]})
        for idx in range(i2, ctx_end):
            lines.append({"type": "context", "old": idx + 1, "new": "", "text": old_lines[idx]})
        hunks.append(
            {
                "oldStart": ctx_start + 1,
                "oldLines": ctx_end - ctx_start,
                "newStart": j1 + 1 - _DIFF_CONTEXT if j1 - _DIFF_CONTEXT > 0 else 1,
                "newLines": (j2 - j1) + (ctx_end - ctx_start),
                "lines": lines,
            }
        )
        total_lines += len(lines)
        if total_lines > _MAX_DIFF_LINES:
            truncated = True
            break
    return hunks, additions, deletions, truncated


def _find_chapter(store: MdStore, chapter: int) -> Optional[Any]:
    for doc in store.list_chapters():
        if doc.metadata.get("chapter") == chapter:
            return doc
    return None


def _read_target(novel: str, target: dict) -> tuple[Optional[Any], Optional[str]]:
    """返回 (chapter_doc_or_None, settings_rel_or_None)；失败返回 (None, None)。"""
    store = _store_of(novel)
    kind = target.get("kind")
    key = str(target.get("key", ""))
    if kind == "chapter":
        try:
            chapter = int(str(key).removeprefix("ch-"))
        except ValueError:
            return None, None
        doc = _find_chapter(store, chapter)
        return doc, None
    if kind == "settings" and key.startswith("settings/") and ".." not in key:
        if store.exists(key):
            return None, key
    return None, None


def _store_of(novel: str) -> MdStore:
    # 提案接受会写事实源 → 启用写作历史
    from src.memory.store_factory import open_store

    return open_store(get_settings().novels_dir / novel, writable=True)


def register_proposal_api(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    def _proposals_path(novel: str, cid: str) -> Path:
        d = get_settings().novels_dir / novel / "chats"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{cid}-proposals.json"

    def _load_proposals(novel: str, cid: str) -> list[dict]:
        p = _proposals_path(novel, cid)
        if not p.exists():
            return []
        return json.loads(p.read_text(encoding="utf-8"))

    def _save_proposals(novel: str, cid: str, items: list[dict]) -> None:
        _proposals_path(novel, cid).write_text(
            json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _apply_proposal(novel: str, proposal: dict) -> dict:
        """把提案写入事实源；目标已变化时返回 conflict。"""
        store = _store_of(novel)
        target = proposal["target"]
        if target["kind"] == "chapter":
            doc = _find_chapter(store, int(str(target["key"]).removeprefix("ch-")))
            if doc is None:
                return {"status": "conflict", "message": "章节已不存在。"}
            if doc.content != proposal["original"]:
                return {"status": "conflict", "message": "文稿版本已经变化，未覆盖你的最新内容。"}
            store.write(
                doc.doc_id,
                proposal["proposed"],
                metadata=dict(doc.metadata),
                commit_message=f"{proposal['title']} 人工接受智能体提案",
            )
            return {"status": "accepted", "message": "变更已应用并保存到本机。"}
        rel = target["key"]
        if not store.exists(rel) or store.read(rel).content != proposal["original"]:
            return {"status": "conflict", "message": "文稿版本已经变化，未覆盖你的最新内容。"}
        doc = store.read(rel)
        store.write(
            rel,
            proposal["proposed"],
            metadata=dict(doc.metadata),
            commit_message=f"{proposal['title']} 人工接受智能体提案",
        )
        return {"status": "accepted", "message": "变更已应用并保存到本机。"}

    @app.post("/api/chats/{cid}/propose")
    def chat_propose(cid: str, body: ProposeBody, novel: str = "") -> JSONResponse:
        """让对话智能体对目标文档产出一篇改稿提案（不写盘）。"""
        if not body.instruction.strip():
            raise HTTPException(400, "改稿指令不能为空")
        novel_id = novel or default_novel
        doc, rel = _read_target(novel_id, body.target)
        if doc is None and rel is None:
            raise HTTPException(404, "目标文档不存在")

        original = doc.content if doc is not None else _store_of(novel_id).read(rel).content
        target_title = (
            f"第 {doc.metadata.get('chapter')} 章 {doc.metadata.get('title', '')}".strip()
            if doc is not None
            else Path(rel).stem
        )

        # LLM 生成提案全文（对话 + 全书上下文 + 目标原文 + 指令）
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry
        from src.config.settings import load_models_config
        from src.web.inkforge_windows import agent_prompt

        # 复用会话历史，保持多轮语境
        chat_path = get_settings().novels_dir / novel_id / "chats" / f"{cid}.json"
        history: list[dict] = []
        if chat_path.exists():
            history = json.loads(chat_path.read_text(encoding="utf-8")).get("messages", [])[-8:]

        preset_prompt = agent_prompt(body.agent)
        context = _book_brief(novel_id)
        system = (
            f"{preset_prompt}\n\n"
            "你现在的任务：根据用户指令对「目标文稿」进行改写或修订，直接输出修改后的完整文稿。\n"
            "要求：只输出完整文稿本身，不要输出任何解释、前后缀或代码块标记；"
            "未涉及指令要求的部分必须保持原文不变；与作品事实源保持一致。\n\n"
            f"{context}"
        )
        user = (
            f"【改稿指令】{body.instruction.strip()}\n\n"
            f"【目标文稿：{target_title}】\n{original}"
        )
        messages = [ChatMessage(role="system", content=system)]
        for m in history:
            messages.append(ChatMessage(role=m["role"], content=m["content"]))
        messages.append(ChatMessage(role="user", content=user))

        registry = ModelRegistry(load_models_config())
        role = body.agent if body.agent in registry.roles else (
            "chat" if "chat" in registry.roles else (registry.roles[0] if registry.roles else "chat")
        )
        result = registry.chat_as(role, messages, temperature=0.7)
        proposed = result.content.strip()
        # 剥掉可能的 ``` 包裹
        if proposed.startswith("```"):
            proposed = re.sub(r"^```\w*\n", "", proposed)
            proposed = re.sub(r"\n```\s*$", "", proposed)

        hunks, additions, deletions, truncated = _build_hunks(original, proposed)
        if additions == 0 and deletions == 0:
            raise HTTPException(422, "智能体认为无需修改：返回内容与原文一致。")

        proposal = {
            "id": "p-" + uuid.uuid4().hex[:10],
            "chat_id": cid,
            "agent": body.agent,
            "title": f"{target_title} · 改稿提案",
            "summary": body.instruction.strip()[:80],
            "target": body.target,
            "status": "pending",
            "statusMessage": "",
            "original": original,
            "proposed": proposed,
            "additions": additions,
            "deletions": deletions,
            "hunks": hunks,
            "truncated": truncated,
            "created": time.time(),
        }
        items = _load_proposals(novel_id, cid)
        items.insert(0, proposal)
        _save_proposals(novel_id, cid, items)
        return JSONResponse(_public(proposal))

    @app.get("/api/chats/{cid}/proposals")
    def proposals_list(cid: str, novel: str = "") -> JSONResponse:
        items = _load_proposals(novel or default_novel, cid)
        return JSONResponse({"proposals": [_public(p) for p in items[:20]]})

    @app.post("/api/chats/{cid}/proposals/{pid}/decide")
    def proposal_decide(cid: str, pid: str, body: DecideBody, novel: str = "") -> JSONResponse:
        novel_id = novel or default_novel
        items = _load_proposals(novel_id, cid)
        proposal = next((p for p in items if p["id"] == pid), None)
        if proposal is None:
            raise HTTPException(404, f"提案不存在：{pid}")
        if proposal["status"] not in ("pending", "conflict", "error"):
            raise HTTPException(409, f"提案已是 {proposal['status']} 状态，不可重复审阅")

        if body.decision == "reject":
            proposal["status"] = "rejected"
            proposal["statusMessage"] = "已保留当前文稿，未应用这次变更。"
        elif body.decision == "accept":
            proposal["status"] = "accepting"
            result = _apply_proposal(novel_id, proposal)
            proposal["status"] = result["status"]
            proposal["statusMessage"] = result["message"]
        else:
            raise HTTPException(400, "decision 须为 accept / reject")
        _save_proposals(novel_id, cid, items)
        return JSONResponse(_public(proposal))

    logger.info("Inkforge 提案-审阅流 API 已注册（propose/decide）")


def _book_brief(novel: str) -> str:
    """轻量书籍上下文（与对话上下文一致的精简版）。"""
    try:
        store = _store_of(novel)
        parts = []
        if store.exists("settings/outline.md"):
            meta = store.read("settings/outline.md").metadata
            parts.append(f"《{meta.get('title', '')}》主题：{meta.get('theme', '')}")
        chars = (store.root / "settings" / "characters")
        if chars.exists():
            names = [p.stem for p in sorted(chars.glob('*.md'))[:10]]
            parts.append("人物：" + "、".join(names))
        if store.exists("settings/state-board.md"):
            parts.append("【实体状态板】" + store.read("settings/state-board.md").content[:400])
        return "\n".join(parts)[:2500]
    except Exception:  # noqa: BLE001
        return ""


def _public(proposal: dict) -> dict:
    return {k: v for k, v in proposal.items() if k not in ("original",)}
