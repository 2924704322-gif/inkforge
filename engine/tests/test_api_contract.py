"""API 契约与安全边界测试（70+ 端点的读路径 + 参数校验 + 路径穿越防护）。

本文件**不调用任何真实 LLM**：只覆盖读路径、CRUD、校验与错误码。
生成类端点（/api/start、/api/demo、/api/chats/{id}/send、/api/model-test）
需真实模型，由冒烟测试脚本在受控条件下单独验证。
"""

from __future__ import annotations

import pytest


class TestPingAndBooks:
    def test_ping_is_side_effect_free(self, app_client, sandbox):
        client, _ = app_client
        before = sorted(p.name for p in sandbox.novels.iterdir())
        res = client.get("/api/ping")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["service"] == "inkforge-engine"
        # 关键回归：探活不得创建任何目录（此前走 /api/books 会产生写副作用）
        assert sorted(p.name for p in sandbox.novels.iterdir()) == before

    def test_books_lists_seeded_book(self, app_client):
        client, _ = app_client
        res = client.get("/api/books")
        assert res.status_code == 200
        books = res.json()["books"]
        demo = next(b for b in books if b["novel_id"] == "demo-web")
        assert demo["is_default"] is True
        assert demo["chapters"] == 2
        assert demo["approved"] == 2

    def test_books_does_not_create_subdirs(self, app_client, sandbox):
        """回归 P1-5：列书架不得为损坏/空的书籍创建目录骨架。"""
        client, _ = app_client
        client.get("/api/books")
        assert not (sandbox.novels / "ghost-book").exists()

    def test_create_book_ok(self, app_client, sandbox):
        client, _ = app_client
        res = client.post("/api/books", json={"novel_id": "new-book"})
        assert res.status_code == 200
        assert res.json()["novel_id"] == "new-book"
        for sub in ("chapters", "settings", "summaries", "reviews"):
            assert (sandbox.novels / "new-book" / sub).is_dir()

    @pytest.mark.parametrize(
        "bad_id", ["../escape", "a/b", "a\\b", "", "-leading", "has space", "中文书名", "a..b"]
    )
    def test_create_book_rejects_illegal_id(self, app_client, bad_id):
        client, _ = app_client
        assert client.post("/api/books", json={"novel_id": bad_id}).status_code in (400, 422)

    def test_create_duplicate_book_conflicts(self, app_client):
        client, _ = app_client
        assert client.post("/api/books", json={"novel_id": "dup-book"}).status_code == 200
        assert client.post("/api/books", json={"novel_id": "dup-book"}).status_code == 409

    def test_delete_book_protects_default(self, app_client):
        """默认书不可删除（否则书架会留空壳）。"""
        client, _ = app_client
        assert client.delete("/api/books/demo-web").status_code == 409

    def test_delete_book_works_for_other(self, app_client, sandbox):
        client, _ = app_client
        client.post("/api/books", json={"novel_id": "doomed"})
        assert client.delete("/api/books/doomed").status_code == 200
        assert not (sandbox.novels / "doomed").exists()

    def test_delete_missing_book_404(self, app_client):
        client, _ = app_client
        assert client.delete("/api/books/never-existed").status_code == 404

    def test_illegal_novel_query_rejected(self, app_client):
        client, _ = app_client
        assert client.get("/api/chapters?novel=../evil").status_code == 400


class TestChapterEndpoints:
    def test_list_chapters(self, app_client):
        client, _ = app_client
        res = client.get("/api/chapters?novel=demo-web")
        assert res.status_code == 200
        items = res.json()["chapters"]
        assert [c["chapter"] for c in items] == [1, 2]

    def test_read_chapter_raw(self, app_client):
        client, _ = app_client
        res = client.get("/api/chapters/1/raw?novel=demo-web")
        assert res.status_code == 200
        assert res.json()["chapter"] == 1
        assert res.json()["status"] == "approved"

    def test_read_missing_chapter_404(self, app_client):
        client, _ = app_client
        assert client.get("/api/chapters/999/raw?novel=demo-web").status_code == 404

    def test_save_chapter_roundtrip(self, app_client):
        client, _ = app_client
        res = client.put("/api/chapters/1?novel=demo-web", json={"content": "改写后的正文"})
        assert res.status_code == 200
        assert client.get("/api/chapters/1/raw?novel=demo-web").json()["content"].strip() == "改写后的正文"

    def test_save_preserves_frontmatter(self, app_client):
        client, _ = app_client
        client.put("/api/chapters/2?novel=demo-web", json={"content": "新正文"})
        doc = client.get("/api/chapters/2/raw?novel=demo-web").json()
        assert doc["title"] == "第2章"
        assert doc["status"] == "approved"

    def test_create_chapter_appends_next_number(self, app_client):
        client, _ = app_client
        res = client.post("/api/chapters?novel=demo-web", json={"title": "新的章节"})
        assert res.status_code == 200
        assert res.json()["chapter"] == 3

    def test_delete_chapter(self, app_client):
        client, _ = app_client
        assert client.delete("/api/chapters/2?novel=demo-web").status_code == 200
        assert client.get("/api/chapters/2/raw?novel=demo-web").status_code == 404

    def test_delete_missing_chapter_404(self, app_client):
        client, _ = app_client
        assert client.delete("/api/chapters/77?novel=demo-web").status_code == 404


class TestSettingsEndpoints:
    def test_tree_lists_settings_docs(self, app_client, sandbox, seed_book):
        client, _ = app_client
        seed_book(sandbox.novels / "book2", title="B2")
        (sandbox.novels / "book2" / "settings" / "worldview").mkdir(exist_ok=True)
        (sandbox.novels / "book2" / "settings" / "worldview" / "w.md").write_text(
            "---\ntitle: 世界\n---\n内容", encoding="utf-8"
        )
        res = client.get("/api/settings/tree?novel=book2")
        assert res.status_code == 200
        rels = [i["rel"] for i in res.json()["items"]]
        assert "settings/worldview/w.md" in rels

    def test_get_doc(self, app_client):
        client, _ = app_client
        res = client.get("/api/settings/doc?novel=demo-web&rel=settings/outline.md")
        assert res.status_code == 200
        assert res.json()["title"] == "样例书"

    def test_get_doc_missing_404(self, app_client):
        client, _ = app_client
        assert client.get("/api/settings/doc?novel=demo-web&rel=settings/nope.md").status_code == 404

    @pytest.mark.parametrize(
        "rel",
        [
            "../secrets.md",
            "settings/../../etc/passwd.md",
            "/etc/passwd.md",
            "settings/ok.md/../../../x.md",
            "chapters/ch-001.md",
            "settings/x.txt",
        ],
    )
    def test_doc_path_whitelist(self, app_client, rel):
        """仅允许 settings/**.md —— 相对路径穿越与越界目录一律 400。"""
        client, _ = app_client
        res = client.get(f"/api/settings/doc?novel=demo-web&rel={rel}")
        assert res.status_code in (400, 404)
        assert res.status_code != 200

    def test_save_doc_and_reload(self, app_client):
        client, _ = app_client
        res = client.put(
            "/api/settings/doc?novel=demo-web",
            json={"rel": "settings/style.md", "content": "短句、冷硬。"},
        )
        assert res.status_code == 200
        assert (
            client.get("/api/settings/doc?novel=demo-web&rel=settings/style.md").json()["content"]
            == "短句、冷硬。"
        )

    def test_save_doc_rejects_illegal_rel(self, app_client):
        client, _ = app_client
        res = client.put(
            "/api/settings/doc?novel=demo-web",
            json={"rel": "../escape.md", "content": "x"},
        )
        assert res.status_code == 400


class TestStatusAndQueue:
    def test_status_snapshot(self, app_client):
        client, _ = app_client
        res = client.get("/api/status?novel=demo-web")
        assert res.status_code == 200
        body = res.json()
        assert body["novel_id"] == "demo-web"
        assert body["started"] is False
        assert body["metrics"]["approved"] == 2

    def test_pending_equals_status(self, app_client):
        client, _ = app_client
        assert client.get("/api/pending?novel=demo-web").json()["novel_id"] == "demo-web"

    def test_queue_lists_drafts_only(self, app_client):
        client, _ = app_client
        client.post("/api/chapters?novel=demo-web", json={"title": "草稿章"})
        queue = client.get("/api/queue?novel=demo-web").json()["queue"]
        assert [q["chapter"] for q in queue] == [3]

    def test_decision_without_pending_returns_409(self, app_client):
        client, _ = app_client
        res = client.post("/api/decision?novel=demo-web", json={"action": "approve"})
        assert res.status_code == 409

    def test_resume_without_checkpoint_returns_409(self, app_client):
        client, _ = app_client
        res = client.post("/api/resume?novel=demo-web", json={})
        assert res.status_code == 409

    def test_demo_status_idle(self, app_client):
        client, _ = app_client
        body = client.get("/api/demo?novel=demo-web").json()
        assert body["status"] == "idle"

    def test_interactive_state_idle(self, app_client):
        client, _ = app_client
        body = client.get("/api/interactive/state?novel=demo-web").json()
        assert body["status"] in ("idle", "awaiting_review")

    def test_cross_volume_empty(self, app_client):
        client, _ = app_client
        assert client.get("/api/cross-volume?novel=demo-web").status_code == 200


class TestDashboardBindingsHistory:
    def test_dashboard_metrics(self, app_client):
        client, _ = app_client
        body = client.get("/api/dashboard?novel=demo-web").json()
        assert body["metrics"]["total_chapters"] == 2
        assert body["metrics"]["approved"] == 2

    def test_bindings_empty_then_set(self, app_client):
        client, _ = app_client
        assert client.get("/api/bindings?novel=demo-web").json()["bound_custom"] == []
        res = client.post("/api/bindings?novel=demo-web", json={"custom_skill_ids": [], "pack_ids": []})
        assert res.status_code == 200

    def test_bindings_missing_custom_skill_404(self, app_client):
        client, _ = app_client
        res = client.post(
            "/api/bindings?novel=demo-web", json={"custom_skill_ids": ["no-such-skill"], "pack_ids": []}
        )
        assert res.status_code == 404

    def test_history_creates_and_reports_commits(self, app_client):
        """S1-3：写作历史真正生效——一次写入即产生 Git 提交。"""
        client, _ = app_client
        empty = client.get("/api/history?novel=demo-web").json()
        assert empty["enabled"] is True
        client.put("/api/chapters/1?novel=demo-web", json={"content": "触发一次提交"})
        body = client.get("/api/history?novel=demo-web").json()
        assert body["commits"], "写入后应至少有一条提交记录"
        assert any(c["message"].startswith("[novel]") for c in body["commits"])

    def test_history_disabled_by_env(self, sandbox, monkeypatch):
        from fastapi.testclient import TestClient

        from src.memory.store_factory import GIT_HISTORY_ENV
        from src.web import server as server_mod

        monkeypatch.setenv(GIT_HISTORY_ENV, "0")
        with TestClient(server_mod.create_app("demo-web")) as client:
            body = client.get("/api/history?novel=demo-web").json()
        assert body["enabled"] is False
        assert body["commits"] == []


class TestCustomSkillsAndModelConfig:
    def test_custom_skill_crud(self, app_client):
        client, _ = app_client
        assert client.get("/api/custom-skills").json()["skills"] == []
        created = client.post(
            "/api/custom-skills", json={"title": "冷硬文风", "content": "禁止形容词堆砌。"}
        )
        assert created.status_code == 200
        sid = created.json()["skill_id"]
        assert len(client.get("/api/custom-skills").json()["skills"]) == 1

        updated = client.put(f"/api/custom-skills/{sid}", json={"title": "冷硬文风", "content": "改。"})
        assert updated.status_code == 200
        assert client.delete(f"/api/custom-skills/{sid}").status_code == 200
        assert client.delete(f"/api/custom-skills/{sid}").status_code == 404

    def test_custom_skill_requires_title_and_content(self, app_client):
        client, _ = app_client
        assert client.post("/api/custom-skills", json={"title": "", "content": "x"}).status_code == 400
        assert client.post("/api/custom-skills", json={"title": "t", "content": "  "}).status_code == 400

    def test_model_config_masks_api_key(self, app_client):
        client, _ = app_client
        body = client.get("/api/model-config").json()
        assert body["providers"], "应至少有一个接入点"
        for provider in body["providers"]:
            key = provider["api_key"]
            # 占位符或掩码，绝不返回明文
            assert key.startswith("${") or "****" in key

    def test_model_config_role_update_writes_temp_config(self, app_client, sandbox):
        client, _ = app_client
        res = client.put(
            "/api/model-config/roles/writer",
            json={"provider": "deepseek", "model": "deepseek-reasoner", "temperature": 0.5},
        )
        assert res.status_code == 200
        content = (sandbox.configs / "models.yaml").read_text(encoding="utf-8")
        assert "deepseek-reasoner" in content

    def test_model_config_unknown_role_404(self, app_client):
        client, _ = app_client
        res = client.put(
            "/api/model-config/roles/nope",
            json={"provider": "deepseek", "model": "m", "temperature": 0.5},
        )
        assert res.status_code == 404

    def test_model_config_rejects_undefined_provider(self, app_client):
        client, _ = app_client
        res = client.put(
            "/api/model-config/roles/writer",
            json={"provider": "ghost-provider", "model": "m", "temperature": 0.5},
        )
        assert res.status_code == 400

    def test_agent_presets_listed(self, app_client):
        client, _ = app_client
        presets = client.get("/api/agent-presets").json()["presets"]
        keys = {p["key"] for p in presets}
        assert {"master", "character", "plot", "outline", "prose", "review"} <= keys

    def test_agent_preset_override_and_reset(self, app_client):
        client, _ = app_client
        assert client.put("/api/agent-presets/plot", json={"prompt": "自定义"}).status_code == 200
        item = next(p for p in client.get("/api/agent-presets").json()["presets"] if p["key"] == "plot")
        assert item["custom"] is True and item["prompt"] == "自定义"
        assert client.delete("/api/agent-presets/plot").status_code == 200
        item = next(p for p in client.get("/api/agent-presets").json()["presets"] if p["key"] == "plot")
        assert item["custom"] is False

    def test_agent_preset_unknown_key_404(self, app_client):
        client, _ = app_client
        assert client.put("/api/agent-presets/nope", json={"prompt": "x"}).status_code == 404


class TestMaterialsAndLearning:
    def test_material_crud(self, app_client):
        client, _ = app_client
        assert client.get("/api/materials").json()["materials"] == []
        created = client.post("/api/materials", json={"title": "参考", "content": "内容"})
        assert created.status_code == 200
        mid = created.json()["id"]
        assert len(client.get("/api/materials").json()["materials"]) == 1
        assert client.delete(f"/api/materials/{mid}").status_code == 200

    def test_material_requires_fields(self, app_client):
        client, _ = app_client
        assert client.post("/api/materials", json={"title": "", "content": "x"}).status_code == 400

    def test_material_bad_id_rejected(self, app_client):
        client, _ = app_client
        assert client.delete("/api/materials/../../etc").status_code in (400, 404)

    def test_learning_requires_min_length(self, app_client):
        client, _ = app_client
        assert client.post("/api/learning", json={"title": "t", "sample": "太短"}).status_code == 400

    def test_learning_list_empty(self, app_client):
        client, _ = app_client
        assert client.get("/api/learning").json()["items"] == []


class TestChats:
    def test_chat_crud_without_llm(self, app_client):
        client, _ = app_client
        assert client.get("/api/chats?novel=demo-web").json()["chats"] == []
        created = client.post("/api/chats?novel=demo-web", json={"agent": "master"})
        assert created.status_code == 200
        cid = created.json()["chat"]["id"]
        fetched = client.get(f"/api/chats/{cid}?novel=demo-web")
        assert fetched.status_code == 200
        assert fetched.json()["messages"] == []
        assert len(client.get("/api/chats?novel=demo-web").json()["chats"]) == 1
        assert client.delete(f"/api/chats/{cid}?novel=demo-web").status_code == 200

    def test_chat_unknown_agent_rejected(self, app_client):
        client, _ = app_client
        assert client.post("/api/chats?novel=demo-web", json={"agent": "evil"}).status_code == 400

    def test_chat_missing_404(self, app_client):
        client, _ = app_client
        assert client.get("/api/chats/c-nonexistent?novel=demo-web").status_code == 404

    def test_chat_cid_path_traversal_blocked(self, app_client):
        client, _ = app_client
        res = client.get("/api/chats/..%2F..%2Fconfig?novel=demo-web")
        assert res.status_code in (404, 400)

    def test_proposals_list_empty(self, app_client):
        client, _ = app_client
        created = client.post("/api/chats?novel=demo-web", json={"agent": "master"}).json()
        cid = created["chat"]["id"]
        assert client.get(f"/api/chats/{cid}/proposals?novel=demo-web").json()["proposals"] == []

    def test_proposal_decide_missing_404(self, app_client):
        client, _ = app_client
        created = client.post("/api/chats?novel=demo-web", json={"agent": "master"}).json()
        cid = created["chat"]["id"]
        res = client.post(f"/api/chats/{cid}/proposals/p-nope/decide?novel=demo-web", json={"decision": "accept"})
        assert res.status_code == 404

    def test_empty_message_rejected(self, app_client):
        client, _ = app_client
        created = client.post("/api/chats?novel=demo-web", json={"agent": "master"}).json()
        cid = created["chat"]["id"]
        res = client.post(f"/api/chats/{cid}/send?novel=demo-web", json={"message": "   "})
        assert res.status_code == 400


class TestDistillGuards:
    def test_init_rejects_relative_path(self, app_client):
        client, _ = app_client
        res = client.post("/api/distill/init", json={"file_path": "book.txt"})
        assert res.status_code == 400

    def test_init_rejects_non_book_file(self, app_client, tmp_path):
        client, _ = app_client
        target = tmp_path / "secret.key"
        target.write_text("secret", encoding="utf-8")
        assert client.post("/api/distill/init", json={"file_path": str(target)}).status_code == 400

    def test_init_accepts_authorized_txt(self, app_client, tmp_path):
        client, _ = app_client
        target = tmp_path / "sample-book.txt"
        target.write_text("第一章 开端\n" + "正文内容。" * 200, encoding="utf-8")
        res = client.post("/api/distill/init", json={"file_path": str(target), "skill_id": "t-book_v1"})
        assert res.status_code == 200

    def test_skill_list_and_export_missing(self, app_client):
        client, _ = app_client
        assert client.get("/api/skills/list").status_code == 200
        assert client.get("/api/skills/export/does-not-exist").status_code == 404

    def test_distill_start_unknown_skill_404(self, app_client):
        client, _ = app_client
        assert client.post("/api/distill/start", json={"skill_id": "ghost"}).status_code in (400, 404)
