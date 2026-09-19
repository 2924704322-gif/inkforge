"""Inkforge 对话框可用性探针 —— 真实浏览器走查。

流程：真实 App.vue 外壳 + 内存桥桩 → 把每个对话框走到头，报告哪一个不可用。
用法：python ui-probe/dialogs_probe_check.py
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
VIEWPORT = {"width": 1600, "height": 1000}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


def calls(page: Page) -> list[str]:
    return page.evaluate("() => window.__probe.calls")


def has_call(page: Page, needle: str) -> bool:
    return any(needle in c for c in calls(page))


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{port}", httpd


def console_errors(page: Page) -> list[str]:
    return page.evaluate("() => window.__probeErrors || []")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not (DIST / "dialogs.html").exists():
        print(f"[FATAL] 探针产物不存在：{DIST / 'dialogs.html'}")
        print("        先跑：npx vite build --config ui-probe/vite.config.ts")
        return 1

    SHOTS.mkdir(parents=True, exist_ok=True)
    base, httpd = serve(DIST)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport=VIEWPORT)
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: errors.append(m.text) if m.type == "error" else None,
            )
            page.goto(f"{base}/dialogs.html", wait_until="networkidle")
            page.wait_for_function("() => window.__probe && window.__probe.ready")
            page.wait_for_timeout(500)

            print("[A] 主界面与会话输入框")
            check("三栏外壳已渲染", page.locator(".chat-panel").count() == 1)
            check("对话输入框存在", page.locator(".composer-input").count() == 1)
            page.fill(".composer-input", "探针：发一条消息")
            page.press(".composer-input", "Enter")
            page.wait_for_timeout(600)
            check(
                "回车后确实发出对话请求（POST /api/chats/c-probe/send）",
                has_call(page, "/api/chats/c-probe/send"),
                str([c for c in calls(page) if "/api/chats" in c][-3:]),
            )
            check("输入框内容已清空（发送被受理）", page.input_value(".composer-input") == "")
            page.screenshot(path=str(SHOTS / "dialogs-a-chat.png"))

            print("\n[B] 书架对话框")
            page.get_by_role("button", name="书架").first.click()
            page.wait_for_timeout(500)
            check("点左侧「书架」后对话框弹出", page.locator("text=新建作品").count() > 0)
            page.screenshot(path=str(SHOTS / "dialogs-b-bookshelf.png"))

            print("\n[C] 新建作品 → 结构化 brief → 创建")
            # 精确匹配：左侧导航按钮的可见文案是「📚 书架 / 新建作品」，
            # 用子串匹配会同时命中它与书架对话框里的「新建作品」（strict mode 直接报错）。
            page.get_by_role("button", name="新建作品", exact=True).click()
            page.wait_for_timeout(400)
            check(
                "「新建作品」表单已弹出",
                page.locator('input[placeholder="例：my-first-novel"]').count() == 1,
            )
            page.fill('input[placeholder="例：my-first-novel"]', "probe-new")
            brief_input = page.locator('[placeholder*="东方玄幻"]')
            # 两个都在才算对：① 用户自己提供的 brief 框 ② 结构化字段里的「体裁」
            check("创作需求 brief 框 + 结构化字段都在", brief_input.count() >= 2,
                  f"匹配 {brief_input.count()} 个")
            if brief_input.count():
                brief_input.first.fill("东方玄幻，冷峻剑修主角")
            page.get_by_role("button", name="创建").click()
            page.wait_for_timeout(900)
            check(
                "点「创建」后调用 POST /api/demo（进入向导）",
                has_call(page, "/api/demo"),
                str([c for c in calls(page) if "/api/demo" in c]),
            )
            page.screenshot(path=str(SHOTS / "dialogs-c-create.png"))

            print("\n[D] 设定 Demo 审核向导")
            wizard = page.locator(".wizard")
            check("向导弹窗已渲染", wizard.count() == 1)
            if wizard.count():
                page.wait_for_timeout(1900)  # 等一次轮询把 done 的 demo 取回
                check(
                    "向导展示了可编辑的设定字段（书名输入框）",
                    page.locator('.wizard input').count() >= 3,
                )
                check("向导「同意设定并开始生成」按钮存在",
                      page.get_by_role("button", name="同意设定并开始生成").count() == 1)
                check("向导有「打回 · 重新生成」按钮（带审核意见）",
                      page.get_by_role("button", name="打回 · 重新生成").count() == 1)
                check("向导有审核意见输入框",
                      page.locator(".wizard textarea").count() >= 4)

                # 向导打开时点背景：不得把底层书架/向导一起关掉
                page.mouse.click(12, 12)
                page.wait_for_timeout(400)
                check("点背景不会误关向导", page.locator(".wizard").count() == 1)

                page.get_by_role("button", name="同意设定并开始生成").click()
                page.wait_for_timeout(900)
                check(
                    "确认入库调用 POST /api/demo/confirm",
                    has_call(page, "/api/demo/confirm"),
                )
                check(
                    "确认后调用 POST /api/start",
                    has_call(page, "/api/start"),
                    str([c for c in calls(page) if "/api/start" in c]),
                )
                check("确认后向导关闭", page.locator(".wizard").count() == 0)
            page.screenshot(path=str(SHOTS / "dialogs-d-wizard.png"))

            print("\n[E] 其它对话框")
            for nav, title in (("模型配置", "模型配置"), ("风格工坊", "风格工坊")):
                page.get_by_role("button", name=nav).first.click()
                page.wait_for_timeout(600)
                check(f"「{nav}」对话框可打开", page.locator(f"text={title}").count() > 0)
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)

            print("\n[E2] 风格工坊 · 第三个同级模块「约束提炼」（双输入形态）")
            page.get_by_role("button", name="风格工坊").first.click()
            page.wait_for_timeout(600)
            tabs = page.locator(".tabs .tab")
            check("风格工坊有三个同级标签（新增第三个）",
                  tabs.count() == 3, f"{tabs.count()} 个：{tabs.all_inner_texts()}")
            tabs.nth(2).click()
            page.wait_for_timeout(400)
            src_box = page.locator("textarea[placeholder*='粘贴要提炼的正文']")
            req_box = page.locator("textarea[placeholder*='把这段里可复用的写作约束']")
            check("① 正文内容框存在", src_box.count() == 1)
            check("② 提炼需求框存在", req_box.count() == 1)
            check("两个内容框是分开的两个 textarea（不是同一个）",
                  src_box.count() == 1 and req_box.count() == 1
                  and page.locator(".forge-field textarea").count() == 2,
                  f"共 {page.locator('.forge-field textarea').count()} 个")
            check("有字数计数（正文 / 上限）",
                  page.locator(".forge-counter").count() >= 2,
                  page.locator(".forge-counter").first.inner_text())
            check("有「提炼」按钮",
                  page.get_by_role("button", name="提炼", exact=True).count() == 1)
            scope = page.locator(".scope-no")
            check("隔离声明按新语义（看不到作品库 / 没粘贴的内容）",
                  scope.count() == 1 and "作品库" in scope.inner_text(),
                  scope.inner_text() if scope.count() else "缺失")
            check("第三页有「查看智能体设定」入口",
                  page.get_by_role("button", name="查看智能体设定").count() == 1)

            src_box.fill("剑冢的雪落了三天，他把断剑插回石缝，指节冻得发紫。")
            req_box.fill("提炼这段的文风与节奏约束")
            page.get_by_role("button", name="提炼", exact=True).click()
            page.wait_for_timeout(700)
            check("点「提炼」调用 POST /api/style-forge/constraints",
                  has_call(page, "/api/style-forge/constraints"),
                  str([c for c in calls(page) if "style-forge" in c]))
            out = page.locator(".forge-out textarea")
            value = out.input_value() if out.count() else ""
            check("结果区渲染出 `- ` 条目",
                  value.startswith("- ") and "\n- " in value, repr(value))
            check("结果区有「复制」按钮",
                  page.get_by_role("button", name="复制", exact=True).count() == 1)
            check("结果区有「存入约束库」按钮",
                  page.get_by_role("button", name="存入约束库").count() == 1)
            page.screenshot(path=str(SHOTS / "dialogs-e2-style-forge-constraint.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            print("\n[F] 运行期 JS 报错")
            real_errors = [e for e in errors if "favicon" not in e.lower()]
            check("无运行期 JS 报错", not real_errors, str(real_errors[:3]))

            page.close()
        finally:
            browser.close()
    httpd.shutdown()

    passed = sum(1 for _n, ok, _d in results if ok)
    failed = [(n, d) for n, ok, d in results if not ok]
    print("\n" + "=" * 72)
    print(f"总计：{passed} 通过 / {len(failed)} 失败")
    for n, d in failed:
        print(f"  FAIL {n}" + (f"  — {d}" if d else ""))
    print(f"截图目录：{SHOTS}")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
