"""兜底链接脚本生成。

**只在最后一刻使用**: 工程里没有 ``.ld``、xtcli 模板库里也没有该芯片的 ``.ld`` 时,
用设备表里的容量信息生成一份**最小可用**的 ARM 链接脚本。

为什么需要它: 换一个厂商 (GD32/AT32/APM32/N32...) 后, 模板库里必然没有对应脚本,
于是明明 CPU/FPU/源文件都认得, 却只能停在"找不到链接脚本"上。而容量信息在设备表里
已经有了 —— 只要型号能查到 flash/ram, 就能确定性地生成一份不会越界的脚本。

**它不是厂商脚本的替代品**: 不包含 CCM/DTCM/备份域/特殊保留区等厂商特有段。
所以调用方必须 (1) 只在兜底时用, (2) 打显著告警, (3) 把生成物放到 ``xtcli/`` 并登记
进受管清单 (用户可以随时换成自己的 .ld, 不会被覆盖)。

非 ARM 架构 (RISC-V 等) 的段布局与启动约定完全不同, 因此**拒绝生成** —— 宁可停在
"请提供 .ld", 也不生成一份看起来能链接、实际跑不起来的脚本。
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_FLASH_BASE = "0x08000000"
DEFAULT_RAM_BASE = "0x20000000"


def _size_literal(size: int) -> str:
    """容量字面量: 能被 K 整除就写 K (与 ST 官方 .ld 一致的读法), 否则写字节数。"""
    if size and size % 1024 == 0:
        return f"{size // 1024}K"
    return str(size)


def ld_path(root: Path, device: str) -> Path:
    """兜底脚本的落点: ``<root>/xtcli/<型号>_xtcli_FLASH.ld``。

    名字里带 ``xtcli`` 是为了**一眼看出这是工具生成的**, 而且不会与工程自带脚本撞名
    (``<型号>_FLASH.ld`` 是 CubeMX 的命名)。
    """
    safe = "".join(ch for ch in (device or "chip") if ch.isalnum() or ch in "_-") or "chip"
    return root / "xtcli" / f"{safe}_xtcli_FLASH.ld"


def render_ld(
    device: str,
    flash: int,
    ram: int,
    *,
    flash_base: str = DEFAULT_FLASH_BASE,
    ram_base: str = DEFAULT_RAM_BASE,
    heap: int = 0x200,
    stack: int = 0x400,
) -> str:
    """生成最小可用的 ARM 链接脚本。

    符号名 (``_sidata`` / ``_sdata`` / ``_edata`` / ``_sbss`` / ``_ebss`` / ``_estack``)
    与 ST/GD32/AT32 等厂商 startup 汇编里引用的名字一致 —— 名字对不上时链接会直接报
    undefined reference, 属于**明确的失败**, 不会生成能跑错固件的脚本。
    """
    flash_base = flash_base or DEFAULT_FLASH_BASE
    ram_base = ram_base or DEFAULT_RAM_BASE
    header = [
        "/*",
        f" * 由 xtcli 生成的**兜底**链接脚本 —— 芯片: {device}",
        f" *   FLASH {flash} 字节 ({_size_literal(flash)}) @ {flash_base}",
        f" *   RAM   {ram} 字节 ({_size_literal(ram)}) @ {ram_base}",
        " *",
        " * 生成原因: 工程里没有 .ld, xtcli 模板库里也没有该芯片的 .ld。",
        " * 容量来自 data/devices 设备表, 不会超出真机; 但不含厂商特有的内存段",
        " * (CCM/DTCM/备份域/保留区)。若你的工程用到这些, 请换成厂商提供的 .ld。",
        " * 重新生成/替换: 删掉本文件并放入自己的 .ld, 再跑一次 init。",
        " */",
    ]
    body = f"""
ENTRY(Reset_Handler)

_estack = ORIGIN(RAM) + LENGTH(RAM);

_Min_Heap_Size = {heap};
_Min_Stack_Size = {stack};

MEMORY
{{
  FLASH (rx)  : ORIGIN = {flash_base}, LENGTH = {_size_literal(flash)}
  RAM   (xrw) : ORIGIN = {ram_base}, LENGTH = {_size_literal(ram)}
}}

SECTIONS
{{
  .isr_vector :
  {{
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  }} >FLASH

  .text :
  {{
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)
    KEEP(*(.init))
    KEEP(*(.fini))
    . = ALIGN(4);
    _etext = .;
  }} >FLASH

  .rodata :
  {{
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  }} >FLASH

  .ARM.extab : {{ *(.ARM.extab* .gnu.linkonce.armextab.*) }} >FLASH

  .ARM :
  {{
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  }} >FLASH

  .preinit_array :
  {{
    PROVIDE_HIDDEN(__preinit_array_start = .);
    KEEP(*(.preinit_array*))
    PROVIDE_HIDDEN(__preinit_array_end = .);
  }} >FLASH

  .init_array :
  {{
    PROVIDE_HIDDEN(__init_array_start = .);
    KEEP(*(SORT(.init_array.*)))
    KEEP(*(.init_array*))
    PROVIDE_HIDDEN(__init_array_end = .);
  }} >FLASH

  .fini_array :
  {{
    PROVIDE_HIDDEN(__fini_array_start = .);
    KEEP(*(SORT(.fini_array.*)))
    KEEP(*(.fini_array*))
    PROVIDE_HIDDEN(__fini_array_end = .);
  }} >FLASH

  _sidata = LOADADDR(.data);

  .data :
  {{
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)
    . = ALIGN(4);
    _edata = .;
  }} >RAM AT> FLASH

  . = ALIGN(4);
  .bss :
  {{
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)
    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  }} >RAM

  ._user_heap_stack :
  {{
    . = ALIGN(8);
    PROVIDE(end = .);
    PROVIDE(_end = .);
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  }} >RAM

  /DISCARD/ :
  {{
    libc.a(*)
    libm.a(*)
    libgcc.a(*)
  }}

  .ARM.attributes 0 : {{ *(.ARM.attributes) }}
}}
"""
    return "\n".join(header) + body
