"""Inkforge 扩展 API（三）：深挖自 DeepWrite 的新功能后端。

- 智能体设置：五阶段智能体的系统提示词覆盖（chat/propose 运行时读取）
- 素材库：可绑定到作品的参考资料条目（合成进 custom-skills.md）
- 学习仿写：三阶段（素材拆解 / 剧情学习 / 文风学习）样本分析
- 数据看板：验收指标 + 评分历史 + 伏笔台账汇总
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import frontmatter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

_NOVEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _overrides_path() -> Path:
    """智能体提示词覆盖文件路径。

    修复（本次冒烟测试发现）：原实现直接 write_text 到 ``data/config/``，
    但该目录在全项目任何位置都不会被创建 → 首次保存智能体提示词恒 500
    （FileNotFoundError），「智能体设置」面板在干净安装上完全不可用。
    """
    d = get_settings().novels_dir.parent / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d / "agent-presets.json"


def _load_overrides() -> dict:
    p = _overrides_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def agent_prompt(agent: str) -> str:
    """供 chat / propose 使用的最终预设提示词（覆盖优先）。

    P1 墨师全域化：具备动作能力的智能体（当前为 master）在**任何情况下**都会拿到
    工作台动作说明——包括用户自定义覆盖了提示词的情形。因此动作块在这里统一追加，
    而不是写进 AGENT_PRESETS 常量（后者会被 override 整段替换掉）。
    """
    from src.agents.action_prompt import actions_block_for
    from src.web.inkforge_api import AGENT_PRESETS, CONSTITUTION

    if agent == "chat":
        agent = "master"  # 旧会话兼容
    override = _load_overrides().get(agent, {}).get("prompt", "")
    base = override or AGENT_PRESETS.get(agent, AGENT_PRESETS["master"])["prompt"]
    # 最高刚性指令：无论默认还是用户自定义提示词，运行时一律附加
    if "第零条" not in base:
        base = base + "\n\n" + CONSTITUTION
    # 动作说明：与 CONSTITUTION 同级，永远附带
    actions = actions_block_for(agent)
    if actions and "【工作台动作清单" not in base:
        base = base + "\n\n" + actions
    return base


class PresetBody(BaseModel):
    prompt: str


class MaterialBody(BaseModel):
    title: str
    content: str


class LearningBody(BaseModel):
    title: str
    sample: str


def register_windows_api(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    def _store(novel: str = "") -> MdStore:
        nid = novel or default_novel
        if not _NOVEL_RE.match(nid):
            raise HTTPException(400, f"非法书名标识：{nid!r}")
        # 本模块 _store 仅服务看板等只读视图 → 不挂 Git
        from src.memory.store_factory import open_store

        return open_store(get_settings().novels_dir / nid, writable=False)

    def _data_dir(name: str) -> Path:
        d = get_settings().novels_dir.parent / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ══════════ 智能体设置（提示词覆盖） ══════════

    @app.get("/api/agent-presets")
    def agent_presets_list() -> JSONResponse:
        from src.web.inkforge_api import AGENT_PRESETS

        overrides = _load_overrides()
        items = []
        for key, preset in AGENT_PRESETS.items():
            items.append(
                {
                    "key": key,
                    "label": preset["label"],
                    "prompt": overrides.get(key, {}).get("prompt", preset["prompt"]),
                    "custom": key in overrides,
                }
            )
        return JSONResponse({"presets": items})

    @app.put("/api/agent-presets/{key}")
    def agent_presets_put(key: str, body: PresetBody) -> JSONResponse:
        from src.web.inkforge_api import AGENT_PRESETS

        if key not in AGENT_PRESETS:
            raise HTTPException(404, f"未知智能体：{key}")
        overrides = _load_overrides()
        overrides.setdefault(key, {})["prompt"] = body.prompt
        _overrides_path().write_text(
            json.dumps(overrides, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return JSONResponse({"ok": True, "key": key})

    @app.delete("/api/agent-presets/{key}")
    def agent_presets_reset(key: str) -> JSONResponse:
        overrides = _load_overrides()
        overrides.pop(key, None)
        _overrides_path().write_text(
            json.dumps(overrides, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return JSONResponse({"ok": True, "key": key, "reset": True})

    # ══════════ 素材库 ══════════

    @app.get("/api/materials")
    def materials_list() -> JSONResponse:
        items = []
        for path in sorted(_data_dir("materials").glob("*.md")):
            post = frontmatter.load(str(path))
            items.append(
                {
                    "id": path.stem,
                    "title": str(post.metadata.get("title") or path.stem),
                    "content": post.content,
                }
            )
        return JSONResponse({"materials": items})

    @app.post("/api/materials")
    def materials_create(body: MaterialBody) -> JSONResponse:
        if not body.title.strip() or not body.content.strip():
            raise HTTPException(400, "素材名称与内容不能为空")
        mid = "mt-" + uuid.uuid4().hex[:8]
        path = _data_dir("materials") / f"{mid}.md"
        path.write_text(
            frontmatter.dumps(
                frontmatter.Post(body.content, title=body.title.strip())
            ),
            encoding="utf-8",
            newline="\n",
        )
        return JSONResponse({"ok": True, "id": mid, "title": body.title.strip()})

    @app.delete("/api/materials/{mid}")
    def materials_delete(mid: str) -> JSONResponse:
        if not re.match(r"^mt-[a-f0-9]{8}$", mid):
            raise HTTPException(400, "非法素材 ID")
        path = _data_dir("materials") / f"{mid}.md"
        if path.exists():
            path.unlink()
        return JSONResponse({"ok": True})

    # ══════════ 学习仿写（三阶段） ══════════

    def _learning_dir() -> Path:
        return _data_dir("learning")

    @app.get("/api/learning")
    def learning_list() -> JSONResponse:
        items = []
        for path in sorted(_learning_dir().glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            post = frontmatter.load(str(path))
            items.append(
                {
                    "id": path.stem,
                    "title": str(post.metadata.get("title") or path.stem),
                    "created": path.stat().st_mtime,
                }
            )
        return JSONResponse({"items": items})

    @app.get("/api/learning/{lid}")
    def learning_get(lid: str) -> JSONResponse:
        path = _learning_dir() / f"{lid}.md"
        if not re.match(r"^ln-[a-f0-9]{8}$", lid) or not path.exists():
            raise HTTPException(404, "学习成果不存在")
        post = frontmatter.load(str(path))
        return JSONResponse({"id": lid, "title": post.metadata.get("title", ""), "content": post.content})

    @app.delete("/api/learning/{lid}")
    def learning_delete(lid: str) -> JSONResponse:
        path = _learning_dir() / f"{lid}.md"
        if path.exists():
            path.unlink()
        return JSONResponse({"ok": True})

    @app.post("/api/learning")
    def learning_run(body: LearningBody) -> JSONResponse:
        """三阶段学习仿写：素材拆解 → 剧情学习 → 文风学习。"""
        if len(body.sample.strip()) < 200:
            raise HTTPException(400, "样本正文太短（至少 200 字），请粘贴更完整的章节/片段")
        from src.config.settings import load_models_config
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry

        registry = ModelRegistry(load_models_config())
        sample = body.sample.strip()[:12000]

        stages = [
            (
                "素材拆解",
                "对以下文本做素材拆解：列出其中的世界观设定点、人物设定点、道具/地点/组织、可复用的桥段。逐条输出，每条一行。",
            ),
            (
                "剧情学习",
                "分析以下文本的剧情技法：主线推进方式、冲突设计、转折点位置与效果、结尾钩子手法。输出要点清单。",
            ),
            (
                "文风学习",
                "分析以下文本的文风：叙事视角、句式节奏、描写偏好（动作/环境/五感）、对话风格、词汇倾向。输出可模仿的写作规范清单。",
            ),
        ]
        sections = [f"# 学习仿写：{body.title.strip()}", f"> 样本 {len(body.sample.strip())} 字 · 生成于 {time.strftime('%Y-%m-%d %H:%M')}"]
        for label, ask in stages:
            messages = [
                ChatMessage(
                    role="system",
                    content="你是文学分析引擎，只输出结构化 Markdown 要点，不要客套与复述原文。",
                ),
                ChatMessage(role="user", content=f"{ask}\n\n【样本文本】\n{sample}"),
            ]
            result = registry.chat_as("chat", messages, temperature=0.4)
            sections.append(f"\n## {label}\n\n{result.content.strip()}")

        lid = "ln-" + uuid.uuid4().hex[:8]
        path = _learning_dir() / f"{lid}.md"
        path.write_text("\n\n".join(sections), encoding="utf-8", newline="\n")
        return JSONResponse({"ok": True, "id": lid, "title": body.title.strip()})

    # ══════════ 数据看板 ══════════

    @app.get("/api/dashboard")
    def dashboard(novel: str = "") -> JSONResponse:
        store = _store(novel)
        chapters = store.list_chapters()
        approved = [c for c in chapters if c.metadata.get("status") == "approved"]
        scores = [c.metadata.get("score") for c in approved if c.metadata.get("score")]
        first_pass = [c for c in approved if c.metadata.get("first_review_passed")]

        history = [
            {
                "chapter": c.metadata.get("chapter"),
                "attempt": c.metadata.get("attempt"),
                "score": c.metadata.get("score"),
                "status": c.metadata.get("status"),
                "model": c.metadata.get("model"),
            }
            for c in sorted(chapters, key=lambda d: d.metadata.get("chapter", 0))
        ]

        foreshadow = {"total": 0, "resolved": 0, "items": []}
        if store.exists("settings/foreshadowing.md"):
            items = store.read("settings/foreshadowing.md").metadata.get("items", []) or []
            foreshadow["total"] = len(items)
            foreshadow["resolved"] = sum(1 for i in items if i.get("status") == "resolved")
            foreshadow["items"] = [
                {
                    "id": i.get("id"),
                    "desc": str(i.get("desc", ""))[:60],
                    "status": i.get("status", "planted"),
                    "planted_ch": i.get("planted_ch"),
                    "resolve_ch": i.get("resolve_ch"),
                }
                for i in items[:20]
            ]

        return JSONResponse(
            {
                "metrics": {
                    "total_chapters": len(chapters),
                    "approved": len(approved),
                    "first_pass_rate": round(len(first_pass) / len(approved) * 100, 1)
                    if approved
                    else None,
                    "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                },
                "chapters": history,
                "foreshadow": foreshadow,
            }
        )

    logger.info("Inkforge 扩展 API（三）已注册（智能体设置/素材库/学习仿写/数据看板）")
