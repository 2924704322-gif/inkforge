# Inkforge 修改交接清单（HANDOVER）

> **给后续 agent 的阅读说明**：这是 2026-09-14「S1 止血 + S2 质量基座」升级批次的完整交接文档。
> 文档结构与 `docs/STATUS.md`（进度真源）、`docs/SMOKE_TEST_REPORT.md`（测试证据）配套。
> **先读 §0 和 §5**：§0 让你 30 秒内知道怎么验证，§5 列的是**改动了行为契约、不遵守就会出 bug** 的条目。

---

## §0 三十秒上手

```bash
cd E:\zcode-test\inkforge

# 1) 引擎依赖自检（5 秒，确认环境可跑）
python engine/scripts/verify_env.py

# 2) 引擎回归测试（248 例，约 27 秒，零真实 LLM 调用）
cd engine && python -m pytest -q && cd ..

# 3) 真实冒烟测试（84 项，真实进程 + 真实 HTTP，约 30 秒）
cd engine && python smoke_test.py && cd ..
#    追加 --with-llm 会多做 1 次真实模型调用（会计费）

# 4) 静态门禁（与 CI 一致）
cd engine && python -m ruff check src tests scripts && cd ..
python scripts/check_dep_manifest.py
python scripts/check_doc_paths.py

# 5) 前端
cd apps/desktop && npm run typecheck && npm run build
```

**期望全绿。** 任何一项红，先看 §9「已知坑」再动手改。

**改代码前必读**：`engine/src/memory/store_factory.py`（读写视图语义）、`engine/src/web/auth.py`（每个新端点自动受保护）、
`engine/tests/conftest.py`（测试隔离机制）。

---

## §1 当前状态

| 项 | 值 |
|---|---|
| 仓库 | `E:\zcode-test\inkforge`（本批次前**不存在 Git 仓库**） |
| 分支 | 默认分支，3 个提交，工作区干净 |
| 跟踪文件 | 142 |
| Python | 3.13.12（conda env `langchain1.2`） |
| 引擎路由数 | 76（基线 74 + `/api/ping` + `/api/history`） |
| 回归测试 | 248 例 / 210 个用例函数 / 5 个文件 |
| 真实冒烟 | 84 项 / 12 套件 |
| 前端 | Electron 42.5.0 + Vue 3.5.39 + Naive UI 2.44.1 |

### 提交历史（provenance）

| SHA | 内容 |
|---|---|
| `064d51d` | **建立版本控制基线**。注意：这次提交**已经包含**本批次的骨架性新增（`auth.py`、`store_factory.py`、`tests/`、`pyproject.toml`、`requirements.lock`、CI、守卫脚本、`docs/STATUS.md`）——因为它们在 `git init` 之前就已写好。**不要用提交边界来判断"哪些是新加的"**，以 §3 的文件清单为准。 |
| `4add783` | S1 止血 + S2 质量基座的主体变更（50 文件，+1369/−336） |
| `17defad` | 测试报告 + 旧报告归档 |

相对基线的净变化：**52 文件，+1659 / −379**。

---

## §2 变更总览（按关注点）

| 关注点 | 处置 | 主要文件 |
|---|---|---|
| 版本控制缺失（全项目零 Git） | 建立仓库 + `.gitattributes` 统一 LF + `.gitignore` 加固 | `.gitignore`、`.gitattributes` |
| 「每本书 Git 仓库」是死代码 | 新增 `store_factory`，按读写语义显式开启 | `engine/src/memory/store_factory.py`、`engine/src/memory/md_store.py` |
| Git 提交性能/正确性 | 纯 Python 索引操作 + 精确路径 add + 进程级仓库缓存 | `engine/src/memory/md_store.py` |
| 依赖清单不完整（装不起来） | 补齐 + 锁文件 + 自检 + CI 静态比对 | `engine/requirements.txt`、`requirements.lock`、`scripts/verify_env.py`、`scripts/check_dep_manifest.py` |
| 本地 HTTP 无鉴权 | Bearer token + Origin 拒绝中间件 | `engine/src/web/auth.py` |
| 任意文件读取 | 上传路径四重校验 + IPC 授权 | `engine/src/web/server.py`、`apps/desktop/src/main/index.ts` |
| 端口竞态 | 内核分配 + stdout 标记回报 | `engine/src/web/server.py`、`apps/desktop/src/main/engine-supervisor.ts` |
| 探活有写副作用 | 新增无副作用 `/api/ping` | `engine/src/web/server.py`、`engine-supervisor.ts` |
| 读路径写副作用 | 子目录惰性创建 + 读/写视图拆分 | `engine/src/memory/md_store.py`、4 个 web 模块 |
| 零测试 | 248 例回归 + 84 项真实冒烟脚本 | `engine/tests/`、`engine/smoke_test.py` |
| 无 CI/lint | 4 job workflow + ruff/mypy 配置 | `.github/workflows/ci.yml`、`engine/pyproject.toml` |
| 文档漂移 7 处 | 重写 README/ARCHITECTURE + 新增 STATUS | `README.md`、`docs/ARCHITECTURE.md`、`docs/STATUS.md` |

---

## §3 新增文件清单

### 3.1 引擎运行时代码

| 文件 | 行数 | 用途 | 扩展点 |
|---|---:|---|---|
| `engine/src/web/auth.py` | 95 | 本地 HTTP 访问控制：Bearer token 中间件 + Origin 拒绝 | 新增**公开**路径需改 `_PUBLIC_PREFIXES`；其余端点自动受保护 |
| `engine/src/memory/store_factory.py` | 41 | `MdStore` 构造工厂：`open_store(path, writable=...)` | 所有新建的 `MdStore()` 调用点都应改用它 |

### 3.2 测试与验收

| 文件 | 行数 | 用途 |
|---|---:|---|
| `engine/tests/conftest.py` | 166 | 沙箱夹具（`NOVELS_DIR`/`RUNTIME_DIR`/`CONFIGS_DIR` 重定向 + `get_settings.cache_clear()`）、`FakeIndex`、`app_client`、`seed_book` |
| `engine/tests/test_security.py` | 176 | 鉴权边界 + 上传路径四重校验（31 例） |
| `engine/tests/test_store_history.py` | 251 | MD 契约、Git 历史、读路径无副作用、**Git 索引卫生**、提交吞吐 |
| `engine/tests/test_memory_rules.py` | 356 | 伏笔四条防御、状态板、反剧透、完本扫描（36 例） |
| `engine/tests/test_api_contract.py` | 455 | 70+ 端点契约与校验（63 例） |
| `engine/tests/test_units.py` | 352 | 分块/RRF/diff/深合并/门禁/拓扑（49 例） |
| `engine/smoke_test.py` | 739 | **真实冒烟**：起真进程、真 HTTP、沙箱数据、84 项断言，输出 `engine/smoke-reports/smoke-*.json` |

### 3.3 工具与门禁

| 文件 | 用途 |
|---|---|
| `engine/scripts/verify_env.py` | 依赖自检：17 项必需 + 6 项可选 import 校验，失败非零退出 |
| `scripts/check_dep_manifest.py` | **静态比对** `requirements.txt` 与 `engine/src` 的顶层 import（P0-2 的根治性护栏） |
| `scripts/check_doc_paths.py` | 校验 README / ARCHITECTURE 中反引号路径真实存在（P1-2 漂移护栏） |
| `.github/workflows/ci.yml` | 4 个 job：python(lint+test+依赖自检) / desktop(typecheck+build) / guard(密钥+文档路径) / depsync |
| `engine/pyproject.toml` | 项目元数据 + pytest/ruff/mypy 配置（含 ruff 存量基线 ignore 说明） |
| `engine/requirements.lock` | 精确版本锁定（由验证环境导出） |
| `.gitattributes` | **统一 LF**（保护 `md_store` 的字节哈希一致性）、二进制标注、bat 保留 CRLF |

### 3.4 文档

| 文件 | 用途 |
|---|---|
| `docs/STATUS.md` | **进度单一真源**：里程碑完成度 + 本批次交付表 + 已知缺口优先级 + 数据规模 |
| `docs/SMOKE_TEST_REPORT.md` | 本批次测试报告（84/84 + 248/248 + 6 个已修缺陷 + 假故障排查记录 + 性能） |
| `docs/reports/04-冒烟测试报告-20260913.md` | 上一版报告归档 |

### 3.5 UI 回归探针（开发期脚手架）

| 文件 | 用途 |
|---|---|
| `apps/desktop/ui-probe/main.ts` | 挂载**真实**渲染层组件（只把 `window.inkforge` 换成内存桩） |
| `apps/desktop/ui-probe/index.html` / `vite.config.ts` | 独立构建入口，产物落 `apps/desktop/out/ui-probe`（已 gitignore） |
| `apps/desktop/ui-probe/ui_probe_check.py` | 起静态服务 + 真实 Chromium 走查 + **像素采样断言**，截图落 `out/ui-probe-shots` |
| `apps/desktop/ui-probe/README.md` | 跑法与 naive-ui 对话框坑位清单 |

**为什么存在**：Electron 渲染层没有端到端 UI 测试，而 naive-ui 对话框有一类只在运行时暴露的问题
（面板有没有背景、遮罩叠几层、某个 prop 是否真被识别）。这套探针把「肉眼观感」变成可断言的数字。
上线首日就抓出了本文档作者自己的两个错误（见 §9 第 10–11 条）。
依赖 Python `playwright` + `Pillow`（不在项目依赖内，按需自装）。已纳入 `tsconfig.web.json` 的 include，
因此 `npm run typecheck` 会一并校验，不会静默腐烂。

---

## §4 修改文件清单（改了什么 / 为什么）

### 4.1 `engine/src/memory/md_store.py`（+258/−60，本批次改动最大）

| 改动 | 原因 |
|---|---|
| 子目录**惰性创建**（`__init__` 不再 mkdir）+ 新增 `ensure_subdirs()` | 原来「列一次书架 = 一次写操作」 |
| 新增 `create_repo` 参数 | 读路径不得新建 `.git` |
| 新增模块级 `_REPO_CACHE` + `_resolve_repo()` / `_compute_repo()` | 原来每次构造都 spawn `git` 与 `git check-ignore` 子进程 |
| 新增 `_repo_scope` | 复用外层仓库时，历史必须按本书子目录过滤 |
| `_commit()` 改为**显式文件路径** + 纯 Python `index.add/remove/diff/commit` | ①不再 spawn 子进程 ②**绝不传 `.`**（见 §5.4） |
| Git 提交加 `_GIT_LOCK` | 并行起草时 index/commit 非线程安全 |
| 新增 `delete()` | 章节删除原来绕过本层，不进历史、哈希索引留悬空条目 |
| 新增 `history()` | 为 `GET /api/history` 提供 git log 只读视图 |
| 新增 `_to_repo_paths()` / `_safe_tree_paths()` | 路径换算 + `.git` 兜底过滤 |

### 4.2 `engine/src/web/server.py`（+87/−45）

- 新增 `APP_VERSION = "0.2.0"`、`_UPLOAD_SUFFIXES`、`UPLOAD_ROOTS_ENV`
- 新增 `GET /api/ping`（**无副作用健康探针**）
- 新增 `_resolve_upload_path()` / `_is_within()`（上传路径四重校验）
- `ReviewSession.store()` 改为**只读**视图；新增 `write_store()` 写视图
- `create_app` 末尾挂载鉴权中间件（在全部路由注册**之后**）
- `main()` 改用 `uvicorn.Config.bind_socket()` + `Server.run(sockets=[sock])`，打印 `INKFORGE_ENGINE_PORT=<n>`

### 4.3 web 扩展模块（读写视图拆分）

| 文件 | 改动 |
|---|---|
| `inkforge_api.py` | `_store(novel, writable=False)`；`settings_doc_save` / `chapter_save` / `bindings_set` 传 `writable=True` |
| `inkforge_extra.py` | `_store(novel, writable=False)`；`chapter_create` / `chapter_delete` 传 `writable=True`；`chapter_delete` 改走 `store.delete()`；新增 `GET /api/history` |
| `inkforge_proposals.py` | `_store_of(novel, writable=False)`；`_apply_proposal` 传 `writable=True` |
| `inkforge_windows.py` | `_overrides_path()` **自动建目录**（修 500）；`_store` 改 `writable=False`（仅看板用） |

### 4.4 其他引擎改动

| 文件 | 改动 |
|---|---|
| `orchestrator/bootstrap.py` | `open_store(..., writable=True)`（生成流水线是写路径） |
| `cli/main.py` | 3 处只读调用点改 `open_store(..., writable=False)` |
| `distillation/skill_store.py` | `_skills_dir()` 从硬编码 `PROJECT_ROOT` 改为 `get_settings().novels_dir.parent / "skills"`（可重定向 / 测试隔离） |
| `configs/base.yaml` | 修正 `distill.enabled` 注释与值自相矛盾 |
| 其余 ~34 个引擎源文件 | **仅 ruff 自动修复**（未使用 import 清理、import 排序、f-string 等），无逻辑变更 |

### 4.5 桌面壳

| 文件 | 改动 |
|---|---|
| `src/main/engine-supervisor.ts` | 每启动生成 32 字节随机 token 并注入子进程；`--port 0` + 解析 stdout 端口标记；健康检查改打 `/api/ping`；所有请求带 `Authorization`；新增授权路径集合 `grantedPaths` / `grantPath()` / `isGranted()`；`uploadRoots()` 注入 `INKFORGE_UPLOAD_ROOTS`；**后台 pump 线程持续排空 stdout/stderr** |
| `src/main/index.ts` | `validateApiPath()`（URL 解码后拒 `..`/`\`/控制字符）；8 MB body 上限；`authorizeUploadPath()`（蒸馏路径必须来自对话框授权）；`isSafeId()` 统一 ID 校验；`shell.openExternal` 协议白名单（仅 http/https）；`app:info` 增加 `enginePort` |
| `src/shared/bridge.ts` | `AppInfo` 增加 `enginePort: number` |
| `启动Inkforge.bat` | 解释器解析顺序改为 ①环境变量 ②本机默认路径 ③PATH 上的 `python`（不再硬失败） |

### 4.6 文档

| 文件 | 改动 |
|---|---|
| `README.md` | 重写：指标口径分离（母项目 vs 本项目）、修正 `LeftSidebar.vue` 等 7 处漂移、新增安全模型表、新增「已知缺口（诚实清单）」 |
| `docs/ARCHITECTURE.md` | 修正端点数量（40+ → 76）、移除已失效的 `v-html` 告警、新增 §5 读写语义表、新增 §6 访问控制、修正「引擎不动」表述、`LeftSidebar.vue` → 实际组件清单 |
| `.gitignore` | 排除 `.env.*`、`*.tsbuildinfo`、`engine/data/*`（全部用户数据）、`smoke-reports/`、`.smoke/` |
| `apps/desktop/src/renderer/src/components/` | 无改动 |

---

## §5 ⚠ 行为契约变更（**必须遵守**，违反会引入 bug）

### 5.1 `MdStore` 构造一律走工厂

```python
from src.memory.store_factory import open_store

open_store(path, writable=True)   # 写路径：挂 Git，缺失即 git init
open_store(path, writable=False)  # 读路径：只挂已存在的仓库，绝不新建
```

- **不要**再直接写 `MdStore(path, auto_git=...)`。
- **不要**给 GET 处理器传 `writable=True` —— 否则用户「打开一次软件」就会为每本书凭空创建 `.git`（这是本批次修掉的缺陷 3）。
- 全局关闭写作历史：`INKFORGE_GIT_HISTORY=0`。

### 5.2 `ReviewSession` 有读/写两个视图

```python
sess.store()        # 只读：状态、章节列表、概览（每 2.5s 被轮询）
sess.write_store()  # 写入：应用 Skill、写入设定 Demo
```

写操作**必须**用 `write_store()`，否则不产生 Git 提交。

### 5.3 `MdStore.__init__` 不再创建子目录

```python
store.ensure_subdirs()   # 需要 chapters/ settings/ summaries/ reviews/ 骨架时显式调用
```

`write()` 仍会自动创建目标文件的父目录链。建书向导（`POST /api/books`）已在创建时显式 mkdir。

### 5.4 `_commit` 绝不传 `.`

```python
self._commit(msg, [rel_path])                    # ✅ 精确文件
self._commit(msg, [rel_path], removed=True)      # ✅ 删除
self._commit(msg)                                # ⚠ 走 _safe_tree_paths 兜底（较慢，尽量别用）
```

**原因**：GitPython 的 `IndexFile.add()` 是纯 Python 实现，**不会**像 git porcelain 那样自动排除 `.git/`。
传 `.` 会把仓库自身 40 余个对象文件加进索引，导致每次提交重新遍历、开销 **O(n²) 累积**
（实测 60 次提交从 12s 恶化到 >240s 不收敛）。回归护栏：`TestGitIndexHygiene`。

### 5.5 新增 `/api/*` 端点会自动受鉴权保护

- 中间件在 `create_app` 末尾挂载，覆盖所有 `/api/*`；
- 需要**公开**的路径（无需 token）必须加入 `auth._PUBLIC_PREFIXES`（当前仅 `/`、`/docs`、`/redoc`、`/openapi.json`、`/favicon.ico`）；
- `INKFORGE_ENGINE_TOKEN` 未设置时鉴权关闭（CLI / pytest / 旧内置单页场景）——**这是有意的向后兼容，不是漏洞**。

### 5.6 `/api/ping` 必须保持「零副作用」

它同时是：
- Electron `EngineSupervisor.waitReady()` 的健康探针（每次启动轮询到 200）；
- 冒烟测试的握手校验点。

**不要**在它里面触碰任何 `MdStore` / 文件系统 / LLM。

### 5.7 引擎端口不再是固定的 8137

由内核分配，经 stdout 的 `INKFORGE_ENGINE_PORT=<n>` 标记回报。任何新代码
（测试脚本、调试工具、文档）**都不得硬编码端口**，必须从标记或 `app:info` 取。

### 5.8 蒸馏上传路径必须来自用户授权

链路：`dialog:pickBookFile` → `supervisor.grantPath()` → IPC `authorizeUploadPath()` → 引擎 `_resolve_upload_path()`。
新增任何「读取本机文件」的端点，都必须复用这套授权模型，不得直接接受客户端传入的路径。

### 5.9 进程 spawn 必须持续排空 stdout/stderr

否则管道缓冲区（Windows ~64KB）写满后子进程**阻塞在 write**，表现为「端口在监听但所有请求超时」。
生产代码已由 `engine-supervisor.ts` 的 `pipeLogs()` 保证；测试脚本见 `smoke_test.py` 的 `_pump()`。

---

## §6 环境变量总表

| 变量 | 默认 | 作用 | 何时设置 |
|---|---|---|---|
| `INKFORGE_PYTHON` | PATH 上的 `python` | 引擎解释器路径 | 用户/启动脚本 |
| `INKFORGE_ENGINE_TOKEN` | 空 | 引擎访问 token；**空 = 关闭鉴权** | Electron 主进程每启动轮换注入 |
| `INKFORGE_UPLOAD_ROOTS` | 空 | 上传路径白名单根目录（`os.pathsep` 分隔） | 主进程设为用户主目录 |
| `INKFORGE_GIT_HISTORY` | `1` | `0/false/off/no` 关闭每本书 Git 写作历史 | CI / 临时环境 |
| `NOVELS_DIR` / `RUNTIME_DIR` / `CONFIGS_DIR` / `PROJECT_ROOT` | `engine/data/novels` 等 | 数据与配置根目录（用于测试隔离与多实例） | pytest / smoke / 部署 |
| `APP_ENV` | `dev` | 分层配置选择（`dev`/`prod`） | 部署 |
| `LOG_LEVEL` | `INFO` | 日志级别 | `.env` |
| `DEEPSEEK_API_KEY` / `EMBEDDING_API_KEY` / `ANTHROPIC_API_KEY` | — | 模型与向量化密钥（`.env`，**不入库**） | 用户 |
| `MILVUS_HOST` / `MILVUS_PORT` | — | prod 档向量后端 | 部署（当前 `pymilvus` 未声明，prod 档不可用） |
| `ELECTRON_RENDERER_URL` | 空 | electron-vite 开发服务器地址 | `npm run dev` |

---

## §7 CI 门禁与如何满足

`.github/workflows/ci.yml` 有 4 个 job：

| Job | 检查 | 本地复现 |
|---|---|---|
| `python` | `verify_env.py` → `ruff check src tests scripts` → `mypy`（`continue-on-error`）→ `pytest -q` | `cd engine && python scripts/verify_env.py && python -m ruff check src tests scripts && python -m pytest -q` |
| `desktop` | `npm ci` → `npm run typecheck` → `npm run build` | 同 |
| `guard` | 明文密钥扫描（`sk-…` / `ghp_…` / `AKIA…`）+ `.env` 不入库 + 文档路径 | `python scripts/check_doc_paths.py` + `git grep -nE 'sk-[A-Za-z0-9_-]{20,}'` |
| `depsync` | 顶层 import ↔ `requirements.txt` 一致性 | `python scripts/check_dep_manifest.py` |

**新增第三方顶层 import 时**：必须同步更新 `engine/requirements.txt`（宽松范围）与
`engine/requirements.lock`（精确版本），否则 `depsync` 红。

**ruff 现状**：`ruff check` 全绿。`pyproject.toml` 里有一组**存量基线 ignore**
（`B904` 等 10 条），注释里写明了「新代码不再引入、存量随重构收敛」。**新增告警不要往 ignore 里加**。

**注意**：CI 目前**没有** `ruff format --check`（46 个文件会被重排）。若后续要接入，
必须作为**独立提交**全量格式化，避免污染业务 diff。

---

## §8 测试约定

### 8.1 加回归测试（`engine/tests/`）

```python
def test_something(app_client):          # TestClient + 沙箱 novels 目录 + 样例书 demo-web
    client, _ = app_client
    assert client.get("/api/books").status_code == 200

def test_memory_rule(make_memory):       # (MdStore, MemoryManager)，用 FakeIndex 免真实向量库
    store, memory = make_memory("my-book")
```

**硬性要求**：
1. **零真实 LLM 调用**。生成类路径用 `FakeIndex` / 直接构造请求体绕过；需要模型的场景标
   `@pytest.mark.llm` 并排除在默认运行外。
2. **不触碰真实数据**。必须用 `sandbox` / `app_client` / `make_memory` 夹具——
   它们通过环境变量重定向 `NOVELS_DIR`/`RUNTIME_DIR`/`CONFIGS_DIR` 并清空 `get_settings` 的 `lru_cache`。
   直接 `MdStore(Path("data/novels/..."))` 会污染真实作品目录（本批次踩过一次）。
3. 断言**不变量**（不产生脏数据、不泄露未来剧情、路径不穿越、断点不重复生成），
   **不要**断言具体提示词文本或评分数值——那会锁死后续调优空间。

### 8.2 加冒烟用例（`engine/smoke_test.py`）

每个套件是一个 `Suite`，用 `suite.check(名称, 条件, detail, evidence)` 记录。
新增套件后必须把结果 append 进 `report["suites"]`。沙箱默认在系统临时目录，
**不要**改到仓库内（会走「外层仓库跟踪本书」这条非典型分支，掩盖真实问题——本批次踩过一次）。

---

## §9 已知坑（本批次踩过的，避免重复）

| # | 坑 | 症状 | 规避 |
|---|---|---|---|
| 1 | 用 PowerShell `Set-Content -Encoding UTF8` 写 `.py`/`.ts`/`.md` | **写入 UTF-8 BOM**，`ast.parse` 报 `invalid non-printable character U+FEFF`，`check_dep_manifest.py` 直接崩 | 用编辑工具；必须用 shell 时用 `[System.IO.File]::WriteAllText($p, $t, [System.Text.UTF8Encoding]::new($false))`。已有检测：全仓搜 `EF BB BF` |
| 2 | 长命令管道接 `Select-Object -Last N` | **整条管道被缓冲**，进程结束前零输出，看起来像卡死 | 长任务走后台作业 + 流式读取 |
| 3 | 测试脚本不排空子进程 stdout | 引擎「在监听但不响应」，约 50 个请求后复发 | 后台 pump 线程持续排空（§5.9） |
| 4 | 用 `readline()` 等子进程输出 | 若子进程静默则阻塞到超时 | 用 `threading.Event` + 超时（见 `EngineProcess.start`） |
| 5 | 在脚本仓库内建测试沙箱 | 命中「外层仓库未忽略本书」分支，走到与生产不同的代码路径 | 沙箱放系统临时目录 |
| 6 | `git status` 显示 M 但 `git diff` 为空 | `.gitattributes` 的行尾归一化导致 stat 缓存过期 | 直接 `git add`，不是问题 |
| 7 | 误以为 `length_deviation` 带符号 | 它返回**绝对值**；方向信息在 `length_revision_note` 里 | 见 `test_length_deviation_is_absolute` |
| 8 | 误以为小语料上 BM25 有效 | `BM25Okapi` 在小语料上 IDF=0（术语出现在过半文档时 `log(1)=0`），`scores>0` 过滤后稀疏召回为空 | 测试语料至少 10 篇且目标词只出现一次（见 `test_bm25_only_result_survives`） |
| 9 | 在 `engine/tests` 里直接 import 未安装的可选依赖 | `ebooklib` / `pymilvus` / `FlagEmbedding` 在本机**未安装** | 它们是惰性 import 的可选依赖，不要在模块顶层引用 |
| 10 | **凭记忆写 naive-ui 的遮罩 prop** | 写 `:mask="false"` **不报错但完全不生效**：naive-ui 2.44.1 的 `modalProps` 只有 `showMask` / `maskClosable`（旧名 `unstable-show-mask` 已废弃），`mask` 会静默落入 `$attrs` 变成无效 DOM 属性，遮罩照旧显示 | 正确写法是 **`:show-mask`**；且 `showMask=false` 时 naive-ui 会**转而挂载 `clickoutside` 指令**，必须同步把 `maskClosable` 也关掉，否则点外部会关掉底层对话框 |
| 11 | 把 `role` 写在 `NModal` 上期望透传到面板 | `role` 是 `NModal` **自己声明的 prop**，会被消费掉，不会到子元素（`aria-modal` 不是 prop，才会透传） | 可访问性属性直接写在自绘面板的根元素上 |
| 12 | 用**无 `preset`** 的 `NModal` 装自绘面板 | 面板**全透明**：`.n-modal` 默认样式只有 `position/align-self/margin/box-shadow`，没有 `background`（背景由 `NCard`/`NDialog` 预设提供）→ 屏幕全灰、只有自带背景的输入框可见 | 自绘面板必须自己给 `background`；`content-style` 也只在 `preset="card"` 时生效，无 preset 时被静默忽略 |
| 13 | 把需要独立存活的弹窗嵌套在另一个 `NModal` 的 slot 里 | `displayDirective` 默认 `'if'`：**外层一关闭就卸载整个 slot，内层弹窗跟着被销毁** | 长时间运行的向导/流程弹窗应提到顶层（或至少清楚这条耦合，别让外层被意外关闭） |

---

## §10 待办（按优先级）

| 优先级 | 事项 | 位置 |
|---|---|---|
| **P0** | **轮换 `.env` 中的真实密钥**（DeepSeek + 硅基流动）——已从版本控制排除并有 CI 扫描，但**轮换需人工操作** | `engine/.env` |
| P1 | UI 改为一律把 API Key 写 `${ENV}` 占位符 + 接入 Electron `safeStorage` | `server.py::model_config_upsert_provider`、`ModelConfigDialog.vue` |
| P1 | 引入 vitest，覆盖 `api.ts` 的 reactive Proxy 剥离（历史阻断缺陷）与 `DiffView` | `apps/desktop` |
| P1 | 契约单一真源：JSON Schema → TS + pydantic 双向生成 + CI diff 校验 | 新增 `packages/schemas/` |
| P1 | 长书性能：`detect_changed()` 每章全书 sha256 扫描 → 加 mtime+size 快速判据 | `engine/src/memory/md_store.py` |
| P1 | BM25 缓存键去掉 `max_chapter`，改检索后过滤 | `engine/src/memory/hybrid.py` |
| P2 | SSE 推送替代轮询（1.5–2.5s） | 新增 `/api/stream/{novel}` |
| P2 | 人审 diff / 评分历史落盘（现仅内存，重启即丢） | `server.py::ReviewSession` |
| P2 | `SessionHub` 加 LRU 上限 | `server.py::SessionHub` |
| P2 | Token 成本记账 + 预算熔断 | `llm/registry.py` + 看板 |
| P3 | 死资产约 900 行（`web/page.py`、`milvus_index.py`、`translate.py`、`image_gen.py`）移入 `legacy/` | — |
| P3 | 巨型单文件拆分（`server.py` 1767 行、`ChatPanel.vue` 930 行） | — |
| P3 | `ruff format` 全量格式化（独立提交） | 46 个文件 |

完整版见 `docs/STATUS.md` §三。

---

## §11 看起来像 bug 但不是

| 现象 | 真相 |
|---|---|
| `engine/data/novels/demo-web/` 的内容全部同一天同一秒写入 | **样例数据**（2026-09-13 13:55 一次性种入），**不是本机流水线产出**。流水线从未在本机跑过完整 30 章 |
| README 提到母项目「30 章完本 / 一次通过率 100%」 | 那是 **novel_agent2.1 的验收结论**（继承资产的能力声明），本项目 README 已明确标注口径分离 |
| `PUT /api/agent-presets/{key}` 以前恒 500 | 已修（缺陷 1）。若复现，检查 `_overrides_path()` 的 mkdir |
| `INKFORGE_ENGINE_TOKEN` 未设时端点无需 token | **有意设计**（CLI/pytest/旧单页场景），见 §5.5 |
| `engine/data/skills/` 在 `.gitignore` 里 | 蒸馏产物可由 `distill run` 重建，不入库是刻意的 |
| `prod.yaml` 声明 milvus 但装不上 | `pymilvus` 在 `requirements.txt` 中被注释（版本约束复杂），**prod 档当前不可用**，属已知缺口 |
| `engine/smoke_chat_flow.py` 依赖真实 LLM | 它是历史手工脚本，**不是回归测试**；真正的回归测试在 `engine/tests/` |

---

## §12 回滚

```bash
cd E:\zcode-test\inkforge

# 只回滚本批次的功能改动，保留版本控制基线
git revert --no-commit 4add783 && git commit

# 彻底回到"什么都没有的原始状态"（丢弃全部新增）
git reset --hard 064d51d          # 基线（含新增文件）
# 或完全移除版本控制：
rm -rf .git .gitattributes        # 会丢失全部历史，慎用

# 运行时数据不受影响：engine/data/ 从未入库
```

**注意**：`.gitignore` 排除了 `engine/data/`，所以任何 `git reset` 都不会动用户作品数据。
但**每本书目录内可能已有独立 `.git`**（写作历史），若要彻底清理需单独删除。

---

## §13 关键文件速查

| 想做什么 | 看哪个文件 |
|---|---|
| 改生成流水线 | `engine/src/orchestrator/graph.py`（串行）、`parallel_runner.py`（卷级并行） |
| 改审校规则 | `engine/src/agents/prompts/editor_review.md` + `agents/editor.py` |
| 改写作提示词 | `engine/src/agents/prompts/writer_chapter.md` |
| 改记忆召回 | `engine/src/memory/memory_manager.py`、`hybrid.py` |
| 改伏笔/状态板规则 | `engine/src/memory/memory_manager.py`（`_apply_foreshadow_ops` / `apply_state_ops`） |
| 改 Git 写作历史 | `engine/src/memory/md_store.py`、`store_factory.py` |
| 加 API 端点 | `engine/src/web/server.py` 或 `inkforge_*.py`（记得读写视图与鉴权） |
| 改安全边界 | `engine/src/web/auth.py`、`apps/desktop/src/main/index.ts` |
| 改 IPC 契约 | `apps/desktop/src/shared/bridge.ts`（真源）→ 同步 `preload/index.ts`、`api.ts`、`types.ts` |
| 改引擎监督 | `apps/desktop/src/main/engine-supervisor.ts` |
| 加测试 | `engine/tests/`（夹具见 `conftest.py`） |
| 加冒烟用例 | `engine/smoke_test.py` |
| 改对话框 / 排查 UI 观感问题 | `apps/desktop/ui-probe/`（真实浏览器 + 像素断言，见其 README 的坑位清单） |
| 查全部提示词位置 | `engine/src/agents/prompts/`、`engine/src/distillation/prompts/`、`engine/src/web/inkforge_api.py`（`AGENT_PRESETS` / `SUB_AGENTS`）、`inkforge_windows.py` |
