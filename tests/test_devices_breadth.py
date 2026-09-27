"""多厂商设备表测试（P1: 芯片覆盖广度）。

这一层的价值不在"表里有几条", 而在**每一张表都必须与真实环境自洽**:

* ``ocd`` 必须是本机 OpenOCD 真实存在的 target 脚本 —— 写一个不存在的 cfg 会让烧录
  在最后一步失败; 写一个**别的厂商**的 cfg 会烧错芯片, 这是最坏的一类错误。
  所以这里对**所有**表的**所有**系列逐个核对脚本是否存在 (本机没有 OpenOCD 时跳过)。
* 同一个型号在 longest-prefix 匹配下必须落到正确的表与系列 (GD32VF103 是 RISC-V,
  不能被 GD32F1 的 Cortex-M3 参数吃掉)。
* 非 ARM 内核必须被确定性拒绝, 不能带着 ``-mcpu=riscv`` 交给 arm-none-eabi-gcc。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (把 src/ 放进 sys.path)

from xtcli import devices
from xtcli.backends import gcc_make
from xtcli.model import ProjectModel

# 本机 OpenOCD 脚本目录（存在才做 cfg 真实性核对）
_OCD_SCRIPTS = Path(r"E:\env\msys64\mingw64\share\openocd\scripts")

# 型号 -> (表名, 系列, cpu)
_LOOKUPS = [
    ("STM32F103RCTx", "stm32", "STM32F1", "cortex-m3"),
    ("GD32F103C8T6", "gd32", "GD32F103", "cortex-m3"),
    ("GD32F303VET6", "gd32", "GD32F303", "cortex-m4"),
    ("GD32E230C8T6", "gd32", "GD32E230", "cortex-m23"),
    # RISC-V (Nuclei N205): 由 arch/abi 表达, cpu 为空
    ("GD32VF103C8T6", "gd32", "GD32VF103", ""),
    ("AT32F403AVGT7", "at32", "AT32F403A", "cortex-m4"),
    ("AT32F421C8T7", "at32", "AT32F421", "cortex-m4"),
    ("APM32F103C8T6", "apm32", "APM32F103", "cortex-m3"),
    ("APM32F003F6P6", "apm32", "APM32F003", "cortex-m0plus"),
    ("RP2040", "rp2040", "RP2040", "cortex-m0plus"),
    ("RP2350A", "rp2040", "RP2350", "cortex-m33"),
    ("NRF52840_XXAA", "nordic", "NRF52840", "cortex-m4"),
    ("nRF52810", "nordic", "NRF52810", "cortex-m4"),
    ("ATSAMD21J18A", "samd", "SAMD21", "cortex-m0plus"),
    ("SAMD51J19A", "samd", "SAMD51", "cortex-m4"),
    ("ATSAML21J18A", "samd", "SAML21", "cortex-m0plus"),
    # RISC-V: 没有 -mcpu, 由 arch/abi 表达 (见 tests/test_riscv.py 的端到端用例)
    ("CH32V003F4P6", "ch32v", "CH32V003", ""),
]

# 未知厂商必须保持"查不到"（不能让前缀匹配过宽而误认）
_UNKNOWN = ["MIMXRT1062", "STM32F9XX", "ESP32-S3", "AT32F999"]


class TestTablesAreSelfConsistent(unittest.TestCase):
    """所有设备表的形状与"环境自洽性"。"""

    def setUp(self):
        self.tables = devices.load_tables()
        self.assertTrue(self.tables, "至少要有一张设备表")

    def test_schema_and_cores(self):
        for name, data in self.tables.items():
            with self.subTest(table=name):
                self.assertEqual(data.get("schema_version"), 1)
                families = data.get("families") or {}
                self.assertTrue(families, f"{name}.json 没有 families")
                for key, entry in families.items():
                    self.assertIsInstance(entry, dict, f"{name}:{key}")
                    # ARM 给 cpu; 非 ARM (RISC-V) 给 arch/abi —— 二者必须有其一
                    self.assertTrue(
                        entry.get("cpu") or entry.get("arch"),
                        f"{name}:{key} 既没有 cpu 也没有 arch",
                    )
                    self.assertIn("fpu", entry, f"{name}:{key} 缺 fpu (无 FPU 也要写 '-mfloat-abi=soft')")
                    # 非 ARM 内核不许出现 -mthumb/-mfpu 之类 ARM 专用参数。
                    # 要么给出 arch/abi (走非 ARM 路径), 要么用 _note 说明"当前只能拒绝"。
                    if not str(entry["cpu"]).startswith("cortex-"):
                        self.assertEqual(entry.get("fpu"), "", f"{name}:{key} 非 ARM 却给了 FPU 开关")
                        self.assertTrue(
                            entry.get("arch") or entry.get("_note"),
                            f"{name}:{key} 非 ARM 必须给出 arch (或说明为何只拒绝)",
                        )

    @unittest.skipUnless(_OCD_SCRIPTS.is_dir(), "本机没有 OpenOCD 脚本目录")
    def test_ocd_targets_exist_locally(self):
        """表里写的 openocd target 必须真的存在 —— 不存在就得有 _note 说明。

        不存在又没说明 = 烧录时必然失败 (而 burn 会把它报成"烧录失败", 误导排查方向)。
        允许 "名字正确但本机 OpenOCD 版本较老" 的情形, 但必须写清 + burn 前预检
        (见 gcc_burn._target_cfg_exists)。
        """
        for name, data in self.tables.items():
            for key, entry in (data.get("families") or {}).items():
                if not isinstance(entry, dict):
                    continue
                ocd = entry.get("ocd")
                if not ocd or (_OCD_SCRIPTS / str(ocd)).is_file():
                    continue
                with self.subTest(table=name, family=key):
                    self.assertTrue(
                        entry.get("_note"),
                        f"{name}:{key} 写的 {ocd} 在本机 OpenOCD 里不存在, 且没有 _note 说明",
                    )

    @unittest.skipUnless(_OCD_SCRIPTS.is_dir(), "本机没有 OpenOCD 脚本目录")
    def test_probe_cfgs_exist_locally(self):
        for name, data in self.tables.items():
            for probe in devices.probes(data):
                with self.subTest(table=name, probe=probe.get("name")):
                    self.assertTrue((_OCD_SCRIPTS / probe["cfg"]).is_file())

    def test_memory_entries_have_sources(self):
        """memory 是"拦住超出真机"的依据, 所以每条都必须能追到出处。"""
        for name, data in self.tables.items():
            for family, table in (data.get("memory") or {}).items():
                with self.subTest(table=name, family=family):
                    self.assertIsInstance(table, dict)
                    self.assertTrue(
                        table.get("_sources") or table.get("models") or data.get("_memory_comment"),
                        f"{name}:{family} 的容量数据没有出处",
                    )


class TestVendorLookup(unittest.TestCase):
    def setUp(self):
        self.tables = devices.load_tables()

    def test_known_devices_route_to_their_table(self):
        for device, table, family, cpu in _LOOKUPS:
            with self.subTest(device=device):
                name, info = devices.device_info_any(device, self.tables)
                self.assertEqual(name, table)
                self.assertEqual(info.family, family)
                self.assertEqual(info.cpu, cpu)
                self.assertEqual(devices.table_of(device, self.tables)[0], table)

    def test_unknown_vendors_stay_unknown(self):
        """未覆盖的厂商必须**查不到** —— 查不到会走"CPU 无法确定"的确定性拒绝。"""
        for device in _UNKNOWN:
            with self.subTest(device=device):
                name, info = devices.device_info_any(device, self.tables)
                self.assertEqual(name, "")
                self.assertIsNone(info.cpu)

    def test_memory_code_path_gd32(self):
        data = self.tables["gd32"]
        self.assertEqual(devices.memory_for("GD32F103C8T6", "GD32F103", data), (65536, 20480))
        self.assertEqual(devices.memory_for("GD32F103RCT6", "GD32F103", data), (262144, 49152))
        # 容量码不在表里时跳过校验, 而不是猜一个
        self.assertIsNone(devices.memory_for("GD32F103XXX", "GD32F103", data))

    def test_memory_models_path_at32(self):
        data = self.tables["at32"]
        self.assertEqual(devices.memory_for("AT32F403AVGT7", "AT32F403A", data), (1048576, 229376))
        # models 是精确表: 表里没有的型号直接跳过, 不再按容量码猜
        self.assertIsNone(devices.memory_for("AT32F403AZZT7", "AT32F403A", data))

    def test_alias_prefix_also_resolves_memory(self):
        """真实订货型号带厂商前缀 (ATSAMD21J18A) —— 别名既能查系列, 也能查容量。"""
        data = self.tables["samd"]
        self.assertEqual(devices.family_by_prefix("ATSAMD21J18A", data), "SAMD21")
        self.assertEqual(devices.memory_for("ATSAMD21J18A", "SAMD21", data), (262144, 32768))
        self.assertEqual(devices.memory_for("SAMD21J18A", "SAMD21", data), (262144, 32768))

    def test_non_arm_flash_base_follows_vendor(self):
        """烧录基址必须跟着厂商走: RP2040 的 XIP 在 0x10000000, nRF 在 0x00000000。"""
        self.assertEqual(devices.flash_base(self.tables["rp2040"], "RP2040"), "0x10000000")
        self.assertEqual(devices.flash_base(self.tables["nordic"], "NRF52840"), "0x00000000")
        self.assertEqual(devices.flash_base(self.tables["samd"], "SAMD51"), "0x00000000")

    def test_flash_base_follows_table(self):
        self.assertEqual(devices.flash_base(self.tables["gd32"], "GD32F103"), "0x08000000")
        self.assertEqual(devices.flash_base({}, None), "0x08000000")
        self.assertEqual(devices.ram_base(self.tables["at32"], "AT32F435"), "0x20000000")


class TestNonArmIsRefused(unittest.TestCase):
    """非 ARM 内核 (RISC-V) 必须停住, 绝不能带着 -mcpu=riscv 去调 arm-none-eabi-gcc。"""

    def _model(self, **kw) -> ProjectModel:
        return ProjectModel(backend="gcc_make", root=Path("."), device="GD32VF103C8T6", **kw)

    def test_riscv_core_is_refused(self):
        refusal = gcc_make._cpu_guard(self._model(cpu="riscv"))
        self.assertIsNotNone(refusal)
        self.assertIn("不是 ARM", refusal.message)

    def test_arm_cores_pass(self):
        for cpu in ("cortex-m0plus", "cortex-m3", "cortex-m4", "cortex-m33"):
            with self.subTest(cpu=cpu):
                self.assertIsNone(gcc_make._cpu_guard(self._model(cpu=cpu)))

    def test_arch_declared_non_arm_passes(self):
        """设备表给出 arch/abi 时走非 ARM 路径, 不算"内核未知"。"""
        self.assertIsNone(gcc_make._cpu_guard(self._model(cpu="riscv", arch="rv32imac", abi="ilp32")))

    def test_missing_core_is_not_this_guard(self):
        """内核为空由"CPU/FPU 无法确定"那条负责, 不要在这里重复报错。"""
        self.assertIsNone(gcc_make._cpu_guard(self._model(cpu="")))


class TestEntryAcceptsStringPaths(unittest.TestCase):
    """后端入口 (build_model) 收字符串也认 —— 库调用方不该吃到裸 TypeError。"""

    def test_gcc_build_model_accepts_string(self):
        import tempfile

        from support import MAIN_C, _MIN_CPROJECT, _MIN_STARTUP

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "p"
            (root / "Core" / "Src").mkdir(parents=True)
            (root / "Core" / "Startup").mkdir(parents=True)
            (root / "Core" / "Src" / "main.c").write_text(MAIN_C, encoding="utf-8")
            (root / "Core" / "Startup" / "startup_stm32f103rctx.s").write_text(_MIN_STARTUP, encoding="utf-8")
            (root / "STM32F103RCTX_FLASH.ld").write_text(
                "MEMORY\n{\n  FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 256K\n"
                "  RAM (xrw)  : ORIGIN = 0x20000000, LENGTH = 48K\n}\nSECTIONS\n{\n}\n",
                encoding="utf-8",
            )
            (root / ".cproject").write_text(_MIN_CPROJECT, encoding="utf-8")
            model = gcc_make.build_model(str(root), {})  # 字符串而不是 Path
            self.assertEqual(model.device, "STM32F103RCTx")
            self.assertEqual(model.cpu, "cortex-m3")


if __name__ == "__main__":
    unittest.main()
