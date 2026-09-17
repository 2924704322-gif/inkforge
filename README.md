# Inkforge（墨锻）

> 长篇小说智能创作工作台 —— 由 **DeepWrite**（协作工作台）与 **novel_agent2.1**（自动化生产线）两个项目蒸馏合并而成的新项目。

Inkforge 既有**全自动生产线**（大纲 → 写作 → 四维审校 → 人审 → 摘要 → 伏笔闭环），又有 **Codex 式的三栏协作工作台**（左资源树 / 中对话 / 右编辑器 + 审阅卡）。所有作品与技能以**本地 Markdown 文件夹为唯一事实源**，可 Git 管理、可迁移、不锁定在任何云端。

> **指标口径说明（重要）**：本项目 README 只陈述**本项目实测**过的数据。
> 母项目（novel_agent2.1）的验收结论「30 章完本、一次通过率 100%、伏笔回收率 100%」
> 属于**继承资产的能力声明**，不是本项目在桌面端复现过的结果。
> 本项目当前实测覆盖记录在本地 `docs/`（内部文档，不随本仓库分发）。

## 架构一图流

```
┌─────────────────────────────── Electron 桌面壳（TS） ───────────────────────────────┐
│  Vue 3 渲染层（功能导航 · 三栏工作台 · 书架 · 蒸馏工坊 · 设置）                       │
│      │ window.inkforge 语义 API（contextBridge 白名单，无 Node/fs）                  │
│  Electron Main（信任汇聚点：API 路径校验 · 系统对话框授权 · 引擎监督 · token 注入）    │
└──────────────────────────────────────────┬────────────────────────────────────────┘
                                           │ 本地 HTTP（127.0.0.1，仅 /api/*，Bearer 鉴权）
┌──────────────────────────────────────────▼────────────────────────────────────────┐
│  Python 引擎 sidecar（FastAPI）                                                     │
│  LangGraph 流水线（Architect→Writer→Editor→Summarizer，人审 interrupt）             │
│  三层记忆（MD 事实源 + 向量/BM25 混合检索 + 状态板/伏笔台账）                         │
│  NDS 16 维蒸馏（书籍 → 技能包）· Skill 插件 · 按角色绑定模型                          │
└───────────────────────────────────────────────────────────────────────────────────┘
```

> **接手开发请先读 [HANDOVER.md](HANDOVER.md)**：修改交接清单，含验证命令、行为契约变更（必须遵守）、环境变量总表、CI 门禁、已知坑与待办优先级。

## 目录结构

```
inkforge/
├── apps/desktop/        # Electron 壳（main / preload / renderer）
├── engine/              # Python 引擎 sidecar（自包含：src + configs + data + tests + scripts）
│   ├── src/             #   流水线 / 记忆 / 蒸馏 / Skill / Web API
│   ├── configs/         #   分层配置 + models.yaml（按角色绑定模型）
│   ├── tests/           #   回归测试（零真实 LLM 调用）
│   ├── scripts/         #   verify_env.py（依赖自检）
│   └── data/            #   用户数据：novels（MD 事实源）、skills（技能包）、custom_skills
├── .github/workflows/   # CI：lint + 测试 + 依赖一致性 + 密钥扫描
├── scripts/             # 仓库级守卫脚本（依赖清单比对 / 文档路径校验）
└── docs/                # 本地内部文档（架构/状态/测试报告；不入本仓库）
```

## 快速开始

### 环境要求

- Node.js 18+（开发验证环境：24.15）
- Python 3.12+（开发验证环境：3.13.12 / conda env `langchain1.2`）
- 复制 `engine/.env.example` 为 `engine/.env`，填入模型 API Key（如 `DEEPSEEK_API_KEY`）

### 安装

```bash
cd apps/desktop && npm ci

# 引擎依赖（二选一，推荐锁文件保证与验证环境一致）
pip install -r engine/requirements.lock
# 或宽松范围：
pip install -r engine/requirements.txt

# 自检：确认所有必需依赖就绪（5 秒内暴露漏装问题）
python engine/scripts/verify_env.py
```

> **为什么需要 `verify_env.py`**：早期版本的 `requirements.txt` 漏声明了
> `fastapi` / `uvicorn` / `markdown` / `python-dotenv` 四个硬依赖，
> 导致按文档安装后引擎根本起不来。现在 CI 会在 `scripts/check_dep_manifest.py`
> 中静态比对「顶层 import」与「依赖清单」，此类问题不会再溜过去。

### 指定引擎 Python 解释器

引擎由桌面壳作为子进程拉起。通过环境变量 `INKFORGE_PYTHON` 指定解释器（不设则用 PATH 中的 `python`）：

```bash
set INKFORGE_PYTHON=C:\Users\<you>\.conda\envs\langchain1.2\python.exe
```

Windows 下可直接双击根目录 `启动Inkforge.bat`（自动设置解释器 → 启动应用 → 引擎随后自动拉起）。

### 开发 / 构建 / 测试

```bash
cd apps/desktop
npm run dev          # 开发模式（热更新）
npm run build        # 生产构建
npm run typecheck    # vue-tsc + tsc 双重类型检查

cd engine
pytest -q            # 引擎回归测试（零真实 LLM 调用，< 60s）
python scripts/verify_env.py --optional
```

### 使用流程

1. **设置** → 填好接入点 API Key、确认各角色模型绑定；
2. **书架** → 新建作品（填写 brief；推荐勾选「先生成设定 Demo」逐字段审核世界观与人物）；
3. **工作台** → 大纲人审（通过/打回附意见）→ 章节循环：每稿即落盘，Editor 四维评分 + 问题清单 + 与上一稿的行级 diff，通过才定稿，打回可定向修订或整章重写；
4. **蒸馏工坊** → 上传一本完整书籍（TXT/EPUB）→ 16 维蒸馏 → 得到可复用、可导出的技能包；
5. 一切数据在 `engine/data/` 下：`novels/<id>/`（章节/大纲/角色/伏笔，**每本书一个独立 Git 仓库**）、`workspace/chats/`（工作区会话）、`skills/`（技能包）、`custom_skills/`（自定义创作约束）。

### 墨师全域总控（不用先建书也能干活）

主智能体「墨师」不只在某一本书里工作：启动后**不强制建书**，直接进入**工作区**模式，用一句话就能办事。

- **能聊天、能查**：问"我有哪些书、进度如何"、"把 A 书第 3 章调出来"、"列出所有素材/技能包/约束"，
  墨师会自己调用只读动作取真实数据再回答（不靠记忆编造）；
- **能建书、能开写**：说"帮我建一本赛博修仙、30 章"→ 弹出**确认卡**（含将要做什么的影响说明）
  → 点「确认执行」才真正落盘，并自动把工作台切到新书；再说"开始写 5 章，卷级并行"→ 确认后启动、
  停在每章的人审关卡；
- **所有写动作都要你确认**：未确认前**磁盘零变化**；确认后才执行，执行结果与耗时记入审计日志
  （看板 →「墨师操作」页可查，含读取类动作）；
- **作用域隔离**：工作区会话存在 `data/workspace/chats/`，不隶属任何书；书内会话行为与改造前完全一致。

左侧功能区的 **「💬 主对话（工作区）」** 是"回到不隶属任何一本书的对话"的一键入口：

- 点它 → 切到工作区作用域 + 开一个**全新空白会话**，之后的指令不会被某本书的上下文污染；
- 上一段对话**不删除**：仍在「🕘 历史对话」面板里，点开即可继续；
- 工作区顶栏有对称的「↩ 回到《当前书》」，不用绕道书架。

> 对话改稿（✎ 改稿）以**右侧正在显示的文稿**为目标：提案卡上会写明"本次改的是哪份文稿
> （路径 + 原文字数）"；若右侧正文还没跟上你选中的文档，界面会告警并拦下这次发送，
> 避免把改动落到你没在看的文稿上。

## 安全模型

| 边界 | 机制 |
|---|---|
| Renderer → Main | `contextBridge` 白名单语义 API；`sandbox: true` + `contextIsolation: true` + `nodeIntegration: false`；CSP `default-src 'self'`；渲染层**无 `v-html`** |
| Main → 引擎 | 每次启动生成一次性 32 字节 token 注入子进程；引擎侧 `Bearer` 中间件校验，**并拒绝任何携带 `Origin` 头的请求**（挡住浏览器上下文） |
| 引擎端口 | 由内核分配（`--port 0`），经 stdout 结构化标记回报，消除端口探测竞态 |
| 文件读取 | 蒸馏输入路径必须来自系统对话框授权；引擎侧再做绝对路径 / 存在性 / 后缀 / 根目录包含性四重校验 |
| 外部链接 | `shell.openExternal` 仅放行 `http:` / `https:` |
| 密钥 | `.env` 不入库；CI 扫描明文密钥模式 |

> 仍未闭合：.env 目前以明文存放在工作区，且 UI 允许把明文 API Key 写入 `configs/models.yaml`。
> 缓解计划见本地 `docs/` 状态表的「已知缺口」段。

## 蒸馏溯源（本项目从哪里来）

| 能力 | 来源 |
| --- | --- |
| 三栏工作台、引擎监督、IPC 白名单、系统对话框授权 | DeepWrite（重写为精简版） |
| LangGraph 流水线、四维审校、分级打回、磋稿协商、三层记忆、NDS 蒸馏、Skill 框架、书架、模型角色绑定 | novel_agent2.1（引擎主体保留） |
| 设定 Demo 审核向导、审阅卡（评分 + diff + 通过/打回）、指标看板、提案-审阅流、对话智能体编排 | 两者融合的新实现 |

## 许可（License）

**源码可见，仅供非商业使用；不得再分发，也不得修改后发布。**

本项目采用 [**PolyForm Strict License 1.0.0**](LICENSE)（SPDX：`PolyForm-Strict-1.0.0`）：

- ✅ 阅读 / 学习 / 研究 / 在本机原样运行实验 / 个人业余用途；学校、科研、政府等机构的非商业使用；
- ❌ **二传再分发**（拷贝、上传、打包、转给他人）、**商用**（任何有商业预期的场景）、
  **修改后发布衍生作品**；
- 需要商用或二次开发 → 到 Issues 取得书面授权。

中文说明见 [`LICENSE-ZH.md`](LICENSE-ZH.md)（正式条款以 `LICENSE` 为准）。
