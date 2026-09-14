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
from pathlib import Path
from typing import Iterator, Optional

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
        # create_repo=False 用于只读路径：只挂载**已存在**的书内仓库，绝不新建，
        # 避免「查一次写作历史就凭空创建 .git」这类写副作用。
        self._repo = self._ensure_repo(create=create_repo) if auto_git else None

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
        metadata: Optional[dict] = None,
        commit_message: Optional[str] = None,
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
            self._commit(commit_message or f"update {rel_path}")
        return path

    def update_metadata(
        self, rel_path: str, updates: dict, commit_message: Optional[str] = None
    ) -> None:
        """只更新 frontmatter 字段，正文不变。"""
        doc = self.read(rel_path)
        meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
        meta.update(updates)
        self.write(rel_path, doc.content, meta, commit_message)

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

    def _ensure_repo(self, create: bool = True) -> Optional[Repo]:
        """小说目录独立 Git 仓库；若位于外层仓库内且未被忽略则复用外层。

        外层仓库 .gitignore 排除 data/novels/ 时（用户创作数据不随仓库分发），
        在小说目录内初始化独立仓库，保证自动提交/历史能力不受影响。

        create=False（只读路径）：只返回**本就存在**的书内仓库；否则返回 None，
        既不新建仓库，也不误挂外层项目仓库（那会让「本书历史」混入项目提交）。
        """
        try:
            repo = Repo(self.root, search_parent_directories=True)
        except InvalidGitRepositoryError:
            if not create:
                return None
            logger.info("初始化小说数据 Git 仓库: %s", self.root)
            return Repo.init(self.root)
        work_tree = repo.working_tree_dir
        if work_tree and Path(work_tree).resolve() == self.root.resolve():
            return repo
        if self._outer_repo_ignores(repo):
            if not create:
                return None
            logger.info("外层仓库已忽略 %s，初始化独立 Git 仓库", self.root)
            return Repo.init(self.root)
        if not create:
            # 未忽略：本书文件由外层仓库跟踪，其提交不属于「本书历史」
            return None
        return repo

    def _outer_repo_ignores(self, repo: Repo) -> bool:
        """外层仓库是否忽略本小说目录。

        `git check-ignore` 退出码 0=被忽略，1=未忽略，其它=探测失败。
        探测失败按「未忽略」处理（复用外层），并告警——因为误 init 嵌套仓库
        会把外层仓库弄脏，而复用外层最坏只是提交被跳过。
        """
        try:
            repo.git.check_ignore(str(self.root.resolve()))
            return True
        except GitCommandError as exc:
            if exc.status not in (1, None):
                logger.warning("check-ignore 探测失败（按未忽略处理）: %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("check-ignore 探测异常（按未忽略处理）: %s", exc)
            return False

    def _commit(self, message: str) -> None:
        try:
            with _GIT_LOCK:
                repo = self._repo
                # 按仓库根解析待 add 路径：独立仓库时为 "."，复用外层仓库时为小说子目录
                rel = self.root.resolve().relative_to(
                    Path(repo.working_tree_dir).resolve()
                )
                repo.git.add(str(rel))
                if repo.is_dirty(index=True, working_tree=False, untracked_files=True):
                    repo.index.commit(f"[novel] {message}")
        except Exception as e:
            logger.warning("Git 自动提交失败（不影响写入）: %s", e)

    # ---------- 写作历史（git log 只读视图） ----------

    def history(self, limit: int = 30) -> list[dict]:
        """返回最近 limit 条提交记录；无 Git 仓库或读取失败时返回空列表。"""
        if self._repo is None:
            return []
        try:
            with _GIT_LOCK:
                commits = list(self._repo.iter_commits(max_count=max(1, min(limit, 200))))
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


def slugify(name: str) -> str:
    """角色名/文件名安全化。"""
    return re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name).strip("-") or "unnamed"
