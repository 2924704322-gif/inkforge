"""结构化输出：JSON mode 优先，能力不足时降级为提示词约束 + 解析重试（R4）。"""

from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.base import ChatMessage
from src.llm.refusal_probe import refusal_signal
from src.llm.registry import ModelRegistry
from src.utils.logger import get_logger

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class StructuredOutputError(RuntimeError):
    """结构化输出多次解析失败。"""


def _repair_json(text: str) -> str:
    """尽力修复 LLM 常见 JSON 破损，不保证修复后必可解析。

    覆盖：字符串内未转义双引号（如 quote 字段引用原文）、字符串内裸换行/制表符、
    尾部多余逗号、输出截断（未闭合的字符串/括号自动补齐）。
    """
    out: list[str] = []
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            continue
        if escape:
            out.append(ch)
            escape = False
        elif ch == "\\":
            out.append(ch)
            escape = True
        elif ch == '"':
            # 向后看首个非空白字符：后接结构符才是真闭合，否则是内容里的裸引号
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j >= len(text) or text[j] in ",:}]":
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    if escape:
        out.pop()  # 截断在反斜杠上：丢弃悬挂转义符
    repaired = "".join(out)
    if in_str:
        repaired += '"'  # 截断在字符串内：补闭引号
    # 尾部多余逗号（含截断后的悬挂逗号）
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(r",\s*$", "", repaired)
    # 截断补齐：扫描未闭合括号后逆序补上
    opens: list[str] = []
    in_str = escape = False
    for ch in repaired:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            opens.append(ch)
        elif ch in "}]" and opens:
            opens.pop()
    for ch in reversed(opens):
        repaired += "}" if ch == "{" else "]"
    return repaired


def extract_json(text: str) -> dict:
    """从模型输出中尽力提取 JSON 对象：多候选提取 → 失败后修复重试。"""
    text = text.strip()
    candidates = [text]
    # ```json 代码块
    m = _JSON_BLOCK_PATTERN.search(text)
    if m:
        candidates.append(m.group(1))
    # 首个 { 到最后一个 } 的最大范围；无闭合 } 时取首个 { 到末尾（截断场景）
    start, end = text.find("{"), text.rfind("}")
    if start != -1:
        candidates.append(text[start: end + 1] if end > start else text[start:])
    last_error: json.JSONDecodeError | None = None
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError as e:
            last_error = e
    # 全部候选直接解析失败：修复常见破损（裸引号/裸换行/尾逗号/截断）后重试
    for cand in candidates:
        try:
            data = json.loads(_repair_json(cand))
            logger.info("JSON 修复后解析成功（原始错误：%s）", str(last_error)[:120])
            return data
        except json.JSONDecodeError:
            continue
    raise last_error or json.JSONDecodeError("未找到可解析的 JSON 对象", text, 0)


def _content_fingerprint(content: str) -> str:
    """产出的**形状指纹**：只描述长度与空白/括号构成，不泄露一个字正文。

    由来（2026-09-27 真机排障）：日志里 `产出 314 字` 与 `拒答信号=空产出` 同时出现，
    `len(content)` 与 `content.strip()` 的结论互斥，日志无法自证到底属于哪种失败
    （空字符串 / 纯空白 / 非 JSON 文本），只能靠猜。指纹把三种情形一次钉死，
    且因为只记空白字符与括号个数，可以安全落盘。
    """
    total = len(content)
    if total == 0:
        return "总 0 字（真空串）"
    if not content.strip():
        counts: dict[str, int] = {}
        for ch in content:
            counts[ch] = counts.get(ch, 0) + 1
        top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
        detail = "、".join(f"{ch!r}×{n}" for ch, n in top)
        return f"总 {total} 字 / 纯空白（{detail}）"
    nonws = sum(1 for ch in content if not ch.isspace())
    left, right = content.count("{"), content.count("}")
    head = "以左花括号开头" if content.lstrip().startswith("{") else "不以左花括号开头"
    return (
        f"总 {total} 字 / 非空白 {nonws} 字 / 左花括号 {left} 个 / 右花括号 {right} 个 / {head}"
    )


#: 纯空白产出的纠错语（2026-09-27）。措辞刻意不出现花括号字面量，避免 f-string 转义噪音；
#: `第 n 次` 让每一轮的 prompt 都不完全相同，否则第 2/3 轮等于原样重放。
_BLANK_NUDGE_TMPL = (
    "\n\n【格式校验未通过（第 {n} 次）】你上一条回复是空白，没有任何可见字符。"
    "请立刻只输出一个符合 Schema 的 JSON 对象：必须以左花括号开头、以右花括号结尾，"
    "不要任何解释、不要思考过程、不要代码块围栏、不要前后缀。"
)


def _short_error(exc: BaseException) -> str:
    """把报错压成**模型看得懂的短句**（只留字段名与原因，丢掉 URL 与 schema 转储）。

    由来（2026-09-27 真机）：互动设定 Demo 的第一次失败是 `book_title → Field required`，
    而回灌给模型的却是 pydantic 的整段长文（含 errors.pydantic.dev 链接与嵌套 schema），
    对"把缺的字段补上"这件事毫无帮助。人看长文，模型看短句，两者分工。
    """
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:  # noqa: BLE001 - 拿不到就退回原文
            return str(exc)[:300]
        parts = []
        for it in items[:6]:
            loc = ".".join(str(x) for x in it.get("loc", ())) or "(根)"
            parts.append(f"{loc} → {it.get('msg', '')}")
        if parts:
            more = f"（另有 {len(items) - 6} 处未列出）" if len(items) > 6 else ""
            return "；".join(parts) + more
    return str(exc)[:300]


def chat_structured(
    registry: ModelRegistry,
    role: str,
    messages: list[ChatMessage],
    schema: type[T],
    max_parse_retries: int = 2,
    temperature: float | None = None,
) -> T:
    """以指定角色调用模型并解析为 pydantic 模型。

    流程：附加 schema 约束提示 → json_mode 调用 → 提取解析 →
    失败则携带错误信息重试（上限 max_parse_retries 次）。

    两类失败**处置不同**（2026-09-27 真机排障修正）：
      · 非 JSON 文本（模型写了散文 / 拒答措辞）→ 把坏产出回灌 + 追加纠错轮，
        保持 user/assistant 交替，模型能看见自己写错了什么；
      · 纯空白产出（`finish=stop` 的静默空回）→ **不回灌**：丢弃整段工作历史，
        在原始提示词尾部叠一条更硬的格式指令重问。回灌空回复等于告诉模型
        "交白卷也可以"——真机上 `desktop-20260927.log` 连续三次都是 314 字纯空白 /
        541 tokens / finish=stop，走的就是旧口径。丢弃历史同时也保证了不会出现
        user→user 相邻消息，兼容要求严格交替的端点。
    """
    schema_hint = (
        "\n\n【输出格式要求】只输出一个 JSON 对象，不要输出任何其他文字。"
        "字符串值内的换行必须写作 \\n；引用原文时若含双引号，请改用『』或「」，"
        "避免破坏 JSON 结构。"
        f"JSON Schema 如下：\n{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )
    work_messages = list(messages)
    work_messages[-1] = ChatMessage(
        role=work_messages[-1].role, content=work_messages[-1].content + schema_hint
    )
    base_messages = list(work_messages)
    base_tail = base_messages[-1]

    last_error = ""
    last_content = ""
    blank_rounds = 0
    for attempt in range(max_parse_retries + 1):
        if blank_rounds:
            # 上一轮是纯空白：丢掉整段工作历史，只在原始提示词上叠一条更硬的格式指令
            work_messages = [
                *base_messages[:-1],
                ChatMessage(
                    role=base_tail.role,
                    content=base_tail.content + _BLANK_NUDGE_TMPL.format(n=blank_rounds + 1),
                ),
            ]
        result = registry.chat_as(
            role, work_messages, json_mode=True, temperature=temperature
        )
        try:
            data = extract_json(result.content)
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)
            #: 结构化链路里"JSON 合法但字段不合 Schema"占相当比例（真机首错就是
            # `book_title → Field required`）：给模型的纠错语要**点名到字段**，
            # 而不是甩一段 pydantic 长文（含 URL）——后者只会把下一轮带偏。
            schema_error = isinstance(e, ValidationError)
            short_error = _short_error(e)
            # 诊断信息（2026-09-22 真机排障补）：只记**长度与收尾原因**，不记产出正文。
            # 由来：日志里只有 `Expecting value: line 1 column 1 (char 0)`（这正是
            # `json.loads("")` 的签名），无法区分"模型返回空内容"与"模型返回非 JSON 文本"——
            # 两者处置完全不同：前者是输出预算/推理吃光（该加预算），后者是格式不听命（该改提示）。
            content = result.content or ""
            last_content = content
            blank = not content.strip()
            # 拒答信号（2026-09-22 真机排障补）：只记**命中的通用措辞**，不记正文。
            # 由来：出卡链路实测 6 次调用全部 2~3 秒返回 110~242 字、finish=stop，
            # 日志里只有"解析失败"，无法判断是"模型拒答"还是"JSON 写坏了"——
            # 前者该改提示词/换模型，后者该改 schema 提示，处置完全不同。
            # 这条信号此前只长在自由文本链路（generate_faithful）上，结构化链路是盲区。
            signal = refusal_signal(content)
            logger.warning(
                "角色[%s]结构化输出解析失败（第 %d 次）: %s | 产出 %d 字 / %s tokens / "
                "finish_reason=%s / 产出指纹=%s / 拒答信号=%s%s",
                role,
                attempt + 1,
                last_error[:200],
                len(content),
                (result.usage or {}).get("completion_tokens", "?"),
                result.finish_reason or "（未透出）",
                _content_fingerprint(content),
                signal,
                "｜空产出：finish=stop 已排除预算耗尽，疑模型侧静默空回，本轮改走不回灌的纠错重试"
                if blank
                else "",
            )
            if blank:
                blank_rounds += 1
            else:
                # 携带错误信息追加重试
                correction = (
                    f"你上面输出的 JSON **字段不符合 Schema**：{short_error}\n"
                    "请补齐/改正上述字段后，重新只输出一个**完整**的 JSON 对象："
                    "必填字段一个都不能少、类型必须正确，不要输出任何其他文字。"
                    if schema_error
                    else f"你上面的输出无法解析为合法的 JSON，错误：{short_error}\n"
                         "请重新只输出一个符合 Schema 的 JSON 对象，不要任何其他文字。"
                )
                work_messages = work_messages + [
                    ChatMessage(role="assistant", content=result.content),
                    ChatMessage(role="user", content=correction),
                ]
    raise StructuredOutputError(
        f"角色[{role}]结构化输出连续 {max_parse_retries + 1} 次解析失败: {last_error[:500]}"
        f"（末轮产出指纹：{_content_fingerprint(last_content)}）"
    )
