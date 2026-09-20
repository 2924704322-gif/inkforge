"""书库服务层（P0 抽取）：建书 / 删书 / 书架枚举 / 自定义 Skill 约束合成。

为什么单独抽这一层（墨师全域化改造的"单一实现源"要求）：
- 同一个能力的调用方现在有两个：HTTP 端点（``server.py``）与墨师动作执行器
  （``actions.py``）。若各自实现一份，行为必然漂移。
- 本模块**不依赖 fastapi / hub / 向量库**，只做"纯数据与文件系统"操作，因此可以在
  端点、动作层、后台线程、单元测试中自由复用。
- 错误统一用 :class:`LibraryError`（带机器可读 code），由调用方决定翻译成
  HTTP 状态码还是动作回执。

既有端点行为保持逐字不变：``HTTPException`` 文案与状态码由 ``server.py`` 的薄包装
按 ``LibraryError.code`` 还原。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import sys
from pathlib import Path

import frontmatter

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.memory.store_factory import open_store
from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------- 常量（与 server.py 原实现同源；server.py 改为 re-export 本模块） ----------

NOVEL_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
# 注意：书名 ID 的校验强度是历史契约（首字符不得为 "-"），由契约测试逐项锁定；
# 后端取书路径（inkforge_api）另有一份更宽松的实现，本模块**不**去统一它们。
SKILL_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]*$")
BOOK_SUBDIRS = ("chapters", "settings", "summaries", "reviews")
CUSTOM_SKILLS_REL = "settings/custom-skills.md"
#: 书名落盘位置（书架 list_books 的事实源之一；书名优先级：outline.md:title → 本文件 book_title）
BOOK_OVERVIEW_REL = "settings/story-overview.md"
UPLOAD_ROOTS_ENV = "INKFORGE_UPLOAD_ROOTS"
UPLOAD_SUFFIXES = (".txt", ".epub", ".md", ".markdown")
CREATION_MODES = ("pipeline", "interactive")


class LibraryError(RuntimeError):
    """书库操作错误：code ∈ {bad_request, not_found, conflict}。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _bad(message: str) -> LibraryError:
    return LibraryError("bad_request", message)


def _not_found(message: str) -> LibraryError:
    return LibraryError("not_found", message)


def _conflict(message: str) -> LibraryError:
    return LibraryError("conflict", message)


# ---------- 路径与会话目录 ----------

def novels_root() -> Path:
    return get_settings().novels_dir


def custom_skills_dir() -> Path:
    d = novels_root().parent / "custom_skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def novel_dir(novel_id: str) -> Path:
    return novels_root() / novel_id


# ---------- 自定义 Skill（约束型，全局共享） ----------

def list_custom_skills() -> list[dict]:
    """枚举全部自定义 Skill（按文件名升序）。"""
    items = []
    for path in sorted(custom_skills_dir().glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001 - 损坏文件跳过不阻断列表
            logger.error("自定义 Skill 解析失败，已跳过 %s: %s", path.name, exc)
            continue
        items.append({
            "skill_id": path.stem,
            "title": post.metadata.get("title") or path.stem,
            "content": post.content.strip(),
        })
    return items


def compose_skill_constraints(skill_ids: list[str]) -> tuple[str, list[str]]:
    """把选定 Skill 合并为约束文本；返回 (合并正文, 命中的标题列表)。"""
    by_id = {s["skill_id"]: s for s in list_custom_skills()}
    sections, titles = [], []
    for sid in skill_ids:
        skill = by_id.get(sid)
        if skill is None:
            logger.warning("向导选定的自定义 Skill 不存在，已跳过: %s", sid)
            continue
        sections.append(f"## {skill['title']}\n\n{skill['content']}")
        titles.append(skill["title"])
    return "\n\n".join(sections), titles


def apply_skills_to_store(store: MdStore, skill_ids: list[str]) -> list[str]:
    """将选定 Skill 合并写入该书 settings/custom-skills.md（未选则不动既有文件）。"""
    if not skill_ids:
        return []
    content, titles = compose_skill_constraints(skill_ids)
    if not content:
        return []
    store.write(
        CUSTOM_SKILLS_REL,
        content,
        metadata={"title": "自定义创作约束", "skills": titles},
        commit_message="向导选定自定义 Skill",
    )
    return titles


BINDING_HEADER = "# 创作约束（风格工坊绑定，最高优先级）"


def sync_custom_skill_to_books(skill_id: str) -> list[str]:
    """把全局约束库里的某条约束**就地刷新**到所有已绑定它的书。

    由来（真实事故两连发）：① 约束建了没绑 → 正文里一条都没进去；② 绑了之后再去
    改约束内容，改动只落在全局库 `data/custom_skills/`，各书 settings/custom-skills.md
    里的仍是旧副本 —— 用户于是看到"改了也没用"。本函数让「编辑约束」立即对已绑作品生效。

    只重写约束段落，**保留各书原有的 bound_packs 技能包摘要**（不重新计算摘要，
    避免把包的旧快照换成新快照而产生意外差异）。返回被刷新的书目 id 列表。
    """
    by_id = {s["skill_id"]: s for s in list_custom_skills()}
    skill = by_id.get(skill_id)
    label = skill["title"] if skill else skill_id
    updated: list[str] = []
    novels_dir = novels_root()
    if not novels_dir.is_dir():
        return []
    for path in sorted(p for p in novels_dir.iterdir() if p.is_dir()):
        if not NOVEL_ID_RE.match(path.name):
            continue
        store = open_store(path, writable=False)
        rel = CUSTOM_SKILLS_REL
        if not store.exists(rel):
            continue
        try:
            doc = store.read(rel)
            bound_custom = [str(s) for s in (doc.metadata.get("bound_custom") or [])]
        except Exception as exc:  # noqa: BLE001 - 单本损坏不影响其它书
            logger.warning("读取 %s 的约束绑定失败，已跳过：%s", path.name, exc)
            continue
        if skill_id not in bound_custom:
            continue
        # 保留包的摘要段落：原文件里 "### 蒸馏技能包：" 起的部分原样截出来
        pack_tail = ""
        marker = "### 蒸馏技能包："
        if marker in doc.content:
            pack_tail = "\n\n" + doc.content[doc.content.index(marker):].strip()
        sections = [BINDING_HEADER]
        for sid in bound_custom:
            item = by_id.get(sid)
            if item is None:      # 约束已被删除：跳过但保留绑定记录，便于用户发现
                continue
            sections.append(f"## 自定义约束：{item['title']}\n\n{item['content']}")
        store_write = open_store(path, writable=True)
        store_write.write(
            rel,
            "\n\n".join(sections) + pack_tail,
            metadata={"bound_custom": bound_custom,
                      "bound_packs": list(doc.metadata.get("bound_packs") or [])},
            commit_message=f"同步约束更新：{label}",
        )
        updated.append(path.name)
    return updated


# ---------- 建书 ----------

def validate_novel_id(nid: str) -> str:
    value = (nid or "").strip()
    if not NOVEL_ID_RE.match(value):
        raise _bad(f"非法书名标识：{value!r}（仅限字母/数字/下划线/连字符）")
    return value


def book_exists(novel_id: str) -> bool:
    return novel_dir(novel_id).exists()


def derive_novel_id(title: str, taken: set[str] | None = None) -> str:
    """由书名派生一个合法的本书标识（目录名）。

    为什么需要它：二开建书是"给新书起个名"的轻量动作，用户填的多半是中文书名，
    而标识必须匹配 ``NOVEL_ID_RE``（纯 ASCII）。原实现把"英文目录名"硬塞给用户填，
    中文书名直接 400——体感就是"这功能还得先想个英文目录名"。
    派生规则（确定性、可预期）：
      1. 取书名的 ASCII 字母/数字部分（小写化，其余转 -）；
      2. 一个 ASCII 字符都不剩（纯中文书名）→ 回落 ``book-YYYYMMDD``；
      3. ``taken`` 里已存在 → 追加 -2/-3…（避免建书 409 让用户自己改）。
    """
    import datetime as _dt
    import re as _re

    raw = (title or "").strip().lower()
    slug = _re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    slug = _re.sub(r"-{2,}", "-", slug)[:40].strip("-")
    used = taken or set()

    def _free(base: str) -> str:
        if base and base not in used:
            return base
        n = 2
        while f"{base}-{n}" in used:
            n += 1
        return f"{base}-{n}"

    if slug and NOVEL_ID_RE.match(slug):
        return _free(slug)
    stamp = _dt.date.today().strftime("%Y%m%d")
    return _free(f"book-{stamp}")


def create_book(novel_id: str, mode: str = "pipeline", title: str = "") -> str:
    """创建空书目录骨架（纯文件系统；会话注册由调用方负责）。

    创作模式：
    · ``pipeline``（默认）＝自由创作：大纲 → 章节流水线；
    · ``interactive`` ＝互动创作：只做世界观/人物设定（不产大纲），
      落地方式就是建一个 ``<书>/interactive/`` 目录。

    `title`：作者给的书名。**必须落盘**（`settings/story-overview.md` 的
    `book_title` 字段）——书架/资源树的书名只认 `outline.md` 与
    `story-overview.md` 两处事实源，缺了就只能回落目录名。
    由来（用户实测）："二开建书只填了书名，结果书名消失、书架显示目录名"——
    修复前 `title` 参数在调用方算出来就被丢掉，从没落过盘。
    该文件**不带** `demo_confirmed`，因此不会被误判成"已确认的设定 Demo"。
    """
    nid = validate_novel_id(novel_id)
    if mode not in CREATION_MODES:
        raise _bad(f"未知创作模式：{mode!r}（pipeline / interactive）")
    book_dir = novel_dir(nid)
    if book_dir.exists():
        raise _conflict(f"书 {nid} 已存在")
    for sub in BOOK_SUBDIRS:
        (book_dir / sub).mkdir(parents=True, exist_ok=True)
    if mode == "interactive":
        (book_dir / "interactive").mkdir(parents=True, exist_ok=True)
    logger.info("新建书目 %s（mode=%s，title=%r）", nid, mode, title or "")
    if title.strip():
        store = open_store(book_dir, writable=True)
        store.write(
            BOOK_OVERVIEW_REL,
            f"# {title.strip()}\n\n> 书名由建书时填写；本书尚未生成设定或大纲。\n",
            metadata={"title": title.strip(), "book_title": title.strip(),
                      "theme": "", "created_from": "create_book"},
            commit_message=f"记录书名：{title.strip()}",
        )
    return nid


def ensure_book_skeleton(novel_id: str) -> Path:
    """确保书目录骨架存在（幂等；书已存在时不改动任何既有文件）。"""
    nid = validate_novel_id(novel_id)
    book_dir = novel_dir(nid)
    if not book_dir.exists():
        for sub in BOOK_SUBDIRS:
            (book_dir / sub).mkdir(parents=True, exist_ok=True)
    return book_dir


# ---------- 书架枚举（墨师全域上下文的事实源） ----------

def list_books(default_novel: str = "", active: set[str] | None = None,
               done: set[str] | None = None) -> list[dict]:
    """枚举 novels_dir 下全部书目及进度元信息（纯读，无写副作用）。

    与 ``GET /api/books`` 同源：书架列表是纯读路径，只挂载**已存在**的 Git 仓库，
    绝不新建骨架子目录（曾导致「列书架有写副作用」）。
    """
    novels_dir = novels_root()
    _active = active or set()
    _done = done or set()
    items: list[dict] = []
    ids: set[str] = set()
    if novels_dir.exists():
        ids = {p.name for p in novels_dir.iterdir()
               if p.is_dir() and NOVEL_ID_RE.match(p.name)}
    if default_novel:
        ids.add(default_novel)   # 默认书未落盘也展示
    for nid in sorted(ids):
        store = open_store(novels_dir / nid, writable=False)
        title = nid
        planned = 0
        if store.exists("settings/outline.md"):
            meta = store.read("settings/outline.md").metadata
            title = meta.get("title") or nid
            planned = sum(
                len(v.get("chapters", [])) for v in (meta.get("volumes") or [])
                if isinstance(v, dict)
            )
        elif store.exists("settings/story-overview.md"):
            # 互动模式书无 outline.md，书名回落到已确认的故事概要
            meta = store.read("settings/story-overview.md").metadata
            title = meta.get("book_title") or meta.get("title") or nid
        interactive = (store.root / "interactive").is_dir()
        chapters = store.list_chapters() if store.root.exists() else []
        approved = sum(1 for c in chapters if c.metadata.get("status") == "approved")
        is_active = nid in _active
        # 已绑定的自定义约束数：书架要能一眼看出"这本书到底有没有约束在生效"
        # （真实事故：用户在风格工坊建了约束却没绑定，正文里一条都没进去，界面上毫无线索）。
        bound_custom: list[str] = []
        if store.exists(CUSTOM_SKILLS_REL):
            try:
                bound_custom = list(store.read(CUSTOM_SKILLS_REL).metadata.get("bound_custom") or [])
            except Exception:  # noqa: BLE001 - 元数据损坏不影响书架列表
                bound_custom = []
        # 完结判定：本进程会话已跑完，或已定稿章数达到大纲计划章数（能扛重启）。
        finished = (not is_active) and (
            nid in _done or (planned > 0 and approved >= planned)
        )
        items.append({
            "novel_id": nid,
            "title": title,
            "planned": planned,
            "chapters": len(chapters),
            "approved": approved,
            "active": is_active,
            "finished": finished,
            "interactive": interactive,
            "is_default": nid == default_novel,
            "bound_constraints": len(bound_custom),
        })
    return items


# ---------- 删书 ----------

def _rmtree_onerror(func, path, _exc):
    """rmtree 出错回调：清除只读位后重试（func 为失败的 os 操作）。

    独立书目录常含 .git（用户数据独立仓库），其 objects/pack 文件被 git 标记为
    只读；Windows 下 shutil.rmtree 默认对只读文件抛 PermissionError。
    """
    os.chmod(path, stat.S_IWRITE)
    func(path)


def force_rmtree(path: Path) -> None:
    """删除目录树，兼容含只读文件（如 .git 对象）的场景。"""
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rmtree_onerror)
    else:
        shutil.rmtree(path, onerror=_rmtree_onerror)


def purge_derived_data(novel_id: str) -> list[str]:
    """删除一本书的派生数据（向量库/checkpoint/日志）；全部 best-effort。

    MD 事实源是唯一事实源，派生数据即使残留也可随时重建/覆盖，
    因此任何一步失败只记警告，不阻断删除。
    """
    settings = get_settings()
    warnings: list[str] = []

    # 1) Chroma 向量 collection（命名规则与 retriever.VectorIndex 一致）
    chroma_dir = settings.chroma_dir
    if chroma_dir.exists():
        try:
            import chromadb

            name = "novel-" + hashlib.sha1(novel_id.encode("utf-8")).hexdigest()[:12]
            client = chromadb.PersistentClient(path=str(chroma_dir))
            client.delete_collection(name)
        except Exception as exc:  # collection 不存在或库不可用
            warnings.append(f"向量库清理跳过: {exc}")

    # 2) checkpoints.sqlite 中该书的检查点（thread_id == novel_id）
    if settings.checkpoint_db.exists():
        try:
            conn = sqlite3.connect(str(settings.checkpoint_db))
            try:
                for table in ("checkpoints", "writes"):
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE thread_id = ?", (novel_id,))
                    except sqlite3.OperationalError:
                        pass    # 表不存在（尚未生成过）
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            warnings.append(f"checkpoint 清理跳过: {exc}")

    # 3) 运行日志（Windows 下可能被日志句柄占用，失败不阻断）
    log_file = settings.runtime_dir / "logs" / f"{novel_id}.log"
    try:
        log_file.unlink(missing_ok=True)
    except OSError as exc:
        warnings.append(f"日志清理跳过: {exc}")

    return warnings


def delete_book(novel_id: str, default_novel: str = "",
                active: set[str] | None = None, remove_session=None) -> list[str]:
    """删除书目：校验 → 移除会话 → 删目录 → 清理派生数据。返回警告列表。

    保护规则（与既有端点逐字一致）：非法 ID bad_request；不存在 not_found；
    生成中 conflict；默认书 conflict。
    """
    nid = validate_novel_id(novel_id)
    if default_novel and nid == default_novel:
        raise _conflict(f"书 {nid} 是默认书，不可删除")
    if nid in (active or set()):
        raise _conflict(f"书 {nid} 正在生成中，请等生成结束后再删除")
    book_dir = novel_dir(nid)
    if not book_dir.is_dir():
        raise _not_found(f"书 {nid} 不存在")

    if remove_session is not None:
        remove_session(nid)
    force_rmtree(book_dir)
    warnings = purge_derived_data(nid)
    if warnings:
        logger.warning("删书 %s 部分派生数据未清理: %s", nid, "; ".join(warnings))
    logger.info("已删除书目 %s", nid)
    return warnings
