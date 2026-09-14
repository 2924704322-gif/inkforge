# Inkforge（墨锻）

> 长篇小说智能创作工作台 —— 由 **DeepWrite**（协作工作台）与 **novel_agent2.1**（自动化生产线）两个项目蒸馏合并而成的新项目。

Inkforge 既有**全自动生产线**（大纲 → 写作 → 四维审校 → 人审 → 摘要 → 伏笔闭环），又有 **Codex 式的三栏协作工作台**（左资源树 / 中对话 / 右编辑器 + 审阅卡）。所有作品与技能以**本地 Markdown 文件夹为唯一事实源**，可 Git 管理、可迁移、不锁定在任何云端。

> **指标口径说明（重要）**：本项目 README 只陈述**本项目实测**过的数据。
> 母项目（novel_agent2.1）的验收结论「30 章完本、一次通过率 100%、伏笔回收率 100%」
> 属于**继承资产的能力声明**，不是本项目在桌面端复现过的结果。
> 本项目当前实测覆盖见 [docs/SMOKE_TEST_REPORT.md](docs/SMOKE_TEST_REPORT.md) 与 [docs/STATUS.md](docs/STATUS.md)。

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

- 继承自 **DeepWrite**：三栏产品形态、安全边界（Renderer 无 Node/文件系统/密钥，Main 校验一切跨边界请求）、系统对话框授权模式、引擎子进程监督与自动重启。
- 继承自 **novel_agent2.1**：生成流水线与提示词资产、Editor 四维评分 + 分级打回 + 磋稿协商、三层记忆、NDS 蒸馏、书架多书管理、按角色绑定模型。
- 详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) 与 [docs/STATUS.md](docs/STATUS.md)（逐里程碑的真实完成度）。

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
└── docs/                # 架构文档、状态表、测试报告
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
5. 一切数据在 `engine/data/` 下：`novels/<id>/`（章节/大纲/角色/伏笔，**每本书一个独立 Git 仓库**）、`skills/`（技能包）、`custom_skills/`（自定义创作约束）。

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
> 缓解计划见 [docs/STATUS.md](docs/STATUS.md) 的「已知缺口」段。

## 蒸馏溯源（本项目从哪里来）

| 能力 | 来源 |
| --- | --- |
| 三栏工作台、引擎监督、IPC 白名单、系统对话框授权 | DeepWrite（重写为精简版） |
| LangGraph 流水线、四维审校、分级打回、磋稿协商、三层记忆、NDS 蒸馏、Skill 框架、书架、模型角色绑定 | novel_agent2.1（引擎主体保留） |
| 设定 Demo 审核向导、审阅卡（评分 + diff + 通过/打回）、指标看板、提案-审阅流、对话智能体编排 | 两者融合的新实现 |

## 已知缺口（诚实清单）

- **测试**：引擎已有 238 条零 LLM 回归用例，**桌面壳前端仍无自动化测试**；
- **性能**：`detect_changed()` 每章做全书哈希扫描，长书（数百章）下会明显劣化；
- **进度推送**：引擎无 SSE/WebSocket，UI 采用 1.5–2.5s 轮询；
- **打包分发**：PyInstaller sidecar + electron-builder 尚未落地，安装包形态待做；
- **密钥管理**：尚未接入 `safeStorage`，`.env` 明文存放；
- **契约**：TS 与 Python 两侧类型仍为手写双份，JSON Schema 单一真源待落地。

## License

私人项目，未附加开源许可证。
