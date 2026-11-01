"""Inkforge 扩展 API（五）：创作功能第四个模块 —— 「正文改写」。

模块定位（用户需求 2026-10-04）：作者粘贴**已有的正文** + 写出**目标修改/新增内容** →
专职智能体按作者要求改出**整篇**正文，严格遵循原文、只改点名处、参考原文文风、自定义约束优先。

**硬性不变量：禁止看我的小说内容。**
与风格工坊「约束提炼」同一口径（HANDOVER §5.10）——**看什么由作者决定**，引擎不替他决定去翻什么。
隔离不是提示词里的一句请求，而是结构性的四道闸（对应回归 `tests/test_revision_forge.py::TestIsolation`）：

| 闸 | 做法 |
|---|---|
| 签名层 | `revision_forge.build_messages(requirement, source_text, ...)` **不接受** novel/book/store/chapter |
| 端点层 | `POST /api/revision/rewrite` 的请求体只有"作者自己的文本"这几个字段，**没有 novel**；无 query 参数 |
| 依赖层 | 本模块与 `services/revision_forge.py` **不 import** `MdStore / store_factory / library`，不读 `novels_dir`（AST 断言） |
| 行为层 | 造一本带哨兵串的书 → 发请求 → 断言发往模型的报文与响应**都不含**哨兵 |

> 作者在创作空间点「载入」、或从人审卡点「局部改写」把某一章送进来，
> 是**作者自己选择交给它看**的那一份文本 —— 那叫"你给了它"，不叫"它去翻了你的书"。
> UI 上每个入口都写明了这一点。

三条既有契约照抄，不另造：
  · 响应头只放 ASCII 来源码（中文 hint 进 detail 正文）—— 见 HANDOVER §5.11；
  · 模型失败的 detail 一律 `redact_secrets()` 脱敏 —— 见 §5.13；
  · 非法入参一律 400，不抛裸 ValueError —— 见 §5.13。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.services import revision_forge as rev
from src.utils.logger import get_logger
from src.web.server import classify_failure, redact_secrets

logger = get_logger(__name__)

#: 失败来源 → 中文标签（与 inkforge_api / inkforge_forge 的同名表一致）
_SOURCE_LABEL = {
    "network": "网络连接中断",
    "provider_auth": "接入点鉴权失败",
    "config": "模型配置错误",
    "parse": "模型输出无法解析",
    "engine": "引擎内部错误",
}


class RevisionAnchor(BaseModel):
    """锚点：作者点名的改动位置（段号或原文片段，二选一）。"""

    #: 1 起的原文段号（"在它之后"）
    after_index: int = 0
    #: 原文片段（用来定位段号；UI 的段落列表点选时给的就是它）
    text: str = ""


class RevisionBody(BaseModel):
    """改写请求体：**两个内容位**（原文 + 要求）+ 几个可选增强位。

    刻意只有"作者自己的文本"这些字段：**没有 novel、没有 chapter、没有 rel** ——
    调用方无从指定作品库里的任何东西。作者没粘贴/没送进来的内容，端点这一层就拿不到。
    """

    #: ① 原文（要改的正文，作者自己粘贴或从创作空间主动送进来）
    source_text: str
    #: ② 改写要求（作者自己写；**唯一指令来源**）
    requirement: str
    #: 模式：auto / expand / rewrite / insert / replace / rewrite_all（auto = 由要求自动判定）
    mode: str = rev.MODE_AUTO
    #: ③ 自定义约束（最高优先级；可为手写条目，也可为从约束库勾选后拼起来的文本）
    constraints: str = ""
    #: 锚点（点名的改动位置；空 = 由模型按要求判断"该改哪儿"）
    anchors: list[RevisionAnchor] = Field(default_factory=list)
    #: 锁定片段：要求逐字保留的内容
    lock_segments: list[str] = Field(default_factory=list)
    #: 自动把原文里的对白加入锁定（扩写时最容易被顺手改掉的就是对白）
    lock_dialogue: bool = False
    #: 文风样本（作者另给一段语感样本；留空则用原文自己的客观文风指标）
    style_sample: str = ""


def register_revision_api(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    # 显式声明本模块的隔离边界：签名与其它扩展模块统一，但这些参数**不被使用**——
    # 本模块任何一条代码路径都不应触碰作品数据（静态回归：tests/test_revision_forge.py）。
    _ = (hub, default_novel)

    def _runtime_prompt() -> str:
        """本智能体的最终系统提示词（含用户覆盖与第零条，与其它智能体同源装配）。"""
        from src.web.inkforge_windows import agent_prompt

        return agent_prompt(rev.AGENT_KEY)

    @app.get("/api/revision/agent")
    def revision_agent_view() -> JSONResponse:
        """正文改写模块的智能体名片：它是谁、能看什么、看不到什么、有哪些模式与上限。"""
        return JSONResponse({
            "key": rev.AGENT_KEY,
            "label": rev.AGENT_LABEL,
            "role": rev.MODEL_ROLE,
            "prompt": _runtime_prompt(),
            "limits": {
                "source_max": rev.MAX_SOURCE_CHARS,
                "source_min": rev.MIN_SOURCE_CHARS,
                "requirement_max": rev.MAX_REQUIREMENT_CHARS,
                "constraints_max": rev.MAX_CONSTRAINTS_CHARS,
                "anchors_max": rev.MAX_ANCHORS,
                "lock_max": rev.MAX_LOCK_SEGMENTS,
            },
            "modes": [
                {"value": m, "label": rev.MODE_LABELS[m]} for m in rev.ALL_MODES
            ],
            # 视野声明：UI 直接展示。与约束提炼同口径——**看什么由你决定**。
            "sees": [
                "你在【原文】框里粘贴的正文（或你在创作空间/人审卡主动送进来的那一章、那一段）",
                "你在【改写要求】框里写的要求",
                "你在【自定义约束】里写的、或从约束库里勾选的那几条约束",
            ],
            "never_sees": [
                "你作品库里的任何文件（正文 / 章节 / 大纲 / 设定 / 素材 / 摘要 / 文风指纹）",
                "你没有粘贴、也没有主动送进来的任何内容",
                "任何以 novel 参数传入的内容（端点根本不收这个参数）",
            ],
        })

    @app.post("/api/revision/rewrite")
    def revision_rewrite(body: RevisionBody) -> JSONResponse:
        """原文 + 要求 → 整篇改后正文 + 段级改动对照（一次模型调用，不落盘、不读库）。"""
        source_text = body.source_text.strip()
        requirement = body.requirement.strip()
        constraints = (body.constraints or "").strip()

        if not source_text:
            raise HTTPException(400, "请先把要改的正文放进【原文】框（它只改你给的或你送进来的文本）")
        if len(source_text) < rev.MIN_SOURCE_CHARS:
            raise HTTPException(
                400,
                f"原文太短（{len(source_text)} 字，至少 {rev.MIN_SOURCE_CHARS} 字）——"
                "贴一段够看的正文，改写才有依据。",
            )
        if len(source_text) > rev.MAX_SOURCE_CHARS:
            raise HTTPException(
                400,
                f"原文过长（{len(source_text)} 字，上限 {rev.MAX_SOURCE_CHARS} 字）。"
                "改写要整篇回传，请按章拆分（一次一章，效果也更好）。",
            )
        if not requirement:
            raise HTTPException(400, "请写下【改写要求】——说清你要改什么、改哪里、改到什么程度")
        if len(requirement) > rev.MAX_REQUIREMENT_CHARS:
            raise HTTPException(
                400,
                f"改写要求过长（{len(requirement)} 字，上限 {rev.MAX_REQUIREMENT_CHARS} 字）。"
                "要求写清「怎么改」即可，正文请放【原文】框。",
            )
        if len(constraints) > rev.MAX_CONSTRAINTS_CHARS:
            raise HTTPException(
                400,
                f"自定义约束过长（{len(constraints)} 字，上限 {rev.MAX_CONSTRAINTS_CHARS} 字）。",
            )

        anchors = [a.model_dump() for a in body.anchors]
        locks = [s for s in (body.lock_segments or []) if str(s).strip()]
        if len(anchors) > rev.MAX_ANCHORS:
            raise HTTPException(400, f"锚点过多（{len(anchors)} 个，上限 {rev.MAX_ANCHORS} 个）。")
        if len(locks) > rev.MAX_LOCK_SEGMENTS:
            raise HTTPException(400, f"锁定片段过多（{len(locks)} 条，上限 {rev.MAX_LOCK_SEGMENTS} 条）。")

        started = time.time()
        try:
            result = rev.extract_revision(
                source_text,
                requirement,
                mode=body.mode,
                constraints=constraints,
                anchors=anchors,
                lock_segments=locks,
                lock_dialogue=body.lock_dialogue,
                style_sample=body.style_sample or "",
                system_prompt=_runtime_prompt(),
            )
        except rev.RevisionFidelityError as exc:
            # 保真闸门（越界 / 零改动 / 锁段被改 / 非整篇）重试耗尽：不把坏产出交出去
            logger.warning("正文改写保真闸门未通过：%s", exc)
            raise HTTPException(
                422,
                f"改写结果不可用：{exc}\n\n建议：把要求写得更具体（例如「只在第 N 段之后加」），"
                "或改用「整体重写」模式再试。",
                headers={"X-Inkforge-Error-Source": "parse"},
            ) from exc
        except ValueError as exc:
            logger.warning("改写产出不可用：%s", exc)
            raise HTTPException(
                422,
                f"改写结果不可用：{exc}\n\n建议：把要求写得更具体，或缩短原文后重试。",
                headers={"X-Inkforge-Error-Source": "parse"},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 模型/配置错误透传给前端
            logger.exception("正文改写失败")
            source, hint = classify_failure(exc)
            raise HTTPException(
                502,
                f"模型调用失败（{_SOURCE_LABEL.get(source, source)}）："
                f"{redact_secrets(f'{type(exc).__name__}: {exc}')}\n\n建议：{hint}",
                headers={"X-Inkforge-Error-Source": source},
            ) from exc

        return JSONResponse({
            "ok": True,
            "agent": {"key": rev.AGENT_KEY, "label": rev.AGENT_LABEL},
            "mode": result["mode"],
            "mode_label": result["mode_label"],
            "mode_reason": result["mode_reason"],
            "scope": result["scope"],
            "scope_label": result["scope_label"],
            "scope_reason": result["scope_reason"],
            "revised": result["revised"],
            "segments": result["segments"],
            "stats": result["stats"],
            "checks": result["checks"],
            "summary": result["summary"],
            "style_shift": result["style_shift"],
            "constraints": result["constraints"],
            "anchors": result["anchors"],
            "locked": result["locked"],
            "role": result["role"],
            "model": result["model"],
            "provider": result["provider"],
            "elapsed": round(time.time() - started, 2),
            # 回执里显式复述输入边界：让"它到底看了什么"在 UI 上永远可见
            "isolation": "只使用你在本页粘贴/送进来的正文与要求；未读取你作品库里的任何文件。",
        })

    logger.info("正文改写 API 已注册（/api/revision/agent · /api/revision/rewrite）")
