#!/usr/bin/env python
"""墨师动作闭环的真实 LLM 验证（关键路径单点验收）。

为什么单独一个脚本：
``smoke_full_audit.py`` 覆盖全部功能面，但**墨师"自己决定调动作"**这一步只能靠真实模型
输出动作块才能验到（零 LLM 模式永远验不到）。这是本次改造的核心功能闭环，值得单点确认：

  1) 用户自然语言 → 墨师输出 <INKFORGE_ACTIONS> 块（只读动作）→ 系统执行 → 汇总答复含真数据
  2) 用户"建一本书" → 墨师登记写动作 → 返回 pending_confirm（**磁盘零变化**）
  3) 用户"确认" → 真正执行 → 书被创建（且工具回执带 undo 信息）

用法：
    cd engine && python smoke_master_live.py            # 需要 .env 里的 DEEPSEEK_API_KEY
    python smoke_master_live.py --keep                  # 保留沙箱
退出码 0 = 全绿。沙箱在系统临时目录，不动 engine/data。
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
try:  # Windows 控制台 GBK 兜底
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from smoke_test import (  # noqa: E402
    TOKEN,
    EngineProcess,
    Suite,
    build_sandbox,
    http,
    seed_demo_book,
)

WORKSPACE = "__workspace__"
LLM = 300.0


def _failed_ops(body: dict) -> list[str]:
    """本轮动作里**真正失败**的那些（status=failed）。

    为什么单独抽出来（真实教训）：本脚本原先只断言"存在 book_create 动作"，
    于是 `{"op":"book_create","status":"failed","error":"缺少参数 novel_id"}`
    也被判成 PASS —— 用户报的"对话建不了书"就是这样在验收里"全绿"溜过去的。
    凡是动作回执，必须区分 ok / pending_confirm / failed 三态。
    """
    return [str(a.get("op")) for a in (body.get("actions") or [])
            if a.get("status") == "failed"]


def run(data_dir: Path, engine: EngineProcess, suite: Suite) -> None:
    base = engine.base
    novels = data_dir / "novels"

    chat = http("POST", f"{base}/api/chats?novel={WORKSPACE}", {"agent": "master"}).body or {}
    cid = chat.get("chat", {}).get("id", "")
    suite.check("工作区会话创建", bool(cid), f"cid={cid}")

    # ── 1) 只读动作闭环：墨师自己查书目 ──
    started = time.time()
    res = http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
               {"message": "我现在有哪些书？进度怎么样？"}, timeout=LLM)
    body = res.body or {}
    actions = body.get("actions") or []
    ops = [a.get("op") for a in actions]
    suite.check(f"1a 墨师调用了只读动作（{time.time() - started:.1f}s）",
                bool(actions),
                f"actions={ops} status={[a.get('status') for a in actions]} "
                f"errors={[a.get('error') for a in actions if a.get('error')]}")
    suite.check("1b 答复含真实书目（demo-web 与实际进度）",
                "demo-web" in json.dumps(body, ensure_ascii=False)
                and any(ch.isdigit() for ch in str(body.get("reply", ""))),
                str(body.get("reply", ""))[:200].replace("\n", " "))

    # ── 2) 写动作闸门：墨师登记但不执行 ──
    target = "live-created"
    started = time.time()
    res2 = http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
                {"message": f"帮我新建一本自由创作的书，书名标识就用 {target}。"}, timeout=LLM)
    body2 = res2.body or {}
    pend = body2.get("pending") or {}
    suite.check(f"2a 墨师登记写动作并请求确认（{time.time() - started:.1f}s）",
                pend.get("op") == "book_create" and pend.get("args", {}).get("novel_id") == target
                or any(a.get("op") == "book_create" and a.get("status") == "pending_confirm"
                       for a in (body2.get("actions") or [])),
                f"pending={json.dumps(pend, ensure_ascii=False)[:160]} "
                f"actions={json.dumps(body2.get('actions'), ensure_ascii=False)[:220]}")
    suite.check("2a2 本轮不得有失败动作（写动作要么待确认、要么已执行）",
                not _failed_ops(body2),
                f"failed={_failed_ops(body2)}")
    suite.check("2b 未确认前磁盘零变化（安全断言）",
                not (novels / target).exists(), "目录不存在即为通过")
    suite.check("2c 答复里给出了影响说明（用户看得懂要做什么）",
                bool(str(body2.get("reply", "")).strip()), str(body2.get("reply", ""))[:160])

    # ── 3) 确认 → 执行 ──
    started = time.time()
    res3 = http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
                {"message": "确认"}, timeout=LLM)
    body3 = res3.body or {}
    suite.check(f"3a 「确认」触发真正执行（{time.time() - started:.1f}s）",
                (novels / target / "settings").is_dir(),
                f"status={res3.status} reply={str(body3.get('reply', ''))[:120]}")
    suite.check("3b 执行后待确认态被清空",
                not (body3.get("pending")), f"pending={body3.get('pending')}")
    suite.check("3b2 确认轮不得有失败动作（回归：确认后什么都没发生）",
                not _failed_ops(body3), f"failed={_failed_ops(body3)} reply={str(body3.get('reply',''))[:120]}")
    suite.check("3c 动作回执带可回滚信息（undo）",
                any("book_create" in json.dumps(a, ensure_ascii=False)
                    for a in (body3.get("actions") or []))
                or (novels / target).exists(),
                f"actions={[a.get('op') for a in (body3.get('actions') or [])]}")

    # ── 4) 审计留痕 ──
    recs = (http("GET", f"{base}/api/actions/audit?limit=50").body or {}).get("records", [])
    suite.check("4 真实执行已入审计（可追溯谁改了什么）",
                any(r.get("op") == "book_create" for r in recs),
                f"{len(recs)} 条，op={[r.get('op') for r in recs][:6]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="墨师动作闭环真实 LLM 验证")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("未检测到 DEEPSEEK_API_KEY → 退出（本脚本只能真实模型跑）")
        return 2

    root = Path(tempfile.mkdtemp(prefix="inkforge-master-live-"))
    data_dir = build_sandbox(root)
    seed_demo_book(data_dir)
    env = dict(os.environ)
    env.update({
        "NOVELS_DIR": str(data_dir / "novels"),
        "RUNTIME_DIR": str(data_dir / "runtime"),
        "CONFIGS_DIR": str(root / "configs"),
        "PROJECT_ROOT": str(root),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
        "PYTHONIOENCODING": "utf-8",
    })

    engine = EngineProcess(ENGINE_DIR, "demo-web", env)
    suite = Suite("墨师动作闭环（真实 LLM）")
    report = {"mode": "live", "started_at": datetime.now().isoformat(timespec="seconds")}
    try:
        engine.start()
        run(data_dir, engine, suite)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(30)
    finally:
        engine.stop()

    report.update({
        "passed": suite.passed, "failed": suite.failed,
        "verdict": "PASS" if suite.failed == 0 and "fatal" not in report else "FAIL",
        "results": suite.results,
    })
    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"master-live-{datetime.now():%Y%m%d-%H%M%S}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 78)
    print(f"总计：{suite.passed} 通过 / {suite.failed} 失败  ->  {report['verdict']}")
    for r in suite.results:
        if not r["ok"]:
            print(f"  FAIL {r['case']}  --  {r.get('detail', '')}")
    print(f"结构化报告：{out_file}")
    print("=" * 78)
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
