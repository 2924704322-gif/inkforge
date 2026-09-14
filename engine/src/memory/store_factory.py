"""MdStore 构造工厂：按调用语义决定是否启用「每本书独立 Git 仓库」。

背景（S1-3 修复）：全仓库原先 10 个 MdStore 构造点**一律**传 auto_git=False，
使 README/架构文档承诺的「每本书一个 Git 仓库、写作历史由 Git 承担」成为死代码，
项目与用户创作数据实际零版本保护。

本工厂把语义显式化：
- ``writable=True``  → 会产生事实源写入的路径（生成流水线、编辑器保存、建书、
  删章、提案落盘）：启用 Git 自动提交，写作历史真正生效。
- ``writable=False`` → 纯读路径（书架列表、看板、健康探针、状态轮询）：
  关闭 Git，避免读操作产生提交与额外 IO。

可用 ``INKFORGE_GIT_HISTORY=0`` 全局关闭（CI / 临时环境用）。
"""

from __future__ import annotations

import os
from pathlib import Path

from src.memory.md_store import MdStore

GIT_HISTORY_ENV = "INKFORGE_GIT_HISTORY"

_FALSY = {"0", "false", "off", "no", ""}


def git_history_enabled() -> bool:
    """全局开关：默认开启，``INKFORGE_GIT_HISTORY=0/false/off/no`` 关闭。"""
    return (os.environ.get(GIT_HISTORY_ENV) or "1").strip().lower() not in _FALSY


def open_store(novel_dir: Path | str, *, writable: bool = False) -> MdStore:
    """打开一部小说的 MD 事实源。

    - ``writable=True``：挂 Git 并在缺失时初始化仓库（写路径）。
    - ``writable=False``：只挂载已存在的书内仓库，绝不新建（读路径）。
    """
    if not git_history_enabled():
        return MdStore(Path(novel_dir), auto_git=False)
    return MdStore(Path(novel_dir), auto_git=True, create_repo=bool(writable))
