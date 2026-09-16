"""动态验证（规格 §8.3）。

静态读完必须做动态验证：用「产出物」反查最快。本文件把四条步骤落成可重跑用例：

  1. 反例投喂：3 条已知会被通用模型拒答的输入，跑完整管线（设定 → 大纲 → 正文），
     逐跳记录实际产出；任一跳出现拒绝语义 / 「无法生成」类回复即判失败并定位到该跳。
  2. 断点定位：用 frontmatter 的 attempt / used_fallback + 审查报告 overall 与问题标签反查降级跳。
  3. ★ 对照实验：同一输入，绕过管线单发模型 vs 走完整管线，差值 = 管线本身的净效应。
  4. 记录：模型版本、temperature、日期。

零真实 LLM 调用：被验证的模型行为由 ScriptedRegistry 精确建模（见 _PIPELINE_SCAFFOLDING
说明），断言对象是「管线装配差异造成的产出差异」，与具体厂商无关。
"""

from __future__ import annotations

import json
import time

from test_pipeline_closure import (  # noqa: E402 - 与 A9 共用样本，不重复造数据
    A9_SAMPLES,
    EXTREME_FLAG,
    REFUSAL_MARKERS,
    ScriptedRegistry,
    _balk_json,
    _characters_json,
    _fields_of,
    _outline_json,
    _review_json,
    _worldview_json,
)

from src.agents.architect import BRIEF_FIELDS, Architect, render_brief_fields
from src.agents.editor import Editor
from src.agents.schemas import WorldviewDoc
from src.agents.writer import Writer
from src.llm.base import ChatMessage
from src.memory.memory_manager import ChapterContext

# 审计现场记录（§8.4 第 4 步：模型版本 / temperature / 日期）
RUN_RECORD = {
    "model": "mock-1",
    "provider": "mock",
    "temperature": "角色绑定默认（管线未显式覆盖）；X7 末次重试降至 0.2",
    "generated_at": time.strftime("%Y-%m-%d"),
}

# 反例投喂样本（含惊悚/犯罪/恐怖/悲剧结局要素，取自 A9 共用样本）
COUNTER_EXAMPLES = [
    s for s in A9_SAMPLES if s[-1] == EXTREME_FLAG
][:3]


def _pipeline_scaffolding(prompt: str) -> bool:
    """模拟「对裸请求拒答、对装配完整的管线请求产出」的通用模型。

    判据（管线装配的判别标记）：
      · 设定/大纲跳：注入块含「虚构框架声明」+「零拒绝」（W6 / X2 / X7）；
      · 正文跳：prompt 含「## 写作要求」+「只输出章节正文本身」（分层装配完备）。
    这个判据就是待验证的假设——对照实验量出来的正是它的净效应。
    """
    if "虚构框架声明" in prompt and "零拒绝" in prompt:
        return True
    return "## 写作要求" in prompt and "只输出章节正文本身" in prompt


def _dynamic_responder(fields: dict[str, str]):
    """按 schema 分派：装配完备 → 合规产出；否则 → 拒绝语义产出。

    分派判据是 chat_structured 追加的 JSON Schema 字段名（DemoOutput 同时含
    characters，故必须先于 CharactersOutput 判定）。
    """
    def responder(role, messages, _n):
        prompt = "\n".join(m.content for m in messages)
        ok = _pipeline_scaffolding(prompt)
        if "## 写作要求" in prompt and "只输出章节正文本身" in prompt:   # Writer 正文
            if not ok:
                return "抱歉，无法生成。"
            return "第 1 章正文：" + ("林墨推开第九处的铁门，走廊尽头的灯灭了。" * 12)
        if "\"volumes\"" in prompt:                       # OutlineOutput
            return _outline_json(fields) if ok else _balk_json()
        if "\"synopsis\"" in prompt:                      # DemoOutput
            return _demo_json(fields) if ok else _balk_json()
        if "\"docs\"" in prompt:                          # WorldviewOutput
            return _worldview_json() if ok else json.dumps({"docs": []}, ensure_ascii=False)
        if "\"characters\"" in prompt:                    # CharactersOutput
            if ok:
                return _characters_json(fields)
            return json.dumps({"characters": []}, ensure_ascii=False)
        if "\"consistency\"" in prompt:                   # ReviewOutput
            return _review_json(9.0, [])
        return _balk_json()

    return responder


class _MemStore:
    """轻量事实源替身：Architect 只需 exists / read / write / iter_documents。"""

    CONSTRAINT = "【约束】全篇避免现代网络用语。"

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def exists(self, rel: str) -> bool:
        return rel in self.written or rel == "settings/custom-skills.md"

    def read(self, rel: str):
        from types import SimpleNamespace

        if rel == "settings/custom-skills.md":
            return SimpleNamespace(content=self.CONSTRAINT, metadata={})
        return SimpleNamespace(content=self.written.get(rel, ""), metadata={})

    def write(self, rel: str, content: str, metadata=None, commit_message: str = "") -> None:
        self.written[rel] = content

    def iter_documents(self):
        return []


def _demo_json(fields: dict[str, str]) -> str:
    return json.dumps(
        {
            "book_title": f"《{fields['genre']}·样本》",
            "synopsis": f"{fields['protagonist']}在{fields['setting']}中挣扎。",
            "overview": f"开端/发展/高潮/结局均沿「{fields['arc']}」推进。",
            "theme": fields["themes"],
            "worldview": [{"filename": "w", "title": "W", "content": fields["setting"]}],
            "characters": [
                {"name": fields["protagonist"], "role": "主角", "appearance": "清瘦",
                 "personality": fields["tone"], "background": fields["setting"]}
            ],
        },
        ensure_ascii=False,
    )


def _assert_no_refusal(jump: str, payload: str) -> None:
    """★ 任一跳出现拒绝语义 / 「无法生成」类回复 → 判失败并定位到该跳。"""
    hits = [m for m in REFUSAL_MARKERS if m in payload]
    assert not hits, f"第「{jump}」跳出现拒绝语义 {hits}"


def _run_full_pipeline(sample: tuple[str, ...], store) -> dict:
    """跑完整管线：设定 → 大纲 → 正文，并逐跳记录实际产出。"""
    fields = _fields_of(sample)
    brief = render_brief_fields(fields)
    registry = ScriptedRegistry(_dynamic_responder(fields))
    jumps: dict[str, str] = {}

    # ① 设定跳（T1：设定 Demo）
    demo = Architect(registry, store).generate_demo(
        brief, 3, custom_constraints=_MemStore.CONSTRAINT
    )
    jumps["设定"] = demo.model_dump_json()

    # ② 大纲跳（T2–T5）
    outline = Architect(registry, store).generate_settings(
        brief, total_chapters=3, custom_constraints=_MemStore.CONSTRAINT
    )
    jumps["大纲"] = outline.model_dump_json()

    # ③ 正文跳（T8：Writer，分层装配完备）
    writer = Writer(registry, target_words=200, tolerance=100000,
                    max_continuation_attempts=0)
    ctx = ChapterContext(chapter=1, outline=outline.volumes[0].chapters[0].outline)
    result = writer.write_chapter(ctx)
    jumps["正文"] = result.content

    return {"jumps": jumps, "registry": registry, "result": result, "outline": outline}


def _prepare_store(make_memory, novel_id: str):
    """准备一本最小可用的书（角色档案 + 文风指纹/状态板留空）。"""
    store, memory = make_memory(novel_id)
    store.write(
        "settings/characters/林墨.md",
        "# 林墨\n\n## 定位\n主角\n\n## 外貌\n清瘦\n\n## 性格\n执拗\n\n## 背景\n无\n",
        metadata={"title": "林墨", "role": "主角", "location": ""},
        commit_message="seed",
    )
    return store, memory


# ══════════════════════ 步骤 1 · 反例投喂 ══════════════════════

def test_dynamic_counter_examples_three_jumps(sandbox, make_memory):
    """3 条反例输入跑完整管线，逐跳断言无拒绝语义。"""
    assert len(COUNTER_EXAMPLES) == 3
    for idx, sample in enumerate(COUNTER_EXAMPLES, 1):
        store, _memory = _prepare_store(make_memory, f"dyn-{idx}")
        run = _run_full_pipeline(sample, store)
        for jump in ("设定", "大纲", "正文"):
            _assert_no_refusal(jump, run["jumps"][jump])


def test_dynamic_outline_jump_triggers_a14(sandbox, make_memory):
    """大纲跳即触发 A14 断言口径：X1 字段逐项命中 + 零改写。"""
    for idx, sample in enumerate(COUNTER_EXAMPLES, 1):
        store, _memory = _prepare_store(make_memory, f"dyn-a14-{idx}")
        fields = _fields_of(sample)
        registry = ScriptedRegistry(_dynamic_responder(fields))
        architect = Architect(registry, store)
        architect.generate_settings(
            render_brief_fields(fields), total_chapters=3,
            custom_constraints=_MemStore.CONSTRAINT,
        )
        prompt = registry.prompts()[0]
        missing = [label for _k, label in BRIEF_FIELDS if f"- {label}：" not in prompt]
        assert missing == [], f"[{sample[0]}] X1 字段缺失：{missing}"
        payload = store.read("settings/outline.md").content
        assert fields["protagonist"] in payload


# ══════════════════════ 步骤 2 · 断点定位 ══════════════════════

def test_dynamic_degradation_is_locatable(sandbox, make_memory):
    """产出退化时，用 frontmatter 的 attempt/used_fallback + 审查报告标签反查降级跳。"""
    store, memory = _prepare_store(make_memory, "dyn-locate")
    sample = COUNTER_EXAMPLES[0]
    run = _run_full_pipeline(sample, store)

    # 正文落盘（与 graph.writer_node 同样的元数据口径）
    rel = store.chapter_rel_path(1, 1)
    store.write(
        rel,
        run["jumps"]["正文"],
        metadata={
            "chapter": 1, "volume": 1, "status": "draft", "attempt": 1,
            "model": f"{run['result'].provider_name}/{run['result'].model}",
            "used_fallback": run["result"].used_fallback,
        },
        commit_message="ch-001 第 1 稿",
    )
    doc = store.read(rel)
    assert doc.metadata["attempt"] == 1
    assert doc.metadata["used_fallback"] is False      # 无降级跳可定位

    # 审查报告：overall + 问题标签（维度/严重度）可反查退化维度
    editor = Editor(ScriptedRegistry(lambda role, msgs, n: _review_json(
        5.0,
        [{"dimension": "consistency", "severity": "major",
          "description": "与实体状态板冲突。", "quote": "铁门", "suggestion": "改。"}],
    )), store)
    ctx = memory.retrieve_context(
        chapter=1, chapter_outline=run["outline"].volumes[0].chapters[0].outline,
        characters=["林墨"], total_chapters=3,
    )
    review = editor.review_chapter(ctx, run["jumps"]["正文"], attempt=1)
    report = store.read(store.review_rel_path(1))
    assert report.metadata["overall"] == review.overall
    # overall 7.25 ∈ [6,8) → verdict=partial_rewrite（低于送审阈值 8.0），可据此反查降级跳
    assert report.metadata["verdict"] == "partial_rewrite"
    assert any(i.dimension == "consistency" and i.severity == "major" for i in review.issues)
    assert review.overall < 8.0, "退化产出应低于送审阈值，便于反查降级跳"


# ══════════════════════ 步骤 3 · ★ 对照实验 ══════════════════════

def test_dynamic_control_experiment_net_effect(sandbox, make_memory):
    """同一输入：绕过管线单发模型 vs 走完整管线；差值 = 管线本身的净效应。"""
    sample = COUNTER_EXAMPLES[1]
    fields = _fields_of(sample)
    store, _memory = _prepare_store(make_memory, "dyn-control")

    # 对照组：同一 brief 单发（无管线装配）→ 复现「裸请求被拒」
    raw_registry = ScriptedRegistry(_dynamic_responder(fields))
    raw_result = raw_registry.chat_as(
        "architect", [ChatMessage("user", render_brief_fields(fields))]
    )
    raw_balked = any(m in raw_result.content for m in REFUSAL_MARKERS)
    assert raw_balked, "对照组未复现「裸请求被拒」"

    # 实验组：走完整管线
    run = _run_full_pipeline(sample, store)
    ok_jumps = sum(
        1 for jump in ("设定", "大纲", "正文")
        if not any(m in run["jumps"][jump] for m in REFUSAL_MARKERS)
    )
    # 净效应 = 实验组通过跳数 − 对照组通过跳数（0）＝ 3
    assert ok_jumps == 3, "实验组不应出现拒绝语义"
    assert ok_jumps - 0 == 3


# ══════════════════════ 步骤 4 · 记录 ══════════════════════

def test_dynamic_run_record_complete():
    """记录模型版本、temperature、日期。"""
    for key in ("model", "provider", "temperature", "generated_at"):
        assert RUN_RECORD.get(key), f"现场记录缺字段：{key}"
    assert RUN_RECORD["generated_at"] == time.strftime("%Y-%m-%d")


def test_dynamic_prompts_carry_scaffolding(sandbox, make_memory):
    """对照实验的前提：管线确实注入了判别标记（否则净效应无从谈起）。"""
    sample = COUNTER_EXAMPLES[2]
    fields = _fields_of(sample)
    store, _memory = _prepare_store(make_memory, "dyn-scaffold")
    registry = ScriptedRegistry(_dynamic_responder(fields))
    architect = Architect(registry, store)
    architect.generate_demo(render_brief_fields(fields), 3,
                            custom_constraints=_MemStore.CONSTRAINT)
    writer = Writer(registry, target_words=200, tolerance=100000,
                    max_continuation_attempts=0)
    architect.generate_settings(render_brief_fields(fields), total_chapters=3,
                                custom_constraints=_MemStore.CONSTRAINT)
    writer.write_chapter(ChapterContext(chapter=1, outline="第 1 章"))

    prompts = registry.prompts()
    assert any("虚构框架声明" in p and "零拒绝" in p for p in prompts)
    assert any("## 写作要求" in p and "只输出章节正文本身" in p for p in prompts)


def test_dynamic_worldview_doc_schema_guard():
    """防回归：WorldviewDoc 必须仍可被 scripted 产出正常校验。"""
    doc = WorldviewDoc(filename="w", title="W", content="正文")
    assert doc.filename == "w"
