#!/usr/bin/env python
"""引擎依赖自检（S1-1 回归防线）。

用途：在安装后 / CI 中一次性验证「requirements 声明的依赖」与「代码实际 import 的
第三方包」是否一致。旧版本漏声明 fastapi/uvicorn/markdown/python-dotenv，导致干净
环境按 README 安装后引擎无法启动——本脚本让该问题在 5 秒内暴露，而不是等到运行期。

用法：
    python engine/scripts/verify_env.py            # 校验核心运行依赖
    python engine/scripts/verify_env.py --optional # 额外校验可选依赖（不通过只告警）

退出码：0 = 通过；1 = 存在缺失的必需依赖。
"""

from __future__ import annotations

import argparse
import importlib
import sys

# 「代码真实 import 的第三方包」→ 「pip 分发名」的映射。
# 每次新增第三方 import 都应在此登记；CI 会用 scripts/check_dep_manifest.py 做静态比对。
REQUIRED: dict[str, str] = {
    # Web 服务
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "markdown": "markdown",
    # Agent 编排
    "langgraph": "langgraph",
    "langgraph.checkpoint.sqlite": "langgraph-checkpoint-sqlite",
    # 模型接入
    "openai": "openai",
    # 记忆层
    "chromadb": "chromadb",
    "rank_bm25": "rank-bm25",
    "jieba": "jieba",
    # MD / 配置
    "frontmatter": "python-frontmatter",
    "git": "GitPython",
    "yaml": "PyYAML",
    "pydantic": "pydantic",
    "pydantic_settings": "pydantic-settings",
    "dotenv": "python-dotenv",
    # CLI
    "typer": "typer",
    "rich": "rich",
}

OPTIONAL: dict[str, str] = {
    "anthropic": "anthropic",          # 仅在使用 Anthropic 接入点时需要
    "ebooklib": "ebooklib",            # 仅 EPUB 蒸馏输入需要
    "FlagEmbedding": "FlagEmbedding",  # 仅 embedding.type=local_bge 需要
    "pymilvus": "pymilvus",            # 仅 prod 档 vector_store.type=milvus 需要
    "pytest": "pytest",                # 测试
    "httpx": "httpx",                  # FastAPI TestClient
}


def _check(group: dict[str, str], label: str) -> list[tuple[str, str]]:
    missing: list[tuple[str, str]] = []
    for module, dist in sorted(group.items()):
        try:
            importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - 任意导入失败都算缺失
            missing.append((dist, f"{type(exc).__name__}: {exc}"))
    if missing:
        print(f"[FAIL] {label}：缺失 {len(missing)} 项")
        for dist, err in missing:
            print(f"        - {dist}  ({err})")
    else:
        print(f"[ OK ] {label}：{len(group)} 项全部就绪")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Inkforge 引擎依赖自检")
    parser.add_argument("--optional", action="store_true", help="同时校验可选依赖（缺失仅告警）")
    args = parser.parse_args()

    print(f"Python {sys.version.split()[0]}  ({sys.executable})")
    missing_required = _check(REQUIRED, "必需依赖")
    if args.optional:
        _check(OPTIONAL, "可选依赖")

    if missing_required:
        print("\n结果：不通过。请执行  pip install -r engine/requirements.txt  （或 requirements.lock）")
        return 1
    print("\n结果：通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
