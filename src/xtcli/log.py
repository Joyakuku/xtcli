"""分级日志。

颜色只在 tty 上启用（管道/重定向时不输出 ANSI, 便于被工具捕获）;
支持 NO_COLOR、-Quiet、-LogFile（落盘留现场）。
"""

from __future__ import annotations

import os
import sys
from typing import IO

_ANSI = {
    "step": "\033[36m",
    "info": "\033[90m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "err": "\033[31m",
    "raw": "",
}
_RESET = "\033[0m"
_PREFIX = {
    "step": "\n==> ",
    "info": "    ",
    "ok": "[ok] ",
    "warn": "[!]  ",
    "err": "[x]  ",
    "raw": "",
}

_quiet = False
_color = False
_logfp: IO[str] | None = None


def _detect_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def configure(*, quiet: bool = False, log_file: str | None = None) -> None:
    global _quiet, _color, _logfp
    _quiet = bool(quiet)
    _color = _detect_color()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if log_file:
        # 长生命周期句柄, 由 log.close() 负责关闭
        _logfp = open(log_file, "a", encoding="utf-8")  # noqa: SIM115


def close() -> None:
    global _logfp
    if _logfp:
        try:
            _logfp.flush()
            _logfp.close()
        finally:
            _logfp = None


def log(level: str, message: str) -> None:
    if _quiet and level not in ("err", "raw"):
        return
    text = f"{_PREFIX.get(level, '')}{message}"
    if _color and _ANSI.get(level):
        sys.stdout.write(f"{_ANSI[level]}{text}{_RESET}\n")
    else:
        sys.stdout.write(f"{text}\n")
    sys.stdout.flush()
    if _logfp:
        _logfp.write(f"{text}\n")
        _logfp.flush()


def step(message: str) -> None:
    log("step", message)


def info(message: str) -> None:
    log("info", message)


def ok(message: str) -> None:
    log("ok", message)


def warn(message: str) -> None:
    log("warn", message)


def err(message: str) -> None:
    log("err", message)


def raw(message: str) -> None:
    log("raw", message)
