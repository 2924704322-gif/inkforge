#!/usr/bin/env python
"""Inkforge 墨师全域化验收（P0–P4）：**默认零真实 LLM**，可常驻跑。

验证目标（对应改造方案的验收标准）：
  [1] 工作区会话：不建书也能开对话（会话落在 data/workspace/chats/，不进任何书）
  [2] 动作清单与审计端点可读
  [3] 只读动作免确认直接执行
  [4] 写动作闸门：未确认只回"影响说明"、零落盘；确认后才执行
  [5] 会话内确认/取消 + 工作台当前书目切换
  [6] 审计可视化：/api/actions/audit 只暴露必要字段（无 args_digest）
  [7] 既有语义回归：缺省 novel 仍是默认书；非法 novel 仍被拒
  [8] 可选：真实 LLM 对话（--with-llm，会计费）

用法：
    cd engine && python smoke_master_workspace.py          # 零 LLM（默认）
    python smoke_master_workspace.py --with-llm            # 追加真实 LLM 用例

沙箱落在系统临时目录，绝不动 engine/data 下的真实作品。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from smoke_test import (  # noqa: E402 - 复用真实进程 / HTTP 夹具
    TOKEN,
    EngineProcess,
    Suite,
    build_sandbox,
    http,
    seed_demo_book,
)

WORKSPACE = "__workspace__"
LLM_TIMEOUT = 240.0


def run(data_dir: Path, engine: EngineProcess, suite: Suite, args: argparse.Namespace) -> None:
    base = engine.base

    # ── [1] 工作区会话：不建书也能开对话 ──
    print("\n[1] 工作区会话（无书可聊）", flush=True)
    res = http("POST", f"{base}/api/chats?novel={WORKSPACE}", {"agent": "master"})
    suite.check("POST /api/chats（工作区）→ 200", res.status == 200, f"status={res.status}")
    chat = (res.body or {}).get("chat", {})
    chat_id = chat.get("id", "")
    suite.check("工作区会话 scope=workspace 且不隶属任何书",
                chat.get("scope") == "workspace" and chat.get("novel") == "",
                f"scope={chat.get('scope')} novel={chat.get('novel')!r}")
    ws_chat_file = data_dir / "workspace" / "chats" / f"{chat_id}.json"
    suite.check("会话文件落在 data/workspace/chats/ 而非某本书内",
                ws_chat_file.exists(), str(ws_chat_file))

    listed = http("GET", f"{base}/api/chats?novel={WORKSPACE}").body or {}
    suite.check("工作区会话列表只含工作区会话",
                [c.get("scope") for c in listed.get("chats", [])] == ["workspace"],
                json.dumps(listed, ensure_ascii=False)[:160])

    # ── [2] 动作清单 ──
    print("\n[2] 动作清单", flush=True)
    manifest = http("GET", f"{base}/api/actions").body or {}
    ops = {a["op"]: a for a in manifest.get("actions", [])}
    suite.check("动作清单可读且读写两类齐备",
                "book_list" in ops and ops["book_list"]["scope"] == "read"
                and "book_create" in ops and ops["book_create"]["scope"] == "write",
                f"{len(ops)} 个动作")

    # ── [3] 只读动作：免确认直接执行 ──
    print("\n[3] 只读动作（免确认）", flush=True)
    books = http("GET", f"{base}/api/books").body or {}
    suite.check("书架可读（默认书 demo-web 在内）",
                any(b["novel_id"] == "demo-web" for b in books.get("books", [])),
                f"{len(books.get('books', []))} 本")
    read_only = http("POST", f"{base}/api/actions/run",
                     {"op": "chapter_list", "args": {"novel": "demo-web"}})
    suite.check("只读动作不得走写通道（/api/actions/run 只服务写动作）→ 400",
                read_only.status == 400, f"status={read_only.status}")

    # ── [4] 写动作闸门：未确认零落盘 ──
    print("\n[4] 写动作必须确认（未确认零落盘）", flush=True)
    new_book = "smoke-created"
    preview = http("POST", f"{base}/api/actions/run",
                   {"op": "book_create", "args": {"novel_id": new_book, "mode": "pipeline"}})
    body = preview.body or {}
    suite.check("未确认 → pending_confirm + 影响说明",
                preview.status == 200 and body.get("status") == "pending_confirm"
                and "将新建书目" in json.dumps(body, ensure_ascii=False),
                f"status={preview.status} body={json.dumps(body, ensure_ascii=False)[:160]}")
    suite.check("未确认 → 磁盘上没有任何变化",
                not (data_dir / "novels" / new_book).exists(), "目录不存在即为通过")

    done = http("POST", f"{base}/api/actions/run",
                {"op": "book_create", "args": {"novel_id": new_book, "mode": "pipeline"},
                 "confirm": True})
    suite.check("确认后 → 真正建书成功",
                done.status == 200 and (done.body or {}).get("ok") is True
                and (data_dir / "novels" / new_book / "settings").is_dir(),
                f"status={done.status}")

    wrong = http("POST", f"{base}/api/actions/run", {"op": "book_list", "confirm": True})
    suite.check("只读动作走写通道 → 400", wrong.status == 400, f"status={wrong.status}")

    # ── [5] 会话内确认 / 取消闸门 + 当前书目 ──
    print("\n[5] 会话内待确认动作：确认执行 / 取消丢弃", flush=True)
    http("PUT", f"{base}/api/book-select", {"novel_id": new_book})
    selected = http("GET", f"{base}/api/book-select").body or {}
    suite.check("工作台当前书目已切换", selected.get("novel_id") == new_book,
                json.dumps(selected, ensure_ascii=False))

    payload = json.loads(ws_chat_file.read_text(encoding="utf-8"))
    payload["pending_action"] = {
        "op": "book_select",
        "args": {"novel_id": "demo-web"},
        "impact": "将把工作台当前书目切换为 demo-web。",
    }
    ws_chat_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    confirmed = http("POST", f"{base}/api/chats/{chat_id}/action?novel={WORKSPACE}")
    suite.check("确认接口执行成功",
                confirmed.status == 200 and (confirmed.body or {}).get("ok") is True,
                json.dumps(confirmed.body, ensure_ascii=False)[:160])
    now_selected = http("GET", f"{base}/api/book-select").body or {}
    suite.check("确认后当前书目真的切换了", now_selected.get("novel_id") == "demo-web",
                json.dumps(now_selected, ensure_ascii=False))

    payload = json.loads(ws_chat_file.read_text(encoding="utf-8"))
    payload["pending_action"] = {
        "op": "book_delete", "args": {"novel": new_book}, "impact": "将删除书目",
    }
    ws_chat_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    cancelled = http("DELETE", f"{base}/api/chats/{chat_id}/action?novel={WORKSPACE}")
    suite.check("取消接口生效",
                cancelled.status == 200 and (cancelled.body or {}).get("cancelled") is True,
                json.dumps(cancelled.body, ensure_ascii=False)[:160])
    suite.check("取消后数据仍在（没被误删）",
                (data_dir / "novels" / new_book).is_dir(), "目录仍存在")

    # ── [6] 审计 ──
    print("\n[6] 墨师操作审计", flush=True)
    audit = http("GET", f"{base}/api/actions/audit?limit=50").body or {}
    records = audit.get("records", [])
    suite.check("审计有记录", len(records) > 0, f"{len(records)} 条")
    suite.check("审计不外泄内部指纹（无 args_digest）",
                all("args_digest" not in r for r in records), "字段已收敛")
    suite.check("审计内容含动作名与作用域",
                all(r.get("op") and r.get("scope") in ("read", "write") for r in records),
                f"op 分布：{[r.get('op') for r in records][:8]}")

    # ── [7] 既有语义回归 ──
    print("\n[7] 既有语义回归（缺省 novel = 默认书）", flush=True)
    legacy = http("POST", f"{base}/api/chats", {"agent": "master"}).body or {}
    legacy_chat = legacy.get("chat", {})
    suite.check("不带 novel 的会话仍是书内会话（scope=book）",
                legacy_chat.get("scope") == "book"
                and legacy_chat.get("novel") == "demo-web",
                json.dumps(legacy_chat, ensure_ascii=False)[:160])
    bad = http("GET", f"{base}/api/chapters?novel=../evil")
    suite.check("非法 novel 仍被拒（400）", bad.status == 400, f"status={bad.status}")

    # ── [8] 可选：真实 LLM ──
    if args.with_llm:
        print("\n[8] 真实 LLM：让墨师列出书目（会计费）", flush=True)
        started = time.time()
        res = http(
            "POST",
            f"{base}/api/chats/{chat_id}/send?novel={WORKSPACE}",
            {"message": "我现在有哪些书？各自进度如何？"},
            timeout=LLM_TIMEOUT,
        )
        body = res.body or {}
        ok = res.status == 200 and bool(body.get("reply"))
        suite.check("墨师答复返回", ok,
                    f"status={res.status} 耗时 {time.time() - started:.1f}s")
        if ok:
            suite.check("答复含真实书目（不是空话）",
                        "demo-web" in json.dumps(body, ensure_ascii=False),
                        str(body.get("reply", ""))[:200])
            suite.check("本轮产生了动作回执", bool(body.get("actions")),
                        json.dumps(body.get("actions"), ensure_ascii=False)[:200])


def main() -> int:
    parser = argparse.ArgumentParser(description="墨师全域化验收（默认零 LLM）")
    parser.add_argument("--with-llm", action="store_true",
                        help="追加真实 LLM 对话用例（会计费）")
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录（排障用）")
    args = parser.parse_args()

    if args.with_llm and not os.environ.get("DEEPSEEK_API_KEY"):
        print("已请求 --with-llm 但环境变量 DEEPSEEK_API_KEY 未设置 → 跳过真实 LLM 用例。")
        args.with_llm = False

    root = Path(tempfile.mkdtemp(prefix="inkforge-master-smoke-"))
    data_dir = build_sandbox(root)
    seed_demo_book(data_dir)

    env = dict(os.environ)
    env.update({
        "NOVELS_DIR": str(data_dir / "novels"),
        "RUNTIME_DIR": str(data_dir / "runtime"),
        "CONFIGS_DIR": str(root / "configs"),
        "PROJECT_ROOT": str(root),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
    })

    engine = EngineProcess(ENGINE_DIR, "demo-web", env)
    suite = Suite("墨师全域化（P0–P4）")
    report: dict = {"mode": "with-llm" if args.with_llm else "zero-llm"}

    try:
        engine.start()
        run(data_dir, engine, suite, args)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(30)
    finally:
        engine.stop()

    total, failed = suite.passed, suite.failed
    report.update({
        "passed": total, "failed": failed,
        "verdict": "PASS" if failed == 0 and "fatal" not in report else "FAIL",
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })

    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"master-workspace-{datetime.now():%Y%m%d-%H%M%S}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"总计：{total} 通过 / {failed} 失败  →  {report['verdict']}")
    for r in suite.results:
        if not r["ok"]:
            print(f"  FAIL {r['case']}  —  {r.get('detail', '')}")
    print(f"结构化结果：{out_file}")
    print("=" * 78)
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
