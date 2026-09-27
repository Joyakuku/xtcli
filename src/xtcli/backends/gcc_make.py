"""通用 GCC + Makefile 后端（STM32 是它的特化）。

按"工具链生态"划分后端, 不按芯片 —— 这一层覆盖所有 GCC + Makefile 的工程
（STM32 / GD32 / CH32 / NXP / 国产 MCU ...）。芯片差异全部由数据表
（``data/devices/*.json``）与探测器提供。

本文件只保留: **模型组装** (``build_model``) + 三个动作 (init/build/doctor)
+ 后端注册。其余按职责分到同级模块, 并从本文件**原样 re-export** ——
测试与调用方一直用 ``xtcli.backends.gcc_make.<名>``, 拆分不改变这个入口:

  gcc_traits.py   工程特征识别 (Traits / get_traits / detect_score)
  gcc_sources.py  源文件取舍 + 启动文件 + 链接脚本选择
  gcc_scaffold.py 生成物与运行时桩 (config.mk / rules.mk / stubs / compile_commands)
  gcc_common.py   构建与烧录共用的小工具 (make 计划 / 产物新鲜度 / 拒绝结果)
  gcc_burn.py     烧录与探针 (openocd / pyOCD / 回读校验)
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .. import config, devices, env, exec, flash, log, managed, memmap
from ..errors import Exit, Refusal, Result
from ..makefiles import config_mk
from ..model import Backend, Context, ProjectModel, register
from . import eclipse_cproject, iar_ewp, ioc
from .gcc_burn import (
    _burn_openocd,
    _burn_pyocd,
    _detect_probe,
    _looks_like_probe_unavailable,
    _probe_test,
    _read_artifact_words,
    _readback_verify,
    _resolve_flasher,
    action_burn,
)
from .gcc_chip import (
    _can_generate_ld,
    _cpu_guard,
    apply_chip_params,
    apply_memory_plan,
    write_generated_ld,
)
from .gcc_common import (
    _artifact_path,
    _check_artifact_fresh,
    _check_fingerprint,
    _make_plan,
    _refusal_result,
)
from .gcc_scaffold import (
    _copy_runtime_stubs,
    _defines_symbol,
    _wanted_stubs,
    _write_compile_commands,
    _write_skeleton,
)
from .gcc_sources import (
    _ld_matches_device,
    _scan_sources,
    _startup_candidate,
    _startup_rank,
    collect_cpp_sources,
    collect_sources,
    find_startup,
    is_excluded,
    resolve_ld_script,
)
from .gcc_traits import Traits, _find_ide_project, _is_owned_makefile, detect_score, get_traits


# ===========================================================================
# 模型组装
# ===========================================================================
def _ioc_route(model: ProjectModel, root: Path, info: ioc.IocInfo, tables: dict) -> None:
    """兜底: 从 .ioc + 目录约定推导。"""
    model.source = "ioc"
    model.device = info.device
    model.warnings.append("未找到 .cproject, 参数由 .ioc + 目录约定推导 —— 建议核对 size 是否与参考构建一致")
    # 密度宏要用**该型号所属的**设备表推导, 不是固定 STM32 表
    density = devices.density_define(info.device, devices.family_of(info.device), devices.table_of(info.device, tables)[1])
    model.defines = ["USE_HAL_DRIVER"] + ([density] if density else [])

    candidates = [
        root / "Core" / "Src",
        root / "FreeRTOS" / "Src",
        root / "App",
    ]
    drivers = root / "Drivers"
    if drivers.is_dir():
        for child in sorted(drivers.iterdir()):
            if child.is_dir():
                candidates.append(child / "Src")
    model.src_dirs = [p for p in candidates if p.is_dir()]

    include_dirs: list[Path] = []
    for base_name in ("Core", "Drivers", "FreeRTOS", "App", "Middlewares"):
        base = root / base_name
        if not base.is_dir():
            continue
        for header in sorted(base.rglob("*.h")):
            if is_excluded(header):
                continue
            if header.parent not in include_dirs:
                include_dirs.append(header.parent)
    model.includes = include_dirs


def build_model(root: Path, opt: dict) -> ProjectModel:
    # 收字符串也认: extract 是后端对外入口, 库调用方不该因为类型吃到裸 TypeError
    root = Path(root)
    config_name = str(opt.get("config") or "Debug")
    tables = devices.load_tables()
    traits = get_traits(root)

    model = ProjectModel(backend="gcc_make", root=root, target=root.name, config=config_name)

    # ---------- 1) 来源判定: .cproject > .ewp > 用户 Makefile > .ioc 推导 ----------
    cp: eclipse_cproject.CProjectConfig | None = None
    config_missing: eclipse_cproject.CProjectConfigNotFound | None = None
    if traits.cproject:
        try:
            cp = eclipse_cproject.read(root, config_name)
            model.source = "cproject"
        except eclipse_cproject.CProjectConfigNotFound as exc:
            # .cproject **存在**, 只是里面没有这个配置名 —— 绝不能报成"找不到 .cproject",
            # 那会让用户去恢复一个其实存在的文件。留到第 2 步给确定性拒绝 + -Config 提示。
            config_missing = exc
            model.warnings.append(f".cproject 不可用: {exc.detail}")
        except ValueError as exc:
            model.warnings.append(f".cproject 不可用: {exc}")

    ewp: iar_ewp.EwpConfig | None = None
    if cp is None and traits.ewp is not None:
        try:
            ewp = iar_ewp.read(traits.ewp, config_name)
        except ValueError as exc:
            model.warnings.append(f".ewp 不可用: {exc}")

    ioc_info = ioc.read(root) if traits.ioc is not None else None

    # ---------- 2) 确定性拒绝（先判定, 再决定是否写盘）----------
    if config_missing is not None and cp is None and ewp is None and not traits.makefile:
        # 与"真的没有 .cproject"区分开: 这里文件是存在的, 缺的是配置名。
        # 文案必须可操作 —— 列出 .cproject 里实际可用的配置名 + -Config 用法。
        available = ", ".join(config_missing.available) or "无"
        example = config_missing.available[0] if config_missing.available else "Debug"
        model.refusals.append(Refusal(
            message=(
                f"配置 '{config_missing.config_name}' 不存在: .cproject 是存在的, "
                f"但它没有这个配置 (实际可用: {available})"
            ),
            missing=[],
            next_step=(
                f"用 -Config <名> 指定实际存在的配置, 例如 -Config {example}"
                " —— 不要去找 .cproject, 它就在工程根目录下"
            ),
        ))

    if cp is None and ewp is None and not traits.makefile:
        if traits.core_src and ioc_info is not None:
            if traits.ewp is not None or traits.uvprojx:
                model.warnings.append(
                    "该工程是 IAR/Keil 工具链; 参数由 .ioc + 目录约定推导 (未读取 .ewp/.uvprojx), "
                    "可能与原 IDE 构建有差异 —— 建议用 xtcli-doctor 核对宏定义, 或核对 size"
                )
            if traits.platformio:
                model.warnings.append("检测到 platformio.ini; 本工具不使用 PlatformIO, 直接走 arm-none-eabi 构建")
        elif traits.core_src and ioc_info is None:
            model.refusals.append(Refusal(
                message="有源码但缺芯片信息: 找不到 .ioc 也找不到 .cproject",
                missing=["*.ioc", ".cproject"],
                next_step="用 CubeMX 打开工程并保存一次(会生成 .ioc), 或恢复 .cproject/Makefile",
            ))
        elif ioc_info is not None and not traits.core_src:
            model.refusals.append(Refusal(
                message="CubeMX 尚未生成代码 (缺 Core/Src)",
                missing=["Core/Src/", "Core/Inc/", "Drivers/"],
                next_step=f"先用 CubeMX 打开 {ioc_info.path} 生成代码, 再运行 stm32-init-pj",
            ))
        else:
            model.refusals.append(Refusal(
                message="不是 STM32 工程: 找不到 .cproject / Makefile / .ioc / Core/Src",
                missing=[".cproject", "Makefile", "*.ioc", "Core/Src"],
            ))

    # ---------- 3) 填充模型 ----------
    if cp is not None:
        model.device = cp.device or ""
        model.defines = cp.defines
        model.includes = cp.includes
        model.src_dirs = cp.src_dirs
        model.libs = cp.libs
        model.lib_paths = cp.lib_dirs
        model.ld_script = cp.ld_script
        model.extra["prebuild_step"] = cp.prebuild_step
        model.warnings.extend(cp.warnings)
        model.opt = _map_optimize(cp.optimize)
        model.dbg = _map_debug(cp.debug_level)
    elif ewp is not None:
        model.source = "ewp"
        model.device = ewp.device or ""
        model.defines = ewp.defines
        model.includes = ewp.includes
        model.src_files = ewp.src_files
        model.extra["cpp_files"] = list(ewp.cpp_files)
        model.warnings.extend(ewp.warnings)
        model.warnings.append(
            "原工程用 IAR 编译器; 本构建用 GCC, 固件体积/代码布局必然不同。"
            "此处保证的是宏定义、头文件路径、源文件集合与原工程一致"
        )
        # GCC 运行时桩: 原 IDE 用自己的运行库, 清单里不会有这两个文件
        for stub in ("syscalls.c", "sysmem.c"):
            path = root / "Core" / "Src" / stub
            if path.is_file() and path not in model.src_files:
                model.src_files.append(path)
    elif traits.makefile:
        model.source = "makefile"
        if ioc_info is not None:
            model.device = ioc_info.device
    elif ioc_info is not None and traits.core_src:
        _ioc_route(model, root, ioc_info, tables)

    if not model.device and ioc_info is not None:
        model.device = ioc_info.device

    # 有拒绝结论就不再往下推导 —— 缺芯片信息时继续查设备表没有意义
    if model.refusals:
        return model

    # ---------- 4) 芯片参数（来自设备表; 支持 CLI 覆盖）----------
    if not apply_chip_params(model, opt, tables):
        return model
    data = model.extra["data"]
    if model.source != "makefile" and not model.device:
        model.warnings.append("未能确定芯片型号 (不影响构建参数, 但烧录需要 -OcdTarget 指定)")

    # 密度宏: 配置里有就信配置, 没有才推导
    if model.device and not any(re.match(r"^STM32[A-Z]\d+x[A-Z]$", d) for d in model.defines):
        derived = devices.density_define(model.device, model.family, data, model.warnings)
        if derived:
            model.defines.append(derived)
            model.warnings.append(f"密度宏由型号推导: {derived}")

    # ---------- 5) 启动文件 / 链接脚本 / 内存越界校验 ----------
    if model.source != "makefile":
        apply_memory_plan(
            model,
            root,
            data,
            declared_ld=cp.ld_script if cp else None,
            icf=ewp.icf if ewp is not None else None,
        )

    # ---------- 6) 源码非空校验 ----------
    if model.source != "makefile":
        c_files, s_files = collect_sources(model)
        cpp_files = collect_cpp_sources(model)
        if not c_files and not cpp_files:
            model.refusals.append(Refusal(
                message="没有任何可编译的 C/C++ 源文件",
                missing=[f"在 {', '.join(str(p) for p in model.src_dirs)} 下未找到 .c/.cpp"],
                next_step="确认工程未损坏, 或 .cproject 的 sourceEntries 是否正确",
            ))
        if any(p.suffix == ".C" for p in cpp_files):
            model.warnings.append(
                "工程里有 .C 源文件 (C++ 的传统写法): Windows 下 .C 与 .c 在文件系统层面"
                "无法区分, 本工具按 C++ 处理 (交给编译器的扩展名判断); 若出现异常请改名为 .cpp"
            )
        del s_files

    # ---------- 7) 路径空格告警（make 无法安全处理）----------
    for path in list(model.src_dirs) + list(model.includes):
        if " " in str(path):
            model.warnings.append(f"路径含空格, make 无法安全处理: {path}")
            break

    return model


def _map_optimize(value: str | None) -> str:
    text = str(value or "")
    for key, flag in (("o0", "-O0"), ("og", "-Og"), ("os", "-Os"), ("o2", "-O2"), ("o3", "-O3")):
        if text.endswith(f"value.{key}"):
            return flag
    return "-O0"


def _map_debug(value: str | None) -> str:
    text = str(value or "")
    for key, flag in (("g0", "-g0"), ("g1", "-g1"), ("g2", "-g2"), ("g3", "-g3"), ("gnone", "")):
        if text.endswith(f"value.{key}"):
            return flag
    return "-g3"


def action_init(ctx: Context) -> Result:
    model = ctx.model
    opt = ctx.opt

    # 确定性拒绝优先于任何写入 —— 绝不产出"能编但错"的工程
    if model.refusals:
        first = model.refusals[0]
        log.err(first.message)
        for item in first.missing:
            log.info(f"缺少: {item}")
        if first.next_step:
            log.info(f"建议: {first.next_step}")
        return Result(code=first.code, message=first.message)

    if model.source == "makefile":
        log.ok('该工程自带 Makefile, 按"优先复用已有构建系统"策略, 无需初始化')
        log.info("stm32-build-pj 会直接调用工程自己的 Makefile")
        return Result(code=int(Exit.OK), message="复用已有 Makefile")

    log.step(f"初始化 {model.root}")

    owned = managed.Owned(model.root)

    # 兜底链接脚本: 写进 xtcli/ 并登记受管 (工程里原本没有 .ld, 所以不会被覆盖;
    # 用户后续换成自己的 .ld 时, 受管清单能识别出"这不再是我们的产物")。
    write_generated_ld(model, owned)

    if model.ld_script and not env.is_within(model.ld_script, model.root):
        dst = model.root / model.ld_script.name
        if dst.is_file():
            # 工程里已有同名 .ld -> 绝不覆盖。
            # 实测过: `.cproject` 里声明的 .ld 解析到工程外时, 旧实现会把工程内
            # 同名文件从 5376 字节覆盖成 55 字节, 日志还谎称"原先不存在"。
            log.warn(f"工程里已有同名链接脚本, 保留工程自己的: {dst.name}")
        else:
            owned.copy_file(model.ld_script, dst)
            log.info(f"链接脚本已从模板库复制进工程: {dst.name}")
            model.warnings.append(
                "链接脚本原先不存在, 已用同芯片模板补齐 (请核对 flash/ram 长度是否符合你的芯片)"
            )
        model.ld_script = dst

    # 桩必须在 _write_skeleton 之前决定 —— config.mk 要把它们写进 STUB_SRCS
    stubs = _copy_runtime_stubs(model, owned, disabled=bool(opt.get("no_stubs")))

    skeleton = _write_skeleton(model, owned)
    log.ok(f"已生成 {skeleton['xt_dir']}\\{{config.mk, rules.mk, Makefile}}")
    if skeleton["overwrote"]:
        log.info("config.mk 已存在, 已被重新生成覆盖")
    if skeleton["root_makefile"]:
        log.info("工程根无 Makefile, 已补一个转发壳 (裸 make 可用)")

    for stub in stubs:
        log.info(f"已补齐 GCC 运行时桩: {env.relative_to(model.root, stub)} (工程里没有对应的符号定义)")

    written = _write_compile_commands(model, ctx.tools, owned)
    if written:
        log.ok(f"已生成 compile_commands.json ({written[1]} 条)")

    owned.flush()
    if owned.backups:
        log.info(
            f"共备份 {len(owned.backups)} 个原有文件为 *{managed.BAK_SUFFIX} "
            f"(原文件不是 xtcli 生成的, 或在生成后被改过)"
        )

    for warning in model.warnings:
        log.warn(warning)
    prebuild = model.extra.get("prebuild_step")
    if prebuild:
        log.warn("该工程在 CubeIDE 里配置了 prebuild 钩子, xtcli 不会执行它:")
        log.info(str(prebuild))

    if opt.get("no_build"):
        log.info("已跳过试编译 (-NoBuild)")
        return Result(code=int(Exit.OK), message="初始化完成 (未试编译)")

    log.step("试编译")
    build_result = action_build(ctx)
    if build_result.ok:
        return Result(code=int(Exit.OK), message="初始化完成, 试编译通过")
    log.err("初始化已生成构建系统, 但试编译失败 —— 参数可能需要微调 (见上面的编译器输出)")
    return Result(code=build_result.code, message="初始化完成, 但试编译失败")


def action_build(ctx: Context) -> Result:
    model = ctx.model
    tools = ctx.tools

    refused = _refusal_result(model)
    if refused is not None:
        return refused

    if not tools.make:
        log.err("环境缺少 make")
        return Result(code=int(Exit.ENVIRONMENT), message="make 不可用")

    # 动手前先让用户看到"到底在哪个目录构建" —— 定位到错误的根时这是唯一的线索
    log.info(f"工程根: {model.root}")

    plan = _make_plan(model)
    if plan is None:
        log.err("找不到构建入口 (既无 xtcli/Makefile 也无根 Makefile)")
        log.info("请先运行: stm32-init-pj")
        return Result(code=int(Exit.PROJECT), message="缺少构建入口")

    makefile, owned = plan
    if owned:
        _check_fingerprint(model)

    msys_bin = str(tools.make.parent)
    # 子进程 PATH 必须把**本次选定的工具链**放在最前面。
    # xtcli 自己生成的 Makefile 用的是绝对路径 (CC := $(XT_TOOLCHAIN_BIN)/arm-none-eabi-gcc),
    # 但 **A 档 (工程自带 Makefile)** 的 `CC := arm-none-eabi-gcc` 是裸名 —— 不前置就
    # 会从环境 PATH 解析。实测过: 把宿主 gcc 冒充成 arm-none-eabi-gcc 放在 PATH 最前,
    # xtcli 生成的入口不受影响, 而自带 Makefile 的工程直接编错 (用了宿主 gcc)。
    xt_path = [str(tools.gcc_root), msys_bin] if tools.gcc_root else [msys_bin]
    jobs = os.cpu_count() or 4
    common = [
        "-C", str(model.root),
        "-f", makefile,
        f"-j{jobs}",
        f"XT_TOOLCHAIN_BIN={tools.gcc_root}",
    ]
    if tools.openocd:
        common.append(f"OPENOCD={env.to_posix(tools.openocd)}")

    if ctx.opt.get("clean"):
        if not owned:
            # A 档 (工程自带 Makefile): 它的 clean 目标是**用户自己写的**, 可能删掉
            # 任意文件 —— 实测过: 某个用户工程的 clean 会删掉 precious.txt 与
            # userdata/。所以不替用户执行它, 只把命令和原因说清楚。
            log.warn(
                f"跳过清理: {makefile} 是工程自带的 (不是 xtcli 生成的), "
                f"它的 clean 目标可能删除你的文件"
            )
            log.info(f"  确需清理请手动执行: make -C {model.root} -f {makefile} clean")
            log.info("  想让 xtcli 管理清理, 请先运行 stm32-init-pj 生成它自己的构建入口")
        else:
            log.step(f"清理 ({makefile})")
            cleaned = exec.run([tools.make, *common, "clean"], cwd=model.root, prepend_path=xt_path)
            if cleaned.exit_code != 0:
                # 清理失败绝不能吞掉 —— 否则接下来 make 可能对旧产物报
                # "Nothing to be done", 看起来成功, 实际什么都没重编。
                log.err(f"清理失败 (make clean 退出码 {cleaned.exit_code}), 已中止")
                return Result(code=int(Exit.BUILD), message="清理失败", data={"exit_code": cleaned.exit_code})

    targets: list[str] = []
    make_target = str(ctx.opt.get("make_target") or "")
    if make_target:
        targets.append(make_target)

    label = f"{makefile}{', xtcli 生成' if owned else ', 工程自带'}"
    log.step(f"构建 ({label})")
    result = exec.run([tools.make, *common, *targets], cwd=model.root, prepend_path=xt_path)
    if result.exit_code == 0:
        if not make_target:
            _check_artifact_fresh(model, _artifact_path(model, "elf"))
        return Result(code=int(Exit.OK), message="构建成功")
    log.err(f"构建失败 (make 退出码 {result.exit_code})")
    return Result(code=int(Exit.BUILD), message="构建失败", data={"exit_code": result.exit_code})


def action_doctor(ctx: Context) -> Result:
    model = ctx.model
    tools = ctx.tools
    opt = ctx.opt
    table_name = str(model.extra.get("device_table") or "")
    cfg = config.load()

    log.step("xtcli 环境")
    from .. import __version__

    log.info(f"版本        : {__version__}")
    log.info(f"工具搜索根  : {'  |  '.join(str(r) for r in tools.roots)}")
    alias = "  (稳定别名)" if tools.gcc_alias else ""
    log.info(f"gcc         : {tools.gcc_path}{alias}")
    log.info(f"gcc 版本    : {tools.gcc_version}")
    log.info(f"make        : {tools.make}   [{tools.make_version}]")
    log.info(f"openocd     : {tools.openocd}   [{tools.openocd_version}]")
    log.info(f"ocd scripts : {tools.openocd_scripts}")
    for line in flash.describe(flash.find_pyocd()):
        log.info(line)
    for item in tools.missing:
        log.warn(f"缺失: {item}")

    traits = get_traits(model.root)
    log.step(f"工程: {model.root}")
    kinds: list[str] = []
    if traits.cproject:
        kinds.append(".cproject")
    if traits.makefile:
        kinds.append("Makefile")
    elif traits.makefile_owned:
        kinds.append("Makefile(xtcli 生成)")
    if traits.ioc is not None:
        kinds.append("*.ioc")
    if traits.ewp is not None:
        kinds.append("*.ewp")
    if traits.uvprojx:
        kinds.append("*.uvprojx")
    if traits.platformio:
        kinds.append("platformio.ini")
    if traits.ld_files:
        kinds.append("*.ld")
    log.info(f"识别到的构建配置: {', '.join(kinds)}")

    for line in model.summary_lines():
        log.info(line)

    if model.refusals:
        log.step("确定性拒绝")
        for refusal in model.refusals:
            log.err(refusal.message)
            for item in refusal.missing:
                log.info(f"缺少: {item}")
            if refusal.next_step:
                log.info(f"建议: {refusal.next_step}")
    for warning in model.warnings:
        log.warn(warning)

    if model.source != "makefile":
        c_files, s_files = collect_sources(model)
        cpp_files = collect_cpp_sources(model)
        line = f"可编译源文件: {len(c_files)} 个 .c, {len(s_files)} 个 .s"
        if cpp_files:
            line += f", {len(cpp_files)} 个 C++ (用 g++ 编译)"
        log.info(line)

    probe_name = None
    if opt.get("probe"):
        log.step("探针探测 (openocd init;exit, 每个 8s 超时)")
        if not tools.openocd:
            log.err("openocd 不可用, 跳过")
        else:
            probe_target = str(opt.get("ocd_target") or model.extra.get("ocd_target") or "")
            if not probe_target:
                log.warn("无法确定 openocd target 配置, 探针自检结论可能不准")
            for probe in devices.probes_for(table_name):
                tested = _probe_test(tools, probe["cfg"], probe_target or None)
                if tested.ok:
                    log.ok(f"{probe.get('name')}  ({probe['cfg']}) 连上了")
                    config.remember_probe(cfg, probe.get("name", "?"), probe["cfg"])
                    config.save(cfg)
                    log.info("已记住该探针, stm32-burn-pj 会直接复用")
                    probe_name = probe.get("name")
                    break
                why = "超时" if tested.timed_out else f"退出码 {tested.exit_code}"
                log.warn(f"{probe.get('name')}  ({probe['cfg']}) 未连上: {why}")
            if probe_name is None:
                log.info("没有探针响应。检查 USB / 驱动 (WinUSB) / SWD 接线 / 是否被其他工具占用")

    code = int(Exit.OK)
    if model.refusals:
        code = model.refusals[0].code
    elif tools.missing:
        code = int(Exit.ENVIRONMENT)
    return Result(code=code, message="环境检查完成", data={"probe": probe_name})


# ===========================================================================
# 注册
# ===========================================================================
register(Backend(
    name="gcc_make",
    aliases=(
        "stm32",
        "stm32c0", "stm32f0", "stm32f1", "stm32f2", "stm32f3", "stm32f4", "stm32f7",
        "stm32g0", "stm32g4", "stm32h5", "stm32h7", "stm32l0", "stm32l1", "stm32l4",
        "stm32l5", "stm32u5", "stm32wb", "stm32wl",
    ),
    detect=detect_score,
    extract=build_model,
    init=action_init,
    build=action_build,
    burn=action_burn,
    doctor=action_doctor,
))

# ---------------------------------------------------------------------------
# 对外入口保持不变: 测试与调用方一直用 xtcli.backends.gcc_make.<名>。
# 拆分后这些名字**原样 re-export** —— 见文件头说明。加进 __all__ 同时表明
# 它们是刻意导出的 (不是为了消除 lint 告警的 noqa)。
# ---------------------------------------------------------------------------
__all__ = [
    "Backend",
    "Context",
    "Exit",
    "ProjectModel",
    "Refusal",
    "Result",
    # gcc_traits
    "Traits",
    "_artifact_path",
    "_burn_openocd",
    "_burn_pyocd",
    # gcc_chip
    "_can_generate_ld",
    "_check_artifact_fresh",
    "_check_fingerprint",
    "_copy_runtime_stubs",
    "_cpu_guard",
    "_defines_symbol",
    "_detect_probe",
    "_find_ide_project",
    "_is_owned_makefile",
    "_ld_matches_device",
    "_looks_like_probe_unavailable",
    # gcc_common
    "_make_plan",
    "_probe_test",
    "_read_artifact_words",
    "_readback_verify",
    "_refusal_result",
    "_resolve_flasher",
    "_scan_sources",
    "_startup_candidate",
    "_startup_rank",
    "_wanted_stubs",
    "_write_compile_commands",
    # gcc_scaffold
    "_write_skeleton",
    "action_build",
    # gcc_burn
    "action_burn",
    "action_doctor",
    "action_init",
    # 本文件定义
    "build_model",
    "collect_cpp_sources",
    "collect_sources",
    # 模块 (旧代码里以 gcc_make.exec / gcc_make.devices / gcc_make.config_mk / gcc_make.memmap 形式被引用)
    "config",
    "config_mk",
    "detect_score",
    "devices",
    "eclipse_cproject",
    "env",
    "exec",
    "find_startup",
    "flash",
    "get_traits",
    "iar_ewp",
    "ioc",
    # gcc_sources
    "is_excluded",
    "log",
    "managed",
    "memmap",
    "register",
    "resolve_ld_script",
]
