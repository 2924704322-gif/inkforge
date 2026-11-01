#!/usr/bin/env python
"""真实冒烟：拒答率**达标判定**（Phase 4 · 改后对照，含可证伪阈值）。

与 `smoke_refusal_baseline.py` 的分工：
- baseline = 采集（Phase 0，先测后改），输出 R0；
- 本脚本 = 同一套采集器（`refusal-collector/v1`，实现只在 baseline 里，这里 import）
  + **判定**：改动后拒答率 R1 是否达标、是否不劣化、prompt 是否真的带上了第零条与虚构框架声明。

阈值全部可失败（"或由作者确认的容忍上限"这类永远成立的写法一律不写）。
**阈值与样本量必须配套**：一个比例阈值只在样本足够时才有意义（`--repeat 1` 时 8 次尝试里
出现 1 次空产出就是 12.5%，那只是方差）。因此：

  **正式验收协议 = `--repeat 3 --with-quality`**（7 场景 × 3 = 21 次尝试 ≥ 门槛 18）；
  达不到门槛时，比例类判定自动降级为「观测」，不计失败，并在报告里标 `sample_sufficient=false`。

判定项与阈值（2026-09-22 按真机数据定）：
  1.【主指标】空产出率 ≤ `--max-empty-rate`（0.10）—— 真机 16000 实测 2.3%，老配置 8000 是 38.7%
  2.【主指标】截断率 ≤ `--max-truncation-rate`（0.15）—— 实测 4.7%，老配置 54.8%；
     0.60 这种"等于老配置水平"的值不构成约束，故不收
  2b.【主指标·5000 字场景】`long` 单场景截断率 ≤ `--max-long-truncation-rate`（0.34）
     —— 聚合闸看不见 long 的退化（7 场景里 long 只占 1/7），故单设此闸
  3. 调用异常 = 0（有异常说明接入点/参数有问题，数据不可用于判定）
  4. 每次运行的 prompt 必须含第零条探针句与虚构框架声明（防拒绝块端到端覆盖）
  5.【安全网】拒答率 ≤ `--max-refusal-rate`（0.02）—— **真机基线恒为 0**，
     这条只防回归，**不能用来证明改进**；收到 0 会被已知的"尾部对白 `「抱歉。」`"误判打红，
     故保留 0.02 不动
  6. 给了 `--baseline` 时：空产出率/截断率不升、字数落区间率不降
     （容差 `--max-quality-drop` 0.05 + 绝对容差 `--abs-tol` 0.05；两侧样本 ≥ 门槛才判）
  7. 质量不劣化（编辑评分 / 约束术语命中率；评分样本 ≥ `--min-quality-runs` 才判）

指标为何换（2026-09-22 真机数据驱动）：原主指标"拒答率"在真机上恒为 0，
"≤2%"于是永远通过、不可失败 —— 正是本方案当初批评旧 DoD 的那个毛病。
改以真机上真实存在、且会随改动变化的空产出率与截断率为主指标。

用法：
    python engine/smoke_refusal_flash.py --repeat 5                 # 便宜探路
    python engine/smoke_refusal_flash.py --repeat 20 \
        --baseline engine/smoke-reports/refusal-baseline-<时间戳>.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from smoke_refusal_baseline import (  # noqa: E402
    _CHILD_QUALITY,
    COLLECTOR_VERSION,
    SCENARIOS,
    aggregate,
    aggregate_quality,
    collect,
    git_state,
    load_writer_params,
    make_env,
    precheck,
    print_quality,
    print_summary,
    save_report,
)


def _baseline_rates(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    agg = data.get("aggregate") or {}
    return {
        "path": str(path),
        "protocol": data.get("protocol"),
        # 主指标（真机基线非 0，会随改动变化）
        "empty_output_rate": agg.get("empty_output_rate"),
        "truncation_rate": agg.get("truncation_rate"),
        "length_in_band_rate": agg.get("length_in_band_rate"),
        # 安全网指标（基线为 0，仅作回归防护）
        "refusal_rate": agg.get("refusal_rate"),
        "runs_total": agg.get("runs_total"),
        "attempts_total": agg.get("attempts_total"),
        # 质量口径（仅 --with-quality 的报告里有）
        "quality": data.get("quality_aggregate") or {},
        "git": data.get("git", {}),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="拒答率达标判定（Phase 4）")
    ap.add_argument("--repeat", type=int, default=3,
                    help="每场景重复次数。**正式验收协议 = 3**（21 次尝试 ≥ 门槛 18）；"
                         "快跑用 1（比例判定会降级为观测）")
    ap.add_argument("--only", default="", help="只跑指定场景 id，逗号分隔")
    ap.add_argument("--baseline", default="", help="对照基线报告 JSON 路径（baseline 或上一轮 flash）")
    ap.add_argument("--max-empty-rate", type=float, default=0.10,
                    help="主指标阈值：空产出率上限（真机 16000 实测 2.3%%，留 ~4 倍余量）")
    ap.add_argument("--max-truncation-rate", type=float, default=0.15,
                    help="主指标阈值：截断率上限（真机 16000 实测 4.7%%；老配置 8000 是 54.8%%）")
    ap.add_argument("--max-long-truncation-rate", type=float, default=0.34,
                    help="主指标阈值：**long（5000 字）单场景**截断率上限。聚合闸测不出 long 的"
                         "退化（7 场景里 long 只占 1/7），故单设此闸")
    ap.add_argument("--max-refusal-rate", type=float, default=0.02,
                    help="安全网阈值：拒答率上限（真机基线为 0，仅防回归）")
    ap.add_argument("--max-quality-drop", type=float, default=0.05)
    ap.add_argument("--temperature", type=float, default=None, help="A/B 覆写（同 baseline 脚本）")
    ap.add_argument("--max-tokens", type=int, default=None, help="A/B 覆写（同 baseline 脚本）")
    ap.add_argument("--min-attempts", type=int, default=18,
                    help="**全部比例类判定**的最小样本量（正式协议 `--repeat 3` = 21 次尝试）；"
                         "不足时该判定降级为「观测」，不计失败")
    ap.add_argument("--min-long-attempts", type=int, default=3,
                    help="long 单场景闸的最小样本量")
    ap.add_argument("--abs-tol", type=float, default=0.05,
                    help="「不劣化」比率判定的绝对容差（百分点）：新值 ≤ 旧值 + 该值")
    ap.add_argument("--min-quality-runs", type=int, default=5,
                    help="质量口径「不劣化」判定的最小样本量")
    ap.add_argument("--with-quality", action="store_true",
                    help="额外跑质量口径（编辑评分 + 约束术语命中率）；"
                         "需要 --baseline 指向带 quality_aggregate 的报告才能判「不劣化」")
    ap.add_argument("--from-report", default="",
                    help="不重新调用模型：直接对一份已保存的 flash 报告重新判定"
                         "（阈值/口径改了以后想重判旧数据时用）")
    ap.add_argument("--expect-model", default="")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    scenarios = SCENARIOS
    if args.only:
        want = {s.strip() for s in args.only.split(",") if s.strip()}
        scenarios = [s for s in SCENARIOS if s["id"] in want]
        if not scenarios:
            print(f"未匹配到场景：{args.only}（可选：{[s['id'] for s in SCENARIOS]}）")
            return 2

    sandbox = Path(tempfile.mkdtemp(prefix="inkforge-refusal-flash-"))
    env = make_env(sandbox)
    params = load_writer_params()
    report: dict = {
        "protocol": COLLECTOR_VERSION,
        "script": "smoke_refusal_flash.py",
        "git": git_state(),
        "thresholds": {"max_empty_rate": args.max_empty_rate,
                       "max_truncation_rate": args.max_truncation_rate,
                       "max_long_truncation_rate": args.max_long_truncation_rate,
                       "max_refusal_rate": args.max_refusal_rate,
                       "max_quality_drop": args.max_quality_drop,
                       "min_attempts": args.min_attempts,
                       "min_long_attempts": args.min_long_attempts,
                       "abs_tol": args.abs_tol,
                       "min_quality_runs": args.min_quality_runs},
        "params": {**params, "repeat": args.repeat,
                   "temperature_override": args.temperature,
                   "max_tokens_override": args.max_tokens,
                   "scenarios": [s["id"] for s in scenarios]},
    }
    checks: list[tuple[str, bool, str]] = []
    observations: list[tuple[str, str]] = []

    def chk(name: str, ok: bool, ev: object = "", sufficient: bool = True,
            reason: str = "") -> None:
        """判定一条；`sufficient=False` 时降级为观测（样本不足，不计失败）。"""
        if not sufficient:
            observations.append((name, str(ev)[:260]))
            print(f"  [观测·样本不足] {name}  -- {str(ev)[:260]}{('｜' + reason) if reason else ''}")
            return
        checks.append((name, bool(ok), str(ev)[:260]))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {str(ev)[:260]}" if ev else ""))

    try:
        print("=" * 78)
        print(f"拒答率达标判定 | 场景 {len(scenarios)} × repeat {args.repeat} | "
              f"最多 {len(scenarios) * args.repeat * 4} 次模型调用")
        print(f"绑定：provider={params['provider']} model={params['model']} "
              f"temperature={params['temperature']} max_tokens={params['max_tokens']}")
        print(f"工作区：rev={report['git']['rev'][:12]} 脏文件={report['git']['dirty_files']}")
        print("=" * 78)

        print("[预检] 1 次最小调用…")
        if args.from_report:
            data = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
            agg = data.get("aggregate") or {}
            records = data.get("records") or []
            runs = data.get("runs") or []
            q_agg_preloaded = data.get("quality_aggregate")
            report.update({
                "rejudged_from": str(args.from_report),
                "protocol_source": data.get("protocol"),
                "git": data.get("git", report["git"]),
                "params": data.get("params", report["params"]),
                "aggregate": agg, "records": records, "runs": runs,
                "precheck": data.get("precheck", {}),
            })
            print(f"  [rejudge] 不重新调用模型：复用 {Path(args.from_report).name}"
                  f"（协议 {data.get('protocol')}，尝试 {agg.get('attempts_total')} 条）")
        else:
            try:
                pre = precheck(env, timeout=min(args.timeout, 120.0))
            except Exception as exc:  # noqa: BLE001
                print(f"  FATAL 预检失败：{type(exc).__name__}: {exc}")
                report["precheck"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                save_report("refusal-flash", report)
                return 1
            report["precheck"] = {"ok": True, **pre}
            print(f"  实际：{pre['provider_type']} {pre['provider_base_url']} → "
                  f"model={pre['model_returned']} usage={pre['usage']} {pre['elapsed_s']}s")
            if args.expect_model:
                chk(f"预检：返回模型含 {args.expect_model!r}",
                    args.expect_model in str(pre.get("model_returned", "")),
                    pre.get("model_returned", ""))

            print("[采集] 逐场景真实调用…")
            records, runs = collect(env, scenarios, args.repeat, timeout=args.timeout,
                                temperature=args.temperature, max_tokens=args.max_tokens)
        agg = aggregate(records, runs, args.max_tokens or params["max_tokens"])
        report["aggregate"] = agg
        report["records"] = records
        report["runs"] = runs
        print_summary(agg)

        print()
        print("── 判定（全部可失败；样本不足的比例判定降级为观测）──")
        n = agg.get("attempts_total") or 0
        enough = n >= args.min_attempts
        sample_note = f"样本 {n} 次尝试 < 门槛 {args.min_attempts}；正式协议 --repeat 3 = 21 次"
        if not enough:
            print(f"  ⚠ 本轮为快跑（{sample_note}）")
        empty_rate, trunc_rate = agg["empty_output_rate"], agg["truncation_rate"]
        chk(f"1【主指标】空产出率 ≤ {args.max_empty_rate:.1%}",
            empty_rate is not None and empty_rate <= args.max_empty_rate,
            f"实测 {'—' if empty_rate is None else f'{empty_rate:.1%}'} "
            f"({agg['empty_outputs']}/{agg['attempts_total']} 次尝试)",
            sufficient=enough, reason=sample_note)
        chk(f"2【主指标】截断率 ≤ {args.max_truncation_rate:.1%}",
            trunc_rate is not None and trunc_rate <= args.max_truncation_rate,
            f"实测 {'—' if trunc_rate is None else f'{trunc_rate:.1%}'} "
            f"({agg['truncated_outputs']} 条，其中带正文 {agg['truncated_nonempty']} 条；"
            f"收尾原因 {agg['finish_reasons']})",
            sufficient=enough, reason=sample_note)

        slot = (agg.get("per_scenario") or {}).get("long") or {}
        long_att, long_trunc = slot.get("attempts") or 0, slot.get("truncated") or 0
        long_rate = (long_trunc / long_att) if long_att else None
        chk(f"2b【主指标·5000 字场景】long 单场景截断率 ≤ {args.max_long_truncation_rate:.0%}",
            long_rate is not None and long_rate <= args.max_long_truncation_rate,
            f"实测 {'—' if long_rate is None else f'{long_rate:.0%}'}（{long_trunc}/{long_att} 次尝试）"
            "｜聚合闸看不见 long 的退化（它只占 1/7），故单设此闸",
            sufficient=long_att >= args.min_long_attempts,
            reason=f"long 样本 {long_att} < 门槛 {args.min_long_attempts}")

        chk("3 调用异常 = 0", agg["call_errors"] == 0, f"异常 {agg['call_errors']} 条")

        covered = [r for r in runs
                   if r.get("has_constitution") is not True or r.get("has_fiction_anchor") is not True]
        chk("4 prompt 端到端含第零条探针句 + 虚构框架声明（防拒绝块）",
            not covered, f"未覆盖的运行 {len(covered)}/{len(runs)}")

        rate = agg["refusal_rate"]
        chk(f"5【安全网】拒答率 ≤ {args.max_refusal_rate:.1%}（基线为 0，不可用于证明改进）",
            rate is not None and rate <= args.max_refusal_rate,
            f"实测 {'—' if rate is None else f'{rate:.2%}'} "
            f"({agg['refusal_attempts']}/{agg['attempts_total']} 次尝试)",
            sufficient=enough, reason=sample_note)

        if args.baseline:
            base = _baseline_rates(Path(args.baseline))
            report["baseline"] = base
            print(f"  基报告：{base['path']}（protocol={base['protocol']} "
                  f"attempts={base['attempts_total']} git={str(base['git'].get('rev', ''))[:12]}）")
            drop = args.max_quality_drop
            n_new, n_old = agg.get("attempts_total") or 0, base.get("attempts_total") or 0
            enough_pair = min(n_new, n_old) >= args.min_attempts

            def _not_worse(name: str, key: str, higher_is_better: bool) -> None:
                old, new = base.get(key), agg.get(key)
                if old is None or new is None:
                    chk(f"6 不劣化·{name}", False, f"基线={old} 改动后={new}（数据缺失）")
                    return
                # 比率类指标：相对容差在"基线=0"时退化成绝对零容差（任何一次偶发都判 FAIL），
                # 因此叠加绝对容差，并要求最小样本量 —— 样本不足时只作观测、不计失败
                # （2026-09-22 实测：基线 0% 空产出 vs 一轮 12.5%（1/8），属于样本噪声而非劣化）。
                bound = (old * (1 - drop) if higher_is_better
                         else old * (1 + drop) + args.abs_tol)
                ok = new >= bound if higher_is_better else new <= bound
                detail = (f"基线={old:.3f} → 改动后={new:.3f}（阈值={bound:.3f}）")
                chk(f"6 不劣化·{name}（容差 {drop:.0%} + 绝对 {args.abs_tol:.0%}）", ok, detail,
                    sufficient=enough_pair, reason=f"样本 {n_old} vs {n_new}，门槛 {args.min_attempts}")

            _not_worse("空产出率", "empty_output_rate", higher_is_better=False)
            _not_worse("截断率", "truncation_rate", higher_is_better=False)
            _not_worse("字数落区间率", "length_in_band_rate", higher_is_better=True)
        else:
            print("  [skip] 未给 --baseline：跳过「不劣化」判定（主指标绝对值仍判）")

        if args.with_quality and not args.from_report:
            print()
            print("[质量] 注入作者约束 → 产出 → 真实 Editor 审查…")
            q_attempts, q_runs = collect(env, scenarios, args.repeat, timeout=args.timeout,
                                         temperature=args.temperature,
                                         max_tokens=args.max_tokens, child=_CHILD_QUALITY)
            report["quality_aggregate"] = aggregate_quality(q_runs)
            report["quality_runs"] = q_runs
            report["quality_attempts"] = q_attempts

        if args.from_report and q_agg_preloaded:
            # 重判旧报告：质量口径沿用原报告的评分结果（不重新调用模型）
            report["quality_aggregate"] = q_agg_preloaded

        q = report.get("quality_aggregate")
        if q:
            print_quality(q)
            q0 = (report.get("baseline") or {}).get("quality") or {}
            nq_old = q0.get("runs_scored") or 0
            nq_new = q.get("runs_scored") or 0
            for label, key in (("编辑评分 overall", "overall_avg"),
                               ("约束术语命中率", "term_hit_rate_avg")):
                old, new = q0.get(key), q.get(key)
                if old is None or new is None:
                    chk(f"7 质量不劣化·{label}", False,
                        f"基线={old} 改动后={new}（基线缺 quality_aggregate 或本次未评分）")
                    continue
                bound = old * (1 - args.max_quality_drop)
                detail = (f"基线={old:.3f} → 改动后={new:.3f}（下限={bound:.3f}）")
                chk(f"7 质量不劣化·{label}（容差 {args.max_quality_drop:.0%}）", new >= bound, detail,
                    sufficient=min(nq_old, nq_new) >= args.min_quality_runs,
                    reason=f"评分样本 {nq_old} vs {nq_new}，门槛 {args.min_quality_runs}")

        fails = [c for c in checks if not c[1]]
        print()
        print("=" * 78)
        print(f"总计：{len(checks) - len(fails)} 通过 / {len(fails)} 失败 → "
              f"{'PASS' if not fails else 'FAIL'}"
              + (f"｜另有 {len(observations)} 条观测（样本不足，未计入判定）" if observations else ""))
        if observations:
            print(f"⚠ 有 {len(observations)} 条判定因样本不足未计入（见上方 [观测·样本不足] 行）；"
                  "单臂比例判定需 ≥ 门槛次尝试，配对「不劣化」还需**基线侧同样达标**。")
        print("=" * 78)
        report["checks"] = [{"case": n, "ok": o, "evidence": e} for n, o, e in checks]
        report["observations"] = [{"case": n, "evidence": e} for n, e in observations]
        report["sample_sufficient"] = not observations
        out = save_report("refusal-flash", report)
        print(f"结构化结果：{out}")
        return 1 if fails else 0
    finally:
        if not args.keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            print(f"沙箱已保留：{sandbox}")


if __name__ == "__main__":
    raise SystemExit(main())
