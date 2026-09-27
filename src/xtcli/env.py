"""路径与运行环境。

这里集中了所有平台相关的假设, 便于将来做跨平台:
  * home 定位（<home>\\src\\xtcli\\env.py -> <home>）
  * venv / conda 双重自检 —— 防止误用系统或 anaconda 的 python
  * junction/symlink 解析（E:\\env\\Arm\\cortex-m 就是 junction）
  * 原子写（生成物中断不留半个文件）
"""

from __future__ import annotations

import contextlib
import itertools
import os
import sys
import time
from pathlib import Path, PurePath

from .errors import Exit, XtError

_ENV_OVERRIDE = "XTCLI_HOME"
_CONDA_MARKERS = ("anaconda", "miniconda", "miniforge", "mambaforge", "conda")


# ---------------------------------------------------------------------------
# 位置
# ---------------------------------------------------------------------------
def home() -> Path:
    override = os.environ.get(_ENV_OVERRIDE)
    if override and Path(override).is_dir():
        return Path(override).resolve()
    # <home>\src\xtcli\env.py
    return Path(__file__).resolve().parents[2]


def venv_dir() -> Path:
    return home() / ".venv"


def venv_python() -> Path:
    return venv_dir() / "Scripts" / "python.exe"


def config_path() -> Path:
    return home() / "xtcli.json"


def data_dir() -> Path:
    return home() / "data"


def assets_dir() -> Path:
    """包内资产（rules.mk / 链接脚本 / 运行时桩）。"""
    return Path(__file__).resolve().parent / "assets"


# ---------------------------------------------------------------------------
# 解释器自检
# ---------------------------------------------------------------------------
def looks_like_conda(path: str | PurePath) -> bool:
    """判断解释器/环境是否属于 conda 家族。

    只看目录名不够 —— Miniforge / mambaforge 的目录名里没有 "conda",
    ``conda create -p E:\\envs\\ee`` 更是任意路径。所以补上**结构特征**:
    conda 系环境一定带 ``conda-meta/`` 目录; 环境变量 ``CONDA_PREFIX`` 指向的
    环境同样算 (被 conda 激活的 venv 也会带上这个变量, 但那时底层确实是 conda)。
    """
    text = str(path).lower().replace("/", "\\")
    if any(marker in text for marker in _CONDA_MARKERS):
        return True

    probe = Path(path)
    base = probe if probe.is_dir() else probe.parent
    try:
        if (base / "conda-meta").is_dir():
            return True
    except OSError:  # pragma: no cover
        return False

    env_prefix = os.environ.get("CONDA_PREFIX")
    if env_prefix:
        try:
            prefix = Path(env_prefix)
            if base == prefix or prefix in base.parents:
                return True
        except (OSError, ValueError):  # pragma: no cover
            return False
    return False


def assert_running_in_venv() -> None:
    """确保运行在本项目的 venv 里, 且底座不是 conda。

    venv 只隔离 site-packages, 它的 python.exe 加载的是 base 的 pythonXY.dll
    与标准库。所以"建在 anaconda 上的 venv"其实是 anaconda + 一层包目录覆盖,
    不是独立环境 —— 必须显式排除。
    """
    expected = venv_dir()
    try:
        actual = Path(sys.prefix).resolve()
    except OSError:  # pragma: no cover
        actual = Path(sys.prefix)
    if actual != expected:
        raise XtError(
            f"未在项目 venv 中运行: 当前解释器 {sys.executable}",
            code=int(Exit.ENVIRONMENT),
            hint=f"请先运行 xtcli-setup 创建 {expected}, 或改用 bin\\ 下的命令",
        )

    base = Path(getattr(sys, "base_prefix", sys.prefix))
    base_exe = getattr(sys, "_base_executable", "") or ""
    if looks_like_conda(base) or looks_like_conda(base_exe):
        raise XtError(
            f"venv 底座是 conda 解释器, 拒绝运行: {base}",
            code=int(Exit.ENVIRONMENT),
            hint="用独立 CPython 重建: xtcli-setup -Force",
        )


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------
def to_posix(path: str | PurePath) -> str:
    return str(path).replace("\\", "/")


def real_path(path: str | PurePath) -> Path:
    """解析 junction / symlink。

    E:\\env\\Arm\\cortex-m 是指向版本化目录的 junction; 不解析会让工具发现
    把同一套工具链算成两份。
    """
    try:
        return Path(os.path.realpath(str(path)))
    except OSError:  # pragma: no cover
        return Path(path)


def relative_to(base: str | PurePath, path: str | PurePath) -> str:
    """绝对路径 -> 相对 base 的 posix 路径; 不在 base 内则原样返回绝对路径。"""
    try:
        rel = os.path.relpath(str(path), str(base))
    except ValueError:  # 跨盘符
        return to_posix(path)
    return to_posix(rel)


def is_within(child: str | PurePath, parent: str | PurePath) -> bool:
    try:
        Path(real_path(child)).relative_to(Path(real_path(parent)))
        return True
    except ValueError:
        return False


_TMP_COUNTER = itertools.count()
_REPLACE_RETRIES = 5
_REPLACE_DELAY = 0.02


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """``os.replace`` 带重试。

    Windows 上两个进程/线程同时 replace 到**同一个目标**时, 会短暂报
    ``PermissionError: [WinError 5] 拒绝访问``（目标在改名的一瞬间被占用）。
    实测: 两个 xtcli 同时写同一个 config.mk 就会撞上。重试几次即可, 无需锁。
    """
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY * (attempt + 1))


def atomic_write_text(path: str | PurePath, text: str) -> None:
    """原子写文本: 临时文件 + os.replace。

    生成物（config.mk 之类）被中断写坏, 会导致"能编但错"的固件 —— 这类事故
    已经踩过一次, 所以一律原子写。

    临时文件名必须**每次唯一**（pid + 序号）: 固定的 ``<名字>.xtcli-tmp`` 会让
    两个同时运行的 xtcli 互相踩同一个临时文件（IDE 触发 + 手动运行是很常见的
    组合), 于是可能把两次写入混合后的内容 replace 上去。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.xtcli-tmp-{os.getpid()}-{next(_TMP_COUNTER)}")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp, target)
    finally:
        # 失败/被打断时不留垃圾; 成功时 tmp 已被 replace, 这里是空操作
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()
