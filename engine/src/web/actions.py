"""墨师动作层（P2/P3）：把"墨师能办事"落在白名单化的动作注册表 + 执行器上。

设计基线（用户已确认）：
- **所有写动作一律需用户确认**：先产出 ``PendingAction``（含参数与影响预览）落盘到
  会话，用户显式确认后才真正执行；只读动作免确认。
- **单一实现源**：动作 handler 直接复用 ``src/services/*`` 与既有会话对象，
  不复制端点逻辑。
- **跨书保护**：动作默认只能操作"会话所属的当前书"；操作别的书必须显式声明
  ``novel``，且写动作总是要求确认。

动作协议（复用既有 json_mode 范式，与 ROUTER_PROMPT 同族）：
    {"actions": [{"op": "book_list", "args": {}}, ...], "reply_hint": "..."}

执行结果回灌给墨师做汇总：
    {"results": [{"op": ..., "ok": true, "status": "ok|pending_confirm|failed",
                  "summary": "自然语言回执", "data": {...}}]}
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import frontmatter

from src.config.settings import get_settings
from src.services import library
from src.services.workspace import invalidate as invalidate_workspace_index
from src.utils.logger import get_logger
from src.web.scope import WORKSPACE

logger = get_logger(__name__)

#: 单轮用户消息内允许的动作数上限（防止墨师一口气排一长串危险操作）
MAX_ACTIONS_PER_TURN = 6

#: 会话内"待确认动作"字段名
PENDING_KEY = "pending_action"

#: 书内设定文档相对路径白名单（与 /api/settings/doc 同源规则）
REL_RE = re.compile(r"^settings/[A-Za-z0-9_\-/\u4e00-\u9fff]+\.md$")

#: 审计日志（每行一条 JSON）
AUDIT_LOG = "actions.log"
AUDIT_KEEP_LINES = 500


# ---------- 数据结构 ----------

@dataclass(frozen=True)
class Action:
    """一个动作的声明（注册表元素）。"""

    op: str
    scope: str                      # "read" | "write"
    desc: str
    args: tuple[str, ...] = ()
    handler: Callable[..., Any] = None  # type: ignore[assignment]

    @property
    def is_write(self) -> bool:
        return self.scope == "write"


@dataclass
class ActionExec:
    """动作执行结果。"""

    op: str
    ok: bool
    status: str                     # ok | pending_confirm | failed
    summary: str = ""
    data: Any = None
    pending: dict | None = None
    error: str = ""
    #: 真正生效的（规范化后的）参数。预览登记与确认比对都以此为准，
    #: 避免"模型写 free、系统折算成 pipeline"造成两边参数不相等而拒绝确认。
    args: dict = field(default_factory=dict)


@dataclass
class ActionContext:
    """执行器依赖（由 web 层注入，动作层不 import web）。"""

    hub: Any
    default_novel: str
    active_novel: Callable[[], str]
    session_key: str = ""           # 发起动作的会话维度（书 id 或 __workspace__）
    book: str = ""                  # 本次会话的当前书（工作区下可能为空）
    extra: dict = field(default_factory=dict)


class ActionError(Exception):
    """动作参数非法（会被翻译成 status=failed 的回执，而不是 500）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------- 审计 ----------

def audit_path() -> Path:
    d = get_settings().runtime_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / AUDIT_LOG


def _digest(value: Any) -> str:
    """参数摘要（只记指纹，不落明文，避免密钥/长文本进日志）。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def write_audit(record: dict) -> None:
    """追加一条审计记录（失败只告警，绝不影响动作本身）。"""
    try:
        path = audit_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        _trim_audit(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("动作审计写入失败: %s", exc)


def _trim_audit(path: Path) -> None:
    """审计日志裁剪：只保留最近 AUDIT_KEEP_LINES 行。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > AUDIT_KEEP_LINES:
            path.write_text("\n".join(lines[-AUDIT_KEEP_LINES:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - 裁剪失败无伤大雅
        pass


def read_audit(limit: int = 100) -> list[dict]:
    """读取最近的动作审计记录（倒序）。"""
    path = audit_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001 - 单行损坏跳过
            continue
    out.reverse()
    return out


# ---------- 参数工具 ----------

def _arg_str(args: dict, key: str, *, required: bool = True, default: str = "") -> str:
    value = str(args.get(key, default) or "").strip()
    if required and not value:
        raise ActionError(f"缺少参数 {key}")
    return value


def _arg_int(args: dict, key: str, *, required: bool = True, default: int = 0) -> int:
    raw = args.get(key, default)
    if raw in (None, ""):
        if required:
            raise ActionError(f"缺少参数 {key}")
        return default
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ActionError(f"参数 {key} 必须是整数：{raw!r}") from exc


def resolve_book(ctx: ActionContext, args: dict, *, required: bool = True) -> str:
    """解析动作目标书优先级：显式 novel > 会话当前书 > 工作台当前书 > 唯一书目。

    · 工作区会话来操作某本书时，有两种写法：显式给 ``novel``，或先用 ``book_select`` 切到它。
    · **单书工作区**（系统里只有一本书）自动落到那本书上——实测模型在只有一本书时
      常省略 ``novel``，此时报错会让用户白等一轮；只有一个候选就没有歧义。
    · 多书且都没指定时才报错——**绝不**静默落到默认书（否则会改错书）。
    """
    nid = str(args.get("novel") or "").strip()
    if nid and nid != WORKSPACE:
        return library.validate_novel_id(nid)
    for candidate in (ctx.book, ctx.active_novel()):
        if candidate and candidate != WORKSPACE:
            return candidate
    if not required:
        return ""
    only = _sole_book()
    if only:
        return only
    raise ActionError("未指定书目：请先说明操作哪本书（或用 book_select 切换当前书目）")


def _sole_book() -> str:
    """系统里唯一的书目 ID（0 本或 ≥2 本时返回空串）。"""
    try:
        novels_dir = get_settings().novels_dir
        if not novels_dir.is_dir():
            return ""
        names = [p.name for p in novels_dir.iterdir()
                 if p.is_dir() and library.NOVEL_ID_RE.match(p.name)]
    except OSError:
        return ""
    return names[0] if len(names) == 1 else ""


def _require_book_exists(nid: str) -> None:
    if not (get_settings().novels_dir / nid).is_dir():
        raise ActionError(f"书 {nid} 不存在", status=404)


def _load_store(nid: str, writable: bool = False):
    from src.memory.store_factory import open_store

    return open_store(get_settings().novels_dir / nid, writable=writable)


def _session(ctx: ActionContext, nid: str):
    """取该书生成会话（校验书名校验规则由 hub 负责）。"""
    try:
        return ctx.hub.get_or_create(nid)
    except ValueError as exc:
        raise ActionError(str(exc)) from exc


def _interactive_session(ctx: ActionContext, nid: str):
    try:
        return ctx.hub.get_or_create_interactive(nid)
    except ValueError as exc:
        raise ActionError(str(exc)) from exc


def _is_interactive(nid: str) -> bool:
    return (get_settings().novels_dir / nid / "interactive").is_dir()


# ---------- 只读动作 ----------

def _h_book_list(ctx: ActionContext, args: dict) -> ActionExec:
    items = library.list_books(ctx.default_novel, ctx.hub.active_ids(), ctx.hub.done_ids())
    lines = []
    for b in items:
        flags = []
        if b["is_default"]:
            flags.append("默认")
        if b["active"]:
            flags.append("生成中")
        if b["interactive"]:
            flags.append("互动模式")
        if b["finished"]:
            flags.append("已完结")
        flags.append(f"{b['approved']}/{b['planned']}章" if b["planned"] else f"{b['chapters']}章")
        lines.append(f"- {b['novel_id']}：《{b['title']}》（{'、'.join(flags)}）")
    summary = f"共 {len(items)} 本书：\n" + "\n".join(lines) if items else "书架为空。"
    return ActionExec("book_list", True, "ok", summary, {"books": items})


def _h_book_stat(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    store = _load_store(nid)
    chapters = store.list_chapters()
    approved = [c for c in chapters if c.metadata.get("status") == "approved"]
    scores = [c.metadata.get("score") for c in approved if c.metadata.get("score")]
    first_pass = sum(1 for c in approved if c.metadata.get("first_review_passed"))
    fs = {"total": 0, "resolved": 0}
    if store.exists("settings/foreshadowing.md"):
        items = store.read("settings/foreshadowing.md").metadata.get("items", []) or []
        fs["total"] = len(items)
        fs["resolved"] = sum(1 for i in items if i.get("status") == "resolved")
    data = {
        "novel": nid,
        "chapters": len(chapters),
        "approved": len(approved),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "first_pass_rate": round(first_pass / len(approved) * 100, 1) if approved else None,
        "foreshadow_total": fs["total"],
        "foreshadow_resolved": fs["resolved"],
    }
    summary = (
        f"《{nid}》共 {data['chapters']} 章、已定稿 {data['approved']} 章；"
        f"平均分 {data['avg_score']}；首稿通过率 {data['first_pass_rate']}%；"
        f"伏笔 {fs['resolved']}/{fs['total']} 已回收。"
    )
    return ActionExec("book_stat", True, "ok", summary, data)


def _h_doc_list(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    store = _load_store(nid)
    root = store.root / "settings"
    items = []
    if root.exists():
        for path in sorted(root.rglob("*.md")):
            rel = path.relative_to(store.root).as_posix()
            try:
                meta = store.read(rel).metadata
            except Exception:  # noqa: BLE001
                meta = {}
            items.append({"rel": rel, "title": str(meta.get("title") or path.stem)})
    summary = "设定文档：" + ("、".join(f"{i['title']}({i['rel']})" for i in items) or "（无）")
    return ActionExec("doc_list", True, "ok", summary, {"novel": nid, "items": items})


def _h_doc_read(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    rel = _arg_str(args, "rel")
    if not REL_RE.match(rel):
        raise ActionError(f"非法文档路径：{rel!r}（仅允许 settings/**/*.md）")
    store = _load_store(nid)
    if not store.exists(rel):
        raise ActionError(f"文档不存在：{rel}", status=404)
    content = store.read(rel).content
    return ActionExec(
        "doc_read", True, "ok",
        f"《{nid}》{rel} 正文如下。",
        {"novel": nid, "rel": rel, "content": content},
    )


def _h_chapter_list(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    store = _load_store(nid)
    items = [
        {
            "chapter": c.metadata.get("chapter"),
            "title": c.metadata.get("title", ""),
            "status": c.metadata.get("status", ""),
            "words": len(c.content),
        }
        for c in sorted(store.list_chapters(), key=lambda d: d.metadata.get("chapter", 0))
    ]
    summary = (
        f"《{nid}》{len(items)} 章：" +
        "、".join(f"第{i['chapter']}章{i['title']}[{i['status']}]" for i in items[:20]) +
        ("…" if len(items) > 20 else "")
    )
    return ActionExec("chapter_list", True, "ok", summary, {"novel": nid, "chapters": items})


def _h_chapter_read(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    want = _arg_int(args, "chapter")
    store = _load_store(nid)
    for doc in store.list_chapters():
        if doc.metadata.get("chapter") == want:
            return ActionExec(
                "chapter_read", True, "ok",
                f"《{nid}》第 {want} 章正文如下。",
                {"novel": nid, "chapter": want,
                 "title": doc.metadata.get("title", ""),
                 "status": doc.metadata.get("status", ""),
                 "content": doc.content},
            )
    raise ActionError(f"《{nid}》第 {want} 章不存在", status=404)


def _h_outline_read(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    store = _load_store(nid)
    if not store.exists("settings/outline.md"):
        raise ActionError(f"《{nid}》尚无大纲（可能还没生成设定/大纲）", status=404)
    doc = store.read("settings/outline.md")
    return ActionExec(
        "outline_read", True, "ok", f"《{nid}》大纲如下。",
        {"novel": nid, "title": str(doc.metadata.get("title") or ""),
         "theme": str(doc.metadata.get("theme") or ""), "content": doc.content,
         "volumes": doc.metadata.get("volumes") or []},
    )


def _h_search_workspace(ctx: ActionContext, args: dict) -> ActionExec:
    """跨书检索（书名 / 章节标题 / 设定文档名）——不走向量，纯标题级索引检索。"""
    keyword = _arg_str(args, "keyword")
    hits: list[dict] = []
    novels_dir = get_settings().novels_dir
    if novels_dir.exists():
        for book in sorted(p for p in novels_dir.iterdir() if p.is_dir()):
            store = _load_store(book.name)
            planned = book.name.lower().find(keyword.lower()) >= 0
            if planned:
                hits.append({"novel": book.name, "kind": "book", "title": book.name})
            for doc in store.list_chapters():
                title = str(doc.metadata.get("title", ""))
                if keyword.lower() in title.lower():
                    hits.append({"novel": book.name, "kind": "chapter",
                                 "chapter": doc.metadata.get("chapter"), "title": title})
            sroot = store.root / "settings"
            if sroot.exists():
                for path in sorted(sroot.rglob("*.md")):
                    if keyword.lower() in path.stem.lower():
                        hits.append({"novel": book.name, "kind": "setting",
                                     "rel": path.relative_to(store.root).as_posix(),
                                     "title": path.stem})
    summary = (
        f"命中 {len(hits)} 条：" +
        "；".join(f"{h['novel']}/{h.get('title')}" for h in hits[:15]) +
        ("…" if len(hits) > 15 else "")
    ) if hits else f"没有找到包含「{keyword}」的书名/章节/设定。"
    return ActionExec("search_workspace", True, "ok", summary, {"keyword": keyword, "hits": hits})


def _h_material_list(ctx: ActionContext, args: dict) -> ActionExec:
    d = library.novels_root().parent / "materials"
    items = []
    if d.exists():
        for path in sorted(d.glob("*.md")):
            post = frontmatter.load(str(path))
            items.append({"id": path.stem, "title": str(post.metadata.get("title") or path.stem)})
    return ActionExec("material_list", True, "ok",
                      f"素材库 {len(items)} 条：" +
                      ("、".join(i["title"] for i in items) or "（空）"),
                      {"materials": items})


def _h_material_read(ctx: ActionContext, args: dict) -> ActionExec:
    """读素材正文：list 只能给标题，用户问"某条素材讲了什么"必须能取到内容。"""
    mid = _arg_str(args, "id")
    if not re.match(r"^mt-[a-f0-9]{8}$", mid):
        raise ActionError(f"非法素材 ID：{mid!r}", status=400)
    path = library.novels_root().parent / "materials" / f"{mid}.md"
    if not path.exists():
        raise ActionError(f"素材不存在：{mid}", status=404)
    post = frontmatter.load(str(path))
    return ActionExec("material_read", True, "ok",
                      f"素材《{post.metadata.get('title', mid)}》正文如下。",
                      {"id": mid, "title": str(post.metadata.get("title") or mid),
                       "content": post.content})


def _h_learning_list(ctx: ActionContext, args: dict) -> ActionExec:
    """列出学习仿写的历史成果（三阶段分析报告）。

    实测缺口：用户问"找出学习仿写里的历史内容"，墨师手上没有这个动作，
    只能回答"我查不到"——功能端点是有的（GET /api/learning），但没进动作清单。
    """
    d = library.novels_root().parent / "learning"
    items: list[dict] = []
    if d.exists():
        for path in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                post = frontmatter.load(str(path))
            except Exception:  # noqa: BLE001 - 单文件损坏跳过
                continue
            items.append({
                "id": path.stem,
                "title": str(post.metadata.get("title") or path.stem),
                "created": path.stat().st_mtime,
            })
    summary = (f"学习仿写历史 {len(items)} 条：" +
               "；".join(f"{i['title']}（{i['id']}）" for i in items)) if items \
        else "学习仿写历史为空：还没有做过样本分析（可在「学习仿写」页粘贴样本生成）。"
    return ActionExec("learning_list", True, "ok", summary, {"items": items})


def _h_learning_read(ctx: ActionContext, args: dict) -> ActionExec:
    lid = _arg_str(args, "id")
    if not re.match(r"^ln-[a-f0-9]{8}$", lid):
        raise ActionError(f"非法学习成果 ID：{lid!r}", status=400)
    path = library.novels_root().parent / "learning" / f"{lid}.md"
    if not path.exists():
        raise ActionError(f"学习成果不存在：{lid}", status=404)
    post = frontmatter.load(str(path))
    return ActionExec("learning_read", True, "ok",
                      f"学习仿写《{post.metadata.get('title', lid)}》全文如下。",
                      {"id": lid, "title": str(post.metadata.get("title") or lid),
                       "content": post.content})


def _h_skill_list(ctx: ActionContext, args: dict) -> ActionExec:
    from src.distillation.skill_store import load_index

    packs = load_index().get("skills") or []
    return ActionExec("skill_list", True, "ok",
                      f"蒸馏技能包 {len(packs)} 个：" +
                      ("、".join(f"{p.get('book_title')}(v{p.get('version')})" for p in packs)
                       or "（空）"),
                      {"skills": packs})


def _h_constraint_list(ctx: ActionContext, args: dict) -> ActionExec:
    items = library.list_custom_skills()
    return ActionExec("constraint_list", True, "ok",
                      f"自定义创作约束 {len(items)} 条：" +
                      ("、".join(i["title"] for i in items) or "（空）"),
                      {"skills": [{"skill_id": i["skill_id"], "title": i["title"]} for i in items]})


def _h_model_config(ctx: ActionContext, args: dict) -> ActionExec:
    from src.config.settings import load_models_config

    cfg = load_models_config()
    roles = getattr(cfg, "roles", {}) or {}
    detail = []
    for name, binding in roles.items():
        provider = getattr(binding, "provider", "")
        model = getattr(binding, "model", "")
        temp = getattr(binding, "temperature", "")
        detail.append(f"- {name}: {provider}/{model} (T={temp})")
    return ActionExec("model_config", True, "ok",
                      f"模型角色 {len(detail)} 个：\n" + "\n".join(detail),
                      {"roles": [{"role": r} for r in roles]})


# ---------- 写动作（一律先确认） ----------

def _normalize_mode(raw: str) -> str:
    """把模型写出的创作模式折算到规范值（pipeline / interactive）。

    实测模型会自造 "free"、"自由创作"、"互动模式" 这类说法；它们语义明确，
    直接失败让用户白等一轮没有意义，因此在**动作层**统一折算（与 op 别名同类）。
    """
    value = (raw or "").strip().lower()
    if value in library.CREATION_MODES:
        return value
    if any(k in value for k in ("interactive", "互动", "剧情卡", "逐章")):
        return "interactive"
    if value in ("", "pipeline", "free", "自由", "自由创作", "流水线", "大纲", "默认"):
        return "pipeline"
    if any(k in value for k in ("自由", "流水", "大纲", "pipeline")):
        return "pipeline"
    return value


def _preview_book_create(ctx: ActionContext, args: dict) -> str:
    nid = library.validate_novel_id(_arg_str(args, "novel_id"))
    mode = _normalize_mode(_arg_str(args, "mode", required=False, default="pipeline"))
    if mode not in library.CREATION_MODES:
        raise ActionError(f"未知创作模式：{mode!r}（pipeline / interactive）")
    if library.book_exists(nid):
        raise ActionError(f"书 {nid} 已存在", status=409)
    label = "自由创作（大纲→章节流水线）" if mode == "pipeline" else "互动创作（剧情卡逐章推进）"
    return f"将新建书目 {nid}，创作模式：{label}；会创建 data/novels/{nid}/ 目录骨架。"


def _run_book_create(ctx: ActionContext, args: dict) -> ActionExec:
    nid = library.create_book(
        _arg_str(args, "novel_id"),
        _normalize_mode(_arg_str(args, "mode", required=False, default="pipeline")),
    )
    try:
        ctx.hub.get_or_create(nid)
    except ValueError:  # 理论上不会发生（已过校验）
        pass
    invalidate_workspace_index()
    ctx.extra["created_book"] = nid
    return ActionExec("book_create", True, "ok", f"已创建书目 {nid}。",
                      {"novel": nid, "undo": {"op": "book_delete", "args": {"novel": nid}}})


def _preview_book_select(ctx: ActionContext, args: dict) -> str:
    nid = library.validate_novel_id(_arg_str(args, "novel_id"))
    _require_book_exists(nid)
    return f"将把工作台当前书目切换为 {nid}。"


def _run_book_select(ctx: ActionContext, args: dict) -> ActionExec:
    nid = library.validate_novel_id(_arg_str(args, "novel_id"))
    _require_book_exists(nid)
    ctx.extra["select_book"] = nid
    return ActionExec("book_select", True, "ok", f"已切换到《{nid}》。", {"novel": nid})


def _preview_book_delete(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    if nid == ctx.default_novel:
        raise ActionError(f"书 {nid} 是默认书，不可删除", status=409)
    if nid in ctx.hub.active_ids():
        raise ActionError(f"书 {nid} 正在生成中，不能删除", status=409)
    _require_book_exists(nid)
    return (f"⚠ 将删除书目 {nid} 的全部数据（章节/设定/摘要/伏笔/写作历史），"
            "并清理向量索引与断点记录。此操作不可撤销。")


def _run_book_delete(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    warns = library.delete_book(nid, ctx.default_novel, ctx.hub.active_ids(),
                                remove_session=ctx.hub.remove)
    invalidate_workspace_index()
    return ActionExec("book_delete", True, "ok",
                      f"已删除书目 {nid}。" + (f"（告警：{'; '.join(warns)}）" if warns else ""),
                      {"novel": nid})


def _preview_bind_skills(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    custom = args.get("custom_skill_ids") or []
    packs = args.get("pack_ids") or []
    return (f"将把 {len(custom)} 条自定义约束、{len(packs)} 个蒸馏技能包绑定到《{nid}》，"
            "重写该书 settings/custom-skills.md（最高优先级创作约束）。")


def _run_bind_skills(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    custom = [str(x) for x in (args.get("custom_skill_ids") or [])]
    packs = [str(x) for x in (args.get("pack_ids") or [])]

    sections = ["# 创作约束（风格工坊绑定，最高优先级）"]
    by_id = {s["skill_id"]: s for s in library.list_custom_skills()}
    for sid in custom:
        skill = by_id.get(sid)
        if skill is None:
            raise ActionError(f"自定义约束不存在：{sid}", status=404)
        sections.append(f"## 自定义约束：{skill['title']}\n\n{skill['content']}")
    from src.distillation.skill_store import load_manifest, load_report
    from src.web.inkforge_api import _pack_digest

    for pid in packs:
        try:
            load_manifest(pid)
            load_report(pid)
        except FileNotFoundError as exc:
            raise ActionError(f"技能包不存在：{pid}", status=404) from exc
        sections.append(_pack_digest(pid))

    store = _load_store(nid, writable=True)
    store.write(
        "settings/custom-skills.md",
        "\n\n".join(sections),
        metadata={"bound_custom": custom, "bound_packs": packs},
        commit_message="墨师动作：更新技能绑定",
    )
    return ActionExec("book_bind_skills", True, "ok",
                      f"已把 {len(custom)} 条约束、{len(packs)} 个技能包绑定到《{nid}》。",
                      {"novel": nid})


def _preview_gen_start(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    sess = _session(ctx, nid)
    snap = sess.snapshot()
    if snap.get("started") and not snap.get("done"):
        raise ActionError(f"《{nid}》正在生成中，不能重复启动", status=409)
    brief = _arg_str(args, "brief")
    chapters = _arg_int(args, "chapters", required=False, default=0)
    parallel = bool(args.get("parallel"))
    return (f"将启动《{nid}》的正式生成：目标 {chapters or '（按大纲）'} 章，"
            f"{'卷级并行' if parallel else '串行流水线'}；"
            f"创作需求摘要：{brief[:80]}…。生成会停在每一章的人审关卡等你确认。")


def _run_gen_start(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    sess = _session(ctx, nid)
    skill_ids = [str(x) for x in (args.get("skill_ids") or [])]
    sess.start(
        _arg_str(args, "brief"),
        _arg_int(args, "chapters", required=False, default=0),
        parallel=bool(args.get("parallel")),
        workers=_arg_int(args, "workers", required=False, default=0),
        skill_ids=skill_ids,
    )
    return ActionExec("gen_start", True, "ok",
                      f"《{nid}》生成已启动，将在每章人审关卡暂停。", {"novel": nid})


def _preview_gen_resume(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    return f"将从断点续跑《{nid}》的生成（已定稿章节不会重写）。"


def _run_gen_resume(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    _session(ctx, nid).resume_existing(
        parallel=bool(args.get("parallel")),
        workers=_arg_int(args, "workers", required=False, default=0),
    )
    return ActionExec("gen_resume", True, "ok", f"《{nid}》已从断点续跑。", {"novel": nid})


def _preview_gen_pause(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    return f"将暂停《{nid}》的生成（已写出的章节保留，可随时续跑）。"


def _run_gen_pause(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    result = _session(ctx, nid).request_pause()
    return ActionExec("gen_pause", True, "ok", f"《{nid}》已请求暂停。", result)


def _preview_gen_decide(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    decision = _arg_str(args, "action")
    if decision not in ("approve", "reject"):
        raise ActionError("action 只能是 approve 或 reject")
    snap = _session(ctx, nid).snapshot()
    pending = snap.get("pending") or {}
    if not pending:
        raise ActionError(f"《{nid}》当前没有待裁决项", status=409)
    if decision == "approve":
        return f"将【通过】《{nid}》的待审项（{pending.get('type')}），通过即定稿入库。"
    return (f"将【打回】《{nid}》的待审项并携带意见："
            f"{_arg_str(args, 'feedback', required=False)[:80]}…")


def _run_gen_decide(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    decision = _arg_str(args, "action")
    payload = {"action": decision}
    if decision == "reject":
        payload["feedback"] = _arg_str(args, "feedback")
        payload["revision_mode"] = str(args.get("revision_mode") or "targeted")
    _session(ctx, nid).submit_decision(payload)
    verb = "通过" if decision == "approve" else "打回"
    return ActionExec("gen_decide", True, "ok", f"《{nid}》待审项已{verb}。", {"novel": nid})


def _preview_demo_run(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    return (f"将为《{nid}》生成设定 Demo（世界观/人物/梗概/主题），"
            f"约 {_arg_int(args, 'chapters', required=False, default=0) or '（默认）'} 章规模；"
            "生成后需要你审核确认才会写入资料库。")


def _run_demo_run(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    skill_ids = [str(x) for x in (args.get("skill_ids") or [])]
    _session(ctx, nid).start_demo(
        _arg_str(args, "brief"),
        _arg_int(args, "chapters", required=False, default=0),
        _arg_str(args, "feedback", required=False),
        skill_ids,
    )
    return ActionExec("demo_run", True, "ok", f"《{nid}》设定 Demo 生成中。", {"novel": nid})


def _preview_demo_confirm(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    snap = _session(ctx, nid).demo_snapshot()
    if snap.get("status") != "done":
        raise ActionError(f"《{nid}》没有待确认的设定 Demo（当前状态：{snap.get('status')}）",
                          status=409)
    demo = snap.get("demo") or {}
    return (f"将把《{nid}》的设定 Demo 写入资料库：书名《{demo.get('book_title', '')}》，"
            "写入后世界观与人物成为后续所有章节的事实源。")


def _run_demo_confirm(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    info = _session(ctx, nid).confirm_demo()
    return ActionExec("demo_confirm", True, "ok",
                      f"《{nid}》设定已入库（书名《{info.get('book_title', '')}》）。",
                      {"novel": nid, **info})


def _preview_interactive_start(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    _require_book_exists(nid)
    if not _is_interactive(nid):
        raise ActionError(f"《{nid}》是自由创作模式，互动创作只对建书时选「互动创作」的书开放",
                          status=409)
    return f"将启动《{nid}》的互动创作：为下一章生成 3 张剧情卡供你选择。"


def _run_interactive_start(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    _interactive_session(ctx, nid).start()
    return ActionExec("interactive_start", True, "ok", f"《{nid}》互动创作已启动，正在出卡。",
                      {"novel": nid})


def _preview_interactive_choose(ctx: ActionContext, args: dict) -> str:
    nid = resolve_book(ctx, args)
    card = _arg_str(args, "card_id")
    if card == "custom":
        text = _arg_str(args, "custom_text")
        return f"将按你自拟的剧情卡写《{nid}》下一章：{text[:80]}…"
    return f"将按剧情卡 {card} 写《{nid}》下一章（写完后进入人审）。"


def _run_interactive_choose(ctx: ActionContext, args: dict) -> ActionExec:
    nid = resolve_book(ctx, args)
    card = _arg_str(args, "card_id")
    text = "" if card != "custom" else _arg_str(args, "custom_text")
    target = _arg_int(args, "target_words", required=False, default=0) or None
    _interactive_session(ctx, nid).choose(card, text, target_words=target)
    return ActionExec("interactive_choose", True, "ok", f"《{nid}》第 {card} 卡已选定，正在写章。",
                      {"novel": nid})


def _preview_material_create(ctx: ActionContext, args: dict) -> str:
    title = _arg_str(args, "title")
    return f"将在素材库新增条目《{title}》（{len(_arg_str(args, 'content'))} 字）。"


def _run_material_create(ctx: ActionContext, args: dict) -> ActionExec:
    import uuid as _uuid

    title = _arg_str(args, "title")
    content = _arg_str(args, "content")
    d = library.novels_root().parent / "materials"
    d.mkdir(parents=True, exist_ok=True)
    mid = "mt-" + _uuid.uuid4().hex[:8]
    (d / f"{mid}.md").write_text(
        frontmatter.dumps(frontmatter.Post(content, title=title)),
        encoding="utf-8", newline="\n",
    )
    return ActionExec("material_create", True, "ok", f"素材《{title}》已入库。", {"id": mid})


def _preview_constraint_create(ctx: ActionContext, args: dict) -> str:
    title = _arg_str(args, "title")
    return (f"将新增自定义创作约束《{title}》，并写入全局约束库 "
            "data/custom_skills/（建书或绑定时可选用）。")


def _run_constraint_create(ctx: ActionContext, args: dict) -> ActionExec:
    import hashlib as _hashlib

    title = _arg_str(args, "title")
    content = _arg_str(args, "content")
    sid = "sk-" + _hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
    path = library.custom_skills_dir() / f"{sid}.md"
    if path.exists():
        raise ActionError(f"同名约束已存在：{title}（{sid}）", status=409)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(content, title=title)),
        encoding="utf-8", newline="\n",
    )
    return ActionExec("constraint_create", True, "ok", f"约束《{title}》已入库。",
                      {"skill_id": sid})


# ---------- 注册表 ----------

ACTIONS: dict[str, Action] = {}


def _register(op: str, scope: str, desc: str, args: tuple[str, ...],
              handler: Callable[..., Any]) -> None:
    ACTIONS[op] = Action(op, scope, desc, args, handler)


def _split(op: str, preview: Callable[..., str], run: Callable[..., ActionExec],
           scope: str, desc: str, args: tuple[str, ...]) -> None:
    """写动作注册：执行器指向 _preview_*，真正执行时按 op 查 _RUN 表。"""

    def handler(ctx: ActionContext, a: dict, *, execute: bool = False):
        if execute:
            return run(ctx, a)
        return ActionExec(op, True, "pending_confirm", preview(ctx, a),
                          pending={"op": op, "args": a, "impact": preview(ctx, a)})

    _register(op, scope, desc, args, handler)


_RUN: dict[str, Callable[[ActionContext, dict], ActionExec]] = {}


def _register_read(op: str, desc: str, args: tuple[str, ...],
                   handler: Callable[[ActionContext, dict], ActionExec]) -> None:
    _register(op, "read", desc, args, lambda ctx, a, execute=False: handler(ctx, a))


def _register_write(op: str, desc: str, args: tuple[str, ...],
                    preview: Callable[[ActionContext, dict], str],
                    run: Callable[[ActionContext, dict], ActionExec]) -> None:
    _RUN[op] = run
    _split(op, preview, run, "write", desc, args)


def _bootstrap_registry() -> None:
    """幂等注册全部动作（模块可被重复导入 / 测试重载）。"""
    if ACTIONS:
        return
    _register_read("book_list", "列出全部书目与进度", (), _h_book_list)
    _register_read("book_stat", "某本书的进度/评分/首稿通过率", ("novel",), _h_book_stat)
    _register_read("doc_list", "列出某本书的设定文档", ("novel",), _h_doc_list)
    _register_read("doc_read", "读取某本书的设定文档正文", ("novel", "rel"), _h_doc_read)
    _register_read("chapter_list", "列出某本书的章节", ("novel",), _h_chapter_list)
    _register_read("chapter_read", "读取某本书某章正文", ("novel", "chapter"), _h_chapter_read)
    _register_read("outline_read", "读取某本书的大纲", ("novel",), _h_outline_read)
    _register_read("search_workspace", "跨书检索书名/章节/设定名", ("keyword",),
                   _h_search_workspace)
    _register_read("material_list", "列出素材库条目", (), _h_material_list)
    _register_read("material_read", "读取某条素材的正文", ("id",), _h_material_read)
    _register_read("learning_list", "列出学习仿写的历史成果（三阶段分析）", (),
                   _h_learning_list)
    _register_read("learning_read", "读取某份学习仿写成果的全文", ("id",), _h_learning_read)
    _register_read("skill_list", "列出蒸馏技能包", (), _h_skill_list)
    _register_read("constraint_list", "列出自定义创作约束", (), _h_constraint_list)
    _register_read("model_config", "查看各角色模型绑定", (), _h_model_config)

    _register_write("book_create", "新建书", ("novel_id", "mode"),
                    _preview_book_create, _run_book_create)
    _register_write("book_select", "切换工作台当前书目", ("novel_id",),
                    _preview_book_select, _run_book_select)
    _register_write("book_delete", "删除书目（不可撤销）", ("novel",),
                    _preview_book_delete, _run_book_delete)
    _register_write("book_bind_skills", "绑定约束/技能包到某本书",
                    ("novel", "custom_skill_ids", "pack_ids"),
                    _preview_bind_skills, _run_bind_skills)
    _register_write("gen_start", "启动正式生成",
                    ("novel", "brief", "chapters", "parallel", "workers", "skill_ids"),
                    _preview_gen_start, _run_gen_start)
    _register_write("gen_resume", "从断点续跑", ("novel", "parallel", "workers"),
                    _preview_gen_resume, _run_gen_resume)
    _register_write("gen_pause", "暂停生成", ("novel",),
                    _preview_gen_pause, _run_gen_pause)
    _register_write("gen_decide", "章节人审裁决",
                    ("novel", "action", "feedback", "revision_mode"),
                    _preview_gen_decide, _run_gen_decide)
    _register_write("demo_run", "生成设定 Demo", ("novel", "brief", "chapters", "skill_ids"),
                    _preview_demo_run, _run_demo_run)
    _register_write("demo_confirm", "确认设定 Demo 入库", ("novel",),
                    _preview_demo_confirm, _run_demo_confirm)
    _register_write("interactive_start", "启动互动创作", ("novel",),
                    _preview_interactive_start, _run_interactive_start)
    _register_write("interactive_choose", "选定互动剧情卡",
                    ("novel", "card_id", "custom_text", "target_words"),
                    _preview_interactive_choose, _run_interactive_choose)
    _register_write("material_create", "新增素材库条目", ("title", "content"),
                    _preview_material_create, _run_material_create)
    _register_write("constraint_create", "新增自定义创作约束", ("title", "content"),
                    _preview_constraint_create, _run_constraint_create)


_bootstrap_registry()


#: 动作名别名表：模型很容易写出更"自然"的名字（list_books / read_chapter …）。
#: 实测（真实 DeepSeek 调用）它会把 `book_list` 说成 `list_books`、`chapter_list` 说成
#: `list_chapters`——名字猜错不该让整件事失败，因此做一层**规范化**：
#: 命中别名即换算到规范 op；未命中则照常报"未知动作"（并如实回执，不再静默吞掉）。
OP_ALIASES: dict[str, str] = {
    # 只读
    "list_books": "book_list",
    "books_list": "book_list",
    "list_book": "book_list",
    "list_learning": "learning_list",
    "learning_history": "learning_list",
    "list_study": "learning_list",
    "asset_list": "learning_list",
    "assets_list": "learning_list",
    "list_assets": "learning_list",
    "study_list": "learning_list",
    "read_learning": "learning_read",
    "get_learning": "learning_read",
    "list_material": "material_list",
    "read_material": "material_read",
    "get_material": "material_read",
    "list_chapters": "chapter_list",
    "chapters_list": "chapter_list",
    "read_chapter": "chapter_read",
    "get_chapter": "chapter_read",
    "chapter_get": "chapter_read",
    "read_doc": "doc_read",
    "get_doc": "doc_read",
    "doc_get": "doc_read",
    "read_setting": "doc_read",
    "read_outline": "outline_read",
    "get_outline": "outline_read",
    "outline_get": "outline_read",
    "list_docs": "doc_list",
    "docs_list": "doc_list",
    "stat_book": "book_stat",
    "get_book_stat": "book_stat",
    "list_materials": "material_list",
    "materials_list": "material_list",
    "list_skills": "skill_list",
    "skills_list": "skill_list",
    "list_constraints": "constraint_list",
    "constraints_list": "constraint_list",
    "get_model_config": "model_config",
    "search": "search_workspace",
    # 写
    "create_book": "book_create",
    "new_book": "book_create",
    "select_book": "book_select",
    "switch_book": "book_select",
    "delete_book": "book_delete",
    "remove_book": "book_delete",
    "bind_skills": "book_bind_skills",
    "start_generation": "gen_start",
    "start_generate": "gen_start",
    "resume_generation": "gen_resume",
    "pause_generation": "gen_pause",
    "decide": "gen_decide",
    "arbitrate": "gen_decide",
    "run_demo": "demo_run",
    "confirm_demo": "demo_confirm",
    "start_interactive": "interactive_start",
    "choose_card": "interactive_choose",
    "select_card": "interactive_choose",
    "create_material": "material_create",
    "add_material": "material_create",
    "create_constraint": "constraint_create",
    "add_constraint": "constraint_create",
}


def normalize_op(op: str) -> str:
    """把模型写出的动作名规范化到注册表里的 op（未知名字原样返回）。"""
    value = (op or "").strip()
    if not value:
        return ""
    if value in ACTIONS:
        return value
    lowered = value.lower().replace("-", "_").replace(" ", "_")
    if lowered in ACTIONS:
        return lowered
    if lowered in OP_ALIASES:
        candidate = OP_ALIASES[lowered]
        if candidate in ACTIONS:
            return candidate
    return value


def normalize_args(op: str, args: dict | None) -> dict:
    """把参数折算到规范取值，并**丢弃未声明的多余键**。

    必须在**执行前、且对同一次动作只做一次**：预览时登记的 args 与确认时比对的 args
    都取同一份规范化结果，否则"模型写了 free、系统折算成 pipeline"或"模型自造了
    title 参数"都会让两边不相等，确认被拒（实测踩到过：用户点确认，卡还挂在那里）。

    丢弃未声明键也顺带起到白名单作用：动作只会收到自己声明的参数。
    """
    raw = dict(args or {})
    allowed = ACTIONS[op].args if op in ACTIONS else tuple(raw)
    out = {k: v for k, v in raw.items() if k in allowed}
    if op == "book_create" and "mode" in out:
        out["mode"] = _normalize_mode(str(out.get("mode") or ""))
    return out


def known_ops() -> list[str]:
    return sorted(ACTIONS)


#: 参数取值约束（供提示词/规划调用展示）。取值受限的关键参数必须写在提示词里，
#: 否则模型会自造枚举值（实测把 mode 写成 free / 自由创作），导致动作直接失败。
ARG_CONSTRAINTS: dict[str, str] = {
    "mode": "pipeline|interactive",
    "action": "approve|reject",
    "revision_mode": "targeted|rewrite",
    "card_id": "c1|c2|c3|custom",
    "chapter": "整数章号",
    "novel": "书目标识",
    "novel_id": "书目标识（新建时自定）",
}


def manifest() -> list[dict]:
    """动作清单（供前端/调试与提示词生成；不含内部 handler）。

    ``args_hint`` 把参数与取值约束并列呈现，例如 ``mode(pipeline|interactive)``，
    让模型不必靠猜枚举值。
    """
    out = []
    for a in sorted(ACTIONS.values(), key=lambda x: (x.scope, x.op)):
        hints = [f"{name}({ARG_CONSTRAINTS[name]})" if name in ARG_CONSTRAINTS else name
                 for name in a.args]
        out.append({
            "op": a.op,
            "scope": a.scope,
            "desc": a.desc,
            "args": list(a.args),
            "args_hint": hints,
        })
    return out


# ---------- 执行器 ----------

def execute(op: str, args: dict | None = None, *, ctx: ActionContext,
            execute_write: bool = False) -> ActionExec:
    """执行一个动作。

    execute_write=False（默认）：写动作只产出"待确认"回执，不落任何数据。
    execute_write=True：确认后真正执行（只对写动作有意义）。
    """
    started = time.monotonic()
    canonical = normalize_op(op)
    action = ACTIONS.get(canonical)
    if action is None:
        return _finish(ctx, ActionExec(op, False, "failed",
                                       error=f"未知动作：{op}（可用：{', '.join(known_ops())}）"),
                       args or {}, started)
    op = canonical
    args = normalize_args(op, args)   # 枚举折算只做一次，预览/确认两边一致
    if action.is_write and not execute_write:
        execute_write = False  # 显式声明意图，写动作永远先确认
    try:
        result = action.handler(ctx, args or {}, execute=execute_write)
    except ActionError as exc:
        result = ActionExec(op, False, "failed", error=exc.message)
    except library.LibraryError as exc:
        result = ActionExec(op, False, "failed", error=exc.message)
    except FileNotFoundError as exc:
        result = ActionExec(op, False, "failed", error=str(exc))
    except RuntimeError as exc:
        # 会话状态类错误（已启动/无待审项等）→ 失败而非 500
        result = ActionExec(op, False, "failed", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - 动作失败不能掀翻整轮对话
        logger.exception("动作 %s 执行异常", op)
        result = ActionExec(op, False, "failed", error=f"{type(exc).__name__}: {exc}")
    result.args = args          # 带上规范化后的参数，供预览登记/确认比对使用
    return _finish(ctx, result, args or {}, started)


def _finish(ctx: ActionContext, result: ActionExec, args: dict,
            started: float) -> ActionExec:
    scope = ACTIONS.get(result.op).scope if result.op in ACTIONS else "?"
    base = {
        "ts": time.time(),
        "session": ctx.session_key,
        "book": ctx.book or ctx.active_novel(),
        "op": result.op,
        "scope": scope,
        "args_digest": _digest(args),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    if result.status == "pending_confirm":
        # 待确认也留痕：审计要能回答"墨师提议过什么、用户批了没有"，
        # 只记成功/失败会丢掉整个审批链路（实测排障时正缺这条）。
        write_audit({**base, "status": "pending_confirm", "ok": True,
                     "summary": (result.summary or "")[:200]})
    elif result.ok:
        write_audit({**base, "status": result.status, "ok": True,
                     "summary": (result.summary or "")[:200]})
    else:
        write_audit({**base, "status": "failed", "ok": False,
                     "error": (result.error or "")[:200]})
    return result


def execute_pending(ctx: ActionContext, pending: dict) -> ActionExec:
    """执行一个已确认的待办动作。"""
    return execute(pending.get("op", ""), pending.get("args") or {},
                   ctx=ctx, execute_write=True)


# ---------- 预览登记（P3 加固）：确认必须对应一次真实预览 ----------
# 由来：`/api/actions/run` 若接受任意 confirm=true，就等于有人"声称确认过"即可执行写动作，
# 中间没有"系统确实给过影响说明"的证据。这里把每次预览登记下来，确认时必须命中，
# 保证"确认"永远对应一次用户看得见的预览（对话确认与界面确认走同一套）。

_PREVIEWS: dict[str, dict] = {}
PREVIEW_TTL_SECONDS = 15 * 60


def _preview_key(ctx: ActionContext, op: str) -> str:
    """预览登记键：按"动作作用域"而不是"会话维度"归并。

    为什么不用 ``session_key``（会话维度）：同一个写动作可能从工作区会话发起预览、
    又在书内会话里确认（反之亦然），若按会话维度分会话就会互相找不到预览，
    表现为"确认被拒、要重新预览"——用户看到的是"点了确认没反应"。
    统一用 ``{目标书}::{op}``：预览说的是"对哪本书做什么"，与会话无关。
    """
    target = ctx.book or ctx.active_novel() or ctx.default_novel
    return f"{target}::{op}"


def register_preview(ctx: ActionContext, op: str, args: dict, impact: str,
                     token: str = "") -> dict:
    """登记一次写动作预览，返回可直接回给前端的 pending 结构。"""
    tk = token or hashlib.sha1(
        f"{_preview_key(ctx, op)}::{json.dumps(args, sort_keys=True, default=str)}"
        f"::{time.time()}".encode("utf-8")
    ).hexdigest()[:16]
    _PREVIEWS[_preview_key(ctx, op)] = {
        "token": tk, "args": args, "impact": impact, "at": time.time(),
    }
    return {"op": op, "args": args, "impact": impact, "token": tk}


def consume_preview(ctx: ActionContext, pending: dict) -> str:
    """校验并消费一次预览；返回空串表示通过，否则返回拒绝原因。"""
    key = _preview_key(ctx, pending.get("op", ""))
    record = _PREVIEWS.get(key)
    if record is None:
        logger.info("预览消费失败：没有登记（key=%s）", key)
        return "该写动作没有经过预览登记：请先发起一次预览，确认后才会执行"
    if time.time() - float(record.get("at", 0)) > PREVIEW_TTL_SECONDS:
        _PREVIEWS.pop(key, None)
        logger.info("预览消费失败：已过期（key=%s）", key)
        return "预览已过期（超过 15 分钟），请重新发起一次"
    token = str(pending.get("token") or "")
    if token and token != record["token"]:
        logger.info("预览消费失败：凭证不匹配（key=%s）", key)
        return "预览凭证不匹配：请重新发起一次预览并在同一张卡上确认"
    if (record.get("args") or {}) != (pending.get("args") or {}):
        logger.info("预览消费失败：参数不一致（key=%s）登记=%s 待办=%s", key,
                    json.dumps(record.get("args") or {}, ensure_ascii=False),
                    json.dumps(pending.get("args") or {}, ensure_ascii=False))
        return "待执行参数与预览参数不一致：请重新预览后再确认"
    _PREVIEWS.pop(key, None)
    return ""


def forget_preview(ctx: ActionContext, op: str) -> None:
    """取消预览登记（用户点"取消"时调用）。"""
    _PREVIEWS.pop(_preview_key(ctx, op), None)
