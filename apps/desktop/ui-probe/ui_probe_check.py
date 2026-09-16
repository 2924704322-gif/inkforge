"""Inkforge 向导弹窗灰屏缺陷 —— 真实浏览器像素级验证。

流程：起静态服务 → 真实 Chromium 打开探针 → 走「书架 → 新建作品 → 创建」→
对截图做像素采样，量化证明向导弹窗面板是否有背景、遮罩有几层。

用法：python ui_probe_check.py
退出码：0 全部断言通过；1 有失败。
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
DIST = ROOT.parent / "out" / "ui-probe"
SHOTS = ROOT.parent / "out" / "ui-probe-shots"
VIEWPORT = {"width": 1280, "height": 800}

# 面板尺寸与位置（与 BookshelfDialog.vue 中向导 NModal 的 style 一致）
PANEL_W, PANEL_H_VH = 860, 0.86


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


def effective_bg(page, x: int, y: int) -> str:
    """从命中点向上累计背景，返回该点实际呈现的颜色（rgba 字符串）。"""
    return page.evaluate(
        """([x, y]) => {
            let el = document.elementFromPoint(x, y);
            const chain = [];
            while (el) {
              const cs = getComputedStyle(el);
              chain.push({
                tag: el.tagName.toLowerCase(),
                cls: el.className && el.className.toString().slice(0, 60),
                bg: cs.backgroundColor,
                shadow: cs.boxShadow === 'none' ? '' : 'has-shadow',
              });
              const bg = cs.backgroundColor;
              const m = bg.match(/rgba?\\(([^)]+)\\)/);
              if (m) {
                const parts = m[1].split(',').map(s => parseFloat(s.trim()));
                const a = parts.length > 3 ? parts[3] : 1;
                if (a >= 0.999) return { hit: chain[0], opaqueAt: chain[chain.length - 1], color: bg, chain };
              }
              el = el.parentElement;
            }
            return { hit: chain[0], opaqueAt: null, color: 'transparent-through-body', chain };
        }""",
        [x, y],
    )


def px(img: Image.Image, x: int, y: int) -> tuple[int, int, int]:
    return img.convert("RGB").getpixel((x, y))


def main() -> int:  # noqa: C901
    if not DIST.exists():
        print(f"[FAIL] 探针产物不存在：{DIST}（先跑 npx vite build --config ui-probe/vite.config.ts）")
        return 1
    SHOTS.mkdir(parents=True, exist_ok=True)
    url, httpd = serve(DIST)
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))

    print("=" * 76)
    print("Inkforge 向导弹窗灰屏缺陷 —— 真实浏览器验证")
    print("=" * 76)
    print(f"探针地址: {url}")
    print(f"视口: {VIEWPORT['width']}x{VIEWPORT['height']}\n")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
            page.goto(url)
            page.wait_for_function("() => window.__probe && window.__probe.ready")
            page.wait_for_timeout(400)

            print("[1] 复现用户路径：书架 → 新建作品 → 创建（勾选先生成 Demo）")
            check("书架弹窗已渲染", page.locator("text=新建作品").count() > 0)
            page.get_by_role("button", name="新建作品").click()
            page.wait_for_timeout(350)
            check("「新建作品」表单已弹出", page.locator("text=书名标识").count() > 0)

            page.locator('input[placeholder="例：my-first-novel"]').fill("probe-book")
            # X1 结构化 brief 之后这里已经是 <input>（不再是单个 textarea）——选择器不限标签
            page.locator('[placeholder*="东方玄幻"]').fill("东方玄幻，冷峻剑修主角，复仇主线。")
            with_demo = page.locator('input[type="checkbox"]').is_checked()
            check("「先生成设定 Demo」默认勾选", with_demo)
            page.screenshot(path=str(SHOTS / "1-create-form.png"))

            page.get_by_role("button", name="创建").click()
            page.wait_for_selector("text=设定 Demo 审核", timeout=10_000)
            page.wait_for_timeout(700)
            page.screenshot(path=str(SHOTS / "2-wizard-after.png"))

            calls = page.evaluate("() => window.__probe.calls")
            check(
                "点创建后确实调用了 /api/demo（进入向导而非直启流水线）",
                any(c.startswith("POST /api/demo") and "/confirm" not in c for c in calls),
                str([c for c in calls if "demo" in c]),
            )
            print()

            print("[2] 像素采样：向导弹窗面板是否真的有背景")
            top = (VIEWPORT["height"] - VIEWPORT["height"] * PANEL_H_VH) / 2
            left = (VIEWPORT["width"] - PANEL_W) / 2
            # 面板内、且落在 .wizard 的 16px padding 区（避开输入框/文字）
            inside = (int(left + 8), int(top + 8))
            # 面板外、远离面板（应只有遮罩层）
            outside = (24, 24)

            img = Image.open(SHOTS / "2-wizard-after.png")
            inside_rgb = px(img, *inside)
            outside_rgb = px(img, *outside)
            inside_info = effective_bg(page, *inside)

            check(
                "面板内部像素为不透明白色 (255,255,255)",
                inside_rgb == (255, 255, 255),
                f"{inside} → {inside_rgb}",
            )
            check(
                "命中点归属向导弹窗面板（.wizard / .n-modal）",
                any(
                    s in (inside_info["hit"]["cls"] or "")
                    for s in ("wizard", "n-modal")
                ),
                f"hit={inside_info['hit']['tag']}.{inside_info['hit']['cls']}",
            )
            print(
                f"       面板外遮罩实测 {outside_rgb}；"
                f"单层 mask = 0.6×255 ≈ (153,153,153)；双层叠加 = 0.36×255 ≈ (92,92,92)"
            )
            check(
                "面板外为单层遮罩（≈153，而非双层 ≈92）",
                145 <= outside_rgb[0] <= 160,
                f"{outside} → {outside_rgb}",
            )
            print()

            print("[3] 反证：注入 CSS 还原修复前的透明面板，观察差异")
            page.add_style_tag(
                content=".wizard{background:transparent !important;border-radius:0 !important;box-shadow:none !important;}"
            )
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / "3-wizard-beforefix-simulated.png"))
            before_img = Image.open(SHOTS / "3-wizard-beforefix-simulated.png")
            before_rgb = px(before_img, *inside)
            check(
                "去掉背景后面板内像素退化为遮罩灰（复现原缺陷）",
                before_rgb != (255, 255, 255) and abs(before_rgb[0] - outside_rgb[0]) < 12,
                f"{inside} → {before_rgb}（与面板外遮罩 {outside_rgb} 基本一致）",
            )
            page.reload()
            page.wait_for_function("() => window.__probe && window.__probe.ready")
            print()

            print("[4] 模板层面：确认向导 NModal 未使用无效的 content-style")
            page.get_by_role("button", name="新建作品").click()
            page.wait_for_timeout(300)
            page.locator('input[placeholder="例：my-first-novel"]').fill("probe-book")
            page.locator('[placeholder*="东方玄幻"]').fill("测试内容")
            page.get_by_role("button", name="创建").click()
            page.wait_for_selector("text=设定 Demo 审核", timeout=10_000)
            page.wait_for_timeout(500)
            probe_js = page.evaluate(
                """() => {
                    const panel = document.querySelector('.wizard');
                    const wrapper = panel && panel.closest('.n-modal-body-wrapper');
                    const mask = document.querySelector('.n-modal-mask');
                    return {
                      panelWidth: panel ? Math.round(panel.getBoundingClientRect().width) : 0,
                      panelHeight: panel ? Math.round(panel.getBoundingClientRect().height) : 0,
                      maskCount: document.querySelectorAll('.n-modal-mask').length,
                      roleAttr: panel ? panel.getAttribute('role') : null,
                      arieModal: panel ? panel.getAttribute('aria-modal') : null,
                      maskBg: mask ? getComputedStyle(mask).backgroundColor : null,
                      wrapper: !!wrapper,
                    };
                }"""
            )
            check(
                "面板宽度按 style 生效（860px）",
                probe_js["panelWidth"] == 860,
                f"width={probe_js['panelWidth']}",
            )
            check(
                "面板高度按 style 生效（86vh = 688px）",
                abs(probe_js["panelHeight"] - int(VIEWPORT["height"] * PANEL_H_VH)) <= 2,
                f"height={probe_js['panelHeight']}",
            )
            check(
                "页面中只存在 1 层遮罩（修复前为 2 层）",
                probe_js["maskCount"] == 1,
                f"maskCount={probe_js['maskCount']}",
            )
            check(
                "无 preset 分支已补齐 role/aria-modal（可访问性对齐卡片预设）",
                probe_js["roleAttr"] == "dialog" and probe_js["arieModal"] == "true",
                f"role={probe_js['roleAttr']} aria-modal={probe_js['arieModal']}",
            )
            page.screenshot(path=str(SHOTS / "4-final.png"))
            browser.close()
    finally:
        httpd.shutdown()

    passed = sum(1 for _, ok, _ in results if ok)
    failed = len(results) - passed
    print()
    print("=" * 76)
    print(f"总计：{passed} 通过 / {failed} 失败  →  {'PASS' if failed == 0 else 'FAIL'}")
    print(f"截图目录：{SHOTS}")
    print("=" * 76)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
