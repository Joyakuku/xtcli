"""IAR .ewp 的配置选择 —— 回归测试。

缺陷: 请求的配置名在 .ewp 里不存在时, 旧实现会**静默改用第一个配置**。
配置不同意味着宏定义 / 头文件路径 / 优化级别都可能不同, 于是静默产出与用户预期
不符的固件。现在必须给出告警并列出可用配置名（提示用 ``-Config``）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path)

from xtcli.backends import iar_ewp

_EWP = """<?xml version="1.0" encoding="UTF-8"?>
<project>
  <configuration>
    <name>{name}</name>
    <settings>
      <name>General</name>
      <data>
        <option>
          <name>OGChipSelectEditMenu</name>
          <state>STM32F103RC\tST STM32F103RC</state>
        </option>
      </data>
    </settings>
  </configuration>
</project>
"""


def _write_ewp(root: Path, names: list[str]) -> Path:
    blocks = []
    for name in names:
        blocks.append(_EWP.format(name=name).split("<project>")[1].split("</project>")[0])
    path = root / "proj.ewp"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<project>' + "".join(blocks) + "</project>\n",
        encoding="utf-8",
    )
    return path


class TestEwpConfigSelection(unittest.TestCase):
    def test_existing_config_is_used_without_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_ewp(Path(tmp), ["Debug", "Release"])
            cfg = iar_ewp.read(path, "Release")
        self.assertEqual(cfg.config, "Release")
        self.assertEqual([w for w in cfg.warnings if "配置" in w], [])

    def test_missing_config_warns_and_lists_available(self):
        """回归: 曾静默改用第一个配置。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_ewp(Path(tmp), ["Release", "Debug"])
            cfg = iar_ewp.read(path, "NoSuchConfig")
        joined = " | ".join(cfg.warnings)
        self.assertIn("NoSuchConfig", joined)
        self.assertIn("Release", joined)
        self.assertIn("Debug", joined)
        self.assertIn("-Config", joined)
        # 仍然能用（回退到第一个配置），只是不再无声
        self.assertEqual(cfg.device, "STM32F103RC")

    def test_first_config_is_used_when_nothing_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_ewp(Path(tmp), ["Release", "Debug"])
            cfg = iar_ewp.read(path, "")
        self.assertEqual(cfg.device, "STM32F103RC")
        self.assertEqual([w for w in cfg.warnings if "配置" in w], [])


if __name__ == "__main__":
    unittest.main()
