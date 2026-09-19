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
from src.web.actions import ACTIONS as _ACTION_REGISTRY
from src.web.actions import (
    MAX_ACTIONS_PER_TURN,
    ActionContext,
    consume_preview,
    execute_pending,
    forget_preview,
    is_placeholder_value,
    normalize_args,
    normalize_op,
    read_audit,
    register_preview,
)
from src.web.actions import (
    execute as actions_execute,
)
from src.web.actions import (
    manifest as actions_manifest_list,
)
from src.web.scope import WORKSPACE, chats_dir, is_workspace
from src.web.scope import workspace_dir as workspace_data_dir
from src.web.server import classify_failure, redact_secrets

logger = get_logger(__name__)


def _with_constitution(system: str) -> str:
    """给任意 system 提示词补第零条（幂等）。

    所有面向用户的交互式链路都必须经过它：路由、规划、动作协议、主智能体作答。
    漏一处就会出现"这条路径上的指令没有无条件执行条款"（真实缺口：规划器与动作协议块）。
    """
    from src.agents.prompt_loader import ensure_constitution

    return ensure_constitution(system)

#: 失败来源 → 中文标签（错误文案与 X-Inkforge-Error-Source 响应头共用）
_SOURCE_LABEL = {
    "network": "网络连接中断",
    "provider_auth": "接入点鉴权失败",
    "config": "模型配置错误",
    "parse": "模型输出无法解析",
    "engine": "引擎内部错误",
}

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


def _content_echo(reply: str, results: list[dict]) -> str:
    """读类动作的正文"确定性回显"：模型只复述摘要、没把正文给用户时补上。

    真实缺口（真机 F5 两次实测）：`learning_read` 成功、`data.content` 里就是全文，
    但墨师只回了一句"学习仿写《ln-xxx》全文如下（共 2191 字）。"——**正文一个字没有**。
    用户体感是"点开还是看不到内容"。这里不依赖模型自觉：凡是本次动作取回了长文本
    （content 字段），而回复里没有**它的实质内容行**，就把正文拼在回复末尾。

    判据用"内容的前几行是否出现在回复里"，而不是"前 40 字"：
    标题行形如 `# 学习仿写：xxx`，会被"全文如下（xxx）"这种摘要句意外命中，
    导致明明没给正文却判成已给（第一版就是这么漏的）。
    """
    chunks: list[str] = []
    for r in results or []:
        if r.get("status") != "ok":
            continue
        data = r.get("data")
        if not isinstance(data, dict):
            continue
        content = data.get("content")
        if not isinstance(content, str) or len(content) < 120:
            continue
        probes = [line.strip() for line in content.splitlines() if len(line.strip()) >= 20][:3]
        if probes and any(p in (reply or "") for p in probes):
            continue                    # 模型已经把正文实质内容带上了，不重复
        label = str(data.get("title") or data.get("id") or r.get("op") or "")
        chunks.append(f"【{label} 正文】\n{content}")
    if not chunks:
        return reply or ""
    joined = "\n\n".join(chunks)
    return f"{reply}\n\n{joined}".strip() if reply else joined


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
                out[key] = (value[:2000] +
                            f"…（共 {len(value)} 字，界面只展示前 2000 字；"
                            "需要继续看后半段请让我按段落继续读）")
            elif isinstance(value, (dict, list)):
                out[key] = _shrink_action_data(value)
            else:
                out[key] = value
        return out
    if isinstance(data, list):
        return [_shrink_action_data(v) for v in data[:50]]
    return data


#: 用户话术 → 动作名前缀（只用于判断"模型自己选的动作是否答非所问"）。
#: 实测：模型会在正文里说"动作清单里没有学习仿写查询接口"却只调了 book_list ——
#: 这时补一次规划调用就能命中正确工具，所以需要一个"答非所问"的判据。
_TOPIC_TO_OP_PREFIX: tuple[tuple[tuple[str, ...], str], ...] = (
    (("学习仿写", "学习成果", "样本拆解", "文风学习", "剧情学习", "学习历史"), "learning"),
    (("素材", "参考资料"), "material"),
    (("技能包", "蒸馏", "16 维", "十六维"), "skill"),
    (("创作约束", "自定义约束", "风格约束"), "constraint"),
    (("模型绑定", "接入点", "模型配置"), "model_config"),
    (("书目", "有哪些书", "书架", "作品列表"), "book_list"),
    (("章节", "第几章"), "chapter"),
    (("大纲",), "outline"),
    (("世界观", "人物档案", "设定文档", "资料库"), "doc"),
)


def _topic_ops(message: str) -> set[str]:
    """用户话术对应的动作名前缀集合（空集 = 无法判定主题）。"""
    text = message or ""
    return {prefix for words, prefix in _TOPIC_TO_OP_PREFIX
            if any(w in text for w in words)}


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

    抽成模块级纯函数，便于单测锁定"确认/取消/拒绝"三条路径的文案与回执形态。
    """
    kind = verdict.get("kind")
    result = verdict.get("result")
    pending = verdict.get("pending") or {}
    op = pending.get("op")
    if kind == "refused":
        return (f"⚠ 已阻止执行 {op}：{verdict.get('reason', '')}。"
                "请重新发起一次操作，我给出影响说明后再确认。"), [], False
    if kind == "executed" and result is not None:
        if result.ok:
            text = f"✅ 已执行 {op}：{result.summary}"
        else:
            # 确认后仍失败：必须让用户看清"什么都没落盘"，不能含糊成"已安排"
            text = (f"❌ {op} 执行失败：{result.error}\n"
                    "本次**没有写入任何数据**。请按上面的原因补充或更正参数后再说一次。")
        receipts = [{
            "op": result.op, "status": result.status, "ok": result.ok,
            "summary": result.summary, "error": result.error,
            "data": _shrink_action_data(result.data),
        }]
        return text, receipts, True
    return f"已取消待确认动作：{op}。", [], False


_IDENT_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,40}")

#: 中文书名 → ASCII 标识的兜底前缀（书名标识必须是 ASCII，见 library.NOVEL_ID_RE）
_NOVEL_SLUG_PREFIX = "novel"

#: 从用户原话里抠"书名"的句式（书名本身可以是中文，标识另生成）
_TITLE_PATTERNS = (
    r"《([^》]{1,40})》",
    r"书名\s*(?:就叫|叫做|叫|是|为|：|:)\s*[「『\"']?([^」』\"'\n，,。；;]{1,40})",
    r"(?:写|做|开|建)(?:一)?本\s*[「『\"']?([^」』\"'\n，,。；;]{2,40}?)(?:[」』\"']|的?(?:书|小说)|，|,|$)",
)


def _looks_like_title(raw: str) -> bool:
    r"""像书名吗：去语气词后含中日韩字符、或以书名号/引号包裹。

    为什么要这道闸：`_TITLE_PATTERNS` 的第三条会把 `建一本叫 \`my-book\` 的书`
    抠成"叫 `my-book`"，那是**标识**不是书名；拿它当书名会让确认卡显示错误信息。
    因此这里先剥掉"叫/叫做/的书"这类语气与量词，剩纯 ASCII 且未被包裹 → 不算书名。
    """
    original = (raw or "").strip().strip("`\"' ")
    value = original
    for prefix in ("叫做", "叫", "名叫", "名为", "是", "为"):
        if value.startswith(prefix):
            value = value[len(prefix):].strip().strip("`\"' ：:》」』")
            break
    for suffix in ("的书", "小说", "这本书", "书"):
        if value.endswith(suffix):
            value = value[: -len(suffix)].strip().strip("`\"' ：:《「『»")
            break
    if not value:
        return False
    if any("\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff"
           or "\uac00" <= ch <= "\ud7af" for ch in value):
        return True
    # 全 ASCII：只接受"被书名号/引号包裹"的形态
    stripped = original.strip()
    return stripped[:1] in "《「『“" or stripped[-1:] in "》」』”"


def _guess_title_from_user(message: str) -> str:
    """从用户原话里抠出书名（可含中文；用于生成 ASCII 标识与确认卡展示）。"""
    text = message or ""
    for pat in _TITLE_PATTERNS:
        m = re.search(pat, text)
        if not m:
            continue
        raw = m.group(1)
        if not _looks_like_title(raw):
            continue
        title = raw.strip().strip("`\"' ")
        for prefix in ("叫做", "叫", "名叫", "名为"):
            if title.startswith(prefix):
                title = title[len(prefix):].strip().strip("`\"' ：:》」』")
                break
        for suffix in ("的书", "这本书", "小说"):
            if title.endswith(suffix):
                title = title[: -len(suffix)].strip().strip("`\"' ：:《「『»")
                break
        if title and not is_placeholder_value(title):
            return title[:40]
    return ""


def _slug_from_title(title: str) -> str:
    """中文/任意书名 → 可直接用作目录名的 ASCII 标识（稳定、可读、不冲突）。"""
    ascii_part = re.sub(r"[^A-Za-z0-9]+", "-", (title or "").strip().lower()).strip("-")
    ascii_part = ascii_part[:24].strip("-")
    stamp = time.strftime("%y%m%d")
    suffix = uuid.uuid4().hex[:4]
    return f"{ascii_part}-{stamp}-{suffix}" if ascii_part else f"{_NOVEL_SLUG_PREFIX}-{stamp}-{suffix}"


def _guess_novel_id(message: str) -> str:
    """从用户原话里猜书目标识（``书名标识就用 xxx`` / ``叫 xxx`` / 反引号包裹的 id）。

    只用于**建书**这一个动作的兜底：标识本来就是用户随手起的名字，
    猜测失败也没关系（会如实回执"缺少参数"），但猜中就能把整条链走通。
    占位符（`novel_id` / `书名标识` 这类）一律不算猜中——它们永远是错的。
    """
    text = message or ""
    patterns = (
        r"(?:书名标识|标识|novel_id|book_id|id)\s*(?:就用|用|是|为|叫|：|:)\s*[`\"']?([A-Za-z][A-Za-z0-9_-]{2,40})",
        r"[`\"']([A-Za-z][A-Za-z0-9_-]{3,40})[`\"']",
    )
    for pat in patterns:
        m = re.search(pat, text)
        if m and not is_placeholder_value(m.group(1)):
            return m.group(1)
    return ""


def _fallback_action_from_history(chat: dict, user_msg: str) -> list[dict]:
    """兜底：用户已确认但本轮没能拿到动作时，按上文尝试重建一个建书动作。

    实测缺口两处（都在本轮修复）：
      ① 模型只说"我将按以下参数新建《X》"、不给动作块 → 用户回"确认"时整条链断掉；
      ② 判断只看字面量 ``book_create``，而模型写的是 ``create_book`` / ``new_book``
         这类别名（别名表里有，但这行代码不查表）→ 兜底失效。
    现在先把上文的动作名**过一遍别名折算**，再做判断。
    """
    recent = " ".join(str(m.get("content", ""))[:600]
                      for m in (chat.get("messages") or [])[-8:]
                      if m.get("role") == "assistant")
    title = _guess_title_from_user(user_msg)
    has_ascii_id = bool(_guess_novel_id(user_msg))
    wants_create = any(
        w in (user_msg or "") for w in ("建", "新建", "创建", "开一本", "写一本", "来一本",
                                        "开书", "做一本", "弄一本", "起一本"))
    # 兜底条件：① 明确的建书意图 + 给了可用的名字（中文书名或 ASCII 标识）；
    #           ② 或虽没写"建"字，但**显式给了书名标识**（"书名标识就用 dark-crime"
    #              本身就是建书特有的话术，用户不可能在问别的事）。
    # 目的是既不把"建书流程怎么走？"变成建书卡，也不漏掉真机里出现过的那些写法。
    wants_create = (wants_create and bool(title or has_ascii_id)) or has_ascii_id
    # 把上文里出现过的动作写法折算成规范名后再判断（create_book → book_create）
    mentioned = set()
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,30}", recent):
        canonical = normalize_op(raw)
        if canonical in _ACTION_REGISTRY:
            mentioned.add(canonical)
    hinted = ("book_create" in mentioned
              or ("新建" in recent and "书" in recent)
              # ③ "再帮我建一本《雾都回响》"这类**只给中文书名、不给标识**的续写式请求，
              #    模型有时整轮不输出动作块 → 建书动作根本没被登记（真机 E8 实测）。
              #    此时以用户原话为第一事实源直接兜底；写动作仍走确认闸门，猜错可取消。
              or wants_create)
    if not hinted:
        return []
    # 顺序很关键：先信用户这一轮的原话，再退回上文里出现过的标识，
    # 最后兜"中文书名 → 生成 ASCII 标识"。**占位符一律不算命中**——
    # 否则会把提示词里的字面量 `novel_id` 建成一本真书（真实事故）。
    for source in (user_msg, recent):
        nid = _guess_novel_id(source) or _guess_arg_from_user("novel_id", source)
        if nid and not is_placeholder_value(nid):
            logger.info("用户原话/上文可确定书名标识：兜底重建 book_create（novel_id=%s）", nid)
            return [{"op": "book_create", "args": {"novel_id": nid, "mode": "pipeline"}}]
    if title:
        nid = _slug_from_title(title)
        logger.info("用户只给了中文书名《%s》：兜底生成 ASCII 标识 %s 并登记建书", title, nid)
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
    system = _with_constitution(
        "你是工具调用规划器。根据用户消息与上下文，判断需要调用哪些工具。\n"
        "只输出一个 JSON 对象，形如：{\"actions\": [{\"op\": \"book_list\", \"args\": {}}]}。\n"
        "op 必须**逐字**使用下面清单里的名字；args 用清单里的参数名。\n"
        "只读工具用于取事实；写操作也照常列入（系统会先请用户确认，不会直接执行）。\n"
        "**参数取值约定**：`mode` 只能是 pipeline 或 interactive；"
        "**新建书请用 `novel_id`（不是 book_key / id / 书名标识）**；"
        "`novel` 传已存在书目的标识（上下文里出现过的那本）；未提到书目时可省略 novel。\n"
        "**用户话术 → 工具对照（按用户用词直接选，不要绕到别的工具）**：\n"
        "  · 学习仿写 / 学习成果 / 样本拆解 / 剧情学习 / 文风学习 → learning_list（列历史）/ learning_read\n"
        "  · 素材 / 素材库 / 参考资料 → material_list / material_read\n"
        "  · 技能包 / 蒸馏 / 16 维 → skill_list\n"
        "  · 创作约束 / 风格约束 → constraint_list\n"
        "  · 书 / 书目 / 作品 → book_list；章节 → chapter_list；设定 / 世界观 / 人物 → doc_list\n"
        "用户明确问哪一类，就只调那一类的列取工具（配套需要时再补列取，不要用别的工具凑）。\n"
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
    return _backfill_from_user_message(out, user_msg)


#: 动作参数 → "从用户原话里取值的兜底提示词"。模型经常漏填这些，而用户其实说得很清楚。
_ARG_VALUE_PATTERNS: dict[str, tuple[str, ...]] = {
    "novel_id": (
        r"书名标识|标识|novel_id|book_id",
        r"就叫|叫|用|填|取名|命名为",
        r"[`\"'：:]\s*([A-Za-z][A-Za-z0-9_-]{2,40})",
    ),
}


def _guess_arg_from_user(key: str, message: str) -> str:
    r"""从用户原话里猜一个参数取值（目前只做 novel_id，建书最常见）。

    真实缺口（真机 G1 实测）：用户写"书名**标识**就用 dark-crime"时，
    旧正则把分隔符组写成可选（`\s*`），于是 `书名标识` 之后**没吃空格**就要求 `就用`，
    整条匹配失败 → 拿不到标识 → 兜底登记不了建书动作。这里把分隔符组改成
    "非空分隔符 + 可选空白"，并补两条更宽松的写法（`book id：x` / 全角括号包裹）。
    """
    text = message or ""
    if key != "novel_id":
        return ""
    patterns = (
        # 书名标识 / 标识 / novel_id / book id ... ：（分隔符可有可无，出现时必须吃到空白）
        # 注意：多词变体排在前面（`book id` 在 `book` 之前），否则 `book id：x` 会先命中
        # `book` 并把值抠成 "id"。
        r"(?:书名标识|书目标识|novel_id|book_id|book\s*id|book\s*key|标识)\s*"
        r"(?:[（(<\[][^）)\]>]{0,12}[）)\]>]\s*)?"      # 允许 `标识（book id）` 这类括注
        r"(?:(?:就用|用|是|为|叫|就叫|设为|填|取名|：|:|＝|=|（|\()\s*)?[`\"']?"
        r"([A-Za-z][A-Za-z0-9_-]{2,40})",
        r"[`\"']([A-Za-z][A-Za-z0-9_-]{3,40})[`\"']",
    )
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        value = m.group(1)
        # "book id" 这类词被拆开后可能只剩下通用词，那不是标识
        if is_placeholder_value(value) or value.strip().lower() in {
                "book", "novel", "id", "key", "name", "title", "the", "and"}:
            continue
        return value
    # 只给了中文书名（"帮我建一本《雨夜委托》"）→ 中文不能直接做标识，
    # 生成 ASCII 标识，书名归书名。确认卡上会把两者都写清楚。
    title = _guess_title_from_user(text)
    return _slug_from_title(title) if title else ""


def _backfill_from_user_message(actions: list[dict], user_msg: str) -> list[dict]:
    """补齐模型漏掉的必填参数（值从用户原话里取）。

    实测：用户已经说"标识就用 xx"，模型仍会漏 ``novel_id``（或写成 title/book_key/id），
    结果动作直接失败、用户点了确认却什么都没发生。这里做一层确定性补齐，顺序：
      ① 参数名折算已由 ``normalize_args`` 负责（book_key/id/title → novel_id）；
      ② 这里负责**把空值填上**，来源是用户原话（含中文书名 → 生成 ASCII 标识）。
    """
    for item in actions:
        op = normalize_op(str(item.get("op") or ""))
        item["op"] = op
        args = normalize_args(op, item.get("args") or {})
        if op == "book_create":
            nid = str(args.get("novel_id") or "").strip()
            if not nid or is_placeholder_value(nid):
                guess = _guess_arg_from_user("novel_id", user_msg) or _guess_novel_id(user_msg)
                if guess:
                    args["novel_id"] = guess
                    logger.info("规划结果缺少可用 novel_id，已从用户原话补齐：%s", guess)
        item["args"] = args
    return actions


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

    · 用户说"确认/执行" → 校验预览凭证后真正执行，返回 {"kind": "executed", ...}
    · 用户说"取消/算了" → 丢弃待办与预览登记，返回 {"kind": "cancelled", ...}
    · 其它话语 → 返回 None（视为新话题，待办保留在原处）

    抽成模块级函数：这是安全关键路径（写动作的唯一放行口），必须可独立单测。
    """
    pending = chat.get(_ACTIONS_PENDING_KEY) or {}
    if not pending:
        return None
    decision = _pending_decision(message)
    if decision == "approve":
        refusal = consume_preview(ctx, pending)
        if refusal:
            # 预览凭证缺失/过期 → 不执行，回来执意提醒用户重新预览（不静默落盘）
            return {"kind": "refused", "pending": pending, "result": None,
                    "reason": refusal}
        result = execute_pending(ctx, pending)
        chat.pop(_ACTIONS_PENDING_KEY, None)
        return {"kind": "executed", "pending": pending, "result": result}
    if decision == "cancel":
        forget_preview(ctx, str(pending.get("op") or ""),
                       pending.get("args") if isinstance(pending.get("args"), dict) else None)
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
    """直接执行墨师动作（P3）：写动作必须带 confirm=true，且需匹配一次真实预览。"""

    op: str
    args: dict = Field(default_factory=dict)
    confirm: bool = False
    token: str = ""


class ChatSendBody(BaseModel):
    message: str
    target: dict | None = None  # {kind, key}：右侧选中文档作为本轮主上下文


class BindingsBody(BaseModel):
    custom_skill_ids: list[str] = Field(default_factory=list)
    pack_ids: list[str] = Field(default_factory=list)
    #: true = 追加合并（未列出的已绑资源保持不动）；false = 整体替换（默认，勾选面板语义）
    merge: bool = False


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

# W3 单一源（同上）：约束提炼智能体的默认提示词只在 src/services/constraint_forge.py
# 定义一份，此处按其常量引用，避免"改了一处另一处不生效"。第零条由 agent_prompt() 运行时追加。
from src.services import constraint_forge  # noqa: E402 - 集中登记转出

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
    # 风格工坊「约束提炼」页的专职智能体：与五个阶段子智能体同级注册，
    # 因此自带"可在智能体设置里查看/改提示词 + 运行时必带第零条"两项既有能力。
    # 它不是对话/路由用的阶段智能体（不进 ROUTER_PROMPT 的委派枚举）。
    constraint_forge.AGENT_KEY: {
        "label": constraint_forge.AGENT_LABEL,
        "prompt": constraint_forge.SYSTEM_PROMPT,
    },
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
            # 2026-09-19 对抗排查修正：原先这里抛**裸 ValueError** → FastAPI 兜成 500
            # 「Internal Server Error」。路径校验本身是生效的（点号与斜杠都进不来，**不构成穿越**），
            # 但非法入参被报成服务端错误：用户看不到原因、日志里全是假故障。
            # 本模块其它 `_store` 兄弟实现（inkforge_extra / inkforge_windows）都返回 400，此处对齐。
            raise HTTPException(400, f"非法书名标识：{nid!r}")
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
        # 会话型智能体白名单：**排除约束提炼智能体** —— 它是风格工坊页面的专用智能体，
        # 数据通路只有"用户文本 → 约束条目"；一旦让它当会话智能体，就会走进带作品上下文
        # 的对话链路（那正是它承诺不看的东西）。隔离要成立，入口也得一起关。
        allowed = (set(AGENT_PRESETS) | {"chat"}) - {constraint_forge.AGENT_KEY}
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
        if _pending_before:
            logger.info("待确认闸门：用户=%r 裁决=%s", user_msg, (verdict or {}).get("kind"))
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
            # 归因：网络断线 / Key 失效 / 配置错误 / 引擎内部异常 → 前端据此给确切提示
            source, hint = classify_failure(exc)
            # 坑（2026-09-19 实测，同 inkforge_forge）：HTTP 响应头只能是 latin-1，
            # 中文 hint 放进 X-Inkforge-Hint 会让 starlette 在组装响应时抛
            # UnicodeEncodeError —— 本意是"给确切原因"的失败通路，反而变成 500 且归因全丢。
            # 因此：响应头只留 ASCII 来源码，可执行提示并入 detail 正文。
            raise HTTPException(
                502,
                f"模型调用失败（{_SOURCE_LABEL.get(source, source)}）："
                f"{redact_secrets(f'{type(exc).__name__}: {exc}')}\n\n建议：{hint}",
                headers={"X-Inkforge-Error-Source": source},
            ) from exc

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

    def _action_ctx(scope_value: str, book: str, *, user_msg: str = "",
                    title_hint: str = "") -> ActionContext:
        ctx = ActionContext(
            hub=hub,
            default_novel=default_novel,
            active_novel=_active_novel,
            session_key=scope_value or default_novel,
            book=book,
        )
        # 交付给动作层的"用户原话"侧信息（不进 args、不影响预览键）：
        #   · title_hint：建书确认卡上展示的中文书名（从用户原话抠出来的）
        #   · user_msg：确定性兜底用（模型漏参数时从原话补）
        ctx.extra["title_hint"] = title_hint or (_guess_title_from_user(user_msg) if user_msg else "")
        ctx.extra["user_msg"] = user_msg
        return ctx

    def _actions_prompt(scope_value: str, book: str) -> str:
        """动作协议提示块（含当前书目，供墨师解析"这本书"）。

        带第零条：动作块会作为 system 消息单独送达（动作轮汇总、补规划），那条路径上
        没有主提示词兜底，缺了它就会出现"这条链路上的指令没有无条件执行条款"。
        """
        where = f"《{book}》" if book else "工作区（尚未选定书目）"
        return _with_constitution(f"{_ACTION_PROMPT_HEAD}\n\n当前上下文：{where}")

    def _run_actions(scope_value: str, book: str, actions: list[dict],
                     *, confirmed: bool, dry_run: bool = False,
                     user_msg: str = "") -> list[dict]:
        """执行墨师给出的动作清单。

        · dry_run=True（默认）：写动作只登记待确认，不落盘；
        · dry_run=False + confirmed=True：用户已确认，写动作直接执行。
        · user_msg：用户原话（确定性兜底 + 建书确认卡展示中文书名用）。
        """
        ctx = _action_ctx(scope_value, book, user_msg=user_msg)
        out: list[dict] = []
        for item in actions[:MAX_ACTIONS_PER_TURN]:
            if not isinstance(item, dict):
                continue
            op = str(item.get("op", "")).strip()
            args = item.get("args") if isinstance(item.get("args"), dict) else {}
            result = actions_execute(op, args, ctx=ctx,
                                     execute_write=(confirmed and not dry_run))
            out.append({
                "op": result.op,
                "status": result.status,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "args": result.args,
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
        confirmed_round = False          # 本轮动作是否来自"用户刚确认过的重规划"
        if actions:
            # 模型自己给了动作，但要防"答非所问"：用户问学习仿写、它却只列书目。
            # 命中这种情况就当作没给动作，走补漏规划。
            wanted = _topic_ops(user_msg)
            got = {str(a.get("op", "")) for a in actions if isinstance(a, dict)}
            if (wanted and got
                    and not any(op.startswith(p) for op in got for p in wanted)
                    and not any(o.startswith(("book_create", "book_select", "gen_",
                                              "demo_", "interactive_", "book_delete",
                                              "book_bind", "material_create",
                                              "constraint_create"))
                            for o in got)):
                logger.info("模型动作答非所问（wanted=%s got=%s），改走补漏规划",
                            sorted(wanted), sorted(got))
                actions = []
        if not actions:
            if not allow_planner:
                # 本轮已经执行过一个已确认动作：绝不再补规划，
                # 否则同一个写动作会被登记第二次（实测：用户确认后又被挂成待确认）。
                return clean, []
            if _is_affirmative(user_msg):
                # 用户说"确认"但没有待办（上一轮模型只问了、没登记）：
                # 这意味着用户已经同意过，本轮的动作应当**直接执行**而不是再挂一张确认卡。
                actions = _fallback_action_from_history(chat, user_msg)
                if actions:
                    confirmed_round = True
                    logger.info("用户确认：确定性转换出 %d 个动作并直接执行", len(actions))
            if not actions and _looks_actionable(user_msg):
                # 补漏：消息明确要求办事 → 规划调用（写动作仍会走确认闸门）。
                actions = _plan_actions_with_model(registry, role, user_msg, prior, context)
            elif not actions:
                actions = _plan_actions_with_model(registry, role, user_msg, prior, context,
                                                   after_confirm=True)
                if actions:
                    confirmed_round = True
                    logger.info("用户确认但无待办：按已确认补规划出 %d 个动作并直接执行",
                                len(actions))
            if not actions:
                return clean, []

            # 统一规范化一次：参数名折算（book_key/id/title → novel_id）、枚举折算、
            # 占位符过滤都会在此落地，后续"补建书标识/登记预览/执行"用的都是这一份。
            actions = [{"op": normalize_op(str(a.get("op") or "")),
                        "args": normalize_args(normalize_op(str(a.get("op") or "")),
                                               a.get("args") or {})}
                       for a in actions if isinstance(a, dict)]
            actions = _backfill_from_user_message(actions, user_msg)

            # 中文书名 → 自动生成 ASCII 标识（用户只说了《…》没给标识时的唯一出路）
            for item in actions:
                if item["op"] != "book_create":
                    continue
                if not str(item["args"].get("novel_id") or "").strip():
                    title = _guess_title_from_user(user_msg)
                    if title:
                        item["args"]["novel_id"] = _slug_from_title(title)
                        logger.info("用户只给了中文书名，已生成 ASCII 标识：%s（书名：%s）",
                                    item["args"]["novel_id"], title)

            # 规划结果若"缺必填参数"而确定性兜底能给全（典型：模型只给模式、忘了 novel_id），
            # 用兜底版本替换——否则用户点了"确认"却只收到一条失败回执（实测反复踩到）。
            # 判据只看"关键必填参数是否为空"，不再比键集合：折算后的 args 键集已规范化，
            # 比集合会把"模型给了 mode、兜底也能给 mode"误判成缺参数。
            fallback = _fallback_action_from_history(chat, user_msg)
            for cand in fallback:
                same = [a for a in actions
                        if isinstance(a, dict) and a.get("op") == cand["op"]]
                if not same:
                    continue
                merged = dict(same[0].get("args") or {})
                for k, v in (cand.get("args") or {}).items():
                    if not str(merged.get(k) or "").strip():
                        merged[k] = v
                if merged != (same[0].get("args") or {}):
                    logger.info("规划结果缺必填参数，已用确定性兜底值补齐：%s", merged)
                    actions = [{**a, "args": merged} if a is same[0] else a for a in actions]

        # 用户刚确认过的回合：直接执行（不再登记待确认），否则用户点了确认却看到新卡。
        results = _run_actions(scope_value, book, actions,
                               confirmed=confirmed_round, dry_run=not confirmed_round,
                               user_msg=user_msg)
        if confirmed_round and results:
            chat.pop(_ACTIONS_PENDING_KEY, None)
            return (_action_response_text(results) or clean), results
        pending = next((r for r in results if r["status"] == "pending_confirm"), None)
        if pending:
            # 用**规范化后的参数**登记预览：确认时比对的也是这一份，
            # 否则"模型写 mode=free、系统折算 pipeline"会让两边不等而拒绝确认。
            chat[_ACTIONS_PENDING_KEY] = register_preview(
                _action_ctx(scope_value, book),
                pending["op"],
                pending.get("args") or {},
                pending.get("summary", ""),
            )
        else:
            chat.pop(_ACTIONS_PENDING_KEY, None)

        receipt = _summary_from_results(results)
        if not receipt:
            return clean, results

        logger.info(
            "动作轮：用户已确认=%s 结果=%s 待确认项=%s",
            confirmed_round, [r["status"] for r in results], (pending or {}).get("op"),
        )

        # 全失败轮：把"什么都没做成"钉死在回复里（不依赖模型自觉）。
        # 实测缺口：模型会在正文里把失败写成"已安排/我来帮你建"，用户以为成了却没结果——
        # 这里给一条确定性前缀，模型怎么改写都盖不掉。
        all_failed = bool(results) and all(r["status"] == "failed" for r in results)
        if all_failed:
            fails = "；".join(f"{r['op']}：{r.get('error', '')}" for r in results)
            warning = f"⚠ 本轮操作没有执行成功：{fails}"
            clean = f"{clean}\n\n{warning}".strip() if clean else warning
            return clean, results

        # 纯"待确认"轮：把确定性的影响说明固定附在回复末尾（不依赖模型改写它）。
        # 理由：这条消息会被反复读取（确认前后都在），内容必须稳定，
        # 否则前端确认卡看起来像挂在另一条消息上（用户会以为按钮没生效）。
        # 同时保留模型自己的话术（它对参数的理解写在正文里），只是把"影响说明"这条钉死。
        if pending is not None and not any(r["status"] == "ok" for r in results):
            notice = _action_response_text(results) or pending.get("summary", "")
            if notice and notice not in (clean or ""):
                clean = (f"{clean}\n\n{notice}").strip() if clean else notice
            return clean, results

        # 读类动作取回的长文本必须真的到用户眼前（模型只复述摘要时补上确定性回显）
        if any(r["status"] == "ok" for r in results):
            clean = _content_echo(clean, results)

        # 结果回灌：让墨师用自然语言汇总（并在纯动作轮里给出可读答复）
        summarize = [
            ChatMessage(role="system",
                        content=_with_constitution(
                            _ACTION_PROMPT_HEAD
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

        参数一律先过 ``normalize_args``：预览登记的 args 与确认时比对的 args 必须是
        **同一份规范化结果**，否则"模型写 mode=free、系统折算 pipeline"这类折算会让
        两边不相等而拒绝确认（预览键由规范化参数派生，见 actions._preview_key）。
        """
        scope_value, book = _resolve_scope(novel)
        op = normalize_op(body.op.strip())
        if not actions_is_write(op):
            raise HTTPException(400, f"动作 {op} 不是写动作，请走只读通道/对话")
        args = normalize_args(op, body.args)
        ctx = _action_ctx(scope_value, book)
        if not body.confirm:
            result = actions_execute(op, args, ctx=ctx, execute_write=False)
            # 失败也照样登记预览：用户必须始终能看到"要做什么/为什么没成"的卡片，
            # 而不是只刷新出一句报错（实测：失败回执不发卡，用户以为按钮坏了）。
            pending = register_preview(ctx, op, args,
                                       result.summary or result.error or "")
            return JSONResponse({"ok": True, "status": "pending_confirm", "pending": pending,
                                 "preview_status": result.status, "preview_error": result.error})
        # 确认必须对应一次真实预览（防"口头声称确认"直接落盘）
        refusal = consume_preview(ctx, {"op": op, "args": args, "token": body.token})
        if refusal:
            raise HTTPException(409, refusal)
        result = actions_execute(op, args, ctx=ctx, execute_write=True)
        if result.ok and op == "book_select":
            set_active_novel(str(args.get("novel_id") or ""))
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
        refusal = consume_preview(ctx, pending)
        if refusal:
            raise HTTPException(409, refusal)
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
        """取消会话里的待确认动作（含预览登记）。"""
        scope_value, book = _resolve_scope(novel)
        chat = _load_chat(scope_value, cid)
        pending = chat.pop(_ACTIONS_PENDING_KEY, None)
        if pending:
            forget_preview(_action_ctx(scope_value, book), str(pending.get("op") or ""),
                           pending.get("args") if isinstance(pending.get("args"), dict) else None)
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

    def apply_binding(
        novel: str, custom_skill_ids: list[str], pack_ids: list[str], merge: bool = False
    ) -> dict:
        """把自定义约束 + 蒸馏技能包摘要合成 settings/custom-skills.md（绑定唯一实现）。

        端点与墨师动作 `book_bind_skills` 都走这里（单一实现源），避免两条写入路径
        的行为漂移；语义见 `bindings_set` 文档。
        """
        store = _store(novel, writable=True)
        rel = "settings/custom-skills.md"
        current_custom: list[str] = []
        current_packs: list[str] = []
        if merge and store.exists(rel):
            meta = store.read(rel).metadata
            current_custom = list(meta.get("bound_custom") or [])
            current_packs = list(meta.get("bound_packs") or [])
        custom_ids = list(dict.fromkeys([*current_custom, *custom_skill_ids]))
        pack_ids = list(dict.fromkeys([*current_packs, *pack_ids]))

        sections: list[str] = ["# 创作约束（风格工坊绑定，最高优先级）"]
        for sid in custom_ids:
            path = get_settings().novels_dir.parent / "custom_skills" / f"{sid}.md"
            if not path.exists():
                raise HTTPException(404, f"自定义 Skill 不存在：{sid}")
            post = frontmatter.load(str(path))
            sections.append(f"## 自定义约束：{post.metadata.get('title', sid)}\n\n{post.content}")
        for pid in pack_ids:
            try:
                sections.append(_pack_digest(pid))
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc))
        store.write(
            rel,
            "\n\n".join(sections),
            metadata={"bound_custom": custom_ids, "bound_packs": pack_ids},
            commit_message="风格工坊：更新技能绑定",
        )
        return {
            "ok": True,
            "bound_custom": custom_ids,
            "bound_packs": pack_ids,
            # 一行可读回执：用户能立刻确认"这次到底绑上了什么"
            "summary": f"已绑定 {len(custom_ids)} 条自定义约束、{len(pack_ids)} 个技能包",
        }

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
        """把自定义约束 + 蒸馏技能包摘要合成 settings/custom-skills.md。

        写入语义（P1 修复"覆盖丢约束"）：
        · 默认**整体替换**：请求里的清单即生效清单（风格工坊的勾选面板依赖此语义，
          取消勾选必须真的解绑）；
        · ``merge=true`` 为**追加合并**：未列出的已绑资源保持不动。供「新建约束后
          一键绑定到当前作品」这类单点增补使用 —— 它只知道新加的那一条，若走替换语义
          会把该书原有的技能包绑定整段冲掉。
        """
        return JSONResponse(
            apply_binding(novel, body.custom_skill_ids, body.pack_ids, merge=body.merge)
        )

    #: 供墨师动作层复用（actions.py::book_bind_skills 导入它，保证单一实现源）
    app.state.apply_binding = apply_binding

    logger.info("Inkforge 扩展 API 已注册（chat/settings/chapter/bindings）")
