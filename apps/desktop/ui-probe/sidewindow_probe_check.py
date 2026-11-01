"""侧窗（功能小窗）专项探针 —— 悬浮页形态。

作者指定的形态：智能体设置 / 学习仿写 / 素材库 / 正文改写 / 数据看板 / 导出中心
一律是**悬浮页**（盖在左侧、正文列保持可见），不做占位换页。

本探针验四件事：
1. 确实是 position:fixed 的悬浮层，且左边缘贴着导航（跟着 --rail-w 走，不是写死 184px）；
2. 不压到右侧「创作空间」，也不超出视口底部；
3. 正文列在浮层打开时仍然可见（悬浮页不占位）；
4. 头部有「← 回到 X」，Esc 能关。
"""
import functools
import http.server
import pathlib
import socketserver
import threading

from playwright.sync_api import sync_playwright

DIST = pathlib.Path(__file__).resolve().parent.parent / "out" / "ui-probe"
SHOTS = pathlib.Path(__file__).resolve().parent.parent / "out" / "ui-probe-shots"
SHOTS.mkdir(exist_ok=True)

handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{port}"

MEASURE = """
() => {
  const box = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) };
  };
  const win = document.querySelector('.side-window');
  const cs = win ? getComputedStyle(win) : null;
  const ed = document.querySelector('.editor-pane');
  const railEl = document.querySelector('.feature-nav');
  return {
    sideWindow: box('.side-window'),
    position: cs ? cs.position : null,
    zIndex: cs ? cs.zIndex : null,
    railW: railEl ? Math.round(railEl.getBoundingClientRect().width) : null,
    editor: box('.editor-pane'),
    editorVisible: !!ed && getComputedStyle(ed).display !== 'none' && box('.editor-pane').w > 0,
    chat: box('.chat-panel'),
    tree: box('.workspace'),
    backBtn: (document.querySelector('.back-btn') || {}).textContent?.trim() ?? null,
    origin: (document.querySelector('.win-origin') || {}).textContent?.trim() ?? null,
    vw: window.innerWidth,
    vh: window.innerHeight,
  };
}
"""

failures: list[str] = []
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    for w, h in ((1366, 768), (1600, 1000)):
        page = browser.new_page(viewport={"width": w, "height": h})
        page.goto(f"{base}/review.html", wait_until="networkidle")
        page.wait_for_selector(".review-card", timeout=15000)
        page.evaluate("() => { window.__store.sideWindow = 'revision'; }")
        page.wait_for_timeout(700)
        m = page.evaluate(MEASURE)
        page.screenshot(path=str(SHOTS / f"sidewindow-{w}x{h}.png"))
        print(f"[{w}x{h}] {m}")
        tag = f"{w}x{h}"

        if not m["sideWindow"]:
            failures.append(f"{tag}: 侧窗没渲染")
            page.close()
            continue
        if m["position"] != "fixed":
            failures.append(f"{tag}: 侧窗不是 position:fixed（应为悬浮页）")
        if m["editor"] and not m["editorVisible"]:
            failures.append(f"{tag}: 悬浮页打开时正文列被隐藏（悬浮页不该占位）")
        # 左边缘贴导航（+8px 间距）
        expect_left = (m["railW"] or 176) + 8
        if abs(m["sideWindow"]["x"] - expect_left) > 2:
            failures.append(
                f"{tag}: 浮层左边缘 {m['sideWindow']['x']} ≠ 导航+8({expect_left})"
                "（不该写死 184px）"
            )
        # 不压创作空间
        if m["sideWindow"]["x"] + m["sideWindow"]["w"] > m["tree"]["x"] + 1:
            failures.append(
                f"{tag}: 浮层压到创作空间（右缘 {m['sideWindow']['x'] + m['sideWindow']['w']} "
                f"> 树左缘 {m['tree']['x']}）"
            )
        # 不切正文列（浮层最多盖满对话列）
        if m["editor"]["w"] > 0 and m["sideWindow"]["x"] + m["sideWindow"]["w"] > m["editor"]["x"] + 1:
            failures.append(
                f"{tag}: 浮层盖住正文列（右缘 {m['sideWindow']['x'] + m['sideWindow']['w']} "
                f"> 正文左缘 {m['editor']['x']}）"
            )
        # 不超视口
        if m["sideWindow"]["y"] + m["sideWindow"]["h"] > m["vh"] + 1:
            failures.append(f"{tag}: 浮层超出视口底部")
        if not m["backBtn"] or "回到" not in str(m["backBtn"]):
            failures.append(f"{tag}: 浮层头部缺少「← 回到 X」")

        # Esc 关闭
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        gone = page.evaluate("() => document.querySelectorAll('.side-window').length")
        if gone != 0:
            failures.append(f"{tag}: Esc 没有关掉浮层（还剩 {gone} 个）")
        page.close()

    # 导航折叠后浮层要跟着贴到 56px 处（验证"不写死 184px"）
    page = browser.new_page(viewport={"width": 1366, "height": 768})
    page.goto(f"{base}/review.html", wait_until="networkidle")
    page.wait_for_selector(".review-card", timeout=15000)
    page.evaluate(
        """() => {
            window.__store.navCollapsed = true;
            window.__store.sideWindow = 'revision';
        }"""
    )
    page.wait_for_timeout(700)
    m = page.evaluate(MEASURE)
    print(f"[1366 导航折叠] {m}")
    if m["sideWindow"] and abs(m["sideWindow"]["x"] - (m["railW"] + 8)) > 2:
        failures.append(
            f"导航折叠后浮层没跟上：x={m['sideWindow']['x']} railW={m['railW']}"
        )
    page.screenshot(path=str(SHOTS / "sidewindow-1366-railcollapsed.png"))
    page.close()
    browser.close()
httpd.shutdown()

print("\n=== 结论 ===")
if failures:
    for f in failures:
        print("  FAIL", f)
    raise SystemExit(1)
print("  侧窗（悬浮页）验证通过：fixed / 贴导航 / 不压树 / 不超视口 / 正文可见 / 返回路径 / Esc")
