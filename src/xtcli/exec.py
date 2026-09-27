"""子进程执行: 环境净化 + 超时 + 退出码 + UTF-8。

**绝不使用 `shell=True`** —— 一切走 argv 列表, 避免 cmd/sh 的引号与转义差异。

环境净化是这一层最重要的职责, 它把两个实测踩到的坑变成结构性禁止:

* ``GCC_ROOT`` —— Arm GNU Toolchain **自己会读**这个变量来定位内部子程序(cc1)。
  GNU make 会把命令行变量自动导出到子进程, 于是 ``make GCC_ROOT=...`` 会让
  编译器报 ``cannot execute 'cc1': CreateProcess: No such file or directory``。
  实测确认: 不设该变量正常, 设为工具链 bin 目录即失败。
* ``CONDA_*`` —— conda 注入的变量会随每个 make/gcc/openocd 子进程传播并影响
  DLL 搜索。本工具不依赖 conda, 连同 PATH 里的 anaconda 目录一起剔除。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .log import raw as log_raw

DENY_ENV: tuple[str, ...] = (
    "GCC_ROOT",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "CONDA_SHLVL",
    "CONDA_EXE",
    "CONDA_PYTHON_EXE",
    "CONDA_PROMPT_MODIFIER",
)

PATH_DENY_MARKERS: tuple[str, ...] = ("anaconda", "miniconda", "\\conda\\", "/conda/")


@dataclass
class RunResult:
    argv: list[str]
    exit_code: int
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.interrupted

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def harden_path(extra_front: Iterable[str] = ()) -> str:
    """前置指定目录, 过滤 conda 目录, 去重。

    前置 msys 的 ``usr\\bin`` 是为了让 make 稳定找到 ``sh.exe``: 否则 make 可能
    用 ``cmd.exe`` 执行 ``mkdir -p``, 从而凭空造出一个名为 ``-p`` 的目录
    (该事故在本项目真实发生过, 工程里留下了那个空目录)。
    """
    parts: list[str] = []
    for item in extra_front:
        if item and item not in parts:
            parts.append(str(item))
    for item in os.environ.get("PATH", "").split(os.pathsep):
        if not item:
            continue
        low = item.lower().replace("/", "\\")
        if any(marker in low for marker in PATH_DENY_MARKERS):
            continue
        if item not in parts:
            parts.append(item)
    return os.pathsep.join(parts)


def child_env(prepend_path: Iterable[str] = (), extra: dict[str, str] | None = None) -> dict[str, str]:
    """构造受控的子进程环境。

    ``extra`` 在 deny-list **之后**应用, 所以像 IDF_PATH/IDF_TOOLS_PATH 这类
    工具链必需变量不会被误删。
    """
    child = dict(os.environ)
    for name in DENY_ENV:
        child.pop(name, None)
    child["PATH"] = harden_path(prepend_path)
    child["PYTHONUTF8"] = "1"
    child["PYTHONIOENCODING"] = "utf-8"
    if extra:
        child.update({k: str(v) for k, v in extra.items()})
    return child


def run(
    argv: Iterable[str | Path],
    *,
    cwd: str | Path | None = None,
    prepend_path: Iterable[str] = (),
    extra_env: dict[str, str] | None = None,
    timeout: float | None = None,
    echo: bool = True,
    quiet: bool = False,
) -> RunResult:
    """执行原生程序并捕获输出（stdout/stderr 合并）。"""
    args = [str(a) for a in argv]
    if not args:
        return RunResult(argv=[], exit_code=-1, lines=["空命令"])
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            env=child_env(prepend_path, extra_env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return RunResult(argv=args, exit_code=127, lines=[f"找不到可执行文件: {args[0]}"])
    except PermissionError as exc:
        return RunResult(argv=args, exit_code=126, lines=[f"无法执行 {args[0]}: {exc}"])
    except subprocess.TimeoutExpired as exc:
        lines = _decode_lines(exc.stdout)
        if echo and not quiet:
            for line in lines:
                log_raw(line)
        return RunResult(argv=args, exit_code=-1, lines=lines, timed_out=True)
    except KeyboardInterrupt:
        return RunResult(argv=args, exit_code=130, lines=["已被用户中断 (Ctrl-C)"], interrupted=True)

    lines = _decode_lines(proc.stdout)
    if echo and not quiet:
        for line in lines:
            log_raw(line)
    return RunResult(argv=args, exit_code=int(proc.returncode), lines=lines)


def _decode_lines(blob: object) -> list[str]:
    if not blob:
        return []
    text = blob.decode("utf-8", "replace") if isinstance(blob, bytes) else str(blob)
    return text.replace("\r\n", "\n").splitlines()
