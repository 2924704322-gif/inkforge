"""请求作用域（scope）解析：书内 / 工作区两种维度。

背景（墨师全域化改造 P0）：
- 改造前，所有 ``/api/*`` 端点用 ``novel or default_novel`` 兜底——没有书就没有
  任何可用上下文，墨师只能在"某本书"里工作。
- 改造后引入**工作区作用域**：``novel="__workspace__"`` 表示"不隶属任何书"的
  工作台级会话（墨师全域总控）。它使用**显式哨兵值**而非改动缺省语义，因此：

  · ``novel=""``            → 默认书（旧语义，逐字不变，兼容所有既有客户端）
  · ``novel="<book-id>"``   → 指定书（旧语义，逐字不变）
  · ``novel="__workspace__"`` → 工作区（新增）

哨兵值天然满足既有 ``_NOVEL_ID_RE = ^[A-Za-z0-9_-]+$``，故一切既有校验函数零改动，
非法值（如 ``../evil``）仍被拒。
"""

from __future__ import annotations

from pathlib import Path

# 工作区作用域哨兵值（唯一来源；任何模块不得再写字面量副本）
WORKSPACE = "__workspace__"

#: 工作区目录名（位于 data/ 下，与 novels/ 平级）
WORKSPACE_DIR_NAME = "workspace"


def is_workspace(novel: str | None) -> bool:
    """是否工作区作用域。"""
    return (novel or "").strip() == WORKSPACE


def resolve_book(novel: str | None, default_novel: str) -> str:
    """把请求的 novel 参数解析为"实际书目 ID"。

    工作区不隶属任何书 → 返回空串（调用方据此短路"书内数据"分支，
    **不会**静默落到默认书，避免工作区误读默认书的事实源）。
    """
    value = (novel or "").strip()
    if value == WORKSPACE:
        return ""
    return value or default_novel


def workspace_dir(novels_dir: Path) -> Path:
    """工作区根目录（``<novels_dir>/../workspace``，data/ 下与 novels/ 平级）。"""
    return novels_dir.parent / WORKSPACE_DIR_NAME


def chats_dir(root: Path) -> Path:
    """会话目录：书 → ``<书>/chats``；工作区 → ``<workspace>/chats``。"""
    d = root / "chats"
    d.mkdir(parents=True, exist_ok=True)
    return d
