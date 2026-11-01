#!/usr/bin/env python
"""真机日志口径统计（内容无关）：为「实机模型轮换对照」提供可复算的分母与信号计数。

为什么存在（2026-09-22 交接遗留 §5.3）：
  "拒答暴增"只能在**真机**上判定 —— 7 个易诱发拒答的合成场景在 flash 上跑了 50+ 次，
  实测拒答 0 次（见 `docs/13-…实施与验证报告.md`）。所以拒答维度只能靠作者在界面上按臂
  轮换，再由日志侧统计。本脚本把"日志侧统计"固化成一条命令：
  **只读日志，不读任何作品数据（data/novels/**），也不打印日志原文。**

口径（与 docs/13 §4 一致，全部是"计数信号"，非正文）
  · 拒答命中   = 「保真链判定产出含拒绝语义」行数（每次被判拒答的尝试产生 1 条）
  · 拒答事件   = max(拒答命中行, 拒答致重试行) —— **跨时段比较用这个**
    （一次拒答会同时产生"判定行"与"重试行"两条，裸 grep 会把同一事件算两次）
  · 尝试数粗口径 = 「Writer: 第 N 章…生成中」次数 + 「保真链第 N 次产出不合格」次数
  · 尝试数精口径 = 「[调用] role=…」行数（2026-09-22 新增的诊断行，**重启应用后才有**）
  · 结构化失败 = 「结构化输出解析失败」行数 ÷ 调用数
  · 致命       = 「BriefFidelityError」行数

时间戳：日志**文件名只到天、内容却会跨午夜续写**（实测 `desktop-20260921.log` 从 09-21 08:39
  写到 09-22 06:00）。本脚本按「时刻回退即换日」推定日期，输出完整 `YYYY-MM-DD HH:MM:SS`，
  否则 `--since` 与按小时分段会把两天搅在一起。

为什么要剔除 traceback 回显：
  electron 会把引擎的 rich traceback 整块写进日志，里面**含源码行**（如
  `logger.warning("brief 保真链第 %d 次产出不合格…")`）。不过滤会把计数虚高
  （实测本仓库日志：`BriefFidelityError` 19 → 真实 12，`保真链第` 123 → 真实 120）。
  过滤规则：去掉 `[engine HH:MM:SS] ` 前缀后以 `│` 开头，或匹配 `logger.xxx(` 的行。

分段：默认按**模型绑定变更**自动切段（界面改绑定会留下「角色绑定更新 → a/b」与
「探活: 角色 → a / b」），于是 A→B→C 轮换实验的数据天然按臂分好，不需要人工记时间戳。

用法：
    python engine/smoke_log_stats.py                         # 最新日志，按绑定分段
    python engine/smoke_log_stats.py --by hour               # 按小时（+小时内换绑）分段
    python engine/smoke_log_stats.py --since '2026-09-22 09:30:00'
    python engine/smoke_log_stats.py --log <路径> --by none --json out.json
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
LOG_DIR = ENGINE_DIR / "data" / "runtime" / "logs"

STATS_VERSION = "log-stats/v1"

# ---------- 行级解析 ----------

#: 两种前缀都要吃：
#:   electron 转写  → `[engine HH:MM:SS] …`（`[` 属于 `[engine ` 这一段）
#:   引擎原生 stdout → `[HH:MM:SS] …`（`[` 自己一段）
#: 所以是 `(?:\[engine )?` + **可选** `\[`。漏掉可选的 `\[` 会让原生格式静默失配
#: （表现为"整个日志零事件"）；把它写成必需又会反过来干掉 electron 格式。
_LINE_RE = re.compile(r"^(?:\[engine )?\[?(\d{2}:\d{2}:\d{2})\]\s?(.*)$")
_DAY_RE = re.compile(r"desktop-(\d{8})\.log$")
_SRC_ECHO_RE = re.compile(r"logger\.\w+\(")

#: ⚠ 分隔符一律用 `\s+` 而不是单个空格：rich 折行时**每段右侧会补空格到列宽**，
#: 拼接后 `deepseek-flash` 与 `prompt_chars=` 之间会出现一长串空格（实测 18 个），
#: 写死单个空格则永远匹配不上（这就是"折行后精口径分母归零"的真因）。
_CALL_RE = re.compile(
    r"\[调用\]\s+role=(\S+)\s+(\S+)/(\S+?)(?:（降级）)?\s+prompt_chars=(\d+)\s+"
    r"max_tokens=(\S+)\s+→\s+(\d+)\s*字\s*/\s*(\S+)\s+tokens\s*/\s*finish=(\S+)\s*/"
    r"\s*([\d.]+)s"
)
_CALL_FAIL_RE = re.compile(
    r"\[调用\]\s+role=(\S+)\s+(\S+)/(\S+)\s+prompt_chars=(\d+)\s+→\s+失败："
)
_BIND_RE = re.compile(r"角色绑定更新 → (\S+)/(\S+)")
_PROBE_RE = re.compile(r"探活: 角色 → (\S+) / (\S+)")
_CHAIN_RETRY_RE = re.compile(r"保真链第 \d+ 次产出不合格")

# 信号 → 匹配规则（substring 或 regex）。顺序即上报顺序。
SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("拒答命中", re.compile(r"保真链判定产出含拒绝语义")),
    # ⚠ 收尾括号不能写死：2026-09-22 03:44 之后 last_error 变成
    # `产出含拒绝语义（命中 短产出:抱歉｜13 字）`，于是同一行渲染成
    # `…不合格（产出含拒绝语义（命中 …）），携原因重试` —— 写死 `）` 会漏掉新版全部 30 条。
    ("拒答致重试", re.compile(r"次产出不合格（产出含拒绝语义")),
    ("空产出", re.compile(r"产出为空")),
    ("保真链重试", _CHAIN_RETRY_RE),
    ("保真链恢复", re.compile(r"次尝试取得合格产出")),
    ("提前收敛", re.compile(r"提前收敛于第")),
    ("写作启动", re.compile(r"Writer: 第 \d+ 章.*生成中")),
    ("写作完成", re.compile(r"Writer: 第 \d+ 章.*完成（")),
    ("结构化解析失败", re.compile(r"结构化输出解析失败")),
    # 结构化链路里的**真拒答**（2026-09-22 补）：解析失败时附带的拒答信号，
    # 只有命中通用措辞才算 —— `未命中`/`空产出` 是格式问题，不是拒答。
    ("结构化拒答", re.compile(r"拒答信号=(?:短产出|尾部\d+字|meta):")),
    ("结构化耗尽", re.compile(r"结构化输出连续 \d+ 次解析失败")),
    ("字数门禁触发", re.compile(r"字数门禁触发")),
    ("致命", re.compile(r"BriefFidelityError")),
)

# 计算比率时用到的信号名
_DENOM_COARSE = ("写作启动", "保真链重试")

CAVEATS = (
    "「[调用]」行是 2026-09-22 新增的诊断行，重启应用后才有 —— 老时段调用数为 0 属正常，"
    "此时比率只能用粗口径（写作启动 + 保真链重试 作分母）。",
    "「拒答命中」（`保真链判定产出含拒绝语义`）**本身也是 2026-09-22 新增的行**："
    "更早的时段只能看到「拒答致重试」——故跨时段比较请用「拒答事件(估计)」。",
    "一次拒答会产生**两条**日志行（判定行 + 重试行），裸 grep `产出含拒绝语义` 会把同一事件"
    "算两次。实测复核：交接文档 §2.3 里 09-22 04h–05h 的「64 次拒绝命中」= 判定行 33 + 重试行 31，"
    "去重后是 **33 次事件**（本脚本的 refusal_events），比率也随之从 0.627 降到 0.452。"
    "→ 引用历史数字时务必注明是「行数」还是「事件数」。",
    "粗口径分母**偏大**：保真链里每一次不合格尝试都会被计入 → 请只用它做**臂间相对比较**，"
    "不要当绝对拒答率。",
    "本脚本只统计日志信号，不判断内容质量；编辑评分/字数需用 smoke_refusal_*.py 的真机口径。",
    "日志为 electron 转写，长消息会换行折断 —— 信号串若恰好跨行会漏计（实测存在但量级很小）。",
    "日志**文件名只到天、内容却会跨午夜续写**（实测 `desktop-20260921.log` 写到次日 06:00）。"
    "本脚本按「时刻回退即换日」推定日期；`--since` 只给 `HH:MM:SS` 时按该文件最后一天解释 —— "
    "跨午夜日志请给完整 `YYYY-MM-DD HH:MM:SS`。",
)


@dataclass
class CallRec:
    ts: str
    role: str
    provider: str
    model: str
    prompt_chars: int
    max_tokens: str
    out_chars: int
    completion_tokens: str
    finish: str
    elapsed: float


@dataclass
class Segment:
    """一个统计段：同一模型绑定（或同一小时）内的全部事件。"""

    day: str
    binding: str
    start: str
    end: str
    signals: Counter = field(default_factory=Counter)
    calls: list[CallRec] = field(default_factory=list)
    call_failures: int = 0
    bind_changes: int = 0

    @property
    def attempts_coarse(self) -> int:
        return sum(self.signals[k] for k in _DENOM_COARSE)

    @property
    def refusal_events(self) -> int:
        """拒答事件（估计）：一次被判拒答最多产生 1 条「拒答命中」+ 1 条「拒答致重试」，
        故取两者较大值即可跨时段比较（早于 2026-09-22 的时段只有后者）。"""
        return max(self.signals["拒答命中"], self.signals["拒答致重试"])

    def ratio(self, name: str) -> float | None:
        if name == "结构化解析失败":
            denom = len(self.calls) or None
        else:
            denom = self.attempts_coarse or None
        if not denom:
            return None
        return round(self.signals[name] / denom, 4)

    def absorb(self, ts: str, kind: str, payload: str) -> None:
        """把一个事件并入本段（收尾时间跟着走）。"""
        self.end = ts
        if kind == "signal":
            self.signals[payload] += 1
        elif kind == "call":
            self.calls.append(CallRec(ts=ts, **json.loads(payload)))
        elif kind == "call_fail":
            self.call_failures += 1

    def to_dict(self) -> dict:
        lat = [c.elapsed for c in self.calls]
        by_role: dict[str, list[float]] = {}
        finish = Counter()
        for c in self.calls:
            by_role.setdefault(c.role, []).append(c.elapsed)
            finish[c.finish] += 1
        return {
            "day": self.day,
            "binding": self.binding,
            "start": self.start,
            "end": self.end,
            "signals": dict(self.signals),
            "attempts_coarse": self.attempts_coarse,
            "refusal_events": self.refusal_events,
            "calls": len(self.calls),
            "call_failures": self.call_failures,
            "bind_changes": self.bind_changes,
            "call_latency_all": _lat(lat),
            "call_latency_by_role": {k: _lat(v) for k, v in sorted(by_role.items())},
            "finish_reason": dict(finish),
            "rates": {name: self.ratio(name) for name, _ in SIGNALS},
        }


def _lat(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
    return {
        "n": len(ordered),
        "p50": round(statistics.median(ordered), 1),
        "p90": round(ordered[idx], 1),
        "max": round(ordered[-1], 1),
    }


def _is_source_echo(content: str) -> bool:
    """rich traceback 的源码回显 / 控制台源码行 —— 统计时必须剔除。"""
    return content.startswith("│") or bool(_SRC_ECHO_RE.search(content))


def _split_prefix(line: str) -> tuple[str | None, str]:
    """拆出 `(时间戳, 正文)`；支持两种前缀：electron 的 `[engine HH:MM:SS] ` 与引擎原生的 `[HH:MM:SS] `。"""
    m = _LINE_RE.match(line)
    if m:
        return m.group(1), m.group(2)
    return None, line


def iter_units(lines: Sequence[str]) -> Iterator[tuple[str | None, str]]:
    """产出 `(时间戳, 正文)`，并对**被 rich 折行的 `[调用]` 记录做拼接**。

    为什么需要（2026-09-22 实测量到）：`RichHandler` 会按控制台宽度折行——管道输出时默认
    80 列，于是约 130 字的 `[调用] role=… → N 字 / M tokens / finish=X / T s` 被拆成 3 段
    （实测：`[07:38:54] INFO  [调用] role=writer deepseek/deepseek-flash` /
    `prompt_chars=… → 3281 字 / 5123` / `tokens / finish=stop / 62.3s`）→ 正则匹配不到，
    **精口径分母静默丢失**。

    拼接规则刻意保守：**匹配成功即停**（上限 4 行），因此永远不会把下一条真正的新记录吸进来；
    其余记录一律不拼接，避免把 supervisor 自有行（`角色绑定更新 → a/b`）误并进上一条
    （那会让"绑定分段"错位）。
    """
    i, n = 0, len(lines)
    while i < n:
        ts, content = _split_prefix(lines[i].rstrip("\r\n"))
        if "[调用]" in content and not (_CALL_RE.search(content) or _CALL_FAIL_RE.search(content)):
            buf, j, matched = content.rstrip(), i, None
            while j + 1 < n and j - i < 3:
                j += 1
                _, nxt = _split_prefix(lines[j].rstrip("\r\n"))
                buf = f"{buf} {nxt.strip()}"
                if _CALL_RE.search(buf):
                    matched = j
                    break
            if matched is not None:
                yield ts, buf
                i = matched + 1
                continue
        yield ts, content
        i += 1


def classify_content(content: str) -> list[tuple[str, str]]:
    """把**一行日志的正文**（已去掉时间戳前缀）分类成 [(类型, 载荷)]。

    类型 ∈ {signal, call, call_fail, bind}。同一行可返回多条（信号可叠加）。
    抽成独立函数是为了让**别的运行器**（如 `smoke_arm_rotation.py` 捕获的引擎原生 stdout）
    复用同一套口径 —— 两处计数必须一致，否则对照实验的分母没有意义。
    """
    if _is_source_echo(content):
        return []
    out: list[tuple[str, str]] = []
    cm = _CALL_RE.search(content)
    if cm:
        return [("call", json.dumps({
            "role": cm.group(1), "provider": cm.group(2), "model": cm.group(3),
            "prompt_chars": int(cm.group(4)), "max_tokens": cm.group(5),
            "out_chars": int(cm.group(6)), "completion_tokens": cm.group(7),
            "finish": cm.group(8), "elapsed": float(cm.group(9)),
        }, ensure_ascii=False))]
    if _CALL_FAIL_RE.search(content):
        return [("call_fail", content[:200])]
    bm = _BIND_RE.search(content) or _PROBE_RE.search(content)
    if bm:
        return [("bind", f"{bm.group(1)}/{bm.group(2)}")]
    for name, pat in SIGNALS:
        # 同一行可以同时命中多个信号（例如「保真链第 1 次产出不合格（产出含拒绝语义）」
        # 同时是「保真链重试」与「拒答致重试」）——每个信号各自计数，不 break。
        if pat.search(content):
            out.append(("signal", name))
    return out


def count_signals(lines: list[str]) -> Counter:
    """对**任意来源的行**做同一套信号计数（引擎原生 stdout / electron 转写 / 日志文件）。

    返回 Counter{信号名: 次数}。只做计数，不返回也不打印任何正文。
    """
    counter: Counter = Counter()
    for _ts, content in iter_units(lines):
        for kind, payload in classify_content(content):
            if kind == "signal":
                counter[payload] += 1
    return counter


def collect_calls(lines: list[str]) -> list[dict]:
    """从任意来源的行里提取 `[调用]` 诊断行（精口径：每调用一条）。

    返回的行只含 角色 / 接入点 / 模型 / prompt 长度 / 预算 / 产出长度 / tokens / 收尾原因 / 耗时
    —— **不含任何产出正文**。
    """
    calls: list[dict] = []
    for _ts, content in iter_units(lines):
        for kind, payload in classify_content(content):
            if kind == "call":
                calls.append(json.loads(payload))
    return calls


def parse_log(path: Path) -> list[tuple[str, str, str]]:
    """返回 [(时间戳, 类型, 载荷)]，时间戳为 `YYYY-MM-DD HH:MM:SS`。

    为什么必须带日期：日志**文件名只到天**，但一个文件会跨午夜续写
    （实测 `desktop-20260921.log` 从 09-21 08:39 一直写到 09-22 06:00）。
    只保留 `HH:MM:SS` 的话，`--since` 过滤与按小时分段都会把两天搅在一起。
    这里用「时刻回退即换日」推定日期（同一文件内时刻单调递增，回退点就是午夜）。
    """
    day = ""
    m = _DAY_RE.search(path.name)
    if m:
        day = m.group(1)
    date = datetime.strptime(day, "%Y%m%d").date() if day else None
    prev = ""
    events: list[tuple[str, str, str]] = []
    # utf-8-sig：日志若带 BOM（Windows 侧写出时常见），用 utf-8 读会让**首行**匹配不上而静默丢失。
    with path.open(encoding="utf-8-sig", errors="replace") as fh:
        for ts, content in iter_units(fh.read().splitlines()):
            if ts is None:
                continue
            if date is None:
                stamp = ts
            else:
                if prev and ts < prev:
                    date += timedelta(days=1)
                prev = ts
                stamp = f"{date.isoformat()} {ts}"
            events.extend((stamp, kind, payload) for kind, payload in classify_content(content))
    return events


def segment_by_binding(events: list[tuple[str, str, str]], day: str) -> list[Segment]:
    segs: list[Segment] = []
    cur: Segment | None = None
    for ts, kind, payload in events:
        if kind == "bind":
            if cur is None or cur.binding != payload:
                cur = Segment(day=day, binding=payload, start=ts, end=ts)
                segs.append(cur)
            else:
                cur.bind_changes += 1
                cur.end = ts
            continue
        if cur is None:
            cur = Segment(day=day, binding="（未探活 / 绑定未知）", start=ts, end=ts)
            segs.append(cur)
        cur.absorb(ts, kind, payload)
    return [s for s in segs if s.attempts_coarse or s.calls or s.signals]


def segment_by_hour(events: list[tuple[str, str, str]], day: str) -> list[Segment]:
    """按小时切段；**小时内换绑也切段** —— 否则同一段会混进两个臂，比率不可比。"""
    segs: list[Segment] = []
    cur: Segment | None = None
    binding = "（未探活）"
    for ts, kind, payload in events:
        if kind == "bind":
            binding = payload
            cur = None
            continue
        hour = ts[11:13]
        if cur is None or cur.start[11:13] != hour:
            cur = Segment(day=day, binding=f"{binding} @ {ts[:10]} {hour}h", start=ts, end=ts)
            segs.append(cur)
        cur.absorb(ts, kind, payload)
    return [s for s in segs if s.attempts_coarse or s.calls or s.signals]


def segment_none(events: list[tuple[str, str, str]], day: str) -> list[Segment]:
    """整份日志只出一段（绑定取最后一个已知值）。"""
    keep = [e for e in events if e[1] != "bind"]
    if not keep:
        return []
    binding = "（全部）"
    for _, kind, payload in reversed(events):
        if kind == "bind":
            binding = payload
            break
    seg = Segment(day=day, binding=binding, start=keep[0][0], end=keep[-1][0])
    for ts, kind, payload in keep:
        seg.absorb(ts, kind, payload)
    return [seg]


def print_report(segments: list[Segment], title: str) -> None:
    line = "=" * 100
    print(line)
    print(f"真机日志口径统计 {STATS_VERSION} | {title}")
    print(line)
    if not segments:
        print("该范围内没有可统计的事件（信号/调用/绑定全为空）。")
        return
    for i, seg in enumerate(segments, 1):
        print()
        print(f"--- 段 {i} · {seg.start} ~ {seg.end} · 绑定 {seg.binding} "
              f"（绑定变更 {seg.bind_changes} 次）")
        if seg.signals:
            body = "  ".join(f"{k}={v}" for k, v in seg.signals.items())
            print(f"    信号：{body}")
        else:
            print("    信号：（无）")
        print(f"    粗口径尝试数（写作启动+保真链重试）= {seg.attempts_coarse}")
        if seg.calls:
            print(f"    精口径调用数（[调用] 行）= {len(seg.calls)}"
                  f"（失败 {seg.call_failures}）  角色分布="
                  f"{dict(Counter(c.role for c in seg.calls))}")
            print(f"    收尾原因="
                  f"{dict(Counter(c.finish for c in seg.calls))}")
            lat = _lat([c.elapsed for c in seg.calls])
            print(f"    耗时 p50={lat['p50']}s p90={lat['p90']}s max={lat['max']}s")
            for role, vals in sorted(
                    {c.role: [x.elapsed for x in seg.calls if x.role == c.role]
                     for c in seg.calls}.items()):
                r = _lat(vals)
                print(f"      · {role:<10} n={r['n']:<4} p50={r['p50']}s max={r['max']}s")
        else:
            print("    精口径调用数（[调用] 行）= 0 ——该诊断行 2026-09-22 新增，重启后才有")
        ref = seg.ratio("拒答命中")
        if seg.attempts_coarse:
            print(f"    拒答：判定行={seg.signals['拒答命中']}  重试行={seg.signals['拒答致重试']}"
                  f"  → 事件(估计)={seg.refusal_events}")
            print(f"    拒答事件(估计)/粗口径尝试 = {seg.refusal_events}/{seg.attempts_coarse} "
                  f"= {seg.refusal_events / seg.attempts_coarse:.3f}")
        if ref is not None:
            print(f"    拒答命中(判定行)/粗口径尝试 = {seg.signals['拒答命中']}/{seg.attempts_coarse} "
                  f"= {ref:.3f}")
        sf = seg.ratio("结构化解析失败")
        if sf is not None:
            print(f"    结构化解析失败/调用 = {seg.signals['结构化解析失败']}/{len(seg.calls)} "
                  f"= {sf:.3f}")
        elif seg.signals["结构化解析失败"]:
            print(f"    结构化解析失败 = {seg.signals['结构化解析失败']}（无调用分母，只报绝对值）")
    print()
    print(line)
    print("【口径与已知偏差】")
    for c in CAVEATS:
        print(f"  · {c}")
    print(line)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="真机日志口径统计（只读日志，不碰作品数据）")
    ap.add_argument("--log", action="append", default=None,
                    help="日志路径，可多次；缺省取 LOG_DIR 下最新一份")
    ap.add_argument("--by", choices=("binding", "hour", "none"), default="binding",
                    help="分段方式：binding=按模型绑定变更（默认，配合轮换实验）")
    ap.add_argument("--since", default=None,
                    help="只统计该时刻之后的记录，格式 `YYYY-MM-DD HH:MM:SS`（也可只给 `HH:MM:SS`，"
                         "此时按**最后一天**解释；跨午夜的日志建议给完整日期）")
    ap.add_argument("--json", dest="json_out", default=None, help="结果落盘路径")
    args = ap.parse_args()

    if args.log:
        paths = [Path(p) for p in args.log]
    else:
        cands = sorted(LOG_DIR.glob("desktop-*.log"), key=lambda p: p.stat().st_mtime)
        paths = cands[-1:]
        if not paths:
            print(f"未找到日志：{LOG_DIR}")
            return 1
    missing = [p for p in paths if not p.exists()]
    if missing:
        print("日志不存在：" + "，".join(str(p) for p in missing))
        return 1

    segments: list[Segment] = []
    since = args.since
    for path in paths:
        events = parse_log(path)
        if since:
            # 只给 `HH:MM:SS` 时按该文件的**最后一天**解释（跨午夜日志必须给完整日期，否则会
            # 把早于该时刻的记录也留下——这正是本选项之前的缺陷）。
            bound = since if len(since) > len("HH:MM:SS") else (
                f"{events[-1][0][:10]} {since}" if events else since
            )
            events = [e for e in events if e[0] >= bound]
        day = ""
        m = _DAY_RE.search(path.name)
        if m:
            day = m.group(1)
        if args.by == "binding":
            segments.extend(segment_by_binding(events, day))
        elif args.by == "hour":
            segments.extend(segment_by_hour(events, day))
        else:
            segments.extend(segment_none(events, day))

    title = " + ".join(p.name for p in paths)
    print_report(segments, title)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "version": STATS_VERSION,
            "stamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "logs": [str(p) for p in paths],
            "by": args.by,
            "since": args.since,
            "segments": [s.to_dict() for s in segments],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
