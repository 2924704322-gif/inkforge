"""Inkforge 扩展 API（四）：风格工坊第三个模块 —— 「约束提炼」端点。

模块定位（用户需求）：风格工坊原有两页（技能包与约束 / 绑定到当前作品），
本文件服务新增的**同级第三页**：作者写一段要求 → 专职智能体提炼成约束条目 → 复制 / 入库。

**硬性不变量：禁止看小说内容。**
这里的隔离不是提示词里的一句请求，而是结构性的：
- 本模块**不 import** MdStore / store_factory / library，也不碰 novels_dir；
- 两个端点都**没有 novel / book 参数**，请求体里只有用户手写的要求文本；
- register_forge_api 收到的 hub / default_novel 一律**不落地使用**（签名与其它扩展模块统一，
  但本模块刻意不用它们 —— 这样"某个改动顺手把书塞进来"也无处可塞）。
对应回归见 tests/test_constraint_forge.py::TestIsolation。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.services import constraint_forge as forge
from src.utils.logger import get_logger
from src.web.server import classify_failure, redact_secrets

logger = get_logger(__name__)

#: 失败来源 → 中文标签（与 inkforge_api 的同名表一致；错误文案与响应头共用）
_SOURCE_LABEL = {
    "network": "网络连接中断",
    "provider_auth": "接入点鉴权失败",
    "config": "模型配置错误",
    "parse": "模型输出无法解析",
    "engine": "引擎内部错误",
}


class ForgeRequirementBody(BaseModel):
    """提炼请求体：**两个内容位**（正文 + 需求）+ 条数/名字。

    刻意只有"用户粘贴的文本"这些字段：没有 novel、没有 chapter、没有 target ——
    调用方无从指定作品库里的任何东西。作者没粘贴的内容，端点这一层就拿不到。
    """

    #: 【正文内容】要提炼的文本（作者自己粘贴）
    source_text: str
    #: 【提炼需求】要从这段正文里提炼什么（作者自己写）
    requirement: str
    #: 条数上限；0 或缺省 = 默认值（见 constraint_forge.DEFAULT_MAX_ITEMS）
    max_items: int = 0
    #: 期望的名字（可空 = 由需求首句自动取名）
    title_hint: str = ""


def register_forge_api(app: Any, hub: Any, default_novel: str) -> None:
    from fastapi import HTTPException

    # 显式声明本模块的隔离边界：签名与其它扩展模块统一，但这两个参数**不被使用**——
    # 本模块任何一条代码路径都不应触碰作品数据（静态回归：tests/test_constraint_forge.py）。
    _ = (hub, default_novel)

    def _runtime_prompt() -> str:
        """本智能体的最终系统提示词（含用户覆盖与第零条，与其它智能体同源装配）。"""
        from src.web.inkforge_windows import agent_prompt

        return agent_prompt(forge.AGENT_KEY)

    @app.get("/api/style-forge/agent")
    def forge_agent_view() -> JSONResponse:
        """风格工坊第三页的智能体名片：它是谁、能看什么、看不到什么。"""
        return JSONResponse({
            "key": forge.AGENT_KEY,
            "label": forge.AGENT_LABEL,
            "role": forge.MODEL_ROLE,
            "prompt": _runtime_prompt(),
            "max_items": {
                "default": forge.DEFAULT_MAX_ITEMS,
                "min": forge.MIN_MAX_ITEMS,
                "max": forge.MAX_MAX_ITEMS,
            },
            "limits": {
                "source_max": forge.MAX_SOURCE_CHARS,
                "source_min": forge.MIN_SOURCE_CHARS,
                "requirement_max": forge.MAX_REQUIREMENT_CHARS,
            },
            # 视野声明：UI 直接展示。语义（2026-09-19 订正）：**看什么由你决定** ——
            # 你在输入框里粘的才算给它看；它不会自己去翻你的作品库。
            "sees": [
                "你在【正文内容】框里粘贴的文本",
                "你在【提炼需求】框里写的要求",
            ],
            "never_sees": [
                "你作品库里的任何文件（正文 / 章节 / 大纲 / 设定 / 素材 / 摘要）",
                "你没有粘贴进输入框的任何内容",
                "任何以 novel 参数传入的内容（端点根本不收这个参数）",
            ],
        })

    @app.post("/api/style-forge/constraints")
    def forge_constraints(body: ForgeRequirementBody) -> JSONResponse:
        """正文 + 需求 → 条目（一次模型调用，不落盘、不读库）。

        落盘（存进约束库）是另一个动作，由前端调用既有 `POST /api/custom-skills` 完成——
        本端点因此既不需要触碰作品库，也不需要重复一套落盘语义。
        """
        source_text = body.source_text.strip()
        requirement = body.requirement.strip()
        if not source_text:
            raise HTTPException(400, "请先把要提炼的正文粘贴进【正文内容】框（它只提炼你给的文本）")
        if len(source_text) < forge.MIN_SOURCE_CHARS:
            raise HTTPException(
                400,
                f"正文太短（{len(source_text)} 字，至少 {forge.MIN_SOURCE_CHARS} 字）——"
                "贴一段够看的正文，提炼才有依据。",
            )
        if len(source_text) > forge.MAX_SOURCE_CHARS:
            raise HTTPException(
                400,
                f"正文过长（{len(source_text)} 字，上限 {forge.MAX_SOURCE_CHARS} 字）。"
                "请分段提炼（一次一段），效果也更好。",
            )
        if not requirement:
            raise HTTPException(400, "请写下【提炼需求】——说清你要我从这段正文里提炼什么")
        if len(requirement) > forge.MAX_REQUIREMENT_CHARS:
            raise HTTPException(
                400,
                f"提炼需求过长（{len(requirement)} 字，上限 {forge.MAX_REQUIREMENT_CHARS} 字）。"
                "需求写清「要提炼什么」即可，正文请放【正文内容】框。",
            )

        started = time.time()
        try:
            result = forge.extract_constraints(
                requirement,
                source_text,
                max_items=body.max_items,
                title_hint=body.title_hint,
                system_prompt=_runtime_prompt(),
            )
        except ValueError as exc:
            # 输入/输出不可用（模型空回或输出无法解析为条目）：不把空结果当成功返回
            logger.warning("提炼产出不可用：%s", exc)
            raise HTTPException(
                422,
                f"提炼结果不可用：{exc}。可把需求写得更具体，或换一段正文后重试。",
                headers={"X-Inkforge-Error-Source": "parse"},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 模型/配置错误透传给前端
            logger.exception("约束提炼失败")
            source, hint = classify_failure(exc)
            # 坑（本模块实测踩到）：HTTP 响应头只能是 latin-1，把中文 hint 塞进
            # X-Inkforge-Hint 会在 starlette 组装响应时抛 UnicodeEncodeError ——
            # 于是"模型调用失败"反而变成 500 且丢失全部归因信息。
            # 因此：响应头只放 ASCII 的来源码，可执行提示放进 detail 正文。
            #
            # 对抗排查修正（2026-09-19）：上游 5xx 时 SDK 会把**上游响应体原文**塞进异常，
            # 原样回传可能把凭据/上游内部信息带出去 → 统一走 redact_secrets 脱敏 + 截断。
            raise HTTPException(
                502,
                f"模型调用失败（{_SOURCE_LABEL.get(source, source)}）："
                f"{redact_secrets(f'{type(exc).__name__}: {exc}')}\n\n建议：{hint}",
                headers={"X-Inkforge-Error-Source": source},
            ) from exc

        return JSONResponse({
            "ok": True,
            "agent": {"key": forge.AGENT_KEY, "label": forge.AGENT_LABEL},
            "title": result["title"],
            "constraints": result["constraints"],
            "items": result["items"],
            "count": result["count"],
            "role": result["role"],
            "model": result["model"],
            "provider": result["provider"],
            "elapsed": round(time.time() - started, 2),
            # 回执里显式复述输入边界：让"它到底看了什么"在 UI 上永远可见
            "isolation": "只提炼你在本页粘贴的正文；未读取你作品库里的任何文件。",
        })

    logger.info("风格工坊约束提炼 API 已注册（/api/style-forge/agent · /api/style-forge/constraints）")
