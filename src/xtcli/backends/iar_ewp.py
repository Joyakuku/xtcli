"""``.ewp``（IAR EWARM）解析。

价值在于拿到**精确的**宏定义 / 头文件路径 / 源文件清单 —— 比按目录约定推导准。

两个刻意不用的东西:
  * ``.icf`` 链接脚本: IAR 私有格式, 无法可靠翻译成 GCC ``.ld`` → 用同芯片模板
  * IAR 汇编启动文件（``__iar_program_start`` 等）: 与 GNU as 不兼容
    → 改用 CMSIS 的 ``gcc/startup_*.s``

注意: 编译器不同（IAR vs GCC）, 固件体积/布局必然不同。这里保证的是宏定义、
头文件路径、源文件集合与原工程一致。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

_TOOLCHAIN_VARS = ("$TOOLKIT_DIR$", "$EW_DIR$", "$CMSIS_PACK_ROOT$")


# C++ 源文件后缀。`.C` 是 C++ 的传统写法, 必须按原始大小写判断 ——
# suffix.lower() 会把它变成 ".c" 而误判成 C。
_CPP_SUFFIXES = (".cpp", ".cc", ".cxx", ".C")


@dataclass
class EwpConfig:
    config: str
    device: str | None = None
    icf: Path | None = None
    defines: list[str] = field(default_factory=list)
    includes: list[Path] = field(default_factory=list)
    src_files: list[Path] = field(default_factory=list)
    cpp_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def find_ewp(root: Path, max_depth: int = 3) -> Path | None:
    """IAR 工程文件通常在 ``EWARM\\`` 这类子目录里, 所以必须限深递归找。"""
    matches = sorted(root.rglob("*.ewp"))
    for path in matches:
        if "xtcli" in {part.lower() for part in path.parts}:
            continue
        try:
            depth = len(path.relative_to(root).parts)
        except ValueError:  # pragma: no cover
            continue
        if depth <= max_depth:
            return path
    return None


def read(ewp_path: Path, config_name: str = "") -> EwpConfig:
    try:
        tree = ET.parse(ewp_path)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f".ewp 解析失败 (XML 损坏?): {exc}") from exc

    root_el = tree.getroot()
    proj_dir = ewp_path.parent
    warnings: list[str] = []

    # --- 选配置（IAR 的配置名在 <configuration><name> 里）---
    configs: list[tuple[str, ET.Element]] = []
    for candidate in root_el.iter("configuration"):
        name_el = candidate.find("name")
        name = (name_el.text or "").strip() if name_el is not None else ""
        configs.append((name, candidate))
    if not configs:
        raise ValueError("未找到 <configuration> 节点")

    names = [name for name, _element in configs]
    cfg: ET.Element | None = None
    if config_name:
        cfg = next((element for name, element in configs if name == config_name), None)
    if cfg is None:
        cfg = configs[0][1]
        if config_name:
            # 旧实现会**静默**改用第一个配置 —— 配置不同意味着宏定义/头文件路径/优化
            # 都可能不同, 于是静默产出与用户预期不符的固件。至少要看见, 并给出可用名。
            warnings.append(
                f".ewp 里没有名为 '{config_name}' 的配置, 已改用第一个配置 '{names[0]}' "
                f"(可用配置: {', '.join(n for n in names if n) or '(无名)'}; 用 -Config 指定)"
            )

    # --- 选项字典: "settingsName/optionName" -> state 列表 ---
    opt_map: dict[str, list[str]] = {}
    for settings in cfg.findall("settings"):
        name_el = settings.find("name")
        if name_el is None:
            continue
        settings_name = (name_el.text or "").strip()
        data = settings.find("data")
        if data is None:
            continue
        for option in data.findall("option"):
            opt_name_el = option.find("name")
            if opt_name_el is None:
                continue
            states = [(state.text or "") for state in option.findall("state")]
            opt_map[f"{settings_name}/{(opt_name_el.text or '').strip()}"] = states

    def get_opt(settings: str, option: str) -> list[str]:
        return opt_map.get(f"{settings}/{option}", [])

    # --- 芯片: OGChipSelectEditMenu 形如 "STM32F103RC<TAB>ST STM32F103RC" ---
    device: str | None = None
    chip = get_opt("General", "OGChipSelectEditMenu")
    if chip and chip[0]:
        device = re.split(r"\s+", chip[0].strip())[0]

    def substitute(raw: str) -> Path | None:
        value = (raw or "").strip()
        if not value:
            return None
        if any(var in value for var in _TOOLCHAIN_VARS):
            return None  # 工具链自带路径, GCC 用不上
        value = value.replace("$PROJ_DIR$", str(proj_dir))
        return Path(os.path.normpath(value))

    # --- 宏定义 / 头文件路径（ICCARM = C 编译器块）---
    defines = [d for d in get_opt("ICCARM", "CCDefines") if d]
    includes: list[Path] = []
    missing_includes: list[str] = []
    for item in get_opt("ICCARM", "CCIncludePath2"):
        if not item:
            continue
        resolved = substitute(item)
        if resolved is None:
            continue
        if resolved.exists():
            includes.append(resolved)
        else:
            missing_includes.append(item)
    if missing_includes:
        warnings.append("头文件路径不存在, 已剔除: " + " | ".join(missing_includes))

    # --- 源文件清单 ---
    src_files: list[Path] = []
    cpp_files: list[Path] = []
    skipped_asm: list[str] = []
    missing_src: list[str] = []
    for file_el in root_el.iter("file"):
        name_el = file_el.find("name")
        if name_el is None:
            continue
        resolved = substitute(name_el.text or "")
        if resolved is None:
            continue
        suffix = resolved.suffix.lower()
        if resolved.suffix in _CPP_SUFFIXES or suffix in (".cpp", ".cc", ".cxx"):
            if resolved.exists():
                if resolved not in cpp_files:
                    cpp_files.append(resolved)
            else:
                missing_src.append(resolved.name)
        elif suffix == ".c":
            if resolved.exists():
                if resolved not in src_files:
                    src_files.append(resolved)
            else:
                missing_src.append(resolved.name)
        elif suffix in (".s", ".asm"):
            skipped_asm.append(resolved.name)

    if skipped_asm:
        warnings.append(
            "已跳过 IAR 汇编文件 (语法与 GNU as 不兼容): " + ", ".join(skipped_asm)
            + " —— 启动文件需要工程里自带的 CMSIS **gcc** 版本; 若工程里没有, 构建时会报"
            " undefined reference to `Reset_Handler'"
        )
    if missing_src:
        warnings.append("清单里的源文件不存在, 已跳过: " + ", ".join(missing_src))

    icf = [v for v in get_opt("ILINK", "IlinkIcfFile") if v]
    if not icf:
        icf = [v for v in get_opt("ILINK", "IlinkConfigDefines") if v and v.lower().endswith(".icf")]
    icf_path: Path | None = None
    if icf:
        resolved_icf = substitute(icf[0])
        icf_path = resolved_icf if (resolved_icf and resolved_icf.is_file()) else None
        name = resolved_icf.name if resolved_icf else icf[0]
        # 措辞必须准确: 冒号后面是**被弃用**的 .icf, 不是替代品 (旧文案把它写成了替代品)。
        # 内存布局改由哪个 .ld 提供、差在哪里, 由 build_model 用 memmap 打印对比。
        warnings.append(
            f"IAR 链接脚本 (.icf) 无法翻译成 GCC 的 .ld, 已弃用: {name}"
            " —— 内存布局改用 .ld 提供, 具体差异见下面的对比"
        )

    return EwpConfig(
        config=config_name,
        device=device,
        icf=icf_path,
        defines=_dedup(defines),
        includes=_dedup_paths(includes),
        src_files=_dedup_paths(src_files),
        cpp_files=_dedup_paths(cpp_files),
        warnings=warnings,
    )


def _dedup(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _dedup_paths(items: list[Path]) -> list[Path]:
    out: list[Path] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out
