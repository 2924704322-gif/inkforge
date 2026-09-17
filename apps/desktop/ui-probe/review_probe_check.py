"""Inkforge 人审关卡布局探针 —— 真实浏览器几何量测。

流程：起静态服务 → 真实 Chromium 打开 review.html（挂载真实 App.vue + 内存桥桩）
→ 量测三栏与审阅卡内各盒子的几何 → 输出 JSON + 截图。

用法：python ui-probe/review_probe_check.py
退出码：0 全部断言通过；1 有失败。

背景：三栏宽度由 localStorage 的 inkforge.w.editor / inkforge.w.tree 持久化，
所以「同一个窗口尺寸」也可能出现不同的挤压结果 —— 因此对每个视口都跑三种
分栏宽度场景。
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DIST = ROOT.parent / "out" / "ui-probe"
SHOTS = ROOT.parent / "out" / "ui-probe-shots"

# 1600x1000 = apps/desktop/src/main/index.ts 的 BrowserWindow 默认尺寸；其余为常见屏幕
VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1440, "height": 900},
    {"width": 1600, "height": 1000},
    {"width": 1920, "height": 1080},
]

# 三栏宽度（App.vue 的可拖拽分栏，落 localStorage）
# auto = 不写 localStorage，用 App.vue 的默认值（editor 470 / tree 288）
SCENARIOS = [
    {"name": "auto", "editor": None, "tree": None},
    {"name": "editor780", "editor": 780, "tree": 288},
    {"name": "editor780-tree480", "editor": 780, "tree": 480},
]

VISIBLE_JS = """
() => {
  const streamEl = document.querySelector('.stream');
  const sb = streamEl.getBoundingClientRect();
  const probe = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { y: -1, h: 0, inView: false };
    const r = el.getBoundingClientRect();
    return {
      y: Math.round(r.y), h: Math.round(r.height),
      inView: r.top >= sb.top - 2 && r.top <= sb.bottom,
    };
  };
  return {
    stream: { top: Math.round(sb.top), bottom: Math.round(sb.bottom), scrollTop: Math.round(streamEl.scrollTop) },
    chip: (document.querySelector('.chat-head .chip') || {}).textContent?.trim() ?? '',
    scoreRow: probe('.score-row'),
    issueBox: probe('.issue-box'),
    draft: probe('.draft-scroll-box'),
    decision: probe('.decision-area'),
    outlineScroll: probe('.outline-scroll'),
  };
}
"""

FLOW_JS = """
() => {
  const r = (el) => {
    if (!el) return null;
    const b = el.getBoundingClientRect();
    return { y: Math.round(b.y), h: Math.round(b.height), w: Math.round(b.width) };
  };
  const streamEl = document.querySelector('.stream');
  const streamBox = r(streamEl);
  const sb = streamEl.getBoundingClientRect();
  const proposalEl = document.querySelector('.proposal-card');
  const reviewEl = document.querySelector('.review-card');
  const msgEls = [...document.querySelectorAll('.stream > .msg')];
  const last = msgEls.length ? msgEls[msgEls.length - 1] : null;
  const p = proposalEl ? proposalEl.getBoundingClientRect() : null;
  const l = last ? last.getBoundingClientRect() : null;
  return {
    stream: streamEl ? {
      clientH: streamEl.clientHeight, scrollH: streamEl.scrollHeight,
      scrollTop: Math.round(streamEl.scrollTop),
      top: Math.round(sb.top), bottom: Math.round(sb.bottom),
    } : null,
    proposal: r(proposalEl),
    review: r(reviewEl),
    messages: msgEls.map(r),
    proposalBelowLastMsg: p && l ? p.top >= l.bottom - 1 : null,
    proposalInView: p ? (p.top < sb.bottom && p.bottom > sb.top) : null,
  };
}
"""

MEASURE_JS = """
() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height),
      position: cs.position,
      overflowY: cs.overflowY,
      scrollH: el.scrollHeight, clientH: el.clientHeight,
      flex: cs.flex, minHeight: cs.minHeight, maxHeight: cs.maxHeight,
      zIndex: cs.zIndex,
    };
  };
  const shell = document.querySelector('.shell');
  const kids = shell ? [...shell.children].map((el) => {
    const r = el.getBoundingClientRect();
    return {
      cls: (el.className || '').toString().split(' ')[0],
      w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x),
    };
  }) : [];
  const overlap = (a, b) => {
    if (!a || !b) return 0;
    const x = Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
    const y = Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
    return Math.round(x * y);
  };
  // 被裁掉的元素：右边界超出窗口
  const clipped = kids.filter(k => k.x + k.w > window.innerWidth + 1).map(k => k.cls);
  const draft = box('.draft-scroll-box') || box('.draft-box');
  const decision = box('.decision-area') || box('.ic-actions');
  const stream = box('.stream');
  const card = box('.review-card') || box('.interactive-card');
  const input = document.querySelector('.decision-area textarea, .interactive-card textarea');
  const inputBox = input ? (() => {
    const r = input.getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height), y: Math.round(r.y) };
  })() : null;
  return {
    viewport: { w: window.innerWidth, h: window.innerHeight },
    shellKids: kids,
    shellScrollW: shell ? shell.scrollWidth : null,
    shellClientW: shell ? shell.clientWidth : null,
    clipped,
    stream, card, draft, decision, inputBox,
    overlapDraftDecision: overlap(draft, decision),
  };
}
"""


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


def seed_script(scenario: dict) -> str:
    """在页面脚本执行前写入分栏宽度（与 App.vue 读 localStorage 的键一致）。"""
    parts = []
    if scenario["editor"] is not None:
        parts.append(f"localStorage.setItem('inkforge.w.editor', '{scenario['editor']}');")
    if scenario["tree"] is not None:
        parts.append(f"localStorage.setItem('inkforge.w.tree', '{scenario['tree']}');")
    return "\n".join(parts)


def main() -> int:
    # Windows 控制台默认 GBK，写中文会崩；强制 UTF-8 输出
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not (DIST / "review.html").exists():
        print(f"[FATAL] 探针产物不存在：{DIST / 'review.html'}")
        print("        先跑：npx vite build --config ui-probe/vite.config.ts")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    base, httpd = serve(DIST)
    reports: list[dict] = []
    # 断言收集器必须在浏览器块**之前**定义：2026-09-17 新增的主对话/改稿目标/告警条
    # 三组断言在 `with sync_playwright()` 内部就会写入，晚定义会 UnboundLocalError。
    failures: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for vp in VIEWPORTS:
                for sc in SCENARIOS:
                    page = browser.new_page(viewport=vp, device_scale_factor=1)
                    seed = seed_script(sc)
                    if seed:
                        page.add_init_script(seed)
                    page.goto(f"{base}/review.html", wait_until="networkidle")
                    page.wait_for_selector(".review-card", timeout=15000)
                    page.wait_for_timeout(300)
                    data = page.evaluate(MEASURE_JS)
                    data["name"] = f"{vp['width']}x{vp['height']}/{sc['name']}"
                    reports.append(data)
                    shot = SHOTS / f"review-{vp['width']}x{vp['height']}-{sc['name']}.png"
                    page.screenshot(path=str(shot))
                    # 再抓一张「把正文框滚进视野」的图：卡片高于视口时首屏看不到它
                    if vp["width"] == 1600 and sc["name"] == "auto":
                        page.eval_on_selector(
                            ".draft-scroll-box",
                            "el => el.scrollIntoView({ block: 'center' })",
                        )
                        page.wait_for_timeout(200)
                        page.screenshot(path=str(SHOTS / "review-1600x1000-draft-visible.png"))
                    page.close()

            # 大纲审阅分支单独跑一次：卡片必须保持「内容高度」，不能被撑出空白
            outline_page = browser.new_page(viewport={"width": 1600, "height": 1000})
            outline_page.goto(f"{base}/review.html#outline", wait_until="networkidle")
            outline_page.wait_for_selector(".review-card", timeout=15000)
            outline_page.wait_for_timeout(300)
            outline = outline_page.evaluate(MEASURE_JS)
            outline_vis = outline_page.evaluate(VISIBLE_JS)
            outline_page.screenshot(path=str(SHOTS / "review-1600x1000-outline.png"))
            outline_page.close()
            print(
                f"[1600x1000/outline] 大纲 JSON 容器 h={outline_vis['outlineScroll']['h']}"
                f"（46vh=460，修复前 max-height:240px 失效会被撑到内容高度）"
            )

            # 改稿提案场景：观察提案卡在对话流中的位置与它和审阅卡的先后关系
            for mode in ("proposal", "outline-proposal"):
                pr = browser.new_page(viewport={"width": 1600, "height": 1000})
                pr.goto(f"{base}/review.html#{mode}", wait_until="networkidle")
                pr.wait_for_selector(".proposal-card", timeout=15000)
                pr.wait_for_timeout(400)
                flow = pr.evaluate(FLOW_JS)
                print(
                    f"[1600x1000/{mode}] 提案卡 y={flow['proposal']['y']}.."
                    f"{flow['proposal']['y'] + flow['proposal']['h']} (h={flow['proposal']['h']}) | "
                    f"审阅卡 y={flow['review']['y'] if flow['review'] else '—'} | "
                    f"消息 y={[m['y'] for m in flow['messages']]} | "
                    f"stream 视口 {flow['stream']['clientH']}/内容 {flow['stream']['scrollH']} "
                    f"scrollTop={flow['stream']['scrollTop']}"
                )
                print(
                    f"                提案卡是否在最后一条消息【下方】={flow['proposalBelowLastMsg']}"
                    f" | 首屏可见={flow['proposalInView']}"
                )
                pr.screenshot(path=str(SHOTS / f"review-1600x1000-{mode}.png"))

                # 提案卡必须带审校评分 / 意见 / 打回框（人工审批门禁）
                for sel, label in (
                    (".p-score-row", "提案评分行"),
                    (".p-review", "审校意见区"),
                    (".p-feedback", "打回意见框"),
                ):
                    if pr.locator(sel).count() == 0:
                        failures.append(f"{mode}: 提案卡缺少{label}（{sel}）")
                if pr.get_by_role("button", name="打回重做").count() == 0:
                    failures.append(f"{mode}: 提案卡没有「打回重做」按钮")
                if pr.get_by_role("button", name="通过并写入创作空间").count() == 0:
                    failures.append(f"{mode}: 提案卡没有「通过并写入创作空间」按钮")
                pr.close()

                # 提案卡必须插回创建位置（第 2 条消息之后），不能永远挂在最后
                if flow["proposalBelowLastMsg"]:
                    failures.append(f"{mode}: 提案卡仍挂在最后一条消息下方（未插回原位）")
                msgs = flow["messages"]
                if len(msgs) >= 4:
                    second_bottom = msgs[1]["y"] + msgs[1]["h"]
                    third_top = msgs[2]["y"]
                    if not (second_bottom <= flow["proposal"]["y"] and
                            flow["proposal"]["y"] + flow["proposal"]["h"] <= third_top):
                        failures.append(
                            f"{mode}: 提案卡未落在第 2、3 条消息之间"
                            f"（msg2 底 {second_bottom} / 卡 {flow['proposal']['y']} / msg3 顶 {third_top}）"
                        )

            # ── 2026-09-17 新增：主对话入口 / 改稿目标展示 / 选中态不同步告警 ──
            # 为什么用真实内核而不是 DOM 断言：这三条都属于"元素在代码里存在、
            # 但可能从不渲染或高度为 0"的类型（同 README 坑位 6/10 的教训）。
            mx = browser.new_page(viewport={"width": 1600, "height": 1000})
            mx.goto(f"{base}/review.html#proposal", wait_until="networkidle")
            mx.wait_for_selector(".proposal-card", timeout=15000)
            mx.wait_for_timeout(400)

            # (1) 提案卡必须显示"本次改的是哪份文稿"（路径 + 原文字数）
            tgt = mx.locator(".p-target")
            if tgt.count() == 0:
                failures.append("mainchat/proposal: 提案卡缺少目标文稿行（.p-target）")
            else:
                box = tgt.first.bounding_box()
                text = (tgt.first.inner_text() or "").replace("\n", " ")
                bh = box["height"] if box else 0
                print(f"[1600x1000/proposal] 目标文稿行：h={bh} | {text[:80]}")
                if bh < 14:
                    failures.append(
                        f"proposal: 目标文稿行被压扁（h={bh}px，元素存在但读不到）"
                    )
                if "settings/outline.md" not in text:
                    failures.append(f"proposal: 目标文稿行未显示真实路径（实际 {text[:80]!r}）")
                if "1180" not in text:
                    failures.append(f"proposal: 目标文稿行未显示原文字数（实际 {text[:80]!r}）")

            # (2) 选中态与已加载正文不同步 → 页内必须出现告警条
            mx.evaluate(
                "() => { window.__store.selection = {kind:'settings',"
                " rel:'settings/outline.md', title:'大纲'}; }"
            )
            mx.wait_for_timeout(500)
            warn = mx.locator(".align-warn")
            aligned = mx.evaluate("() => window.__probe.docAligned()")
            wbox = warn.first.bounding_box() if warn.count() else None
            wh = wbox["height"] if wbox else 0
            print(
                f"[1600x1000/proposal] 选中态不同步：docAligned={aligned} | "
                f"告警条 {'h=' + str(wh) if wbox else '未出现'}"
            )
            if aligned is not False:
                failures.append(f"mainchat/proposal: 正文加载失败后 docAligned 仍为 {aligned}（应为 False）")
            if wh < 16:
                failures.append(
                    f"proposal: 选中态不同步时告警条未渲染/被压扁（h={wh}px）"
                )
            mx.screenshot(path=str(SHOTS / "review-align-warn.png"))

            # (3) 主对话入口：点一下必须切到工作区 + 开全新空白会话
            before_token = mx.evaluate("() => window.__probe.chatResetToken()")
            nav = mx.get_by_role("button", name="主对话（工作区）")
            if nav.count() == 0:
                failures.append("mainchat: 左侧功能导航没有「主对话（工作区）」按钮")
            else:
                nav.first.click()
                mx.wait_for_timeout(900)
                after_token = mx.evaluate("() => window.__probe.chatResetToken()")
                state = mx.evaluate(
                    """() => ({
                        bookId: window.__store.bookId,
                        chatId: window.__store.chatId,
                        scope: window.__store.bookId || '__workspace__',
                        workspaceChip: document.querySelectorAll('.chip.workspace').length,
                        welcome: document.querySelectorAll('.welcome').length,
                        bubbles: document.querySelectorAll('.msg.assistant, .msg.user').length,
                        backBtn: document.querySelector('.back-to-book')?.textContent?.trim() || '',
                        wsChats: window.__probe.workspaceChatCount(),
                    })"""
                )
                print(
                    f"[1600x1000/mainchat] 切工作区：bookId={state['bookId']!r} "
                    f"chatId={state['chatId']!r} 工作区徽标={state['workspaceChip']} "
                    f"空会话欢迎页={state['welcome']} 消息气泡={state['bubbles']} "
                    f"回书按钮={state['backBtn']!r} 工作区会话数={state['wsChats']}"
                )
                mx.screenshot(path=str(SHOTS / "review-mainchat.png"))
                if after_token <= before_token:
                    failures.append(
                        f"mainchat: 点击后 chatResetToken 未自增（{before_token}→{after_token}）"
                    )
                if state["bookId"]:
                    failures.append(f"mainchat: 未切到工作区（bookId 仍为 {state['bookId']!r}）")
                if state["workspaceChip"] == 0:
                    failures.append("mainchat: 顶部未出现「🧭 工作区」徽标")
                if state["welcome"] == 0 or state["bubbles"] != 0:
                    failures.append(
                        f"mainchat: 未开成空白会话（欢迎页 {state['welcome']}，"
                        f"残留消息气泡 {state['bubbles']}）"
                    )
                if state["wsChats"] == 0:
                    failures.append("mainchat: 未为工作区新建空白会话（POST /api/chats 未被调用）")
                if not state["backBtn"]:
                    failures.append(
                        "mainchat: 工作区顶栏没有「回到《X》」入口（lastBookId 未记录）"
                    )
                elif "探针样板书" not in state["backBtn"]:
                    failures.append(f"mainchat: 回书按钮书名不对（{state['backBtn']!r}）")

                # (4) 作用域隔离 + 旧会话可回到：工作区不显示书内会话；点回书后能看到
                hist_ws = mx.evaluate(
                    "() => Array.from(document.querySelectorAll('.history-item .h-title'))"
                    ".map(e => e.textContent)"
                )
                if any("大纲改稿" in str(t) for t in hist_ws):
                    failures.append("mainchat: 工作区历史里出现了书内会话（作用域未隔离）")
                mx.get_by_role("button", name="历史对话").click()
                mx.wait_for_timeout(200)
                ws_titles = mx.evaluate(
                    "() => Array.from(document.querySelectorAll('.history-item .h-title'))"
                    ".map(e => e.textContent)"
                )
                print(f"[1600x1000/mainchat] 工作区历史会话：{ws_titles}")

                if state["backBtn"]:
                    mx.locator(".back-to-book").first.click()
                    mx.wait_for_timeout(900)
                    back = mx.evaluate(
                        """async () => {
                            const btns = Array.from(document.querySelectorAll('.ghost-btn'));
                            const h = btns.find(b => (b.textContent || '').includes('历史对话'));
                            if (h) h.click();
                            return null;
                        }"""
                    )
                    mx.wait_for_timeout(300)
                    book_titles = mx.evaluate(
                        "() => Array.from(document.querySelectorAll('.history-item .h-title'))"
                        ".map(e => e.textContent)"
                    )
                    print(
                        f"[1600x1000/mainchat] 回到《探针样板书》后历史会话：{book_titles}"
                        f"（back={back}）"
                    )
                    if not any("大纲改稿" in str(t) for t in book_titles):
                        failures.append(
                            "mainchat: 回到该书后仍看不到原会话（旧会话应可在对应历史里继续）"
                        )
                    mx.screenshot(path=str(SHOTS / "review-mainchat-back.png"))
            mx.close()

            # 互动出卡：页面必须仍然可点（本轮真实 bug：出卡后整页卡死）
            ia = browser.new_page(viewport={"width": 1600, "height": 1000})
            ia.goto(f"{base}/review.html#interactive", wait_until="networkidle")
            ia.wait_for_selector(".plot-card", timeout=15000)
            ia.wait_for_timeout(800)
            ia.screenshot(path=str(SHOTS / "review-interactive-cards.png"))
            masks = ia.evaluate("() => document.querySelectorAll('.n-modal-mask').length")
            center_hit = ia.evaluate(
                "() => { const e = document.elementFromPoint(innerWidth/2, innerHeight/2);"
                " return e ? (e.className || e.tagName).toString().slice(0,60) : 'null'; }"
            )
            clickable = True
            try:
                ia.locator(".composer-input").click(timeout=3000)
            except Exception:
                clickable = False
            print(
                f"[1600x1000/interactive] 遮罩层数={masks} | 屏幕中心命中={center_hit!r} "
                f"| 输入框可点={clickable}"
            )
            # 点一张卡，再测一次
            ia.locator(".plot-card").first.click()
            ia.wait_for_timeout(600)
            ia.screenshot(path=str(SHOTS / "review-interactive-chosen.png"))
            if masks != 0:
                failures.append(f"interactive: 出卡后残留 {masks} 层遮罩（页面会被挡住）")
            if not clickable:
                failures.append(f"interactive: 出卡后输入框点不动，屏幕中心被 {center_hit!r} 挡住")
            ia.close()

            # 评分 / 审阅意见必须落在首屏（待审出现时对齐卡片顶部）
            vis_page = browser.new_page(viewport={"width": 1366, "height": 768})
            vis_page.goto(f"{base}/review.html", wait_until="networkidle")
            vis_page.wait_for_selector(".review-card", timeout=15000)
            vis_page.wait_for_timeout(500)
            vis = vis_page.evaluate(VISIBLE_JS)
            print(
                f"[1366x768/chapter] 评分行 y={vis['scoreRow']['y']} | 意见框 y={vis['issueBox']['y']} "
                f"| 正文框 y={vis['draft']['y']} h={vis['draft']['h']} "
                f"| 决策区 y={vis['decision']['y']} | 首屏 {vis['stream']['top']}..{vis['stream']['bottom']}"
            )
            vis_page.screenshot(path=str(SHOTS / "review-1366x768-review-top.png"))
            print(f"[1366x768/chapter] 顶部工作状态 = 「{vis['chip']}」")
            if "待审" not in vis["chip"] and "生成中" not in vis["chip"]:
                failures.append(f"1366x768: 有待审内容时工作状态显示为「{vis['chip']}」，未反映智能体状态")
            vis_page.close()
            for key, label in (("scoreRow", "评分行"), ("issueBox", "审阅意见框")):
                if not vis[key]["inView"]:
                    failures.append(
                        f"1366x768: {label}不在首屏（y={vis[key]['y']}，首屏 "
                        f"{vis['stream']['top']}..{vis['stream']['bottom']}）"
                    )
            if vis["draft"]["h"] < 120:
                failures.append(f"1366x768: 正文框被压到 {vis['draft']['h']}px")

            # 外部改写（treeVersion 自增）后编辑器必须重取正文
            ed = browser.new_page(viewport={"width": 1600, "height": 1000})
            ed.goto(f"{base}/review.html", wait_until="networkidle")
            ed.wait_for_selector(".doc-preview", timeout=15000)
            ed.wait_for_timeout(500)
            before = ed.text_content(".doc-preview") or ""
            hits_before = ed.evaluate("() => window.__probe.rawHits()")
            ed.evaluate("() => { window.__store.treeVersion += 1 }")
            ed.wait_for_timeout(600)
            after = ed.text_content(".doc-preview") or ""
            hits_after = ed.evaluate("() => window.__probe.rawHits()")
            ed.screenshot(path=str(SHOTS / "review-editor-reload.png"))
            ed.close()
            print(
                f"[编辑器] treeVersion 自增前 raw 取用 {hits_before} 次 → 后 {hits_after} 次；"
                f"正文{'已刷新' if before != after else '未刷新'}"
            )
            if before == after:
                failures.append("treeVersion 自增后正文编辑器未重取正文（改稿后看不到新正文）")
            if hits_after <= hits_before:
                failures.append(
                    f"treeVersion 自增后未重新请求 /api/chapters/1/raw（{hits_before} → {hits_after}）"
                )
        finally:
            browser.close()
    httpd.shutdown()

    report_path = SHOTS / "review-probe-report.json"
    report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[报告] {report_path}\n")

    # failures 已在浏览器块之前初始化（新增断言会提前写入），此处**不能**重置，
    # 否则会把主对话/改稿目标/告警条的失败清空。
    # 已知缺陷（单独跟踪，不计入退出码）：见 README「已知缺陷」一节
    known: list[str] = []
    for r in reports:
        name = r["name"]
        d, card, stream = r["draft"], r["card"], r["stream"]
        editor = next((k for k in r["shellKids"] if k["cls"] == "editor-pane"), None)
        chat = next((k for k in r["shellKids"] if k["cls"] == "chat-panel"), None)
        if d is None or card is None or stream is None:
            failures.append(f"{name}: 审阅卡未渲染")
            continue
        print(
            f"[{name}] 正文框 {d['w']}x{d['h']} | 意见框 {r['inputBox']} | "
            f"审阅卡 {card['w']}x{card['h']} | stream 视口 {stream['clientH']}/内容 {stream['scrollH']} | "
            f"对话栏 {chat['w'] if chat else '?'} 正文栏 {editor['w'] if editor else '?'}"
            + (f" | 被裁 {r['clipped']}" if r["clipped"] else "")
        )
        # A. 正文框不该被压到几乎没有高度
        if d["h"] < 120:
            failures.append(f"{name}: 正文框高度仅 {d['h']}px（被垂直挤压）")
        # B. 意见框与正文框不该重叠
        if r["overlapDraftDecision"] > 0:
            failures.append(f"{name}: 意见框压住正文框，重叠 {r['overlapDraftDecision']}px²")
        # C/D. 三栏固定宽度在窄窗口下会溢出（已定位，属**另一个**缺陷，见下方「已知缺陷」）
        if r["shellScrollW"] and r["shellScrollW"] > r["shellClientW"] + 1:
            known.append(f"{name}: 外壳横向溢出 {r['shellScrollW'] - r['shellClientW']}px")
        if r["clipped"]:
            known.append(f"{name}: 被推出窗口 {r['clipped']}")

    # E. 大纲审阅分支：卡片高度必须由内容决定，不能长出一大截空白
    oc, ostream = outline["card"], outline["stream"]
    if oc and ostream:
        print(
            f"[1600x1000/outline] 审阅卡 {oc['w']}x{oc['h']} | stream 视口 {ostream['clientH']}"
        )
        if oc["h"] > ostream["clientH"]:
            failures.append(
                f"outline: 审阅卡高度 {oc['h']}px 超过可视 {ostream['clientH']}px（被撑高）"
            )
        if oc["h"] < 300:
            failures.append(f"outline: 审阅卡高度仅 {oc['h']}px（内容被压扁）")
        # 大纲 JSON 容器必须被限高（NScrollbar 的 max-height 不生效是已知陷阱）
        if outline_vis["outlineScroll"]["h"] > 470:
            failures.append(
                f"outline: 大纲 JSON 容器高 {outline_vis['outlineScroll']['h']}px，未被 46vh 限高"
            )

    print("\n=== 回归护栏（决定退出码）===")
    if failures:
        for f in failures:
            print("  FAIL " + f)
    else:
        print("  全部通过")

    print("\n=== 已知缺陷（另行跟踪，不影响退出码）===")
    if known:
        for k in known:
            print("  [已知] " + k)
        print("        三栏为固定宽度（App.vue 的 flex: 0 0 <w>px），窗口偏窄时总和超过窗口，")
        print("        右侧的「创作空间」会被推出窗口。修法：改 flex 为 '0 1 <w>px' 并给各栏 min-width。")
    else:
        print("  无")

    if failures:
        print(f"\n{len(failures)} 项回归失败（{len(reports)} 组场景）")
        return 1
    print(f"\n回归护栏全部通过（{len(reports)} 组场景）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
