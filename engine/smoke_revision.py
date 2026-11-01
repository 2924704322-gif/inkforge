"""真实冒烟：正文改写模块（创作功能第四项）。

默认 **dry-run**（只打印将要发什么、不发调用）；加 `--yes` 才真发模型调用（会计费）。

为什么需要它：`tests/test_revision_forge.py` 全部打桩 —— 它证明的是"装配与闸门写对了"，
证明不了"真模型产出的东西符合要求"。本脚本补的正是这一环：

  1. 扩写（点名锚点）→ 断言闸门通过、且**未点名的段落逐字未动**；
  2. 越界诱导（要求写着"只加一段"，看它会不会顺手润色别的段）→ 统计越界次数；
  3. 带自定义约束 → 看约束条目是否体现在产出里；
  4. 拒答诱导（露骨/黑暗题材）→ 断言没有把拒答文本当结果返回。

用法：
  python engine/smoke_revision.py            # dry-run：看场景与 prompt 摘要
  python engine/smoke_revision.py --yes      # 真发调用
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.services import revision_forge as rev  # noqa: E402

SOURCE = """他推开门，屋里没人。

木桌上的茶还温着，杯口浮着一层薄薄的水汽。

窗外的雨声忽然停了，屋檐上最后几滴落下来，砸在青石板上，一声，又一声。

他站着没动，手还搭在门框上。

灯芯爆了一下，把整间屋子照得晃了晃。他想起出门前她说的话——「你要是回来得早，就先把火生上。」

他没生火。"""

SCENARIOS = [
    {
        "name": "1-扩写（点名锚点）",
        "requirement": "在第 1 段（「他推开门，屋里没人。」）之后补一段院子里的雨与灯的环境描写，约 200 字。前面的句子一句都不要动。",
        "anchors": [{"after_index": 1}],
        "constraints": "",
    },
    {
        "name": "2-越界诱导（只加一段，看它动不动别的段）",
        "requirement": "加一段描写。",
        "anchors": [],
        "constraints": "",
    },
    {
        "name": "3-带自定义约束（约束优先于要求）",
        "requirement": "把最后一段改写得更含蓄一点。",
        "anchors": [{"after_index": 6}],
        "constraints": "- **句式**：不连续三句用同一主语\n- **用词**：不使用「仿佛」「宛如」这类比喻堆砌",
    },
    {
        "name": "4-拒答诱导（黑暗题材 + 直白要求）",
        "requirement": "把第 5 段改写得更阴冷、更绝望，保留所有既有句子，只在句末加短句。",
        "anchors": [{"after_index": 5}],
        "constraints": "",
    },
    {
        "name": "5-清楚要求 + 自定义约束（不带锚点 · 防「改了个寂寞」）",
        "requirement": "把这几段的节奏收紧：凡是连续用同一个主语开头的句子，改成不同主语或换说法；"
                       "最后一段收得更利落。其余内容保持不变。",
        "anchors": [],
        "constraints": "- **句式**：不连续三句用同一主语开头\n- **用词**：不使用「仿佛」「宛如」这类比喻堆砌",
    },
    {
        # 这一条锁的是 2026-10-05 的真机缺陷：**长文 + 全篇性要求**曾被越界闸门拦到 4 轮全废，
        # 而模型在"不许乱改"的纪律下最省事的合规输出就是原样抄一遍（日志 8/9 次回吐原文）。
        # 现在按"要求作用范围"分叉：全篇要求下越界让路，只守「内容不许丢 / 不许原样回吐」。
        "name": "6-长文 + 全篇要求（越界让路 · 防多轮失败）",
        "requirement": "整体读起来太拖沓，全篇润色：删掉重复的形容，长句拆短，节奏收紧。情节与对白内容不变。",
        "anchors": [],
        "constraints": "",
        "long": True,
    },
]

#: 场景 6 用的长文本（~2000 字，24 段）：长文才会暴露"全篇要求 vs 局部闸门"的冲突
LONG_SOURCE = "\n\n".join(
    ["他推开门，屋里没人。", "木桌上的茶还温着，杯口浮着一层薄薄的水汽。"]
    + [
        f"第{i}间屋子在走廊尽头。他推门进去，里面比上一间更潮，墙皮掉了一大片，"
        f"露出底下灰扑扑的水泥。窗户朝北，玻璃上积着一层洗不掉的灰，光透进来也是灰的。"
        f"靠墙有一张单人床，床板上摊着一条发霉的毯子。床头柜上放着{i}个空药瓶，"
        f"瓶身上的标签被水泡烂了，只剩下半行字。他蹲下来看了一会儿，没有动它们。"
        for i in range(1, 17)
    ]
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真发模型调用（默认 dry-run）")
    args = ap.parse_args()

    print(f"正文改写真实冒烟 — 原文 {len(SOURCE)} 字 / {len(rev.split_paragraphs(SOURCE))} 段")
    print(f"模式：{'真机调用' if args.yes else 'dry-run（加 --yes 才发调用）'}")
    fails = 0

    for sc in SCENARIOS:
        source = LONG_SOURCE if sc.get("long") else SOURCE
        mode, why = rev.detect_mode(sc["requirement"])
        scope, scope_why = rev.resolve_scope(sc["requirement"], sc["anchors"])
        print("\n" + "=" * 78)
        print(f"[{sc['name']}]  原文 {len(source)} 字 · 判定模式={rev.MODE_LABELS.get(mode)}"
              f"{f'（命中「{why}」）' if why else ''}"
              f" · 作用范围={rev.SCOPE_LABELS.get(scope)}"
              f"{f'（判据「{scope_why}」）' if scope_why else ''}")
        if not args.yes:
            msgs = rev.build_messages(
                source, sc["requirement"], mode=mode,
                anchors=rev.resolve_anchors(source, sc["anchors"]),
                constraints=sc["constraints"],
                fingerprint=rev.style_fingerprint(source),
                scope=scope,
            )
            print(f"  system {len(msgs[0].content)} 字 / user {len(msgs[1].content)} 字"
                  f"（其中原文 {len(source)} 字）")
            continue

        try:
            out = rev.extract_revision(
                source, sc["requirement"], mode=mode,
                anchors=sc["anchors"], constraints=sc["constraints"],
                lock_dialogue=True,
            )
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本要看清失败原因
            print(f"  [FAIL] {type(exc).__name__}: {exc}")
            fails += 1
            continue

        st = out["stats"]
        ck = out["checks"]
        print(f"  {out['summary']}")
        if st["unchanged"]:
            print("  [FAIL] 产出与原文一字未改（零改动闸门应当已经拦下它）")
            fails += 1
            continue
        print(f"  闸门：最小改动={ck['minimal_edit']['ok']}"
              f"（软告警={ck['minimal_edit']['soft']}，越界 {len(ck['minimal_edit']['out_of_scope'])} 段）"
              f" 锁定={ck['locked']['ok']} 整篇={ck['whole_returned']['ok']}"
              f"（相似度 {ck['whole_returned']['similarity']}）"
              f" · 作用范围={out['scope_label']}"
              f"（{out['scope_reason'] or '默认'}）")
        if ck["minimal_edit"].get("relaxed"):
            print(f"  收敛：本轮放宽过 {'；'.join(ck['minimal_edit']['relaxed'])}")
        print(f"  文风：句长 {out['style_shift']['before']['avg_sentence_len']}"
              f" → {out['style_shift']['after']['avg_sentence_len']}"
              f"（{'一致' if out['style_shift']['ok'] else '偏差偏大'}）")
        print(f"  {out['model']} · 新增 {st['inserted']} 段 / 改写 {st['edited']} 段 / "
              f"删除 {st['deleted']} 段 / 未动 {st['kept']} 段")
        if not ck["minimal_edit"]["ok"] or not ck["locked"]["ok"]:
            fails += 1

    print("\n" + "=" * 78)
    print(f"总计：{len(SCENARIOS)} 场景 · {fails} 失败"
          + ("" if args.yes else "（dry-run，未实际发送）"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
