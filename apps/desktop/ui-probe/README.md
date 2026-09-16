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

# 3) 人审关卡布局探针（几何量测，见下节）
python ui-probe/review_probe_check.py
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

## 人审关卡布局探针（`review_probe_check.py` + `review-main.ts`）

挂载**真实的 `App.vue` 整个外壳**，只把桥桩换成内存实现，让 `/api/status` 返回一个
`chapter_review` 待审快照（正文是探针自造的填充文本，**不读任何真实作品数据**），
然后在 5 个视口 × 3 组分栏宽度下量测几何：

- `#chapter`（默认）章节审阅分支；`#outline` 大纲审阅分支。
- `#proposal` / `#outline-proposal`：再叠一张待审的**改稿提案卡**
  （标题沿用引擎命名 `outline · 改稿提案`），用于验证提案卡是否插回对话流原位。

| 断言 | 期望 |
|---|---|
| **回归护栏（决定退出码）** | |
| 正文预览框高度 | ≥ 120px（回归护栏：修复前实测 22px） |
| 意见框与正文预览框的重叠面积 | `0`（修复前 6490–8294px²） |
| 评分行 / 审阅意见框在首屏 | 都可见（1366×768：评分 y=86、意见 y=196，首屏 48..645） |
| 大纲 JSON 容器高度 | ≤ 470px（46vh；修复前 max-height 失效被撑到 530px） |
| 大纲审阅分支的卡片高度 | 由内容决定，既不压扁也不长出空白 |
| 提案卡在对话流中的位置 | 落在创建时刻的两条消息之间，**不再挂在最后一条消息下方** |
| `treeVersion` 自增后正文编辑器 | 重新请求 `/api/chapters/{n}/raw` 并刷新正文（改稿后能看到新正文） |
| **已知缺陷（另行跟踪，不影响退出码）** | |
| 三栏总宽 | 超出窗口、右侧「创作空间」被推出窗口 —— 属 `App.vue` 三栏固定宽度（`flex: 0 0 <w>px`）的独立缺陷，未在本探针的修复范围内 |

1600×1000 = `src/main/index.ts` 里 `BrowserWindow` 的默认尺寸；分栏宽度会落
`localStorage`（`inkforge.w.editor` / `inkforge.w.tree`），所以同一窗口尺寸下
用户拖过分栏也会得到不同结果，探针用 `add_init_script` 预置这两个键来覆盖。

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
6. **定高列向 flex 容器会「先压扁子项，再滚动」。** `.stream`（对话流）是
   `flex:1; min-height:0; overflow-y:auto; display:flex; flex-direction:column`。
   它的子项默认 `flex-shrink:1`，容器高度不够时浏览器**先压缩子项**，只有压缩到子项的
   最小尺寸后才会产生滚动条。谁的最小尺寸是 0，谁就被压到 0：
   - `overflow-y:auto` 的子项（如正文预览框）自动最小尺寸 = 0；
   - 再加一句 `min-height: 0` 的卡片，连 `min-height:auto` 的兜底都没了。
   实测：1366×768 下审阅卡的正文预览框被压成 **22px 高**，整条对话流只有 74px 可滚，
   正文实际上**读不到**。
   修法：在容器侧写 `.stream > * { flex-shrink: 0 }`，让容器老老实实滚动；
   在子项侧给硬地板（`min-height`），别只给 `max-height`。
7. **`position: sticky; bottom: 0` 的「悬浮操作区」会一直盖住滚动内容。**
   审阅意见框用 sticky 悬浮在卡片底部，设计意图是「无论正文多长都可见」，
   代价是它永远浮在视口底部那一层内容之上——卡片被压扁时正好糊住正文预览框
   （实测重叠 6490–8294px²）。除非确实要做悬浮层，否则用正常文档流的页脚。
8. **把卡片统一渲染在消息列表之后 = 卡片「永远挂在最下面」。**
   `ProposalCard` 原来是 `v-for` 在全部消息之后，于是任何一张待审提案都不在
   它产生的位置：后续消息全排在它上面，而每次发送后的 `scrollBottom()` 又把视野
   拽到流底，正好停在提案卡上。修法是按时间戳（提案 `created` ↔ 消息 `ts`，同一时钟域，
   重启后依然成立）把卡片插回原位，见 `ChatPanel.vue::proposalsByAnchor`。
9. **`NScrollbar` 拿不到父组件的 scoped 属性。** naive-ui 的 `NScrollbar` 由 `vueuc`
   渲染，父组件写 `<NScrollbar class="outline-scroll">` 时，class 到了根元素上
   （`class="outline-scroll n-scrollbar"`），但 `data-v-xxx` **没有**——
   于是 `.outline-scroll { max-height: 240px }` 静默失效，容器被内容撑到 530px。
   实证：同一张卡里的 `NAlert`（`.issue-box`）拿到了 `data-v-xxx`，规则正常生效。
   解法：外面套一个普通元素（如 `<div class="outline-scroll">`）承载尺寸，
   内层 `NScrollbar` 用 `style="height: 100%"`。
10. **比视口高的卡片会把「顶部信息」滚出视野。** 审阅卡一屏放不下时，对话流默认停在
   最底部，长在卡片顶部的评分与审阅意见就"看不见了"。`ChatPanel` 现在在待审出现时
   用 `scrollToReviewTop()` 对齐卡片顶部，而不是停在流底。
