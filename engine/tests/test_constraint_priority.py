"""自定义创作约束的**优先级与注入完整性**回归（2026-10-04 用户实测批次）。

用户原话：「自定义创作约束注入不完善，或者优先级不高；不管是创作未开始注入还是
创作中途注入，都需要把自定义创作约束的优先级提高。」

定位到的两处结构性缺口（本模块逐条钉死）：

【缺口 A · 创作前注入被稀释】约束**实体**只在提示词前部出现一次，之后是数千字的
网文文风总纲与上下文（writer_chapter 渲染后 ≈6.8k 字，约束实体落在 13% 处），
而末尾（recency 位）此前只留一句 `完整约束与执行清单见上文`——**指针不携带内容**，
模型生成时最后看到的是【文风指纹】【待审正文】而不是约束。
→ 修法：`render_constraint_tail()` 末位复现清单实体，与高位块构成"双锚点"；
   并把「三重压制（概念/流程/执行）」补进刚性块（知识库 §2.4 / §7.4）。

【缺口 B · 创作中途注入整段丢失】对话链路实际用的是 `inkforge_api._book_context`
——它是模块级 `build_book_context` 的**早期重复实现**，压根不读
`settings/custom-skills.md`；随后四处 `context[:1500/2200/2600/3200]` 裸切片再叠加一层截断。
→ 修法：`_book_context` 委托唯一实现；约束段刚性化 + 提位到 blocks[0]；
   `clip_context()` 保证约束头永不被截。
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------- 缺口 A：创作前注入的强度 ----------

#: 必须携带末位锚点的模板（与 prompt_loader.CONSTRAINT_TAIL_TEMPLATES 同源断言）
KEY_TEMPLATES = (
    "writer_chapter", "editor_review", "plotter_cards", "architect_outline_revise",
    "writer_negotiate", "editor_arbitrate",
)

SAMPLE = """# 创作约束（风格工坊绑定）
## 自定义约束：术语表
- 力量体系称呼统一为「炁」，禁止写成「真气」
- **禁忌**：禁止出现现代词汇（如手机、科学、网络）
- 主角全程不得使用枪械，只能近身搏杀
- 每章必须出现一次环境嗅觉描写
"""

_NO_BLOCK_MARKERS = ("### 约束原文", "### 三重压制条款")


def _writer_vars(constraints: str) -> dict[str, object]:
    from src.agents.reality_policy import reality_policy_text

    probe: dict[str, object] = dict.fromkeys(
        ("target_words", "tolerance", "length_floor", "length_ceiling", "revision_section",
         "chapter", "outline", "recent_summaries", "related_summaries", "character_states",
         "foreshadowing", "due_foreshadowing", "worldview_rules", "state_board",
         "style_guide", "brief", "brief_fidelity"), "（占位）")
    probe["reality_policy"] = reality_policy_text(False)
    probe["custom_constraints"] = constraints
    return probe


def test_key_templates_declare_tail_anchor():
    """4 个关键模板必须声明末位锚点占位符（否则长模板里约束必被后文稀释）。"""
    from src.agents.prompt_loader import CONSTRAINT_TAIL_TEMPLATES, load_all

    templates = load_all()
    for name in KEY_TEMPLATES:
        assert name in CONSTRAINT_TAIL_TEMPLATES, f"{name} 应列入强制锚点模板"
        assert "{{custom_constraints_tail}}" in templates[name], f"{name} 缺末位锚点占位符"


def test_validate_templates_rejects_missing_tail_anchor(tmp_path, monkeypatch):
    """启动期断言：关键模板漏了末位锚点 → 直接 ConfigError（Fail-Fast，不留到用户实测）。"""
    from src.agents import prompt_loader as pl

    (tmp_path / "writer_chapter.md").write_text(
        "{{constitution}}\n{{custom_constraints}}\n", encoding="utf-8"
    )
    monkeypatch.setattr(pl, "PROMPTS_DIR", tmp_path)
    pl._load_template.cache_clear()
    try:
        with pytest.raises(pl.ConfigError, match="末位约束锚点"):
            pl.validate_templates()
    finally:
        pl._load_template.cache_clear()


def test_tail_anchor_repeats_checklist_not_pointer():
    """末位锚点必须**复现清单实体**（旧契约的"见上文"指针不算数）。"""
    from src.agents.prompt_loader import CONSTRAINT_TAIL_HEADING, render_prompt

    prompt = render_prompt("writer_chapter", **_writer_vars(SAMPLE))
    at = prompt.rindex(CONSTRAINT_TAIL_HEADING)
    assert at > len(prompt) * 0.8, "末位锚点必须落在提示词尾部（recency 位）"
    zone = prompt[at:]
    for item in ("力量体系称呼统一为「炁」", "禁止出现现代词汇", "不得使用枪械"):
        assert item in zone, f"末位锚点未复现清单条目：{item}"
    assert "- [ ]" in zone, "末位锚点必须是可逐条勾选的清单"
    assert "完整约束与执行清单见上文" not in prompt, "指针式旧写法应已替换为实体锚点"


def test_three_layer_suppression_present():
    """三重压制（概念 / 流程·强制预读 / 执行·冲突裁决）必须齐备 —— 缺后两重即"约束可被打折"。"""
    from src.agents.prompt_loader import render_prompt

    prompt = render_prompt("writer_chapter", **_writer_vars(SAMPLE))
    assert "【概念压制】" in prompt
    assert "【流程压制 · 强制预读】" in prompt
    assert "【执行压制 · 冲突裁决】" in prompt
    # 执行压制的关键语义：冲突时调整大纲去服从约束，而不是把约束打折
    assert "调整大纲与剧情的执行方式来服从本条" in prompt


def test_placeholder_constraints_produce_no_spurious_block():
    """「（无）」占位不得渲染成空喊"最高优先级"的刚性块（那会反过来稀释真约束）。"""
    from src.agents.prompt_loader import (
        CONSTRAINT_TAIL_HEADING,
        render_prompt,
    )

    prompt = render_prompt("writer_chapter", **_writer_vars("（无）"))
    for marker in _NO_BLOCK_MARKERS:
        assert marker not in prompt, f"占位约束不应产生刚性块：{marker}"
    assert CONSTRAINT_TAIL_HEADING not in prompt, "占位约束不应产生末位锚点"


# ---------- 缺口 B：创作中途注入（对话链路） ----------

LONG_CONSTRAINT = (
    "# 创作约束（风格工坊绑定）\n"
    + "\n".join(f"- 第{i}条硬约束：全文必须严格遵守本条设定与尺度边界" for i in range(1, 61))
    + "\n- 最后一条：章末必须留下未解决的问题。\n"
)


def test_clip_context_never_cuts_constraint_head(sandbox):
    """约束头无论多长都整段保留；裸切片会把它切掉（这就是"绑了约束却不算数"的机制）。"""
    from src.memory.md_store import MdStore
    from src.web.inkforge_api import CONSTRAINT_HEAD_BOUNDARY, build_book_context, clip_context

    store = MdStore(sandbox.novels / "clip-book", auto_git=False)
    store.write("settings/custom-skills.md", LONG_CONSTRAINT,
                metadata={"title": "约束"}, commit_message="s")
    store.write("settings/state-board.md", "状态板硬事实：" + "甲" * 300,
                metadata={"title": "板"}, commit_message="s")

    ctx = build_book_context(store, "clip-book")
    assert CONSTRAINT_HEAD_BOUNDARY in ctx
    head = ctx.split(CONSTRAINT_HEAD_BOUNDARY)[0]
    assert len(head) > 1500, "构造样本的约束头应超过最小切片预算，才能验证截断行为"

    kept = clip_context(ctx, 1500)
    assert "最后一条：章末必须留下未解决的问题。" in kept, "约束头被截断"
    assert head in kept, "约束头未整段保留"

    # 对照：裸切片（旧实现）确实会切掉约束尾
    assert "最后一条：章末必须留下未解决的问题。" not in ctx[:1500]


def test_book_context_constraint_is_first_and_rigid(sandbox):
    """约束块必须是上下文首段、带统一锚头与三重压制 —— 顺序即优先级。"""
    from src.agents.prompt_loader import CONSTRAINT_HEADING
    from src.memory.md_store import MdStore
    from src.web.inkforge_api import CONSTRAINT_HEAD_BOUNDARY, build_book_context

    store = MdStore(sandbox.novels / "first-book", auto_git=False)
    store.write("settings/custom-skills.md", SAMPLE,
                metadata={"title": "约束"}, commit_message="s")
    store.write("settings/state-board.md", "状态板硬事实",
                metadata={"title": "板"}, commit_message="s")

    ctx = build_book_context(store, "first-book")
    assert ctx.startswith("【作者指定 · 创作约束】"), "约束块必须排在作品事实源之前"
    assert CONSTRAINT_HEADING in ctx
    assert "【执行压制 · 冲突裁决】" in ctx, "对话链路约束块未带三重压制"
    assert CONSTRAINT_HEAD_BOUNDARY in ctx, "缺约束头/事实源的显式边界"
    # 状态板仍必须在场（提位不得挤掉事实源）
    assert "【实体状态板】" in ctx


def test_live_chat_path_uses_single_book_context_implementation():
    """对话链路的 `_book_context` 必须委托唯一实现 —— 旧的重复实现不读 custom-skills.md。

    这是"创作中途注入"整段失效的根因：模块级 `build_book_context` 带约束段却只被测试调用，
    线上走的是另一个不读约束的副本（"测试绿、线上缺"）。
    """
    src = (Path(__file__).resolve().parents[1] / "src" / "web" / "inkforge_api.py").read_text(
        encoding="utf-8")
    body = src.split("def _book_context(novel: str)", 1)[1].split("\ndef ", 1)[0]
    # 剥离文档字符串：注释里说明"旧实现长什么样"是正常的，判定只看可执行代码
    code = body.split('"""', 2)[-1] if '"""' in body else body
    assert "build_book_context(_store(novel), novel)" in code, "对话链路未委托唯一实现"
    # 旧副本的特征代码（自建 parts 列表 + 末尾 text[:6000] 硬切）不得残留
    assert "text[:6000]" not in code, "旧副本的二次硬切仍在"
    assert "parts.append" not in code, "旧副本的重复组装逻辑仍在"
    # 调用方不得再裸切片上下文（会绕过 clip_context 的约束保底）
    assert "context[:1500]" not in src
    assert "context[:2200]" not in src
    assert "context[:2600]" not in src
    assert "context[:3200]" not in src
