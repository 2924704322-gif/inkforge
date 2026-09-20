"""字数门禁（非对称）与「打回重写」链路的回归测试。

对应用户实测的三类问题：
  ① 多维评分给出的修改建议复制到「打回重写」后，重写稿仍没按建议改；
  ② 要求至少 5000 字，实际输出远小于该数；
  ③ 生成之前想按章自定义预期字数。

这里覆盖的是**路径级**行为（互动创作 / 自由创作两条路走同一套门禁），
不依赖真实 LLM：Writer/Editor 用脚本化替身，只验证"批数、指令、落盘、参数贯通"。
"""

from __future__ import annotations

from src.agents.editor import Editor
from src.agents.schemas import ReviewIssue, ReviewOutput
from src.agents.writer import (
    human_revision_notes,
    length_assessment,
    length_revision_note,
)
from src.config.app_config import GenerationConfig
from src.orchestrator.interactive import InteractiveRunner
from src.orchestrator.scheduler import generation_config
from src.orchestrator.state import resolve_chapter_target

# ══════════════════════ 替身 ══════════════════════

class _FakeWriter:
    """脚本化 Writer：按预设字数逐轮吐稿，并记录每次收到的重写指令。"""

    def __init__(self, lengths: list[int]):
        self._lengths = list(lengths)
        self._n = 0
        self._target_words = 3000
        self.calls: list[dict] = []

    @property
    def target_words(self) -> int:
        return self._target_words

    def write_chapter(self, ctx, revision_notes=None, previous_text=None,
                      target_words_override=None):
        self.calls.append({
            "revision_notes": revision_notes,
            "previous_text": previous_text,
            "target_words": target_words_override,
        })
        n = self._lengths[min(self._n, len(self._lengths) - 1)]
        self._n += 1
        from src.llm.base import ChatResult

        return ChatResult(content="正" * n, model="fake", provider_name="fake")


class _FakeEditor:
    """脚本化 Editor：只记调用，不真的调模型。"""

    def __init__(self, score: float = 9.0):
        self._score = score
        self.calls: list[dict] = []

    def review_chapter(self, ctx, chapter_text, attempt, target_words=None,
                       length_bounds=None):
        self.calls.append({"attempt": attempt, "target_words": target_words,
                           "length_bounds": length_bounds})
        return ReviewOutput(consistency=self._score, plot=self._score,
                            continuity=self._score, prose=self._score)


class _FakeMemory:
    def sync_changed(self) -> None:
        return None


class _FakeStore:
    def __init__(self):
        self.writes: list[dict] = []

    def chapter_rel_path(self, volume: int, chapter: int) -> str:
        return f"chapters/vol-{volume:02d}/ch-{chapter:03d}.md"

    def exists(self, rel: str) -> bool:
        return False

    def write(self, rel, content, metadata=None, commit_message="") -> None:
        self.writes.append({"rel": rel, "len": len(content), "metadata": metadata or {}})


class _FakePipe:
    def __init__(self, writer, editor):
        self.writer = writer
        self.editor = editor
        self.store = _FakeStore()
        self.memory = _FakeMemory()
        self.gen_config = GenerationConfig()
        # InteractiveRunner 的构造函数会用它装配 Plotter（本测试不触发出卡）
        self.registry = object()


def _runner(writer, editor, tmp_path) -> InteractiveRunner:
    r = InteractiveRunner(_FakePipe(writer, editor))
    # 上下文组装会真的去检索记忆：这里只关心门禁与指令链路，直接短路。
    r._chapter_context = lambda chapter, plan: object()  # noqa: SLF001
    return r


PLAN = {"title": "测试章", "outline": "本章唯一事件：对峙。", "characters": []}


# ══════════════════════ ① 字数门禁（问题②） ══════════════════════

def test_interactive_short_draft_is_rewritten_until_in_band(tmp_path):
    """互动路径：初稿远低于下限 → 自动带差额打回重写，直到落进可接受区间。

    由来（用户实测）：互动路径原先"写一遍、超差只打一条日志就算了"，
    于是"要求 5000 字、实际远小于"没有任何补救动作。
    """
    writer = _FakeWriter([2000, 3500, 5200])
    editor = _FakeEditor()
    runner = _runner(writer, editor, tmp_path)

    out = runner.write_chapter(1, PLAN, target_words=5000)

    assert len(writer.calls) == 3, "欠字数没有触发多轮重写"
    assert out["words"] == 5200
    assert out["length_ok"] is True
    # 每一轮都按同一个目标写（不许中途换数）
    assert {c["target_words"] for c in writer.calls} == {5000}
    # 第 2、3 轮必须带"补字数"的硬指令，且给出确切差额
    assert "字数修正" in writer.calls[1]["revision_notes"]
    assert "4500" in writer.calls[1]["revision_notes"]
    # 重写必须携带上一稿（否则"定向修订"无从落地）
    assert writer.calls[1]["previous_text"] == "正" * 2000


def test_interactive_stops_at_retry_cap_and_reports(tmp_path):
    """重写轮数有上限：到顶仍不达标就落盘最新稿并明确告知（不无限烧钱）。"""
    writer = _FakeWriter([1000])           # 永远只有 1000 字
    runner = _runner(writer, _FakeEditor(), tmp_path)

    out = runner.write_chapter(1, PLAN, target_words=5000)

    gen = GenerationConfig()
    assert len(writer.calls) == gen.max_length_retries + 1
    assert out["length_ok"] is False
    assert out["words"] == 1000
    assert out["target_words"] == 5000


def test_interactive_overshoot_within_ceiling_is_not_rewritten(tmp_path):
    """上浮 ≤2000 字视为合格：内容完整性优先，不再因为"超出目标"而重写。

    这是与旧口径最关键的差别：旧口径 5000 字章只容许 5750，多写就要压缩。
    """
    writer = _FakeWriter([5100, 6900])     # 首轮 5100 已合格
    runner = _runner(writer, _FakeEditor(), tmp_path)

    out = runner.write_chapter(1, PLAN, target_words=5000)

    assert len(writer.calls) == 1, "区间内的稿子不该被重写"
    assert out["length_ok"] is True
    assert out["length_floor"] == 4500 and out["length_ceiling"] == 7000


def test_interactive_reresolves_target_from_frontmatter(tmp_path):
    """打回重写不传目标字数时，必须沿用**本章已落盘的目标**。

    旧行为：`target_words=None` 直接落到 Writer 实例默认值 → 用户设的 5000 字
    在第一次打回后静默变成另一个数（用户不可能察觉）。
    """
    writer = _FakeWriter([5000])
    runner = _runner(writer, _FakeEditor(), tmp_path)
    # 本章已有一稿（6200 字目标落盘）
    runner.draft_snapshot = lambda chapter: {  # noqa: SLF001
        "draft_text": "旧稿", "attempt": 1, "title": "t", "review": None,
        "words": 3000, "target_words": 6200,
        "length_floor": 5700, "length_ceiling": 8200, "length_deviation": -3200,
    }

    out = runner.write_chapter(1, PLAN, feedback="把对峙写得更狠", revision_mode="targeted")

    assert writer.calls[0]["target_words"] == 6200, "打回后目标字数被静默重置"
    assert "把对峙写得更狠" in writer.calls[0]["revision_notes"]
    assert writer.calls[0]["previous_text"] == "旧稿"
    assert out["target_words"] == 6200


def test_interactive_rewrite_keeps_user_feedback_in_later_rounds(tmp_path):
    """字数打回的第 2 轮仍要带上**原始人工意见**，不能退化成"只补字数"。"""
    writer = _FakeWriter([1500, 1500])
    runner = _runner(writer, _FakeEditor(), tmp_path)
    runner.draft_snapshot = lambda chapter: {  # noqa: SLF001
        "draft_text": "旧稿", "attempt": 1, "title": "t", "review": None,
        "words": 1500, "target_words": 5000,
        "length_floor": 4500, "length_ceiling": 7000, "length_deviation": -3500,
    }

    runner.write_chapter(1, PLAN, feedback="女主必须在场并说出台词",
                         revision_mode="targeted", target_words=5000)

    assert len(writer.calls) >= 2
    for call in writer.calls:
        assert "女主必须在场并说出台词" in call["revision_notes"], \
            "后续轮次丢了人工打回意见"


def test_interactive_whole_rewrite_drops_previous_draft(tmp_path):
    """选「整章重写」时不携带上一稿（大改意见需要重起一章，而非在旧稿上打补丁）。"""
    writer = _FakeWriter([5000])
    runner = _runner(writer, _FakeEditor(), tmp_path)
    runner.draft_snapshot = lambda chapter: {  # noqa: SLF001
        "draft_text": "旧稿", "attempt": 1, "title": "t", "review": None,
        "words": 3000, "target_words": 5000,
        "length_floor": 4500, "length_ceiling": 7000, "length_deviation": -2000,
    }

    runner.write_chapter(1, PLAN, feedback="整章重来", revision_mode="rewrite")

    assert writer.calls[0]["previous_text"] is None


# ══════════════════════ ② 打回意见 → 重写指令（问题①） ══════════════════════

def test_targeted_notes_allow_expansion_but_forbid_plot_drift():
    """定向修订的指令必须同时给出两件事，且二者不打架：

    · 未点名处的情节/人物/场景不得改动（不许借"丰富细节"改剧情）；
    · 但**允许在既有场景内加戏补足字数**（字数要求与"逐字保留"并不冲突）。
    """
    note = human_revision_notes("1. 对峙太软 → 建议：让女主当面拆穿他", "targeted")
    assert "定向修订" in note
    assert "逐字保留" in note
    assert "1. 对峙太软" in note


def test_rewrite_section_states_hard_band_not_loose_tolerance():
    """重写指令里的字数必须是**区间硬线**，不是"允许误差 ±N"。

    由来：旧文案写"仍须达到 5000 字（允许误差 ±500 字）"，模型读到的是"4250 也行"，
    再加上"不得新增场景/角色"的限制，于是重写稿规模原地不动——用户看到的正是
    "打回后还是不够/没按建议改"。
    """
    from src.agents.writer import Writer

    section = Writer.__new__(Writer)._revision_section(
        "人审意见占位", 5000, 4500, 7000, current_length=2000
    )
    assert "4500-7000 字" in section
    assert "低于 4500 字或高于 7000 字都直接判不合格" in section
    # ★ 上一稿字数与这一稿的伸缩余量必须写明（否则模型落实意见时顺手顶穿上限）
    assert "上一稿 2000 字" in section
    assert "至少再增加 2500 字" in section
    assert "在既有场景内补足动作、对话、神态与感官细节" in section


def test_rewrite_section_warns_when_already_in_band():
    """上一稿已在区间内时，必须明确"不得把总字数顶出上限"。

    真实冒烟抓到的缺陷：初稿 2269 字（可接受 700-3200），打回后模型加了戏变成 3210 字
    ——比上限多 10 字被判不合格，门禁白跑一轮。指令里不写这个数，模型无从收敛。
    """
    from src.agents.writer import Writer

    section = Writer.__new__(Writer)._revision_section(
        "人审意见占位", 1200, 700, 3200, current_length=2269
    )
    assert "已在合格区间内" in section
    assert "2269-3200 字都合格" in section
    assert "收紧到与上一稿相近的规模" in section   # 不诱导模型顺手加戏顶穿上限


def test_rewrite_section_tells_model_to_shrink_when_over_ceiling():
    from src.agents.writer import Writer

    section = Writer.__new__(Writer)._revision_section(
        "人审意见占位", 1200, 700, 3200, current_length=4000
    )
    assert "已超上限" in section
    assert "至少删减 800 字" in section      # 4000 - 3200


def test_unchanged_rewrite_is_detected_and_retried():
    """★ 重写稿与上一稿几乎一致时必须自动重试，而不是静默落盘。

    真实冒烟（5000 字目标）抓到的缺陷：模型把上一稿**逐字原样返回**，
    attempt 累加到 4 稿、每次输出完全一致（before_len == after_len），
    用户的打回意见等于没生效，还白烧三轮调用。
    """
    from src.agents import writer as writer_mod
    from src.agents.writer import Writer

    old = "正" * 2000
    call_count = {"n": 0}

    def _fake_registry_chat(role, msgs, temperature=None, **kw):
        from src.llm.base import ChatResult

        call_count["n"] += 1
        prompt = msgs[-1].content
        if "重写无效" in prompt:
            # 被强制重试的那一次才真的改
            return ChatResult(content="改" * 2200, model="fake", provider_name="fake")
        return ChatResult(content=old, model="fake", provider_name="fake")

    w = Writer.__new__(Writer)
    w._registry = None
    w._target_words = 5000
    w._tolerance = 4500
    w._ceiling = 7000
    w._max_continuation_attempts = 0

    calls: list[str] = []
    orig = writer_mod._faithful_text

    def _spy(registry, prompt):
        from src.llm.base import ChatMessage, ChatResult

        calls.append(prompt)
        content = _fake_registry_chat("writer", [ChatMessage("user", prompt)])
        return ChatResult(content=content.content, model="fake", provider_name="fake")

    writer_mod._faithful_text = _spy
    try:
        from src.memory.memory_manager import ChapterContext

        ctx = ChapterContext(chapter=1, outline="大纲占位")
        result = w.write_chapter(
            ctx,
            revision_notes="【人工审阅打回意见，必须逐条落实】\n1. 把对话写细",
            previous_text=old,
            target_words_override=5000,
        )
    finally:
        writer_mod._faithful_text = orig

    assert len(calls) == 2, f"未触发「重写无效」重试（实际调用 {len(calls)} 次）"
    assert "重写无效" in calls[1]
    assert result.content.startswith("改"), "重试后仍未采用真实改写的稿子"


def test_unchanged_detection_ignores_legit_revision():
    """判定必须两头都对：整篇复制要抓得住，正常修订不能误判（误判＝白烧一次调用）。

    分辨率说明：比对按 200 字块做，因此**小于一块的改动检测不到**——
    这是刻意的（误差方向是"漏判→少烧一次"，而不是"误判→白烧"）。
    """
    from src.agents.writer import _looks_unchanged, _unchanged_ratio

    old = "正" * 2000 + "甲" * 600
    assert _looks_unchanged(old, old) is True                   # 逐字返回 → 抓得住
    tail_swapped = "正" * 2000 + "甲" * 200 + "乙" * 400          # 换掉两块以上 → 视为真改了
    assert _looks_unchanged(tail_swapped, old) is False
    assert _unchanged_ratio(tail_swapped, old) < 0.85
    revised = "".join("改" if i % 4 == 0 else c for i, c in enumerate(old))
    assert _looks_unchanged(revised, old) is False
    assert _unchanged_ratio(revised, old) < 0.2


# ══════════════════════ ③ 评分侧的确定性字数校验 ══════════════════════

def test_editor_overrides_model_self_scored_length():
    """字数是否达标是**客观事实**，不交给模型自评（模型常给满分却远小于要求）。"""
    review = ReviewOutput(consistency=9, plot=9, continuity=9, prose=9,
                          length=10.0, issues=[], comment="很好")
    fixed = Editor._reconcile_length(review, target=5000, actual=2000, floor=4500, ceiling=7000)

    assert fixed.length < 10.0, "欠字数却没扣 length 分"
    length_issues = [i for i in fixed.issues if i.dimension == "length"]
    assert len(length_issues) == 1
    assert length_issues[0].severity == "major"
    assert "扩充至约 5000 字" in length_issues[0].suggestion
    # 四维总分不受影响（length 是独立门禁维度）
    assert fixed.overall == 9.0


def test_editor_clears_spurious_length_issue_within_band():
    """落在区间内（含上浮）时，模型若因"超出目标"标了 issue，一律纠正掉。"""
    review = ReviewOutput(consistency=9, plot=9, continuity=9, prose=9, length=6.0,
                          issues=[ReviewIssue(dimension="length", severity="minor",
                                              description="超出目标字数", quote="",
                                              suggestion="压缩")],
                          comment="")
    fixed = Editor._reconcile_length(review, target=5000, actual=6500, floor=4500, ceiling=7000)
    assert fixed.length == 10.0
    assert [i for i in fixed.issues if i.dimension == "length"] == []


def test_editor_flags_overshoot_beyond_ceiling():
    review = ReviewOutput(consistency=9, plot=9, continuity=9, prose=9, length=10.0,
                          issues=[], comment="")
    fixed = Editor._reconcile_length(review, target=5000, actual=9000, floor=4500, ceiling=7000)
    assert fixed.length < 10.0
    assert any(i.dimension == "length" for i in fixed.issues)


# ══════════════════════ ④ 生成前设定字数（问题③） ══════════════════════

def test_chapter_target_resolution_prefers_explicit_then_frontmatter():
    """本章目标的解析优先级：显式传入 > 已落盘目标 > 管线默认。"""
    assert resolve_chapter_target({"target_words": 5000}, 3000) == 5000
    assert resolve_chapter_target(None, 5000) == 5000
    # 非法值不抛错，回落
    assert resolve_chapter_target({"target_words": "abc"}, 5000) == 5000
    assert resolve_chapter_target({"target_words": -1}, None) is None


# ══════════════════════ ⑤ 字数目标的事实源（不丢、不带错） ══════════════════════

def test_chosen_target_is_persisted_and_reloaded(sandbox, make_memory):
    """生成前设定的字数必须落进剧情卡记录并读得回。

    由来（用户实测）：选卡时设的字数只存在**内存会话**里，一旦重启 / 断点续写 /
    会话重建就退回默认值——用户看到的是"设了也不管用"。
    """
    from src.orchestrator.interactive import InteractiveRunner

    store, memory = make_memory("interactive-target")
    pipe = _FakePipe(_FakeWriter([1]), _FakeEditor())
    pipe.store = store
    pipe.memory = memory
    runner = InteractiveRunner(pipe)

    cards = [{"card_id": "c1", "title": "甲", "tag": "主线推进", "outline": "o1",
              "hook": "h1", "characters": ["A"]},
             {"card_id": "c2", "title": "乙", "tag": "冲突爆发", "outline": "o2",
              "hook": "h2", "characters": ["B"]},
             {"card_id": "c3", "title": "丙", "tag": "伏笔支线", "outline": "o3",
              "hook": "h3", "characters": ["C"]}]

    runner._save_cards(4, cards)                                   # noqa: SLF001
    assert runner.load_cards(4)["target_words"] is None             # 未设定 → None

    runner.choose_card(4, "c2", target_words=6200)
    rec = runner.load_cards(4)
    assert rec["chosen"] == "c2"
    assert rec["target_words"] == 6200, "选卡时设的字数没有落盘（重启后必丢）"

    # 重抽剧情卡（不带字数）不得把已设定的字数抹掉
    runner._save_cards(4, cards)                                   # noqa: SLF001
    assert runner.load_cards(4)["target_words"] == 6200


def test_draft_snapshot_reads_target_and_band(sandbox, make_memory):
    """回读草稿时：目标字数与可接受区间都要能恢复（断点续跑用）。"""
    from src.orchestrator.interactive import InteractiveRunner

    store, memory = make_memory("interactive-draft")
    pipe = _FakePipe(_FakeWriter([1]), _FakeEditor())
    pipe.store = store
    pipe.memory = memory
    runner = InteractiveRunner(pipe)

    store.write(
        store.chapter_rel_path(1, 1),
        "正" * 100,
        metadata={"chapter": 1, "volume": 1, "status": "draft", "attempt": 2,
                  "words": 6100, "target_words": 6200},
        commit_message="ch-001 第 2 稿",
    )
    snap = runner.draft_snapshot(1)
    assert snap["target_words"] == 6200
    assert snap["words"] == 6100
    assert (snap["length_floor"], snap["length_ceiling"]) == (5700, 8200)
    assert snap["length_deviation"] == -100


# ══════════════════════════ ⑤ 扩写 / 压缩必须收敛 ══════════════════════════

def _writer_with_stub(monkeypatch):
    """装一个脚本化 Writer：记录每次提示词，按脚本返回内容。"""
    from src.agents import writer as writer_mod
    from src.agents.writer import Writer

    w = Writer.__new__(Writer)
    w._registry = None
    w._target_words = 5000
    w._tolerance = 4500
    w._ceiling = 7000
    w._max_continuation_attempts = 0
    calls: list[str] = []

    def _spy(registry, prompt):
        from src.llm.base import ChatResult

        calls.append(prompt)
        return ChatResult(content=writer_mod.__dict__["_TEST_REPLY"](prompt),
                          model="fake", provider_name="fake")

    monkeypatch.setattr(writer_mod, "_faithful_text", _spy)
    return w, calls


def test_expand_retries_until_floor_then_stops(monkeypatch):
    """欠字数：一轮不够就继续补，出现"没有加长"就立刻停（不空转）。"""
    from src.agents import writer as writer_mod

    replies = ["补" * 800, "补" * 800, "补" * 800, "补" * 800]
    idx = {"i": 0}

    def _reply(prompt):
        if "字数补充" not in prompt:
            return "正" * 1000
        n = replies[min(idx["i"], len(replies) - 1)]
        idx["i"] += 1
        return n

    writer_mod.__dict__["_TEST_REPLY"] = _reply
    w, calls = _writer_with_stub(monkeypatch)
    w._max_continuation_attempts = 1


    out = w._expand("基础提示词", "正" * 1000, 5000, 4500)
    # 1000 → 1800 → 2600 → 3400（每轮 800 字，段间补两个换行）；3 轮上限时停在 3400 附近
    assert len(calls) == 3, f"扩写轮数不符合预期：{len(calls)}"
    assert 3300 <= len(out) <= 3500, f"扩写结果异常：{len(out)}"
    assert out.startswith("正" * 1000), "扩写必须保留原稿（只往后接）"


def test_expand_stops_when_model_does_not_grow(monkeypatch):
    """模型返回空/复述（没有真的加长）时立即停止，避免白烧调用。"""
    from src.agents import writer as writer_mod

    def _reply(prompt):
        return "" if "字数补充" in prompt else "正" * 1000

    writer_mod.__dict__["_TEST_REPLY"] = _reply
    w, calls = _writer_with_stub(monkeypatch)

    out = w._expand("基础提示词", "正" * 1000, 5000, 4500)
    assert len(calls) == 1, "空产出应立即停止，不该继续重试"
    assert out == "正" * 1000


def test_compress_retries_until_ceiling(monkeypatch):
    """超上限：一次压缩不一定够（实测 3986 > 3500），必须继续压到区间内。"""
    from src.agents import writer as writer_mod

    replies = ["压" * 3900, "压" * 3300]   # 第 1 次压了但没到位 → 必须再压一次
    idx = {"i": 0}

    def _reply(prompt):
        if "字数压缩" not in prompt:
            return "长" * 3986
        n = replies[min(idx["i"], len(replies) - 1)]
        idx["i"] += 1
        return n

    writer_mod.__dict__["_TEST_REPLY"] = _reply
    w, calls = _writer_with_stub(monkeypatch)

    out = w._compress("基础提示词", "长" * 3986, 1500, 3500)
    assert len(calls) == 2, f"压缩轮数不符合预期：{len(calls)}"
    assert len(out) == 3300
    assert len(out) <= 3500


def test_compress_keeps_shorter_version_when_not_shrinking(monkeypatch):
    """压缩版没有变短 → 保留较短的原稿退出（绝不接受更长的"压缩结果"）。"""
    from src.agents import writer as writer_mod

    def _reply(prompt):
        return "长" * 4200 if "字数压缩" in prompt else "长" * 3986

    writer_mod.__dict__["_TEST_REPLY"] = _reply
    w, calls = _writer_with_stub(monkeypatch)

    out = w._compress("基础提示词", "长" * 3986, 1500, 3500)
    assert len(calls) == 1
    assert len(out) == 3986, "变长的压缩结果被采纳了"


def test_editor_review_without_length_bounds_does_not_crash(sandbox, make_memory):
    """★ 不传 length_bounds 的调用点不得崩（真机踩到：展开 None → TypeError）。

    `review_chapter(ctx, text, attempt, target_words=N)` 是外部脚本/子进程的常见直调形态，
    区间必须**按非对称口径现算**，而不是要求调用方一定传参。
    """
    from src.agents.editor import Editor

    store, memory = make_memory("editor-nobounds")
    captured: dict = {}

    class _Reg:
        def chat_as(self, role, msgs, **kw):
            from src.llm.base import ChatResult

            captured["prompt"] = msgs[-1].content
            return ChatResult(
                content=ReviewOutput(consistency=9, plot=9, continuity=9, prose=9,
                                     length=10.0, issues=[], comment="ok").model_dump_json(),
                model="fake", provider_name="fake",
            )

    editor = Editor(_Reg(), store)
    ctx = memory.retrieve_context(chapter=1, chapter_outline="第一课", characters=[],
                                  total_chapters=2)
    # 目标 5000 → 区间 4500-7000，必须出现在渲染后的提示词里
    review = editor.review_chapter(ctx, "正" * 3000, 1, target_words=5000)
    assert review.length < 10.0, "欠字数应按客观字数判不合格"
    assert "4500-7000" in captured["prompt"], "未按非对称口径现算区间"


def test_generation_config_is_the_single_source_of_length_policy():
    """门禁、写作、评分、前端拿到的边界必须来自同一个出口。"""
    gen = generation_config(_FakePipe(_FakeWriter([1]), _FakeEditor()))
    assert gen.length_bounds(5000) == (4500, 7000)
    # 管线级配置被尊重（不是硬编码）：上浮侧完全跟随配置
    custom = GenerationConfig(word_count_ceiling_offset=800)
    pipe = _FakePipe(_FakeWriter([1]), _FakeEditor())
    pipe.gen_config = custom
    assert generation_config(pipe).length_bounds(5000) == (4500, 5800)
    # 下浮侧受 word_count_tolerance（500）兜底：调小 floor_offset 不会低于该硬线
    tight = GenerationConfig(word_count_floor_offset=300)
    assert tight.length_bounds(5000) == (4500, 7000)


def test_length_assessment_boundaries_are_inclusive():
    """边界必须闭合：恰好等于下限/上限都算合格。"""
    assert length_assessment(5000, 4500, 4500, 7000) == "pass"
    assert length_assessment(5000, 7000, 4500, 7000) == "pass"
    assert length_assessment(5000, 4499, 4500, 7000) == "short"
    assert length_assessment(5000, 7001, 4500, 7000) == "long"
    # 缺省边界按非对称口径现算（4500/7000）
    assert length_assessment(5000, 4500) == "pass"
    assert length_assessment(5000, 7001) == "long"


def test_length_revision_note_names_the_exact_deficit_and_ceiling():
    note = length_revision_note(5000, 4000, 4500, 7000)
    assert "4000 字" in note and "5000 字" in note
    assert "还差 1000 字" in note          # 差额按"到目标"算，与门禁判定同一对数
    assert "4500-7000" in note
    # 已过下限但未到目标：差额仍按目标算（模型读到的是"还差多少"，不会误判为已达标）
    mid = length_revision_note(5000, 4600, 4500, 7000)
    assert "还差 400 字" in mid
