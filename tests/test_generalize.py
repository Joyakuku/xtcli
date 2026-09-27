"""泛化能力测试: 非 STM32 工程、设备表前缀匹配、启动文件通用兜底。

最关键的一条:**CPU/FPU 未知时绝不能静默继续**。否则 config.mk 会退化成
``-mthumb``(armv4t), 编出来的固件架构是错的 —— 这个坑实测踩过
(``selected processor does not support `cpsid i' in Thumb mode``)。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_eclipse_family import VENDOR_PREFIXES, make_cproject

from xtcli import devices
from xtcli.backends import gcc_make
from xtcli.makefiles import config_mk


def _make_project(tmp: str, *, device: str, layout: str = "cubemx") -> Path:
    """造一个非 STM32 的 Cortex-M 工程（合成, 用于验证泛化逻辑）。"""
    root = Path(tmp) / "vendor_proj"
    (root / "Core" / "Inc").mkdir(parents=True)
    (root / "Core" / "Src").mkdir(parents=True)
    (root / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    (root / "app.ld").write_text("/* ld */\n", encoding="utf-8")

    if layout == "cubemx":
        (root / "Core" / "Startup").mkdir(parents=True)
        (root / "Core" / "Startup" / f"startup_{device.lower()}.s").write_text("; asm\n", encoding="utf-8")
    elif layout == "generic":
        (root / "startup").mkdir(parents=True)
        (root / "startup" / "startup_vendor.S").write_text("/* asm */\n", encoding="utf-8")
    elif layout == "iar_only":
        (root / "EWARM").mkdir(parents=True)
        (root / "EWARM" / "startup_vendor.s").write_text("; IAR asm\n", encoding="utf-8")

    prefix = VENDOR_PREFIXES["MCUXpresso"]
    (root / ".cproject").write_text(make_cproject(prefix, device=device), encoding="utf-8")
    return root


class TestDeviceTablePrefixMatching(unittest.TestCase):
    """设备表按"最长前缀"匹配 —— 加一张表就支持一个厂商, 不写死命名规则。"""

    def test_longest_prefix_wins(self):
        data = devices.load_stm32()
        self.assertEqual(devices.family_by_prefix("STM32F103RCTx", data), "STM32F1")
        self.assertEqual(devices.family_by_prefix("STM32WB55CG", data), "STM32WB")
        self.assertEqual(devices.family_by_prefix("STM32H743ZI", data), "STM32H7")
        self.assertIsNone(devices.family_by_prefix("MIMXRT1062", data))

    def test_multi_table_lookup(self):
        tables = devices.load_tables()
        self.assertIn("stm32", tables)
        name, info = devices.device_info_any("STM32F103RCTx", tables)
        self.assertEqual(name, "stm32")
        self.assertEqual(info.cpu, "cortex-m3")

        unknown, info2 = devices.device_info_any("MIMXRT1062", tables)
        self.assertEqual(unknown, "")
        self.assertIsNone(info2.cpu)

    def test_existing_device_info_unchanged(self):
        data = devices.load_stm32()
        info = devices.device_info("STM32F103RCTx", data)
        self.assertEqual(info.family, "STM32F1")
        self.assertEqual(info.cpu, "cortex-m3")
        self.assertEqual(info.ocd_target, "target/stm32f1x.cfg")


class TestSubSeriesNeverMismatched(unittest.TestCase):
    """内核不同的子系列不能被更短的系列键吃掉。

    STM32WBA (Cortex-M33) 与 STM32WB0 (Cortex-M0+) 都以 STM32WB (Cortex-M4)
    开头, 一旦错配就会**静默**给出错误的 -mcpu/-mfpu（"能编但错"的固件）。
    """

    def test_wba_resolves_m33_not_m4(self):
        data = devices.load_stm32()
        for device in ("STM32WBA55CG", "STM32WBA52CG", "STM32WBA6M"):
            info = devices.device_info(device, data)
            self.assertEqual(info.family, "STM32WBA", device)
            self.assertEqual(info.cpu, "cortex-m33", device)
            self.assertNotEqual(info.cpu, "cortex-m4", device)
            # Cortex-M33 的 FPU 是 fpv5, 绝不能拿到 WB 的 fpv4
            self.assertNotIn("fpv4", info.fpu or "", device)

    def test_wb0_resolves_m0plus_without_fpu(self):
        data = devices.load_stm32()
        for device in ("STM32WB09KE", "STM32WB05KN"):
            info = devices.device_info(device, data)
            self.assertEqual(info.family, "STM32WB0", device)
            self.assertEqual(info.cpu, "cortex-m0plus", device)
            self.assertNotEqual(info.cpu, "cortex-m4", device)
            self.assertNotIn("fpv4", info.fpu or "", device)
            self.assertIn("soft", info.fpu or "", device)

    def test_plain_wb_dictates_its_own_m4(self):
        """回归: 修子系列不能误伤真正的 STM32WB。"""
        data = devices.load_stm32()
        for device in ("STM32WB55CG", "STM32WB55RG", "STM32WB15CC"):
            info = devices.device_info(device, data)
            self.assertEqual(info.family, "STM32WB", device)
            self.assertEqual(info.cpu, "cortex-m4", device)
            self.assertEqual(info.ocd_target, "target/stm32wbx.cfg", device)


class TestSubSeriesAmbiguityWarns(unittest.TestCase):
    """表里缺了子系列键时必须告警 + 确定性拒绝, 不能静默套用短键的参数。"""

    @staticmethod
    def _legacy_table() -> dict:
        """只有 STM32WB、没有任何子系列标注的"老表"。"""
        return {
            "families": {
                "STM32WB": {
                    "cpu": "cortex-m4",
                    "fpu": "-mfpu=fpv4-sp-d16 -mfloat-abi=hard",
                    "ocd": "target/stm32wbx.cfg",
                },
            }
        }

    def test_wba_extension_is_flagged(self):
        data = self._legacy_table()
        warnings = []
        info = devices.device_info("STM32WBA55CG", data, warnings)
        self.assertTrue(warnings, "STM32WBA* 是 STM32WB 的前缀延伸, 必须告警")
        self.assertEqual(info.family, "STM32WB")
        self.assertIsNone(info.cpu, "告警场景下不能套用 STM32WB 的 cortex-m4")
        self.assertIsNone(info.fpu)

    def test_wb0_extension_only_warns(self):
        """回归: 数字子系列位只告警, 不拒绝 —— 否则会误伤同内核的真实型号。

        实测: ``STM32WLE5JC``(Cortex-M4 无 FPU, 与 STM32WL 条目一致) 与
        ``STM32WB5MMG`` 这类模块/型号位延伸都落在数字位分支; 一律拒绝会把本来
        能用的工程挡在门外。**字母**子系列位 (WBA 之于 WB) 才拒绝 —— 见上一个用例。
        """
        data = self._legacy_table()
        warnings = []
        info = devices.device_info("STM32WB09KE", data, warnings)
        self.assertTrue(warnings, "STM32WB0* 是 STM32WB 的数字子系列, 必须告警")
        self.assertEqual(info.cpu, "cortex-m4", "数字位只告警, 仍按父系列参数继续")
        self.assertIn("数字位通常是型号/模块码", warnings[0])

    def test_plain_wb_part_number_is_not_flagged(self):
        """STM32WB55 的 55 是 WB 自己的型号位, 不是子系列, 不能误报。"""
        data = self._legacy_table()
        warnings = []
        info = devices.device_info("STM32WB55CG", data, warnings)
        self.assertEqual(warnings, [])
        self.assertEqual(info.cpu, "cortex-m4")
        self.assertEqual(devices.ambiguity_of("STM32WB55CG", data), [])

    def test_other_families_part_numbers_are_not_flagged(self):
        """F1 / L4 / C0 等系列键本身带数字, 不能因为延伸段含字母就误报。"""
        data = self._legacy_table()
        data["families"]["STM32F1"] = {"cpu": "cortex-m3"}
        data["families"]["STM32L4"] = {"cpu": "cortex-m4"}
        for device in ("STM32F103RCTx", "STM32F103C8Tx", "STM32L471RETx"):
            self.assertEqual(devices.ambiguity_of(device, data), [], device)

    def test_declared_subseries_suppresses_the_warning(self):
        """表里已经声明过的子系列标识不再告警（数据驱动, 不写死厂商规则）。"""
        data = self._legacy_table()
        data["families"]["STM32WB"]["subseries"] = ["A", "0"]
        self.assertEqual(devices.ambiguity_of("STM32WBA55CG", data), [])
        self.assertEqual(devices.ambiguity_of("STM32WB09KE", data), [])

    def test_real_table_has_no_ambiguity_left(self):
        """正式表补齐 WBA/WB0 后, 这些型号都不应再有歧义告警。"""
        data = devices.load_stm32()
        for device in ("STM32WBA55CG", "STM32WB09KE", "STM32WB05KN", "STM32WBA6M"):
            self.assertEqual(devices.ambiguity_of(device, data), [], device)

    def test_verified_same_core_subseries_build_without_warning(self):
        """回归: 已确认同内核的字母子系列必须照常构建, 不能被当成"内核不明"拒绝。

        ``STM32WLE5JC`` = Cortex-M4、无 FPU（ST 数据手册; RIOT 的 lora-e5-dev
        (STM32WLE5JC) 板级资料亦标注 Family: ARM Cortex-M4 / FPU: no), 与
        ``STM32WL`` 条目一致, 因此登记进该条目的 ``subseries``。
        这是实测出来的误拒: 只按"延伸段是字母+数字"的形状判断会把 WLE5 拒掉。
        """
        data = devices.load_stm32()
        for device in ("STM32WLE5JC", "STM32WLE5CC"):
            warnings = []
            info = devices.device_info(device, data, warnings)
            self.assertEqual(warnings, [], device)
            self.assertEqual(info.family, "STM32WL", device)
            self.assertEqual(info.cpu, "cortex-m4", device)

    def test_digit_module_code_builds_with_warning(self):
        """``STM32WB5MMG`` 这类模块位: 告警但不拒绝（实测落在数字位分支）。"""
        data = devices.load_stm32()
        warnings = []
        info = devices.device_info("STM32WB5MMG", data, warnings)
        self.assertTrue(warnings)
        self.assertEqual(info.cpu, "cortex-m4")

    def test_build_model_refuses_when_subseries_has_no_entry(self):
        """端到端: 子系列查不到条目时, 构建必须确定性拒绝而不是静默给出 -mcpu。

        ``_entry_for`` 在这种情况下返回空条目（cpu=None）, ``build_model`` 必须据此
        拒绝 —— 这正是"空 CPU 必须拒绝"那条保护要拦住的情形。
        """
        unavailable = ("", devices.DeviceInfo(family="STM32WB"))
        with unittest.mock.patch.object(gcc_make.devices, "device_info_any", return_value=unavailable):
            with tempfile.TemporaryDirectory() as tmp:
                root = _make_project(tmp, device="STM32WBA55CG")
                model = gcc_make.build_model(root, {})
        self.assertTrue(model.refusals, "畸形/缺条目的设备表必须拒绝, 不能静默退化架构")
        self.assertEqual(model.cpu, "")
        self.assertIn("CPU/FPU 无法确定", model.refusals[0].message)

    def test_build_model_uses_correct_flags_for_wba(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="STM32WBA55CG")
            model = gcc_make.build_model(root, {})
        self.assertEqual(model.refusals, [], [r.message for r in model.refusals])
        self.assertEqual(model.cpu, "cortex-m33")
        self.assertIn("fpv5-sp-d16", model.fpu)
        text = config_mk.render(model)
        self.assertIn("-mcpu=cortex-m33", text)
        self.assertNotIn("-mcpu=cortex-m4", text)
        self.assertNotIn("fpv4-sp-d16", text)

class TestDensityDefineNeverSilent(unittest.TestCase):
    """密度宏: F1 的容量码推导保持原样; 没有容量码区分的系列必须给出说明而不是静默 None。"""

    def test_f1_capacity_code_still_derived(self):
        data = devices.load_stm32()
        self.assertEqual(devices.density_define("STM32F103RCTx", "STM32F1", data), "STM32F103xE")

    def test_wba_density_is_explained_not_silent(self):
        data = devices.load_stm32()
        warnings = []
        result = devices.density_define("STM32WBA55CG", "STM32WBA", data, warnings)
        self.assertIsNone(result, "WBA 的 HAL 家族宏由 CMSIS 器件头定义, 不额外加 -D")
        self.assertTrue(warnings, "不能静默返回 None")
        self.assertIn("STM32WBA", warnings[0])

    def test_wb0_density_is_explained_not_silent(self):
        data = devices.load_stm32()
        warnings = []
        result = devices.density_define("STM32WB09KE", "STM32WB0", data, warnings)
        self.assertIsNone(result)
        self.assertTrue(warnings, "不能静默返回 None")
        self.assertIn("STM32WB0", warnings[0])

    def test_unrecognized_stm32_shape_is_reported(self):
        data = devices.load_stm32()
        warnings = []
        result = devices.density_define("STM32F1", "STM32F1", data, warnings)
        self.assertIsNone(result)
        self.assertTrue(warnings, "认不出的型号形状必须给出提示")


class TestUnknownDeviceNeverSilentlyBuilds(unittest.TestCase):
    def test_unknown_device_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062")
            model = gcc_make.build_model(root, {})
            self.assertTrue(model.refusals, "未知厂商的器件必须拒绝, 而不是静默退化架构")
            self.assertIn("CPU/FPU 无法确定", model.refusals[0].message)

    def test_cpu_override_unblocks(self):
        """-Cpu/-Fpu 覆盖后应当能继续, 且生成正确的编译旗标。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062")
            model = gcc_make.build_model(
                root,
                {"cpu": "cortex-m7", "fpu": "-mfpu=fpv5-d16 -mfloat-abi=hard"},
            )
            self.assertEqual(model.refusals, [], [r.message for r in model.refusals])
            self.assertEqual(model.cpu, "cortex-m7")

            text = config_mk.render(model)
            self.assertIn("-mcpu=cortex-m7", text)
            self.assertIn("-mfpu=fpv5-d16", text)
            self.assertIn("-mfloat-abi=hard", text)
            # 绝不能出现"只有 -mthumb"的退化形态
            self.assertNotIn("CPU         := -mthumb", text)

    def test_no_device_at_all_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062")
            # 把 target_mcu 抹掉, 模拟没有芯片信息的工程
            text = (root / ".cproject").read_text(encoding="utf-8")
            (root / ".cproject").write_text(
                text.replace('value="MIMXRT1062"', 'value=""'), encoding="utf-8"
            )
            model = gcc_make.build_model(root, {})
            self.assertTrue(model.refusals)


class TestFindStartupGeneralization(unittest.TestCase):
    def test_cubemx_layout_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062", layout="cubemx")
            found = gcc_make.find_startup(root, None)
            self.assertIsNotNone(found)
            self.assertIn("Startup", str(found))

    def test_generic_layout_found(self):
        """非 CubeMX 布局: 任意 startup*.S 都能被找到。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062", layout="generic")
            found = gcc_make.find_startup(root, None)
            self.assertIsNotNone(found, "通用兜底应能找到 startup/startup_vendor.S")
            self.assertEqual(found.name, "startup_vendor.S")

    def test_iar_asm_not_picked(self):
        """IAR 汇编不能当启动文件用（GNU as 编不了）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_project(tmp, device="MIMXRT1062", layout="iar_only")
            self.assertIsNone(gcc_make.find_startup(root, None))

    def test_candidate_filter(self):
        self.assertTrue(gcc_make._startup_candidate(Path("startup/startup_x.S")))
        self.assertFalse(gcc_make._startup_candidate(Path("EWARM/startup_x.s")))
        self.assertFalse(gcc_make._startup_candidate(Path("Templates/arm/startup_x.s")))
        self.assertFalse(gcc_make._startup_candidate(Path("MDK-ARM/startup_x.s")))


class TestReusableMakefileProjectsUnaffected(unittest.TestCase):
    """A 档（工程自带 Makefile）不需要 CPU/FPU, 不能被新拒绝逻辑误伤。"""

    def test_makefile_source_not_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "own_makefile"
            (root / "Core" / "Src").mkdir(parents=True)
            (root / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (root / "Makefile").write_text("all:\n\t@echo hi\n", encoding="utf-8")
            model = gcc_make.build_model(root, {})
            self.assertEqual(model.source, "makefile")
            self.assertEqual(model.refusals, [], [r.message for r in model.refusals])


if __name__ == "__main__":
    unittest.main()
