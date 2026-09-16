"""P1 对话全域化测试：工作区会话 / 工作区上下文 / 动作说明注入 / 当前书目。

决策基线（用户确认）：
- 首次启动不弹书架 → 工作区必须"不建书也能对话"；
- 所有写动作需确认 → 墨师必须**拿到**动作清单（否则无法发起确认）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from src.agents.action_prompt import actions_block_for, master_actions_block
from src.web.scope import WORKSPACE


# ---------- 动作说明注入 ----------

def test_master_gets_action_block_others_do_not():
    """动作清单只给具备动作能力的智能体，且与 CONSTITUTION 同级强制追加。"""
    from src.web.inkforge_windows import agent_prompt

    master = agent_prompt("master")
    assert "【工作台动作清单" in master
    assert "第零条" in master                     # 刚性指令仍在
    for sub in ("character", "plot", "outline", "prose", "review"):
        assert "【工作台动作清单" not in agent_prompt(sub), sub


def test_action_block_survives_user_override(sandbox: SimpleNamespace):
    """用户自定义墨师提示词时，动作清单不得被覆盖掉（改动前会丢）。"""
    from src.web import inkforge_windows as win

    overrides = win._overrides_path()
    overrides.parent.mkdir(parents=True, exist_ok=True)
    overrides.write_text(
        json.dumps({"master": {"prompt": "我是被自定义过的墨师。"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        prompt = win.agent_prompt("master")
        assert prompt.startswith("我是被自定义过的墨师。")
        assert "【工作台动作清单" in prompt
    finally:
        overrides.unlink(missing_ok=True)


def test_action_prompt_files_are_not_in_template_dir():
    """动作说明必须放在 prompts/ 之外，否则启动期模板校验会 Fail-Fast。"""
    from src.agents.prompt_loader import PROMPTS_DIR

    assert not (PROMPTS_DIR / "master_actions.md").exists()
    assert "book_create" in master_actions_block()
    assert actions_block_for("prose") == ""


# ---------- 工作区会话 ----------

def test_workspace_chat_lives_outside_any_book(app_client, sandbox: SimpleNamespace):
    """工作区会话落盘在 data/workspace/chats/，不进任何书的目录。"""
    client, _ = app_client
    res = client.post(f"/api/chats?novel={WORKSPACE}", json={"agent": "master"})
    assert res.status_code == 200
    chat = res.json()["chat"]
    assert chat["scope"] == "workspace"
    assert chat["novel"] == ""

    ws_dir = sandbox.novels.parent / "workspace" / "chats"
    assert (ws_dir / f"{chat['id']}.json").exists()
    assert not (sandbox.novels / "demo-web" / "chats").exists()

    listed = client.get(f"/api/chats?novel={WORKSPACE}").json()["chats"]
    assert [c["id"] for c in listed] == [chat["id"]]
    assert listed[0]["scope"] == "workspace"
    # 书内会话列表看不到工作区会话（作用域隔离）
    assert client.get("/api/chats?novel=demo-web").json()["chats"] == []


def test_book_chat_keeps_legacy_shape(app_client):
    """书内会话行为与改造前一致（缺省 scope=book，且带书标识）。"""
    client, _ = app_client
    chat = client.post("/api/chats?novel=demo-web", json={"agent": "master"}).json()["chat"]
    assert chat["scope"] == "book"
    assert chat["novel"] == "demo-web"


def test_workspace_context_mentions_books_and_budget():
    """工作区上下文：书目清单 + 全局资源 + 写动作需确认的硬规则。"""
    from src.web.inkforge_api import _make_workspace_context

    ctx = _make_workspace_context("demo-web", {"demo-web"}, set(), "")
    assert "【工作区】" in ctx and "【书目清单】" in ctx
    assert "demo-web" in ctx
    assert "必须" in ctx


# ---------- 工作台当前书目 ----------

def test_book_select_roundtrip(app_client):
    client, _ = app_client
    assert client.get("/api/book-select").json()["novel_id"] == ""

    ok = client.put("/api/book-select", json={"novel_id": "demo-web"})
    assert ok.status_code == 200 and ok.json()["exists"] is True
    assert client.get("/api/book-select").json()["novel_id"] == "demo-web"

    # 回到工作区
    assert client.put("/api/book-select", json={"novel_id": ""}).json()["novel_id"] == ""
    assert client.put("/api/book-select", json={"novel_id": "../evil"}).status_code == 400


def test_active_book_used_when_novel_omitted(app_client):
    """未显式指定 novel 时，会话落到"工作台当前书目"（而非写死的默认书）。"""
    client, _ = app_client
    client.put("/api/book-select", json={"novel_id": "demo-web"})
    chat = client.post("/api/chats", json={"agent": "master"}).json()["chat"]
    assert chat["novel"] == "demo-web"
    assert chat["scope"] == "book"
