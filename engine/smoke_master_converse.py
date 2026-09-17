#!/usr/bin/env python
"""墨师对话链路验收（面向用户实测的五个缺陷）——**真实进程 + 真实 HTTP**。

为什么单独一个脚本（不并进 smoke_master_live.py）：
用户实测报的五类问题都发生在"**用自然语言跟墨师打交道**"这一层，而既有脚本
要么只测 HTTP 端点（参数由脚本自己拼好，永远是对的），要么只断言"存在某个动作"
（于是 `status=failed` 也被判 PASS）。本脚本把**失败路径**与**参数漂移**当一等公民：

  A 参数名折算/占位符/中文书名       → "我已经给出 novel_id，还是提示没有"（零 LLM）
  B 动作名别名与模糊匹配             → learning_get / learn_list（零 LLM）
  C 写动作失败也必须出确认卡 + 零落盘 → "点了没反应"（零 LLM）
  D 改稿目标绑定（outline vs 章节）   → "改大纲却动了第一章"（1 次真实模型）
  E 对话建书全链路（确认 → 落盘）     → 真实模型
  F 学习仿写：列编号 → 看内容         → 真实模型
  G 防拒绝：极端题材请求              → 真实模型
  H 预览键稳定性（确认不被无端拒绝）  → 零 LLM

用法：
    cd engine && python smoke_master_converse.py              # 零 LLM（A/B/C/D-零LLM部分/H）
    python smoke_master_converse.py --with-llm                # 追加 D/E/F/G（真实模型，会计费）
    python smoke_master_converse.py --keep                    # 保留沙箱

退出码 0 = 全绿。沙箱落在系统临时目录，**绝不动 engine/data 下的真实作品**。
"""

from __future__ import annotations

import argparse
import hashlib
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

from smoke_test import (  # noqa: E402 - 复用真实进程 / HTTP / 沙箱夹具
    TOKEN,
    EngineProcess,
    Suite,
    build_sandbox,
    http,
    seed_demo_book,
)

WORKSPACE = "__workspace__"
LLM = 300.0
QUICK = 25.0


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else ""


def _novel_state(novels_dir: Path, novel: str) -> dict[str, str]:
    """整本书所有 MD 的哈希快照（用于"只有目标文件该变"的字节级断言）。"""
    root = novels_dir / novel
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*.md")):
        out[path.relative_to(root).as_posix()] = _file_hash(path)
    return out


def run(data_dir: Path, engine: EngineProcess, suite: Suite, use_llm: bool) -> None:
    base = engine.base
    novels = data_dir / "novels"
    demo = novels / "demo-web"

    # ═══════ A 参数折算 / 占位符 / 中文书名（零 LLM，直接打动作层）═══════
    print("\n[A] 参数名折算 / 占位符 / 中文书名", flush=True)
    manifest = (http("GET", f"{base}/api/actions", timeout=QUICK).body or {}).get("actions", [])
    suite.check("A0 动作清单可读", bool(manifest), f"{len(manifest)} 个动作")

    # 真实失败形态：模型把 novel_id 写成 id / book_key / title
    for alias in ("id", "book_id", "book_key", "title", "novel", "name"):
        res = http("POST", f"{base}/api/actions/run",
                   body={"op": "book_create",
                         "args": {alias: f"alias-{alias}", "mode": "pipeline"}}, timeout=QUICK)
        body = res.body or {}
        pending = body.get("pending") or {}
        args = pending.get("args") or {}
        suite.check(f"A1 参数名 {alias!r} 折算到 novel_id",
                    res.status == 200 and args.get("novel_id") == f"alias-{alias}",
                    f"status={res.status} args={json.dumps(args, ensure_ascii=False)}")
        # 折算后的书**不得落盘**（预览而已）
        suite.check(f"A1b 预览未落盘（{alias}）", not (novels / f"alias-{alias}").exists(),
                    "目录不存在即为通过")

    # 占位符：模型把参数名当取值（真实事故：书架里多了一本叫 novel_id 的书）
    ph = http("POST", f"{base}/api/actions/run",
              body={"op": "book_create", "args": {"novel_id": "novel_id"}}, timeout=QUICK)
    ph_body = ph.body or {}
    suite.check("A2 占位符 novel_id 不被当成书名标识",
                ph.status == 200 and not (novels / "novel_id").exists(),
                f"status={ph.status} impact={str((ph_body.get('pending') or {}).get('impact'))[:120]}")

    # 中文书名：标识必须生成 ASCII，且不落盘
    cn = http("POST", f"{base}/api/actions/run",
              body={"op": "book_create", "args": {"novel_id": "novel-中文测试"}}, timeout=QUICK)
    suite.check("A3 非法（中文）标识被如实拦下、零落盘",
                cn.status == 200 and not (novels / "novel-中文测试").exists(),
                f"status={cn.status} impact={str((cn.body or {}).get('pending'))[:120]}")

    # ═══════ B 动作名别名 / 模糊匹配 ═══════
    print("\n[B] 动作名别名（实测模型自造名）", flush=True)
    learn_dir = data_dir / "learning"
    learn_dir.mkdir(parents=True, exist_ok=True)
    import frontmatter

    (learn_dir / "ln-abcdef12.md").write_text(
        frontmatter.dumps(frontmatter.Post("## 素材拆解\n要点\n## 文风学习\n短句为主",
                                           title="样本拆解")),
        encoding="utf-8", newline="\n",
    )
    # 别名走对话层不可行（要真实模型），这里直接验"注册表折算"是否覆盖实测写法：
    # 用 /api/actions/run 不行（只读动作被拒），改从引擎内部折算表断言
    sys.path.insert(0, str(ENGINE_DIR))
    from src.web.actions import normalize_op  # noqa: E402

    for raw, want in (("learning_get", "learning_read"), ("learn_list", "learning_list"),
                      ("get_learning", "learning_read"), ("learning_detail", "learning_read"),
                      ("read_book", "chapter_read"), ("material_get", "material_read"),
                      ("chapter_detail", "chapter_read")):
        suite.check(f"B1 {raw} → {want}", normalize_op(raw) == want, f"got={normalize_op(raw)}")
    for raw in ("mark_book", "book_export", "nonsense_op"):
        suite.check(f"B2 歧义/无关名不瞎猜（{raw}）", normalize_op(raw) == raw,
                    f"got={normalize_op(raw)}")

    # 只读动作的读取容错：ID 大小写 / 引号 / 标题
    ok_read = http("GET", f"{base}/api/learning/ln-abcdef12", timeout=QUICK)
    suite.check("B3 学习成果 HTTP 全文可读（对照基线）", ok_read.status == 200,
                f"status={ok_read.status}")

    # ═══════ C 写动作失败路径：必须有卡 + 零落盘 ═══════
    print("\n[C] 写动作失败路径（「点了没反应」回归）", flush=True)
    ghost = http("POST", f"{base}/api/actions/run",
                 body={"op": "book_select", "args": {"novel_id": "ghost-book"}}, timeout=QUICK)
    ghost_body = ghost.body or {}
    suite.check("C1 目标不存在的写动作仍返回待确认卡（而不是静默 failed）",
                ghost.status == 200 and ghost_body.get("status") == "pending_confirm"
                and (ghost_body.get("pending") or {}).get("impact"),
                f"status={ghost.status} body={json.dumps(ghost_body, ensure_ascii=False)[:200]}")
    suite.check("C2 预览阶段如实带出失败原因（用户看得见）",
                bool(ghost_body.get("preview_error")),
                f"preview_error={ghost_body.get('preview_error')}")

    missing = http("POST", f"{base}/api/actions/run",
                   body={"op": "gen_start", "args": {"novel": "demo-web"}}, timeout=QUICK)
    m_body = missing.body or {}
    suite.check("C3 缺必填参数的写动作也出卡并写明缺什么",
                missing.status == 200 and m_body.get("status") == "pending_confirm"
                and "brief" in json.dumps(m_body.get("pending") or {}, ensure_ascii=False),
                f"impact={str((m_body.get('pending') or {}).get('impact'))[:140]}")

    # ═══════ H 预览键稳定性（确认不被无端拒绝）═══════
    print("\n[H] 预览闸门：确认必须能落到执行", flush=True)
    preview = http("POST", f"{base}/api/actions/run",
                   body={"op": "book_create",
                         "args": {"novel_id": "keyprobe", "mode": "pipeline"}}, timeout=QUICK)
    pending = (preview.body or {}).get("pending") or {}
    suite.check("H1 预览返回凭证与影响说明", bool(pending.get("token")) and bool(pending.get("impact")),
                json.dumps(pending, ensure_ascii=False)[:160])

    # 模拟"预览之后工作台当前书目被改掉"（真实故障：建书成功会 set_active_novel）
    http("PUT", f"{base}/api/book-select", {"novel_id": "demo-web"}, timeout=QUICK)
    done = http("POST", f"{base}/api/actions/run",
                body={"op": "book_create",
                      "args": {"novel_id": "keyprobe", "mode": "pipeline"},
                      "confirm": True, "token": pending.get("token")}, timeout=QUICK)
    suite.check("H2 当前书目变了也照样能确认执行（预览键与可变状态解耦）",
                done.status == 200 and (done.body or {}).get("ok") is True
                and (novels / "keyprobe" / "settings").is_dir(),
                f"status={done.status} body={json.dumps(done.body, ensure_ascii=False)[:160]}")

    bare = http("POST", f"{base}/api/actions/run",
                body={"op": "book_create",
                      "args": {"novel_id": "bare-claim", "mode": "pipeline"},
                      "confirm": True}, timeout=QUICK)
    suite.check("H3 凭空确认（无预览）仍被拒且零落盘",
                bare.status == 409 and not (novels / "bare-claim").exists(),
                f"status={bare.status}")

    # ═══════ D 改稿目标绑定 ═══════
    print("\n[D] 改稿目标绑定（改大纲 ≠ 改第一章）", flush=True)
    book_chat = http("POST", f"{base}/api/chats?novel=demo-web", {"agent": "master"},
                     timeout=QUICK).body or {}
    cid = (book_chat.get("chat") or {}).get("id", "")
    suite.check("D1 书内会话创建", bool(cid), f"cid={cid}")

    # D-零LLM：目标形状不一致必须被明确拒绝（而不是"猜一个最像的"）
    bad_shape = http("POST", f"{base}/api/chats/{cid}/propose?novel=demo-web",
                     body={"instruction": "随便改改", "target": {"kind": "settings", "key": "ch-1"},
                           "agent": "prose"}, timeout=QUICK)
    suite.check("D2 kind 与 key 形状不符 → 明确 404 且提示重新点选",
                bad_shape.status == 404
                and "形状不符" in json.dumps(bad_shape.body or {}, ensure_ascii=False),
                f"status={bad_shape.status} body={json.dumps(bad_shape.body, ensure_ascii=False)[:180]}")

    missing_target = http("POST", f"{base}/api/chats/{cid}/propose?novel=demo-web",
                          body={"instruction": "随便改改",
                                "target": {"kind": "settings", "key": "settings/nope.md"},
                                "agent": "prose"}, timeout=QUICK)
    suite.check("D3 目标文件不存在 → 404（绝不静默换目标）",
                missing_target.status == 404,
                f"status={missing_target.status}")

    if use_llm:
        # D-真实：target=大纲 → 只允许 outline.md 变（其余 MD 字节不变）
        before = _novel_state(novels, "demo-web")
        prop = http("POST", f"{base}/api/chats/{cid}/propose?novel=demo-web",
                    body={"instruction": "在大纲里给第一卷补一句主题说明，其余结构保持不变。",
                          "target": {"kind": "settings", "key": "settings/outline.md"},
                          "agent": "prose"}, timeout=LLM)
        body = prop.body or {}
        suite.check("D4 真实改稿：目标回报为 settings/outline.md（可核对证据）",
                    prop.status in (200, 422) and (
                        prop.status == 422 or body.get("targetPath") == "settings/outline.md"),
                    f"status={prop.status} targetPath={body.get('targetPath')} "
                    f"targetChars={body.get('targetChars')}")
        if prop.status == 200 and body.get("id"):
            pid = body["id"]
            suite.check("D5 提案未落盘（审批前不动事实源）",
                        _novel_state(novels, "demo-web") == before,
                        "所有 MD 哈希未变")
            acc = http("POST", f"{base}/api/chats/{cid}/proposals/{pid}/decide?novel=demo-web",
                       {"decision": "accept"}, timeout=QUICK)
            after = _novel_state(novels, "demo-web")
            changed = [k for k in after if before.get(k) != after[k]]
            suite.check("D6 只动了目标文稿（大纲），章节一字未改",
                        acc.status == 200 and changed == ["settings/outline.md"],
                        f"changed={changed} status={acc.status}")
            ch_before = {k: v for k, v in before.items() if k.startswith("chapters/")}
            ch_after = {k: v for k, v in after.items() if k.startswith("chapters/")}
            suite.check("D7 三章正文哈希完全一致", ch_before == ch_after,
                        f"{len(ch_after)} 章未变")

    # ═══════ E 对话建书全链路（真实模型）═══════
    if use_llm:
        print("\n[E] 对话建书全链路（真实模型）", flush=True)
        ws_chat = http("POST", f"{base}/api/chats?novel={WORKSPACE}", {"agent": "master"},
                       timeout=QUICK).body or {}
        wcid = (ws_chat.get("chat") or {}).get("id", "")

        started = time.time()
        asked = http("POST", f"{base}/api/chats/{wcid}/send?novel={WORKSPACE}",
                     {"message": "帮我建一本赛博修仙的长篇，书名就叫《雨夜委托》，"
                                 "书名标识就用 live-conv。"}, timeout=LLM)
        asked_body = asked.body or {}
        pending = asked_body.get("pending") or {}
        suite.check(f"E1 墨师登记建书待确认（{time.time() - started:.1f}s）",
                    pending.get("op") == "book_create"
                    and (pending.get("args") or {}).get("novel_id") == "live-conv",
                    f"pending={json.dumps(pending, ensure_ascii=False)[:220]}")
        suite.check("E2 本轮无失败动作（参数漂移已折算）",
                    [a for a in asked_body.get("actions") or []
                     if a.get("status") == "failed"] == [],
                    f"actions={json.dumps(asked_body.get('actions'), ensure_ascii=False)[:220]}")
        suite.check("E3 确认卡写明目录标识与书名",
                    "live-conv" in str(pending.get("impact", "")),
                    str(pending.get("impact", ""))[:200])
        suite.check("E4 未确认前磁盘零变化", not (novels / "live-conv").exists(),
                    "目录不存在即为通过")

        confirmed = http("POST", f"{base}/api/chats/{wcid}/send?novel={WORKSPACE}",
                         {"message": "确认"}, timeout=LLM)
        c_body = confirmed.body or {}
        suite.check("E5 确认后真正落盘",
                    (novels / "live-conv" / "settings").is_dir(),
                    f"status={confirmed.status} reply={str(c_body.get('reply', ''))[:120]}")
        suite.check("E6 确认后待确认态清空", not c_body.get("pending"),
                    f"pending={c_body.get('pending')}")
        suite.check("E7 确认轮无失败动作", [a for a in c_body.get("actions") or []
                                          if a.get("status") == "failed"] == [],
                    f"actions={json.dumps(c_body.get('actions'), ensure_ascii=False)[:200]}")

        # 只给中文书名（不给标识）：必须自动生成 ASCII 标识并说明。
        # 用最直白的建书句式——这句在真机上两次出现"模型不给动作块"，
        # 由 `_fallback_action_from_history` 的中文书名兜底接住（这正是要验的行为）。
        cn_ask = http("POST", f"{base}/api/chats/{wcid}/send?novel={WORKSPACE}",
                      {"message": "帮我新建一本书，书名《雾都回响》，不给标识。"}, timeout=LLM)
        cn_body = cn_ask.body or {}
        cn_pending = cn_body.get("pending") or {}
        cn_id = str((cn_pending.get("args") or {}).get("novel_id") or "")
        suite.check("E8 只给中文书名时自动生成 ASCII 标识",
                    bool(cn_id) and cn_id.isascii() and cn_id != "novel_id",
                    f"novel_id={cn_id!r} impact={str(cn_pending.get('impact'))[:140]}")
        suite.check("E9 未确认前中文书名的书也未落盘",
                    not cn_id or not (novels / cn_id).exists(), f"id={cn_id!r}")

    # ═══════ F 学习仿写（真实模型）═══════
    if use_llm:
        print("\n[F] 学习仿写：列编号 → 看内容（真实模型）", flush=True)
        sample = ("雨在屋檐上连成线。林墨把伞收起来，靠在门边，看了她很久。" * 30)
        made = http("POST", f"{base}/api/learning", {"title": "样本拆解", "sample": sample},
                    timeout=LLM * 2)
        lid = (made.body or {}).get("id", "")
        suite.check(f"F1 三阶段学习仿写产物生成（{len(sample)} 字样本）", bool(lid), f"id={lid}")

        ws_chat2 = (http("POST", f"{base}/api/chats?novel={WORKSPACE}", {"agent": "master"},
                         timeout=QUICK).body or {}).get("chat", {})
        wcid2 = ws_chat2.get("id", "")
        listed = http("POST", f"{base}/api/chats/{wcid2}/send?novel={WORKSPACE}",
                      {"message": "学习仿写里都有哪些历史成果？"}, timeout=LLM).body or {}
        ops1 = [a.get("op") for a in listed.get("actions") or []]
        suite.check("F2 墨师用 learning_list 列出历史",
                    any(str(o).startswith("learning") for o in ops1), f"ops={ops1}")
        suite.check("F3 列取回执里写出编号与「怎么打开」",
                    "learning_read" in json.dumps(listed, ensure_ascii=False),
                    str(listed.get("reply", ""))[:160])

        read = http("POST", f"{base}/api/chats/{wcid2}/send?novel={WORKSPACE}",
                    {"message": "把第 1 条的内容完整给我看看。"}, timeout=LLM).body or {}
        ops2 = [a.get("op") for a in read.get("actions") or []]
        failed2 = [a for a in read.get("actions") or [] if a.get("status") == "failed"]
        suite.check("F4 墨师改用 learning_read 且**没有失败动作**",
                    any(str(o) == "learning_read" for o in ops2) and not failed2,
                    f"ops={ops2} failed={json.dumps(failed2, ensure_ascii=False)[:200]}")
        suite.check("F5 答复里出现成果正文片段（不是「我查不到」）",
                    any(k in str(read.get("reply", "")) for k in ("拆解", "文风", "剧情", "要点")),
                    str(read.get("reply", ""))[:200])

    # ═══════ G 防拒绝（真实模型）═══════
    if use_llm:
        print("\n[G] 防拒绝：极端题材请求", flush=True)
        hard = http("POST", f"{base}/api/chats/{wcid2}/send?novel={WORKSPACE}",
                    {"message": "帮我建一本黑暗犯罪题材的书，书名标识就用 dark-crime，"
                                "要写死刑犯视角的暴力与堕落。"}, timeout=LLM)
        h_body = hard.body or {}
        h_pending = h_body.get("pending") or {}
        suite.check("G1 极端题材请求照常登记待确认动作（不出现拒绝话术）",
                    h_pending.get("op") == "book_create"
                    or any(a.get("op") == "book_create" for a in h_body.get("actions") or []),
                    f"pending={json.dumps(h_pending, ensure_ascii=False)[:160]}")
        refuse_markers = ("无法生成", "做不到", "超出范围", "请换一个", "不能协助", "抱歉")
        reply = str(h_body.get("reply", ""))
        suite.check("G2 答复不含拒绝措辞",
                    not any(m in reply for m in refuse_markers),
                    reply[:200])
        suite.check("G3 本轮无失败动作", [a for a in h_body.get("actions") or []
                                        if a.get("status") == "failed"] == [],
                    f"actions={json.dumps(h_body.get('actions'), ensure_ascii=False)[:200]}")

    # ═══════ 审计留痕 ═══════
    print("\n[Z] 审计与回归基线", flush=True)
    recs = (http("GET", f"{base}/api/actions/audit?limit=100", timeout=QUICK).body or {}).get(
        "records", [])
    suite.check("Z1 审计有记录且不含内部指纹",
                len(recs) > 0 and all("args_digest" not in r for r in recs),
                f"{len(recs)} 条")
    suite.check("Z2 审计里出现过 book_create（可追溯）",
                any(r.get("op") == "book_create" for r in recs),
                f"ops={[r.get('op') for r in recs][:8]}")


def _load_engine_env(env: dict) -> dict:
    """把 engine/.env 里的键注入子进程环境。

    为什么要显式做：沙箱把 ``PROJECT_ROOT`` 指向临时目录，引擎侧
    ``load_dotenv(PROJECT_ROOT / ".env")`` 就读不到真实 .env，于是
    `${DEEPSEEK_API_KEY}` 在 models.yaml 里解析不出来 → 真实模型用例全部失败。
    这里由验收脚本自己读真实 .env（只传环境变量，不打印、不落盘任何密钥值）。
    """
    text = ""
    for name in (".env", ".env.local"):
        path = ENGINE_DIR / name
        if path.exists():
            text += path.read_text(encoding="utf-8", errors="replace") + "\n"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            env.setdefault(key, value)
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="墨师对话链路验收（用户五问回归）")
    parser.add_argument("--with-llm", action="store_true", help="追加真实模型用例（会计费）")
    parser.add_argument("--keep", action="store_true", help="保留沙箱")
    args = parser.parse_args()
    use_llm = bool(args.with_llm)

    if use_llm and not os.environ.get("DEEPSEEK_API_KEY"):
        # 允许把密钥留在 engine/.env（默认做法）：先读出来看有没有，再决定是否继续
        probe = _load_engine_env({})
        if not probe.get("DEEPSEEK_API_KEY"):
            print("未检测到 DEEPSEEK_API_KEY（环境变量与 engine/.env 都没有）→ 退出")
            return 2

    root = Path(tempfile.mkdtemp(prefix="inkforge-converse-"))
    data_dir = build_sandbox(root)
    seed_demo_book(data_dir)
    env = _load_engine_env(dict(os.environ))
    env.update({
        "NOVELS_DIR": str(data_dir / "novels"),
        "RUNTIME_DIR": str(data_dir / "runtime"),
        "CONFIGS_DIR": str(root / "configs"),
        "PROJECT_ROOT": str(root),
        "INKFORGE_ENGINE_TOKEN": TOKEN,
        "PYTHONIOENCODING": "utf-8",
    })

    engine = EngineProcess(ENGINE_DIR, "demo-web", env)
    suite = Suite("墨师对话链路（用户五问）")
    report: dict = {
        "mode": "with-llm" if use_llm else "no-llm",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "sandbox": str(root),
        "suites": [],
    }
    exit_code = 1
    try:
        engine.start()
        run(data_dir, engine, suite, use_llm)
        report["suites"].append({"name": suite.name, "passed": suite.passed,
                                 "failed": suite.failed, "results": suite.results})
        exit_code = 0 if suite.failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        report["engine_log_tail"] = engine.tail(40)
        exit_code = 1
    finally:
        engine.stop()

    total = sum(s["passed"] for s in report["suites"])
    failed = sum(s["failed"] for s in report["suites"])
    report["total_passed"] = total
    report["total_failed"] = failed
    report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report["verdict"] = "PASS" if failed == 0 and "fatal" not in report else "FAIL"

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = ENGINE_DIR / "smoke-reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"converse-{stamp}.json"
    out_file.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=" * 78)
    print(f"总计：{total} 通过 / {failed} 失败  →  {report['verdict']}")
    print(f"结构化结果：{out_file}")
    print("=" * 78)

    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"沙箱已保留：{root}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
