"""芯片参数与内存布局: 设备表查表 → 目标架构 → 链接脚本 → 越界校验。

从 ``gcc_make.py`` 分出来的一整块内聚逻辑, 因为它管的是**同一个问题**:
"这块芯片到底是什么, 编出来的东西放得下吗"。这一层是最不能出错的一层 ——
参数错会编出架构错误的固件, 容量错会放过超界的固件, 两者都是"能编但错"。

三条不可退让的规则 (都有实测代价):
1. 目标架构**不确定就拒绝**: 带着空 CPU 继续会退化成 armv4t 的 ``-mthumb``;
2. 非 ARM 内核 (RISC-V) **必须拒绝**: 带着 ``-mcpu=riscv`` 调 arm-none-eabi-gcc
   不是"编不过"就是"编错";
3. 链接脚本超界**必须拒绝**: 链接能过、真机跑不起来是最坏的方向。
"""

from __future__ import annotations

from pathlib import Path

from .. import devices, env, linker, log, managed, memmap
from ..errors import Exit, Refusal, Result
from ..model import ProjectModel
from .gcc_sources import find_startup, resolve_ld_script


def _can_generate_ld(model: ProjectModel, chip: tuple[int, int] | None) -> bool:
    """能不能生成兜底链接脚本。

    三个条件缺一不可: 有真实容量 (设备表)、是 ARM 架构 (段布局/启动约定一致)、
    有启动文件 (否则链接必然缺 Reset_Handler, 生成脚本也没意义)。
    """
    if chip is None or model.arch:
        return False
    return bool(model.asm_srcs)


def _cpu_guard(model: ProjectModel) -> Refusal | None:
    """非 ARM 内核的确定性拒绝。

    设备表里已经有 CPU 不是 ARM 的器件 (例如 GD32VF103 是 RISC-V)。带着它继续会生成
    ``-mcpu=riscv -mthumb`` 交给 arm-none-eabi-gcc —— 那要么编不过 (还算好), 要么在
    某些参数下**编出错的固件**。所以这里必须先拦住: 非 ARM 器件要么由设备表给出
    ``arch``/``abi`` + ``toolchain``(走非 ARM 路径), 要么明确拒绝。
    """
    if not model.cpu or model.arch or model.cpu.startswith("cortex-"):
        return None
    return Refusal(
        message=(
            f"器件 '{model.device}' 的内核是 '{model.cpu}' (不是 ARM), "
            "本工具当前只支持 ARM 工具链"
        ),
        missing=["设备表里该系列的 arch/abi + toolchain 条目", "对应架构的 GCC 工具链"],
        next_step=(
            "该型号需要 RISC-V 之类非 ARM 工具链; 设备表补上 arch/abi 与 toolchain 后即可支持。"
            "若你确认它其实是 ARM 内核, 请核对型号或用 -Cpu 显式覆盖"
        ),
    )


def apply_chip_params(
    model: ProjectModel, opt: dict, tables: dict
) -> bool:
    """查设备表填 CPU/FPU/架构/工具链, 处理 CLI 覆盖与架构守卫。

    返回 ``True`` 表示"架构已确定, 可以继续"; ``False`` 表示已写入拒绝结论。
    """
    table_name, info = devices.device_info_any(model.device, tables)
    model.family = info.family or ""
    model.cpu = info.cpu or ""
    model.fpu = info.fpu or ""
    model.arch = info.arch or ""
    model.abi = info.abi or ""
    model.toolchain = info.toolchain or ""
    model.extra["ocd_target"] = info.ocd_target or ""
    model.extra["openocd_if"] = "interface/stlink.cfg"
    model.extra["device_table"] = table_name
    # 后面的密度宏/内存校验/烧录基址全部改用**该型号所属的**设备表 ——
    # 固定用 STM32 表会让非 ST 芯片的 flashBase/容量校验全错。
    data = devices.table_data(table_name)
    model.extra["data"] = data
    model.extra["flash_base"] = devices.flash_base(data, model.family)
    model.extra["ram_base"] = devices.ram_base(data, model.family)

    # 设备表没覆盖到的厂商: 允许直接用 -Cpu/-Fpu 覆盖, 不必先改数据文件
    if opt.get("cpu"):
        model.cpu = str(opt["cpu"])
        model.warnings.append(f"CPU 由 -Cpu 覆盖: {model.cpu}")
    if opt.get("fpu"):
        model.fpu = str(opt["fpu"])
        model.warnings.append(f"FPU 由 -Fpu 覆盖: {model.fpu}")

    not_arm = _cpu_guard(model)
    if not_arm is not None:
        model.refusals.append(not_arm)
        return False

    if model.source != "makefile" and not model.cpu and not model.arch:
        # **绝不能带着空架构继续**: config.mk 会退化成 `-mthumb`(armv4t),
        # 编出来的固件架构是错的。这类"静默错误"必须变成确定性拒绝。
        # (实测踩过: 解析器修好后忘了重新 init, 旧 config.mk 里 CPU 为空,
        #  编译器报 "selected processor does not support `cpsid i' in Thumb mode")
        # 非 ARM (RISC-V 等) 用 arch/abi 表达目标, 所以 arch 非空也算"架构已确定"。
        message = (
            f"设备表里没有 '{model.device}' (系列 {model.family or '未知'}), CPU/FPU 无法确定"
            if model.device
            else "无法确定芯片型号与 CPU/FPU"
        )
        model.refusals.append(Refusal(
            message=message,
            missing=["data/devices/*.json 里该系列的条目", "-Cpu / -Fpu 覆盖"],
            next_step='加一条设备数据, 或用 -Cpu cortex-m4 -Fpu "-mfpu=fpv4-sp-d16 -mfloat-abi=hard" 覆盖',
        ))
        return False
    return True


def apply_memory_plan(
    model: ProjectModel,
    root: Path,
    data: dict,
    *,
    declared_ld: Path | None = None,
    icf: Path | None = None,
) -> None:
    """启动文件 + 链接脚本 + 内存越界校验 (会往 model 里写告警/拒绝/兜底标记)。"""
    density = next((d for d in model.defines if _is_density(d)), None)
    startup = find_startup(root, density, model.warnings)
    if startup is not None:
        model.asm_srcs = [startup]
    else:
        model.warnings.append(
            "找不到启动文件 (Core/Startup/*.s 或 CMSIS Templates/gcc/startup_*.s) —— 链接会缺 Reset_Handler"
        )

    chip = devices.memory_for(model.device, model.family, data)
    ld_script = resolve_ld_script(root, declared_ld, model.device, model.warnings)
    generated = False
    if ld_script is None and _can_generate_ld(model, chip):
        # 兜底: 工程与模板库都没有 .ld, 但设备表里有该型号的真实容量 ->
        # 生成一份最小 ARM 脚本 (init 时写入 xtcli/, 受管), 而不是停在"找不到 .ld"。
        # 仅 ARM: 非 ARM 的段布局/启动约定完全不同 (见 linker.py 的说明)。
        ld_script = linker.ld_path(root, model.device)
        generated = True
        model.warnings.append(
            f"工程与模板库里都没有链接脚本: 已按设备表容量生成兜底 .ld ({ld_script.name}) —— "
            "它只含通用段, 不含厂商特有内存段 (CCM/DTCM 等); 若你的工程用到这些, "
            "请放入厂商提供的 .ld 并在 init 时替换"
        )
    if ld_script is not None:
        model.ld_script = ld_script
        if generated:
            model.extra["generate_ld"] = True
            model.extra["generate_ld_chip"] = chip
        elif not env.is_within(ld_script, root):
            model.warnings.append(f"链接脚本来自 xtcli 模板库 (工程内没有 .ld): {ld_script} —— 已复制进工程")
        # 内存布局核对 —— IAR 工程的内存布局**唯一出处**是 .icf, 换成 .ld 就是换了布局。
        # 实测过的真实案例: .icf 是厂商 xE 通用模板 (512K/64K), 而 .ewp 与工程 .ld 指向的
        # 芯片只有 256K/48K。三方必须摆在一起看, 并拦住"超出真机"的方向 —— 那个方向
        # 链接能过, 真机却跑不起来 (代码越界 / 栈顶落在不存在的 RAM 上)。
        ld_map = memmap.parse_ld(model.ld_script)
        if icf is not None:
            icf_map = memmap.parse_icf(icf)
            model.warnings.extend(memmap.compare(icf_map, ld_map, chip, model.device))
        exceeded = memmap.chip_exceeded(ld_map, chip, model.device)
        if exceeded:
            model.refusals.append(Refusal(
                message=exceeded,
                missing=[f"与 {model.device} 匹配的 .ld"],
                next_step=(
                    "换成该芯片对应的 .ld (用 CubeMX 生成 CubeIDE/Makefile 工程会自带), "
                    "或修正 .ld 里 MEMORY 的 LENGTH; 若确认芯片本身不是这个型号, "
                    "请在 .ewp/.cproject/.ioc 里把型号改对"
                ),
            ))
        return

    templates = sorted(p.name for p in (env.assets_dir() / "stm32" / "ld").glob("*.ld"))
    why = []
    if chip is None:
        why.append(
            f"设备表里没有 '{model.device}' (系列 {model.family or '未知'}) 的容量数据, 无法自动生成"
        )
    if model.arch:
        why.append("该型号不是 ARM 架构 (本工具只生成 ARM 兜底脚本)")
    if not model.asm_srcs:
        why.append("没有启动文件 (startup*.s), 链接必然缺 Reset_Handler")
    model.refusals.append(Refusal(
        message="找不到链接脚本 (.ld), 且模板库里没有该芯片的可用脚本"
        + ("; " + "; ".join(why) if why else ""),
        missing=["*.ld", f"{model.device}_FLASH.ld"],
        next_step=(
            "用 CubeMX 生成 CubeIDE/Makefile 工具链工程会自带 .ld; 或手工放入一个。"
            f"模板库现有: {', '.join(templates) if templates else '(空)'}"
        ),
    ))


def _is_density(define: str) -> bool:
    import re

    return bool(re.match(r"^STM32[A-Z]\d+x[A-Z]$", define))


def write_generated_ld(model: ProjectModel, owned: managed.Owned) -> Result | None:
    """把兜底链接脚本写进 ``xtcli/`` 并登记受管; 没生成时返回 None。"""
    chip = model.extra.get("generate_ld_chip")
    if not model.extra.get("generate_ld") or not isinstance(chip, tuple) or model.ld_script is None:
        return None
    text = linker.render_ld(
        model.device,
        chip[0],
        chip[1],
        flash_base=str(model.extra.get("flash_base") or linker.DEFAULT_FLASH_BASE),
        ram_base=str(model.extra.get("ram_base") or linker.DEFAULT_RAM_BASE),
    )
    owned.write_text(model.ld_script, text)
    log.ok(f"已生成兜底链接脚本 {env.relative_to(model.root, model.ld_script)}")
    log.info("说明: 它只含通用段 (容量来自设备表), 不含厂商特有内存段; 可用自己的 .ld 替换")
    return Result(code=int(Exit.OK), message="已生成兜底链接脚本")
