"""工作台动作说明的提示词加载（单文件，独立于 prompts/ 模板目录）。

为什么**不能**放在 ``src/agents/prompts/``：
``prompt_loader.validate_templates()`` 在引擎启动时会扫描该目录下**所有** ``*.md``，
把它们当作 {{var}} 模板做一致性校验。动作清单里写的是给人/模型看的参数占位
（``{novel_id}`` 这种单花括号），会被当作"静默丢失的变量"直接 Fail-Fast，
导致引擎起不来（本坑真实踩过）。
因此动作说明放在 ``src/agents/actions/``，由本模块直读，不进模板体系。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_ACTIONS_DIR = Path(__file__).resolve().parent / "actions"

#: 动作说明只在"具备动作能力"的对话智能体上追加（工作台 master）。其他子智能体
#: 仍是"一次性产出型"（出人物卡/大纲/正文），不给它们写动作会误导模型瞎调。
ACTION_AWARE_AGENTS = frozenset({"master"})


class ActionPromptError(RuntimeError):
    """动作说明缺失或不可读（Fail-Fast，避免静默退化成"墨师没有手"）。"""


@lru_cache
def master_actions_block() -> str:
    """工作台动作说明全文（进程内缓存；文件缺失即报错）。"""
    path = _ACTIONS_DIR / "master_actions.md"
    if not path.exists():
        raise ActionPromptError(f"动作说明文件缺失：{path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ActionPromptError(f"动作说明文件为空：{path}")
    return text


def actions_block_for(agent: str) -> str:
    """按智能体返回动作说明（不具备动作能力的返回空串）。"""
    if agent not in ACTION_AWARE_AGENTS:
        return ""
    return master_actions_block()
