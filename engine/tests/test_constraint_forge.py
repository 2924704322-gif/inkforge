"""风格工坊「约束提炼」模块回归（第三个模块 · 专职智能体）。

覆盖三组不变量（对应用户需求的三条硬约束）：

1. **格式**：产出必须是 `- 条目`，能被 `prompt_loader.constraint_items()` 切成执行清单
   —— 否则约束进不了"逐条核对"的刚性协议，等于没生效（既有链路的既定契约）。
2. **隔离（禁止看小说内容）**：这不是提示词里的一句请求，而是结构性边界——
   端点的请求体里没有 novel，实现里不碰 store，**作品内容无从进入模型调用**。
   本文件用「造一本带唯一哨兵串的书 → 真发一次请求 → 断言模型收到的每个字都不含哨兵」
   把它钉成可证伪的断言（而不是读一遍代码说"应该没读"）。
3. **契约**：端点与智能体注册的对外形状，含失败路径（空要求 / 超长 / 模型空回 / 模型报错）。

全部用例零真实 LLM 调用（ModelRegistry 打桩）。
"""

from __future__ import annotations

import ast
import inspect

import frontmatter
import pytest

#: 只写在作品正文里的哨兵串；一旦它出现在模型调用或响应里，隔离即被击穿
SENTINEL = "玄霜剑诀第七式不可外传密文"


def _code_identifiers(mod) -> set[str]:
    """取模块**代码层**的标识符（Name / Attribute / import 别名）。

    刻意走 AST 而不做字符串搜索：注释与文档字符串里合法地会写
    "不 import MdStore"这类说明，字符串搜索会把说明本身当成违规（自证式误报）。
    """
    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
    return names


def _seed_book_with_sentinel(novels_dir, novel_id: str = "isolation-book"):
    """造一本正文里带哨兵串的书：用于探测"提炼智能体有没有偷看作品"。"""
    d = novels_dir / novel_id
    (d / "settings").mkdir(parents=True, exist_ok=True)
    (d / "chapters" / "vol-01").mkdir(parents=True, exist_ok=True)
    (d / "summaries").mkdir(exist_ok=True)
    (d / "settings" / "outline.md").write_text(
        frontmatter.dumps(frontmatter.Post(f"# 大纲\n\n{SENTINEL}（大纲里的机密设定）", title="机密书")),
        encoding="utf-8",
        newline="\n",
    )
    (d / "chapters" / "vol-01" / "ch-001.md").write_text(
        frontmatter.dumps(
            frontmatter.Post(f"第一章正文。{SENTINEL}。主角拔剑。", chapter=1, volume=1,
                             title="第一章", status="approved", characters=[SENTINEL])
        ),
        encoding="utf-8",
        newline="\n",
    )
    return d


@pytest.fixture()
def spy_registry(monkeypatch):
    """ModelRegistry 打桩：记录每次调用的 messages，返回调用方指定内容。

    返回值 make(reply) 里 reply 可以是字符串，也可以是异常实例（异常会照原样抛出，
    用来验失败路径）。
    """
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


class TestFormatContract:
    """产出格式：`- 条目` 是硬指标（下游执行清单的解析契约）。"""

    def test_extracts_bullets_of_all_styles(self):
        from src.services import constraint_forge as f

        raw = (
            "- **禁忌**：不要系统流\n"
            "* 每章结尾留钩子\n"
            "2. 单段不超过 4 行\n"
            "1）对话占比不低于 40%\n"
        )
        assert f.normalize_constraints(raw, 10) == [
            "**禁忌**：不要系统流",
            "每章结尾留钩子",
            "单段不超过 4 行",
            "对话占比不低于 40%",
        ]

    def test_drops_fences_and_headings_keeps_inner_items(self):
        """整段被围栏包住是模型的常见写法：围栏行要丢，围栏里的条目要留。"""
        from src.services import constraint_forge as f

        raw = "```markdown\n# 约束\n- 不要旁白解释\n- 结尾必须留钩子\n```"
        assert f.normalize_constraints(raw, 10) == ["不要旁白解释", "结尾必须留钩子"]

    def test_dedupes_exact_and_contained_items(self):
        """完全重复 + 互相包含都只留一条（模型实测会写「不要系统流」与「**禁忌**：不要系统流」两条）。"""
        from src.services import constraint_forge as f

        raw = "- 不要系统流\n- **禁忌**：不要系统流\n- 不要系统流。"
        assert f.normalize_constraints(raw, 10) == ["**禁忌**：不要系统流"]

    def test_bold_label_is_not_mangled(self):
        """回归：去重前用字符集 lstrip 会把 `**加粗**` 的星号啃掉，产出过 `A**：x` 残行。"""
        from src.services import constraint_forge as f

        assert f.normalize_constraints("- **标签**：内容", 5) == ["**标签**：内容"]

    def test_falls_back_to_prose_split(self):
        """模型整段散文（无列表符号）时也必须产出条目，否则格式保证不成立。"""
        from src.services import constraint_forge as f

        items = f.normalize_constraints("要冷硬克制。不要系统流；每章结尾必须留钩子", 10)
        assert items == ["要冷硬克制", "不要系统流", "每章结尾必须留钩子"]

    def test_limit_is_clamped_to_legal_range(self):
        from src.services import constraint_forge as f

        raw = "\n".join(f"- 第{i}条要求" for i in range(1, 30))
        assert len(f.normalize_constraints(raw, 1)) == f.MIN_MAX_ITEMS  # 下限
        assert len(f.normalize_constraints(raw, 999)) == 29             # 上限内全部保留
        assert f.clamp_max_items(0) == f.DEFAULT_MAX_ITEMS              # 0 = 默认

    def test_output_is_parseable_by_constraint_checklist(self):
        """**关键契约**：提炼结果必须能被既有渲染器切成执行清单条目（≥2 条）。"""
        from src.agents.prompt_loader import constraint_items
        from src.services import constraint_forge as f

        text = f.render_constraints(
            f.normalize_constraints("- 不要系统流\n- 每章结尾留钩子\n- 单段不超过 4 行", 10)
        )
        items = constraint_items(text)
        assert len(items) >= 2, f"产出必须能进执行清单：{text!r} → {items!r}"
        assert items == ["不要系统流", "每章结尾留钩子", "单段不超过 4 行"]

    def test_title_uses_first_clause_and_hint(self):
        from src.services import constraint_forge as f

        assert f.suggest_title("写一个冷硬修仙文，不要系统流") == "写一个冷硬修仙文"
        assert f.suggest_title("随便什么", title_hint="我的规范") == "我的规范"
        assert f.suggest_title("，。") == "自定义约束"


class TestIsolation:
    """**禁止看小说内容** —— 用结构断言 + 行为断言双重钉死。"""

    def test_module_source_never_touches_novels(self):
        """服务层与端点源码里不得出现任何作品数据入口（静态闸）。"""
        import src.services.constraint_forge as svc
        import src.web.inkforge_forge as api

        forbidden = {
            "MdStore", "open_store", "store_factory", "novels_dir",
            "list_chapters", "list_documents", "chapter_rel_path",
        }
        for mod in (svc, api):
            hits = _code_identifiers(mod) & forbidden
            assert not hits, f"{mod.__name__} 出现了作品数据入口 {sorted(hits)}"

    def test_no_novel_parameter_in_signature(self):
        """服务层的入口函数签名里没有 novel/book/store —— 调用方想塞也无处可塞。"""
        from src.services import constraint_forge as f

        params = set(inspect.signature(f.build_messages).parameters)
        params |= set(inspect.signature(f.extract_constraints).parameters)
        assert not (params & {"novel", "book", "novel_id", "store", "chapter"}), params

    def test_request_body_schema_has_no_novel_field(self, app_client):
        """端点请求体只接受「用户粘贴的两段文本 + 条数/名字」：多一个 novel 都是隔离缺口。"""
        client, _ = app_client
        schema = client.get("/openapi.json").json()
        body = schema["paths"]["/api/style-forge/constraints"]["post"]["requestBody"]
        ref = body["content"]["application/json"]["schema"]["$ref"]
        props = schema["components"]["schemas"][ref.rsplit("/", 1)[-1]]["properties"]
        assert set(props) == {"source_text", "requirement", "max_items", "title_hint"}, sorted(props)

    def test_requirement_query_params_are_empty(self, app_client):
        """端点上不得挂任何查询参数（novel 之类一旦出现就是"顺手把书塞进来"的入口）。"""
        client, _ = app_client
        op = client.get("/openapi.json").json()["paths"]["/api/style-forge/constraints"]["post"]
        assert not op.get("parameters"), op.get("parameters")

    def test_pasted_text_reaches_model_but_book_content_does_not(self, sandbox, spy_registry):
        """**新语义的核心隔离断言**（2026-09-19 方向订正后重写）：

        用户粘贴的正文**必须**进模型（这是功能要求：智能体要在它上面提炼）；
        而作品库里的书稿（用户没粘贴的）**一个字节都不能进**。
        做法：书里埋哨兵 A、输入框粘贴含哨兵 B 的正文 → 断言报文含 B、不含 A。
        """
        from fastapi.testclient import TestClient

        from src.web import server as server_mod

        _seed_book_with_sentinel(sandbox.novels)
        pasted = "这一段是作者自己粘贴进来的正文，里面埋着另一个哨兵：粘-贴-哨-兵-乙。"
        captured = spy_registry("**文风**：叙述冷硬克制\n- 单段不超过 4 行")

        app = server_mod.create_app("isolation-book")
        with TestClient(app) as client:
            res = client.post(
                "/api/style-forge/constraints",
                json={"source_text": pasted, "requirement": "提炼这段的文风与节奏约束"},
            )
        assert res.status_code == 200, res.text

        assert captured, "没有捕获到模型调用"
        blob = "\n".join(m.content for c in captured for m in c["messages"])
        # ① 作品库里的书稿（未粘贴）不得出现
        assert SENTINEL not in blob, "作品库正文/大纲泄漏进了模型调用"
        assert SENTINEL not in res.text, "响应里回带了作品库内容"
        # ② 用户粘贴的正文必须出现（否则功能就是坏的）
        assert "粘-贴-哨-兵-乙" in blob, "粘贴的正文没有进模型——它就没东西可提炼"
        assert "提炼这段的文风与节奏约束" in blob
        # 消息结构：system（含第零条）+ user（需求 + 正文），无其它上下文注入
        assert len(captured[0]["messages"]) == 2, "除 system/user 外不应有额外上下文注入"
        assert captured[0]["messages"][0].role == "system"
        assert captured[0]["messages"][1].role == "user"
        assert res.json()["isolation"]

    def test_works_without_any_book_open(self, sandbox, spy_registry):
        """没有书、没有工作区上下文时也必须能跑 —— 它本来就与作品库无关。"""
        from fastapi.testclient import TestClient

        from src.web import server as server_mod

        spy_registry("- 不要系统流")
        with TestClient(server_mod.create_app("ghost-book")) as client:
            res = client.post("/api/style-forge/constraints", json={
                "source_text": "这是一段足够长的、作者粘贴进来的正文内容，用于验证无书场景。",
                "requirement": "提炼禁忌",
            })
        assert res.status_code == 200, res.text
        assert res.json()["count"] == 1


class TestEndpointContract:
    #: 一段够长、能过正文下限的占位正文
    SRC = "这是一段作者粘贴进来的正文样本，用于走通端点契约：剑冢的雪落了三天，他把断剑插回石缝。"

    def test_agent_card(self, app_client):
        client, _ = app_client
        res = client.get("/api/style-forge/agent")
        assert res.status_code == 200
        body = res.json()
        assert body["key"] == "constraint"
        assert body["role"] == "constraint"
        # 视野声明是 UI 直接展示给用户的"它看不看我的书"的答案（新语义：看什么由你粘贴决定）
        never = "".join(body["never_sees"])
        assert "作品库" in never and "没有粘贴" in never, never
        assert any("粘贴" in s for s in body["sees"]), body["sees"]
        # 运行时提示词与其它智能体同源装配：必带第零条
        assert "第零条" in body["prompt"]
        assert body["max_items"]["default"] >= body["max_items"]["min"]
        assert body["limits"]["source_max"] > body["limits"]["requirement_max"]

    def test_happy_path_returns_copyable_bullets(self, app_client, spy_registry):
        client, _ = app_client
        captured = spy_registry("- **禁忌**：不要系统流\n- 每章结尾留一个具体钩子")
        res = client.post(
            "/api/style-forge/constraints",
            json={"source_text": self.SRC, "requirement": "提炼禁忌与结尾钩子", "max_items": 10},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["count"] == 2
        assert body["constraints"].splitlines() == [
            "- **禁忌**：不要系统流",
            "- 每章结尾留一个具体钩子",
        ]
        assert body["title"] == "提炼禁忌与结尾钩子"
        assert body["model"] == "spy-model" and body["provider"] == "spy-provider"
        assert captured[0]["role"] == "constraint"
        # 条数上限必须真的传进提示词（否则模型按自己心情写 30 条）
        assert "最多 10 条" in captured[0]["messages"][1].content
        # 不传 temperature = 沿用角色绑定（否则「模型配置」里改的温度对这个智能体无效）
        assert "temperature" not in captured[0]["kwargs"], captured[0]["kwargs"]

    def test_two_inputs_are_sent_separately_and_labelled(self, app_client, spy_registry):
        """两个输入位必须**分块可辨**：需求是"任务指令"，正文是"待分析材料"。"""
        client, _ = app_client
        captured = spy_registry("- 单段不超过 4 行")
        res = client.post(
            "/api/style-forge/constraints",
            json={"source_text": self.SRC, "requirement": "提炼文风与节奏"},
        )
        assert res.status_code == 200, res.text
        user = captured[0]["messages"][1].content
        assert "【提炼需求（你的任务指令，以此为准）】" in user
        assert "【正文内容（待分析材料" in user
        assert self.SRC in user and "提炼文风与节奏" in user
        # 正文被显式夹在分隔标记里，避免与指令混淆
        assert "<<<正文开始>>>" in user and "<<<正文结束>>>" in user
        assert user.index("提炼文风与节奏") < user.index(self.SRC), "需求应在正文之前"

    def test_empty_source_rejected(self, app_client):
        client, _ = app_client
        res = client.post("/api/style-forge/constraints",
                          json={"source_text": "   ", "requirement": "提炼文风"})
        assert res.status_code == 400
        assert "正文" in res.json()["detail"]

    def test_too_short_source_rejected(self, app_client):
        """几个字没有可提炼的信息：提前拦掉，省一次模型调用。"""
        client, _ = app_client
        res = client.post("/api/style-forge/constraints",
                          json={"source_text": "太短了", "requirement": "提炼文风"})
        assert res.status_code == 400
        assert "太短" in res.json()["detail"]

    def test_overlong_source_rejected(self, app_client):
        """正文上限：防一次贴整本书打爆上下文（提示分段提炼）。"""
        client, _ = app_client
        from src.services.constraint_forge import MAX_SOURCE_CHARS

        res = client.post("/api/style-forge/constraints", json={
            "source_text": "文" * (MAX_SOURCE_CHARS + 1), "requirement": "提炼文风",
        })
        assert res.status_code == 400
        assert "正文过长" in res.json()["detail"]

    def test_empty_requirement_rejected(self, app_client):
        client, _ = app_client
        res = client.post("/api/style-forge/constraints",
                          json={"source_text": self.SRC, "requirement": "   "})
        assert res.status_code == 400
        assert "提炼需求" in res.json()["detail"]

    def test_overlong_requirement_rejected(self, app_client):
        """需求上限：需求是一段话；写成长文多半是把正文误贴进了需求框（文案要点明这一点）。"""
        client, _ = app_client
        from src.services.constraint_forge import MAX_REQUIREMENT_CHARS

        res = client.post(
            "/api/style-forge/constraints",
            json={"source_text": self.SRC, "requirement": "要" * (MAX_REQUIREMENT_CHARS + 1)},
        )
        assert res.status_code == 400
        assert "提炼需求过长" in res.json()["detail"]
        assert "正文内容" in res.json()["detail"]

    def test_unparsable_output_is_not_reported_as_success(self, app_client, spy_registry):
        """模型空回时回 422（而不是把空约束当成功给用户复制）。"""
        client, _ = app_client
        spy_registry("")
        res = client.post("/api/style-forge/constraints", json={"source_text": self.SRC, "requirement": "不要系统流"})
        assert res.status_code == 422
        assert res.headers.get("X-Inkforge-Error-Source") == "parse"

    def test_gateway_error_page_is_rejected_not_saved_as_constraint(self, app_client, spy_registry):
        """上游错误页被当成正文返回时必须 422。

        由来（2026-09-19 对抗排查实测）：接入点 5xx 时 `<html>…Bad Gateway…</html>`
        会被规范化兜底切成**一条"约束"**并 200 返回 —— 用户于是把一段错误页存进约束库、
        还绑给了作品。整份产出都像错误页时直接判不可用。
        """
        client, _ = app_client
        spy_registry("<html><body><h1>502 Bad Gateway</h1></body></html>")
        res = client.post("/api/style-forge/constraints", json={"source_text": self.SRC, "requirement": "不要系统流"})
        assert res.status_code == 422, res.text
        assert "错误页" in res.json()["detail"]

    def test_garbage_signature_needs_the_whole_output(self):
        """判据是"每一条都像错误页"：正常约束里出现一次相关字样不该被误杀。"""
        from src.services.constraint_forge import looks_like_garbage

        assert looks_like_garbage(["- <html>502 Bad Gateway</html>"])
        assert not looks_like_garbage(["- 禁止写服务器报错台词：不要出现 500/服务不可用 之类字样"])
        assert not looks_like_garbage([])

    def test_provider_error_detail_is_redacted(self, app_client, spy_registry):
        """回给前端的 502 detail 不得把凭据带出去（上游错误里回显 Key 的情形）。"""
        client, _ = app_client
        from src.llm.base import ProviderError

        spy_registry(ProviderError(
            "deepseek", "500: {'error': {'message': 'boom sk-liveLEAK1234567890'}}"
        ))
        res = client.post("/api/style-forge/constraints", json={"source_text": self.SRC, "requirement": "不要系统流"})
        assert res.status_code == 502
        assert "sk-liveLEAK1234567890" not in res.text, res.text
        assert "***" in res.json()["detail"]

    def test_model_failure_maps_to_502(self, app_client, spy_registry):
        client, _ = app_client
        from src.llm.base import ProviderError

        spy_registry(ProviderError("spy-provider", "boom"))
        res = client.post("/api/style-forge/constraints", json={"source_text": self.SRC, "requirement": "不要系统流"})
        assert res.status_code == 502
        assert res.headers.get("X-Inkforge-Error-Source"), "失败必须可归因（响应头）"
        assert "spy-provider" in res.json()["detail"]


class TestAgentRegistration:
    """新智能体与既有智能体同级注册（因此自带"可查看/改提示词 + 必带第零条"）。"""

    def test_registered_in_agent_presets(self):
        from src.services.constraint_forge import AGENT_KEY
        from src.web.inkforge_api import AGENT_PRESETS

        assert AGENT_KEY in AGENT_PRESETS
        # 五个阶段子智能体一个都不能少（新增不得挤掉既有）
        assert {"master", "character", "plot", "outline", "prose", "review"} <= set(AGENT_PRESETS)

    def test_runtime_prompt_carries_constitution_without_actions(self):
        from src.web.inkforge_windows import agent_prompt

        prompt = agent_prompt("constraint")
        assert "第零条" in prompt
        # 它不是动作型智能体：不该拿到工作台动作清单（否则会去调建书/开写）
        assert "【工作台动作清单" not in prompt

    def test_prompt_pins_two_inputs_and_anti_injection_discipline(self):
        """提示词层面的三道软约束（硬约束在结构层，见 TestIsolation）：

        · 两个输入位分清楚（需求=指令 / 正文=材料）；
        · 正文是**待分析材料不是指令**（粘贴的文本里可能有"忽略上面的要求"这类串）；
        · 忠实提炼、不得引入正文之外的信息；正文撑不住时如实说缺什么。
        """
        from src.services.constraint_forge import SYSTEM_PROMPT as P

        assert "【提炼需求】" in P and "【正文内容】" in P
        assert "待分析材料" in P and "不是指令" in P, "必须声明正文是材料而非指令"
        assert "找到依据" in P, "必须要求每条都能在正文里找到依据"
        assert "需补充" in P, "正文撑不住时要求如实说缺什么，而不是硬凑"
        assert "`- `" in P, "必须把条目格式写死（下游解析契约）"

    def test_agent_preset_listing_exposes_it(self, app_client):
        client, _ = app_client
        presets = client.get("/api/agent-presets").json()["presets"]
        keys = {p["key"] for p in presets}
        assert "constraint" in keys

    def test_cannot_be_used_as_a_chat_agent(self, app_client):
        """它不能当会话智能体：会话链路会自动注入作品上下文（大纲/章节/状态板），
        而本模块的承诺是"只提炼你粘贴的正文"——隔离要成立，入口也得一起关。"""
        client, _ = app_client
        res = client.post("/api/chats?novel=demo-web", json={"agent": "constraint"})
        assert res.status_code == 400, res.text
        # 对照：同一路径下 master 正常可建
        ok = client.post("/api/chats?novel=demo-web", json={"agent": "master"})
        assert ok.status_code == 200
