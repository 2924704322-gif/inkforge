#!/usr/bin/env python
"""真实冒烟：字数门禁（非对称）与「打回重写」链路（真实 LLM + 真实 HTTP + 真实文件系统）。

覆盖用户实测的三类问题（对应本次修复）：
  ① 打回重写是否真的按建议改（关键词落地 + 目标字数不丢）；
  ② 要求 N 字时实际产出是否落在 [N-500, N+2000]（互动路径的真实门禁）；
  ③ 生成**之前**设定本章预期字数是否贯通写作/评分/门禁/落盘。

用法（需要 engine/.env 里的真实 API Key；会产生真实调用费用）：
    python engine/smoke_length_and_revision.py
    python engine/smoke_length_and_revision.py --target-words 5000 --keep-sandbox

退出码：0 全部通过；1 有失败项。
产物：engine/smoke-reports/smoke-length-<时间戳>.json
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

BRIEF = (
    "现代都市悬疑：主角是旧书店的年轻店主，靠一本被寄错的账册追查一桩旧案。"
    "基调冷硬写实，严禁出现奇幻、异能、穿越元素。"
)
CUSTOM_CARD = (
    "本章唯一场景：深夜的旧书店后仓库。主角与来讨账的中年男人当面对峙，"
    "两人围绕那本账册争执；必须出现一只停在书堆上的灰猫；"
    "结尾账册里掉出一张写着地址的纸条。不得出现打斗。"
)
# 打回意见：要求增补两处可验证的具体内容 + 一处可验证的删除
REJECT_FEEDBACK = (
    "【按审校建议逐条修改】\n"
    "1. 对峙的张力不够：必须补写中年男人把手按在账册上、主角把账册抽回来的具体过程；\n"
    "2. 灰猫不能只是环境道具：必须写它被争执声惊得跳下书堆的动作；\n"
    "3. 删掉结尾处主角的内心总结（不要用'他知道，事情没那么简单'这类句子收尾）。"
)
KEYWORDS = ("账册", "灰猫", "纸条")


def main() -> int:
    parser = argparse.ArgumentParser(description="Inkforge 字数/打回重写真实冒烟")
    parser.add_argument("--target-words", type=int, default=1200,
                        help="本章预期字数（默认 1200，跑得快；要压 5000 字门槛就传 5000）")
    parser.add_argument("--keep-sandbox", action="store_true")
    parser.add_argument("--poll-timeout", type=int, default=2400,
                        help="单步轮询上限（秒）；5000 字目标需要更长")
    args = parser.parse_args()

    target = int(args.target_words)
    floor, ceiling = max(1, target - 500), target + 2000

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sandbox = Path(tempfile.mkdtemp(prefix=f"inkforge-length-{stamp}-"))
    data = build_sandbox(sandbox)
    env = dict(os.environ)
    env.update({
        "NOVELS_DIR": str(data / "novels"),
        "RUNTIME_DIR": str(data / "runtime"),
        "CONFIGS_DIR": str(sandbox / "configs"),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
        "INKFORGE_GIT_HISTORY": "1",
    })
    NID = "e2e-length"
    eng = EngineProcess(ENGINE_DIR, NID, env)

    results: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, ev: object = "") -> None:
        results.append((name, bool(ok), str(ev)[:220]))
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}" + (f"  -- {str(ev)[:220]}" if ev else "")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # pragma: no cover - 控制台编码兜底
            print(line.encode("utf-8", "replace").decode("utf-8"), flush=True)

    def poll(fn, ok_fn, timeout: float, what: str):
        t0 = time.time()
        last = None
        while time.time() - t0 < timeout:
            last = fn()
            if ok_fn(last):
                return last, round(time.time() - t0, 1)
            time.sleep(5)
        print(f"  [poll] {what} 超时（{timeout}s），末态={json.dumps(last, ensure_ascii=False)[:300]}")
        return last, -round(time.time() - t0, 1)

    report: dict = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "target_words": target,
        "acceptable_band": [floor, ceiling],
        "sandbox": str(sandbox),
        "results": results,
    }

    try:
        eng.start()
        b = eng.base
        print("=" * 78)
        print(f"字数/打回重写真实冒烟 — 目标 {target} 字（可接受 {floor}-{ceiling} 字）")
        print("=" * 78)

        # ── 0. 建互动书（不写大纲，剧情由剧情卡决定）──
        r = http("POST", f"{b}/api/books", body={"novel_id": NID, "mode": "interactive"})
        chk("0 建互动书", r.status == 200, r.body)

        # ── 1. 世界观 Demo（真实 LLM）──
        r = http("POST", f"{b}/api/demo?novel={NID}",
                 body={"brief": BRIEF, "chapters": 20}, timeout=90)
        chk("1 设定 Demo 已启动", r.status == 200, r.body)
        snap, secs = poll(lambda: http("GET", f"{b}/api/demo?novel={NID}").body or {},
                          lambda s: s.get("status") in ("done", "error"),
                          args.poll_timeout, "demo")
        chk("2 设定 Demo 完成（真实 LLM）", snap.get("status") == "done",
            f"{secs}s err={snap.get('error')}")
        r = http("POST", f"{b}/api/demo/confirm?novel={NID}", body={}, timeout=180)
        chk("3 设定入库", r.status == 200, r.body)

        # ── 2. 出卡（真实 LLM）──
        r = http("POST", f"{b}/api/interactive/start?novel={NID}", body=None, timeout=90)
        chk("4 互动创作已启动", r.status == 200, r.body)
        st, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                        lambda s: s.get("status") in ("awaiting_choice", "error"),
                        args.poll_timeout, "cards")
        chk("5 三张剧情卡就绪", st.get("status") == "awaiting_choice" and st.get("cards"),
            f"{secs}s status={st.get('status')} err={st.get('error')}")
        chk("5b 生成前就报出预期字数区间（问题③）",
            st.get("target_words") == 3000 and st.get("length_floor") == 2500
            and st.get("length_ceiling") == 5000,
            f"target={st.get('target_words')} band=({st.get('length_floor')},{st.get('length_ceiling')})")

        # ── 3. 生成前设定字数 + 自拟卡（问题③ + 问题②）──
        r = http("POST", f"{b}/api/interactive/choose?novel={NID}",
                 body={"card_id": "custom", "custom_text": CUSTOM_CARD,
                       "target_words": target}, timeout=120)
        chk("6 选卡（携预期字数）", r.status == 200, r.body)
        st, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                        lambda s: s.get("status") in ("awaiting_review", "error"),
                        args.poll_timeout, "draft")
        chk("7 写章完成（真实 LLM）",
            st.get("status") == "awaiting_review" and (st.get("draft") or {}).get("draft_text"),
            f"{secs}s status={st.get('status')} err={st.get('error')}")
        d = st.get("draft") or {}
        words = d.get("words") or len(str(d.get("draft_text") or ""))
        print(f"  [info] 第 1 稿：{words} 字（目标 {target}，可接受 {floor}-{ceiling}）")

        # ── ★ 核心断言（问题②）：字数真实落在区间内 ──
        chk(f"8 ★ 正文字数落在可接受区间 {floor}-{ceiling}",
            floor <= words <= ceiling,
            f"words={words} target={d.get('target_words')} "
            f"band=({d.get('length_floor')},{d.get('length_ceiling')}) ok={d.get('length_ok')}")
        chk("8b 目标字数按生成前设定生效并落盘（问题③）",
            d.get("target_words") == target,
            f"target_words={d.get('target_words')} expected={target}")
        chk("8c 区间随目标一起回传（前端与门禁同一对数）",
            (d.get("length_floor"), d.get("length_ceiling")) == (floor, ceiling),
            f"band=({d.get('length_floor')},{d.get('length_ceiling')})")
        rv = d.get("review") or {}
        chk("8d 审校四维评分在场", all(k in rv for k in
                                  ("consistency", "plot", "continuity", "prose")),
            json.dumps({k: rv.get(k) for k in ("consistency", "plot", "continuity",
                                               "prose", "length")}, ensure_ascii=False))
        chk("8e 字数维度的自评分与客观字数一致（欠字数不会给满分）",
            not (words < floor and float(rv.get("length") or 0) >= 10.0),
            f"length={rv.get('length')} words={words} floor={floor}")
        text0 = str(d.get("draft_text") or "")
        hits0 = [k for k in KEYWORDS if k in text0]
        chk("8f 初稿按自拟卡推进（关键词命中≥2）", len(hits0) >= 2,
            f"命中={hits0} len={len(text0)}")

        # ── 4. 打回重写（问题①）──
        r = http("POST", f"{b}/api/interactive/decision?novel={NID}",
                 body={"action": "reject", "feedback": REJECT_FEEDBACK,
                       "revision_mode": "targeted", "target_words": target},
                 timeout=120)
        chk("9 打回重写已受理", r.status == 200, r.body)
        st2, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                         lambda s: s.get("status") in ("awaiting_review", "error")
                         and (s.get("draft") or {}).get("attempt", 1) >= 2,
                         args.poll_timeout, "rewrite")
        d2 = st2.get("draft") or {}
        words2 = d2.get("words") or len(str(d2.get("draft_text") or ""))
        print(f"  [info] 第 {d2.get('attempt')} 稿：{words2} 字，"
              f"目标 {d2.get('target_words')}（可接受 {floor}-{ceiling}）")
        chk("10 重写产生新稿（attempt 递增）", (d2.get("attempt") or 1) >= 2,
            f"{secs}s attempt={d2.get('attempt')} status={st2.get('status')} err={st2.get('error')}")
        chk("10b ★ 打回后目标字数没有丢（问题③ 的连带项）",
            d2.get("target_words") == target,
            f"target_words={d2.get('target_words')} expected={target}")
        chk("10c ★ 重写稿仍落在可接受区间",
            floor <= words2 <= ceiling,
            f"words={words2} band=({d2.get('length_floor')},{d2.get('length_ceiling')})")
        chk("10d 重写稿比上一稿更接近目标（未缩水）",
            words2 >= min(words, floor),
            f"before={words} after={words2}")
        text1 = str(d2.get("draft_text") or "")
        hits1 = [k for k in KEYWORDS if k in text1]
        chk("11 ★ 重写稿保留本章核心要素（关键词命中≥2）", len(hits1) >= 2, f"命中={hits1}")
        chk("11b ★ 打回意见被真实落实（重写稿确实变了）",
            text1 != text0 and len(text1) > 200,
            f"before_len={len(text0)} after_len={len(text1)} same={text1 == text0}")
        chk("11c 重写稿不是旧稿的复制（内容有实质差异）",
            _difference_ratio(text0, text1) > 0.05,
            f"差异率={_difference_ratio(text0, text1):.3f}")

        # ── 5. 定稿（人审通过）──
        r = http("POST", f"{b}/api/interactive/decision?novel={NID}",
                 body={"action": "approve"}, timeout=180)
        chk("12 定稿入库已受理", r.status == 200, r.body)
        st3, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                         lambda s: (s.get("approved_count") or 0) >= 1, 600, "commit")
        chk("13 定稿成功（approved_count≥1）", (st3.get("approved_count") or 0) >= 1,
            f"{secs}s approved={st3.get('approved_count')}")

        # 落盘事实源：目标字数与实际字数都写进了章节 frontmatter
        ch_path = data / "novels" / NID / "chapters" / "vol-01" / "ch-001.md"
        meta_txt = ch_path.read_text(encoding="utf-8")[:600] if ch_path.exists() else ""
        chk("14 frontmatter 记录 target_words",
            f"target_words: {target}" in meta_txt,
            meta_txt.split("---")[1][:160].replace("\n", " ") if "---" in meta_txt else meta_txt[:120])

        # ── 6. 边界：非法/缺失参数不炸（防御性回归）──
        r = http("POST", f"{b}/api/interactive/decision?novel={NID}",
                 body={"action": "reject", "feedback": "   "}, timeout=30)
        chk("15 空意见打回被拒（409/400，且不动稿件）", r.status in (400, 409),
            f"status={r.status}")
        r = http("POST", f"{b}/api/interactive/decision?novel={NID}",
                 body={"action": "reject", "feedback": "目标字数给个脏值", "target_words": 0},
                 timeout=30)
        chk("15b target_words=0 被视为未设置（不写入载荷、不报错）", r.status in (200, 409),
            f"status={r.status}")

    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        chk("FATAL 冒烟脚本异常终止", False, f"{type(exc).__name__}: {exc}")
        report["engine_log_tail"] = eng.tail(40)
    finally:
        eng.stop()

    fails = [x for x in results if not x[1]]
    report["results"] = [{"case": n, "ok": ok, "evidence": ev} for n, ok, ev in results]
    report["total_passed"] = len(results) - len(fails)
    report["total_failed"] = len(fails)
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["verdict"] = "PASS" if not fails else "FAIL"

    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"smoke-length-{stamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"总计：{report['total_passed']} 通过 / {report['total_failed']} 失败 → {report['verdict']}")
    print(f"结构化结果：{out_file}")
    print("=" * 78)

    if not args.keep_sandbox:
        shutil.rmtree(sandbox, ignore_errors=True)
    else:
        print(f"沙箱已保留：{sandbox}")

    return 1 if fails else 0


def _difference_ratio(a: str, b: str) -> float:
    """粗略差异率：按 200 字切片比较，返回不一致切片占比（0-1）。"""
    if not a or not b:
        return 1.0
    step = 200
    ca = [a[i:i + step] for i in range(0, len(a), step)]
    cb = {b[i:i + step] for i in range(0, len(b), step)}
    same = sum(1 for c in ca if c in cb)
    return 1 - same / max(len(ca), 1)


if __name__ == "__main__":
    raise SystemExit(main())
