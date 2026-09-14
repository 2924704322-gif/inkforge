"""统一日志：rich 控制台输出 + 可选文件输出。"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.logging import RichHandler

_CONFIGURED = False


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """初始化根日志器（幂等）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
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
