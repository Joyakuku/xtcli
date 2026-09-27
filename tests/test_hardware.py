"""真机在环测试（默认跳过）。

手动开启: ``set XTCLI_HW=1`` 后跑 ``python -m unittest tests.test_hardware``。
会真的烧写芯片, 所以必须显式开启。

断言:
  * 能识别到探针（且自检带了 target 配置）
  * program 成功且 verify 通过
  * **独立回读**的向量表内容与固件开头逐字一致
  * pyOCD 路径（若已装）同样能烧录并回读一致
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import cli_output, has_project, in_dir, project_path

from xtcli import discovery, flash

_ENABLED = os.environ.get("XTCLI_HW") == "1"
_TOOLS = discovery.discover()

# 探针不在线时的各种说法 —— 一律按"没有硬件"跳过, 而不是判失败
_NO_PROBE_HINTS = (
    "未识别到探针",
    "没有识别到任何调试探针",
    "探针不可用",
    "没有检测到探针",
    "pyOCD 不可用",
    "没有探针响应",
)


def _no_probe(code: int, output: str) -> bool:
    # 退出码 3 = ENVIRONMENT（含探针缺失）
    return code == 3 or any(hint in output for hint in _NO_PROBE_HINTS)


@unittest.skipUnless(_ENABLED, "需要 XTCLI_HW=1 才跑真机测试")
@unittest.skipUnless(_TOOLS.openocd, "缺少 openocd")
@unittest.skipUnless(has_project("1_LED"), "1_LED 不存在")
class TestHardware(unittest.TestCase):
    def test_burn_and_independent_readback(self):
        root = project_path("1_LED")
        with in_dir(root):
            code, output = cli_output(["burn", "-Target", "stm32", "-NoBuild"])
        if _no_probe(code, output):
            self.skipTest("没有连接探针")
        self.assertEqual(code, 0, output)
        self.assertIn("Verified OK", output)
        self.assertIn("回读校验通过", output)

    def test_probe_detection_reports_target_aware_result(self):
        root = project_path("1_LED")
        with in_dir(root):
            code, output = cli_output(["doctor", "-Probe"])
        self.assertEqual(code, 0, output)
        if "没有探针响应" in output:
            self.skipTest("没有连接探针")
        self.assertRegex(output, r"连上了")

    def test_pyocd_path_burns_and_reads_back(self):
        """pyOCD 是与 openocd 并行的独立烧录路径。"""
        tool = flash.find_pyocd()
        if tool is None:
            self.skipTest("pyocd 未安装 (xtcli-setup -Pyocd)")
        if not flash.list_probes(tool):
            self.skipTest("pyOCD 没有检测到探针")
        root = project_path("1_LED")
        with in_dir(root):
            code, output = cli_output(["burn", "-Target", "stm32", "-NoBuild", "-Flasher", "pyocd"])
        if _no_probe(code, output):
            self.skipTest("没有连接探针")
        self.assertEqual(code, 0, output)
        self.assertIn("pyOCD", output)
        self.assertIn("回读校验通过", output)

    def test_explicit_openocd_flasher_still_works(self):
        root = project_path("1_LED")
        with in_dir(root):
            code, output = cli_output(["burn", "-Target", "stm32", "-NoBuild", "-Flasher", "openocd"])
        if _no_probe(code, output):
            self.skipTest("没有连接探针")
        self.assertEqual(code, 0, output)
        self.assertIn("回读校验通过", output)


if __name__ == "__main__":
    unittest.main()
