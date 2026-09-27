"""启动文件与链接脚本的选择 —— 回归测试。

这两处的历史缺陷都是**静默选错**（比报错危险得多）:

* ``resolve_ld_script`` 按字母序取第一个 ``*.ld``。实测: 同目录放
  ``STM32F030C8_FLASH.ld`` 与 ``STM32F103RCTx_FLASH.ld``、器件是 F103RC 时,
  会选中 F030 的那个 —— 内存布局与芯片不符, 而且零告警。
* ``find_startup`` 在 ``Core/Startup`` 里按字母序取第一个。实测: 同时存在
  ``startup_stm32f103xb.s``(128K) 与 ``xE``(512K)、芯片是 RCTx(256K) 时选中
  ``xb`` —— 链接能过, 但向量表偏小、中断向量缺失, 属于"能编能烧但行为错误"。

选择原则: 能按型号/密度宏确定就按它选; 确定不了就**告警并说明选了谁**,
绝不静默。测试都是纯单元, 不需要工具链。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path; 否则单独跑本文件会 ImportError)

from xtcli.backends import gcc_make


class TestLdScriptSelection(unittest.TestCase):
    def _root(self, tmp: str, names: list[str]) -> Path:
        root = Path(tmp) / "proj"
        root.mkdir(parents=True)
        for name in names:
            (root / name).write_text("/* ld */\n", encoding="utf-8")
        return root

    def test_single_ld_is_used_without_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["STM32F103RCTx_FLASH.ld"])
            warnings: list[str] = []
            found = gcc_make.resolve_ld_script(root, None, "STM32F103RCTx", warnings)
            self.assertEqual(found, root / "STM32F103RCTx_FLASH.ld")
            self.assertEqual(warnings, [])

    def test_multiple_ld_picks_the_one_matching_device(self):
        """回归 F2: 不能按字母序取第一个 (F030 会排在 F103 前面)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["STM32F030C8_FLASH.ld", "STM32F103RCTx_FLASH.ld"])
            warnings: list[str] = []
            found = gcc_make.resolve_ld_script(root, None, "STM32F103RCTx", warnings)
            self.assertEqual(found, root / "STM32F103RCTx_FLASH.ld")
            self.assertTrue(any("多个 .ld" in item for item in warnings), warnings)

    def test_multiple_ld_with_unknown_device_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["aaa.ld", "bbb.ld"])
            warnings: list[str] = []
            found = gcc_make.resolve_ld_script(root, None, "STM32F103RCTx", warnings)
            self.assertIsNotNone(found)
            self.assertTrue(any("内存布局可能不符" in item for item in warnings), warnings)

    def test_template_library_is_used_when_project_has_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir(parents=True)
            warnings: list[str] = []
            found = gcc_make.resolve_ld_script(root, None, "STM32F103RCTx", warnings)
            self.assertIsNotNone(found)
            self.assertNotIn(root, found.parents)


class TestStartupSelection(unittest.TestCase):
    def _root(self, tmp: str, files: list[str]) -> Path:
        root = Path(tmp) / "proj"
        for rel in files:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("  .syntax unified\n", encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_density_match_wins_over_alphabetical_order(self):
        """回归 F3(c): xb 与 xE 并存时, 必须按密度宏选 xE。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp,
                [
                    "Core/Startup/startup_stm32f103xb.s",
                    "Core/Startup/startup_stm32f103xe.s",
                ],
            )
            warnings: list[str] = []
            found = gcc_make.find_startup(root, "STM32F103xE", warnings)
            self.assertEqual(found.name, "startup_stm32f103xe.s")
            self.assertEqual(warnings, [])

    def test_capacity_mismatch_warns_instead_of_silence(self):
        """只有 xb 而芯片是 xE: 用工程里的文件, 但必须告警。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["Core/Startup/startup_stm32f103xb.s"])
            warnings: list[str] = []
            found = gcc_make.find_startup(root, "STM32F103xE", warnings)
            self.assertEqual(found.name, "startup_stm32f103xb.s")
            self.assertTrue(any("容量码不一致" in item for item in warnings), warnings)

    def test_ide_specific_startup_is_never_used(self):
        """回归 F3(a): 只有 Keil/IAR 的启动文件时必须拒绝, 不能拿去给 GNU as 编。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["MDK-ARM/startup_stm32f103xe.s"])
            warnings: list[str] = []
            self.assertIsNone(gcc_make.find_startup(root, "STM32F103xE", warnings))

    def test_cmsis_gcc_template_is_usable_when_density_matches(self):
        """回归 F3(d): StdPeriph 布局的 Templates/gcc/ 启动文件应当能用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp,
                [
                    "Libraries/CMSIS/Device/ST/STM32F1xx/Source/Templates/gcc/"
                    "startup_stm32f103xe.s"
                ],
            )
            warnings: list[str] = []
            found = gcc_make.find_startup(root, "STM32F103xE", warnings)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "startup_stm32f103xe.s")

    def test_cmsis_template_with_wrong_density_is_refused(self):
        """模板目录里每个容量都有一份, 不匹配就不能猜。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(
                tmp,
                [
                    "Libraries/CMSIS/Device/ST/STM32F1xx/Source/Templates/gcc/"
                    "startup_stm32f103xb.s"
                ],
            )
            warnings: list[str] = []
            found = gcc_make.find_startup(root, "STM32F103xE", warnings)
            self.assertIsNone(found)
            self.assertTrue(any("已跳过" in item for item in warnings), warnings)

    def test_uppercase_S_startup_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._root(tmp, ["Core/Startup/startup_stm32f103xe.S"])
            warnings: list[str] = []
            found = gcc_make.find_startup(root, "STM32F103xE", warnings)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "startup_stm32f103xe.S")


if __name__ == "__main__":
    unittest.main()
