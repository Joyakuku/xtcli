"""工具链发现 —— 独立性回归测试。

对照的三个真实缺陷:

1. **gcc 没有 PATH 兜底**（`make`/`openocd` 一直有）。后果: 工具链不在默认根
   ``E:\\env`` 下时直接报"环境缺失", 哪怕它就在 PATH 上。实测确认过。
2. **缺工具的提示写死路径**（"在 E:\\env 下未找到"）: 用户用 XTCLI_ROOTS 指了别处
   也照旧这么说, 换台机器更是一点信息都没有。
3. **配置错的搜索根被静默跳过**: 用户以为根生效了, 实际什么都没搜。

（``XTCLI_ROOTS`` 本身是**追加**语义而非替换 —— 代码与实测都确认, 这里加一条
测试把它钉住, 免得以后被"优化"成替换。）
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path)

from xtcli import config, discovery


class TestGccPathFallback(unittest.TestCase):
    def test_gcc_is_found_on_path_when_roots_are_empty(self):
        """回归: 搜索根全无效时, 必须退到 PATH —— 修复前这里返回 None。"""
        fake = Path(r"Z:\somewhere\bin\arm-none-eabi-gcc.exe")

        def fake_from_path(name: str) -> Path | None:
            return fake if name.startswith("arm-none-eabi-gcc") else None

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config.load()
            cfg["roots"] = []
            with mock.patch.object(config, "load", return_value=cfg), \
                 mock.patch.object(discovery, "DEFAULT_ROOTS", (str(Path(tmp) / "nope"),)), \
                 mock.patch.dict(os.environ, {"XTCLI_ROOTS": ""}), \
                 mock.patch.object(discovery, "_from_path", side_effect=fake_from_path):
                tools = discovery.discover()
        self.assertEqual(tools.gcc_path, fake)
        self.assertEqual(tools.gcc_root, "Z:/somewhere/bin")
        self.assertFalse(any("arm-none-eabi-gcc" in item for item in tools.missing))

    def test_flat_toolchain_layout_is_found(self):
        """平铺布局 <root>\\bin\\arm-none-eabi-gcc.exe 也要能找到。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir(parents=True)
            gcc = root / "bin" / "arm-none-eabi-gcc.exe"
            gcc.write_bytes(b"stub")
            found = discovery._gcc_candidates([root])
        self.assertEqual([item[0] for item in found], [gcc])


class TestSearchRootsSemantics(unittest.TestCase):
    def test_configured_roots_are_appended_not_replaced(self):
        """XTCLI_ROOTS 是追加: 默认根必须仍在列表里（顺序: 配置的在前）。"""
        with tempfile.TemporaryDirectory() as tmp:
            extra = Path(tmp)
            cfg = {"roots": []}
            with mock.patch.dict(os.environ, {"XTCLI_ROOTS": str(extra)}), \
                 mock.patch.object(discovery, "DEFAULT_ROOTS", (str(extra / "d1"),)):
                (extra / "d1").mkdir()
                roots = discovery.search_roots(cfg)
        resolved = [str(item) for item in roots]
        self.assertEqual(resolved[0], str(extra.resolve()))
        self.assertIn(str((extra / "d1").resolve()), resolved)

    def test_bad_configured_root_is_reported(self):
        """配置错的根不能静默跳过 —— 要能报出来。"""
        skipped: list[str] = []
        bad = r"Z:\__xtcli_definitely_not_here__"
        with mock.patch.dict(os.environ, {"XTCLI_ROOTS": bad}):
            roots = discovery.search_roots({"roots": []}, skipped)
        self.assertIn(bad, skipped)
        self.assertNotIn(Path(bad), roots)

    def test_missing_message_lists_actual_roots(self):
        message = discovery._missing_message("arm-none-eabi-gcc", [Path("E:/env"), Path("D:/tools")])
        self.assertIn(str(Path("E:/env")), message)
        self.assertIn(str(Path("D:/tools")), message)
        self.assertIn("PATH", message)

    def test_missing_message_without_roots_says_so(self):
        message = discovery._missing_message("make", [])
        self.assertIn("没有有效的搜索根", message)


if __name__ == "__main__":
    unittest.main()
