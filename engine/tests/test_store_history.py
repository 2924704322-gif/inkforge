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
