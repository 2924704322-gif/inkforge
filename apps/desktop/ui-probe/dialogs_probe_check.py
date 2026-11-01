"""Inkforge 对话框可用性探针 —— 真实浏览器走查。

流程：真实 App.vue 外壳 + 内存桥桩 → 把每个对话框走到头，报告哪一个不可用。
用法：python ui-probe/dialogs_probe_check.py
退出码：0 全部通过；1 有失败。
"""

from __future__ import annotations

import functools
import http.server
import re
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

            print("\n[A2] 输入框模型徽标 = 真实绑定（不再写死模型名）")
            chip = page.locator(".model-chip")
            check("输入框右下角有模型徽标", chip.count() == 1)
            check(
                "徽标显示主智能体（master）的绑定模型",
                chip.count() == 1 and chip.inner_text().strip() == "probe-master-model",
                chip.inner_text() if chip.count() else "缺失",
            )
            page.locator(".tool-btn").first.click()
            page.wait_for_timeout(400)
            check(
                "切到改稿模式后徽标跟着变成正文写手（prose）的绑定",
                page.locator(".model-chip").inner_text().strip() == "probe-prose-model",
                page.locator(".model-chip").inner_text(),
            )
            page.locator(".tool-btn").first.click()
            page.wait_for_timeout(300)
            page.locator(".model-chip").first.click()
            page.wait_for_timeout(700)
            check(
                "点徽标直达「模型配置」对话框",
                page.locator("text=角色绑定（按 Agent 绑定模型接入点）").count() > 0,
            )
            page.screenshot(path=str(SHOTS / "dialogs-a2-model-chip.png"))
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

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

            print("\n[E1] 模型配置 · 「获取模型」到点选目标模型")
            page.get_by_role("button", name="模型配置").first.click()
            page.wait_for_timeout(600)
            rows = page.locator(".row-item")
            check("角色绑定行已渲染（探针桩给了 1 个角色）", rows.count() >= 1, f"{rows.count()} 行")
            edit_btns = page.get_by_role("button", name="编辑", exact=True)
            check("角色行有「编辑」按钮", edit_btns.count() >= 1)
            edit_btns.first.click()
            page.wait_for_timeout(600)
            check(
                "角色弹窗里模型名是**下拉**（不再是只能手填的输入框）",
                page.locator(".model-select").count() == 1,
            )
            prefetched = [c for c in calls(page) if "probe-provider/models" in c]
            check(
                "打开角色弹窗即静默预取模型清单（不必先点按钮）",
                len(prefetched) >= 1,
                str(prefetched),
            )
            get_btn = page.locator(".model-row").get_by_role("button", name="获取模型")
            check("角色弹窗有「获取模型」按钮", get_btn.count() == 1)
            before = len([c for c in calls(page) if "probe-provider/models" in c])
            if get_btn.count():
                get_btn.first.click()
                page.wait_for_timeout(800)
            after = len([c for c in calls(page) if "probe-provider/models" in c])
            check(
                "点「获取模型」真的再发一次请求（刷新清单）",
                after > before,
                f"{before} → {after}",
            )
            page.locator(".model-select").click()
            page.wait_for_timeout(500)
            options = page.locator(".n-base-select-option")
            texts = options.all_inner_texts()
            check(
                "下拉候选 = 接入点接口返回的模型清单",
                any("probe-large" in t for t in texts) and any("probe-small" in t for t in texts),
                str(texts),
            )
            page.get_by_text("probe-large", exact=True).first.click()
            page.wait_for_timeout(400)
            check(
                "选中后模型名落到下拉框上",
                "probe-large" in page.locator(".model-select").inner_text(),
                page.locator(".model-select").inner_text(),
            )
            page.screenshot(path=str(SHOTS / "dialogs-e1-model-pick.png"))
            page.get_by_role("button", name="保存", exact=True).click()
            page.wait_for_timeout(700)
            check(
                "保存后写回角色绑定（PUT /api/model-config/roles/{role}）",
                any(c.startswith("PUT /api/model-config/roles/") for c in calls(page)),
                str([c for c in calls(page) if c.startswith("PUT /api/model-config/roles/")][-1:]),
            )

            # 接入点弹窗：用**还没保存**的表单值拉清单，且不得把 ${ENV_VAR} 占位符当密钥回传。
            # 由来（2026-09-21 线上实测）：引擎对占位符不掩码，编辑弹窗原样回传，
            # 后端若当成字面量 Key，一个可用的 deepseek 接入点会报「环境变量未设置」。
            page.get_by_role("button", name="编辑", exact=True).last.click()
            page.wait_for_timeout(500)
            check("接入点弹窗有「获取模型」按钮（在保存/取消左侧）",
                  page.locator(".foot-left").count() == 1)
            if page.locator(".foot-left").count():
                page.locator(".foot-left").click()
            page.wait_for_timeout(900)
            # 不锁定具体接入点名：探针有多个接入点时，「编辑」按钮取到的是最后一行那个
            with_query = [c for c in calls(page) if re.search(r"providers/[^/]+/models\?", c)]
            last = with_query[-1] if with_query else ""
            check(
                "接入点弹窗的「获取模型」带上了表单里的 type/base_url（未保存也能拉）",
                "type=openai_compat" in last and "base_url=" in last,
                last or "没有带查询参数的请求",
            )
            check(
                "不回传占位符密钥（${PROBE_API_KEY} 不被当字面量发出去）",
                bool(last) and "api_key=" not in last and "%24" not in last,
                last or "缺失",
            )
            hint = page.locator(".fetch-hint")
            check(
                "接入点弹窗回显清单条数",
                hint.count() == 1 and re.search(r"\d+ 个可选", hint.inner_text()) is not None,
                hint.inner_text() if hint.count() else "缺失",
            )

            # 自定义请求头（2026-09-21 用户实测：opencode-go 的 /models 200、
            # /chat/completions 400 MissingSessionID —— 缺的就是这个头）
            hdr_box = page.locator(".headers-input textarea")
            check("接入点弹窗有「自定义请求头」输入框", hdr_box.count() == 1)
            check(
                "已存的请求头回显在输入框里",
                hdr_box.count() == 1 and "x-opencode-session" in hdr_box.input_value(),
                repr(hdr_box.input_value()) if hdr_box.count() else "缺失",
            )
            quick = page.get_by_role("button", name="填入 OpenCode 常用头")
            check("有「填入 OpenCode 常用头」一键按钮", quick.count() == 1)
            if quick.count():
                quick.click()
                page.wait_for_timeout(300)
            check(
                "一键填入写入会话头与自定义 User-Agent",
                "x-opencode-session" in hdr_box.input_value()
                and "User-Agent" in hdr_box.input_value(),
                repr(hdr_box.input_value()),
            )
            page.get_by_role("button", name="保存", exact=True).click()
            page.wait_for_timeout(800)
            hdr_calls = [c for c in calls(page) if c.startswith("HEADERS ")]
            check(
                "保存接入点时请求头确实被提交（PUT 带 headers）",
                bool(hdr_calls) and "x-opencode-session" in hdr_calls[-1],
                str(hdr_calls[-1:]),
            )
            page.screenshot(path=str(SHOTS / "dialogs-e1-provider-models.png"))
            # 「保存」已经把接入点弹窗关掉了，这里只关外层「模型配置」
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)

            print("\n[E1b] 模型配置 · 角色行内联直接选模型（打开即有选项）")
            page.get_by_role("button", name="模型配置").first.click()
            page.wait_for_timeout(1000)  # 等打开时的自动预取把清单备好
            rows_sel = page.locator(".row-model")
            check("每个角色行都有一个模型下拉", rows_sel.count() == 3, f"{rows_sel.count()} 个")
            first_sel = rows_sel.first
            check(
                "行内下拉显示的就是当前绑定",
                first_sel.inner_text().strip() == "probe-master-model",
                first_sel.inner_text(),
            )
            first_sel.click()
            page.wait_for_timeout(600)
            menu = page.locator(".n-base-select-option").all_inner_texts()
            check(
                "点开就有候选（不是空列表）",
                any("probe-large" in t for t in menu) and any("probe-small" in t for t in menu),
                str(menu),
            )
            page.get_by_text("probe-large", exact=True).first.click()
            page.wait_for_timeout(900)
            check(
                "选中即落库（PUT /api/model-config/roles/{role}）",
                any(c.startswith("PUT /api/model-config/roles/") for c in calls(page)),
                str([c for c in calls(page) if c.startswith("PUT /api/model-config/roles/")][-1:]),
            )
            check(
                "行内下拉随之显示新模型",
                page.locator(".row-model").first.inner_text().strip() == "probe-large",
                page.locator(".row-model").first.inner_text(),
            )
            page.screenshot(path=str(SHOTS / "dialogs-e1b-row-model.png"))

            # 行内换接入点：新增接入点后必须能在角色行里选到它（2026-09-21 用户反馈：
            # 「新增了一个接入点，但智能体换不过去，还是只有 deepseek」——行内原先只给选模型）
            prov_sel = page.locator(".row-provider")
            check("每个角色行也有接入点下拉", prov_sel.count() == 3, f"{prov_sel.count()} 个")
            check(
                "接入点下拉列出全部接入点（不只当前那一个）",
                prov_sel.first.inner_text().strip() == "probe-provider",
                prov_sel.first.inner_text(),
            )
            prov_sel.first.click()
            page.wait_for_timeout(500)
            prov_opts = page.locator(".n-base-select-option").all_inner_texts()
            check(
                "新增的接入点在候选里（probe-second）",
                any("probe-second" in t for t in prov_opts),
                str(prov_opts),
            )
            # 必须点**下拉里的那一项**：接入点清单区里也有同名的标签，
            # 用 get_by_text 会先命中下面那个标签，点了等于没点（实测踩过）。
            page.locator(".n-base-select-option", has_text="probe-second").first.click()
            page.wait_for_timeout(1200)
            put_calls = [c for c in calls(page) if c.startswith("PUT /api/model-config/roles/")]
            check(
                "换接入点即落库（PUT /api/model-config/roles/{role}）",
                bool(put_calls),
                str(put_calls[-1:]),
            )
            check(
                "换接入点后自动拉新接入点的模型清单",
                has_call(page, "probe-second/models"),
                str([c for c in calls(page) if "probe-second" in c][-2:]),
            )
            check(
                "模型跟着落到新接入点上真实存在的型号",
                page.locator(".row-model").first.inner_text().strip() == "probe-alt-1",
                page.locator(".row-model").first.inner_text(),
            )
            check(
                "行内接入点下拉显示新接入点",
                page.locator(".row-provider").first.inner_text().strip() == "probe-second",
                page.locator(".row-provider").first.inner_text(),
            )
            page.screenshot(path=str(SHOTS / "dialogs-e1b-row-provider.png"))
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
