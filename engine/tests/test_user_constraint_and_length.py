"""用户三问修复的回归测试（2026-09-17 批次）。

三问：
  ① 用户指令（brief）约束力不足 → brief 必须逐章进入生成/审查链路
  ② 大纲打回重写与原描述相差大 → 打回必须走**定向修订**（带原大纲），不是从零重生成
  ③ 生成新章节前的预期字数 → 逐章目标贯通写作/评分/字数门禁，且可传可落盘
"""

from __future__ import annotations

from pathlib import Path

from src.agents.writer import length_deviation, length_revision_note
from src.config.app_config import GenerationConfig
from src.memory.memory_manager import (
    BRIEF_REL,
    read_brief,
    write_brief,
)
from src.orchestrator.state import resolve_chapter_target

# ---------- 问题① brief 约束传导 ----------

def test_brief_roundtrip_and_idempotent(sandbox, make_memory):
    """brief 落盘为书内事实源；重复写同样内容不产生新提交（幂等）。"""
    store, _memory = make_memory("brief-book")
    assert read_brief(store) == ""                      # 旧书没这个文件：不报错

    assert write_brief(store, "主角是个哑巴剑客，全书不许出现对话引号") is True
    assert store.exists(BRIEF_REL)
    assert "哑巴剑客" in read_brief(store)

    # 同内容再写 → 返回 False（不写盘），避免噪声提交
    assert write_brief(store, "主角是个哑巴剑客，全书不许出现对话引号") is False


def test_brief_reaches_chapter_context(sandbox, make_memory):
    """关键回归：组装章节上下文时必须**直读**到 brief（此前 ChapterContext 里根本没有它）。"""
    store, memory = make_memory("brief-ctx")
    write_brief(store, "赛博修仙，主角是修剑的义体人，禁止出现穿越桥段")
    store.write("settings/outline.md", "# 大纲\n",
                metadata={"title": "T", "theme": "t", "volumes": [
                    {"volume": 1, "title": "V", "chapters": [
                        {"chapter": 1, "title": "C1", "outline": "o", "characters": []}]}]},
                commit_message="s")

    ctx = memory.retrieve_context(chapter=1, chapter_outline="C1", characters=[],
                                 total_chapters=1, is_finale=True)
    assert "义体人" in ctx.brief, "brief 未进入章节上下文（问题①未修）"
    assert "穿越桥段" in ctx.brief


def test_brief_renders_into_writer_and_editor_prompts():
    """brief 必须在**渲染后的提示词正文里**（模板占位符真被替换），而非只存在于对象字段。"""
    from src.agents.prompt_loader import render_prompt

    writer_vars = dict.fromkeys((
        "target_words", "tolerance", "revision_section", "chapter", "outline",
        "recent_summaries", "related_summaries", "character_states",
        "foreshadowing", "due_foreshadowing", "worldview_rules", "state_board",
        "style_guide", "custom_constraints",
    ), "x")
    writer_vars["brief"] = "标记串BRIEF_MARK_42"
    assert "BRIEF_MARK_42" in render_prompt("writer_chapter", **writer_vars)

    editor_vars = dict.fromkeys((
        "chapter", "outline", "recent_summaries", "character_states",
        "worldview_rules", "foreshadowing", "style_guide", "custom_constraints",
        "chapter_text", "target_words", "actual_length", "tolerance",
    ), "x")
    editor_vars["brief"] = "标记串BRIEF_MARK_42"
    assert "BRIEF_MARK_42" in render_prompt("editor_review", **editor_vars)


# ---------- 问题② 大纲定向修订 ----------

def test_revise_outline_template_carries_original_and_mode():
    """定向修订模板必须同时给出：原大纲 JSON、意见、修订模式（缺一即漂移）。"""
    from src.agents.prompt_loader import render_prompt

    rendered = render_prompt(
        "architect_outline_revise",
        brief="作者需求原文",
        original_outline='{"volumes": [{"volume": 1, "title": "卷一", "chapters": []}]}',
        revision_notes="第 3 章冲突太弱，改成正面交锋",
        revision_mode="targeted",
        worldview_digest="- 卷1《卷一》第1-3章",
        character_digest="",
        total_chapters=3,
        custom_constraints="（无）",
        brief_fidelity="（保真声明）",
    )
    assert "卷一" in rendered, "原大纲未进入修订提示词"
    assert "第 3 章冲突太弱" in rendered, "打回意见未进入修订提示词"
    assert "targeted" in rendered
    # 定向修订的硬约束必须写明"未点名处逐字保留"
    assert "逐字保留" in rendered


def test_graph_routes_outline_reject_to_revision_not_regeneration():
    """大纲打回时，graph 必须调 revise_outline（而不是重新 generate_settings）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    node = src.split("def architect_node", 1)[1].split("def outline_review_node", 1)[0]
    assert "revise_outline" in node, "大纲打回未走定向修订（问题②未修）"
    assert 'mode != "rewrite"' in node, "定向修订未按 revision_mode 分支"
    # 意见不得再被拼进 brief 字符串（那是"意见冒充作者需求"的旧写法）
    assert '【人工审阅打回意见，必须落实】' in node   # 仅 rewrite 分支保留
    assert node.count("【人工审阅打回意见，必须落实】") == 1


def test_parallel_outline_reject_uses_revision():
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator"
           / "parallel_runner.py").read_text(encoding="utf-8")
    body = src.split("def ensure_outline", 1)[1].split("\ndef ", 1)[0]
    assert "revise_outline" in body, "并行路径的大纲打回未走定向修订"


# ---------- 问题③ 逐章预期字数 ----------

def test_resolve_chapter_target_precedence_and_fallback():
    """逐章目标：大纲预算优先；缺失/非法回落默认；都不合法返回 None。"""
    assert resolve_chapter_target({"target_words": 5000}, 3000) == 5000
    assert resolve_chapter_target({"target_words": None}, 3000) == 3000
    assert resolve_chapter_target({}, 3000) == 3000
    assert resolve_chapter_target({"target_words": 0}, 3000) == 3000      # 0 视为未设
    assert resolve_chapter_target({"target_words": "abc"}, 3000) == 3000  # 脏值不抛错
    assert resolve_chapter_target(None, None) is None
    assert resolve_chapter_target({"target_words": "4500"}, 3000) == 4500  # 字符串数字可接受


def test_tolerance_scales_with_target():
    """容差按目标比例放大：长章不被固定 ±500 卡死，短章也不至于过松。"""
    gen = GenerationConfig()               # 默认 ratio=0.15, tolerance=500
    assert gen.tolerance_for(3000) == 500   # 3000*0.15=450 < 500 → 取 500
    assert gen.tolerance_for(5000) == 750   # 5000*0.15=750 > 500 → 取 750
    assert gen.tolerance_for(1000) == 500   # 短章仍保底 500


def test_length_revision_note_uses_passed_tolerance():
    """门禁指令里的"允许误差"必须与门禁实际用的容差一致（否则来回打架）。"""
    note = length_revision_note(5000, 4000, 750)
    assert "±750 字" in note and "1000 字" in note
    note2 = length_revision_note(3000, 4200, 500)
    assert "±500 字" in note2
    assert length_deviation(4200, 3000) == 1200   # 契约：返回绝对值


def test_chapter_plan_has_target_words_field():
    """大纲 schema 必须能承载逐章预期字数（否则大纲阶段产不出预算）。"""
    from src.agents.schemas import ChapterPlan

    plan = ChapterPlan(chapter=1, title="t", outline="o", characters=[],
                       target_words=4200)
    assert plan.target_words == 4200
    assert ChapterPlan(chapter=2, title="t", outline="o", characters=[]).target_words is None
    assert "target_words" in ChapterPlan.model_json_schema()["properties"]


def test_chapter_gate_payload_exposes_target_and_accepts_override():
    """逐章关卡：报告下一章预期字数，且能带新字数放行（"生成前设定"的唯一入口）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    gate = src.split("def chapter_gate_node", 1)[1].split("def route_after_writeback", 1)[0]
    assert '"target_words": default_target' in gate, "关卡未上报预期字数"
    assert 'decision.get("target_words")' in gate, "关卡未接受用户改写的字数"
    assert '"chapter_target_words": chosen' in gate, "关卡未把选定字数写入 state"


def test_writer_and_editor_share_the_same_target():
    """写作与评分必须取同一个 target（否则"模型按 A 写、评分按 B 判"）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    writer = src.split("def writer_node", 1)[1].split("def editor_node", 1)[0]
    editor = src.split("def editor_node", 1)[1].split("def route_after_editor", 1)[0]
    assert "target_words_override=target" in writer
    assert '"target_words": target' in writer or "target_words: target" in writer
    assert "state.get(\"chapter_target_words\") or" in editor
    assert "target_words=target" in editor
    assert "gen.tolerance_for(target)" in editor


def test_chapter_target_words_persisted_to_frontmatter():
    """目标字数要落章节 frontmatter（事实源）：断点续跑/看板/后续章节都读得到。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    assert '"target_words": target,' in src
    sched = (Path(__file__).resolve().parents[1] / "src" / "orchestrator"
             / "scheduler.py").read_text(encoding="utf-8")
    assert '"target_words": target,' in sched, "并行路径未落 frontmatter"
    assert "resolve_chapter_target(plan" in sched, "并行路径未用逐章目标"


def test_parallel_and_serial_use_same_resolver():
    """串行/并行必须共用同一个目标解析入口（否则两条路口径漂移）。"""
    root = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
    graph_src = (root / "graph.py").read_text(encoding="utf-8")
    sched_src = (root / "scheduler.py").read_text(encoding="utf-8")
    assert "resolve_chapter_target(" in graph_src
    assert "resolve_chapter_target(" in sched_src
