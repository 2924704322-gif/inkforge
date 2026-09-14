# ui-probe —— 真实浏览器 UI 回归探针

> 开发期验证脚手架，**不参与产品构建**（`electron-vite` 不引用它；`out/` 已被 .gitignore 排除）。

## 它解决什么问题

Electron 渲染层无法在 CI 里跑端到端 UI 测试，而 naive-ui 的对话框有一类**只在运行时才暴露**的坑
（面板是否真的有背景、遮罩叠了几层、某个 prop 是否真的被识别）。这类问题静态检查和类型检查都查不出来。

本探针用一个真实 Chromium 挂载**真实的**渲染层组件（不是仿写），只把 Electron 的
`window.inkforge` 桥替换成内存桩，然后对截图做**像素采样**，把「肉眼观感」变成可断言的数字。

## 怎么跑

```bash
cd apps/desktop

# 1) 构建探针（产物到 out/ui-probe）
npx vite build --config ui-probe/vite.config.ts

# 2) 起静态服务 + 真实浏览器 + 像素断言（截图落 out/ui-probe-shots）
python ui-probe/ui_probe_check.py
```

依赖（不在项目依赖内，按需自装）：

- Python 3.10+ 与 `playwright`、`Pillow`
- Chromium：`playwright install chromium`

## 当前覆盖的用例

「书架 → 新建作品 → 创建（勾选先生成 Demo）」这条路径的向导弹窗，断言：

| 断言 | 期望 |
|---|---|
| 面板内部像素 | 不透明白色 `(255,255,255)` |
| 面板外遮罩像素 | 单层 `(153,153,153)`（双层叠加会是 `(92,92,92)`） |
| 页面遮罩元素数量 | `1` |
| 面板尺寸 | `860 × 86vh`（由 `style` 经 `$attrs` 落到面板根元素） |
| 可访问性 | `role="dialog"` / `aria-modal="true"` |
| 反证 | 注入 CSS 去掉背景后，面板内像素退化为遮罩灰（复现原缺陷） |

## 已知坑（写在这里，避免下次重踩）

1. **`NModal` 的遮罩 prop 叫 `show-mask`，不是 `mask`。** naive-ui 2.44.1 的 `modalProps`
   里只有 `showMask` / `maskClosable`（`unstable-show-mask` 已废弃）。写 `:mask` 不会被识别，
   会静默落入 `$attrs` 变成无效 DOM 属性 —— **遮罩照旧显示，且没有任何报错**。
2. **`role` 是 `NModal` 自己声明的 prop**，会被它消费，不会透传到面板元素；
   `aria-modal` 不是 prop 才会透传。可访问性属性建议直接写在自绘面板的根元素上。
3. **无 `preset` 的 `NModal` 不提供面板背景**。`.n-modal` 的默认样式只有
   `position / align-self / margin / box-shadow`，没有 `background`；背景由 `NCard`（`preset="card"`）
   或 `NDialog`（`preset="dialog"`）提供。自绘面板必须自己给 `background`。
4. **`content-style` 只在 `preset="card"` 时生效**（它属于 `presetProps = cardBaseProps ∪ dialogProps`），
   无 preset 时被静默忽略。
5. **`displayDirective` 默认是 `'if'`**：外层 `NModal` 一关闭，它的 slot 内容会被卸载，
   **嵌套在其中的内层 `NModal` 会跟着被销毁**。所以「向导」这类需要独立存活于父对话框之外的弹窗，
   不要长期依赖嵌套结构（本探针正是为了守住这条约束）。
