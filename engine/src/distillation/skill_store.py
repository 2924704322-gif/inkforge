"""技能包文件存储层：data/skills/ 目录的读写。

目录结构：
  data/skills/
  ├── index.json                    # 全局技能索引
  └── {skill_id}/                   # 每本书独立目录
      ├── manifest.json             # 元数据
      ├── full_report.json          # 完整 16 维报告
      ├── full_report.md            # 人类可读版
      ├── chunks/                   # 原始分块存档
      │   ├── chunk_001.txt
      │   └── ...
      └── history/                  # 版本历史
          └── ...
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from src.distillation.chunker import Chunk
from src.distillation.schemas import FullReport
from src.utils.logger import get_logger

logger = get_logger(__name__)


def _skills_dir() -> Path:
    """data/skills/ 根目录。

    统一走 get_settings()（而非模块级 PROJECT_ROOT 常量），使技能包目录与
    novels/custom_skills/materials/learning 一样落在同一个「数据根目录」下，
    并可通过 NOVELS_DIR 等环境变量重定向（测试隔离 / 多实例部署必需）。
    """
    from src.config.settings import get_settings
    d = get_settings().novels_dir.parent / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── 技能索引 ──

def load_index() -> dict:
    """读取全局技能索引 index.json。"""
    path = _skills_dir() / "index.json"
    if not path.exists():
        return {"skills": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(index: dict) -> None:
    """写回全局技能索引。"""
    (_skills_dir() / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n",
    )


def _add_to_index(skill_id: str, book_title: str, version: str, status: str) -> None:
    index = load_index()
    # 去重：同一 skill_id 已存在则替换
    index["skills"] = [s for s in index["skills"] if s.get("skill_id") != skill_id]
    index["skills"].append({
        "skill_id": skill_id,
        "book_title": book_title,
        "version": version,
        "created_at": _now_iso()[:10],
        "status": status,
        "path": f"./{skill_id}/",
    })
    save_index(index)


def _update_index_status(skill_id: str, status: str) -> None:
    index = load_index()
    for s in index["skills"]:
        if s.get("skill_id") == skill_id:
            s["status"] = status
            break
    save_index(index)


def remove_from_index(skill_id: str) -> None:
    index = load_index()
    index["skills"] = [s for s in index["skills"] if s.get("skill_id") != skill_id]
    save_index(index)


# ── Skill 目录操作 ──

def skill_dir(skill_id: str) -> Path:
    return _skills_dir() / skill_id


def create_skill_package(
    skill_id: str,
    book_title: str,
    version: str,
    chunks: list[Chunk],
    total_chunks: int,
    provider: str = "",
    model: str = "",
) -> Path:
    """初始化技能包目录：manifest + chunks 存档。"""
    d = skill_dir(skill_id)
    if d.exists():
        raise FileExistsError(f"技能包 {skill_id} 已存在，请使用其他版本号或先删除")

    d.mkdir(parents=True)
    (d / "chunks").mkdir()
    (d / "history").mkdir()

    # 写 manifest
    manifest = {
        "skill_id": skill_id,
        "book_title": book_title,
        "version": version,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "total_chunks": total_chunks,
        "status": "IN_PROGRESS",
        "provider": provider,
        "model": model,
        "dimensions": ["all_16"],
    }
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n",
    )

    # 存档原始分块
    for c in chunks:
        chunk_path = d / "chunks" / f"chunk_{c.index:03d}.txt"
        chunk_path.write_text(c.text, encoding="utf-8", newline="\n")

    # 注册到全局索引
    _add_to_index(skill_id, book_title, version, "IN_PROGRESS")

    logger.info("技能包 %s 初始化完成（%d 块）", skill_id, total_chunks)
    return d


def save_report(skill_id: str, report: FullReport) -> None:
    """保存完整 16 维报告（JSON + Markdown）。"""
    d = skill_dir(skill_id)
    if not d.exists():
        raise FileNotFoundError(f"技能包 {skill_id} 不存在")

    # JSON
    (d / "full_report.json").write_text(
        report.model_dump_json(indent=2), encoding="utf-8", newline="\n",
    )

    # Markdown
    md = _report_to_markdown(report)
    (d / "full_report.md").write_text(md, encoding="utf-8", newline="\n")

    # 更新 manifest
    manifest = load_manifest(skill_id)
    manifest["status"] = "COMPLETED"
    manifest["updated_at"] = _now_iso()
    manifest["provider"] = report.provider or manifest.get("provider", "")
    manifest["model"] = report.model or manifest.get("model", "")
    (d / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n",
    )

    _update_index_status(skill_id, "COMPLETED")
    logger.info("技能包 %s 报告已保存", skill_id)


def load_manifest(skill_id: str) -> dict:
    path = skill_dir(skill_id) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"技能包 {skill_id} 的 manifest 不存在")
    return json.loads(path.read_text(encoding="utf-8"))


def load_report(skill_id: str) -> FullReport:
    path = skill_dir(skill_id) / "full_report.json"
    if not path.exists():
        raise FileNotFoundError(f"技能包 {skill_id} 的报告尚未生成")
    return FullReport.model_validate_json(path.read_text(encoding="utf-8"))


def load_chunks(skill_id: str) -> list[dict]:
    """加载存档的原始分块（用于重新蒸馏）。"""
    d = skill_dir(skill_id) / "chunks"
    if not d.exists():
        return []
    chunks = []
    for p in sorted(d.glob("chunk_*.txt")):
        chunks.append({
            "index": int(p.stem.split("_")[1]),
            "text": p.read_text(encoding="utf-8"),
            "chapter_range": "",
        })
    return chunks


def delete_skill_package(skill_id: str) -> None:
    """删除技能包目录及索引条目。"""
    d = skill_dir(skill_id)
    if d.exists():
        shutil.rmtree(d)
    remove_from_index(skill_id)
    logger.info("技能包 %s 已删除", skill_id)


def export_skill_package(skill_id: str, output_path: Path | None = None) -> Path:
    """打包技能包为 .zip。"""
    d = skill_dir(skill_id)
    if not d.exists():
        raise FileNotFoundError(f"技能包 {skill_id} 不存在")

    output = output_path or (_skills_dir() / f"{skill_id}.zip")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(d.rglob("*")):
            if file_path.is_file():
                arcname = file_path.relative_to(d).as_posix()
                zf.write(file_path, arcname)

    logger.info("技能包 %s 已导出为 %s", skill_id, output)
    return output


def checkpoint_db_for(skill_id: str) -> Path:
    """返回该技能包的 LangGraph checkpoint 数据库路径。"""
    d = skill_dir(skill_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / "distill_checkpoints.sqlite"


# ── Markdown 渲染 ──

def _report_to_markdown(report: FullReport) -> str:
    """将 FullReport 渲染为人类可读的 Markdown。"""

    lines = [
        f"# 《{report.book_title}》蒸馏分析报告",
        "",
        f"> 块数：{report.total_chunks} | 模型：{report.provider}/{report.model} | 维度：16",
        "",
        "---",
    ]

    sections = [
        ("一、世界构建", _format_world),
        ("二、情节结构", _format_plot),
        ("三、主题星系", _format_theme),
        ("四、叙事架构", _format_narrative),
        ("五、人物系统", _format_characters),
        ("六、环境与空间", _format_environment),
        ("七、环境描写技法", _format_env_desc),
        ("八、人物描写技法", _format_char_desc),
        ("九、对话艺术", _format_dialogue),
        ("十、动作与场景调度", _format_action),
        ("十一、语体与语气", _format_style),
        ("十二、节奏与韵律", _format_rhythm),
        ("十三、感官与情感地图", _format_sensory),
        ("十四、时间与记忆编码", _format_time_memory),
        ("十五、元叙事与互文性", _format_meta),
        ("十六、意识形态与价值观", _format_ideology),
    ]

    for title, formatter in sections:
        lines.append(f"\n## {title}\n")
        lines.append(formatter(report))

    if report.cross_references:
        lines.append("\n## 跨维度关联\n")
        for ref in report.cross_references:
            lines.append(f"- {ref}")

    if report.pending_questions:
        lines.append("\n## 待解问题\n")
        for q in report.pending_questions:
            lines.append(f"- {q}")

    return "\n".join(lines)


def _fmt_list(items: list, key: str = "name", max_items: int = 50) -> str:
    """格式化列表为 Markdown 条目。"""
    if not items:
        return "（暂无数据）\n"
    lines = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            name = item.get(key, str(item))
            detail = {k: v for k, v in item.items() if k != key and v}
            if detail:
                lines.append(f"- **{name}**：{_fmt_dict_compact(detail)}")
            else:
                lines.append(f"- {name}")
        elif hasattr(item, "model_dump"):
            d = item.model_dump()
            name = d.get(key, str(item))
            detail = {k: v for k, v in d.items() if k != key and v}
            if detail:
                lines.append(f"- **{name}**：{_fmt_dict_compact(detail)}")
            else:
                lines.append(f"- {name}")
        else:
            lines.append(f"- {item}")
    if len(items) > max_items:
        lines.append(f"  …等 {len(items)} 条")
    return "\n".join(lines) + "\n"


def _fmt_dict_compact(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v[:5])
        parts.append(f"{k}={v}")
    return "; ".join(parts[:8])


def _format_world(r: FullReport) -> str:
    parts = [f"**一致性**: {r.world.consistency_notes or '（暂无）'}\n"]
    if r.world.rules:
        parts.append(f"### 规则体系 ({len(r.world.rules)}条)\n")
        parts.append(_fmt_list(r.world.rules, "name"))
    if r.world.power_graph:
        parts.append(f"### 权力图谱 ({len(r.world.power_graph)}个节点)\n")
        parts.append(_fmt_list(r.world.power_graph, "entity"))
    if r.world.history_layers:
        parts.append(f"### 历史地层 ({len(r.world.history_layers)}层)\n")
        parts.append(_fmt_list(r.world.history_layers))
    return "".join(parts)


def _format_plot(r: FullReport) -> str:
    parts = []
    if r.plot.closed_loops:
        parts.append(f"**叙事闭环**: {r.plot.closed_loops}\n")
    if r.plot.main_thread:
        parts.append(f"### 主线事件 ({len(r.plot.main_thread)}条)\n")
        parts.append(_fmt_list(r.plot.main_thread, "event"))
    if r.plot.subplots:
        parts.append(f"### 支线 ({len(r.plot.subplots)}条)\n")
        parts.append(_fmt_list(r.plot.subplots, "name"))
    if r.plot.suspense_techniques:
        parts.append("### 悬念手法\n")
        parts.append(_fmt_list(r.plot.suspense_techniques))
    if r.plot.turning_points:
        parts.append(f"### 转折点 ({len(r.plot.turning_points)}个)\n")
        parts.append(_fmt_list(r.plot.turning_points, "description"))
    if r.plot.foreshadowing:
        parts.append(f"### 伏笔 ({len(r.plot.foreshadowing)}条)\n")
        parts.append(_fmt_list(r.plot.foreshadowing, "desc"))
    return "".join(parts)


def _format_theme(r: FullReport) -> str:
    parts = [f"**核心命题**: {r.theme.core_proposition or '（暂无）'}\n"]
    parts.append(f"**作者立场**: {r.theme.author_stance or '（暂无）'}\n")
    if r.theme.variations:
        parts.append(f"### 主题变奏 ({len(r.theme.variations)}种)\n")
        parts.append(_fmt_list(r.theme.variations))
    if r.theme.image_system:
        parts.append(f"### 意象系统 ({len(r.theme.image_system)}个)\n")
        parts.append(_fmt_list(r.theme.image_system, "image"))
    if r.theme.value_conflicts:
        parts.append("### 价值冲突\n")
        parts.append(_fmt_list(r.theme.value_conflicts))
    return "".join(parts)


def _format_narrative(r: FullReport) -> str:
    parts = [f"**叙述者**: {r.narrative.narrator_type or '（暂无）'}\n"]
    if r.narrative.levels:
        parts.append(f"**叙述层次**: {', '.join(r.narrative.levels)}\n")
    if r.narrative.focalization:
        parts.append(f"**聚焦方式**: {', '.join(r.narrative.focalization)}\n")
    if r.narrative.time_manipulation:
        parts.append("**时间畸变手法**:\n")
        parts.append(_fmt_list(r.narrative.time_manipulation))
    return "".join(parts)


def _format_characters(r: FullReport) -> str:
    parts = []
    if r.characters.characters:
        parts.append(f"### 角色 ({len(r.characters.characters)}人)\n")
        parts.append(_fmt_list(r.characters.characters, "name"))
    if r.characters.relations:
        parts.append(f"### 关系拓扑 ({len(r.characters.relations)}条)\n")
        parts.append(_fmt_list(r.characters.relations, "source"))
    if r.characters.group_dynamics:
        parts.append("### 群体动力学\n")
        parts.append(_fmt_list(r.characters.group_dynamics))
    return "".join(parts)


def _format_environment(r: FullReport) -> str:
    parts = []
    if r.environment.topology:
        parts.append(f"### 空间拓扑 ({len(r.environment.topology)}处)\n")
        parts.append(_fmt_list(r.environment.topology, "name"))
    if r.environment.artifacts:
        parts.append(f"### 人造物 ({len(r.environment.artifacts)}件)\n")
        parts.append(_fmt_list(r.environment.artifacts, "name"))
    if r.environment.environmental_shifts:
        parts.append("### 环境突变\n")
        parts.append(_fmt_list(r.environment.environmental_shifts))
    return "".join(parts)


def _format_env_desc(r: FullReport) -> str:
    parts = []
    if r.env_description.techniques:
        parts.append(f"### 技法 ({len(r.env_description.techniques)}种)\n")
        parts.append(_fmt_list(r.env_description.techniques, "technique"))
    return "".join(parts)


def _format_char_desc(r: FullReport) -> str:
    parts = []
    if r.char_description.techniques:
        parts.append(f"### 技法 ({len(r.char_description.techniques)}种)\n")
        parts.append(_fmt_list(r.char_description.techniques, "technique"))
    return "".join(parts)


def _format_dialogue(r: FullReport) -> str:
    parts = []
    if r.dialogue.character_fingerprints:
        parts.append(f"### 言语指纹 ({len(r.dialogue.character_fingerprints)}人)\n")
        parts.append(_fmt_list(r.dialogue.character_fingerprints, "character"))
    return "".join(parts)


def _format_action(r: FullReport) -> str:
    parts = []
    if r.action.scenes:
        parts.append(f"### 动作场景 ({len(r.action.scenes)}场)\n")
        parts.append(_fmt_list(r.action.scenes, "description"))
    return "".join(parts)


def _format_style(r: FullReport) -> str:
    parts = [
        f"**语体光谱**: {r.style.register_spectrum or '（暂无）'}\n",
        f"**语气稳定性**: {r.style.tone_stability or '（暂无）'}\n",
        f"**修辞格密度**: {r.style.rhetorical_density or '（暂无）'}\n",
    ]
    if r.style.lexicon_fields:
        parts.append(f"**高频词汇域**: {', '.join(r.style.lexicon_fields)}\n")
    if r.style.sentence_patterns:
        parts.append("**标志性句式**:\n")
        parts.append(_fmt_list(r.style.sentence_patterns))
    return "".join(parts)


def _format_rhythm(r: FullReport) -> str:
    parts = [f"**张力曲线**: {r.rhythm.tension_curve or '（暂无）'}\n"]
    if r.rhythm.syntactic_rhythm:
        parts.append("**句法节奏**:\n")
        parts.append(_fmt_list(r.rhythm.syntactic_rhythm))
    if r.rhythm.chapter_beats:
        parts.append(f"**章节节拍** ({len(r.rhythm.chapter_beats)}个)\n")
        parts.append(_fmt_list(r.rhythm.chapter_beats, "beat_type"))
    return "".join(parts)


def _format_sensory(r: FullReport) -> str:
    parts = [
        f"**情感主导色**: {r.sensory.dominant_emotion or '（暂无）'}\n",
    ]
    if r.sensory.emotion_curve:
        parts.append(f"### 情感路线 ({len(r.sensory.emotion_curve)}段)\n")
        parts.append(_fmt_list(r.sensory.emotion_curve, "emotion"))
    if r.sensory.sensory_bindings:
        parts.append(f"### 感官-情感绑定 ({len(r.sensory.sensory_bindings)}组)\n")
        parts.append(_fmt_list(r.sensory.sensory_bindings, "sensory_input"))
    return "".join(parts)


def _format_time_memory(r: FullReport) -> str:
    parts = [f"**物理vs叙事时间**: {r.time_memory.physical_vs_narrative_time or '（暂无）'}\n"]
    if r.time_memory.memory_presence:
        parts.append("**记忆在场**:\n")
        parts.append(_fmt_list(r.time_memory.memory_presence))
    return "".join(parts)


def _format_meta(r: FullReport) -> str:
    parts = [f"**类型意识**: {r.meta_narrative.genre_awareness or '（暂无）'}\n"]
    if r.meta_narrative.self_reference:
        parts.append(f"**自我指涉**: {len(r.meta_narrative.self_reference)}处\n")
    if r.meta_narrative.intertextuality:
        parts.append(f"### 互文引用 ({len(r.meta_narrative.intertextuality)}处)\n")
        parts.append(_fmt_list(r.meta_narrative.intertextuality, "source"))
    return "".join(parts)


def _format_ideology(r: FullReport) -> str:
    parts = [f"**问题意识**: {r.ideology.problem_consciousness or '（暂无）'}\n"]
    if r.ideology.explicit_claims:
        parts.append(f"**显性主张**: {', '.join(r.ideology.explicit_claims[:10])}\n")
    if r.ideology.implicit_presumptions:
        parts.append(f"**隐性预设**: {', '.join(r.ideology.implicit_presumptions[:10])}\n")
    if r.ideology.value_conflicts:
        parts.append(f"### 价值冲突 ({len(r.ideology.value_conflicts)}组)\n")
        parts.append(_fmt_list(r.ideology.value_conflicts, "parties"))
    return "".join(parts)
