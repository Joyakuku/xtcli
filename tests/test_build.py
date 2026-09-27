"""端到端构建测试: 在**临时副本**上 init + build, 断言 size 与黄金值一致。

绝不改动用户的工程目录 —— 先把工程复制出来再操作。

三个黄金值来自 PowerShell 版实测, 与 CubeIDE 参考产物逐字节对齐（.text 用
Makefile 源顺序时为 70608）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import (
    GOLDEN_SIZE,
    MAIN_C,
    cli_output,
    copy_project,
    elf_size,
    has_project,
    in_dir,
    make_min_project,
)

from xtcli import discovery

_TOOLS = discovery.discover()
_REQUIRE = unittest.skipUnless(
    _TOOLS.gcc_path and _TOOLS.make and _TOOLS.size,
    "工具链不完整 (需要 arm-none-eabi-gcc / make / size)",
)


@_REQUIRE
class TestBuildParity(unittest.TestCase):
    def _build(self, name: str, tmp: str) -> tuple[int, int, Path]:
        root = copy_project(name, Path(tmp))
        with in_dir(root):
            init_code, init_out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
            self.assertEqual(init_code, 0, init_out)
            build_code, build_out = cli_output(["build", "-Target", "stm32", "-Clean"])
        elf = root / "Debug" / f"{name}.elf"
        self.assertTrue(elf.is_file(), build_out)
        return build_code, build_out, elf

    def test_demo(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output, elf = self._build("demo", tmp)
            self.assertEqual(code, 0, output)
            self.assertEqual(elf_size(_TOOLS, elf), GOLDEN_SIZE["demo"])

    def test_freertos_hal_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output, elf = self._build("freertos_hal_template", tmp)
            self.assertEqual(code, 0, output)
            self.assertEqual(elf_size(_TOOLS, elf), GOLDEN_SIZE["freertos_hal_template"])

    def test_1_led_from_ewp(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, output, elf = self._build("1_LED", tmp)
            self.assertEqual(code, 0, output)
            self.assertEqual(elf_size(_TOOLS, elf), GOLDEN_SIZE["1_LED"])


@_REQUIRE
@unittest.skipUnless(has_project("demo"), "demo 工程不存在")
class TestBuildHygiene(unittest.TestCase):
    def test_no_stray_dash_p_directory(self):
        """坑: shell 分支判错会让 cmd 执行 ``mkdir -p``, 凭空造出名为 ``-p`` 的目录。

        （该项目里真的留下过一个这样的空目录 —— 先清掉它, 再断言没有被重新造出来。）
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("demo", Path(tmp))
            stray = root / "-p"
            if stray.exists():
                stray.rmdir()
            with in_dir(root):
                cli_output(["init", "-Target", "stm32", "-NoBuild"])
                cli_output(["build", "-Target", "stm32", "-Clean"])
            self.assertFalse(stray.exists(), "出现了 -p 目录: make 走了错误的 shell 分支")

    def test_clean_failure_is_not_swallowed(self):
        """清理失败必须中止 —— 否则 make 会对旧产物报 Nothing to be done, 假成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("demo", Path(tmp))
            with in_dir(root):
                cli_output(["init", "-Target", "stm32", "-NoBuild"])
                # 追加在末尾才能覆盖 rules.mk 里的 clean 目标（make 取最后一条配方）
                makefile = root / "xtcli" / "Makefile"
                makefile.write_text(
                    makefile.read_text(encoding="utf-8") + "\nclean:\n\t@exit 3\n", encoding="utf-8"
                )
                code, output = cli_output(["build", "-Target", "stm32", "-Clean"])
            self.assertNotEqual(code, 0)
            self.assertIn("清理失败", output)

    def test_init_creates_sentinel_root_makefile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("freertos_hal_template", Path(tmp))
            root_makefile = root / "Makefile"
            if root_makefile.exists():
                root_makefile.unlink()
            with in_dir(root):
                code, output = cli_output(["init", "-Target", "stm32", "-NoBuild"])
            self.assertEqual(code, 0, output)
            for rel in ("xtcli/config.mk", "xtcli/rules.mk", "xtcli/Makefile", "compile_commands.json"):
                self.assertTrue((root / rel).is_file(), rel)
            self.assertTrue(root_makefile.is_file(), "应补一个转发壳让裸 make 可用")
            self.assertIn("xtcli:generated", root_makefile.read_text(encoding="utf-8"))
            self.assertIn("XT_FINGERPRINT:", (root / "xtcli" / "config.mk").read_text(encoding="utf-8"))

    def test_stale_config_mk_warns(self):
        """config.mk 与工程脱节时必须告警，而不是静默编出错误固件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("demo", Path(tmp))
            with in_dir(root):
                cli_output(["init", "-Target", "stm32", "-NoBuild"])
                cfg = root / "xtcli" / "config.mk"
                cfg.write_text(cfg.read_text(encoding="utf-8").replace(
                    "XT_FINGERPRINT: ", "XT_FINGERPRINT: deadbeef"), encoding="utf-8")
                _code, output = cli_output(["build", "-Target", "stm32"])
            self.assertIn("不一致", output)

    def test_init_is_idempotent(self):
        """第二次 init 必须仍然重新生成骨架（不能被自己造的转发壳骗过去）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("freertos_hal_template", Path(tmp))
            with in_dir(root):
                first, _ = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                second, output = cli_output(["init", "-Target", "stm32", "-NoBuild"])
            self.assertEqual(first, 0)
            self.assertEqual(second, 0)
            self.assertNotIn("无需初始化", output)


@_REQUIRE
class TestCleanNeverRunsUserGoals(unittest.TestCase):
    """回归: A 档 (工程自带 Makefile) 的 ``-Clean`` 曾被直接执行用户的 clean 目标。

    实测危害: 用户工程的 clean 目标可能删除任意文件（踩过 ``precious.txt`` /
    ``userdata/``）。现在只拒绝并说明怎么自己跑, 不替用户执行。
    """

    @staticmethod
    def _make_makefile_project(root: Path) -> None:
        (root / "Core" / "Src").mkdir(parents=True)
        (root / "Core" / "Src" / "main.c").write_text(
            "int main(void){return 0;}\n", encoding="utf-8"
        )
        (root / "proj.ioc").write_text(
            "ProjectManager.DeviceId=STM32F103RCTx\nProjectManager.ProjectName=proj\n",
            encoding="utf-8",
        )
        # 用户的 clean 目标会写一个哨兵文件: 只要它出现, 就说明我们替他跑了 clean
        (root / "Makefile").write_text(
            "all:\n\t@echo ALL > ran_all.txt\nclean:\n\t@echo CLEAN > ran_clean.txt\n",
            encoding="utf-8",
        )

    def test_clean_does_not_run_the_user_clean_goal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            root.mkdir(parents=True)
            self._make_makefile_project(root)
            with in_dir(root):
                _, output = cli_output(["build", "-Clean"])
            self.assertFalse(
                (root / "ran_clean.txt").exists(),
                f"不得替用户执行他自己 Makefile 的 clean 目标\n{output}",
            )
            self.assertIn("跳过清理", output)
            self.assertIn("make -C", output)


@_REQUIRE
class TestChildEnvPinsToolchain(unittest.TestCase):
    """回归: 子进程 PATH 必须**前置本次选定的工具链目录**。

    xtcli 自己生成的 Makefile 用绝对路径（``CC := $(XT_TOOLCHAIN_BIN)/arm-none-eabi-gcc``），
    但 **A 档（工程自带 Makefile）** 里常见的 ``CC := arm-none-eabi-gcc`` 是**裸名**，
    不前置就会从环境 PATH 解析。

    实测过（把宿主 gcc 冒充成 ``arm-none-eabi-gcc`` 放在 PATH 最前面）：
    修复前自带 Makefile 的工程 ``build=6``（用了宿主 gcc 编 Cortex-M），
    把工具链目录前置进子进程 PATH 之后 ``build=0``；xtcli 生成的入口两种情况下都不受影响。
    """

    def test_build_pins_toolchain_before_ambient_path(self):
        from types import SimpleNamespace
        from unittest import mock

        from xtcli.backends import gcc_make
        from xtcli.model import Context

        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            # 造出 A 档场景: 工程自带一个 Makefile（CC 为裸名）
            (root / "Makefile").write_text(
                "CC := arm-none-eabi-gcc\nall:\n\t@echo all\nclean:\n\t@echo clean\n",
                encoding="utf-8",
            )
            model = gcc_make.build_model(root, {})
            self.assertEqual(model.refusals, [], [item.message for item in model.refusals])
            ctx = Context(root=root, opt={}, tools=_TOOLS, model=model, backend=None)

            seen: list[list[str]] = []

            def fake_run(_argv, **kwargs):
                seen.append(list(kwargs.get("prepend_path") or []))
                return SimpleNamespace(
                    exit_code=0, lines=[], text="", timed_out=False, ok=True, stdout="", stderr=""
                )

            with mock.patch.object(gcc_make.exec, "run", side_effect=fake_run), \
                 mock.patch.object(gcc_make, "_check_artifact_fresh", lambda *_a: None), \
                 mock.patch.object(gcc_make, "_check_fingerprint", lambda *_a: None):
                result = gcc_make.action_build(ctx)

            self.assertEqual(int(result.code), 0)
            self.assertTrue(seen, "应当至少调用了一次 make")
            self.assertEqual(seen[0][0], str(_TOOLS.gcc_root), f"工具链目录必须是子进程 PATH 的第一项: {seen[0]}")


if __name__ == "__main__":
    unittest.main()
