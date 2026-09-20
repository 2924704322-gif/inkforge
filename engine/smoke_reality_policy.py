#!/usr/bin/env python
"""真实冒烟：审校是否因"不符合现实"要求改稿（用户要求③）。

判据：用一份**在作者设定内自洽、但现实中不可能**的短正文跑真实审校，看它会不会
以"不符合现实 / 不合理 / 现实中不可能"为理由扣分或提建议。

必测（默认档 `allow_realism=false`）：**不得**出现现实性判据。
对照（打开开关 `allow_realism=true`）：同一份正文允许出现现实性判据——
对照组不是为了"必须命中"，而是用来证明这条口径真的在起作用（开关不是摆设）。

用法（消耗 2 次真实 LLM 调用）：
    python engine/smoke_reality_policy.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_test import TOKEN, build_sandbox  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parent

#: 现实性判据词表（只认这些**明文**表述；"与设定冲突/吃书"不算，那是作者框架内的错误）
REALISM_MARKERS = (
    "不符合现实", "不现实", "脱离现实", "现实逻辑", "现实中不可能", "现实中不成立",
    "不合常理", "违背常识", "缺乏合理性", "不够合理", "生物", "医学", "生理学",
    "物理定律", "科学依据", "令人信服", "现实中不会",
)

BRIEF = (
    "现代都市设定，但这个世界有一条铁律：人的身体像钟表一样是**可拆装**的，"
    "心脏可以拧下来放进抽屉，换一个新的再拧回去人就活着。"
    "主角林昭是 17 岁的大学终身教授，同时是三个孩子的母亲。"
    "基调：冷静、克制、临床式描写，不要奇幻腔，不要解释这条铁律的由来。"
)

WORLDVIEW = (
    "# 身体铁律\n\n"
    "1. 人的四肢与脏器均可拆装，接口式连接，拆下后本人在 6 小时内仍可活动；\n"
    "2. 换新件后需要静置 20 分钟让接口长合；\n"
    "3. 这条铁律在本世界是常识，任何人不会对此感到惊讶，也不会解释它为何成立。\n"
)

CHAPTER = (
    "林昭把课本夹在腋下，回到办公室，先拧下自己的左腕放在桌角，"
    "再打开抽屉找出备用件。接口处的银线整齐排开，她把新的那截对上，"
    "听见一声轻响。\n\n"
    "“今天那节课，你讲得比上个月好。”同事推门进来，看见桌上那只手，"
    "只是把外卖放在旁边，绕开了它。\n\n"
    "“学生比上个月安静。”林昭用右手把左腕接回去，静置，等接口长合。"
    "她想起家里三个孩子，最小的那个昨天把心脏拿出来放进冰箱，说是要看看它会不会凉。\n\n"
    "接口长合。她把课本放进包里，出门前看了一眼窗外——十八层楼下的操场，"
    "一个学生正抱着自己的腿跑过跑道，跑得比谁都快。\n"
)


def _seed(data: Path, novel: str, allow_realism: bool) -> Path:
    """造一本最小可用的书：世界观 + 角色 + 作者需求 + 待审正文（全部为冒烟自造内容）。"""
    import frontmatter

    book = data / "novels" / novel
    (book / "settings" / "worldview").mkdir(parents=True, exist_ok=True)
    (book / "settings" / "characters").mkdir(parents=True, exist_ok=True)
    (book / "chapters" / "vol-01").mkdir(parents=True, exist_ok=True)
    (book / "summaries").mkdir(parents=True, exist_ok=True)
    (book / "reviews").mkdir(parents=True, exist_ok=True)

    (book / "settings" / "outline.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 拆装时代\n",
            title="拆装时代", theme="身体即机械",
            volumes=[{"volume": 1, "title": "卷", "depends_on": [], "chapters": [
                {"chapter": 1, "title": "第一课", "outline": "林昭下课回家，处理一桩换件的小事，结尾留钩子",
                 "characters": ["林昭"], "target_words": 1200},
                {"chapter": 2, "title": "第二课", "outline": "延续", "characters": ["林昭"]},
            ]}],
        )), encoding="utf-8", newline="\n")
    (book / "settings" / "worldview" / "body-rule.md").write_text(
        frontmatter.dumps(frontmatter.Post(WORLDVIEW, title="身体铁律")),
        encoding="utf-8", newline="\n")
    (book / "settings" / "characters" / "林昭.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            "# 林昭\n\n## 定位\n主角\n\n## 外貌\n清瘦，左腕有银色接口\n\n"
            "## 性格\n冷静克制\n\n## 背景\n17 岁终身教授，三个孩子的母亲\n",
            title="林昭", name="林昭", role="主角", personality="冷静克制")),
        encoding="utf-8", newline="\n")
    (book / "settings" / "brief.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            BRIEF,
            title="作者创作需求（brief）",
            brief_fields={"genre": "现代都市", "tone": "冷静克制",
                          "allow_realism": bool(allow_realism)},
        )), encoding="utf-8", newline="\n")
    (book / "chapters" / "vol-01" / "ch-001.md").write_text(
        frontmatter.dumps(frontmatter.Post(
            CHAPTER, chapter=1, volume=1, title="第一课", status="draft",
            characters=["林昭"], attempt=1, target_words=1200, words=len(CHAPTER))),
        encoding="utf-8", newline="\n")
    return book


def _review_via_engine(env: dict, novel: str, timeout: float = 300.0) -> dict:
    """直接在引擎进程内跑同一套 Editor（真实 LLM），拿回评分与问题清单。

    为什么不走 `/api/start`：那会连带跑大纲与章节生成（多余调用）。
    这里只复用生产同一条 prompt 渲染链路（`build_pipeline` + `retrieve_context`
    + `Editor.review_chapter`），因此校验的正是真实生效的那份口径。
    """
    import subprocess

    code = (
        "import json;"
        "from src.orchestrator.bootstrap import build_pipeline;"
        f"pipe = build_pipeline({novel!r}, target_words=1200);"
        "store = pipe.store;"
        "text = store.read('chapters/vol-01/ch-001.md').content;"
        "ctx = pipe.memory.retrieve_context(chapter=1, chapter_outline='第一课',"
        " characters=['林昭'], total_chapters=2);"
        "r = pipe.editor.review_chapter(ctx, text, 1, target_words=1200);"
        "print('INKFORGE_REVIEW=' + json.dumps({'review': r.model_dump(),"
        " 'prompt_has_policy': '现实性评判口径' in ctx.reality_policy,"
        " 'policy_text': ctx.reality_policy[:60]}, ensure_ascii=False))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ENGINE_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"审查子进程失败：{proc.stderr[-900:]}")
    marker = "INKFORGE_REVIEW="
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None)
    if line is None:
        raise RuntimeError(f"未取到审查结果：{proc.stdout[-900:]}")
    return json.loads(line[len(marker):])


def _realism_hits(review: dict) -> list[str]:
    blob = json.dumps(review, ensure_ascii=False)
    return sorted({m for m in REALISM_MARKERS if m in blob})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sandbox = Path(tempfile.mkdtemp(prefix=f"inkforge-reality-{stamp}-"))
    data = build_sandbox(sandbox)
    env = dict(os.environ)
    env.update({"NOVELS_DIR": str(data / "novels"), "RUNTIME_DIR": str(data / "runtime"),
                "CONFIGS_DIR": str(sandbox / "configs"),
                "INKFORGE_ENGINE_TOKEN": TOKEN, "INKFORGE_GIT_HISTORY": "1"})
    results: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, ev: object = "") -> None:
        results.append((name, bool(ok), str(ev)[:260]))
        line = f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {str(ev)[:260]}" if ev else "")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # pragma: no cover
            print(line.encode("utf-8", "replace").decode("utf-8"), flush=True)

    try:
        _seed(data, "reality-off", allow_realism=False)
        _seed(data, "reality-on", allow_realism=True)

        print("=" * 78)
        print("现实性口径真实冒烟：默认档不得因『不符合现实』要求改稿")
        print("=" * 78)

        off = _review_via_engine(env, "reality-off")
        rv_off = off["review"]
        hits_off = _realism_hits(rv_off)
        print("  [info] allow_realism=false 评分："
              f"{ {k: rv_off[k] for k in ('consistency','plot','continuity','prose','length')} }")
        print(f"  [info] 问题清单：{json.dumps(rv_off.get('issues'), ensure_ascii=False)[:400]}")
        chk("1 默认档：上下文带现实性口径", off["prompt_has_policy"])
        chk("2 ★ 默认档：审校未以『不符合现实/不合理』为由提任何要求",
            not hits_off, f"命中={hits_off}")
        chk("3 默认档：四维评分仍正常给出（不是靠不给分逃避）",
            all(isinstance(rv_off.get(k), (int, float))
                for k in ("consistency", "plot", "continuity", "prose")),
            json.dumps({k: rv_off.get(k) for k in
                        ("consistency", "plot", "continuity", "prose")}, ensure_ascii=False))

        on = _review_via_engine(env, "reality-on")
        rv_on = on["review"]
        hits_on = _realism_hits(rv_on)
        print("  [info] allow_realism=true 评分："
              f"{ {k: rv_on[k] for k in ('consistency','plot','continuity','prose','length')} }")
        print(f"  [info] 问题清单：{json.dumps(rv_on.get('issues'), ensure_ascii=False)[:400]}")
        chk("4 对照档：上下文同样带口径（文案切换为『作者已要求现实约束』）",
            on["prompt_has_policy"] and "作者已要求现实约束" in on.get("policy_text", ""),
            f"policy={on.get('policy_text', '')[:60]}")
        # 对照组不作"必须命中"的硬断言（模型可能仍认为该设定在书内自洽），
        # 只作为"开关确实改变了输入"的观测证据打印出来。
        print(f"  [info] 对照组现实性判据命中：{hits_on}（仅观测，不作硬断言）")

    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        chk("FATAL 冒烟异常", False, f"{type(exc).__name__}: {exc}")

    fails = [x for x in results if not x[1]]
    print()
    print("=" * 78)
    print(f"总计：{len(results) - len(fails)} 通过 / {len(fails)} 失败 → "
          f"{'PASS' if not fails else 'FAIL'}")
    print("=" * 78)
    out = ENGINE_DIR / "smoke-reports" / f"smoke-reality-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"results": [{"case": n, "ok": o, "evidence": e} for n, o, e in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结构化结果：{out}")
    if not args.keep_sandbox:
        shutil.rmtree(sandbox, ignore_errors=True)
    else:
        print(f"沙箱已保留：{sandbox}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
