"""Agent 结构化输出 Schema（pydantic）。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ---------- Architect ----------

class WorldviewDoc(BaseModel):
    filename: str = Field(description="文件名（英文小写连字符，如 power-system）")
    title: str = Field(description="文档标题")
    content: str = Field(description="Markdown 正文，具体、可判定")


class WorldviewOutput(BaseModel):
    docs: list[WorldviewDoc] = Field(description="2-4 份世界观文档")


class CharacterProfile(BaseModel):
    name: str
    role: str = Field(description="主角/配角/反派")
    appearance: str = Field(description="外貌特征")
    personality: str = Field(description="性格、欲望与缺陷")
    background: str = Field(description="背景故事")
    level: str = Field(default="", description="初始修为/能力等级")
    location: str = Field(default="", description="初始位置")
    items: list[str] = Field(default_factory=list, description="初始持有物品")
    relations: dict[str, str] = Field(
        default_factory=dict, description="与其他角色的关系，键为角色名"
    )


class CharactersOutput(BaseModel):
    characters: list[CharacterProfile]


class ChapterPlan(BaseModel):
    chapter: int = Field(description="全书统一连续章节号，从 1 开始")
    title: str
    outline: str = Field(description="本章大纲 80-150 字：核心事件/冲突/结尾钩子")
    characters: list[str] = Field(description="本章出场角色名")


class VolumePlan(BaseModel):
    volume: int = Field(description="卷号，从 1 开始")
    title: str
    depends_on: list[int] = Field(
        default_factory=list, description="依赖的前置卷号（卷级并行标记，M2 使用）"
    )
    chapters: list[ChapterPlan]


class ForeshadowItem(BaseModel):
    id: str = Field(description="伏笔编号，如 f001")
    desc: str = Field(description="伏笔内容")
    planted_ch: int = Field(description="埋设章")
    resolve_ch: int = Field(description="预期回收章")


class OutlineOutput(BaseModel):
    book_title: str
    theme: str = Field(description="全书主题一句话")
    volumes: list[VolumePlan]
    foreshadowing: list[ForeshadowItem] = Field(description="5-10 条贯穿性伏笔")


class DemoOutput(BaseModel):
    """创作向导设定 Demo（人工审核通过后才写入资料库 settings/）。"""

    book_title: str = Field(description="书名")
    synopsis: str = Field(description="故事梗概 300-500 字")
    overview: str = Field(description="整体概要 400-600 字：开端/发展/高潮/结局")
    theme: str = Field(description="核心主题一句话")
    worldview: list[WorldviewDoc] = Field(description="2-4 份世界观文档")
    characters: list[CharacterProfile] = Field(
        description="贯穿全文的主要人物（不含临时炮灰角色）"
    )


# ---------- Plotter（互动创作模式：每章剧情卡三选一 + 用户自定义卡） ----------

class PlotCard(BaseModel):
    """单张剧情卡：本章可选的剧情走向（等价一份 ChapterPlan.outline）。"""

    card_id: str = Field(description="卡片编号：c1/c2/c3")
    title: str = Field(description="卡片标题（≤15 字，本章章名候选）")
    tag: str = Field(description="类型标签：主线推进/冲突爆发/伏笔支线 等")
    outline: str = Field(description="本章剧情走向 100-200 字：核心事件/冲突/结尾钩子")
    hook: str = Field(description="结尾钩子一句话")
    characters: list[str] = Field(description="本章出场角色名")


class PlotCardsOutput(BaseModel):
    cards: list[PlotCard] = Field(description="恰好 3 张方向互斥的剧情卡")


# ---------- Editor ----------

class ReviewIssue(BaseModel):
    dimension: Literal["consistency", "plot", "continuity", "prose", "length"]
    severity: Literal["major", "minor"]
    description: str = Field(description="问题描述")
    quote: str = Field(default="", description="原文片段引用（≤40 字）")
    suggestion: str = Field(description="修改建议")


class ReviewOutput(BaseModel):
    consistency: float = Field(ge=0, le=10, description="设定一致性得分")
    plot: float = Field(ge=0, le=10, description="大纲符合度得分")
    continuity: float = Field(ge=0, le=10, description="衔接连贯性得分")
    prose: float = Field(ge=0, le=10, description="文笔质量得分")
    length: float = Field(default=10.0, ge=0, le=10, description="字数符合度得分（独立门禁维度，不计入总分）")
    issues: list[ReviewIssue] = Field(default_factory=list)
    comment: str = Field(default="", description="总评一句话")

    @property
    def overall(self) -> float:
        return round((self.consistency + self.plot + self.continuity + self.prose) / 4, 2)


# ---------- 对话协商（v2.0 P4-A：partial_rewrite 时 Writer↔Editor 磋稿协商） ----------

class IssueResponse(BaseModel):
    index: int = Field(description="对应问题清单序号，从 1 开始")
    stance: Literal["accept", "dispute"] = Field(description="accept 采纳 / dispute 申辩")
    reply: str = Field(description="accept 时为修改思路；dispute 时为有依据的申辩理由")


class NegotiationOutput(BaseModel):
    responses: list[IssueResponse] = Field(description="逐条回应，与问题清单一一对应")


class ArbitrationItem(BaseModel):
    index: int = Field(description="对应问题清单序号，从 1 开始")
    final: Literal["revise", "waive"] = Field(description="revise 坚持修改 / waive 接受申辩豁免")
    directive: str = Field(default="", description="revise 时的最终修改指令；waive 时留空")


class ArbitrationOutput(BaseModel):
    items: list[ArbitrationItem] = Field(description="逐条仲裁，与问题清单一一对应")


# ---------- Editor 跨卷一致性合并审查（R2，卷级并行后执行） ----------

class CrossVolumeIssue(BaseModel):
    volumes: list[int] = Field(description="涉及冲突的卷号列表")
    severity: Literal["major", "minor"]
    description: str = Field(description="跨卷冲突描述（设定/角色状态/时间线）")
    suggestion: str = Field(description="修改建议")


class CrossVolumeReviewOutput(BaseModel):
    consistent: bool = Field(description="并行卷之间是否不存在 major 级设定冲突")
    issues: list[CrossVolumeIssue] = Field(default_factory=list)
    comment: str = Field(default="", description="总评一句话")


# ---------- Summarizer ----------

class CharacterUpdate(BaseModel):
    name: str
    updates: dict[str, str] = Field(
        description="状态变化字段，如 {level: ..., location: ..., items: ..., relations: ...}"
    )


class ForeshadowOp(BaseModel):
    action: Literal["plant", "resolve"]
    id: Optional[str] = Field(default=None, description="resolve 时必填已知伏笔 id")
    desc: str = Field(default="", description="plant 时必填伏笔内容")
    resolve_ch: Optional[int] = Field(default=None, description="plant 时预期回收章")


class StateOp(BaseModel):
    """实体状态板变更：影响后续剧情的硬事实（被捕/死亡/身份揭露/物品易主等）。"""

    entity: str = Field(description="实体名（角色/物品/势力，或 时间线 表示全局事件锚点）")
    fact: str = Field(description="一句话陈述的硬事实，如：被李正逮捕，关押于第九处")


class SummaryOutput(BaseModel):
    summary: str = Field(description="300-500 字结构化摘要")
    characters_present: list[str] = Field(description="出场角色清单")
    character_updates: list[CharacterUpdate] = Field(default_factory=list)
    foreshadow_ops: list[ForeshadowOp] = Field(default_factory=list)
    state_ops: list[StateOp] = Field(
        default_factory=list,
        description="实体状态板变更；无硬事实变更时为空",
    )
