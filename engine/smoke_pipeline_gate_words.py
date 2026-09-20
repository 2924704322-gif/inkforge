#!/usr/bin/env python
"""真实冒烟：自由创作逐章关卡的「生成前设定字数」是否真的生效（问题③ 的另一条路）。

验证链：大纲通过 → 第 1 章定稿 → 逐章关卡上报字数区间 → 关卡上改写字数放行
        → 第 2 章**必须按关卡上改的那个数**写作/评分/落盘（不被大纲预算覆盖）。

用法（需要 engine/.env 里的真实 API Key）：
    python engine/smoke_pipeline_gate_words.py
    python engine/smoke_pipeline_gate_words.py --gate-words 5000
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import TOKEN, EngineProcess, build_sandbox, http  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parent
BRIEF = "现代都市悬疑：旧书店店主靠一本被寄错的账册追查旧案。基调冷硬写实，不要奇幻元素。"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate-words", type=int, default=1500,
                    help="在关卡上为第 2 章设定的预期字数")
    ap.add_argument("--keep-sandbox", action="store_true")
    ap.add_argument("--poll-timeout", type=int, default=2400)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sandbox = Path(tempfile.mkdtemp(prefix=f"inkforge-gate-{stamp}-"))
    data = build_sandbox(sandbox)
    env = dict(os.environ)
    env.update({"NOVELS_DIR": str(data / "novels"), "RUNTIME_DIR": str(data / "runtime"),
                "CONFIGS_DIR": str(sandbox / "configs"),
                "INKFORGE_ENGINE_TOKEN": TOKEN, "INKFORGE_GIT_HISTORY": "1"})
    nid = "e2e-gate"
    eng = EngineProcess(ENGINE_DIR, nid, env)
    results: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, ev: object = "") -> None:
        results.append((name, bool(ok), str(ev)[:200]))
        line = f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {str(ev)[:200]}" if ev else "")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # pragma: no cover
            print(line.encode("utf-8", "replace").decode("utf-8"), flush=True)

    def poll(fn, ok_fn, timeout, what):
        t0, last = time.time(), None
        while time.time() - t0 < timeout:
            last = fn()
            if ok_fn(last):
                return last, round(time.time() - t0, 1)
            time.sleep(5)
        print(f"  [poll] {what} 超时，末态={json.dumps(last, ensure_ascii=False)[:240]}")
        return last, -round(time.time() - t0, 1)

    def pending(body): return (body or {}).get("pending") or {}

    try:
        eng.start()
        b = eng.base
        print("=" * 78)
        print(f"自由创作关卡字数真实冒烟 — 关卡设定 {args.gate_words} 字")
        print("=" * 78)
        r = http("POST", f"{b}/api/books", body={"novel_id": nid, "mode": "pipeline"})
        chk("0 建自由创作书", r.status == 200, r.body)
        r = http("POST", f"{b}/api/start?novel={nid}",
                 body={"brief": BRIEF, "chapters": 2}, timeout=90)
        chk("1 启动生成（2 章）", r.status == 200, r.body)

        p, secs = poll(lambda: pending(http("GET", f"{b}/api/status?novel={nid}").body or {}),
                       lambda q: q.get("type") == "outline_review", args.poll_timeout, "大纲")
        chk("2 大纲待审", p.get("type") == "outline_review", f"{secs}s type={p.get('type')}")
        vols = (p.get("outline") or {}).get("volumes") or []
        budgets = [c.get("target_words") for v in vols for c in (v.get("chapters") or [])]
        chk("2b 大纲为每章给出席位字数预算（问题③ 的源头）",
            any(isinstance(x, int) and x > 0 for x in budgets), f"budgets={budgets}")
        http("POST", f"{b}/api/decision?novel={nid}", body={"action": "approve"}, timeout=60)

        # 第 1 章：按大纲预算写完 → 审阅 → 定稿 → 到达逐章关卡
        p, secs = poll(lambda: pending(http("GET", f"{b}/api/status?novel={nid}").body or {}),
                       lambda q: q.get("type") == "chapter_review", args.poll_timeout, "ch1审阅")
        chk("3 第 1 章送审", p.get("type") == "chapter_review",
            f"{secs}s words={p.get('actual_length')} target={p.get('target_words')}")
        chk("3b 审阅载荷带字数区间（前端据此显示合格线）",
            isinstance(p.get("length_floor"), int) and isinstance(p.get("length_ceiling"), int),
            f"band=({p.get('length_floor')},{p.get('length_ceiling')}) target={p.get('target_words')}")
        http("POST", f"{b}/api/decision?novel={nid}", body={"action": "approve"}, timeout=60)

        gate, secs = poll(lambda: pending(http("GET", f"{b}/api/status?novel={nid}").body or {}),
                          lambda q: q.get("type") == "chapter_gate", args.poll_timeout, "关卡")
        chk("4 到达逐章关卡", gate.get("type") == "chapter_gate",
            f"{secs}s next={gate.get('next_chapter')}")
        chk("4b 关卡上报下一章预期字数 + 可接受区间（生成前设定入口）",
            bool(gate.get("target_words")) and isinstance(gate.get("length_floor"), int),
            f"target={gate.get('target_words')} band=({gate.get('length_floor')},{gate.get('length_ceiling')})")

        # ★ 关卡上改写字数并放行
        r = http("POST", f"{b}/api/decision?novel={nid}",
                 body={"action": "approve", "target_words": args.gate_words}, timeout=60)
        chk("5 按新字数放行", r.status == 200, r.body)
        p2, secs = poll(lambda: pending(http("GET", f"{b}/api/status?novel={nid}").body or {}),
                        lambda q: q.get("type") == "chapter_review"
                        and q.get("chapter") == 2, args.poll_timeout, "ch2审阅")
        chk("6 第 2 章送审", p2.get("chapter") == 2 and p2.get("type") == "chapter_review",
            f"{secs}s type={p2.get('type')} chapter={p2.get('chapter')}")
        chk("7 ★★ 关卡上设定的字数真正生效（未被大纲预算覆盖）",
            p2.get("target_words") == args.gate_words,
            f"target={p2.get('target_words')} expected={args.gate_words}")
        words2, target2 = p2.get("actual_length"), p2.get("target_words")
        if isinstance(words2, int) and isinstance(target2, int):
            lo, hi = target2 - 500, target2 + 2000
            chk("8 ★ 第 2 章正文落在该目标的区间内", lo <= words2 <= hi,
                f"words={words2} band={lo}-{hi}")
        ch2 = data / "novels" / nid / "chapters" / "vol-01" / "ch-002.md"
        meta = ch2.read_text(encoding="utf-8")[:400] if ch2.exists() else ""
        chk("9 章节 frontmatter 记录该目标字数",
            f"target_words: {args.gate_words}" in meta,
            meta.split("---")[1][:150].replace("\n", " ") if "---" in meta else meta[:100])
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        chk("FATAL 冒烟异常", False, f"{type(exc).__name__}: {exc}")
        print("\n".join(eng.tail(30)))
    finally:
        eng.stop()

    fails = [x for x in results if not x[1]]
    print()
    print("=" * 78)
    print(f"总计：{len(results) - len(fails)} 通过 / {len(fails)} 失败 → "
          f"{'PASS' if not fails else 'FAIL'}")
    print("=" * 78)
    out = ENGINE_DIR / "smoke-reports" / f"smoke-gate-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"results": [{"case": n, "ok": o, "evidence": e} for n, o, e in results],
         "gate_words": args.gate_words}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化结果：{out}")
    if not args.keep_sandbox:
        shutil.rmtree(sandbox, ignore_errors=True)
    else:
        print(f"沙箱已保留：{sandbox}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
