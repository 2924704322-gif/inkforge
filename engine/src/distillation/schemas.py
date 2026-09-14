"""蒸馏 16 维结构化 Schema — 超级宽松版。

所有字段均接受任意 JSON 值（不校验具体类型），确保 LLM 输出永不被 Schema 拒绝。
结构化分析由提示词引导，pydantic 仅提供外层容器框架。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class WorldBuilding(BaseModel):
    """1. 世界构建。"""
    rules: Any = Field(default_factory=list)
    power_graph: Any = Field(default_factory=list)
    history_layers: Any = Field(default_factory=list)
    consistency_notes: Any = Field(default="")


class PlotStructure(BaseModel):
    """2. 情节结构。"""
    main_thread: Any = Field(default_factory=list)
    subplots: Any = Field(default_factory=list)
    suspense_techniques: Any = Field(default_factory=list)
    turning_points: Any = Field(default_factory=list)
    foreshadowing: Any = Field(default_factory=list)
    closed_loops: Any = Field(default="")


class ThemeAnalysis(BaseModel):
    """3. 主题星系。"""
    core_proposition: Any = Field(default="")
    variations: Any = Field(default_factory=list)
    image_system: Any = Field(default_factory=list)
    value_conflicts: Any = Field(default_factory=list)
    author_stance: Any = Field(default="")


class NarrativeLayer(BaseModel):
    """4. 叙事架构。"""
    levels: Any = Field(default_factory=list)
    narrator_type: Any = Field(default="")
    focalization: Any = Field(default_factory=list)
    time_manipulation: Any = Field(default_factory=list)


class CharacterSystem(BaseModel):
    """5. 人物系统。"""
    characters: Any = Field(default_factory=list)
    relations: Any = Field(default_factory=list)
    group_dynamics: Any = Field(default_factory=list)


class EnvironmentSpace(BaseModel):
    """6. 环境与空间。"""
    topology: Any = Field(default_factory=list)
    artifacts: Any = Field(default_factory=list)
    environmental_shifts: Any = Field(default_factory=list)


class EnvironmentDescription(BaseModel):
    """7. 环境描写技法。"""
    techniques: Any = Field(default_factory=list)
    rhetorical_patterns: Any = Field(default_factory=list)
    dynamic_layers: Any = Field(default_factory=list)


class CharacterDescription(BaseModel):
    """8. 人物描写技法。"""
    techniques: Any = Field(default_factory=list)
    psychological_distance: Any = Field(default_factory=list)
    identity_markers: Any = Field(default_factory=list)


class DialogueArt(BaseModel):
    """9. 对话艺术。"""
    character_fingerprints: Any = Field(default_factory=list)
    pragmatic_functions: Any = Field(default_factory=list)
    dialogue_pacing: Any = Field(default_factory=list)
    silence_and_interruption: Any = Field(default_factory=list)


class ActionChoreography(BaseModel):
    """10. 动作与场景调度。"""
    scenes: Any = Field(default_factory=list)
    multitask_paragraphs: Any = Field(default_factory=list)


class StyleProfile(BaseModel):
    """11. 语体与语气。"""
    register_spectrum: Any = Field(default="")
    tone_stability: Any = Field(default="")
    rhetorical_density: Any = Field(default="")
    lexicon_fields: Any = Field(default_factory=list)
    sentence_patterns: Any = Field(default_factory=list)


class RhythmProfile(BaseModel):
    """12. 节奏与韵律。"""
    syntactic_rhythm: Any = Field(default_factory=list)
    chapter_beats: Any = Field(default_factory=list)
    tension_curve: Any = Field(default="")


class SensoryEmotionMap(BaseModel):
    """13. 感官与情感地图。"""
    dominant_emotion: Any = Field(default="")
    emotion_curve: Any = Field(default_factory=list)
    sensory_bindings: Any = Field(default_factory=list)


class TimeMemoryEncoding(BaseModel):
    """14. 时间与记忆编码。"""
    physical_vs_narrative_time: Any = Field(default="")
    memory_presence: Any = Field(default_factory=list)
    history_and_oblivion: Any = Field(default_factory=list)


class MetaNarrative(BaseModel):
    """15. 元叙事与互文性。"""
    self_reference: Any = Field(default_factory=list)
    intertextuality: Any = Field(default_factory=list)
    genre_awareness: Any = Field(default="")


class IdeologyMatrix(BaseModel):
    """16. 意识形态与价值观矩阵。"""
    explicit_claims: Any = Field(default_factory=list)
    implicit_presumptions: Any = Field(default_factory=list)
    value_conflicts: Any = Field(default_factory=list)
    problem_consciousness: Any = Field(default="")


# ═══════════════════════════════════════════
# 容器
# ═══════════════════════════════════════════

class DimensionIncrements(BaseModel):
    """单次增量提取。None = 该维度无增量。"""
    world: Optional[WorldBuilding] = Field(default=None)
    plot: Optional[PlotStructure] = Field(default=None)
    theme: Optional[ThemeAnalysis] = Field(default=None)
    narrative: Optional[NarrativeLayer] = Field(default=None)
    characters: Optional[CharacterSystem] = Field(default=None)
    environment: Optional[EnvironmentSpace] = Field(default=None)
    env_description: Optional[EnvironmentDescription] = Field(default=None)
    char_description: Optional[CharacterDescription] = Field(default=None)
    dialogue: Optional[DialogueArt] = Field(default=None)
    action: Optional[ActionChoreography] = Field(default=None)
    style: Optional[StyleProfile] = Field(default=None)
    rhythm: Optional[RhythmProfile] = Field(default=None)
    sensory: Optional[SensoryEmotionMap] = Field(default=None)
    time_memory: Optional[TimeMemoryEncoding] = Field(default=None)
    meta_narrative: Optional[MetaNarrative] = Field(default=None)
    ideology: Optional[IdeologyMatrix] = Field(default=None)


class ChunkExtraction(BaseModel):
    """单块蒸馏输出。"""
    chunk_index: int = Field(default=0)
    chapter_range: str = Field(default="")
    increments: DimensionIncrements = Field(default_factory=DimensionIncrements)
    cross_references: Any = Field(default_factory=list)
    pending_questions: Any = Field(default_factory=list)


class FullReport(BaseModel):
    """完整 16 维蒸馏报告。"""
    book_title: str = Field(default="")
    total_chunks: int = Field(default=0)
    merged_at: str = Field(default="")
    provider: str = Field(default="")
    model: str = Field(default="")

    world: WorldBuilding = Field(default_factory=WorldBuilding)
    plot: PlotStructure = Field(default_factory=PlotStructure)
    theme: ThemeAnalysis = Field(default_factory=ThemeAnalysis)
    narrative: NarrativeLayer = Field(default_factory=NarrativeLayer)
    characters: CharacterSystem = Field(default_factory=CharacterSystem)
    environment: EnvironmentSpace = Field(default_factory=EnvironmentSpace)
    env_description: EnvironmentDescription = Field(default_factory=EnvironmentDescription)
    char_description: CharacterDescription = Field(default_factory=CharacterDescription)
    dialogue: DialogueArt = Field(default_factory=DialogueArt)
    action: ActionChoreography = Field(default_factory=ActionChoreography)
    style: StyleProfile = Field(default_factory=StyleProfile)
    rhythm: RhythmProfile = Field(default_factory=RhythmProfile)
    sensory: SensoryEmotionMap = Field(default_factory=SensoryEmotionMap)
    time_memory: TimeMemoryEncoding = Field(default_factory=TimeMemoryEncoding)
    meta_narrative: MetaNarrative = Field(default_factory=MetaNarrative)
    ideology: IdeologyMatrix = Field(default_factory=IdeologyMatrix)

    cross_references: Any = Field(default_factory=list)
    pending_questions: Any = Field(default_factory=list)
