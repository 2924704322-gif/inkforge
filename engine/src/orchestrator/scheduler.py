"""卷级并行子图调度（M2 / T2.2）：大纲依赖标记 → 拓扑分层 → 并行起草 → 状态合并。

设计约束（D4 逐章人审 + D9 MD 唯一事实源）：
- 仅"起草 + 自动分级重写"阶段并行：writer→editor 自动循环（局部/整章重写各上限 2 次，D6），
  该阶段只读共享记忆、只写各自章节草稿文件，天然无写冲突。
- 逐章人审仍串行进行；记忆回写发生在人审通过之后（writeback），故并行阶段不触碰记忆写入。
- 并行粒度为"卷"：仅并行 depends_on 关系上彼此独立的卷；卷内章节因连贯性保持串行。
- MdStore 的 Git 提交非线程安全，故并行阶段的落盘统一用 store_lock 串行化（LLM 调用仍并行，
  瓶颈在 LLM，锁开销可忽略）。

依赖标记规范：VolumePlan.depends_on = [前置卷号...]。空表示无依赖（可最早起草）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict

from src.agents.editor import Editor, negotiate_revision, verdict_of
from src.agents.writer import chapter_length, length_deviation, length_revision_note
from src.config.app_config import GenerationConfig, get_app_config
from src.memory.memory_manager import ChapterContext
from src.orchestrator.state import NovelState, chapter_plans, resolve_chapter_target
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 默认重试上限（D6）；实际生效值经 generation_config 从分层配置读取（P4-A 接线）
MAX_PARTIAL_RETRIES = 2
MAX_FULL_RETRIES = 2


def generation_config(pipe) -> GenerationConfig:
    """取生成流程参数：优先 Pipeline 装配注入（pipe.gen_config），
    缺省回退全局分层配置（含测试 mock pipe）。"""
    cfg = getattr(pipe, "gen_config", None)
    return cfg if cfg is not None else get_app_config().generation


# ---------- 依赖解析与拓扑分层 ----------

def volume_deps(outline: dict) -> dict[int, list[int]]:
    """提取卷依赖图：{卷号: [前置卷号...]}。"""
    deps: dict[int, list[int]] = {}
    for vol in outline.get("volumes", []):
        vid = vol["volume"]
        deps[vid] = [d for d in vol.get("depends_on", []) if d != vid]
    return deps


def compute_waves(outline: dict) -> list[list[int]]:
    """按卷依赖做拓扑分层（Kahn）。返回并行波次列表，每个波次内的卷可并行起草。

    - 同一波次内的卷彼此无依赖关系。
    - 依赖了不存在卷号的项被忽略（容错），指向自身的依赖已在 volume_deps 中剔除。
    - 存在环时抛 ValueError（大纲依赖非法）。
    """
    deps = volume_deps(outline)
    known = set(deps)
    # 仅保留指向已知卷的依赖
    pending = {vid: {d for d in ds if d in known} for vid, ds in deps.items()}

    waves: list[list[int]] = []
    resolved: set[int] = set()
    while pending:
        ready = sorted(vid for vid, ds in pending.items() if ds <= resolved)
        if not ready:
            raise ValueError(f"卷依赖存在环或无法满足：{pending}")
        waves.append(ready)
        resolved.update(ready)
        for vid in ready:
            pending.pop(vid)
    return waves


def chapters_of_volume(outline: dict, volume: int) -> list[dict]:
    """取某卷下按章节号升序排列的章节计划（含 volume 字段）。"""
    plans = [p for p in chapter_plans({"outline": outline}) if p["volume"] == volume]
    plans.sort(key=lambda c: c["chapter"])
    return plans


# ---------- 并行起草 ----------

def draft_chapter(pipe, plan: dict, ctx: ChapterContext,
                  store_lock: threading.Lock | None = None,
                  revision_notes: str | None = None,
                  previous_text: str = "",
                  start_attempt: int = 0) -> dict:
    """对单章执行 writer→editor 自动分级重写循环，产出待人审草稿记录（不做人审、不回写记忆）。

    revision_notes/previous_text/start_attempt 供"人工打回后重稿"复用：携带人工意见
    与上一稿正文继续循环，attempt 从 start_attempt 继续累计。
    返回记录：{chapter, volume, title, draft_text, review, attempt, verdict, retry_exceeded, ctx}。
    """
    chapter = plan["chapter"]
    volume = plan["volume"]
    gen = generation_config(pipe)
    # 逐章预期字数（问题3）：与主图同一解析入口（大纲预算优先 → 全局默认）；
    # 并行路径此前只用管线级 target，导致"大纲里写了 5000、并行写出来按 3000 判"。
    target = resolve_chapter_target(plan, getattr(pipe.writer, "target_words", None))
    tolerance = gen.tolerance_for(target) if target else gen.word_count_tolerance
    attempt = start_attempt
    draft_text = previous_text
    partial_retries = 0
    full_retries = 0
    length_retries = 0
    retry_exceeded = False
    review = None
    verdict = "pass"

    while True:
        result = pipe.writer.write_chapter(
            ctx, revision_notes=revision_notes, previous_text=draft_text or None,
            target_words_override=target,
        )
        attempt += 1
        draft_text = result.content
        rel = pipe.store.chapter_rel_path(volume, chapter)
        meta = {
            "chapter": chapter,
            "volume": volume,
            "title": plan.get("title", ""),
            "characters": plan.get("characters", []),
            "status": "draft",
            "attempt": attempt,
            "target_words": target,
            "model": f"{result.provider_name}/{result.model}",
            "used_fallback": result.used_fallback,
        }
        # Git 提交非线程安全：并行阶段串行化落盘
        if store_lock is not None:
            with store_lock:
                pipe.store.write(rel, draft_text, metadata=meta,
                                 commit_message=f"ch-{chapter:03d} 第 {attempt} 稿（并行起草）")
        else:
            pipe.store.write(rel, draft_text, metadata=meta,
                             commit_message=f"ch-{chapter:03d} 第 {attempt} 稿（并行起草）")

        review = pipe.editor.review_chapter(ctx, draft_text, attempt, target_words=target)
        verdict = verdict_of(review.overall)

        # 字数门禁：质量 pass 但字数超差 → 追加一次字数修正（保留上一稿，不协商）
        length_note = ""
        if target is not None and gen.length_gate_enabled:
            actual = chapter_length(draft_text)
            if length_deviation(actual, target) > tolerance:
                length_note = length_revision_note(target, actual, tolerance)

        if verdict == "pass":
            if length_note and length_retries < gen.max_length_retries:
                logger.info(
                    "第 %d 章字数门禁触发：偏差 %+d 字 → 追加字数修正",
                    chapter, chapter_length(draft_text) - target,
                )
                length_retries += 1
                revision_notes = length_note
                continue
            break
        if verdict == "partial_rewrite":
            if partial_retries < gen.max_partial_retries:
                notes = (
                    negotiate_revision(pipe, ctx, review, draft_text)
                    if gen.negotiation_enabled else None
                )
                if notes == "":
                    # 协商后全部问题被豁免：跳过重写直接送人审
                    logger.info("第 %d 章协商后全部问题豁免，直接送人审", chapter)
                    break
                partial_retries += 1
                base = notes or Editor.issues_digest(review)
                revision_notes = (
                    "【局部重写】" + base
                    + (("\n\n" + length_note) if length_note else "")
                )
                continue
            retry_exceeded = True
            break
        else:  # full_rewrite
            if full_retries < gen.max_full_retries:
                full_retries += 1
                revision_notes = (
                    "【整章重写】上一稿总分过低，请抛弃原稿结构重新创作。问题清单：\n"
                    + Editor.issues_digest(review)
                    + (("\n\n" + length_note) if length_note else "")
                )
                draft_text = ""
                continue
            retry_exceeded = True
            break

    actual_words = chapter_length(draft_text)
    dev = length_deviation(actual_words, target) if target is not None else None
    return {
        "chapter": chapter,
        "volume": volume,
        "title": plan.get("title", ""),
        "draft_text": draft_text,
        "review": review.model_dump() if review else {},
        "attempt": attempt,
        "verdict": verdict,
        "retry_exceeded": retry_exceeded,
        # 实际生成用的接入点/模型与降级标记（随记录传递到人审展示）
        "model": f"{result.provider_name}/{result.model}",
        "used_fallback": result.used_fallback,
        # 字数统计
        "words": actual_words,
        "length_deviation": dev,
        # 上下文随记录传递：人审通过后 finalize（摘要/回写）需要伏笔清单等
        "ctx": asdict(ctx),
    }


def draft_volume(pipe, state: NovelState, volume: int,
                 store_lock: threading.Lock | None = None,
                 skip_chapters: set[int] | None = None) -> list[dict]:
    """顺序起草某卷全部章节（卷内串行以保证连贯性），返回草稿记录列表。

    skip_chapters：已人审通过的章节号集合（断点续跑时跳过，不重复起草）。
    """
    outline = state["outline"]
    total = state.get("total_chapters", 0)
    records: list[dict] = []
    for plan in chapters_of_volume(outline, volume):
        if skip_chapters and plan["chapter"] in skip_chapters:
            continue
        ctx = pipe.memory.retrieve_context(
            chapter=plan["chapter"],
            chapter_outline=f"《{plan['title']}》{plan['outline']}",
            characters=plan.get("characters", []),
            total_chapters=total,
            is_finale=bool(total) and plan["chapter"] >= total,
        )
        records.append(draft_chapter(pipe, plan, ctx, store_lock=store_lock))
    return records


def parallel_draft_wave(pipe, state: NovelState, volume_ids: list[int],
                        max_workers: int = 0,
                        skip_chapters: set[int] | None = None) -> dict[int, list[dict]]:
    """并行起草同一波次内的多个独立卷。返回 {卷号: [草稿记录...]}。

    卷内串行、卷间并行；落盘用共享锁串行化以规避 Git 并发。
    max_workers<=0 时取 min(卷数, 4)。skip_chapters 透传给 draft_volume（断点续跑）。
    """
    if not volume_ids:
        return {}
    workers = max_workers if max_workers > 0 else min(len(volume_ids), 4)
    store_lock = threading.Lock()
    results: dict[int, list[dict]] = {}
    if workers == 1 or len(volume_ids) == 1:
        for vid in volume_ids:
            results[vid] = draft_volume(pipe, state, vid, store_lock=store_lock,
                                        skip_chapters=skip_chapters)
        return results
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(draft_volume, pipe, state, vid, store_lock, skip_chapters): vid
            for vid in volume_ids
        }
        for fut in as_completed(futures):
            vid = futures[fut]
            results[vid] = fut.result()
    logger.info("波次并行起草完成：卷 %s", volume_ids)
    return results


# ---------- 状态合并 ----------

def merge_draft_records(wave_results: dict[int, list[dict]]) -> list[dict]:
    """把并行波次的各卷草稿记录合并为按全局章节号升序的有序队列（供逐章人审消费）。"""
    merged: list[dict] = []
    for records in wave_results.values():
        merged.extend(records)
    merged.sort(key=lambda r: r["chapter"])
    return merged


def schedule_all(outline: dict) -> list[dict]:
    """给出完整调度计划：按波次列出可并行卷与其章节数（供 CLI/日志展示，不执行生成）。"""
    plan: list[dict] = []
    for i, wave in enumerate(compute_waves(outline), start=1):
        plan.append({
            "wave": i,
            "volumes": wave,
            "chapters": {vid: len(chapters_of_volume(outline, vid)) for vid in wave},
        })
    return plan
