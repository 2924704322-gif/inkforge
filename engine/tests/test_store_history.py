"""数据层回归测试：MD 事实源、写作历史（Git）、路径约定、变更检测。

对应调研报告 P0-1（每本书 Git 仓库此前是死代码）与 P1-5（读路径写副作用）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.memory.md_store import MdStore, slugify
from src.memory.store_factory import GIT_HISTORY_ENV, git_history_enabled, open_store


class TestStoreFactoryGitSemantics:
    def test_history_enabled_by_default(self, sandbox, monkeypatch):
        monkeypatch.delenv(GIT_HISTORY_ENV, raising=False)
        assert git_history_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "off", "no"])
    def test_history_can_be_disabled(self, sandbox, monkeypatch, value):
        monkeypatch.setenv(GIT_HISTORY_ENV, value)
        assert git_history_enabled() is False

    def test_writable_store_creates_repo(self, sandbox):
        book = sandbox.novels / "git-book"
        store = open_store(book, writable=True)
        assert store._repo is not None
        assert (book / ".git").exists()

    def test_readonly_store_never_creates_repo(self, sandbox):
        """回归 P1-5：查一次书架/历史不得凭空创建 .git。"""
        book = sandbox.novels / "readonly-book"
        store = open_store(book, writable=False)
        assert store._repo is None
        assert not (book / ".git").exists()

    def test_readonly_store_attaches_existing_repo(self, sandbox):
        book = sandbox.novels / "existing-book"
        open_store(book, writable=True)  # 先由写路径建立仓库
        store = open_store(book, writable=False)
        assert store._repo is not None

    def test_global_switch_disables_git_for_writable_store(self, sandbox, monkeypatch):
        monkeypatch.setenv(GIT_HISTORY_ENV, "0")
        book = sandbox.novels / "nogit-book"
        store = open_store(book, writable=True)
        assert store._repo is None
        assert not (book / ".git").exists()

    def test_write_still_works_without_git(self, sandbox, monkeypatch):
        monkeypatch.setenv(GIT_HISTORY_ENV, "0")
        store = open_store(sandbox.novels / "nogit2", writable=True)
        store.write("settings/outline.md", "# 大纲", metadata={"title": "T"})
        assert store.read("settings/outline.md").content.strip() == "# 大纲"


class TestGitIndexHygiene:
    """回归：纯 Python 的 IndexFile.add **不会**自动排除 .git/。

    曾实测到把仓库自身的 38 个对象文件加进索引，导致每次提交重新遍历对象，
    开销 O(n²) 累积（60 次提交从 12s 恶化到 240s 不收敛）。
    """

    def _tracked(self, repo_root: Path) -> list[str]:
        import subprocess

        return subprocess.run(
            ["git", "ls-files"], cwd=repo_root, capture_output=True, text=True
        ).stdout.splitlines()

    def test_nested_repo_never_tracks_git_internals(self, sandbox):
        book = sandbox.novels / "hygiene-nested"
        store = open_store(book, writable=True)
        for i in range(5):
            store.write(f"settings/d{i}.md", f"内容{i}", metadata={"title": f"T{i}"})
        tracked = self._tracked(book)
        assert tracked, "应有文件被跟踪"
        assert not [t for t in tracked if ".git/" in t], f"索引被 .git 污染: {tracked[:5]}"
        assert len(tracked) == 5

    def test_outer_repo_never_tracks_git_internals(self, sandbox, tmp_path):
        import subprocess

        outer = tmp_path / "hygiene-outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        store = open_store(outer / "novels" / "b", writable=True)
        for i in range(5):
            store.write(f"settings/d{i}.md", f"内容{i}", metadata={"title": f"T{i}"})
        tracked = self._tracked(outer)
        assert not [t for t in tracked if ".git/" in t], f"索引被 .git 污染: {tracked[:5]}"

    def test_commit_throughput_does_not_degrade(self, sandbox):
        """提交耗时不得随提交次数超线性增长（O(n²) 回归护栏）。"""
        import time

        store = open_store(sandbox.novels / "throughput", writable=True)
        timings: list[float] = []
        for i in range(24):
            t0 = time.perf_counter()
            store.write(f"settings/d{i:02d}.md", f"内容{i}" * 30, metadata={"title": f"T{i}"})
            timings.append(time.perf_counter() - t0)
        first_half = sum(timings[:8]) / 8
        last_half = sum(timings[-8:]) / 8
        # 允许 3 倍波动（CI 机器抖动），但不得出现数量级恶化
        assert last_half < first_half * 3 + 0.05, (
            f"提交耗时随次数恶化：前 8 次均值 {first_half:.3f}s，后 8 次均值 {last_half:.3f}s"
        )

    def test_delete_is_recorded_in_history(self, sandbox):
        """回归：章节删除此前直接 unlink，绕过本层 → 不进历史、哈希索引留悬空条目。"""
        store = open_store(sandbox.novels / "del-book", writable=True)
        store.write("chapters/vol-01/ch-001.md", "正文", metadata={"chapter": 1, "volume": 1})
        store.mark_synced(["chapters/vol-01/ch-001.md"])
        before = len(store.history(200))
        assert store.delete("chapters/vol-01/ch-001.md", commit_message="删除第一章") is True
        after = store.history(200)
        assert len(after) > before
        assert any("删除第一章" in c["message"] for c in after)
        assert not (store.root / "chapters" / "vol-01" / "ch-001.md").exists()
        assert "chapters/vol-01/ch-001.md" not in store._load_hashes()

    def test_delete_missing_file_returns_false(self, sandbox):
        store = open_store(sandbox.novels / "del-missing", writable=True)
        assert store.delete("settings/ghost.md") is False

    def test_untracked_git_dir_is_excluded_from_tree_walk(self, sandbox):
        """_safe_tree_paths 兜底：即便走「整树」分支也不得包含 .git。"""
        book = sandbox.novels / "walk-book"
        store = open_store(book, writable=True)
        store.write("settings/a.md", "A", metadata={"title": "A"})
        paths = store._safe_tree_paths(book.resolve())
        assert paths
        assert not [p for p in paths if ".git" in p.split("/")]


class TestWritingHistory:
    def test_write_produces_commit(self, sandbox):
        store = open_store(sandbox.novels / "hist-book", writable=True)
        store.write("settings/outline.md", "# 大纲", metadata={"title": "测试"}, commit_message="建大纲")
        commits = store.history()
        assert len(commits) >= 1
        assert commits[0]["message"].startswith("[novel]")
        assert "建大纲" in commits[0]["message"]
        assert len(commits[0]["sha"]) == 10

    def test_history_is_chronological_desc(self, sandbox):
        store = open_store(sandbox.novels / "hist-book2", writable=True)
        store.write("settings/outline.md", "A", commit_message="first")
        store.write("settings/outline.md", "B", commit_message="second")
        msgs = [c["message"] for c in store.history()]
        assert any("second" in m for m in msgs)
        assert msgs[0] > msgs[-1] or "second" in msgs[0]

    def test_history_respects_limit(self, sandbox):
        store = open_store(sandbox.novels / "hist-book3", writable=True)
        for i in range(5):
            store.write("settings/outline.md", f"v{i}", commit_message=f"rev{i}")
        assert len(store.history(limit=2)) == 2

    def test_history_empty_without_repo(self, sandbox):
        store = open_store(sandbox.novels / "no-history", writable=False)
        assert store.history() == []

    def test_history_survives_across_store_instances(self, sandbox):
        book = sandbox.novels / "hist-book4"
        open_store(book, writable=True).write("settings/outline.md", "A", commit_message="persist")
        assert any("persist" in c["message"] for c in open_store(book, writable=False).history())

    def test_chapter_write_produces_commit(self, sandbox):
        store = open_store(sandbox.novels / "hist-book5", writable=True)
        store.write(
            "chapters/vol-01/ch-001.md",
            "正文",
            metadata={"chapter": 1, "volume": 1, "status": "draft"},
            commit_message="ch-001 第 1 稿",
        )
        assert any("ch-001" in c["message"] for c in store.history())


class TestNoSideEffectReads:
    def test_constructor_does_not_create_subdirs(self, sandbox):
        book = sandbox.novels / "lazy-book"
        MdStore(book, auto_git=False)
        assert book.exists()
        assert not (book / "chapters").exists()
        assert not (book / "settings").exists()

    def test_readonly_open_does_not_create_subdirs(self, sandbox):
        book = sandbox.novels / "lazy-book2"
        open_store(book, writable=False)
        assert not (book / "settings").exists()

    def test_ensure_subdirs_creates_skeleton(self, sandbox):
        store = MdStore(sandbox.novels / "skeleton-book", auto_git=False)
        store.ensure_subdirs()
        for sub in MdStore.SUBDIRS:
            assert (store.root / sub).is_dir()

    def test_write_creates_parent_chain(self, sandbox):
        store = MdStore(sandbox.novels / "write-book", auto_git=False)
        store.write("settings/worldview/world.md", "内容", metadata={"title": "W"})
        assert (store.root / "settings" / "worldview" / "world.md").exists()


class TestMdStoreContract:
    def test_frontmatter_roundtrip(self, sandbox):
        store = MdStore(sandbox.novels / "rt", auto_git=False)
        meta = {"chapter": 3, "volume": 2, "title": "标题", "characters": ["A", "B"], "status": "draft"}
        store.write("chapters/vol-02/ch-003.md", "正文内容", metadata=meta)
        doc = store.read("chapters/vol-02/ch-003.md")
        assert doc.content.strip() == "正文内容"
        assert doc.metadata["chapter"] == 3
        assert doc.metadata["characters"] == ["A", "B"]
        assert doc.doc_id == "chapters/vol-02/ch-003.md"

    def test_private_metadata_stripped_on_write(self, sandbox):
        """以下划线开头的内部字段不得落盘（read() 会另行注入 _rel_path，故查磁盘原文）。"""
        store = MdStore(sandbox.novels / "priv", auto_git=False)
        store.write("settings/x.md", "c", metadata={"_rel_path": "leak", "title": "T"})
        on_disk = (store.root / "settings" / "x.md").read_text(encoding="utf-8")
        assert "_rel_path" not in on_disk
        assert "title: T" in on_disk

    def test_update_metadata_preserves_body(self, sandbox):
        store = MdStore(sandbox.novels / "upd", auto_git=False)
        store.write("settings/x.md", "原始正文", metadata={"title": "T", "n": 1})
        store.update_metadata("settings/x.md", {"n": 2, "status": "approved"})
        doc = store.read("settings/x.md")
        assert doc.content.strip() == "原始正文"
        assert doc.metadata["n"] == 2
        assert doc.metadata["status"] == "approved"

    def test_path_conventions(self):
        assert MdStore.chapter_rel_path(2, 7) == "chapters/vol-02/ch-007.md"
        assert MdStore.summary_rel_path(7) == "summaries/ch-007.summary.md"
        assert MdStore.review_rel_path(7) == "reviews/ch-007.review.md"

    def test_missing_document_raises(self, sandbox):
        store = MdStore(sandbox.novels / "miss", auto_git=False)
        with pytest.raises(FileNotFoundError):
            store.read("settings/ghost.md")

    def test_iter_documents_skips_broken_file(self, sandbox):
        """R6：损坏的 frontmatter 不应中断遍历。"""
        store = MdStore(sandbox.novels / "broken", auto_git=False)
        store.write("settings/ok.md", "good", metadata={"title": "OK"})
        (store.root / "settings" / "bad.md").write_text("---\n: : bad\n---\nbody", encoding="utf-8")
        ids = [d.doc_id for d in store.iter_documents("settings")]
        assert "settings/ok.md" in ids


class TestChangeDetection:
    def test_written_file_is_not_reported_as_changed(self, sandbox):
        store = MdStore(sandbox.novels / "chg", auto_git=False)
        store.write("settings/a.md", "A", metadata={"title": "A"})
        assert store.detect_changed() == []

    def test_manual_edit_detected(self, sandbox):
        store = MdStore(sandbox.novels / "chg2", auto_git=False)
        store.write("settings/a.md", "A", metadata={"title": "A"})
        path = store.root / "settings" / "a.md"
        path.write_text(path.read_text(encoding="utf-8").replace("A", "改"), encoding="utf-8")
        assert "settings/a.md" in store.detect_changed()

    def test_mark_synced_clears_changes(self, sandbox):
        store = MdStore(sandbox.novels / "chg3", auto_git=False)
        store.write("settings/a.md", "A", metadata={"title": "A"})
        (store.root / "settings" / "a.md").write_text("被改了", encoding="utf-8")
        changed = store.detect_changed()
        store.mark_synced(changed)
        assert store.detect_changed() == []

    def test_new_file_detected(self, sandbox):
        store = MdStore(sandbox.novels / "chg4", auto_git=False)
        store.write("settings/a.md", "A", metadata={"title": "A"})
        store.mark_synced(["settings/a.md"])
        (store.root / "settings" / "new.md").write_text("# 新的", encoding="utf-8")
        assert "settings/new.md" in store.detect_changed()


class TestSlugify:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("林墨", "林墨"),
            ("Alice", "Alice"),
            ("a b", "a-b"),
            ("a/b", "a-b"),
            ("  trim  ", "trim"),
            ("", "unnamed"),
            ("!!!", "unnamed"),
            ("角色A-B", "角色A-B"),
        ],
    )
    def test_slugify(self, raw, expected):
        assert slugify(raw) == expected

    def test_slugify_blocks_path_traversal(self):
        assert "/" not in slugify("../../etc/passwd")
        assert ".." not in slugify("../../etc/passwd")


class TestEnclosingRepoDetection:
    def test_book_inside_unignored_project_repo_reuses_outer(self, sandbox, tmp_path):
        """书目录位于外层仓库内且未被忽略 → 复用外层（不产生嵌套仓库）。"""
        import subprocess

        outer = tmp_path / "outer"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        book = outer / "data" / "novels" / "b1"
        store = open_store(book, writable=True)
        assert store._repo is not None
        assert Path(store._repo.working_tree_dir).resolve() == outer.resolve()
        assert not (book / ".git").exists()

    def test_book_inside_ignored_project_repo_gets_own_repo(self, sandbox, tmp_path):
        """外层仓库忽略 data/novels/ → 必须初始化独立仓库，写作历史才真正生效。"""
        import subprocess

        outer = tmp_path / "outer2"
        outer.mkdir()
        (outer / ".gitignore").write_text("data/novels/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        book = outer / "data" / "novels" / "b2"
        store = open_store(book, writable=True)
        assert (book / ".git").exists()
        assert Path(store._repo.working_tree_dir).resolve() == book.resolve()

    def test_outer_repo_scope_is_book_subdir(self, sandbox, tmp_path):
        """复用外层仓库时，必须把历史范围限定在本书子目录（scope 非空）。"""
        import subprocess

        outer = tmp_path / "outer3"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        book = outer / "novels" / "b3"
        store = open_store(book, writable=True)
        assert store._repo_scope == "novels/b3"

    def test_history_filters_out_other_books(self, sandbox, tmp_path):
        """核心回归：外层仓库场景下，本书历史不得混入其他书/其他项目的提交。"""
        import subprocess

        outer = tmp_path / "outer4"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        (outer / "unrelated.txt").write_text("项目文件", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=outer, check=True)
        subprocess.run(
            ["git", "-c", "user.name=T", "-c", "user.email=t@e", "commit", "-q", "-m", "unrelated"],
            cwd=outer,
            check=True,
        )

        book_a = outer / "novels" / "a"
        book_b = outer / "novels" / "b"
        open_store(book_a, writable=True).write("settings/outline.md", "A", commit_message="book-a")
        open_store(book_b, writable=True).write("settings/outline.md", "B", commit_message="book-b")

        hist_a = [c["message"] for c in open_store(book_a, writable=True).history()]
        assert any("book-a" in m for m in hist_a)
        assert not any("book-b" in m for m in hist_a), "本书历史混入了另一本书的提交"
        assert not any("unrelated" in m for m in hist_a), "本书历史混入了项目无关提交"

    def test_history_works_in_read_mode_for_outer_repo(self, sandbox, tmp_path):
        """此前只读路径在「外层仓库跟踪本书」时返回空历史 → 写作历史面板永久空白。"""
        import subprocess

        outer = tmp_path / "outer5"
        outer.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
        book = outer / "novels" / "readhist"
        open_store(book, writable=True).write("settings/outline.md", "内容", commit_message="写入一次")
        read_store = open_store(book, writable=False)
        assert read_store._repo is not None
        assert any("写入一次" in c["message"] for c in read_store.history())

    def test_repo_resolution_is_cached(self, sandbox, tmp_path):
        """性能回归：重复构造 MdStore 不得反复 spawn git 去解析仓库归属。"""
        from src.memory import md_store as md

        book = tmp_path / "cached-book"
        open_store(book, writable=True)
        key = book.resolve().as_posix()
        assert key in md._REPO_CACHE
        before = md._REPO_CACHE[key]
        for _ in range(5):
            open_store(book, writable=True)
        assert md._REPO_CACHE[key] is before
