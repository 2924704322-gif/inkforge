"""Inkforge 对话创作全流程冒烟驱动（阶段式执行）。

用法：python smoke_chat_flow.py stage1|stage2|stage3
依赖引擎已在 127.0.0.1:8137 运行。
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = "http://127.0.0.1:8137"
NOVEL = "chat-novel"
STATE = Path(__file__).parent / "smoke-state.json"


def req(method: str, path: str, body=None, timeout: int = 240):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "detail": e.read().decode("utf-8")[:400]}


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def stage1():
    """建书 + 对话设计（人物 / 大纲，两次委派）。"""
    st = load_state()
    print("── 1. 建书 ──")
    print(req("POST", "/api/books", {"novel_id": NOVEL}))

    print("── 2. 新建主智能体会话 ──")
    chat = req("POST", f"/api/chats?novel={NOVEL}", {"agent": "master"})
    cid = chat["chat"]["id"]
    st["cid"] = cid
    print("chat_id:", cid)

    print("── 3. 对话①：人物设计（预期委派 character）──")
    t0 = time.time()
    r = req("POST", f"/api/chats/{cid}/send?novel={NOVEL}", {
        "message": (
            "我要创作一部 3 章的都市奇幻短篇《深夜驿站》：外卖骑手陆远在暴雨夜接到了"
            "一个送往『深夜驿站』的神秘订单，发现这个驿站能为逝者传递遗言。请设计主角陆远、"
            "驿站守夜人、以及一个与驿站对抗的反派，给出人物弧光与关系网。"
        )
    })
    print(f"耗时 {time.time()-t0:.0f}s")
    if "detail" in r:
        print("错误:", r["detail"]); return
    for d in r.get("delegations", []):
        print("委派 →", d["label"], "| 指令:", d["instruction"][:60])
    st["character_reply"] = r["reply"]
    print("reply 前 160 字:", r["reply"][:160].replace("\n", " "))

    print("── 4. 对话②：大纲规划（预期委派 outline）──")
    t0 = time.time()
    r = req("POST", f"/api/chats/{cid}/send?novel={NOVEL}", {
        "message": "基于刚才的人物设计，规划 3 章大纲：每章给出标题、核心事件、冲突、结尾钩子与出场角色，并安排一条贯穿伏笔。"
    })
    print(f"耗时 {time.time()-t0:.0f}s")
    if "detail" in r:
        print("错误:", r["detail"]); return
    for d in r.get("delegations", []):
        print("委派 →", d["label"], "| 指令:", d["instruction"][:60])
    st["outline_reply"] = r["reply"]
    print("reply 前 160 字:", r["reply"][:160].replace("\n", " "))

    save_state(st)
    print("stage1 完成 ✓")


def stage2():
    """落盘大纲与人物 → 建章节骨架 → 改稿提案让写手创作 → 接受。"""
    st = load_state()
    cid = st["cid"]

    print("── 5. 人物设计写入资料库 ──")
    r = req("PUT", f"/api/settings/doc?novel={NOVEL}", {
        "rel": "settings/characters/深夜驿站-人物设计.md",
        "content": st["character_reply"],
    })
    print(r)

    print("── 6. 大纲写入（pipeline 大纲接口，带 frontmatter）──")
    volumes = {
        "book_title": "深夜驿站",
        "theme": "为逝者传递遗言的驿站，照见生者的执念",
        "volumes": [{
            "volume": 1, "title": "第一卷", "depends_on": [],
            "chapters": [
                {"chapter": 1, "title": "暴雨订单",
                 "outline": "陆远暴雨夜接到送往深夜驿站的神秘订单，包裹里是一封无法投递的信；他发现地址不存在于任何地图，却被引到了驿站门口。冲突：订单与女儿医院病危通知的时间冲突。结尾钩子：守夜人说『这封信，是写给你的』。",
                 "characters": ["陆远", "守夜人"]},
                {"chapter": 2, "title": "遗言",
                 "outline": "陆远得知驿站能让逝者传话，见识了一场遗言传递；反派『收割者』出现，专门截取遗言中的秘密勒索生者。冲突：陆远的女儿的病，是否可以用一次传递换奇迹。结尾钩子：收割者亮出陆远亡父的照片。",
                 "characters": ["陆远", "守夜人", "收割者"]},
                {"chapter": 3, "title": "最后一单",
                 "outline": "陆远与守夜人联手对抗收割者，代价是驿站关闭、陆远永远失去与亡父对话的机会。结尾：女儿痊愈，陆远收到父亲生前留下的最后一句话——他早就知道这一切。",
                 "characters": ["陆远", "守夜人", "收割者"]},
            ],
        }],
    }
    r = req("PUT", f"/api/outline?novel={NOVEL}", volumes)
    print(r)

    print("── 7. 建第 1 章骨架 ──")
    r = req("POST", f"/api/chapters?novel={NOVEL}", {"title": "暴雨订单"})
    print(r)

    print("── 8. 改稿提案：写手子智能体创作全文（真实 LLM，约 1-2 分钟）──")
    t0 = time.time()
    r = req("POST", f"/api/chats/{cid}/propose?novel={NOVEL}", {
        "instruction": (
            "这是第 1 章的骨架。请按大纲与人物设定，创作完整正文：目标 2200 字左右；"
            "暴雨夜的氛围要足；『深夜驿站』首次出场要有仪式感；结尾落在守夜人那句『这封信，是写给你的』。"
        ),
        "target": {"kind": "chapter", "key": "ch-1"},
        "agent": "prose",
    }, timeout=300)
    print(f"耗时 {time.time()-t0:.0f}s")
    if "_status" in r:
        print("错误:", r); return
    print(f"提案：+{r['additions']} / −{r['deletions']}，{len(r['hunks'])} 个变更块")
    st["proposal"] = {"id": r["id"], "chat_id": cid}

    print("── 9. 接受提案（写入事实源）──")
    r = req("POST", f"/api/chats/{cid}/proposals/{r['id']}/decide?novel={NOVEL}", {"decision": "accept"})
    print("状态:", r.get("status"), "|", r.get("statusMessage"))
    save_state(st)
    print("stage2 完成 ✓")


def stage3():
    """对话审校（委派 review）→ 导出 → 汇总。"""
    st = load_state()
    cid = st["cid"]

    print("── 10. 对话审校第 1 章（预期委派 review，携带主上下文）──")
    t0 = time.time()
    r = req("POST", f"/api/chats/{cid}/send?novel={NOVEL}", {
        "message": "审校第 1 章正文：给出四维评分与问题清单（含原文引用与修改建议）。",
        "target": {"kind": "chapter", "key": "ch-1"},
    })
    print(f"耗时 {time.time()-t0:.0f}s")
    if "detail" in r:
        print("错误:", r["detail"]); return
    for d in r.get("delegations", []):
        print("委派 →", d["label"])
        print("审校产出前 300 字:", d["output"][:300])
    st["review"] = r["reply"][:200]

    print("── 11. 导出 TXT ──")
    import urllib.request
    try:
        with urllib.request.urlopen(f"{BASE}/api/export?novel={NOVEL}&format=txt", timeout=60) as resp:
            data = resp.read()
        print(f"TXT 导出成功：{len(data)} bytes")
    except urllib.error.HTTPError as e:
        print("导出失败:", e.code, e.read().decode("utf-8")[:120])

    print("── 12. 终态 ──")
    books = req("GET", "/api/books")
    for b in books["books"]:
        if b["novel_id"] == NOVEL:
            print(f"《{b['title']}》 {b['approved']}/{b['chapters']} 章")
    st.pop("proposal", None)
    save_state(st)
    print("stage3 完成 ✓ 全流程结束")


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "stage1"
    {"stage1": stage1, "stage2": stage2, "stage3": stage3}[stage]()
