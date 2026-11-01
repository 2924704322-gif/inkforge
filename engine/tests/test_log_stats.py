"""`smoke_log_stats.py` 的解析口径回归测试（零模型调用、零作品数据）。

为什么需要：本脚本是真机「实机轮换对照」的**唯一分母来源** —— 它算错不会报错，
只会安静地给出一个错的拒答率。开发期已经踩到两个真实缺陷，这里各锁一条：

1. **日志带 BOM 时首行被静默丢弃**（`utf-8` 读不匹配 `[engine HH:MM:SS]` 前缀），
   而首行往往正是「探活: 角色 → 绑定」那一行 → 整段绑定归属错。
2. **同一行命中多个信号时被 `break` 吃掉**：`brief 保真链第 1 次产出不合格（产出含拒绝语义）`
   同时是「保真链重试」与「拒答致重试」，只记前者会让分母偏小、拒答率虚高。

另外锁住：rich traceback 的**源码回显**必须剔除（实测 `BriefFidelityError` 19 → 真实 12）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import smoke_log_stats as stats

# ---- 合成日志（与 electron 转写格式一致；不含任何作品数据） ----

PROBE = "[engine 10:00:00] INFO     探活: 角色 → deepseek / deepseek-flash"
CALL_OK = ("[engine 10:00:05] INFO     [调用] role=writer deepseek/deepseek-flash "
           "prompt_chars=9102 max_tokens=16000 → 3281 字 / 5123 tokens / finish=stop / 62.3s")
CALL_DOWNGRADED = ("[engine 10:00:10] INFO     [调用] role=architect deepseek/deepseek-flash（降级） "
                   "prompt_chars=880 max_tokens=16000 → 0 字 / 16000 tokens / finish=length / 41.0s")
CALL_FAILED = ("[engine 10:00:12] WARNING  [调用] role=editor deepseek/deepseek-flash "
               "prompt_chars=1200 → 失败：ProviderError(timeout)")
STRUCT_FAIL = ("[engine 10:00:11] WARNING  角色[plotter]结构化输出解析失败（第 1 次）: "
               "Expecting value: line 1 column 1 (char 0) | 产出 0 字 / 16000 tokens / finish=stop")
REFUSAL_RETRY = "[engine 10:02:00] WARNING  brief 保真链第 1 次产出不合格（产出含拒绝语义），携原因重试"
REFUSAL_HIT = ("[engine 10:02:01] WARNING  brief 保真链判定产出含拒绝语义："
               "产出含拒绝语义（命中 短产出:抱歉｜13 字）")
BIND_SWITCH = "[engine 10:09:00] INFO     角色绑定更新 → opencode-go/deepseek-v4-flash"
CALL_GATEWAY = ("[engine 10:09:30] INFO     [调用] role=writer opencode-go/deepseek-v4-flash "
                "prompt_chars=9000 max_tokens=16000 → 5100 字 / 6100 tokens / finish=stop / 206.1s")
# rich traceback 的源码回显：必须被剔除，否则计数虚高
SRC_ECHO_CHAIN = '[engine 10:11:00] │   218 │   │   │   │   logger.info("brief 保真链第 %d 次 │'
SRC_ECHO_FATAL = '[engine 10:11:01] │   218 │   │   logger.warning("BriefFidelityError │'

FULL_LOG = "\n".join([
    PROBE, CALL_OK, CALL_DOWNGRADED, STRUCT_FAIL, CALL_FAILED,
    REFUSAL_RETRY, REFUSAL_HIT, BIND_SWITCH, CALL_GATEWAY,
    SRC_ECHO_CHAIN, SRC_ECHO_FATAL,
]) + "\n"


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "desktop-20260922.log"
    path.write_text(FULL_LOG, encoding="utf-8")
    return path


@pytest.fixture
def bom_log_file(tmp_path: Path) -> Path:
    path = tmp_path / "desktop-20260922.log"
    path.write_text(FULL_LOG, encoding="utf-8-sig")
    return path


def _signals(events: list[tuple[str, str, str]]) -> list[str]:
    return [payload for _, kind, payload in events if kind == "signal"]


# ---- 行解析 ----

def test_call_line_is_parsed_with_all_fields(log_file: Path) -> None:
    calls = [json.loads(p) for _, k, p in stats.parse_log(log_file) if k == "call"]
    assert len(calls) == 3, "三条 [调用]（正常 / 降级 / 网关）都应解析出来"
    first = calls[0]
    assert first["role"] == "writer"
    assert first["provider"] == "deepseek"
    assert first["model"] == "deepseek-flash", "「（降级）」不得被并进模型名"
    assert first["out_chars"] == 3281
    assert first["finish"] == "stop"
    assert first["elapsed"] == 62.3
    assert calls[1]["model"] == "deepseek-flash"
    assert calls[1]["out_chars"] == 0 and calls[1]["finish"] == "length"


def test_failed_call_line_counted_but_not_as_a_call(log_file: Path) -> None:
    events = stats.parse_log(log_file)
    assert sum(1 for _, k, _ in events if k == "call_fail") == 1


def test_probe_and_binding_switch_both_seen(log_file: Path) -> None:
    binds = [p for _, k, p in stats.parse_log(log_file) if k == "bind"]
    assert binds == ["deepseek/deepseek-flash", "opencode-go/deepseek-v4-flash"]


# ---- 两个真实缺陷的回归 ----

def test_first_line_survives_a_byte_order_mark(bom_log_file: Path) -> None:
    """BOM 不得吃掉首行 —— 否则首行若为「探活」整段绑定归属就错。"""
    binds = [p for _, k, p in stats.parse_log(bom_log_file) if k == "bind"]
    assert binds[0] == "deepseek/deepseek-flash"


def test_one_line_can_count_for_several_signals(log_file: Path) -> None:
    """「保真链第 N 次产出不合格（产出含拒绝语义）」必须同时计入两个信号。"""
    sig = _signals(stats.parse_log(log_file))
    assert sig.count("保真链重试") == 1
    assert sig.count("拒答致重试") == 1
    assert sig.count("拒答命中") == 1


# ---- rich 折行容错（2026-09-22 实测：管道输出按 80 列折行，精口径分母会静默归零） ----

#: 真实折行形态：每段**右侧补空格到列宽**（这正是"单空格正则会失配"的真因）
WRAPPED_CALL = "\n".join([
    "[engine 10:00:05] [10:00:05] INFO     [调用] role=writer deepseek/deepseek-flash      ",
    "[engine 10:00:05]                     prompt_chars=31020 max_tokens=16000 → 3281 字 /  ",
    "[engine 10:00:05]                     5123 tokens / finish=stop / 62.3s                ",
]) + "\n"

NATIVE_PREFIX_CALL = (
    "[10:00:05] INFO     [调用] role=writer deepseek/deepseek-flash prompt_chars=9102 "
    "max_tokens=16000 → 3281 字 / 5123 tokens / finish=stop / 62.3s"
)


def test_call_line_survives_rich_line_wrapping(tmp_path: Path) -> None:
    """折行 + 段尾补齐空格后仍要解析出完整调用记录。"""
    path = tmp_path / "desktop-20260922.log"
    path.write_text(WRAPPED_CALL, encoding="utf-8")
    calls = stats.collect_calls(path.read_text(encoding="utf-8").splitlines())
    assert len(calls) == 1
    assert calls[0]["role"] == "writer" and calls[0]["model"] == "deepseek-flash"
    assert calls[0]["out_chars"] == 3281 and calls[0]["finish"] == "stop"


def test_rewrapping_does_not_swallow_the_next_record(tmp_path: Path) -> None:
    """拼接必须"匹配成功即停"，不得把下一条记录吸进上一条。"""
    path = tmp_path / "desktop-20260922.log"
    path.write_text(
        WRAPPED_CALL
        + "[engine 10:00:07] [10:00:07] WARNING  brief 保真链判定产出含拒绝语义：产出含拒绝语义（命中 x）\n"
        + "[engine 10:00:08] [10:00:08] INFO     角色绑定更新 → opencode-go/deepseek-v4-flash\n",
        encoding="utf-8",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(stats.collect_calls(lines)) == 1
    assert dict(stats.count_signals(lines)) == {"拒答命中": 1}
    binds = [p for _, k, p in stats.parse_log(path) if k == "bind"]
    assert binds == ["opencode-go/deepseek-v4-flash"], "绑定分段不得被拼接污染"


def test_line_regex_accepts_both_engine_and_electron_prefixes() -> None:
    """electron 转写与本机引擎原生 stdout 两种前缀都要吃。

    ⚠ 这条曾经真的坏过：`\\[engine ` 分支里自带的 `[` 被误当成"两种情况共用的 `[`"，
    结果原生格式静默失配（表现为"整份日志零事件"）。
    """
    assert stats._LINE_RE.match("[engine 21:24:53] [21:24:53] WARNING  x").group(1) == "21:24:53"
    assert stats._LINE_RE.match("[21:24:53] WARNING  x").group(1) == "21:24:53"
    assert stats._LINE_RE.match("[21:24:53] WARNING  x").group(2) == "WARNING  x"


def test_collect_calls_handles_native_engine_stdout() -> None:
    calls = stats.collect_calls([NATIVE_PREFIX_CALL])
    assert len(calls) == 1 and calls[0]["elapsed"] == 62.3


def test_structured_refusal_signal_is_counted_but_format_noise_is_not() -> None:
    """结构化链路：只有命中通用拒答措辞才算拒答，`未命中`/`空产出` 属格式问题。"""
    lines = [
        "[10:00:01] WARNING  角色[architect]结构化输出解析失败（第 1 次）: Expecting value: "
        "line 1 column 1 (char 0) | 产出 190 字 / 321 tokens / finish_reason=stop / "
        "拒答信号=短产出:抱歉｜190 字",
        "[10:00:02] WARNING  角色[architect]结构化输出解析失败（第 2 次）: Expecting value: "
        "line 1 column 1 (char 0) | 产出 242 字 / 466 tokens / finish_reason=stop / "
        "拒答信号=未命中",
        "[10:00:03] WARNING  角色[architect]结构化输出解析失败（第 3 次）: Expecting value: "
        "line 1 column 1 (char 0) | 产出 0 字 / 16000 tokens / finish_reason=stop / "
        "拒答信号=空产出",
    ]
    sig = dict(stats.count_signals(lines))
    assert sig["结构化解析失败"] == 3
    assert sig["结构化拒答"] == 1, "只有第一条（短产出:抱歉）是真拒答"


def test_refusal_retry_matches_the_nested_paren_wording(tmp_path: Path) -> None:
    """2026-09-22 03:44 之后同一行多嵌了一层「（命中 …）」——不得因为收尾括号写死而漏计。"""
    old = "[engine 10:02:00] WARNING  brief 保真链第 1 次产出不合格（产出含拒绝语义），携原因重试"
    new = ("[engine 10:02:00] WARNING  brief 保真链第 1 次产出不合格"
           "（产出含拒绝语义（命中 短产出:抱歉｜13 字）），携原因重试")
    path = tmp_path / "desktop-20260922.log"
    path.write_text(old + "\n" + new + "\n", encoding="utf-8")
    sig = _signals(stats.parse_log(path))
    assert sig.count("拒答致重试") == 2
    assert sig.count("保真链重试") == 2


def test_traceback_source_echoes_are_excluded(log_file: Path) -> None:
    """源码回显既不能算调用，也不能算信号。"""
    events = stats.parse_log(log_file)
    assert sum(1 for _, k, _ in events if k == "call") == 3
    sig = _signals(events)
    assert "致命" not in sig, "SRC_ECHO_FATAL 里的 BriefFidelityError 不得计入"
    assert sig.count("保真链重试") == 1, "SRC_ECHO_CHAIN 里的「保真链第」不得再计一次"


# ---- 分段与比率 ----

def test_segments_split_on_binding_change(log_file: Path) -> None:
    segs = stats.segment_by_binding(stats.parse_log(log_file), "20260922")
    assert [s.binding for s in segs] == ["deepseek/deepseek-flash",
                                         "opencode-go/deepseek-v4-flash"]
    assert [len(s.calls) for s in segs] == [2, 1]
    assert segs[0].start == "2026-09-22 10:00:00" and segs[0].end == "2026-09-22 10:02:01"
    assert segs[1].start == "2026-09-22 10:09:00"


def test_timestamps_carry_a_date_so_cross_midnight_logs_stay_ordered(tmp_path: Path) -> None:
    """文件名只到天，但一个文件会跨午夜续写（实测 desktop-20260921.log 写到次日 06:00）。

    只保留 `HH:MM:SS` 会让 `--since` 与按小时分段把两天搅在一起；这里锁住"时刻回退即换日"。
    """
    path = tmp_path / "desktop-20260921.log"
    path.write_text(
        "[engine 23:58:00] WARNING  brief 保真链判定产出含拒绝语义：产出含拒绝语义（命中 x）\n"
        "[engine 00:02:00] WARNING  brief 保真链第 1 次产出不合格（产出含拒绝语义），携原因重试\n",
        encoding="utf-8",
    )
    # 第 2 行同时命中「保真链重试」与「拒答致重试」→ 会产生两个事件，这里按时间戳去重
    stamps = list(dict.fromkeys(ts for ts, _, _ in stats.parse_log(path)))
    assert stamps == ["2026-09-21 23:58:00", "2026-09-22 00:02:00"]


def test_refusal_events_uses_the_larger_of_the_two_lines(log_file: Path) -> None:
    """一次拒答同时产生「判定行」与「重试行」→ 事件数取较大值，避免重复计。"""
    seg = stats.segment_by_binding(stats.parse_log(log_file), "20260922")[0]
    assert seg.signals["拒答命中"] == 1
    assert seg.signals["拒答致重试"] == 1
    assert seg.refusal_events == 1


def test_attempts_coarse_is_writer_starts_plus_chain_retries(log_file: Path) -> None:
    seg = stats.segment_by_binding(stats.parse_log(log_file), "20260922")[0]
    assert seg.signals["写作启动"] == 0, "合成日志没有「生成中」行 → 分母只由重试贡献"
    assert seg.attempts_coarse == seg.signals["保真链重试"] == 1


def test_structured_failure_rate_uses_per_call_denominator(log_file: Path) -> None:
    seg = stats.segment_by_binding(stats.parse_log(log_file), "20260922")[0]
    assert seg.ratio("结构化解析失败") == pytest.approx(0.5, abs=1e-4)
def test_hour_segmentation_also_splits_on_binding_change(log_file: Path) -> None:
    """小时内换绑必须切段，否则同一段混两个臂、比率不可比。"""
    segs = stats.segment_by_hour(stats.parse_log(log_file), "20260922")
    assert [s.binding for s in segs] == ["deepseek/deepseek-flash @ 2026-09-22 10h",
                                         "opencode-go/deepseek-v4-flash @ 2026-09-22 10h"]
    assert [len(s.calls) for s in segs] == [2, 1]


def test_to_dict_is_json_serialisable(log_file: Path) -> None:
    seg = stats.segment_by_binding(stats.parse_log(log_file), "20260922")[0]
    payload = json.dumps(seg.to_dict(), ensure_ascii=False)
    back = json.loads(payload)
    assert back["calls"] == 2 and back["refusal_events"] == 1
    assert back["finish_reason"] == {"stop": 1, "length": 1}
    assert back["call_latency_by_role"]["writer"]["p50"] == 62.3
