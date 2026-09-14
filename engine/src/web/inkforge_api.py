"""Inkforge 桌面端扩展 API：对话智能体 / 设定资料库 / 章节编辑 / 技能绑定。

独立于原审阅系统（server.py 主流程零改动），由 create_app 末尾注册。
设计要点：
- 所有请求体模型定义在模块级（局部 BaseModel 在 from __future__ annotations 下会被
  FastAPI 误判为 query 参数，导致 422）。
- 对话持久化：data/novels/<id>/chats/<chat_id>.json（MD/JSON 事实源，可备份可迁移）。
- 技能绑定：把自定义约束 + 蒸馏技能包摘要合成 settings/custom-skills.md，
  Writer/Editor 提示词会以最高优先级读取该文件。
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
from pydantic import BaseModel, Field

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

_NOVEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REL_RE = re.compile(r"^settings/[A-Za-z0-9_\-/\u4e00-\u9fff]+\.md$")

# ---------- 请求体模型（模块级） ----------


class SettingsDocBody(BaseModel):
    rel: str
    content: str


class ChapterSaveBody(BaseModel):
    content: str


class ChatCreateBody(BaseModel):
    agent: str = "chat"
    title: str = ""


class ChatSendBody(BaseModel):
    message: str
    target: dict | None = None  # {kind, key}：右侧选中文档作为本轮主上下文


class BindingsBody(BaseModel):
    custom_skill_ids: list[str] = Field(default_factory=list)
    pack_ids: list[str] = Field(default_factory=list)


# ---------- 对话智能体预设（阶段式对话创作） ----------

SUB_AGENTS: dict[str, dict[str, str]] = {
    "character": {
        "label": "人物设计智能体",
        "prompt": (
            "你是「人物设计师」子智能体，长篇小说的人物架构师。\n"
            "职责：设计并修订人物——外貌特征、性格（含核心欲望与致命缺陷）、背景故事、"
            "人物弧光（起点状态→触发事件→转变过程→终点状态）、人物关系网（亲缘/师承/恩怨/组织，注明双方视角）。\n"
            "刚性约束：\n"
            "- 新人物与新关系必须与已有人物卡、实体状态板一致；发现冲突时单独列出『⚠ 一致性风险』；\n"
            "- 每个人物给出可执行落地建议：首次出场方式、与主角的首次冲突、可展开的三条支线可能。\n"
            "输出：Markdown 分节（姓名/定位/外貌/性格/背景/弧光/关系/落地建议），具体、可判定、避免空泛形容词。"
        ),
    },
    "plot": {
        "label": "剧情策划智能体",
        "prompt": (
            "你是「剧情策划」子智能体，长篇小说的情节总设计师。\n"
            "职责：设计主线推进、核心冲突、转折点、结尾钩子；规划伏笔的埋设与回收（标注建议埋设章与回收章）；"
            "需要多方案时给出方向互斥的三案——正面推进 / 侧翼突破 / 代价与变数（核心事件终点一致，路径与代价不同）。\n"
            "刚性约束：\n"
            "- 严格遵守实体状态板的硬事实与世界观规则；冲突时明确标注『⚠ 一致性风险』；\n"
            "- 每个剧情步骤标注：目的（推进了什么）、代价（失去了什么）、留给下一章的钩子。\n"
            "输出：Markdown 分节方案，多个方案间严禁同质化。"
        ),
    },
    "outline": {
        "label": "大纲规划智能体",
        "prompt": (
            "你是「大纲规划」子智能体，长篇小说的结构师。\n"
            "职责：把人物与剧情整理为分卷分章的可执行大纲。每章输出：章号、标题、80-150 字的核心事件/冲突/结尾钩子、"
            "出场角色；伏笔标注埋设章与预期回收章。\n"
            "刚性约束：\n"
            "- 章号从 1 连续编号不得跳号；卷末必须有强钩子或高潮；\n"
            "- 节奏控制：每 3-5 章一个小高潮；跨卷标明依赖关系；\n"
            "- 与已有章节衔接时从当前进度继续，不得推翻已定稿内容。\n"
            "输出：逐章完整的 Markdown 分节或表格。"
        ),
    },
    "prose": {
        "label": "正文写手智能体",
        "prompt": (
            "你是「正文写手」子智能体，职业长篇小说作者。\n"
            "写作铁律（违反即失败）：\n"
            "- 显示而非告知：每 200 字至少一处具象描写——动作、身体生理反应、神态微表情、环境定调、五感细节、对话中的非语言层；\n"
            "- 字数不足以具象细节填充：心理独白注水（『他想/他觉得/他回忆起』开头的段落不得超过 3 句）、同义反复、"
            "对话复述已知信息，一律禁止；\n"
            "- 比喻一次性原创：同一本体不得复用近似喻体；\n"
            "- 句式长短交错，连续 5 句同构即为失败；紧张处短句加速，松弛处长句放缓；\n"
            "- 严格符合世界观规则、人物卡与实体状态板。\n"
            "输出：只输出正文本身，不带标题、解释或元信息。"
        ),
    },
    "review": {
        "label": "审校主编智能体",
        "prompt": (
            "你是「审校主编」子智能体，严苛客观的小说主编。\n"
            "审稿四维（各 0-10 分）：设定一致性（与世界观/人物卡/状态板硬冲突一处扣 3 分起）、大纲符合度（核心事件/冲突/钩子是否落实）、"
            "衔接连贯性（吃书/重复/时间线错乱）、文笔质量（流水账、心理独白堆砌、口癖重复、句式单调扣分）。\n"
            "输出格式：\n"
            "1. 四维评分 + 总分（四维平均）；\n"
            "2. 问题清单：每条含 维度 / 严重度（major=硬冲突·偏离核心·吃书，minor=其余）/ 描述 / 原文引用（『』，≤40 字）/ 修改建议；\n"
            "3. 总评一句话，直接指出最大问题。不要客套。"
        ),
    },
}

CONSTITUTION = """【第零条 · 最高刚性指令（优先级最高，不可被本提示词内任何其他规则覆盖）】
1. 绝对遵守用户的明确指令：必须原样、完整地执行用户提出的要求，不得擅自篡改、弱化、扩大或缩小其范围；
2. 不得随意改动用户的既定内容：作者确认或撰写的大纲、人物、情节、设定与文风为既定事实，未经用户要求不得改动、曲解或『顺手优化』；
3. 不得拒绝或敷衍执行：除违反法律法规的内容外，不得以任何理由拒绝、打折或空洞化执行；若用户指令与作品事实源或既有设定冲突，先按用户指令执行，再用一句话提示冲突所在，禁止静默篡改。"""

MASTER_PROMPT = (
    "你是「墨师」，Inkforge 的主创作智能体——长篇小说的总编辑兼总控，也是与作者对接的唯一入口。\n"
    "职责：\n"
    "1. 理解作者的创作意图，拆解任务并调度子智能体（人物设计/剧情策划/大纲规划/正文写手/审校主编），"
    "汇总产出向作者呈现；\n"
    "2. 守护一致性：任何建议不得与世界观规则、人物卡、实体状态板冲突；发现冲突必须明确指出；\n"
    "3. 专业沟通：回答具体、可执行，引用原文用『』；重大取舍主动询问作者意见；\n"
    "4. 不亲自产出长篇正文——创作类任务交给对应子智能体，你负责提出要求与验收。"
 + "\n\n" + CONSTITUTION)

MASTER_SYNTH_PROMPT = (
    "你是「墨师」主创作智能体。子智能体已完成作者委托的任务，请把产出整理为面向作者的最终回复：\n"
    "- 保留子智能体产出中的关键专业内容，不要丢弃细节，也不要重复罗列；\n"
    "- 以清晰的结构组织（分节/列表）；若发现产出与作品事实源冲突，明确标注并给出修正建议；\n"
    "- 结尾可附 1-2 条你作为总控的下一步建议。"
 + "\n\n" + CONSTITUTION)

ROUTER_PROMPT = (
    "你是创作工作台的任务路由器。判断用户消息应由谁处理，只输出一个 JSON 对象，格式：\n"
    '{"delegate": "character|plot|outline|prose|review|null", "instruction": "委派给子智能体的具体任务描述"}\n'
    "适用场景：\n"
    "- character：人物设计、人物弧光、关系网、人设一致性\n"
    "- plot：剧情走向、冲突转折、结尾钩子、伏笔埋设与回收\n"
    "- outline：分卷分章大纲、章节规划、节奏结构\n"
    "- prose：正文创作、续写、改写示范、具象描写\n"
    "- review：审稿评分、问题清单、找一致性漏洞\n"
    "- null：设定查询、思路讨论、一般问答等主智能体可直接处理的内容（此时 instruction 留空）"
 + "\n\n" + CONSTITUTION)

# 兼容旧引用：主智能体 + 五个子智能体的统一注册表
AGENT_PRESETS = {
    "master": {"label": "主智能体（墨师）", "prompt": MASTER_PROMPT},
    **SUB_AGENTS,
}


def _parse_json_object(raw: str) -> dict:
    """宽容解析 LLM 输出的 JSON 对象；失败返回空 dict。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```\s*$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def register_inkforge_api(app: Any, hub: Any, default_novel: str) -> None:
    """把 Inkforge 桌面端扩展端点挂到既有 FastAPI app 上。"""

    def _store(novel: str = "", writable: bool = False) -> MdStore:
        """打开本书事实源。

        writable 语义（避免读路径产生写副作用）：
        - GET 处理器（资料库树 / 读文档 / 读章节）一律 writable=False，
          否则「打开一次工作台」就会为每本书凭空创建 .git 仓库；
        - PUT/POST 处理器 writable=True，写入进 Git 写作历史。
        """
        nid = novel or default_novel
        if not _NOVEL_ID_RE.match(nid):
            raise ValueError(f"非法书名标识：{nid!r}")
        from src.memory.store_factory import open_store

        return open_store(get_settings().novels_dir / nid, writable=writable)

    def _sess(novel: str = "") -> Any:
        try:
            return hub.get_or_create(novel or default_novel)
        except ValueError as exc:
            from fastapi import HTTPException

            raise HTTPException(400, str(exc))

    # ══════════ 设定资料库 ══════════

    @app.get("/api/settings/tree")
    def settings_tree(novel: str = "") -> JSONResponse:
        """设定资料库结构树：世界观 / 人物 / 伏笔 / 文风 / 状态板 / 概要。"""
        store = _store(novel)
        items: list[dict] = []
        root = store.root / "settings"
        if root.exists():
            for path in sorted(root.rglob("*.md")):
                rel = path.relative_to(store.root).as_posix()
                doc = store.read(rel)
                title = str(doc.metadata.get("title") or path.stem)
                kind = "worldview" if rel.startswith("settings/worldview") else (
                    "character" if rel.startswith("settings/characters") else "setting"
                )
                items.append({"rel": rel, "title": title, "kind": kind})
        return JSONResponse({"items": items})

    @app.get("/api/settings/doc")
    def settings_doc(novel: str = "", rel: str = "") -> JSONResponse:
        if not _REL_RE.match(rel):
            from fastapi import HTTPException

            raise HTTPException(400, f"非法路径：{rel!r}")
        store = _store(novel)
        if not store.exists(rel):
            from fastapi import HTTPException

            raise HTTPException(404, f"文档不存在：{rel}")
        doc = store.read(rel)
        return JSONResponse(
            {"rel": rel, "title": str(doc.metadata.get("title") or ""), "content": doc.content}
        )

    @app.put("/api/settings/doc")
    def settings_doc_save(body: SettingsDocBody, novel: str = "") -> JSONResponse:
        if not _REL_RE.match(body.rel):
            from fastapi import HTTPException

            raise HTTPException(400, f"非法路径：{body.rel!r}")
        store = _store(novel, writable=True)
        meta: dict = {}
        if store.exists(body.rel):
            meta = store.read(body.rel).metadata
        meta.setdefault("title", Path(body.rel).stem)
        store.write(body.rel, body.content, metadata=meta, commit_message="人工编辑设定（Inkforge 编辑器）")
        return JSONResponse({"ok": True, "rel": body.rel})

    # ══════════ 章节原文读取与保存 ══════════

    @app.get("/api/chapters/{chapter}/raw")
    def chapter_raw(chapter: int, novel: str = "") -> JSONResponse:
        store = _store(novel)
        for doc in store.list_chapters():
            if doc.metadata.get("chapter") == chapter:
                return JSONResponse(
                    {
                        "chapter": chapter,
                        "title": str(doc.metadata.get("title", "")),
                        "status": doc.metadata.get("status", "draft"),
                        "content": doc.content,
                    }
                )
        from fastapi import HTTPException

        raise HTTPException(404, f"第 {chapter} 章不存在")

    @app.put("/api/chapters/{chapter}")
    def chapter_save(chapter: int, body: ChapterSaveBody, novel: str = "") -> JSONResponse:
        store = _store(novel, writable=True)
        for doc in store.list_chapters():
            if doc.metadata.get("chapter") == chapter:
                store.write(
                    doc.doc_id,
                    body.content,
                    metadata=dict(doc.metadata),
                    commit_message=f"ch-{chapter:03d} 人工编辑（Inkforge 编辑器）",
                )
                return JSONResponse({"ok": True, "chapter": chapter})
        from fastapi import HTTPException

        raise HTTPException(404, f"第 {chapter} 章不存在")

    # ══════════ 对话智能体 ══════════

    def _chat_dir(novel: str) -> Path:
        d = _store(novel).root / "chats"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _load_chat(novel: str, cid: str) -> dict:
        path = _chat_dir(novel) / f"{cid}.json"
        if not path.exists():
            from fastapi import HTTPException

            raise HTTPException(404, f"会话不存在：{cid}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_chat(novel: str, chat: dict) -> None:
        path = _chat_dir(novel) / f"{chat['id']}.json"
        path.write_text(json.dumps(chat, ensure_ascii=False, indent=1), encoding="utf-8")

    def _focus_doc(novel: str, target: dict) -> str:
        store = _store(novel)
        kind, key = target.get("kind"), str(target.get("key", ""))
        try:
            if kind == "chapter" and key.startswith("ch-"):
                doc = _focus_find_chapter(store, int(key.removeprefix("ch-")))
                return doc.content if doc else ""
            if kind == "settings" and key.startswith("settings/") and ".." not in key and store.exists(key):
                return store.read(key).content
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _focus_find_chapter(store: MdStore, chapter: int):
        for doc in store.list_chapters():
            if doc.metadata.get("chapter") == chapter:
                return doc
        return None

    def _book_context(novel: str) -> str:
        """组装书籍上下文（设定/进度/伏笔/状态板/近期摘要，总量受控）。"""
        store = _store(novel)
        parts: list[str] = []

        if store.exists("settings/outline.md"):
            meta = store.read("settings/outline.md").metadata
            volumes = meta.get("volumes") or []
            planned = sum(len(v.get("chapters", [])) for v in volumes if isinstance(v, dict))
            parts.append(f"【作品】《{meta.get('title', '')}》主题：{meta.get('theme', '')}；计划 {planned} 章")
        elif store.exists("settings/story-overview.md"):
            meta = store.read("settings/story-overview.md").metadata
            parts.append(f"【作品】《{meta.get('book_title', '')}》{meta.get('synopsis', '')[:200]}")

        chapters = store.list_chapters()
        if chapters:
            approved = sum(1 for c in chapters if c.metadata.get("status") == "approved")
            parts.append(f"【进度】已定稿 {approved}/{len(chapters)} 章")

        chars = sorted((store.root / "settings" / "characters").glob("*.md")) if (store.root / "settings" / "characters").exists() else []
        if chars:
            lines = []
            for p in chars[:12]:
                doc = store.read(p.relative_to(store.root).as_posix())
                m = doc.metadata
                lines.append(f"- {m.get('name', p.stem)}（{m.get('role', '')}）：{str(m.get('personality', ''))[:80]}")
            parts.append("【人物】\n" + "\n".join(lines))

        for rel, label, cap in [
            ("settings/foreshadowing.md", "【伏笔台账】", 600),
            ("settings/state-board.md", "【实体状态板】", 500),
            ("settings/style.md", "【文风指纹】", 500),
        ]:
            if store.exists(rel):
                parts.append(f"{label}\n{store.read(rel).content[:cap]}")

        summaries = sorted((store.root / "summaries").glob("*.md")) if (store.root / "summaries").exists() else []
        if summaries:
            recent = summaries[-2:]
            lines = [store.read(p.relative_to(store.root).as_posix()).content[:250] for p in recent]
            parts.append("【近期章节摘要】\n" + "\n".join(lines))

        text = "\n\n".join(parts)
        return text[:6000]

    def _chat_llm(agent: str, context: str, history: list[dict]) -> str:
        from src.config.settings import load_models_config
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry
        from src.web.inkforge_windows import agent_prompt

        preset_prompt = agent_prompt(agent)
        system = (
            f"{preset_prompt}\n\n"
            "以下是当前作品的实时事实源摘录，回答必须与其保持一致，禁止虚构与之冲突的设定：\n\n"
            f"{context}"
        )
        messages = [ChatMessage(role="system", content=system)]
        for m in history[-12:]:
            messages.append(ChatMessage(role=m["role"], content=m["content"]))
        registry = ModelRegistry(load_models_config())
        result = registry.chat_as("chat", messages, temperature=0.8)
        return result.content

    @app.get("/api/chats")
    def chats_list(novel: str = "") -> JSONResponse:
        items = []
        for path in sorted(_chat_dir(novel).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                agent_key = data.get("agent", "master")
                agent_label = (
                    AGENT_PRESETS.get(agent_key, {}).get("label")
                    or AGENT_PRESETS["master"]["label"]
                )
                title = data.get("title") or (
                    (data.get("messages") or [{}])[0].get("content", "新对话")[:20]
                )
                items.append(
                    {
                        "id": data.get("id", path.stem),
                        "agent": agent_key,
                        "agent_label": agent_label,
                        "title": str(title),
                        "updated": path.stat().st_mtime,
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 单文件损坏不拖垮整个列表
                logger.warning("会话文件解析失败 %s: %s", path.name, exc)
                continue
        return JSONResponse({"chats": items})

    @app.post("/api/chats")
    def chats_create(body: ChatCreateBody, novel: str = "") -> JSONResponse:
        allowed = set(AGENT_PRESETS) | {"chat"}
        if body.agent not in allowed:
            from fastapi import HTTPException

            raise HTTPException(400, f"未知智能体：{body.agent}")
        chat = {
            "id": "c-" + uuid.uuid4().hex[:10],
            "agent": body.agent,
            "title": body.title.strip(),
            "created": time.time(),
            "messages": [],
        }
        _save_chat(novel, chat)
        return JSONResponse({"ok": True, "chat": chat})

    @app.get("/api/chats/{cid}")
    def chats_get(cid: str, novel: str = "") -> JSONResponse:
        return JSONResponse(_load_chat(novel, cid))

    @app.delete("/api/chats/{cid}")
    def chats_delete(cid: str, novel: str = "") -> JSONResponse:
        path = _chat_dir(novel) / f"{cid}.json"
        if path.exists():
            path.unlink()
        return JSONResponse({"ok": True})

    @app.post("/api/chats/{cid}/send")
    def chats_send(cid: str, body: ChatSendBody, novel: str = "") -> JSONResponse:
        """主智能体编排：路由决策 →（需要时）委派子智能体 → 汇总呈现。"""
        if not body.message.strip():
            from fastapi import HTTPException

            raise HTTPException(400, "消息不能为空")
        chat = _load_chat(novel, cid)
        user_msg = body.message.strip()
        chat["messages"].append({"role": "user", "content": user_msg, "ts": time.time()})

        context = _book_context(novel)
        if body.target:
            focus = _focus_doc(novel, body.target)
            if focus:
                context += (
                    "\n\n【主上下文（用户正在查看的文档）】\n" + focus[:3500]
                )

        registry = None
        delegations: list[dict] = []
        try:
            from src.config.settings import load_models_config
            from src.llm.base import ChatMessage
            from src.llm.registry import ModelRegistry

            registry = ModelRegistry(load_models_config())
            from src.web.inkforge_windows import agent_prompt

            def _role(preferred: str) -> str:
                # 角色对齐：子智能体优先用同名角色，未配置时回退 chat/master
                if preferred in registry.roles:
                    return preferred
                if "chat" in registry.roles:
                    return "chat"
                return registry.roles[0] if registry.roles else "chat"

            history = chat["messages"][-10:]
            prior = [ChatMessage(role=m["role"], content=m["content"]) for m in history[:-1]]

            # ── 1. 路由：主智能体判断是否委派 ──
            route_raw = registry.chat_as(
                _role("master"),
                [
                    ChatMessage(role="system", content=ROUTER_PROMPT),
                    ChatMessage(
                        role="user",
                        content=(
                            f"【作品上下文摘要】\n{context[:2200]}\n\n【用户消息】\n{user_msg}"
                        ),
                    ),
                ],
                json_mode=True,
                temperature=0.1,
            ).content
            decision = _parse_json_object(route_raw)
            target = decision.get("delegate")
            if target not in AGENT_PRESETS or target == "master":
                target = None
            instruction = str(decision.get("instruction") or user_msg)[:600]

            if target:
                # ── 2. 委派子智能体执行 ──
                label = AGENT_PRESETS[target]["label"]
                sub_system = (
                    f"{agent_prompt(target)}\n\n"
                    "以下是当前作品的实时事实源摘录，产出必须与其保持一致：\n\n"
                    f"{context}"
                )
                sub_messages = [ChatMessage(role="system", content=sub_system), *prior]
                sub_messages.append(ChatMessage(role="user", content=instruction))
                sub_out = registry.chat_as(_role(target), sub_messages, temperature=0.7).content
                delegations.append(
                    {
                        "agent": target,
                        "label": label,
                        "instruction": instruction,
                        "output": sub_out,
                    }
                )
                logger.info("已委派 %s：%.60s", label, instruction)

                # ── 3. 主智能体汇总呈现 ──
                synth_messages = [
                    ChatMessage(
                        role="system",
                        content=MASTER_SYNTH_PROMPT + "\n\n【作品上下文】\n" + context[:2600],
                    ),
                    *prior,
                    ChatMessage(role="user", content=user_msg),
                    ChatMessage(
                        role="system",
                        content=f"子智能体「{label}」的任务产出：\n{sub_out}",
                    ),
                ]
                reply = registry.chat_as(_role("master"), synth_messages, temperature=0.6).content
            else:
                # ── 主智能体直接作答 ──
                master_messages = [
                    ChatMessage(
                        role="system",
                        content=MASTER_PROMPT + "\n\n【作品上下文】\n" + context[:3200],
                    ),
                    *prior,
                    ChatMessage(role="user", content=user_msg),
                ]
                reply = registry.chat_as(_role("master"), master_messages, temperature=0.8).content
        except Exception as exc:  # noqa: BLE001 - 模型错误透传给前端
            logger.exception("主智能体编排失败")
            from fastapi import HTTPException

            raise HTTPException(502, f"模型调用失败：{exc}")

        assistant_msg: dict = {"role": "assistant", "content": reply, "ts": time.time()}
        if delegations:
            assistant_msg["delegations"] = delegations
        chat["messages"].append(assistant_msg)
        if not chat.get("title"):
            chat["title"] = user_msg[:20]
        _save_chat(novel, chat)
        return JSONResponse({"ok": True, "reply": reply, "delegations": delegations})

    # ══════════ 风格工坊：技能绑定 ══════════

    def _pack_digest(pack_id: str) -> str:
        from src.distillation.skill_store import load_manifest, load_report

        manifest = load_manifest(pack_id)
        report = load_report(pack_id).model_dump()
        lines = [f"### 蒸馏技能包：{manifest.get('book_title', pack_id)}（v{manifest.get('version', '')}）"]
        for key, value in report.items():
            if key in {"book_title", "total_chunks", "merged_at", "provider", "model"} or not value:
                continue
            text = json.dumps(value, ensure_ascii=False, indent=0)
            text = re.sub(r"\s+", " ", text)
            lines.append(f"- **{key}**：{text[:600]}")
        return "\n".join(lines)

    @app.get("/api/bindings")
    def bindings_get(novel: str = "") -> JSONResponse:
        store = _store(novel)
        bound_custom: list[str] = []
        bound_packs: list[str] = []
        if store.exists("settings/custom-skills.md"):
            meta = store.read("settings/custom-skills.md").metadata
            bound_custom = list(meta.get("bound_custom") or [])
            bound_packs = list(meta.get("bound_packs") or [])
        return JSONResponse({"bound_custom": bound_custom, "bound_packs": bound_packs})

    @app.post("/api/bindings")
    def bindings_set(body: BindingsBody, novel: str = "") -> JSONResponse:
        """把自定义约束 + 蒸馏技能包摘要合成 settings/custom-skills.md。"""
        store = _store(novel, writable=True)
        sections: list[str] = ["# 创作约束（风格工坊绑定，最高优先级）"]
        for sid in body.custom_skill_ids:
            path = get_settings().novels_dir.parent / "custom_skills" / f"{sid}.md"
            if not path.exists():
                from fastapi import HTTPException

                raise HTTPException(404, f"自定义 Skill 不存在：{sid}")
            post = frontmatter.load(str(path))
            sections.append(f"## 自定义约束：{post.metadata.get('title', sid)}\n\n{post.content}")
        for pid in body.pack_ids:
            try:
                sections.append(_pack_digest(pid))
            except FileNotFoundError as exc:
                from fastapi import HTTPException

                raise HTTPException(404, str(exc))
        content = "\n\n".join(sections)
        store.write(
            "settings/custom-skills.md",
            content,
            metadata={"bound_custom": body.custom_skill_ids, "bound_packs": body.pack_ids},
            commit_message="风格工坊：更新技能绑定",
        )
        return JSONResponse({"ok": True, "bound_custom": body.custom_skill_ids, "bound_packs": body.pack_ids})

    logger.info("Inkforge 扩展 API 已注册（chat/settings/chapter/bindings）")
