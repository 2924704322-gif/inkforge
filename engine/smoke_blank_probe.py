"""结构化「纯空白产出」探针（2026-09-27 真机排障 · **默认 dry-run，不发调用**）。

背景
----
`desktop-20260927.log` 里 38 条结构化失败是「产出 314 字 / 541 tokens / finish=stop」
+ `拒答信号=空产出`：`len(content) > 0` 而 `content.strip() == ""`。
重试策略已按"不回灌空白 + 换措辞重问"修好（见 HANDOVER §5.18），但**根因未钉死**，
而三种根因的修法完全不同：

  · 纯空白（`\\n` / 空格 / 全角空格）→ 客户端口径问题，本轮改法就该吃到效果；
  · 控制字符 / 不可见字符 → 客户端 `strip()` 之外的清洗问题；
  · 供应商侧内容过滤后的**静默空回** → 重试次数再多也没用，该换接入点或降级非 JSON 链路。

本脚本只做一件事：用**合成 prompt**（不含任何小说内容、不含 NSFW）在指定角色上连发 N 次
`json_mode` 调用，把每次的产出指纹 / tokens / finish_reason 打出来，并汇总
「空白率」。它**不写文件、不改配置**，只打印结论。

用法（不加 `--yes` 永远只打印计划）
    python smoke_blank_probe.py                        # dry-run：看计划
    python smoke_blank_probe.py --yes                  # 真发 3 次调用（计费）
    python smoke_blank_probe.py --yes -n 5             # 发 5 次
    python smoke_blank_probe.py --yes --role plotter   # 换角色（默认 architect）
    python smoke_blank_probe.py --yes --no-json-mode   # 关掉 json_mode 做对照

判读
----
  · 全部 `非空白 + 以左花括号开头` → 本地跑不出空产出，说明是**偶发/上下文相关**，
    需要拿真机日志继续攒样本（新日志已自带 `产出指纹=` 字段，不必再猜）；
  · 出现 `纯空白` → 复现成功，看括号里的构成（`'\\n'×N` / `' '×N` / `'\\u3000'×N`）；
  · 出现 `总 0 字（真空串）` 且 `completion_tokens` 接近 max_tokens → 预算被推理吃光，
    该调 `max_tokens` 或提示词。
"""

from __future__ import annotations

import argparse
import sys

from src.config.settings import load_models_config
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import _content_fingerprint

#: 合成任务：形状贴近真实 architect 调用（要 JSON、有 schema 提示），但内容与作品无关。
SYNTHETIC_TASK = (
    "【合成测试数据 · 与任何作者作品无关】你是剧情卡生成器。请为一部虚构的"
    "都市悬疑长篇生成 2 张剧情卡，每张卡包含 title（12 字以内）、summary（80 字以内）、"
    "beats（3 条，每条 20 字以内）。题材示例：一枚旧钟表店收到的匿名怀表。"
    "只输出 JSON，不要任何解释。"
)


def _plan(args: argparse.Namespace) -> None:
    cfg = load_models_config()
    binding = cfg.roles.get(args.role)
    print("【计划】dry-run —— 不会发出任何模型调用")
    print(f"  角色      : {args.role}")
    if binding is None:
        print(f"  ⚠ models.yaml 里没有角色 {args.role!r}，可用：{sorted(cfg.roles)}")
    else:
        print(f"  接入点    : {binding.provider} / {binding.model}")
        print(f"  max_tokens: {binding.max_tokens}")
    print(f"  调用次数  : {args.n}（每次 1 段合成 prompt，约 {len(SYNTHETIC_TASK)} 字，不含作品内容）")
    print(f"  json_mode : {not args.no_json_mode}")
    print("  预计花费  : 每次产出几十~几百 tokens；本探针不写盘、不改配置")
    print("\n真发调用请加 --yes（授权后执行）。")


def _run(args: argparse.Namespace) -> int:
    cfg = load_models_config()
    registry = ModelRegistry(cfg)
    messages = [ChatMessage(role="user", content=SYNTHETIC_TASK)]

    blank = 0
    failures = 0
    for i in range(1, args.n + 1):
        try:
            res = registry.chat_as(
                args.role, messages, json_mode=not args.no_json_mode
            )
        except Exception as exc:  # noqa: BLE001 - 探针要把调用异常与空产出分开数
            failures += 1
            print(f"  [{i}/{args.n}] 调用异常：{type(exc).__name__}: {exc}")
            continue
        content = res.content or ""
        fp = _content_fingerprint(content)
        # 只打印指纹，不打印产出正文（探针同样守"不落正文"的口径）
        print(
            f"  [{i}/{args.n}] 指纹={fp} / "
            f"{len(content)} 字 / {(res.usage or {}).get('completion_tokens', '?')} tokens / "
            f"finish_reason={res.finish_reason or '（未透出）'}"
        )
        if not content.strip():
            blank += 1

    print(
        f"\n【汇总】{args.n} 次：纯空白 {blank} / 调用异常 {failures} / "
        f"非空白 {args.n - blank - failures}"
    )
    if blank:
        print("→ 复现到空产出：按脚本头部的判读表分档（纯空白 / 控制字符 / 预算耗尽）。")
    else:
        print("→ 本地未复现：属偶发或上下文相关，改用真机日志的「产出指纹=」字段继续攒样本。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="结构化纯空白产出探针（默认 dry-run）")
    ap.add_argument("--yes", action="store_true", help="真的发出模型调用（计费）")
    ap.add_argument("-n", type=int, default=3, help="调用次数（默认 3）")
    ap.add_argument("--role", default="architect", help="角色名（默认 architect）")
    ap.add_argument("--no-json-mode", action="store_true", help="关掉 json_mode 做对照")
    args = ap.parse_args()
    if not args.yes:
        _plan(args)
        return 0
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
