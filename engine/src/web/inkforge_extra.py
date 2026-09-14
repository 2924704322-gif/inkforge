"""Inkforge 扩展 API（二）：章节追加/删除、文稿导出、模型连接测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

_NOVEL_RE = __import__("re").compile(r"^[A-Za-z0-9_-]+$")


class ChapterCreateBody(BaseModel):
    title: str = ""


class ModelTestBody(BaseModel):
    role: str = "writer"


def register_inkforge_extra(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    def _store(novel: str = "") -> MdStore:
        nid = novel or default_novel
        if not _NOVEL_RE.match(nid):
            raise HTTPException(400, f"非法书名标识：{nid!r}")
        # 含章节追加/删除 → 启用写作历史
        from src.memory.store_factory import open_store

        return open_store(get_settings().novels_dir / nid, writable=True)

    # ── 章节追加（树行末「+」）──

    @app.post("/api/chapters")
    def chapter_create(body: ChapterCreateBody, novel: str = "") -> JSONResponse:
        store = _store(novel)
        existing = store.list_chapters()
        next_no = (
            max((int(c.metadata.get("chapter", 0)) for c in existing), default=0) + 1
        )
        volume = (
            max((int(c.metadata.get("volume", 1)) for c in existing), default=1)
        )
        rel = store.chapter_rel_path(volume, next_no)
        content = f"# 第{next_no}章 {body.title.strip() or '未命名'}\n\n"
        store.write(
            rel,
            content,
            metadata={
                "chapter": next_no,
                "volume": volume,
                "title": body.title.strip() or "未命名",
                "status": "draft",
                "characters": [],
            },
            commit_message=f"ch-{next_no:03d} 新建章节（Inkforge）",
        )
        return JSONResponse({"ok": True, "chapter": next_no, "volume": volume, "rel": rel})

    # ── 章节删除（树行末「⋯」）──

    @app.delete("/api/chapters/{chapter}")
    def chapter_delete(chapter: int, novel: str = "") -> JSONResponse:
        store = _store(novel)
        for doc in store.list_chapters():
            if doc.metadata.get("chapter") == chapter:
                path = store.root / doc.doc_id
                if path.exists():
                    path.unlink()
                return JSONResponse({"ok": True, "chapter": chapter})
        raise HTTPException(404, f"第 {chapter} 章不存在")

    # ── 文稿导出（复用既有 ExportSkill）──

    @app.get("/api/export")
    def export_manuscript(
        novel: str = "", format: str = "txt", include_unapproved: bool = False
    ):
        nid = novel or default_novel
        from src.skills.base import SkillContext
        from src.skills.export import ExportSettings, ExportSkill

        store = _store(nid)
        ctx = SkillContext(novel_id=nid, store=store, memory=None, registry=None)
        skill = ExportSkill(ExportSettings(enabled=True, format=format), ctx)
        try:
            result = skill.run(include_unapproved=include_unapproved)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc))
        path = Path(str(result.get("path", "")))
        if not path.exists():
            raise HTTPException(404, f"导出产物缺失：{path}")
        media = "application/epub+zip" if format == "epub" else "text/plain; charset=utf-8"
        return FileResponse(path, media_type=media, filename=path.name)

    # ── 模型连接测试（按角色真实发一条消息）──

    @app.post("/api/model-test")
    def model_test(body: ModelTestBody) -> JSONResponse:
        from src.config.settings import load_models_config
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry

        try:
            registry = ModelRegistry(load_models_config())
            result = registry.chat_as(
                body.role,
                [ChatMessage(role="user", content="回复「连接正常」四个字，不要输出其他内容。")],
                temperature=0.0,
            )
            return JSONResponse(
                {"ok": True, "provider": result.provider_name, "model": result.model,
                 "reply": result.content[:40]}
            )
        except Exception as exc:  # noqa: BLE001 - 测试失败原样透出
            return JSONResponse({"ok": False, "error": str(exc)[:300]})

    # ── 写作历史（每本书独立 Git 仓库的只读视图，S1-3） ──

    @app.get("/api/history")
    def novel_history(novel: str = "", limit: int = 30) -> JSONResponse:
        """列出该书的最近提交记录（生成/人审/人工编辑均由 Git 承载）。"""
        nid = novel or default_novel
        if not _NOVEL_RE.match(nid):
            raise HTTPException(400, f"非法书名标识：{nid!r}")
        from src.memory.store_factory import git_history_enabled, open_store

        if not git_history_enabled():
            return JSONResponse({"enabled": False, "commits": [], "hint": "INKFORGE_GIT_HISTORY=0 已关闭写作历史"})
        store = open_store(get_settings().novels_dir / nid, writable=False)
        commits = store.history(limit=limit)
        return JSONResponse({
            "enabled": True,
            "commits": commits,
            "hint": "" if commits else "尚无提交：生成或编辑一次内容后即出现记录",
        })

    logger.info("Inkforge 扩展 API（二）已注册（章节 CRUD / 导出 / 模型测试 / 写作历史）")
