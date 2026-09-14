"""引擎本地 HTTP 访问控制（S1-4 安全加固）。

背景：引擎监听 127.0.0.1 供 Electron 主进程代理调用。原实现无任何鉴权，
本机任意进程 / 浏览器页面均可读写全部作品数据（含密钥导流、任意文件读取）。

本模块提供两道互相独立的门：

1. **Bearer token**（主门）：桌面壳启动子进程时生成随机 token，经环境变量
   ``INKFORGE_ENGINE_TOKEN`` 注入；每个 ``/api/*`` 请求必须携带
   ``Authorization: Bearer <token>``，否则 401。
   token 只存在于主进程与引擎子进程内存中，渲染层无法读到。

2. **Origin 拒绝**（纵深防御）：渲染层经 IPC 转发，主进程的 fetch 不带 Origin；
   任何携带 Origin 头的请求都来自浏览器上下文，一律 403。
   即使 token 意外泄露，也能挡住「恶意网页直接打 localhost」这一类攻击。

向后兼容：``INKFORGE_ENGINE_TOKEN`` 未设置时不启用鉴权——CLI 入口、旧内置单页
与单元测试仍可直接访问（这些场景不存在本机第三方进程威胁模型）。
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

TOKEN_ENV = "INKFORGE_ENGINE_TOKEN"

# 不需要鉴权的路径前缀（非 /api 前缀的静态/文档入口）
_PUBLIC_PREFIXES: tuple[str, ...] = ("/docs", "/redoc", "/openapi.json", "/favicon.ico")


def engine_token() -> str:
    """读取当前进程的引擎访问 token；空串表示未启用鉴权。"""
    return (os.environ.get(TOKEN_ENV) or "").strip()


def _bearer_of(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return value.strip()


class EngineAccessMiddleware(BaseHTTPMiddleware):
    """Bearer token + Origin 拒绝的鉴权中间件。"""

    def __init__(self, app, token: str, public_prefixes: Iterable[str] = _PUBLIC_PREFIXES):
        super().__init__(app)
        self._token = token
        self._public_prefixes = tuple(public_prefixes)

    def _is_public(self, path: str) -> bool:
        if path == "/":
            return True
        return any(path.startswith(prefix) for prefix in self._public_prefixes)

    async def dispatch(self, request: Request, call_next):
        if self._is_public(request.url.path):
            return await call_next(request)

        # 纵深防御：浏览器上下文发起的请求一律拒绝（主进程代理不带 Origin）
        if request.headers.get("origin"):
            return JSONResponse(
                {"detail": "拒绝来自浏览器上下文的请求（Origin 头不允许）"},
                status_code=403,
            )

        supplied = _bearer_of(request)
        if not supplied or not hmac.compare_digest(supplied, self._token):
            return JSONResponse(
                {
                    "detail": (
                        "未授权：缺少或错误的引擎访问令牌。"
                        "桌面端请经主进程代理访问；"
                        f"命令行调试请设置 {TOKEN_ENV} 并携带 Authorization: Bearer <token>。"
                    )
                },
                status_code=401,
            )
        return await call_next(request)


def install_auth(app, token: str | None = None) -> bool:
    """给 FastAPI app 装上鉴权中间件；token 为空则不装（返回 False）。"""
    effective = (token if token is not None else engine_token()).strip()
    if not effective:
        return False
    app.add_middleware(EngineAccessMiddleware, token=effective)
    return True
