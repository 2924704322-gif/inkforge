"""增量合并器：将 LLM 提取的逐块增量合并入累积的 16 维 FullReport。

合并规则：
- 标量字段：新值覆盖空值；非空时追加备注（如 consistency_notes 追加行）。
- 列表字段：追加新条目（不去重——LLM 被要求不重复提取已有内容）。
- 嵌套对象字段：递归合并。
"""

from __future__ import annotations

from typing import Any

from src.distillation.schemas import (
    ChunkExtraction,
    FullReport,
)
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _merge_lists(base: list, incoming: Any) -> list:
    """追加合并，不去重。incoming 可能是 list/str/其他。"""
    if not incoming:
        return base
    if isinstance(incoming, list):
        return base + incoming
    if isinstance(incoming, str) and incoming.strip():
        return base + [incoming]
    # 其他非空值：转为字符串追加
    if incoming:
        return base + [str(incoming)]
    return base


def _merge_str(base: Any, incoming: Any, separator: str = "\n") -> str:
    """字符串合并：base 为空则用 incoming；否则追加。"""
    base_s = str(base) if base and not isinstance(base, str) else (base or "")
    inc_s = str(incoming) if incoming and not isinstance(incoming, str) else (incoming or "")
    if not inc_s:
        return base_s
    if not base_s:
        return inc_s
    return base_s + separator + inc_s


def merge_increment(report: FullReport, extraction: ChunkExtraction) -> FullReport:
    """将单块提取结果合并入累积报告（原地修改，同时返回）。"""
    inc = extraction.increments

    # 1. world
    if inc.world:
        report.world.rules = _merge_lists(report.world.rules, inc.world.rules)
        report.world.power_graph = _merge_lists(report.world.power_graph, inc.world.power_graph)
        report.world.history_layers = _merge_lists(report.world.history_layers, inc.world.history_layers)
        report.world.consistency_notes = _merge_str(report.world.consistency_notes, inc.world.consistency_notes)

    # 2. plot
    if inc.plot:
        report.plot.main_thread = _merge_lists(report.plot.main_thread, inc.plot.main_thread)
        report.plot.subplots = _merge_lists(report.plot.subplots, inc.plot.subplots)
        report.plot.suspense_techniques = _merge_lists(report.plot.suspense_techniques, inc.plot.suspense_techniques)
        report.plot.turning_points = _merge_lists(report.plot.turning_points, inc.plot.turning_points)
        report.plot.foreshadowing = _merge_lists(report.plot.foreshadowing, inc.plot.foreshadowing)
        report.plot.closed_loops = _merge_str(report.plot.closed_loops, inc.plot.closed_loops)

    # 3. theme
    if inc.theme:
        report.theme.core_proposition = inc.theme.core_proposition or report.theme.core_proposition
        report.theme.variations = _merge_lists(report.theme.variations, inc.theme.variations)
        report.theme.image_system = _merge_lists(report.theme.image_system, inc.theme.image_system)
        report.theme.value_conflicts = _merge_lists(report.theme.value_conflicts, inc.theme.value_conflicts)
        report.theme.author_stance = inc.theme.author_stance or report.theme.author_stance

    # 4. narrative
    if inc.narrative:
        report.narrative.levels = _merge_lists(report.narrative.levels, inc.narrative.levels)
        report.narrative.narrator_type = inc.narrative.narrator_type or report.narrative.narrator_type
        report.narrative.focalization = _merge_lists(report.narrative.focalization, inc.narrative.focalization)
        report.narrative.time_manipulation = _merge_lists(report.narrative.time_manipulation, inc.narrative.time_manipulation)

    # 5. characters
    if inc.characters:
        report.characters.characters = _merge_lists(report.characters.characters, inc.characters.characters)
        report.characters.relations = _merge_lists(report.characters.relations, inc.characters.relations)
        report.characters.group_dynamics = _merge_lists(report.characters.group_dynamics, inc.characters.group_dynamics)

    # 6. environment
    if inc.environment:
        report.environment.topology = _merge_lists(report.environment.topology, inc.environment.topology)
        report.environment.artifacts = _merge_lists(report.environment.artifacts, inc.environment.artifacts)
        report.environment.environmental_shifts = _merge_lists(report.environment.environmental_shifts, inc.environment.environmental_shifts)

    # 7. env_description
    if inc.env_description:
        report.env_description.techniques = _merge_lists(report.env_description.techniques, inc.env_description.techniques)
        report.env_description.rhetorical_patterns = _merge_lists(report.env_description.rhetorical_patterns, inc.env_description.rhetorical_patterns)
        report.env_description.dynamic_layers = _merge_lists(report.env_description.dynamic_layers, inc.env_description.dynamic_layers)

    # 8. char_description
    if inc.char_description:
        report.char_description.techniques = _merge_lists(report.char_description.techniques, inc.char_description.techniques)
        report.char_description.psychological_distance = _merge_lists(report.char_description.psychological_distance, inc.char_description.psychological_distance)
        report.char_description.identity_markers = _merge_lists(report.char_description.identity_markers, inc.char_description.identity_markers)

    # 9. dialogue
    if inc.dialogue:
        report.dialogue.character_fingerprints = _merge_lists(report.dialogue.character_fingerprints, inc.dialogue.character_fingerprints)
        report.dialogue.pragmatic_functions = _merge_lists(report.dialogue.pragmatic_functions, inc.dialogue.pragmatic_functions)
        report.dialogue.dialogue_pacing = _merge_lists(report.dialogue.dialogue_pacing, inc.dialogue.dialogue_pacing)
        report.dialogue.silence_and_interruption = _merge_lists(report.dialogue.silence_and_interruption, inc.dialogue.silence_and_interruption)

    # 10. action
    if inc.action:
        report.action.scenes = _merge_lists(report.action.scenes, inc.action.scenes)
        report.action.multitask_paragraphs = _merge_lists(report.action.multitask_paragraphs, inc.action.multitask_paragraphs)

    # 11. style
    if inc.style:
        report.style.register_spectrum = inc.style.register_spectrum or report.style.register_spectrum
        report.style.tone_stability = inc.style.tone_stability or report.style.tone_stability
        report.style.rhetorical_density = inc.style.rhetorical_density or report.style.rhetorical_density
        report.style.lexicon_fields = _merge_lists(report.style.lexicon_fields, inc.style.lexicon_fields)
        report.style.sentence_patterns = _merge_lists(report.style.sentence_patterns, inc.style.sentence_patterns)

    # 12. rhythm
    if inc.rhythm:
        report.rhythm.syntactic_rhythm = _merge_lists(report.rhythm.syntactic_rhythm, inc.rhythm.syntactic_rhythm)
        report.rhythm.chapter_beats = _merge_lists(report.rhythm.chapter_beats, inc.rhythm.chapter_beats)
        report.rhythm.tension_curve = _merge_str(report.rhythm.tension_curve, inc.rhythm.tension_curve)

    # 13. sensory
    if inc.sensory:
        report.sensory.dominant_emotion = inc.sensory.dominant_emotion or report.sensory.dominant_emotion
        report.sensory.emotion_curve = _merge_lists(report.sensory.emotion_curve, inc.sensory.emotion_curve)
        report.sensory.sensory_bindings = _merge_lists(report.sensory.sensory_bindings, inc.sensory.sensory_bindings)

    # 14. time_memory
    if inc.time_memory:
        report.time_memory.physical_vs_narrative_time = inc.time_memory.physical_vs_narrative_time or report.time_memory.physical_vs_narrative_time
        report.time_memory.memory_presence = _merge_lists(report.time_memory.memory_presence, inc.time_memory.memory_presence)
        report.time_memory.history_and_oblivion = _merge_lists(report.time_memory.history_and_oblivion, inc.time_memory.history_and_oblivion)

    # 15. meta_narrative
    if inc.meta_narrative:
        report.meta_narrative.self_reference = _merge_lists(report.meta_narrative.self_reference, inc.meta_narrative.self_reference)
        report.meta_narrative.intertextuality = _merge_lists(report.meta_narrative.intertextuality, inc.meta_narrative.intertextuality)
        report.meta_narrative.genre_awareness = inc.meta_narrative.genre_awareness or report.meta_narrative.genre_awareness

    # 16. ideology
    if inc.ideology:
        report.ideology.explicit_claims = _merge_lists(report.ideology.explicit_claims, inc.ideology.explicit_claims)
        report.ideology.implicit_presumptions = _merge_lists(report.ideology.implicit_presumptions, inc.ideology.implicit_presumptions)
        report.ideology.value_conflicts = _merge_lists(report.ideology.value_conflicts, inc.ideology.value_conflicts)
        report.ideology.problem_consciousness = inc.ideology.problem_consciousness or report.ideology.problem_consciousness

    # 跨维度参考
    report.cross_references = _merge_lists(report.cross_references, extraction.cross_references)
    report.pending_questions = _merge_lists(report.pending_questions, extraction.pending_questions)

    return report


def build_cumulative_summary(report: FullReport, max_chars: int = 3000) -> str:
    """从当前累积报告生成精简摘要文本，供 LLM 下一次调用的上下文窗口。

    仅输出已有内容的关键字段摘要，不超过 max_chars 字符。
    """
    parts: list[str] = []

    def _add(label: str, items: list, key: str = "name") -> None:
        if not items:
            return
        names = []
        for item in items:
            if isinstance(item, dict):
                n = item.get(key, str(item))
            elif hasattr(item, key):
                n = getattr(item, key)
            else:
                n = str(item)
            if n:
                names.append(str(n)[:40])
        if names:
            parts.append(f"{label}({len(items)}): {', '.join(names[:15])}")

    # 只输出最关键的维度摘要
    if report.world.rules:
        _add("世界观规则", [r.model_dump() if hasattr(r, 'model_dump') else r for r in report.world.rules])
    if report.characters.characters:
        _add("角色", report.characters.characters)
    if report.plot.main_thread:
        parts.append(f"主线事件: {len(report.plot.main_thread)}条")
    if report.plot.foreshadowing:
        entries = []
        for f in report.plot.foreshadowing:
            if hasattr(f, 'desc'):
                entries.append(f.desc[:50])
            elif isinstance(f, dict):
                entries.append(f.get('desc', '')[:50])
        parts.append(f"伏笔({len(report.plot.foreshadowing)}): {'; '.join(entries[:10])}")
    if report.theme.core_proposition:
        parts.append(f"核心命题: {report.theme.core_proposition[:120]}")
    if report.style.register_spectrum:
        parts.append(f"语体: {report.style.register_spectrum[:80]}")

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text
