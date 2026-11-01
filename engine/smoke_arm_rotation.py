#!/usr/bin/env python
"""实机轮换对照：在**真实作品上下文**上跑三个授权模型臂，判定「拒答维度」。

为什么非做不可、又为什么不能靠合成场景
  7 个"易诱发拒答"的自造场景在 flash 上跑了 50+ 次，实测**拒答 0 次**（docs/13 §3.1）——
  拒答只发生在作者的具体作品上下文里，合成场景复现不了。所以只能在真实上下文上对照。

怎么在**不违反硬约束**的前提下做
  · 硬约束 1「禁止读取任何小说正文」→
      本脚本对作品目录只做**按字节复制**（`shutil.copytree`）；正文从不进入任何分析路径。
      所有对外输出都经 `_safe_view()` 白名单过滤，只保留 状态 / 计数 / 长度 / 耗时 / 异常**类型名**；
      **原始 HTTP 响应体与引擎原始日志行一律不打印、不落盘**。
      唯一例外是错误文本，本脚本用 `_error_kind()` 只抽出异常类名，绝不回显 `str(exc)`
      （pydantic 的 ValidationError 会把模型原始 JSON 带进消息里）。
  · 硬约束 3「不改作品数据」→ 全程在 `smoke_test.build_sandbox` 临时沙箱里跑；
      真实 `data/novels/**` 只读不写（跑完删沙箱，`--keep-sandbox` 可保留排查）。
  · 硬约束 2「只测三个模型对象」→ 臂定义**直接复用** `smoke_model_bakeoff.ARMS`，不新增对象。

流程（每臂 × 每轮）
  1. 沙箱里把全部 roles 绑到该臂（只换 provider/model，max_tokens / temperature 不动）
  2. 用 `EngineProcess` 起**真实 uvicorn 引擎**，走生产 HTTP 端点的真实代码路径
  3. 动作 1：`POST /api/interactive/redraw`（= 界面上的「重出卡」）
     动作 2：`POST /api/interactive/choose` c1（= 界面上的「选卡写章」）
  4. 轮询 `/api/interactive/state` 直到收敛；只记 状态 / 耗时 / 异常类型名
  5. 停引擎，把它的 stdout 用 **`smoke_log_stats` 的同一套信号规则**计数（口径一致才可比）

用法：
    python engine/smoke_arm_rotation.py --dry-run                 # 零 LLM：只验沙箱+引擎+端点
    python engine/smoke_arm_rotation.py --book 4 --repeats 1       # 真跑（消耗真实调用）
    python engine/smoke_arm_rotation.py --arms official-flash --repeats 1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import yaml  # noqa: E402

from smoke_log_stats import collect_calls, count_signals  # noqa: E402
from smoke_model_bakeoff import ARMS  # noqa: E402
from smoke_test import TOKEN, EngineProcess, build_sandbox, http  # noqa: E402

ROTATION_VERSION = "arm-rotation/v1"

#: 真实作品目录（只读；本脚本只对它做按字节复制）
REPO_NOVELS = ENGINE_DIR / "data" / "novels"

#: 引擎侧状态机取值（src/web/server.py:889）
BUSY = ("generating_cards", "writing", "committing")
#: 「出卡就绪」有两个终态：`awaiting_choice`（等选卡）与 `awaiting_review`（本章已有稿在待审）。
#: 漏掉后者会让轮询一直空转到 timeout —— 实测踩到过：作品当前章本来就有一稿在待审，
#: start 立刻进入 awaiting_review，而这里只等 awaiting_choice → 白等 420 秒。
TERMINAL_CARDS = ("awaiting_choice", "awaiting_review", "error")
TERMINAL_CHOICE = ("awaiting_choice", "error")
TERMINAL_WRITE = ("awaiting_review", "error")

_ERR_KIND_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_.]*Error|[A-Z][A-Za-z0-9_]*Exception)")


def _safe_view(snap: dict) -> dict:
    """快照 → **只含非正文**的视图。

    白名单是安全边界，不是格式化：`snapshot()` 里的 `cards`（剧情卡文案）与
    `draft.draft_text`（整章正文）**绝不能**出现在返回值里。
    """
    cards = snap.get("cards") or []
    draft = snap.get("draft") or {}
    draft_text = draft.get("draft_text") or ""
    return {
        "status": snap.get("status"),
        "chapter": snap.get("chapter"),
        "card_count": len(cards),
        "card_ids": [c.get("card_id") for c in cards],
        "draft_present": bool(draft),
        "draft_chars": len(draft_text),
        "draft_status": draft.get("status"),
        "target_words": snap.get("target_words"),
        "length_floor": snap.get("length_floor"),
        "length_ceiling": snap.get("length_ceiling"),
        "approved_count": snap.get("approved_count"),
        "error_source": snap.get("error_source") or None,
        "error_kind": _error_kind(snap.get("error")),
        "error_chars": len(snap.get("error") or ""),
        "models": snap.get("models") or {},
        "version": snap.get("version"),
    }


def _error_kind(error: object) -> str | None:
    """只抽出异常**类型名**，绝不回显消息（消息可能带模型原始 JSON = 正文）。"""
    if not error:
        return None
    m = _ERR_KIND_RE.match(str(error))
    if m:
        return m.group(1)
    return "（非 `<Type>Error:` 形态，已省略）"


# ────────────────────────── 沙箱 ──────────────────────────

def stage_master(root: Path, book: str) -> Path:
    """把真实作品目录按字节复制成**只读母本**：只 `copytree`，不解析、不读取内容。"""
    src = REPO_NOVELS / book
    if not src.is_dir():
        raise SystemExit(f"作品目录不存在：{src}")
    dst = root / "master" / book
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)
    return dst


def stage_arm_book(arm_root: Path, master: Path, book: str) -> Path:
    """每臂从**母本**重新复制一份作品。

    为什么必须这么做：如果三臂共用一份副本，A 臂写完一章后 B 臂就从"被改过的书"起跑
    —— 起始状态不同，对照就不成立（doc 的"同一本书、同一章"要求起始状态一致）。
    """
    dst = arm_root / "data" / "novels" / book
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(master, dst)
    return dst


def write_arm_config(cfg_dir: Path, provider: str, model: str) -> Path:
    """沙箱 configs/models.yaml：全部 roles 绑到该臂，其余参数原样保留。"""
    base = yaml.safe_load((ENGINE_DIR / "configs" / "models.yaml").read_text(encoding="utf-8"))
    for role_cfg in (base.get("roles") or {}).values():
        role_cfg["provider"] = provider
        role_cfg["model"] = model
    path = cfg_dir / "models.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(base, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    return path


def arm_env(root: Path, book: str) -> dict:
    env = dict(os.environ)
    env.update({
        "NOVELS_DIR": str(root / "data" / "novels"),
        "RUNTIME_DIR": str(root / "data" / "runtime"),
        "CONFIGS_DIR": str(root / "configs"),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
        "INKFORGE_GIT_HISTORY": "0",          # 沙箱里不需要写作历史仓库
        "PYTHONIOENCODING": "utf-8",
        # rich 在非 TTY 时取 COLUMNS、缺省 80 列 → 约 130 字的 `[调用]` 行会被折成 3 段，
        # 段尾还补空格到列宽，精口径分母直接归零。给足宽度即可一行落盘。
        # 行宽给足，否则 rich 折行会拆断 `[调用]` 诊断行（精口径分母就没了）
        "COLUMNS": "400",
        "SMOKE_PYTHON": sys.executable,       # 用本进程的解释器（venv）
    })
    return env


# ────────────────────────── HTTP 驱动 ──────────────────────────

def _get_state(base: str, book: str) -> dict:
    r = http("GET", f"{base}/api/interactive/state?novel={quote(book)}", timeout=30)
    return r.body if (r.status == 200 and isinstance(r.body, dict)) else {}


def _wait_terminal(base: str, book: str, terminal: tuple[str, ...],
                   timeout: float, prev_status: str | None = None,
                   start_grace: float = 45.0) -> tuple[dict, float, bool]:
    """轮询直到进入终态；返回 (快照, 耗时, 是否命中终态)。

    先花最多 `start_grace` 秒确认**动作真的起来了**（状态离开 `prev_status`）——
    否则 POST 被 4xx 拒掉时这里会安静地空转满 `timeout`（实测踩到：一个臂白等 15 分钟）。
    """
    t0 = time.time()
    last: dict = {}
    if prev_status is not None:
        started = False
        while time.time() - t0 < start_grace:
            snap = _get_state(base, book)
            if snap:
                last = snap
                if snap.get("status") != prev_status or snap.get("status") in terminal:
                    started = True
                    break
            time.sleep(1.0)
        if not started:
            return last, time.time() - t0, False
    while time.time() - t0 < timeout:
        snap = _get_state(base, book)
        if snap:
            last = snap
            if snap.get("status") in terminal:
                return last, time.time() - t0, True
        time.sleep(2.0)
    return last, time.time() - t0, False


def _engine_bindings(base: str) -> dict:
    """从**只读**端点 `/api/model-config` 取引擎**实际看到**的角色绑定。

    为什么不用快照里的 `models`：那个字段由 runner 组装后才有值，`start` 之前恒为空
    ——dry-run 就验不到"绑定是否真的生效"。这个端点直接回读 `configs/models.yaml`，
    不触发任何生成。
    """
    r = http("GET", f"{base}/api/model-config", timeout=30)
    if r.status != 200 or not isinstance(r.body, dict):
        return {}
    return {x.get("role"): f"{x.get('provider')}/{x.get('model')}"
            for x in (r.body.get("roles") or []) if isinstance(x, dict)}


def _post(base: str, path: str, book: str, body: dict | None = None) -> dict:
    """POST 并只回传**非正文**的回执：状态码 + ok + 4xx 的 detail。

    4xx 的 `detail` 是引擎里**手写的中文提示**（如"这本书是「自由创作」模式…"
    "互动会话正在处理中，请等当前这一步跑完"），属结构性信息、不含作品正文，故原样保留
    （截 160 字）—— 这是定位"动作为什么没起来"的唯一线索。5xx/异常文本不保留，只留类型名。
    """
    r = http("POST", f"{base}{path}?novel={quote(book)}", body=body or {}, timeout=60)
    out: dict = {"status_code": r.status}
    if isinstance(r.body, dict):
        out["ok"] = r.body.get("ok")
        if r.status != 200 and isinstance(r.body.get("detail"), str):
            out["detail"] = r.body["detail"][:160]
    else:
        out["ok"] = None
        if r.status == 0:
            out["transport_error"] = True
    return out


def run_arm(arm: dict, rounds: int, book: str, master: Path, root: Path, timeout: float,
            dry_run: bool) -> dict:
    """跑一个臂的全部轮次；返回**不含正文**的结构化记录。

    每臂一个独立沙箱根（`root/arms/<arm-id>`）：绑定、runtime 索引、作品副本都互不串味，
    且作品副本每次都从母本重取，保证三臂起始状态完全一致。
    """
    arm_root = root / "arms" / arm["id"]
    build_sandbox(arm_root)                                   # configs + data/{novels,runtime,skills}
    stage_arm_book(arm_root, master, book)
    write_arm_config(arm_root / "configs", arm["provider"], arm["model"])
    env = arm_env(arm_root, book)
    engine = EngineProcess(ENGINE_DIR, book, env)

    record: dict = {
        "arm": arm["id"], "provider": arm["provider"], "model": arm["model"],
        "rounds": [], "engine_started": False,
    }
    try:
        try:
            engine.start(timeout=120.0)
        except Exception as exc:  # noqa: BLE001 - 启动失败只报类型，不回显引擎原始输出
            record["engine_error_kind"] = type(exc).__name__
            record["engine_error_chars"] = len(str(exc))
            return record
        record["engine_started"] = True
        record["base"] = engine.base
        record["engine_models"] = _safe_view(_get_state(engine.base, book)).get("models")

        if dry_run:
            # 零 LLM：**不点任何动作**。`/api/interactive/start` 本身就会触发出卡调用，
            # 所以 dry-run 只验「沙箱 + 引擎能起 + 只读端点可读 + 绑定已生效」。
            record["engine_bindings"] = _engine_bindings(engine.base)
            record["dry_state"] = _safe_view(_get_state(engine.base, book))
            record["dry_ping"] = http("GET", f"{engine.base}/api/ping").status
            return record

        for i in range(1, rounds + 1):
            mark = len(engine.log_lines)
            rnd: dict = {"round": i}
            abort = False

            # ---- 动作 0：启动会话（只在第 1 轮）----
            if i == 1:
                prev = (_get_state(engine.base, book) or {}).get("status")
                rnd["start"] = _post(engine.base, "/api/interactive/start", book)
                if rnd["start"].get("status_code") != 200:
                    rnd["start"]["aborted"] = True
                    abort = True
                else:
                    snap, secs, ok = _wait_terminal(engine.base, book, TERMINAL_CARDS,
                                                    timeout, prev_status=prev)
                    rnd["start"].update(_safe_view(snap)
                                        | {"elapsed": round(secs, 1), "settled": ok})

            # 动作序列固定为 **discard → redraw → choose**，三臂完全一致。
            # 为什么每轮先 discard：真实作品当前章很可能已经有一稿在待审（实测就是这样），
            # 此时 redraw 会被 409 拒掉。先删掉本章产出回到"出卡"阶段，才能保证
            # **每一臂、每一轮的起始状态与动作序列完全相同**，对照才成立。
            steps = [
                ("/api/interactive/discard", "discard", None, TERMINAL_CHOICE,
                 "删除本章产出 + 重新出卡"),
                ("/api/interactive/redraw", "redraw", {"feedback": ""}, TERMINAL_CHOICE,
                 "重出卡"),
                ("/api/interactive/choose", "choose",
                 {"card_id": "c1", "custom_text": "", "target_words": None}, TERMINAL_WRITE,
                 "选卡写章"),
            ]
            for path, key, body, terminal, _label in steps:
                if abort:
                    rnd[key] = {"skipped": True}
                    continue
                prev = (_get_state(engine.base, book) or {}).get("status")
                rnd[key] = _post(engine.base, path, book, body)
                if rnd[key].get("status_code") != 200:
                    rnd[key]["aborted"] = True
                    abort = True
                    continue
                snap, secs, ok = _wait_terminal(engine.base, book, terminal, timeout,
                                                prev_status=prev)
                rnd[key].update(_safe_view(snap) | {"elapsed": round(secs, 1), "settled": ok})
                if snap.get("status") == "error":
                    abort = True

            lines = engine.log_lines[mark:]
            rnd["signals"] = dict(count_signals(lines))
            rnd["calls"] = collect_calls(lines)
            record["rounds"].append(rnd)

            if abort or (_get_state(engine.base, book) or {}).get("status") == "error":
                # 与真机一致：失败按提示重试 1 次即止，不追加轮次
                _post(engine.base, "/api/interactive/reset", book)
                break
    finally:
        record["engine_lines"] = len(engine.log_lines)
        record["arm_signals"] = dict(count_signals(engine.log_lines))
        record["arm_calls"] = collect_calls(engine.log_lines)
        engine.stop()
    return record


# ────────────────────────── 汇总 ──────────────────────────

def _refusal_events(sig: Counter | dict) -> int:
    get = sig.get
    return max(get("拒答命中", 0), get("拒答致重试", 0))


def _p50(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    return round(ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2, 1)


def summarize(record: dict) -> dict:
    calls = record.get("arm_calls") or []
    sig = Counter(record.get("arm_signals") or {})
    attempts = sig["写作启动"] + sig["保真链重试"]
    lat = [c["elapsed"] for c in calls]
    by_role: dict[str, list[float]] = {}
    for c in calls:
        by_role.setdefault(c["role"], []).append(c["elapsed"])
    per_round = []
    for rnd in record.get("rounds") or []:
        rs = Counter(rnd.get("signals") or {})
        last = rnd.get("choose") or rnd.get("redraw") or rnd.get("discard") or rnd.get("start") or {}
        per_round.append({
            "round": rnd["round"],
            "card_count": (rnd.get("redraw") or {}).get("card_count")
                          or (rnd.get("discard") or {}).get("card_count")
                          or (rnd.get("start") or {}).get("card_count"),
            "draft_chars": (rnd.get("choose") or {}).get("draft_chars"),
            "draft_status": (rnd.get("choose") or {}).get("draft_status"),
            "refusal_events": _refusal_events(rs),
            "calls": len(rnd.get("calls") or []),
            "final_status": last.get("status"),
            "error_source": last.get("error_source"),
            "error_kind": last.get("error_kind"),
            "http": {k: (v.get("status_code"), v.get("detail"))
                     for k, v in rnd.items() if isinstance(v, dict) and "status_code" in v},
        })
    return {
        "arm": record["arm"], "provider": record["provider"], "model": record["model"],
        "engine_started": record.get("engine_started"),
        "engine_models": record.get("engine_models"),
        "engine_bindings": record.get("engine_bindings"),
        "dry_state": record.get("dry_state"),
        "dry_ping": record.get("dry_ping"),
        "engine_error_kind": record.get("engine_error_kind"),
        "calls": len(calls),
        "attempts_coarse": attempts,
        "refusal_events": _refusal_events(sig),
        "refusal_rate": round(_refusal_events(sig) / attempts, 3) if attempts else None,
        "signals": dict(sig),
        "latency": {
            "all_p50": _p50(lat), "all_max": round(max(lat), 1) if lat else None,
            "by_role": {r: {"n": len(v), "p50": _p50(v), "max": round(max(v), 1)}
                        for r, v in sorted(by_role.items())},
        },
        "finish_reason": dict(Counter(c["finish"] for c in calls)),
        "empty_outputs": sum(1 for c in calls if c["out_chars"] == 0),
        "rounds": per_round,
    }


def print_report(rows: list[dict]) -> None:
    line = "=" * 100
    print(line)
    print(f"实机轮换对照 {ROTATION_VERSION}")
    print(line)
    for s in rows:
        print()
        print(f"--- 臂 {s['arm']}  =  {s['provider']} / {s['model']}")
        if not s.get("engine_started"):
            print(f"    引擎启动失败：{s.get('engine_error_kind')}（原始输出已省略）")
            continue
        if s.get("engine_bindings"):
            bad = {r: v for r, v in s["engine_bindings"].items()
                   if v != f"{s['provider']}/{s['model']}"}
            print(f"    引擎侧绑定：{len(s['engine_bindings'])} 个角色"
                  + ("，全部指向本臂 ✓" if not bad else f"，**未统一**：{bad}"))
        if s.get("dry_state"):
            d = s["dry_state"]
            print(f"    dry-run：ping={s.get('dry_ping')}  status={d.get('status')}  "
                  f"chapter={d.get('chapter')}  卡片={d.get('card_count')}  "
                  f"草稿={d.get('draft_chars')} 字  目标={d.get('target_words')}"
                  f"（{d.get('length_floor')}-{d.get('length_ceiling')}）")
        if s.get("engine_models"):
            print(f"    引擎侧实际绑定：{s['engine_models']}")
        print(f"    调用（[调用] 行）= {s['calls']}   粗口径尝试 = {s['attempts_coarse']}   "
              f"空产出 = {s['empty_outputs']}")
        print(f"    拒答事件 = {s['refusal_events']}"
              f"   拒答率 = {s['refusal_rate'] if s['refusal_rate'] is not None else '（无分母）'}")
        if s["signals"]:
            print("    信号：" + "  ".join(f"{k}={v}" for k, v in s["signals"].items()))
        print(f"    耗时：p50={s['latency']['all_p50']}s  max={s['latency']['all_max']}s   "
              f"收尾={s['finish_reason']}")
        for role, v in s["latency"]["by_role"].items():
            print(f"      · {role:<10} n={v['n']:<4} p50={v['p50']}s max={v['max']}s")
        for r in s["rounds"]:
            http_bits = " ".join(f"{k}={v[0]}" for k, v in (r.get("http") or {}).items())
            details = [v[1] for v in (r.get("http") or {}).values() if v[1]]
            print(f"    轮 {r['round']}：出卡={r['card_count']} 张  草稿={r['draft_chars']} 字"
                  f"/{r['draft_status']}  拒答={r['refusal_events']}  调用={r['calls']}"
                  f"  终态={r['final_status']}  [{http_bits}]"
                  + (f"  ({r['error_source']}/{r['error_kind']})" if r.get("error_kind") else ""))
            for d in details:
                print(f"        ↳ 端点拒绝原因：{d}")
    print()
    print(line)
    print("【口径与诚实声明】")
    print("  · 每臂 n = 每轮 1 次出卡 + 1 次写章。**样本量很小，只能看趋势，不能做统计结论**。")
    print("  · 拒答事件 = max(判定行, 重试行)，与 smoke_log_stats 同口径（一次拒答产生两条行）。")
    print("  · 本脚本不读正文：作品目录只做字节复制；输出只含状态/计数/长度/耗时/异常类名。")
    print(line)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="实机轮换对照（真实上下文 × 三个授权臂）")
    ap.add_argument("--book", default="4", help="真实作品目录名（默认 4 = 当前活跃作品）")
    ap.add_argument("--repeats", type=int, default=1, help="每臂轮数")
    ap.add_argument("--arms", default="", help="逗号分隔的臂 id；缺省=全部三个授权臂")
    ap.add_argument("--timeout", type=float, default=900.0, help="单动作等待上限（秒）")
    ap.add_argument("--dry-run", action="store_true", help="零 LLM：只验沙箱 + 引擎 + 端点")
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    wanted = [a.strip() for a in args.arms.split(",") if a.strip()]
    arms = [a for a in ARMS if not wanted or a["id"] in wanted]
    if not arms:
        raise SystemExit(f"没有匹配的臂：{wanted}；可选 {[a['id'] for a in ARMS]}")

    root = Path(tempfile.mkdtemp(prefix="inkforge-rotation-"))
    raw: list[dict] = []
    try:
        master = stage_master(root, args.book)
        print(f"沙箱：{root}")
        print(f"作品母本：{master.name}（按字节复制，未解析内容）")
        print(f"臂：{[a['id'] for a in arms]}   轮数/臂：{args.repeats}   "
              f"dry-run：{args.dry_run}")
        print("每臂独立沙箱 + 每次从母本重取作品 → 三臂起始状态一致")
        for arm in arms:
            print(f"\n>>> 开始臂 {arm['id']} …", flush=True)
            t0 = time.time()
            rec = run_arm(arm, args.repeats, args.book, master, root, args.timeout,
                          args.dry_run)
            rec["elapsed_total"] = round(time.time() - t0, 1)
            raw.append(rec)
            print(f"<<< 臂 {arm['id']} 结束（{rec['elapsed_total']}s）", flush=True)
    finally:
        rows = [summarize(r) for r in raw]
        print_report(rows)
        out = ENGINE_DIR / "smoke-reports" / f"rotation-{datetime.now():%Y%m%d-%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "version": ROTATION_VERSION,
            "stamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
            "book": args.book, "repeats": args.repeats, "dry_run": args.dry_run,
            "arms": [a["id"] for a in arms],
            "summary": rows,
            "raw": raw,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结构化结果：{out}")
        if not args.keep_sandbox:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print(f"沙箱已保留：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
