"""MD 存储层（D9：MD 唯一事实源）。

- frontmatter 读写：结构化字段放 YAML frontmatter，正文为 Markdown。
- 目录约定：settings/ chapters/ summaries/ reviews/（见计划书 §3.2）。
- Git 自动提交：每次系统写入自动 commit，历史由 Git 承担。
- hash 变更检测：维护 .file-hashes.json，供重嵌入判断人工编辑。
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from pathlib import Path

import frontmatter
from git import GitCommandError, InvalidGitRepositoryError, Repo

from src.utils.logger import get_logger

logger = get_logger(__name__)

HASH_INDEX_FILE = ".file-hashes.json"

# GitPython 的 index/commit 链路非线程安全（并行起草时多个线程会同时落盘）。
# 进程内全局串行化提交；LLM 调用仍并行，锁只覆盖文件系统与 git 操作，开销可忽略。
_GIT_LOCK = threading.RLock()


class MdDocument:
    """一份带 frontmatter 的 MD 文档。"""

    def __init__(self, path: Path, metadata: dict, content: str):
        self.path = path
        self.metadata = metadata
        self.content = content

    @property
    def doc_id(self) -> str:
        """以相对小说根目录路径为稳定 ID（由 MdStore 填充 rel_path）。"""
        return self.metadata.get("_rel_path", str(self.path))


class MdStore:
    """单部小说的数据仓库，根目录 data/novels/<novel-id>/。"""

    SUBDIRS = (
        "settings/worldview",
        "settings/characters",
        "chapters",
        "summaries",
        "reviews",
    )

    def __init__(self, novel_dir: Path, auto_git: bool = True, create_repo: bool = True):
        self.root = Path(novel_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        # 子目录惰性创建：读路径不再产生写副作用（书架列表/健康探针会构造大量 MdStore）。
        # 需要骨架目录的调用方（如建书向导）显式调 ensure_subdirs()。
        #
        # create_repo=False 用于只读路径：只挂载**已存在**的仓库，绝不新建，
        # 避免「查一次写作历史就凭空创建 .git」这类写副作用。
        if auto_git:
            self._repo, self._repo_scope = _resolve_repo(self.root, create=create_repo)
        else:
            self._repo, self._repo_scope = None, None

    def ensure_subdirs(self) -> None:
        """创建标准子目录骨架（建书/首次写入时调用）。"""
        for sub in self.SUBDIRS:
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # ---------- frontmatter 读写 ----------

    def read(self, rel_path: str) -> MdDocument:
        path = self.root / rel_path
        if not path.exists():
            raise FileNotFoundError(f"MD 文件不存在: {path}")
        post = frontmatter.load(str(path))
        meta = dict(post.metadata)
        meta["_rel_path"] = rel_path
        return MdDocument(path, meta, post.content)

    def write(
        self,
        rel_path: str,
        content: str,
        metadata: dict | None = None,
        commit_message: str | None = None,
    ) -> Path:
        """写入 MD（覆盖），自动更新 hash 索引并 Git 提交。"""
        path = self.root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {k: v for k, v in (metadata or {}).items() if not k.startswith("_")}
        post = frontmatter.Post(content, **meta)
        text = frontmatter.dumps(post)
        # 统一 LF 换行，保证 hash 计算与磁盘字节一致（Windows 下勿用默认换行翻译）
        path.write_text(text, encoding="utf-8", newline="\n")
        self._update_hash(rel_path, text)
        if self._repo is not None:
            self._commit(commit_message or f"update {rel_path}", [rel_path])
        return path

    def update_metadata(
        self, rel_path: str, updates: dict, commit_message: str | None = None
    ) -> None:
        """只更新 frontmatter 字段，正文不变。"""
        doc = self.read(rel_path)
        meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
        meta.update(updates)
        self.write(rel_path, doc.content, meta, commit_message)

    def delete(self, rel_path: str, commit_message: str | None = None) -> bool:
        """删除 MD 文档并同步提交（写作历史必须记录删除）。

        背景：章节删除此前直接 `path.unlink()`，绕过了本层 → 删除不进 Git 历史、
        `.file-hashes.json` 留下悬空条目。历史不完整会让「可回滚」的承诺落空。
        返回是否真的删除了文件。
        """
        path = self.root / rel_path
        if not path.exists():
            return False
        path.unlink()
        hashes = self._load_hashes()
        if rel_path in hashes:
            hashes.pop(rel_path, None)
            self._hash_index_path().write_text(
                json.dumps(hashes, ensure_ascii=False, indent=1), encoding="utf-8"
            )
        if self._repo is not None:
            self._commit(commit_message or f"delete {rel_path}", [rel_path], removed=True)
        return True

    def exists(self, rel_path: str) -> bool:
        return (self.root / rel_path).exists()

    def iter_documents(self, subdir: str = "") -> Iterator[MdDocument]:
        """遍历（子）目录下全部 MD 文档。"""
        base = self.root / subdir if subdir else self.root
        if not base.exists():
            return
        for path in sorted(base.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            try:
                yield self.read(rel)
            except Exception as e:  # frontmatter 损坏（R6）：跳过并告警
                logger.error("MD 解析失败，已跳过 %s: %s", rel, e)

    # ---------- 章节路径约定 ----------

    @staticmethod
    def chapter_rel_path(volume: int, chapter: int) -> str:
        return f"chapters/vol-{volume:02d}/ch-{chapter:03d}.md"

    @staticmethod
    def summary_rel_path(chapter: int) -> str:
        return f"summaries/ch-{chapter:03d}.summary.md"

    @staticmethod
    def review_rel_path(chapter: int) -> str:
        return f"reviews/ch-{chapter:03d}.review.md"

    def list_chapters(self) -> list[MdDocument]:
        return list(self.iter_documents("chapters"))

    # ---------- hash 变更检测 ----------

    def _hash_index_path(self) -> Path:
        return self.root / HASH_INDEX_FILE

    def _load_hashes(self) -> dict[str, str]:
        p = self._hash_index_path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
        return {}

    def _update_hash(self, rel_path: str, text: str) -> None:
        hashes = self._load_hashes()
        hashes[rel_path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._hash_index_path().write_text(
            json.dumps(hashes, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def detect_changed(self) -> list[str]:
        """对比 hash 索引，返回被人工修改/新增的 MD 相对路径（用于重嵌入）。"""
        hashes = self._load_hashes()
        changed: list[str] = []
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if hashes.get(rel) != digest:
                changed.append(rel)
        return changed

    def mark_synced(self, rel_paths: list[str]) -> None:
        """重嵌入完成后刷新 hash 索引。"""
        hashes = self._load_hashes()
        for rel in rel_paths:
            path = self.root / rel
            if path.exists():
                hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
        self._hash_index_path().write_text(
            json.dumps(hashes, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---------- Git 自动提交 ----------

    def _commit(
        self,
        message: str,
        rel_paths: list[str] | None = None,
        removed: bool = False,
    ) -> None:
        """把指定文件（相对本书根目录）的变更提交到本书所属仓库。

        性能与正确性（2026-09-14 两轮修复）：
        1. 原实现用 `repo.git.add()` + `repo.is_dirty()`，每次提交都 spawn git 子进程，
           且作用域是整棵目录树；改为纯 Python 的 IndexFile 操作后不再产生子进程。
        2. **必须传入具体文件路径，绝不传 "."**：GitPython 的 `IndexFile.add()`
           是纯 Python 实现，**不会**像 git porcelain 那样自动排除 `.git/`——
           传 "." 会把仓库自身的 40 余个对象文件加进索引，导致每次提交重新遍历并
           重新写入，开销呈 O(n²) 累积（实测 60 次提交从 12s 恶化到 >240s 不收敛，
           同时污染索引）。此处按文件精确 add，并再做一道 `.git` 兜底过滤。
        """
        if self._repo is None:
            return
        try:
            with _GIT_LOCK:
                repo = self._repo
                work_tree = Path(repo.working_tree_dir).resolve()
                items = self._to_repo_paths(rel_paths, work_tree)
                if not items:
                    return
                index = repo.index
                if removed:
                    index.remove(items, working_tree=False)
                else:
                    # force=False：尊重 .gitignore，不把被忽略的文件塞进索引
                    index.add(items, force=False)
                if repo.head.is_valid():
                    changed = bool(index.diff("HEAD"))
                else:
                    changed = True  # 空仓库（unborn HEAD）：首次提交
                if changed:
                    index.commit(f"[novel] {message}")
        except Exception as e:  # noqa: BLE001 - 提交失败不影响事实源写入
            logger.warning("Git 自动提交失败（不影响写入）: %s", e)

    def _to_repo_paths(self, rel_paths: list[str] | None, work_tree: Path) -> list[str]:
        """把本书内相对路径换算为仓库相对路径，并剔除 `.git` 内部与越界项。"""
        if not rel_paths:
            # 无显式清单时退化为「本书根目录」；独立仓库下为 "."，但必须过滤 .git
            candidates = [""]
        else:
            candidates = list(rel_paths)
        out: list[str] = []
        for rel in candidates:
            target = (self.root / rel).resolve() if rel else self.root.resolve()
            try:
                repo_rel = target.relative_to(work_tree).as_posix()
            except ValueError:
                continue  # 不在本仓库内（异常布局）→ 跳过
            if repo_rel == ".":
                # 兜底：绝不把仓库根整体加入（会连带 .git）；改为逐项列出本书内容
                out.extend(self._safe_tree_paths(work_tree))
                continue
            if repo_rel == ".git" or repo_rel.startswith(".git/") or "/.git/" in repo_rel:
                continue
            out.append(repo_rel)
        # 去重保序
        seen: set[str] = set()
        return [p for p in out if not (p in seen or seen.add(p))]

    def _safe_tree_paths(self, work_tree: Path) -> list[str]:
        """列出本书目录下应纳入索引的文件（排除 .git 与忽略文件）。"""
        out: list[str] = []
        for path in sorted(self.root.rglob("*")):
            if ".git" in path.parts:
                continue
            if not path.is_file():
                continue
            try:
                repo_rel = path.resolve().relative_to(work_tree).as_posix()
            except ValueError:
                continue
            if repo_rel == ".git" or repo_rel.startswith(".git/") or "/.git/" in repo_rel:
                continue
            out.append(repo_rel)
        return out

    # ---------- 写作历史（git log 只读视图） ----------

    def history(self, limit: int = 30) -> list[dict]:
        """返回本书最近 limit 条提交记录；无仓库或读取失败时返回空列表。

        当本书由外层仓库跟踪（_repo_scope 非空）时，按路径过滤，只回本该书的提交。
        """
        if self._repo is None:
            return []
        try:
            kwargs: dict = {"max_count": max(1, min(limit, 200))}
            if self._repo_scope:
                kwargs["paths"] = self._repo_scope
            with _GIT_LOCK:
                commits = list(self._repo.iter_commits(**kwargs))
            return [
                {
                    "sha": c.hexsha[:10],
                    "message": str(c.message).strip().splitlines()[0] if c.message else "",
                    "author": str(c.author),
                    "committed": int(c.committed_date),
                }
                for c in commits
            ]
        except Exception as e:  # noqa: BLE001 - 空仓库（无 HEAD）等情况
            logger.debug("读取写作历史失败: %s", e)
            return []


# ---------- 仓库归属解析（进程级缓存，避免重复 spawn git） ----------

# root(as_posix) -> (repo | None, scope | None)
# scope 为 None 表示该仓库的根就是本书目录；否则为本书在外层仓库中的相对路径。
_REPO_CACHE: dict[str, tuple[Repo | None, str | None]] = {}
_REPO_CACHE_LOCK = threading.Lock()


def _resolve_repo(root: Path, create: bool) -> tuple[Repo | None, str | None]:
    """解析本书应归属的 Git 仓库，返回 (repo, scope)。

    **进程级缓存**：`Repo(..., search_parent_directories=True)` 与
    `git check-ignore` 都会 spawn 子进程，而 MdStore 在每次 HTTP 请求中会被构造多次。
    不缓存时，几十次请求就会累积上百次 git 子进程，实测拖垮引擎。
    """
    root = Path(root).resolve()
    key = root.as_posix()
    with _REPO_CACHE_LOCK:
        cached = _REPO_CACHE.get(key)
    if cached is not None:
        repo, scope = cached
        # 缓存命中：已有仓库直接复用；判定为「无仓库」时仅读路径可返回，
        # 写路径需要重新判定（可能此时才需要并允许新建仓库）。
        if repo is not None or not create:
            return repo, scope

    result = _compute_repo(root, create)
    with _REPO_CACHE_LOCK:
        _REPO_CACHE[key] = result
    return result


def _compute_repo(root: Path, create: bool) -> tuple[Repo | None, str | None]:
    try:
        repo = Repo(root, search_parent_directories=True)
    except InvalidGitRepositoryError:
        if not create:
            return None, None
        logger.info("初始化小说数据 Git 仓库: %s", root)
        return Repo.init(root), None

    work_tree = repo.working_tree_dir
    if work_tree and Path(work_tree).resolve() == root:
        return repo, None

    if _outer_repo_ignores(repo, root):
        if not create:
            return None, None
        logger.info("外层仓库已忽略 %s，初始化独立 Git 仓库", root)
        return Repo.init(root), None

    # 外层仓库跟踪本书：复用外层，并把历史查询范围限定在本书子目录
    try:
        scope = str(root.relative_to(Path(repo.working_tree_dir).resolve())).replace("\\", "/")
    except ValueError:
        scope = None
    return repo, scope


def _outer_repo_ignores(repo: Repo, root: Path) -> bool:
    """外层仓库是否忽略本小说目录。

    `git check-ignore` 退出码 0=被忽略，1=未忽略，其它=探测失败。
    探测失败按「未忽略」处理（复用外层），并告警——因为误 init 嵌套仓库
    会把外层仓库弄脏，而复用外层最坏只是作用域变大。
    """
    try:
        repo.git.check_ignore(str(root))
        return True
    except GitCommandError as exc:
        if exc.status not in (1, None):
            logger.warning("check-ignore 探测失败（按未忽略处理）: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("check-ignore 探测异常（按未忽略处理）: %s", exc)
        return False


def slugify(name: str) -> str:
    """角色名/文件名安全化。"""
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name).strip("-") or "unnamed"
