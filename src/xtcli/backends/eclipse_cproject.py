"""``.cproject``（Eclipse CDT managed build）解析。

CubeIDE / TI CCS / NXP MCUXpresso / Simplicity Studio / Renesas e² studio /
WCH MounRiver / S32DS 都是同一格式, 只是工具链 id 前缀不同 —— 所以这里
**按 ``superClass`` 后缀匹配**, 不硬编码任何 IDE 前缀。

路径基准有两套, **先按写法判定, 写法判不出来才用存在性兜底**:
  * ``../Core/Inc``                        —— 相对 ``<工程>/<配置名>``（编译器 CWD!）
  * ``${workspace_loc:/${ProjName}/App}``  —— 相对工程根

"哪个候选存在就用哪个"这种兜底是**错**的: 工程里同时有 ``<工程>/Debug/Core/Inc``
时, ``${workspace_loc:/${ProjName}/Core/Inc}`` 会被解析到那份构建副本上, 而正确
答案是 ``<工程>/Core/Inc`` —— 静默改变头文件/库/.ld 的取舍。所以只有当写法本身
（既非绝对路径、也不带工作区前缀、也不以 ``../`` 开头）判不出基准时, 才回退到
存在性; 此时若两个候选都存在, 必须告警说明选了哪个、另一个是什么。
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# 顺序有意义: 长的具体前缀在前。剥离后剩"相对工程根的路径"。
_WORKSPACE_PREFIXES = (
    r"^\$\{workspace_loc:/\$\{ProjName\}/",
    r"^\$\{workspace_loc:/[^/]+/",
    r"^\$\{ProjName\}/",
    r"^\$\{workspace_loc:",
    r"^\$\{PROJECT_ROOT\}/",  # TI CCS
    r"^\$\{PROJECT_LOC\}/",   # TI CCS
)
# 同上, 但**不**剥离（只用于"看写法判断基准"）—— 含工程根语义的变量前缀。
_ROOT_RELATIVE_LEAD = (
    "${workspace_loc",
    "${ProjName}",
    "${PROJECT_ROOT}",
    "${PROJECT_LOC}",
)
# 以这些前缀开头 -> 相对 <工程>/<配置名>（CubeIDE 里 include 路径就是相对配置目录写的）
_CONFIG_RELATIVE_LEAD = ("../", r"..\\")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class CProjectError(ValueError):
    """``.cproject`` 不可用的基类。"""

    prefix = ".cproject 不可用"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.prefix}: {detail}")
        self.detail = detail
        self.message = str(self)


class CProjectParseError(CProjectError):
    """``.cproject`` 存在但解析不了（XML 损坏）。"""

    prefix = ".cproject 解析失败 (XML 损坏?)"


class CProjectConfigNotFound(CProjectError):
    """``.cproject`` **存在**, 但里面没有用户要的那个配置名。

    必须与"真的没有 .cproject"区分开: 否则用户被叫去恢复一个其实存在的文件,
    排查方向完全错。``available`` 是 .cproject 里实际可用的配置名。
    """

    def __init__(self, config_name: str, available: list[str]) -> None:
        self.config_name = config_name
        self.available = list(available)
        names = ", ".join(self.available) or "无"
        super().__init__(
            f"配置 '{config_name}' 在 .cproject 中不存在 (该文件里有: {names})"
            f" —— 用 -Config <名> 指定上面之一, 例如 -Config {self.available[0] if self.available else 'Debug'}"
        )


def _looks_absolute(value: str) -> bool:
    """盘符 ``C:``、UNC ``\\\\``/``//``、以 ``/`` 开头 —— 一律原样。"""
    if os.path.isabs(value) or value.startswith(("\\\\", "//", "/")):
        return True
    return bool(_DRIVE_RE.match(value))


def _path_base_notation(value: str) -> str | None:
    """按**写法**判断基准: ``"workspace"``(工程根) / ``"config"``(配置目录) / None(判不出)。

    None 表示写法本身无法判定 —— 只有这时才允许回退到存在性兜底。
    """
    if _looks_absolute(value):
        return None
    if value.startswith(_ROOT_RELATIVE_LEAD):
        return "workspace"
    if value.startswith(_CONFIG_RELATIVE_LEAD):
        return "config"
    return None


# ---------------------------------------------------------------------------
# 构建选项识别表
#
# 用**后缀**匹配而不是完整 id: 各 IDE 只是前缀不同 —— STM32CubeIDE
# (com.st.stm32cube.ide...)、TrueSTUDIO / SW4STM32 (com.atollic... / fr.ac6...)、
# MCUXpresso (com.nxp... / com.crt.advproject...)、Simplicity Studio
# (com.silabs.ide.si32.gcc...)、e² studio (com.renesas.cdt.managedbuild.gnuarm...)、
# MounRiver (com.mounriver.ide...)、S32DS (com.nxp.s32ds...)。
# TI CCS 是**另一套命名**（compilerID.* / linkerID.*），单独列出。
#
# 诚实说明: 除 STM32CubeIDE 外, 这些家族目前**没有真实工程可验证**。
# 所以策略是"尽力解析 + 把未识别的构建选项明确报出来", 而不是假装支持 ——
# 报出来的 superClass 可以直接拿来补这张表。
# ---------------------------------------------------------------------------
_OPTION_SUFFIX_MAP: tuple[tuple[str, str], ...] = (
    ("option.target_mcu", "device"),
    ("c.compiler.option.definedsymbols", "c_defines"),
    ("c.compiler.option.includepaths", "c_includes"),
    ("c.compiler.option.optimization.level", "optimize"),
    ("c.compiler.option.debuglevel", "debug_level"),
    ("assembler.option.definedsymbols", "asm_defines"),
    ("assembler.option.includepaths", "asm_includes"),
    # 汇编器/C++ 编译器也各有优化与调试等级。单独收进不同的键, 解析时按
    # "C 编译器 > C++ 编译器 > 汇编器" 取值 —— 直接覆盖写会让 XML 里靠后的
    # 那个(通常是汇编器)错误地胜出。
    ("assembler.option.optimization.level", "asm_optimize"),
    ("assembler.option.debuglevel", "asm_debug_level"),
    ("cpp.compiler.option.optimization.level", "cpp_optimize"),
    ("cpp.compiler.option.debuglevel", "cpp_debug_level"),
    ("c.linker.option.script", "ld_script"),
    ("c.linker.option.libraries", "libs"),
    ("c.linker.option.directories", "lib_dirs"),
    ("option.defaults", "defaults"),
    # --- TI CCS（命名体系完全不同）---
    ("compilerID.DEFINE", "c_defines"),
    ("compilerID.INCLUDE_PATH", "c_includes"),
    ("compilerID.OPT_LEVEL", "optimize"),
    ("linkerID.INCLUDE_PATH", "lib_dirs"),
    ("linkerID.LIBRARY", "libs"),
    ("linkerID.FILE", "foreign_linker"),  # TI 的 .cmd, 不能用于 GNU ld
)

_SCALAR_FIELDS = frozenset({
    "device",
    "optimize",
    "debug_level",
    "asm_optimize",
    "asm_debug_level",
    "cpp_optimize",
    "cpp_debug_level",
    "ld_script",
    "defaults",
    "foreign_linker",
})

_BUILD_OPTION_MARKERS = (
    "compilerID",
    "linkerID",
    "c.compiler",
    "c.linker",
    "cpp.compiler",
    "cpp.linker",
    "assembler",
)


def _classify_option(super_class: str) -> str | None:
    for suffix, fname in _OPTION_SUFFIX_MAP:
        if super_class.endswith(suffix):
            return fname
    return None


def _is_build_option(super_class: str) -> bool:
    return any(marker in super_class for marker in _BUILD_OPTION_MARKERS)


def _ccsproject_device(root: Path) -> str | None:
    """TI CCS 把器件型号写在 ``.ccsproject`` 里, 不在 ``.cproject``。"""
    path = root / ".ccsproject"
    if not path.is_file():
        return None
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return None
    for tag in ("deviceVariant", "device", "deviceId"):
        node = tree.getroot().find(f".//{tag}")
        if node is not None and (node.text or "").strip():
            return node.text.strip()
    return None


@dataclass
class CProjectConfig:
    config: str
    device: str | None = None
    defines: list[str] = field(default_factory=list)
    includes: list[Path] = field(default_factory=list)
    src_dirs: list[Path] = field(default_factory=list)
    libs: list[str] = field(default_factory=list)
    lib_dirs: list[Path] = field(default_factory=list)
    ld_script: Path | None = None
    optimize: str | None = None
    debug_level: str | None = None
    prebuild_step: str | None = None
    warnings: list[str] = field(default_factory=list)


def convert_path(
    raw: str,
    root: Path,
    config_name: str = "Debug",
    relative_to: str = "auto",
    warnings: list[str] | None = None,
) -> Path | None:
    """把 .cproject 里的各种写法归一成绝对路径。

    基准判定顺序: **写法 > 存在性**。

    * ``${workspace_loc:}`` / ``${ProjName}`` / ``${PROJECT_ROOT}`` / ``${PROJECT_LOC}``
      开头 → 相对**工程根**（与 CDT 语义一致, 不再看存在性）;
    * ``../`` 开头 → 相对 ``<工程根>/<配置名>``（CubeIDE 的 include 路径相对配置目录写）;
    * 盘符 / UNC / 以 ``/`` 开头 → 原样返回;

    只有写法判不出来（既非绝对、也无工作区前缀、也不以 ``../`` 开头）时才回退存在性;
    此时若两个候选都存在, 追加一条 warning 说明选了哪个、另一个是什么。传入
    ``warnings`` 列表即可收集（不传则静默, 保持函数签名兼容）。
    """
    value = (raw or "").strip().strip('"').strip()
    if not value:
        return None
    # 写法必须在**剥离前缀之前**看一眼 —— 剥离后就分不出基准了（缺陷 M1 的根因）
    notation = _path_base_notation(value)
    for pattern in _WORKSPACE_PREFIXES:
        value = re.sub(pattern, "", value)
    value = re.sub(r"\}$", "", value)
    if not value:
        return None

    if _looks_absolute(value):
        return Path(value)
    # 剥完前缀只剩 "/" 之类的: 指的是工作区/工程根本身
    if value in ("/", "\\", ".", "./", ".\\"):
        return root

    build_base = root / config_name
    cand_build = Path(os.path.normpath(build_base / value))
    cand_root = Path(os.path.normpath(root / value))

    if relative_to == "root":
        return cand_root
    if relative_to == "build":
        return cand_build
    if notation == "workspace":
        return cand_root
    if notation == "config":
        return cand_build

    # 写法判不出基准 —— 这时才看存在性
    if cand_build.exists() and cand_root.exists():
        if warnings is not None:
            warnings.append(
                f"路径基准无法从写法判定: '{raw.strip()}' 在配置目录与工程根下都存在, "
                f"已选 {cand_build} (配置目录基准 {config_name}/), 另一个候选是 {cand_root}"
                "  —— 如需工程根, 请把该路径写成 ${workspace_loc:/${ProjName}/...} 形式"
            )
        return cand_build
    if cand_build.exists():
        return cand_build
    if cand_root.exists():
        return cand_root
    return cand_build


def _option_states(option: ET.Element) -> list[str]:
    return [item.get("value", "") for item in option.findall("listOptionValue") if item.get("value")]


def read(root: Path, config_name: str = "Debug") -> CProjectConfig:
    path = root / ".cproject"
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError) as exc:
        raise CProjectParseError(str(exc)) from exc

    root_el = tree.getroot()

    # --- 选配置: 取带 toolChain + sourceEntries 的 <configuration> 节点 ---
    cfg: ET.Element | None = None
    for candidate in root_el.iter("configuration"):
        if candidate.get("name") == config_name:
            cfg = candidate
            break
    if cfg is None:
        for storage in root_el.iter("storageModule"):
            if storage.get("name") == config_name:
                child = storage.find("configuration")
                if child is not None:
                    cfg = child
                    break
    if cfg is None:
        names: list[str] = []
        for candidate in root_el.iter("configuration"):
            name = candidate.get("name")
            if name and name not in names:
                names.append(name)
        for storage in root_el.iter("storageModule"):
            name = storage.get("name")
            if name and storage.find("configuration") is not None and name not in names:
                names.append(name)
        # **存在**的 .cproject 里没有这个配置名 —— 不是"文件不见了", 必须分开报
        raise CProjectConfigNotFound(config_name, names)

    warnings: list[str] = []
    buckets: dict[str, list[str]] = {fname: [] for _suffix, fname in _OPTION_SUFFIX_MAP}
    scalars: dict[str, str] = {}
    unrecognized: list[str] = []

    for option in cfg.iter("option"):
        super_class = option.get("superClass") or ""
        if not super_class:
            continue
        field = _classify_option(super_class)
        if field is None:
            # 只上报"构建相关"的未识别选项, 避免把 convertbinary / cpuclock 这类
            # 无关选项也报出来变成噪音。这是"不假装支持"的关键: 遇到没见过的
            # 厂商命名, 明确告诉用户哪些选项没被理解。
            if _is_build_option(super_class) and (_option_states(option) or option.get("value")):
                unrecognized.append(re.sub(r"\.\d+$", "", super_class))
            continue
        if field in _SCALAR_FIELDS:
            scalars[field] = option.get("value", "")
        else:
            buckets[field].extend(_option_states(option))

    c_defines = buckets["c_defines"]
    asm_defines = buckets["asm_defines"]
    c_includes = buckets["c_includes"]
    asm_includes = buckets["asm_includes"]
    libs = buckets["libs"]
    lib_paths_raw = buckets["lib_dirs"]
    ld_raw: str | None = scalars.get("ld_script") or None
    device: str | None = scalars.get("device") or None
    # C 编译器 > C++ 编译器 > 汇编器
    optimize: str | None = scalars.get("optimize") or scalars.get("cpp_optimize") or scalars.get("asm_optimize") or None
    debug_level: str | None = (
        scalars.get("debug_level") or scalars.get("cpp_debug_level") or scalars.get("asm_debug_level") or None
    )
    defaults: str | None = scalars.get("defaults") or None

    if unrecognized:
        unique = list(dict.fromkeys(unrecognized))
        shown = ", ".join(unique[:8])
        more = f" (等 {len(unique)} 项)" if len(unique) > 8 else ""
        warnings.append(
            f"有 {len(unique)} 个构建选项未被识别, 可能影响参数对齐: {shown}{more}"
            "  —— 如果这是非 STM32CubeIDE 的 IDE (CCS/MCUXpresso/Simplicity/...), "
            "请把上面这些 superClass 反馈以便补映射"
        )

    # 非 GNU 链接脚本: 存在但不能用于 GCC 构建, 必须明确告知
    if scalars.get("foreign_linker"):
        warnings.append(
            f"该工程的链接脚本不是 GNU ld 格式 ({scalars['foreign_linker']}), 已忽略; "
            "需要提供 GCC 的 .ld (或让 xtcli 用同芯片模板补齐)"
        )

    # TI CCS 把器件型号写在 .ccsproject 里, 不在 .cproject
    if not device:
        device = _ccsproject_device(root)
        if device:
            warnings.append(f"芯片型号取自 .ccsproject: {device}")

    # --- 兜底: 从 Defaults 那个管道串里补（部分 CubeMX 版本不写结构化选项）---
    if defaults:
        parts = re.split(r"\s*\|\|\s*", defaults)
        if not device and len(parts) > 5 and parts[5]:
            device = parts[5]
        if not c_defines and len(parts) > 13 and parts[13]:
            c_defines.extend(part for part in re.split(r"\s*\|\s*", parts[13]) if part)
            warnings.append("宏定义取自 .cproject 的 Defaults 字段 (结构化选项缺失)")
        if not c_includes and len(parts) > 10 and parts[10]:
            c_includes.extend(part for part in re.split(r"\s*\|\s*", parts[10]) if part)
        if not ld_raw and len(parts) > 18 and parts[18]:
            ld_raw = parts[18]

    # 顺序很关键: 头文件搜索顺序由 C 编译器那份列表决定（与 CubeIDE 一致）,
    # 汇编器独有的项追加在后。顺序错了会让同名头文件解析到不同副本,
    # 实测表现为 .text 差 16 字节。
    defines = _dedup(c_defines + [d for d in asm_defines if d not in c_defines])
    includes_raw = c_includes + [i for i in asm_includes if i not in c_includes]

    includes: list[Path] = []
    missing_includes: list[str] = []
    for item in _dedup(includes_raw):
        resolved = convert_path(item, root, config_name, warnings=warnings)
        if resolved is None:
            continue
        if resolved.exists():
            includes.append(resolved)
        else:
            missing_includes.append(item)
    if missing_includes:
        warnings.append(
            "头文件路径不存在, 已剔除: " + " | ".join(missing_includes)
            + "  (CubeMX 重新生成后可能残留, 属于工程内部不一致)"
        )

    # sourceEntries 是 Eclipse 资源路径, 一律相对工程根
    src_dirs: list[Path] = []
    src_missing: list[str] = []
    entries = cfg.find("sourceEntries")
    if entries is not None:
        for entry in entries.findall("entry"):
            if entry.get("kind") != "sourcePath":
                continue
            name = entry.get("name")
            if not name:
                continue
            resolved = convert_path(name, root, config_name, relative_to="root")
            if resolved is not None and resolved.exists():
                if resolved not in src_dirs:
                    src_dirs.append(resolved)
            else:
                src_missing.append(name)
    if src_missing:
        warnings.append("sourceEntry 声明的目录不存在: " + " | ".join(src_missing))

    lib_dirs: list[Path] = []
    for item in _dedup(lib_paths_raw):
        resolved = convert_path(item, root, config_name, warnings=warnings)
        if resolved is not None:
            lib_dirs.append(resolved)

    ld_script = convert_path(ld_raw, root, config_name, warnings=warnings) if ld_raw else None

    return CProjectConfig(
        config=config_name,
        device=device,
        defines=defines,
        includes=includes,
        src_dirs=src_dirs,
        libs=_dedup(libs),
        lib_dirs=lib_dirs,
        ld_script=ld_script,
        optimize=optimize,
        debug_level=debug_level,
        prebuild_step=cfg.get("prebuildStep"),
        warnings=warnings,
    )


def _dedup(items: list[str]) -> list[str]:
    """保序去重。"""
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out
