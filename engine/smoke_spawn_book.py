#!/usr/bin/env python
"""真实冒烟：二开建书（书名落盘 + 素材只取当前书）。

覆盖用户实测的两个问题：
  ① 只填了书名，结果**书名消失**（书架回落目录名）；
  ② 默认把**全库**素材都勾上导入（用户感受：多书混合资料库）。

本脚本**不调用 LLM**（建书 + 素材导入都是纯本地路径），用自造素材跑在临时沙箱里：
  · 建两本"来源书"，各写若干条不同分类的素材（走 /api/materials，带 source_book）；
  · 从其中一本书二开建书，传**该书专属的 material_ids**（等价于前端"默认只勾当前书"）；
  · 断言：书架书名 == 作者填的书名；导入集合**逐条属于该书**；另一本书的素材一条都没进来；
  · 再用不传 material_ids 的老调用形态跑一次，确认默认行为仍然可用。

用法：
    python engine/smoke_spawn_book.py
    python engine/smoke_spawn_book.py --keep-sandbox
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
from smoke_test import TOKEN, EngineProcess, build_sandbox, http  # noqa: E402

ENGINE_DIR = Path(__file__).resolve().parent
CATS = ["世界观", "人物", "道具", "地点", "技法", "文风", "桥段"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-sandbox", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sandbox = Path(tempfile.mkdtemp(prefix=f"inkforge-spawn-{stamp}-"))
    data = build_sandbox(sandbox)
    env = dict(os.environ)
    env.update({"NOVELS_DIR": str(data / "novels"), "RUNTIME_DIR": str(data / "runtime"),
                "CONFIGS_DIR": str(sandbox / "configs"),
                "INKFORGE_ENGINE_TOKEN": TOKEN, "INKFORGE_GIT_HISTORY": "1"})
    nid = "smoke-spawn"
    eng = EngineProcess(ENGINE_DIR, nid, env)
    results: list[tuple[str, bool, str]] = []

    def chk(name: str, ok: bool, ev: object = "") -> None:
        results.append((name, bool(ok), str(ev)[:220]))
        line = f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {str(ev)[:220]}" if ev else "")
        try:
            print(line, flush=True)
        except UnicodeEncodeError:  # pragma: no cover
            print(line.encode("utf-8", "replace").decode("utf-8"), flush=True)

    try:
        eng.start()
        b = eng.base

        # ── 造两本"来源书"的素材（自造内容，不碰用户素材库）──
        made: dict[str, dict[str, str]] = {"书甲": {}, "书乙": {}}
        for book in ("书甲", "书乙"):
            for cat in CATS:
                title = f"{book}-{cat}"
                r = http("POST", f"{b}/api/materials", body={
                    "title": title,
                    "content": f"{book}的{cat}条目（冒烟自造，与真实作品无关）",
                    "category": cat,
                    "source_book": book,
                })
                chk(f"0 造素材 {title}", r.status == 200, r.body)
                made[book][cat] = (r.body or {}).get("id", "")

        # 前端 spawnPick 的等价选择：只勾"书甲"的素材（桥段默认不勾）
        picked = [made["书甲"][c] for c in CATS if c != "桥段" and made["书甲"][c]]

        # ── ① + ②：二开建书（携当前书的素材 id）──
        r = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-a", "title": "甲书二开新作", "mode": "interactive",
            "material_ids": picked,
        })
        chk("1 二开建书成功", r.status == 200, r.body)
        body = r.body or {}
        chk("2 回执带书名", body.get("title") == "甲书二开新作", f"title={body.get('title')!r}")

        # ★ 书名落盘 + 书架可见
        ov = data / "novels" / "spawn-a" / "settings" / "story-overview.md"
        chk("3 ★ 书名落盘 settings/story-overview.md", ov.exists(),
            str(ov.relative_to(data)))
        if ov.exists():
            txt = ov.read_text(encoding="utf-8")
            chk("4 ★ frontmatter 记录 book_title",
                "book_title: 甲书二开新作" in txt,
                txt.split("---")[1][:80].replace("\n", " ") if "---" in txt else txt[:60])
        books = (http("GET", f"{b}/api/books").body or {}).get("books", [])
        item = next((x for x in books if x["novel_id"] == "spawn-a"), None)
        chk("5 ★ 书架书名 = 作者填的书名（不再回落目录名）",
            bool(item) and item.get("title") == "甲书二开新作",
            f"title={item and item.get('title')!r}")

        # ★ 只导入了"书甲"的素材
        by_book = body.get("by_book") or {}
        chk("6 ★ 导入来源只有书甲一本书", list(by_book.keys()) == ["书甲"],
            json.dumps(by_book, ensure_ascii=False))
        chk("7 ★ 导入条数 = 书甲该批素材数（无别的书混入）",
            body.get("imported") == len(picked),
            f"imported={body.get('imported')} expected={len(picked)}")

        new_book = data / "novels" / "spawn-a" / "settings"
        imported_text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in list((new_book / "worldview").glob("*.md"))
            + list((new_book / "characters").glob("*.md"))
            + [q for q in (new_book / "style.md", new_book / "custom-skills.md") if q.exists()]
        )
        chk("8 ★ 落盘内容里没有书乙的任何条目", "书乙" not in imported_text,
            f"len={len(imported_text)}")
        chk("9 落盘内容含书甲条目", "书甲" in imported_text)

        # ── 老调用形态（不传 material_ids）：默认分类导入仍可用 ──
        r2 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-b", "title": "默认分类新作", "mode": "pipeline",
        })
        chk("10 不传 material_ids 时按默认分类导入", r2.status == 200
            and (r2.body or {}).get("imported", 0) >= 1,
            json.dumps(r2.body, ensure_ascii=False)[:160])
        chk("11 该路径同样落盘书名",
            (data / "novels" / "spawn-b" / "settings" / "story-overview.md").exists())

        # ── 重名保护 ──
        r3 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-a", "title": "重名",
        })
        chk("12 重复建书 → 409", r3.status == 409, f"status={r3.status}")

        # ── ★ 用户实测缺陷：点了「只看当前书」后仍导入全库 ──
        # 复现路径：先点「全部来源书」（全部维度来源清空），再点「只看当前书」。
        # UI 侧选择逻辑已由 check-spawn-pick.mjs 锁定；这里验证**提交后引擎真的只导书甲**。
        all_ids = [made[book][cat] for book in ("书甲", "书乙") for cat in CATS]
        r5 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-mix", "title": "全库混选", "mode": "interactive",
            "material_ids": all_ids,
        })
        chk("14 先点「全部来源书」→ 确实导入两本书的素材（对照组）",
            "书甲" in (r5.body or {}).get("by_book", {})
            and "书乙" in (r5.body or {}).get("by_book", {}),
            json.dumps((r5.body or {}).get("by_book"), ensure_ascii=False))

        # 「只看当前书」= 所有维度来源回到书甲 + 按书甲重新预勾选（桥段不勾）
        back_ids = [made["书甲"][c] for c in CATS if c != "桥段"]
        r6 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-back", "title": "只看当前书", "mode": "interactive",
            "material_ids": back_ids,
        })
        chk("15 ★ 点「只看当前书」后只导当前书（书乙 0 条）",
            (r6.body or {}).get("by_book", {}).keys() == {"书甲"},
            json.dumps((r6.body or {}).get("by_book"), ensure_ascii=False))
        chk("16 ★ 导入条数 = 当前书该批素材数",
            (r6.body or {}).get("imported") == len(back_ids),
            f"imported={(r6.body or {}).get('imported')} expected={len(back_ids)}")
        new2 = data / "novels" / "spawn-back" / "settings"
        text2 = "\n".join(
            p.read_text(encoding="utf-8")
            for p in list((new2 / "worldview").glob("*.md"))
            + list((new2 / "characters").glob("*.md"))
            + [q for q in (new2 / "style.md", new2 / "custom-skills.md") if q.exists()]
        )
        chk("17 ★ 落盘内容里没有书乙条目", "书乙" not in text2, f"len={len(text2)}")

        # ── ★ 中文书名 + 不填目录名：按书名派生标识（二版默认路径）──
        r7 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "", "title": "拆装时代", "mode": "interactive",
            "material_ids": [made["书甲"]["世界观"]],
        })
        chk("18 ★ 中文书名不填目录名也能建书（不再 400）", r7.status == 200, r7.body)
        derived = (r7.body or {}).get("novel_id", "")
        chk("19 ★ 目录名按书名派生（纯中文 → book-日期）",
            derived.startswith("book-"), f"novel_id={derived!r}")
        chk("20 派生目录下书名仍是中文书名",
            (r7.body or {}).get("title") == "拆装时代"
            and (data / "novels" / derived / "settings" / "story-overview.md").exists(),
            f"title={(r7.body or {}).get('title')!r}")
        books2 = (http("GET", f"{b}/api/books").body or {}).get("books", [])
        it2 = next((x for x in books2 if x["novel_id"] == derived), None)
        chk("21 书架显示中文书名", bool(it2) and it2.get("title") == "拆装时代",
            f"title={it2 and it2.get('title')!r}")

        r8 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "", "title": "My Second Book", "mode": "interactive",
            "material_ids": [made["书甲"]["人物"]],
        })
        chk("22 英文书名派生成 slug",
            (r8.body or {}).get("novel_id") == "my-second-book",
            f"novel_id={(r8.body or {}).get('novel_id')!r}")

        # ── 名称留空 → 回落 novel_id（老语义保留）──
        r4 = http("POST", f"{b}/api/materials/spawn-book", body={
            "novel_id": "spawn-c", "title": "  ", "mode": "interactive",
        })
        chk("13 书名留空 → 回落为 novel_id", r4.status == 200
            and (r4.body or {}).get("title") == "spawn-c",
            f"title={(r4.body or {}).get('title')!r}")

    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        chk("FATAL 冒烟异常", False, f"{type(exc).__name__}: {exc}")
        print("\n".join(eng.tail(30)))
    finally:
        eng.stop()

    fails = [x for x in results if not x[1]]
    print()
    print("=" * 78)
    print(f"总计：{len(results) - len(fails)} 通过 / {len(fails)} 失败 → "
          f"{'PASS' if not fails else 'FAIL'}")
    print("=" * 78)
    out = ENGINE_DIR / "smoke-reports" / f"smoke-spawn-{stamp}.json"
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
