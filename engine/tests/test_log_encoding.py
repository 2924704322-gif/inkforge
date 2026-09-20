"""日志/管道编码回归：引擎输出必须是 UTF-8，否则上层按 UTF-8 解码会吃掉中文。

由来（2026-09-19 实测复现）：Python 在 Windows 下被 spawn 成管道子进程时，
`sys.stdout.encoding` 取 locale 编码（实测 `gbk`）→ 中文日志写成 GBK 字节；
Electron supervisor 用 Node 的 `chunk.toString()`（默认 UTF-8）解码 → 每个字节变 U+FFFD，
落到 `desktop-<日期>.log` 后**不可逆**（实测一条中文日志产生 8 个替换符，排查时只剩 ASCII 骨架可读）。

两道保险，这里各钉一条：
1. 启动方（supervisor）注入 `PYTHONIOENCODING=utf-8` / `PYTHONUTF8=1`（主修）；
2. `setup_logging()` 对**非 TTY** 流兜底 reconfigure（CLI / 冒烟脚本 / 漏设环境变量的启动方式）。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parents[1]

CHINESE = "中文日志一行：第 1 章字数 838 / 目标 3000"


class _FakeStream:
    """最小 stdout 替身：只关心 isatty 与 reconfigure 是否被调用。"""

    def __init__(self, tty: bool):
        self._tty = tty
        self.reconfigured: list[dict] = []

    def isatty(self) -> bool:
        return self._tty

    def reconfigure(self, **kwargs) -> None:
        self.reconfigured.append(kwargs)


def test_redirected_stream_forced_to_utf8_but_tty_left_alone(monkeypatch):
    """兜底只在**非 TTY** 生效：交互式控制台保持原编码（中文 Windows 控制台靠 cp936 才正常显示）。"""
    from src.utils import logger as logger_mod

    redirected, tty = _FakeStream(tty=False), _FakeStream(tty=True)
    monkeypatch.setattr(sys, "stdout", redirected)
    monkeypatch.setattr(sys, "stderr", tty)

    logger_mod._force_utf8_on_redirected_streams()  # noqa: SLF001 - 直测该兜底

    assert redirected.reconfigured == [
        {"encoding": "utf-8", "errors": "backslashreplace"}
    ], "被重定向的流必须改成 UTF-8（且不可丢字符）"
    assert tty.reconfigured == [], "TTY 不得被动（否则中文控制台会变乱码）"


def test_log_file_keeps_chinese_verbatim(sandbox):
    """文件 handler 必须逐字保留中文（编码口径即目标口径）。

    注意：`logging.basicConfig(handlers=...)` 在根日志器**已有 handler 时是 no-op**
    （pytest 自己就挂了 handler），所以这里先把根 handler 摘下来，让 setup_logging 真正生效。
    """
    from src.utils import logger as logger_mod

    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    logger_mod._CONFIGURED = False  # noqa: SLF001 - 让本次 setup 真正执行
    try:
        log_path = sandbox.runtime / "logs" / "encoding-probe.log"
        logger_mod.setup_logging("INFO", log_path)
        logging.getLogger("encoding-probe").info(CHINESE)
        for handler in root.handlers:
            handler.flush()

        text = log_path.read_text(encoding="utf-8")
        assert CHINESE in text, "日志文件里的中文被破坏"
        assert "\ufffd" not in text, "日志文件出现替换符（编码链路又坏了）"
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
        logger_mod._CONFIGURED = False  # noqa: SLF001


def test_piped_engine_output_is_utf8_readable():
    """端到端（管道口径）：子进程经 stdout=PIPE 输出中文，按 **UTF-8** 解码必须逐字可读。

    这正是 supervisor `chunk.toString()`（默认 UTF-8）的解码口径 ——
    修复前这里是 8 个 U+FFFD，修复后必须是原文。
    """
    code = (
        "from src.utils.logger import setup_logging;"
        "setup_logging('INFO');"
        "import logging;"
        f"logging.getLogger('pipe-probe').info({CHINESE!r})"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        cwd=str(ENGINE_DIR),
    )
    out = proc.stdout.decode("utf-8", errors="replace")

    assert "\ufffd" not in out, f"管道输出按 UTF-8 解码出现替换符：{out[:200]!r}"
    assert CHINESE in out, f"中文没能在管道里逐字保留：{out[:200]!r}"


def test_supervisor_injects_utf8_env():
    """主修在启动方：supervisor 必须给引擎子进程注入 UTF-8 环境变量。

    静态闸的理由：这两行被删掉时**不会报任何错**，只会让日志中文重新变成替换符
    （症状出现在很久以后，且表现为"日志看不懂"而不是报错）——正是上一轮排查踩的坑。
    """
    supervisor = (
        ENGINE_DIR.parent / "apps" / "desktop" / "src" / "main" / "engine-supervisor.ts"
    )
    assert supervisor.exists(), f"未找到 supervisor：{supervisor}"
    text = supervisor.read_text(encoding="utf-8")
    assert "PYTHONIOENCODING" in text and "'utf-8'" in text, "supervisor 未注入 PYTHONIOENCODING=utf-8"
    assert "PYTHONUTF8" in text, "supervisor 未注入 PYTHONUTF8=1"
