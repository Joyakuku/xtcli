"""兜底链接脚本生成 (P3) 与 openocd target 预检测试。

价值: 换厂商后 (GD32/AT32/APM32...) 模板库里必然没有对应 .ld, 旧实现只能停在
"找不到链接脚本"。现在由设备表的真实容量生成一份最小脚本, 于是"新厂商"能真正走到
构建完成 —— 这里的端到端用例就是拿 GD32F103C8T6 走完整条链路并**真的编一次**。

同时守住两条不会退让的红线:
* 生成物必须与设备表容量**一致** (它同时是内存越界校验的依据);
* 非 ARM 内核 / 容量未知时**不生成**, 而是确定性拒绝。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import (
    MAIN_C,
    _MIN_CPROJECT,
    _MIN_STARTUP,
    cli_output,
    in_dir,
    toolchain_ready,
)

import support  # noqa: F401  (把 src/ 放进 sys.path)

from xtcli import devices, memmap, linker
from xtcli.backends import gcc_burn, gcc_make
from xtcli.model import ProjectModel

# GD32F103C8T6: 64KB flash / 20KB RAM (gd32.json 里由 Keil 器件页佐证)
GD32_DEVICE = "GD32F103C8T6"
GD32_FLASH = 65536
GD32_RAM = 20480


def _gd32_project(root: Path) -> Path:
    """自造一个 GD32 最小工程: 有源码/启动文件, 但**没有任何 .ld**。"""
    for sub in ("Core/Inc", "Core/Src", "Core/Startup"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "Core" / "Src" / "main.c").write_text(MAIN_C, encoding="utf-8")
    (root / "Core" / "Startup" / "startup_gd32f103c8t6.s").write_text(_MIN_STARTUP, encoding="utf-8")
    (root / ".cproject").write_text(
        _MIN_CPROJECT.replace("STM32F103RCTx", GD32_DEVICE), encoding="utf-8"
    )
    return root


class TestRenderLd(unittest.TestCase):
    def test_lengths_come_from_the_device_table(self):
        text = linker.render_ld("GD32F103C8T6", GD32_FLASH, GD32_RAM)
        self.assertIn("LENGTH = 64K", text)
        self.assertIn("LENGTH = 20K", text)
        self.assertIn("ORIGIN = 0x08000000", text)
        self.assertIn("ORIGIN = 0x20000000", text)
        # startup 汇编引用的符号一个都不能少
        for symbol in ("_estack", "_sidata", "_sdata", "_edata", "_sbss", "_ebss"):
            self.assertIn(symbol, text)
        self.assertIn("ENTRY(Reset_Handler)", text)

    def test_odd_sizes_fall_back_to_bytes(self):
        text = linker.render_ld("X", 1000, 3000)
        self.assertIn("LENGTH = 1000", text)
        self.assertIn("LENGTH = 3000", text)

    def test_flash_base_is_configurable(self):
        text = linker.render_ld("RP2040", 2 * 1024 * 1024, 256 * 1024, flash_base="0x10000000")
        self.assertIn("ORIGIN = 0x10000000", text)

    def test_ld_path_is_inside_xtcli_and_marked(self):
        path = linker.ld_path(Path("E:/proj"), GD32_DEVICE)
        self.assertEqual(path.parent.name, "xtcli")
        self.assertIn("xtcli", path.name)
        self.assertEqual(linker.ld_path(Path("E:/proj"), "../../etc/passwd").name, "etcpasswd_xtcli_FLASH.ld")


class TestCanGenerate(unittest.TestCase):
    """只在"有真实容量 + ARM + 有启动文件"时才生成。"""

    def _model(self, **kw) -> ProjectModel:
        return ProjectModel(backend="gcc_make", root=Path("."), device=GD32_DEVICE, **kw)

    def test_requires_known_capacity(self):
        model = self._model(asm_srcs=[Path("startup.s")])
        self.assertFalse(gcc_make._can_generate_ld(model, None))
        self.assertTrue(gcc_make._can_generate_ld(model, (GD32_FLASH, GD32_RAM)))

    def test_refuses_non_arm(self):
        model = self._model(asm_srcs=[Path("startup.s")], arch="rv32imac")
        self.assertFalse(gcc_make._can_generate_ld(model, (GD32_FLASH, GD32_RAM)))

    def test_requires_startup(self):
        model = self._model()
        self.assertFalse(gcc_make._can_generate_ld(model, (GD32_FLASH, GD32_RAM)))


class TestTargetCfgPreflight(unittest.TestCase):
    """openocd target 脚本不存在时必须在烧录前拦住 (而不是报"烧录失败")。"""

    @staticmethod
    def _tools(scripts: Path | None):
        from types import SimpleNamespace

        return SimpleNamespace(openocd_scripts=scripts)

    def test_existing_script_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp)
            (scripts / "target").mkdir()
            (scripts / "target" / "stm32f1x.cfg").write_text("", encoding="utf-8")
            self.assertTrue(gcc_burn._target_cfg_exists(self._tools(scripts), "target/stm32f1x.cfg"))

    def test_missing_script_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp)
            (scripts / "target").mkdir()
            self.assertFalse(gcc_burn._target_cfg_exists(self._tools(scripts), "target/stm32h5x.cfg"))

    def test_explicit_absolute_path_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mine.cfg"
            cfg.write_text("", encoding="utf-8")
            self.assertTrue(gcc_burn._target_cfg_exists(self._tools(None), str(cfg)))

    def test_unknown_scripts_dir_does_not_block(self):
        self.assertTrue(gcc_burn._target_cfg_exists(self._tools(None), "target/whatever.cfg"))


@unittest.skipUnless(toolchain_ready(), "工具链不完整")
class TestGd32EndToEnd(unittest.TestCase):
    """端到端: 一个**没有任何 .ld** 的 GD32 工程, init 生成兜底脚本并能构建。"""

    def test_init_generates_ld_and_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _gd32_project(Path(tmp) / "gd32proj")
            with in_dir(root):
                code, output = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                self.assertEqual(code, 0, output)
                ld = root / "xtcli" / f"{GD32_DEVICE}_xtcli_FLASH.ld"
                self.assertTrue(ld.is_file(), output)

                # 生成物必须与设备表容量一致 —— 否则内存越界校验本身就是错的
                table = devices.load_tables()["gd32"]
                chip = devices.memory_for(GD32_DEVICE, "GD32F103", table)
                self.assertEqual(chip, (GD32_FLASH, GD32_RAM))
                parsed = memmap.parse_ld(ld)
                self.assertIsNotNone(parsed.flash)
                self.assertIsNotNone(parsed.ram)
                self.assertEqual(parsed.flash.size, GD32_FLASH)
                self.assertEqual(parsed.ram.size, GD32_RAM)
                self.assertIsNone(memmap.chip_exceeded(parsed, chip, GD32_DEVICE))

                from xtcli import managed

                self.assertIn("xtcli/" + ld.name, managed.load(root))

                build_code, build_out = cli_output(["build", "-Target", "stm32"])
            self.assertEqual(build_code, 0, build_out)
            self.assertTrue((root / "Debug" / "gd32proj.elf").is_file(), build_out)


if __name__ == "__main__":
    unittest.main()
