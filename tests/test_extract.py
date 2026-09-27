"""抽取层对等测试: 黄金字段 + 本次踩到的坑（排除规则 / 去重 / 包含顺序 / 指纹 / 探针自检）。

这些断言就是抽取层的"等价性"证明: 三工程的参数必须逐字段等于黄金值。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import copy_project, has_project, project_path  # noqa: F401

from xtcli import devices, discovery
from xtcli.backends import gcc_make
from xtcli.makefiles import config_mk


class TestExclusionRules(unittest.TestCase):
    """坑 1: 厂商模板必须排除。

    CMSIS 的 ``Templates/`` 里有全部型号的启动文件与重复的 ``system_*.c``;
    HAL 目录里有依赖未开启模块的 ``*_template.c``（会报 unknown type name）。
    """

    def test_template_dirs_excluded(self):
        self.assertTrue(gcc_make.is_excluded(Path("Drivers/CMSIS/Device/ST/STM32F1xx/Source/Templates/x.c")))
        self.assertTrue(gcc_make.is_excluded(Path("Drivers/CMSIS/Core/Template/ARMv8-M/main_s.c")))
        self.assertTrue(gcc_make.is_excluded(Path("Drivers/CMSIS/Core_A/Source/irq_ctrl_gic.c")))

    def test_template_files_excluded(self):
        self.assertTrue(gcc_make.is_excluded(Path("Drivers/HAL/Src/stm32f1xx_hal_msp_template.c")))
        self.assertTrue(gcc_make.is_excluded(Path("Drivers/HAL/Src/stm32f1xx_hal_timebase_tim_template.c")))

    def test_normal_sources_kept(self):
        self.assertFalse(gcc_make.is_excluded(Path("Core/Src/main.c")))
        self.assertFalse(gcc_make.is_excluded(Path("Core/Src/stm32f1xx_hal_timebase_tim.c")))
        self.assertFalse(gcc_make.is_excluded(Path("Core/Startup/startup_stm32f103rctx.s")))


@unittest.skipUnless(has_project("demo"), "demo 工程不存在")
class TestDemoExtraction(unittest.TestCase):
    def setUp(self):
        self.model = gcc_make.build_model(project_path("demo"), {"config": "Debug"})

    def test_source_and_device(self):
        self.assertEqual(self.model.source, "cproject")
        self.assertEqual(self.model.device, "STM32F103RCTx")
        self.assertEqual(self.model.family, "STM32F1")
        self.assertEqual(self.model.cpu, "cortex-m3")
        self.assertEqual(self.model.fpu, "-mfloat-abi=soft")

    def test_defines_and_optimization(self):
        self.assertEqual(self.model.defines, ["DEBUG", "USE_HAL_DRIVER", "STM32F103xE"])
        self.assertEqual(self.model.opt, "-O0")
        self.assertEqual(self.model.dbg, "-g3")

    def test_include_order_is_c_compiler_first(self):
        """坑 2: 包含顺序必须以 c.compiler 列表为准。

        顺序错了会让同名头文件解析到不同副本, 实测表现为 .text 差 16 字节
        (70592 vs 70608)。
        """
        self.assertEqual(len(self.model.includes), 12)
        names = [str(p.relative_to(project_path("demo"))) for p in self.model.includes[:5]]
        self.assertEqual(
            names,
            [
                r"Core\Inc",
                r"Drivers\STM32F1xx_HAL_Driver\Inc\Legacy",
                r"Drivers\STM32F1xx_HAL_Driver\Inc",
                r"Drivers\CMSIS\Device\ST\STM32F1xx\Include",
                r"Drivers\CMSIS\Include",
            ],
        )

    def test_src_dirs_and_counts(self):
        self.assertEqual([p.name for p in self.model.src_dirs], ["FreeRTOS", "App", "Core", "Drivers"])
        c_files, s_files = gcc_make.collect_sources(self.model)
        self.assertEqual(len(c_files), 57)
        self.assertEqual(len(s_files), 1)

    def test_artifacts(self):
        self.assertEqual(self.model.asm_srcs[0].name, "startup_stm32f103rctx.s")
        self.assertEqual(self.model.ld_script.name, "STM32F103RCTX_FLASH.ld")
        self.assertEqual(self.model.libs, [":lcd.a"])
        self.assertEqual(self.model.lib_paths[0].name, "Drivers")


@unittest.skipUnless(has_project("1_LED"), "1_LED 工程不存在")
class TestIarExtraction(unittest.TestCase):
    def setUp(self):
        self.model = gcc_make.build_model(project_path("1_LED"), {"config": "Debug"})

    def test_uses_ewp_not_convention(self):
        self.assertEqual(self.model.source, "ewp")
        self.assertEqual(self.model.device, "STM32F103RC")
        self.assertEqual(self.model.defines, ["USE_HAL_DRIVER", "STM32F103xE"])
        self.assertEqual(len(self.model.includes), 5)

    def test_explicit_source_list_deduped(self):
        """坑 3: 显式清单 + GCC 运行时桩必须去重, 且不含 IAR 的汇编启动文件。

        重复的目标文件会导致 ``multiple definition of `g_pfnVectors'``。
        """
        self.assertEqual(len(self.model.src_files), len(set(self.model.src_files)))
        self.assertEqual(len(self.model.src_files), 17)  # 15 个原始 .c + syscalls/sysmem
        self.assertTrue(any(p.name == "syscalls.c" for p in self.model.src_files))
        self.assertFalse(any(p.suffix.lower() in (".s", ".asm") for p in self.model.src_files))

    def test_gcc_startup_replaces_iar_asm(self):
        self.assertEqual(len(self.model.asm_srcs), 1)
        self.assertEqual(self.model.asm_srcs[0].name, "startup_stm32f103xe.s")
        self.assertIn("gcc", {part.lower() for part in self.model.asm_srcs[0].parts})

    def test_warns_about_iar_differences(self):
        joined = " ".join(self.model.warnings)
        self.assertIn("IAR", joined)
        self.assertIn(".icf", joined)


@unittest.skipUnless(has_project("freertos_hal_template"), "freertos_hal_template 不存在")
class TestTemplateExtraction(unittest.TestCase):
    def test_cproject_without_makefile(self):
        model = gcc_make.build_model(project_path("freertos_hal_template"), {"config": "Debug"})
        self.assertEqual(model.source, "cproject")
        self.assertEqual(model.device, "STM32F103RCTx")
        c_files, _ = gcc_make.collect_sources(model)
        self.assertGreater(len(c_files), 0)


class TestDeviceTable(unittest.TestCase):
    def test_stm32_table_schema(self):
        data = devices.load_stm32()
        self.assertEqual(data.get("schema_version"), 1)
        self.assertIn("STM32F1", data["families"])
        self.assertEqual([p["name"] for p in devices.probes(data)], ["stlink", "cmsis-dap", "jlink"])

    def test_family_and_density(self):
        data = devices.load_stm32()
        self.assertEqual(devices.family_of("STM32F103RCTx"), "STM32F1")
        self.assertEqual(devices.family_of("STM32WB55"), "STM32WB")
        # F1 的容量码 C 属于 high density -> xE（不是字面对应的 xC）
        self.assertEqual(devices.density_define("STM32F103RCTx", "STM32F1", data), "STM32F103xE")

    def test_device_info(self):
        data = devices.load_stm32()
        info = devices.device_info("STM32F103RCTx", data)
        self.assertEqual(info.cpu, "cortex-m3")
        self.assertEqual(info.ocd_target, "target/stm32f1x.cfg")


class TestProbeTestMustCarryTarget(unittest.TestCase):
    """坑 4: 探针自检必须带 ``-f target``。

    ``interface/*.cfg`` 里没有 ``transport select``, 只加载 interface 会报
    "session transport was not selected" —— 结果在**真机连接正常时也会误判为
    探针没连上**（实测确认）。
    """

    def test_argv_contains_interface_and_target(self):
        tools = discovery.discover()
        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append([str(a) for a in argv])
            from xtcli.exec import RunResult

            return RunResult(argv=[str(a) for a in argv], exit_code=0)

        with mock.patch("xtcli.exec.run", side_effect=fake_run):
            gcc_make._probe_test(tools, "interface/stlink.cfg", "target/stm32f1x.cfg")

        self.assertTrue(captured)
        argv = captured[0]
        self.assertIn("-f", argv)
        self.assertIn("interface/stlink.cfg", argv)
        self.assertIn("target/stm32f1x.cfg", argv)
        self.assertLess(argv.index("interface/stlink.cfg"), argv.index("target/stm32f1x.cfg"))


class TestFingerprint(unittest.TestCase):
    """坑 5: config.mk 陈旧时必须能被发现。

    实测踩过: 芯片解析修好后忘记重新 init, config.mk 里 CPU 为空, 目标架构退化
    成 armv4t, 报 "selected processor does not support `cpsid i' in Thumb mode"。
    """

    def test_roundtrip_and_sensitivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "xtcli").mkdir(parents=True)

            from xtcli.model import ProjectModel

            base = ProjectModel(backend="gcc_make", root=root, target="t", device="STM32F103RCTx",
                               cpu="cortex-m3", fpu="-mfloat-abi=soft")
            path = root / "xtcli" / "config.mk"
            path.write_text(config_mk.render(base), encoding="utf-8")

            self.assertEqual(config_mk.read_fingerprint(path), config_mk.fingerprint(base))

            changed = ProjectModel(backend="gcc_make", root=root, target="t", device="STM32F103RCTx",
                                   cpu="", fpu="")  # CPU 丢了（真实踩过的形态）
            self.assertNotEqual(config_mk.read_fingerprint(path), config_mk.fingerprint(changed))

    def test_old_config_without_fingerprint_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.mk"
            path.write_text("TARGET := demo\n", encoding="utf-8")
            self.assertIsNone(config_mk.read_fingerprint(path))

    def test_generated_config_never_shadows_toolchain_var(self):
        """生成物里绝不能出现 GCC_ROOT —— 那是 Arm 工具链会读的变量名。"""
        with tempfile.TemporaryDirectory() as tmp:
            from xtcli.model import ProjectModel

            model = ProjectModel(backend="gcc_make", root=Path(tmp), target="t")
            text = config_mk.render(model)
            self.assertNotIn("GCC_ROOT", text)
            self.assertIn("XT_TOOLCHAIN_BIN", (Path(__file__).resolve().parents[1] / "src" / "xtcli"
                                              / "assets" / "rules.mk").read_text(encoding="utf-8"))


class TestOwnedMakefileDetection(unittest.TestCase):
    """坑 6: 必须区分"用户自带 Makefile"与"xtcli 生成的转发壳"。

    否则第二次 init 会把自己上一次的产物误判成"工程自带构建系统", 跳过初始化,
    留下过期的 xtcli/rules.mk。
    """

    def test_sentinel_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("# xtcli:generated\ninclude xtcli/Makefile\n", encoding="utf-8")
            traits = gcc_make.get_traits(root)
            self.assertTrue(traits.makefile_owned)
            self.assertFalse(traits.makefile)

    def test_user_makefile_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile").write_text("all:\n\t@echo hi\n", encoding="utf-8")
            traits = gcc_make.get_traits(root)
            self.assertTrue(traits.makefile)
            self.assertFalse(traits.makefile_owned)


if __name__ == "__main__":
    unittest.main()
