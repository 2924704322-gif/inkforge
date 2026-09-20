"""统一日志：rich 控制台输出 + 可选文件输出。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from rich.logging import RichHandler

_CONFIGURED = False


def _force_utf8_on_redirected_streams() -> None:
    """把**被重定向（非 TTY）**的 stdout/stderr 强制成 UTF-8。

    由来（2026-09-19 实测复现）：Windows 下 Python 被 spawn 成管道子进程时，
    `sys.stdout.encoding` 取 locale 编码（实测 `gbk`）→ 中文日志写成 GBK 字节；
    上层若按 UTF-8 解码（Node `chunk.toString()` 的默认行为、Electron supervisor 的日志捕获），
    每个字节都变成 U+FFFD，落盘后**不可逆**（一条中文日志实测产生 8 个替换符），
    排障时只能靠 ASCII 骨架猜。

    两道保险：启动方（Electron supervisor）注入 `PYTHONIOENCODING=utf-8` 是**主修**，
    这里是非 TTY 场景的**兜底**（CLI / 冒烟脚本 / 任何忘了设环境变量的启动方式都能受益）。

    **只动非 TTY**：交互式控制台保持原编码 —— 中文 Windows 控制台用 cp936 才能正常显示，
    强行 UTF-8 反而会把中文显示成乱码。`errors="backslashreplace"` 保证真遇到不可编码字符时
    也不会丢成替换符（宁可看到转义序列，也不要看到一串 U+FFFD）。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is None or stream.isatty():
                continue
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 编码兜底失败绝不能影响主流程
            continue


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """初始化根日志器（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _force_utf8_on_redirected_streams()
    handlers: list[logging.Handler] = [
        RichHandler(rich_tracebacks=True, show_path=False, markup=True)
    ]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        handlers.append(fh)
    logging.basicConfig(
        level=level.upper(), format="%(message)s", datefmt="[%X]", handlers=handlers
    )
    # 降低三方库噪声
    for noisy in ("httpx", "httpcore", "chromadb", "openai", "anthropic", "git"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
