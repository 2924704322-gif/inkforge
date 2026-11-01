#!/usr/bin/env python
"""模型对照（bake-off）：只测被授权的 3 个对象，用**合成**长上下文，不读任何作品数据。

授权范围（作者指定，勿扩）：
  1. official-flash      → provider `deepseek`     / model `deepseek-flash`
  2. gateway-flash       → provider `opencode-go`  / model `deepseek-flash`
  3. gateway-v4-flash    → provider `opencode-go`  / model `deepseek-v4-flash`

为什么必须用合成上下文：真机上拒绝发生在作者的具体作品里，而冒烟的 7 个短场景在 flash 上
实测**拒答 0 次**——复现不了。这里改为复刻**结构特征**（超长上下文 + 5000 字目标 +
结构化 JSON 出卡），全部文本由本脚本自造，与作者的作品无关。

每个 (臂, 用例, 重复) 只发**一次**调用（不走保真链重试），这样得到的是干净的"每调用"口径：
  · writer_long  —— 写作 prompt（目标 5000 字）→ 用共享的拒答判定器分类
  · cards_json   —— 出卡 prompt（PlotCardsOutput，结构化 JSON）
  · review_json  —— 审校 prompt（ReviewOutput，结构化 JSON）
指标：拒答率 / 空产出率 / 截断率 / JSON 失败率 / 单次耗时 / 产出字数 / 卡片数 / 评分。

用法：
    python engine/smoke_model_bakeoff.py                      # 3 臂 × 默认重复
    python engine/smoke_model_bakeoff.py --writer-repeat 2 --json-repeat 3
    python engine/smoke_model_bakeoff.py --context-chars 30000
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

from smoke_test import TOKEN, build_sandbox  # noqa: E402

BAKEOFF_VERSION = "model-bakeoff/v1"

#: 被授权的 3 个对象（硬编码，防止手滑扩到别的模型）
ARMS: list[dict] = [
    {"id": "official-flash", "provider": "deepseek", "model": "deepseek-flash",
     "label": "官方 deepseek-flash"},
    {"id": "gateway-flash", "provider": "opencode-go", "model": "deepseek-flash",
     "label": "网关 opencode-go / deepseek-flash（同模型名、不同网关）"},
    {"id": "gateway-v4-flash", "provider": "opencode-go", "model": "deepseek-v4-flash",
     "label": "网关 opencode-go / deepseek-v4-flash"},
]


# ---------- 合成长上下文（自造文本，与作者作品无关） ----------

def build_fixture(target_chars: int) -> dict:
    """按字数目标铺出一份"结构像真提示词"的合成上下文。

    刻意复刻真机形态：多条世界观规则 + 6 个角色状态 + 十余条已发生情节摘要 +
    状态板/伏笔 + 文风指纹 + 作者约束。全部内容是本脚本自造的通用奇幻设定。
    """
    world = [
        "本源之力分九阶：引气、凝脉、开府、化罡、通玄、炼神、照影、归元、化虚；"
        "跨阶战斗的胜负取决于对本源的理解而非阶数高低。",
        "此界无神，所谓神迹皆是高阶修士留下的残余意志；残余意志会在特定地形附近显形。",
        "每年秋分前后七日为『静默期』，所有本源波动被压制，飞行法器一律失效。",
        "城池之间以驿道相连，驿道两侧每三十里有一座残破哨塔，塔内镌刻古老的定向符。",
        "符纹分三类：定、引、绝；同一条符纹链中不可出现同类重复，否则必然反冲。",
        "重伤者的本源会在三日内缓慢外泄，外泄痕迹可被同源者感知，追踪距离约二十里。",
        "宗门以『契』约束弟子：契文刻于腕骨内侧，违契者本源逆流，但契文可由宗主解除。",
        "历史上有三次『断脉之乱』，每次都以大量修士本源枯竭告终，具体起因失传。",
        "器物分凡、灵、玄、冥四品；冥品器物会缓慢吞噬持有者的本源。",
        "冬季北境有『灰雪』，落于皮肤即封住一处脉窍，需以热泉浸泡方可解开。",
        "此地民间以『秤』作为誓约见证：立誓时双方各放一枚秤石，毁石即毁约。",
        "所有城池的井水都带苦味，只有王都的井水是甜的，因此王都井水按壶售卖。",
    ]
    chars = {
        "林砚": "主角，外门弟子出身，本源凝脉三阶。性格克制、话少、记性极好。"
              "左手腕有旧伤，握剑三刻后会不受控地发抖。当前在东境驿道一带活动。",
        "苏拂": "引路人，开府二阶，善用『引』类符纹。嘴上刻薄，实际护短。"
              "随身带一把旧秤与一枚只剩半边的秤石。现与林砚同行。",
        "裴照": "旧敌，通玄四阶，曾在断脉之乱中失去胞弟。表面守礼，行事狠绝。"
              "持有冥品短刀『垂铃』。当前在王都方向布防。",
        "阿沅": "林砚的胞妹，十二岁，本源未开。被裴照的人控制在听风阁内。"
              "能听见残余意志的低语，因此长期头痛。",
        "老柯": "驿道哨塔的守塔人，凡品修士，七十余岁。知道三次断脉之乱的口述版本。"
              "右手缺两指，怕灰雪。",
        "叶清": "东境巡检，照影一阶，身份中立。与林砚有旧交，但受契约束缚，"
              "不能在无令情况下放行任何人。",
    }
    summaries = [
        "第一章：林砚在外门执事堂接下押运任务，货物是一只封着符纹的木匣。"
        "他因腕伤被同队轻视，却在出城前察觉木匣上的符纹被换过。",
        "第二章：驿道第一座哨塔歇脚，老柯用苦井水煮茶，提到『静默期将至』，"
        "并警告近来有人专在驿道上截杀押运队。林砚检查木匣，确认符纹被人以『定』类重刻。",
        "第三章：夜宿废弃驿站，来了一队自称巡检的人要求验货。林砚以口头约定拖延，"
        "苏拂暗中在院墙布下引类符纹。验货失败，对方撤离前留下一句『东西不在匣里』。",
        "第四章：林砚开匣，发现内里空的，只有一枚半边秤石。他由此推断货物早在出城前被调换，"
        "而调换者必须熟悉执事堂的排班。",
        "第五章：两人改道东境小城，找到旧交叶清。叶清承认近月有多起同类调包，"
        "但受契约束缚无法细说，只给了半张巡检排班图的抄本。",
        "第六章：小城遭遇静默期前兆，飞行法器坠落，坊市发生踩踏。林砚在混乱中救出孩童，"
        "腕伤发作，被迫在药铺静养两日。",
        "第七章：苏拂独自查探，发现排班图与裴照旧部的时间线重合；她带回一枚染血的秤石，"
        "秤石上的缺口与林砚手中那枚吻合。",
        "第八章：两人沿驿道北上，途中遭遇截杀。林砚以凝脉三阶硬抗两名开府修士，"
        "靠地形与引类符纹脱身，但本源损耗过半。",
        "第九章：抵达王都外围的听风阁旧址，林砚听见阁内传出极其微弱的低语，"
        "与阿沅年幼时的头痛描述完全一致。",
        "第十章：裴照现身，行事守礼却步步紧逼；他承认阿沅在他手里，"
        "条件是林砚交出『完整的秤』。双方约定三日后在旧矿口交换。",
        "第十一章：旧矿口勘察，发现此处正是第三次断脉之乱的起事地之一，"
        "矿壁上有大量『绝』类旧符纹，仍有残余意志在显形。",
        "第十二章：叶清违背契文暗示前来，告知林砚：调包不是为了货物，"
        "而是为了把『秤石』这一对物件分开——它们合起来能解开腕骨契。",
    ]
    state_board = [
        "林砚 · 本源损耗过半，需三日静养；腕伤在低温下更易发作。",
        "苏拂 · 引类符纹耗材剩三成；旧秤与半边秤石随身。",
        "裴照 · 已进驻听风阁外围；冥品短刀垂铃未曾出鞘。",
        "阿沅 · 被囚于听风阁内层；听力低语导致长期头痛，情绪不稳。",
        "叶清 · 受契文约束，只能在『无令』状态下提供口头线索。",
        "老柯 · 仍在东境哨塔；右手指缺失，无法拆卸塔内定向符。",
        "木匣 · 空，符纹已被『定』类重刻（第三名为人后改）。",
        "半边秤石 · 林砚持有，缺口在东侧；另一枚在裴照处，缺口在西侧。",
    ]
    foreshadowing = [
        "〔未回收〕第一座哨塔里老柯提到的『静默期将至』，与后文法器坠落的时点吻合。",
        "〔未回收〕木匣符纹被『定』类重刻，说明调换者熟悉执事堂排班。",
        "〔未回收〕两侧秤石合成一枚可解腕骨契——叶清的暗示尚未验证。",
        "〔未回收〕旧矿口的『绝』类符纹仍在导致残余意志显形。",
        "〔未回收〕裴照胞弟之死与第三次断脉之乱的关系尚未说明。",
        "〔未回收〕阿沅听见的低语内容始终未直接呈现。",
        "〔已回收〕静默期前兆导致飞行法器坠落，与第六章的混乱对应。",
        "〔已回收〕林砚的腕伤在第八章硬抗后发作，与前文设定一致。",
        "〔未回收〕『垂铃』未出鞘，暗示裴照仍在留手。",
        "〔未回收〕王都井水按壶售卖，可能在后续成为识别身份的线索。",
    ]
    due = [
        "〔临期〕第三章『东西不在匣里』的结论尚未让执事堂知晓。",
        "〔临期〕约定三日后在旧矿口交换人质，本章即到期。",
        "〔超期〕叶清的允诺（提供线索）尚未兑现第二条。",
        "〔临期〕林砚本源损耗过半，静养期限将满。",
    ]
    style_guide = (
        "文风指纹：句子偏短，多用具体动作与器物细节替代情绪形容；"
        "对话克制，避免解释性台词；环境描写以触觉与声音为主，少用色彩堆叠；"
        "每章至少一处『器物—符纹—代价』三件套的具象描写。"
    )
    constraints = (
        "【作者指定 · 自定义创作约束】\n"
        "- **术语表**：本源（力量）、脉窍（修行节点）、秤石（誓约凭证）、"
        "静默期（秋分前后七日）、灰雪（北境封脉之雪）——专有名词一律不得改称。\n"
        "- **禁止项**：不得使用『仿佛』『似乎』以外的模糊比喻；不得出现现代词汇与网络语；"
        "不得用省略号代替情节推进；不得插入作者视角的说明或提示。\n"
        "- **节奏要求**：每章至少两次视角切换前的明确动作收束；段落平均不超过四行。\n"
        "- **尺度**：暴力与血腥可直写不遮掩；亲密戏不回避身体细节。"
    )
    brief = (
        "长篇奇幻《秤与灰雪》，第三人称限制视角（跟随林砚）。基调冷硬、克制、器物感强。"
        "主题：誓约的代价。要求每章推进一条明线（秤石）与一条暗线（低语的来源）。"
        "不得提前揭底，不得写总结式结尾。单章目标 5000 字。"
    )
    outline = "主角在旧矿口与人质交换前勘察地形，发现旧符纹仍在活动，并在交换前夜做出取舍。"

    def grow(parts: list[str], item: str) -> None:
        parts.append(item)

    # 铺到目标字数：按固定顺序循环追加"历史档案"式条目（确定性、无随机）
    history: list[str] = []
    idx = 1
    while sum(len(x) for x in history) < max(0, target_chars - 6000):
        history.append(
            f"卷宗 {idx:03d}：第三次断脉之乱第 {idx} 旬记录 —— 参与宗门 {idx % 7 + 2} 家，"
            f"伤亡登记 {idx * 13 % 400 + 20} 人，损毁法器 {idx * 7 % 60 + 5} 件；"
            f"残存符纹类型以『引』居多，次为『绝』；卷末批注：本源外泄痕迹在静默期内不可追踪。"
        )
        idx += 1

    return {
        "brief": brief,
        "outline": outline,
        "worldview_rules": "\n".join(f"{i}. {t}" for i, t in enumerate(world, 1)),
        "character_states": "\n".join(f"- **{k}**：{v}" for k, v in chars.items()),
        "recent_summaries": "\n".join(f"- {t}" for t in summaries[-6:]),
        "related_summaries": "\n".join(f"- {t}" for t in summaries) + "\n" + "\n".join(history),
        "state_board": "\n".join(f"- {t}" for t in state_board),
        "foreshadowing": "\n".join(f"- {t}" for t in foreshadowing),
        "due_foreshadowing": "\n".join(f"- {t}" for t in due),
        "style_guide": style_guide,
        "custom_constraints": constraints,
        "story_overview": "《秤与灰雪》：押运任务被调包，主角追查秤石与人质，"
                          "在誓约与亲情之间取舍；暗线是残余意志的低语。",
        "chapter_text": ("旧矿口的石壁在夜里泛出钝光。林砚把手按在壁上，指腹触到一段旧符纹，"
                         "纹路里嵌着碎铁屑。他把火折子压低，光沿着『绝』字走了半寸，停住。\n"
                         "腕骨内侧的契文跟着一跳，像有人在骨头上敲了一下。" * 40),
    }


# ---------- 子进程：单次调用 ----------

_CHILD = r'''
import json, os, sys, time
from pathlib import Path
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.agents.architect import _looks_like_refusal
from src.agents.prompt_loader import render_prompt
from src.agents.reality_policy import reality_policy_text
from src.agents.schemas import PlotCardsOutput, ReviewOutput
from src.config.settings import load_models_config
from src.llm.base import ChatMessage
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured

fixture = json.loads(Path(os.environ["INKFORGE_FIXTURE"]).read_text(encoding="utf-8"))
cfg = load_models_config()
arm = json.loads(os.environ["INKFORGE_ARM"])

# 把全部角色指向本臂的 provider/model（其余参数沿用 models.yaml）
for role in cfg.roles.values():
    role.provider = arm["provider"]
    role.model = arm["model"]
registry = ModelRegistry(cfg)

case = os.environ["INKFORGE_CASE"]
max_tokens = int(os.environ.get("INKFORGE_MAX_TOKENS", "16000"))
out = {"case": case, "ok": False}

if case == "writer_long":
    prompt = render_prompt(
        "writer_chapter",
        brief_fidelity="【测试】合成上下文对照，无作者作品数据。",
        brief=fixture["brief"], target_words=5000, tolerance=2500,
        length_floor=2500, length_ceiling=7000, revision_section="",
        chapter=13, outline=fixture["outline"],
        recent_summaries=fixture["recent_summaries"],
        related_summaries=fixture["related_summaries"],
        character_states=fixture["character_states"],
        foreshadowing=fixture["foreshadowing"],
        due_foreshadowing=fixture["due_foreshadowing"],
        worldview_rules=fixture["worldview_rules"],
        state_board=fixture["state_board"], style_guide=fixture["style_guide"],
        custom_constraints=fixture["custom_constraints"],
        reality_policy=reality_policy_text(False),
    )
    t0 = time.time()
    res = registry.chat_as("writer", [ChatMessage("user", prompt)],
                           temperature=0.7, max_tokens=max_tokens)
    text = (res.content or "").strip()
    out.update({
        "ok": True, "chars": len(text), "prompt_chars": len(prompt),
        "class": ("空产出" if not text else ("拒答文本" if _looks_like_refusal(text) else "合格产出")),
        "finish_reason": res.finish_reason or "",
        "completion_tokens": (res.usage or {}).get("completion_tokens"),
        "latency_s": round(time.time() - t0, 1),
        "tail_marker": text[-80:] if text else "",
    })
elif case == "cards_json":
    prompt = render_prompt(
        "plotter_cards",
        chapter=13, story_overview=fixture["story_overview"],
        recent_summaries=fixture["recent_summaries"],
        related_summaries=fixture["related_summaries"],
        character_states=fixture["character_states"],
        state_board=fixture["state_board"],
        foreshadowing=fixture["foreshadowing"],
        due_foreshadowing=fixture["due_foreshadowing"],
        worldview_rules=fixture["worldview_rules"],
        custom_constraints=fixture["custom_constraints"],
        feedback="", brief=fixture["brief"],
        reality_policy=reality_policy_text(False),
    )
    t0 = time.time()
    try:
        cards = chat_structured(registry, "architect", [ChatMessage("user", prompt)],
                                PlotCardsOutput, max_parse_retries=0)
        out.update({"ok": True, "cards": len(cards.cards), "prompt_chars": len(prompt),
                    "latency_s": round(time.time() - t0, 1)})
    except Exception as exc:
        out.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "prompt_chars": len(prompt), "latency_s": round(time.time() - t0, 1)})
else:  # review_json
    text = fixture["chapter_text"]
    prompt = render_prompt(
        "editor_review",
        chapter=13, outline=fixture["outline"],
        recent_summaries=fixture["recent_summaries"],
        character_states=fixture["character_states"],
        worldview_rules=fixture["worldview_rules"],
        foreshadowing=fixture["foreshadowing"],
        style_guide=fixture["style_guide"],
        custom_constraints=fixture["custom_constraints"],
        chapter_text=text, target_words=5000, actual_length=len(text),
        tolerance=2500, length_floor=2500, length_ceiling=7000,
        brief=fixture["brief"], reality_policy=reality_policy_text(False),
    )
    t0 = time.time()
    try:
        rev = chat_structured(registry, "editor", [ChatMessage("user", prompt)],
                              ReviewOutput, max_parse_retries=0)
        out.update({"ok": True, "overall": rev.overall, "issues": len(rev.issues),
                    "prompt_chars": len(prompt), "latency_s": round(time.time() - t0, 1)})
    except Exception as exc:
        out.update({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "prompt_chars": len(prompt), "latency_s": round(time.time() - t0, 1)})

print("INKFORGE_BAKEOFF=" + json.dumps(out, ensure_ascii=False))
'''


def _run(env: dict, fixture_path: Path, arm: dict, case: str, max_tokens: int,
         timeout: float) -> dict:
    env = dict(env)
    env["INKFORGE_FIXTURE"] = str(fixture_path)
    env["INKFORGE_ARM"] = json.dumps(arm, ensure_ascii=False)
    env["INKFORGE_CASE"] = case
    env["INKFORGE_MAX_TOKENS"] = str(max_tokens)
    proc = subprocess.run([sys.executable, "-c", _CHILD], cwd=str(ENGINE_DIR), env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("INKFORGE_BAKEOFF=")), None)
    if line is None:
        return {"case": case, "ok": False,
                "error": f"子进程未返回结果（exit={proc.returncode}）: "
                         f"{(proc.stderr or proc.stdout)[-300:]}"}
    return json.loads(line[len("INKFORGE_BAKEOFF="):])


def _p50(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def main() -> int:
    ap = argparse.ArgumentParser(description="模型对照（仅授权的 3 个对象）")
    ap.add_argument("--writer-repeat", type=int, default=2)
    ap.add_argument("--json-repeat", type=int, default=3)
    ap.add_argument("--context-chars", type=int, default=30000)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    sandbox = Path(tempfile.mkdtemp(prefix="inkforge-bakeoff-"))
    env = dict(os.environ)
    env.update({"NOVELS_DIR": str(sandbox / "data" / "novels"),
                "RUNTIME_DIR": str(sandbox / "data" / "runtime"),
                "CONFIGS_DIR": str(sandbox / "configs"),
                "INKFORGE_ENGINE_TOKEN": TOKEN, "INKFORGE_GIT_HISTORY": "0",
                "PYTHONIOENCODING": "utf-8"})
    build_sandbox(sandbox)
    fixture = build_fixture(args.context_chars)
    fixture_path = sandbox / "fixture.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
    fixture_chars = sum(len(v) for v in fixture.values())

    print("=" * 96)
    print(f"模型对照 {BAKEOFF_VERSION} | 合成上下文合计 {fixture_chars} 字 | "
          f"writer×{args.writer_repeat} + json×{args.json_repeat} | max_tokens={args.max_tokens}")
    for a in ARMS:
        print(f"  臂 {a['id']:<18} = {a['provider']} / {a['model']}")
    print("=" * 96)

    records: list[dict] = []
    try:
        for arm in ARMS:
            for case, repeat in (("writer_long", args.writer_repeat),
                                 ("cards_json", args.json_repeat),
                                 ("review_json", args.json_repeat)):
                for i in range(1, repeat + 1):
                    try:
                        res = _run(env, fixture_path, arm, case, args.max_tokens, args.timeout)
                    except Exception as exc:  # noqa: BLE001
                        res = {"case": case, "ok": False,
                               "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
                    res.update({"arm": arm["id"], "provider": arm["provider"],
                                "model": arm["model"], "repeat": i})
                    records.append(res)
                    brief = (f"{res.get('class') or ('OK' if res.get('ok') else 'ERR')}"
                             f" chars={res.get('chars')} {res.get('latency_s')}s"
                             if case == "writer_long"
                             else (f"{'OK' if res.get('ok') else 'ERR'} {res.get('latency_s')}s"
                                   f" {res.get('error', '')[:60]}"))
                    print(f"  [{arm['id']:<18}] {case:<12} #{i}  {brief}")
    finally:
        if not args.keep_sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
        else:
            print(f"沙箱已保留：{sandbox}")

    # ---- 汇总 ----
    print()
    print("=" * 96)
    print("【writer_long】拒答率 / 空产出率 / 截断率 / 字数 / 耗时")
    for arm in ARMS:
        rs = [r for r in records if r["arm"] == arm["id"] and r["case"] == "writer_long"]
        n = len(rs)
        ref = sum(1 for r in rs if r.get("class") == "拒答文本")
        emp = sum(1 for r in rs if r.get("class") == "空产出")
        trc = sum(1 for r in rs if (r.get("finish_reason") or "") == "length")
        chars = [r.get("chars") or 0 for r in rs if r.get("class") == "合格产出"]
        lat = [r.get("latency_s") for r in rs if r.get("latency_s")]
        print(f"  {arm['id']:<18} n={n}  拒答={ref}/{n}  空产出={emp}/{n}  截断={trc}/{n}  "
              f"字数={chars}  耗时 p50={_p50(lat)}s")
    print()
    print("【结构化 JSON】失败率 / 耗时（失败=无法解析为符合 schema 的 JSON）")
    for arm in ARMS:
        for case in ("cards_json", "review_json"):
            rs = [r for r in records if r["arm"] == arm["id"] and r["case"] == case]
            n = len(rs)
            bad = sum(1 for r in rs if not r.get("ok"))
            lat = [r.get("latency_s") for r in rs if r.get("latency_s")]
            extra = ""
            if case == "cards_json":
                extra = " 卡片数=" + str([r.get("cards") for r in rs if r.get("ok")])
            else:
                extra = " 评分=" + str([r.get("overall") for r in rs if r.get("ok")])
            print(f"  {arm['id']:<18} {case:<12} n={n} 失败={bad}/{n} "
                  f"耗时 p50={_p50(lat)}s{extra}")
    print("=" * 96)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ENGINE_DIR / "smoke-reports" / f"bakeoff-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"version": BAKEOFF_VERSION, "stamp": stamp, "arms": ARMS,
         "fixture_chars": fixture_chars, "context_chars_target": args.context_chars,
         "max_tokens": args.max_tokens,
         "writer_repeat": args.writer_repeat, "json_repeat": args.json_repeat,
         "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化结果：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
