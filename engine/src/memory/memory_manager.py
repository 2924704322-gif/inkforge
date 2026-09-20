"""Memory Manager：三层记忆协调 + RAG 四维检索 + 记忆回写 + 索引重建。"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.memory.hybrid import HybridRetriever
from src.memory.md_store import MdStore
from src.memory.retriever import VectorIndex
from src.utils.logger import get_logger

logger = get_logger(__name__)

# 摘要分级（M1）：近期章节用详细摘要，远期用压缩摘要
RECENT_WINDOW = 5  # 最近 N 章视为"近期"
RECENT_SUMMARY_CHARS = 500
FAR_SUMMARY_CHARS = 100

# 伏笔主动召回（M2 / T2.4）：距预期回收章 ≤N 章时视为"临期"，注入紧急提醒
FORESHADOW_DUE_WINDOW = 2

# 实体状态板（v2.0 P1）：影响后续剧情的硬事实表，起草前全量直读注入 Writer
STATE_BOARD_REL = "settings/state-board.md"

# 风格一致性引擎（v2.0 P4-D）：文风指纹 MD，仿状态板全量直读注入 Writer/Editor
STYLE_REL = "settings/style.md"

# 自定义创作 Skill（用户约束）：向导选定的 Skill 合并落盘，仿 style.md 直读注入
CUSTOM_SKILLS_REL = "settings/custom-skills.md"

# 作者创作需求（brief）：**唯一事实源**。此前 brief 只在架构阶段被消费一次
# （世界观/人物/大纲/文风都是从它蒸馏出来的产物），**正文写作链路完全看不到原始需求**——
# 一旦蒸馏有偏差或后续章节偏离，没有任何"作者原话"能把它拉回来。
# 这就是"用户指令约束力不足"的结构性根因，故把 brief 落盘为书内事实源并逐章直读注入。
BRIEF_REL = "settings/brief.md"


def read_brief(store: MdStore) -> str:
    """直读作者创作需求 settings/brief.md 正文（与 read_custom_constraints 同源同逻辑）。

    文件不存在返回空串（旧书向后兼容：调用方渲染成"（未提供）"，不报错、不阻塞）。
    frontmatter 里的结构化字段（brief_fields）已在落盘时渲染进正文，此处只取正文。
    """
    if not store.exists(BRIEF_REL):
        return ""
    return store.read(BRIEF_REL).content.strip()


def write_brief(store: MdStore, brief: str, fields: dict | None = None) -> bool:
    """把作者需求写入书内事实源；已有内容且未变化时不重复写（幂等，避免噪声提交）。

    `fields`：结构化 brief 字段（X1）。落盘时**逐项原样**渲染进正文，
    使"写入"与"读取"路径都保留作者原文，不经任何摘要。

    `allow_realism`（现实性口径开关）有两处落点，缺一不可：
    - frontmatter `brief_fields.allow_realism` → 程序用 `allow_realism(store)` 读回；
    - 正文里一行可读标记 → 人看得见"这本书的评分口径是什么"，也兼容字段机制之前的旧书。
    """
    from src.agents.architect import render_brief_fields
    from src.agents.reality_policy import BRIEF_FIELD_KEY, normalize_allow_realism

    text = (brief or "").strip()
    structured = render_brief_fields(fields) if fields else ""
    want_realism = normalize_allow_realism((fields or {}).get(BRIEF_FIELD_KEY))
    marker = (
        f"- {BRIEF_FIELD_KEY}：true（已开启「要求现实合理性约束」）"
        if want_realism else ""
    )
    body = "\n\n".join(p for p in (structured, marker, text) if p).strip()
    if not body:
        return False
    metadata = {"title": "作者创作需求（brief）", "brief_fields": fields or {}}
    if store.exists(BRIEF_REL):
        doc = store.read(BRIEF_REL)
        # 幂等判据必须**同时**比对正文与结构化字段：只比正文的话，
        # 单独改开关（正文不变）会被判成"没变化"而静默丢弃（真实易踩的坑）。
        if (doc.content.strip() == body
                and (doc.metadata.get("brief_fields") or {}) == (fields or {})):
            return False
    store.write(
        BRIEF_REL,
        body,
        metadata=metadata,
        commit_message="记录作者创作需求（brief）",
    )
    return True


def read_brief_fields(store: MdStore) -> dict:
    """直读 brief 的结构化字段（frontmatter `brief_fields`）。

    为什么需要单独一个入口：正文渲染时结构化字段已经被渲染进 brief 正文（人读友好），
    但**布尔开关类字段**（如 `allow_realism`）必须能被程序读回，不能靠解析自然语言。
    文件缺失/字段缺失/脏值一律返回空 dict（旧书向后兼容）。
    """
    if not store.exists(BRIEF_REL):
        return {}
    try:
        fields = store.read(BRIEF_REL).metadata.get("brief_fields")
    except Exception as exc:  # noqa: BLE001 - 元数据损坏不得阻断生成
        logger.warning("读取 brief 结构化字段失败（按未设置处理）：%s", exc)
        return {}
    return fields if isinstance(fields, dict) else {}


def allow_realism(store: MdStore) -> bool:
    """作者是否要求「现实合理性约束」（默认 **否** = 以作者创作目标为准）。

    真实来源优先级：brief 结构化字段 → brief 正文里的显式行 → 默认 False。
    正文兜底是为了兼容"字段机制之前就写好的旧书"（用户手写一行也能生效）。
    """
    from src.agents.reality_policy import BRIEF_FIELD_KEY, normalize_allow_realism

    fields = read_brief_fields(store)
    if BRIEF_FIELD_KEY in fields:
        return normalize_allow_realism(fields.get(BRIEF_FIELD_KEY))
    if not store.exists(BRIEF_REL):
        return False
    for line in store.read(BRIEF_REL).content.splitlines():
        text = line.strip().lstrip("-*").strip()
        if text.startswith(f"{BRIEF_FIELD_KEY}：") or text.startswith(f"{BRIEF_FIELD_KEY}:"):
            _, _, value = text.replace("：", ":").partition(":")
            return normalize_allow_realism(value)
    return False


def read_custom_constraints(store: MdStore) -> str:
    """直读项目级约束 settings/custom-skills.md 正文（W5 传参唯一读取口）。

    与 retrieve_context 第 ⑦ 步同源同逻辑：文件不存在返回空串。Architect /
    Summarizer 的 custom_constraints 传参与 Writer/Editor 注入都走这一个实现，
    不建立第二套读取逻辑。
    """
    if not store.exists(CUSTOM_SKILLS_REL):
        return ""
    return store.read(CUSTOM_SKILLS_REL).content.strip()


def kind_for_path(rel_path: str) -> str:
    """由相对路径推断文档类型。"""
    if rel_path.startswith("settings/worldview"):
        return "worldview"
    if rel_path.startswith("settings/characters"):
        return "character"
    if rel_path.startswith("settings/outline"):
        return "outline"
    if rel_path.startswith("settings/foreshadowing"):
        return "foreshadowing"
    if rel_path.startswith("chapters/"):
        return "chapter"
    if rel_path.startswith("summaries/"):
        return "summary"
    if rel_path.startswith("reviews/"):
        return "review"
    return "other"

# 进入向量索引的类型（正文与审查报告不入库：正文过长且摘要已代表其内容）
INDEXED_KINDS = {"worldview", "character", "summary", "foreshadowing"}


@dataclass
class ChapterContext:
    """RAG 四维检索结果，供 Prompt 组装。"""

    chapter: int
    outline: str = ""                       # 本章大纲
    recent_summaries: list[str] = field(default_factory=list)   # 短期记忆（分级）
    related_summaries: list[str] = field(default_factory=list)  # ①远期摘要（混合召回）
    character_states: dict[str, str] = field(default_factory=dict)  # ②出场角色状态
    unresolved_foreshadowing: list[dict] = field(default_factory=list)  # ③未回收伏笔
    due_foreshadowing: list[dict] = field(default_factory=list)  # ③b 临期/超期伏笔（主动召回）
    worldview_rules: list[str] = field(default_factory=list)    # ④世界观规则
    state_board: list[dict] = field(default_factory=list)       # ⑤实体状态板（硬事实，直读）
    style_guide: str = ""                                        # ⑥文风指纹（style.md 直读）
    custom_constraints: str = ""                                 # ⑦自定义 Skill 约束（custom-skills.md 直读）
    brief: str = ""                                              # ⑧作者创作需求（brief.md 直读，最高优先级）
    # ⑨现实性评判口径（默认"以作者目标为准"，不得因"不符合现实"改稿）——
    # 与 ⑦⑧ 同源（brief.md 的字段），由 reality_policy 单一定义文本。
    reality_policy: str = ""


class MemoryManager:
    """协调 MdStore（事实源）与 VectorIndex（派生索引）。"""

    def __init__(self, store: MdStore, index: VectorIndex):
        self.store = store
        self.index = index
        self.hybrid = HybridRetriever(index)

    # ---------- 索引维护 ----------

    def index_document(self, rel_path: str) -> int:
        kind = kind_for_path(rel_path)
        if kind not in INDEXED_KINDS:
            return 0
        doc = self.store.read(rel_path)
        n = self.index.upsert_document(doc, kind)
        self.hybrid.invalidate()
        logger.debug("已嵌入 %s（%d 块，kind=%s）", rel_path, n, kind)
        return n

    def rebuild_index(self) -> int:
        """全量重建向量索引（MD 为唯一事实源，索引可随时重建）。"""
        self.index.clear()
        total = 0
        synced: list[str] = []
        for doc in self.store.iter_documents():
            rel = doc.doc_id
            kind = kind_for_path(rel)
            if kind in INDEXED_KINDS:
                total += self.index.upsert_document(doc, kind)
            synced.append(rel)
        self.store.mark_synced(synced)
        self.hybrid.invalidate()
        logger.info("索引重建完成：%d 个向量块", total)
        return total

    def sync_changed(self) -> list[str]:
        """检测人工编辑过的 MD 并重嵌入（hash 变更检测）。"""
        changed = self.store.detect_changed()
        for rel in changed:
            self.index_document(rel)
        if changed:
            self.store.mark_synced(changed)
            logger.info("已重嵌入 %d 个人工变更文件: %s", len(changed), changed)
        return changed

    # ---------- RAG 四维检索 ----------

    def retrieve_context(
        self,
        chapter: int,
        chapter_outline: str,
        characters: list[str],
        top_k: int = 4,
        total_chapters: int = 0,
        is_finale: bool = False,
    ) -> ChapterContext:
        """以本章大纲为 Query 的四维检索（计划书 §2.5）。"""
        ctx = ChapterContext(chapter=chapter, outline=chapter_outline)

        # 短期记忆：最近 RECENT_WINDOW 章摘要，分级截断
        for ch in range(max(1, chapter - RECENT_WINDOW), chapter):
            rel = self.store.summary_rel_path(ch)
            if not self.store.exists(rel):
                continue
            doc = self.store.read(rel)
            limit = (
                RECENT_SUMMARY_CHARS
                if chapter - ch <= 2
                else FAR_SUMMARY_CHARS
            )
            ctx.recent_summaries.append(
                f"【第{ch}章摘要】{doc.content.strip()[:limit]}"
            )

        # ① 远期章节摘要混合召回（向量+BM25 融合 + 时间衰减，排除近期窗口已直读的）
        for hit in self.hybrid.query(
            chapter_outline,
            top_k=top_k,
            kind="summary",
            max_chapter=chapter - 1,
            current_chapter=chapter,
        ):
            hit_ch = hit.metadata.get("chapter", 0)
            if hit_ch and chapter - hit_ch <= RECENT_WINDOW:
                continue
            ctx.related_summaries.append(f"【第{hit_ch}章摘要片段】{hit.text}")

        # ② 出场角色状态：直接读取角色档案（事实源优先，不走向量）
        from src.memory.md_store import slugify

        for name in characters:
            rel = f"settings/characters/{slugify(name)}.md"
            if self.store.exists(rel):
                doc = self.store.read(rel)
                state_fields = {
                    k: v
                    for k, v in doc.metadata.items()
                    if not k.startswith("_") and k not in ("title",)
                }
                ctx.character_states[name] = (
                    f"档案字段: {state_fields}\n{doc.content.strip()[:800]}"
                )
            else:
                ctx.character_states[name] = "（无档案，可能为新角色）"

        # ③ 未回收伏笔：读取伏笔表 frontmatter items；④b 临期/超期伏笔主动召回（T2.4）
        fs_rel = "settings/foreshadowing.md"
        if self.store.exists(fs_rel):
            doc = self.store.read(fs_rel)
            for item in doc.metadata.get("items", []) or []:
                if not isinstance(item, dict) or item.get("status") == "resolved":
                    continue
                # 仅暴露"已埋设"的伏笔：planted_ch > 当前章的属未来计划伏笔，
                # 尚不存在于正文，若列出会诱导 Summarizer 提前误判回收
                # （产生 resolved_ch < planted_ch 的脏数据），故过滤。
                planted_ch = item.get("planted_ch") or 0
                if planted_ch and planted_ch > chapter:
                    continue
                ctx.unresolved_foreshadowing.append(item)
                resolve_ch = item.get("resolve_ch")
                # 完结章：强制把所有仍未回收的已埋设伏笔纳入"务必回收"清单（完本收尾扫描）
                if is_finale or resolve_ch and chapter >= resolve_ch - FORESHADOW_DUE_WINDOW:
                    ctx.due_foreshadowing.append(item)

        # ④ 世界观规则混合召回
        for hit in self.hybrid.query(chapter_outline, top_k=top_k, kind="worldview"):
            ctx.worldview_rules.append(hit.text)

        # ⑤ 实体状态板：全量直读硬事实（事实源优先，不走向量）。
        # 只注入 chapter < 当前章的事实，防止并行波次内后卷先定稿时
        # 将"未来章节"的事实泄露给前面章节的重稿。
        if self.store.exists(STATE_BOARD_REL):
            doc = self.store.read(STATE_BOARD_REL)
            for item in doc.metadata.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                if (item.get("chapter") or 0) < chapter:
                    ctx.state_board.append(item)

        # ⑥ 文风指纹（P4-D）：全量直读 style.md 正文（MD 唯一事实源，人工可编辑）
        if self.store.exists(STYLE_REL):
            ctx.style_guide = self.store.read(STYLE_REL).content.strip()

        # ⑦ 自定义 Skill 约束：全量直读 custom-skills.md 正文（向导选定，人工可编辑）
        ctx.custom_constraints = read_custom_constraints(self.store)

        # ⑧ 作者创作需求（brief）：全量直读 brief.md 正文。
        # 与 ⑦ 同级并列（都是"作者指定"），同样**不经向量检索/摘要/裁剪**——
        # 这是问题1 的修法：让每一章生成都能看到作者原话，而不是只看蒸馏物。
        ctx.brief = read_brief(self.store)

        # ⑨ 现实性评判口径（用户要求）：以作者创作目标为准，默认不得因"不符合现实"改稿。
        # 与 ⑦⑧ 同源同读法（brief.md 的字段），逐章注入写作/审查链路。
        from src.agents.reality_policy import reality_policy_text

        ctx.reality_policy = reality_policy_text(allow_realism(self.store))

        return ctx

    # ---------- 记忆回写 ----------

    def writeback_chapter(
        self,
        chapter: int,
        volume: int,
        summary_content: str,
        character_updates: dict[str, dict],
        foreshadow_ops: list[dict],
        total_chapters: int = 0,
        state_ops: list[dict] | None = None,
    ) -> None:
        """人审通过后的记忆回写：摘要入库 + 角色状态更新 + 伏笔表更新 + 重嵌入。"""
        # 1) 摘要 MD 落盘并嵌入
        sum_rel = self.store.summary_rel_path(chapter)
        self.store.write(
            sum_rel,
            summary_content,
            metadata={"chapter": chapter, "volume": volume},
            commit_message=f"ch-{chapter:03d} 摘要回写",
        )
        self.index_document(sum_rel)

        # 2) 角色状态 frontmatter 更新并重嵌入
        from src.memory.md_store import slugify

        for name, updates in (character_updates or {}).items():
            rel = f"settings/characters/{slugify(name)}.md"
            if self.store.exists(rel):
                self.store.update_metadata(
                    rel, updates, commit_message=f"ch-{chapter:03d} 角色状态更新: {name}"
                )
            else:
                self.store.write(
                    rel,
                    f"# {name}\n\n（第 {chapter} 章首次出场，档案待补全）",
                    metadata={"title": name, **updates},
                    commit_message=f"ch-{chapter:03d} 新角色建档: {name}",
                )
            self.index_document(rel)

        # 3) 伏笔表更新（埋设 planted / 回收 resolved）
        if foreshadow_ops:
            self._apply_foreshadow_ops(chapter, foreshadow_ops, total_chapters)

        # 4) 实体状态板更新（硬事实追加）
        if state_ops:
            self.apply_state_ops(chapter, state_ops)

        self.store.mark_synced([sum_rel])

    def _apply_foreshadow_ops(
        self, chapter: int, ops: list[dict], total_chapters: int = 0
    ) -> None:
        fs_rel = "settings/foreshadowing.md"
        if self.store.exists(fs_rel):
            doc = self.store.read(fs_rel)
            meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
            content = doc.content
        else:
            meta, content = {}, "# 伏笔表\n\n结构化条目见 frontmatter items。"
        items: list[dict] = list(meta.get("items", []) or [])
        existing_descs = {
            (i.get("desc") or "").strip() for i in items if isinstance(i, dict)
        }
        # 末章窗口内新种伏笔已无回收空间，直接拒绝（避免稀释回收率分母）
        final_window = total_chapters and chapter >= total_chapters - 1

        for op in ops:
            action = op.get("action")
            if action == "plant":
                desc = (op.get("desc") or "").strip()
                # 去重：同一 desc 已存在则跳过（Summarizer 常把既有伏笔当新伏笔重种）
                if not desc or desc in existing_descs:
                    continue
                if final_window:
                    continue
                resolve_ch = op.get("resolve_ch")
                # 回收章缺失或超出总章数时钳制到合理范围（默认埋设后 3 章、上限总章数）
                if not isinstance(resolve_ch, int) or resolve_ch <= chapter:
                    resolve_ch = chapter + 3
                if total_chapters and resolve_ch > total_chapters:
                    resolve_ch = total_chapters
                items.append(
                    {
                        "id": op.get("id") or f"f{len(items) + 1:03d}",
                        "desc": desc,
                        "planted_ch": chapter,
                        "resolve_ch": resolve_ch,
                        "status": "open",
                    }
                )
                existing_descs.add(desc)
            elif action == "resolve":
                for item in items:
                    if item.get("id") != op.get("id"):
                        continue
                    # 合法性校验：只回收"已埋设且尚未回收"的伏笔，
                    # 拒绝 planted_ch > 当前章的非法回收（防 resolved_ch < planted_ch 脏数据）
                    if item.get("status") == "resolved":
                        break
                    if (item.get("planted_ch") or 0) > chapter:
                        break
                    item["status"] = "resolved"
                    item["resolved_ch"] = chapter
                    break
        meta["items"] = items
        self.store.write(
            fs_rel, content, meta, commit_message=f"ch-{chapter:03d} 伏笔表更新"
        )
        self.index_document(fs_rel)

    def apply_state_ops(self, chapter: int, ops: list[dict]) -> None:
        """实体状态板追加硬事实（同伏笔表模式：frontmatter items 整表覆写）。

        去重规则：同 entity + 同 fact 已存在则跳过（Summarizer 常重复抽取既有事实）。
        事实只追加不修改：状态演进（如被捕→越狱）以新条目记录，保留时间线。
        """
        if self.store.exists(STATE_BOARD_REL):
            doc = self.store.read(STATE_BOARD_REL)
            meta = {k: v for k, v in doc.metadata.items() if not k.startswith("_")}
            content = doc.content
        else:
            meta, content = {}, "# 实体状态板\n\n影响后续剧情的硬事实，结构化条目见 frontmatter items。"
        items: list[dict] = list(meta.get("items", []) or [])
        existing = {
            ((i.get("entity") or "").strip(), (i.get("fact") or "").strip())
            for i in items
            if isinstance(i, dict)
        }
        added = 0
        for op in ops:
            entity = (op.get("entity") or "").strip()
            fact = (op.get("fact") or "").strip()
            if not entity or not fact or (entity, fact) in existing:
                continue
            items.append({"entity": entity, "fact": fact, "chapter": chapter})
            existing.add((entity, fact))
            added += 1
        if not added:
            return
        meta["items"] = items
        self.store.write(
            STATE_BOARD_REL, content, meta,
            commit_message=f"ch-{chapter:03d} 状态板更新（+{added} 条）",
        )
        logger.info("状态板：第 %d 章新增 %d 条硬事实", chapter, added)

    def foreshadowing_stats(self) -> dict:
        """伏笔回收率统计（验收指标②）。"""
        fs_rel = "settings/foreshadowing.md"
        if not self.store.exists(fs_rel):
            return {"total": 0, "resolved": 0, "rate": None}
        items = self.store.read(fs_rel).metadata.get("items", []) or []
        total = len(items)
        resolved = sum(1 for i in items if isinstance(i, dict) and i.get("status") == "resolved")
        return {
            "total": total,
            "resolved": resolved,
            "rate": (resolved / total) if total else None,
        }
