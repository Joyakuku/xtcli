"""命令链表契约: 表 / shim / 帮助 / 文档 必须一致。

背景: 命令名原先散落在 setup.ps1、cli.show_help、两份手册、test_architecture 四处,
改名时必然漏一处 (实测踩过)。现在唯一出处是 ``src/xtcli/commands.py``, 这层测试把它
钉住 —— 包括"预设选项"这类容易在某一处写错的东西。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import MAIN_C, cli_output, in_dir, make_min_project, toolchain_ready

from xtcli import cli, commands

REPO = Path(__file__).resolve().parents[1]
SETUP = REPO / "setup.ps1"
README = REPO / "README.md"
USER_MANUAL = REPO / "docs" / "使用手册.md"

_REQUIRE_REAL_BUILD = unittest.skipUnless(
    toolchain_ready(), "工具链不完整 (需要 arm-none-eabi-gcc / make)"
)


def _shim_block(text: str) -> str:
    return text[text.index("$shims = [ordered]@{"): text.index("foreach ($name in $shims.Keys)")]


class TestChainTable(unittest.TestCase):
    def test_every_chain_uses_a_real_verb(self):
        for chain in commands.CHAINS:
            with self.subTest(chain=chain.name):
                self.assertIn(chain.verb, cli.VERBS)

    def test_names_are_unique(self):
        names = [chain.name for chain in commands.CHAINS]
        self.assertEqual(len(names), len(set(names)))

    def test_preset_tokens_are_real_options(self):
        known = {
            option
            for action in cli.build_parser()._actions
            for option in getattr(action, "option_strings", [])
        }
        for chain in commands.CHAINS:
            for token in chain.preset:
                if token.startswith("-"):
                    with self.subTest(chain=chain.name, token=token):
                        self.assertIn(token, known)

    def test_burn_variants_lock_one_flasher_each(self):
        """burn / burn-openocd / burn-flash: 默认那条不锁 flasher, 另两条各锁一条链。"""
        by_name = {chain.name: chain for chain in commands.CHAINS}
        self.assertEqual(by_name["burn"].preset, ())
        self.assertEqual(by_name["burn-openocd"].preset, ("-Flasher", "openocd"))
        self.assertEqual(by_name["burn-flash"].preset, ("-Flasher", "pyocd"))

    def test_no_historical_names_in_the_table(self):
        """v1.0.0 起不留任何历史命令名 (含 -pj 别名): 老名字由 setup.ps1 从 PATH 清掉。"""
        names = [chain.name for chain in commands.CHAINS]
        self.assertFalse([name for name in names if name.endswith("-pj")], names)
        self.assertFalse([name for name in names if name.startswith(("stm32-", "esp32-"))], names)
        self.assertFalse(hasattr(commands, "aliases"), "别名机制已移除")

    def test_expand_keeps_user_arguments_and_accepts_both_spellings(self):
        self.assertEqual(
            commands.expand("build-all", ["-MakeTarget", "size"]),
            ["build", "-Clean", "-MakeTarget", "size"],
        )
        self.assertEqual(commands.expand("xtcli-build-all", []), ["build", "-Clean"])
        self.assertEqual(commands.expand("build", []), ["build"])
        self.assertIsNone(commands.expand("nope", []))
        self.assertIsNone(commands.expand("build-pj", []), "历史名不再被接受")


class TestShimsHelpAndDocsAgree(unittest.TestCase):
    def test_setup_ps1_generates_exactly_the_shim_names(self):
        block = _shim_block(SETUP.read_text(encoding="utf-8"))
        found = set(re.findall(r"'(xtcli[A-Za-z0-9_-]*)\.cmd'", block))
        self.assertEqual(found, set(commands.shim_names()) | {"xtcli"})

    def test_setup_ps1_presets_match_the_table(self):
        block = _shim_block(SETUP.read_text(encoding="utf-8"))
        for chain in commands.CHAINS:
            expected = " ".join([chain.verb, *chain.preset])
            with self.subTest(chain=chain.command):
                self.assertIn(f"'{chain.command}.cmd'", block)
                self.assertIn(f"= '{expected}'", block)

    def test_setup_ps1_cleans_historical_shims(self):
        """历史命令名的 shim 必须被主动清掉 —— 否则升级后 PATH 上留着指向已删命令的壳。"""
        text = SETUP.read_text(encoding="utf-8")
        block = text[text.index("$staleShims = @("): text.index("\n)\n", text.index("$staleShims = @("))]
        for stale in ("xtcli-init-pj", "xtcli-build-pj", "xtcli-burn-pj", "pj-build", "stm32-build-pj"):
            with self.subTest(stale=stale):
                self.assertIn(f"'{stale}'", block)

    def test_help_lists_every_command(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.show_help()
        help_text = buffer.getvalue()
        for chain in commands.CHAINS:
            with self.subTest(command=chain.command):
                self.assertIn(chain.command, help_text)

    def test_docs_list_every_command(self):
        for path in (README, USER_MANUAL):
            text = path.read_text(encoding="utf-8")
            for chain in commands.CHAINS:
                with self.subTest(doc=path.name, command=chain.command):
                    self.assertIn(chain.command, text)


class TestChainInvocation(unittest.TestCase):
    """命令名走真实 CLI 路径: 预设选项必须真的生效。"""

    def test_historical_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["build-pj"])
        self.assertNotEqual(code, 0, out)
        self.assertIn("未知动作", out)

    @_REQUIRE_REAL_BUILD
    def test_build_all_cleans_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                self.assertEqual(cli_output(["init", "-NoBuild"])[0], 0)
                self.assertEqual(cli_output(["build"])[0], 0)
                obj = root / "build" / "Core__Src__main.o"
                self.assertTrue(obj.is_file())
                sentinel = root / "build" / "leftover.o"
                sentinel.write_bytes(b"\x00")
                code, out = cli_output(["build-all"])
                # 必须在临时目录被删掉之前取状态 (否则断言会"假绿")
                obj_exists = obj.is_file()
                sentinel_exists = sentinel.exists()
        self.assertEqual(code, 0, out)
        self.assertFalse(sentinel_exists, "build-all 必须先 clean (build/ 应被清掉)")
        self.assertTrue(obj_exists, "clean 之后必须重新编译出对象")


if __name__ == "__main__":
    unittest.main()
