"""蒸馏 Skill：将长篇小说转化为 16 维结构化知识技能包。

继承 Skill 基类，通过 @register 注册入 SkillRegistry。
支持：
- 初始化：上传书籍 → 分块 → 创建技能包目录
- 执行：LangGraph 驱动的逐块增量蒸馏（断点续跑）
- 报告：加载完整 16 维蒸馏结果
- 重新蒸馏：版本更新（增量追加 / 深度升级 / 全量重蒸）
- 导出：打包为 .zip
- 删除：清理技能包目录
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, ClassVar

from src.distillation.chunker import Chunk, chunk_file
from src.distillation.graph import DistillPipeline, run_distill
from src.distillation.schemas import FullReport
from src.distillation.skill_store import (
    checkpoint_db_for,
    create_skill_package,
    delete_skill_package,
    export_skill_package,
    load_chunks,
    load_index,
    load_manifest,
    load_report,
    save_report,
)
from src.llm.registry import ModelRegistry
from src.skills.base import Skill, SkillSettings
from src.skills.registry import register
from src.utils.logger import get_logger

logger = get_logger(__name__)


class DistillSettings(SkillSettings):
    enabled: bool = False                       # 有 LLM 成本，默认关闭
    chunk_strategy: str = "BY_CHAPTERS"          # BY_CHAPTERS / BY_WORDS
    chunk_size: int = 20000                      # BY_WORDS 策略每块字数
    depth: str = "DEEP"                          # DEEP = 16 维（预留 SHALLOW = 4 维）


@register
class DistillSkill(Skill):
    name: ClassVar[str] = "distill"
    SettingsModel: ClassVar[type[SkillSettings]] = DistillSettings

    # ── 蒸馏会话（后台线程驱动 LangGraph，与 ReviewSession 模式一致） ──

    _sessions: dict[str, _DistillSession] = {}

    def run(
        self,
        action: str = "status",
        file_path: str = "",
        **kwargs: Any,
    ) -> dict:
        """Skill 主入口：按 action 路由到对应操作。

        actions:
          - init: 上传文件 → 分块 → 创建技能包
          - start: 启动后台蒸馏线程
          - status: 查询进度
          - report: 获取完整报告
          - reprocess: 重新蒸馏
          - export: 导出 .zip
          - delete: 删除技能包
        """
        if action == "init":
            return self._init(file_path, **kwargs)
        if action == "start":
            return self._start(**kwargs)
        if action == "status":
            return self._status(**kwargs)
        if action == "report":
            return self._report(**kwargs)
        if action == "reprocess":
            return self._reprocess(**kwargs)
        if action == "export":
            return self._export(**kwargs)
        if action == "delete":
            return self._do_delete(**kwargs)
        if action == "list":
            return self._list()
        raise ValueError(f"未知蒸馏操作: {action}（可用: init/start/status/report/reprocess/export/delete/list）")

    # ── 操作实现 ──

    def _init(self, file_path: str, skill_id: str = "", version: str = "1.0.0",
              **kwargs: Any) -> dict:
        """初始化蒸馏项目：分块 + 创建技能包目录。

        返回 {skill_id, total_chunks, book_title}。
        """
        if not file_path:
            raise ValueError("缺少 file_path 参数")
        fp = Path(file_path)
        if not fp.exists():
            raise FileNotFoundError(f"文件不存在: {fp}")

        strategy = self.settings.chunk_strategy
        chunk_size = kwargs.get("chunk_size", self.settings.chunk_size)
        chunks, title = chunk_file(fp, strategy=strategy, chunk_size=chunk_size)

        sid = skill_id or f"{title}_v{version}"
        create_skill_package(sid, title, version, chunks, len(chunks))

        return {
            "skill_id": sid,
            "book_title": title,
            "total_chunks": len(chunks),
            "status": "READY",
        }

    def _start(self, skill_id: str = "", **kwargs: Any) -> dict:
        """启动后台蒸馏线程（非阻塞，与现有 ReviewSession 模式一致）。"""
        if not skill_id:
            raise ValueError("缺少 skill_id")
        manifest = load_manifest(skill_id)

        if skill_id in self._sessions:
            sess = self._sessions[skill_id]
            if sess.is_running:
                return {"skill_id": skill_id, "status": "ALREADY_RUNNING",
                        "progress": sess.progress}
            # 之前异常终止，允许重新启动
            del self._sessions[skill_id]

        session = _DistillSession(
            skill_id=skill_id,
            book_title=manifest["book_title"],
            registry=self.context.registry,
        )
        self._sessions[skill_id] = session
        session.start()
        return {"skill_id": skill_id, "status": "STARTED"}

    def _status(self, skill_id: str = "", **kwargs: Any) -> dict:
        """查询蒸馏进度。"""
        if not skill_id:
            # 返回所有技能包列表
            return self._list()

        sess = self._sessions.get(skill_id)
        if sess:
            return sess.snapshot()

        # 检查已完成的技能包
        try:
            manifest = load_manifest(skill_id)
            if manifest["status"] == "COMPLETED":
                return {
                    "skill_id": skill_id,
                    "status": "COMPLETED",
                    "progress": f"{manifest['total_chunks']}/{manifest['total_chunks']}",
                    "book_title": manifest["book_title"],
                }
            return {"skill_id": skill_id, "status": manifest["status"]}
        except FileNotFoundError:
            raise FileNotFoundError(f"技能包 {skill_id} 不存在")

    def _report(self, skill_id: str = "", **kwargs: Any) -> dict:
        """获取完整 16 维报告。"""
        if not skill_id:
            raise ValueError("缺少 skill_id")
        report = load_report(skill_id)
        manifest = load_manifest(skill_id)
        return {
            "skill_id": skill_id,
            "manifest": manifest,
            "full_report": report.model_dump(),
        }

    def _reprocess(self, skill_id: str = "", mode: str = "INCREMENTAL",
                   new_file_path: str = "", new_version: str = "", **kwargs: Any) -> dict:
        """重新蒸馏（版本更新）。

        mode 可选：
          - INCREMENTAL: 追加新章节（需提供 new_file_path）
          - DEEP_UPGRADE: 深度升级（用更深维度的提示词重新分析已有分块）
          - FULL: 全量重蒸（完全重新分析）
        """
        if not skill_id:
            raise ValueError("缺少 skill_id")

        old_manifest = load_manifest(skill_id)
        old_version = old_manifest["version"]

        if mode == "INCREMENTAL":
            if not new_file_path:
                raise ValueError("增量追加模式需要提供 new_file_path")
            fp = Path(new_file_path)
            if not fp.exists():
                raise FileNotFoundError(f"文件不存在: {fp}")
            new_chunks, _ = chunk_file(fp, strategy=self.settings.chunk_strategy)
            all_chunks = load_chunks(skill_id) + [
                {"index": len(load_chunks(skill_id)) + c.index,
                 "text": c.text, "chapter_range": c.chapter_range}
                for c in new_chunks
            ]
            # 使用已有块的原始文本，加上新块的文本
            total = len(all_chunks)
        else:
            # DEEP_UPGRADE / FULL: 使用已有分块
            all_chunks = load_chunks(skill_id)
            total = len(all_chunks)

        # 计算新版本号
        if new_version:
            version = new_version
        else:
            parts = old_version.split(".")
            if mode == "INCREMENTAL":
                version = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
            elif mode == "DEEP_UPGRADE":
                version = f"{parts[0]}.{int(parts[1]) + 1}.0"
            else:
                version = f"{int(parts[0]) + 1}.0.0"

        new_skill_id = f"{old_manifest['book_title']}_v{version}"
        create_skill_package(new_skill_id, old_manifest["book_title"], version,
                             [Chunk(c["index"], c["text"], c.get("chapter_range", ""))
                              for c in all_chunks], total)

        return {
            "old_skill_id": skill_id,
            "new_skill_id": new_skill_id,
            "version": version,
            "mode": mode,
            "total_chunks": total,
        }

    def _export(self, skill_id: str = "", **kwargs: Any) -> dict:
        """导出技能包为 .zip。"""
        if not skill_id:
            raise ValueError("缺少 skill_id")
        output = export_skill_package(skill_id)
        return {"skill_id": skill_id, "path": str(output), "format": "zip"}

    def _do_delete(self, skill_id: str = "", **kwargs: Any) -> dict:
        """删除技能包。"""
        if not skill_id:
            raise ValueError("缺少 skill_id")
        # 如果有运行中的会话，先停止
        self._sessions.pop(skill_id, None)
        delete_skill_package(skill_id)
        return {"success": True, "skill_id": skill_id, "message": "技能包已删除"}

    def _list(self) -> dict:
        """列出所有技能包。"""
        index = load_index()
        return {"skills": index.get("skills", [])}


# ── 后台蒸馏会话 ──

class _DistillSession:
    """单次蒸馏会话：后台线程驱动 LangGraph（线程安全，与 ReviewSession 模式一致）。"""

    def __init__(self, skill_id: str, book_title: str, registry: ModelRegistry):
        self.skill_id = skill_id
        self.book_title = book_title
        self.registry = registry
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._error: str | None = None
        self._done = False
        self._chunk_index = 0
        self._total_chunks = 0
        self._result: FullReport | None = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def progress(self) -> str:
        return f"{self._chunk_index}/{self._total_chunks}"

    def start(self) -> None:
        if self.is_running:
            raise RuntimeError("蒸馏会话已在运行中")
        manifest = load_manifest(self.skill_id)
        self._total_chunks = manifest["total_chunks"]

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            all_chunks = load_chunks(self.skill_id)
            chunks = [Chunk(c["index"], c["text"], c.get("chapter_range", ""))
                      for c in all_chunks]

            pipe = DistillPipeline(registry=self.registry)
            pipe.registry.probe_all()

            checkpoint_db = checkpoint_db_for(self.skill_id)
            report = run_distill(
                pipe,
                skill_id=self.skill_id,
                book_title=self.book_title,
                chunks=chunks,
                checkpoint_db=checkpoint_db,
            )

            save_report(self.skill_id, report)

            with self._lock:
                self._done = True
                self._result = report
                self._chunk_index = self._total_chunks

            logger.info("蒸馏完成: %s (共 %d 块)", self.skill_id, self._total_chunks)

        except Exception as exc:  # noqa: BLE001
            logger.exception("蒸馏会话异常: %s", self.skill_id)
            with self._lock:
                self._error = str(exc)
                self._done = True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "skill_id": self.skill_id,
                "status": "ERROR" if self._error else ("COMPLETED" if self._done else "RUNNING"),
                "progress": self.progress if self._total_chunks else "?",
                "error": self._error,
                "book_title": self.book_title,
            }
