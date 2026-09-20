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
        "target_words", "tolerance", "length_floor", "length_ceiling",
        "revision_section", "chapter", "outline",
        "recent_summaries", "related_summaries", "character_states",
        "foreshadowing", "due_foreshadowing", "worldview_rules", "state_board",
        "style_guide", "custom_constraints", "brief_fidelity",
        "reality_policy",
    ), "x")
    writer_vars["brief"] = "标记串BRIEF_MARK_42"
    assert "BRIEF_MARK_42" in render_prompt("writer_chapter", **writer_vars)

    editor_vars = dict.fromkeys((
        "chapter", "outline", "recent_summaries", "character_states",
        "worldview_rules", "foreshadowing", "style_guide", "custom_constraints",
        "chapter_text", "target_words", "actual_length", "tolerance",
        "length_floor", "length_ceiling", "reality_policy",
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


# ---------- 问题④ 现实性口径：以作者创作目标为准（用户要求） ----------

def test_reality_policy_default_is_author_goal_first():
    """默认档必须写死三件事：唯一基准是作者目标、禁止以"不符合现实"扣分/提建议、例外只有吃书。"""
    from src.agents.reality_policy import reality_policy_text

    text = reality_policy_text(False)
    assert "唯一评判基准" in text
    assert "禁止" in text and "不符合现实" in text
    assert "吃书" in text and "自相矛盾" in text       # 唯一例外：作者框架内的错误
    assert "作者设定框架内" in text                     # 建议只能"怎么写更好"


def test_reality_policy_on_switches_semantics():
    """勾选「要求现实合理性约束」后语义反转，但作者指定仍优先于现实逻辑。"""
    from src.agents.reality_policy import reality_policy_text

    text = reality_policy_text(True)
    assert "作者已要求现实约束" in text
    assert "现实合理性" in text
    assert "作者明确要的东西优先于现实逻辑" in text


def test_allow_realism_normalization():
    from src.agents.reality_policy import normalize_allow_realism

    assert normalize_allow_realism(True) is True
    assert normalize_allow_realism("是") is True
    assert normalize_allow_realism("true") is True
    assert normalize_allow_realism(False) is False
    assert normalize_allow_realism(None) is False       # 缺省 = 以作者目标为准
    assert normalize_allow_realism("乱写") is False


def test_brief_persists_allow_realism_and_reads_back(sandbox, make_memory):
    """开关必须落盘可读回；且**单独改开关**（正文不变）也要写盘。"""
    from src.memory.memory_manager import (
        allow_realism,
        read_brief,
        read_brief_fields,
        write_brief,
    )

    store, _memory = make_memory("realism-flag")
    assert allow_realism(store) is False                     # 旧书/未设置 → 默认关

    assert write_brief(store, "主角是义体剑客", {"genre": "赛博修仙"}) is True
    assert allow_realism(store) is False
    # 单独把开关打开（创作需求正文一字不变）→ 必须写盘，不能被幂等判据吃掉
    assert write_brief(store, "主角是义体剑客",
                       {"genre": "赛博修仙", "allow_realism": True}) is True
    assert allow_realism(store) is True
    assert read_brief_fields(store).get("allow_realism") is True
    assert "allow_realism：true" in read_brief(store), "正文里应留可读标记"

    # 再写同样内容 → 幂等不写盘
    assert write_brief(store, "主角是义体剑客",
                       {"genre": "赛博修仙", "allow_realism": True}) is False


def test_reality_policy_reaches_chapter_context(sandbox, make_memory):
    """章节上下文必须带上现实性口径（写作/审查/出卡三条链路都读它）。"""
    from src.agents.reality_policy import reality_policy_text
    from src.memory.memory_manager import write_brief

    store, memory = make_memory("realism-ctx")
    store.write("settings/outline.md", "# 大纲\n",
                metadata={"title": "T", "theme": "t", "volumes": [
                    {"volume": 1, "title": "V", "chapters": [
                        {"chapter": 1, "title": "C1", "outline": "o", "characters": []}]}]},
                commit_message="s")
    write_brief(store, "主角是义体剑客")
    ctx = memory.retrieve_context(chapter=1, chapter_outline="C1", characters=[],
                                  total_chapters=1, is_finale=True)
    assert reality_policy_text(False) in ctx.reality_policy

    # 打开开关 → 上下文随之切换（同一本书内即时生效）
    write_brief(store, "主角是义体剑客", {"allow_realism": True})
    ctx2 = memory.retrieve_context(chapter=1, chapter_outline="C1", characters=[],
                                   total_chapters=1, is_finale=True)
    assert reality_policy_text(True) in ctx2.reality_policy


def test_reality_policy_present_in_writer_editor_and_plotter_prompts():
    """★ 四类出口（写手/审校/协商/出卡）的渲染结果里都必须有这条口径。"""
    from src.agents.prompt_loader import render_prompt
    from src.agents.reality_policy import reality_policy_text

    marker = "【现实性评判口径（刚性 · 作者未要求现实约束）】"
    assert marker in reality_policy_text(False)

    writer = render_prompt("writer_chapter", **{**dict.fromkeys((
        "target_words", "tolerance", "length_floor", "length_ceiling",
        "revision_section", "chapter", "outline", "recent_summaries",
        "related_summaries", "character_states", "foreshadowing",
        "due_foreshadowing", "worldview_rules", "state_board", "style_guide",
        "custom_constraints", "brief", "brief_fidelity",
    ), "x"), "reality_policy": reality_policy_text(False)})
    editor = render_prompt("editor_review", **{**dict.fromkeys((
        "chapter", "outline", "recent_summaries", "character_states",
        "worldview_rules", "foreshadowing", "style_guide", "custom_constraints",
        "chapter_text", "target_words", "actual_length", "tolerance",
        "length_floor", "length_ceiling", "brief",
    ), "x"), "reality_policy": reality_policy_text(False)})
    plotter = render_prompt("plotter_cards", **{**dict.fromkeys((
        "chapter", "story_overview", "recent_summaries", "related_summaries",
        "character_states", "state_board", "foreshadowing", "due_foreshadowing",
        "worldview_rules", "custom_constraints", "feedback", "brief",
    ), "x"), "reality_policy": reality_policy_text(False)})
    negotiate = render_prompt("writer_negotiate", **{**dict.fromkeys((
        "chapter", "outline", "issues", "comment", "chapter_text",
        "custom_constraints", "brief_fidelity",
    ), "x"), "reality_policy": reality_policy_text(False)})

    for name, prompt in (("writer_chapter", writer), ("editor_review", editor),
                         ("plotter_cards", plotter), ("writer_negotiate", negotiate)):
        assert marker in prompt, f"{name} 缺现实性口径（又可按 '不合理' 要求改稿了）"
        assert "不符合现实" in prompt
    # 审校还必须有一条"常见误区"级别的显式禁令
    assert "拿现实逻辑当尺子" in editor


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


def test_length_bounds_are_asymmetric():
    """字数口径是**非对称**的（用户要求）：下浮 ≤500 硬线，上浮 ≤2000 放宽。

    旧口径 `max(500, target*0.15)` 是**对称**的：5000 字的章既容许少 750 字（4250 也算合格）、
    又只容许写到 5750，与"至少 5000 字、向下最多 500、向上可到 2000"的要求方向相反。
    """
    gen = GenerationConfig()               # floor_offset=500, ceiling_offset=2000, ratio=0.0
    assert gen.length_bounds(5000) == (4500, 7000)
    assert gen.length_bounds(3000) == (2500, 5000)
    assert gen.length_bounds(1000) == (500, 3000)
    # 下浮一律 500（不随目标放大）；上浮一律 2000
    for target in (1000, 3000, 5000, 12000):
        lo, hi = gen.length_bounds(target)
        assert target - lo == 500, f"下浮不是 500：target={target}"
        assert hi - target == 2000, f"上浮不是 2000：target={target}"


def test_length_assessment_is_one_sided_strict():
    """判定：低于下限=short（硬线），区间内=pass，超上限=long。"""
    from src.agents.writer import length_assessment

    # 目标 5000 → 可接受 4500-7000
    assert length_assessment(5000, 4499) == "short"
    assert length_assessment(5000, 4500) == "pass"
    assert length_assessment(5000, 5000) == "pass"
    assert length_assessment(5000, 7000) == "pass"      # 上浮 2000 以内都算合格
    assert length_assessment(5000, 7001) == "long"
    # 显式区间优先（调用方已按本章目标算好）
    assert length_assessment(5000, 4000, 3900, 6000) == "pass"
    assert length_assessment(5000, 3800, 3900, 6000) == "short"


def test_length_revision_note_uses_passed_tolerance():
    """门禁指令里的数字必须与门禁实际用的同一对数（否则来回打架）。

    入参语义：tolerance=最低可接受字数，ceiling=最高可接受字数。
    """
    note = length_revision_note(5000, 4000, 4500, 7000)
    assert "4500" in note and "7000" in note
    assert "还差 1000 字" in note     # 差额按"到目标"算（5000-4000）
    assert "加戏" in note            # 缺字的修法必须是加戏，不是注水
    note2 = length_revision_note(3000, 4200, 2500, 5000)
    assert "5000" in note2
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


def test_gate_target_survives_assemble_context():
    """★ 关卡上设定的字数不能被下一跳（assemble_context）用大纲预算覆盖。

    真实缺陷：`chapter_gate → assemble_context` 正是"关卡定字数 → 开写本章"的路径，
    而 assemble_context 原先无条件用大纲里的 target_words 覆盖 state，
    于是用户"按此字数开写第 N 章"没有任何效果（问题③ 的隐形杀手）。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    node = src.split("def assemble_context_node", 1)[1].split("def writer_node", 1)[0]
    assert 'preset_target = state.get("chapter_target_words")' in node, "未读取关卡预设字数"
    assert '"chapter_target_words": chapter_target' in node
    # 预设优先：chapter_target = preset or resolve(...)
    assert "chapter_target = preset_target or resolve_chapter_target(" in node


def test_writer_and_editor_share_the_same_target():
    """写作与评分/门禁必须取同一个 target 与同一对字数边界（否则"模型按 A 写、门禁按 B 判"）。"""
    src = (Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "graph.py").read_text(
        encoding="utf-8")
    writer = src.split("def writer_node", 1)[1].split("def editor_node", 1)[0]
    editor = src.split("def editor_node", 1)[1].split("def route_after_editor", 1)[0]
    assert "target_words_override=target" in writer
    assert '"target_words": target' in writer or "target_words: target" in writer
    assert "state.get(\"chapter_target_words\") or" in editor
    assert "target_words=target" in editor
    # 字数口径已从"对称容差"改为"非对称区间"：门禁与评分都读同一对边界
    assert "gen.length_bounds(target)" in editor
    assert "length_bounds=(floor, ceiling)" in editor
    assert "length_assessment(target" in editor


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
