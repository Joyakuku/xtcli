"""RISC-V 通路端到端测试（P2 的 arch/abi/toolchain 落地验证）。

背景: 工具链本身装好了不等于通路可用。这条通路有 5 个环节必须串起来:
设备表给出 arch/abi/toolchain → 模型带上它 → config.mk 生成 -march/-mabi 与
XT_TOOLCHAIN_PREFIX → cli 按该前缀**重新查找**工具链 → make 用该前缀构建。

这里用一个自造的 CH32V003 工程 (Eclipse 风格 .cproject + 自己的 .ld + RISC-V 启动
汇编) 把 5 个环节全部走一遍, 并且**真的编出固件**, 再用 readelf 确认产物是
``elf32-littleriscv`` —— 只断言文本参数抓不到"用臂工具链编出 ARM 固件"这类错误。

另外守住一条跨架构的坑: 运行时桩 ``sysmem.c`` 依赖 CubeMX 风格的 ``_estack`` 符号,
RISC-V 的 .ld 用 ``_sp`` —— 补进去只会得到指向我们自己的 undefined reference。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (把 src/ 放进 sys.path)

from support import _MIN_CPROJECT, cli_output, in_dir

from xtcli import devices, discovery

RISCV_PREFIX = "riscv-none-elf-"
_RISCV = discovery.discover(prefix=RISCV_PREFIX)
_REQUIRE = unittest.skipUnless(
    _RISCV.gcc_path is not None and _RISCV.objdump is not None,
    f"未找到 {RISCV_PREFIX} 工具链 (E:\\env\\RiscV)",
)

DEVICE = "CH32V003F4P6"

MAIN_C = """
volatile unsigned int ticks;
void SystemInit(void) { }
int main(void) { for (;;) { ticks++; } }
"""

STARTUP_S = """
  .section .text.Reset_Handler,"ax",%progbits
  .globl Reset_Handler
  .type Reset_Handler, @function
Reset_Handler:
  la sp, _sp
  call SystemInit
  call main
1: j 1b
  .size Reset_Handler, .-Reset_Handler

  .section .isr_vector,"a",%progbits
  .globl __vector_table
__vector_table:
  .word _sp
  .word Reset_Handler
"""

# 故意用非 CubeMX 的符号名 (_sp 而不是 _estack): 这正是 RISC-V/自定义 .ld 的常见写法
LINKER = """
ENTRY(Reset_Handler)
MEMORY
{
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 16K
  RAM   (xrw) : ORIGIN = 0x20000000, LENGTH = 2K
}
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } >FLASH
  .text   : { *(.text*) *(.rodata*) } >FLASH
  .data   : { _sdata = .; *(.data*) _edata = .; } >RAM AT> FLASH
  .bss    : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } >RAM
  _sp = ORIGIN(RAM) + LENGTH(RAM);
}
"""


def _project(root: Path) -> Path:
    for sub in ("Core/Inc", "Core/Src", "Core/Startup"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "Core" / "Src" / "main.c").write_text(MAIN_C, encoding="utf-8")
    (root / "Core" / "Startup" / "startup_ch32v003.S").write_text(STARTUP_S, encoding="utf-8")
    (root / "ch32v003.ld").write_text(LINKER, encoding="utf-8")
    (root / ".cproject").write_text(_MIN_CPROJECT.replace("STM32F103RCTx", DEVICE), encoding="utf-8")
    return root


class TestRiscvDeviceData(unittest.TestCase):
    """设备表 → 模型参数 (不需要工具链, 始终可跑)。"""

    def test_ch32v003_declares_riscv_arch_instead_of_cpu(self):
        tables = devices.load_tables()
        name, info = devices.device_info_any(DEVICE, tables)
        self.assertEqual(name, "ch32v")
        self.assertEqual(info.family, "CH32V003")
        # RISC-V 没有 -mcpu, 用 arch/abi 表达目标
        self.assertFalse(info.cpu)
        self.assertEqual(info.arch, "rv32ec")
        self.assertEqual(info.abi, "ilp32e")
        self.assertEqual(info.toolchain, RISCV_PREFIX)
        self.assertEqual(info.fpu, "")
        self.assertEqual(devices.memory_for(DEVICE, "CH32V003", devices.table_data(name)), (16384, 2048))
        self.assertEqual(devices.flash_base(devices.table_data(name), "CH32V003"), "0x08000000")

    def test_gd32vf103_is_now_buildable_not_refused(self):
        """GD32VF103 曾因"内核不是 ARM"被拒绝; 补上 arch/abi/toolchain 后应放行。"""
        from types import SimpleNamespace

        from xtcli.backends import gcc_make

        tables = devices.load_tables()
        name, info = devices.device_info_any("GD32VF103C8T6", tables)
        self.assertEqual(name, "gd32")
        self.assertEqual(info.arch, "rv32imac")
        self.assertEqual(info.abi, "ilp32")
        self.assertEqual(info.toolchain, RISCV_PREFIX)
        model = SimpleNamespace(cpu=info.cpu or "", arch=info.arch, device="GD32VF103C8T6")
        self.assertIsNone(gcc_make._cpu_guard(model))

    def test_arch_cpu_guard_allows_declared_arch(self):
        """有 arch 就不算"内核未知"; 而无 arch 的 riscv 仍要被拒绝 (GD32VF103 那条路)。"""
        from types import SimpleNamespace

        from xtcli.backends import gcc_make

        model = SimpleNamespace(cpu="", arch="rv32ec", device=DEVICE)
        self.assertIsNone(gcc_make._cpu_guard(model))
        model = SimpleNamespace(cpu="riscv", arch="", device="GD32VF103C8T6")
        self.assertIsNotNone(gcc_make._cpu_guard(model))


@_REQUIRE
class TestRiscvEndToEnd(unittest.TestCase):
    def test_init_and_build_a_ch32v003_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _project(Path(tmp) / "ch32proj")
            with in_dir(root):
                init_code, init_out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                self.assertEqual(init_code, 0, init_out)

                config = (root / "xtcli" / "config.mk").read_text(encoding="utf-8")
                self.assertIn(f"XT_TOOLCHAIN_PREFIX := {RISCV_PREFIX}", config)
                self.assertIn("ARCH        := -march=rv32ec -mabi=ilp32e", config)
                self.assertIn("CPU         := \n", config, "RISC-V 工程绝不能带上 -mthumb")

                # 桩: syscalls 可补; sysmem 依赖 _estack, 本工程 .ld 用 _sp -> 不补并告警
                self.assertTrue((root / "xtcli" / "stubs" / "syscalls.c").is_file())
                self.assertFalse((root / "xtcli" / "stubs" / "sysmem.c").is_file())
                self.assertIn("sysmem.c", init_out)
                self.assertIn("_estack", init_out)

                # compile_commands.json 也必须给 RISC-V 旗标 (曾经写死 -mthumb/-mcpu)
                cdb = (root / "compile_commands.json").read_text(encoding="utf-8")
                self.assertIn("-march=rv32ec", cdb)
                self.assertIn("-mabi=ilp32e", cdb)
                self.assertNotIn("-mthumb", cdb)

                build_code, build_out = cli_output(["build", "-Target", "stm32"])
            self.assertEqual(build_code, 0, build_out)
            elf = root / "Debug" / "ch32proj.elf"
            self.assertTrue(elf.is_file(), build_out)

            # 产物必须是 RISC-V, 而不是被 ARM 工具链编出来的东西
            header = subprocess.run(
                [str(_RISCV.objdump), "-f", str(elf)], capture_output=True, text=True, timeout=120
            )
            self.assertEqual(header.returncode, 0, header.stderr)
            self.assertIn("elf32-littleriscv", header.stdout)

            size = subprocess.run(
                [str(_RISCV._sibling("size")), str(elf)], capture_output=True, text=True, timeout=120
            )
            self.assertEqual(size.returncode, 0, size.stderr)
            fields = [ln.split() for ln in size.stdout.splitlines() if ln.strip()]
            text_bytes = int(fields[-1][0])
            self.assertGreater(text_bytes, 0)
            self.assertLess(text_bytes, 16384, "不该超出 CH32V003 的 16KB Flash")


if __name__ == "__main__":
    unittest.main()
