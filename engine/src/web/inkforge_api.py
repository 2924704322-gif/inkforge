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
from types import SimpleNamespace
from typing import Any

import frontmatter
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger
from src.web.actions import (
    MAX_ACTIONS_PER_TURN,
    ActionContext,
    execute as actions_execute,
    execute_pending,
    manifest as actions_manifest_list,
    read_audit,
)
from src.web.actions import ACTIONS as _ACTION_REGISTRY
from src.web.scope import WORKSPACE, chats_dir, is_workspace
from src.web.scope import workspace_dir as workspace_data_dir

logger = get_logger(__name__)

_NOVEL_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REL_RE = re.compile(r"^settings/[A-Za-z0-9_\-/\u4e00-\u9fff]+\.md$")

# ---------- 书籍上下文配额（W2）----------
# 由「单次 text[:6000] 截断」改为「分段保底 + 按需分配」：任一段变长都不会静默
# 挤掉其它段（旧实现的缺陷是"新增段落静默挤掉既有段落"），截断处一律显式标注。

BOOK_CONTEXT_TOTAL_BUDGET = 6000

# (相对路径, 段标签, 保底字数, 上限字数)
BOOK_CONTEXT_SECTIONS: list[tuple[str, str, int, int]] = [
    ("settings/custom-skills.md", "【作者指定 · 创作约束】", 1200, 4000),
    ("settings/state-board.md", "【实体状态板】", 800, 3000),
    ("settings/foreshadowing.md", "【伏笔台账】", 400, 600),
    ("settings/style.md", "【文风指纹】", 400, 500),
]

# 启动期断言：保底之和必须小于总量，否则保底本身不可满足
if sum(floor for _rel, _label, floor, _cap in BOOK_CONTEXT_SECTIONS) >= BOOK_CONTEXT_TOTAL_BUDGET:
    raise RuntimeError(
        "书籍上下文保底配额之和不得大于等于总量："
        f"{sum(floor for _rel, _label, floor, _cap in BOOK_CONTEXT_SECTIONS)}"
        f" >= {BOOK_CONTEXT_TOTAL_BUDGET}"
    )


def _allocate_context(
    total: int, needs: list[int], floors: list[int], caps: list[int]
) -> list[int]:
    """配额分配：① 先满足保底（不超过各段上限与需求）→ ② 剩余额度按各段超额
    需求比例分配，仍受上限约束；比例取整产生的余量按序补足，不浪费额度。"""
    quotas = [min(n, f, c) for n, f, c in zip(needs, floors, caps)]
    remaining = max(total - sum(quotas), 0)
    while remaining > 0:
        extra = [max(min(n, c) - q, 0) for n, q, c in zip(needs, quotas, caps)]
        total_extra = sum(extra)
        if total_extra == 0:
            break
        for i, want in enumerate(extra):
            if want == 0 or remaining == 0:
                continue
            share = min(want, max(1, remaining * want // total_extra), remaining)
            quotas[i] += share
            remaining -= share
    return quotas


def _clip_context_section(label: str, text: str, quota: int) -> str:
    """按配额裁剪段落；发生截断时在返回值内显式标注。"""
    if len(text) <= quota:
        return f"{label}\n{text}"
    return f"{label}\n{text[:quota]}\n（注：本段超出配额，已截断至 {quota} 字）"


def _is_rule_like(value: Any) -> bool:
    """是否为"规则类"结构化字段（清单/条目），需要完整保真而不是归一化截断。"""
    if isinstance(value, dict):
        return "rules" in value
    if isinstance(value, list):
        return bool(value)
    return False


def build_book_context(store: MdStore, novel: str = "") -> str:
    """组装书籍上下文（设定/进度/伏笔/状态板/近期摘要）。

    W2：分段保底 + 按需分配。设定类段落按 BOOK_CONTEXT_SECTIONS 的保底/上限
    分配额度，任一段超长都不会静默挤掉其它段；前置段落（作品/进度/人物/摘要）
    单独受配额约束，任何截断处均在返回值内显式标注。
    """
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

    chars_dir = store.root / "settings" / "characters"
    chars = sorted(chars_dir.glob("*.md")) if chars_dir.exists() else []
    if chars:
        lines = []
        for p in chars[:12]:
            doc = store.read(p.relative_to(store.root).as_posix())
            m = doc.metadata
            lines.append(f"- {m.get('name', p.stem)}（{m.get('role', '')}）："
                         f"{str(m.get('personality', ''))[:80]}")
        parts.append("【人物】\n" + "\n".join(lines))

    summaries_dir = store.root / "summaries"
    summaries = sorted(summaries_dir.glob("*.md")) if summaries_dir.exists() else []
    if summaries:
        recent = summaries[-2:]
        lines = [store.read(p.relative_to(store.root).as_posix()).content[:250] for p in recent]
        parts.append("【近期章节摘要】\n" + "\n".join(lines))

    fixed = "\n\n".join(parts)

    # 设定类段落：先按保底预留额度，剩余额度按各段超额需求比例分配
    entries = [
        (rel, label, store.read(rel).content.strip(), floor, cap)
        for rel, label, floor, cap in BOOK_CONTEXT_SECTIONS
        if store.exists(rel)
    ]
    floors = [floor for _r, _l, _t, floor, _c in entries]
    caps = [cap for _r, _l, _t, _f, cap in entries]
    needs = [min(len(text), cap) for _r, _l, text, _f, cap in entries]

    fixed_budget = max(BOOK_CONTEXT_TOTAL_BUDGET - sum(floors), 0)
    fixed_truncated = len(fixed) > fixed_budget
    if fixed_truncated:
        fixed = fixed[:fixed_budget] + (
            f"\n\n（注：前置段落超出 {fixed_budget} 字配额，已截断）"
        )
    quotas = _allocate_context(
        sum(floors) + max(fixed_budget - len(fixed), 0), needs, floors, caps
    )

    # 归因日志（W11）：每次组装记录各段 origin 与最终长度，产出异常时可定位
    attribution: list[dict] = [{
        "origin": "project-facts",
        "label": "【作品/进度/人物/近期摘要】",
        "chars": len(fixed),
        "budget": fixed_budget,
        "truncated": fixed_truncated,
    }]
    blocks = [fixed] if fixed else []
    for (rel, label, text, _floor, _cap), quota in zip(entries, quotas):
        block = _clip_context_section(label, text, quota)
        blocks.append(block)
        attribution.append({
            "origin": rel,
            "label": label,
            "chars": len(block),
            "quota": quota,
            "truncated": len(text) > quota,
        })
    logger.info(
        "书籍上下文组装归因[%s] 总长 %d/%d：%s",
        novel,
        sum(len(b) for b in blocks),
        BOOK_CONTEXT_TOTAL_BUDGET,
        json.dumps(attribution, ensure_ascii=False),
    )
    return "\n\n".join(blocks)


def actions_is_write(op: str) -> bool:
    action = _ACTION_REGISTRY.get(op)
    return bool(action and action.is_write)


# ---------- 墨师动作协议（P2/P3） ----------
# 约定：墨师在回复末尾用一个 XML 块给出动作意图；系统执行后把结果回灌，再让它汇总。
# 用 XML 包裹而非裸 JSON，是为了与正文里的普通 JSON 示例/代码块区分开。
_ACTION_PROMPT_HEAD = (
    "【动作协议】当你需要查询事实或执行操作时，在回复的最末尾追加一个动作块，格式：\n"
    "<INKFORGE_ACTIONS>\n"
    '{"actions": [{"op": "book_list", "args": {}}]}\n'
    "</INKFORGE_ACTIONS>\n"
    "**op 必须逐字使用清单里的名字**（例如 `book_list`、`chapter_list`、`book_create`、"
    "`gen_start`），不要写成 list_books / list_chapters 这类自造名；"
    "args 里用清单标注的参数名。\n"
    "规则：一次最多 6 个动作；只需要事实时用只读动作；任何写操作都必须先向用户说明并等待确认，"
    "不得在同一轮里当成已确认执行。不需要动作时**不要**输出该块。\n"
    "**重要**：当用户的消息是在要求你办事（例如「建一本 X」「把某章的章节列出来」"
    "「查一下进度」「开写 3 章」）时，**必须在同一轮就给出动作块**——不要只用一句"
    "「需要我帮你做吗？」把球踢回去。参数不全时按最合理的推断填写，然后在正文里说明"
    "你推断的值；系统会先请你确认，不会直接落盘。\n"
)
_ACTIONS_PENDING_KEY = "pending_action"

#: "用户在要求办事"的意图词（用于决定是否补一次定向规划调用）
ACTION_INTENT_WORDS: tuple[str, ...] = (
    "帮我", "给我", "替我", "新建", "建一本", "建书", "创建", "删掉", "删除",
    "切换", "打开", "调出来", "取出来", "列一下", "列出", "拉取", "找出", "检索",
    "查一下", "查询", "查查", "看看进度", "开写", "开始写", "启动", "续跑", "暂停",
    "绑定", "确认并", "裁决", "通过这一章", "打回", "新增素材", "导出", "统计",
)

_XML_ACTIONS_RE = re.compile(
    r"<INKFORGE_ACTIONS>\s*(.*?)\s*</INKFORGE_ACTIONS>", re.DOTALL | re.IGNORECASE
)
_APPROVE_RE = re.compile(r"^(确认|确定|执行|同意|可以|好的|行|ok|yes|好的，?执行|开始执行)\W*$",
                         re.IGNORECASE)
_CANCEL_RE = re.compile(r"^(取消|算了|不用了|先不|no|cancel)\W*$", re.IGNORECASE)

#: 工作台当前书目（P1）：/api/book-select 更新，供工作区会话解析"用户在看哪本书"
#: 与逐章确认关卡的放行目标。进程内单例即可——桌面壳是单窗口应用。
_APP_STATE: dict[str, str] = {"active_novel": ""}


def app_state() -> object:
    """轻量进程级状态容器（当前书目等）。"""
    return SimpleNamespace(**_APP_STATE)


def set_active_novel(novel_id: str) -> None:
    _APP_STATE["active_novel"] = novel_id or ""


def _pending_decision(message: str) -> str:
    """判断这句话是对"待确认动作"的放行还是取消（空串 = 与本话题无关）。"""
    text = (message or "").strip()
    if not text or len(text) > 20:
        return ""
    if _APPROVE_RE.match(text):
        return "approve"
    if _CANCEL_RE.match(text):
        return "cancel"
    return ""


def _summary_from_results(results: list[dict]) -> str:
    """把动作回执整理为供墨师汇总的文本；无内容时返回空串。

    失败/未知动作**必须**如实喂给模型（并在回执里可见），否则用户会看到
    "模型以为调了工具、实际什么都没发生"的静默失败。
    """
    chunks: list[str] = []
    for r in results or []:
        status = r.get("status")
        op = r.get("op")
        if status == "ok":
            chunks.append(f"[已执行] {op}：{r.get('summary', '')}")
            data = r.get("data")
            if data:
                chunks.append("    结果数据：" + json.dumps(data, ensure_ascii=False)[:1500])
        elif status == "pending_confirm":
            chunks.append(f"[待用户确认] {op}：{r.get('summary', '')}")
            chunks.append("    → 现在请把这条影响说明给用户，并请他确认或取消；不要替他确认。")
        else:
            chunks.append(f"[失败] {op}：{r.get('error', '')}")
            chunks.append("    → 请把失败原因如实告诉用户；若是动作名/参数写错，"
                          "请按动作清单里的规范名重试一次。")
    return "\n".join(chunks)


def _action_response_text(results: list[dict]) -> str:
    """纯动作轮的确定性答复（不依赖模型二次改写，避免参数被改写走样）。"""
    if not results:
        return ""
    if all(r.get("status") == "pending_confirm" for r in results):
        return "\n".join(r.get("summary", "") for r in results if r.get("summary"))
    lines = []
    for r in results:
        if r.get("status") == "ok":
            lines.append(r.get("summary", "") or f"{r.get('op')} 已完成。")
        elif r.get("status") == "failed":
            lines.append(f"操作 {r.get('op')} 未成功：{r.get('error', '')}")
    return "\n".join(lines)


def _shrink_action_data(data: Any) -> Any:
    """动作回执体积闸门：超长正文只回片段给前端，避免把 UI 撑爆。"""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > 2000:
                out[key] = value[:2000] + f"…（共 {len(value)} 字，已截断）"
            elif isinstance(value, (dict, list)):
                out[key] = _shrink_action_data(value)
            else:
                out[key] = value
        return out
    if isinstance(data, list):
        return [_shrink_action_data(v) for v in data[:50]]
    return data


def _looks_actionable(message: str) -> bool:
    """用户这句话是否明确要求"办事"（而不是闲聊/提问）。

    用途：真实模型有时只输出一句"要我把章节清单取出来吗？"就停下，动作块一个都不给——
    这时补一次**定向规划调用**（只让它输出动作 JSON），保证动作闭环不依赖模型的自觉。
    判据取保守的动词/意图短语，避免把普通问答也拉进规划调用（那是白花钱）。
    """
    text = (message or "").strip()
    if not text:
        return False
    return any(word in text for word in ACTION_INTENT_WORDS)


def _pending_verdict_payload(verdict: dict) -> tuple[str, list[dict], bool]:
    """把闸门裁决转成 (回复文本, 动作回执, 是否真的执行了)。

    抽成模块级纯函数，便于单测锁定"确认/取消"两条路径的文案与回执形态。
    """
    result = verdict.get("result")
    pending = verdict.get("pending") or {}
    op = pending.get("op")
    if verdict.get("kind") == "executed" and result is not None:
        text = (f"✅ 已执行 {op}：{result.summary}" if result.ok
                else f"❌ {op} 执行失败：{result.error}")
        receipts = [{
            "op": result.op, "status": result.status, "ok": result.ok,
            "summary": result.summary, "error": result.error,
            "data": _shrink_action_data(result.data),
        }]
        return text, receipts, True
    return f"已取消待确认动作：{op}。", [], False


_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,40}")


def _guess_novel_id(message: str) -> str:
    """从用户原话里猜书目标识（``书名标识就用 xxx`` / ``叫 xxx`` / 反引号包裹的 id）。

    只用于**建书**这一个动作的兜底：标识本来就是用户随手起的名字，
    猜测失败也没关系（会如实回执"缺少参数"），但猜中就能把整条链走通。
    """
    text = message or ""
    patterns = (
        r"(?:书名标识|标识|novel_id|book_id|id)\s*(?:就用|用|是|为|叫|：|:)\s*[`\"']?([A-Za-z][A-Za-z0-9_-]{2,40})",
        r"[`\"']([A-Za-z][A-Za-z0-9_-]{3,40})[`\"']",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return ""


def _fallback_action_from_history(chat: dict, user_msg: str) -> list[dict]:
    """兜底：用户已确认但本轮没能拿到动作时，按上文尝试重建一个建书动作。

    实测缺口：模型有时只在正文里说"我将按以下参数新建《X》"，既不输出动作块，
    也不给出可解析的参数。用户回"确认"时若什么都不做，整条链就断在这里。
    这里对**建书**（最常见的新起点操作）做确定性兜底：从用户原话取标识，
    模式固定为 pipeline——写动作仍会走确认闸门，所以猜错也不会有副作用。
    """
    recent = " ".join(str(m.get("content", ""))[:600]
                      for m in (chat.get("messages") or [])[-8:]
                      if m.get("role") == "assistant")
    if "book_create" in recent or ("新建" in recent and "书目" in recent):
        nid = _guess_novel_id(user_msg) or _guess_novel_id(recent)
        if nid:
            logger.info("用户确认但无待办：按上文兜底重建 book_create（novel_id=%s）", nid)
            return [{"op": "book_create", "args": {"novel_id": nid, "mode": "pipeline"}}]
    return []


def _is_affirmative(message: str) -> bool:
    """用户是否在"确认"（含"确认建书"这类短确认句）。

    用于兜住一个实测缺口：上一轮墨师曾表态"要我做 X 吗"但没登记待确认动作，
    用户回"确认"时既没有闸门可放行、也没有新意图词可触发补漏 → 卡住。
    此时应补一次规划调用，按"用户已确认上一条提议"直接出动作。
    """
    text = (message or "").strip()
    if not text or len(text) > 24:
        return False
    if _pending_decision(text) == "approve":
        return True
    return any(word in text for word in ("确认", "同意", "可以", "执行", "开始执行", "按你说的"))


def _summarize_action_manifest() -> str:
    """把动作注册表压成给规划调用看的紧凑清单（控制 token）。

    参数带上取值约束（``mode(pipeline|interactive)``）：实测模型会自造枚举值
    （把 mode 写成 free），把可选取值摆在它眼前是最省的修法。
    """
    lines = []
    for item in actions_manifest_list():
        hints = item.get("args_hint") or item["args"]
        args = "、".join(hints) if hints else "无"
        lines.append(f"- {item['op']}（{item['scope']}；参数：{args}）：{item['desc']}")
    return "\n".join(lines)


def _plan_actions_with_model(registry, role: str, user_msg: str,
                             history: list, context: str,
                             *, after_confirm: bool = False) -> list[dict]:
    """定向规划调用：强制模型只输出一个动作 JSON（用于补漏）。

    after_confirm=True 时额外要求：用户已同意上一条提议，请直接给出对应动作、
    不要再反问（实测模型会在这里反复追问，把用户卡死）。
    """
    from src.llm.base import ChatMessage

    extra = (
        "\n【重要】用户刚刚已明确【确认】。请根据上文你自己的提议直接给出要执行的动作，"
        "**不要再反问参数**；参数尽量从上下文与上一条提议里取，取不到的用最合理默认值。\n"
        if after_confirm else ""
    )
    system = (
        "你是工具调用规划器。根据用户消息与上下文，判断需要调用哪些工具。\n"
        "只输出一个 JSON 对象，形如：{\"actions\": [{\"op\": \"book_list\", \"args\": {}}]}。\n"
        "op 必须**逐字**使用下面清单里的名字；args 用清单里的参数名。\n"
        "只读工具用于取事实；写操作也照常列入（系统会先请用户确认，不会直接执行）。\n"
        "**参数取值约定**：`mode` 只能是 pipeline 或 interactive；"
        "**新建书请用 `novel_id`（不是 book_key / id / 书名标识）**；"
        "`novel` 传已存在书目的标识（上下文里出现过的那本）；未提到书目时可省略 novel。\n"
        f"{extra}"
        "若确实无需任何工具，输出 {\"actions\": []}。\n\n"
        f"【工具清单】\n{_summarize_action_manifest()}\n\n"
        f"【当前上下文摘录】\n{context[:1500]}"
    )
    messages = [ChatMessage(role="system", content=system), *history[-4:],
                ChatMessage(role="user", content=user_msg)]
    raw = registry.chat_as(role, messages, json_mode=True, temperature=0.0).content
    items: Any = []
    match = _XML_ACTIONS_RE.search(raw or "")
    if match:
        raw = match.group(1)
    try:
        data = json.loads((raw or "").strip().removeprefix("```json").removesuffix("```").strip())
        items = data.get("actions") if isinstance(data, dict) else data
    except Exception:  # noqa: BLE001 - 规划失败就当没有动作
        logger.warning("动作规划调用解析失败：%.120s", raw)
        return []
    out: list[dict] = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("op"):
                args = item.get("args") if isinstance(item.get("args"), dict) else {}
                out.append({"op": str(item["op"]).strip(), "args": args})
            elif isinstance(item, dict) and len(item) == 1:  # {"list_books": {...}}
                key = next(iter(item))
                payload = item[key]
                out.append({"op": str(key).strip(),
                            "args": payload if isinstance(payload, dict) else {}})
    return out


def _parse_actions(text: str) -> tuple[str, list[dict]]:
    """从墨师回复中切出动作 JSON 块；返回 (去掉动作块的正文, 动作列表)。

    模块级函数（纯函数、可单测），兼容两种写法：
      · `{"actions": [{"op": "book_list", "args": {}}]}`
      · `[{"list_books": {}}]`（动作名当键）——实测模型常这么写，交由执行器别名表折算。
    """
    match = _XML_ACTIONS_RE.search(text or "")
    if not match:
        return (text or "").strip(), []
    raw = match.group(1).strip()
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - 解析失败则当作普通文本，不猜测
        logger.warning("墨师动作块 JSON 解析失败，已忽略：%.80s", raw)
        return (_XML_ACTIONS_RE.sub("", text or "").strip(), [])
    items = data.get("actions") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return (_XML_ACTIONS_RE.sub("", text or "").strip(), [])
    out: list[dict] = []
    for item in items:
        if isinstance(item, dict) and item.get("op"):
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            out.append({"op": str(item["op"]).strip(), "args": args})
        elif isinstance(item, dict) and len(item) == 1:
            key = next(iter(item))
            payload = item[key]
            out.append({"op": str(key).strip(),
                        "args": payload if isinstance(payload, dict) else {}})
    return (_XML_ACTIONS_RE.sub("", text or "").strip(), out)


def _save_chat_to(root: Path, chat: dict) -> None:
    """把会话写入 ``<root>/chats/<id>.json``（模块级，供端点/测试共用一份实现）。"""
    path = chats_dir(root) / f"{chat['id']}.json"
    path.write_text(json.dumps(chat, ensure_ascii=False, indent=1), encoding="utf-8")


def _decide_pending_action(chat: dict, message: str, ctx) -> dict | None:
    """待确认动作闸门（纯函数，不落盘）。

    · 用户说"确认/执行" → 真正执行，返回 {"kind": "executed", ...}
    · 用户说"取消/算了" → 丢弃待办，返回 {"kind": "cancelled", ...}
    · 其它话语 → 返回 None（视为新话题，待办保留在原处）

    抽成模块级函数：这是安全关键路径（写动作的唯一放行口），必须可独立单测。
    """
    pending = chat.get(_ACTIONS_PENDING_KEY) or {}
    if not pending:
        return None
    decision = _pending_decision(message)
    if decision == "approve":
        result = execute_pending(ctx, pending)
        chat.pop(_ACTIONS_PENDING_KEY, None)
        return {"kind": "executed", "pending": pending, "result": result}
    if decision == "cancel":
        chat.pop(_ACTIONS_PENDING_KEY, None)
        return {"kind": "cancelled", "pending": pending, "result": None}
    return None


def _make_workspace_context(default_novel: str = "", active: set[str] | None = None,
                            done: set[str] | None = None,
                            current_book: str = "") -> str:
    """组装工作区上下文（P1 模块级实现，便于单测与动作层复用）。

    工作区**不是**一本书：这里给的是"索引"（书目清单 + 全局资源 + 动作铁律），
    命中细节后再由只读动作（doc_read / chapter_read）按需拉取。
    """
    from src.services.workspace import build_workspace_context, workspace_index

    index = workspace_index(default_novel, active or set(), done or set())
    return build_workspace_context(index, current_book=current_book)

# ---------- 请求体模型（模块级） ----------


class SettingsDocBody(BaseModel):
    rel: str
    content: str


class ChapterSaveBody(BaseModel):
    content: str


class ChatCreateBody(BaseModel):
    agent: str = "chat"
    title: str = ""


class BookSelectBody(BaseModel):
    """工作台当前书目（P1）：空串表示回到工作区。"""

    novel_id: str = ""


class ActionRunBody(BaseModel):
    """直接执行墨师动作（P3）：写动作必须带 confirm=true。"""

    op: str
    args: dict = Field(default_factory=dict)
    confirm: bool = False


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
            "你是「正文写手」子智能体，靠连载追读率吃饭的职业网文作者。\n"
            "写作铁律（违反即失败）：\n"
            "- 文风默认按中文网络小说连载写：段落短（单段 1-4 行）、对话占全章 40% 以上、口语化白话叙述、"
            "内心戏短句直给带情绪；开篇 300 字内进戏，每 800-1200 字一个推进点/爽点，章末必须留具体钩子（画面或台词），"
            "严禁天气景物开篇、大段设定解说、叙述转述对话（『他把情况说了一遍』）、软收尾；\n"
            "- 显示而非告知：每 200 字至少一处具象描写——动作、身体生理反应、神态微表情、环境定调、五感细节、对话中的非语言层；\n"
            "- 字数不够就加戏，不许注水：把对抗写实、把对话打狠、把配角反应写出来、把下一层冲突提前引爆；"
            "禁止抽象形容词堆砌、同义反复、对话复述已知信息；\n"
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
            "衔接连贯性（吃书/重复/时间线错乱）、文笔质量（流水账、过场无推进、水对话、句式单调、口癖重复扣分）。\n"
            "网文口径：本书按中文网络小说连载阅读体验衡量——短段落、高对话密度、口语化叙述、主角内心吐槽、快节奏推进"
            "一律不视为缺点，不得以『太通俗/文学性不足』为由扣分；真正该扣的是没有推进、没有冲突、章末无钩子（软收尾）、"
            "开篇未进戏、信息靠旁白硬塞。\n"
            "输出格式：\n"
            "1. 四维评分 + 总分（四维平均）；\n"
            "2. 问题清单：每条含 维度 / 严重度（major=硬冲突·偏离核心·吃书，minor=其余）/ 描述 / 原文引用（『』，≤40 字）/ 修改建议；\n"
            "3. 总评一句话，直接指出最大问题。不要客套。"
        ),
    },
}

# W3 单一源：第零条正文只在 src/agents/prompt_loader.CONSTITUTION 定义一份，
# 此处按名转出（re-export），不得内嵌副本——本模块历史上内嵌过一份，已删除。
from src.agents.prompt_loader import CONSTITUTION  # noqa: E402 - 集中登记转出

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

            raise HTTPException(400, f"非法路径：{rel!r}")
        store = _store(novel)
        if not store.exists(rel):

            raise HTTPException(404, f"文档不存在：{rel}")
        doc = store.read(rel)
        return JSONResponse(
            {"rel": rel, "title": str(doc.metadata.get("title") or ""), "content": doc.content}
        )

    @app.put("/api/settings/doc")
    def settings_doc_save(body: SettingsDocBody, novel: str = "") -> JSONResponse:
        if not _REL_RE.match(body.rel):

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
        raise HTTPException(404, f"第 {chapter} 章不存在")

    # ══════════ 对话智能体 ══════════

    def _chat_root(novel: str) -> Path:
        """会话落盘根目录（P1：书内 / 工作区两种作用域）。

        · 书内   → ``<novels>/<book>/chats``（既有行为不变）
        · 工作区 → ``<data>/workspace/chats``（墨师全域会话）
        """
        if is_workspace(novel):
            return workspace_data_dir(get_settings().novels_dir)
        return _store(novel).root

    def _chat_dir(novel: str) -> Path:
        return chats_dir(_chat_root(novel))

    def _active_novel() -> str:
        """工作台当前书目（无书时为空串）。"""
        return str(getattr(app_state(), "active_novel", "") or "")

    # 逐章确认：「在对话框发指令 → 开始写下一章」的放行词
    _CONTINUE_RE = re.compile(
        r"(继续|接着写|接着|往下写|下一章|下章|写第\s*\d+\s*章|开始写|开写|go\s*on|continue)",
        re.IGNORECASE,
    )

    def _release_chapter_gate(novel: str, message: str) -> str:
        """流水线停在逐章确认关卡时，用对话框里的一句话放行。

        返回一句可拼进回复的提示；没有关卡或不是放行指令时返回空串。
        工作区会话下 book 可能是空串——此时**不**去猜默认书，宁可不放行。
        """
        if not novel or not _CONTINUE_RE.search(message or ""):
            return ""
        try:
            sess = hub.get_or_create(novel)
            snap = sess.snapshot()
        except Exception:  # noqa: BLE001 - 没有会话就当作无关卡
            return ""
        pending = snap.get("pending") or {}
        if isinstance(pending, dict) and pending.get("type") == "chapter_gate":
            try:
                sess.submit_decision({"action": "approve"})
            except RuntimeError:
                return ""
            return f"已按你的指令开始生成第 {pending.get('next_chapter')} 章。"
        return ""

    def _resolve_scope(novel: str) -> tuple[str, str]:
        """解析会话维度 → (生效会话维度的原值, 该书目 id 或空串)。

        工作区会话不隶属任何书（book 为空），因此上下文与关卡放行都不会
        误落到"默认书"——这正是改造前"没书就什么都做不了"的根因。
        """
        if is_workspace(novel):
            return WORKSPACE, ""
        if novel:
            return novel, novel
        book = _active_novel() or default_novel
        return novel, book

    def _load_chat(novel: str, cid: str) -> dict:
        path = _chat_dir(novel) / f"{cid}.json"
        if not path.exists():
            raise HTTPException(404, f"会话不存在：{cid}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_chat(novel: str, chat: dict) -> None:
        _save_chat_to(_chat_root(novel), chat)

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

    def _workspace_context() -> str:
        """组装工作区上下文（P1）：书目索引 + 全局资源 + 动作铁律。"""
        try:
            active = set(hub.active_ids())
            done = set(hub.done_ids())
        except Exception:  # noqa: BLE001 - 索引缺标记不影响可用性
            logger.warning("工作区上下文：会话状态读取失败，退化为无标记索引")
            active, done = set(), set()
        return _make_workspace_context(default_novel, active, done, _active_novel())

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
        scope_value, _ = _resolve_scope(novel)
        for path in sorted(_chat_dir(scope_value).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
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
                        # P1：作用域随会话落盘（旧会话文件无此字段 → 一律视为书内会话）
                        "scope": data.get("scope", "book"),
                        "novel": data.get("novel", ""),
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
            raise HTTPException(400, f"未知智能体：{body.agent}")
        scope_value, book = _resolve_scope(novel)
        chat = {
            "id": "c-" + uuid.uuid4().hex[:10],
            "agent": body.agent,
            "title": body.title.strip(),
            "created": time.time(),
            "messages": [],
            # P1：会话作用域随会话落盘，重启/切换后仍知道它是"工作区会话"还是"某本书的会话"
            "scope": "workspace" if is_workspace(scope_value) else "book",
            "novel": book,
        }
        _save_chat(scope_value, chat)
        return JSONResponse({"ok": True, "chat": chat})

    @app.get("/api/chats/{cid}")
    def chats_get(cid: str, novel: str = "") -> JSONResponse:
        scope_value, _ = _resolve_scope(novel)
        chat = _load_chat(scope_value, cid)
        pending = chat.get(_ACTIONS_PENDING_KEY)
        if pending:
            chat["pending"] = pending
        return JSONResponse(chat)

    @app.delete("/api/chats/{cid}")
    def chats_delete(cid: str, novel: str = "") -> JSONResponse:
        scope_value, _ = _resolve_scope(novel)
        path = _chat_dir(scope_value) / f"{cid}.json"
        if path.exists():
            path.unlink()
        return JSONResponse({"ok": True})

    @app.post("/api/chats/{cid}/send")
    def chats_send(cid: str, body: ChatSendBody, novel: str = "") -> JSONResponse:
        """主智能体编排：路由决策 →（需要时）委派子智能体 → 动作闭环 → 汇总呈现。

        P1：支持工作区作用域（``novel=__workspace__``）——工作区会话不隶属任何书，
        上下文改由工作区索引提供（书目清单 + 全局资源 + 动作铁律），因此
        "不建书也能对话、也能指挥墨师办事"。
        """
        if not body.message.strip():
            raise HTTPException(400, "消息不能为空")
        scope_value, book = _resolve_scope(novel)
        in_workspace = is_workspace(scope_value)
        chat = _load_chat(scope_value, cid)
        user_msg = body.message.strip()
        chat["messages"].append({"role": "user", "content": user_msg, "ts": time.time()})

        # ── 待确认动作闸门（P3）：只有用户显式"确认"才执行；"取消"丢弃 ──
        # 注意：一旦执行**立即返回**，绝不继续跑 LLM/动作循环——否则同一写动作会被
        # 重新登记为待确认，用户点了"确认"却看到还挂着一条待办（实测踩到过）。
        _pending_before = chat.get(_ACTIONS_PENDING_KEY) or {}
        verdict = _decide_pending_action(chat, user_msg, _action_ctx(scope_value, book))
        if verdict is not None:
            was_executed = verdict["kind"] == "executed"
            if was_executed and verdict["pending"].get("op") == "book_select":
                set_active_novel(
                    str((verdict["pending"].get("args") or {}).get("novel_id") or "")
                )
            text, action_results, _executed_ok = _pending_verdict_payload(verdict)
            chat["messages"].append({
                "role": "assistant", "content": text, "ts": time.time(),
                **({"action_result": action_results[0]} if action_results else {}),
            })
            if not chat.get("title"):
                chat["title"] = user_msg[:20]
            _save_chat(scope_value, chat)
            return JSONResponse({
                "ok": bool(action_results[0]["ok"]) if action_results else True,
                "reply": text,
                "delegations": [],
                "scope": "workspace" if in_workspace else "book",
                "novel": book,
                "actions": action_results,
                "pending": None,
            })

        # 逐章确认：用户这句话如果是「继续/下一章」，直接放行关卡开始写下一章
        gate_note = _release_chapter_gate(book, user_msg)

        context = _workspace_context() if in_workspace else _book_context(scope_value)
        if body.target and book:
            focus = _focus_doc(book, body.target)
            if focus:
                context += (
                    "\n\n【主上下文（用户正在查看的文档）】\n" + focus[:3500]
                )

        registry = None
        delegations: list[dict] = []
        action_block = _actions_prompt(scope_value, book)
        actions_taken: list[dict] = []
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
                        content=(MASTER_SYNTH_PROMPT + "\n\n" + action_block
                                 + "\n\n【作品上下文】\n" + context[:2600]),
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
                        content=(MASTER_PROMPT + "\n\n" + action_block
                                 + "\n\n【作品上下文】\n" + context[:3200]),
                    ),
                    *prior,
                    ChatMessage(role="user", content=user_msg),
                ]
                reply = registry.chat_as(_role("master"), master_messages, temperature=0.8).content

            # ── 4. 墨师动作：解析 → 执行 → 结果回灌二次汇总（P2/P3） ──
            reply, actions_taken = _run_action_cycle(
                reply=reply,
                user_msg=user_msg,
                context=context,
                prior=prior,
                chat=chat,
                registry=registry,
                role=_role("master"),
                scope_value=scope_value,
                book=book,
            )
        except Exception as exc:  # noqa: BLE001 - 模型错误透传给前端
            logger.exception("主智能体编排失败")
            raise HTTPException(502, f"模型调用失败：{type(exc).__name__}: {exc}") from exc

        # 纯动作轮：用确定性文本作答（避免模型二次改写把参数/影响说明写走样）
        if actions_taken and not reply:
            reply = _action_response_text(actions_taken)
        if not reply:
            reply = "（没有产出内容，请再说一次你的需求。）"

        assistant_msg: dict = {"role": "assistant", "content": reply, "ts": time.time()}
        if delegations:
            assistant_msg["delegations"] = delegations
        if actions_taken:
            assistant_msg["actions"] = actions_taken
        chat["messages"].append(assistant_msg)
        if not chat.get("title"):
            chat["title"] = user_msg[:20]
        _save_chat(scope_value, chat)
        if gate_note:
            reply = f"{gate_note}\n\n{reply}"
        pending = chat.get(_ACTIONS_PENDING_KEY) or {}
        return JSONResponse({
            "ok": True,
            "reply": reply,
            "delegations": delegations,
            "scope": "workspace" if in_workspace else "book",
            "novel": book,
            "actions": actions_taken,
            "pending": pending or None,
        })

    # ══════════ 墨师动作（P2/P3）：确认 → 执行 → 审计 ══════════

    def _action_ctx(scope_value: str, book: str) -> ActionContext:
        return ActionContext(
            hub=hub,
            default_novel=default_novel,
            active_novel=_active_novel,
            session_key=scope_value or default_novel,
            book=book,
        )

    def _actions_prompt(scope_value: str, book: str) -> str:
        """动作协议提示块（含当前书目，供墨师解析"这本书"）。"""
        where = f"《{book}》" if book else "工作区（尚未选定书目）"
        return f"{_ACTION_PROMPT_HEAD}\n\n当前上下文：{where}"

    def _run_actions(scope_value: str, book: str, actions: list[dict],
                     *, confirmed: bool) -> list[dict]:
        """执行墨师给出的动作清单（只读自动执行；写动作未确认时只登记）。"""
        ctx = _action_ctx(scope_value, book)
        out: list[dict] = []
        for item in actions[:MAX_ACTIONS_PER_TURN]:
            if not isinstance(item, dict):
                continue
            op = str(item.get("op", "")).strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            result = actions_execute(op, args, ctx=ctx, execute_write=confirmed)
            out.append({
                "op": result.op,
                "status": result.status,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "data": _shrink_action_data(result.data),
            })
            # 建书后立刻把工作台当前书目切过去，用户不必再说一句"切到新书"
            if result.ok and result.status == "ok" and result.op == "book_create":
                nid = (result.data or {}).get("novel") or args.get("novel_id")
                if nid:
                    set_active_novel(str(nid))
        # book_select 动作：同步后端"当前书目"
        for item in actions[:MAX_ACTIONS_PER_TURN]:
            if isinstance(item, dict) and str(item.get("op")) == "book_select" and confirmed:
                nid = str((item.get("args") or {}).get("novel_id") or "")
                if nid:
                    set_active_novel(nid)
        return out

    def _run_action_cycle(*, reply: str, user_msg: str, context: str, prior: list,
                          chat: dict, registry, role: str, scope_value: str,
                          book: str, allow_planner: bool = True) -> tuple[str, list[dict]]:
        """一轮动作闭环：解析动作块 → 执行（只读自动 / 写动作只登记）→ 结果回灌汇总。

        返回 (最终回复, 动作回执列表)。无动作时原样返回模型回复。
        allow_planner=False 用于"本轮已经执行过已确认动作"的情形：不再补规划，
        避免同一写动作被重复登记。
        """
        from src.llm.base import ChatMessage

        clean, actions = _parse_actions(reply)
        if not actions:
            if not allow_planner:
                # 本轮已经执行过一个已确认动作：绝不再补规划，
                # 否则同一个写动作会被登记第二次（实测：用户确认后又被挂成待确认）。
                return clean, []
            if _is_affirmative(user_msg):
                # 优先确定性转换：用户确认 + 上文是要建书 → 直接从原话取标识建 pending，
                # 不依赖模型（实测模型会漏 args，如 novel_id 缺失，导致"确认"后什么都没发生）。
                actions = _fallback_action_from_history(chat, user_msg)
                if actions:
                    logger.info("用户确认：确定性转换出 %d 个动作（跳过规划调用）", len(actions))
            if not actions and _looks_actionable(user_msg):
                # 补漏：消息明确要求办事 → 规划调用。
                actions = _plan_actions_with_model(registry, role, user_msg, prior, context)
            elif not actions:
                # 用户在对上一条提议说"确认"但系统没登记待办 → 按"已确认"再规划一次
                actions = _plan_actions_with_model(registry, role, user_msg, prior, context,
                                                   after_confirm=True)
                if actions:
                    logger.info("用户确认但无待办：按已确认补规划出 %d 个动作", len(actions))
            if not actions:
                return clean, []

        results = _run_actions(scope_value, book, actions, confirmed=False)
        pending = next((r for r in results if r["status"] == "pending_confirm"), None)
        if pending:
            chat[_ACTIONS_PENDING_KEY] = {
                "op": pending["op"],
                "args": next((a.get("args") for a in actions
                              if isinstance(a, dict)
                              and str(a.get("op")) == str(pending["op"])), {}),
                "impact": pending.get("summary", ""),
            }
        else:
            chat.pop(_ACTIONS_PENDING_KEY, None)

        receipt = _summary_from_results(results)
        if not receipt:
            return clean, results

        # 结果回灌：让墨师用自然语言汇总（并在纯动作轮里给出可读答复）
        summarize = [
            ChatMessage(role="system",
                        content=(_ACTION_PROMPT_HEAD
                                 + "\n以下是系统刚执行完的动作结果，请据此作答；"
                                   "不要重复动作块，除非还需要新的动作。\n\n" + receipt)),
            *prior,
            ChatMessage(role="user", content=user_msg),
        ]
        try:
            final = registry.chat_as(role, summarize, temperature=0.4).content
        except Exception:  # noqa: BLE001 - 汇总失败退回确定性文本
            logger.exception("动作结果汇总失败，退回确定性答复")
            final = ""
        final_clean, _extra = _parse_actions(final)
        # 汇总可能"只剩动作块"或被剥空 —— 这种情况绝不能把空串返回给用户，
        # 否则界面上是一片空白（实测遇到过：动作真的执行了，回复却是空的）。
        if not final_clean.strip():
            logger.info("动作结果汇总为空，改用确定性回执文本")
            final_clean = _action_response_text(results)
        # 若本轮登记了待确认写动作（且没有已执行结果），回执必须包含"未执行"的事实，
        # 否则模型可能顺势说成"正在创建"，用户以为已经落盘（实测踩到过）。
        if pending is not None and not any(r["status"] == "ok" for r in results):
            notice = f"\n\n（尚未执行：需要你确认后我才会真正执行。操作：{pending['op']}）"
            if notice.strip() not in final_clean:
                final_clean = (final_clean + notice).strip()
        return final_clean, results

    @app.get("/api/actions")
    def actions_manifest() -> JSONResponse:
        """动作清单（前端/调试用；不含 handler）。"""
        return JSONResponse({"actions": actions_manifest_list()})

    @app.get("/api/actions/audit")
    def actions_audit(limit: int = 100) -> JSONResponse:
        """墨师操作审计记录（倒序）。

        对外只暴露必要字段：``args_digest`` 等内部指纹不返回（日志文件里保留，
        便于排障，但没必要出现在界面上）。
        """
        capped = max(1, min(int(limit or 100), 500))
        records = []
        for r in read_audit(capped):
            records.append({
                "ts": r.get("ts", 0),
                "session": r.get("session", ""),
                "book": r.get("book", ""),
                "op": r.get("op", ""),
                "scope": r.get("scope", ""),
                "status": r.get("status", ""),
                "ok": bool(r.get("ok")),
                "elapsed_ms": r.get("elapsed_ms", 0),
                "summary": r.get("summary", ""),
                "error": r.get("error", ""),
            })
        return JSONResponse({"records": records})

    @app.post("/api/actions/run")
    def actions_run(body: ActionRunBody, novel: str = "") -> JSONResponse:
        """直接执行一个动作（前端"确认"按钮走这里）。

        写动作必须带 ``confirm=true``——与对话确认语义一致；只读动作不走本通道。
        """
        scope_value, book = _resolve_scope(novel)
        op = body.op.strip()
        if not actions_is_write(op):
            raise HTTPException(400, f"动作 {op} 不是写动作，请走只读通道/对话")
        if not body.confirm:
            result = actions_execute(op, body.args, ctx=_action_ctx(scope_value, book),
                                     execute_write=False)
            pending = {"op": op, "args": body.args, "impact": result.summary}
            return JSONResponse({"ok": True, "status": "pending_confirm", "pending": pending})
        result = actions_execute(op, body.args, ctx=_action_ctx(scope_value, book),
                                 execute_write=True)
        if result.ok and op == "book_select":
            set_active_novel(str(body.args.get("novel_id") or ""))
        return JSONResponse({
            "ok": result.ok,
            "status": result.status,
            "summary": result.summary,
            "error": result.error,
            "data": _shrink_action_data(result.data),
        })

    @app.post("/api/chats/{cid}/action")
    def chat_action(cid: str, novel: str = "") -> JSONResponse:
        """执行会话里的待确认动作（前端确认卡"确认"按钮）。"""
        scope_value, book = _resolve_scope(novel)
        chat = _load_chat(scope_value, cid)
        pending = chat.get(_ACTIONS_PENDING_KEY)
        if not pending:
            raise HTTPException(409, "当前没有待确认的动作")
        ctx = _action_ctx(scope_value, book)
        result = execute_pending(ctx, pending)
        chat.pop(_ACTIONS_PENDING_KEY, None)
        chat["messages"].append({
            "role": "assistant",
            "content": (f"✅ {pending.get('op')}：{result.summary}" if result.ok
                        else f"❌ {pending.get('op')} 执行失败：{result.error}"),
            "ts": time.time(),
            "action_result": {"op": result.op, "ok": result.ok,
                              "status": result.status, "error": result.error},
        })
        if result.ok and pending.get("op") == "book_select":
            set_active_novel(str((pending.get("args") or {}).get("novel_id") or ""))
        _save_chat(scope_value, chat)
        return JSONResponse({
            "ok": result.ok,
            "status": result.status,
            "summary": result.summary,
            "error": result.error,
            "data": _shrink_action_data(result.data),
        })

    @app.delete("/api/chats/{cid}/action")
    def chat_action_cancel(cid: str, novel: str = "") -> JSONResponse:
        """取消会话里的待确认动作。"""
        scope_value, _book = _resolve_scope(novel)
        chat = _load_chat(scope_value, cid)
        pending = chat.pop(_ACTIONS_PENDING_KEY, None)
        _save_chat(scope_value, chat)
        return JSONResponse({"ok": True, "cancelled": bool(pending)})

    # ══════════ 工作台当前书目（P1：工作区会话的操作目标） ══════════

    @app.get("/api/book-select")
    def book_select_get() -> JSONResponse:
        """读取工作台当前书目（空串 = 工作区/未选书）。"""
        return JSONResponse({"novel_id": _active_novel() or ""})

    @app.put("/api/book-select")
    def book_select_set(body: BookSelectBody) -> JSONResponse:
        """设置工作台当前书目（前端 openBook 同步调用；空串 = 回到工作区）。

        只记录"用户在看哪本书"，**不做**任何数据读写。
        """
        nid = body.novel_id.strip()
        if nid and not _NOVEL_ID_RE.match(nid):
            raise HTTPException(400, f"非法书名标识：{nid!r}")
        set_active_novel(nid)
        exists = bool(nid) and (get_settings().novels_dir / nid).is_dir()
        return JSONResponse({"ok": True, "novel_id": nid, "exists": exists})

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
                raise HTTPException(404, f"自定义 Skill 不存在：{sid}")
            post = frontmatter.load(str(path))
            sections.append(f"## 自定义约束：{post.metadata.get('title', sid)}\n\n{post.content}")
        for pid in body.pack_ids:
            try:
                sections.append(_pack_digest(pid))
            except FileNotFoundError as exc:
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
