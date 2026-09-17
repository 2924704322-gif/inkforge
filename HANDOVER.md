# Inkforge 修改交接清单（HANDOVER）

> **给后续 agent 的阅读说明**：这是 2026-09-14「S1 止血 + S2 质量基座」升级批次的完整交接文档。
> **注意 `docs/` 是本地内部文档，不随公开仓库分发**（已被 `.gitignore` 排除）：
> 本文中所有 `docs/xxx.md` 的引用都指**本地检出**中的文件；在 fresh clone / CI 上不存在，
> `scripts/check_doc_paths.py` 已适配为"该目录不存在时跳过相关引用校验"。
> 文档结构与 `docs/STATUS.md`（进度真源）、`docs/SMOKE_TEST_REPORT.md`（测试证据）配套。
> **先读 §0 和 §5**：§0 让你 30 秒内知道怎么验证，§5 列的是**改动了行为契约、不遵守就会出 bug** 的条目。

---

## §0 三十秒上手

```bash
cd E:\zcode-test\inkforge

# 1) 引擎依赖自检（5 秒，确认环境可跑）
python engine/scripts/verify_env.py

# 2) 引擎回归测试（350 例，约 37 秒，零真实 LLM 调用）
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
| 回归测试 | 350 例 / 11 个测试文件（2026-09-17 两批次：320 → 336 → 350） |
| 真实冒烟 | 84 项 / 12 套件（+ 2026-09-17 新增 `smoke_master_converse.py`：37 项零 LLM / 58 项含真机） |
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

### 5.A2 2026-09-17 批次二：生成约束与逐章字数（用户三问修复）

**(1) `brief` 是书内事实源，不是运行参数**

- 位置 `settings/brief.md`；写入点：`POST /api/demo`、`POST /api/start`（`server._persist_brief`，
  **写失败只告警、不阻断生成**）；读取点：`MemoryManager.retrieve_context` 第 ⑧ 步直读。
- `ChapterContext.brief` 与 `custom_constraints` **同级并列**（都是"作者指定"，全量直读、不经检索/裁剪）。
- **新增/改动模板时必须同步三处**：模板加 `{{brief}}` → 渲染调用传 `brief=` →
  `prompt_loader._KNOWN_PARAMS` 登记；漏第三处会在**引擎启动期** Fail-Fast（这是刻意的，不是 bug）。
- 缺 brief.md 的旧书读到空串 → 模板渲染成"（未提供）"，**不得报错、不得跳过生成**。

**(2) 大纲打回走"定向修订"，禁止从零重生成**

- 打回 → `outline_feedback` + `outline_feedback_mode`（新增 state 字段）→ `architect_node`：
  `mode != "rewrite"` 且已有 outline 时**必须**调 `Architect.revise_outline(原大纲, 意见, mode, brief…)`。
- `targeted` 契约：**未点名的章节其 title/outline/characters/章号逐字保留**（真机验证 10/10）。
- 把意见**拼进 brief 字符串**是旧写法（只在 `rewrite` 分支保留）：那会让"人工意见冒充作者需求"，
  与 brief 保真条款冲突，且模型看不到原稿 → 整篇漂移（问题②根因）。
- 并行路径同构：`parallel_runner.ensure_outline` 也走 `revise_outline`，并把"上一版"推进为修订基线。

**(3) 逐章预期字数：单一目标，三处共用**

- 数据流：`ChapterPlan.target_words`（大纲阶段产出）→ `resolve_chapter_target(plan, 全局默认)`
  （**唯一解析入口**）→ `state.chapter_target_words` → writer `target_words_override` /
  editor `target_words=` / 字数门禁。
- 可改入口**只有两个**：逐章确认关卡（`chapter_gate` 的 `target_words`）与章节审阅卡打回时
  （`decision.target_words`）。其余地方不得各自解释目标字数。
- **写死一条**：`generation.tolerance_for(target)` = `max(word_count_tolerance, target*ratio)`；
  门禁判据与 `length_revision_note` 的"允许误差"必须用**同一个** tolerance，否则出现
  "模型按 ±500 写、门禁按 ±750 判"的互相打架。
- 目标字数落**章节 frontmatter `target_words`**（事实源：重启/看板/续跑都读它）。
- 串行/并行/互动**共用同一解析入口**，不得各写一份。

### 5.A 2026-09-17 批次新增契约（用户实测五问修复）

> 详细取证与验证见 `docs/STATUS.md` §三、`docs/11-对话链路修复报告-20260917.md`。

**(1) 写动作"预览期异常"不再等于失败**

```python
# actions._split 的 handler：
#   预览抛 ActionError / LibraryError / RuntimeError / FileNotFoundError
#   → 一律返回 status="pending_confirm"，把原因写进 pending["preview_error"] 与 impact
```

- **不要**再把写动作的预览异常改成 `status="failed"`：那会让闸门**不登记待确认**，
  用户既看不到确认卡也没有可点的按钮（"点了没反应"的直接来源，真实缺陷）。
- 真正执行（`execute_write=True`）时仍走 `_run_*`，失败照常 `status="failed"`。

**(2) 预览键由"规范化参数"派生，禁止再读可变状态**

```python
_preview_key(ctx, op, args)  # = f"{op}::{sha1(规范化 args)[:16]}"
```

- **禁止**在键里再引入 `ctx.book` / `ctx.active_novel()` / `session_key`：
  建书成功会 `set_active_novel(新书)`，而预览发生在建书之前 —— 键一变，
  确认就找不到登记 → 409、零落盘、待办还挂着（真实故障，`master-live-final6` 现场）。
- `consume_preview` 保留了一条"按 op 归并的旧键"兼容分支，新代码不要依赖它。

**(3) 参数名与取值折算集中在 `normalize_args`，且只做一次**

- `ARG_ALIASES`：写动作的参数名漂移（`book_create` 的 `id/book_key/title/novel/name` → `novel_id` …）。
- 占位符哨兵：`is_placeholder_value()` —— 模型把参数名当取值（`novel_id` / `<书名>`）时折成空串，
  绝不落盘（真实事故：曾建出一本名为 `novel_id` 的书）。
- `mode` 折算扩表 + 判不出来按默认 `pipeline`；**原话保留在 `mode_raw`**，确认卡上并列展示。
- **新增写动作时**：先想清楚"模型可能怎么写歪"，把它加进 `ARG_ALIASES` / 折算表，
  而不是让用户在确认卡上吃一个失败回执。

**(4) 对话里的长文本必须真的送达用户**

- `_content_echo()`：读类动作 `data.content` ≥120 字且回复里没有其开头片段时，
  **确定性追加**在回复末尾。禁止只把正文留在动作回执里（用户看不到）。

**(5) 改稿目标必须可核对**

- `_read_target` 强校验 kind/key 形状（`chapter`↔`ch-N`、`settings`↔`settings/**.md`），
  不符即 404，**绝不"猜一个最像的目标"**。
- 提案必须带 `targetPath` / `targetTitle` / `targetChars`，UI 必须展示；
  前端 `EditorPane` 维护 `appStore.docAligned`，选中态与已加载正文不同步时**拦截改稿**。

**(6) 主对话入口与会话作用域**

- `store.resetToMainChat()`（左侧「💬 主对话」）→ 切工作区 + 开全新空白会话。
- `loadChats({autoResume:false})`：**禁止**在用户主动回主对话后自动恢复旧会话；
  旧会话保留在「历史对话」面板，不做删除。

### 5.0 墨师全域化（P0–P4）：作用域哨兵 / 动作层 / 确认闸门

墨师现在可以在**工作区**（不隶属任何书）里工作：对话、查书目与资料、建书、开写。
以下四条是新增的硬契约，改动相关代码前必须读懂。

**(1) 作用域哨兵 `novel="__workspace__"`**

```python
from src.web.scope import WORKSPACE, is_workspace, resolve_book

resolve_book(novel, default_novel)
#  ""            → 默认书（旧语义，逐字不变）
#  "<book-id>"   → 指定书（旧语义，逐字不变）
#  "__workspace__" → ""（工作区不隶属任何书，**绝不**静默落到默认书）
```

- 哨兵值天然通过既有 `NOVEL_ID_RE`，因此**所有既有校验函数零改动**；非法值（`../evil`）仍被拒。
- 禁止在任何地方再写字面量 `"__workspace__"`（唯一来源是 `src/web/scope.py`）。
- 工作区会话落在 `data/workspace/chats/`（`scope.chats_dir()`），不进任何书的目录。

**(2) 动作层：单一实现源**

- 建书/删书/书架枚举 = `src/services/library.py`；HTTP 端点与动作 handler **共用同一实现**，
  端点只做 `LibraryError.code → HTTP 状态码` 的薄翻译（`server.py::_library_error_to_http`）。
- 动作注册表在 `src/web/actions.py`；新增能力时**先抽服务层函数，再两端调用**，不许各写一份。

**(3) 写动作闸门：未确认绝不落盘**

- `execute(op, args, ctx, execute_write=False)` 对 `scope="write"` 的动作只返回
  `status="pending_confirm"` + 影响说明；真正执行必须 `execute_write=True`。
- 会话内的待办存在 chat JSON 的 `pending_action` 字段（会话即事实源），确认词表见
  `inkforge_api._pending_decision`（"确认/执行/OK" 才放行，"取消/算了" 丢弃，其它话语不动待办）。
- 网关逻辑抽在 `inkforge_api._decide_pending_action`（纯函数）——**安全关键路径必须可单测**，
  不允许把它埋回 HTTP 处理里。

**(4) 动作说明不进 `prompts/` 模板目录**

- `src/agents/actions/master_actions.md` 是**独立目录**：`prompt_loader.validate_templates()`
  会扫描 `prompts/*.md` 并拒绝含 `{var}` 单花括号的文件（写动作参数占位就是单花括号），
  放进去会让引擎启动即 Fail-Fast。
- 动作块由 `agent_prompt()` 统一追加（与 `CONSTITUTION` 同级），**用户自定义提示词覆盖不掉**。

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

### 8.0 墨师全域化的四道验证（改这块必跑）

```bash
cd engine
python -m pytest tests -q                      # 315 条：含 workspace/scope/actions/gate 契约
python smoke_master_workspace.py               # 21 项真实引擎进程验收（零 LLM，可常驻）
python smoke_master_workspace.py --with-llm    # 追加"墨师自己决定调动作"的真实 LLM 用例（会计费）
```

- 新增动作时：先在 `tests/test_master_actions.py` 补"未确认零落盘 + 确认后生效"两条；
- 新增作用域相关端点时：在 `tests/test_workspace_scope.py` 补"缺省仍是默认书"的回归，防止旧语义被改坏。

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
| 14 | **对「有未提交改动」的文件执行 `git checkout -- <path>`** | 本批次真实踩过：`inkforge_api.py` 当时含大量未提交改动（W2 上下文预算 + 墨师动作层），被回退到上一次提交，**丢失约千行**；只能按测试断言重建 | **绝不**对未提交文件用 `checkout` 做"恢复"；需要参考旧版用 `git show HEAD:path > /tmp/ref`（只读）；改大文件前先 `git add`（或 commit 一个 WIP）建立安全点 |
| 15 | 在函数内写 `from fastapi import HTTPException` 后又在该函数**别处**用 `raise HTTPException(...)` | Python 把该名字判为**局部变量** → `UnboundLocalError: cannot access local variable 'HTTPException'`，接口 500 且栈里看不到真实原因 | 模块顶部已统一 import；**不要**在函数内重复 import 同名符号 |
| 16 | 在 `src/agents/prompts/` 放非模板的 `.md`（如动作说明） | `prompt_loader.validate_templates()` 会扫描该目录**全部** `*.md` 当模板校验，含 `{var}` 单花括号即 `ConfigError` → **引擎启动失败** | 非模板文档放 `src/agents/actions/`（由 `action_prompt.py` 直读） |
| 17 | 以为"模型的工具调用"会按协议输出 | 真实 DeepSeek 实测：只回答不调工具、动作名自造（`list_books`）、枚举值自造（`mode: "free"`）、参数漏填（缺 `novel_id`）、"确认"后反复追问 | 见 `docs/07-全量功能冒烟报告-20260916.md` §5.2 的 7 条修法：补漏规划调用 + 确定性兜底 + op 别名表 + 枚举折算 + 单书自动定位 + 闸门即时返回 + "未执行"强制提示 |
| 18 | 把写动作的"预览登记"按**会话维度**做键 | 同一个写动作可能在工作区会话预览、在书内会话确认（或反之），按会话分会话就互相找不到预览 → 用户点"确认"被拒、卡片一直挂着 | 预览键统一用 `{目标书}::{op}`（与会话无关），并在**参数规范化之后**登记/比对（`normalize_args` 一次折算、并丢弃未声明键），两边才严格相等 |
| 19 | 用户说"确认"时按普通回合再规划一次 | 用户已经同意过，却又被挂成一张新的待确认卡（"点了确认，卡还在"） | 命中确认词 → 该轮标记 `confirmed_round`，动作**直接执行**（`dry_run=False`）并立即返回，绝不再登记待办 |
| 20 | 指望模型把用户原话里的参数填进动作 | 用户明说"标识就用 xx"，模型仍漏 `novel_id`（或写成 `title`）→ 动作失败、确认后什么都没发生 | 规划结果过一遍 `_backfill_from_user_message`：`book_create` 缺 `novel_id` 时从用户原话确定性补齐（`_guess_arg_from_user`） |
| 21 | 模型在正文里说"这个功能我查不到" | 其实清单里有对应工具（如 `learning_list`），它却把用户话术（"学习仿写"）匹配到了别的工具 | 规划提示词加"用户话术 → 工具对照表"；执行前用 `_topic_ops` 做"答非所问"判定，命中就丢弃模型自选动作、改走补漏规划 |
| 22 | **把"动作失败"当成"动作存在"来断言** | 验收全绿但用户手上是坏的：`smoke_master_live.py` 原先只断言"存在 book_create 动作"，于是 `{"status":"failed","error":"缺少参数 novel_id"}` 也 PASS —— 用户报的"对话建不了书"就是这样溜过去的 | 凡把动作回执纳入断言，必须区分 `ok` / `pending_confirm` / `failed`；`Suite.check_no_failed_actions()` 已提供现成断言 |
| 23 | 以为"预览成功"和"确认时"算的是同一把键 | 键里读了 `ctx.active_novel()` 这类**可变状态**，建书成功会 `set_active_novel` → 确认时键变了 → 409「没有经过预览登记」、零落盘、待办还挂着 | 预览键**只由 op + 规范化 args 派生**；`test_preview_key_is_independent_of_active_book` 锁定 |
| 24 | 以为模型写歪参数"反正会报错，用户会重说" | 模型把 `novel_id` 写成 `id`/`book_key`，而 `normalize_args` 只保留**声明过的键** → 参数被静默丢弃 → 用户看到"缺少参数 novel_id"、反复重说也没用 | 加 `ARG_ALIASES` 参数名折算表；新增写动作时同步想清楚"模型会怎么写歪" |
| 25 | 以为"模型返回了长正文"就等于"用户看到了" | 墨师只复述摘要（"全文如下（共 2191 字）"）而正文一个字没带 → 用户体感"点开没内容" | `_content_echo()`：读类动作的长文本在回复缺失时**确定性追加**，不依赖模型自觉 |
| 26 | 以为 `generate_faithful` 什么产出都能吃 | 它直接 `out.model_dump_json()`，而自由文本链路返回的 `ChatResult` 只有 `.content` → 一把保真链铺到正文就 `AttributeError`（说明此前从未被自由文本路径走到过） | 走 `_output_text()` 兼容 str / pydantic / `ChatResult`；铺新的链路前先跑一次真机 |
| 27 | 用小样本真机跑一次就以为"覆盖到了" | `mode="长篇"` 这类**体裁词当枚举值**的写法第一次真机才暴露（此前测试都喂规范值） | `smoke_master_converse.py` 专测"模型会怎么写歪"（参数别名/占位符/模式词/名字别名），真机与零 LLM 两层都跑 |

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
| 加冒烟用例 | `engine/smoke_test.py`（零 LLM 接口回归）、`smoke_master_workspace.py`（墨师全域化验收） |
| 改墨师作用域/会话维度 | `engine/src/web/scope.py`（哨兵唯一来源） |
| 加/改墨师动作 | `engine/src/web/actions.py`（注册表）+ `src/services/library.py`（实现）+ `src/agents/actions/master_actions.md`（提示词） |
| 改对话框 / 排查 UI 观感问题 | `apps/desktop/ui-probe/`（真实浏览器 + 像素断言，见其 README 的坑位清单） |
| 查全部提示词位置 | `engine/src/agents/prompts/`、`engine/src/agents/actions/`、`engine/src/distillation/prompts/`、`engine/src/web/inkforge_api.py`（`AGENT_PRESETS` / `SUB_AGENTS`）、`inkforge_windows.py` |
