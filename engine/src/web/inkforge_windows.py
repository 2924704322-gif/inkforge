"""Inkforge 扩展 API（三）：深挖自 DeepWrite 的新功能后端。

- 智能体设置：五阶段智能体的系统提示词覆盖（chat/propose 运行时读取）
- 素材库：可绑定到作品的参考资料条目（合成进 custom-skills.md）
- 学习仿写：两种模式 —— full 全量蒸馏（素材拆解/剧情学习/文风学习）；
  style 只学通用写法（文风指纹/技法模板/负面清单，并做专有名词净化，不含原书内容）
- 数据看板：验收指标 + 评分历史 + 伏笔台账汇总
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import frontmatter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config.settings import get_settings
from src.memory.md_store import MdStore
from src.utils.logger import get_logger

logger = get_logger(__name__)

_NOVEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _overrides_path() -> Path:
    """智能体提示词覆盖文件路径。

    修复（本次冒烟测试发现）：原实现直接 write_text 到 ``data/config/``，
    但该目录在全项目任何位置都不会被创建 → 首次保存智能体提示词恒 500
    （FileNotFoundError），「智能体设置」面板在干净安装上完全不可用。
    """
    d = get_settings().novels_dir.parent / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d / "agent-presets.json"


def _load_overrides() -> dict:
    p = _overrides_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def agent_prompt(agent: str) -> str:
    """供 chat / propose 使用的最终预设提示词（覆盖优先）。

    P1 墨师全域化：具备动作能力的智能体（当前为 master）在**任何情况下**都会拿到
    工作台动作说明——包括用户自定义覆盖了提示词的情形。因此动作块在这里统一追加，
    而不是写进 AGENT_PRESETS 常量（后者会被 override 整段替换掉）。

    2026-09-19（防拒绝覆盖审计）：第零条的"是否已注入"判定改用
    `ensure_constitution()` 的**探针句**（CONSTITUTION 正文里的独特整句），
    不再用 3 字子串"第零条"——后者会被"提到这三个字"的用户提示词骗过而静默跳过注入。
    """
    from src.agents.action_prompt import actions_block_for
    from src.agents.prompt_loader import CONSTITUTION_MARK
    from src.web.inkforge_api import AGENT_PRESETS, CONSTITUTION

    if agent == "chat":
        agent = "master"  # 旧会话兼容
    override = _load_overrides().get(agent, {}).get("prompt", "")
    base = override or AGENT_PRESETS.get(agent, AGENT_PRESETS["master"])["prompt"]
    # 最高刚性指令：无论默认还是用户自定义提示词，运行时一律附加
    if CONSTITUTION_MARK not in base:
        base = base + "\n\n" + CONSTITUTION
    # 动作说明：与 CONSTITUTION 同级，永远附带
    actions = actions_block_for(agent)
    if actions and "【工作台动作清单" not in base:
        base = base + "\n\n" + actions
    return base


class PresetBody(BaseModel):
    prompt: str


class MaterialBody(BaseModel):
    title: str
    content: str
    #: 分类：世界观 / 人物 / 道具地点 / 桥段 / 技法 / 文风 / 其他（决定二开建书落盘去向）
    category: str = "其他"
    #: 归属书籍（素材库按书浏览用）；留空则归到「无来源（手工）」
    source_book: str = ""


class SpawnBookBody(BaseModel):
    """从素材库二开建书。"""

    novel_id: str
    title: str
    #: 精确指定的素材 id（优先级最高）
    material_ids: list[str] = []
    #: 分类项：可写 `世界观`（不限来源书），也可写 `世界观:书A`（限定用书 A 的世界观）
    #: —— 支持"用书 A 的世界观 + 书 B 的人物"这种混搭
    categories: list[str] = []
    #: 创作模式：pipeline（自由创作）/ interactive（互动创作）
    mode: str = "interactive"


class LearningBody(BaseModel):
    title: str
    sample: str
    #: full=全量蒸馏学习（素材+剧情+文风）/ style=只学通用写法（文风+技法+负面清单）
    mode: str = "full"
    #: 本次投喂的章节标签（默认「第一章」）
    label: str = ""


class LearningAppendBody(BaseModel):
    """往既有成果追加一章（累积式蒸馏）。"""

    sample: str
    #: 章节标签（缺省按已有来源自动编号为「第 N 章」）
    label: str = ""
    #: 传了就必须与成果既有模式一致，否则 409
    mode: str = ""
    #: 同一内容重复投喂时强制重算（默认幂等跳过）
    force: bool = False


class ConflictResolveBody(BaseModel):
    """冲突裁决：keep_existing / take_incoming / merge_both / edit / undo。"""

    action: str
    #: 单条裁决时的冲突序号
    index: int | None = None
    #: action=edit 时的最终文本
    text: str = ""
    #: 对同一动作批量处理全部待裁决冲突
    bulk: bool = False
    #: action=undo 时的裁决记录序号（对应 resolved[]）
    undo_index: int | None = None


def register_windows_api(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    def _store(novel: str = "") -> MdStore:
        nid = novel or default_novel
        if not _NOVEL_RE.match(nid):
            raise HTTPException(400, f"非法书名标识：{nid!r}")
        # 本模块 _store 仅服务看板等只读视图 → 不挂 Git
        from src.memory.store_factory import open_store

        return open_store(get_settings().novels_dir / nid, writable=False)

    def _data_dir(name: str) -> Path:
        d = get_settings().novels_dir.parent / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ══════════ 智能体设置（提示词覆盖） ══════════

    @app.get("/api/agent-presets")
    def agent_presets_list() -> JSONResponse:
        from src.web.inkforge_api import AGENT_PRESETS

        overrides = _load_overrides()
        items = []
        for key, preset in AGENT_PRESETS.items():
            items.append(
                {
                    "key": key,
                    "label": preset["label"],
                    "prompt": overrides.get(key, {}).get("prompt", preset["prompt"]),
                    "custom": key in overrides,
                }
            )
        return JSONResponse({"presets": items})

    @app.put("/api/agent-presets/{key}")
    def agent_presets_put(key: str, body: PresetBody) -> JSONResponse:
        from src.web.inkforge_api import AGENT_PRESETS

        if key not in AGENT_PRESETS:
            raise HTTPException(404, f"未知智能体：{key}")
        overrides = _load_overrides()
        overrides.setdefault(key, {})["prompt"] = body.prompt
        _overrides_path().write_text(
            json.dumps(overrides, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return JSONResponse({"ok": True, "key": key})

    @app.delete("/api/agent-presets/{key}")
    def agent_presets_reset(key: str) -> JSONResponse:
        overrides = _load_overrides()
        overrides.pop(key, None)
        _overrides_path().write_text(
            json.dumps(overrides, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return JSONResponse({"ok": True, "key": key, "reset": True})

    # ══════════ 素材库（统一容器：手工 / 墨师 / 蒸馏 三条入口都汇到这里） ══════════

    def _materials_root() -> Path:
        return _learning_dir().parent

    def _unique_rel(rel: str, used: set[str]) -> str:
        """同名素材避免互相覆盖：第二个起加 -2/-3 后缀。"""
        if rel not in used:
            used.add(rel)
            return rel
        stem, _, ext = rel.rpartition(".")
        n = 2
        while f"{stem}-{n}.{ext}" in used:
            n += 1
        final = f"{stem}-{n}.{ext}"
        used.add(final)
        return final

    def _material_view(m) -> dict:
        return {
            "id": m.id, "title": m.title, "content": m.content,
            "category": m.category, "source": m.source,
            "source_book": m.source_book,
            "chapters": m.chapters, "hits": m.hits, "updated": m.updated,
            # 重要度：列表默认只显示"主级"（反复出现/被强调过的）
            "importance": m.importance,
        }

    @app.get("/api/materials")
    def materials_list(category: str = "", book: str = "") -> JSONResponse:
        """素材列表（**按来源书籍**为主浏览维度）。

        · 不传参 → 返回按书分组的书籍列表（素材库第一屏）；
        · `book=书名` → 返回该书的素材并按七维分类聚合（详情页）；
        · `category=` → 全局按分类筛选（兼容旧用法）。
        """
        from src.services import materials as materials_svc

        root = _materials_root()
        if book:
            items = materials_svc.list_materials(root, source_book=book)
            by_cat: dict[str, list[dict]] = {}
            for m in items:
                by_cat.setdefault(m.category, []).append(_material_view(m))
            return JSONResponse({
                "book": book,
                "total": len(items),
                "categories": [c for c in materials_svc.CATEGORIES if c in by_cat],
                "all_categories": list(materials_svc.CATEGORIES),
                "by_category": by_cat,
                "materials": [_material_view(m) for m in items],
            })
        items = materials_svc.list_materials(root, category=category.strip())
        return JSONResponse({
            "materials": [_material_view(m) for m in items],
            "books": materials_svc.group_by_book(root),
            "categories": list(materials_svc.CATEGORIES),
        })

    @app.post("/api/materials")
    def materials_create(body: MaterialBody) -> JSONResponse:
        from src.services import materials as materials_svc

        if not body.title.strip() or not body.content.strip():
            raise HTTPException(400, "素材名称与内容不能为空")
        category = body.category.strip() or materials_svc.CAT_OTHER
        if category not in materials_svc.CATEGORIES:
            raise HTTPException(400, f"未知分类：{category}（可选：{'/'.join(materials_svc.CATEGORIES)}）")
        m = materials_svc.create_material(
            _materials_root(), title=body.title, content=body.content,
            category=category, source="手工",
            source_book=body.source_book.strip() or materials_svc.NO_SOURCE_BOOK,
        )
        return JSONResponse({"ok": True, "id": m.id, "title": m.title, "category": m.category})

    @app.post("/api/materials/spawn-book")
    def materials_spawn_book(body: SpawnBookBody) -> JSONResponse:
        """从素材库**二开建书**：把素材按分类落到新书 settings/，建好即可直接开写。

        落盘规则（沿用 Architect.save_demo 的既有格式，保证生成链路认得）：
          世界观   → settings/worldview/<标题>.md
          人物     → settings/characters/<标题>.md
          桥段     → settings/custom-skills.md（标为"参考素材"，默认不导入）
          技法/其他 → settings/custom-skills.md
          文风     → settings/style.md
        幂等：同一本书重复导入同一批素材时，按（分类+标题）同名合并，不产生重复条目。
        """
        from src.memory.md_store import slugify
        from src.memory.store_factory import open_store
        from src.services import library as library_svc
        from src.services import materials as materials_svc

        novel_id = body.novel_id.strip()
        title = body.title.strip() or novel_id
        if not title:
            raise HTTPException(400, "请填写新书书名")
        # 标识缺省/非法一律**由书名派生**（§5.A4）：二开建书是"给新书起个名"的轻量动作，
        # 用户填的多半是中文书名；原先非 ASCII 标识直接 400，等于逼用户先想英文目录名。
        if not library_svc.NOVEL_ID_RE.match(novel_id):
            taken = {b["novel_id"] for b in library_svc.list_books()}
            novel_id = library_svc.derive_novel_id(title, taken)
            logger.info("二开建书：标识缺失/非法，按书名派生 novel_id=%s（title=%r）",
                        novel_id, title)
        mode = body.mode if body.mode in ("pipeline", "interactive") else "interactive"
        try:
            # title 必须落盘：修复前它算出来只进了回执，书架只能回落目录名（用户实测"书名消失"）
            library_svc.create_book(novel_id, mode, title=title)
        except library_svc.LibraryError as exc:
            raise HTTPException(409 if exc.code == "conflict" else 400, exc.message) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        # 来源书籍限定：`categories` 里给的是 `分类:书名`（如 "人物:书B"），
        # 支持"用书 A 的世界观 + 书 B 的人物"这种混搭；只给分类名则不限书。
        # 显式写成 `分类:书名` 的条目**同时**把该分类加入可选集合 —— 否则那些人会被
        # `m.category not in wanted_cats` 直接筛掉，导入结果为 0（实测踩过）。
        cat_book: dict[str, str] = {}
        wanted_cats: set[str] = set()
        for raw in body.categories:
            text = (raw or "").strip()
            if not text:
                continue
            if ":" in text or "：" in text:
                cat, _, bk = text.replace("：", ":").partition(":")
                cat, bk = cat.strip(), bk.strip()
                if cat in materials_svc.CATEGORIES and bk:
                    cat_book[cat] = bk
                    wanted_cats.add(cat)
                else:
                    wanted_cats.add(cat or text)
            else:
                wanted_cats.add(text)
        if not wanted_cats and not body.material_ids:
            # 既没给分类也没给具体素材 → 才用默认分类；只给了 material_ids 时
            # **不要**再套默认分类，否则会连带导入一堆没勾选的素材（真机踩过：
            # 只勾了 2 条桥段，结果 import 了 10 条全分类素材）。
            wanted_cats = set(materials_svc.DEFAULT_SPAWN_CATEGORIES)
        wanted_cats = {c for c in wanted_cats if c in materials_svc.CATEGORIES}
        all_items = materials_svc.list_materials(_materials_root())
        explicit = set(body.material_ids)
        picked = []
        for m in all_items:
            if m.id in explicit:
                picked.append(m)
                continue
            if m.category not in wanted_cats:
                continue
            limit_book = cat_book.get(m.category)
            if limit_book and m.source_book != limit_book:
                continue
            picked.append(m)
        if not picked:
            return JSONResponse({
                "ok": True, "novel_id": novel_id, "imported": 0, "skipped": 0,
                "summary": "没有匹配的素材可导入（新书已建好，可先去素材库勾选）",
            })

        store = open_store(library_svc.novel_dir(novel_id), writable=True)
        counts: dict[str, int] = {}
        by_book: dict[str, int] = {}
        used_titles: set[str] = set()
        constraint_blocks: list[str] = []
        style_blocks: list[str] = []
        for m in picked:
            cat = m.category
            counts[cat] = counts.get(cat, 0) + 1
            # 来源书可能为空（手工素材 / 旧数据）：归一到 NO_SOURCE_BOOK，
            # 否则 by_book 里会出现 None 键，回执 JSON 序列化不安全。
            src_book = (m.source_book or "").strip() or materials_svc.NO_SOURCE_BOOK
            by_book[src_book] = by_book.get(src_book, 0) + 1
            if cat == materials_svc.CAT_STYLE:
                style_blocks.append(f"- 【{m.title}】{m.content}")
            elif cat in (materials_svc.CAT_TECHNIQUE, materials_svc.CAT_PLOT,
                         materials_svc.CAT_OTHER):
                label = "参考素材（写法参考，不得逐字复现）" if cat == materials_svc.CAT_PLOT \
                    else "创作约束"
                constraint_blocks.append(f"## {label}：{m.title}\n\n{m.content}")
            elif cat == materials_svc.CAT_CHARACTER:
                rel = _unique_rel(f"settings/characters/{slugify(m.title)}.md", used_titles)
                store.write(
                    rel,
                    f"# {m.title}\n\n## 设定\n{m.content}\n",
                    metadata={"title": m.title, "source": m.source,
                              "chapters": m.chapters,
                              "imported_from": "素材库二开"},
                    commit_message=f"二开导入人物：{m.title}",
                )
            else:  # 世界观 / 道具地点
                rel = _unique_rel(f"settings/worldview/{slugify(m.title)}.md", used_titles)
                store.write(
                    rel,
                    f"{m.content}\n",
                    metadata={"title": m.title, "category": cat, "source": m.source,
                              "chapters": m.chapters,
                              "imported_from": "素材库二开"},
                    commit_message=f"二开导入设定：{m.title}",
                )
        if style_blocks:
            store.write(
                "settings/style.md",
                "# 文风指纹（素材库导入）\n\n" + "\n".join(style_blocks),
                metadata={"title": "文风指纹", "imported_from": "素材库二开"},
                commit_message="二开导入文风指纹",
            )
        if constraint_blocks:
            store.write(
                "settings/custom-skills.md",
                "# 创作约束（素材库导入，最高优先级）\n\n" + "\n\n".join(constraint_blocks),
                metadata={"title": "创作约束", "imported_from": "素材库二开"},
                commit_message="二开导入创作约束/参考素材",
            )
        # 选了桥段 → 必须能"接着原文往下写"：按**章节顺序**把桥段与原文片段拼成
        # 一份续写线索（用户要求：桥段要跟原文后面推进，且按顺序排列）。
        plot_items = [m for m in picked if m.category == materials_svc.CAT_PLOT]
        if plot_items:
            guide = _build_plot_guide(plot_items)
            store.write(
                "settings/plot-continuation.md",
                guide,
                metadata={"title": "原文剧情线索（续写起点）",
                          "imported_from": "素材库二开",
                          "plot_count": len(plot_items)},
                commit_message="二开导入原文剧情线索",
            )
        # 书内设定落盘后立刻让书架/资源树看到
        from src.services import workspace as workspace_svc

        workspace_svc.invalidate()
        logger.info("二开建书 %s：导入素材 %d 条 %s（来源 %s）",
                    novel_id, len(picked), counts, by_book)
        src_desc = "、".join(f"{bk} {n} 条" for bk, n in by_book.items())
        return JSONResponse({
            "ok": True,
            "novel_id": novel_id,
            "title": title,
            "imported": len(picked),
            "by_category": counts,
            "by_book": by_book,
            "summary": "已建书并导入：" + "、".join(f"{k} {v} 条" for k, v in counts.items()),
            "source_summary": f"素材来源：{src_desc}",
        })

    @app.delete("/api/materials/{mid}")
    def materials_delete(mid: str) -> JSONResponse:
        from src.services import materials as materials_svc

        if not materials_svc.MATERIAL_ID_RE.match(mid):
            raise HTTPException(400, "非法素材 ID")
        materials_svc.delete_material(_materials_root(), mid)
        return JSONResponse({"ok": True})

    # ══════════ 学习仿写（三阶段） ══════════

    def _learning_dir() -> Path:
        return _data_dir("learning")

    @app.get("/api/learning")
    def learning_list() -> JSONResponse:
        items = []
        for path in sorted(_learning_dir().glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
            post = frontmatter.load(str(path))
            sources = post.metadata.get("sources") or []
            items.append(
                {
                    "id": path.stem,
                    "title": str(post.metadata.get("title") or path.stem),
                    "created": path.stat().st_mtime,
                    # 两种模式在列表里必须分得清（用户看得见"这条学的是什么"）
                    "mode": str(post.metadata.get("mode") or "full"),
                    "mode_label": str(post.metadata.get("mode_label") or "全量蒸馏学习"),
                    "scrubbed": int(post.metadata.get("scrubbed") or 0),
                    # 累积式蒸馏：列表要显示"学过几章、多少条、有没有待裁决冲突"
                    "sources": len(sources) if isinstance(sources, list) else 1,
                    "items": len(post.metadata.get("items") or []),
                    "materials": len(post.metadata.get("materials") or []),
                    "updated": float(post.metadata.get("updated") or path.stat().st_mtime),
                    "conflicts": len(post.metadata.get("conflicts") or []),
                }
            )
        return JSONResponse({"items": items})

    @app.get("/api/learning/{lid}")
    def learning_get(lid: str) -> JSONResponse:
        path = _learning_dir() / f"{lid}.md"
        if not re.match(r"^ln-[a-f0-9]{8}$", lid) or not path.exists():
            raise HTTPException(404, "学习成果不存在")
        from src.services import learning as learning_svc

        post = frontmatter.load(str(path))
        doc = learning_svc.parse_stored_document(post.metadata, post.content)
        return JSONResponse({
            "id": lid,
            "title": post.metadata.get("title", ""),
            "mode": str(post.metadata.get("mode") or "full"),
            "mode_label": str(post.metadata.get("mode_label") or "全量蒸馏学习"),
            "content": post.content,
            # 累积结构：来源章节 / 条目表（带溯源）/ 待裁决冲突 / 蒸馏日志
            "sources": doc["sources"],
            "items": doc["items"],
            # 蒸馏产出并入库的素材引用（标题 + 分类），供 UI 追溯"这条任务产出了什么"
            "materials": list(post.metadata.get("materials") or []),
            "conflicts": doc["conflicts"],
            # 已裁决历史（供「撤销」）
            "resolved": list(post.metadata.get("resolved") or []),
            "changelog": doc["changelog"],
            "legacy": doc["legacy"],
        })

    @app.delete("/api/learning/{lid}")
    def learning_delete(lid: str) -> JSONResponse:
        path = _learning_dir() / f"{lid}.md"
        if path.exists():
            path.unlink()
        return JSONResponse({"ok": True})

    # ══════════ 学习仿写（累积式蒸馏：一条成果可多次投喂） ══════════

    def _distill_sample(
        mode: str, sample: str, known_items: list[str] | None = None, seq: int = 1
    ) -> tuple[list[dict], list[dict], dict[str, str], list[str]]:
        """跑一轮分析：返回 (写法成果条目, 素材条目, 净化替换表, 泄漏词)。

        full 模式的**素材小节**（世界观/人物/道具/地点/桥段）产出的是素材条目，
        交给素材库统一入库；**写法小节**（剧情技法/文风）才是写法成果条目。
        这样"学习仿写"不再自己存一份知识卡片（与素材库重叠），只做解析。

        路由判据以 `learning.MATERIAL_SECTIONS` 的白名单为准（2026-09-19 真机审计修正）：
        原先用 `materials.categorize(label) != CAT_OTHER` 判定，而技法/文风本身也是
        合法的素材分类 → 判据恒真、写法条目永远为空（成果落成「条目 0 条」）。

        known_items 非空 = 追加轮：把已学条目回灌给模型，要求只报新增/修正，
        否则模型会把同一批写法换措辞重列（实测两章堆到 244 条）。
        seq = 本章章号；**必须传给条目解析**，否则追加进来的条目也会被标成第 1 章
        （实测踩过：hits 涨了但 chapters 永远是 [1]，溯源全错）。
        """
        from src.agents.prompt_loader import ensure_constitution
        from src.config.settings import load_models_config
        from src.llm.base import ChatMessage
        from src.llm.registry import ModelRegistry
        from src.services import learning as learning_svc

        registry = ModelRegistry(load_models_config())
        candidates = (
            learning_svc.proper_noun_candidates(sample) if mode == learning_svc.MODE_STYLE else []
        )
        # 第零条（无条件执行）必须覆盖这条通路：否则"把这段拆了学写法"可能被模型自行打折
        system = ensure_constitution(learning_svc.system_for(mode))
        replaced: dict[str, str] = {}
        leak: list[str] = []
        reports: list[tuple[str, str]] = []
        prompts = learning_svc.build_stage_prompts(mode, known_items)
        sections = [label for label, _ask in prompts]
        for label, ask in prompts:
            messages = [
                ChatMessage(role="system", content=system),
                ChatMessage(role="user", content=f"{ask}\n\n【样本文本】\n{sample}"),
            ]
            content = registry.chat_as("chat", messages, temperature=0.4).content.strip()
            if mode == learning_svc.MODE_STYLE and content:
                scrubbed = learning_svc.scrub_proper_nouns(content, candidates)
                content = scrubbed.text
                replaced.update(scrubbed.replaced)
                leak.extend(scrubbed.leak_terms)
            reports.append((label, content))

        items: list[dict] = []
        material_items: list[dict] = []
        for label, text in reports:
            # 只取"属于本阶段"的那一段再解析：模型经常把多节写在一段回复里，
            # 直接整段解析会把别节的内容也算进本阶段（实测 1 条变 3 条、并刷出假冲突）。
            chunk = learning_svc.extract_stage_chunk(text, label, sections)
            report = f"## {label}\n\n{chunk.strip()}"
            parsed = learning_svc.parse_report_into_items(report, sections=[label], seq=seq)
            for it in parsed:
                it["section"] = label
            # full 模式：**所有小节都产出素材**（世界观/人物/道具/地点/桥段/技法/文风）。
            # 曾经只让"能映射到分类的小节"产素材，结果技法与文风两边都不落地、
            # 凭空消失（用户实测发现）。素材库是唯一容器，不再有第二种出口。
            if mode == learning_svc.MODE_FULL:
                material_items.extend(parsed)
            else:
                items.extend(parsed)
        return items, material_items, replaced, leak

    def _split_material_title(text: str, section: str) -> tuple[str, str, str]:
        """拆解素材条目 → (标题, 正文, 显式分类或空串)。

        模型输出可能是 `[人物] 林尘：外门弟子…`、`**周长老**：…`，也可能是一整句话；
        统一交给 materials.split_tag / clean_title 处理（早先直接截前 16 字，
        产出过「**数字具象化**」「拒绝连接词与过渡句，句与句之间靠」这类破碎标题）。
        """
        from src.services import materials as materials_svc

        tagged, body = materials_svc.split_tag(str(text or ""))
        raw = body.strip().lstrip("-•*+ \t").strip()
        title = materials_svc.clean_title(raw) or section
        sep = re.search(materials_svc._TITLE_SEP_CLASS, raw)  # noqa: SLF001 - 同模块常量
        content = raw[sep.end():].strip() if sep else raw
        return title, (content or raw), tagged

    def _material_refs(material_items: list[dict]) -> list[dict]:
        """把素材条目整理成任务里的引用清单（标题 + 分类），供 UI 追溯来源。"""
        from src.services import materials as materials_svc

        refs: list[dict] = []
        for it in material_items:
            raw = str(it.get("text", ""))
            title, _body, tagged = _split_material_title(raw, str(it.get("section", "")))
            refs.append({
                "title": title,
                "category": tagged or materials_svc.categorize(str(it.get("section", "")), raw),
            })
        return refs

    def _store_material_items(
        material_items: list[dict],
        *,
        chapter: int,
        label: str,
        title: str,
        sample: str = "",
    ) -> tuple[int, int]:
        """把蒸馏出的素材条目并入素材库：返回 (新建数, 合并数)。

        `sample` 会截一段存进素材（`sample_excerpt`）—— 桥段要靠它按章节序拼成
        "原文剧情线索"，二开建书时才知道剧情推进到哪儿。
        """
        from src.services import materials as materials_svc

        data_root = _learning_dir().parent
        excerpt = (sample or "").strip()[:400]
        created = merged = 0
        for it in material_items:
            text = str(it.get("text", "")).strip()
            section = str(it.get("section", ""))
            if not text:
                continue
            item_title, body, tagged = _split_material_title(text, section)
            category = tagged or materials_svc.categorize(section, text)
            # 桥段：模型常把多个桥段塞在一行（用 | 分隔），拆成多条入库，
            # 否则"按序排列"就只有一个条目、排不出先后（真机踩过）。
            texts = materials_svc.split_beat_lines(text) \
                if category == materials_svc.CAT_PLOT else [text]
            for raw in texts:
                if category == materials_svc.CAT_PLOT:
                    item_title, body, _ = _split_material_title(raw, section)
                    body = body or raw
                    item_title = item_title or section
                _, is_new = materials_svc.merge_material(
                    data_root,
                    title=item_title,
                    content=body or text,
                    category=category,
                    source=f"蒸馏:{title}#{label}",
                    chapter=chapter,
                    sample_excerpt=excerpt,
                )
                if is_new:
                    created += 1
                else:
                    merged += 1
        return created, merged

    def _build_plot_guide(plot_items: list) -> str:
        """把桥段素材按**章节顺序**拼成"原文剧情线索 + 续写指令"。

        用户要求：选了桥段就必须跟原文后面推进剧情，且桥段按顺序排列。
        因此这里做三件事：
        1. 按 `chapters[0]` 排序（同章内保持素材库里的先后顺序）；
        2. 每章附上该章的样本片段，让 Writer 知道"故事推进到哪里了"；
        3. 明确写出续写规则：新章从最后一条桥段之后接着写，不得重演已发生的桥段。
        """
        ordered = sorted(
            plot_items,
            key=lambda m: (min(m.chapters) if m.chapters else 10**6, m.title),
        )
        lines = [
            "# 原文剧情线索（续写起点）",
            "",
            "> 本书由素材库「二开建书」导入。以下桥段**按原文章节顺序**排列，"
            "是前作已经发生过的剧情；本作从最后一条之后接着往下写。",
            "",
            "## 续写规则（刚性）",
            "1. 新章剧情必须**承接**最后一条桥段之后的时间线与人物状态，不得倒退、不得重演；",
            "2. 桥段只提供「发生了什么事」，具体场景、对话与细节由你新写，"
            "**禁止逐字复现**原文；",
            "3. 若与其它设定（世界观/人物）冲突，以本线索的时间顺序为准。",
            "",
            "## 桥段顺序",
        ]
        last_chapter = None
        for i, m in enumerate(ordered, 1):
            chapter = min(m.chapters) if m.chapters else 0
            if chapter != last_chapter:
                lines.append("")
                lines.append(f"### 原第 {chapter} 章" if chapter else "### 未标章节")
                last_chapter = chapter
            lines.append(f"{i}. 【{m.title}】{m.content}")
        excerpts = []
        seen = set()
        for m in ordered:
            chapter = min(m.chapters) if m.chapters else 0
            if m.sample_excerpt and chapter not in seen:
                seen.add(chapter)
                excerpts.append(
                    f"### 原第 {chapter} 章 片段\n\n<details><summary>展开原文片段"
                    f"（仅供衔接，禁止照抄）</summary>\n\n{m.sample_excerpt}\n\n</details>"
                )
        if excerpts:
            lines += ["", "## 原文片段（仅供衔接参考，禁止照抄）", "", *excerpts]
        return "\n".join(lines)

    def _write_learning_doc(
        path: Path, meta: dict, items: list[dict], sample: str
    ) -> None:
        """落盘成果：元数据（条目表/来源/冲突/日志）+ 由条目渲染的正文。

        正文由条目渲染而不是保留原始产出 —— 保证"人看到的内容"与"生效的条目表"永远一致；
        原始样本按章节摘要存进元数据（可复查、可重算），全文只在总量受限时保留。
        """
        from src.services import learning as learning_svc

        body_parts = [
            f"# {meta.get('mode_label')}：{meta.get('title')}",
            f"> 来源 {len(meta.get('sources') or [])} 章 · "
            f"产出素材 {len(meta.get('materials') or [])} 条 · "
            f"更新于 {time.strftime('%Y-%m-%d %H:%M')}",
        ]
        if meta.get("mode") == learning_svc.MODE_FULL:
            body_parts.append(
                "> 本任务的产出**全部进了素材库**（按分类可直接用于「二开建书」或绑定作品）；"
                "这里保留任务记录：来源章节、产出清单与蒸馏日志。"
            )
        if meta.get("mode") == learning_svc.MODE_STYLE:
            body_parts.append(
                "> 本产出**只含可迁移的通用写法**（文风/技法/禁忌），已做专有名词净化，"
                "不含原书设定、人物、剧情与桥段。"
            )
        if meta.get("purify_note"):
            body_parts.append(f"> **净化记录**（最近一轮）：{meta['purify_note']}")
        body_parts.append(
            "> 条目前的 `〔…〕` 是来源章号；被多章同时印证过的条目更可信（正文可整体"
            "作为文风指纹使用，标记不影响阅读）。"
        )
        body_parts.append(learning_svc.render_items_body(items))
        log = meta.get("changelog") or []
        if log:
            body_parts.append("## 蒸馏日志\n\n" + "\n".join(f"- {line}" for line in log))
        post = frontmatter.Post("\n\n".join(body_parts), **meta)
        path.write_text(frontmatter.dumps(post), encoding="utf-8", newline="\n")

    @app.post("/api/learning")
    def learning_run(body: LearningBody) -> JSONResponse:
        """新建一条蒸馏任务：full=提取素材 / style=提取写法（含专名净化）。

        full 产出以**素材条目**为主，直接并入素材库（分类：世界观/人物/道具地点/桥段），
        其写法类小节（剧情技法/文风）留在任务里；style 产出全部是写法条目。
        任务本身就是"来源记录 + 追加进度"，因此可继续 `/{lid}/append` 追加章节。
        """
        from src.services import learning as learning_svc

        sample = body.sample.strip()
        if len(sample) < 200:
            raise HTTPException(400, "样本正文太短（至少 200 字），请粘贴更完整的章节/片段")
        mode = learning_svc.normalize_mode(body.mode)
        items, material_items, replaced, leak = _distill_sample(mode, sample)
        label = (body.label or "第一章").strip()
        now = time.time()
        created_materials, merged_materials = _store_material_items(
            material_items, chapter=1, label=label, title=body.title.strip(), sample=sample
        )
        meta: dict = {
            "schema": learning_svc.SCHEMA_VERSION,
            "title": body.title.strip(),
            "mode": mode,
            "mode_label": learning_svc.mode_label(mode),
            "created": now,
            "updated": now,
            "scrubbed": len(replaced),
            "sources": [{
                "seq": 1, "label": label, "chars": len(sample),
                "sha256": hashlib.sha256(sample.encode("utf-8")).hexdigest(),
                "distilled_at": now,
            }],
            "items": items,
            "materials": _material_refs(material_items),
            "conflicts": [],
            "changelog": [
                f"{time.strftime('%Y-%m-%d %H:%M')} 初次蒸馏「{label}」："
                f"素材 {len(material_items)} 条（新建 {created_materials} / 合并 {merged_materials}）"
                + (f"，写法 {len(items)} 条" if items else "")
            ],
        }
        if mode == learning_svc.MODE_STYLE and replaced:
            meta["purify_note"] = "、".join(f"`{k}`→{v}" for k, v in sorted(replaced.items()))
        lid = "ln-" + uuid.uuid4().hex[:8]
        _write_learning_doc(_learning_dir() / f"{lid}.md", meta, items, sample)
        return JSONResponse({
            "ok": True,
            "id": lid,
            "title": body.title.strip(),
            "mode": mode,
            "mode_label": learning_svc.mode_label(mode),
            "scrubbed": len(replaced),
            "leak_terms": sorted(set(leak)),
            "items": len(items),
            "materials": len(material_items),
            "materials_created": created_materials,
            "materials_merged": merged_materials,
            "sources": 1,
        })

    @app.post("/api/learning/{lid}/conflicts/resolve")
    def learning_resolve_conflict(lid: str, body: ConflictResolveBody) -> JSONResponse:
        """人工裁决待裁决冲突（确定性落库，不再调用模型）。

        由来（用户实测）：冲突只被列出来却没有任何处理入口 —— 两条相左的说法会一起进
        Writer 提示词（"段落≤4行" 与 "段落≤3行"），必然打架。这里给出四个动作 + 撤销：
        keep_existing / take_incoming / merge_both / edit，以及 undo 回滚。
        """
        from src.services import learning as learning_svc

        path = _learning_dir() / f"{lid}.md"
        if not re.match(r"^ln-[a-f0-9]{8}$", lid) or not path.exists():
            raise HTTPException(404, "学习成果不存在")
        post = frontmatter.load(str(path))
        doc = learning_svc.parse_stored_document(post.metadata, post.content)
        resolved_log = list(post.metadata.get("resolved") or [])
        now = time.time()
        stamp = time.strftime("%Y-%m-%d %H:%M")

        if body.action == learning_svc.ConflictAction.UNDO:
            if body.undo_index is None or not (0 <= body.undo_index < len(resolved_log)):
                raise HTTPException(400, f"撤销序号越界：{body.undo_index}（共 {len(resolved_log)} 条裁决）")
            record = resolved_log[body.undo_index]
            outcome = learning_svc.undoing_resolution(doc["items"], doc["conflicts"], record)
            resolved_log = [r for i, r in enumerate(resolved_log) if i != body.undo_index]
            summary = f"已撤销一次裁决（{record.get('action')}），冲突回到待裁决"
            log_line = f"{stamp} 撤销裁决：{record.get('action')}（{str(record.get('section'))}）"
        else:
            if body.action not in learning_svc.VALID_CONFLICT_ACTIONS:
                raise HTTPException(
                    400,
                    f"未知裁决动作：{body.action}（可选："
                    f"{'/'.join(learning_svc.VALID_CONFLICT_ACTIONS)}）",
                )
            if not doc["conflicts"]:
                return JSONResponse({
                    "ok": True, "resolved": 0, "remaining": 0, "changed": 0,
                    "warnings": [], "summary": "当前没有待裁决冲突",
                })
            try:
                outcome = learning_svc.apply_conflict_resolution(
                    doc["items"], doc["conflicts"],
                    index=body.index, action=body.action, text=body.text, bulk=body.bulk,
                )
            except learning_svc.ConflictResolutionError as exc:
                raise HTTPException(400, str(exc)) from exc
            for rec in outcome.resolved:
                resolved_log.append({**rec, "resolved_at": now})
            label = {
                learning_svc.ConflictAction.KEEP_EXISTING: "保留原有",
                learning_svc.ConflictAction.TAKE_INCOMING: "采纳新增",
                learning_svc.ConflictAction.MERGE_BOTH: "合并两条",
                learning_svc.ConflictAction.EDIT: "手动编辑",
            }[body.action]
            summary = (
                f"已裁决 {len(outcome.resolved)} 条冲突（{label}），"
                f"剩余 {len(outcome.conflicts)} 条"
            )
            log_line = f"{stamp} 裁决冲突 {len(outcome.resolved)} 条：{label}"

        meta = {
            **post.metadata,
            "items": outcome.items,
            "conflicts": outcome.conflicts,
            "resolved": resolved_log,
            "updated": now,
            "changelog": [*doc["changelog"], log_line],
        }
        _write_learning_doc(path, meta, outcome.items, "")
        return JSONResponse({
            "ok": True,
            "resolved": len(outcome.resolved),
            "remaining": len(outcome.conflicts),
            "changed": outcome.changed,
            "warnings": outcome.warnings,
            "resolved_total": len(resolved_log),
            "summary": summary,
        })

    @app.post("/api/learning/{lid}/append")
    def learning_append(lid: str, body: LearningAppendBody) -> JSONResponse:
        """往既有成果里**追加一章**（累积式蒸馏）。

        · 幂等：同一段内容（sha256）已蒸馏过 → already=true，不重复烧模型（force 可强制重算）；
        · 模式保护：往 style 成果里追 full（或反之）→ 409，两种产出结构不同，混入只会互相污染；
        · 冲突：同小节"主题相同、说法不同"的条目进 `待裁决冲突`，不静默覆盖任何一方。
        """
        from src.services import learning as learning_svc

        path = _learning_dir() / f"{lid}.md"
        if not re.match(r"^ln-[a-f0-9]{8}$", lid) or not path.exists():
            raise HTTPException(404, "学习成果不存在")
        sample = body.sample.strip()
        if len(sample) < 200:
            raise HTTPException(400, "样本正文太短（至少 200 字）")
        post = frontmatter.load(str(path))
        mode = str(post.metadata.get("mode") or "full")
        if body.mode and learning_svc.normalize_mode(body.mode) != mode:
            raise HTTPException(
                409,
                f"这条成果是「{learning_svc.mode_label(mode)}」，不能混入"
                f"「{learning_svc.mode_label(learning_svc.normalize_mode(body.mode))}」的内容；"
                "换学习方式请新建一条成果",
            )
        doc = learning_svc.parse_stored_document(post.metadata, post.content)
        digest = hashlib.sha256(sample.encode("utf-8")).hexdigest()
        if not body.force and any(s.get("sha256") == digest for s in doc["sources"]):
            return JSONResponse({
                "ok": True, "already": True, "id": lid,
                "summary": "这段内容已经蒸馏过了（按内容哈希判定），未重复计入",
                "sources": len(doc["sources"]), "items": len(doc["items"]),
            })
        seq = max((int(s.get("seq") or 0) for s in doc["sources"]), default=0) + 1
        label = (body.label or f"第 {seq} 章").strip()
        known = [str(i.get("text", "")) for i in doc["items"]]
        items_new, material_items, replaced, leak = _distill_sample(mode, sample, known, seq)
        outcome = learning_svc.merge_items(doc["items"], items_new, seq=seq)
        created_materials, merged_materials = _store_material_items(
            material_items, chapter=seq, label=label,
            title=str(post.metadata.get("title") or ""), sample=sample,
        )

        now = time.time()
        sources = [*doc["sources"], {
            "seq": seq, "label": label, "chars": len(sample),
            "sha256": digest, "distilled_at": now,
        }]
        log_line = (
            f"{time.strftime('%Y-%m-%d %H:%M')} 追加「{label}」："
            + (f"素材新建 {created_materials} / 合并 {merged_materials} 条；" if material_items else "")
            + f"写法新增 {outcome.added} 条，合并 {outcome.merged} 条"
            + (f"，冲突 {len(outcome.conflicts)} 条" if outcome.conflicts else "")
        )
        meta = {
            **{k: v for k, v in post.metadata.items() if k != "purify_note"},
            "schema": learning_svc.SCHEMA_VERSION,
            "updated": now,
            "scrubbed": int(post.metadata.get("scrubbed") or 0) + len(replaced),
            "sources": sources,
            "items": outcome.items,
            "materials": [
                *(post.metadata.get("materials") or []),
                *_material_refs(material_items),
            ],
            "conflicts": [*doc["conflicts"], *outcome.conflicts],
            "changelog": [*doc["changelog"], log_line],
        }
        if replaced:
            meta["purify_note"] = "、".join(f"`{k}`→{v}" for k, v in sorted(replaced.items()))
        elif post.metadata.get("purify_note"):
            meta["purify_note"] = post.metadata["purify_note"]
        if doc["legacy"]:
            meta["changelog"].insert(
                0, f"{time.strftime('%Y-%m-%d %H:%M')} 已从旧格式升级为条目化（此前内容记为第 1 章）"
            )
        _write_learning_doc(path, meta, outcome.items, sample)
        return JSONResponse({
            "ok": True, "already": False, "id": lid,
            "seq": seq, "label": label,
            "added": outcome.added, "merged": outcome.merged,
            "materials": len(material_items),
            "materials_created": created_materials,
            "materials_merged": merged_materials,
            "conflicts": len(outcome.conflicts),
            "scrubbed": len(replaced),
            "leak_terms": sorted(set(leak)),
            "sources": len(sources), "items": len(outcome.items),
            "summary": log_line,
        })

    # ══════════ 数据看板 ══════════

    @app.get("/api/dashboard")
    def dashboard(novel: str = "") -> JSONResponse:
        store = _store(novel)
        chapters = store.list_chapters()
        approved = [c for c in chapters if c.metadata.get("status") == "approved"]
        scores = [c.metadata.get("score") for c in approved if c.metadata.get("score")]
        first_pass = [c for c in approved if c.metadata.get("first_review_passed")]

        history = [
            {
                "chapter": c.metadata.get("chapter"),
                "attempt": c.metadata.get("attempt"),
                "score": c.metadata.get("score"),
                "status": c.metadata.get("status"),
                "model": c.metadata.get("model"),
            }
            for c in sorted(chapters, key=lambda d: d.metadata.get("chapter", 0))
        ]

        foreshadow = {"total": 0, "resolved": 0, "items": []}
        if store.exists("settings/foreshadowing.md"):
            items = store.read("settings/foreshadowing.md").metadata.get("items", []) or []
            foreshadow["total"] = len(items)
            foreshadow["resolved"] = sum(1 for i in items if i.get("status") == "resolved")
            foreshadow["items"] = [
                {
                    "id": i.get("id"),
                    "desc": str(i.get("desc", ""))[:60],
                    "status": i.get("status", "planted"),
                    "planted_ch": i.get("planted_ch"),
                    "resolve_ch": i.get("resolve_ch"),
                }
                for i in items[:20]
            ]

        return JSONResponse(
            {
                "metrics": {
                    "total_chapters": len(chapters),
                    "approved": len(approved),
                    "first_pass_rate": round(len(first_pass) / len(approved) * 100, 1)
                    if approved
                    else None,
                    "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
                },
                "chapters": history,
                "foreshadow": foreshadow,
            }
        )

    logger.info("Inkforge 扩展 API（三）已注册（智能体设置/素材库/学习仿写/数据看板）")
