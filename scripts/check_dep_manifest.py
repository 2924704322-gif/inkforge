#!/usr/bin/env python3
"""依赖清单一致性检查（P0-2 回归防线）。

问题背景：requirements.txt 曾漏声明 fastapi / uvicorn / markdown / python-dotenv，
而它们都是**模块级**（顶层）import —— 意味着按 README 安装后引擎根本起不来。
CI 里没有人会去「真的装一遍再启动」，所以必须用静态检查提前拦住。

规则：
- **顶层 import** 的第三方模块 → 必须出现在 engine/requirements.txt（缺失即失败）。
- **函数内 import** 的第三方模块 → 视为可选依赖，只告警（例如 ebooklib / pymilvus / FlagEmbedding）。

用法：python scripts/check_dep_manifest.py
退出码：0 通过；1 存在缺失的顶层依赖。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE_SRC = REPO / "engine" / "src"
REQUIREMENTS = REPO / "engine" / "requirements.txt"

# 模块名 → pip 分发名（两者不同的才需要登记）
MODULE_TO_DIST = {
    "dotenv": "python-dotenv",
    "frontmatter": "python-frontmatter",
    "git": "GitPython",
    "yaml": "PyYAML",
    "pydantic_settings": "pydantic-settings",
    "rank_bm25": "rank-bm25",
    "langgraph": "langgraph",
    "FlagEmbedding": "FlagEmbedding",
}

# 不参与检查：标准库、本项目自身包
FIRST_PARTY = {"src", "tests", "scripts"}


def stdlib_names() -> set[str]:
    return set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()


def _normalize(name: str) -> str:
    """PEP 503 归一化：连字符/下划线/点等价（rank_bm25 == rank-bm25）。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def declared_distributions() -> set[str]:
    """requirements.txt 中声明的分发名（归一化、去掉版本约束与注释）。"""
    names: set[str] = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        if name:
            names.add(_normalize(name))
    return names


def collect_imports() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """返回 (顶层 import, 函数内 import)：{模块名: {文件相对路径}}。"""
    top_level: dict[str, set[str]] = {}
    nested: dict[str, set[str]] = {}

    for path in sorted(ENGINE_SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = alias.name.split(".")[0]
                    bucket = top_level if _is_top_level(tree, node) else nested
                    bucket.setdefault(module, set()).add(rel)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:  # 相对导入
                    continue
                if not node.module:
                    continue
                module = node.module.split(".")[0]
                bucket = top_level if _is_top_level(tree, node) else nested
                bucket.setdefault(module, set()).add(rel)
    return top_level, nested


def _is_top_level(tree: ast.Module, target: ast.stmt) -> bool:
    """判断某个 import 语句是否处于模块顶层（不在任何 def/class 内）。

    实现：遍历所有函数/类节点，若 target 落在其中则非顶层。
    """
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.walk(node):
                if child is target:
                    return False
    return True


def main() -> int:
    if not REQUIREMENTS.exists():
        print(f"[FAIL] 找不到依赖清单：{REQUIREMENTS}")
        return 1

    std = stdlib_names()
    declared = declared_distributions()
    top_level, nested = collect_imports()

    def is_third_party(module: str) -> bool:
        return module not in std and module not in FIRST_PARTY and not module.startswith("_")

    missing: list[tuple[str, str, list[str]]] = []
    for module, files in sorted(top_level.items()):
        if not is_third_party(module):
            continue
        dist = _normalize(MODULE_TO_DIST.get(module, module))
        if dist not in declared:
            missing.append((module, dist, sorted(files)))

    optional_gaps: list[tuple[str, str]] = []
    for module in sorted(nested.keys()):
        if not is_third_party(module) or module in top_level:
            continue
        dist = _normalize(MODULE_TO_DIST.get(module, module))
        if dist not in declared:
            optional_gaps.append((module, dist))

    print(f"引擎源码：{ENGINE_SRC}")
    print(f"声明依赖：{len(declared)} 个")
    print(f"顶层第三方 import：{sum(1 for m in top_level if is_third_party(m))} 个模块")
    print()

    if optional_gaps:
        print("[WARN] 以下为函数内惰性 import，未在清单中声明（可接受，但建议登记为可选依赖）：")
        for module, dist in optional_gaps:
            print(f"       - {module}  (pip: {dist})")
        print()

    if missing:
        print("[FAIL] 以下**顶层**第三方依赖未在 engine/requirements.txt 中声明：")
        for module, dist, files in missing:
            print(f"       - {module}  (pip: {dist})")
            for f in files[:3]:
                print(f"           {f}")
        print("\n结果：不通过。补齐 requirements.txt 后再提交（这正是 P0-2 的成因）。")
        return 1

    print("结果：通过。requirements.txt 与引擎顶层 import 一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
