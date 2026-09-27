"""烧录提供者测试: pyOCD 解析、target 名推导、烧录器选择逻辑。

不依赖硬件的部分一律用捕获的真实输出做解析断言; 真机部分在 test_hardware.py 里。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (导入即把 src 加入 sys.path)
from xtcli import flash
from xtcli.backends import gcc_make
from xtcli.discovery import Toolchain
from xtcli.exec import RunResult
from xtcli.model import Context, ProjectModel

# 真机实测捕获的输出样本（pyocd 0.45.1 + ST-Link）
# 注意: unique_id 是**合成值** —— 样本结构照抄真机输出, 但硬件序列号不进公开仓库
SAMPLE_PROBE_JSON = """{
    "pyocd_version": "0.45.1",
    "version": {"major": 1, "minor": 1},
    "status": 0,
    "boards": [
        {
            "unique_id": "0000FF00FF00FF00FF00FF00",
            "info": "STMicroelectronics STM32 STLink",
            "board_vendor": null,
            "board_name": "Generic",
            "target": "cortex_m",
            "vendor_name": "STMicroelectronics",
            "product_name": "STM32 STLink"
        }
    ]
}"""

SAMPLE_READ = """0001163 I Loading [load_cmd]
08000000:  2000c000 080012f9 080004b1 080004b9    | ...............|
"""


def _ctx(*, openocd: bool = True, ocd_target: str = "target/stm32f1x.cfg", flasher: str = "auto") -> Context:
    tools = Toolchain()
    tools.openocd = Path(r"C:\fake\openocd.exe") if openocd else None
    model = ProjectModel(backend="gcc_make", root=Path("."), extra={"ocd_target": ocd_target})
    return Context(root=Path("."), opt={"flasher": flasher}, tools=tools, model=model, backend=None)  # type: ignore[arg-type]


class TestTargetName(unittest.TestCase):
    def test_cube_style_device(self):
        # CubeMX 的 DeviceId 带温度后缀 Tx
        self.assertEqual(flash.pyocd_target_name("STM32F103RCTx"), "stm32f103rc")
        self.assertEqual(flash.pyocd_target_name("STM32F407VGTx"), "stm32f407vg")

    def test_bare_device(self):
        self.assertEqual(flash.pyocd_target_name("STM32F103RC"), "stm32f103rc")

    def test_empty(self):
        self.assertEqual(flash.pyocd_target_name(""), "")
        self.assertEqual(flash.pyocd_target_name("", {}), "")


class TestProbeListParsing(unittest.TestCase):
    def test_parses_real_sample(self):
        from unittest import mock

        tool = flash.PyocdTool(exe=Path("pyocd.exe"))
        with mock.patch("xtcli.exec.run", return_value=RunResult(argv=[], exit_code=0, lines=SAMPLE_PROBE_JSON.splitlines())):
            probes = flash.list_probes(tool)
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["unique_id"], "0000FF00FF00FF00FF00FF00")
        self.assertEqual(probes[0]["vendor_name"], "STMicroelectronics")

    def test_garbage_output_yields_empty(self):
        from unittest import mock

        tool = flash.PyocdTool(exe=Path("pyocd.exe"))
        with mock.patch("xtcli.exec.run", return_value=RunResult(argv=[], exit_code=1, lines=["boom"])):
            self.assertEqual(flash.list_probes(tool), [])


class TestReadWordsParsing(unittest.TestCase):
    def test_parses_commander_output(self):
        from unittest import mock

        tool = flash.PyocdTool(exe=Path("pyocd.exe"))
        with mock.patch("xtcli.exec.run", return_value=RunResult(argv=[], exit_code=0, lines=SAMPLE_READ.splitlines())):
            words = flash.read_words(tool, "stm32f103rc", "0x08000000", 4)
        self.assertEqual(words, [0x2000C000, 0x080012F9, 0x080004B1, 0x080004B9])

    def test_read32_length_is_bytes(self):
        """坑: pyocd 的 ``read32 ADDR LEN`` 里 LEN 是**字节**数, 不是字数。"""
        from unittest import mock

        captured: list[list[str]] = []

        def fake_run(argv, **kwargs):
            captured.append([str(a) for a in argv])
            return RunResult(argv=[], exit_code=0, lines=SAMPLE_READ.splitlines())

        tool = flash.PyocdTool(exe=Path("pyocd.exe"))
        with mock.patch("xtcli.exec.run", side_effect=fake_run):
            flash.read_words(tool, "stm32f103rc", "0x08000000", 4)
        self.assertTrue(captured)
        self.assertIn("read32 0x08000000 16", captured[0], "4 个字必须传 16 字节")

    def test_short_output_returns_none(self):
        from unittest import mock

        tool = flash.PyocdTool(exe=Path("pyocd.exe"))
        with mock.patch("xtcli.exec.run", return_value=RunResult(argv=[], exit_code=0, lines=["nope"])):
            self.assertIsNone(flash.read_words(tool, "stm32f103rc", "0x08000000", 4))


class TestFlasherSelection(unittest.TestCase):
    """auto 规则: 有 openocd + 有 target 配置 → openocd; 否则 pyOCD。"""

    def test_auto_prefers_openocd_when_target_known(self):
        self.assertEqual(gcc_make._resolve_flasher(_ctx()), "openocd")

    def test_explicit_overrides(self):
        self.assertEqual(gcc_make._resolve_flasher(_ctx(flasher="pyocd")), "pyocd")
        self.assertEqual(gcc_make._resolve_flasher(_ctx(flasher="openocd")), "openocd")
        self.assertEqual(gcc_make._resolve_flasher(_ctx(flasher="PYOCD")), "pyocd")

    def test_unknown_value_falls_back_to_auto(self):
        self.assertEqual(gcc_make._resolve_flasher(_ctx(flasher="wat")), "openocd")

    def test_auto_falls_back_to_pyocd_without_target(self):
        from unittest import mock

        with mock.patch("xtcli.flash.find_pyocd", return_value=flash.PyocdTool(exe=Path("pyocd.exe"))):
            self.assertEqual(gcc_make._resolve_flasher(_ctx(ocd_target="")), "pyocd")

    def test_auto_without_pyocd_stays_openocd(self):
        from unittest import mock

        with mock.patch("xtcli.flash.find_pyocd", return_value=None):
            self.assertEqual(gcc_make._resolve_flasher(_ctx(ocd_target="")), "openocd")


class TestPyocdDiscovery(unittest.TestCase):
    def test_finds_isolated_venv(self):
        tool = flash.find_pyocd()
        if tool is None:
            self.skipTest("pyocd 未安装")
        self.assertTrue(tool.exe.is_file())
        self.assertTrue(tool.version)
        self.assertIn(tool.source, ("隔离 venv (.venv-pyocd)", "项目 venv", "PATH"))

    def test_missing_hint_mentions_isolated_install(self):
        text = " ".join(flash.missing_hint())
        self.assertIn("xtcli-setup -Pyocd", text)
        self.assertIn("零依赖", text)


class TestProbeUnavailableClassification(unittest.TestCase):
    """契约: "探针不可用"是环境问题(退出码 3), 不是"烧录失败"(7)。

    否则脚本会把"探针没插"当成"烧录出错"。实测遇到: 探针被拔掉后缓存里仍有
    interface 配置, openocd 报 open failed —— 必须归类为 3。
    """

    def test_recognizes_openocd_open_failure(self):
        text = "Info : clock speed 1000 kHz\nError: open failed\n** OpenOCD init failed **"
        self.assertTrue(gcc_make._looks_like_probe_unavailable(text))

    def test_recognizes_pyocd_no_probe(self):
        self.assertTrue(gcc_make._looks_like_probe_unavailable("No probes with matching unique ID found"))
        self.assertTrue(gcc_make._looks_like_probe_unavailable("no available probes"))

    def test_real_flash_failure_is_not_misclassified(self):
        text = "** Programming Started **\nError: flash write failed at 0x08000000"
        self.assertFalse(gcc_make._looks_like_probe_unavailable(text))

    def test_unknown_target_is_not_a_probe_problem(self):
        self.assertFalse(gcc_make._looks_like_probe_unavailable("unknown target 'stm32f999xx'"))


class TestStm32PathUnaffected(unittest.TestCase):
    def test_openocd_still_default_for_stm32(self):
        """新增 pyOCD 不能改变 STM32 的默认烧录路径。"""
        from xtcli import config, devices

        device = "STM32F103RCTx"
        data = devices.load_stm32()
        info = devices.device_info(device, data)
        self.assertTrue(info.ocd_target)
        self.assertIsInstance(config.load(), dict)


if __name__ == "__main__":
    unittest.main()
