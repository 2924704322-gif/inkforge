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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


# ---------- 占位符哨兵（实测：模型会把提示词里的参数名当取值填进来） ----------
# 由来（真实事故）：提示词里写 `book_create {novel_id, mode}`，模型直接把字面量
# `novel_id` 当成书名标识提交，于是书架里真的多出一本叫 `novel_id` 的书。
# 这类值**永远是错的**，必须在参数层拦掉：当作"没填"，走补齐/反问，绝不落盘。

_PLACEHOLDER_VALUES = frozenset({
    "novel_id", "book_id", "book_key", "novel", "id", "name", "title", "xxx", "xxxx",
    "string", "value", "example", "示例", "书名", "书名标识", "书目标识", "标识",
    "待定", "未知", "无", "none", "null", "n/a", "na", "tbd", "placeholder",
    "<书名>", "<novel_id>", "{{novel_id}}", "${novel_id}",
})

#: 中日文书名号/引号包裹：模型常把「书名字面量」写成 `《书名》`、`《X》`，甚至在只有
#: 一个书名变量时输出空的 `《》`（真实事故：`interactive_start` 参数 novel="《》" →
#: 命中原样送进 validate_novel_id 被硬拒，预览与执行双双失败，用户只看到一行报错）。
_BOOK_TITLE_WRAP = "《》〈〉「」『』"


def _strip_book_title_wrap(value: str) -> str:
    """剥离书名号/引号包裹与首尾空白（`《源质觉醒》` → `源质觉醒`）。"""
    text = (value or "").strip()
    while len(text) >= 2 and text[0] in _BOOK_TITLE_WRAP and text[-1] in _BOOK_TITLE_WRAP:
        text = text[1:-1].strip()
    return text


def is_placeholder_value(value: Any) -> bool:
    """该取值是否是"提示词占位符"而不是用户真实意图（大小写/括号/引号不敏感）。"""
    text = str(value or "").strip().strip("<>[]{}`\"'“”‘’").strip().lower()
    text = _strip_book_title_wrap(text).lower()   # 书名号包裹：`《书名》` 也是占位符
    if not text:
        return False
    if text in _PLACEHOLDER_VALUES:
        return True
    # `novel-id` / `novel id` / `bookid` 这类变体一并按占位符处理
    squeezed = re.sub(r"[\s_\-]+", "", text)
    return squeezed in {"novelid", "bookid", "bookkey", "bookname", "booktitle"}


def clean_arg_value(value: Any) -> Any:
    """过滤占位符：命中占位符的取值一律折算为空串（= 没填）。"""
    if isinstance(value, str) and is_placeholder_value(value):
        return ""
    return value


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
    · **容错（真实事故）**：模型偶尔把书名占位符/书名号写成 ``novel="《》"``、
      ``《书名》`` 或中文书名。这类取值不是"要操作的书"，而是"没填对"：
      旧的实现把它原样送进 ``validate_novel_id`` 直接硬拒（用户只看到
      `非法书名标识：'《》'`），书内会话明明知道当前是哪本书却白白失败一轮。
      现在按「剥离书名号 → 命中书目按书目 → 否则当作没填 → 回落当前书」处理，
      只有**真的指定了另一本不存在的书**时才报错。
    """
    raw = _strip_book_title_wrap(str(args.get("novel") or ""))
    if raw and raw != WORKSPACE and not is_placeholder_value(raw):
        try:
            return library.validate_novel_id(raw)
        except library.LibraryError as exc:
            # 只对"人话书名/占位符"回落；`../evil`、`a/b` 这类**明确非法**的标识仍然报错，
            # 避免把路径穿越之类的东西静默折算成别的书。
            if _looks_like_book_title(raw):
                fallback = _resolve_from_candidates(ctx)
                if fallback:
                    logger.warning(
                        "动作参数 novel=%r 不是合法书目标识（%s），按会话当前书 %r 处理",
                        args.get("novel"), exc.message, fallback,
                    )
                    return fallback
            raise
    return _resolve_from_candidates(ctx, required=required)


def _looks_like_book_title(value: str) -> bool:
    """该取值是否像"人写的书名"而不是"非法路径/残缺标识"。

    判据：含非 ASCII 字符（中日文书名）或原本被书名号包裹过。纯 ASCII 的
    `../evil` / `a/b` / `-bad` 一律判定为非法标识，不进入回落。
    """
    text = (value or "").strip()
    if not text:
        return False
    if text != _strip_book_title_wrap(text):
        return True   # 原本带《》「」等书名号
    return any(ord(ch) > 0x7F for ch in text)


def _resolve_from_candidates(ctx: ActionContext, *, required: bool = True) -> str:
    """无显式 ``novel`` 时的回落链：会话当前书 → 工作台当前书 → 唯一书目。"""
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


def _h_material_read(ctx: ActionContext, args: dict) -> ActionExec:
    """读素材正文：list 只能给标题，用户问"某条素材讲了什么"必须能取到内容。"""
    root = library.novels_root().parent / "materials"
    mid, post = _read_by_id_or_title(root, _arg_str(args, "id"), _MATERIAL_ID_RE,
                                     "素材", "mt-5e9d9fe7")
    return ActionExec("material_read", True, "ok",
                      f"素材《{post.metadata.get('title', mid)}》正文如下（编号 {mid}）。",
                      {"id": mid, "title": str(post.metadata.get("title") or mid),
                       "content": post.content})


def _h_material_list(ctx: ActionContext, args: dict) -> ActionExec:
    from src.services import materials as materials_svc

    data_root = library.novels_root().parent
    items = materials_svc.list_materials(data_root)
    if items:
        lines = [
            f"- 第 {i + 1} 条：{m.title}〔{m.category}〕（编号 {m.id}）"
            for i, m in enumerate(items)
        ]
        summary = (
            f"素材库 {len(items)} 条：\n" + "\n".join(lines)
            + "\n（读正文用 material_read，id 填「编号」）"
        )
    else:
        summary = "素材库为空。"
    return ActionExec("material_list", True, "ok", summary, {
        "materials": [
            {"id": m.id, "title": m.title, "category": m.category} for m in items
        ]
    })


#: 读取类动作的 ID 正则（宽松：大小写不敏感；模型常写成 LN-XXXX 或带引号/空格）
_LEARNING_ID_RE = re.compile(r"^(ln-[a-f0-9]{4,32})$", re.IGNORECASE)
_MATERIAL_ID_RE = re.compile(r"^(mt-[a-f0-9]{4,32})$", re.IGNORECASE)


def _normalize_id(raw: str) -> str:
    """ID 容错：去掉包裹的引号/反引号/书名号与首尾空白（模型爱加这些）。"""
    return (raw or "").strip().strip("`\"'“”‘’《》<>[]（）()").strip()


def _read_by_id_or_title(root: Path, raw: str, pattern: re.Pattern[str],
                         kind: str, id_example: str) -> tuple[str, Any]:
    """按 ID 读一条 MD；ID 不认识时**按标题唯一命中**再兜一次。

    实测缺口：用户说"看第 1 条的内容"时，模型常把列表里的**标题**当 ID 传过来，
    于是「非法学习成果 ID」直接失败——用户看到的是"点开就报错"。
    这里允许按 frontmatter title 精确匹配，**只在唯一命中时**放行（有歧义就报错，
    不猜）。返回 (命中的 id, frontmatter post)。
    """
    value = _normalize_id(raw)
    if not value:
        raise ActionError(f"缺少参数 id（{kind}的编号）")
    if pattern.match(value):
        path = root / f"{value.lower()}.md"
        if not path.exists():
            # 大小写不敏感兜底：目录里可能是原始大小写
            cands = [p for p in root.glob("*.md") if p.stem.lower() == value.lower()]
            if not cands:
                raise ActionError(f"{kind}不存在：{value}", status=404)
            path = cands[0]
        return path.stem, frontmatter.load(str(path))
    # 按标题唯一命中
    if root.exists():
        hits = []
        for path in root.glob("*.md"):
            try:
                post = frontmatter.load(str(path))
            except Exception:  # noqa: BLE001 - 损坏文件跳过
                continue
            title = str(post.metadata.get("title") or path.stem)
            if value == title or value == path.stem:
                hits.append((path.stem, post))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ActionError(f"「{value}」匹配到多条{kind}，请用编号指定：" +
                              "、".join(h[0] for h in hits[:5]), status=400)
    raise ActionError(f"非法{kind} ID：{raw!r}（列表里的编号形如 {id_example}）；"
                      "请用列表回执里给出的编号", status=400)


def _h_learning_list(ctx: ActionContext, args: dict) -> ActionExec:
    """列出学习仿写的历史成果（三阶段分析报告）。

    实测缺口：用户问"找出学习仿写里的历史内容"，墨师手上没有这个动作，
    只能回答"我查不到"——功能端点是有的（GET /api/learning），但没进动作清单。
    回执里**必须带上"点开就用这个编号"的用法**，否则会出现"能列编号、点开失败"。
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
    if items:
        lines = [f"- 第 {i + 1} 条：{it['title']}（编号 {it['id']}）"
                 for i, it in enumerate(items)]
        hint = (f"共 {len(items)} 条：\n" + "\n".join(lines) +
                "\n（要看全文，用 learning_read 并把「编号」原样填进 id；"
                "不要说改成 learning_get / learn_list 这类名字）")
        return ActionExec("learning_list", True, "ok", hint, {"items": items})
    return ActionExec("learning_list", True, "ok",
                      "学习仿写历史为空：还没有做过样本分析（可在「学习仿写」页粘贴样本生成）。",
                      {"items": []})


def _h_learning_read(ctx: ActionContext, args: dict) -> ActionExec:
    root = library.novels_root().parent / "learning"
    lid, post = _read_by_id_or_title(root, _arg_str(args, "id"), _LEARNING_ID_RE,
                                     "学习成果", "ln-8d30a9d7")
    return ActionExec("learning_read", True, "ok",
                      f"学习仿写《{post.metadata.get('title', lid)}》全文如下"
                      f"（编号 {lid}，共 {len(post.content)} 字）。",
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

#: 模型把"创作模式"写成别的词时的折算表（命中即折算，不再失败）。
#: 全部来自真实调用实测：`free` / `自由创作` / `长篇` / `互动模式` / `剧情卡` …
#: 分两类：互动类 → interactive；其余（体裁/篇幅/产出形态描述）→ pipeline。
_MODE_INTERACTIVE_WORDS = ("interactive", "互动", "剧情卡", "逐章", "卡片", "选卡", "分支")
_MODE_PIPELINE_WORDS = (
    "pipeline", "free", "流水", "大纲", "自由", "默认", "default", "auto",
    # 体裁/篇幅/产出形态：用户说"长篇/网文/连载"，模型就把这个当成 mode 传了过来
    "长篇", "短篇", "中篇", "微篇", "小说", "网文", "连载", "系列", "全书", "成书",
    "novel", "series", "serial", "book", "长文", "标准",
)


def _normalize_mode(raw: str) -> str:
    """把模型写出的创作模式折算到规范值（pipeline / interactive）。

    实测模型会自造各种说法；它们语义明确，直接失败让用户白等一轮没有意义，
    因此在**动作层**统一折算（与 op 别名同类）。

    真实事故（本批次真机复现）：用户说"建一本**长篇**"，模型把 `mode` 写成 `"长篇"`，
    折算表没覆盖 → 确认卡带着"未知创作模式"出卡 → 用户点确认 → **什么都没落盘**。
    因此这里的原则是：**宁可按默认的 pipeline 走，也不让建书卡在措辞上**；
    真的判不出来时返回 pipeline 并记一条日志（用户的兜底永远是"确认卡 + 可改"）。
    """
    value = (raw or "").strip().lower()
    if value in library.CREATION_MODES:
        return value
    if any(k in value for k in _MODE_INTERACTIVE_WORDS):
        return "interactive"
    if any(k in value for k in _MODE_PIPELINE_WORDS):
        return "pipeline"
    if value:
        logger.info("创作模式取值无法判定（%r）→ 按默认 pipeline 处理，用户可在确认卡上纠正", raw)
    return "pipeline"


def _preview_book_create(ctx: ActionContext, args: dict) -> str:
    """建书预览：**必须把"目录标识"和"书名"分开写清楚**。

    为什么强调：书名标识是 ASCII（决定 `data/novels/<id>/` 目录名），而用户想的是中文书名。
    实测用户看到"将新建书目 novel-260917-a1f3"会以为书名被改坏了；
    因此影响说明里同时给出两个字段，并说明书名可以后改。

    书名来源（`ctx.extra["title_hint"]`）：由墨师网关从**用户原话**里抠出来（`《…》`/"就叫…"），
    不经模型转述——模型转述过一层就会丢。书名本身不参与目录命名，所以它**不是**动作参数，
    不会进入预览键（预览键只由 novel_id/mode 决定）。
    """
    raw_nid = _arg_str(args, "novel_id", required=False)
    if is_placeholder_value(raw_nid) or not str(raw_nid).strip():
        # ① 占位符（模型把参数名当取值，如 novel_id / <书名>）在参数层已被折算成空串；
        # ② 真空值由 _backfill 兜底链负责补，到这一步说明兜底也没辙。
        # 两种情况给同一条**可执行**的提示，而不是一句"缺少参数"。
        raise ActionError(
            "没有拿到可用的书名标识：请给一个真实的 ASCII 标识（如 my-book），"
            "或直接说中文书名（我会自动生成 ASCII 标识）"
        )
    nid = library.validate_novel_id(raw_nid)
    mode = _normalize_mode(_arg_str(args, "mode", required=False, default="pipeline"))
    if mode not in library.CREATION_MODES:
        raise ActionError(f"未知创作模式：{mode!r}（pipeline / interactive）")
    if library.book_exists(nid):
        raise ActionError(f"书 {nid} 已存在（若要另建一本，请给一个不同的书名标识）", status=409)
    label = "自由创作（大纲→章节流水线）" if mode == "pipeline" else "互动创作（剧情卡逐章推进）"
    mode_raw = str((ctx.extra or {}).get("mode_raw") or args.get("mode_raw") or "").strip()
    title_hint = str((ctx.extra or {}).get("title_hint") or "").strip()
    lines = [
        "将新建书目：",
        f"  · 书名标识（目录名，ASCII）：`{nid}`",
    ]
    if title_hint:
        lines.append(f"  · 书名：《{title_hint}》（中文书名不进目录名；生成大纲时用它作为作品名）")
    mode_line = f"  · 创作模式：{label}"
    if mode_raw:
        # 模型原话与规范化结果不一致时如实并列：用户能当场发现"它把长篇理解错了"
        mode_line += f"（我按你的说法「{mode_raw}」折算成 {mode}）"
    lines.append(mode_line)
    lines += [
        f"  · 会创建 `data/novels/{nid}/` 目录骨架（chapters/settings/summaries/reviews）",
        "确认后才会落盘；书名与标识后续都可以改。",
    ]
    return "\n".join(lines)


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
    # 同步刷新工作区索引缓存，让书架/资源树立刻看到新的绑定
    invalidate_workspace_index()
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
    # 预期字数（生成前设定 / 打回时改目标）：与 /api/decision 同一语义，一并透传给图
    target = _arg_int(args, "target_words", required=False, default=0)
    if target:
        payload["target_words"] = target
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
    from src.services import materials as materials_svc

    title = _arg_str(args, "title")
    content = _arg_str(args, "content")
    category = _arg_str(args, "category", required=False, default="") or materials_svc.CAT_OTHER
    if category not in materials_svc.CATEGORIES:
        category = materials_svc.categorize(title, content)
    m = materials_svc.create_material(
        library.novels_root().parent, title=title, content=content,
        category=category, source="墨师",
    )
    return ActionExec("material_create", True, "ok",
                      f"素材《{m.title}》已入库（分类：{m.category}）。", {"id": m.id})


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
    """写动作注册：执行器指向 _preview_*，真正执行时按 op 查 _RUN 表。

    预览期异常**不再直接判 failed**：实测（master-live-final6 / 用户实测）里，
    写动作缺参数时返回 `status=failed`，于是**根本不会登记待确认动作**，
    用户既看不到确认卡也没有可点的按钮，只得到一句报错——"点了没反应"的直接来源。
    现在统一转成 `pending_confirm` + 影响说明（含失败原因），让闸门始终可见：
    参数补齐后确认才真正执行，没补齐则确认时如实报失败原因。
    """

    def handler(ctx: ActionContext, a: dict, *, execute: bool = False):
        if execute:
            return run(ctx, a)
        error = ""
        try:
            impact = preview(ctx, a)
        except ActionError as exc:
            error = exc.message
            impact = (f"⚠ 这个写动作暂时无法执行：{error}\n"
                      f"补齐参数后我再执行；若参数由我推断的部分不对，请直接纠正我。")
        except library.LibraryError as exc:
            error = exc.message
            impact = f"⚠ 这个写动作暂时无法执行：{error}"
        except (RuntimeError, FileNotFoundError) as exc:
            error = str(exc)
            impact = f"⚠ 这个写动作暂时无法执行：{error}"
        pending = {"op": op, "args": a, "impact": impact}
        if error:
            pending["preview_error"] = error
        result = ActionExec(op, True, "pending_confirm", impact, pending=pending)
        if error:
            # 回执里也带上原因，供墨师如实转述（不是"已安排"，而是"没成、缺什么"）
            result.error = error
        return result

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

    _register_write("book_create", "新建书", ("novel_id", "mode", "mode_raw"),
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
                    ("novel", "action", "feedback", "revision_mode", "target_words"),
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
    _register_write("material_create", "新增素材库条目", ("title", "content", "category"),
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
    # 实测缺口：模型把 learning_read 写成 learning_get / learn_list（审计日志里两次真实失败）
    "learning_get": "learning_read",
    "learn_read": "learning_read",
    "learning_detail": "learning_read",
    "learning_content": "learning_read",
    "read_learn": "learning_read",
    "learn_list": "learning_list",
    "learnings": "learning_list",
    "learning_index": "learning_list",
    "list_learnings": "learning_list",
    "list_material": "material_list",
    "read_material": "material_read",
    "get_material": "material_read",
    "material_get": "material_read",
    "material_detail": "material_read",
    "list_chapters": "chapter_list",
    "chapters_list": "chapter_list",
    "list_chapter": "chapter_list",
    "read_chapter": "chapter_read",
    "get_chapter": "chapter_read",
    "chapter_get": "chapter_read",
    "chapter_detail": "chapter_read",
    "read_book": "chapter_read",
    "read_doc": "doc_read",
    "get_doc": "doc_read",
    "doc_get": "doc_read",
    "doc_detail": "doc_read",
    "settings_read": "doc_read",
    "read_setting": "doc_read",
    "read_outline": "outline_read",
    "get_outline": "outline_read",
    "outline_get": "outline_read",
    "list_docs": "doc_list",
    "docs_list": "doc_list",
    "setting_list": "doc_list",
    "stat_book": "book_stat",
    "get_book_stat": "book_stat",
    "book_status": "book_stat",
    "list_materials": "material_list",
    "materials_list": "material_list",
    "list_skills": "skill_list",
    "skills_list": "skill_list",
    "list_constraints": "constraint_list",
    "constraints_list": "constraint_list",
    "get_model_config": "model_config",
    "search": "search_workspace",
    "workspace_search": "search_workspace",
    # 写
    "create_book": "book_create",
    "new_book": "book_create",
    "books_create": "book_create",
    "add_book": "book_create",
    "select_book": "book_select",
    "switch_book": "book_select",
    "open_book": "book_select",
    "set_book": "book_select",
    "delete_book": "book_delete",
    "remove_book": "book_delete",
    "bind_skills": "book_bind_skills",
    "skills_bind": "book_bind_skills",
    "start_generation": "gen_start",
    "start_generate": "gen_start",
    "gen_start_writing": "gen_start",
    "write_start": "gen_start",
    "resume_generation": "gen_resume",
    "gen_continue": "gen_resume",
    "pause_generation": "gen_pause",
    "gen_stop": "gen_pause",
    "decide": "gen_decide",
    "arbitrate": "gen_decide",
    "review_decide": "gen_decide",
    "run_demo": "demo_run",
    "demo_generate": "demo_run",
    "confirm_demo": "demo_confirm",
    "start_interactive": "interactive_start",
    "interactive_run": "interactive_start",
    "choose_card": "interactive_choose",
    "interactive_select": "interactive_choose",
    "select_card": "interactive_choose",
    "create_material": "material_create",
    "add_material": "material_create",
    "material_add": "material_create",
    "create_constraint": "constraint_create",
    "add_constraint": "constraint_create",
    "constraint_add": "constraint_create",
}


def normalize_op(op: str) -> str:
    """把模型写出的动作名规范化到注册表里的 op（未知名字原样返回）。

    三级折算（实测模型自造名字的方式非常有规律，逐级兜）：
      ① 逐字命中注册表  →  ② 归一化（小写/连字符/空格）后命中  →  ③ 别名表
      → ④ **保守模糊匹配**：按 `_` 切词，命中唯一注册表 op 才折算。
    第 ④ 级是新增的：实测模型把 `learning_read` 写成 `learning_get`、
    `learning_list` 写成 `learn_list`，别名表穷举不完，而这类词干组合是可判定的。
    **保守**体现在"必须唯一命中"——歧义时原样返回，由执行器如实报未知动作（不吞错）。
    """
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
    guessed = _guess_op_by_tokens(lowered)
    return guessed or value


def _guess_op_by_tokens(lowered: str) -> str:
    """按词干组合猜 op（唯一命中原生 op 才返回，否则返回空串）。

    只处理两类可判定形态，不做通用相似度（避免把 `book_delete` 猜成 `book_select`）：
      · 词序颠倒：`learning_get` / `get_learning` → `learning_read`
        （"读"的同义词集合 {read,get,detail,show,view,open,content} 与
          "列"的同义词集合 {list,ls,all,index} 统一折算）
      · 单复数/缩写：`list_books` → `book_list`（别名表已覆盖，此处兜遗漏形态）
    """
    if not lowered:
        return ""
    parts = [p for p in lowered.split("_") if p]
    if len(parts) < 2:
        return ""
    read_words = {"read", "get", "detail", "details", "show", "view", "open", "content", "load"}
    list_words = {"list", "ls", "all", "index", "history"}
    singular = {"books": "book", "chapters": "chapter", "docs": "doc", "materials": "material",
                "skills": "skill", "constraints": "constraint", "outline": "outline"}
    words = [singular.get(p, p) for p in parts]
    verb = next((w for w in words if w in read_words or w in list_words), "")
    if not verb:
        return ""
    want_read = verb in read_words
    # 目标名词：去掉动词后剩下的词干（保序，取最长可匹配的）
    stems = [w for w in words if w not in read_words and w not in list_words]
    if not stems:
        return ""
    noun = "_".join(stems)
    # 只在"名词词干确实出现在某个 op 里"时折算，且要求动作方向一致
    candidates = []
    for op_name, action in ACTIONS.items():
        if action.scope == "write":
            continue          # 只折算只读动作：写动作猜错方向代价太大
        if noun not in op_name or op_name.startswith(noun):
            continue
        op_verb = op_name.replace(noun, "").strip("_")
        if (op_verb in read_words and want_read) or (op_verb in list_words and not want_read):
            candidates.append(op_name)
    return candidates[0] if len(candidates) == 1 else ""


#: 参数名折算表（op → {模型写的别名: 注册表里的规范名}）。
#: 实测依据（全部来自真实运行日志，见 docs/HANDOFF 的修复记录）：
#:   · `{"op":"book_create","args":{"id":"live-created","mode":"pipeline"}}` → 缺 novel_id
#:   · 模型自述"书名标识（book_key）：live-created"
#:   · `book_select` 写成 `{"novel": "2"}`（其它动作都用 novel，它跟着习惯写）
#: 这些别名**语义明确**，折算比让用户白等一轮好；折算只做一次，预览与确认两边一致。
ARG_ALIASES: dict[str, dict[str, str]] = {
    "book_create": {
        "novel": "novel_id", "book": "novel_id", "book_id": "novel_id",
        "book_key": "novel_id", "id": "novel_id", "name": "novel_id",
        "title": "novel_id", "key": "novel_id",
        "creation_mode": "mode", "type": "mode",
    },
    "book_select": {"novel": "novel_id", "book": "novel_id", "book_id": "novel_id",
                    "book_key": "novel_id", "id": "novel_id", "key": "novel_id"},
    "book_delete": {"novel_id": "novel", "book": "novel", "id": "novel"},
    "book_bind_skills": {"novel_id": "novel", "custom": "custom_skill_ids",
                         "packs": "pack_ids", "skill_ids": "pack_ids"},
    "gen_start": {"novel_id": "novel", "chapters_count": "chapters", "total_chapters": "chapters"},
    "gen_resume": {"novel_id": "novel"},
    "gen_pause": {"novel_id": "novel"},
    "gen_decide": {"novel_id": "novel", "decision": "action", "verdict": "action",
                   "comment": "feedback", "opinion": "feedback"},
    "demo_run": {"novel_id": "novel"},
    "demo_confirm": {"novel_id": "novel"},
    "interactive_start": {"novel_id": "novel"},
    "interactive_choose": {"novel_id": "novel", "card": "card_id", "cardId": "card_id",
                           "text": "custom_text", "content": "custom_text"},
}


def _fold_arg_names(op: str, raw: dict) -> dict:
    """把模型写的参数别名折算到规范名（规范名优先，别名不覆盖已填的规范名）。"""
    table = ARG_ALIASES.get(op)
    if not table:
        return raw
    out = dict(raw)
    for alias, canonical in table.items():
        if canonical in out and str(out.get(canonical) or "").strip():
            out.pop(alias, None)
            continue
        if alias in out:
            out[canonical] = out.pop(alias)
    return out


def normalize_args(op: str, args: dict | None) -> dict:
    """把参数折算到规范取值，并**丢弃未声明的多余键**。

    必须在**执行前、且对同一次动作只做一次**：预览时登记的 args 与确认时比对的 args
    都取同一份规范化结果，否则"模型写了 free、系统折算成 pipeline"或"模型自造了
    title 参数"都会让两边不相等，确认被拒（实测踩到过：用户点确认，卡还挂在那里）。

    丢未声明键顺带起到白名单作用。三步顺序固定：
      ① 参数名折算（ARG_ALIASES）→ ② 白名单过滤 → ③ 取值折算（枚举/占位符/整数）。
    """
    raw = _fold_arg_names(op, dict(args or {}))
    allowed = ACTIONS[op].args if op in ACTIONS else tuple(raw)
    out = {k: clean_arg_value(v) for k, v in raw.items() if k in allowed}
    if op == "book_create":
        # mode_raw = **模型原始说法**（供确认卡如实并列"你给的是『长篇』，按流水线处理"）。
        # 必须在 clean_arg_value 之外单独保留：clean_arg_value 会把占位符/无关值折成空串。
        raw_mode = str(raw.get("mode") or raw.get("mode_raw") or "").strip()
        out["mode"] = _normalize_mode(raw_mode) if raw_mode else "pipeline"
        out["mode_raw"] = raw_mode if raw_mode.lower() not in library.CREATION_MODES else ""
        if "novel_id" in out:
            out["novel_id"] = str(out.get("novel_id") or "").strip()
    if op in ("chapter_read", "interactive_choose") and "chapter" in out:
        out["chapter"] = _coerce_int(out.get("chapter"))
    if op in ("gen_start", "demo_run") and "chapters" in out:
        out["chapters"] = _coerce_int(out.get("chapters"))
    return out


def _coerce_int(value: Any) -> Any:
    """把"第 3 章"/"3章"/"３"这类写法折算成纯数字串（失败原样返回，由校验报错）。"""
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return text
    m = re.search(r"-?\d+", text.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    return int(m.group(0)) if m else text


def known_ops() -> list[str]:
    return sorted(ACTIONS)


#: 参数取值约束（供提示词/规划调用展示）。取值受限的关键参数必须写在提示词里，
#: 否则模型会自造枚举值（实测把 mode 写成 free / 自由创作），导致动作直接失败。
#: 尾部带"系统自动"字样的参数是**系统注入**的，模型不必也不应填。
ARG_CONSTRAINTS: dict[str, str] = {
    "mode": "pipeline|interactive",
    "mode_raw": "系统自动（留空即可）",
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


def _preview_key(ctx: ActionContext, op: str, args: dict | None = None) -> str:
    """预览登记键：**由规范化参数派生**，与会话状态、当前书目都无关。

    曾经的三版实现与各自的真实故障：
      · 按 ``session_key`` 分会话 → 工作区预览、书内确认互相找不到（"确认被拒"）；
      · 改成 ``{ctx.book or ctx.active_novel()}::{op}`` → 仍然读**可变状态**：
        预览 `book_create` 时工作台还没选书（键 `__workspace__::book_create`），
        而 `book_create` 成功后 ``set_active_novel(新书)`` 会立刻改掉这个值，
        于是确认时算出的键变成 `<新书>::book_create` → 找不到登记 → 409、
        零落盘、待办还挂着（master-live-final6 实测现场：3b 待确认态未清空）。
      · 本版：键只由 ``{op} + 规范化 args`` 决定。预览与确认的 args 是**同一份**
        规范化结果（见 ``execute`` 的 ``result.args``），因此两次算出的键必然相等，
        且不受"期间又建了书 / 切了书 / 换了会话"影响。

    兼容性：``args=None``（旧调用形态）时退化为按 op 归并，行为与旧版一致。
    """
    if args is None:
        target = ctx.book or ctx.active_novel() or ctx.default_novel
        return f"{target}::{op}"
    payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{op}::{digest}"


def register_preview(ctx: ActionContext, op: str, args: dict, impact: str,
                     token: str = "") -> dict:
    """登记一次写动作预览，返回可直接回给前端的 pending 结构。"""
    tk = token or hashlib.sha1(
        f"{_preview_key(ctx, op, args)}::{time.time()}".encode()
    ).hexdigest()[:16]
    _PREVIEWS[_preview_key(ctx, op, args)] = {
        "token": tk, "args": args, "impact": impact, "at": time.time(),
    }
    return {"op": op, "args": args, "impact": impact, "token": tk}


def consume_preview(ctx: ActionContext, pending: dict) -> str:
    """校验并消费一次预览；返回空串表示通过，否则返回拒绝原因。"""
    op = str(pending.get("op") or "")
    args = pending.get("args") if isinstance(pending.get("args"), dict) else {}
    key = _preview_key(ctx, op, args)
    record = _PREVIEWS.get(key)
    if record is None:
        # 兜底：按 op 归并的旧键（进程内从旧版本升级上来时的残留登记）
        legacy = _preview_key(ctx, op)
        record = _PREVIEWS.get(legacy)
        if record is not None:
            key = legacy
            logger.info("预览消费命中旧键（兼容路径）：key=%s", key)
    if record is None:
        logger.info("预览消费失败：没有登记（key=%s op=%s）", key, op)
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


def forget_preview(ctx: ActionContext, op: str, args: dict | None = None) -> None:
    """取消预览登记（用户点"取消"时调用）。

    ``args`` 给出时按键精确删除；不给时清理该 op 的**全部**登记 ——
    旧版按 `{目标书}::{op}` 归并，同一个 op 只可能有 1 条，按 op 删即可；
    新键与 args 绑定（同一 op 可能有多条不同参数的登记），因此取消时若不带 args，
    就把该 op 前缀下的所有登记一并作废（取消语义上没有问题且更安全）。
    """
    if args is not None:
        _PREVIEWS.pop(_preview_key(ctx, op, args), None)
        return
    prefix = f"{op}::"
    for key in [k for k in _PREVIEWS if k.startswith(prefix)]:
        _PREVIEWS.pop(key, None)
    _PREVIEWS.pop(_preview_key(ctx, op), None)
