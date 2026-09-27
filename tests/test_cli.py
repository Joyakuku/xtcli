"""CLI 契约单测: 退出码、命令面、拒绝位置参数。

这些是**对外承诺**, 与 PowerShell 版逐字一致, 移植时不允许"顺手改进"。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import cli_output, in_dir, make_fixture

from xtcli.errors import Exit


class TestArgvContract(unittest.TestCase):
    def test_help_exits_zero(self):
        code, out = cli_output(["help"])
        self.assertEqual(code, int(Exit.OK))
        self.assertIn("xtcli-build-pj", out)
        self.assertIn("退出码", out)

    def test_help_flags(self):
        for flag in ("-h", "--help"):
            code, out = cli_output([flag])
            self.assertEqual(code, int(Exit.OK), flag)
            self.assertIn("xtcli", out)

    def test_version_reports_interpreter(self):
        code, out = cli_output(["version"])
        self.assertEqual(code, int(Exit.OK))
        self.assertIn("解释器", out)
        self.assertIn("底座", out)

    def test_positional_dir_is_rejected_with_usage_code(self):
        """不接受"工程目录"位置参数 —— 这是刻意的接口决定。"""
        code, out = cli_output(["build", r"E:\some\dir"])
        self.assertEqual(code, int(Exit.USAGE))
        self.assertIn("不接受", out)

    def test_unknown_option_is_rejected(self):
        code, _ = cli_output(["build", "-Nobuild"])
        self.assertEqual(code, int(Exit.USAGE))

    def test_unknown_verb_is_usage_error(self):
        code, out = cli_output(["frobnicate"])
        self.assertEqual(code, int(Exit.USAGE))
        self.assertIn("未知动作", out)

    def test_option_spellings_preserved(self):
        """-Clean / -NoBuild 这类单横线多字符写法必须保留（用户肌肉记忆）。"""
        from xtcli.cli import build_parser

        args = build_parser().parse_args(["build", "-Clean", "-NoBuild", "-Config", "Release"])
        self.assertTrue(args.clean)
        self.assertTrue(args.no_build)
        self.assertEqual(args.config, "Release")
        # 双横线写法同样可用
        args2 = build_parser().parse_args(["build", "--clean", "--no-build"])
        self.assertTrue(args2.clean)
        self.assertTrue(args2.no_build)


class TestRefusalExitCodes(unittest.TestCase):
    def _init_code(self, kind: str) -> int:
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), kind)
            with in_dir(root):
                code, _ = cli_output(["init", "-Target", "stm32"])
            return code

    def test_not_a_project_is_unsupported_code(self):
        """目录里没有任何后端认得的工程标记 -> UNSUPPORTED(5), 不是 PROJECT(4)。

        区别是刻意的: 5 = 没有后端认得这个目录 (含显式 -Target 与目录不匹配);
        4 = 认出了工程形态但缺东西 (下面两个用例)。
        """
        self.assertEqual(self._init_code("not-a-project"), int(Exit.UNSUPPORTED))

    def test_ioc_without_code_is_project_code(self):
        self.assertEqual(self._init_code("ioc-only"), int(Exit.PROJECT))

    def test_sources_without_chip_info_is_project_code(self):
        self.assertEqual(self._init_code("no-chip"), int(Exit.PROJECT))


class TestRootOption(unittest.TestCase):
    """``-Root``: 从工程子目录里也能指定真正的工程根。

    实测过的场景: 在 ESP-IDF 工程的 ``main/`` 下运行会被当成工程根 (那里有
    CMakeLists.txt), 于是报"没有 backend 能处理该目录"。``-Root ..`` 可以纠正。
    """

    def test_root_points_at_project_from_a_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "ioc-only")
            deep = root / "sub" / "deep"
            deep.mkdir(parents=True)
            with in_dir(deep):
                code, output = cli_output(["doctor", "-Root", str(root)])
            # 用 -Root 后必须落到工程本身: ioc-only 是"确定性拒绝"而不是"不认识这个目录"
            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("CubeMX 尚未生成代码", output)
            self.assertNotIn("没有 backend 能处理该目录", output)

    def test_missing_root_directory_is_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope"
            with in_dir(Path(tmp)):
                code, output = cli_output(["doctor", "-Root", str(missing)])
            self.assertEqual(code, int(Exit.USAGE), output)
            self.assertIn("-Root 指定的目录不存在", output)

    def test_far_above_marker_tells_where_the_root_is(self):
        """回归 M3: 工程标记超出向上查找上限时, 必须把**真正的工程根**报出来。

        旧实现会静默把当前目录当工程根, 只报"没有 backend 能处理该目录 (目录: <当前>)",
        完全不提"向上其实有工程标记、只是太远" —— 用户无法判断该往哪走。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "proj"
            deep = base
            for part in ("b", "c", "d", "e", "f", "g"):
                deep = deep / part
            deep.mkdir(parents=True)
            (base / "Makefile").write_text("all:\n\t@echo hi\n", encoding="utf-8")
            with in_dir(deep):
                code, output = cli_output(["doctor", "-Target", "stm32"])
            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("工程标记在", output)
            self.assertIn("超出查找上限", output)
            # 这一次是**找到了**、只是太远 —— 不能说"未找到"
            self.assertNotIn("未找到工程标记", output)

    def test_no_marker_anywhere_says_not_found(self):
        """整条祖先链都没有标记时, note 才说"未找到", 且退出码仍是 5。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "not-a-project")
            with in_dir(root):
                code, output = cli_output(["doctor", "-Target", "stm32"])
            self.assertEqual(code, int(Exit.UNSUPPORTED), output)
            self.assertIn("没有 backend 能处理该目录", output)


class TestFpuDashValue(unittest.TestCase):
    """回归: `-Fpu` 的取值本身以 `-` 开头, 曾被 argparse 当成选项。

    实测 `-Fpu -mfloat-abi=soft` → ``error: argument -Fpu/--fpu: expected one
    argument``（退出码 2）—— 也就是**帮助里写的用法直接用不了**。
    """

    def test_documented_space_form_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "ioc-only")
            with in_dir(root):
                code, output = cli_output(["doctor", "-Fpu", "-mfloat-abi=soft"])
            # 不再是 USAGE(2); ioc-only 是确定性拒绝
            self.assertNotEqual(code, int(Exit.USAGE), output)
            self.assertEqual(code, int(Exit.PROJECT), output)

    def test_equals_form_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "ioc-only")
            with in_dir(root):
                code, _ = cli_output(["doctor", "-Fpu=-mfloat-abi=soft"])
            self.assertNotEqual(code, int(Exit.USAGE))

    def test_missing_value_is_still_a_usage_error(self):
        """漏了取值时不能被"折叠"掩盖成合法调用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "ioc-only")
            with in_dir(root):
                code, _ = cli_output(["doctor", "-Fpu", "-NoBuild"])
            self.assertEqual(code, int(Exit.USAGE))


if __name__ == "__main__":
    unittest.main()
