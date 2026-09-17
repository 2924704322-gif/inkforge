"""P2/P3 墨师动作层测试：只读直执行 / 写动作需确认 / 跨书保护 / 审计落盘。

决策基线（用户确认）：
- **所有写动作一律需确认**——未确认时不得产生任何数据变更；
- 动作失败必须是"回执"（status=failed）而不是 500 掀翻整轮；
- 每次真实执行都要在审计日志里留痕（供看板展示）。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.services import library
from src.web import actions as act
from src.web.scope import WORKSPACE


@pytest.fixture()
def act_ctx(app_client, sandbox: SimpleNamespace):
    """构造动作执行上下文（默认书 demo-web；工作台当前书为空）。"""
    _client, server_mod = app_client
    hub = server_mod.SessionHub(3000)
    hub.get_or_create("demo-web")
    state = {"active": ""}

    def _active() -> str:
        return state["active"]

    ctx = act.ActionContext(hub=hub, default_novel="demo-web", active_novel=_active,
                            session_key="demo-web", book="demo-web")
    ctx.extra["_state"] = state
    return ctx


# ---------- 只读动作：免确认，直接执行 ----------

def test_read_actions_execute_directly(act_ctx):
    listed = act.execute("book_list", {}, ctx=act_ctx)
    assert listed.ok and listed.status == "ok"
    assert "demo-web" in listed.summary

    stat = act.execute("book_stat", {"novel": "demo-web"}, ctx=act_ctx)
    assert stat.ok and stat.data["novel"] == "demo-web"
    assert stat.data["approved"] == 2

    docs = act.execute("doc_list", {"novel": "demo-web"}, ctx=act_ctx)
    assert docs.ok and any(i["rel"] == "settings/outline.md" for i in docs.data["items"])

    outline = act.execute("outline_read", {"novel": "demo-web"}, ctx=act_ctx)
    assert outline.ok and "大纲" in outline.summary

    chapter = act.execute("chapter_read", {"novel": "demo-web", "chapter": 1}, ctx=act_ctx)
    assert chapter.ok and "第1章正文" in chapter.data["content"]

    search = act.execute("search_workspace", {"keyword": "第1章"}, ctx=act_ctx)
    assert search.ok and search.data["hits"]

    for op in ("material_list", "skill_list", "constraint_list", "model_config"):
        assert act.execute(op, {}, ctx=act_ctx).ok, op


def test_read_action_bad_args_are_receipts_not_crashes(act_ctx):
    bad_path = act.execute("doc_read", {"novel": "demo-web", "rel": "../evil.md"}, ctx=act_ctx)
    assert bad_path.status == "failed" and "非法文档路径" in bad_path.error

    missing = act.execute("chapter_read", {"novel": "demo-web", "chapter": 999}, ctx=act_ctx)
    assert missing.status == "failed" and "不存在" in missing.error

    unknown = act.execute("no_such_op", {}, ctx=act_ctx)
    assert unknown.status == "failed" and "未知动作" in unknown.error


# ---------- 写动作：必须确认 ----------

def test_book_create_requires_confirmation(act_ctx, sandbox: SimpleNamespace):
    pending = act.execute("book_create", {"novel_id": "newidea", "mode": "pipeline"},
                          ctx=act_ctx)
    assert pending.status == "pending_confirm" and pending.ok
    assert "将新建书目" in pending.summary and "newidea" in pending.summary
    # 未确认 → 磁盘上不得有任何变化
    assert not (sandbox.novels / "newidea").exists()

    done = act.execute("book_create", {"novel_id": "newidea", "mode": "pipeline"},
                       ctx=act_ctx, execute_write=True)
    assert done.ok and done.status == "ok"
    assert (sandbox.novels / "newidea" / "settings").is_dir()
    # 建书回执带可回滚提示
    assert done.data["undo"]["op"] == "book_delete"


def test_book_create_duplicate_is_visible_blocker(act_ctx):
    """重名不再"静默失败"：仍出确认卡，但卡上写明原因（用户看得见、点得动）。

    旧行为：预览期异常 → status=failed → **不登记待确认** → 用户既没卡也没得点。
    """
    dup = act.execute("book_create", {"novel_id": "demo-web"}, ctx=act_ctx)
    assert dup.status == "pending_confirm" and dup.ok
    assert "已存在" in dup.summary and dup.error and "已存在" in dup.error
    assert dup.pending and "preview_error" in dup.pending
    # 确认执行时仍必须失败（不能让重名把既有书覆盖掉）
    forced = act.execute("book_create", {"novel_id": "demo-web"}, ctx=act_ctx,
                         execute_write=True)
    assert forced.status == "failed" and "已存在" in forced.error


def test_preview_error_still_produces_confirm_card(act_ctx):
    """写动作预览期出错时必须仍出确认卡（回归：用户实测"点了没反应"）。

    旧行为：预览抛错 → status=failed → 不登记待确认 → 界面既没有卡也没有按钮。
    """
    # ① 目标不存在
    ghost = act.execute("book_select", {"novel_id": "ghost"}, ctx=act_ctx)
    assert ghost.status == "pending_confirm" and ghost.pending is not None
    assert "不存在" in ghost.pending["impact"] and ghost.pending["preview_error"]

    # ② 缺必填参数：卡上必须写明缺什么，且提示补齐后再执行
    missing = act.execute("gen_start", {"novel": "demo-web"}, ctx=act_ctx)
    assert missing.status == "pending_confirm"
    assert "brief" in missing.summary and "补齐" in missing.summary


def test_single_book_fallback_when_workspace(act_ctx):
    """只有一本书时省略 novel 允许自动落到那本书（实测模型常省略）。"""
    act_ctx.book = ""
    act_ctx.extra["_state"]["active"] = ""
    res = act.execute("book_stat", {"novel": ""}, ctx=act_ctx)
    assert res.ok and res.data["novel"] == "demo-web"


def test_write_action_requires_book_when_workspace(act_ctx):
    """工作区里多本书且都没说 → 明确报错，绝不静默落到默认书。"""
    library.create_book("second", "pipeline")   # 制造"多书"歧义
    act_ctx.book = ""
    act_ctx.extra["_state"]["active"] = ""
    res = act.execute("book_stat", {"novel": ""}, ctx=act_ctx)
    assert res.status == "failed" and "未指定书目" in res.error


def test_write_action_can_target_explicit_book(act_ctx):
    """显式指定另一本书时，动作必须作用在那本书上。"""
    library.create_book("other", "pipeline")
    res = act.execute("chapter_list", {"novel": "other"}, ctx=act_ctx)
    assert res.ok and res.data["novel"] == "other"


def test_book_delete_protects_default_and_active(act_ctx):
    # 默认书不可删：仍出卡但卡上写明拒绝原因（不再静默 failed）
    default = act.execute("book_delete", {"novel": "demo-web"}, ctx=act_ctx)
    assert default.status == "pending_confirm" and "默认书" in default.summary

    library.create_book("doomed", "pipeline")
    act_ctx.hub.get_or_create("demo-web")
    ghost = act.execute("book_delete", {"novel": "ghost"}, ctx=act_ctx)
    assert ghost.status == "pending_confirm" and "不存在" in ghost.summary
    assert act.execute("book_delete", {"novel": "doomed"}, ctx=act_ctx).status == "pending_confirm"
    done = act.execute("book_delete", {"novel": "doomed"}, ctx=act_ctx, execute_write=True)
    assert done.ok
    # 默认书"强行确认"也必须删不掉
    forced = act.execute("book_delete", {"novel": "demo-web"}, ctx=act_ctx, execute_write=True)
    assert forced.status == "failed" and "默认书" in forced.error


def test_gen_start_requires_brief_and_checks_running(act_ctx):
    missing = act.execute("gen_start", {"novel": "demo-web"}, ctx=act_ctx)
    # 缺 brief：出确认卡 + 卡上写明缺什么（用户看得到"还差一步"）
    assert missing.status == "pending_confirm" and "brief" in missing.summary
    assert act.execute("gen_start", {"novel": "demo-web"}, ctx=act_ctx,
                       execute_write=True).status == "failed"

    pending = act.execute(
        "gen_start",
        {"novel": "demo-web", "brief": "写一个赛博修仙", "chapters": 5, "parallel": True},
        ctx=act_ctx,
    )
    assert pending.status == "pending_confirm"
    assert "卷级并行" in pending.summary


def test_gen_decide_without_pending_is_visible_blocker(act_ctx):
    """没有待裁决项时：出卡并写明原因（而不是一句 failed 让用户无从下手）。"""
    res = act.execute("gen_decide", {"novel": "demo-web", "action": "approve"}, ctx=act_ctx)
    assert res.status == "pending_confirm" and "没有待裁决项" in res.summary


def test_select_and_clear_pending_via_execute_pending(act_ctx):
    pending = {"op": "book_select", "args": {"novel_id": "demo-web"}}
    res = act.execute_pending(act_ctx, pending)
    assert res.ok and res.data["novel"] == "demo-web"

    library.create_book("tmp", "pipeline")
    bad = act.execute_pending(act_ctx, {"op": "book_select", "args": {"novel_id": "nope"}})
    assert bad.status == "failed"


# ---------- 审计 ----------

def test_audit_records_executions_and_proposals(act_ctx, sandbox: SimpleNamespace):
    """审计覆盖"提议 → 确认 → 执行"全链路。

    · 只读动作：直接执行并留痕；
    · 写动作未确认：留一条 ``pending_confirm``（状态不是 ok，便于区分"提议"与"已执行"）；
    · 写动作确认后：再留一条真正执行的记录。
    """
    act.execute("book_list", {}, ctx=act_ctx)                       # 只读 → ok
    act.execute("book_create", {"novel_id": "aud1"}, ctx=act_ctx)   # 未确认 → pending_confirm
    records = act.read_audit(50)
    assert any(r["op"] == "book_list" and r["status"] == "ok" for r in records)
    proposed = [r for r in records if r["op"] == "book_create"]
    assert proposed and all(r["status"] == "pending_confirm" for r in proposed)

    act.execute("book_create", {"novel_id": "aud1"}, ctx=act_ctx, execute_write=True)
    records = act.read_audit(50)
    entry = next(r for r in records
                 if r["op"] == "book_create" and r["status"] == "ok")
    assert entry["ok"] is True and entry["scope"] == "write"
    assert "args_digest" in entry and "novel_id" not in json.dumps(entry)

    # 失败也留痕（排障用）
    act.execute("chapter_read", {"novel": "demo-web", "chapter": 999}, ctx=act_ctx)
    assert any(r["op"] == "chapter_read" and r["ok"] is False for r in act.read_audit(50))


def test_audit_trim_keeps_tail(sandbox: SimpleNamespace, monkeypatch):
    monkeypatch.setattr(act, "AUDIT_KEEP_LINES", 5)
    path = act.audit_path()
    path.write_text("".join(json.dumps({"n": i}) + "\n" for i in range(20)), encoding="utf-8")
    act.write_audit({"n": 99})
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5 and json.loads(lines[-1])["n"] == 99


# ---------- 协议解析（回复里的动作块 / 确认词） ----------

def test_parse_actions_extracts_and_strips_block():
    from src.web.inkforge_api import _parse_actions

    text = ("好的，我先看看书架。\n<INKFORGE_ACTIONS>\n"
            '{"actions": [{"op": "book_list", "args": {}}]}\n</INKFORGE_ACTIONS>')
    clean, acts = _parse_actions(text)
    assert clean.strip() == "好的，我先看看书架。"
    assert acts == [{"op": "book_list", "args": {}}]

    assert _parse_actions("没有动作块") == ("没有动作块", [])
    broken, none = _parse_actions("<INKFORGE_ACTIONS>{not json}</INKFORGE_ACTIONS>")
    assert none == [] and broken == ""


def test_pending_decision_words():
    from src.web.inkforge_api import _pending_decision

    for word in ("确认", "确定", "执行", "同意", "可以", "好的", "OK", "yes"):
        assert _pending_decision(word) == "approve", word
    for word in ("取消", "算了", "不用了", "先不", "cancel"):
        assert _pending_decision(word) == "cancel", word
    assert _pending_decision("这本书写得怎么样") == ""
    assert _pending_decision("确认" * 20) == ""      # 过长句子不当作确认


# ---------- 别名容错（真实模型实测会长这样写） ----------

def test_op_alias_normalization():
    """模型容易把 book_list 写成 list_books：必须容错而不是直接失败。"""
    assert act.normalize_op("list_books") == "book_list"
    assert act.normalize_op("list_chapters") == "chapter_list"
    assert act.normalize_op("read_chapter") == "chapter_read"
    assert act.normalize_op("create_book") == "book_create"
    assert act.normalize_op("start_generation") == "gen_start"
    assert act.normalize_op("book_list") == "book_list"        # 规范名原样
    assert act.normalize_op("BOOK_LIST") == "book_list"        # 大小写容错
    assert act.normalize_op("nonsense_op") == "nonsense_op"    # 未知名保持未知


def test_aliased_action_executes(act_ctx):
    """别名命中时真的执行（不是"识别了但没做"）。"""
    res = act.execute("list_books", {}, ctx=act_ctx)
    assert res.ok and res.op == "book_list" and "demo-web" in res.summary


def test_parse_actions_accepts_both_shapes():
    """两种动作块形态都要能解析：{op,args} 与 {动作名: args}。"""
    from src.web.inkforge_api import _parse_actions

    a = ('<INKFORGE_ACTIONS>{"actions":[{"op":"book_list","args":{}}]}</INKFORGE_ACTIONS>')
    assert _parse_actions(a)[1] == [{"op": "book_list", "args": {}}]

    b = ('<INKFORGE_ACTIONS>{"actions":[{"list_chapters":{"novel":"demo-web"}},'
         '{"book_list":null}]}</INKFORGE_ACTIONS>')
    parsed = _parse_actions(b)[1]
    assert parsed[0] == {"op": "list_chapters", "args": {"novel": "demo-web"}}
    assert parsed[1] == {"op": "book_list", "args": {}}


def test_failed_action_is_visible_in_receipt():
    """未知动作/失败必须如实进回执与答复文本（不能静默吞掉）。"""
    from src.web.inkforge_api import _action_response_text, _summary_from_results

    results = [{"op": "nonsense_op", "status": "failed", "ok": False,
                "error": "未知动作：nonsense_op"}]
    receipt = _summary_from_results(results)
    assert "失败" in receipt and "nonsense_op" in receipt
    text = _action_response_text(results)
    assert "未成功" in text and "nonsense_op" in text


def test_action_manifest_marks_scopes():
    ops = {a["op"]: a for a in act.manifest()}
    assert ops["book_list"]["scope"] == "read"
    assert ops["book_create"]["scope"] == "write"
    # 学习仿写/素材读取必须可被墨师调用（用户实测过"找不出学习仿写历史"）
    for op in ("learning_list", "learning_read", "material_read"):
        assert ops[op]["scope"] == "read", op
    assert act.MAX_ACTIONS_PER_TURN >= 1
    assert WORKSPACE == "__workspace__"


def test_learning_actions_list_and_read(act_ctx, sandbox: SimpleNamespace):
    """学习仿写历史：能列出、能读全文（这是用户报的"墨师找不到"的回归）。"""
    import frontmatter

    d = sandbox.novels.parent / "learning"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ln-abcdef12.md").write_text(
        frontmatter.dumps(frontmatter.Post("## 素材拆解\n要点\n## 文风学习\n短句",
                                           title="样本拆解")),
        encoding="utf-8",
    )
    listed = act.execute("learning_list", {}, ctx=act_ctx)
    assert listed.ok and any(i["id"] == "ln-abcdef12" for i in listed.data["items"])
    read = act.execute("learning_read", {"id": "ln-abcdef12"}, ctx=act_ctx)
    assert read.ok and "文风学习" in read.data["content"]
    assert act.execute("learning_read", {"id": "bad-id"}, ctx=act_ctx).status == "failed"


def test_material_read_and_alias(act_ctx, sandbox: SimpleNamespace):
    """素材正文可读；模型写 list_learning / read_material 之类的别名也能命中。"""
    import frontmatter

    d = sandbox.novels.parent / "materials"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mt-abcdef12.md").write_text(
        frontmatter.dumps(frontmatter.Post("素材正文", title="末法剑修")), encoding="utf-8"
    )
    read = act.execute("read_material", {"id": "mt-abcdef12"}, ctx=act_ctx)
    assert read.ok and read.op == "material_read" and "素材正文" in read.data["content"]
    assert act.execute("list_materials", {}, ctx=act_ctx).op == "material_list"


# ---------- 待确认闸门：只认显式"确认/取消" ----------

def test_pending_gate_confirm_executes(act_ctx, sandbox: SimpleNamespace):
    from src.web.inkforge_api import _decide_pending_action

    # 预览登记：确认必须对应一次真实的预览（否则闸门拒绝执行）
    pending = act.register_preview(act_ctx, "book_create",
                                   {"novel_id": "gated", "mode": "pipeline"},
                                   "将新建书目 gated")
    chat = {act.PENDING_KEY: pending}
    # 与待办无关的话 → 不执行、不清除
    assert _decide_pending_action(chat, "这本书写得怎么样", act_ctx) is None
    assert act.PENDING_KEY in chat
    assert not (sandbox.novels / "gated").exists()

    verdict = _decide_pending_action(chat, "确认", act_ctx)
    assert verdict and verdict["kind"] == "executed"
    assert verdict["result"].ok
    assert act.PENDING_KEY not in chat
    assert (sandbox.novels / "gated" / "settings").is_dir()


def test_pending_gate_confirm_without_preview_is_refused(act_ctx, sandbox: SimpleNamespace):
    """没有预览登记就"确认" → 拒绝执行（防口头声称确认直接落盘）。"""
    from src.web.inkforge_api import _decide_pending_action

    chat = {act.PENDING_KEY: {"op": "book_create",
                             "args": {"novel_id": "sneaky", "mode": "pipeline"},
                             "impact": "将新建书目 sneaky"}}
    verdict = _decide_pending_action(chat, "确认", act_ctx)
    assert verdict and verdict["kind"] == "refused"
    assert "预览" in verdict["reason"]
    assert not (sandbox.novels / "sneaky").exists()


def test_pending_gate_preview_token_must_match(act_ctx, sandbox: SimpleNamespace):
    """预览凭证不匹配（换了参数或旧凭证）→ 拒绝。"""
    from src.web.inkforge_api import _decide_pending_action

    pending = act.register_preview(act_ctx, "book_create",
                                  {"novel_id": "tok1", "mode": "pipeline"}, "将新建 tok1")
    tampered = {**pending, "args": {"novel_id": "tok2", "mode": "pipeline"}}
    verdict = _decide_pending_action({act.PENDING_KEY: tampered}, "确认", act_ctx)
    assert verdict and verdict["kind"] == "refused"
    assert not (sandbox.novels / "tok2").exists()


def test_pending_gate_cancel_discards(act_ctx, sandbox: SimpleNamespace):
    from src.web.inkforge_api import _decide_pending_action

    pending = act.register_preview(act_ctx, "book_create",
                                   {"novel_id": "never", "mode": "pipeline"}, "将新建 never")
    chat = {act.PENDING_KEY: pending}
    verdict = _decide_pending_action(chat, "算了", act_ctx)
    assert verdict and verdict["kind"] == "cancelled"
    assert act.PENDING_KEY not in chat
    assert not (sandbox.novels / "never").exists()
    # 取消会同时清掉预览登记 → 再拿旧凭证确认也无效
    assert act.consume_preview(act_ctx, pending) != ""


def test_pending_gate_no_pending_is_none(act_ctx):
    from src.web.inkforge_api import _decide_pending_action

    assert _decide_pending_action({}, "确认", act_ctx) is None


# ---------- 参数名折算 / 占位符 / 别名（用户实测"对话建不了书"的回归） ----------

def test_arg_alias_folding_for_book_create(act_ctx):
    """模型把 novel_id 写成 id / book_key / title / novel 都要能折算（真实日志里全出现过）。

    实测现场（master-live 系列 JSON）：
      · `{"op":"book_create","args":{"id":"live-created","mode":"pipeline"}}` → 缺 novel_id；
      · 模型自述"书名标识（book_key）：live-created"。
    """
    assert act.normalize_args("book_create", {"id": "a-book"})["novel_id"] == "a-book"
    assert act.normalize_args("book_create", {"book_key": "b-book"})["novel_id"] == "b-book"
    assert act.normalize_args("book_create", {"novel": "c-book"})["novel_id"] == "c-book"
    assert act.normalize_args("book_create", {"title": "d-book"})["novel_id"] == "d-book"
    # 声明参数不存在时补默认模式，保证确认卡文案有内容
    assert act.normalize_args("book_create", {"id": "e-book"})["mode"] == "pipeline"
    # 规范名优先：别名不得覆盖已填好的规范名
    folded = act.normalize_args("book_create", {"novel_id": "real", "title": "fake"})
    assert folded["novel_id"] == "real"
    # 其它写动作的同类漂移
    assert act.normalize_args("book_select", {"novel": "demo-web"})["novel_id"] == "demo-web"
    assert act.normalize_args("gen_decide", {"novel": "demo-web", "decision": "reject",
                                             "comment": "太快"})["action"] == "reject"
    assert act.normalize_args("gen_decide", {"novel": "demo-web", "decision": "reject",
                                             "comment": "太快"})["feedback"] == "太快"


def test_placeholder_values_are_rejected(act_ctx, sandbox: SimpleNamespace):
    """提示词占位符不是书名标识：必须当"没填"处理，绝不建出一本叫 novel_id 的书。

    真实事故：审计日志里有 `已创建书目 novel_id` —— 模型把参数名当取值提交，
    而当时参数层不校验，书架里就真的多了一本占位符书名的书。
    """
    for value in ("novel_id", "<书名>", "书名标识", "BOOK_ID", "novel-id"):
        assert act.is_placeholder_value(value), value
    assert not act.is_placeholder_value("live-created")
    assert act.normalize_args("book_create", {"novel_id": "novel_id"})["novel_id"] == ""

    res = act.execute("book_create", {"novel_id": "novel_id"}, ctx=act_ctx)
    # 必须出确认卡（用户看得见）＋ 卡上写清"没拿到可用的标识"，且**绝不落盘**
    assert res.status == "pending_confirm" and "没有拿到可用的书名标识" in res.summary
    assert not (sandbox.novels / "novel_id").exists()


def test_op_alias_and_fuzzy_matching():
    """别名扩表 + 保守模糊匹配：实测模型自造的 learning_get / learn_list 必须命中。"""
    # 实测失败名（审计日志里两次真实失败）
    assert act.normalize_op("learning_get") == "learning_read"
    assert act.normalize_op("learn_list") == "learning_list"
    # 同义词与词序
    assert act.normalize_op("get_learning") == "learning_read"
    assert act.normalize_op("learning_detail") == "learning_read"
    assert act.normalize_op("list_learnings") == "learning_list"
    assert act.normalize_op("chapter_detail") == "chapter_read"
    assert act.normalize_op("material_get") == "material_read"
    assert act.normalize_op("read_book") == "chapter_read"
    # 保守性：歧义/无关名一律原样返回（由执行器如实报"未知动作"，不猜）
    assert act.normalize_op("mark_book") == "mark_book"
    assert act.normalize_op("book_export") == "book_export"
    assert act.normalize_op("nonsense_op") == "nonsense_op"


def test_learning_read_tolerates_model_id_shapes(act_ctx, sandbox: SimpleNamespace):
    """ID 容错：大小写/引号/标题写法都要能读（用户报"能列编号、点开就失败"）。"""
    import frontmatter

    d = sandbox.novels.parent / "learning"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ln-abcdef12.md").write_text(
        frontmatter.dumps(frontmatter.Post("## 文风学习\n短句为主", title="样本拆解")),
        encoding="utf-8",
    )
    for raw in ("ln-abcdef12", "LN-ABCDEF12", "`ln-abcdef12`", " ln-abcdef12 "):
        res = act.execute("learning_read", {"id": raw}, ctx=act_ctx)
        assert res.ok, raw
        assert "文风学习" in res.data["content"]

    # 模型把标题当 id 传：唯一命中时允许（有歧义才报错）
    by_title = act.execute("learning_read", {"id": "样本拆解"}, ctx=act_ctx)
    assert by_title.ok and by_title.data["id"] == "ln-abcdef12"
    # 不存在的编号：如实失败，并提示"用列表里的编号"
    missing = act.execute("learning_read", {"id": "ln-00000000"}, ctx=act_ctx)
    assert missing.status == "failed" and "不存在" in missing.error


def test_learning_list_tells_model_how_to_open(act_ctx, sandbox: SimpleNamespace):
    """列表回执必须写清"要看全文就用这个编号"——否则用户会得到"能列编号、点不开"。"""
    import frontmatter

    d = sandbox.novels.parent / "learning"
    d.mkdir(parents=True, exist_ok=True)
    (d / "ln-abcdef12.md").write_text(
        frontmatter.dumps(frontmatter.Post("正文", title="样本拆解")), encoding="utf-8"
    )
    listed = act.execute("learning_list", {}, ctx=act_ctx)
    assert listed.ok
    assert "learning_read" in listed.summary and "ln-abcdef12" in listed.summary


def test_book_create_mode_folding_never_blocks(act_ctx, sandbox: SimpleNamespace):
    """创作模式写歪了也必须能建书（真机复现：模型把「长篇」当 mode 传，确认后零落盘）。

    真实现场（本批次 smoke_master_converse --with-llm）：
      pending.args = {"novel_id": "live-conv", "mode": "长篇"} → 确认卡带"未知创作模式" →
      用户点确认 → `book_create 执行失败：未知创作模式：'长篇'`，目录始终不存在。
    """
    for raw in ("长篇", "短篇", "网文", "连载", "long", "novel", "series", "标准", "随便"):
        args = act.normalize_args("book_create", {"novel_id": "m1", "mode": raw})
        assert args["mode"] == "pipeline", raw
        # 模型原话留在 mode_raw，确认卡上要如实并列展示
        assert args.get("mode_raw") == raw, raw

    for raw in ("互动创作", "剧情卡", "逐章推进", "interactive", "分支"):
        assert act.normalize_args("book_create", {"novel_id": "m2", "mode": raw})["mode"] == \
            "interactive", raw

    # 完全判不出来也不许卡死：走默认 pipeline（用户可在确认卡上纠正）
    assert act.normalize_args("book_create", {"novel_id": "m3", "mode": "xxooxx"})["mode"] == \
        "pipeline"

    # 确认卡必须把"我按你的说法折算成 pipeline"写出来
    act_ctx.extra["title_hint"] = "雾都回响"
    res = act.execute("book_create", {"novel_id": "wudu", "mode": "长篇"}, ctx=act_ctx)
    assert res.status == "pending_confirm"
    assert "长篇" in res.summary and "pipeline" in res.summary
    done = act.execute("book_create", {"novel_id": "wudu", "mode": "长篇"}, ctx=act_ctx,
                       execute_write=True)
    assert done.ok and (sandbox.novels / "wudu" / "settings").is_dir()


def test_guess_novel_id_and_title_from_user_phrases():
    """从用户原话抠标识/书名：真机里出现过的写法逐条锁定。

    真机缺口（G1）：`"书名标识就用 dark-crime"` 旧正则抠不出标识 →
    兜底登记不了建书动作 → 用户被告知"需要确认两点"却拿不到确认卡。
    """
    from src.web.inkforge_api import _guess_arg_from_user, _guess_title_from_user

    cases = [
        ("帮我新建一本自由创作的书，书名标识就用 live-created。", "live-created", "自由创作"),
        ("帮我建一本赛博修仙的长篇，书名就叫《雨夜委托》，书名标识就用 live-conv。",
         "live-conv", "雨夜委托"),
        ("帮我建一本黑暗犯罪题材的书，书名标识就用 dark-crime，要写死刑犯视角的暴力与堕落。",
         "dark-crime", "黑暗犯罪题材"),
        ("书名标识（book id）：dark-crime", "dark-crime", ""),
        ("book key: abc-def", "abc-def", ""),
        ("建一本叫 `my-book` 的书", "my-book", ""),      # 反引号里是标识，不是书名
        ("建书流程怎么走？", "", ""),                     # 提问不该被当成建书
        ("帮我写一本关于末法剑修的长篇，书名《枯剑纪元》，标识就用 rotten-sword",
         "rotten-sword", "枯剑纪元"),
    ]
    for text, want_id, want_name in cases:
        assert _guess_arg_from_user("novel_id", text) == want_id, text
        assert _guess_title_from_user(text) == want_name, text
    # 只给中文书名 → 生成 ASCII 标识（不是空、也不是占位符）
    slug = _guess_arg_from_user("novel_id", "再帮我建一本《雾都回响》。")
    assert slug.startswith("novel-") and slug.isascii()


def test_fallback_action_covers_create_intent_without_action_block():
    """模型整轮不给动作块时，用户原话就是第一事实源（真机 E8 / G1 回归）。"""
    from src.web.inkforge_api import _fallback_action_from_history

    chat = {"messages": [{"role": "user", "content": "帮我建一本《雾都回响》。"}]}
    acts = _fallback_action_from_history(chat, "再帮我建一本《雾都回响》。")
    assert acts and acts[0]["op"] == "book_create"
    assert acts[0]["args"]["novel_id"].isascii()

    acts2 = _fallback_action_from_history(chat, "书名标识就用 dark-crime，写死刑犯视角。")
    assert acts2 and acts2[0]["args"]["novel_id"] == "dark-crime"

    # 纯提问不兜底（否则会把"建书流程怎么走"变成一张建书卡）
    assert _fallback_action_from_history({}, "建书流程怎么走？") == []


# ---------- 预览键稳定性（确认被无端拒绝的根因） ----------

def test_preview_key_is_independent_of_active_book(act_ctx, sandbox: SimpleNamespace):
    """预览键只由（op + 规范化 args）决定，不受"期间又建了书/切了书"影响。

    真实故障（master-live-final6 现场）：预览 book_create 时工作台还没选书
    （键 `__workspace__::book_create`），而建书成功会立刻 `set_active_novel(新书)`，
    于是确认时算出的键变成 `<新书>::book_create` → 找不到登记 → 409、零落盘、
    待办还挂着。用户的体感是"点了确认，什么都没发生"。
    """
    args = {"novel_id": "keyprobe", "mode": "pipeline"}
    pending = act.register_preview(act_ctx, "book_create", args, "将新建书目 keyprobe")

    # 模拟"预览之后，工作台当前书目被改成别的书"
    act_ctx.extra["_state"]["active"] = "demo-web"
    assert act.consume_preview(act_ctx, pending) == ""

    # 换个会话维度/换本书也一样能确认（键与会话无关）
    act_ctx.book = ""
    act_ctx.session_key = WORKSPACE
    pending2 = act.register_preview(act_ctx, "book_create", args, "将新建书目 keyprobe")
    act_ctx.extra["_state"]["active"] = ""
    assert act.consume_preview(act_ctx, pending2) == ""


def test_preview_args_mismatch_still_refused(act_ctx):
    """键变稳了也不能放过参数被改的情况：参数不同 = 不同的键，必须拒绝。"""
    pending = act.register_preview(act_ctx, "book_create",
                                   {"novel_id": "keep", "mode": "pipeline"}, "将新建 keep")
    tampered = {**pending, "args": {"novel_id": "swap", "mode": "pipeline"}}
    assert act.consume_preview(act_ctx, tampered) != ""


def test_forget_preview_clears_all_variants(act_ctx):
    """取消时把该 op 的所有登记清掉（新键与 args 绑定，可能有多条）。"""
    a = act.register_preview(act_ctx, "book_create", {"novel_id": "f1"}, "将新建 f1")
    b = act.register_preview(act_ctx, "book_create", {"novel_id": "f2"}, "将新建 f2")
    act.forget_preview(act_ctx, "book_create")
    assert act.consume_preview(act_ctx, a) != ""
    assert act.consume_preview(act_ctx, b) != ""



def test_actions_endpoints(app_client, sandbox: SimpleNamespace):
    client, _ = app_client
    manifest = client.get("/api/actions").json()["actions"]
    assert any(a["op"] == "book_list" for a in manifest)

    audit = client.get("/api/actions/audit?limit=10").json()["records"]
    assert isinstance(audit, list)

    # 只读动作不得走写通道
    res = client.post("/api/actions/run", json={"op": "book_list", "confirm": True})
    assert res.status_code == 400

    # 写动作未确认 → 只回影响说明与预览凭证，不落盘
    preview = client.post("/api/actions/run",
                          json={"op": "book_create",
                                "args": {"novel_id": "via-api:".replace(":", "-"),
                                         "mode": "pipeline"}})
    assert preview.status_code == 200
    body = preview.json()
    assert body["status"] == "pending_confirm" and body["pending"]["token"]
    assert not (sandbox.novels / "via-api-").exists()

    # 凭空"确认"（没有对应预览）→ 409，且不落盘
    bare = client.post("/api/actions/run",
                       json={"op": "book_create",
                             "args": {"novel_id": "bare-claim", "mode": "pipeline"},
                             "confirm": True})
    assert bare.status_code == 409
    assert not (sandbox.novels / "bare-claim").exists()

    done = client.post("/api/actions/run",
                       json={"op": "book_create",
                             "args": {"novel_id": "via-api-", "mode": "pipeline"},
                             "confirm": True, "token": body["pending"]["token"]})
    assert done.status_code == 200 and done.json()["ok"] is True
    assert (sandbox.novels / "via-api-" / "settings").is_dir()


def test_chat_action_confirm_and_cancel(app_client, sandbox: SimpleNamespace):
    """会话内确认/取消：走"预览登记 → 确认执行"两步，两步都必须对应。"""
    client, _ = app_client
    chat_id = client.post("/api/chats?novel=demo-web",
                          json={"agent": "master"}).json()["chat"]["id"]

    # 无待办时确认 → 409
    assert client.post(f"/api/chats/{chat_id}/action?novel=demo-web").status_code == 409

    # 先预览（拿到凭证），再确认 → 真正执行
    preview = client.post("/api/actions/run?novel=demo-web",
                          json={"op": "book_create",
                                "args": {"novel_id": "from-chat", "mode": "pipeline"}})
    assert preview.status_code == 200
    pending = preview.json()["pending"]
    assert pending["token"]

    # 把预览登记同步进会话（真实链路上由墨师动作循环写入）
    from src.web.inkforge_api import _save_chat_to

    chat = client.get(f"/api/chats/{chat_id}?novel=demo-web").json()
    chat["pending_action"] = pending
    _save_chat_to(sandbox.novels / "demo-web", chat)

    res = client.post(f"/api/chats/{chat_id}/action?novel=demo-web")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert (sandbox.novels / "from-chat" / "settings").is_dir()
    assert client.get(f"/api/chats/{chat_id}?novel=demo-web").json().get("pending_action") is None

    # 取消路径：再造一个待办并取消 → 数据不动，且凭证失效
    preview2 = client.post("/api/actions/run?novel=demo-web",
                           json={"op": "book_delete", "args": {"novel": "from-chat"}})
    pending2 = preview2.json()["pending"]
    chat = client.get(f"/api/chats/{chat_id}?novel=demo-web").json()
    chat["pending_action"] = pending2
    _save_chat_to(sandbox.novels / "demo-web", chat)

    cancelled = client.delete(f"/api/chats/{chat_id}/action?novel=demo-web")
    assert cancelled.status_code == 200 and cancelled.json()["cancelled"] is True
    assert (sandbox.novels / "from-chat").is_dir()   # 取消后数据仍在
    # 用已作废的凭证确认 → 拒绝
    stale = client.post("/api/actions/run?novel=demo-web",
                        json={"op": "book_delete", "args": {"novel": "from-chat"},
                              "confirm": True, "token": pending2["token"]})
    assert stale.status_code == 409
    assert (sandbox.novels / "from-chat").is_dir()


def test_audit_endpoint_hides_args_digest(app_client, act_ctx):
    act.execute("book_list", {}, ctx=act_ctx)
    client, _ = app_client
    records = client.get("/api/actions/audit?limit=20").json()["records"]
    assert records and all("args_digest" not in r for r in records)
