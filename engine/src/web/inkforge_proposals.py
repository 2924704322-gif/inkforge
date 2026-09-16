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
from typing import Any

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
    feedback: str = ""  # 打回意见：reject 时建议填写，引擎据此重做一版


# ---------- 提案评审（审校主编口径：四项评分 + 意见清单） ----------

_REVIEW_SYSTEM = (
    "你是长篇小说的审校主编，负责在人工审批前给出可执行的评审意见。\n"
    "下面给出作品事实源摘录与一份【待审文稿】。待审文稿可能是正文章节，也可能是大纲 / 世界观 / "
    "人物设定等**设定类文档**——两种都必须照常评审，**不要以「这不是正文」为由拒评或给 0 分**。\n"
    "四项评分含义（0-10；括号内是设定类文档的口径）：\n"
    "· consistency 设定一致性（设定类：与全书既有设定是否自洽、有无矛盾）\n"
    "· plot 大纲符合度（设定类：结构是否完整、能否执行到章）\n"
    "· continuity 衔接连贯性（设定类：与前后卷 / 既有文档是否衔接）\n"
    "· prose 文笔质量（设定类：表述是否清晰、可判定、无歧义）\n"
    "length 填该文稿的正文字数。\n"
    "只输出一个 JSON 对象，不要任何解释或代码块标记：\n"
    '{"consistency":n,"plot":n,"continuity":n,"prose":n,"length":n,"comment":"一句话总评",'
    '"issues":[{"dimension":"consistency|plot|continuity|prose","severity":"major|minor",'
    '"description":"问题描述","quote":"原文片段","suggestion":"具体改法"}]}\n'
    "issues 给 1-5 条具体可执行的修改建议；评分要真实判断，不要敷衍给满分，也不要一律给 0。"
)


def _score(value: object, default: float = 0.0) -> float:
    """把模型返回的分数容错成 float（可能是字符串、None 或带单位）。"""
    try:
        return round(float(str(value).strip()), 2)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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


def _find_chapter(store: MdStore, chapter: int) -> Any | None:
    for doc in store.list_chapters():
        if doc.metadata.get("chapter") == chapter:
            return doc
    return None


def _read_target(novel: str, target: dict) -> tuple[Any | None, str | None]:
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


def _store_of(novel: str, writable: bool = False) -> MdStore:
    # 读路径（读取目标文稿）不建仓库；接受提案时 writable=True 进写作历史
    from src.memory.store_factory import open_store

    return open_store(get_settings().novels_dir / novel, writable=writable)


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
        store = _store_of(novel, writable=True)
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

    def _review_proposal(novel_id: str, target_title: str, proposed: str) -> tuple[dict | None, str]:
        """审校主编评审提案成稿：四项评分 + 意见清单（与流水线人审同一口径）。

        返回 (review, error)：评审失败时 review=None 且 error 带原因——**失败要能看见**，
        否则卡片默默不出评分，用户只会看到"评分没工作"。
        """
        try:
            from src.config.settings import load_models_config
            from src.llm.base import ChatMessage
            from src.llm.registry import ModelRegistry
            from src.web.inkforge_api import _parse_json_object

            registry = ModelRegistry(load_models_config())
            role = (
                "editor"
                if "editor" in registry.roles
                else ("chat" if "chat" in registry.roles else (registry.roles[0] if registry.roles else "chat"))
            )
            messages = [
                ChatMessage(
                    role="system",
                    content=f"{_REVIEW_SYSTEM}\n\n{_book_brief(novel_id)}",
                ),
                ChatMessage(
                    role="user",
                    content=f"【待审文稿：{target_title}】\n{proposed[:8000]}",
                ),
            ]
            raw = registry.chat_as(role, messages, temperature=0.2).content
            data = _parse_json_object(raw)
            issues: list[dict] = []
            for item in (data.get("issues") or [])[:5]:
                if isinstance(item, dict):
                    issues.append(
                        {
                            "dimension": str(item.get("dimension", "prose")),
                            "severity": "major" if item.get("severity") == "major" else "minor",
                            "description": str(item.get("description", "")),
                            "quote": str(item.get("quote", "")),
                            "suggestion": str(item.get("suggestion", "")),
                        }
                    )
            if not any(k in data for k in ("consistency", "plot", "continuity", "prose")):
                logger.warning("提案评审返回无法解析：%s", raw[:200])
                return None, f"评审模型返回无法解析：{raw[:120]}"
            return {
                "consistency": _score(data.get("consistency")),
                "plot": _score(data.get("plot")),
                "continuity": _score(data.get("continuity")),
                "prose": _score(data.get("prose")),
                "length": int(_score(data.get("length"), len(proposed))),
                "comment": str(data.get("comment", "")),
                "issues": issues,
            }, ""
        except Exception as exc:  # noqa: BLE001 - 评审是增值项，失败不阻断
            logger.warning("提案评审失败（不影响提案可用性）：%s", exc)
            return None, f"{type(exc).__name__}: {exc}"

    def _generate(novel_id: str, cid: str, instruction: str, target: dict, agent: str) -> dict:
        """按指令产出一版改稿提案（不落盘）：正文 + 行级 diff + 审校评分。"""
        doc, rel = _read_target(novel_id, target)
        if doc is None and rel is None:
            raise HTTPException(404, "目标文档不存在")

        original = doc.content if doc is not None else _store_of(novel_id).read(rel).content
        target_title = (
            f"第 {doc.metadata.get('chapter')} 章 {doc.metadata.get('title', '')}".strip()
            if doc is not None
            else Path(rel).stem
        )

        # LLM 生成提案全文（对话 + 全书上下文 + 目标原文 + 指令）
        from src.agents.architect import brief_fidelity_block
        from src.config.settings import load_models_config
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry
        from src.web.inkforge_windows import agent_prompt

        # 复用会话历史，保持多轮语境
        chat_path = get_settings().novels_dir / novel_id / "chats" / f"{cid}.json"
        history: list[dict] = []
        if chat_path.exists():
            history = json.loads(chat_path.read_text(encoding="utf-8")).get("messages", [])[-8:]

        preset_prompt = agent_prompt(agent)
        context = _book_brief(novel_id)
        system = (
            f"{preset_prompt}\n\n{brief_fidelity_block()}\n\n"
            "你现在的任务：根据用户指令对「目标文稿」进行改写或修订，直接输出修改后的完整文稿。\n"
            "要求：只输出完整文稿本身，不要输出任何解释、前后缀或代码块标记；"
            "未涉及指令要求的部分必须保持原文不变；与作品事实源保持一致。\n\n"
            f"{context}"
        )
        user = f"【改稿指令】{instruction}\n\n【目标文稿：{target_title}】\n{original}"
        messages = [ChatMessage(role="system", content=system)]
        for m in history:
            messages.append(ChatMessage(role=m["role"], content=m["content"]))
        messages.append(ChatMessage(role="user", content=user))

        registry = ModelRegistry(load_models_config())
        role = agent if agent in registry.roles else (
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

        review, review_error = _review_proposal(novel_id, target_title, proposed)
        return {
            "id": "p-" + uuid.uuid4().hex[:10],
            "chat_id": cid,
            "agent": agent,
            "title": f"{target_title} · 改稿提案",
            "summary": instruction[:80],
            # 完整指令与打回意见都留档：打回重做要按原指令 + 新意见再来一版
            "instruction": instruction,
            "target": target,
            "status": "pending",
            "statusMessage": "",
            "original": original,
            "proposed": proposed,
            "additions": additions,
            "deletions": deletions,
            "hunks": hunks,
            "truncated": truncated,
            "review": review,
            "reviewError": review_error,
            "created": time.time(),
        }

    @app.post("/api/chats/{cid}/propose")
    def chat_propose(cid: str, body: ProposeBody, novel: str = "") -> JSONResponse:
        """让对话智能体对目标文档产出一篇改稿提案（**不写盘**，等人工审批）。"""
        if not body.instruction.strip():
            raise HTTPException(400, "改稿指令不能为空")
        novel_id = novel or default_novel
        proposal = _generate(novel_id, cid, body.instruction.strip(), body.target, body.agent)
        items = _load_proposals(novel_id, cid)
        items.insert(0, proposal)
        _save_proposals(novel_id, cid, items)
        return JSONResponse(_public(proposal))


    @app.get("/api/chats/{cid}/proposals")
    def proposals_list(cid: str, novel: str = "") -> JSONResponse:
        items = _load_proposals(novel or default_novel, cid)
        return JSONResponse({"proposals": [_public(p) for p in items[:20]]})

    @app.post("/api/chats/{cid}/proposals/{pid}/review")
    def proposal_review(cid: str, pid: str, novel: str = "") -> JSONResponse:
        """补一次审校评分。

        存在的意义：升级前生成的待审提案没有 `review` 字段，评审失败时也会为空。
        没有它，用户只能看到"评分没工作"，而唯一出路是重新生成一版提案。
        """
        novel_id = novel or default_novel
        items = _load_proposals(novel_id, cid)
        proposal = next((p for p in items if p["id"] == pid), None)
        if proposal is None:
            raise HTTPException(404, f"提案不存在：{pid}")
        target = proposal.get("target", {})
        title = str(proposal.get("title", "")).replace(" · 改稿提案", "")
        review, error = _review_proposal(
            novel_id, title or str(target.get("key", "")), str(proposal.get("proposed", ""))
        )
        if review is None:
            raise HTTPException(502, f"审校评分失败：{error or '未知原因'}")
        proposal["review"] = review
        proposal["reviewError"] = ""
        _save_proposals(novel_id, cid, items)
        return JSONResponse(_public(proposal))

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
            feedback = (body.feedback or "").strip()
            proposal["status"] = "rejected"
            proposal["statusMessage"] = (
                "已打回，正在按你的意见重做一版。" if feedback else "已打回，未应用这次变更。"
            )
            replacement: dict | None = None
            if feedback:
                base = proposal.get("instruction") or proposal.get("summary", "")
                try:
                    replacement = _generate(
                        novel_id,
                        cid,
                        f"{base}\n\n【人工打回意见，必须落实】\n{feedback}",
                        proposal["target"],
                        proposal.get("agent", "chat"),
                    )
                    items.insert(0, replacement)
                except HTTPException as exc:
                    proposal["statusMessage"] = f"已打回；按意见重做失败：{exc.detail}"
                except Exception as exc:  # noqa: BLE001
                    proposal["statusMessage"] = f"已打回；按意见重做失败：{exc}"
            _save_proposals(novel_id, cid, items)
            payload = _public(proposal)
            payload["replacementId"] = replacement["id"] if replacement else None
            return JSONResponse(payload)
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
