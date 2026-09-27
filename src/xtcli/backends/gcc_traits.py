"""工程特征识别: 判断一个目录是什么工程、值不值得本后端接手。

只做**只读**判断 —— detect 得分决定 auto 分发是否交给本后端; 拿不准就给低分,
让上层的"没有 backend 能处理该目录"来兜底。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import ioc

_MAKEFILE_SENTINEL = ("xtcli:generated", "include xtcli/makefile")


# ===========================================================================
# 特征识别
# ===========================================================================
@dataclass
class Traits:
    root: Path
    cproject: bool = False
    makefile: bool = False          # 用户自己的构建系统
    makefile_owned: bool = False    # xtcli 生成的转发壳
    xt_skeleton: bool = False
    ewp: Path | None = None
    uvprojx: bool = False
    ioc: Path | None = None
    core_src: bool = False
    core_startup: bool = False
    ld_files: list[Path] = field(default_factory=list)
    platformio: bool = False


def _is_owned_makefile(path: Path) -> bool:
    """区分"用户自己的 Makefile"与"xtcli 生成的转发壳"。

    否则第二次 init 会把自己上一次的产物误判成"工程自带构建系统", 直接跳过
    初始化, 留下过期的 xtcli/rules.mk。
    """
    try:
        head = "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:12]).lower()
    except OSError:
        return False
    return any(token in head for token in _MAKEFILE_SENTINEL)


def _find_ide_project(root: Path, pattern: str, max_depth: int = 3) -> Path | None:
    for path in sorted(root.rglob(pattern)):
        if "xtcli" in {part.lower() for part in path.parts}:
            continue
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:  # pragma: no cover
            continue
        if depth <= max_depth:
            return path
    return None


def get_traits(root: Path) -> Traits:
    traits = Traits(root=root)
    traits.cproject = (root / ".cproject").is_file()
    makefile = root / "Makefile"
    if makefile.is_file():
        if _is_owned_makefile(makefile):
            traits.makefile_owned = True
        else:
            traits.makefile = True
    traits.xt_skeleton = (root / "xtcli" / "Makefile").is_file()
    ewp = _find_ide_project(root, "*.ewp")
    if ewp is not None:
        traits.ewp = ewp
    traits.uvprojx = _find_ide_project(root, "*.uvprojx") is not None
    traits.ioc = ioc.find_ioc(root)
    traits.core_src = (root / "Core" / "Src").is_dir()
    traits.core_startup = (root / "Core" / "Startup").is_dir()
    traits.ld_files = sorted(root.glob("*.ld"))
    traits.platformio = (root / "platformio.ini").is_file()
    return traits


def detect_score(root: Path) -> int:
    if not root.is_dir():
        return 0
    traits = get_traits(root)
    score = 0
    if traits.cproject:
        score += 60
    if traits.ioc:
        score += 30
    if traits.core_src:
        score += 25
    if traits.makefile:
        score += 15
    if traits.ld_files:
        score += 10
    if traits.ewp or traits.uvprojx:
        score += 10
    if score > 0 and (traits.cproject or traits.ioc or traits.core_src):
        return min(score, 100)
    return 0


