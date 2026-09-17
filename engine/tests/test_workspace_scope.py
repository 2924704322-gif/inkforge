"""P0 基础设施测试：作用域解析 / 书库服务层 / 工作区索引与上下文。

本文件是墨师全域化改造的底座契约：
- 作用域哨兵值必须与既有 novel 语义**互不干扰**（缺省仍是默认书）；
- 书库服务层与 HTTP 端点在行为上必须一致（同一实现，避免漂移）；
- 工作区索引是"索引而不是全文"，且带 TTL 缓存与失效接口。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services import library, workspace
from src.web.scope import WORKSPACE, chats_dir, is_workspace, resolve_book, workspace_dir

# ---------- 作用域 ----------

def test_scope_sentinel_and_default_semantics():
    """工作区哨兵值不改动既有缺省语义。"""
    assert WORKSPACE == "__workspace__"
    assert is_workspace(WORKSPACE) is True
    assert is_workspace("") is False
    assert is_workspace("demo-web") is False
    # 工作区 → 不隶属任何书（返回空串，绝不静默落到默认书）
    assert resolve_book(WORKSPACE, "demo-web") == ""
    # 既有语义保持：空 → 默认书；指定 → 指定书
    assert resolve_book("", "demo-web") == "demo-web"
    assert resolve_book("other", "demo-web") == "other"
    assert resolve_book(None, "demo-web") == "demo-web"


def test_scope_sentinel_passes_existing_id_validation():
    """哨兵值必须天然通过既有书名校验（否则端点在 400 之前就被拒）。"""
    assert library.NOVEL_ID_RE.match(WORKSPACE)


def test_workspace_dir_is_sibling_of_novels(sandbox: SimpleNamespace):
    assert workspace_dir(sandbox.novels) == sandbox.novels.parent / "workspace"
    d = chats_dir(sandbox.novels.parent / "workspace")
    assert d.is_dir() and d.name == "chats"


# ---------- 书库服务层 ----------

def test_create_book_and_list(sandbox: SimpleNamespace):
    nid = library.create_book("alpha", "interactive")
    assert nid == "alpha"
    root = sandbox.novels / "alpha"
    for sub in ("chapters", "settings", "summaries", "reviews", "interactive"):
        assert (root / sub).is_dir()

    items = library.list_books(default_novel="demo-web", active=set(), done=set())
    by_id = {b["novel_id"]: b for b in items}
    assert set(by_id) == {"alpha", "demo-web"}
    assert by_id["alpha"]["interactive"] is True
    assert by_id["demo-web"]["is_default"] is True
    assert by_id["demo-web"]["active"] is False


def test_create_book_rejects_illegal_and_duplicate(sandbox: SimpleNamespace):
    with pytest.raises(library.LibraryError) as bad:
        library.create_book("../evil", "pipeline")
    assert bad.value.code == "bad_request"

    with pytest.raises(library.LibraryError) as bad_mode:
        library.create_book("ok-book", "sandbox")
    assert bad_mode.value.code == "bad_request"

    library.create_book("dup", "pipeline")
    with pytest.raises(library.LibraryError) as dup:
        library.create_book("dup", "pipeline")
    assert dup.value.code == "conflict"


def test_ensure_book_skeleton_is_idempotent(sandbox: SimpleNamespace):
    """骨架补齐不覆盖既有文件（既有书不能被"顺手重建"）。"""
    library.create_book("keep", "pipeline")
    marker = sandbox.novels / "keep" / "settings" / "outline.md"
    marker.write_text("# 人工改过的大纲", encoding="utf-8")
    library.ensure_book_skeleton("keep")
    assert marker.read_text(encoding="utf-8") == "# 人工改过的大纲"


def test_delete_book_protections(sandbox: SimpleNamespace):
    library.create_book("doomed", "pipeline")
    # 默认书不可删
    with pytest.raises(library.LibraryError) as default_guard:
        library.delete_book("doomed", default_novel="doomed")
    assert default_guard.value.code == "conflict"
    # 生成中不可删
    with pytest.raises(library.LibraryError) as active_guard:
        library.delete_book("doomed", default_novel="demo-web", active={"doomed"})
    assert active_guard.value.code == "conflict"
    # 不存在 404 语义
    with pytest.raises(library.LibraryError) as missing:
        library.delete_book("ghost", default_novel="demo-web")
    assert missing.value.code == "not_found"
    # 正常删除
    removed: list[str] = []
    library.delete_book("doomed", default_novel="demo-web", active=set(),
                        remove_session=removed.append)
    assert removed == ["doomed"]
    assert not (sandbox.novels / "doomed").exists()


def test_apply_skills_to_store_uses_single_writer(tmp_path):
    """自定义约束写入走 memory_manager 同源的相对路径（唯一事实源）。"""
    from src.memory.md_store import MdStore

    (tmp_path / "custom_skills").mkdir()
    import frontmatter

    (tmp_path / "custom_skills" / "sk-1.md").write_text(
        frontmatter.dumps(frontmatter.Post("约束正文", title="约束A")), encoding="utf-8"
    )
    import os

    os.environ["PROJECT_ROOT"] = str(tmp_path)
    os.environ["NOVELS_DIR"] = str(tmp_path / "novels")
    from src.config import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    try:
        store = MdStore(tmp_path / "novels" / "b1", auto_git=False)
        titles = library.apply_skills_to_store(store, ["sk-1"])
        assert titles == ["约束A"]
        assert store.exists("settings/custom-skills.md")
        assert "约束正文" in store.read("settings/custom-skills.md").content
        # 未选任何 Skill 时不得触碰既有文件
        store.write("settings/custom-skills.md", "既有约束", commit_message="seed")
        assert library.apply_skills_to_store(store, []) == []
        assert store.read("settings/custom-skills.md").content.strip() == "既有约束"
    finally:
        settings_mod.get_settings.cache_clear()


# ---------- 工作区索引与上下文 ----------

def test_workspace_index_lists_books_and_resources(sandbox: SimpleNamespace):
    library.create_book("alpha", "pipeline")
    idx = workspace.workspace_index(default_novel="demo-web", use_cache=False)
    ids = [b["novel_id"] for b in idx.books]
    assert "alpha" in ids and "demo-web" in ids
    assert set(idx.resources) >= {"materials", "custom_skills", "skill_packs", "model_roles"}
    assert idx.error == ""


def test_workspace_context_budget_and_rules(sandbox: SimpleNamespace):
    library.create_book("alpha", "pipeline")
    idx = workspace.workspace_index(default_novel="demo-web", use_cache=False)
    text = workspace.build_workspace_context(idx)
    assert "【工作区】" in text and "【书目清单】" in text and "【全局资源】" in text
    assert "必须" in text          # 写动作需确认的硬规则
    assert "alpha" in text
    assert len(text) <= workspace.WORKSPACE_BUDGET


def test_workspace_index_cache_invalidation(sandbox: SimpleNamespace):
    """TTL 缓存命中时返回同一对象；invalidate 后重新读盘（建书后立即可见）。"""
    first = workspace.workspace_index(default_novel="demo-web", use_cache=True)
    cached = workspace.workspace_index(default_novel="demo-web", use_cache=True)
    assert cached is first
    assert cached is first

    library.create_book("fresh", "pipeline")
    workspace.invalidate()
    again = workspace.workspace_index(default_novel="demo-web", use_cache=True)
    assert again is not first
    assert "fresh" in [b["novel_id"] for b in again.books]
