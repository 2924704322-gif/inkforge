"""正文改写模块回归（创作功能第四项 · 专职智能体）。

覆盖用户提的五条要求里**可证伪**的部分：

1. **严格按要求改（最小改动）**：段级回算把「越界改动」钉成断言——双向测：
   越界产出被拦（并重试），合法产出不被误杀。
2. **整篇回传**：只回片段 → 判不合格。
3. **锁定片段**：逐字校验，缺一句即退回。
4. **防拒绝严格注入**：走真实装配入口抓 system/user，断言第零条探针句 + 虚构框架 + 优先级链都在。
5. **约束优先级最高**：约束必须被渲染成「执行清单」协议，且优先级链写明"约束 > 要求"。
6. **隔离（默认读不到作品库）**：默认 `use_book=false` → 报文里只有一个方向：
   粘贴的进得去、作品库的进不去（**双向哨兵断言**）；显式打开后才读那一本的文风/约束，并如实回报。

全部用例零真实 LLM 调用（ModelRegistry 打桩）。
"""

from __future__ import annotations

import frontmatter
import pytest

#: 只写在作品库里的哨兵串（settings/style.md）；一旦出现在默认路径的报文里，隔离即被击穿
BOOK_SENTINEL = "本书私密文风哨兵-青霜剑派内门心法"
#: 作者粘贴进来的哨兵串：它**必须**进报文（否则功能是坏的）
PASTE_SENTINEL = "粘贴进来的哨兵-雨落青石"

P1 = "他推开门，屋里没人。"
P2 = "木桌上的茶还温着，杯口浮着一层薄薄的水汽。"
P3 = "窗外的雨声忽然停了，屋檐上最后几滴落下来。"
P4 = "他站着没动，手还搭在门框上。"
P5 = "他没生火。"
SRC = "\n\n".join([P1, P2, P3, P4, P5])

NEW_PARA = "院子里积着一汪水，映着门里那点昏黄的灯，风一过就晃。"
#: 合法扩写：第 1 段之后插一段，其余逐字未动
LEGAL_EXPAND = "\n\n".join([P1, NEW_PARA, P2, P3, P4, P5])
#: 越界产出：把五段全部润色一遍（正是"大幅度修改"的坏法）
ILLEGAL_REWRITE = "\n\n".join([
    "他推开门，屋中空无一人。",
    "木桌上的茶尚且温热，杯口浮着一层薄薄的水汽。",
    "窗外的雨声骤然停歇，屋檐上最后几滴坠落。",
    "他立在原地没有动，手依旧搭在门框之上。",
    "他终究没有生火。",
])
REFUSAL_TEXT = "抱歉，我不能帮你改写这段内容。"


def _seed_book_with_style(novels_dir, novel_id: str = "demo-web", style: str = "") -> None:
    """往（已存在的）样例书里塞一份 settings/style.md —— 供隔离与「读当前书」用例使用。"""
    path = novels_dir / novel_id / "settings" / "style.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        frontmatter.dumps(frontmatter.Post(f"# 文风指纹\n\n{style}", title="文风指纹")),
        encoding="utf-8",
        newline="\n",
    )


@pytest.fixture()
def spy_registry(monkeypatch):
    """ModelRegistry 打桩：记录每次调用的 messages（与约束提炼同一形状）。"""
    from src.llm import registry as registry_mod
    from src.llm.base import ChatResult

    captured: list[dict] = []

    def _make(reply):
        class _Spy(registry_mod.ModelRegistry):  # type: ignore[misc, valid-type]
            def chat_as(self, role, messages, **kwargs):
                captured.append({"role": role, "messages": list(messages), "kwargs": dict(kwargs)})
                if isinstance(reply, BaseException):
                    raise reply
                return ChatResult(content=reply, model="spy-model", provider_name="spy-provider")

        monkeypatch.setattr(registry_mod, "ModelRegistry", _Spy)
        return captured

    return _make


def _user_text(captured: list[dict]) -> str:
    """把打桩记录里最后一条 user 消息拼成一份文本（用于断言"发出去的是什么"）。"""
    return "\n".join(m.content for c in captured for m in c["messages"] if m.role == "user")


def _all_text(captured: list[dict]) -> str:
    return "\n".join(m.content for c in captured for m in c["messages"])


# ══════════════════════ 模式判定 ══════════════════════


class TestModeDetection:
    @pytest.mark.parametrize(("requirement", "want"), [
        ("在第 1 段之后扩写一段环境描写", "expand"),
        ("把这段写详细一点，补足细节", "expand"),
        ("在这之后加一段对白", "insert"),
        ("把「他没生火」改成「他终究没有生火」", "replace"),
        ("把第 2 段改得更紧凑", "rewrite"),
        ("整体重写，换掉现在这个走向", "rewrite_all"),
        ("推翻重来", "rewrite_all"),
    ])
    def test_detect_mode_priority(self, requirement, want):
        from src.services import revision_forge as rev

        assert rev.detect_mode(requirement)[0] == want

    def test_no_keyword_falls_back_to_rewrite(self):
        from src.services import revision_forge as rev

        assert rev.detect_mode("看看这段")[0] == rev.MODE_REWRITE

    def test_explicit_mode_wins_over_auto(self):
        from src.services import revision_forge as rev

        mode, reason = rev.resolve_mode("insert", "整体重写吧")
        assert (mode, reason) == ("insert", "用户指定")

    def test_illegal_mode_falls_back_to_detection(self):
        from src.services import revision_forge as rev

        assert rev.resolve_mode("乱写的模式", "扩写一下")[0] == rev.MODE_EXPAND

    def test_rewrite_all_is_the_only_mode_that_opens_the_gate(self):
        from src.services import revision_forge as rev

        mode, _ = rev.detect_mode("整体重写")
        diff = rev.build_diff(SRC, ILLEGAL_REWRITE, mode=mode)
        assert diff["checks"]["minimal_edit"]["ok"] is True


# ══════════════════════ 要求的作用范围（局部 / 全篇） ══════════════════════


class TestScopeResolution:
    """2026-10-05 真机「多轮失败」的根因：把「只改第 3 段」与「整体更紧凑」混为一谈。

    全文要求下模型改了 24 段里的 12 段 → 被判"越界" → 连烧 4 轮；
    而模型在"不许乱改"的纪律下最省事的合规输出就是**原样抄一遍**（日志 8/9 次回吐 3942 字）。
    """

    def test_anchor_forces_local(self):
        from src.services import revision_forge as rev

        assert rev.resolve_scope("整体润色一下", [{"index": 3}])[0] == rev.SCOPE_LOCAL

    @pytest.mark.parametrize("requirement", [
        "整体更紧凑一点",
        "全篇润色，删掉重复的形容",
        "把节奏收紧",
        "每一段都精简一下",
    ])
    def test_global_requirements(self, requirement):
        from src.services import revision_forge as rev

        assert rev.resolve_scope(requirement)[0] == rev.SCOPE_GLOBAL

    @pytest.mark.parametrize("requirement", [
        "只改第 3 段的对白",
        "把「他没生火。」改成「他终究没有生火。」",
        "其余保持原样，只把这一段改得含蓄一点",
    ])
    def test_local_requirements(self, requirement):
        from src.services import revision_forge as rev

        assert rev.resolve_scope(requirement)[0] == rev.SCOPE_LOCAL

    def test_default_is_conservative_local(self):
        from src.services import revision_forge as rev

        assert rev.resolve_scope("看看这段")[0] == rev.SCOPE_LOCAL

    def test_global_scope_lets_a_whole_text_pass_go_through(self):
        """全篇要求下改很多段不再算越界 —— 而内容丢失照样硬拦。

        为什么用 12 段：容差是**按段数比例**给的（max(2, 15%)）。
        5 段的样本里"全改"恰好落在容差内，测不出两种口径的差别。
        """
        from src.services import revision_forge as rev

        paras = [f"第{i}间屋子空着，他推门进去看了一眼就退出来。" for i in range(1, 13)]
        original = "\n\n".join(paras)
        whole_rewrite = "\n\n".join([p + "他没有多停。" for p in paras])

        local = rev.build_diff(original, whole_rewrite, mode=rev.MODE_REWRITE, scope=rev.SCOPE_LOCAL)
        glob = rev.build_diff(original, whole_rewrite, mode=rev.MODE_REWRITE, scope=rev.SCOPE_GLOBAL)
        assert local["stats"]["edited"] == 12 and local["stats"]["lost"] == 0
        assert local["checks"]["minimal_edit"]["ok"] is False, "局部口径下这就是越界"
        assert glob["checks"]["minimal_edit"]["ok"] is True, "全篇口径下这就是照办"

        lost = rev.build_diff(
            original, "\n\n".join(paras[:10]), mode=rev.MODE_REWRITE, scope=rev.SCOPE_GLOBAL
        )
        assert lost["checks"]["minimal_edit"]["ok"] is False, "全篇口径的底线不变：内容不许丢"
        assert lost["stats"]["lost"] >= 1

    def test_prompt_states_the_scope(self):
        from src.services import revision_forge as rev

        glob = rev.build_messages(
            SRC, "整体更紧凑一点", mode=rev.MODE_REWRITE, scope=rev.SCOPE_GLOBAL
        )[1].content
        loc = rev.build_messages(
            SRC, "只改第 1 段", mode=rev.MODE_REWRITE, scope=rev.SCOPE_LOCAL
        )[1].content
        assert "作用范围：全篇" in glob and "作用范围：局部" in loc

    def test_global_requirement_is_reported_back(self, sandbox, spy_registry):
        from src.services import revision_forge as rev

        spy_registry("\n\n".join([f"{p}他站了很久。" for p in [P1, P2, P3, P4, P5]]))
        out = rev.extract_revision(SRC, "整体更紧凑一点，把长句拆短")
        assert out["scope"] == rev.SCOPE_GLOBAL
        assert out["scope_label"] == "全篇"
        assert out["checks"]["minimal_edit"]["ok"] is True


# ══════════════════════ 分段口径 ══════════════════════


class TestParagraphSplit:
    """粘贴来源常常是单换行分段：只认空行会让「整章被当成一段」，锚点与越界判定一起失效。"""

    def test_blank_line_separated(self):
        from src.services import revision_forge as rev

        assert rev.split_paragraphs(SRC) == [P1, P2, P3, P4, P5]

    def test_single_newline_separated_falls_back_to_lines(self):
        from src.services import revision_forge as rev

        pasted = "\n".join([P1, P2, P3, P4, P5])
        assert rev.split_paragraphs(pasted) == [P1, P2, P3, P4, P5]

    def test_single_paragraph_is_not_broken(self):
        from src.services import revision_forge as rev

        assert rev.split_paragraphs(P1) == [P1]


# ══════════════════════ 最小改动闸门（核心） ══════════════════════


class TestMinimalEditGate:
    def test_legal_expand_passes(self):
        from src.services import revision_forge as rev

        diff = rev.build_diff(
            SRC, LEGAL_EXPAND, mode=rev.MODE_EXPAND, anchors=[{"after_index": 1}]
        )
        assert diff["checks"]["minimal_edit"]["ok"] is True
        assert diff["checks"]["minimal_edit"]["soft"] is False
        assert diff["stats"]["inserted"] == 1
        assert diff["stats"]["kept"] == 5
        assert diff["stats"]["edited"] == 0

    def test_full_rewrite_is_blocked(self):
        from src.services import revision_forge as rev

        diff = rev.build_diff(SRC, ILLEGAL_REWRITE, mode=rev.MODE_EXPAND)
        check = diff["checks"]["minimal_edit"]
        assert check["ok"] is False
        assert check["hard"] is True
        assert check["out_of_scope"], "越界段必须被点名"

    def test_deleting_a_paragraph_is_always_a_violation(self):
        from src.services import revision_forge as rev

        shortened = "\n\n".join([P1, P2, P3, P5])
        diff = rev.build_diff(SRC, shortened, mode=rev.MODE_REWRITE)
        assert diff["stats"]["deleted"] == 1
        assert diff["stats"]["lost"] == 1
        assert diff["checks"]["minimal_edit"]["ok"] is False

    def test_merging_two_paragraphs_is_a_soft_warning_not_a_failure(self):
        """段结构变了但内容还在（并段）→ 告警，不判死。

        真机实测：模型把最后两段并成一段，内容一个字没丢，却被当成"删段"硬拦 4 轮后整次失败。
        """
        from src.services import revision_forge as rev

        merged = "\n\n".join([P1, P2, P3, P4 + P5])
        diff = rev.build_diff(SRC, merged, mode=rev.MODE_REWRITE, anchors=[{"after_index": 5}])
        assert diff["stats"]["merged"] == 1
        assert diff["stats"]["lost"] == 0
        check = diff["checks"]["minimal_edit"]
        assert check["ok"] is True and check["soft"] is True
        assert [o["kind"] for o in check["out_of_scope"]] == ["merged"]

    def test_losing_content_is_still_a_hard_failure(self):
        from src.services import revision_forge as rev

        msg = rev.fidelity_problem(
            "\n\n".join([P1, P2, P3, P5]), original=SRC, mode=rev.MODE_REWRITE
        )
        assert "删除" in msg and "逐字恢复" in msg

    def test_anchor_paragraph_must_stay_untouched_in_expand(self):
        """扩写是"在它附近加"，不是"改它"——锚点段被改也算越界。"""
        from src.services import revision_forge as rev

        touched = "\n\n".join([P1.replace("没人", "空无一人"), NEW_PARA, P2, P3, P4, P5])
        diff = rev.build_diff(
            SRC, touched, mode=rev.MODE_EXPAND, anchors=[{"after_index": 1}]
        )
        assert diff["checks"]["minimal_edit"]["out_of_scope"][0]["index"] == 1

    def test_anchor_paragraph_may_change_in_rewrite_mode(self):
        """改写模式恰恰是"改点名处"——锚点段必须被允许改，否则功能自相矛盾。"""
        from src.services import revision_forge as rev

        rewritten_p2 = "木桌上的茶还温着，杯壁凝着一层细密的水珠。"
        out = "\n\n".join([P1, rewritten_p2, P3, P4, P5])
        diff = rev.build_diff(SRC, out, mode=rev.MODE_REWRITE, anchors=[{"after_index": 2}])
        assert diff["stats"]["edited"] == 1
        assert diff["checks"]["minimal_edit"]["ok"] is True

    def test_one_stray_edit_is_soft_passed_not_hard_failed(self):
        """点名后多改了 1 段：放过并告警，不把 4 轮重试全烧掉。"""
        from src.services import revision_forge as rev

        out = "\n\n".join([P1, NEW_PARA, P2, P3.replace("忽然停了", "骤然停歇"), P4, P5])
        diff = rev.build_diff(SRC, out, mode=rev.MODE_EXPAND, anchors=[{"after_index": 1}])
        check = diff["checks"]["minimal_edit"]
        assert check["ok"] is True and check["soft"] is True

    def test_problem_text_names_the_offending_paragraph(self):
        from src.services import revision_forge as rev

        msg = rev.fidelity_problem(ILLEGAL_REWRITE, original=SRC, mode=rev.MODE_EXPAND)
        # 重试语必须"点名 + 给出可执行动作"，否则模型下一轮还会错同一处（真机实测踩到）
        assert "第" in msg and "逐字恢复" in msg
        assert len(msg) <= 300, "重试语会被 _retry_note 截到 300 字，别写太长"

    def test_problem_text_is_empty_for_legal_output(self):
        from src.services import revision_forge as rev

        assert rev.fidelity_problem(
            LEGAL_EXPAND, original=SRC, mode=rev.MODE_EXPAND, anchors=[{"after_index": 1}]
        ) == ""


class TestNoChangeGate:
    """**零改动与乱改同罪** —— 用户实测「给出清楚要求与约束，输出正文却无改变」。"""

    def test_identical_output_is_a_gate_failure(self):
        from src.services import revision_forge as rev

        msg = rev.fidelity_problem(SRC, original=SRC, mode=rev.MODE_REWRITE)
        assert "一字未改" in msg and "真的改" in msg

    def test_whitespace_only_difference_still_counts_as_unchanged(self):
        from src.services import revision_forge as rev

        assert rev.fidelity_problem(
            SRC.replace("\n\n", "\n\n "), original=SRC, mode=rev.MODE_REWRITE
        ) != ""

    def test_any_real_change_passes(self):
        from src.services import revision_forge as rev

        assert rev.fidelity_problem(
            LEGAL_EXPAND, original=SRC, mode=rev.MODE_EXPAND, anchors=[{"after_index": 1}]
        ) == ""

    def test_unchanged_output_retries_then_raises(self, sandbox, spy_registry):
        from src.agents.architect import GENERATE_RETRIES
        from src.services import revision_forge as rev

        captured = spy_registry(SRC)  # 模型原样吐回原文
        with pytest.raises(rev.RevisionFidelityError):
            rev.extract_revision(SRC, "把最后一段改得更含蓄一点", anchors=[{"after_index": 5}])
        assert len(captured) == GENERATE_RETRIES + 2
        assert "一字未改" in _user_text(captured[1:])

    def test_rules_tell_the_model_it_must_actually_change(self):
        """纪律必须两头都写：不该动的别动 + 该动的必须动。"""
        from src.services import revision_forge as rev

        assert "必须真的改" in rev.MINIMAL_EDIT_RULES or "必须真的改" in rev.SYSTEM_PROMPT
        # 那句会与"必须真的改"打架的旧措辞不许回来
        assert "改动越少越好" not in rev.MINIMAL_EDIT_RULES


# ══════════════════════ 防拒绝机制：与互动创作/自由创作对照 ══════════════════════


class TestAntiRefusalParity:
    """「防拒绝机制是否落实」的对照断言。

    互动创作与自由创作的正文生成链路（writer 写正文 / 协商、plotter 出卡）用
    **四件套**：第零条 + brief 保真块 + 现实性口径 + 自定义约束清单 + 失败自动消化。
    本模块必须**同源同件**——但不许在这里另抄一份文本（单一源）。
    """

    #: 正文生成链路的两条模板：四件套全部以占位符声明
    WRITER_TEMPLATES = ("writer_chapter", "writer_negotiate")
    FOUR = (
        "{{constitution}}",
        "{{brief_fidelity}}",
        "{{reality_policy}}",
        "{{custom_constraints}}",
    )

    def test_writer_chain_declares_all_four_blocks(self):
        from src.agents.prompt_loader import load_all

        tpls = load_all()
        for name in self.WRITER_TEMPLATES:
            missing = [p for p in self.FOUR if p not in tpls[name]]
            assert not missing, f"{name} 缺占位符 {missing}"

    def test_plotter_injects_the_fidelity_block_in_code(self):
        """plotter 的保真块是**代码注入**而非模板占位符 —— 两条路都算"落实"。"""
        import inspect

        from src.agents import plotter

        assert "brief_fidelity_block" in inspect.getsource(plotter)

    def test_revision_prompt_carries_the_same_four_blocks(self):
        from src.agents.architect import BRIEF_FIDELITY_DIRECTIVE, FICTION_FRAMING
        from src.agents.prompt_loader import CONSTITUTION_MARK, CONSTRAINT_HEADING
        from src.agents.reality_policy import reality_policy_text
        from src.services import revision_forge as rev

        msgs = rev.build_messages(SRC, "把最后一段改得更含蓄一点", mode=rev.MODE_REWRITE,
                                  constraints="- **用词**：不用「仿佛」")
        system = msgs[0].content
        user = msgs[1].content

        assert CONSTITUTION_MARK in system, "① 第零条没进 system 位"
        assert FICTION_FRAMING.splitlines()[0] in user, "② 虚构框架声明缺失"
        assert BRIEF_FIDELITY_DIRECTIVE.splitlines()[0] in user, "② 零拒绝/零篡改指令缺失"
        assert reality_policy_text(False).splitlines()[0] in user, "③ 现实性评判口径缺失"
        assert CONSTRAINT_HEADING in user and "执行清单" in user, "④ 约束没落成执行清单"

    def test_reality_policy_text_is_the_single_source(self):
        """现实性口径不许在本模块抄副本：与互动创作取的是同一份文本（单一源，§5.12）。"""
        import inspect

        from src.services import revision_forge as rev

        src = inspect.getsource(rev)
        assert "reality_policy_text" in inspect.getsource(rev.build_messages)
        # 该口径正文里的一句独特表述；本模块源码内不得出现（出现即是从别处抄了副本）
        assert "唯一评判基准是**作者的创作目标**" not in src

    def test_failure_is_digested_by_the_shared_fidelity_chain(self):
        """零失败回传：走的是既有 generate_faithful（4 轮 + 末轮降温），不是自造重试。"""
        import inspect

        from src.agents.architect import GENERATE_RETRIES
        from src.services import revision_forge as rev

        src = inspect.getsource(rev.extract_revision)
        assert "generate_faithful" in src
        assert GENERATE_RETRIES >= 2, "轮数被改小等于降低容忍度（见 architect 注释）"

    def test_oracle_refusal_is_never_delivered_as_a_result(self, app_client, spy_registry):
        """拒答文本既不能当结果返回，也不能被误报成"越界"。"""
        from src.services import revision_forge as rev

        client, _ = app_client
        spy_registry(REFUSAL_TEXT)
        res = client.post("/api/revision/rewrite", json={
            "source_text": SRC, "requirement": "扩写一下",
        })
        assert res.status_code == 422
        assert "拒答" in res.json()["detail"]
        # 提示里不许出现"越界"这类错误归因
        assert "越界" not in res.json()["detail"]
        assert rev.looks_like_refusal_text(REFUSAL_TEXT)


class TestLockedSegments:
    def test_missing_dialogue_is_caught(self):
        from src.services import revision_forge as rev

        out = "\n\n".join([P1, "「你先坐。」", P2, P3, P4, P5])
        locks = rev.auto_lock_segments(P1)
        diff = rev.build_diff(SRC, out, mode=rev.MODE_EXPAND, lock_segments=["「你先坐。」"])
        assert diff["checks"]["locked"]["ok"] is True
        assert locks == []

    def test_altered_dialogue_is_caught(self):
        from src.services import revision_forge as rev

        src = "\n\n".join([P1, "「你要是回来得早，就先把火生上。」", P5])
        out = "\n\n".join([P1, "「你若回得早，便先把火生起来。」", P5])
        locks = rev.auto_lock_segments(src)
        assert locks == ["「你要是回来得早，就先把火生上。」"]
        msg = rev.fidelity_problem(out, original=src, mode=rev.MODE_EXPAND, lock_segments=locks)
        assert "锁定" in msg


class TestWholeReturn:
    def test_fragment_is_rejected(self):
        from src.services import revision_forge as rev

        diff = rev.build_diff(SRC, NEW_PARA, mode=rev.MODE_EXPAND)
        assert diff["checks"]["whole_returned"]["ok"] is False

    def test_rewrite_all_bypasses_the_length_ratio(self):
        from src.services import revision_forge as rev

        diff = rev.build_diff(SRC, "短。", mode=rev.MODE_REWRITE_ALL)
        assert diff["checks"]["whole_returned"]["ok"] is True


# ══════════════════════ 文风指纹 ══════════════════════


class TestStyleFingerprint:
    def test_fingerprint_shape(self):
        from src.services import revision_forge as rev

        fp = rev.style_fingerprint(SRC)
        assert fp["paragraphs"] == 5
        assert fp["sentences"] >= 5
        assert 0 <= fp["dialogue_paragraph_ratio"] <= 1

    def test_dialogue_ratio_detects_dialogue(self):
        from src.services import revision_forge as rev

        text = "\n\n".join(["他开口了。", "「你来了。」", "她没回答。"])
        assert rev.style_fingerprint(text)["dialogue_paragraph_ratio"] > 0

    def test_shift_is_reported(self):
        from src.services import revision_forge as rev

        fp = rev.style_fingerprint(SRC)
        far = rev.style_shift(fp, "\n\n".join(["短句。" * 1] * 5))
        assert far["ok"] is False
        assert "avg_sentence_len" in far["delta"]

    def test_style_block_carries_numbers_not_vibes(self):
        from src.services import revision_forge as rev

        block = rev.render_style_block(rev.style_fingerprint(SRC), "样本片段在此")
        assert "平均句长" in block and "样本片段在此" in block


# ══════════════════════ 输出规范化 ══════════════════════


class TestNormalize:
    def test_strips_fences_and_preamble(self):
        from src.services import revision_forge as rev

        raw = "```\n以下是改写后的正文：\n" + LEGAL_EXPAND + "\n```"
        assert rev.normalize_revision(raw) == LEGAL_EXPAND

    def test_keeps_body_that_starts_with_the_word_below(self):
        """正文本身以「以下是……」开头时不许误剥。"""
        from src.services import revision_forge as rev

        body = "以下是改写后的正文：" + "他推开门，屋里没人。这行字很长，长到足够保住自己不被剥掉。" * 3
        assert rev.normalize_revision(body).startswith("以下是改写后的正文：")


# ══════════════════════ 提示词装配与防拒绝 ══════════════════════


class TestPromptAssembly:
    def _messages(self, **kw):
        from src.services import revision_forge as rev

        params = {
            "mode": rev.MODE_EXPAND,
            "anchors": [{"index": 1, "text": P1, "quote": P1[:12]}],
            "lock_segments": ["「你来了。」"],
            "constraints": "- **句式**：不连续三句同一主语\n- **用词**：不用「仿佛」",
            "fingerprint": rev.style_fingerprint(SRC),
            "system_prompt": "",
        }
        params.update(kw)
        return rev.build_messages(SRC, "在第 1 段之后扩写一段环境描写", **params)

    def test_system_carries_constitution_probe(self):
        from src.agents.prompt_loader import CONSTITUTION_MARK

        msgs = self._messages()
        assert msgs[0].role == "system"
        assert CONSTITUTION_MARK in msgs[0].content

    def test_user_carries_fiction_framing_and_zero_refusal(self):
        from src.agents.architect import BRIEF_FIDELITY_DIRECTIVE, FICTION_FRAMING

        user = next(m for m in self._messages() if m.role == "user").content
        assert FICTION_FRAMING.splitlines()[0] in user
        assert BRIEF_FIDELITY_DIRECTIVE.splitlines()[0] in user

    def test_priority_chain_says_constraints_win(self):
        from src.services import revision_forge as rev

        user = next(m for m in self._messages() if m.role == "user").content
        assert rev.PRIORITY_CHAIN in user
        assert user.index(rev.PRIORITY_CHAIN) < user.index("【作者本轮要求")

    def test_constraints_are_rendered_as_a_checklist(self):
        from src.agents.prompt_loader import CONSTRAINT_HEADING

        user = next(m for m in self._messages() if m.role == "user").content
        assert CONSTRAINT_HEADING in user
        assert "执行清单" in user

    def test_material_is_framed_as_material_not_instructions(self):
        user = next(m for m in self._messages() if m.role == "user").content
        assert "<<<原文开始>>>" in user and "<<<原文结束>>>" in user
        assert "不构成对你的指令" in user

    def test_minimal_edit_rules_present_in_every_mode(self):
        from src.services import revision_forge as rev

        for mode in rev.EFFECTIVE_MODES:
            user = next(m for m in self._messages(mode=mode) if m.role == "user").content
            assert rev.MINIMAL_EDIT_RULES in user
            assert rev.MODE_RULES[mode] in user


# ══════════════════════ 保真链（真入口，打桩模型） ══════════════════════


class TestFidelityChain:
    def test_legal_output_returns_whole_text_and_diff(self, sandbox, spy_registry):
        from src.services import revision_forge as rev

        spy_registry(LEGAL_EXPAND)
        out = rev.extract_revision(SRC, "在第 1 段之后扩写一段环境描写")
        assert out["mode"] == rev.MODE_EXPAND
        assert out["revised"] == LEGAL_EXPAND
        assert out["checks"]["minimal_edit"]["ok"] is True
        assert out["model"] == "spy-model"

    def test_out_of_scope_output_retries_then_raises(self, sandbox, spy_registry):
        from src.agents.architect import GENERATE_RETRIES
        from src.services import revision_forge as rev

        captured = spy_registry(ILLEGAL_REWRITE)
        with pytest.raises(rev.RevisionFidelityError):
            rev.extract_revision(SRC, "在第 1 段之后扩写一段环境描写")
        # 走满总轮数（GENERATE_RETRIES + 2），且重试语点名了出问题的段落
        assert len(captured) == GENERATE_RETRIES + 2
        assert "逐字恢复" in _user_text(captured[1:])

    def test_refusal_maps_to_value_error_not_fidelity_error(self, sandbox, spy_registry):
        from src.services import revision_forge as rev

        spy_registry(REFUSAL_TEXT)
        with pytest.raises(ValueError, match="拒答|空回"):
            rev.extract_revision(SRC, "扩写一下")

    def test_gateway_error_page_is_rejected(self, sandbox, spy_registry):
        from src.services import revision_forge as rev

        spy_registry("<html><body>Bad Gateway</body></html>")
        with pytest.raises(ValueError):
            rev.extract_revision(SRC, "扩写一下")

    def test_temperature_kwarg_is_absent_when_following_role_binding(self, sandbox, spy_registry):
        from src.services import revision_forge as rev

        captured = spy_registry(LEGAL_EXPAND)
        rev.extract_revision(SRC, "扩写一下")
        assert "temperature" not in captured[0]["kwargs"]


# ══════════════════════ 端点契约 ══════════════════════


class TestEndpointContract:
    def test_agent_card(self, app_client):
        client, _ = app_client
        res = client.get("/api/revision/agent")
        assert res.status_code == 200
        body = res.json()
        assert body["key"] == "revision"
        assert {m["value"] for m in body["modes"]} >= {"auto", "expand", "rewrite_all"}
        assert body["limits"]["source_max"] > 0
        assert body["sees"] and body["never_sees"]
        assert "book_reads" not in body, "本模块不读作品库：名片里不该有「读当前书」这类能力"

    def test_happy_path(self, app_client, spy_registry):
        client, _ = app_client
        spy_registry(LEGAL_EXPAND)
        res = client.post("/api/revision/rewrite", json={
            "source_text": SRC,
            "requirement": "在第 1 段之后扩写一段环境描写",
            "anchors": [{"after_index": 1}],
        })
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["revised"] == LEGAL_EXPAND
        assert body["stats"]["inserted"] == 1
        assert body["checks"]["minimal_edit"]["ok"] is True
        assert body["mode"] == "expand"
        assert "未读取你作品库里的任何文件" in body["isolation"]

    def test_out_of_scope_output_is_422_not_a_bad_delivery(self, app_client, spy_registry):
        client, _ = app_client
        spy_registry(ILLEGAL_REWRITE)
        res = client.post("/api/revision/rewrite", json={
            "source_text": SRC, "requirement": "扩写一下",
        })
        assert res.status_code == 422
        assert res.headers.get("X-Inkforge-Error-Source") == "parse"

    def test_locked_segment_missing_is_422(self, app_client, spy_registry):
        client, _ = app_client
        src = "\n\n".join([P1, "「你来了。」", P5])
        spy_registry("\n\n".join([P1, "「他来了。」", P5]))
        res = client.post("/api/revision/rewrite", json={
            "source_text": src,
            "requirement": "扩写一下",
            "lock_segments": ["「你来了。」"],
        })
        assert res.status_code == 422

    @pytest.mark.parametrize(("body", "why"), [
        ({"source_text": "", "requirement": "扩写"}, "空原文"),
        ({"source_text": "太短", "requirement": "扩写"}, "原文过短"),
        ({"source_text": SRC, "requirement": ""}, "空要求"),
    ])
    def test_bad_input_is_400(self, app_client, body, why):
        client, _ = app_client
        res = client.post("/api/revision/rewrite", json=body)
        assert res.status_code == 400, why

    def test_overlong_source_is_400_with_actionable_hint(self, app_client):
        from src.services import revision_forge as rev

        client, _ = app_client
        res = client.post("/api/revision/rewrite", json={
            "source_text": "字" * (rev.MAX_SOURCE_CHARS + 1), "requirement": "扩写",
        })
        assert res.status_code == 400
        assert "拆分" in res.json()["detail"]

    def test_provider_error_is_502_and_redacted(self, app_client, spy_registry):
        client, _ = app_client
        spy_registry(RuntimeError("boom sk-fake-leak-0001"))
        res = client.post("/api/revision/rewrite", json={
            "source_text": SRC, "requirement": "扩写一下",
        })
        assert res.status_code == 502
        assert "sk-fake-leak-0001" not in res.json()["detail"]

    def test_request_body_has_no_book_fields(self, app_client):
        """端点层隔离：请求体里没有 novel / chapter / rel —— 调用方无从指定作品库里的东西。"""
        client, _ = app_client
        schema = client.get("/openapi.json").json()
        props = schema["components"]["schemas"]["RevisionBody"]["properties"]
        for forbidden in ("novel", "novel_id", "chapter", "rel", "book", "use_book"):
            assert forbidden not in props, f"请求体不该有 {forbidden}（作品库入口）"
        assert {"source_text", "requirement"} <= set(props)

    def test_lock_dialogue_flag_locks_every_line_of_dialogue(self, app_client, spy_registry):
        """自动锁对白是**便利默认**，不是用户指令：与要求相冲时逐轮收敛会放掉它（并如实回报）。

        用户实测的"多轮失败"里就有这一类：要求点名要改的对白被自己的锁挡住 → 4 轮全废。
        """
        client, _ = app_client
        src = "\n\n".join([P1, "「你来了。」", "「嗯。」", P5])
        spy_registry("\n\n".join([P1, "「他来了。」", "「嗯。」", P5]))
        res = client.post("/api/revision/rewrite", json={
            "source_text": src, "requirement": "扩写一下", "lock_dialogue": True,
        })
        assert res.status_code == 200
        relaxed = res.json()["checks"]["minimal_edit"]["relaxed"]
        assert any("自动锁对白" in r for r in relaxed), relaxed

    def test_user_specified_lock_is_never_relaxed(self, app_client, spy_registry):
        """用户**显式**写的锁定片段永不放弃：连续 4 轮被改 → 422（不许悄悄放行）。"""
        client, _ = app_client
        src = "\n\n".join([P1, "「你来了。」", P5])
        spy_registry("\n\n".join([P1, "「他来了。」", P5]))
        res = client.post("/api/revision/rewrite", json={
            "source_text": src,
            "requirement": "扩写一下",
            "lock_segments": ["「你来了。」"],
        })
        assert res.status_code == 422


# ══════════════════════ 墨师只读动作（ravision_open） ══════════════════════


class TestRevisionOpenAction:
    """墨师只做「定位 + 预填」：只读、零落盘、零长任务。"""

    def _ctx(self):
        from src.web import actions as act

        return act.ActionContext(
            hub=None,
            default_novel="demo-web",
            active_novel=lambda: "demo-web",
            session_key="demo-web",
            book="demo-web",
        )

    def test_registered_as_read_only_action(self):
        from src.web import actions as act

        assert "revision_open" in act.ACTIONS
        assert act.ACTIONS["revision_open"].is_write is False, "长任务不许做成写动作（会撞确认闸门）"
        assert "revision_open" in {m["op"] for m in act.manifest()}

    def test_returns_prefill_payload_without_touching_files(self, sandbox, app_client):
        from src.web import actions as act

        before = sorted(p.name for p in (sandbox.novels / "demo-web").rglob("*"))
        res = act.execute(
            "revision_open",
            {"novel": "demo-web", "chapter": 1, "requirement": "把开头补点环境，别动我的句子"},
            ctx=self._ctx(),
        )
        assert res.ok is True
        prefill = res.data["prefill"]
        assert prefill["chapter"] == 1
        assert prefill["mode"] == "expand"  # 由要求自动判定
        assert "不写任何文件" in res.summary
        after = sorted(p.name for p in (sandbox.novels / "demo-web").rglob("*"))
        assert before == after, "只读动作不得产生任何落盘"

    def test_unknown_book_fails_instead_of_silently_prefilling(self, sandbox, app_client):
        from src.web import actions as act

        res = act.execute(
            "revision_open", {"novel": "no-such-book", "chapter": 1}, ctx=self._ctx()
        )
        assert res.ok is False


# ══════════════════════ 隔离（双向哨兵） ══════════════════════


class TestIsolation:
    """**禁止看我的小说内容** —— 结构性四道闸，不是提示词里的一句请求。"""

    def test_default_path_sends_pasted_text_but_not_book_content(
        self, sandbox, app_client, spy_registry
    ):
        """行为闸（双向）：粘贴的**必须**进（否则功能是坏的），作品库的**必须**进不去。"""
        client, _ = app_client
        _seed_book_with_style(sandbox.novels, style=BOOK_SENTINEL)
        # 哨兵塞在最后一段内部 → 模型照原样保留该段即为合法产出（否则会被越界闸门拦下）
        source = "\n\n".join([P1, P2, P3, P4, P5 + PASTE_SENTINEL])
        reply = "\n\n".join([P1, NEW_PARA, P2, P3, P4, P5 + PASTE_SENTINEL])
        captured = spy_registry(reply)
        res = client.post("/api/revision/rewrite", json={
            "source_text": source,
            "requirement": "在第 1 段之后扩写一段环境描写",
            "anchors": [{"after_index": 1}],
        })
        assert res.status_code == 200, res.text
        sent = _all_text(captured)
        assert PASTE_SENTINEL in sent, "自己粘贴的内容必须进得去"
        assert BOOK_SENTINEL not in sent, "作品库内容必须进不去（本模块根本不读它）"

    def test_service_layer_never_imports_store(self):
        """依赖闸（服务层）：AST 取标识符，不许 import 任何读写作品库的模块。

        为什么走 AST 而不是字符串搜索：源码注释与文档字符串里合法地会写
        "不 import MdStore"这类说明，字符串搜索会把说明本身判成违规（自证式误报）。
        """
        import ast
        import inspect

        from src.services import revision_forge as rev

        tree = ast.parse(inspect.getsource(rev))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        banned = ("src.memory", "store_factory", "md_store", "library")
        assert not [m for m in imported if any(b in m for b in banned)], imported

    def test_web_layer_never_imports_store(self):
        """依赖闸（端点层）：同样只能拿到"作者自己的文本"。"""
        import ast
        import inspect

        from src.web import inkforge_revision as mod

        source = inspect.getsource(mod)
        tree = ast.parse(source)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                names.add(node.name.rsplit(".", 1)[-1])
        for banned in ("MdStore", "open_store", "store_factory", "read_custom_constraints",
                       "novels_dir", "get_settings"):
            assert banned not in names, f"端点层不该出现 {banned}（那是读作品库的通路）"


# ══════════════════════ 注册与运行期 ══════════════════════


class TestAgentRegistration:
    def test_registered_in_agent_presets(self):
        from src.services import revision_forge as rev
        from src.web.inkforge_api import AGENT_PRESETS

        assert rev.AGENT_KEY in AGENT_PRESETS
        assert AGENT_PRESETS[rev.AGENT_KEY]["label"] == rev.AGENT_LABEL
        # 单一源：AGENT_PRESETS 引用模块常量，而不是抄一份文本
        assert AGENT_PRESETS[rev.AGENT_KEY]["prompt"] == rev.SYSTEM_PROMPT

    def test_role_bound_in_models_config(self):
        from src.config.settings import load_models_config

        cfg = load_models_config()
        roles = getattr(cfg, "roles", None) or {}
        assert "revision" in roles, "没绑角色会在调用时抛 KeyError（运行时才炸）"

    def test_runtime_prompt_carries_constitution(self, app_client):
        from src.agents.prompt_loader import CONSTITUTION_MARK
        from src.web.inkforge_windows import agent_prompt

        assert CONSTITUTION_MARK in agent_prompt("revision")

    def test_cannot_be_used_as_a_chat_agent(self, app_client):
        client, _ = app_client
        res = client.post("/api/chats", json={"agent": "revision"})
        assert res.status_code == 400

    def test_agent_preset_listing_exposes_it(self, app_client):
        client, _ = app_client
        keys = {p["key"] for p in client.get("/api/agent-presets").json()["presets"]}
        assert "revision" in keys
