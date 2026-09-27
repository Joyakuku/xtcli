"""烧录提供者（flash providers）。

两条路径, 各自的定位:

* **openocd** —— 既有路径, STM32 上已实测。需要一份 target 配置
  (``target/stm32f1x.cfg`` 之类), 所以只覆盖有现成配置的芯片。
* **pyocd** —— 可选路径。价值在于用 **CMSIS-Pack 自动获取 flash 算法**,
  覆盖大量 Cortex-M 厂商, 不必为每家写 openocd target 配置。

pyOCD 装在**独立 venv**（``<home>/.venv-pyocd``）里, 由 ``setup.ps1 -Pyocd``
创建。这样核心运行时的"零第三方依赖"不受影响, pyOCD 的依赖链（约 25 个包）
也不可能破坏核心。

实测（STM32F103RC + ST-Link, pyocd 0.45.1）::

    pyocd json -p                                  -> 探针 JSON（含 unique_id）
    pyocd load -t stm32f103rc <elf>                -> 写入 + 默认按 CRC 校验, 退出码 0
    pyocd cmd -t <target> -c "read32 <addr> <len>" -> 独立回读（注意 len 是**字节**数）
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import env, log
from . import exec as xt_exec

PYOCD_VENV_NAME = ".venv-pyocd"
_READ_LINE_RE = re.compile(r"^([0-9a-fA-F]{8}):\s*([^|]*)\|")
_WORD_RE = re.compile(r"^[0-9a-fA-F]{8}$")


@dataclass
class PyocdTool:
    exe: Path
    version: str = ""
    source: str = ""


def find_pyocd() -> PyocdTool | None:
    """按优先级找一个可用的 pyocd: 独立 venv > 项目 venv > PATH。"""
    candidates: list[tuple[Path, str]] = [
        (env.home() / PYOCD_VENV_NAME / "Scripts" / "pyocd.exe", "隔离 venv (.venv-pyocd)"),
        (env.venv_dir() / "Scripts" / "pyocd.exe", "项目 venv"),
    ]
    import shutil

    on_path = shutil.which("pyocd")
    if on_path:
        candidates.append((Path(on_path), "PATH"))

    for exe, source in candidates:
        if not exe.is_file():
            continue
        result = xt_exec.run([exe, "--version"], echo=False, timeout=60)
        version = result.lines[0].strip() if result.lines else ""
        if result.exit_code == 0 or version:
            return PyocdTool(exe=exe, version=version, source=source)
    return None


def pyocd_target_name(device: str, data: dict | None = None) -> str:
    """由 Cubemx/IAR 的型号推出 pyOCD 的 target 名。

    ``STM32F103RCTx`` -> ``STM32F103RC`` -> ``stm32f103rc``（pyOCD 用 CMSIS 型号小写）。
    设备表里若显式给了 ``pyocd`` 字段则以它为准。
    """
    if not device:
        return ""
    if data:
        families = data.get("families") or {}
        for entry in families.values():
            if isinstance(entry, dict) and entry.get("devices", {}).get(device, {}).get("pyocd"):
                return str(entry["devices"][device]["pyocd"])
    cleaned = re.sub(r"(?:Tx|xx|x)$", "", device.strip())
    return cleaned.lower()


def list_probes(tool: PyocdTool) -> list[dict]:
    """``pyocd json -p`` 的解析结果（探针列表）。"""
    result = xt_exec.run([tool.exe, "json", "-p"], echo=False, timeout=120)
    text = result.text
    start = text.find("{")
    if start < 0:
        return []
    try:
        data = json.loads(text[start:])
    except ValueError:
        return []
    boards = data.get("boards")
    return [b for b in boards if isinstance(b, dict)] if isinstance(boards, list) else []


def load(
    tool: PyocdTool,
    artifact: Path,
    target: str,
    unique_id: str = "",
    *,
    timeout: float | None = None,
) -> xt_exec.RunResult:
    """写入并校验（pyOCD 默认按 CRC 校验, 除非显式 --trust-crc）。"""
    argv: list[str | Path] = [tool.exe, "load", "-t", target]
    if unique_id:
        argv += ["-u", unique_id]
    argv.append(artifact)
    return xt_exec.run(argv, timeout=timeout)


def reset(tool: PyocdTool, target: str, unique_id: str = "") -> xt_exec.RunResult:
    argv: list[str | Path] = [tool.exe, "reset", "-t", target]
    if unique_id:
        argv += ["-u", unique_id]
    return xt_exec.run(argv, echo=False, timeout=120)


def read_words(tool: PyocdTool, target: str, base: str, count: int, unique_id: str = "") -> list[int] | None:
    """独立回读: 直接读芯片内存, 不经过烧写流程。

    注意 ``read32`` 的第二个参数是**字节数**, 所以要传 ``4 * count``。
    """
    argv: list[str | Path] = [tool.exe, "cmd", "-t", target]
    if unique_id:
        argv += ["-u", unique_id]
    argv += ["-c", f"read32 {base} {4 * count}"]
    result = xt_exec.run(argv, echo=False, timeout=120)
    words: list[int] = []
    for line in result.lines:
        match = _READ_LINE_RE.match(line.strip())
        if not match:
            continue
        for token in match.group(2).split():
            if _WORD_RE.match(token):
                words.append(int(token, 16))
    return words[:count] if len(words) >= count else None


def target_supported(tool: PyocdTool, target: str) -> bool:
    """确认 pyOCD 认识这个 target（不认识时给出可操作的提示）。"""
    result = xt_exec.run([tool.exe, "list", "-t"], echo=False, timeout=180)
    pattern = re.compile(rf"^\s*{re.escape(target)}\s", re.M)
    return bool(pattern.search(result.text))


def describe(tool: PyocdTool | None) -> list[str]:
    """给 doctor 用的描述行。"""
    if tool is None:
        return ["pyocd       : (未安装 —— 可选; 需要时运行 xtcli-setup -Pyocd)"]
    return [f"pyocd       : {tool.exe}   [{tool.version}]  ({tool.source})"]


def missing_hint() -> list[str]:
    return [
        "pyOCD 未安装（可选组件）",
        "安装: xtcli-setup -Pyocd      (会创建独立的 .venv-pyocd, 不影响核心的零依赖)",
        "或用 -Interface 走 openocd 路径",
    ]


def warn_unavailable() -> None:
    for line in missing_hint():
        log.info(line)
