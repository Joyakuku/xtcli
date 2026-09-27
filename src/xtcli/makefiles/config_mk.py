"""``config.mk`` 生成（含指纹）。

指纹用于让 build 发现"生成物与工程当前配置脱节" —— 这类不一致会静默产出
错误固件。实测踩过: 芯片型号解析修好后忘记重新 init, config.mk 里还留着
CPU 为空的旧值, 目标架构退化成 armv4t, 报
``selected processor does not support `cpsid i' in Thumb mode``。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import env
from ..model import ProjectModel

FINGERPRINT_PREFIX = "XT_FINGERPRINT:"
DEFAULT_TOOLCHAIN_PREFIX = "arm-none-eabi-"


def _arch_flags(model: ProjectModel) -> str:
    """非 ARM 目标架构的编译开关 (RISC-V: -march=... -mabi=...)。

    只在设备表显式给了 arch 时生成; ARM 工程为空串, 走 CPU 里的 -mcpu/-mthumb。
    """
    flags: list[str] = []
    if model.arch:
        flags.append(f"-march={model.arch}")
    if model.abi:
        flags.append(f"-mabi={model.abi}")
    return " ".join(flags)


def fingerprint(model: ProjectModel) -> str:
    """把决定构建产物的关键参数压成 16 位十六进制短指纹。"""
    parts: list[str] = [
        model.source,
        model.config,
        model.device or "",
        model.cpu or "",
        model.fpu or "",
        model.opt,
        model.dbg,
        model.out_dir,
        model.build_dir,
        model.target,
        ",".join(model.defines),
        ",".join(str(p) for p in model.includes),
        ",".join(str(p) for p in model.src_dirs),
        ",".join(str(p) for p in model.src_files),
        ",".join(str(p) for p in model.asm_srcs),
        str(model.ld_script or ""),
        ",".join(model.libs),
        ",".join(str(p) for p in model.lib_paths),
    ]
    # C++ 源文件也决定产物, 但只在真的有 C++ 时才加进指纹 ——
    # 这样纯 C 工程的指纹与旧版完全一致 (黄金体积测试依赖稳定性)。
    cpp_files = [str(p) for p in (model.extra.get("cpp_files") or [])]
    if cpp_files:
        parts.append(",".join(cpp_files))
    # 非 ARM 架构 (RISC-V 等) 的 arch/abi/工具链前缀同样决定产物, 但 ARM 工程为空 ——
    # 只在非空时追加, 保住"ARM 工程指纹与旧版逐字节一致"。
    for extra_part in (model.arch, model.abi, model.toolchain):
        if extra_part:
            parts.append(extra_part)
    # 注意: **不要**把 model.extra["stubs"] 加进指纹。它只在 action_init 里被填充,
    # 而 build 是独立进程、重新 extract 出的 model 里它是空的 —— 加进去会让每个
    # "被补齐过运行时桩"的工程在 build 时收到假告警"config.mk 与工程当前配置不一致"。
    # 桩文件清单本来就是源文件集合的确定性函数, 而源文件已在指纹里。
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


def render(model: ProjectModel) -> str:
    """模型 -> config.mk 文本。路径写工程相对形式, 让工程可以整体搬走。"""

    def rel(path: Path | str) -> str:
        return env.relative_to(model.root, path)

    lines: list[str] = [
        "# =============================================================================",
        "#  xtcli 生成 —— 请勿手改; 重新生成请运行: stm32-init-pj",
        f"#  参数来源: {model.source}   配置: {model.config}   芯片: {model.device}",
        f"#  {FINGERPRINT_PREFIX} {fingerprint(model)}",
        "#  (上面的指纹用于让 stm32-build-pj 发现本文件是否已与工程配置脱节)",
        "# =============================================================================",
        "",
        f"TARGET      := {model.target}",
        f"OUT_DIR     := {model.out_dir}",
        f"BUILD_DIR   := {model.build_dir}",
        "",
    ]

    cpu = f"-mcpu={model.cpu} -mthumb" if model.cpu else "-mthumb"
    if model.arch:
        # 目标架构的**唯一**出处是 arch/abi (RISC-V 等); ARM 的 -mcpu/-mthumb
        # 已经在 CPU 里, 所以 CPU 留空 —— 绝不能给非 ARM 芯片塞 -mthumb。
        cpu = ""
    lines += [
        f"CPU         := {cpu}",
        f"ARCH        := {_arch_flags(model)}",
        f"FPU         := {model.fpu}",
        f"OPT         := {model.opt}",
        f"DBG         := {model.dbg}",
        "",
        f"C_DEFS      := {' '.join(f'-D{d}' for d in model.defines if d)}",
    ]
    # 工具链前缀只在非 ARM 时输出 —— ARM 工程 config.mk 与旧版逐字节一致
    if model.toolchain and model.toolchain != DEFAULT_TOOLCHAIN_PREFIX:
        lines.append(f"XT_TOOLCHAIN_PREFIX := {model.toolchain}")

    includes = [f"-I{rel(p)}" for p in model.includes if p]
    if not includes:
        lines.append("C_INCLUDES  :=")
    else:
        lines.append("C_INCLUDES  := \\")
        for index, item in enumerate(includes):
            tail = "" if index == len(includes) - 1 else " \\"
            lines.append(f"  {item}{tail}")
    lines.append("")

    lines.append(f"SRC_DIRS    := {' '.join(rel(p) for p in model.src_dirs if p)}")

    explicit = [rel(p) for p in model.src_files if p]
    if explicit:
        lines.append(f"# 原 IDE 工程的精确源文件清单 ({len(explicit)} 个) —— 非空时 rules.mk 不扫描 SRC_DIRS")
        lines.append("C_SRCS_EXPLICIT := \\")
        for index, item in enumerate(explicit):
            tail = "" if index == len(explicit) - 1 else " \\"
            lines.append(f"  {item}{tail}")
    else:
        lines.append("C_SRCS_EXPLICIT :=")

    # C++ 源文件清单: 只在非空时输出, 让纯 C 工程的 config.mk 与旧版逐字节一致
    cpp_explicit = [rel(p) for p in (model.extra.get("cpp_files") or []) if p]
    if cpp_explicit:
        lines.append(f"# C++ 源文件 ({len(cpp_explicit)} 个) —— 用 g++ 编译, 并额外链接 libstdc++")
        lines.append("CXX_SRCS_EXPLICIT := \\")
        for index, item in enumerate(cpp_explicit):
            tail = "" if index == len(cpp_explicit) - 1 else " \\"
            lines.append(f"  {item}{tail}")
        lines.append("")

    # xtcli 补齐的 GCC 运行时桩: 放在 xtcli/stubs/, 不进用户的源码树。
    # 同样只在非空时输出, 让不需要桩的工程 config.mk 与旧版逐字节一致。
    stub_srcs = [rel(p) for p in (model.extra.get("stubs") or []) if p]
    if stub_srcs:
        lines.append(f"# xtcli 补齐的 GCC 运行时桩 ({len(stub_srcs)} 个) —— 工程里没有对应的符号定义")
        lines.append(f"STUB_SRCS   := {' '.join(stub_srcs)}")
        lines.append("")

    lines.append(f"ASM_SRCS    := {' '.join(rel(p) for p in model.asm_srcs if p)}")
    if model.ld_script:
        lines.append(f"LDSCRIPT    := {rel(model.ld_script)}")
    lines.append("")

    # 库: ':lcd.a' -> '-l:lcd.a'; 补上 nano.specs 需要的 libc/libm
    libs: list[str] = []
    for item in model.libs:
        if not item:
            continue
        libs.append(f"-l{item}")
    for required in ("-lc", "-lm"):
        if required not in libs:
            libs.append(required)
    lines.append(f"LIBS        := {' '.join(libs)}")
    lines.append(f"LIBDIR      := {' '.join(f'-L{rel(p)}' for p in model.lib_paths if p)}")
    lines.append("")

    openocd_if = model.extra.get("openocd_if") or "interface/stlink.cfg"
    ocd_target = str(model.extra.get("ocd_target") or "")
    lines += [
        f"OPENOCD_IF  := {openocd_if}",
        f"OPENOCD_TGT := {ocd_target}",
        "",
    ]
    return "\r\n".join(lines)


def read_fingerprint(path: Path) -> str | None:
    """从已有 config.mk 里取指纹; 取不到（旧版本生成）返回 None。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped.startswith(FINGERPRINT_PREFIX):
            return stripped[len(FINGERPRINT_PREFIX):].strip()
    return None
