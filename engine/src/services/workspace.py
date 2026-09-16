"""工作区上下文（P1）：墨师在"无书"状态下的可用事实源与提示语。

设计要点：
- 工作区**不是一本书**：它没有章节、没有大纲、没有文风指纹。它的上下文是
  "索引"而不是"全文"——书架清单 + 全局资源计数 + 操作引导，命中细节后再由
  动作（doc_read / chapter_read）按需拉取。
- 索引读盘是 O(书数 × 章数) 的 frontmatter 遍历，而墨师每轮对话都可能问一次，
  因此加 **TTL 缓存**；缓存键包含 novels_dir 的 mtime 与书数，任何增删书都会
  使缓存立即失效（不需要重启引擎）。
- 预算独立：``WORKSPACE_BUDGET`` 与书内上下文预算（6000）完全分离，
  不触碰既有的分段保底/截断逻辑。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.services import library
from src.utils.logger import get_logger

logger = get_logger(__name__)

#: 工作区上下文预算（独立于书内 BOOK_CONTEXT_TOTAL_BUDGET=6000）
WORKSPACE_BUDGET = 3000

#: 索引缓存 TTL（秒）：短时间内多轮对话只读一次盘
INDEX_TTL_SECONDS = 5.0


@dataclass
class WorkspaceIndex:
    """工作区索引：书目清单 + 全局资源计数。"""

    books: list[dict] = field(default_factory=list)
    resources: dict[str, int] = field(default_factory=dict)
    error: str = ""


_CACHE: dict = {"at": 0.0, "signature": None, "index": None}


def _novels_signature() -> tuple:
    """书目目录签名：目录 mtime（秒 + 纳秒）+ 大小 + 子目录名。

    为什么要四重（根路径 + mtime 秒 + 纳秒 + 大小 + 子目录名）：
    · 根路径：进程内缓存是全局的，但数据根目录可被环境变量重定向
      （测试隔离 / 多实例），缓存绝不能跨数据根串味；
    · mtime/大小/子目录名：Windows NTFS 的 mtime 粒度较粗，且本目录会被
      并发的哈希账本与建书删书写入扰动，只靠其一都可能"改了却看不出变化"。
    """
    root = library.novels_root()
    if not root.exists():
        return (str(root), 0.0, 0, 0, ())
    try:
        names = tuple(sorted(p.name for p in root.iterdir() if p.is_dir()))
        stat = root.stat()
        return (str(root), stat.st_mtime, stat.st_mtime_ns, stat.st_size, names)
    except OSError:  # 目录被并发删除等
        return (str(root), 0.0, 0, 0, ())


def _count_resources() -> dict[str, int]:
    """全局资源计数（素材 / 技能包 / 自定义约束 / 模型角色）。"""
    from src.config.settings import load_models_config

    counts: dict[str, int] = {}
    data_root = library.novels_root().parent
    for key, name in (("materials", "materials"), ("learning", "learning")):
        d = data_root / name
        counts[key] = len(list(d.glob("*.md"))) if d.exists() else 0
    try:
        counts["custom_skills"] = len(library.list_custom_skills())
    except Exception as exc:  # noqa: BLE001 - 资源计数失败不阻断上下文
        logger.warning("自定义 Skill 计数失败: %s", exc)
        counts["custom_skills"] = 0
    try:
        from src.distillation.skill_store import load_index

        counts["skill_packs"] = len(load_index().get("skills") or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("技能包计数失败: %s", exc)
        counts["skill_packs"] = 0
    try:
        counts["model_roles"] = len(load_models_config().roles)
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型角色计数失败: %s", exc)
        counts["model_roles"] = 0
    return counts


def workspace_index(default_novel: str = "", active: set[str] | None = None,
                    done: set[str] | None = None, *, use_cache: bool = True
                    ) -> WorkspaceIndex:
    """工作区索引（带 TTL 缓存）。

    缓存键 = 目录签名 × 调用参数（默认书 / 活跃书集合 / 已完结集合）：
    不同参数组合绝不复用同一份索引，否则"某本书正在生成中"这类标记会串味。
    """
    signature = (_novels_signature(), default_novel,
                 tuple(sorted(active or ())), tuple(sorted(done or ())))
    now = time.monotonic()
    cached = _CACHE["index"]
    if (
        use_cache
        and isinstance(cached, WorkspaceIndex)
        and _CACHE["signature"] == signature
        and now - float(_CACHE["at"]) < INDEX_TTL_SECONDS
    ):
        return cached

    idx = WorkspaceIndex()
    try:
        idx.books = library.list_books(default_novel, active, done)
    except Exception as exc:  # noqa: BLE001 - 单本损坏不阻断工作区
        logger.exception("工作区书目枚举失败")
        idx.error = f"{type(exc).__name__}: {exc}"
    try:
        idx.resources = _count_resources()
    except Exception as exc:  # noqa: BLE001
        logger.exception("工作区资源计数失败")
        idx.error = idx.error or f"{type(exc).__name__}: {exc}"

    # 组装过程本身可能触碰书目目录（哈希账本、技能索引等），使签名漂移；
    # 因此以**装配后重取的签名**作为缓存键，并把时间戳取为"装配完成时刻"，
    # 保证下一次调用能与它逐字段相等（否则缓存永不命中、每次都重新读盘）。
    settled = (_novels_signature(), default_novel,
               tuple(sorted(active or ())), tuple(sorted(done or ())))
    _CACHE.update({"at": time.monotonic(), "signature": settled, "index": idx})
    if settled != signature:
        logger.debug("工作区索引装配期间目录签名发生变化，已按装配后签名登记缓存")
    return idx


def invalidate() -> None:
    """清空索引缓存（建书/删书后调用，保证下一次读取立即看到变化）。"""
    _CACHE.update({"at": 0.0, "signature": None, "index": None})


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n（注：本段超出 {limit} 字配额，已截断）"


def build_workspace_context(index: WorkspaceIndex | None = None,
                            current_book: str = "") -> str:
    """工作区上下文文本（供墨师 system 注入）。"""
    idx = index or workspace_index()
    parts: list[str] = []

    current = current_book or "（无）"
    parts.append(
        "【工作区】你当前处于**工作区**（不属于任何一本书）。"
        f"活动书目：{current}。"
        "工作区里你可以查看全部书目与全局资源，并在得到用户确认后执行动作"
        "（新建书、切换书目、绑定创作约束、启动/续跑生成、裁决章节等）。"
    )

    if idx.books:
        lines = []
        for b in idx.books:
            flags = []
            if b.get("is_default"):
                flags.append("默认")
            if b.get("active"):
                flags.append("生成中")
            if b.get("interactive"):
                flags.append("互动模式")
            if b.get("finished"):
                flags.append("已完结")
            if b.get("planned"):
                flags.append(f"{b['approved']}/{b['planned']}章")
            elif b.get("chapters"):
                flags.append(f"{b['chapters']}章")
            tail = f"（{'、'.join(flags)}）" if flags else ""
            lines.append(f"- {b['novel_id']}：《{b['title']}》{tail}")
        parts.append("【书目清单】\n" + "\n".join(lines))
    else:
        parts.append("【书目清单】\n（空）书架里还没有任何书。"
                     "若用户想开始创作，可先与他确认书名标识与创作模式，再执行建书动作。")

    res = idx.resources or {}
    parts.append(
        "【全局资源】"
        f"素材 {res.get('materials', 0)} 条；"
        f"蒸馏技能包 {res.get('skill_packs', 0)} 个；"
        f"自定义创作约束 {res.get('custom_skills', 0)} 条；"
        f"模型角色绑定 {res.get('model_roles', 0)} 个。"
    )

    parts.append(
        "【工作区规则】\n"
        "1) 涉及某一本书的细节（章节正文、大纲、设定、伏笔），先用只读动作把它取出来再回答，"
        "不要凭记忆编造；\n"
        "2) 任何写操作（建书/切书/绑定约束/启动或续跑生成/裁决章节/修改模型绑定）都必须先向用户"
        "说明将要做什么并取得确认，不得自行执行；\n"
        "3) 书目不存在或名称不确定时，先列书目再反问用户，不要臆测书名标识。"
    )

    if idx.error:
        parts.append(f"【索引告警】{idx.error}")

    return _clip("\n\n".join(parts), WORKSPACE_BUDGET)
