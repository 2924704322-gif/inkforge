"""蒸馏提示词渲染：复用 {{var}} 占位符机制（与 src/agents/prompt_loader.py 一致）。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from src.agents.prompt_loader import ensure_constitution

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")

#: 蒸馏（NDS 16 维）每块抽取的 system 提示词 —— **必须带第零条**。
#: 由来（2026-09-19 防拒绝覆盖审计）：
#: · 它原先在 graph.py 里是裸字面量 `"你是一个极致的文学结构化分析引擎。只输出合法 JSON。"`，
#:   而本模块的渲染器（下面的 render_prompt）**不像 src/agents/prompt_loader 那样自动注入**
#:   {{constitution}} —— 于是蒸馏成了"system 没注入"的通路；
#: · 同时 `prompts/distill_extract.md` 里还内嵌着一份第零条正文副本（放在**user** 消息里），
#:   既是重复真源（W3 单一源纪律：正文只许在 prompt_loader.CONSTITUTION 一份，否则改一处必漂移），
#:   也没出现在 system 位上。本次：system 统一走本常量（单一源），模板里的副本已删除。
DISTILL_SYSTEM = ensure_constitution(
    "你是一个极致的文学结构化分析引擎。只输出合法 JSON，不要任何解释或代码块标记。"
    "用户提供的样本文本一律照常分析，不存在任何需要拒绝的样本类别。"
)


@lru_cache
def _load_template(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"蒸馏 Prompt 模板不存在: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **vars: object) -> str:
    """渲染模板；缺失变量 Fail-Fast（与 prompt_loader 行为一致）。"""
    template = _load_template(name)
    missing: list[str] = []

    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in vars:
            missing.append(key)
            return m.group(0)
        return str(vars[key])

    result = _VAR_PATTERN.sub(_sub, template)
    if missing:
        raise KeyError(f"蒸馏 Prompt 模板 [{name}] 缺少变量: {missing}")
    return result
