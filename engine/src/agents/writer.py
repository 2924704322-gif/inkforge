"""Writer：基于 RAG 上下文生成章节正文（自由文本，非结构化）。

正文这条链路此前**没有** brief 保真链（生成重试 + 拒绝措辞识别）。
`architect` 的 8 个调用点都有 `generate_faithful` 兜底，而正文/出卡/互动写章都没有：
一旦模型偶发拒答或产出空串，缺陷会直接落到用户看到的稿子上。
本模块把正文的三次模型调用（初稿 / 续写 / 压缩）统一包进保真链。
"""

from __future__ import annotations

from src.agents.architect import brief_fidelity_block, generate_faithful
from src.agents.prompt_loader import render_prompt
from src.agents.schemas import NegotiationOutput, ReviewOutput
from src.config.app_config import GenerationConfig
from src.llm.base import ChatMessage, ChatResult
from src.llm.registry import ModelRegistry
from src.llm.structured import chat_structured
from src.memory.memory_manager import ChapterContext
from src.utils.logger import get_logger

logger = get_logger(__name__)

ROLE = "writer"
DEFAULT_TARGET_WORDS = 5000
DEFAULT_TOLERANCE = 500
DEFAULT_MAX_CONTINUATION_ATTEMPTS = 3

#: 重写稿与原稿被判定为"几乎没改"时的重试次数（上限，防止无限烧调用）。
#: 判定用分块差异率，见 _looks_unchanged。
_UNCHANGED_REWRITE_ATTEMPTS = 1
#: 差异率阈值：低于它视为"换汤不换药"（重写没落实）。
#: 刻意取得保守（≈原文重复 90% 以上才判"没改"）：误判的代价是多烧一次重写调用，
#: 而**漏判的真正代价**是"用户以为改了、其实一个字没动"，所以宁可偶尔多烧一次。
_UNCHANGED_RATIO = 0.10

#: 单次扩写/压缩的收敛轮数。真实冒烟证据：一次压缩后仍 3986 字 > 上限 3500；
#: 一次扩写只补了 400 字。不收敛就要靠上层门禁再烧一整轮重写，代价高得多。
_LENGTH_ENFORCE_ROUNDS = 3

#: 字数口径的唯一来源（下浮/上浮上限、门禁区间）。Writer/Editor/门禁/前端都从
#: GenerationConfig 取，避免"模型按一套数写、门禁按另一套判"（真实踩过）。
_GEN = GenerationConfig()


def _faithful_text(registry: ModelRegistry, prompt: str) -> ChatResult:
    """带 brief 保真链的正文调用（空产出/拒绝措辞在系统内消化，重试上限见 architect）。"""
    return generate_faithful(
        lambda msgs, temp: registry.chat_as(ROLE, msgs, temperature=temp),
        [ChatMessage("user", prompt)],
    )


def chapter_length(text: str) -> int:
    """章节字数（中文按字符计）。全书统一口径。"""
    return len(text)


def length_deviation(actual: int, target: int) -> int:
    """字数偏差绝对值。"""
    return abs(actual - target)


def resolve_bounds(target: int, floor: int | None = None,
                   ceiling: int | None = None) -> tuple[int, int]:
    """把 (target, floor, ceiling) 归一成合法的可接受区间 [最低, 最高]。

    显式传入的 floor/ceiling 优先（编排层已按本章目标算好）；缺省时按 GenerationConfig
    的非对称口径现算（默认：下浮 ≤500，上浮 ≤2000）。floor 一律收敛到 [1, target]，
    避免调用方传进 0/负数导致"任何正文都超标"的死循环。
    """
    lo = int(floor) if floor else _GEN.length_floor(target)
    hi = int(ceiling) if ceiling else _GEN.length_ceiling(target)
    lo = max(1, min(lo, target))
    hi = max(hi, target)
    return lo, hi


def length_assessment(target: int, actual: int, floor: int | None = None,
                      ceiling: int | None = None) -> str:
    """字数是否在可接受区间内：'pass' / 'short' / 'long'。

    非对称判定（用户要求）：**下方是硬线**（"至少"），**上方放宽**（内容完整性优先）。
    门槛数字与实际门禁必须来自同一对边界，否则模型按一套写、门禁按另一套判。
    """
    lo, hi = resolve_bounds(target, floor, ceiling)
    if actual < lo:
        return "short"
    if actual > hi:
        return "long"
    return "pass"


def length_revision_note(target: int, actual: int, tolerance: int = DEFAULT_TOLERANCE,
                         ceiling: int | None = None) -> str:
    """字数门禁打回指令（客观约束，不协商）。

    非对称口径（用户要求）：**少了必须补**（下浮上限严格），**多了从宽**（上浮上限
    只拦真正灌水的稿）。`tolerance` = 最低可接受字数，`ceiling` = 最高可接受字数；
    二者与实际门禁共用同一对数，指令里的数字与判定永远一致。

    参数名保留 `tolerance` 是为了兼容既有调用点，语义已改为"最低可接受字数"。
    """
    if actual < target:
        # 差额一律按"到目标还差多少"正面表述：即使已经越过下限（区间内但未到目标），
        # 也不能让模型读到负数而误判为已达标。
        gap = max(target, int(tolerance)) - actual
        over = ceiling if ceiling is not None else _GEN.length_ceiling(target)
        return (
            f"【字数修正】当前正文 {actual} 字，少于目标 {target} 字，"
            f"距目标还差 {gap} 字（最低可接受字数 {tolerance} 字）。"
            f"这是硬性要求：必须补足到 {target} 字以上（可接受区间 {tolerance}-{over} 字）。"
            f"补足方式只能是**加戏**：把场景写得更实、把对话写得更完整、补足动作与感官细节、"
            f"让配角有反应、把既有冲突推进一层——严禁注水、重复、原地打转或复制前文。"
            f"保持结尾钩子不变，并从上一次停笔处自然接续。"
        )
    over = ceiling if ceiling is not None else _GEN.length_ceiling(target)
    return (
        f"【字数修正】当前正文 {actual} 字，超出目标 {target} 字约 {actual - target} 字"
        f"（最高可接受 {over} 字）。请删减冗余描写、重复表达与无关枝节，"
        f"压缩到 {over} 字以内、尽量贴近目标 {target} 字；"
        f"保留全部核心事件、关键对话与结尾钩子。"
    )

# 人工打回意见固定前缀（供编排层复用；下游断言依赖此文本）
HUMAN_REVISION_HEADER = "【人工审阅打回意见，必须逐条落实】"


def human_revision_notes(feedback: str, mode: str = "targeted") -> str:
    """把人工自然语言意见组装成 Writer 重写指令（P3）。

    mode="targeted"（定向修订，默认）：严格保留未提及内容，只改意见涉及之处；
    mode="rewrite"（整体重写）：允许对全章大幅调整以落实意见。
    """
    if mode == "rewrite":
        directive = (
            "\n\n【修订模式：整体重写】可对全章结构与措辞大幅调整以落实上述意见，"
            "但须保持本章大纲核心事件、结尾钩子与既有设定不变。"
        )
    else:  # targeted
        directive = (
            "\n\n【修订模式：定向修订】只修改上述意见明确涉及之处；"
            "未被提及的段落必须逐字保留，不得改写、删减或调整其措辞与情节。"
        )
    return f"{HUMAN_REVISION_HEADER}\n{feedback}{directive}"


def _fmt_list(items: list[str], empty: str = "（无）") -> str:
    return "\n\n".join(items) if items else empty


def _reality_policy_of(ctx) -> str:
    """取本章的现实性口径文本；上下文里没有时回落到默认档（作者目标优先）。

    为什么要有兜底：`ChapterContext` 被大量测试与旧调用点手工构造（不带新字段），
    模板缺变量会 Fail-Fast 渲染失败——兜底保证"缺字段 = 用默认口径"，而不是炸掉生成。
    """
    from src.agents.reality_policy import reality_policy_text

    text = (getattr(ctx, "reality_policy", "") or "").strip()
    return text or reality_policy_text(False)


def _unchanged_ratio(new_text: str, old_text: str, step: int = 200) -> float:
    """两稿的"未变化程度"：新稿中能在旧稿里原样找到的 200 字块占比（0-1）。

    用分块精确匹配而不是编辑距离：中文长文按块比对对"整段复制"极敏感，
    也不会因为标点微调而误判为"已经改过"。
    """
    if not new_text or not old_text:
        return 0.0
    old_blocks = {old_text[i:i + step] for i in range(0, len(old_text), step)}
    new_blocks = [new_text[i:i + step] for i in range(0, len(new_text), step)]
    if not new_blocks:
        return 0.0
    same = sum(1 for b in new_blocks if b in old_blocks)
    return same / len(new_blocks)


def _looks_unchanged(new_text: str, old_text: str) -> bool:
    """重写稿是否"几乎等于原稿"：块重复率 ≥ 95% 且长度基本一致。"""
    if not new_text.strip() or not old_text.strip():
        return False
    ratio = _unchanged_ratio(new_text, old_text)
    same_scale = abs(len(new_text) - len(old_text)) <= max(50, int(len(old_text) * 0.02))
    return ratio >= (1 - _UNCHANGED_RATIO) and same_scale

def _fmt_characters(states: dict[str, str]) -> str:
    if not states:
        return "（无）"
    return "\n\n".join(f"### {name}\n{state}" for name, state in states.items())


def _fmt_foreshadowing(items: list[dict]) -> str:
    if not items:
        return "（无）"
    return "\n".join(
        f"- [{i.get('id')}] {i.get('desc')}（埋设于第{i.get('planted_ch')}章，"
        f"预期第{i.get('resolve_ch')}章回收）"
        for i in items
    )


def _fmt_state_board(items: list[dict]) -> str:
    if not items:
        return "（无）"
    return "\n".join(
        f"- 【{i.get('entity')}】{i.get('fact')}（第{i.get('chapter')}章确立）"
        for i in items
    )


def fmt_review_issues(review: ReviewOutput) -> str:
    """审查问题清单 → 带序号的文本（协商/仲裁提示词共用，序号与 index 对齐）。"""
    lines = [
        f"{i}. [{issue.severity}/{issue.dimension}] {issue.description}"
        + (f"（原文「{issue.quote}」）" if issue.quote else "")
        + f" → 建议：{issue.suggestion}"
        for i, issue in enumerate(review.issues, 1)
    ]
    return "\n".join(lines) if lines else "（无）"


class Writer:
    """章节正文生成；支持携带 Editor 问题清单 / 人工意见的重写。"""

    def __init__(
        self,
        registry: ModelRegistry,
        target_words: int = DEFAULT_TARGET_WORDS,
        tolerance: int = DEFAULT_TOLERANCE,
        max_continuation_attempts: int = DEFAULT_MAX_CONTINUATION_ATTEMPTS,
        ceiling: int | None = None,
    ):
        self._registry = registry
        self._target_words = target_words
        # `tolerance` 保留旧名（= 最低可接受字数）；`ceiling` = 最高可接受字数。
        # 二者缺省时按 GenerationConfig 的非对称口径现算（下浮 500 / 上浮 2000）。
        self._tolerance = tolerance
        # ceiling 未显式给出时，按实例目标折算上浮上限（而不是留 None 交给临时目标），
        # 保证"按章覆盖字数"时上浮侧仍守 2000 字的约定。
        self._ceiling = ceiling if ceiling is not None else _GEN.length_ceiling(target_words)
        self._max_continuation_attempts = max_continuation_attempts

    @property
    def target_words(self) -> int:
        return self._target_words

    def bounds_for(self, target: int) -> tuple[int, int]:
        """本章可接受字数区间（最低, 最高）；写作/重写/门禁共用同一对数。"""
        return resolve_bounds(target, self._tolerance, self._ceiling)

    def write_chapter(
        self,
        ctx: ChapterContext,
        revision_notes: str | None = None,
        previous_text: str | None = None,
        target_words_override: int | None = None,
    ) -> ChatResult:
        """生成/重写章节。revision_notes 非空时为重写模式。
        target_words_override 非空时覆盖实例默认 target_words，用于互动模式按章自定义字数。"""
        target = target_words_override if target_words_override is not None else self._target_words
        floor, ceiling = self.bounds_for(target)
        revision_section = ""
        if revision_notes:
            revision_section = self._revision_section(
                revision_notes, target, floor, ceiling,
                # 把上一稿的真实字数告诉模型：不写这个数，模型只会"在旧稿上再加点料"，
                # 实测重写稿从 2269 字涨到 3210 字（上限 3200）——差 10 字被判不合格，
                # 门禁于是白跑一轮。有了确切数字，模型才能"补到刚好落在区间内"。
                current_length=chapter_length(previous_text) if previous_text else None,
            )
            if previous_text:
                revision_section += (
                    "\n## 上一稿正文（在此基础上按上述修订指令修改）\n"
                    f"{previous_text}\n"
                )
        prompt = render_prompt(
            "writer_chapter",
            # X2+X7：虚构框架声明 + 零拒绝/零篡改/零失败 + 约束优先级链。
            # **这条链路此前一直缺它**（architect / plotter / 改稿提案都有）：
            # 出卡阶段模型愿意写、到了写正文就被拒答，用户看到的正是"互动创作拒绝生成"。
            # 与出卡链路同源同一个块，不另写措辞（避免两套防拒绝话术漂移）。
            brief_fidelity=brief_fidelity_block(),
            # 作者创作需求（brief）：与 custom_constraints 同级并列的**最高优先级输入**。
            # 缺它是"用户指令约束力不足"的结构性根因——正文此前只看到大纲/文风等蒸馏物。
            brief=ctx.brief.strip() or "（未提供）",
            target_words=target,
            tolerance=floor,
            length_floor=floor,
            length_ceiling=ceiling,
            revision_section=revision_section,
            chapter=ctx.chapter,
            outline=ctx.outline,
            recent_summaries=_fmt_list(ctx.recent_summaries),
            related_summaries=_fmt_list(ctx.related_summaries),
            character_states=_fmt_characters(ctx.character_states),
            foreshadowing=_fmt_foreshadowing(ctx.unresolved_foreshadowing),
            due_foreshadowing=_fmt_foreshadowing(ctx.due_foreshadowing),
            worldview_rules=_fmt_list(ctx.worldview_rules),
            state_board=_fmt_state_board(ctx.state_board),
            style_guide=ctx.style_guide.strip() or "（无）",
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
            # 现实性口径（用户要求）：默认"以作者创作目标为准"，不得因"不符合现实"改稿。
            # 兜底取值保证测试替身/旧调用点不炸（真实链路由 retrieve_context 第 ⑨ 步注入）。
            reality_policy=_reality_policy_of(ctx),
        )
        mode = "重写" if revision_notes else "初稿"
        logger.info("Writer: 第 %d 章%s生成中（目标 %d 字，可接受 %d-%d 字）...",
                    ctx.chapter, mode, target, floor, ceiling)
        result = _faithful_text(self._registry, prompt)

        # Layer 2: 后处理字数核验 —— 欠字数就继续扩写（多轮），超上限才压缩。
        # 单次模型调用的产出上限折算成中文正文只有约 3000-4000 字，因此"一轮就够"
        # 是错觉：5000 字目标必须靠**多轮追加**兑现（旧实现上限 1 轮，实测必然欠字）。
        draft = result.content
        for cont_i in range(max(0, self._max_continuation_attempts)):
            actual = chapter_length(draft)
            state = length_assessment(target, actual, floor, ceiling)
            if state == "pass":
                break
            if state == "short":
                logger.info(
                    "Writer: 第 %d 章第 %d 轮 %d 字不足（最低 %d），继续扩写...",
                    ctx.chapter, cont_i + 1, actual, floor,
                )
                draft = self._expand(prompt, draft, target, floor)
            else:
                logger.info(
                    "Writer: 第 %d 章第 %d 轮 %d 字超上限 %d，压缩中...",
                    ctx.chapter, cont_i + 1, actual, ceiling,
                )
                draft = self._compress(prompt, draft, target, ceiling)

        # ── 重写有效性检测（"按要求重写却没动"的兜底）──
        # 真实冒烟（5000 字目标）抓到的缺陷：打回后模型把上一稿**逐字原样返回**，
        # 字数不达标也修不了，上层门禁只能反复重试、白烧调用。
        # 这里在 write_chapter 内部自查一次：重写稿与上一稿几乎无差异 → 显式要求"必须真改"。
        if revision_notes and previous_text:
            for retry_i in range(_UNCHANGED_REWRITE_ATTEMPTS):
                if not _looks_unchanged(draft, previous_text):
                    break
                logger.warning(
                    "Writer: 第 %d 章重写稿与上一稿几乎一致（差异率 %.3f），"
                    "显式要求必须真实修改后重试（第 %d 次）",
                    ctx.chapter, _unchanged_ratio(draft, previous_text), retry_i + 1,
                )
                force = (
                    "【重写无效 · 必须真实修改】上一次输出与上一稿**几乎完全相同**，"
                    "这说明你只是把原稿抄了一遍，没有落实上面的重写指令。\n"
                    "现在必须逐条执行：\n"
                    "1. 对指令点名的每一处，**改写出新的文字**（该加的加、该删的删、该改的改）；\n"
                    "2. 保留上一稿中未被点名的情节与人物关系（可以保留其大意，但不要整段复制）；\n"
                    f"3. 输出**完整的修改后正文**（不是差异说明、不是片段、不是原稿），"
                    f"字数落在 {floor}-{ceiling} 字之间。\n"
                )
                forced_prompt = f"{prompt}\n\n{force}\n\n## 上一稿正文（须在其基础上真实改写）\n{previous_text}"
                draft = _faithful_text(self._registry, forced_prompt).content or draft

        # 重写路径的兜底压一次：允许误差的实质是"上浮 2000"，但模型落实意见时可能顺手
        # 多写一两百字直接顶穿上限（真实冒烟：3210 字 vs 上限 3200）。
        # 这种情况最经济的修法是当场压回去，而不是让上层门禁再烧一整轮重写。
        if revision_notes:
            over = chapter_length(draft) - ceiling
            if over > 0:
                logger.info(
                    "Writer: 第 %d 章重写稿超出上限 %d 字，直接压缩一轮回到区间内",
                    ctx.chapter, over,
                )
                draft = self._compress(prompt, draft, target, ceiling)

        result.content = draft

        final = chapter_length(result.content)
        logger.info(
            "Writer: 第 %d 章%s完成（%d 字 / 目标 %d 字，可接受 %d-%d 字，判定 %s，tokens=%s）",
            ctx.chapter, mode, final, target, floor, ceiling,
            length_assessment(target, final, floor, ceiling),
            result.usage.get("completion_tokens", "?"),
        )
        return result

    def _revision_section(self, revision_notes: str, target: int,
                          floor: int, ceiling: int,
                          current_length: int | None = None) -> str:
        """重写指令区（人审意见 + 刚性约束）。

        关键修正（"按建议重写但没落实"的根因）：原文写"允许误差 ±N 字"，在 5000 字
        目标下既没告诉模型"低于 4500 就是不合格"，也没告诉它"可以写到 7000"——
        模型于是按旧稿规模微调，用户看到的正是"重写后还是不够/没按建议改"。

        两处必须写死才算"可执行"：
        ① 区间上下限（否则模型不知道合格线在哪）；
        ② **上一稿的实际字数 + 这一稿允许的伸缩量**（否则模型落实意见时顺手多写，
           真实冒烟里就从 2269 字涨到 3210 字、越过了 3200 的上限，
           明明改对了内容却因超 10 字被判不合格）。

        "字数补足"的授权与"不改剧情"的边界分开表述：加戏**在既有场景内展开**即可
        （不新增剧情卡未提及的场景与角色），与"未点名处逐字保留"并不矛盾。
        """
        if current_length is None:
            length_line = (
                f"- 字数目标：重写后正文必须落在 **{floor}-{ceiling} 字**（目标 {target} 字）。"
                f"低于 {floor} 字直接判不合格；高于 {ceiling} 字同样不合格。\n"
            )
        else:
            delta_low = floor - current_length
            delta_high = ceiling - current_length
            if delta_low > 0:
                room = (f"当前字数**不足**：至少再增加 {delta_low} 字（最多可增加 {delta_high} 字），"
                        f"把该写的戏写足、写透，不得注水凑字")
            elif delta_high < 0:
                room = (f"当前字数**已超上限**：必须至少删减 {-delta_high} 字"
                        f"（压缩到 {ceiling} 字以内、贴近 {target} 字）")
            else:
                room = (
                    f"当前字数**已在合格区间内**：{current_length}-{ceiling} 字都合格，"
                    f"**收紧到与上一稿相近的规模**（目标 ±5% 以内），"
                    f"只把意见涉及的段落改到位即可，**不得为了加戏把总字数顶出上限、"
                    f"也不得为了压字数删掉合格内容**"
                )
            length_line = (
                f"- 字数目标：重写后正文必须落在 **{floor}-{ceiling} 字**（目标 {target} 字），"
                f"低于 {floor} 字或高于 {ceiling} 字都直接判不合格；\n"
                f"- 本次修订的字数余量：上一稿 {current_length} 字。{room}。"
                f"落实意见时以「刚好落在区间内」为准，宁可少加也不要顶穿上限；\n"
            )
        return (
            "## 重写指令（必须逐条落实）\n"
            f"{revision_notes}\n"
            "\n## ⚠ 重写约束提醒（刚性，与人审意见同等优先级）\n"
            f"{length_line}"
            "- 落实意见的方式：意见点名之处按意见改；未点名之处的**情节、人物关系、"
            "场景与事件不得改动**，但可以在既有场景内补足动作、对话、神态与感官细节"
            "（这是把字数写够的正常手段，不属于「新增剧情」）；\n"
            "- 剧情卡遵循：重写不得偏离【本章大纲】中剧情卡已确定的人物、场景、核心事件与结尾钩子；\n"
            "- 自定义约束：若【自定义创作约束】非空，其全部规则在重写中仍然生效，为最高优先级；\n"
            "- 严禁注水：不得用抽象形容词堆砌、同义反复、复述前文或复制上一稿来凑字数。\n"
        )

    def _expand(self, prompt: str, draft: str, target: int, floor: int) -> str:
        """欠字数扩写一轮（收敛版）：只要求补差额，从上一次停笔处接续。

        **单轮不一定够**：模型常常只补一部分（实测 2513→3000 字目标，一轮只加了 400 字），
        因此这里按"到 floor 为止"最多重试 `_LENGTH_ENFORCE_ROUNDS` 轮；
        任何一轮若没有真的加长（模型摆烂/复述），保留较长的那一版退出，不空转。
        """
        current = draft
        for attempt in range(_LENGTH_ENFORCE_ROUNDS):
            actual = chapter_length(current)
            if actual >= floor:
                break
            need = max(target - actual, 1)
            note = (
                f"【字数补充 · 第 {actual} 字续写】上一稿 {actual} 字，"
                f"距目标 {target} 字还差约 {need} 字（最低可接受 {floor} 字）。\n"
                f"紧接上一稿末尾**自然往下写**，只输出新增正文，不得重复、不得改写、不得总结已写内容。\n"
                f"新增内容必须继续推进本章剧情（补足场景、对话、动作、配角的反应、感官细节），"
                f"不要用抽象描写或环境铺陈凑字数；仍以本章大纲的核心事件与结尾钩子为终点。\n"
                f"这一次请至少写足 {need} 字。"
            )
            extra = _faithful_text(
                self._registry,
                f"{prompt}\n\n{note}\n\n## 上一稿正文（仅作续写衔接，勿重复其内容）\n{current}",
            )
            if not extra.content.strip():
                break
            merged = f"{current}\n\n{extra.content}"
            if chapter_length(merged) <= actual:
                logger.warning(
                    "Writer: 扩写第 %d 轮没有加长（%d → %d 字），不再空转",
                    attempt + 1, actual, chapter_length(merged),
                )
                break
            current = merged
        return current

    def _compress(self, prompt: str, draft: str, target: int, ceiling: int) -> str:
        """超上限压缩一轮（收敛版）：仅拦真正灌水的稿；上浮在 ceiling 内不触发。

        **单轮压缩同样不一定够**（实测：一次压缩后仍 3986 字 > 上限 3500），
        故重试到 ceiling 以内为止；任何一轮若没有真的变短，保留较短的那一版退出。
        """
        best = draft
        for attempt in range(_LENGTH_ENFORCE_ROUNDS):
            if chapter_length(best) <= ceiling:
                break
            note = (
                f"【字数压缩】上一稿 {chapter_length(best)} 字，超出最高可接受字数 {ceiling} 字。"
                f"删减冗余描写、重复表达与无关枝节，输出压缩后的完整正文，"
                f"压缩到 {ceiling} 字以内并尽量贴近目标 {target} 字；"
                f"保留核心事件、关键对话与结尾钩子，不得省略情节。"
            )
            trimmed = _faithful_text(
                self._registry,
                f"{prompt}\n\n{note}\n\n## 上一稿正文（在此基础上压缩）\n{best}",
            )
            if not trimmed.content.strip():
                break
            if chapter_length(trimmed.content) >= chapter_length(best):
                logger.warning(
                    "Writer: 压缩第 %d 轮没有变短（%d → %d 字），不再空转",
                    attempt + 1, chapter_length(best), chapter_length(trimmed.content),
                )
                break
            best = trimmed.content
        return best

    def respond_issues(
        self, ctx: ChapterContext, review: ReviewOutput, chapter_text: str
    ) -> NegotiationOutput:
        """对话协商（P4-A）：对 Editor 问题清单逐条回应（采纳/申辩）。"""
        prompt = render_prompt(
            "writer_negotiate",
            brief_fidelity=brief_fidelity_block(),
            chapter=ctx.chapter,
            outline=ctx.outline,
            issues=fmt_review_issues(review),
            comment=review.comment or "（无）",
            chapter_text=chapter_text,
            custom_constraints=ctx.custom_constraints.strip() or "（无）",
            reality_policy=_reality_policy_of(ctx),
        )
        logger.info(
            "Writer: 第 %d 章对 %d 条审查问题逐条回应（协商）...",
            ctx.chapter, len(review.issues),
        )
        return chat_structured(
            self._registry, ROLE, [ChatMessage("user", prompt)], NegotiationOutput
        )
