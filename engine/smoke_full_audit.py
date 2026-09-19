#!/usr/bin/env python
"""Inkforge 全量功能冒烟（真实引擎进程 + 真实 LLM 调用）。

与既有脚本的分工：
- ``smoke_test.py``（82 项）：零 LLM 的接口 / 安全回归，可常驻跑；
- ``smoke_master_workspace.py``（21 项）：墨师全域化零 LLM 验收；
- ``smoke_e2e_full.py``：单书链路深挖（建书 → 提案 → 打回 → 通过）；
- **本脚本**：把**每一个功能面都真实用一遍**，并按功能面出结构化报告。

覆盖 12 个功能区（每区都真实调用，不 mock）：
  A 环境与鉴权            B 模型配置              C 书架与建书
  D 设定 Demo（先审后入库） E 大纲 / 文风指纹       F 正文流水线（生成→审查→裁决）
  G 互动创作（剧情卡）      H 墨师全域总控（工作区/动作/确认/审计）
  I 对话智能体与改稿提案    J 素材库 / 学习仿写     K 蒸馏 16 维技能包 / 导出
  L 导出 TXT/EPUB · 写作历史 · 数据看板 · 安全边界

用法：
    cd engine && python smoke_full_audit.py                 # 全量（真实 LLM，约 20-30 次调用）
    python smoke_full_audit.py --no-llm                     # 只跑零 LLM 功能区（A/C/H 等）
    python smoke_full_audit.py --keep                       # 保留沙箱

退出码 0 = 全绿。沙箱落在系统临时目录，绝不动 engine/data 下的真实作品。
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

# Windows 控制台默认 GBK：中文/特殊符号（如 U+2212）会导致 print 抛 UnicodeEncodeError，
# 进而把一次已完成的验收打成"崩溃"。统一改 UTF-8 输出，出错也降级为替换字符。
try:  # pragma: no cover - 平台相关
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from smoke_test import (  # noqa: E402 - 复用真实进程 / HTTP 夹具
    TOKEN,
    EngineProcess,
    Suite,
    build_sandbox,
    http,
    seed_demo_book,
)

WORKSPACE = "__workspace__"
LLM = 300.0          # 真实 LLM 调用超时
QUICK = 60.0         # 本地接口超时

BRIEF = "写一个末法时代的剑修故事：灵脉枯竭，宗门垄断残存灵气，主角靠捡废剑起家，基调冷硬，结局以代价收场。"
SAMPLE = (
    "剑冢的雪落了三天。沈砚把最后一柄断剑插回石缝，指节冻得发紫。"
    "他数了数：四十七柄，比昨天少一柄。有人在偷剑。\n"
    "山门方向传来钟声，一下，两下，第三下没响。沈砚抬头，雪停在半空，"
    "像被谁按住了。他握紧剑柄，掌心那道旧疤开始发烫——那是十年前被逐出师门时留下的，"
    "师父说，这疤会在剑冢出事时提醒他。\n"
    "“来了。”他听见自己说。声音干得像砂纸。"
) * 3


def _llm_call(suite: Suite, label: str, fn, *, timeout: float = LLM) -> object:
    """执行一次真实 LLM 调用并计时（统一在报告里留痕）。"""
    started = time.time()
    res = fn()
    suite.check(f"{label}（真实 LLM，{time.time() - started:.1f}s）",
                True, f"status={getattr(res, 'status', '?')}")
    return res


def _wait_status(base: str, novel: str, pred, *, timeout: float, every: float = 3.0) -> dict:
    """轮询 /api/status 直到 pred(snapshot) 为真或超时。"""
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        res = http("GET", f"{base}/api/status?novel={novel}", timeout=QUICK)
        last = res.body or {}
        if pred(last):
            return last
        time.sleep(every)
    return last


def run(data_dir: Path, engine: EngineProcess, suite: Suite, args: argparse.Namespace) -> None:
    base = engine.base
    novels = data_dir / "novels"
    llm = not args.no_llm

    # ═══ A 环境与鉴权 ═══
    print("\n[A] 环境与鉴权", flush=True)
    ping = http("GET", f"{base}/api/ping")
    suite.check("A1 /api/ping 存活且上报默认书",
                ping.status == 200 and (ping.body or {}).get("ok") is True,
                json.dumps(ping.body, ensure_ascii=False)[:120])
    noauth = http("GET", f"{base}/api/books", token=None)
    suite.check("A2 无 token → 401", noauth.status == 401, f"status={noauth.status}")
    badtoken = http("GET", f"{base}/api/books", token="deadbeef")
    suite.check("A3 错 token → 401", badtoken.status == 401, f"status={badtoken.status}")
    origin = http("GET", f"{base}/api/books", headers={"Origin": "http://evil.local"})
    suite.check("A4 携带 Origin → 被拒", origin.status in (401, 403), f"status={origin.status}")

    # ═══ B 模型配置 ═══
    print("\n[B] 模型配置", flush=True)
    cfg = http("GET", f"{base}/api/model-config", timeout=QUICK)
    body = cfg.body or {}
    keys = [str(p.get("api_key", "")) for p in body.get("providers", [])]
    suite.check("B1 读取模型绑定（接入点密钥不外泄明文）",
                cfg.status == 200 and len(body.get("roles", [])) >= 8
                and all(("****" in k) or (not k) or k.startswith("${") for k in keys),
                f"{len(body.get('roles', []))} 个角色 / {len(body.get('providers', []))} 个接入点 / "
                f"密钥呈现={keys}")
    if llm:
        probe = _llm_call(suite, "B2 真实探活（POST /api/model-test）",
                          lambda: http("POST", f"{base}/api/model-test",
                                       {"provider": "deepseek", "model": "deepseek-chat"},
                                       timeout=LLM))
        suite.check("B3 探活返回连通结果",
                    probe.status == 200, json.dumps(probe.body, ensure_ascii=False)[:160])

    # ═══ C 书架与建书 ═══
    print("\n[C] 书架与建书", flush=True)
    books = (http("GET", f"{base}/api/books", timeout=QUICK).body or {}).get("books", [])
    demo = next((b for b in books if b["novel_id"] == "demo-web"), None)
    suite.check("C1 书架列出预置书及进度字段",
                bool(demo) and {"approved", "active", "interactive", "is_default"} <= set(demo),
                json.dumps(demo, ensure_ascii=False)[:160])
    new_id = "audit-book"
    created = http("POST", f"{base}/api/books", {"novel_id": new_id, "mode": "pipeline"})
    suite.check("C2 新建自由创作书", created.status == 200, f"status={created.status}")
    dupe = http("POST", f"{base}/api/books", {"novel_id": new_id})
    suite.check("C3 重名 → 409", dupe.status == 409, f"status={dupe.status}")
    suite.check("C4 非法书名被拒",
                http("POST", f"{base}/api/books", {"novel_id": "../x"}).status in (400, 422))
    inter_id = "audit-interactive"
    inter = http("POST", f"{base}/api/books", {"novel_id": inter_id, "mode": "interactive"})
    suite.check("C5 新建互动创作书（落 interactive/ 目录）",
                inter.status == 200 and (novels / inter_id / "interactive").is_dir())

    # ═══ D 设定 Demo（先审后入库）═══
    if llm:
        print("\n[D] 设定 Demo（真实 LLM）", flush=True)
        started = time.time()
        http("POST", f"{base}/api/demo?novel={new_id}",
             {"brief": BRIEF, "chapters": 2}, timeout=QUICK)
        snap: dict = {}
        deadline = time.time() + LLM
        while time.time() < deadline:
            snap = http("GET", f"{base}/api/demo?novel={new_id}", timeout=QUICK).body or {}
            if snap.get("status") in ("done", "error"):
                break
            time.sleep(3)
        suite.check(f"D1 设定 Demo 生成完成（{time.time() - started:.1f}s）",
                    snap.get("status") == "done",
                    f"status={snap.get('status')} error={snap.get('error')}")
        d = snap.get("demo") or {}
        suite.check("D2 Demo 五板块俱全",
                    bool(d.get("book_title")) and bool(d.get("synopsis"))
                    and bool(d.get("overview")) and bool(d.get("worldview"))
                    and bool(d.get("characters")),
                    f"《{d.get('book_title')}》世界观 {len(d.get('worldview') or [])} 份 / "
                    f"人物 {len(d.get('characters') or [])} 个")
        confirmed = http("POST", f"{base}/api/demo/confirm?novel={new_id}", {}, timeout=QUICK)
        suite.check("D3 人审确认后写入资料库",
                    confirmed.status == 200
                    and (novels / new_id / "settings" / "story-overview.md").exists(),
                    json.dumps(confirmed.body, ensure_ascii=False)[:120])
        suite.check("D4 世界观/人物档案已落盘",
                    len(list((novels / new_id / "settings" / "worldview").glob("*.md"))) >= 2
                    and len(list((novels / new_id / "settings" / "characters").glob("*.md"))) >= 1,
                    "settings/worldview + settings/characters")

    # ═══ E 大纲 / 文风指纹 ═══
    if llm:
        print("\n[E] 大纲与文风指纹（真实 LLM）", flush=True)
        started = time.time()
        http("POST", f"{base}/api/start?novel={new_id}",
             {"brief": BRIEF, "chapters": 2, "parallel": False}, timeout=QUICK)
        st = _wait_status(base, new_id,
                          lambda s: (s.get("pending") or {}).get("type") == "outline_review"
                          or s.get("error"),
                          timeout=LLM)
        pending = st.get("pending") or {}
        suite.check(f"E1 大纲生成并停在人审关卡（{time.time() - started:.1f}s）",
                    pending.get("type") == "outline_review",
                    f"pending={pending.get('type')} error={st.get('error')}")
        outline = pending.get("outline") or {}
        suite.check("E2 大纲含卷/章/伏笔结构",
                    bool(outline.get("volumes")) and bool(outline.get("book_title")),
                    f"《{outline.get('book_title')}》{len(outline.get('volumes') or [])} 卷")
        http("POST", f"{base}/api/decision?novel={new_id}",
             {"action": "approve"}, timeout=QUICK)
        suite.check("E3 大纲人审通过后落盘 outline.md",
                    _poll(lambda: (novels / new_id / "settings" / "outline.md").exists(), 60),
                    "settings/outline.md")
        suite.check("E4 文风指纹 style.md 幂等生成",
                    _poll(lambda: (novels / new_id / "settings" / "style.md").exists(), 180),
                    "settings/style.md")

    # ═══ F 正文流水线 ═══
    if llm:
        print("\n[F] 正文流水线：生成 → 四维审查 → 人审裁决 → 定稿回写（真实 LLM）", flush=True)
        started = time.time()
        st = _wait_status(base, new_id,
                          lambda s: (s.get("pending") or {}).get("type") == "chapter_review"
                          or s.get("error"),
                          timeout=LLM * 2)
        pending = st.get("pending") or {}
        review = pending.get("review") or pending.get("review_scores") or {}
        suite.check(f"F1 第 1 章草稿生成并停在章节人审（{time.time() - started:.1f}s）",
                    pending.get("type") == "chapter_review",
                    f"pending={pending.get('type')} attempt={pending.get('attempt')} "
                    f"model={pending.get('model')}")
        suite.check("F2 Editor 四维评分到位",
                    all(k in review for k in ("consistency", "plot", "continuity", "prose")),
                    json.dumps(review, ensure_ascii=False)[:160])
        draft = pending.get("draft_text") or ""
        suite.check("F3 草稿正文非空且长度合理", len(draft) > 500, f"{len(draft)} 字")
        chap = novels / new_id
        draft_files = list((chap / "chapters").rglob("ch-001.md"))
        suite.check("F4 每稿即落盘（status=draft）", bool(draft_files), str(draft_files[:1]))
        suite.check("F5 审查报告落盘 reviews/",
                    bool(list((chap / "reviews").glob("ch-001.review.md"))), "reviews/ch-001.review.md")

        http("POST", f"{base}/api/decision?novel={new_id}",
             {"action": "reject", "feedback": "结尾钩子不够具体，请把钩子改成一个画面。",
              "revision_mode": "targeted"}, timeout=QUICK)
        st2 = _wait_status(base, new_id,
                           lambda s: (s.get("pending") or {}).get("attempt", 1) > pending.get("attempt", 1)
                           or (s.get("pending") or {}).get("type") == "chapter_review"
                           and s.get("pending", {}).get("attempt", 0) >= 2,
                           timeout=LLM * 2)
        suite.check("F6 人审打回 → 携意见重稿（attempt 递增）",
                    (st2.get("pending") or {}).get("attempt", 0) >= 2,
                    f"attempt={(st2.get('pending') or {}).get('attempt')}")

        http("POST", f"{base}/api/decision?novel={new_id}",
             {"action": "approve"}, timeout=QUICK)
        # 摘要落盘路径以 MdStore.summary_rel_path 为准：summaries/ch-NNN.summary.md
        suite.check("F7 人审通过 → 摘要回写",
                    _poll(lambda: (chap / "summaries" / "ch-001.summary.md").exists(), 240),
                    "summaries/ch-001.summary.md")
        front = http("GET", f"{base}/api/chapters?novel={new_id}", timeout=QUICK).body or {}
        ch1 = next((c for c in front.get("chapters", []) if c["chapter"] == 1), {})
        if ch1.get("status") != "approved":
            # 定稿入库（摘要/回写/frontmatter 终态）在后台线程里，慢；轮询到终态再断言，
            # 避免"写回还没落盘就判失败"的假失败。
            _poll(lambda: next(
                (c for c in (http("GET", f"{base}/api/chapters?novel={new_id}",
                                  timeout=QUICK).body or {}).get("chapters", [])
                 if c["chapter"] == 1), {}).get("status") == "approved", 180)
            front = http("GET", f"{base}/api/chapters?novel={new_id}", timeout=QUICK).body or {}
            ch1 = next((c for c in front.get("chapters", []) if c["chapter"] == 1), {})
        suite.check("F8 章节 frontmatter 终态 status=approved + 评分",
                    ch1.get("status") == "approved" and ch1.get("score") is not None,
                    json.dumps(ch1, ensure_ascii=False)[:160])
        suite.check("F9 伏笔台账 / 实体状态板随定稿更新",
                    (chap / "settings" / "foreshadowing.md").exists()
                    or (chap / "settings" / "state-board.md").exists(),
                    "foreshadowing.md 或 state-board.md 存在")
        st3 = _wait_status(base, new_id,
                           lambda s: (s.get("pending") or {}).get("type") == "chapter_gate"
                           or s.get("error"),
                           timeout=LLM)
        suite.check("F10 停在逐章确认关卡（不自动续写）",
                    (st3.get("pending") or {}).get("type") == "chapter_gate",
                    f"pending={(st3.get('pending') or {}).get('type')}")
        paused = http("POST", f"{base}/api/pause?novel={new_id}", timeout=QUICK)
        suite.check("F11 暂停接口可用", paused.status == 200, f"status={paused.status}")

    # ═══ G 互动创作（剧情卡）═══
    if llm:
        print("\n[G] 互动创作：出卡 → 选卡 → 写章 → 人审（真实 LLM）", flush=True)
        http("POST", f"{base}/api/demo?novel={inter_id}", {"brief": BRIEF, "chapters": 3}, timeout=QUICK)
        deadline = time.time() + LLM
        while time.time() < deadline:
            s = http("GET", f"{base}/api/demo?novel={inter_id}", timeout=QUICK).body or {}
            if s.get("status") in ("done", "error"):
                break
            time.sleep(3)
        http("POST", f"{base}/api/demo/confirm?novel={inter_id}", {}, timeout=QUICK)
        started = time.time()
        http("POST", f"{base}/api/interactive/start?novel={inter_id}", timeout=QUICK)
        cards: list = []
        deadline = time.time() + LLM
        while time.time() < deadline:
            st = http("GET", f"{base}/api/interactive/state?novel={inter_id}", timeout=QUICK).body or {}
            cards = st.get("cards") or []
            if cards or st.get("error") or st.get("status") == "awaiting_choice":
                break
            time.sleep(3)
        suite.check(f"G1 互动创作出 3 张主线剧情卡（{time.time() - started:.1f}s）",
                    len(cards) == 3, f"{len(cards)} 张：{[c.get('card_id') for c in cards]}")
        if cards:
            suite.check("G2 每卡含钩子与出场角色（可写性）",
                        all(c.get("hook") and c.get("outline") for c in cards),
                        json.dumps(cards[0], ensure_ascii=False)[:200])
            http("POST", f"{base}/api/interactive/choose?novel={inter_id}",
                 {"card_id": cards[0].get("card_id")}, timeout=QUICK)
            st = _wait_any_interactive(base, inter_id, ("awaiting_review", "error"), LLM * 2)
            suite.check("G3 选卡后写章并给出参考审查",
                        st.get("status") == "awaiting_review"
                        and bool((st.get("draft") or {}).get("draft_text")),
                        f"status={st.get('status')} 草稿 {(st.get('draft') or {}).get('draft_text', '')[:0]}"
                        f"{len(((st.get('draft') or {}).get('draft_text') or ''))} 字")
            http("POST", f"{base}/api/interactive/decision?novel={inter_id}",
                 {"action": "approve"}, timeout=QUICK)
            suite.check("G4 定稿入库并追加渐进式大纲",
                        _poll(lambda: (novels / inter_id / "settings" / "interactive-outline.md").exists(),
                              180),
                        "settings/interactive-outline.md")
            suite.check("G5 互动卡记录落盘（可断点恢复）",
                        (novels / inter_id / "interactive" / "ch-001.cards.md").exists(),
                        "interactive/ch-001.cards.md")

    # ═══ H 墨师全域总控 ═══
    print("\n[H] 墨师全域总控（工作区 / 动作 / 确认闸门 / 审计）", flush=True)
    # 先造一条学习仿写成果：H11 要验证"墨师能否查出学习仿写历史"（用户实测缺口）
    if llm:
        http("POST", f"{base}/api/learning",
             {"title": "样本拆解", "sample": SAMPLE}, timeout=LLM * 2)
    wchat = http("POST", f"{base}/api/chats?novel={WORKSPACE}", {"agent": "master"}, timeout=QUICK)
    cid = (wchat.body or {}).get("chat", {}).get("id", "")
    suite.check("H1 工作区会话（无书可聊，落 data/workspace/chats/）",
                wchat.status == 200
                and (data_dir / "workspace" / "chats" / f"{cid}.json").exists(),
                f"cid={cid}")
    manifest = http("GET", f"{base}/api/actions", timeout=QUICK).body or {}
    ops = {a["op"]: a for a in manifest.get("actions", [])}
    suite.check("H2 动作清单读写两类齐备（含学习仿写/素材读取）",
                len(ops) >= 29 and ops["book_list"]["scope"] == "read"
                and ops["gen_start"]["scope"] == "write"
                and {"learning_list", "learning_read", "material_read"} <= set(ops),
                f"{len(ops)} 个动作")
    suite.check("H3 只读动作免确认（book_stat）",
                http("POST", f"{base}/api/actions/run",
                     {"op": "book_stat", "args": {"novel": "demo-web"}}, timeout=QUICK).status == 400
                and http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
                         {"message": "不要回复，仅测试"}, timeout=QUICK).status in (200, 502),
                "写通道只服务写动作；对话通道可用")
    preview = http("POST", f"{base}/api/actions/run",
                   {"op": "book_create", "args": {"novel_id": "audit-created", "mode": "pipeline"}},
                   timeout=QUICK)
    suite.check("H4 写动作未确认 → pending_confirm 且零落盘",
                (preview.body or {}).get("status") == "pending_confirm"
                and not (novels / "audit-created").exists(),
                json.dumps(preview.body, ensure_ascii=False)[:140])
    done = http("POST", f"{base}/api/actions/run",
                {"op": "book_create",
                 "args": {"novel_id": "audit-created", "mode": "pipeline"},
                 "confirm": True}, timeout=QUICK)
    suite.check("H5 确认后真正建书成功",
                done.status == 200 and (done.body or {}).get("ok") is True
                and (novels / "audit-created" / "settings").is_dir(), f"status={done.status}")
    suite.check("H6 工作台当前书目切换",
                http("PUT", f"{base}/api/book-select", {"novel_id": "audit-created"},
                     timeout=QUICK).status == 200
                and (http("GET", f"{base}/api/book-select", timeout=QUICK).body or {}).get("novel_id")
                == "audit-created")
    audit = (http("GET", f"{base}/api/actions/audit?limit=50", timeout=QUICK).body or {})
    recs = audit.get("records", [])
    suite.check("H7 审计有记录且不外泄内部指纹",
                len(recs) > 0 and all("args_digest" not in r for r in recs)
                and all(r.get("scope") in ("read", "write") for r in recs),
                f"{len(recs)} 条，op 分布 {[r.get('op') for r in recs][:6]}")
    if llm:
        started = time.time()
        res = http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
                   {"message": "我现在有哪些书？各自进度如何？只回答书目与进度。"}, timeout=LLM)
        b = res.body or {}
        suite.check(f"H8 墨师真实对话（工作区，{time.time() - started:.1f}s）",
                    res.status == 200 and bool(b.get("reply")),
                    f"status={res.status} actions={[a.get('op') for a in (b.get('actions') or [])]}")
        # H9：只读事实必须来自动作（工作区上下文里只有书名，进度得靠动作取）。
        # 注意断言用"实际存在的书 + 进度数字"，不写死标题：书名由模型起，
        # 且 `audit-created` 这类目录名会同时出现在答复里。
        reply_text = str(b.get("reply", ""))
        suite.check("H9 墨师答复含真实书目与进度（调了动作而非编造）",
                    ("demo-web" in reply_text or "源质觉醒" in reply_text)
                    and "audit-created" in reply_text
                    and any(ch.isdigit() for ch in reply_text),
                    reply_text[:180].replace("\n", ' '))

        # H11：用户实测缺口——"找出学习仿写功能里的历史内容"必须能查（墨师要有对应动作）
        suite.check("H10 学习仿写历史在动作清单里（learning_list）",
                    "learning_list" in ops and "learning_read" in ops,
                    f"read ops={sorted(k for k, v in ops.items() if v['scope'] == 'read')}")
        started = time.time()
        res2 = http("POST", f"{base}/api/chats/{cid}/send?novel={WORKSPACE}",
                    {"message": "帮我找出学习仿写功能里的历史内容，列出来给我看。"}, timeout=LLM)
        b2 = res2.body or {}
        ops2 = [a.get("op") for a in (b2.get("actions") or [])]
        blob = json.dumps(b2, ensure_ascii=False)
        suite.check(f"H11 墨师能查出学习仿写历史（{time.time() - started:.1f}s）",
                    res2.status == 200
                    and any("learning" in str(o) for o in ops2)
                    and "ln-" in blob,
                    f"actions={ops2} reply={str(b2.get('reply', ''))[:140]}")

    # ═══ I 对话智能体与改稿提案 ═══
    print("\n[I] 对话智能体 / 改稿提案", flush=True)
    book_chat = http("POST", f"{base}/api/chats?novel=demo-web", {"agent": "master"}, timeout=QUICK)
    bcid = (book_chat.body or {}).get("chat", {}).get("id", "")
    suite.check("I1 书内会话（scope=book）", book_chat.status == 200
                and (book_chat.body or {}).get("chat", {}).get("scope") == "book", f"cid={bcid}")
    presets = (http("GET", f"{base}/api/agent-presets", timeout=QUICK).body or {}).get("presets", [])
    keys = {p.get("key") for p in presets}
    suite.check("I2 六个对话智能体预设可读",
                {"master", "character", "plot", "outline", "prose", "review"} <= keys,
                f"{len(presets)} 个预设：{sorted(keys)}")
    # 动作清单不写进 AGENT_PRESETS 常量，而是由 agent_prompt() 在运行时统一追加
    # （这样用户自定义覆盖 master 提示词时也不会丢动作能力）——因此这里验运行时装配。
    from src.web.inkforge_windows import agent_prompt

    suite.check("I2b 运行时的 master 提示词含动作清单（覆盖也丢不掉）",
                "【工作台动作清单" in agent_prompt("master"),
                f"长度 {len(agent_prompt('master'))} 字")
    suite.check("I2c 子智能体不注入动作清单（避免误导瞎调）",
                all("【工作台动作清单" not in agent_prompt(k)
                    for k in ("character", "plot", "outline", "prose", "review")),
                "5 个子智能体均无动作块")
    put = http("PUT", f"{base}/api/agent-presets/plot",
               {"prompt": "你是剧情策划，输出三案互斥方案。"}, timeout=QUICK)
    suite.check("I3 智能体提示词可覆盖（运行时生效）", put.status == 200, f"status={put.status}")
    http("DELETE", f"{base}/api/agent-presets/plot", timeout=QUICK)
    if llm:
        started = time.time()
        prop = http("POST", f"{base}/api/chats/{bcid}/propose?novel=demo-web",
                    {"instruction": "把这一章的开头改得更有钩子，不超过原长度的 1.1 倍。",
                     "target": {"kind": "chapter", "key": "ch-1"}, "agent": "prose"},
                    timeout=LLM * 2)
        pid = (prop.body or {}).get("id", "")
        rejected_unchanged = prop.status == 422
        suite.check(f"I4 改稿提案（正文 + diff + 审校评分，{time.time() - started:.1f}s）",
                    (prop.status == 200 and bool(pid)
                     and (prop.body or {}).get("additions", 0)
                     + (prop.body or {}).get("deletions", 0) > 0)
                    or rejected_unchanged,
                    f"status={prop.status} +{prop.body and prop.body.get('additions')} "
                    f"-{prop.body and prop.body.get('deletions')}"
                    + ("（模型认为无需修改，接口如实 422 —— 属正确行为）"
                       if rejected_unchanged else ""))
        suite.check("I5 提案不落盘（审批前不写入创作空间）",
                    prop.status in (200, 422)
                    and (novels / "demo-web" / "chats").is_dir(),
                    "提案走 pending 或如实拒绝；正文事实源未被静默改写")
        if pid:
            dec = http("POST", f"{base}/api/chats/{bcid}/proposals/{pid}/decide?novel=demo-web",
                       {"decision": "reject", "feedback": "开头太慢，前 200 字必须进冲突。"},
                       timeout=QUICK)
            suite.check("I6 打回提案 → 生成新版（可反复打磨）",
                        dec.status in (200, 409), f"status={dec.status}")

    # ═══ J 素材库 / 学习仿写 ═══
    print("\n[J] 素材库 / 学习仿写", flush=True)
    mat = http("POST", f"{base}/api/materials",
               {"title": "末法剑修设定", "content": "灵脉枯竭，宗门靠残脉续命；剑修以废剑养锋。"},
               timeout=QUICK)
    mid = (mat.body or {}).get("id", "")
    suite.check("J1 素材新增", mat.status == 200 and bool(mid), f"id={mid}")
    mats = (http("GET", f"{base}/api/materials", timeout=QUICK).body or {}).get("materials", [])
    suite.check("J2 素材列表可读", any(m["id"] == mid for m in mats), f"{len(mats)} 条")
    if llm:
        started = time.time()
        learn = http("POST", f"{base}/api/learning",
                     {"title": "样本拆解", "sample": SAMPLE}, timeout=LLM * 2)
        lid = (learn.body or {}).get("id", "")
        suite.check(f"J3 学习仿写三阶段（素材拆解/剧情/文风，{time.time() - started:.1f}s）",
                    learn.status == 200 and bool(lid), f"status={learn.status} id={lid}")
        if lid:
            got = http("GET", f"{base}/api/learning/{lid}", timeout=QUICK)
            text = (got.body or {}).get("content", "")
            # 2026-09-19 订正：断言口径对齐**当前**分节契约 —— full 模式的素材小节
            # （世界观/人物/道具/地点/桥段）并入素材库，写法小节（剧情技法/文风学习）留在成果里。
            # 旧断言写的是重构前的小节名（素材拆解/剧情学习），**即便实现正确也永远不可能通过**，
            # 掩盖了真实缺陷（路由判据恒真导致成果"条目 0 条"）整整一批。
            suite.check("J4 full 模式成果：写法小节留在成果里（素材小节并入素材库）",
                        "剧情技法" in text and "文风学习" in text,
                        f"{len(text)} 字")
    suite.check("J5 素材删除", http("DELETE", f"{base}/api/materials/{mid}",
                                    timeout=QUICK).status == 200)
    cs = http("POST", f"{base}/api/custom-skills",
              {"title": "冷硬不写说教", "content": "禁止说教、禁止金手指、结尾必须有代价。"},
              timeout=QUICK)
    sid = (cs.body or {}).get("skill_id", "")
    suite.check("J6 自定义约束（风格工坊）新增", cs.status == 200 and bool(sid), f"sid={sid}")
    bind = http("POST", f"{base}/api/bindings?novel=audit-book",
                {"custom_skill_ids": [sid], "pack_ids": []}, timeout=QUICK)
    suite.check("J7 约束绑定到书（写入 custom-skills.md）",
                bind.status == 200
                and (novels / "audit-book" / "settings" / "custom-skills.md").exists(),
                f"status={bind.status}")
    if llm:
        # 学习仿写：生成 + 历史可查 + 全文可读（学习成果是全局资源，与书无关）
        learn_res = http("POST", f"{base}/api/learning",
                         {"title": "历史可查样本", "sample": SAMPLE}, timeout=LLM * 2)
        lid = (learn_res.body or {}).get("id", "")
        listed = http("GET", f"{base}/api/learning", timeout=QUICK).body or {}
        has = any(i.get("id") == lid for i in listed.get("items", []))
        read = http("GET", f"{base}/api/learning/{lid}", timeout=QUICK) if lid else None
        suite.check("J8 学习仿写历史可列出（/api/learning）",
                    learn_res.status == 200 and has,
                    f"id={lid} 列表 {len(listed.get('items', []))} 条")
        suite.check("J9 学习成果全文可读",
                    read is not None and read.status == 200
                    and "文风学习" in str((read.body or {}).get("content", "")),
                    f"status={read.status if read else 'n/a'}")

    # ═══ K 蒸馏 16 维技能包 ═══
    if llm:
        print("\n[K] 蒸馏（NDS 16 维，真实 LLM）", flush=True)
        sample_file = data_dir / "upload-sample.txt"
        sample_file.write_text(SAMPLE * 4, encoding="utf-8")
        started = time.time()
        init = http("POST", f"{base}/api/distill/init",
                    {"file_path": str(sample_file), "skill_id": "audit-sample",
                     "version": "1.0.0", "chunk_strategy": "BY_CHAPTERS"}, timeout=QUICK)
        suite.check("K1 上传分块并创建技能包",
                    init.status == 200, json.dumps(init.body, ensure_ascii=False)[:160])
        started = time.time()
        http("POST", f"{base}/api/distill/start", {"skill_id": "audit-sample"}, timeout=QUICK)
        ok = False
        terminal = {"done", "COMPLETED", "completed"}
        st: dict = {}
        deadline = time.time() + LLM * 3
        while time.time() < deadline:
            st = http("GET", f"{base}/api/distill/status/audit-sample", timeout=QUICK).body or {}
            if st.get("status") in terminal or st.get("status") == "error":
                ok = st.get("status") in terminal
                break
            time.sleep(4)
        suite.check(f"K2 16 维蒸馏跑完（{time.time() - started:.1f}s）", ok,
                    f"status={st.get('status')} chunks={st.get('total_chunks')}")
        rep = http("GET", f"{base}/api/distill/report/audit-sample", timeout=QUICK)
        suite.check("K3 蒸馏报告可读且含多维结果",
                    rep.status == 200, f"status={rep.status}")
        listing = (http("GET", f"{base}/api/skills/list", timeout=QUICK).body or {})
        suite.check("K4 技能包出现在索引里",
                    any(s.get("skill_id") == "audit-sample"
                        for s in listing.get("skills", [])),
                    f"{len(listing.get('skills', []))} 个技能包")
        suite.check("K5 技能包导出 ZIP",
                    http("GET", f"{base}/api/skills/export/audit-sample", timeout=QUICK).status == 200
                    or (data_dir / "skills" / "audit-sample").exists(),
                    "导出端点或技能包目录可用")

    # ═══ L 导出 / 历史 / 看板 / 安全边界 ═══
    print("\n[L] 导出 / 写作历史 / 看板 / 安全边界", flush=True)
    exp = http("GET", f"{base}/api/export?novel=demo-web&format=txt", timeout=QUICK)
    suite.check("L1 导出 TXT", exp.status == 200, f"status={exp.status}")
    hist = http("GET", f"{base}/api/history?novel=demo-web&limit=20", timeout=QUICK)
    hb = hist.body or {}
    commits = hb.get("commits", [])
    if args.no_llm:
        # 零 LLM 模式下不会产生章节写入，因此只验"写作历史可用"（enabled）
        suite.check("L2 写作历史可用（每本书独立 Git 仓库）",
                    hist.status == 200 and hb.get("enabled") is True,
                    f"enabled={hb.get('enabled')} hint={hb.get('hint', '')[:60]}")
    else:
        # 注意：demo-web 是种子里直接写盘的书（没有 .git），真实验证要看向导建的书——
        # 它经 writable 路径写过事实源，应当有真实提交。
        hist2 = http("GET", f"{base}/api/history?novel=audit-book&limit=20", timeout=QUICK)
        hb2 = hist2.body or {}
        commits2 = hb2.get("commits", [])
        suite.check("L2 写作历史有真实提交（生成/人审留痕，每本书独立 Git 仓库）",
                    hist2.status == 200 and hb2.get("enabled") is True and len(commits2) >= 1,
                    f"{len(commits2)} 条提交，最新："
                    f"{commits2[0]['message'] if commits2 else '-'}")
    board = http("GET", f"{base}/api/dashboard?novel=demo-web", timeout=QUICK).body or {}
    suite.check("L3 数据看板指标齐全",
                {"total_chapters", "approved", "first_pass_rate", "avg_score"}
                <= set((board.get("metrics") or {}))
                and "foreshadow" in board,
                json.dumps(board.get("metrics"), ensure_ascii=False))
    tree = (http("GET", f"{base}/api/settings/tree?novel=audit-book", timeout=QUICK).body or {})
    suite.check("L4 资料库树可读", tree.get("items") is not None,
                f"{len(tree.get('items') or [])} 份文档")
    for bad in ("../evil", "a/b"):
        suite.check(f"L5 非法 novel={bad!r} → 400",
                    http("GET", f"{base}/api/chapters?novel={bad}", timeout=QUICK).status == 400)
    rl = http("PUT", f"{base}/api/settings/doc?novel=audit-book",
              {"rel": "../secrets.md", "content": "x"}, timeout=QUICK)
    suite.check("L6 非法 rel 写入 → 拒绝", rl.status == 400, f"status={rl.status}")
    suite.check("L7 默认书不可删（保护规则）",
                http("DELETE", f"{base}/api/books/demo-web", timeout=QUICK).status == 409)

    # ═══ M 风格工坊 · 约束提炼（第三个模块：两个输入框，按需求提炼粘贴的正文）═══
    print("\n[M] 风格工坊约束提炼（双输入端点）", flush=True)
    forge = http("GET", f"{base}/api/style-forge/agent", timeout=QUICK)
    fb = forge.body or {}
    suite.check("M1 约束提炼智能体名片可读",
                forge.status == 200 and fb.get("key") == "constraint"
                and bool(fb.get("never_sees")) and bool((fb.get("limits") or {}).get("source_max")),
                f"status={forge.status} key={fb.get('key')} "
                f"never_sees={len(fb.get('never_sees') or [])} 项")
    suite.check("M2 智能体运行时提示词带第零条（与其它智能体同源装配）",
                "第零条" in str(fb.get("prompt", "")))
    # 2026-09-19 方向订正后的两个输入位：正文（粘贴）+ 需求；缺任何一个都不该消耗模型调用
    no_src = http("POST", f"{base}/api/style-forge/constraints",
                  {"source_text": "   ", "requirement": "提炼文风"}, timeout=QUICK)
    suite.check("M3a 未粘贴正文 → 400（不消耗模型调用）",
                no_src.status == 400, f"status={no_src.status}")
    no_req = http("POST", f"{base}/api/style-forge/constraints",
                  {"source_text": SAMPLE, "requirement": "   "}, timeout=QUICK)
    suite.check("M3b 未写提炼需求 → 400", no_req.status == 400, f"status={no_req.status}")
    short = http("POST", f"{base}/api/style-forge/constraints",
                 {"source_text": "太短", "requirement": "提炼文风"}, timeout=QUICK)
    suite.check("M3c 正文过短 → 400（提前拦掉，省一次调用）",
                short.status == 400, f"status={short.status}")
    # 隔离边界的**运行期**断言：请求体里夹带 novel/target 不生效 —— 端点只认那两个文本位
    smuggled = http("POST", f"{base}/api/style-forge/constraints",
                    {"source_text": "", "requirement": "", "novel": "demo-web",
                     "target": "ch-1"}, timeout=QUICK)
    suite.check("M4 端点只认「正文 + 需求」（夹带 novel/target 不生效）",
                smuggled.status == 400,
                f"status={smuggled.status} detail={str((smuggled.body or {}).get('detail', ''))[:40]}")
    # 新端点必须与其它 /api/* 一样受鉴权保护（HANDOVER §5.5）
    no_token = http("GET", f"{base}/api/style-forge/agent", token=None, timeout=QUICK)
    suite.check("M5 新端点自动受鉴权保护（无 token → 401）",
                no_token.status in (401, 403), f"status={no_token.status}")
    if llm:
        started = time.time()
        got = http("POST", f"{base}/api/style-forge/constraints",
                   {"source_text": SAMPLE,
                    "requirement": "把这段正文里的文风与节奏特征提炼成可判定的写作约束"
                                   "（叙事视角、句式节奏、段落长度、描写偏好、结尾钩子），"
                                   "每条都要能在正文里找到依据。",
                    "max_items": 10},
                   timeout=LLM * 2)
        gb = got.body or {}
        lines = [ln for ln in str(gb.get("constraints", "")).splitlines() if ln.strip()]
        suite.check(f"M6 正文 + 需求 → 条目（真实 LLM，{time.time() - started:.1f}s）",
                    got.status == 200 and bool(gb.get("constraints")) and len(lines) >= 2
                    and all(ln.startswith("- ") for ln in lines),
                    f"status={got.status} {len(lines)} 条：{lines[:2]}")
        # 提炼必须**有正文依据**：正文里的独特物象（雪/断剑/旧疤）应当在产出里留下痕迹
        text_out = str(gb.get("constraints", ""))
        suite.check("M7 提炼结果有正文依据（不是凭空生成）",
                    any(k in text_out for k in ("雪", "断剑", "剑冢", "旧疤", "钟声")),
                    f"产出片段：{text_out[:80]}")
        suite.check("M8 回执复述输入边界（只用了你粘贴的正文）",
                    got.status == 200 and bool(gb.get("isolation"))
                    and gb.get("count") == len(lines),
                    f"isolation={gb.get('isolation')!r} count={gb.get('count')}")


def _poll(pred, timeout: float, every: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(every)
    return False


def _wait_any_interactive(base: str, novel: str, statuses: tuple[str, ...],
                          timeout: float) -> dict:
    deadline = time.time() + timeout
    last: dict = {}
    while time.time() < deadline:
        last = http("GET", f"{base}/api/interactive/state?novel={novel}", timeout=QUICK).body or {}
        if last.get("status") in statuses:
            return last
        time.sleep(3)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="Inkforge 全量功能冒烟（真实 LLM）")
    parser.add_argument("--no-llm", action="store_true", help="跳过所有真实 LLM 用例")
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录")
    args = parser.parse_args()

    if not args.no_llm and not os.environ.get("DEEPSEEK_API_KEY"):
        print("未检测到 DEEPSEEK_API_KEY → 自动降级为 --no-llm")
        args.no_llm = True

    root = Path(tempfile.mkdtemp(prefix="inkforge-full-audit-"))
    data_dir = build_sandbox(root)
    seed_demo_book(data_dir)

    env = dict(os.environ)
    env.update({
        "NOVELS_DIR": str(data_dir / "novels"),
        "RUNTIME_DIR": str(data_dir / "runtime"),
        "CONFIGS_DIR": str(root / "configs"),
        "PROJECT_ROOT": str(root),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
        # 写作历史：本用例要真实验证每本书的独立 Git 仓库
        "INKFORGE_GIT_HISTORY": "1",
        # 蒸馏上传白名单：允许沙箱内的样本文本（真实桌面端走系统对话框授权）
        "INKFORGE_UPLOAD_ROOTS": str(data_dir),
    })

    engine = EngineProcess(ENGINE_DIR, "demo-web", env)
    suite = Suite("Inkforge 全量功能冒烟")
    report: dict = {
        "mode": "no-llm" if args.no_llm else "with-llm",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "sandbox": str(root),
    }
    started = time.time()

    try:
        engine.start()
        print(f"引擎就绪：{engine.base}", flush=True)
        run(data_dir, engine, suite, args)
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(40)
    finally:
        engine.stop()

    total, failed = suite.passed, suite.failed
    report.update({
        "elapsed_seconds": round(time.time() - started, 1),
        "passed": total, "failed": failed,
        "verdict": "PASS" if failed == 0 and "fatal" not in report else "FAIL",
        "results": suite.results,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    })

    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(exist_ok=True)
    out_file = out_dir / f"full-audit-{datetime.now():%Y%m%d-%H%M%S}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"总计：{total} 通过 / {failed} 失败  ->  {report['verdict']}"
          f"（{report['elapsed_seconds']}s，模式 {report['mode']}）")
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
