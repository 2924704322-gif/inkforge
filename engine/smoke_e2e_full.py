#!/usr/bin/env python
"""Inkforge 端到端全量验收（真实创建一本书 + 真实 LLM 调用）。

与 `smoke_test.py` 的分工：
- `smoke_test.py` 是**零真实 LLM** 的接口/安全回归（82 项），跑得快、可常驻。
- 本脚本**故意调用真实模型**，端到端验证「生成 → 审校评分 → 人工审批 → 写入创作空间」
  这条链路，包括 改稿提案评分 / 打回重做 / 通过落盘。

用法：
    cd engine && python smoke_e2e_full.py            # 真实 LLM（会计费，约 4-6 次调用）
    python smoke_e2e_full.py --reject-only          # 只验打回重做
退出码 0 = 全绿。

沙箱落在系统临时目录，绝不动 engine/data 下的真实作品。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from smoke_test import (  # noqa: E402 - 复用既有真实进程/HTTP 夹具
    TOKEN,
    EngineProcess,
    Suite,
    build_sandbox,
    http,
)

NOVEL = "e2e-book"
OUTLINE_REL = "settings/outline.md"
OUTLINE_SEED = """---
title: 城墙之下
theme: 守夜与失约
volumes:
  - volume: 1
    title: 第一夜
    chapters:
      - chapter: 1
        title: 城墙之下
        outline: 守夜人陆守接下最后一班岗，发现钟声次数对不上。
  - volume: 2
    title: 第二夜
    chapters:
      - chapter: 2
        title: 钟声裂缝
        outline: 失约被揭开，代价落定。
---

# 全书大纲

第一卷写守夜人的日常与异常；第二卷揭开失约。
"""

LLM_TIMEOUT = 240.0


def brief(suite: Suite, label: str, ok: bool, detail: str = "", evidence: str = "") -> None:
    suite.check(label, ok, detail, evidence)


def run(data_dir: Path, engine: EngineProcess, suite: Suite, args: argparse.Namespace) -> None:
    base = engine.base
    novels = data_dir / "novels"

    # ── 1. 真实创建一本书 ───────────────────────────────────────────────
    print("[1] 真实创建书目（POST /api/books）")
    res = http("POST", f"{base}/api/books", body={"novel_id": NOVEL})
    brief(suite, "POST /api/books 创建成功", res.status == 200, f"status={res.status} {res.raw[:120]}")

    book_dir = novels / NOVEL
    brief(suite, "书目目录已落盘", book_dir.exists(), str(book_dir))

    res = http("GET", f"{base}/api/books")
    ids = [b.get("novel_id") for b in (res.body or {}).get("books", [])]
    brief(suite, "新书出现在书架", NOVEL in ids, str(ids))

    # 夹具：直接写入一份大纲作为改稿目标（沙箱内，与真实作品无关）
    (book_dir / "settings").mkdir(parents=True, exist_ok=True)
    (book_dir / OUTLINE_REL).write_text(OUTLINE_SEED, encoding="utf-8")

    res = http("GET", f"{base}/api/settings/doc?novel={NOVEL}&rel={OUTLINE_REL}")
    brief(
        suite,
        "改稿目标文档可读（settings/outline.md）",
        res.status == 200 and "守夜" in json.dumps(res.body, ensure_ascii=False),
        f"status={res.status}",
    )

    # ── 2. 创建会话 ─────────────────────────────────────────────────────
    print("\n[2] 创建对话会话")
    res = http("POST", f"{base}/api/chats?novel={NOVEL}", body={"agent": "master"})
    cid = (res.body or {}).get("chat", {}).get("id", "")
    brief(suite, "POST /api/chats 创建会话", res.status == 200 and bool(cid), f"cid={cid}")

    # ── 3. 生成改稿提案（真实 LLM）+ 审校评分 ───────────────────────────
    print("\n[3] 改稿提案 + 审校评分（真实 LLM，较慢）")
    instruction = "把第 1 章的钩子改强：最后一句留一个明确的悬念，其余保持原样。"
    t0 = time.time()
    res = http(
        "POST",
        f"{base}/api/chats/{cid}/propose?novel={NOVEL}",
        body={"instruction": instruction, "target": {"kind": "settings", "key": OUTLINE_REL}, "agent": "chat"},
        timeout=LLM_TIMEOUT,
    )
    latency = round(time.time() - t0, 1)
    prop = res.body if isinstance(res.body, dict) else {}
    brief(
        suite,
        "POST propose 返回 200",
        res.status == 200,
        f"status={res.status} {latency}s {str(res.body)[:160] if res.status != 200 else ''}",
    )
    brief(suite, "提案带完整成稿 proposed", bool(prop.get("proposed")), f"{len(str(prop.get('proposed', '')))} 字")
    brief(suite, "提案带行级差异 hunks", bool(prop.get("hunks")), f"{len(prop.get('hunks') or [])} 块")

    review = prop.get("review")
    brief(
        suite,
        "★ 提案带审校评分 review（本次回归重点）",
        isinstance(review, dict) and all(k in review for k in ("consistency", "plot", "continuity", "prose")),
        f"review={json.dumps(review, ensure_ascii=False)[:200] if review else None}",
    )
    if isinstance(review, dict):
        brief(suite, "评分带总评 comment", bool(review.get("comment")), str(review.get("comment"))[:80])
        brief(suite, "评分带意见清单 issues", isinstance(review.get("issues"), list),
              f"{len(review.get('issues') or [])} 条")

    # ── 4. 人工审批门禁：未点通过前，事实源不得被改写 ────────────────────
    print("\n[4] 审批门禁：未通过前不得写入创作空间")
    on_disk = (book_dir / OUTLINE_REL).read_text(encoding="utf-8")
    brief(suite, "提案生成后目标文档内容未变（等人工审批）", on_disk == OUTLINE_SEED)

    pid = prop.get("id", "")
    res = http("GET", f"{base}/api/chats/{cid}/proposals?novel={NOVEL}")
    listed = (res.body or {}).get("proposals", [])
    brief(suite, "提案出现在待审列表", any(p.get("id") == pid for p in listed), f"{len(listed)} 条")
    brief(
        suite,
        "列表里同样带 review（重启后仍可看到评分）",
        bool(listed) and isinstance(listed[0].get("review"), dict),
    )

    # ── 5. 打回重做（真实 LLM）──────────────────────────────────────────
    print("\n[5] 打回重做：按意见出一版新提案")
    feedback = "悬念太隐晦了，改成明确的一句话钩子；不要新增人物。"
    t0 = time.time()
    res = http(
        "POST",
        f"{base}/api/chats/{cid}/proposals/{pid}/decide?novel={NOVEL}",
        body={"decision": "reject", "feedback": feedback},
        timeout=LLM_TIMEOUT,
    )
    latency = round(time.time() - t0, 1)
    decided = res.body if isinstance(res.body, dict) else {}
    brief(suite, "POST decide(reject) 返回 200", res.status == 200, f"status={res.status} {latency}s")
    brief(suite, "★ 原提案被标记 rejected（本次回归重点）",
          decided.get("status") == "rejected", f"status={decided.get('status')} msg={decided.get('statusMessage')}")
    replacement_id = decided.get("replacementId")
    brief(suite, "★ 打回后生成了替代提案（重做成功）",
          bool(replacement_id), f"replacementId={replacement_id} msg={decided.get('statusMessage')}")

    res = http("GET", f"{base}/api/chats/{cid}/proposals?novel={NOVEL}")
    items = (res.body or {}).get("proposals", [])
    new_prop = next((p for p in items if p.get("id") == replacement_id), None)
    brief(suite, "新提案在列表中且状态 pending",
          new_prop is not None and new_prop.get("status") == "pending",
          f"id={replacement_id} status={new_prop.get('status') if new_prop else None}")
    if new_prop is not None:
        brief(suite, "新提案带审校评分", isinstance(new_prop.get("review"), dict),
              json.dumps(new_prop.get("review"), ensure_ascii=False)[:160])
        brief(suite, "新提案内容与原提案不同（确实按意见重做）",
              new_prop.get("proposed") != prop.get("proposed"))
    # 打回过程中同样不得写盘
    brief(suite, "打回后目标文档仍未变", (book_dir / OUTLINE_REL).read_text(encoding="utf-8") == OUTLINE_SEED)

    # ── 6. 通过 → 写入创作空间 ──────────────────────────────────────────
    if new_prop is not None:
        print("\n[6] 通过 → 写入创作空间 + 版本历史")
        res = http(
            "POST",
            f"{base}/api/chats/{cid}/proposals/{replacement_id}/decide?novel={NOVEL}",
            body={"decision": "accept"},
            timeout=60,
        )
        accepted = res.body if isinstance(res.body, dict) else {}
        brief(suite, "POST decide(accept) 返回 200", res.status == 200, f"status={res.status}")
        brief(suite, "★ 提案状态为 accepted（写入创作空间）",
              accepted.get("status") == "accepted", f"status={accepted.get('status')} msg={accepted.get('statusMessage')}")

        # 注意：磁盘文件带 frontmatter，store 的 content 只有正文 —— 用 API 口径比对
        res = http("GET", f"{base}/api/settings/doc?novel={NOVEL}&rel={OUTLINE_REL}")
        body = res.body if isinstance(res.body, dict) else {}
        written = str(body.get("content", ""))
        brief(suite, "GET settings/doc 返回 200", res.status == 200, f"status={res.status}")
        brief(
            suite,
            "★ 创作空间里的资料已同步为提案成稿（API 口径）",
            written.strip() == str(new_prop.get("proposed", "")).strip(),
            f"文档 {len(written)} 字 / 提案 {len(str(new_prop.get('proposed', '')))} 字",
        )
        on_disk = (book_dir / OUTLINE_REL).read_text(encoding="utf-8")
        brief(
            suite,
            "★ 事实源文件已落盘（含 frontmatter，正文即提案成稿）",
            written.strip() and written.strip() in on_disk,
            f"文件 {len(on_disk)} 字",
        )

        res = http("GET", f"{base}/api/history?novel={NOVEL}")
        hist = json.dumps(res.body, ensure_ascii=False)
        brief(suite, "写作历史有本次人工接受的提交",
              res.status == 200 and ("人工接受" in hist or "改稿提案" in hist),
              hist[:160])

        res = http("GET", f"{base}/api/chats/{cid}/proposals?novel={NOVEL}")
        left = [p for p in (res.body or {}).get("proposals", []) if p.get("status") in ("pending", "conflict")]
        brief(suite, "已决策的提案离开待审列表", all(p.get("id") != replacement_id for p in left), f"剩余 {len(left)} 条")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inkforge 端到端全量验收（真实 LLM）")
    parser.add_argument("--sandbox", default="", help="沙箱根目录（默认系统临时目录）")
    parser.add_argument("--keep-sandbox", action="store_true")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path(args.sandbox).resolve() if args.sandbox else Path(tempfile.mkdtemp(prefix=f"inkforge-e2e-{stamp}-"))
    root.mkdir(parents=True, exist_ok=True)
    data = build_sandbox(root)

    env = dict(os.environ)
    env["NOVELS_DIR"] = str(data / "novels")
    env["RUNTIME_DIR"] = str(data / "runtime")
    env["CONFIGS_DIR"] = str(root / "configs")
    env["INKFORGE_ENGINE_TOKEN"] = TOKEN
    env["INKFORGE_GIT_HISTORY"] = "1"

    report: dict = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "kind": "e2e-full",
        "python": sys.executable,
        "engine_dir": str(ENGINE_DIR),
        "sandbox": str(root),
        "suites": [],
    }

    engine = EngineProcess(ENGINE_DIR, NOVEL, env)
    suite = Suite("E2E 全量：建书 → 改稿评分 → 打回重做 → 通过落盘")
    try:
        print("=" * 78)
        print("Inkforge 端到端全量验收（真实创建书目 + 真实 LLM）")
        print("=" * 78)
        print(f"沙箱: {root}")
        engine.start()
        run(data, engine, suite, args)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(50)
        suite.check("全流程无异常", False, report["fatal"])
    finally:
        engine.stop()

    report["suites"].append(
        {"name": suite.name, "passed": suite.passed, "failed": suite.failed, "results": suite.results}
    )
    total = suite.passed
    failed = suite.failed
    report["total_passed"] = total
    report["total_failed"] = failed
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["verdict"] = "PASS" if failed == 0 and "fatal" not in report else "FAIL"
    if failed:
        report["engine_log_tail"] = engine.tail(60)

    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"e2e-{stamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"总计：{total} 通过 / {failed} 失败  →  {report['verdict']}")
    for r in suite.results:
        if not r.get("ok"):
            print(f"  FAIL {r.get('case')}  — {r.get('detail', '')}")
    print(f"结构化结果：{out_file}")
    print("=" * 78)
    if not args.keep_sandbox and failed == 0:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
