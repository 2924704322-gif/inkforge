"""互动创作正文预览框探针 —— 真实浏览器验证「可上下拉伸」。

流程：静态服务 out/ui-probe → 打开 interactive.html（挂载**真实** InteractiveCards.vue，
状态桩 = awaiting_review）→ 量测 .draft-box 的 computed style 与几何 → 真实鼠标拖拽右下角
→ 复测高度（证明 resize 真的生效，不只是写了条 CSS 声明）。

用法：python ui-probe/interactive_probe_check.py
退出码：0 全部通过；1 有失败。
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent
DIST = ROOT.parent / "out" / "ui-probe"
SHOTS = ROOT.parent / "out" / "ui-probe-shots"
VIEWPORT = {"width": 1200, "height": 900}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


MEASURE = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return null;
  const cs = getComputedStyle(el);
  const r = el.getBoundingClientRect();
  return { resize: cs.resize, overflowY: cs.overflowY, height: r.height,
           top: r.top, left: r.left, width: r.width, scrollH: el.scrollHeight };
}"""


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if not (DIST / "interactive.html").exists():
        print(f"[FATAL] 探针产物不存在：{DIST / 'interactive.html'}")
        print("        先跑：npx vite build --config ui-probe/vite.config.ts")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    base, httpd = serve(DIST)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page: Page = browser.new_page(viewport=VIEWPORT)
            page.goto(f"{base}/interactive.html")
            page.wait_for_timeout(700)

            box = page.locator(".draft-box")
            check("正文预览框已渲染", box.count() == 1, f"{box.count()} 个")

            m1 = page.evaluate(MEASURE, ".draft-box")
            check("CSS resize 生效（computed resize=vertical）",
                  bool(m1) and m1["resize"] == "vertical", f"resize={m1 and m1['resize']}")
            check("overflow 非 visible（resize 生效的前提）",
                  bool(m1) and m1["overflowY"] != "visible", f"overflowY={m1 and m1['overflowY']}")
            check("初始高度不小于 300px（旧实现被 max-height:260px 卡死）",
                  bool(m1) and m1["height"] >= 300, f"height={m1 and round(m1['height'])}")
            check("正文确实超出可视区（有滚动空间，不是摆设）",
                  bool(m1) and m1["scrollH"] > m1["height"],
                  f"scrollH={m1 and round(m1['scrollH'])} height={m1 and round(m1['height'])}")

            check("有「可上下拖拽」的操作提示",
                  page.locator(".draft-head").count() == 1
                  and "拖拽" in page.locator(".draft-head").inner_text(),
                  page.locator(".draft-head").inner_text() if page.locator(".draft-head").count() else "缺失")
            check("预览框显示字数",
                  any("字" in t for t in page.locator(".draft-head .muted").all_inner_texts()))

            # ── 真实拖拽：右下角向下拖 220px ──
            r = box.bounding_box()
            assert r is not None
            before = r["height"]
            page.mouse.move(r["x"] + r["width"] - 3, r["y"] + r["height"] - 3)
            page.mouse.down()
            page.mouse.move(r["x"] + r["width"] - 3, r["y"] + r["height"] - 3 + 220, steps=14)
            page.mouse.up()
            page.wait_for_timeout(350)

            m2 = page.evaluate(MEASURE, ".draft-box")
            grew = bool(m2) and m2["height"] > before + 120
            check("真实拖拽右下角后高度变大（拉伸真生效）",
                  grew, f"{round(before)} → {m2 and round(m2['height'])} px")
            check("拉伸后不超过 1200px 上限",
                  bool(m2) and m2["height"] <= 1210, f"height={m2 and round(m2['height'])}")

            page.screenshot(path=str(SHOTS / "interactive-draft-resizable.png"), full_page=True)
            print(f"  截图：{SHOTS / 'interactive-draft-resizable.png'}")
            page.close()
        finally:
            browser.close()
    httpd.shutdown()

    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print("\n" + "=" * 70)
    print(f"总计：{passed} 通过 / {len(failed)} 失败")
    for n, d in failed:
        print(f"  FAIL {n}  — {d}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
