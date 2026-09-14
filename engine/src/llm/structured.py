"""结构化输出：JSON mode 优先，能力不足时降级为提示词约束 + 解析重试（R4）。"""

from __future__ import annotations

import json
import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.base import ChatMessage
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
    last_error: Optional[json.JSONDecodeError] = None
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


def chat_structured(
    registry: ModelRegistry,
    role: str,
    messages: list[ChatMessage],
    schema: Type[T],
    max_parse_retries: int = 2,
    temperature: Optional[float] = None,
) -> T:
    """以指定角色调用模型并解析为 pydantic 模型。

    流程：附加 schema 约束提示 → json_mode 调用 → 提取解析 →
    失败则携带错误信息重试（上限 max_parse_retries 次）。
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

    last_error = ""
    for attempt in range(max_parse_retries + 1):
        result = registry.chat_as(
            role, work_messages, json_mode=True, temperature=temperature
        )
        try:
            data = extract_json(result.content)
            return schema.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as e:
            last_error = str(e)
            logger.warning(
                "角色[%s]结构化输出解析失败（第 %d 次）: %s",
                role,
                attempt + 1,
                last_error[:200],
            )
            # 携带错误信息追加重试
            work_messages = work_messages + [
                ChatMessage(role="assistant", content=result.content),
                ChatMessage(
                    role="user",
                    content=(
                        f"你上面的输出无法解析为符合 Schema 的 JSON，错误：{last_error[:500]}\n"
                        "请重新只输出一个符合 Schema 的 JSON 对象，不要任何其他文字。"
                    ),
                ),
            ]
    raise StructuredOutputError(
        f"角色[{role}]结构化输出连续 {max_parse_retries + 1} 次解析失败: {last_error[:500]}"
    )
