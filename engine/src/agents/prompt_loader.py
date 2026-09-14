"""Prompt 模板加载与渲染：{{var}} 占位符替换（正文含 JSON 花括号，不用 str.format）。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


@lru_cache
def _load_template(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 模板不存在: {path}")
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **vars: object) -> str:
    """渲染模板；缺失变量 Fail-Fast。"""
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
        raise KeyError(f"Prompt 模板 [{name}] 缺少变量: {missing}")
    return result
