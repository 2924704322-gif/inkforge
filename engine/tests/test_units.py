"""纯函数与算法单元测试：分块 / RRF 融合 / 提案 diff / 配置合并 / 字数门禁工具。

这些是最便宜也最稳定的护栏：无 IO、无 LLM、无外部服务。
"""

from __future__ import annotations

import pytest

from src.config.app_config import deep_merge
from src.memory.hybrid import HybridRetriever
from src.memory.retriever import RetrievedChunk, chunk_markdown
from src.web.inkforge_proposals import _build_hunks


class TestChunkMarkdown:
    def test_empty_returns_empty(self):
        assert chunk_markdown("") == []
        assert chunk_markdown("   \n\n  ") == []

    def test_small_doc_is_single_chunk(self):
        assert chunk_markdown("一段短文本。") == ["一段短文本。"]

    def test_split_by_heading(self):
        chunks = chunk_markdown("# 一\n\nA\n\n# 二\n\nB")
        assert len(chunks) == 2

    def test_heading_stays_with_body(self):
        chunks = chunk_markdown("# 标题\n\n正文内容")
        assert chunks[0].startswith("# 标题")

    def test_long_section_split_by_paragraph(self):
        para = "x" * 400
        chunks = chunk_markdown(f"{para}\n\n{para}\n\n{para}", max_chars=600)
        assert len(chunks) >= 2
        assert all(len(c) <= 900 for c in chunks)

    def test_oversized_single_paragraph_hard_split(self):
        chunks = chunk_markdown("y" * 1500, max_chars=600)
        assert len(chunks) >= 3
        assert all(len(c) <= 600 for c in chunks)

    def test_content_is_preserved(self):
        text = "甲" * 700 + "\n\n" + "乙" * 700
        joined = "".join(chunk_markdown(text, max_chars=600))
        assert joined.count("甲") == 700
        assert joined.count("乙") == 700

    def test_respects_max_chars_for_normal_input(self):
        text = "\n\n".join("段落" * 20 for _ in range(10))
        for chunk in chunk_markdown(text, max_chars=300):
            assert len(chunk) <= 600


def _chunk(text: str, chapter: int = 0) -> RetrievedChunk:
    return RetrievedChunk(text=text, metadata={"chapter": chapter}, distance=0.0)


class _StubIndex:
    """可控向量检索替身：把预设结果回放给 HybridRetriever。"""

    def __init__(self, dense: list[RetrievedChunk], corpus: list[RetrievedChunk] | None = None):
        self._dense = dense
        self._corpus = corpus if corpus is not None else dense

    def query(self, *args, **kwargs):
        return list(self._dense)

    def get_corpus(self, *args, **kwargs):
        return list(self._corpus)


class TestHybridRetriever:
    def test_returns_at_most_top_k(self):
        index = _StubIndex([_chunk(f"doc{i}") for i in range(10)])
        hits = HybridRetriever(index).query("query", top_k=3)
        assert len(hits) <= 3

    def test_vector_only_result_survives(self):
        index = _StubIndex([_chunk("唯一命中")], corpus=[])
        hits = HybridRetriever(index).query("query", top_k=3)
        assert any(h.text == "唯一命中" for h in hits)

    def test_bm25_only_result_survives(self):
        """仅 BM25 命中（向量未召回）的块也必须进入结果——这正是混合检索的价值。

        注意：BM25Okapi 的 IDF 在小语料上为 0（术语出现在过半文档时 log(1)=0），
        故用 10 篇语料、目标词仅出现一次构造正 IDF。
        """
        target = "完全匹配的查询词"
        corpus = [_chunk(target)] + [_chunk(f"无关内容{i}") for i in range(9)]
        index = _StubIndex([_chunk("无关内容0")], corpus=corpus)
        hits = HybridRetriever(index).query(target, top_k=3)
        assert any(h.text == target for h in hits)

    def test_duplicate_text_merged_not_duplicated(self):
        same = _chunk("重复块")
        index = _StubIndex([same], corpus=[same])
        hits = HybridRetriever(index).query("重复块", top_k=5)
        assert [h.text for h in hits].count("重复块") == 1

    def test_rrf_ranks_double_hit_above_single_hit(self):
        """两路同时召回的块，RRF 分数应高于仅一路召回的块。"""
        both_text = "被两路同时召回"
        dense_only_text = "仅向量召回"
        both = _chunk(both_text)
        dense_only = _chunk(dense_only_text)
        corpus = [both] + [_chunk(f"其它文档{i}") for i in range(9)]
        index = _StubIndex([dense_only, both], corpus=corpus)
        hits = HybridRetriever(index).query(both_text, top_k=2)
        assert hits[0].text == both_text

    def test_time_decay_prefers_recent_chapter(self):
        old = _chunk("相同内容", chapter=1)
        new = _chunk("相同内容2", chapter=99)
        index = _StubIndex([old, new], corpus=[])
        hits = HybridRetriever(index).query("相同内容", top_k=2, current_chapter=100)
        assert hits[0].text == "相同内容2"

    def test_no_time_decay_without_current_chapter(self):
        a = _chunk("aaa", chapter=1)
        b = _chunk("bbb", chapter=99)
        index = _StubIndex([a, b], corpus=[])
        hits = HybridRetriever(index).query("q", top_k=2)
        assert {h.text for h in hits} == {"aaa", "bbb"}

    def test_invalidate_clears_cache(self):
        index = _StubIndex([_chunk("x")])
        retriever = HybridRetriever(index)
        retriever.query("q")
        assert retriever._bm25_cache
        retriever.invalidate()
        assert not retriever._bm25_cache

    def test_empty_corpus_does_not_crash(self):
        index = _StubIndex([], corpus=[])
        assert HybridRetriever(index).query("q") == []

    def test_distance_is_negative_score(self):
        index = _StubIndex([_chunk("a")], corpus=[])
        hits = HybridRetriever(index).query("a")
        assert hits[0].distance < 0


class TestProposalHunks:
    def test_identical_text_produces_no_hunks(self):
        hunks, adds, dels, truncated = _build_hunks("同一段文字", "同一段文字")
        assert hunks == [] and adds == 0 and dels == 0 and truncated is False

    def test_pure_addition(self):
        hunks, adds, dels, _ = _build_hunks("A\nB", "A\nB\nC")
        assert adds == 1 and dels == 0
        assert any(line["type"] == "addition" and line["text"] == "C" for h in hunks for line in h["lines"])

    def test_pure_deletion(self):
        hunks, adds, dels, _ = _build_hunks("A\nB\nC", "A\nC")
        assert adds == 0 and dels == 1

    def test_modification_counts_both(self):
        _, adds, dels, _ = _build_hunks("旧的一行", "新的一行")
        assert adds == 1 and dels == 1

    def test_context_lines_included(self):
        original = "\n".join(f"line{i}" for i in range(1, 21))
        proposed = original.replace("line10", "changed10")
        hunks, _, _, _ = _build_hunks(original, proposed)
        types = [line["type"] for line in hunks[0]["lines"]]
        assert "context" in types
        assert types.count("context") >= 3

    def test_truncation_flag_on_large_diff(self):
        original = "\n".join(f"a{i}" for i in range(600))
        proposed = "\n".join(f"b{i}" for i in range(600))
        _, _, _, truncated = _build_hunks(original, proposed)
        assert truncated is True

    def test_no_truncation_for_small_diff(self):
        _, _, _, truncated = _build_hunks("A\nB\nC", "A\nX\nC")
        assert truncated is False

    def test_hunk_has_line_numbers(self):
        hunks, _, _, _ = _build_hunks("A\nB\nC", "A\nX\nC")
        deletion = next(line for line in hunks[0]["lines"] if line["type"] == "deletion")
        addition = next(line for line in hunks[0]["lines"] if line["type"] == "addition")
        assert deletion["old"] == 2 and deletion["new"] == ""
        assert addition["new"] == 2 and addition["old"] == ""


class TestDeepMerge:
    def test_scalar_override(self):
        assert deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge_keeps_siblings(self):
        base = {"s": {"x": 1, "y": 2}}
        assert deep_merge(base, {"s": {"y": 9}}) == {"s": {"x": 1, "y": 9}}

    def test_list_is_replaced_not_concatenated(self):
        assert deep_merge({"l": [1, 2, 3]}, {"l": [9]}) == {"l": [9]}

    def test_new_key_added(self):
        assert deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_inputs_not_mutated(self):
        base = {"s": {"x": 1}}
        overlay = {"s": {"y": 2}}
        deep_merge(base, overlay)
        assert base == {"s": {"x": 1}}
        assert overlay == {"s": {"y": 2}}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        assert deep_merge(base, {"a": {"b": {"d": 3}}}) == {"a": {"b": {"c": 1, "d": 3}}}


class TestWriterHelpers:
    def test_chapter_length_counts_cjk(self):
        from src.agents.writer import chapter_length

        assert chapter_length("中文五个字") == 5

    def test_chapter_length_ignores_whitespace(self):
        from src.agents.writer import chapter_length

        assert chapter_length("a b\nc") < 6

    def test_length_deviation_is_absolute(self):
        """契约：length_deviation 返回**绝对值**（门禁只关心是否超差，不关心方向）。"""
        from src.agents.writer import length_deviation

        assert length_deviation(3500, 3000) == 500
        assert length_deviation(2500, 3000) == 500
        assert length_deviation(3000, 3000) == 0

    def test_length_revision_note_distinguishes_direction(self):
        """方向信息由 revision_note 承载：低于下限要求加戏补足，超上限才要求删减。

        非对称口径（用户要求）：`tolerance` 实参 = **最低可接受字数**，`ceiling` = 最高可接受字数。
        目标 3000 → 可接受 2500-5000；实际 2000 属"低于下限"，4000 属"区间内"（不该被打回）。
        """
        from src.agents.writer import length_revision_note

        short = length_revision_note(3000, 2000, 2500, 5000)
        long = length_revision_note(3000, 6000, 2500, 5000)
        assert "少于目标" in short and "加戏" in short
        assert "还差 1000 字" in short      # 差额按"到目标"算
        assert "超出目标" in long and "删减" in long

    def test_human_revision_notes_targeted(self):
        from src.agents.writer import human_revision_notes

        note = human_revision_notes("第 2 段情绪不到位", "targeted")
        assert "第 2 段情绪不到位" in note
        assert "局部" in note or "定向" in note

    def test_human_revision_notes_rewrite(self):
        from src.agents.writer import human_revision_notes

        note = human_revision_notes("整体重来", "rewrite")
        assert "整体重来" in note
        assert "重写" in note


class TestEditorVerdict:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [(9.0, "pass"), (8.0, "pass"), (7.9, "partial_rewrite"), (6.0, "partial_rewrite"),
         (5.9, "full_rewrite"), (0.0, "full_rewrite")],
    )
    def test_verdict_thresholds(self, score, expected):
        from src.agents.editor import verdict_of

        assert verdict_of(score) == expected


class TestSchedulerTopology:
    def _outline(self, deps: dict[int, list[int]]) -> dict:
        return {
            "volumes": [
                {"volume": vid, "title": f"卷{vid}", "depends_on": d, "chapters": []}
                for vid, d in deps.items()
            ]
        }

    def test_independent_volumes_share_one_wave(self):
        from src.orchestrator.scheduler import compute_waves

        assert compute_waves(self._outline({1: [], 2: [], 3: []})) == [[1, 2, 3]]

    def test_linear_chain_produces_sequential_waves(self):
        from src.orchestrator.scheduler import compute_waves

        assert compute_waves(self._outline({1: [], 2: [1], 3: [2]})) == [[1], [2], [3]]

    def test_diamond_dependency(self):
        from src.orchestrator.scheduler import compute_waves

        waves = compute_waves(self._outline({1: [], 2: [1], 3: [1], 4: [2, 3]}))
        assert waves[0] == [1]
        assert waves[1] == [2, 3]
        assert waves[2] == [4]

    def test_self_dependency_ignored(self):
        from src.orchestrator.scheduler import compute_waves

        assert compute_waves(self._outline({1: [1]})) == [[1]]

    def test_unknown_dependency_ignored(self):
        from src.orchestrator.scheduler import compute_waves

        assert compute_waves(self._outline({1: [99]})) == [[1]]

    def test_cycle_raises(self):
        from src.orchestrator.scheduler import compute_waves

        with pytest.raises(ValueError):
            compute_waves(self._outline({1: [2], 2: [1]}))

    def test_empty_outline(self):
        from src.orchestrator.scheduler import compute_waves

        assert compute_waves({"volumes": []}) == []


class TestPlanForChapter:
    def _state(self):
        return {
            "outline": {
                "volumes": [
                    {
                        "volume": 1,
                        "chapters": [
                            {"chapter": 1, "title": "一", "outline": "o1", "characters": ["A"]},
                            {"chapter": 2, "title": "二", "outline": "o2"},
                        ],
                    },
                    {"volume": 2, "chapters": [{"chapter": 3, "title": "三", "outline": "o3"}]},
                ]
            }
        }

    def test_finds_chapter_and_volume(self):
        from src.orchestrator.state import plan_for_chapter

        plan = plan_for_chapter(self._state(), 3)
        assert plan["volume"] == 2
        assert plan["title"] == "三"

    def test_missing_chapter_returns_none(self):
        from src.orchestrator.state import plan_for_chapter

        assert plan_for_chapter(self._state(), 99) is None

    def test_chapter_plans_flattened(self):
        from src.orchestrator.state import chapter_plans

        plans = chapter_plans(self._state())
        assert [p["chapter"] for p in plans] == [1, 2, 3]


class TestProviderModelList:
    """「获取模型」能力：接入点要能报出可用模型名，失败必须给出可读原因。

    全部离线：直接替换 provider 的 SDK client，不发任何真实请求。
    """

    @staticmethod
    def _provider(list_impl):
        from types import SimpleNamespace

        from src.llm.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider(
            "t", base_url="http://127.0.0.1:9/v1", api_key="sk-x"
        )
        provider._client = SimpleNamespace(models=SimpleNamespace(list=list_impl))
        return provider

    @staticmethod
    def _models(*ids):
        from types import SimpleNamespace

        return SimpleNamespace(data=[SimpleNamespace(id=i) for i in ids])

    def test_returns_sorted_unique_ids(self):
        provider = self._provider(lambda **kw: self._models("deepseek-chat", "deepseek-reasoner", "deepseek-chat"))
        assert provider.list_models() == ["deepseek-chat", "deepseek-reasoner"]

    def test_items_without_id_are_skipped(self):
        from types import SimpleNamespace

        page = SimpleNamespace(data=[SimpleNamespace(id="a"), SimpleNamespace()])
        assert self._provider(lambda **kw: page).list_models() == ["a"]

    def test_uses_short_timeout(self):
        """拉列表必须有界：超时沿用探活口径，不能借用正文生成的 10 分钟。"""
        from src.llm.openai_compat import PROBE_TIMEOUT_SECONDS

        seen: dict = {}

        def _list(**kw):
            seen.update(kw)
            return self._models("m")

        self._provider(_list).list_models()
        assert seen["timeout"] == PROBE_TIMEOUT_SECONDS

    def test_missing_models_route_is_explained(self):
        from src.llm.base import ProviderError

        class _NotFound(Exception):
            status_code = 404

        def _raise(**kw):
            raise _NotFound("Not Found")

        with pytest.raises(ProviderError) as err:
            self._provider(_raise).list_models()
        assert "/models" in str(err.value)

    def test_rejected_key_is_explained(self):
        from src.llm.base import ProviderError

        class _Unauthorized(Exception):
            status_code = 401

        def _raise(**kw):
            raise _Unauthorized("Unauthorized")

        with pytest.raises(ProviderError) as err:
            self._provider(_raise).list_models()
        assert "API Key" in str(err.value)

    def test_unsupported_provider_type_fails_loudly(self):
        """默认实现必须抛错：返回空列表会被读成「这个接入点没有模型」。"""
        from src.llm.base import ModelProvider, ProviderError

        class _Bare(ModelProvider):
            def chat(self, messages, model, temperature=0.7, max_tokens=None, json_mode=False):
                raise NotImplementedError

            def probe(self, model):
                raise NotImplementedError

        with pytest.raises(ProviderError):
            _Bare("bare").list_models()


class TestProviderCustomHeaders:
    """接入点自定义请求头：网关要求 x-opencode-session 之类的头时，列表拿得到≠模型能用。

    由来（2026-09-21 用户实测）：opencode-go 的 /models 返回 200，/chat/completions 却
    400 MissingSessionID —— 只有能配请求头，这类网关才用得起来。
    """

    def test_openai_compat_client_carries_headers(self):
        from src.config.settings import ProviderConfig
        from src.llm.registry import _build_provider

        provider = _build_provider(
            "gw",
            ProviderConfig(
                type="openai_compat",
                base_url="http://127.0.0.1:9/v1",
                api_key="k",
                headers={"x-opencode-session": "s-1", "User-Agent": "inkforge/0.1"},
            ),
        )
        sent = dict(provider._client.default_headers)
        assert sent["x-opencode-session"] == "s-1"
        assert sent["User-Agent"] == "inkforge/0.1"

    def test_no_headers_stays_none(self):
        from src.config.settings import ProviderConfig

        cfg = ProviderConfig(type="openai_compat", base_url="http://127.0.0.1:9/v1")
        assert cfg.headers == {}

    def test_anthropic_client_carries_headers(self):
        from src.config.settings import ProviderConfig
        from src.llm.registry import _build_provider

        provider = _build_provider(
            "gw",
            ProviderConfig(type="anthropic", api_key="k", headers={"x-probe": "1"}),
        )
        assert provider._client.default_headers["x-probe"] == "1"

    def test_models_config_rejects_non_string_header(self):
        """请求头必须是字符串映射：写错的结构要在加载期就炸，而不是发请求时炸。"""
        from pydantic import ValidationError

        from src.config.settings import ModelsConfig

        with pytest.raises(ValidationError):
            ModelsConfig.model_validate(
                {
                    "providers": {
                        "p": {
                            "type": "openai_compat",
                            "base_url": "http://127.0.0.1:9/v1",
                            "headers": {"x": 1},
                        }
                    },
                    "roles": {
                        "architect": {"provider": "p", "model": "m"},
                        "writer": {"provider": "p", "model": "m"},
                        "editor": {"provider": "p", "model": "m"},
                    },
                }
            )


class TestDotenvRefresh:
    """新增的 .env 键不必重启引擎（否则「测试」会报环境变量未设置，而文件里明明写好了）。"""

    def test_new_key_is_picked_up_without_restart(self, tmp_path, monkeypatch):
        import os

        from src.config import settings as settings_mod

        monkeypatch.setattr(settings_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.delenv("INKFORGE_PROBE_REFRESH_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "INKFORGE_PROBE_REFRESH_KEY=sk-added-later\n", encoding="utf-8"
        )
        settings_mod._load_dotenv()
        assert os.environ["INKFORGE_PROBE_REFRESH_KEY"] == "sk-added-later"

    def test_existing_env_var_is_not_overridden(self, tmp_path, monkeypatch):
        import os

        from src.config import settings as settings_mod

        monkeypatch.setattr(settings_mod, "PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("INKFORGE_PROBE_KEEP_KEY", "from-process")
        (tmp_path / ".env").write_text(
            "INKFORGE_PROBE_KEEP_KEY=from-file\n", encoding="utf-8"
        )
        settings_mod._load_dotenv()
        assert os.environ["INKFORGE_PROBE_KEEP_KEY"] == "from-process"
