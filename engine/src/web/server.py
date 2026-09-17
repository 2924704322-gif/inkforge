"""Web 审阅后端（M2 / T2.1）：FastAPI + 章节 MD 渲染 + 通过/打回 + 进度看板。

设计要点（D9 MD 唯一事实源 + D4 逐章人审）：
- 生成流程仍由 LangGraph 驱动，人审暂停点用 interrupt() 实现。
- Web 后端持有已编译图与线程配置，前端的通过/打回通过 Command(resume=...) 恢复图执行。
- 后端在后台线程推进生成，遇 interrupt 挂起并把待审 payload 暴露给前端轮询。
- 所有正文/评分均从 MD 事实源读取渲染，Web 不持有独立数据副本。
- 多书书架（v2.0 P4-B）：SessionHub 按 novel_id 管理多个 ReviewSession，
  各 /api/* 端点经可选 novel query 参数路由，缺省用启动时的默认书。

启动：python -m src.web.server --novel-id my-novel（--novel-id 为默认打开的书）
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from pathlib import Path

import frontmatter
import markdown as md
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from src.config.settings import (
    ModelsConfig,
    _resolve_env_placeholders,  # noqa: PLC2701 - 校验前解析占位符
    get_settings,
)
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 引擎版本（/api/ping 上报，供壳层做兼容性校验）
APP_VERSION = "0.2.0"

# 预估字数标记正则：<!-- estimated_words: N -->
_ESTIMATED_WORDS_RE = re.compile(r"<!--\s*estimated_words:\s*(\d+)\s*-->")

# 蒸馏上传允许的文件后缀（配合 auth + 主进程授权，收敛「任意文件读取」面）
_UPLOAD_SUFFIXES = (".txt", ".epub", ".md", ".markdown")


def _parse_estimated_words(content: str) -> tuple[str, int | None]:
    """从内容末尾提取预估字数标记，返回 (清理后内容, 预估字数或None)。"""
    m = _ESTIMATED_WORDS_RE.search(content)
    if m is None:
        return content, None
    cleaned = _ESTIMATED_WORDS_RE.sub("", content).rstrip()
    return cleaned, int(m.group(1))

# 自定义创作 Skill（约束型）：全局存于 data/custom_skills/，向导选定后
# 合并写入该书 settings/custom-skills.md。
#
# P0 重构：实现搬到 src/services/library.py（HTTP 端点与墨师动作层共用一份实现），
# 此处保留同名薄包装与常量 re-export，既有调用方与测试接口逐字不变。
from src.services.library import (
    NOVEL_ID_RE as _NOVEL_ID_RE,
)
from src.services.library import (
    SKILL_ID_RE as _SKILL_ID_RE,
)
from src.services.library import (  # noqa: E402 - 顶部导入块之后集中登记
    UPLOAD_ROOTS_ENV,
    LibraryError,
)
from src.services.library import (
    UPLOAD_SUFFIXES as _UPLOAD_SUFFIXES,
)
from src.services.library import (
    LibraryError as _LibraryError,
)
from src.services.library import (
    apply_skills_to_store as _apply_skills_to_store,
)
from src.services.library import (
    compose_skill_constraints as _compose_skill_constraints,
)
from src.services.library import (
    create_book as _lib_create_book,
)
from src.services.library import (
    delete_book as _lib_delete_book,
)
from src.services.library import (
    list_books as _lib_list_books,
)
from src.services.library import (
    list_custom_skills as _list_custom_skills,
)


def _custom_skills_dir() -> Path:
    from src.services.library import custom_skills_dir

    return custom_skills_dir()


def _effective_brief(brief: str, fields: BriefFieldsBody | None) -> str:
    """X1：结构化字段（原样渲染）+ 自由补充文本，合成最终 brief。

    字段全部为空时只返回自由文本（保持既有单框 brief 用法可用）。
    """
    from src.agents.architect import render_brief_fields

    structured = render_brief_fields(fields.model_dump() if fields is not None else None)
    free = (brief or "").strip()
    return "\n\n".join(part for part in (structured, free) if part)


def _library_error_to_http(exc: LibraryError):
    """把服务层错误按 code 翻译为既有 HTTP 状态码与文案（行为保持逐字一致）。"""
    mapping = {"bad_request": 400, "not_found": 404, "conflict": 409}
    raise HTTPException(mapping.get(exc.code, 400), exc.message) from exc


def _invalidate_workspace_index() -> None:
    """书目增删后立即失效工作区索引缓存（让墨师"刚建完的书"马上可见）。"""
    from src.services import workspace as _workspace

    _workspace.invalidate()


def _apply_skills_to_novel(store: MdStore, skill_ids: list[str]) -> list[str]:
    """将选定 Skill 合并写入该书 settings/custom-skills.md（未选则不动既有文件）。"""
    return _apply_skills_to_store(store, skill_ids)


# ---------- 大纲编辑（资料库页人工修改后落盘为唯一事实源） ----------
OUTLINE_REL = "settings/outline.md"


def _render_outline_markdown(book_title: str, theme: str, volumes: list[dict]) -> str:
    """把结构化 volumes 渲染为大纲正文 Markdown（与 Architect.save_outline 格式一致）。"""
    lines = [f"# {book_title}\n", f"> 主题：{theme}\n"]
    for vol in volumes:
        dep = f"（依赖卷 {vol.get('depends_on')}）" if vol.get("depends_on") else ""
        lines.append(f"\n## 第{vol.get('volume')}卷 {vol.get('title', '')}{dep}\n")
        for ch in vol.get("chapters", []):
            chars = "、".join(ch.get("characters", []) or [])
            lines.append(
                f"### 第{ch.get('chapter')}章 {ch.get('title', '')}\n\n"
                f"出场角色：{chars}\n\n{ch.get('outline', '')}\n"
            )
    return "\n".join(lines)


# ---------- Agent 模型接入配置（configs/models.yaml 查看/修改） ----------
# 读写均基于 YAML 原文（不解析 ${ENV_VAR} 占位符），避免明文密钥落盘/回显；
# registry 在每次生成任务/Demo 时重建并重读 YAML，故修改后无需重启即生效。

_MODELS_YAML_HEADER = (
    "# 模型接入配置（可在工具页「Agent 模型配置」面板查看/修改）\n"
    "# providers 定义接入点；roles 将 Agent 角色绑定到 接入点/模型/参数。\n"
    "# api_key 推荐使用 ${ENV_VAR} 占位符从环境变量（.env）读取，不落明文。\n"
)
_PROVIDER_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{(\w+)\}$")


def _models_yaml_path() -> Path:
    return get_settings().models_yaml


def _load_models_raw() -> dict:
    """读取 models.yaml 原文结构（保留 ${ENV_VAR} 占位符）。"""
    path = _models_yaml_path()
    if not path.exists():
        raise HTTPException(500, f"模型配置文件不存在: {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_models_raw(raw: dict) -> None:
    """校验后写回 models.yaml（校验失败抛 400，原文件不动）。"""
    try:
        ModelsConfig.model_validate(_resolve_env_placeholders(raw))
    except Exception as exc:  # noqa: BLE001 - pydantic 校验信息原样透出
        raise HTTPException(400, f"模型配置校验失败：{exc}")
    text = _MODELS_YAML_HEADER + "\n" + yaml.safe_dump(
        raw, allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    _models_yaml_path().write_text(text, encoding="utf-8", newline="\n")


def _mask_api_key(key: str) -> str:
    """占位符/none 原样返回；明文密钥掩码（保留前 4 位）。"""
    if not key or key == "none" or _ENV_PLACEHOLDER_RE.match(key):
        return key
    return (key[:4] + "****") if len(key) > 8 else "****"


def _model_config_view(raw: dict) -> dict:
    """models.yaml → 前端视图（密钥掩码 + 占位符对应环境变量是否已配置）。"""
    providers = []
    for name, p in (raw.get("providers") or {}).items():
        key = str(p.get("api_key", "none"))
        m = _ENV_PLACEHOLDER_RE.match(key)
        providers.append({
            "name": name,
            "type": p.get("type", ""),
            "base_url": p.get("base_url") or "",
            "api_key": _mask_api_key(key),
            "key_env_set": bool(os.environ.get(m.group(1))) if m else None,
        })
    roles = []
    for role, b in (raw.get("roles") or {}).items():
        roles.append({
            "role": role,
            "provider": b.get("provider", ""),
            "model": b.get("model", ""),
            "temperature": b.get("temperature", 0.7),
            "max_tokens": b.get("max_tokens"),
            "fallback": b.get("fallback"),
        })
    return {"providers": providers, "roles": roles,
            "path": str(_models_yaml_path())}


# ---------- 请求体模型（须在模块级定义：from __future__ import annotations 下，
# FastAPI 无法解析函数内局部 BaseModel 的注解，会误判为 query 参数）----------

class BriefFieldsBody(BaseModel):
    """X1 结构化 brief（W6）：以字段替代单框文本，键与 architect.BRIEF_FIELDS 一致。"""

    genre: str = ""        # 体裁
    pov: str = ""          # 视角
    tone: str = ""         # 基调
    protagonist: str = ""  # 主角
    antagonist: str = ""   # 对立面
    setting: str = ""      # 设定
    themes: str = ""       # 主题
    arc: str = ""          # 期望弧线
    avoid: str = ""        # 明确不要的写法


class Decision(BaseModel):
    action: str          # approve / reject
    feedback: str = ""
    revision_mode: str = "targeted"   # P3: targeted 定向修订 / rewrite 整体重写


class StartBody(BaseModel):
    brief: str = ""
    chapters: int = 10
    parallel: bool = False
    workers: int = 0
    skill_ids: list[str] = []   # 向导选定的自定义 Skill（写入 settings/custom-skills.md）
    brief_fields: BriefFieldsBody = BriefFieldsBody()   # X1 结构化 brief（优先）


class ResumeBody(BaseModel):
    parallel: bool = False
    workers: int = 0


class BookCreateBody(BaseModel):
    novel_id: str
    # 创作模式：pipeline（自由创作，大纲+章节流水线）/ interactive（互动创作，只要世界观）
    mode: str = "pipeline"


class DemoBody(BaseModel):
    brief: str = ""
    chapters: int = 10
    feedback: str = ""     # 重新生成时携带上一版审核意见
    skill_ids: list[str] = []   # 自定义 Skill 约束附加进 brief，使 Demo 设定也遵循
    brief_fields: BriefFieldsBody = BriefFieldsBody()   # X1 结构化 brief（优先）


class CustomSkillBody(BaseModel):
    title: str
    content: str
    skill_id: str = ""     # 缺省由标题哈希自动生成


class RoleBindingBody(BaseModel):
    provider: str
    model: str
    temperature: float = 0.7
    max_tokens: int | None = None


class ProviderBody(BaseModel):
    type: str              # openai_compat / anthropic
    base_url: str = ""
    api_key: str = "none"  # 含 **** 视为「保持原值」（编辑时无需重填密钥）


class OutlineChapterBody(BaseModel):
    chapter: int
    title: str = ""
    outline: str = ""
    characters: list[str] = []


class OutlineVolumeBody(BaseModel):
    volume: int
    title: str = ""
    depends_on: list[int] = []
    chapters: list[OutlineChapterBody] = []


class OutlineSaveBody(BaseModel):
    book_title: str = ""
    theme: str = ""
    volumes: list[OutlineVolumeBody] = []


class DemoConfirmBody(BaseModel):
    demo: dict | None = None   # 用户编辑后的完整 Demo（缺省沿用服务端缓存）


class InteractiveChooseBody(BaseModel):
    card_id: str                  # c1/c2/c3/custom
    custom_text: str = ""         # card_id=custom 时的用户自拟剧情
    target_words: int | None = None  # 本章目标字数，None 使用服务默认值


class InteractiveRedrawBody(BaseModel):
    feedback: str = ""            # 重抽意见（可空）


class DistillInitBody(BaseModel):
    file_path: str = ""                    # 上传文件的本地路径（或 base64）
    skill_id: str = ""                     # 缺省由书名+版本号自动生成
    version: str = "1.0.0"
    chunk_strategy: str = "BY_CHAPTERS"
    chunk_size: int = 20000


class DistillStartBody(BaseModel):
    skill_id: str


class DistillReprocessBody(BaseModel):
    skill_id: str
    mode: str = "INCREMENTAL"              # INCREMENTAL / DEEP_UPGRADE / FULL
    new_file_path: str = ""                # INCREMENTAL 模式的追加文件
    new_version: str = ""                  # 缺省自动推算


# ---------- 生成会话：在后台线程驱动 LangGraph，interrupt 时挂起 ----------

class ReviewSession:
    """封装单部小说的图执行与人审暂停队列（线程安全）。"""

    def __init__(self, novel_id: str, target_words: int = 3000):
        self.novel_id = novel_id
        self.target_words = target_words
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._pending: dict | None = None      # 当前待人审 payload
        self._resume: dict | None = None        # 前端提交的决定
        self._worker: threading.Thread | None = None
        self._done = False
        self._error: str | None = None
        self._started = False
        self._parallel = False       # 并行模式（卷级并行驱动器）
        self._workers = 0            # 并行工作线程数（0=自适应）
        self._chapters: int | None = None
        self._last_wave: dict | None = None  # 最近一次跨卷审查概要（展示用）
        # v2.0 P2：重稿 diff 与评分趋势（会话内存态，不落盘）
        self._draft_cache: dict[int, dict] = {}    # chapter -> {attempt, text}
        self._review_history: list[dict] = []      # [{chapter, attempt, overall}]
        # 设定 Demo（创作向导先审后入库）：独立于图执行的轻量后台任务
        self._demo_status = "idle"   # idle / running / done / confirmed / error
        self._demo_result = None                   # DemoOutput
        self._demo_error: str | None = None
        self._demo_thread: threading.Thread | None = None

    @property
    def novel_dir(self) -> Path:
        return get_settings().novels_dir / self.novel_id

    def store(self) -> MdStore:
        """只读视图（状态 / 章节列表 / 概览）。

        只读路径**绝不新建 Git 仓库**：`/api/status`、`/api/queue` 等每 2.5s 被轮询一次，
        若在此处初始化仓库，用户只是打开一次软件就会凭空多出 `.git`。
        """
        from src.memory.store_factory import open_store

        return open_store(self.novel_dir, writable=False)

    def write_store(self) -> MdStore:
        """写入视图（应用 Skill、写入设定 Demo）——启用写作历史。"""
        from src.memory.store_factory import open_store

        return open_store(self.novel_dir, writable=True)

    # -- 后台驱动 --

    def start(self, brief: str, chapters: int, parallel: bool = False,
              workers: int = 0, skill_ids: list[str] | None = None) -> None:
        if self._started:
            raise RuntimeError(
                "会话异常终止，请用「断点重新生成」从断点恢复" if self._error
                else "会话已启动"
            )
        # 正式生成前把向导选定的自定义 Skill 写入该书设定（逐章注入 Writer/Editor）
        titles = _apply_skills_to_novel(self.write_store(), skill_ids or [])
        if titles:
            logger.info("书 %s 已应用自定义 Skill: %s", self.novel_id, "、".join(titles))
        self._started = True
        self._parallel = parallel
        self._workers = workers
        self._chapters = chapters
        self._worker = threading.Thread(
            target=self._run, args=(brief, chapters, None), daemon=True
        )
        self._worker.start()

    def _reset_if_failed(self) -> None:
        """异常终止且后台线程已退出的会话：重置状态以允许断点重新生成。

        数据不丢：串行模式靠 checkpoints.sqlite 恢复到失败前节点，
        并行模式以 MD 事实源自证（approved 章节跳过）。
        """
        with self._lock:
            if not self._error or (self._worker and self._worker.is_alive()):
                return
            logger.info("书 %s 异常会话重置，准备断点重跑（原错误：%s）",
                        self.novel_id, self._error[:200])
            self._error = None
            self._started = False
            self._done = False
            self._pending = None
            self._resume = None

    def resume_existing(self, parallel: bool = False, workers: int = 0) -> None:
        self._reset_if_failed()
        if self._started:
            raise RuntimeError("会话已启动")
        # 校验 checkpoint 存在性：无断点记录则无法续跑（如纯互动模式书）
        db = get_settings().checkpoint_db
        if not db.exists():
            raise RuntimeError("无断点数据库，无法续跑")
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id=? LIMIT 1",
                (self.novel_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise RuntimeError(
                "该书无生成断点记录，无法续跑。互动模式请使用互动创作入口。"
            )
        self._started = True
        self._parallel = parallel
        self._workers = workers
        self._worker = threading.Thread(
            target=self._run, args=(None, None, "resume"), daemon=True
        )
        self._worker.start()

    def _run(self, brief: str | None, chapters: int | None, mode: str | None) -> None:
        try:
            from src.orchestrator.bootstrap import build_app

            pipe, graph = build_app(self.novel_id, target_words=self.target_words)
            pipe.registry.probe_all()

            if self._parallel:
                self._run_parallel(pipe, brief, chapters, mode)
                return

            from langgraph.types import Command

            config = {"configurable": {"thread_id": self.novel_id}, "recursion_limit": 500}

            if mode == "resume":
                result = graph.invoke(None, config)
            else:
                result = graph.invoke(
                    {
                        "novel_id": self.novel_id,
                        "brief": brief,
                        "total_chapters": chapters,
                        "target_words": self.target_words,
                    },
                    config,
                )
            while "__interrupt__" in result:
                payload = result["__interrupt__"][0].value
                decision = self._await_decision(payload)
                result = graph.invoke(Command(resume=decision), config)
            with self._cond:
                self._done = True
                self._pending = None
                self._cond.notify_all()
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成会话异常")
            with self._cond:
                self._error = str(exc)
                self._cond.notify_all()

    def _run_parallel(self, pipe, brief: str | None, chapters: int | None,
                      mode: str | None) -> None:
        """卷级并行驱动：人审回调复用 _await_decision（并行仅在起草阶段，人审仍串行）。"""
        from src.orchestrator.parallel_runner import load_outline, run_parallel

        if mode == "resume":
            outline = load_outline(pipe.store)
            if outline is None:
                raise RuntimeError("并行续跑要求已有大纲（settings/outline.md）")
            total = self._chapters or sum(
                len(v.get("chapters", [])) for v in outline["volumes"]
            )
            run_brief = ""
        else:
            total = chapters or 0
            run_brief = brief or ""

        run_parallel(
            pipe,
            novel_id=self.novel_id,
            brief=run_brief,
            total_chapters=total,
            outline_cb=self._await_decision,
            review_cb=self._await_decision,
            max_workers=self._workers,
            wave_report_cb=self._on_wave_report,
        )
        with self._cond:
            self._done = True
            self._pending = None
            self._cond.notify_all()

    def _on_wave_report(self, wave_no: int, volumes: list, review) -> None:
        """跨卷审查完成回调：报告已由 Editor 落盘，这里仅缓存概要供前端展示。"""
        self._last_wave = {
            "wave": wave_no,
            "volumes": list(volumes),
            "consistent": getattr(review, "consistent", None),
            "issue_count": len(getattr(review, "issues", []) or []),
        }

    def request_pause(self) -> dict:
        """暂停生成。

        不引入新的中断点，复用逐章确认关卡：
        · 已停在关卡上 → 立即结束本轮（action=stop 让关卡路由到 END）；
        · 正在写某一章 → 打标记，这一章写完到达关卡时自动停下，不自动往下写。
        已产出的章节都已落盘，随时可删书或断点续跑。
        """
        with self._cond:
            pending = self._pending or {}
            self._pause_requested = True
            if isinstance(pending, dict) and pending.get("type") == "chapter_gate":
                self._resume = {"action": "stop"}
                self._cond.notify_all()
                return {
                    "paused": True,
                    "immediate": True,
                    "next_chapter": pending.get("next_chapter"),
                }
            return {"paused": True, "immediate": False}

    def _await_decision(self, payload: dict) -> dict:
        """挂起等待前端提交决定。"""
        with self._cond:
            self._pending = self._enrich_chapter_payload(payload)
            self._resume = None
            # 逐章确认关卡：若用户已点过「暂停生成」，不再等待，直接结束本轮
            if payload.get("type") == "chapter_gate" and getattr(self, "_pause_requested", False):
                self._pause_requested = False
                self._pending = None
                logger.info("用户已请求暂停：跳过第 %s 章的生成", payload.get("next_chapter"))
                return {"action": "stop"}
            self._cond.notify_all()
            while self._resume is None:
                self._cond.wait()
            decision = self._resume
            self._resume = None
            self._pending = None
            return decision

    def _enrich_chapter_payload(self, payload: dict) -> dict:
        """章节待审 payload 增强（P2）：附上一稿供 diff，累积评分历史。

        调用方已持锁（_cond 与 _lock 同底层锁），直接读写会话态。
        """
        if payload.get("type") != "chapter_review":
            return payload
        ch = payload.get("chapter")
        attempt = payload.get("attempt", 1)
        enriched = dict(payload)
        cached = self._draft_cache.get(ch)
        if cached and cached["attempt"] < attempt:
            enriched["previous_draft"] = cached["text"]
            enriched["previous_attempt"] = cached["attempt"]
        self._draft_cache[ch] = {"attempt": attempt,
                                 "text": payload.get("draft_text", "")}
        r = payload.get("review") or {}
        if r and all(k in r for k in ("consistency", "plot", "continuity", "prose")):
            overall = round(
                (r["consistency"] + r["plot"] + r["continuity"] + r["prose"]) / 4, 2
            )
            self._review_history.append(
                {"chapter": ch, "attempt": attempt, "overall": overall}
            )
        return enriched

    def submit_decision(self, decision: dict) -> None:
        with self._cond:
            if self._pending is None:
                raise RuntimeError("当前无待审项")
            self._resume = decision
            self._cond.notify_all()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "novel_id": self.novel_id,
                "started": self._started,
                "done": self._done,
                "error": self._error,
                "pending": self._pending,
                "parallel": self._parallel,
                "last_wave": self._last_wave,
                "review_history": list(self._review_history),
            }

    # -- 设定 Demo（创作向导先审后入库，不经 LangGraph） --

    def start_demo(self, brief: str, chapters: int, feedback: str = "",
                   skill_ids: list[str] | None = None) -> None:
        """后台生成设定 Demo；正式生成已启动或 Demo 生成中则拒绝。"""
        # 自定义 Skill 约束附加进 brief，使 Demo 设定（世界观/人物/基调）也遵循
        if skill_ids:
            constraints, _ = _compose_skill_constraints(skill_ids)
            if constraints:
                brief = f"{brief}\n\n【创作约束（用户自定义 Skill，必须遵循）】\n{constraints}"
        with self._lock:
            if self._started:
                raise RuntimeError("正式生成已启动，无法再生成设定 Demo")
            if self._demo_status == "running":
                raise RuntimeError("设定 Demo 正在生成中，请稍候")
            self._demo_status = "running"
            self._demo_result = None
            self._demo_error = None
            self._demo_thread = threading.Thread(
                target=self._run_demo, args=(brief, chapters, feedback), daemon=True
            )
            self._demo_thread.start()

    def _run_demo(self, brief: str, chapters: int, feedback: str) -> None:
        try:
            from src.memory.memory_manager import read_custom_constraints

            store = self.write_store()
            # 项目级约束与 brief 同处送达 Demo 渲染（W5：与 memory_manager 同源直读）
            demo = self._build_architect(store).generate_demo(
                brief, chapters, feedback,
                custom_constraints=read_custom_constraints(store),
                interactive=(store.root / "interactive").is_dir(),
            )
            with self._lock:
                self._demo_result = demo
                self._demo_status = "done"
        except Exception as exc:  # noqa: BLE001
            logger.exception("设定 Demo 生成异常")
            with self._lock:
                self._demo_error = str(exc)
                self._demo_status = "error"

    def _build_architect(self, store=None):
        """轻量装配 Architect（仅 registry+store，不建向量索引/图）。"""
        from src.agents.architect import Architect
        from src.config.settings import load_models_config
        from src.llm.registry import ModelRegistry

        return Architect(ModelRegistry(load_models_config()), store or self.write_store())

    def confirm_demo(self, demo_override=None) -> dict:
        """人工同意后把 Demo 写入资料库 settings/。

        demo_override：用户在前端编辑后的完整 DemoOutput（有则以之为准落盘，
        无则沿用服务端缓存，向后兼容全自动模式）。
        """
        with self._lock:
            if self._started:
                raise RuntimeError("正式生成已启动，无法写入设定 Demo")
            demo = demo_override or self._demo_result
            if demo is None or (demo_override is None and self._demo_status != "done"):
                raise RuntimeError("当前无待确认的设定 Demo")
        self._build_architect().save_demo(
                demo,
                interactive=(get_settings().novels_dir / self.novel_id / "interactive").is_dir(),
            )
        with self._lock:
            self._demo_result = demo
            self._demo_status = "confirmed"
        return {"book_title": demo.book_title}

    def demo_snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self._demo_status,
                "error": self._demo_error,
                "demo": self._demo_result.model_dump() if self._demo_result else None,
            }


# ---------- 互动创作会话（剧情卡选择模式，独立于 LangGraph） ----------

class InteractiveSession:
    """单书互动创作状态机：后台短线程 + 轮询快照（线程安全）。

    状态流转：
      idle → generating_cards → awaiting_choice → writing → awaiting_review
        → (approve) committing → generating_cards（下一章）
        → (reject)  writing（携 feedback + revision_mode 重写）
    断点恢复：start 时以 MD 事实源自证（草稿→awaiting_review，
    未选卡→awaiting_choice，已选卡无草稿→续写，否则重新出卡）。
    """

    def __init__(self, novel_id: str, target_words: int = 3000):
        self.novel_id = novel_id
        self.target_words = target_words
        self._lock = threading.Lock()
        self._status = "idle"   # idle/generating_cards/awaiting_choice/writing/awaiting_review/committing/error
        self._chapter = 0
        self._cards: list[dict] = []
        self._draft: dict | None = None   # {draft_text, attempt, review, model?}
        self._error: str | None = None
        self._worker: threading.Thread | None = None
        self._runner = None                  # InteractiveRunner（重型装配，会话内缓存）

    def _ensure_runner(self):
        if self._runner is None:
            from src.orchestrator.bootstrap import build_pipeline
            from src.orchestrator.interactive import InteractiveRunner

            pipe = build_pipeline(self.novel_id, target_words=self.target_words)
            pipe.registry.probe_all()
            self._runner = InteractiveRunner(pipe)
        return self._runner

    def _spawn(self, fn, *args) -> None:
        """启动短后台线程执行一个动作；异常统一转 error 状态。"""
        def run():
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001
                logger.exception("互动创作会话异常")
                with self._lock:
                    self._status = "error"
                    self._error = str(exc)
        self._worker = threading.Thread(target=run, daemon=True)
        self._worker.start()

    def _busy(self) -> bool:
        return self._status in ("generating_cards", "writing", "committing")

    # -- 用户动作（均在持锁校验后转后台线程） --

    def start(self) -> None:
        """启动/续跑：从 MD 事实源恢复断点；error 状态也可重新 start。"""
        with self._lock:
            if self._busy():
                raise RuntimeError("互动会话正在处理中，请稍候")
            self._status = "generating_cards"
            self._error = None
        self._spawn(self._do_start)

    def _do_start(self) -> None:
        runner = self._ensure_runner()
        chapter = runner.next_chapter()
        # 断点恢复：未定稿草稿→审核阶段
        draft = runner.draft_snapshot(chapter)
        if draft is not None:
            record = runner.load_cards(chapter) or {}
            with self._lock:
                self._chapter = chapter
                self._cards = record.get("cards") or []
                self._draft = draft
                self._status = "awaiting_review"
            return
        record = runner.load_cards(chapter)
        if record is not None:
            if record["chosen"]:
                # 已选卡但无草稿（写作中断）：续写
                plan = runner.chosen_plan(chapter)
                with self._lock:
                    self._chapter = chapter
                    self._cards = record["cards"]
                    self._status = "writing"
                self._write(runner, chapter, plan)
                return
            with self._lock:
                self._chapter = chapter
                self._cards = record["cards"]
                self._status = "awaiting_choice"
            return
        with self._lock:
            self._chapter = chapter
            self._cards = []
            self._draft = None
        cards = runner.generate_cards(chapter)
        with self._lock:
            self._cards = cards
            self._status = "awaiting_choice"

    def choose(self, card_id: str, custom_text: str = "",
               target_words: int | None = None) -> None:
        with self._lock:
            if self._status != "awaiting_choice":
                raise RuntimeError(f"当前不在选卡阶段（{self._status}）")
            self._status = "writing"
            chapter = self._chapter
        self._spawn(self._do_choose, chapter, card_id, custom_text, target_words)

    def _do_choose(self, chapter: int, card_id: str, custom_text: str,
                   target_words: int | None = None) -> None:
        runner = self._ensure_runner()
        try:
            plan = runner.choose_card(chapter, card_id, custom_text)
        except ValueError as exc:
            # 选卡参数错误（如自定义卡空内容）：回退到选卡阶段而非 error
            with self._lock:
                self._status = "awaiting_choice"
                self._error = str(exc)
            return
        self._write(runner, chapter, plan, target_words=target_words)

    def _write(self, runner, chapter: int, plan: dict,
               feedback: str = "", revision_mode: str = "targeted",
               target_words: int | None = None) -> None:
        result = runner.write_chapter(chapter, plan, feedback, revision_mode,
                                      target_words=target_words)
        with self._lock:
            self._draft = result
            self._status = "awaiting_review"

    def redraw(self, feedback: str = "") -> None:
        with self._lock:
            if self._status != "awaiting_choice":
                raise RuntimeError(f"当前不在选卡阶段（{self._status}）")
            self._status = "generating_cards"
            chapter = self._chapter
        self._spawn(self._do_redraw, chapter, feedback)

    def _do_redraw(self, chapter: int, feedback: str) -> None:
        runner = self._ensure_runner()
        cards = runner.generate_cards(chapter, feedback=feedback)
        with self._lock:
            self._cards = cards
            self._status = "awaiting_choice"

    def decision(self, action: str, feedback: str = "",
                 revision_mode: str = "targeted") -> None:
        with self._lock:
            if self._status != "awaiting_review":
                raise RuntimeError(f"当前不在审核阶段（{self._status}）")
            chapter = self._chapter
            if action == "approve":
                self._status = "committing"
            elif action == "reject":
                if not feedback.strip():
                    raise RuntimeError("打回重写必须填写修改意见")
                self._status = "writing"
            else:
                raise RuntimeError(f"未知动作：{action}")
        if action == "approve":
            self._spawn(self._do_commit, chapter)
        else:
            self._spawn(self._do_reject, chapter, feedback, revision_mode)

    def _do_commit(self, chapter: int) -> None:
        runner = self._ensure_runner()
        runner.commit(chapter)
        # 自动进入下一章出卡
        nxt = chapter + 1
        with self._lock:
            self._chapter = nxt
            self._cards = []
            self._draft = None
            self._status = "generating_cards"
        cards = runner.generate_cards(nxt)
        with self._lock:
            self._cards = cards
            self._status = "awaiting_choice"

    def _do_reject(self, chapter: int, feedback: str, revision_mode: str) -> None:
        runner = self._ensure_runner()
        plan = runner.chosen_plan(chapter)
        if plan is None:
            raise RuntimeError(f"第 {chapter} 章无已选剧情卡，无法重写")
        self._write(runner, chapter, plan, feedback, revision_mode)

    def snapshot(self) -> dict:
        with self._lock:
            approved = 0
            if self._runner is not None:
                try:
                    approved = self._runner.approved_count()
                except Exception:  # noqa: BLE001 - 快照不因统计失败而中断
                    approved = 0
            return {
                "novel_id": self.novel_id,
                "status": self._status,
                "chapter": self._chapter,
                "cards": list(self._cards),
                "draft": dict(self._draft) if self._draft else None,
                "approved_count": approved,
                "error": self._error,
                "target_words": self.target_words,
            }


# ---------- 多书会话中枢（v2.0 P4-B）----------


class SessionHub:
    """按 novel_id 管理多个 ReviewSession（线程安全，多书并存）。"""

    def __init__(self, target_words: int = 3000):
        self._target_words = target_words
        self._lock = threading.Lock()
        self._sessions: dict[str, ReviewSession] = {}
        self._interactive: dict[str, InteractiveSession] = {}

    def get_or_create(self, novel_id: str) -> ReviewSession:
        if not _NOVEL_ID_RE.match(novel_id or ""):
            raise ValueError(f"非法书名标识：{novel_id!r}（仅限字母/数字/下划线/连字符）")
        with self._lock:
            if novel_id not in self._sessions:
                self._sessions[novel_id] = ReviewSession(novel_id, self._target_words)
            return self._sessions[novel_id]

    def get_or_create_interactive(self, novel_id: str) -> InteractiveSession:
        if not _NOVEL_ID_RE.match(novel_id or ""):
            raise ValueError(f"非法书名标识：{novel_id!r}（仅限字母/数字/下划线/连字符）")
        with self._lock:
            if novel_id not in self._interactive:
                self._interactive[novel_id] = InteractiveSession(
                    novel_id, self._target_words)
            return self._interactive[novel_id]

    def active_ids(self) -> set[str]:
        with self._lock:
            # 异常终止的会话不算活跃（后台线程已退出，不应阻塞删书等操作）；
            # 已完结（done）的会话也不算活跃，否则书架/顶栏会永久显示「生成中」。
            return {
                nid for nid, s in self._sessions.items()
                if (snap := s.snapshot())["started"]
                and not snap["error"] and not snap["done"]
            }

    def done_ids(self) -> set[str]:
        """本进程内已跑完（_done=True）的会话 id 集合（用于「已完结」徽标）。"""
        with self._lock:
            return {
                nid for nid, s in self._sessions.items()
                if s.snapshot()["done"]
            }

    def remove(self, novel_id: str) -> None:
        """移除会话（删书时调用；不存在则忽略）。"""
        with self._lock:
            self._sessions.pop(novel_id, None)
            self._interactive.pop(novel_id, None)


# ---------- 删书（目录树 / 派生数据清理） ----------
# P0 重构：_force_rmtree 与 _purge_derived_data 的实现已搬到
# src/services/library.py（端点与墨师动作层共用），本文件顶部以 import 别名
# re-export 同名符号，既有调用方与测试接口不变。


# ---------- 上传路径校验（S1-4：收敛任意文件读取面） ----------


def _resolve_upload_path(raw: str) -> Path:
    """校验蒸馏输入文件路径，返回规范化后的绝对路径。

    规则：
    1. 必须为绝对路径；
    2. 必须存在且是普通文件；
    3. 后缀必须在 _UPLOAD_SUFFIXES 内；
    4. 若设置了 INKFORGE_UPLOAD_ROOTS（路径分隔符分隔的目录白名单），
       规范化后必须落在其中某个根目录内（realpath 包含性校验，防符号链接逃逸）。
    """
    if not raw or not raw.strip():
        raise HTTPException(400, "file_path 不能为空")
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        raise HTTPException(400, "file_path 必须是绝对路径")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise HTTPException(400, f"文件不存在或不可访问：{raw}")
    if not resolved.is_file():
        raise HTTPException(400, f"不是普通文件：{raw}")
    if resolved.suffix.lower() not in _UPLOAD_SUFFIXES:
        raise HTTPException(
            400,
            f"不支持的文件类型 {resolved.suffix!r}，"
            f"仅支持 {'/'.join(_UPLOAD_SUFFIXES)}",
        )
    roots_raw = os.environ.get(UPLOAD_ROOTS_ENV, "")
    roots = [Path(p).expanduser().resolve() for p in roots_raw.split(os.pathsep) if p.strip()]
    if roots and not any(_is_within(resolved, root) for root in roots):
        logger.warning("拒绝未授权上传路径 %s（不在 %s 内）", resolved, roots)
        raise HTTPException(403, "该路径未获授权：请通过应用内「选择文件」按钮选取")
    return resolved


def _is_within(path: Path, root: Path) -> bool:
    """path 是否位于 root 之内（含 root 本身）。"""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------- FastAPI 应用 ----------

def create_app(novel_id: str, target_words: int = 3000) -> FastAPI:
    # 启动期模板自检（W1）：模板变量与渲染点参数不一致 → 启动即 ConfigError
    from src.agents.prompt_loader import validate_templates

    validate_templates()
    app = FastAPI(title="小说审阅系统 v1.0")
    hub = SessionHub(target_words)
    session = hub.get_or_create(novel_id)   # 默认书会话（兼容既有单书用法）
    app.state.session = session
    app.state.hub = hub
    app.state.novel_id = novel_id

    def _sess(novel: str = "") -> ReviewSession:
        """novel query 参数 → 对应书的会话；缺省用默认书。"""
        try:
            return hub.get_or_create(novel or novel_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    # ---------- 轻量探活（无副作用） ----------
    # 注意：健康检查**不可**走 /api/books——那条路径会为每本书构造 MdStore 并遍历
    # 全部章节（曾导致「列书架产生写副作用 + 探活 O(书数)」）。/api/ping 不触碰任何数据。

    @app.get("/api/ping")
    def ping() -> JSONResponse:
        from src.web.auth import engine_token

        return JSONResponse({
            "ok": True,
            "service": "inkforge-engine",
            "version": APP_VERSION,
            "auth_required": bool(engine_token()),
            "default_novel": novel_id,
        })

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML.replace("__NOVEL_ID__", novel_id)

    @app.get("/api/books")
    def books() -> JSONResponse:
        """书架：枚举 novels_dir 下全部书目及进度元信息。

        P0 重构：实现委托 src/services/library.list_books（与墨师 book_list 动作同源）。
        """
        items = _lib_list_books(novel_id, hub.active_ids(), hub.done_ids())
        return JSONResponse({"books": items})

    @app.post("/api/books")
    def create_book(body: BookCreateBody) -> JSONResponse:
        """新建书：校验 slug 后创建空书目录骨架（后续经该书 /api/start 走 bootstrap）。

        创作模式（`mode`）：
        · `pipeline`（默认）＝自由创作：大纲 → 章节流水线；
        · `interactive` ＝互动创作：只做世界观/人物设定（不产大纲），后续由剧情卡逐章推进。
          落地方式就是建一个 `<书>/interactive/` 目录 —— `/api/books` 的 `interactive`
          标志正是按这个目录是否存在计算的。

        P0 重构：实现委托 src/services/library.create_book（与墨师 book_create 动作同源）。
        """
        try:
            nid = _lib_create_book(body.novel_id, body.mode)
        except _LibraryError as exc:
            _library_error_to_http(exc)
        hub.get_or_create(nid)
        _invalidate_workspace_index()
        return JSONResponse({"ok": True, "novel_id": nid, "mode": body.mode})

    @app.delete("/api/books/{nid}")
    def delete_book(nid: str) -> JSONResponse:
        """删书：校验后删除 MD 事实源目录，并尽力清理派生数据。

        保护规则：非法 ID 400；不存在 404；生成中 409；默认书 409
        （默认书是启动参数指定的兜底书，删除后书架仍会展示空壳，故禁删）。

        P0 重构：实现委托 src/services/library.delete_book（与墨师 book_delete 动作同源）。
        """
        try:
            warns = _lib_delete_book(
                nid, novel_id, hub.active_ids(), remove_session=hub.remove
            )
        except _LibraryError as exc:
            _library_error_to_http(exc)
        _invalidate_workspace_index()
        return JSONResponse({"ok": True, "novel_id": nid, "warnings": warns})

    # ---------- 自定义创作 Skill（全局，跨书复用） ----------

    @app.get("/api/custom-skills")
    def custom_skills_list() -> JSONResponse:
        """枚举全部自定义 Skill（data/custom_skills/*.md）。"""
        return JSONResponse({"skills": _list_custom_skills()})

    @app.post("/api/custom-skills")
    def custom_skills_create(body: CustomSkillBody) -> JSONResponse:
        """新建自定义 Skill；缺省 skill_id 由标题哈希生成，重名 409。"""
        title = body.title.strip()
        content = body.content.strip()
        if not title or not content:
            raise HTTPException(400, "Skill 名称与内容均不能为空")
        sid = body.skill_id.strip() or "sk-" + hashlib.md5(title.encode("utf-8")).hexdigest()[:8]
        if not _SKILL_ID_RE.match(sid):
            raise HTTPException(400, f"非法 Skill 标识：{sid!r}（仅限字母/数字/下划线/连字符）")
        path = _custom_skills_dir() / f"{sid}.md"
        if path.exists():
            raise HTTPException(409, f"Skill {sid} 已存在（同名标题请改名或先删除）")
        path.write_text(
            frontmatter.dumps(frontmatter.Post(content, title=title)),
            encoding="utf-8", newline="\n",
        )
        return JSONResponse({"ok": True, "skill_id": sid, "title": title})

    @app.put("/api/custom-skills/{sid}")
    def custom_skills_update(sid: str, body: CustomSkillBody) -> JSONResponse:
        """更新自定义 Skill（不存在 404）。"""
        title = body.title.strip()
        content = body.content.strip()
        if not title or not content:
            raise HTTPException(400, "Skill 名称与内容均不能为空")
        if not _SKILL_ID_RE.match(sid):
            raise HTTPException(400, f"非法 Skill 标识：{sid!r}")
        path = _custom_skills_dir() / f"{sid}.md"
        if not path.exists():
            raise HTTPException(404, f"Skill {sid} 不存在")
        path.write_text(
            frontmatter.dumps(frontmatter.Post(content, title=title)),
            encoding="utf-8", newline="\n",
        )
        return JSONResponse({"ok": True, "skill_id": sid, "title": title})

    @app.delete("/api/custom-skills/{sid}")
    def custom_skills_delete(sid: str) -> JSONResponse:
        """删除自定义 Skill（不存在 404；已写入各书的约束不受影响）。"""
        if not _SKILL_ID_RE.match(sid):
            raise HTTPException(400, f"非法 Skill 标识：{sid!r}")
        path = _custom_skills_dir() / f"{sid}.md"
        if not path.exists():
            raise HTTPException(404, f"Skill {sid} 不存在")
        path.unlink()
        return JSONResponse({"ok": True, "skill_id": sid})

    # ---------- Agent 模型接入配置（全局，跨书生效） ----------

    @app.get("/api/model-config")
    def model_config_get() -> JSONResponse:
        """查看各 Agent 角色的接入点/模型绑定与接入点定义（密钥掩码）。"""
        return JSONResponse(_model_config_view(_load_models_raw()))

    @app.put("/api/model-config/roles/{role}")
    def model_config_update_role(role: str, body: RoleBindingBody) -> JSONResponse:
        """修改角色绑定（provider/model/temperature/max_tokens；fallback 保留）。"""
        raw = _load_models_raw()
        roles = raw.setdefault("roles", {})
        if role not in roles:
            raise HTTPException(404, f"未知角色：{role}（可用：{list(roles)}）")
        binding = {"provider": body.provider.strip(),
                   "model": body.model.strip(),
                   "temperature": body.temperature}
        if body.max_tokens:
            binding["max_tokens"] = body.max_tokens
        if isinstance(roles[role], dict) and roles[role].get("fallback"):
            binding["fallback"] = roles[role]["fallback"]
        roles[role] = binding
        _save_models_raw(raw)
        logger.info("角色[%s]绑定更新 → %s/%s", role, body.provider, body.model)
        return JSONResponse({"ok": True, **_model_config_view(raw)})

    @app.put("/api/model-config/providers/{name}")
    def model_config_upsert_provider(name: str, body: ProviderBody) -> JSONResponse:
        """新建/更新接入点；api_key 含 **** 时保持原值（免重填密钥）。"""
        if not _PROVIDER_NAME_RE.match(name):
            raise HTTPException(400, f"非法接入点名：{name!r}（仅限字母/数字/下划线/连字符）")
        raw = _load_models_raw()
        providers = raw.setdefault("providers", {})
        old = providers.get(name) or {}
        key = body.api_key.strip() or "none"
        if "****" in key:
            key = old.get("api_key", "none")   # 掩码回传 → 保持原密钥
        provider = {"type": body.type.strip(), "api_key": key}
        if body.base_url.strip():
            provider["base_url"] = body.base_url.strip()
        providers[name] = provider
        _save_models_raw(raw)
        logger.info("接入点[%s]已%s", name, "更新" if old else "新建")
        return JSONResponse({"ok": True, **_model_config_view(raw)})

    @app.delete("/api/model-config/providers/{name}")
    def model_config_delete_provider(name: str) -> JSONResponse:
        """删除接入点（仍被角色/fallback 引用时 409）。"""
        raw = _load_models_raw()
        providers = raw.get("providers") or {}
        if name not in providers:
            raise HTTPException(404, f"接入点 {name} 不存在")
        used_by = [r for r, b in (raw.get("roles") or {}).items()
                   if b.get("provider") == name
                   or (b.get("fallback") or {}).get("provider") == name]
        if used_by:
            raise HTTPException(409, f"接入点 {name} 仍被角色引用：{used_by}，请先改绑定")
        del providers[name]
        _save_models_raw(raw)
        return JSONResponse({"ok": True, **_model_config_view(raw)})

    @app.get("/api/status")
    def status(novel: str = "") -> JSONResponse:
        s = _sess(novel)
        snap = s.snapshot()
        metrics = _compute_metrics(s.store())
        return JSONResponse({**snap, "metrics": metrics})

    @app.get("/api/chapters")
    def chapters(novel: str = "") -> JSONResponse:
        store = _sess(novel).store()
        if not store.root.exists():
            return JSONResponse({"chapters": []})
        items = []
        for doc in sorted(
            store.list_chapters(), key=lambda d: d.metadata.get("chapter", 0)
        ):
            items.append(
                {
                    "chapter": doc.metadata.get("chapter"),
                    "volume": doc.metadata.get("volume"),
                    "title": doc.metadata.get("title", ""),
                    "status": doc.metadata.get("status", "draft"),
                    "score": doc.metadata.get("score"),
                    "first_review_passed": doc.metadata.get("first_review_passed"),
                    "model": doc.metadata.get("model"),
                    "used_fallback": doc.metadata.get("used_fallback", False),
                }
            )
        return JSONResponse({"chapters": items})

    @app.get("/api/chapters/{chapter}")
    def chapter_detail(chapter: int, novel: str = "") -> JSONResponse:
        store = _sess(novel).store()
        # 章节可能跨卷，遍历定位
        target = None
        for doc in store.list_chapters():
            if doc.metadata.get("chapter") == chapter:
                target = doc
                break
        if target is None:
            raise HTTPException(404, f"第 {chapter} 章不存在")
        review_html = None
        rel_review = store.review_rel_path(chapter)
        if store.exists(rel_review):
            review_html = md.markdown(
                store.read(rel_review).content, extensions=["tables", "fenced_code"]
            )
        return JSONResponse(
            {
                "chapter": chapter,
                "title": target.metadata.get("title", ""),
                "status": target.metadata.get("status", "draft"),
                "score": target.metadata.get("score"),
                "model": target.metadata.get("model"),
                "used_fallback": target.metadata.get("used_fallback", False),
                "content_html": md.markdown(target.content, extensions=["tables"]),
                "review_html": review_html,
            }
        )

    @app.get("/api/outline")
    def outline(novel: str = "") -> JSONResponse:
        store = _sess(novel).store()
        rel = "settings/outline.md"
        if not store.exists(rel):
            return JSONResponse({"html": None})
        return JSONResponse(
            {"html": md.markdown(store.read(rel).content, extensions=["tables"])}
        )

    @app.put("/api/outline")
    def save_outline(body: OutlineSaveBody, novel: str = "") -> JSONResponse:
        """人工编辑大纲后落盘（frontmatter volumes + 正文），作为后续正文生成依据。

        后续生成以此为准：串行图 assemble_context 每章重载 outline.md；
        并行/续跑经 load_outline 从 frontmatter 读取。
        """
        volumes = [v.model_dump() for v in body.volumes]
        # 校验：章号必须从 1 连续编号、不得重复或跳号（正文逐章生成不得缺章）
        chapter_nos = [c["chapter"] for v in volumes for c in v["chapters"]]
        if not chapter_nos:
            raise HTTPException(400, "大纲至少需要一卷且包含一章")
        if sorted(chapter_nos) != list(range(1, len(chapter_nos) + 1)):
            raise HTTPException(400, "章号必须从 1 连续编号、不得重复或跳号")
        store = _sess(novel).store()
        # 书名/主题未传时回落既有值，避免误清空
        title = body.book_title.strip()
        theme = body.theme.strip()
        if store.exists(OUTLINE_REL):
            meta = store.read(OUTLINE_REL).metadata
            title = title or meta.get("title", "")
            theme = theme or meta.get("theme", "")
        store.write(
            OUTLINE_REL,
            _render_outline_markdown(title, theme, volumes),
            metadata={"title": title, "theme": theme, "volumes": volumes},
            commit_message="人工编辑大纲（资料库）",
        )
        return JSONResponse({"ok": True, "chapters": len(chapter_nos)})

    @app.get("/api/cross-volume")
    def cross_volume(novel: str = "") -> JSONResponse:
        """跨卷一致性审查报告（并行模式 R2 产物）列表 + 渲染。"""
        store = _sess(novel).store()
        reviews_dir = store.root / "reviews"
        reports = []
        if reviews_dir.exists():
            for path in sorted(reviews_dir.glob("cross-vol-wave-*.review.md")):
                rel = path.relative_to(store.root).as_posix()
                doc = store.read(rel)
                reports.append(
                    {
                        "wave": doc.metadata.get("wave"),
                        "volumes": doc.metadata.get("volumes", []),
                        "consistent": doc.metadata.get("consistent"),
                        "issue_count": doc.metadata.get("issue_count", 0),
                        "html": md.markdown(doc.content, extensions=["tables"]),
                    }
                )
        return JSONResponse({"reports": reports})

    @app.get("/api/pending")
    def pending(novel: str = "") -> JSONResponse:
        """当前待人审项（大纲或章节）。"""
        return JSONResponse(_sess(novel).snapshot())

    @app.get("/api/queue")
    def queue(novel: str = "") -> JSONResponse:
        """待审队列（P2）：波次内已起草尚未定稿的章节（status=draft）升序。"""
        store = _sess(novel).store()
        if not store.root.exists():
            return JSONResponse({"queue": []})
        items = []
        for doc in sorted(
            store.list_chapters(), key=lambda d: d.metadata.get("chapter", 0)
        ):
            if doc.metadata.get("status") != "draft":
                continue
            ch = doc.metadata.get("chapter")
            entry = {
                "chapter": ch,
                "volume": doc.metadata.get("volume"),
                "title": doc.metadata.get("title", ""),
                "attempt": doc.metadata.get("attempt"),
            }
            rel = store.review_rel_path(ch)
            if store.exists(rel):
                entry["overall"] = store.read(rel).metadata.get("overall")
            items.append(entry)
        return JSONResponse({"queue": items})

    @app.post("/api/pause")
    def pause_run(novel: str = "") -> JSONResponse:
        """暂停生成（当前章写完即停在逐章确认关卡；已停在关卡则立即结束本轮）。"""
        return JSONResponse(_sess(novel).request_pause())

    @app.post("/api/decision")
    def decision(body: Decision, novel: str = "") -> JSONResponse:
        payload = {"action": body.action}
        if body.action == "reject":
            payload["feedback"] = body.feedback
            payload["revision_mode"] = body.revision_mode
        try:
            _sess(novel).submit_decision(payload)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.post("/api/demo")
    def demo_start(body: DemoBody, novel: str = "") -> JSONResponse:
        """发起设定 Demo 生成（先审后入库；重复调用即重新生成）。"""
        brief = _effective_brief(body.brief, body.brief_fields)
        if not brief:
            raise HTTPException(400, "创作需求 brief 不能为空")
        try:
            _sess(novel).start_demo(
                brief, body.chapters, body.feedback, body.skill_ids
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.get("/api/demo")
    def demo_status(novel: str = "") -> JSONResponse:
        """Demo 生成状态轮询：{status, error, demo}。"""
        return JSONResponse(_sess(novel).demo_snapshot())

    @app.post("/api/demo/confirm")
    def demo_confirm(body: DemoConfirmBody = DemoConfirmBody(),
                     novel: str = "") -> JSONResponse:
        """人工同意 Demo：写入资料库 settings/，正式生成时沿用不再推翻。

        可携用户编辑后的完整 demo（互动/向导可编辑审核），无则沿用服务端缓存。
        """
        demo_override = None
        if body.demo is not None:
            from src.agents.schemas import DemoOutput
            try:
                demo_override = DemoOutput.model_validate(body.demo)
            except Exception as exc:  # noqa: BLE001 - 校验信息原样透出
                raise HTTPException(400, f"Demo 校验失败：{exc}")
        try:
            info = _sess(novel).confirm_demo(demo_override)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True, **info})

    # ---------- 互动创作模式（剧情卡选择，独立会话状态机） ----------

    def _isess(novel: str = "") -> InteractiveSession:
        try:
            return hub.get_or_create_interactive(novel or novel_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/interactive/start")
    def interactive_start(novel: str = "") -> JSONResponse:
        """启动/续跑互动会话（从 interactive/ 与章节目录恢复断点）。"""
        nid = novel or novel_id
        # 隔离：只有「互动创作」模式的书能开剧情卡流程
        if not (get_settings().novels_dir / nid / "interactive").is_dir():
            raise HTTPException(
                400,
                "这本书是「自由创作」模式：请用章节流水线推进；互动创作仅对建书时选择"
                "「互动创作」的书开放。",
            )
        if nid in hub.active_ids():
            raise HTTPException(409, f"书 {nid} 正在全自动生成中，无法启动互动创作")
        try:
            _isess(novel).start()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.get("/api/interactive/state")
    def interactive_state(novel: str = "") -> JSONResponse:
        """互动会话快照轮询：{status, chapter, cards, draft, approved_count, error}。"""
        return JSONResponse(_isess(novel).snapshot())

    @app.post("/api/interactive/choose")
    def interactive_choose(body: InteractiveChooseBody, novel: str = "") -> JSONResponse:
        """选卡（c1/c2/c3/custom）→ 后台写章 + Editor 参考审查。"""
        if body.card_id == "custom" and not body.custom_text.strip():
            raise HTTPException(400, "自定义卡剧情内容不能为空")
        try:
            _isess(novel).choose(body.card_id, body.custom_text, body.target_words)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.post("/api/interactive/redraw")
    def interactive_redraw(body: InteractiveRedrawBody = InteractiveRedrawBody(),
                           novel: str = "") -> JSONResponse:
        """携意见重抽 3 张剧情卡。"""
        try:
            _isess(novel).redraw(body.feedback)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.post("/api/interactive/decision")
    def interactive_decision(body: Decision, novel: str = "") -> JSONResponse:
        """人审裁决：approve 定稿入库并自动进入下一章出卡；reject 携意见重写。"""
        try:
            _isess(novel).decision(body.action, body.feedback, body.revision_mode)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.post("/api/start")
    def start(body: StartBody, novel: str = "") -> JSONResponse:
        # 隔离：互动创作模式的书不走章节流水线（剧情由作者逐章决定）
        nid_guard = novel or novel_id
        if (get_settings().novels_dir / nid_guard / "interactive").is_dir():
            raise HTTPException(
                400,
                "这本书是「互动创作」模式：请在对话栏点「互动创作」，用剧情卡逐章推进；"
                "章节流水线只服务「自由创作」模式的书。",
            )
        brief = _effective_brief(body.brief, body.brief_fields)
        if not brief:
            raise HTTPException(400, "创作需求 brief 不能为空")
        try:
            _sess(novel).start(
                brief, body.chapters, body.parallel, body.workers, body.skill_ids
            )
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    @app.post("/api/resume")
    def resume(body: ResumeBody = ResumeBody(), novel: str = "") -> JSONResponse:
        # 互动模式书使用独立的 InteractiveSession，不走 LangGraph 断点续跑
        if (_sess(novel).store().root / "interactive").is_dir():
            raise HTTPException(400, "该书为互动创作模式，请使用互动创作入口开始/续跑")
        try:
            _sess(novel).resume_existing(body.parallel, body.workers)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True})

    # ────────── 蒸馏 API（NDS: Novel Distillation System）──────────
    # 请求体模型已在模块级定义（局部 BaseModel 在 from __future__ import annotations 下
    # 会被 FastAPI 误判为 query 参数，导致 422）。

    @app.post("/api/distill/init")
    def distill_init(body: DistillInitBody) -> JSONResponse:
        """上传书籍文件，分块并创建技能包目录。

        安全（S1-4）：file_path 必须由**用户通过系统对话框授权**。
        桌面壳在转发前校验该路径来自 dialog:pickBookFile 的授权集合，
        引擎侧再做绝对路径 / 存在性 / 后缀 / 可选根目录包含性校验，
        避免「任意本机文件被读入技能包后经 /api/skills/export 外带」。
        """
        from src.skills.distill import DistillSettings, DistillSkill

        resolved = _resolve_upload_path(body.file_path)
        skill = DistillSkill(
            DistillSettings(
                enabled=True,
                chunk_strategy=body.chunk_strategy,
                chunk_size=body.chunk_size,
            ),
            None,  # context not needed for init
        )
        try:
            result = skill.run(
                action="init",
                file_path=str(resolved),
                skill_id=body.skill_id,
                version=body.version,
            )
            return JSONResponse(result)
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/distill/start")
    def distill_start(body: DistillStartBody) -> JSONResponse:
        """启动后台蒸馏（非阻塞）。"""
        from src.config.settings import load_models_config
        from src.llm.registry import ModelRegistry
        from src.skills.distill import DistillSettings, DistillSkill

        # 构建 SkillContext（DistillSkill 需要 registry）
        class _Ctx:
            def __init__(self):
                self.novel_id = ""
                self.store = None
                self.memory = None
                self.registry = ModelRegistry(load_models_config())
        ctx = _Ctx()

        skill = DistillSkill(DistillSettings(enabled=True), ctx)
        try:
            result = skill.run(action="start", skill_id=body.skill_id)
            return JSONResponse(result)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(400, str(exc))
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/distill/status/{skill_id}")
    def distill_status(skill_id: str) -> JSONResponse:
        """蒸馏进度轮询。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(action="status", skill_id=skill_id)
            return JSONResponse(result)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/distill/report/{skill_id}")
    def distill_report(skill_id: str) -> JSONResponse:
        """获取完整 16 维蒸馏报告。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(action="report", skill_id=skill_id)
            return JSONResponse(result)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/skills/list")
    def skills_list() -> JSONResponse:
        """列出所有已蒸馏的技能包。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        result = skill.run(action="list")
        return JSONResponse(result)

    @app.get("/api/skills/load/{skill_id}")
    def skills_load(skill_id: str) -> JSONResponse:
        """按 ID 加载完整技能包。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(action="report", skill_id=skill_id)
            return JSONResponse(result)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.post("/api/skills/reprocess")
    def skills_reprocess(body: DistillReprocessBody) -> JSONResponse:
        """重新蒸馏（版本更新）。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(
                action="reprocess",
                skill_id=body.skill_id,
                mode=body.mode,
                new_file_path=body.new_file_path,
                new_version=body.new_version,
            )
            return JSONResponse(result)
        except (FileNotFoundError, FileExistsError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.get("/api/skills/export/{skill_id}")
    def skills_export(skill_id: str):
        """打包下载技能包 .zip。"""
        from fastapi.responses import FileResponse

        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(action="export", skill_id=skill_id)
            path = result["path"]
            return FileResponse(
                path, media_type="application/zip",
                filename=f"{skill_id}.zip",
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    @app.delete("/api/skills/{skill_id}")
    def skills_delete(skill_id: str) -> JSONResponse:
        """删除指定技能包。"""
        from src.skills.distill import DistillSettings, DistillSkill
        skill = DistillSkill(DistillSettings(enabled=True), None)
        try:
            result = skill.run(action="delete", skill_id=skill_id)
            return JSONResponse(result)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc))

    # ────────── Inkforge 桌面端扩展（对话/资料库/章节编辑/技能绑定） ──────────
    from src.web.inkforge_api import register_inkforge_api
    register_inkforge_api(app, hub, novel_id)
    from src.web.inkforge_extra import register_inkforge_extra
    register_inkforge_extra(app, hub, novel_id)
    from src.web.inkforge_proposals import register_proposal_api
    register_proposal_api(app, hub, novel_id)
    from src.web.inkforge_windows import register_windows_api
    register_windows_api(app, hub, novel_id)

    # ────────── 访问控制（S1-4）──────────
    # 必须在所有路由注册完成后再挂中间件；token 由桌面壳经环境变量注入，
    # 未注入时（CLI / 单元测试 / 旧内置单页）不启用鉴权。
    from src.web.auth import engine_token, install_auth

    if install_auth(app):
        logger.info("引擎访问控制已启用（Bearer token + Origin 拒绝）")
    else:
        logger.warning(
            "引擎访问控制未启用：%s 未设置。仅允许在 CLI/测试场景下如此运行。",
            "INKFORGE_ENGINE_TOKEN",
        )
    _ = engine_token  # 保留导入以便调试时读取

    return app


def _compute_metrics(store: MdStore) -> dict:
    """三验收指标：一次通过率 / 伏笔回收率 / 平均分。"""
    if not store.root.exists():
        return {}
    chapters = store.list_chapters()
    approved = [c for c in chapters if c.metadata.get("status") == "approved"]
    first_pass = [c for c in approved if c.metadata.get("first_review_passed")]
    scores = [c.metadata.get("score") for c in approved if c.metadata.get("score")]
    metrics = {
        "total_chapters": len(chapters),
        "approved": len(approved),
        "first_pass_rate": round(len(first_pass) / len(approved) * 100, 1) if approved else None,
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
    }
    if store.exists("settings/foreshadowing.md"):
        items = store.read("settings/foreshadowing.md").metadata.get("items", []) or []
        total = len(items)
        resolved = sum(1 for i in items if isinstance(i, dict) and i.get("status") == "resolved")
        metrics["foreshadow_total"] = total
        metrics["foreshadow_resolved"] = resolved
        metrics["foreshadow_rate"] = round(resolved / total * 100, 1) if total else None
    return metrics


# 前端页面在单独模块，避免本文件过长
from src.web.page import INDEX_HTML as _INDEX_HTML  # noqa: E402


def main() -> None:
    import argparse

    import uvicorn

    from src.utils.logger import setup_logging

    parser = argparse.ArgumentParser(description="小说 Web 审阅系统")
    parser.add_argument("--novel-id", required=True,
                        help="默认打开的书（书架可切换/新建其他书）")
    parser.add_argument("--words", type=int, default=3000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000,
                        help="监听端口；传 0 由内核分配空闲端口并打印 INKFORGE_ENGINE_PORT")
    args = parser.parse_args()

    setup_logging(get_settings().log_level)
    app = create_app(args.novel_id, args.words)

    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level=get_settings().log_level.lower())
    # 先绑定再上报端口，由内核分配空闲端口（消除「探测-释放-再绑定」的 TOCTOU 竞态）；
    # 端口经 stdout 结构化标记回传，供 Electron 主进程解析。
    sock = config.bind_socket()
    actual_port = sock.getsockname()[1]
    print(f"INKFORGE_ENGINE_PORT={actual_port}", flush=True)
    logger.info("引擎监听 http://%s:%d", args.host, actual_port)
    uvicorn.Server(config).run(sockets=[sock])


if __name__ == "__main__":
    main()
