import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from smoke_test import TOKEN, EngineProcess, build_sandbox, http

root = Path(tempfile.mkdtemp(prefix="inkforge-interactive-e2e-"))
data = build_sandbox(root)
env = dict(os.environ)
env.update({"NOVELS_DIR": str(data/"novels"), "RUNTIME_DIR": str(data/"runtime"),
            "CONFIGS_DIR": str(root/"configs"), "INKFORGE_ENGINE_TOKEN": TOKEN,
            "INKFORGE_GIT_HISTORY": "1"})
NID = "e2e-interactive"
eng = EngineProcess(Path.cwd(), NID, env)
res = []
def chk(name, ok, ev=""):
    res.append((name, bool(ok), str(ev)[:180]))
    print(("[PASS] " if ok else "[FAIL] ") + name + ("  -- " + str(ev)[:180] if ev else ""))

def poll(fn, ok_fn, timeout, what):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if ok_fn(last):
            return last, round(time.time()-t0, 1)
        time.sleep(4)
    return last, -round(time.time()-t0, 1)

try:
    eng.start()
    b = eng.base
    # 1) 建互动书（不要大纲）
    r = http("POST", f"{b}/api/books", body={"novel_id": NID, "mode": "interactive"})
    chk("1 create interactive book", r.status == 200, r.body)
    # 2) 世界观 Demo（真实 LLM）
    r = http("POST", f"{b}/api/demo?novel={NID}", body={"brief": "清末民初江南古镇的历史悬疑：主角是开茶馆的寡妇，靠一本旧账册翻查陈年旧案；严禁出现奇幻、异能、穿越元素。", "chapters": 20}, timeout=60)
    chk("2 demo started", r.status == 200, r.body)
    snap, secs = poll(lambda: http("GET", f"{b}/api/demo?novel={NID}").body or {},
                      lambda s: s.get("status") in ("done", "error"), 600, "demo")
    chk("3 demo done (real LLM)", snap.get("status") == "done", f"{secs}s err={snap.get('error')}")
    demo = snap.get("demo") or {}
    chk("4 demo has worldview, no outline", bool(demo.get("worldview")), f"worldview={len(demo.get('worldview') or [])}")
    dir_plot = (demo.get("synopsis") or "").strip()
    dir_plot2 = (demo.get("overview") or "").strip()
    blob = json.dumps(demo, ensure_ascii=False)
    hits = [k for k in ("茶馆", "寡妇", "账册", "江南", "古镇") if k in blob]
    chk("4a ★ 设定 Demo 确实照作者 brief 走（标记词命中）", len(hits) >= 2,
        f"命中={hits} title={demo.get('book_title')!r}")
    chk("4b demo has NO plot direction", not dir_plot and not dir_plot2,
        f"synopsis={len(dir_plot)} chars, overview={len(dir_plot2)} chars")
    r = http("POST", f"{b}/api/demo/confirm?novel={NID}", body={}, timeout=120)
    chk("5 demo confirmed into settings/", r.status == 200, r.body)
    outline_md = (data/"novels"/NID/"settings"/"outline.md")
    chk("6 NO outline.md created (interactive mode)", not outline_md.exists(), str(outline_md.exists()))
    ov = (data / "novels" / NID / "settings" / "story-overview.md")
    ovtxt = ov.read_text(encoding="utf-8") if ov.exists() else ""
    chk("6b story-overview.md 不含剧情段落",
        "故事梗概" not in ovtxt and "整体概要" not in ovtxt,
        ovtxt[:100].replace("\n", " "))
    st0 = http("GET", f"{b}/api/status?novel={NID}").body or {}
    chk("6c 确认设定后没有自动跑章节流水线", not st0.get("started"),
        f"started={st0.get('started')} pending={bool(st0.get('pending'))}")
    # 3) 互动链路
    r_iso1 = http("POST", f"{b}/api/start?novel={NID}", body={"brief": "隔离测试", "chapters": 3})
    chk("7a isolation: 流水线拒绝互动书", r_iso1.status == 400,
        f"status={r_iso1.status} {str(r_iso1.body)[:120]}")
    http("POST", f"{b}/api/books", body={"novel_id": "e2e-pipeline-iso", "mode": "pipeline"})
    r_iso2 = http("POST", f"{b}/api/interactive/start?novel=e2e-pipeline-iso")
    chk("7b isolation: 互动创作拒绝自由创作书", r_iso2.status == 400,
        f"status={r_iso2.status} {str(r_iso2.body)[:120]}")
    r = http("POST", f"{b}/api/interactive/start?novel={NID}", body=None, timeout=60)
    chk("7 interactive start", r.status == 200, r.body)
    st, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                    lambda s: s.get("status") in ("awaiting_choice", "error"), 900, "cards")
    chk("8 cards generated (real LLM)", st.get("status") == "awaiting_choice" and st.get("cards"),
        f"{secs}s status={st.get('status')} cards={len(st.get('cards') or [])} err={st.get('error')}")
    cards = st.get("cards") or []
    chk("8b 三张卡都是可执行的走向（都有 outline）",
        len(cards) == 3 and all((c.get("outline") or "").strip() for c in cards),
        f"cards={len(cards)}")
    # 本次回归重点：走**自拟卡**，验证后续正文按作者原话推进
    CUSTOM = ("本章唯一场景：雨夜的钟表铺，主角在修一只停摆的怀表；"
              "必须出现一只停在窗台的乌鸦；结尾主角把怀表扔进熔炉。"
              "不得出现战斗场面。")
    r = http("POST", f"{b}/api/interactive/choose?novel={NID}",
             body={"card_id": "custom", "custom_text": CUSTOM}, timeout=60)
    chk("9 choose CUSTOM card", r.status == 200, r.body)
    st, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                    lambda s: s.get("status") in ("awaiting_review", "error"), 900, "draft")
    chk("10 chapter written + review (real LLM)", st.get("status") == "awaiting_review" and st.get("draft"),
        f"{secs}s status={st.get('status')} err={st.get('error')}")
    d = st.get("draft") or {}
    rv = d.get("review") or {}
    chk("11 editor scores present", all(k in rv for k in ("consistency", "plot", "continuity", "prose")),
        json.dumps(rv, ensure_ascii=False)[:150])
    text = str(d.get("draft_text") or "")
    hits = [k for k in ("乌鸦", "熔炉", "怀表") if k in text]
    chk("11b ★ 正文确实按自拟走向推进（关键词命中>=2）", len(hits) >= 2,
        f"命中={hits} 全len={len(text)} 开头={text[:36]!r}")
    r = http("POST", f"{b}/api/interactive/decision?novel={NID}", body={"action": "approve", "feedback": "", "revision_mode": "targeted"}, timeout=120)
    chk("12 approve chapter", r.status == 200, r.body)
    st2, secs = poll(lambda: http("GET", f"{b}/api/interactive/state?novel={NID}").body or {},
                     lambda s: (s.get("approved_count") or 0) >= 1, 300, "commit")
    chk("13 chapter committed (approved_count>=1)", (st2.get("approved_count") or 0) >= 1,
        f"{secs}s approved={st2.get('approved_count')} status={st2.get('status')}")
    chk("14 does NOT auto-write chapter 2", st2.get("status") in ("awaiting_choice", "generating_cards", "committing"),
        f"status={st2.get('status')} chapter={st2.get('chapter')}")
finally:
    eng.stop()

fails = [x for x in res if not x[1]]
print(f"TOTAL {len(res)-len(fails)} passed / {len(fails)} failed")
sys.exit(1 if fails else 0)
