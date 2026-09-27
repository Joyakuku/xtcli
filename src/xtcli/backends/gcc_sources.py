"""源文件取舍 / 启动文件 / 链接脚本选择。

三条实测结论(见原 gcc_make 文档字符串): 厂商模板必须排除、目标文件必须保序去重、
参数优先读 IDE 配置。这里的三件事都会**改变构建产物**, 所以选择过程必须可解释:
多候选要告警, 选不准要拒绝。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import env
from ..model import ProjectModel

EXCLUDE_DIR_SEGMENTS = ("Templates", "Template", "Core_A")
_EXCLUDE_FILE_RE = re.compile(r"_template\.(c|cpp|cc|cxx|s)$", re.IGNORECASE)
# C++ 源文件后缀。`.C` 是 C++ 的传统写法, 必须按原始大小写判断 ——
# suffix.lower() 会把它变成 ".c" 而误判成 C。
_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".C")
# 启动文件通用兜底时排除的目录: IAR/Keil 汇编与厂商模板的语法 GNU as 编不了
_STARTUP_EXCLUDE_SEGMENTS = frozenset({"iar", "keil", "mdk-arm", "ewarm", "template", "templates", "arm"})
_STARTUP_NAME_RE = re.compile(r"^startup[_A-Za-z0-9]*\.(s|S)$")



# ===========================================================================
# 源文件取舍
# ===========================================================================
def is_excluded(path: Path) -> bool:
    parts = path.parts
    for segment in EXCLUDE_DIR_SEGMENTS:
        if segment in parts:
            return True
    return bool(_EXCLUDE_FILE_RE.search(path.name))


def _scan_sources(model: ProjectModel) -> tuple[list[Path], list[Path], list[Path]]:
    """按 SRC_DIRS 递归扫描, 返回 (C, 汇编, C++) 三类源文件。"""
    c_files: list[Path] = []
    s_files: list[Path] = []
    cpp_files: list[Path] = []
    for directory in model.src_dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or is_excluded(path):
                continue
            suffix = path.suffix
            if suffix in _CPP_SUFFIXES:
                bucket = cpp_files
            elif suffix.lower() == ".c":
                bucket = c_files
            elif suffix.lower() == ".s":
                bucket = s_files
            else:
                bucket = None
            if bucket is not None and path not in bucket:
                bucket.append(path)
    return c_files, s_files, cpp_files


def collect_sources(model: ProjectModel) -> tuple[list[Path], list[Path]]:
    """返回 (c 源文件, 汇编源文件)。显式清单优先。"""
    if model.src_files:
        return list(model.src_files), list(model.asm_srcs)

    c_files, s_files, _cpp_files = _scan_sources(model)
    return c_files, s_files


def collect_cpp_sources(model: ProjectModel) -> list[Path]:
    """返回 C++ 源文件 (.cpp/.cc/.cxx/.C)。

    C 与 C++ 必须分开处理: 编译器 (CXX) 和运行库 (libstdc++) 都不同。
    之前 C++ 源文件被**静默丢弃** —— 既不被编译也不告警, 于是要么链接报
    undefined reference, 要么编出一个少了整块逻辑却"成功"的固件。
    """
    if model.src_files:
        return list(model.extra.get("cpp_files") or [])

    _c_files, _s_files, cpp_files = _scan_sources(model)
    return cpp_files


def _startup_rank(path: Path, want: str | None) -> tuple[int, str]:
    """启动文件与目标密度宏的匹配等级: 0 精确, 1 同族但容量码不同, 2 无关。"""
    stem = path.stem.lower()
    name = stem[len("startup_"):] if stem.startswith("startup_") else stem
    if want:
        if name == want:
            return 0, name
        if len(name) == len(want) and name[:-1] == want[:-1]:
            return 1, name
    return 2, name


def _startup_candidate(path: Path, allow_cmsis_template: bool = False) -> bool:
    """排除 IAR/Keil 汇编与厂商模板目录 —— 它们的语法 GNU as 编不了。

    ``Templates/gcc/`` 是例外: StdPeriph 风格工程
    (``Libraries/CMSIS/.../Source/Templates/gcc/``) 的启动文件就在那里。
    但那个目录里**每个容量都有一份**, 所以只有 ``allow_cmsis_template`` 且
    调用方要求"与密度宏精确匹配"时才允许采用。
    """
    segments = {part.lower() for part in path.parts}
    if segments & _STARTUP_EXCLUDE_SEGMENTS:
        usable = allow_cmsis_template and {"gcc", "templates"} <= segments
        if not usable or (segments & (_STARTUP_EXCLUDE_SEGMENTS - {"templates"})):
            return False
    return bool(_STARTUP_NAME_RE.match(path.name))


def find_startup(
    root: Path,
    density_define: str | None,
    warnings: list[str] | None = None,
) -> Path | None:
    """找 GCC 能汇编的启动文件。

    优先级:
      1) CubeMX 布局 ``Core/Startup/*.s``
      2) 按密度宏精确命中（CMSIS ``Templates/gcc/startup_<密度>.s``）
      3) **通用兜底**: 工程里任何 ``startup*.s``/``startup*.S``（排除 IAR/Keil
         汇编与厂商模板目录）—— 让非 CubeMX 布局的 Cortex-M 工程也能用

    **绝不静默选一个型号对不上的**: 向量表偏小时链接照样通过, 但中断向量缺失,
    属于"能编能烧但行为错误"。所以多候选时按密度宏打分; 选不准就把"选了谁、
    为什么可能不对"写进 ``warnings``。实测踩过: ``Core/Startup`` 同时有
    ``startup_stm32f103xb.s`` 与 ``xE``、芯片是 RCTx(256K), 旧实现按字母序选了
    ``xb``, 且零告警。
    """
    want = density_define.lower() if density_define else None

    def pool(paths) -> list[Path]:
        found: list[Path] = []
        for path in paths:
            if (
                path.is_file()
                and _startup_candidate(path, allow_cmsis_template=True)
                and path not in found
            ):
                found.append(path)
        return found

    groups: list[tuple[str, list[Path]]] = []
    core = root / "Core" / "Startup"
    if core.is_dir():
        groups.append(("Core/Startup", pool([*core.glob("*.s"), *core.glob("*.S")])))
    if want:
        groups.append((f"按密度宏 startup_{want}", pool(root.rglob(f"startup_{want}.s"))))
    groups.append((
        "通用兜底",
        pool([
            path
            for pattern in ("startup*.s", "startup*.S", "Startup*.s")
            for path in root.rglob(pattern)
        ]),
    ))

    for label, candidates in groups:
        if not candidates:
            continue
        ranked = sorted(
            candidates,
            key=lambda p: (
                _startup_rank(p, want)[0],
                0 if "gcc" in {x.lower() for x in p.parts} else 1,
                str(p),
            ),
        )
        best = ranked[0]
        rank = _startup_rank(best, want)[0]
        if rank == 0:
            return best
        # CMSIS Templates/ 里的候选必须精确命中才用 (那里每个容量都有一份)
        if "templates" in {x.lower() for x in best.parts}:
            if warnings is not None:
                warnings.append(
                    f"CMSIS 模板目录下有启动文件, 但都不匹配密度宏 {density_define}: 已跳过 "
                    f"{best.name} —— 模板目录里每个容量都有一份, 选错会让向量表偏小"
                )
            continue
        if len(candidates) > 1:
            if warnings is not None:
                names = ", ".join(sorted(p.name for p in candidates)[:6])
                warnings.append(
                    f"{label} 下有多个启动文件 ({names}), 没有与密度宏 {density_define} 精确匹配的那个 "
                    f"—— 已按 gcc/字母序选 {best.name}, 请核对向量表是否与芯片容量一致"
                )
        elif rank == 1 and warnings is not None:
            warnings.append(
                f"启动文件 {best.name} 与密度宏 {density_define} 的容量码不一致: 向量表可能偏小, "
                f"缺失的中断向量会落到默认处理 —— 请核对芯片型号"
            )
        return best
    return None


def _ld_matches_device(candidates: list[Path], device: str) -> Path | None:
    """从多个 .ld 里挑与芯片型号相符的那个 (按文件名归一化比对)。"""
    if not device:
        return None
    want = device.upper().rstrip("XT")           # STM32F103RCTx -> STM32F103RC
    hits = [path for path in candidates if want and want in path.stem.upper()]
    if len(hits) == 1:
        return hits[0]
    match = re.match(r"^(STM32[A-Z]+\d+)", want)
    if match:
        family = match.group(1)
        family_hits = [path for path in candidates if family in path.stem.upper()]
        if len(family_hits) == 1:
            return family_hits[0]
    return None


def resolve_ld_script(
    root: Path,
    declared: Path | None,
    device: str,
    warnings: list[str] | None = None,
) -> Path | None:
    """挑链接脚本。

    工程里有多个 ``*.ld`` 时**不能按字母序随便取第一个** —— 实测过: 同目录放
    ``STM32F030C8_FLASH.ld`` 与 ``STM32F103RCTx_FLASH.ld``、器件是 F103RC,
    旧实现按字母序选中 F030 的那个: 内存布局与芯片不符, 而且零告警。
    """
    if declared and declared.is_file():
        return declared
    candidates = sorted(root.glob("*.ld"))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        names = ", ".join(path.name for path in candidates[:6])
        matched = _ld_matches_device(candidates, device)
        if matched is not None:
            if warnings is not None:
                warnings.append(
                    f"工程里有多个 .ld ({names}); 已按芯片型号选 {matched.name} —— 请核对 flash/ram 长度"
                )
            return matched
        if warnings is not None:
            warnings.append(
                f"工程里有多个 .ld, 且没有一个文件名能匹配芯片 {device or '未知'}: {names} "
                f"—— 已按字母序选 {candidates[0].name}, 内存布局可能不符, 请核对或显式指定"
            )
        return candidates[0]
    if device:
        for name in (f"{device}_FLASH.ld", f"{device}.ld"):
            template = env.assets_dir() / "stm32" / "ld" / name
            if template.is_file():
                return template
    return None


