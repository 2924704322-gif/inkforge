"""素材库统一服务层：分类条目、来源溯源、跨章去重合并。

为什么要有这一层（用户实测的"功能重叠"）：
- 修复前有**两个半**素材入口 —— 素材库（手写一条条）、全量蒸馏（产出一整篇文章）、
  只学写法（产出条目），而它们都只能通到同一个出口「绑定 → 塞进 custom-skills.md 一段约束文本」。
- 优化后：**素材库是唯一容器**，学习仿写降级为"解析引擎"，只往库里产**带分类**的条目；
  分类决定去向（世界观→worldview、人物→characters、文风→style.md、技法/桥段→custom-skills.md）。

条目字段（存 frontmatter，正文就是素材内容）：
  title / category / source / chapters / hits / origin_novel / created / updated
旧数据兼容：没有 category 的按「其他」显示，没有 source 的按「手工」。
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from src.utils.logger import get_logger

logger = get_logger(__name__)

# ---------- 分类（决定"二开建书"时的落盘去向） ----------

CAT_WORLD = "世界观"
CAT_CHARACTER = "人物"
CAT_PROP = "道具"
CAT_PLACE = "地点"
CAT_PLOT = "桥段"
CAT_TECHNIQUE = "技法"
CAT_STYLE = "文风"
CAT_OTHER = "其他"

CATEGORIES: tuple[str, ...] = (
    CAT_WORLD, CAT_CHARACTER, CAT_PROP, CAT_PLACE, CAT_PLOT,
    CAT_TECHNIQUE, CAT_STYLE, CAT_OTHER,
)

#: 分类 → 落盘去向（二开建书用；None = 不落盘到作品，只留库）
CATEGORY_DEST: dict[str, str] = {
    CAT_WORLD: "settings/worldview",
    CAT_CHARACTER: "settings/characters",
    CAT_PROP: "settings/custom-skills.md",
    CAT_PLACE: "settings/custom-skills.md",
    CAT_PLOT: "settings/custom-skills.md",
    CAT_TECHNIQUE: "settings/custom-skills.md",
    CAT_STYLE: "settings/style.md",
    CAT_OTHER: "settings/custom-skills.md",
}

#: 默认勾选导入的分类。桥段**默认不导入**：它是原著最"像"的部分，
#: 直接当硬设定会诱导正文贴着原著情节走（用户选择 A：提取但默认不导入）。
DEFAULT_SPAWN_CATEGORIES: tuple[str, ...] = (
    CAT_WORLD, CAT_CHARACTER, CAT_PROP, CAT_PLACE, CAT_TECHNIQUE, CAT_STYLE,
)

#: 蒸馏小节名 → 分类（模型不太可能逐条写分类，按小节映射最稳）
SECTION_CATEGORY: dict[str, str] = {
    "世界观": CAT_WORLD,
    "人物": CAT_CHARACTER,
    "角色": CAT_CHARACTER,
    "道具": CAT_PROP,
    "物品": CAT_PROP,
    "地点": CAT_PLACE,      # 与「道具」分开：用户实测混在一起太乱
    "场所": CAT_PLACE,
    "地图": CAT_PLACE,
    "组织": CAT_PROP,
    "势力": CAT_PROP,
    "桥段": CAT_PLOT,
    "剧情技法": CAT_TECHNIQUE,   # 必须比「剧情」长，否则会被判成桥段（实测踩过）
    "剧情学习": CAT_TECHNIQUE,
    "剧情": CAT_PLOT,
    "技法": CAT_TECHNIQUE,
    "文风": CAT_STYLE,
    "负面清单": CAT_TECHNIQUE,
    "禁忌": CAT_TECHNIQUE,
}

#: 文本兜底判类（小节名认不出来时按关键词猜）
_KEYWORD_CATEGORY: tuple[tuple[str, str], ...] = (
    ("世界观", CAT_WORLD), ("体系", CAT_WORLD), ("规则", CAT_WORLD), ("境界", CAT_WORLD),
    ("地理", CAT_WORLD), ("设定", CAT_WORLD),
    ("人物", CAT_CHARACTER), ("角色", CAT_CHARACTER), ("性格", CAT_CHARACTER),
    ("外貌", CAT_CHARACTER), ("弧光", CAT_CHARACTER),
    ("道具", CAT_PROP), ("物品", CAT_PROP), ("组织", CAT_PROP), ("门派", CAT_PROP),
    ("宗门", CAT_PROP), ("势力", CAT_PROP), ("法宝", CAT_PROP), ("灵器", CAT_PROP),
    ("地点", CAT_PLACE), ("场所", CAT_PLACE), ("地图", CAT_PLACE), ("城池", CAT_PLACE),
    ("秘境", CAT_PLACE), ("山脉", CAT_PLACE),
    ("桥段", CAT_PLOT), ("剧情", CAT_PLOT), ("冲突", CAT_PLOT), ("反转", CAT_PLOT),
    ("钩子", CAT_PLOT),
    ("文风", CAT_STYLE), ("叙事", CAT_STYLE), ("句式", CAT_STYLE), ("语域", CAT_STYLE),
    ("标点", CAT_STYLE), ("描写", CAT_STYLE),
    ("技法", CAT_TECHNIQUE), ("禁忌", CAT_TECHNIQUE), ("禁止", CAT_TECHNIQUE),
    ("节奏", CAT_TECHNIQUE),
)

MATERIAL_ID_RE = re.compile(r"^mt-[a-f0-9]{8}$")


def materials_dir(data_root: Path) -> Path:
    d = data_root / "materials"
    d.mkdir(parents=True, exist_ok=True)
    return d


#: 条目里显式分类标签：`[世界观] 修为体系：…` / `【人物】林尘：…`
#: 允许缺右括号（模型有截断习惯）；`_TAG_START_RE` 只认行首（用于取分类），
#: `_TAG_ANY_RE` 匹配任意位置（用于把标签从正文里全部清掉 —— 模型会把两个标签写在同一行）。
_TAG_BODY = r"([^\[\]【】\s]{1,6})\s*[\]】]?"
_TAG_START_RE = re.compile(r"^\s*[\[【]\s*" + _TAG_BODY)
_TAG_ANY_RE = re.compile(r"[\[【]\s*" + _TAG_BODY)
_TAG_ALIASES: dict[str, str] = {
    "世界观": CAT_WORLD, "设定": CAT_WORLD, "世界": CAT_WORLD,
    "人物": CAT_CHARACTER, "角色": CAT_CHARACTER,
    "道具": CAT_PROP, "物品": CAT_PROP, "法宝": CAT_PROP, "灵器": CAT_PROP,
    "组织": CAT_PROP, "势力": CAT_PROP,
    "地点": CAT_PLACE, "场所": CAT_PLACE, "地图": CAT_PLACE, "城池": CAT_PLACE,
    "桥段": CAT_PLOT, "剧情": CAT_PLOT,
    "技法": CAT_TECHNIQUE, "禁忌": CAT_TECHNIQUE,
    "文风": CAT_STYLE, "风格": CAT_STYLE,
    # 合并前的旧分类名：老素材里写着「道具地点」，按道具处理（用户可手动改）
    "道具地点": CAT_PROP,
}

#: 标题分隔符（中英文冒号与破折号）。
#: ⚠ 拼进字符类时必须 `re.escape`：`[：:—-－–]` 里的 `-` 会被当作**区间**，
#: `—`(U+2014) 与 `－`(U+FF0D) 之间的区间把大量汉字也包含进来 ——
#: 实测 `re.split("[：:—-－–]", "修为体系：…")` 会在「修」处切开（真机踩过）。
_TITLE_SEP = "：:—-－–"
_TITLE_SEP_CLASS = "[" + re.escape(_TITLE_SEP) + "]"

#: 手工/墨师新增、没有来源书籍的素材统一挂在这个归属下（按书浏览时不会被漏掉）
NO_SOURCE_BOOK = "无来源（手工）"


def split_tag(text: str) -> tuple[str, str]:
    """拆出条目里显式写的分类标签，返回 (分类或空串, 去掉标签的正文)。

    模型有时会在**同一行连写两个标签**（`[世界观] 修为体系…[世界观] 货币：…`），
    这里把所有标签都清掉，只认第一个作为分类 —— 否则标题会拼成
    「修为体系-炼气-筑基-金丹三层-世界观-货币」这种毛刺（真机实测）。
    """
    text = text or ""
    m = _TAG_START_RE.match(text)
    cat = _TAG_ALIASES.get(m.group(1).strip(), "") if m else ""
    body = _TAG_ANY_RE.sub("", text)
    return cat, body


def title_key(title: str) -> str:
    """标题规范化：去 Markdown 装饰、去空白标点、转小写。"""
    text = re.sub(r"[*`#\s]+", "", title or "")
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", text).lower()


def same_entity(a: str, b: str) -> bool:
    """两个标题是否指向同一实体（用于跨章合并）。

    真机实测：同一人物被模型写成「林尘：主角」与「林尘：主角/新入门弟子」，
    规范化后也不相等，于是同一个人进了素材库两次。除精确相等外，
    再认"一方是另一方的前缀且长度 ≥2"（林尘 vs 林尘主角）。
    """
    ka, kb = title_key(a), title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long = sorted((ka, kb), key=len)
    return len(short) >= 2 and long.startswith(short)


def content_duplicate(a: str, b: str) -> bool:
    """两条**正文**是否在说同一件事（标题不同时的兜底判重）。

    真机实测：「叙事视角为第三人称限制视角」与「…限知视角」正文几乎逐字相同，
    但标题差一个字 → 靠标题判定合不了，素材库里并排两条。
    """
    na, nb = _norm_key(a), _norm_key(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return len(min(na, nb, key=len)) >= 12
    ta, tb = _char_bigrams(na), _char_bigrams(nb)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.55


def title_prefix_len(a: str, b: str) -> int:
    """两个标题规范化后的公共前缀长度（「主线推进方式」vs「主线推进采用…」→ 4）。"""
    ka, kb = title_key(a), title_key(b)
    n = 0
    while n < min(len(ka), len(kb)) and ka[n] == kb[n]:
        n += 1
    return n


def looks_same_topic(a_title: str, a_content: str, b_title: str, b_content: str) -> bool:
    """标题相近 + 正文相近 → 判为同一条（合并时用）。

    两道信号都要过，避免把「世界观·势力格局」和「世界观·经济体系」并掉：
    · 标题公共前缀 ≥ 4 字（这些条目名多为"主线推进…""叙事视角…"这类同主题命名）；
    · 正文近似（bigram Jaccard ≥ 0.3）。
    """
    if title_prefix_len(a_title, b_title) < 4:
        return False
    na, nb = _norm_key(a_content), _norm_key(b_content)
    ta, tb = _char_bigrams(na), _char_bigrams(nb)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.3


def _char_bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def clean_title(raw: str, *, limit: int = 24) -> str:
    """从条目文本里解析出**像名字的**标题。

    真实约束：模型并不总按「名称：内容」写，常带 Markdown 加粗/反引号，或整句叙述。
    早先直接取前 16 字，结果产出「**数字具象化**」「拒绝连接词与过渡句，句与句之间靠」
    这类破碎标题（真机实测）。这里：
    1. 去掉 `**`、反引号、列表符号；
    2. 优先按分隔符切出前半段（≤24 字才认，否则视为整句）；
    3. 整句时取第一个句读之前的部分，再截到上限，并去掉结尾虚词。
    """
    text = _TAG_ANY_RE.sub("", raw or "").strip().lstrip("-•*+ \t")
    text = text.replace("**", "").replace("`", "")
    if not text:
        return ""
    # 先按分隔符/句读切出「名称」段：分隔符（冒号破折号）优先，其次标点
    head = re.split(_TITLE_SEP_CLASS, text, maxsplit=1)[0].strip()
    head = re.sub(r"^\*\*|\*\*$", "", head).strip("　`*")
    # 名称后面常跟括注（「数字具象化（例）」）或句读，都要切掉
    head = re.split(r"[，。；！？,;!?（(]", head, maxsplit=1)[0].strip()
    if 2 <= len(head) <= limit:
        return head
    title = head or text
    if len(title) > limit:
        title = title[:limit]
    return re.sub(r"^[\s·\-—]+|[\s·\-—]+$", "", title)


def categorize(section: str, text: str = "") -> str:
    """把蒸馏小节/条目判到一个分类。

    优先级（真机实测的顺序很关键）：
    1. **条目里显式写的分类标签** `[人物] 林尘：…` —— 模型自己标的类别最准；
    2. 小节名映射（**取最长匹配**：「人物设定点」含「设定」，按声明顺序会先撞上「设定→世界观」）；
    3. 文本关键词兜底。
    """
    tagged, body = split_tag(text)
    if tagged:
        return tagged
    title = (section or "").strip()
    best_key, best_cat = "", CAT_OTHER
    for key, cat in SECTION_CATEGORY.items():
        if key in title and len(key) > len(best_key):
            best_key, best_cat = key, cat
    if best_key:
        return best_cat
    best_key = ""
    for key, cat in _KEYWORD_CATEGORY:
        if key in (body or text) and len(key) > len(best_key):
            best_key, best_cat = key, cat
    return best_cat


def _norm_key(title: str) -> str:
    """同名判定用的规范化键（去空白/标点/大小写）。"""
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (title or "").lower())


#: 重要度取值（放在 Material 之前定义，供字段默认值引用）
IMPORTANCE_MAJOR = "主级"
IMPORTANCE_MINOR = "次级"


#: 一条桥段里出现的"动作迹"：箭头或常见动作动词
_BEAT_MARKERS = ("→", "->", "—>", "击", "夺", "走", "出", "入", "见", "令", "答",
                 "退", "救", "死", "杀", "问", "打", "逃", "应", "转身", "开口",
                 "告知", "点明", "宣告", "离开", "抵达", "发现", "决定", "拒绝", "答应")


def split_beat_lines(text: str) -> list[str]:
    """把一个桥段条目拆成多条（模型常把多个桥段写在同一行，只用 `|` 分隔）。

    不能一律按 `|` 拆 —— 会切坏"衣破剑裂 | 修为定位"这种并列短语。判定规则：
    两侧**都**很短（≤6 字）且都不含动作迹（箭头/动词）时才视为并列，合并成一条；
    否则切开。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    parts = [p.strip(" 　|｜") for p in re.split(r"[|｜]", raw)]
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        if out and len(p) <= 6 and len(out[-1]) <= 6 and not _has_beat_marker(p) \
                and not _has_beat_marker(out[-1]):
            out[-1] = f"{out[-1]} | {p}"      # 并列短语：合并回上一条
        else:
            out.append(p)
    return out


def _has_beat_marker(text: str) -> bool:
    return any(m in text for m in _BEAT_MARKERS)


def importance_of(hits: int, chapters: list[int], text: str = "") -> str:
    """判定素材是不是"反复出现 / 被重点强调"的。

    信号（任一成立即主级）：
    · 跨 ≥2 章出现 —— 后文反复提到的道具/地点才值得进新书；
    · 同一轮里被反复写出（hits ≥ 3）；
    · 设定正文里带强调措辞（"关键/核心/重要/贯穿/主要/标志性"）。

    由来（用户实测）：道具地点里混进大量只出现一次的场面话（石阶、雾气），
    列表默认只显示主级，次级保留可展开查看。
    """
    if len(set(chapters or [])) >= 2:
        return IMPORTANCE_MAJOR
    if int(hits or 1) >= 3:
        return IMPORTANCE_MAJOR
    body = text or ""
    if any(k in body for k in ("关键", "核心", "重要", "贯穿", "主要", "标志性")):
        return IMPORTANCE_MAJOR
    return IMPORTANCE_MINOR


@dataclass
class Material:
    """一条素材（category 决定去向；source_book 决定"按书浏览"时归到哪本书下）。"""

    id: str
    title: str
    content: str
    category: str = CAT_OTHER
    source: str = "手工"
    #: 来源书籍 —— 蒸馏任务名（如「玄天宗样本」）；手工新增的为 NO_SOURCE_BOOK
    source_book: str = NO_SOURCE_BOOK
    chapters: list[int] = field(default_factory=list)
    hits: int = 1
    origin_novel: str = ""
    updated: float = 0.0
    #: 重要度：主级（多章出现 / 反复强调）/ 次级（只在一章出现过一次）
    importance: str = IMPORTANCE_MINOR
    #: 来源样本片段（仅蒸馏产出保留）：桥段按章节序排成"原文剧情线索"要用它
    sample_excerpt: str = ""


def source_book_of(source: str) -> str:
    """从来源串推断归属书籍：`蒸馏:玄天宗样本#第二章` → `玄天宗样本`。

    旧数据没有 source_book 字段，靠这条规则反推，保证"按书浏览"对历史素材也成立。
    """
    text = (source or "").strip()
    if not text or text in ("手工", "墨师"):
        return NO_SOURCE_BOOK
    body = text.split("；")[0]          # 合并过来源的取第一条
    if body.startswith("蒸馏:"):
        body = body[len("蒸馏:"):]
    return body.split("#")[0].strip() or NO_SOURCE_BOOK


def _load(path: Path) -> Material | None:
    try:
        post = frontmatter.load(str(path))
    except Exception as exc:  # noqa: BLE001 - 单条损坏不阻断列表
        logger.warning("素材解析失败，已跳过 %s: %s", path.name, exc)
        return None
    meta = post.metadata or {}
    chapters = [int(c) for c in (meta.get("chapters") or [])
                if str(c).lstrip("-").isdigit()]
    source = str(meta.get("source") or "手工")
    content = post.content.strip()
    return Material(
        id=path.stem,
        title=str(meta.get("title") or path.stem),
        content=content,
        category=str(meta.get("category") or CAT_OTHER),
        source=source,
        source_book=str(meta.get("source_book") or source_book_of(source)),
        chapters=chapters,
        hits=int(meta.get("hits") or 1),
        origin_novel=str(meta.get("origin_novel") or ""),
        updated=float(meta.get("updated") or path.stat().st_mtime),
        # 旧数据没有 importance：按同一套规则现算，保证"只显示主级"对历史素材也成立
        importance=str(meta.get("importance") or importance_of(
            int(meta.get("hits") or 1), chapters, content)),
        sample_excerpt=str(meta.get("sample_excerpt") or ""),
    )


def _dump(path: Path, material: Material) -> None:
    post = frontmatter.Post(
        material.content,
        title=material.title,
        category=material.category,
        source=material.source,
        source_book=material.source_book,
        chapters=sorted(set(material.chapters)),
        hits=material.hits,
        origin_novel=material.origin_novel,
        importance=material.importance,
        sample_excerpt=material.sample_excerpt,
        updated=material.updated or time.time(),
    )
    path.write_text(frontmatter.dumps(post), encoding="utf-8", newline="\n")


def list_materials(
    data_root: Path, *, category: str = "", source_book: str = ""
) -> list[Material]:
    out: list[Material] = []
    for path in sorted(materials_dir(data_root).glob("*.md")):
        m = _load(path)
        if m is None:
            continue
        if category and m.category != category:
            continue
        if source_book and m.source_book != source_book:
            continue
        out.append(m)
    return out


def group_by_book(data_root: Path) -> list[dict]:
    """按**来源书籍**分组（素材库的主浏览维度）。

    每组结构：{book, total, updated, categories: {分类: [素材…]}}。
    书内再按七维分类聚合，供 UI 点击进入"七维模块"详情页。
    """
    grouped: dict[str, dict] = {}
    for m in list_materials(data_root):
        g = grouped.setdefault(m.source_book, {
            "book": m.source_book, "total": 0, "updated": 0.0,
            "categories": {}, "sources": set(),
        })
        g["total"] += 1
        g["updated"] = max(float(g["updated"]), m.updated)
        g["categories"].setdefault(m.category, []).append(m)
        if m.source:
            g["sources"].add(m.source)
    out: list[dict] = []
    for book, g in grouped.items():
        out.append({
            "book": book,
            "total": g["total"],
            "updated": g["updated"],
            "counts": {cat: len(items) for cat, items in g["categories"].items()},
            "chapters": sorted({c for items in g["categories"].values()
                                for m in items for c in m.chapters}),
        })
    # 有章节信息的排前面，其次按更新时间倒序
    out.sort(key=lambda x: (not x["chapters"], -float(x["updated"])))
    return out


def create_material(
    data_root: Path,
    *,
    title: str,
    content: str,
    category: str = CAT_OTHER,
    source: str = "手工",
    origin_novel: str = "",
    source_book: str = "",
    sample_excerpt: str = "",
) -> Material:
    """新建一条素材（手工 / 墨师动作走这里）。"""
    mid = "mt-" + uuid.uuid4().hex[:8]
    material = Material(
        id=mid, title=title.strip(), content=content.strip(),
        category=category if category in CATEGORIES else CAT_OTHER,
        source=source,
        source_book=source_book or source_book_of(source),
        origin_novel=origin_novel, updated=time.time(),
        sample_excerpt=sample_excerpt,
    )
    _dump(materials_dir(data_root) / f"{mid}.md", material)
    return material


def delete_material(data_root: Path, mid: str) -> bool:
    path = materials_dir(data_root) / f"{mid}.md"
    if path.exists():
        path.unlink()
        return True
    return False


@dataclass
class MergeResult:
    added: int = 0
    merged: int = 0
    ids: list[str] = field(default_factory=list)


def merge_material(
    data_root: Path,
    *,
    title: str,
    content: str,
    category: str,
    source: str,
    chapter: int = 0,
    origin_novel: str = "",
    source_book: str = "",
    sample_excerpt: str = "",
) -> tuple[Material, bool]:
    """按（**来源书籍** + 分类 + 同一实体）合并入库：命中则 hits+1、章号并集，否则新建。

    返回 (素材, 是否新建)。三个要点：
    · 合并**限定在同一本书内** —— 不同来源书里的「林尘」是两个人，不能并成一条；
    · 判定用 `same_entity`：精确同名之外还认"一方是另一方前缀"
      （真机实测：同一人物被写成「林尘」与「林尘：主角」会重复入库）；
    · 每次合并后**重算重要度**：跨章出现次数增加会让"次级"升级为"主级"。
    """
    book = source_book or source_book_of(source)
    key = _norm_key(title)
    for existing in list_materials(data_root, category=category, source_book=book):
        if not (same_entity(existing.title, title)
                or _norm_key(existing.title) == key
                or content_duplicate(existing.content, content)
                or looks_same_topic(existing.title, existing.content, title, content)):
            continue
        chapters = set(existing.chapters)
        if chapter:
            chapters.add(int(chapter))
        existing.chapters = sorted(chapters)
        existing.hits += 1
        # 内容更长时替换（后学的通常更完整），否则保留原内容
        if len(content.strip()) > len(existing.content):
            existing.content = content.strip()
        existing.updated = time.time()
        if source and source not in existing.source:
            existing.source = f"{existing.source}；{source}"
        if sample_excerpt and not existing.sample_excerpt:
            existing.sample_excerpt = sample_excerpt
        existing.importance = importance_of(existing.hits, existing.chapters, existing.content)
        _dump(materials_dir(data_root) / f"{existing.id}.md", existing)
        return existing, False
    created = create_material(
        data_root, title=title, content=content, category=category,
        source=source, origin_novel=origin_novel, source_book=book,
        sample_excerpt=sample_excerpt,
    )
    if chapter:
        created.chapters = [int(chapter)]
    created.importance = importance_of(created.hits, created.chapters, created.content)
    _dump(materials_dir(data_root) / f"{created.id}.md", created)
    return created, True


def material_digest(materials: list[Material]) -> str:
    """一组素材的内容指纹（幂等判断：同一批素材重复导入不重复落盘）。"""
    payload = "\n".join(f"{m.category}\x00{_norm_key(m.title)}" for m in materials)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
