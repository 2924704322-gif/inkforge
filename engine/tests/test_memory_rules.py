"""记忆层领域规则回归测试（本项目最贵的逻辑，必须有不变量护栏）。

覆盖：
- 伏笔台账四条脏数据防御规则（memory_manager._apply_foreshadow_ops）
- 实体状态板「只追加 + 去重」（apply_state_ops）
- RAG 四维上下文组装：反未来剧透、临期伏笔主动召回、完本收尾扫描
  （memory_manager.retrieve_context）

这些用例断言的是**不变量**（不产生脏数据 / 不泄露未来剧情），
不断言具体提示词文本或评分数值，以免锁死后续调优空间。
"""

from __future__ import annotations

import pytest


class TestForeshadowingPlant:
    def test_plant_creates_open_item(self, make_memory):
        store, memory = make_memory("fs-plant")
        memory._apply_foreshadow_ops(
            3, [{"action": "plant", "desc": "神秘信物", "resolve_ch": 6}], total_chapters=10
        )
        items = store.read("settings/foreshadowing.md").metadata["items"]
        assert len(items) == 1
        assert items[0]["desc"] == "神秘信物"
        assert items[0]["planted_ch"] == 3
        assert items[0]["resolve_ch"] == 6
        assert items[0]["status"] == "open"
        assert items[0]["id"] == "f001"

    def test_plant_assigns_incremental_ids(self, make_memory):
        store, memory = make_memory("fs-ids")
        memory._apply_foreshadow_ops(1, [{"action": "plant", "desc": "A", "resolve_ch": 4}], 10)
        memory._apply_foreshadow_ops(2, [{"action": "plant", "desc": "B", "resolve_ch": 5}], 10)
        ids = [i["id"] for i in store.read("settings/foreshadowing.md").metadata["items"]]
        assert ids == ["f001", "f002"]

    def test_duplicate_desc_is_skipped(self, make_memory):
        """Summarizer 常把既有伏笔当新伏笔重种 → 必须去重，否则回收率分母被稀释。"""
        store, memory = make_memory("fs-dup")
        memory._apply_foreshadow_ops(1, [{"action": "plant", "desc": "同一伏笔", "resolve_ch": 4}], 10)
        memory._apply_foreshadow_ops(2, [{"action": "plant", "desc": "同一伏笔", "resolve_ch": 4}], 10)
        assert len(store.read("settings/foreshadowing.md").metadata["items"]) == 1

    def test_empty_desc_is_ignored(self, make_memory):
        store, memory = make_memory("fs-empty")
        memory._apply_foreshadow_ops(1, [{"action": "plant", "desc": "   ", "resolve_ch": 4}], 10)
        assert "items" not in store.read("settings/foreshadowing.md").metadata or not store.read(
            "settings/foreshadowing.md"
        ).metadata.get("items")

    def test_missing_resolve_ch_defaults_to_plus_three(self, make_memory):
        store, memory = make_memory("fs-default")
        memory._apply_foreshadow_ops(5, [{"action": "plant", "desc": "无回收章"}], 20)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["resolve_ch"] == 8

    def test_resolve_ch_not_later_than_plant_ch(self, make_memory):
        """resolve_ch 早于或等于埋设章属非法 → 钳制为 planted+3。"""
        store, memory = make_memory("fs-back")
        memory._apply_foreshadow_ops(5, [{"action": "plant", "desc": "回溯", "resolve_ch": 3}], 20)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["resolve_ch"] == 8

    def test_resolve_ch_clamped_to_total_chapters(self, make_memory):
        store, memory = make_memory("fs-clamp")
        memory._apply_foreshadow_ops(8, [{"action": "plant", "desc": "超界", "resolve_ch": 99}], 10)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["resolve_ch"] == 10

    def test_final_window_refuses_new_foreshadowing(self, make_memory):
        """末章窗口内新种伏笔已无回收空间 → 拒绝，避免稀释回收率分母。"""
        store, memory = make_memory("fs-final")
        memory._apply_foreshadow_ops(9, [{"action": "plant", "desc": "最后一刻"}], 10)
        assert not store.read("settings/foreshadowing.md").metadata.get("items")

    def test_second_to_last_chapter_also_refused(self, make_memory):
        store, memory = make_memory("fs-final2")
        memory._apply_foreshadow_ops(9, [{"action": "plant", "desc": "X"}], 10)
        memory._apply_foreshadow_ops(8, [{"action": "plant", "desc": "Y"}], 10)
        # total=10 时 chapter>=9 才属末窗，第 8 章应允许
        items = store.read("settings/foreshadowing.md").metadata.get("items") or []
        assert [i["desc"] for i in items] == ["Y"]

    def test_plant_without_total_chapters_allowed(self, make_memory):
        store, memory = make_memory("fs-nototal")
        memory._apply_foreshadow_ops(50, [{"action": "plant", "desc": "长线", "resolve_ch": 52}], 0)
        assert len(store.read("settings/foreshadowing.md").metadata["items"]) == 1


class TestForeshadowingResolve:
    def _seed(self, memory, chapter=1):
        memory._apply_foreshadow_ops(
            chapter, [{"action": "plant", "desc": "线A", "resolve_ch": 5, "id": "fA"}], 10
        )

    def test_valid_resolve(self, make_memory):
        store, memory = make_memory("fr-ok")
        self._seed(memory)
        memory._apply_foreshadow_ops(3, [{"action": "resolve", "id": "fA"}], 10)
        item = store.read("settings/foreshadowing.md").metadata["items"][0]
        assert item["status"] == "resolved"
        assert item["resolved_ch"] == 3

    def test_resolve_unknown_id_is_noop(self, make_memory):
        store, memory = make_memory("fr-unknown")
        self._seed(memory)
        memory._apply_foreshadow_ops(3, [{"action": "resolve", "id": "fZZZ"}], 10)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["status"] == "open"

    def test_double_resolve_keeps_first_chapter(self, make_memory):
        store, memory = make_memory("fr-twice")
        self._seed(memory)
        memory._apply_foreshadow_ops(3, [{"action": "resolve", "id": "fA"}], 10)
        memory._apply_foreshadow_ops(7, [{"action": "resolve", "id": "fA"}], 10)
        item = store.read("settings/foreshadowing.md").metadata["items"][0]
        assert item["resolved_ch"] == 3

    def test_illegal_resolve_before_plant_is_rejected(self, make_memory):
        """核心防御：拒绝「在第 2 章回收第 5 章才埋的伏笔」→ 防 resolved_ch < planted_ch。"""
        store, memory = make_memory("fr-illegal")
        self._seed(memory, chapter=5)
        memory._apply_foreshadow_ops(2, [{"action": "resolve", "id": "fA"}], 10)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["status"] == "open"

    def test_resolve_at_plant_chapter_is_legal(self, make_memory):
        store, memory = make_memory("fr-same")
        self._seed(memory, chapter=5)
        memory._apply_foreshadow_ops(5, [{"action": "resolve", "id": "fA"}], 10)
        assert store.read("settings/foreshadowing.md").metadata["items"][0]["status"] == "resolved"

    def test_mixed_ops_in_one_batch(self, make_memory):
        store, memory = make_memory("fr-mixed")
        self._seed(memory)
        memory._apply_foreshadow_ops(
            4,
            [{"action": "resolve", "id": "fA"}, {"action": "plant", "desc": "线B", "resolve_ch": 8}],
            10,
        )
        items = store.read("settings/foreshadowing.md").metadata["items"]
        assert items[0]["status"] == "resolved"
        assert items[1]["desc"] == "线B"


class TestForeshadowingStats:
    def test_empty_book_stats(self, make_memory):
        _, memory = make_memory("fstat-none")
        assert memory.foreshadowing_stats() == {"total": 0, "resolved": 0, "rate": None}

    def test_rate_computation(self, make_memory):
        _, memory = make_memory("fstat-rate")
        memory._apply_foreshadow_ops(
            1,
            [
                {"action": "plant", "desc": "A", "resolve_ch": 4, "id": "f1"},
                {"action": "plant", "desc": "B", "resolve_ch": 4, "id": "f2"},
            ],
            10,
        )
        memory._apply_foreshadow_ops(2, [{"action": "resolve", "id": "f1"}], 10)
        stats = memory.foreshadowing_stats()
        assert stats["total"] == 2
        assert stats["resolved"] == 1
        assert stats["rate"] == 0.5


class TestStateBoard:
    def test_append_facts(self, make_memory):
        store, memory = make_memory("sb-append")
        memory.apply_state_ops(2, [{"entity": "林墨", "fact": "获得源质能力"}])
        items = store.read("settings/state-board.md").metadata["items"]
        assert items == [{"entity": "林墨", "fact": "获得源质能力", "chapter": 2}]

    def test_duplicate_entity_fact_skipped(self, make_memory):
        store, memory = make_memory("sb-dup")
        memory.apply_state_ops(1, [{"entity": "A", "fact": "F"}])
        memory.apply_state_ops(3, [{"entity": "A", "fact": "F"}])
        assert len(store.read("settings/state-board.md").metadata["items"]) == 1

    def test_same_fact_different_entity_kept(self, make_memory):
        store, memory = make_memory("sb-entity")
        memory.apply_state_ops(1, [{"entity": "A", "fact": "死亡"}, {"entity": "B", "fact": "死亡"}])
        assert len(store.read("settings/state-board.md").metadata["items"]) == 2

    def test_empty_ops_do_not_write(self, make_memory):
        store, memory = make_memory("sb-noop")
        memory.apply_state_ops(1, [{"entity": "", "fact": ""}])
        assert not store.exists("settings/state-board.md")

    def test_facts_are_append_only(self, make_memory):
        """状态演进（被捕→越狱）以新条目记录，保留时间线，不覆盖历史事实。"""
        store, memory = make_memory("sb-appendonly")
        memory.apply_state_ops(1, [{"entity": "A", "fact": "被捕"}])
        memory.apply_state_ops(5, [{"entity": "A", "fact": "越狱"}])
        items = store.read("settings/state-board.md").metadata["items"]
        assert [(i["fact"], i["chapter"]) for i in items] == [("被捕", 1), ("越狱", 5)]


class TestRetrieveContext:
    def _book(self, store, memory):
        store.write(
            "settings/worldview/world.md",
            "# 世界观\n源质是一切力量的来源。",
            metadata={"title": "世界观"},
        )
        store.write(
            "settings/characters/林墨.md", "# 林墨\n主角。", metadata={"title": "林墨", "role": "主角"}
        )
        return store

    def test_recent_summaries_window_and_grading(self, make_memory):
        store, memory = make_memory("ctx-recent")
        for ch in range(1, 8):
            store.write(
                f"summaries/ch-{ch:03d}.summary.md",
                f"第{ch}章摘要内容" * 20,
                metadata={"chapter": ch, "volume": 1},
            )
        ctx = memory.retrieve_context(chapter=8, chapter_outline="q", characters=[])
        # 只用近 5 章
        assert len(ctx.recent_summaries) == 5
        assert "第3章摘要" in ctx.recent_summaries[0]
        # 近 2 章用 500 字档，更远用 100 字档
        assert max(len(s) for s in ctx.recent_summaries) > min(len(s) for s in ctx.recent_summaries)

    def test_character_state_loaded_from_file_not_vector(self, make_memory):
        store, memory = make_memory("ctx-char")
        self._book(store, memory)
        ctx = memory.retrieve_context(chapter=1, chapter_outline="q", characters=["林墨", "路人甲"])
        assert "林墨" in ctx.character_states
        assert "主角" in ctx.character_states["林墨"]
        assert ctx.character_states["路人甲"] == "（无档案，可能为新角色）"

    def test_style_and_custom_constraints_injected(self, make_memory):
        store, memory = make_memory("ctx-style")
        store.write("settings/style.md", "短句为主，冷硬。", metadata={"title": "文风"})
        store.write("settings/custom-skills.md", "禁止出现现代俚语。", metadata={"title": "约束"})
        ctx = memory.retrieve_context(chapter=1, chapter_outline="q", characters=[])
        assert "冷硬" in ctx.style_guide
        assert "现代俚语" in ctx.custom_constraints

    def test_future_planted_foreshadowing_hidden(self, make_memory):
        """反剧透：planted_ch > 当前章的计划伏笔不得暴露给 Writer。"""
        store, memory = make_memory("ctx-future")
        memory._apply_foreshadow_ops(
            1,
            [
                {"action": "plant", "desc": "第1章已埋", "resolve_ch": 5, "id": "f1"},
                {"action": "plant", "desc": "第7章才埋", "resolve_ch": 9, "id": "f2"},
            ],
            10,
        )
        # 人为把 f2 的 planted_ch 改成未来章节（模拟大纲期预登记）
        doc = store.read("settings/foreshadowing.md")
        meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
        meta["items"][1]["planted_ch"] = 7
        store.write("settings/foreshadowing.md", doc.content, meta)

        ctx = memory.retrieve_context(chapter=3, chapter_outline="q", characters=[])
        descs = [i["desc"] for i in ctx.unresolved_foreshadowing]
        assert "第1章已埋" in descs
        assert "第7章才埋" not in descs

    def test_due_foreshadowing_surfaces_near_resolve_chapter(self, make_memory):
        store, memory = make_memory("ctx-due")
        memory._apply_foreshadow_ops(
            1, [{"action": "plant", "desc": "临期线", "resolve_ch": 6, "id": "f1"}], 10
        )
        early = memory.retrieve_context(chapter=2, chapter_outline="q", characters=[])
        assert early.due_foreshadowing == []
        # 距预期回收章 <= 2 章 → 主动召回
        near = memory.retrieve_context(chapter=4, chapter_outline="q", characters=[])
        assert [i["desc"] for i in near.due_foreshadowing] == ["临期线"]

    def test_finale_forces_all_remaining_into_due_list(self, make_memory):
        """完本收尾扫描：最后一章必须把仍未回收的已埋设伏笔全部纳入「务必回收」。"""
        store, memory = make_memory("ctx-finale")
        memory._apply_foreshadow_ops(
            1,
            [
                {"action": "plant", "desc": "早该回收", "resolve_ch": 3, "id": "f1"},
                {"action": "plant", "desc": "远期计划", "resolve_ch": 20, "id": "f2"},
            ],
            10,
        )
        ctx = memory.retrieve_context(
            chapter=10, chapter_outline="q", characters=[], total_chapters=10, is_finale=True
        )
        due = {i["desc"] for i in ctx.due_foreshadowing}
        assert due == {"早该回收", "远期计划"}

    def test_resolved_foreshadowing_excluded(self, make_memory):
        store, memory = make_memory("ctx-resolved")
        memory._apply_foreshadow_ops(1, [{"action": "plant", "desc": "已收", "resolve_ch": 4, "id": "f1"}], 10)
        memory._apply_foreshadow_ops(2, [{"action": "resolve", "id": "f1"}], 10)
        ctx = memory.retrieve_context(chapter=3, chapter_outline="q", characters=[])
        assert ctx.unresolved_foreshadowing == []

    def test_state_board_filters_future_chapters(self, make_memory):
        """核心反剧透：只注入 chapter < 当前章 的状态板硬事实。"""
        store, memory = make_memory("ctx-board")
        memory.apply_state_ops(1, [{"entity": "A", "fact": "第1章事实"}])
        memory.apply_state_ops(9, [{"entity": "B", "fact": "第9章事实"}])
        ctx = memory.retrieve_context(chapter=3, chapter_outline="q", characters=[])
        facts = [i["fact"] for i in ctx.state_board]
        assert facts == ["第1章事实"]

    def test_state_board_excludes_current_chapter(self, make_memory):
        store, memory = make_memory("ctx-board2")
        memory.apply_state_ops(5, [{"entity": "A", "fact": "同章事实"}])
        ctx = memory.retrieve_context(chapter=5, chapter_outline="q", characters=[])
        assert ctx.state_board == []

    def test_empty_book_returns_empty_context(self, make_memory):
        _, memory = make_memory("ctx-empty")
        ctx = memory.retrieve_context(chapter=1, chapter_outline="q", characters=[])
        assert ctx.recent_summaries == []
        assert ctx.character_states == {}
        assert ctx.state_board == []


class TestIndexMaintenance:
    def test_only_indexed_kinds_are_embedded(self, make_memory, fake_index):
        store, memory = make_memory("idx-kinds")
        store.write("settings/worldview/w.md", "w", metadata={"title": "W"})
        store.write("chapters/vol-01/ch-001.md", "c", metadata={"chapter": 1, "volume": 1})
        store.write("settings/outline.md", "o", metadata={"title": "O"})
        for rel in ("settings/worldview/w.md", "chapters/vol-01/ch-001.md", "settings/outline.md"):
            memory.index_document(rel)
        indexed = {rel for rel, _ in fake_index.upserted}
        assert "settings/worldview/w.md" in indexed
        # 正文与大纲不入库（正文过长、大纲另走直读）
        assert "chapters/vol-01/ch-001.md" not in indexed
        assert "settings/outline.md" not in indexed

    def test_rebuild_index_clears_then_fills(self, make_memory, fake_index):
        store, memory = make_memory("idx-rebuild")
        store.write("settings/worldview/w.md", "w", metadata={"title": "W"})
        store.write("settings/characters/c.md", "c", metadata={"title": "C"})
        total = memory.rebuild_index()
        assert fake_index.cleared == 1
        assert total == 2

    def test_kind_for_path_mapping(self):
        from src.memory.memory_manager import kind_for_path

        assert kind_for_path("settings/worldview/w.md") == "worldview"
        assert kind_for_path("settings/characters/a.md") == "character"
        assert kind_for_path("settings/outline.md") == "outline"
        assert kind_for_path("settings/foreshadowing.md") == "foreshadowing"
        assert kind_for_path("chapters/vol-01/ch-001.md") == "chapter"
        assert kind_for_path("summaries/ch-001.summary.md") == "summary"
        assert kind_for_path("reviews/ch-001.review.md") == "review"
        assert kind_for_path("chats/x.json") == "other"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
