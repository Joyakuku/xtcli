"""拒绝路径测试: 退出码 + 精确提示 + **零写入**。

"正确地拒绝"和"正确地成功"一样是功能: 契约要求给出确定性结论, 而不是静默产出
一个能编但错的工程。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import cli_output, in_dir, make_fixture, snapshot

from xtcli.errors import Exit

_CASES = (
    ("ioc-only", "CubeMX 尚未生成代码", ["Core/Src/"]),
    ("no-chip", "有源码但缺芯片信息", ["*.ioc", ".cproject"]),
)


class TestRefusals(unittest.TestCase):
    def test_exit_code_message_missing_and_zero_writes(self):
        for kind, message, missing in _CASES:
            with self.subTest(kind=kind):
                with tempfile.TemporaryDirectory() as tmp:
                    root = make_fixture(Path(tmp), kind)
                    before = snapshot(root)
                    with in_dir(root):
                        code, output = cli_output(["init", "-Target", "stm32"])
                    after = snapshot(root)

                    self.assertEqual(code, int(Exit.PROJECT), output)
                    self.assertIn(message, output)
                    for item in missing:
                        self.assertIn(item, output)
                    self.assertEqual(before, after, f"拒绝时不得写盘, 但新增了 {after - before}")

    def test_doctor_also_reports_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "ioc-only")
            with in_dir(root):
                code, output = cli_output(["doctor"])
            self.assertEqual(code, int(Exit.PROJECT))
            self.assertIn("确定性拒绝", output)

    def test_unrecognized_directory_is_unsupported(self):
        """没有任何工程标记的目录 -> UNSUPPORTED(5), 且零写入。

        与上面的 PROJECT(4) 刻意区分: 4 = 认出了工程形态但缺东西;
        5 = 没有后端认得这个目录 (显式 -Target 也不放行, 见
        TestExplicitTargetCannotBypassDetect)。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = make_fixture(Path(tmp), "not-a-project")
            before = snapshot(root)
            with in_dir(root):
                code, output = cli_output(["init", "-Target", "stm32"])
            self.assertEqual(code, int(Exit.UNSUPPORTED), output)
            self.assertIn("没有 backend 能处理该目录", output)
            self.assertEqual(before, snapshot(root), "拒绝时不得写盘")

    def test_burn_and_build_on_non_project(self):
        for verb in ("build", "burn"):
            with self.subTest(verb=verb):
                with tempfile.TemporaryDirectory() as tmp:
                    root = make_fixture(Path(tmp), "not-a-project")
                    before = snapshot(root)
                    with in_dir(root):
                        code, output = cli_output([verb, "-Target", "stm32"])
                    after = snapshot(root)
                    # 不是任何后端认得的工程 -> 确定性拒绝; 且绝不写盘
                    self.assertEqual(code, int(Exit.UNSUPPORTED), output)
                    self.assertEqual(before, after, f"拒绝时不得写盘: {after - before}")


class TestExplicitTargetCannotBypassDetect(unittest.TestCase):
    """回归: 显式 -Target 曾在"只是恰好有用户 Makefile"的无关目录里执行 make 目标。

    实测过的后果: 在 ``outer/sub/deep`` 下执行 ``build -Clean -Target stm32``,
    make 真的在 ``outer`` 执行了 clean 与 all (哨兵文件被改写), 还返回 0
    报"构建成功"。工程识别门槛必须对显式 -Target 同样生效。
    """

    def test_refuses_unrelated_dir_with_user_makefile(self):
        with tempfile.TemporaryDirectory() as tmp:
            outer = Path(tmp) / "outer"
            deep = outer / "sub" / "deep"
            deep.mkdir(parents=True)
            (outer / "Makefile").write_text(
                "all:\n\t@echo ALL > ran.txt\nclean:\n\t@echo CLEAN > ran.txt\n",
                encoding="utf-8",
            )
            with in_dir(deep):
                code, output = cli_output(["build", "-Clean", "-Target", "stm32"])
            self.assertEqual(code, int(Exit.UNSUPPORTED), output)
            self.assertFalse((outer / "ran.txt").exists(), "不得在定位错的目录里执行 make 目标")

    def test_unknown_target_name_is_usage_error(self):
        """-Target 拼错要报 USAGE 并列出可用取值, 而不是说"工程不被支持"。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Core" / "Src").mkdir(parents=True)
            (root / "Core" / "Src" / "main.c").write_text(
                "int main(void){return 0;}\n", encoding="utf-8"
            )
            with in_dir(root):
                code, output = cli_output(["doctor", "-Target", "stm32wba"])
            self.assertEqual(code, int(Exit.USAGE), output)
            self.assertIn("未知的 -Target 名", output)


if __name__ == "__main__":
    unittest.main()
