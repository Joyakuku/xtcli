"""内存布局核对（.icf / .ld / 芯片真实容量）。

回归的真实案例：某 IAR 工程的 `.icf` 是厂商 xE 通用模板（512K/64K），而 `.ewp`
与工程内 `.ld` 指向的芯片只有 256K/48K（同一块板子上的 CubeMX 工程也声明 RCTx）。
旧实现只说一句"已改用同芯片的 .ld"就过去了 —— 既没告诉用户布局换了，也没拦住
"构建用的 .ld 超出真机"这个**链接能过、真机跑不起来**的方向。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path)
from support import cli_output, copy_project, has_project, in_dir

from xtcli import devices, memmap

REAL = Path(r"E:\Code_workspace\freertos_workspace\software\1_LED")

_ICF = """
define symbol __ICFEDIT_region_ROM_start__ = 0x08000000;
define symbol __ICFEDIT_region_ROM_end__   = 0x0807FFFF;
define symbol __ICFEDIT_region_RAM_start__ = 0x20000000;
define symbol __ICFEDIT_region_RAM_end__   = 0x2000FFFF;
define symbol __ICFEDIT_size_cstack__ = 0x400;
define symbol __ICFEDIT_size_heap__   = 0x200;
define region ROM_region = mem:[from __ICFEDIT_region_ROM_start__ to __ICFEDIT_region_ROM_end__];
define region RAM_region = mem:[from __ICFEDIT_region_RAM_start__ to __ICFEDIT_region_RAM_end__];
define block CSTACK with alignment = 8, size = __ICFEDIT_size_cstack__ { };
define block HEAP   with alignment = 8, size = __ICFEDIT_size_heap__ { };
initialize by copy { readwrite };
do not initialize { section .noinit };
place in ROM_region { readonly };
place in RAM_region { readwrite, block CSTACK, block HEAP };
"""

_LD = """
MEMORY
{
  RAM    (xrw)    : ORIGIN = 0x20000000,   LENGTH = 48K
  FLASH  (rx)     : ORIGIN = 0x8000000,    LENGTH = 256K
}
_estack = ORIGIN(RAM) + LENGTH(RAM);
_Min_Heap_Size = 0x200;
_Min_Stack_Size = 0x200;
"""


class TestDeviceMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.data = devices.load_stm32()

    def test_capacity_comes_from_the_part_number(self):
        self.assertEqual(devices.memory_for("STM32F103RCTx", "STM32F1", self.data), (262144, 49152))
        self.assertEqual(devices.memory_for("STM32F103RETx", "STM32F1", self.data), (524288, 65536))
        # 回归: x8 的容量码是**数字**, 旧正则要求字母 -> 蓝板取不到容量
        self.assertEqual(devices.memory_for("STM32F103C8Tx", "STM32F1", self.data), (65536, 20480))
        self.assertEqual(devices.density_define("STM32F103C8Tx", "STM32F1", self.data), "STM32F103x8")

    def test_families_without_data_are_skipped_not_guessed(self):
        """没有数据的系列/容量码一律跳过校验 —— 宁可不查, 不误报。"""
        self.assertIsNone(devices.memory_for("STM32F407VGTx", "STM32F4", self.data))
        self.assertIsNone(devices.memory_for("STM32WBA55CG", "STM32WBA", self.data))


class TestScriptParsing(unittest.TestCase):
    def _parse(self, text: str, name: str, parser):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(text, encoding="utf-8")
            return parser(path)

    def test_parse_icf(self):
        icf = self._parse(_ICF, "x.icf", memmap.parse_icf)
        self.assertEqual(icf.flash.size, 512 * 1024)
        self.assertEqual(icf.ram.size, 64 * 1024)
        self.assertEqual(icf.stack, 0x400)
        self.assertEqual(icf.heap, 0x200)

    def test_parse_ld_classifies_by_name_not_attributes(self):
        """回归: `RAM (xrw)` 含 'r', 只看属性会把它当成 flash。"""
        ld = self._parse(_LD, "x.ld", memmap.parse_ld)
        self.assertEqual(ld.flash.size, 256 * 1024)
        self.assertEqual(ld.ram.size, 48 * 1024)
        self.assertEqual(ld.stack, 0x200)
        self.assertEqual(ld.heap, 0x200)

    def test_noinit_is_reported_as_unhandled(self):
        icf = self._parse(_ICF, "x.icf", memmap.parse_icf)
        self.assertTrue(any("__no_init" in item for item in icf.unhandled), icf.unhandled)

    def test_common_constructs_are_not_noise(self):
        """`initialize by copy` / `block CSTACK` 是被处理的, 不该刷告警。"""
        icf = self._parse(_ICF, "x.icf", memmap.parse_icf)
        joined = " ".join(icf.unhandled)
        self.assertNotIn("initialize by copy", joined)
        self.assertNotIn("自定义内存块", joined)


class TestCompareAndGuard(unittest.TestCase):
    def _maps(self):
        with tempfile.TemporaryDirectory() as tmp:
            icf_path = Path(tmp) / "x.icf"
            ld_path = Path(tmp) / "x.ld"
            icf_path.write_text(_ICF, encoding="utf-8")
            ld_path.write_text(_LD, encoding="utf-8")
            return memmap.parse_icf(icf_path), memmap.parse_ld(ld_path)

    def test_compare_names_all_three_sources(self):
        icf, ld = self._maps()
        lines = memmap.compare(icf, ld, (262144, 49152), "STM32F103RCTx")
        joined = " | ".join(lines)
        self.assertIn("512K", joined)          # .icf
        self.assertIn("256K", joined)          # .ld
        self.assertIn("芯片 STM32F103RCTx", joined)   # 真机
        self.assertIn("比真机大", joined)

    def test_stack_shrink_is_flagged(self):
        icf, ld = self._maps()
        joined = " | ".join(memmap.compare(icf, ld, (262144, 49152), "STM32F103RCTx"))
        self.assertIn("栈保证变小了", joined)

    def test_matching_ld_is_not_flagged(self):
        _icf, ld = self._maps()
        self.assertIsNone(memmap.chip_exceeded(ld, (262144, 49152), "STM32F103RCTx"))

    def test_over_declared_ld_is_flagged(self):
        """核心保护: 构建用的 .ld 超出真机 -> 必须拒绝 (链接能过但真机跑不了)。"""
        big = memmap.MemMap(source="fake", flash=memmap.Region("FLASH", 0x08000000, 524288))
        message = memmap.chip_exceeded(big, (262144, 49152), "STM32F103RCTx")
        self.assertIsNotNone(message)
        self.assertIn("超出芯片真实容量", message or "")

    def test_no_chip_data_means_no_guard(self):
        big = memmap.MemMap(source="fake", flash=memmap.Region("FLASH", 0x08000000, 524288))
        self.assertIsNone(memmap.chip_exceeded(big, None, "STM32F407VGTx"))


@unittest.skipUnless(REAL.is_dir(), "缺少参考工程 1_LED")
class TestRealIarProject(unittest.TestCase):
    def test_parses_the_real_pair(self):
        icf = memmap.parse_icf(REAL / "EWARM" / "stm32f103xe_flash.icf")
        ld = memmap.parse_ld(REAL / "STM32F103RCTx_FLASH.ld")
        self.assertEqual((icf.flash.size, icf.ram.size), (512 * 1024, 64 * 1024))
        self.assertEqual((ld.flash.size, ld.ram.size), (256 * 1024, 48 * 1024))


@unittest.skipUnless(has_project("1_LED"), "缺少参考工程 1_LED")
class TestInitReportsTheSwap(unittest.TestCase):
    """端到端: init 必须把"布局被换成了什么、与芯片是否相符"说出来。"""

    def test_message_is_accurate_and_comparison_is_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("1_LED", Path(tmp))
            with in_dir(root):
                code, output = cli_output(["init", "-NoBuild"])
        self.assertEqual(code, 0, output)
        # 措辞必须把 .icf 说成"被弃用", 不能把它当成替代品
        self.assertIn("已弃用", output)
        self.assertNotIn("已改用同芯片的 .ld:", output)
        # 三方对比可见
        self.assertIn("IAR 布局", output)
        self.assertIn("芯片 STM32F103RC", output)
        # .ld 与芯片相符 -> 不应因此拒绝
        self.assertNotIn("超出芯片真实容量", output)


if __name__ == "__main__":
    unittest.main()
