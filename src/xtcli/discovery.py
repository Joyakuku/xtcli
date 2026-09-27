"""工具发现。

主根 ``E:\\env``（工具链 / SDK 维护处）, 次根 ``E:\\env\\msys64``（make / openocd
实际所在）, 可被 ``XTCLI_ROOTS`` 或 xtcli.json 的 ``roots`` 覆盖或追加。

发现结果里的版本号会缓存到 xtcli.json; 每次运行只做存在性校验, 失效即重发现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config, env, exec
from .log import warn

DEFAULT_ROOTS = ("E:\\env", "E:\\env\\msys64")

_GCC_ALIAS_DIRS = (
    "Arm\\cortex-m",
    "Arm\\arm-none-eabi",
    "Arm\\gcc-arm-none-eabi",
    "gcc-arm-none-eabi",
)
_MAKE_RELS = (
    "usr\\bin\\make.exe",
    "mingw64\\bin\\make.exe",
    "mingw64\\bin\\mingw32-make.exe",
    "bin\\make.exe",
    "usr\\bin\\mingw32-make.exe",
)
_OPENOCD_RELS = ("mingw64\\bin\\openocd.exe", "bin\\openocd.exe", "mingw32\\bin\\openocd.exe")


@dataclass
class Toolchain:
    roots: list[Path] = field(default_factory=list)
    prefix: str = "arm-none-eabi-"
    gcc_path: Path | None = None
    gcc_root: str | None = None
    gcc_version: str | None = None
    gcc_alias: bool = False
    make: Path | None = None
    make_version: str | None = None
    openocd: Path | None = None
    openocd_version: str | None = None
    openocd_scripts: Path | None = None
    missing: list[str] = field(default_factory=list)

    def _sibling(self, tool: str) -> Path | None:
        if not self.gcc_root:
            return None
        candidate = Path(f"{self.gcc_root}/{self.prefix}{tool}.exe")
        if candidate.is_file():
            return candidate
        # PATH 兜底可能找到 POSIX 工具链 (没有 .exe), 那时同目录下的工具也没有后缀
        bare = Path(f"{self.gcc_root}/{self.prefix}{tool}")
        return bare if bare.is_file() else candidate

    @property
    def cc(self) -> Path | None:
        return self._sibling("gcc")

    @property
    def cxx(self) -> Path | None:
        return self._sibling("g++")

    @property
    def objcopy(self) -> Path | None:
        return self._sibling("objcopy")

    @property
    def size(self) -> Path | None:
        return self._sibling("size")

    @property
    def objdump(self) -> Path | None:
        return self._sibling("objdump")

    @property
    def nm(self) -> Path | None:
        return self._sibling("nm")


def search_roots(cfg: dict | None = None, skipped: list[str] | None = None) -> list[Path]:
    """按 "XTCLI_ROOTS -> xtcli.json 的 roots -> 默认根" 的顺序**追加**收集搜索根。

    ``XTCLI_ROOTS`` 是**追加**而不是替换 —— 实测确认默认根仍然生效; 想只用自己的
    根, 把默认根从搜索里排除的办法是用 xtcli.json 的 roots + 不设 XTCLI_ROOTS 也不
    依赖默认根 (默认根不存在时会被自动跳过)。

    ``skipped`` 收集"用户显式配置但不存在"的根 (默认根不存在属正常, 不报)。
    """
    import os

    cfg = cfg if cfg is not None else config.load()
    explicit: list[str] = []
    override = os.environ.get("XTCLI_ROOTS")
    if override:
        explicit.extend(part.strip() for part in override.split(";") if part.strip())
    roots = cfg.get("roots")
    if isinstance(roots, list):
        explicit.extend(str(r) for r in roots)

    raw: list[str] = list(explicit)
    raw.extend(DEFAULT_ROOTS)

    out: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        path = Path(item)
        if not path.is_dir():
            # 配置错了要说出来, 否则用户以为根生效了却什么都没找到
            if skipped is not None and item in explicit:
                skipped.append(item)
            continue
        key = str(env.real_path(path)).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Path(item).resolve())
    return out


# ---------------------------------------------------------------------------
# gcc
# ---------------------------------------------------------------------------
def _gcc_candidates(roots: list[Path], prefix: str = "arm-none-eabi-") -> list[tuple[Path, bool, str | None]]:
    """返回 (gcc 路径, 是否稳定别名, 版本目录名)。别名优先于版本化目录。

    ``prefix`` 是工具链前缀 (ARM 的 ``arm-none-eabi-`` / RISC-V 的 ``riscv-none-elf-``)。
    别名目录表只对 ARM 有意义, 其它前缀走"浅层/平铺"通用查找 —— 找得到的就认。
    """
    names = (f"{prefix}gcc.exe", f"{prefix}gcc")
    found: list[tuple[Path, bool, str | None]] = []
    for root in roots:
        if prefix == "arm-none-eabi-":
            for alias in _GCC_ALIAS_DIRS:
                path = root / alias / "bin" / names[0]
                if path.is_file():
                    found.append((path, True, None))
        arm = root / "Arm"
        if arm.is_dir():
            for child in sorted(arm.iterdir()):
                path = child / "bin" / names[0]
                if child.is_dir() and path.is_file():
                    found.append((path, False, child.name))
        # 浅层兜底: <root>\<x>\<y>\bin（例如 SDK 自带的工具链）
        for d1 in sorted(root.iterdir()) if root.is_dir() else []:
            if not d1.is_dir():
                continue
            for d2 in sorted(d1.iterdir()):
                path = d2 / "bin" / names[0]
                if d2.is_dir() and path.is_file():
                    found.append((path, False, d2.name))
        # 一层布局: <root>\<x>\bin（例如 xPack 解压出来的 riscv-none-elf 工具链）
        for d1 in sorted(root.iterdir()) if root.is_dir() else []:
            path = d1 / "bin" / names[0]
            if d1.is_dir() and path.is_file():
                found.append((path, False, d1.name))
        # 平铺布局: <root>\bin\<prefix>gcc（工具链直接解压在搜索根下）
        flat = root / "bin" / names[0]
        if flat.is_file():
            found.append((flat, False, None))
        # POSIX 后缀兜底 (MSYS/Cygwin 布局或没有 .exe 的发行版)
        flat_bare = root / "bin" / names[1]
        if flat_bare.is_file():
            found.append((flat_bare, False, None))

    # 去重: 按解析 junction 后的真实路径。别名先入, 所以同一真实路径上别名胜出。
    uniq: list[tuple[Path, bool, str | None]] = []
    seen: set[str] = set()
    for path, is_alias, ver in found:
        key = str(env.real_path(path)).lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append((path, is_alias, ver))
    uniq.sort(key=lambda item: (not item[1], _neg_str(item[2] or "")))
    return uniq


def _neg_str(text: str) -> tuple:
    """给版本目录名做降序排序用的键。"""
    return tuple(-ord(c) for c in text)


def _first_existing(roots: list[Path], rels: tuple[str, ...]) -> Path | None:
    for root in roots:
        for rel in rels:
            path = root / rel
            if path.is_file():
                return path
    return None


def _from_path(name: str) -> Path | None:
    import shutil

    found = shutil.which(name)
    return Path(found) if found else None


def openocd_scripts(openocd_exe: Path) -> Path | None:
    bin_dir = openocd_exe.parent
    for rel in ("..\\share\\openocd\\scripts", "..\\..\\share\\openocd\\scripts"):
        candidate = (bin_dir / rel).resolve()
        if (candidate / "target" / "stm32f1x.cfg").is_file():
            return candidate
        if (candidate / "interface" / "stlink.cfg").is_file():
            return candidate
    return None


def tool_version(cfg: dict, exe: Path, refresh: bool = False) -> str | None:
    if not refresh:
        cached = config.cached_version(cfg, str(exe))
        if cached:
            return cached
    result = exec.run([exe, "--version"], echo=False)
    first = result.lines[0].strip() if result.lines else ""
    if first:
        config.remember_version(cfg, str(exe), first)
        config.save(cfg)
        return first
    return None


def _gcc_from_path(prefix: str = "arm-none-eabi-") -> list[tuple[Path, bool, str | None]]:
    """PATH 兜底。

    ``make`` / ``openocd`` 一直有这条兜底, **gcc 之前漏了** —— 后果是"工具链明明
    就在 PATH 上, 却报环境缺失", 因为默认根 ``E:\\env`` 在别的机器上并不存在
    (实测: PATH 上能找到 arm-none-eabi-gcc, 搜索根换掉后仍然报 missing)。
    """
    found = _from_path(f"{prefix}gcc")
    return [(found, False, None)] if found is not None else []


def _missing_message(tool: str, roots: list[Path], hint: str = "") -> str:
    """缺工具时提示**实际搜索过哪些根**。

    写死某个路径会把用户引向错误方向: 有人用 ``XTCLI_ROOTS`` 指了别处, 提示却仍然
    说"在 E:\\env 下未找到"; 换台机器后 ``E:\\env`` 根本不存在, 这条提示等于没给信息。
    """
    where = ", ".join(str(root) for root in roots) if roots else "(没有有效的搜索根)"
    tail = f"; {hint}" if hint else ""
    return f"{tool} (已搜索工具链目录: {where}, 以及 PATH{tail})"


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def discover(refresh: bool = False, prefix: str = "arm-none-eabi-") -> Toolchain:
    """查找工具链。``prefix`` 决定找哪一套 (ARM / RISC-V ...), 默认 ARM。

    非 ARM 型号 (设备表给了 toolchain 前缀) 会按该前缀重新发现 —— 找不到就报环境缺失,
    **绝不退回 ARM 工具链**: 用错架构的工具链编出来的固件是最坏的一类错误。
    """
    cfg = config.load()
    skipped: list[str] = []
    roots = search_roots(cfg, skipped)
    for item in skipped:
        warn(f"配置的搜索根不存在, 已跳过: {item}")
    tc = Toolchain(roots=roots, prefix=prefix)

    candidates = _gcc_candidates(roots, prefix)
    if not candidates:
        candidates = _gcc_from_path(prefix)
    if candidates:
        path, is_alias, _ver = candidates[0]
        tc.gcc_path = path
        tc.gcc_alias = is_alias
        tc.gcc_root = env.to_posix(path.parent)
        tc.gcc_version = tool_version(cfg, path, refresh)
        if len(candidates) > 1:
            warn(f"发现多套 {prefix} 工具链, 已选: {path}")
    else:
        tc.missing.append(_missing_message(
            f"{prefix}gcc",
            roots,
            "可用 XTCLI_ROOTS(分号分隔) 或 xtcli.json 的 roots 追加工具链目录",
        ))

    make = _first_existing(roots, _MAKE_RELS) or _from_path("make")
    if make:
        tc.make = make
        tc.make_version = tool_version(cfg, make, refresh)
    else:
        tc.missing.append(_missing_message("make", roots))

    openocd = _first_existing(roots, _OPENOCD_RELS) or _from_path("openocd")
    if openocd:
        tc.openocd = openocd
        tc.openocd_version = tool_version(cfg, openocd, refresh)
        tc.openocd_scripts = openocd_scripts(openocd)
    else:
        tc.missing.append(_missing_message("openocd", roots))

    return tc
