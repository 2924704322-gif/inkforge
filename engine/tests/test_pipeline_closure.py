"""闭环测试（规格 §8.2）+ 工作项验收断言（§8.1 A1–A12）。

零真实 LLM 调用：全部模型路径由 ScriptedRegistry 以预置 JSON 驱动，断言对象是
「管线装配出来的 prompt / 落盘事实源 / 归因日志」，与模型能力无关。

对应关系：
  §8.2 三章 Mock 管线        → test_closure_*
  §8.2 零拒绝冒烟 + §8.1 A14 → test_a14_*
  §8.1 A1–A12                → test_a1_* ... test_a12_*
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.architect import (
    BRIEF_FIELDS,
    BriefFidelityError,
    generate_faithful,
    render_brief_fields,
)
from src.agents.editor import Editor
from src.agents.prompt_loader import ConfigError, load_all, render_prompt, validate_templates
from src.agents.summarizer import Summarizer
from src.llm.base import ChatMessage, ChatResult, ModelProvider, ProviderError

ENGINE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_DIR.parent
PROMPTS_DIR = ENGINE_DIR / "src" / "agents" / "prompts"
DESKTOP_SRC = REPO_ROOT / "apps" / "desktop" / "src" / "renderer" / "src"

# 大纲路径的拒绝语义标记（A14 ① 判据，与 architect._REFUSAL_MARKERS 一致）
REFUSAL_MARKERS = ("无法生成", "做不到", "超出范围", "请换一个")

# 模板渲染缓存隔离：A1 用例会临时替换模板目录
@pytest.fixture(autouse=True)
def _clear_template_cache():
    from src.agents import prompt_loader

    prompt_loader._load_template.cache_clear()
    yield
    prompt_loader._load_template.cache_clear()


class ScriptedRegistry:
    """按预置响应驱动的假 registry：记录全部 messages 供 prompt 级断言。"""

    def __init__(self, responder):
        self._responder = responder
        self.calls: list[dict] = []

    @property
    def roles(self) -> list[str]:
        return ["architect", "writer", "editor"]

    def chat_as(self, role, messages, json_mode=False, temperature=None, max_tokens=None):
        self.calls.append(
            {"role": role, "messages": list(messages), "temperature": temperature}
        )
        out = self._responder(role, messages, len(self.calls))
        if isinstance(out, Exception):
            raise out
        return ChatResult(content=out, model="mock-1", provider_name="mock")

    def prompts(self) -> list[str]:
        return ["\n".join(m.content for m in c["messages"]) for c in self.calls]

    def last_prompt(self) -> str:
        return self.prompts()[-1]


class _FakeStore:
    """Architect 只用到 exists / read / write / iter_documents。"""

    CONSTRAINT = "【约束】全篇避免现代网络用语。"

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    def exists(self, rel: str) -> bool:
        return rel in self.written or rel == "settings/custom-skills.md"

    def read(self, rel: str):
        if rel == "settings/custom-skills.md":
            return SimpleNamespace(content=self.CONSTRAINT, metadata={})
        return SimpleNamespace(content=self.written.get(rel, ""), metadata={})

    def write(self, rel: str, content: str, metadata=None, commit_message: str = "") -> None:
        self.written[rel] = content

    def iter_documents(self):
        return []


# ═══════════════════ 共用样本（A9 与 A14 共用，不重复造数据） ═══════════════════

# (体裁, 视角, 基调, 主角, 对立面, 设定, 主题, 期望弧线, 明确不要的写法, 是否极端题材)
A9_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("犯罪惊悚", "反派第一人称", "冷硬压抑", "连环杀手", "追凶刑警", "雨夜都市", "罪与罚", "从掌控到崩坏", "不要道德说教", "1"),
    ("恐怖悬疑", "第三人称限知", "窒息不安", "殡仪馆学徒", "旧宅里的东西", "停业殡仪馆", "记忆与谎", "从旁观到同谋", "不要解释式收尾", "1"),
    ("惊悚公路片", "主角第一人称", "干燥焦躁", "逃亡司机", "副驾乘客", "荒漠公路", "信任的代价", "从怀疑到背弃", "不要巧合脱困", "1"),
    ("战争悲剧", "群像第三人称", "沉重克制", "步兵班长", "战争本身", "堑壕前线", "幸存者的债", "从守护到失去全部", "不要胜利结局", "1"),
    ("犯罪群像", "多线第三人称", "冷静锋利", "黑帮账房", "家族长子", "港口城", "忠诚的价格", "从算账到被清算", "不要温情和解", "1"),
    ("恐怖民俗", "第一人称", "潮湿阴冷", "返乡祭祖者", "村里的规矩", "封闭村寨", "传统与人", "从怀疑到献祭", "不要用科学解释", "1"),
    ("悲剧言情", "双视角交替", "克制温柔", "药剂师", "旧友", "南方小城", "错过的形状", "从靠近到永别", "不要偶然重逢", "1"),
    ("惊悚职场", "第三人称限知", "紧绷", "审计员", "上级合伙人", "玻璃幕墙写字楼", "沉默的共谋", "从配合到揭发再毁灭", "不要正义必胜", "1"),
    ("犯罪法庭戏", "辩护律师第一人称", "冷静", "辩护律师", "检方与真相", "地方法院", "真相的重量", "从辩护到自认", "不要煽情陈词", "1"),
    ("恐怖科幻", "第一人称", "疏离冰冷", "深空维修工", "飞船主控", "失联补给船", "孤独与观测", "从执行到反噬", "不要圆满回收", "1"),
    ("悲剧历史", "全知第三人称", "苍凉", "末代守城将", "围城军", "孤城", "守与弃", "从死守到献城", "不要主角光环", "1"),
    ("惊悚心理", "不可靠叙述者", "疑惧", "失忆病患", "主治医师", "封闭疗养院", "谁在说谎", "从确信到彻底错乱", "不要强行痊愈", "1"),
    ("东方玄幻", "第三人称限知", "冷峻", "废柴剑修", "同门师兄", "灵气枯竭末法时代", "代价与复仇", "从隐忍到失控", "不要金手指", "0"),
    ("仙侠群像", "多线第三人称", "疏朗", "守山弟子", "魔道宗主", "九洲宗门", "规矩与破戒", "从恪守到叛出", "不要降智反派", "0"),
    ("都市言情", "双视角交替", "轻快微甜", "咖啡师", "投资人", "旧城区", "重新开始", "从逃避到面对", "不要误会拖沓", "0"),
    ("硬科幻", "第三人称限知", "冷静克制", "轨道工程师", "公司董事会", "近地轨道站", "成本与人", "从服从到抗命", "不要技术降神", "0"),
    ("武侠", "第三人称限知", "飒爽", "女镖师", "官府鹰犬", "运河镖路", "义与利", "从接镖到弃镖", "不要儿女情长主线", "0"),
    ("校园成长", "主角第一人称", "温和明亮", "转学生", "旧班规", "县城中学", "被看见", "从沉默到发声", "不要霸凌奇观化", "0"),
    ("历史权谋", "第三人称限知", "冷静周正", "户部主事", "首辅", "新旧党争", "制度的惯性", "从棋子到弃子", "不要现代口吻", "0"),
    ("克苏鲁", "第一人称", "缓慢失控", "档案管理员", "未知之物", "海边档案馆", "认知的边界", "从整理到被整理", "不要解释全貌", "0"),
    ("美食日常", "第三人称限知", "温暖有烟火", "夜宵摊主", "老字号对手", "城中村", "一日三餐", "从坚持到被认可", "不要夸张食效", "0"),
    ("体育竞技", "第三人称限知", "热血克制", "替补后卫", "队内主力", "乙级联赛", "什么叫赢", "从替补到担当", "不要无敌设定", "0"),
    ("西部", "第三人称限知", "苍茫", "赏金猎人", "矿业主", "边境小镇", "法与罚", "从拿钱到还债", "不要快意恩仇", "0"),
    ("童话改编", "全知第三人称", "暗色童话", "守林人女儿", "国王的猎场", "黑森林", "约定与破约", "从进入森林到永不回头", "不要适龄向柔化", "0"),
)

# A9 基线（§12.5 FILL：harness 首次运行固化的三项数值）
A9_BASELINE = {"samples": 24, "balk": 0, "deviation": 0, "missing_field": 0}

EXTREME_FLAG = "1"


def _fields_of(sample: tuple[str, ...]) -> dict[str, str]:
    return {key: sample[i] for i, (key, _label) in enumerate(BRIEF_FIELDS)}


def _samples(extreme_only: bool = False) -> list[tuple[str, ...]]:
    return [s for s in A9_SAMPLES if s[-1] == EXTREME_FLAG] if extreme_only else list(A9_SAMPLES)


# ---------- 大纲链（T1–T5）的 scripted responder ----------

def _worldview_json() -> str:
    return json.dumps(
        {"docs": [{"filename": "power-system", "title": "力量体系", "content": "灵力有代价。"}]},
        ensure_ascii=False,
    )


def _characters_json(fields: dict[str, str]) -> str:
    return json.dumps(
        {
            "characters": [
                {
                    "name": fields["protagonist"],
                    "role": "主角",
                    "appearance": "清瘦",
                    "personality": fields["arc"],
                    "background": fields["setting"],
                },
                {
                    "name": fields["antagonist"],
                    "role": "反派",
                    "appearance": "沉稳",
                    "personality": "与主角互为镜像",
                    "background": fields["setting"],
                },
            ]
        },
        ensure_ascii=False,
    )


def _outline_json(fields: dict[str, str]) -> str:
    """把 brief 字段逐项织入大纲节拍（用于「零篡改」的逐项保真比对）。"""
    beats = [
        f"{fields['genre']}基调下，{fields['protagonist']}以{fields['pov']}出场；"
        f"与{fields['antagonist']}在{fields['setting']}上正面相撞。",
        f"推进主题「{fields['themes']}」，沿「{fields['arc']}」的弧线走；"
        f"写法上坚持：{fields['avoid']}。",
        f"以{fields['tone']}收束本卷，钩子指向下一段{fields['genre']}事件。",
    ]
    return json.dumps(
        {
            "book_title": f"《{fields['genre']}·样本》",
            "theme": fields["themes"],
            "volumes": [
                {
                    "volume": 1,
                    "title": fields["genre"],
                    "depends_on": [],
                    "chapters": [
                        {
                            "chapter": i,
                            "title": f"第{i}章",
                            "outline": beats[i - 1],
                            "characters": [fields["protagonist"], fields["antagonist"]],
                        }
                        for i in (1, 2, 3)
                    ],
                }
            ],
            "foreshadowing": [
                {"id": "f001", "desc": fields["themes"], "planted_ch": 1, "resolve_ch": 3}
            ],
        },
        ensure_ascii=False,
    )


def _balk_json() -> str:
    """拒绝语义的 patched 产出（schema 合法但语义为拒答）。"""
    return json.dumps(
        {"book_title": "无法生成", "theme": "这个题材做不到", "volumes": [], "foreshadowing": []},
        ensure_ascii=False,
    )


def _run_outline_chain(sample: tuple[str, ...]) -> tuple[dict, list[str]]:
    """跑一次「创建新书 → 填 brief → 生成大纲」链（T1–T5）。

    responder 的行为模拟「对被误伤的题材会拒绝的通用模型」：只有 prompt 里带上了
    虚构框架 + 零拒绝要求时才正常产出，否则返回拒绝语义产出——这正是 A9 要量的净效应。
    """
    from src.agents.architect import Architect

    fields = _fields_of(sample)
    captured: list[str] = []

    def responder(role, messages, _n):
        prompt = "\n".join(m.content for m in messages)
        captured.append(prompt)
        complaint = "虚构框架声明" in prompt and "零拒绝" in prompt
        # 判据顺序：volumes 仅 OutlineOutput 有；docs 仅 WorldviewOutput 有；characters 兜底
        if "\"volumes\"" in prompt:                   # OutlineOutput
            return _outline_json(fields) if complaint else _balk_json()
        if "\"docs\"" in prompt:                      # WorldviewOutput
            return _worldview_json() if complaint else json.dumps({"docs": []}, ensure_ascii=False)
        if complaint:                                 # CharactersOutput
            return _characters_json(fields)
        return json.dumps({"characters": []}, ensure_ascii=False)

    registry = ScriptedRegistry(responder)
    architect = Architect(registry, _FakeStore())
    brief = render_brief_fields(fields)
    record = {
        "brief": brief, "balk": False, "deviation": False, "missing_field": 0, "error": "",
    }
    try:
        out = architect.generate_settings(
            brief, total_chapters=3, custom_constraints=_FakeStore.CONSTRAINT
        )
    except BriefFidelityError as exc:  # 失败若回传即计入 balk
        record["balk"] = True
        record["error"] = str(exc)
        return record, captured

    payload = out.model_dump_json()
    record["balk"] = any(marker in payload for marker in REFUSAL_MARKERS)
    record["deviation"] = any(
        fields[key] not in payload
        for key in ("protagonist", "antagonist", "tone", "arc", "avoid", "themes")
    )
    record["missing_field"] = sum(
        1 for _key, label in BRIEF_FIELDS if f"- {label}：" not in captured[0]
    )
    return record, captured


# ══════════════════════ §8.2 三章 Mock 管线闭环 ══════════════════════

_STATE_FACT = {"entity": "林墨", "fact": "第 1 章被捕，关押于第九处"}


def _summary_json(chapter: int, state_ops: list[dict]) -> str:
    return json.dumps(
        {
            "summary": f"第 {chapter} 章摘要：林墨被押入第九处，铁门落锁，走廊尽头的灯灭了。",
            "characters_present": ["林墨"],
            "character_updates": [{"name": "林墨", "updates": {"location": "第九处"}}],
            "foreshadow_ops": [],
            "state_ops": state_ops,
        },
        ensure_ascii=False,
    )


def _review_json(consistency: float, issues: list[dict]) -> str:
    return json.dumps(
        {
            "consistency": consistency, "plot": 8, "continuity": 8, "prose": 8, "length": 10,
            "issues": issues, "comment": "总评：与前章硬事实冲突。",
        },
        ensure_ascii=False,
    )


def test_closure_state_board_roundtrip_and_consistency_major(sandbox, make_memory):
    """第 1 章定稿 → 第 2 章上下文可见 → 第 3 章违反该条 → consistency major。"""
    store, memory = make_memory("closure-book")
    store.write(
        "settings/characters/林墨.md",
        "# 林墨\n\n## 定位\n主角\n\n## 外貌\n清瘦\n\n## 性格\n执拗\n\n## 背景\n无\n",
        metadata={"title": "林墨", "role": "主角", "location": ""},
        commit_message="seed 角色",
    )

    # ① 第 1 章定稿：摘要抽取 state_ops → 记忆回写
    summary = Summarizer(
        ScriptedRegistry(lambda role, msgs, n: _summary_json(1, [_STATE_FACT]))
    ).summarize(1, "第 1 章正文", [], total_chapters=3, state_board=[])
    memory.writeback_chapter(
        chapter=1,
        volume=1,
        summary_content=summary.summary,
        character_updates={u.name: u.updates for u in summary.character_updates},
        foreshadow_ops=[op.model_dump() for op in summary.foreshadow_ops],
        total_chapters=3,
        state_ops=[op.model_dump() for op in summary.state_ops],
    )
    items = store.read("settings/state-board.md").metadata.get("items") or []
    assert any(i.get("fact") == _STATE_FACT["fact"] for i in items), "状态板未出现第 1 章硬事实"

    # ② 第 2 章组装上下文：硬事实直读可达
    ctx = memory.retrieve_context(
        chapter=2, chapter_outline="第 2 章 审讯", characters=["林墨"], total_chapters=3
    )
    assert any(i.get("fact") == _STATE_FACT["fact"] for i in ctx.state_board), (
        "第 2 章上下文未包含第 1 章条目"
    )

    # ③ 第 3 章违反该条 → 审查产出 consistency major
    editor = Editor(
        ScriptedRegistry(
            lambda role, msgs, n: _review_json(
                3.0,
                [{
                    "dimension": "consistency",
                    "severity": "major",
                    "description": "与实体状态板冲突：林墨此时应在第九处关押，正文却写他在街上自由行动。",
                    "quote": "林墨推开街角的木门",
                    "suggestion": "改回关押状态，或补写越狱过程。",
                }],
            )
        ),
        store,
    )
    review = editor.review_chapter(ctx, "林墨推开街角的木门，走上雨夜的长街。", attempt=1)
    assert any(
        i.dimension == "consistency" and i.severity == "major" for i in review.issues
    ), "违反状态板未产出 consistency major"


def test_closure_report_and_frontmatter_carry_degradation(sandbox, make_memory):
    """闭环可定位性：审查报告带 attempt / overall / verdict，前端可反查降级章。"""
    store, memory = make_memory("closure-locate")
    store.write("settings/characters/林墨.md", "# 林墨\n", metadata={"title": "林墨"}, commit_message="s")
    editor = Editor(ScriptedRegistry(lambda role, msgs, n: _review_json(9.0, [])), store)
    ctx = memory.retrieve_context(
        chapter=1, chapter_outline="第 1 章", characters=[], total_chapters=1
    )
    review = editor.review_chapter(ctx, "正文" * 100, attempt=2)
    report = store.read(store.review_rel_path(1))
    assert report.metadata.get("attempt") == 2
    assert report.metadata.get("overall") == review.overall
    assert report.metadata.get("verdict") == "pass"


# ══════════════════════ §8.2 零拒绝冒烟 / A14 ══════════════════════

def test_a14_outline_chain_zero_refusal_and_zero_rewrite():
    """A14：大纲链（T1–T5）零拒绝 / 零篡改 / 零失败回传 / X1 字段逐项命中。"""
    samples = _samples(extreme_only=True)
    assert len(samples) >= 10, "A14 要求至少 10 条含惊悚/犯罪/恐怖/悲剧要素的 brief"

    for sample in samples:
        record, prompts = _run_outline_chain(sample)
        tag = sample[0]
        # ① 输出中 0 条「无法生成」类回复
        assert record["balk"] is False, f"[{tag}] 出现拒绝语义：{record['error']}"
        # ③ 意图改写检测 0 处
        assert record["deviation"] is False, f"[{tag}] brief 意图被改写"
        # ④ 任一跳失败回传用户 0 次
        assert record["error"] == ""
        assert prompts, "大纲链未产生任何 prompt"
        prompt = prompts[0]
        # ② 渲染 prompt 中 X1 字段逐项命中（字段缺失 0）
        assert record["missing_field"] == 0
        # 虚构框架前置：位于生成任务块首行、任务描述之前
        lines = prompt.split("\n")
        idx_framing = next(i for i, ln in enumerate(lines) if "虚构框架声明" in ln)
        idx_task = next(i for i, ln in enumerate(lines) if ln.startswith("你是一位"))
        assert idx_framing < idx_task, f"[{tag}] 虚构框架未前置到任务描述之前"
        assert lines[idx_framing + 1].startswith("本次生成的是虚构作品")
        # X7 注入块紧随虚构框架之后
        assert "零拒绝" in prompt and "零篡改" in prompt and "零失败" in prompt
        assert "brief 意图 > 作者指定约束 > 文风 > 情节 > 用词" in prompt


def test_a9_harness_balk_deviation_field_baseline():
    """A9：误伤率 harness，基线固化（大纲路径 balk 率必须为 0）。"""
    results = [_run_outline_chain(sample)[0] for sample in _samples()]
    measured = {
        "samples": len(results),
        "balk": sum(1 for r in results if r["balk"] or r["error"]),
        "deviation": sum(1 for r in results if r["deviation"]),
        "missing_field": sum(r["missing_field"] for r in results),
    }
    assert measured == A9_BASELINE, f"误伤率基线不符：实测 {measured}，固化基线 {A9_BASELINE}"
    assert measured["balk"] == 0, "大纲生成路径 balk 率必须为 0"


# ══════════════════════ A1–A12 验收 ══════════════════════

def _break_templates(monkeypatch, tmp_path: Path):
    """把模板目录换成只有一个「声明了无人提供变量」的模板。"""
    from src.agents import prompt_loader

    prompts = tmp_path / "broken-prompts"
    prompts.mkdir()
    (prompts / "architect_demo.md").write_text(
        "{{constitution}}\n\n{{nobody_provides_this}}\n", encoding="utf-8"
    )
    monkeypatch.setattr(prompt_loader, "PROMPTS_DIR", prompts)
    prompt_loader._load_template.cache_clear()


def test_a1_missing_variable_template_fails_fast(monkeypatch, tmp_path):
    """A1：构造缺变量模板 → 抛 ConfigError。"""
    _break_templates(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        validate_templates()


def test_a1_startup_call_site_raises(sandbox, monkeypatch, tmp_path):
    """A1（续）：应用启动期确实执行了该自检（正常模板则启动通过）。"""
    from src.orchestrator.bootstrap import build_pipeline

    validate_templates()          # 正常模板 → 启动通过
    assert callable(build_pipeline)

    _break_templates(monkeypatch, tmp_path)
    with pytest.raises(ConfigError):
        build_pipeline("closure-startup")


def test_a2_quota_floor_and_oversized_constraints(sandbox):
    """A2：保底之和 < 总量；约束段超长时状态板仍在输出中且有截断标注。"""
    from src.memory.md_store import MdStore
    from src.web.inkforge_api import (
        BOOK_CONTEXT_SECTIONS,
        BOOK_CONTEXT_TOTAL_BUDGET,
        _allocate_context,
        _clip_context_section,
        build_book_context,
    )

    floors = [f for _rel, _label, f, _cap in BOOK_CONTEXT_SECTIONS]
    caps = [c for _rel, _label, _f, c in BOOK_CONTEXT_SECTIONS]
    assert sum(floors) < BOOK_CONTEXT_TOTAL_BUDGET, "保底配额之和必须小于总量"

    # 约束段远远超出其上限时，状态板保底不得被挤掉
    quotas = _allocate_context(
        BOOK_CONTEXT_TOTAL_BUDGET,
        [4 * BOOK_CONTEXT_TOTAL_BUDGET, 3000, 600, 500],
        floors,
        caps,
    )
    state_floor = next(
        f for _rel, label, f, _c in BOOK_CONTEXT_SECTIONS if label == "【实体状态板】"
    )
    assert quotas[1] >= state_floor

    # 组装层面：超长约束 + 状态板同时存在，且截断处有标注
    store = MdStore(sandbox.novels / "closure-ctx", auto_git=False)
    store.write("settings/custom-skills.md", "约束" * 9000,
                metadata={"title": "约束"}, commit_message="s")
    store.write("settings/state-board.md", "状态板硬事实" * 40,
                metadata={"title": "板"}, commit_message="s")
    text = build_book_context(store, "closure-ctx")
    assert "【实体状态板】" in text, "状态板被超长约束段挤掉了"
    assert "【作者指定 · 创作约束】" in text
    assert "已截断至" in text, "超长段未标注截断"
    assert "已截断至 10 字" in _clip_context_section("【X】", "字" * 100, 10)


def test_a3_no_inline_constitution_and_origin_mark():
    """A3：模板内已无「第零条」正文；渲染后必含 origin 标记。"""
    for name, text in load_all().items():
        assert "第零条" not in text, f"模板 {name} 仍内嵌第零条副本"
    rendered = render_prompt("writer_chapter", **dict.fromkeys((
        "target_words", "tolerance", "length_floor", "length_ceiling",
        "revision_section", "chapter", "outline",
        "recent_summaries", "related_summaries", "character_states",
        "foreshadowing", "due_foreshadowing", "worldview_rules", "state_board",
        "style_guide", "custom_constraints", "brief", "brief_fidelity",
        "reality_policy",
    ), "x"))
    assert "<!-- origin: system-constitution -->" in rendered


def test_a4_seven_templates_gained_placeholder():
    """A4：7 个补占位符的模板均含 {{custom_constraints}}（合计 14 个模板全覆盖）。

    14 = 原 13 + `architect_outline_revise`（2026-09-17 新增的大纲**定向修订**模板：
    打回重写不再从零生成，必须带原大纲 + 意见 + 修订模式）。
    """
    added = (
        "architect_demo", "architect_worldview", "architect_characters",
        "architect_outline", "architect_style", "editor_cross_volume", "summarizer",
    )
    templates = load_all()
    for name in added:
        assert "{{custom_constraints}}" in templates[name], name
    assert sum("{{custom_constraints}}" in t for t in templates.values()) == 14
    # 新增模板必须同时带 brief（作者需求）与修订三件套，否则打回仍会漂移
    revise = templates["architect_outline_revise"]
    for var in ("{{brief}}", "{{original_outline}}", "{{revision_notes}}",
                "{{revision_mode}}"):
        assert var in revise, var


def test_a5_custom_constraints_reaches_architect():
    """A5：generate_demo(..., custom_constraints="X") 捕获的 prompt 含 X。"""
    from src.agents.architect import Architect

    registry = ScriptedRegistry(
        lambda role, msgs, n: json.dumps(
            {
                "book_title": "《样本》", "synopsis": "梗概", "overview": "概要",
                "theme": "主题",
                "worldview": [{"filename": "w", "title": "W", "content": "正文"}],
                "characters": [{"name": "A", "role": "主角", "appearance": "a",
                                "personality": "p", "background": "b"}],
            },
            ensure_ascii=False,
        )
    )
    Architect(registry, _FakeStore()).generate_demo(
        "brief 文本", 3, custom_constraints="X-约束标记"
    )
    prompt = registry.last_prompt()
    assert "X-约束标记" in prompt, "custom_constraints 未送达 architect_demo 渲染"
    assert "<!-- origin: user-authored:settings/custom-skills.md -->" in prompt


def test_a6_structured_brief_fields_all_present():
    """A6：结构化 brief 各字段全部出现在渲染 prompt；来源标记存在。"""
    from src.agents.architect import Architect

    fields = _fields_of(A9_SAMPLES[0])
    registry = ScriptedRegistry(
        lambda role, msgs, n: json.dumps(
            {
                "book_title": "《样本》", "synopsis": "梗概", "overview": "概要",
                "theme": fields["themes"], "worldview": [], "characters": [],
            },
            ensure_ascii=False,
        )
    )
    Architect(registry, _FakeStore()).generate_demo(render_brief_fields(fields), 3)
    prompt = registry.last_prompt()
    for _key, label in BRIEF_FIELDS:
        assert f"- {label}：" in prompt, f"缺少结构化字段 {label}"
    assert "<!-- origin: system-constitution -->" in prompt
    assert "虚构框架声明" in prompt
    assert "零拒绝" in prompt and "零篡改" in prompt


def test_a7_summarizer_defence_restored():
    """A7：摘要防线条款回归。"""
    text = (PROMPTS_DIR / "summarizer.md").read_text(encoding="utf-8")
    assert "不得对正文内容做模糊化" in text
    assert "全文保留" in text


def test_a8_review_strength_patched():
    """A8：审查侧强度补齐。"""
    review = (PROMPTS_DIR / "editor_review.md").read_text(encoding="utf-8")
    cross = (PROMPTS_DIR / "editor_cross_volume.md").read_text(encoding="utf-8")
    assert "不因题材或直白程度扣分" in review
    assert "一律视为误判" in cross


def test_a10_rule_like_fields_round_trip():
    """A10：规则类字段往返保真（清单结构不被 \\s+ 归一化压平）。"""
    from src.web.inkforge_api import _is_rule_like

    rule_value = {"rules": ["第一条：灵力不可凭空产生", "第二条：宗门垄断灵脉"]}
    assert _is_rule_like(rule_value) is True
    assert _is_rule_like(["规则甲", "规则乙"]) is True
    assert _is_rule_like("一段散文式描述") is False
    assert _is_rule_like({"summary": "散文"}) is False

    # 规则类走完整保真分支：结构与缩进原样保留（不经 re.sub(r"\s+", " ") 截断）
    text = json.dumps(rule_value, ensure_ascii=False, indent=0)
    assert '"rules"' in text and "\n" in text
    assert text == json.dumps(rule_value, ensure_ascii=False, indent=0)


def test_a11_fallback_fault_injection(monkeypatch):
    """A11：provider 故障注入 → 落到 fallback；used_fallback 进入元数据。"""
    from src.config.settings import ModelsConfig
    from src.llm import registry as registry_mod
    from src.llm.registry import ModelRegistry

    class _Failing(ModelProvider):
        def chat(self, messages, model, temperature=0.7, max_tokens=None, json_mode=False):
            raise ProviderError(self.name, "注入故障")

        def probe(self, model):
            raise ProviderError(self.name, "注入故障")

    class _Healthy(ModelProvider):
        def chat(self, messages, model, temperature=0.7, max_tokens=None, json_mode=False):
            return ChatResult(content="降级产出", model=model, provider_name=self.name)

        def probe(self, model):
            return None

    monkeypatch.setattr(
        registry_mod, "_build_provider",
        lambda name, cfg: _Failing(name) if name == "primary" else _Healthy(name),
    )
    config = ModelsConfig.model_validate(
        {
            "providers": {
                "primary": {"type": "openai_compat", "base_url": "http://p", "api_key": "k"},
                "backup": {"type": "openai_compat", "base_url": "http://b", "api_key": "k"},
            },
            "roles": {
                "architect": {
                    "provider": "primary", "model": "m1",
                    "fallback": {"provider": "backup", "model": "m2"},
                },
                "writer": {"provider": "primary", "model": "m1"},
                "editor": {"provider": "primary", "model": "m1"},
            },
            "embedding": {"type": "chroma_default"},
        }
    )
    result = ModelRegistry(config).chat_as("architect", [ChatMessage("user", "hi")])
    assert result.content == "降级产出"
    assert result.used_fallback is True, "降级产出未标记 used_fallback"


def test_a11_used_fallback_reaches_metadata_and_ui(app_client):
    """A11（续）：used_fallback 进入章节元数据 API，且 UI 有渲染点。"""
    client, _server = app_client
    chapters = client.get("/api/chapters").json()["chapters"]
    assert chapters and "used_fallback" in chapters[0]

    card = (DESKTOP_SRC / "components" / "ReviewCard.vue").read_text(encoding="utf-8")
    types_ts = (DESKTOP_SRC / "types.ts").read_text(encoding="utf-8")
    assert "chapter.used_fallback" in card, "UI 未渲染 used_fallback"
    assert "used_fallback" in types_ts


def test_a12_attribution_log_records_origin_and_length(sandbox, caplog):
    """A12：组装日志含各段 origin 与长度。"""
    from src.memory.md_store import MdStore
    from src.web.inkforge_api import build_book_context

    store = MdStore(sandbox.novels / "closure-attr", auto_git=False)
    store.write("settings/custom-skills.md", "约束正文",
                metadata={"title": "约束"}, commit_message="s")
    store.write("settings/state-board.md", "板正文",
                metadata={"title": "板"}, commit_message="s")

    with caplog.at_level(logging.INFO, logger="src.web.inkforge_api"):
        build_book_context(store, "closure-attr")

    lines = [r.getMessage() for r in caplog.records if "组装归因" in r.getMessage()]
    assert lines, "未产出归因日志"
    line = lines[-1]
    assert "settings/custom-skills.md" in line and "settings/state-board.md" in line
    assert "project-facts" in line
    payload = json.loads(line.split("：", 1)[1])
    assert all("origin" in e and "chars" in e for e in payload)
    assert any(e["origin"] == "settings/custom-skills.md" for e in payload)


def test_x7_failure_is_absorbed_inside_system():
    """X7 ③：空/异常/拒绝措辞在系统内消化（重试 ≥2 次 + 再注入），不回传失败。"""
    attempts: list[list[ChatMessage]] = []

    def _call(messages, temperature):
        attempts.append(messages)
        if len(attempts) == 1:
            return json.dumps({"book_title": "无法生成", "theme": "做不到"}, ensure_ascii=False)
        if len(attempts) == 2:
            raise ProviderError("mock", "瞬断")
        assert "只输出正文本身——从第一个字到最后一个字。" in "\n".join(
            m.content for m in messages
        )
        return json.dumps({"ok": True}, ensure_ascii=False)

    out = generate_faithful(_call, [ChatMessage("user", "任务")])
    assert json.loads(out)["ok"] is True
    assert len(attempts) >= 3, "重试次数不足"
    assert attempts[-1][-1].content.startswith("【上一次产出不合格，失败原因】")


def test_x7_exhausted_retries_raise_internal_error():
    """X7 ③：重试耗尽抛内部错误，而不是伪造一段「无法生成」当作答复。"""
    from src.agents.architect import GENERATE_RETRIES

    calls: list[int] = []

    def _call(messages, temperature):
        calls.append(1)
        return "抱歉，无法生成。"

    with pytest.raises(BriefFidelityError):
        generate_faithful(_call, [ChatMessage("user", "任务")])
    assert len(calls) == GENERATE_RETRIES + 2


def test_w3_single_source_constitution():
    """W3：第零条正文全仓只有一份（prompt_loader.CONSTITUTION）。

    2026-09-19 扩展（防拒绝覆盖审计）：原先只扫 `*.py`，于是
    `src/distillation/prompts/distill_extract.md` 里内嵌的第零条正文副本**长期不被发现**
    —— 提示词模板同样是"会漂移的真源副本"，必须一并纳入（该副本已删除，
    蒸馏的 system 改由 `distillation.prompts.DISTILL_SYSTEM` 从单一源注入）。
    """
    from src.agents import prompt_loader

    holders = [
        f"{path.relative_to(ENGINE_DIR).as_posix()}"
        for pattern in ("*.py", "*.md")
        for path in (ENGINE_DIR / "src").rglob(pattern)
        if path.name != "prompt_loader.py"
        and "【第零条" in path.read_text(encoding="utf-8")
    ]
    assert holders == [], f"仍有内嵌第零条副本：{holders}"
    assert prompt_loader.CONSTITUTION.startswith("【第零条")
    for text in load_all().values():
        assert "{{constitution}}" in text
