#!/usr/bin/env python3
"""README/文档中的文件路径引用有效性检查（P1-2 回归防线）。

问题背景：README 引用过 `LeftSidebar.vue`，而该文件在项目里根本不存在
（实际是 FeatureNav.vue + WorkspaceTree.vue）。文档漂移不会让程序崩，
但会持续误导使用者与决策者，且人工复核成本很高 —— 交给 CI 最划算。

规则：提取 README.md 中反引号包裹的、看起来像路径的字符串，校验其在仓库中是否存在；
      带通配的路径按 glob 校验；明显的命令/代码片段跳过。

用法：python scripts/check_doc_paths.py
退出码：0 通过；1 存在失效引用。
"""

from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 只校验**当前检出里确实存在**的文档：
# `docs/` 是本地内部文档（不入公开仓库），在 fresh clone / CI 上不存在 ——
# 若把它写死进 TARGETS，CI 会因"文档本身不存在"直接判红（假失败）。
TARGETS = [t for t in ("README.md", "docs/ARCHITECTURE.md") if (REPO / t).exists()]

# 本地内部文档目录：该目录不存在时，跳过所有指向它的引用校验（同理，避免假失败）。
LOCAL_DOCS_PREFIX = "docs/"
_HAS_LOCAL_DOCS = (REPO / "docs").exists()

# 文档里的相对路径可能相对于仓库根、engine/ 或 apps/desktop/
RESOLVE_ROOTS = [Path("."), Path("engine"), Path("apps/desktop"), Path("apps/desktop/src")]

# 只把「看起来确实是本仓库文件」的引用纳入校验：
# 必须带下列后缀之一。否则 `conda env langchain1.2` 这类带点的词会被误判为路径。
PATH_EXTENSIONS = (
    ".py", ".ts", ".vue", ".js", ".md", ".json", ".yaml", ".yml",
    ".txt", ".css", ".html", ".toml", ".sh", ".bat", ".lock", ".cfg", ".ini",
)

# 形如 `xxx/yyy.ext` 或 `xxx.yyy` 的引用
PATH_RE = re.compile(r"`([A-Za-z0-9_./@\-]+\.[A-Za-z0-9]{1,6})`")

# Markdown 链接 [text](target)：只校验指向仓库内文件的相对链接
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")

# 明确不作为仓库路径校验的（示例、依赖包内的文件名）
IGNORE_SUBSTRINGS = (
    "node_modules",
    "site-packages",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    ".env.example",
)

# 已知的外部工具/约定文件名，不作为本仓库路径校验
SKIP_EXACT = {
    "deepwrite.json",
    "catalog-registry.json",
    "models.yaml",       # 已按 configs/models.yaml 校验
    "requirements.txt",
    "AGENTS.md",
}


def _candidates(text: str) -> list[str]:
    out: list[str] = []
    for match in PATH_RE.finditer(text):
        raw = match.group(1).strip()
        if any(s in raw for s in IGNORE_SUBSTRINGS) or raw in SKIP_EXACT:
            continue
        if raw.startswith(("http://", "https://")):
            continue
        if raw.startswith(LOCAL_DOCS_PREFIX) and not _HAS_LOCAL_DOCS:
            continue          # docs/ 为本地内部文档，公开检出里没有：不校验、不报错
        if not raw.endswith(PATH_EXTENSIONS):
            continue
        out.append(raw)
    # Markdown 相对链接也要校验（防止 [x](docs/xxx.md) 这类链接腐烂）
    for match in MD_LINK_RE.finditer(text):
        raw = match.group(1).strip()
        if raw.startswith(("http://", "https://", "#", "mailto:")):
            continue
        raw = raw.split("#", 1)[0]  # 去掉锚点
        if not raw or not raw.endswith(PATH_EXTENSIONS):
            continue
        if raw.startswith(LOCAL_DOCS_PREFIX) and not _HAS_LOCAL_DOCS:
            continue
        out.append(raw)
    return out


def _exists(ref: str) -> bool:
    # 1) 按已知根目录逐个尝试
    for root in RESOLVE_ROOTS:
        candidate = (REPO / root / ref) if root != Path(".") else (REPO / ref)
        if candidate.exists():
            return True
    # 2) 无目录前缀的裸文件名：全仓库按 basename 搜索（跳过依赖目录）
    if "/" not in ref:
        for hit in REPO.rglob(ref):
            if "node_modules" not in hit.parts and ".git" not in hit.parts:
                return True
    return False


def main() -> int:
    failures: list[tuple[str, str]] = []

    for rel in TARGETS:
        doc = REPO / rel
        if not doc.exists():
            failures.append((rel, "<文档本身不存在>"))
            continue
        text = doc.read_text(encoding="utf-8")
        for ref in sorted(set(_candidates(text))):
            if "*" in ref:
                if not glob.glob(str(REPO / ref), recursive=True):
                    failures.append((rel, ref))
                continue
            if not _exists(ref):
                failures.append((rel, ref))

    if failures:
        print("[FAIL] 文档中引用了不存在的路径：")
        for doc, ref in failures:
            print(f"       {doc} -> {ref}")
        print("\n结果：不通过。请修正文档或创建对应文件。")
        return 1

    print(f"结果：通过（已校验 {', '.join(TARGETS)}）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
