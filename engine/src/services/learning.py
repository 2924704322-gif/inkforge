"""学习仿写：两种模式的实现（纯逻辑，便于单测）。

模式（用户需求）：
- **full 全量蒸馏学习**（保持现状）：素材拆解（世界观/人物/道具/桥段）+ 剧情学习 + 文风学习。
  产出可以带原书的具体设定与桥段 —— 这是"学到它的世界"。
- **style 只学通用写法**（新增）：只产出「文风指纹 + 技法模板」这类**可迁移的通用写作规范**，
  **不得包含原书任何内容**：人名、地名、组织门派、功法招式、专有物品、具体桥段与剧情。

"禁止带原书内容"必须可验证，而不是只写在提示词里靠模型自觉，因此这里做三道闸：
1. **提示词硬约束**：明确禁止出现任何专有名词 / 剧情 / 桥段，并要求用"主角""反派""某门派"这类角色化称呼；
2. **确定性净化**：从样本文本里抽出高频专有名词候选（反复出现的 2-4 字术语），
   在产出中命中即改写为通用占位（人物→「主角/角色」、其余→「该设定」），并记录净化条目；
3. **泄漏复核**：净化后再扫一遍，仍命中的词一律列入 leak_terms（调用方据此告警）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from src.utils.logger import get_logger

logger = get_logger(__name__)

MODE_FULL = "full"
MODE_STYLE = "style"
VALID_MODES = (MODE_FULL, MODE_STYLE)

#: 只学写法时的产出小节（顺序即渲染顺序）
STYLE_SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "文风指纹",
        "分析以下文本的**文风**，只输出可迁移的写作规范，不要提到任何具体作品内容。"
        "逐项给出：叙事视角（人称/距离感）、句式节奏（长短句配比、断句习惯）、"
        "段落长度与分段习惯、对话密度与对话推进方式、描写偏好（动作/环境/五感/心理的配比）、"
        "词汇语域（书面/口语/古风/网络语）与高频虚词习惯、标点使用习惯。"
        "每一项必须写成**可直接执行的规范**（例：'单段不超过 4 行；每 300 字至少一次短句独立成段压节奏'），"
        "禁止写成'文笔很好'这类评价。",
    ),
    (
        "技法模板",
        "分析以下文本的**叙事技法**，只输出可迁移的通用方法，不要提到任何具体作品内容。"
        "逐项给出：开篇进戏手法、冲突升级阶梯、爽点/爆点的铺垫与兑现方式、"
        "章末钩子的构造方式（画面型/台词型/信息型）、场景转场手法、"
        "伏笔的埋设距离与回收节奏、信息释放的节奏（何时给读者、何时藏）。"
        "每一项写成**步骤化的可复用套路**（例：'先给结果再回补原因；回补不超过 3 句'）。",
    ),
    (
        "负面清单",
        "从以下文本中反推作者**刻意避免**的写法，只输出通用的禁忌与避坑项，"
        "不要提到任何具体作品内容。例如：不写什么样的过场、不用哪类形容词、"
        "不重复什么句式、避免哪种对话。每条给出'禁止 X，改为 Y'的可执行格式。",
    ),
)

#: 只学写法时的 system：硬约束 + 第零条由调用侧注入
STYLE_SYSTEM = (
    "你是文学技法与文风分析引擎。你只做一件事：把样本文本里的**通用写法**提取成可迁移的规范。\n"
    "【硬性禁令（违反即视为无效产出）】\n"
    "1. 禁止出现样本中的任何人名、地名、组织/门派/势力名、功法/招式/技能名、"
    "专有物品与专有称谓；一律改用「主角」「反派」「配角」「某门派」「该功法」这类通用角色化称呼；\n"
    "2. 禁止复述、概括或透露样本的具体剧情、事件、对话内容、桥段与结局；\n"
    "3. 禁止输出任何看起来像原文的片段；需要举例时自己另造**与你我作品无关**的中性例子；\n"
    "4. 只输出结构化 Markdown 要点，不要客套、不要复述原文、不要评价作品好坏。\n"
    "【产出定位】你的产出会被直接写进另一部作品的写作规范，因此每条都必须是"
    "『可照着做』的规则，而不是对这篇文本的读后感。"
)

FULL_SYSTEM = (
    "你是文学分析引擎，只输出结构化 Markdown 要点，不要客套与复述原文。"
)

#: 专有名词候选过滤：纯虚词/常见动词/通用体裁词不算专有名词
_STOPWORDS = frozenset({
    "什么", "这个", "那个", "我们", "你们", "他们", "自己", "一个", "没有", "可以",
    "因为", "所以", "但是", "然后", "已经", "还是", "就是", "不是", "这样", "那样",
    "如果", "虽然", "然而", "于是", "并且", "而且", "这里", "那里", "时候", "东西",
    "知道", "觉得", "看着", "听到", "说着", "笑了", "起来", "出来", "过来", "上去",
    "主人", "长老", "弟子", "师兄", "师姐", "公子", "大人", "前辈", "诸位", "众人",
    "修为", "境界", "功法", "法术", "法宝", "灵气", "神识", "灵力", "丹药", "灵石",
    "宗门", "散修", "坊市", "秘境", "灵脉", "世家", "正道", "魔道", "储物袋",
    "主角", "反派", "配角", "对手", "敌人", "朋友", "师父", "徒弟",
    "第一章", "第二章", "第三章", "一章", "这一章", "上章", "下章",
})

_NGRAM_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")

#: 句中片段：以标点/空白/行首行尾切分，避免跨词切出"林尘走进"这类伪专名。
#: 用显式字符集拼装（不内嵌引号，避免正则字面量与引号打架）。
_CLAUSE_SEPARATORS = (
    "，。！？；：、（）()《》【】…—～·「」『』〈〉,,.!?;:\"'“”‘’"
    " \t\r\n"
)
_CLAUSE_RE = re.compile(f"[^{re.escape(_CLAUSE_SEPARATORS)}]+")

#: 头衔（人名 + 头衔 是最强的人名信号）
_TITLES = ("长老", "掌门", "宗主", "真人", "道人", "上人", "前辈", "师兄", "师姐",
           "师弟", "师妹", "师尊", "公子", "小姐", "老爷", "大人", "将军", "陛下",
           "殿下", "执事", "护法", "堂主", "峰主", "城主", "寨主", "总管")

#: 专有名词后缀（组织/地点/功法/物品）
_ENTITY_SUFFIXES = ("宗", "门", "派", "阁", "殿", "院", "府", "山庄", "洞", "谷", "城",
                    "洲", "界", "域", "海", "山", "剑", "刀", "枪", "诀", "经", "功", "法",
                    "丹", "符", "阵", "诀", "幡", "珠", "鼎", "环", "戒")
#: 出现在人名之前的动词/副词/代词（不得当作名字的一部分）
_NAME_LEADS = ("他", "她", "你", "我", "那", "这", "只见", "忽然", "随后", "便", "又", "却",
               "见", "说", "道", "问", "答", "笑", "走", "站", "念", "低", "抬", "点", "摇")


#: 汉字判定（**不能**用 `[\u4e00-\u9fff]` 这类转义字符类：
#: 本机 Python 3.13 下 `re.match("[\u4e00-\u9fff]", "周")` 实测为 False，
#: 即转义范围类匹配不到汉字；用真实字符 `[一-鿿]` 才正常。函数式判定最稳、也最可读。）
_CJK_LO, _CJK_HI = 0x4E00, 0x9FFF
_BOUNDARY_CHARS = " \t\r\n，。！？；：、（）()《》【】…—「」『』“”\"'"


def _is_cjk(ch: str) -> bool:
    return bool(ch) and _CJK_LO <= ord(ch) <= _CJK_HI


def _cjk_runs(text: str) -> list[tuple[int, str]]:
    """把文本切成连续汉字块：[(起始下标, 块内容)]。"""
    runs: list[tuple[int, str]] = []
    i = 0
    while i < len(text):
        if _is_cjk(text[i]):
            j = i
            while j < len(text) and _is_cjk(text[j]):
                j += 1
            runs.append((i, text[i:j]))
            i = j
        else:
            i += 1
    return runs


#: 专名候选的两种高置信度信号（形态规则，不读内容、不理解语义）：
#:  ① 头衔紧前的 2-3 字（`…周长老` → 周长老；左边界天然是标点，不会切出"见过周"）；
#:  ② 以组织/地点/功法/物品后缀结尾的整词（`玄天宗` / `青霜剑`）。
_TITLE_SUFFIXES = tuple(_TITLES) + ("宗", "门", "派", "阁", "殿", "院", "府", "洞", "谷",
                                    "城", "洲", "界", "域", "剑", "刀", "诀", "经", "丹", "幡")


def _bump(counts: dict[str, int], term: str) -> None:
    """登记一个候选：剥掉前缀动词/代词后规范到 2-3 字。

    人名多为 2 字（"周长老" → 窗口是"周"，必须原样保留），也有 3 字；
    窗口更长时（"弟子林尘见过周长老" → "见过周"）先剥人名引导字，再取尾部 2 字。
    """
    term = (term or "").strip()
    if len(term) == 2:
        if not _is_stopwordish(term):
            counts[term] = counts.get(term, 0) + 1
        return
    if len(term) > 3:
        term = term[-3:]
    term = re.sub(r"^(?:" + "|".join(map(re.escape, _NAME_LEADS)) + r")+", "", term)
    if len(term) > 2:
        term = term[-2:]
    if len(term) == 2 and not _is_stopwordish(term):
        counts[term] = counts.get(term, 0) + 1


def proper_noun_candidates(sample: str, *, min_count: int = 2, top: int = 40) -> list[str]:
    """从样本文本里抽出专有名词候选（人名/地名/组织/专名）。

    用项目已有依赖 **jieba 词性标注**（nr 人名 / ns 地名 / nt 机构 / nz 其他专名），
    比手写规则可靠得多：手写规则要么漏（"林尘" 抓不到），要么跨词误抓（"见过周"）。
    只做形态识别、不做语义理解，也不读内容。

    ⚠ 实测坑：不能用 `[\\u4e00-\\u9fff]` 这类**转义字符类**——本机 Python 3.13 下
    `re.match("[\\u4e00-\\u9fff]", "周")` 为 False（匹配不到汉字）。故这里一律走 jieba。
    """
    text = (sample or "").strip()
    if not text:
        return []
    try:
        import jieba

        jieba.setLogLevel(60)   # 静音：不让分词日志污染引擎 stderr

        counts: dict[str, int] = {}
        from jieba import posseg as pseg

        for word, flag in pseg.cut(text[:12000]):
            if flag in ("nr", "ns", "nt", "nz") and 2 <= len(word) <= 4:
                if _is_stopwordish(word):
                    continue
                counts[word] = counts.get(word, 0) + 1
    except Exception as exc:  # noqa: BLE001 - 分词失败时退化为"无候选"，不阻断功能
        logger.warning("专名候选提取失败（jieba 不可用？）：%s", exc)
        return []
    ranked = sorted(
        ((w, c) for w, c in counts.items() if c >= min_count),
        key=lambda item: (-item[1], -len(item[0])),
    )
    return [w for w, _c in ranked[:top]]


def _cjk_only(text: str) -> str:
    return "".join(re.findall(r"[\u4e00-\u9fff]", text or ""))


def detect_verbatim_copy(sample: str, report: str, *, window: int = 8) -> list[str]:
    """检测产出里是否**逐字复制**了样本连续片段（比专名更硬的泄漏证据）。

    做法：把两侧都压成纯汉字串，滑动窗口取样本的 window 长 n-gram，
    命中产出即记为复制片段。命中任意一条就意味着"带原书内容"。
    """
    src = _cjk_only(sample)
    dst = _cjk_only(report)
    if len(src) < window or len(dst) < window:
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for i in range(len(dst) - window + 1):
        gram = dst[i: i + window]
        if gram in src and gram not in seen:
            seen.add(gram)
            hits.append(gram)
        if len(hits) >= 20:
            break
    return hits


def _is_stopwordish(term: str) -> bool:
    """明显是普通词（虚词/通用体裁词/人称）→ 不算专有名词候选。"""
    if term in _STOPWORDS:
        return True
    return any(t in term for t in ("什么", "这个", "那个", "我们", "他们", "自己", "已经"))


#: 净化的角色化替换：人名 → 主角/配角，其余 → 该设定
#: 判定信号不是"词后跟了什么"，而是**产出句子里这个词处于人物位置**（前后 8 字内有动作/对话词）
_PERSON_HINTS = ("说道", "道：", "抱拳", "点头", "皱眉", "转身", "走", "站", "看", "笑",
                 "喊", "问", "答", "抬手", "咬牙", "说道", "开口", "沉默", "心底", "心想")


@dataclass
class ScrubResult:
    text: str
    replaced: dict[str, str] = field(default_factory=dict)
    leak_terms: list[str] = field(default_factory=list)


def scrub_proper_nouns(text: str, candidates: list[str]) -> ScrubResult:
    """把产出里命中的专有名词替换成通用占位；返回 (净化文本, 替换表, 残留词)。

    替换策略：该词所在的句子片段里出现动作/对话提示（"说道/抱拳/皱眉"…）→ 按**人物**处理，
    写成「主角」；其余一律写成「该设定」（组织/地点/物品）。替换后仍命中的记入 leak_terms。
    """
    out = text or ""
    replaced: dict[str, str] = {}
    for term in candidates:
        if term not in out:
            continue
        idx = out.find(term)
        window = out[max(0, idx - 8): idx + len(term) + 8]
        is_person = any(h in window for h in _PERSON_HINTS)
        placeholder = "主角" if is_person else "该设定"
        out = out.replace(term, placeholder)
        replaced[term] = placeholder
    leak = [t for t in candidates if t in out]
    return ScrubResult(text=out, replaced=replaced, leak_terms=leak)


#: 增量轮追加的指令（**必须有**：模型不知道已学过什么时，会把同一批写法换个措辞重列一遍，
#: 实测两章就堆到 244 条，条目表迅速膨胀）。
INCREMENT_RULE = (
    "\n\n【增量要求（本次是追加学习，不是第一次）】\n"
    "上方已列出此前章节学到的写法。请：\n"
    "1. **只输出这次样本里新出现的、或需要修正既有条目的写法**；换个措辞复述已有的不算新；\n"
    "2. 若这次样本对某条已有写法给出了**不同数值或相反要求**（例：已有『段落 ≤4 行』，"
    "而本文是『段落 ≤3 行』），请原样写出该条并标注 `[修正]`；\n"
    "3. 不要为了凑数罗列——没有新东西就输出空清单。\n"
)

#: 已有条目回灌上限（控制 token；按最近优先保留）
REFERENCE_ITEM_CAP = 120


def build_stage_prompts(
    mode: str, known_items: list[str] | None = None
) -> list[tuple[str, str]]:
    """返回 [(小节标题, 分析指令)]。

    known_items 非空时为**增量轮**：把这些已学条目回灌给模型，并附增量要求。
    """
    base = (
        list(STYLE_SECTIONS)
        if mode == MODE_STYLE
        else [
            (
                "世界观设定点",
                "拆解以下文本的**世界观设定点**：力量/规则体系、社会与势力结构、地理与场所、"
                "资源与代价机制。\n"
                "**每条必须以分类标签开头**，格式：`[世界观] 名称：内容`"
                "（例：`[世界观] 修为体系：炼气→筑基→金丹`）。名称要短（≤10 字），"
                "只写设定本身的名字，不要把整句话当名称。每条一行，不要写小节标题。",
            ),
            (
                "人物设定点",
                "拆解以下文本的**人物设定点**：逐人一条。\n"
                "**每条必须以 `[人物]` 开头**，格式：`[人物] 人物名：定位、外观/身份特征、"
                "性格与欲望、处境与关系`。名称就是人物名（不要写成事件或设定名）。"
                "每条一行，不要写小节标题。",
            ),
            (
                "道具",
                "拆解以下文本里的**道具 / 法宝 / 消耗品**：每项一条。\n"
                "**筛选标准（重要）**：只收录**被反复提到、或对情节起关键作用**的道具；"
                "只出现过一次的场面物（路人手里的杯子、路边石头之类）一律不要。\n"
                "**每条必须以 `[道具]` 开头**，格式：`[道具] 名称：是什么、关键属性、谁在用`。"
                "名称用文中**同名**写法（同一样东西只列一条，不要换名字重复列）。"
                "每条一行，不要写小节标题。",
            ),
            (
                "地点",
                "拆解以下文本里的**地点 / 场所 / 势力据点**：每项一条。\n"
                "**筛选标准（重要）**：只收录**被反复提到、或作为关键事件发生地**的地点；"
                "只出现过一次的过渡场景（一段石阶、一间客栈）一律不要。\n"
                "**每条必须以 `[地点]` 开头**，格式：`[地点] 名称：是什么、关键属性、与谁或什么事相关`。"
                "名称用文中**同名**写法。每条一行，不要写小节标题。",
            ),
            (
                "桥段",
                "拆解以下文本里**按时间顺序发生的关键桥段**：每个桥段一条，"
                "**必须严格按文中出现顺序排列**（先后不能颠倒）；"
                "只保留推动剧情的关键节点，琐碎过场不要。\n"
                "**每条必须以 `[桥段]` 开头**，格式：`[桥段] 桥段名：步骤链`"
                "（例：`[桥段] 入门受命：当众领命→长辈施压→克制应下`）。"
                "桥段名 ≤10 字且是名词性短语，不要把步骤链当名称。"
                "**一行只写一个桥段**（不要用 | 把多条并在一行）。每条一行，不要写小节标题。",
            ),
            (
                "剧情技法",
                "分析以下文本的剧情技法：主线推进方式、冲突设计、转折点位置与效果、"
                "结尾钩子手法。**每条以 `[技法]` 开头**，每条一行，不要写小节标题。",
            ),
            (
                "文风学习",
                "分析以下文本的文风：叙事视角、句式节奏、描写偏好（动作/环境/五感）、"
                "对话风格、词汇倾向。**每条以 `[文风]` 开头**，每条一行，不要写小节标题。",
            ),
        ]
    )
    if not known_items:
        return base
    listed = "\n".join(f"- {t}" for t in known_items[-REFERENCE_ITEM_CAP:])
    reference = f"\n\n【已学到的写法（此前章节，勿重复罗列）】\n{listed}"
    return [(label, ask + reference + INCREMENT_RULE) for label, ask in base]


#: full 模式下产出**素材条目**的小节 = 全部小节。
#:
#: 这条路由踩过两次坑，记录在此避免第三次：
#: ① 判 `materials.categorize(label) != CAT_OTHER` —— 技法/文风本身也是合法素材分类，
#:    判据恒真，结果成果条目永远为空（界面一片空）；
#: ② 反向修成"只让设定类小节产素材" —— 技法与文风**两边都不落地**、凭空消失
#:    （用户实测发现"技法和文风没有加入到素材库"）。
#: 定论：**素材库是唯一容器**，full 模式的每个小节都产素材；
#: 学习成果退化为任务记录（来源章节 / 产出清单 / 蒸馏日志）。
def is_material_section(label: str) -> bool:  # noqa: ARG001 - 保留签名，恒为 True
    """该小节是否产出素材条目（full 模式下恒为 True）。"""
    return True


def system_for(mode: str) -> str:
    return STYLE_SYSTEM if mode == MODE_STYLE else FULL_SYSTEM


def mode_label(mode: str) -> str:
    return "只学写法（通用文风/技法）" if mode == MODE_STYLE else "全量蒸馏学习"


# ══════════════════════════════════════════════════════════════════════
# 累积式蒸馏：一条成果可多次投喂（第一章 → 追加第二章 → …）
#
# 为什么不做"累积重算"（把新内容并进样本整体重跑）：
#   · 每加一章都要重烧全部历史，成本随章节数线性上涨；
#   · 长样本迟早超上下文，早期章节会被截断丢失；
#   · 一次失败可能把已有成果一起烧掉。
# 因此采用**增量合并**：只分析新投喂的内容，把新条目并入既有条目表；
# 重复特征只累加"命中次数"（被两章同时印证 = 更可信），冲突条目进"待裁决"。
# ══════════════════════════════════════════════════════════════════════

#: 判定"同一条目"的阈值：jieba 词级 Jaccard，或最长公共子串长度
DUP_JACCARD = 0.5
DUP_LCS = 12
#: 判定"同主题但说法不同"（→ 待裁决冲突）的阈值。
#: **必须同时满足**重合度与 Jaccard：只看重合度时，短句会因为共享几个字（如都含"句"）
#: 被误判成冲突（实测把"开篇直入动作"和"章末钩子"配成一对）。
CONFLICT_TOPIC_OVERLAP = 0.5
CONFLICT_MIN_JACCARD = 0.2
#: 单轮追加最多入队多少条冲突（条目多的章节可能触发大量近似条目，不能把用户淹没）
MAX_CONFLICTS_PER_RUN = 20
#: 合并元数据的结构版本（未来格式变更时用于迁移）
SCHEMA_VERSION = 2


def _norm_tokens(text: str) -> set[str]:
    """规范化 token 集合（jieba 词级）。

    为什么不用字符/双字滑窗：模型每轮措辞都不同，实测"采用第三人称限知视角，摄像机始终
    贴近主角" 与 "...贴近主角肩后" 在双字滑窗下 Jaccard 只有 0.6 出头，会漏判为两条，
    条目表迅速膨胀（真机一次追加从 105 条涨到 255 条）。词级切分能正确识别为同一条。
    失败时退化为单字集合（仍然比什么都不做强）。
    """
    cleaned = re.sub(r"^\s*(?:\d+[.、)]|[-*+•])\s*", "", (text or "").strip())
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", cleaned).lower()
    if not cleaned:
        return set()
    try:
        import jieba

        jieba.setLogLevel(60)
        words = {w.strip() for w in jieba.lcut(cleaned) if len(w.strip()) >= 2}
    except Exception:  # noqa: BLE001 - 分词不可用时退化为单字
        words = set()
    words.update(re.findall(r"[a-z0-9]{2,}", cleaned))
    words.update(re.findall(r"[\u4e00-\u9fff]", cleaned))
    return words


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（动态规划，条目级别文本很短，开销可忽略）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _norm_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _char_set(text: str) -> set[str]:
    """去标点后的**汉字/字母数字集合**（顺序无关，用于话题重合度）。

    为什么不用分词：实测「对话占全章四成以上」vs「对话占全章六成以上」在大/二-gram
    切分下重合度极低（0.27/0.43），反而漏掉真冲突。字符集 Jaccard 对"同一话题不同措辞"
    更稳，且与"逐字重复"天然一致（重复文本字符集必然高度重合）。
    """
    return set(re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower()))


def is_duplicate(a: str, b: str) -> bool:
    """两条条目是否"说的是同一件事"（同小节内才会调用）。"""
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if _lcs_len(na, nb) >= DUP_LCS:
        return True
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= DUP_JACCARD


def topic_overlap(a: str, b: str) -> float:
    """话题重合度（判定"同话题、说法不同"的潜在冲突）。"""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def token_jaccard(a: str, b: str) -> float:
    """词级 Jaccard（冲突判定的第二条件：既要"像"又要"说同一件事"）。"""
    ta, tb = _norm_tokens(a), _norm_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_conflict_candidate(a: str, b: str) -> tuple[bool, float]:
    """两条同小节条目是否是"同一件事的两种说法"（→ 需要人工裁决）。

    返回 (是否冲突, 重合度)。两个条件都要满足：
    · 重合度 ≥ CONFLICT_TOPIC_OVERLAP（共享词占比高 → 说的是同一处要求）；
    · Jaccard ≥ CONFLICT_MIN_JACCARD（整体也要像 → 排除只蹭到几个常用字的短句）。
    """
    overlap = topic_overlap(a, b)
    if overlap < CONFLICT_TOPIC_OVERLAP:
        return False, overlap
    return token_jaccard(a, b) >= CONFLICT_MIN_JACCARD, overlap


def item_id(section: str, text: str) -> str:
    """稳定条目 id（同一文本在重新解析后 id 不变，便于前端定位与人工干预）。"""
    digest = hashlib.sha1(f"{section}\x00{_norm_text(text)}".encode()).hexdigest()
    return "i-" + digest[:8]


def parse_report_into_items(
    report: str, *, sections: list[str] | None = None, seq: int = 1
) -> list[dict]:
    """把一轮分析产出（Markdown）解析成条目表。

    规则（**必须有小节标题才认条目**）：
    1. 命中 `sections` 里的小节标题 → 其下 `- `/`1. ` 行各成一条；
    2. 整个产出没有小节标题（模型跑了题）→ **整段作为一条**，小节名取 `sections[0]`，
       绝不把它当成"第 1 条、第 2 条"逐行拆碎（实测：模型输出散文时会产生一堆垃圾条目）；
    3. 非小节标题（模型自带的 `# 大标题`）不改当前小节，避免条目挂到不存在的小节上。
    """
    wanted = list(sections or [])
    items: list[dict] = []
    current = ""
    stage = wanted[0] if wanted else ""
    saw_stage_heading = False
    buffer: list[str] = []          # 小节内的段落缓冲（无列表项时整体成条）
    bullets: list[str] = []         # 小节内的列表项缓冲（按行成条）

    def _flush_bullets() -> None:
        while bullets:
            items.append(_make_item(current or stage, bullets.pop(0), seq))

    def _flush_prose() -> None:
        """当前小节里没有列表项时，把累积的散文段落整体作为一条。"""
        if bullets:
            _flush_bullets()
            buffer.clear()
            return
        text = "\n".join(buffer).strip()
        if text and (current or stage):
            items.append(_make_item(current or stage, text, seq))
        buffer.clear()

    for raw in (report or "").splitlines():
        line = raw.rstrip()
        heading = re.match(r"^#{1,4}\s*(.+?)\s*$", line)
        if heading:
            _flush_prose()
            title = heading.group(1)
            if not wanted:
                current = title
            else:
                hit = next((w for w in wanted if w in title), "")
                if hit:
                    stage = hit
                    current = hit
                    saw_stage_heading = True
            continue
        if wanted and not current and not saw_stage_heading:
            # 还没有进入任何小节：正文一律先归到本阶段，避免大段散文被丢掉
            current = stage
        if not current:
            continue
        bullet = re.match(r"^\s*(?:[-*+•]|\d+[.、)])\s+(\S.*)$", line)
        if bullet:
            if buffer:      # 列表前的散文（引言句）：单独成条，不丢掉
                _flush_prose()
            bullets.append(bullet.group(1).strip())
        elif line.strip():
            buffer.append(line.strip())
        else:
            _flush_prose()
    _flush_prose()
    return items


def _make_item(section: str, text: str, seq: int) -> dict:
    return {
        "id": item_id(section, text),
        "section": section,
        "text": text.strip(),
        "chapters": [seq],
        "hits": 1,
    }


@dataclass
class MergeOutcome:
    items: list[dict]
    added: int = 0
    merged: int = 0
    conflicts: list[dict] = field(default_factory=list)


class ConflictAction:
    """冲突裁决动作（由用户点选，程序确定性落库）。"""

    KEEP_EXISTING = "keep_existing"   # 早期章节为准
    TAKE_INCOMING = "take_incoming"   # 后学覆盖（默认推荐：更贴近当下意图，且可撤销）
    MERGE_BOTH = "merge_both"         # 两条都要，写成一个
    EDIT = "edit"                     # 用户给最终文本
    UNDO = "undo"                     # 撤销某次裁决


VALID_CONFLICT_ACTIONS = (
    ConflictAction.KEEP_EXISTING,
    ConflictAction.TAKE_INCOMING,
    ConflictAction.MERGE_BOTH,
    ConflictAction.EDIT,
)


class ConflictResolutionError(RuntimeError):
    """裁决入参非法（如序号越界、手动编辑空文本）→ 由端点翻译成 4xx。"""


@dataclass
class ConflictResolutionOutcome:
    items: list[dict]
    conflicts: list[dict]
    resolved: list[dict]
    changed: int = 0
    warnings: list[str] = field(default_factory=list)
    undone_action: str = ""


def merge_items(
    existing: list[dict], incoming: list[dict], *, seq: int
) -> MergeOutcome:
    """把新一轮条目并入既有条目表。

    · 同小节内命中 is_duplicate → 不新增，chapters 追加章号、hits+1（被多章印证）；
    · 未命中 → 追加为新条目；
    · 冲突判定**只看最近邻**：同小节里与它最像的那条，若相似度没到重复线、但话题重合 ≥
      下限，才算"同一条目、不同说法"（例：段落 ≤4 行 vs ≤3 行；对话 30-40% vs 60%）。
      早先版本跟**所有**已有条目比，导致一轮追加就能刷出几十条假冲突（真机实测 77 条），
      真冲突被淹没。

    注意：只在"既有条目（上一轮及更早）"里找最近邻 —— 同一轮产出内部的条目互为不同细节，
    不该被判成冲突。
    """
    merged_items = [dict(it) for it in existing]
    added = merged = 0
    conflicts: list[dict] = []
    peers: dict[str, list[dict]] = {}
    for it in merged_items:
        peers.setdefault(str(it.get("section") or ""), []).append(it)

    for inc in incoming:
        section = inc.get("section", "")
        text = str(inc.get("text", "")).strip()
        if not text:
            continue
        bucket = peers.get(section, [])
        dup = next((it for it in bucket if is_duplicate(str(it.get("text", "")), text)), None)
        if dup is not None:
            chapters = list(dup.get("chapters") or [])
            if seq not in chapters:
                chapters.append(seq)
            dup["chapters"] = sorted(chapters)
            dup["hits"] = int(dup.get("hits") or 1) + 1
            merged += 1
            continue
        near, score, flag = None, 0.0, False
        for it in bucket:
            is_c, ov = is_conflict_candidate(str(it.get("text", "")), text)
            if is_c and ov > score:
                near, score, flag = it, ov, True
        new_item = dict(inc)
        merged_items.append(new_item)
        bucket.append(new_item)
        added += 1
        if flag and near is not None:
            conflicts.append({
                "section": section,
                "existing": str(near.get("text", "")),
                "existing_chapters": list(near.get("chapters") or []),
                "incoming": text,
                "incoming_chapter": seq,
                # 记下新条目 id：后续裁决要精确地把它并掉/移除（文本查找只作兜底）
                "incoming_id": str(new_item.get("id") or ""),
                "overlap": round(score, 2),
            })
    if len(conflicts) > MAX_CONFLICTS_PER_RUN:
        # 条目多的章节可能刷出大量近似条目：按重合度取最像的前 N 条，其余只作为新增条目保留
        conflicts.sort(key=lambda c: float(c.get("overlap") or 0), reverse=True)
        conflicts = conflicts[:MAX_CONFLICTS_PER_RUN]
    return MergeOutcome(items=merged_items, added=added, merged=merged, conflicts=conflicts)


def render_items_body(items: list[dict]) -> str:
    """条目表 → 干净正文（**带溯源标记**，便于人看，也便于整体当文风指纹注入）。

    标记形如 `〔1,2〕`：该条由第 1、2 章共同印证。没有标记的条目属于没有来源信息的历史数据。
    """
    order: list[str] = []
    grouped: dict[str, list[dict]] = {}
    for it in items:
        sec = str(it.get("section") or "未分类")
        if sec not in grouped:
            grouped[sec] = []
            order.append(sec)
        grouped[sec].append(it)
    blocks: list[str] = []
    seen_sec: set[str] = set()
    for sec in order:
        key = _norm_text(sec)
        if key and key in seen_sec:
            continue        # 同名小节只渲染一次（模型可能把阶段名当小节名回显）
        seen_sec.add(key)
        lines = [f"## {sec}", ""]
        for i, it in enumerate(grouped[sec], 1):
            chapters = it.get("chapters") or []
            mark = f"〔{','.join(str(c) for c in chapters)}〕" if chapters else ""
            lines.append(f"{i}. {mark}{it.get('text', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def parse_stored_document(meta: dict, body: str) -> dict:
    """读取既有成果（含**旧格式兼容**）。

    旧格式判定看 `schema` 标记，**不看条目数**：v2 成果即使条目为 0（模型产出不合格）
    也已经是条目化文档，若按"条目空"当旧格式处理，下一轮就会把**自家正文**（含日志）
    当成模型产出重新解析，形成自反馈污染（真机实测踩过）。
    """
    items = meta.get("items")
    sources = meta.get("sources")
    versioned = int(meta.get("schema") or 0) >= SCHEMA_VERSION
    legacy = not versioned
    if legacy:
        items = parse_report_into_items(body, seq=1)
        sources = list(sources or [{"seq": 1, "label": "原始样本", "chars": len(body),
                                    "sha256": "", "distilled_at": 0}])
    return {
        "items": list(items or []),
        "sources": list(sources or []),
        "conflicts": list(meta.get("conflicts") or []),
        "changelog": list(meta.get("changelog") or []),
        "legacy": legacy,
        "scrubbed": int(meta.get("scrubbed") or 0),
    }


def extract_stage_chunk(report: str, stage: str, all_stages: list[str]) -> str:
    """从模型的一段产出里裁出"属于 stage 的那部分"。

    模型经常把多个小节写在同一段回复里（一次调用给出三节内容）。逐阶段解析时若不裁剪，
    另外两节的内容会被重复算进本阶段（实测：单条变三条，并刷出假冲突）。
    兜底：找不到本阶段标题时返回原文（宁可不裁，也不丢内容）。
    """
    lines = (report or "").splitlines()
    start = -1
    for i, line in enumerate(lines):
        m = re.match(r"^#{1,4}\s*(.+?)\s*$", line)
        if m and stage in m.group(1):
            start = i
            break
    if start < 0:
        return report or ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = re.match(r"^#{1,4}\s*(.+?)\s*$", lines[j])
        if not m:
            continue
        title = m.group(1)
        if stage in title:
            continue                     # 同名标题（罕见）：算作本节内部标题
        if any(other in title for other in all_stages):
            end = j                      # 撞到另一个阶段标题 → 本节到此为止
            break
    body = list(lines[start + 1: end])
    return "\n".join(body).strip() or "\n".join(lines[start: end])


def strip_item_marks(text: str) -> str:
    """去掉条目里的 ``〔1,2〕`` 溯源标记（用于把成果直接当文风指纹注入时）。"""
    return re.sub(r"〔[\d,]*〕", "", text or "")


def _remove_item(items: list[dict], item_id_value: str) -> dict | None:
    """从条目表里摘掉一条（返回被摘掉的那条，没有则 None）。"""
    for i, it in enumerate(items):
        if str(it.get("id")) == item_id_value:
            return items.pop(i)
    return None


def _find_item(items: list[dict], section: str, text: str) -> dict | None:
    """按（小节 + 文本）定位条目：原文命中优先，其次用同一条判定兜底。"""
    norm = _norm_text(text)
    for it in items:
        if it.get("section") == section and _norm_text(str(it.get("text", ""))) == norm:
            return it
    return next(
        (it for it in items
         if it.get("section") == section and is_duplicate(str(it.get("text", "")), text)),
        None,
    )


def _merge_chapters(target: dict, incoming: dict | None, seq: int = 0) -> None:
    """把来源章号并入 target 并累加命中次数。

    为什么要显式传 seq：冲突记录里记着 `incoming_chapter`，而"新条目"在某些流程里可能
    已经被上一步并掉了（那时只有章号信息可靠）。
    """
    chapters = {int(c) for c in (target.get("chapters") or []) if str(c).isdigit()}
    chapters.update(int(c) for c in ((incoming or {}).get("chapters") or []) if str(c).isdigit())
    if seq:
        chapters.add(int(seq))
    target["chapters"] = sorted(chapters)
    target["hits"] = int(target.get("hits") or 1) + int((incoming or {}).get("hits") or 0)


def apply_conflict_resolution(
    items: list[dict],
    conflicts: list[dict],
    *,
    index: int | None = None,
    action: str = "",
    text: str = "",
    bulk: bool = False,
) -> ConflictResolutionOutcome:
    """把人工裁决落到条目表上（**确定性**，不再调用模型）。

    四个动作（见 ConflictAction）：
    · keep_existing  早期为准：新条目并入原条目（章节合并、hits+1）后移除；
    · take_incoming  后学覆盖：原条目文本替换为新的，章节合并、hits+1，新条目移除；
    · merge_both     两条都要：文本做成「原有；新的」（真正重复的裁剪句），章节合并；
    · edit           由用户给最终文本：按用户的写，章节合并、hits+1，新条目移除。
    """
    working_items = [dict(it) for it in items]
    working_conflicts = [dict(c) for c in conflicts]
    if bulk:
        targets = list(range(len(working_conflicts)))
    elif index is None:
        raise ConflictResolutionError("缺少 index（单条裁决）或 bulk（批量裁决）")
    else:
        if index < 0 or index >= len(working_conflicts):
            raise ConflictResolutionError(f"冲突序号越界：{index}（共 {len(working_conflicts)} 条）")
        targets = [index]

    resolved_records: list[dict] = []
    changed = 0
    warnings: list[str] = []
    for pos in sorted(targets, reverse=True):   # 倒序删除，避免下标位移
        c = working_conflicts[pos]
        section = str(c.get("section") or "")
        seq = int(c.get("incoming_chapter") or 0)
        has_existing = bool(c.get("has_existing", True))
        existing_text = str(c.get("existing") or "")
        incoming_text = str(c.get("incoming") or "")
        existing_item = _find_item(working_items, section, existing_text) if has_existing else None
        incoming_id = str(c.get("incoming_id") or item_id(section, incoming_text))
        incoming_item = _remove_item(working_items, incoming_id)
        if incoming_item is None and has_existing and existing_item is not None:
            # 第二轮可能已经把它并进原条目了：从原条目里移除本次追加的章号
            if seq and seq in (existing_item.get("chapters") or []):
                chapters = [x for x in existing_item["chapters"] if x != seq]
                if chapters:
                    existing_item["chapters"] = chapters
                else:
                    _remove_item(working_items, str(existing_item.get("id")))
            changed += 1

        if action == ConflictAction.KEEP_EXISTING:
            if existing_item is not None and incoming_item is not None:
                _merge_chapters(existing_item, incoming_item, seq)
                changed += 1
            resolved_records.append({**c, "action": action, "resolved_at": 0})

        elif action == ConflictAction.TAKE_INCOMING:
            if existing_item is not None:
                old_text = str(existing_item.get("text") or "")
                existing_item["text"] = incoming_text
                existing_item["id"] = item_id(section, incoming_text)
                _merge_chapters(existing_item, incoming_item, seq)
                changed += 1
                resolved_records.append({**c, "action": action, "before_text": old_text})
            else:
                working_items.append({
                    "id": item_id(section, incoming_text), "section": section,
                    "text": incoming_text,
                    "chapters": sorted({int(c.get("incoming_chapter") or 1)}),
                    "hits": 1,
                })
                changed += 1
                resolved_records.append({**c, "action": action})

        elif action == ConflictAction.MERGE_BOTH:
            if existing_item is not None:
                parts = [existing_text]
                if _norm_text(incoming_text) not in _norm_text(existing_text):
                    parts.append(incoming_text)
                existing_item["text"] = "；".join(parts)
                existing_item["id"] = item_id(section, existing_item["text"])
                _merge_chapters(existing_item, incoming_item, seq)
                changed += 1
                resolved_records.append({**c, "action": action})
            else:
                working_items.append({
                    "id": item_id(section, incoming_text), "section": section,
                    "text": incoming_text,
                    "chapters": sorted({int(c.get("incoming_chapter") or 1)}),
                    "hits": 1,
                })
                changed += 1
                resolved_records.append({**c, "action": action})

        elif action == ConflictAction.EDIT:
            final = text.strip()
            if not final:
                raise ConflictResolutionError("手动编辑必须给出文本")
            if existing_item is not None:
                existing_item["text"] = final
                existing_item["id"] = item_id(section, final)
                _merge_chapters(existing_item, incoming_item, seq)
            else:
                working_items.append({
                    "id": item_id(section, final), "section": section, "text": final,
                    "chapters": sorted({int(c.get("incoming_chapter") or 1)}), "hits": 1,
                })
            changed += 1
            resolved_records.append({**c, "action": action, "before_text": existing_text,
                                     "final_text": final})

        else:
            raise ConflictResolutionError(f"未知裁决动作：{action!r}")

        working_conflicts.pop(pos)
        # 裁决后复查：同小节里是否还留着与它相左的条目（例：≤4 行 与 ≤3 行 并存）
        peer_text = text.strip() if (action == ConflictAction.EDIT and text.strip()) else None
        if peer_text is None:
            if action == ConflictAction.KEEP_EXISTING:
                peer_text = existing_text
            elif action == ConflictAction.TAKE_INCOMING:
                peer_text = incoming_text
            else:
                peer_text = existing_text or incoming_text
        clash = next(
            (it for it in working_items
             if it.get("section") == section
             and is_conflict_candidate(str(it.get("text", "")), peer_text)[0]),
            None,
        )
        if clash is not None:
            warnings.append(
                f"「{section}」里同时存在两种说法，写作要求可能打架："
                f"{str(clash.get('text'))[:40]} / {peer_text[:40]}"
            )
    return ConflictResolutionOutcome(
        items=working_items, conflicts=working_conflicts,
        resolved=resolved_records, changed=changed, warnings=warnings,
    )


def undoing_resolution(
    items: list[dict], conflicts: list[dict], record: dict
) -> ConflictResolutionOutcome:
    """撤销一次裁决：条目表与待裁决列表都回到裁决前的样子。"""
    working_items = [dict(it) for it in items]
    section = str(record.get("section") or "")
    existing_text = str(record.get("existing") or "")
    incoming_text = str(record.get("incoming") or "")
    final_text = str(record.get("final_text") or "")
    seq = int(record.get("incoming_chapter") or 1)
    existing_chapters = [int(x) for x in (record.get("existing_chapters") or [])]
    action = str(record.get("action") or "")

    # 1) 还原原条目文本（take_incoming / edit 改过它）
    if existing_text:
        target = _find_item(working_items, section, final_text or existing_text) if final_text \
            else _find_item(working_items, section, existing_text)
        if target is not None:
            target["text"] = existing_text
            target["id"] = item_id(section, existing_text)
            target["chapters"] = sorted(set(existing_chapters))
        else:
            working_items.append({
                "id": item_id(section, existing_text), "section": section,
                "text": existing_text, "chapters": sorted(set(existing_chapters)), "hits": 1,
            })
    # 2) 把新条目加回去
    if incoming_text:
        new_id = str(record.get("incoming_id") or item_id(section, incoming_text))
        if _find_item(working_items, section, incoming_text) is None:
            working_items.append({
                "id": new_id, "section": section, "text": incoming_text,
                "chapters": [seq], "hits": 1,
            })
    # 3) 冲突记录放回待裁决
    restored = {
        "section": section, "existing": existing_text,
        "existing_chapters": existing_chapters,
        "incoming": incoming_text, "incoming_chapter": seq,
        "overlap": record.get("overlap", 0),
        "incoming_id": record.get("incoming_id") or item_id(section, incoming_text),
    }
    working_conflicts = [*conflicts, restored]
    return ConflictResolutionOutcome(
        items=working_items, conflicts=working_conflicts, resolved=[],
        changed=1, warnings=[], undone_action=action,
    )


def normalize_mode(raw: str | None) -> str:
    value = (raw or "").strip().lower()
    if value in VALID_MODES:
        return value
    # 兼容中文/别名输入
    if value in {"style", "只学写法", "文风", "文风学习", "写法", "技法"}:
        return MODE_STYLE
    return MODE_FULL
