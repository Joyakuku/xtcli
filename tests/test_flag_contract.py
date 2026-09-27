"""全局开关契约: SPECS / CSTD 必须**成套**注入编译与链接。

事故背景 (实测): 旧 rules.mk 把 ``--specs=nano.specs`` 只写在 LDFLAGS。于是
链接的是 libc_nano (``struct _reent`` = 76 B), 编译却按标准 newlib 头 (512 B) ——
FreeRTOS 每个 TCB 白多背 436 B, 4 个任务 + 定时器队列把 4096 B 的静态堆顶爆,
``vTaskStartScheduler`` 死在 configASSERT, 板子完全没反应。
而编译、烧录、以及"黄金体积"比对**全都是绿的** (FreeRTOS 堆是 .bss 里的定长数组,
TCB 变大不改任何 section 一个字节)。

这层测试把"成套"变成机器可检的闸门:
  1. 模板结构: SPECS 到得了 C/C++/汇编/链接; CSTD 只进 C 编译;
  2. 解析期自检真的会让 make 失败 (跑一次真构建);
  3. ABI 哨兵真的会拦住"半套"参数 (完整复现历史模板, 跑一次真构建)。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import (
    MAIN_C,
    captured,
    cli_output,
    copy_project,
    has_project,
    in_dir,
    make_min_project,
    toolchain_ready,
)

from xtcli import discovery
from xtcli.backends import gcc_burn, gcc_common, gcc_make

REPO = Path(__file__).resolve().parents[1]
RULES_TEMPLATE = REPO / "src" / "xtcli" / "assets" / "rules.mk"

_TOOLS = discovery.discover()
_REQUIRE_REAL_BUILD = unittest.skipUnless(
    toolchain_ready(), "工具链不完整 (需要 arm-none-eabi-gcc / make)"
)


def _variable_body(text: str, name: str) -> str:
    """取 makefile 里 ``NAME = ...`` 的值 (含 ``\\`` 续行)。"""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{name} ") and "=" in line:
            parts = [line.split("=", 1)[1]]
            while parts[-1].rstrip().endswith("\\"):
                index += 1
                parts.append(lines[index])
            return "\n".join(parts)
    raise AssertionError(f"rules.mk 里找不到变量 {name}")


class TestTemplateFlagContract(unittest.TestCase):
    """模板层的结构不变量 (不需要工具链)。"""

    def test_specs_reaches_every_compiler_and_the_linker(self):
        text = RULES_TEMPLATE.read_text(encoding="utf-8")
        for name in ("CFLAGS", "ASFLAGS", "LDFLAGS"):
            with self.subTest(variable=name):
                self.assertIn("$(SPECS)", _variable_body(text, name))
        # CXXFLAGS 从 CFLAGS 继承(只滤掉 C 专属的 CSTD) => SPECS 自动成套。
        # 一旦有人把它改成手写清单而漏掉 SPECS, 这条会失败。
        cxx = _variable_body(text, "CXXFLAGS")
        self.assertIn("$(filter-out $(CSTD),$(CFLAGS))", cxx)

    def test_cstd_is_c_only(self):
        text = RULES_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("$(CSTD)", _variable_body(text, "CFLAGS"))
        for name in ("ASFLAGS", "LDFLAGS"):
            with self.subTest(variable=name):
                self.assertNotIn("$(CSTD)", _variable_body(text, name))

    def test_no_hardcoded_specs_left_in_the_template(self):
        """``--specs`` 只能以 ``$(SPECS)`` 形式出现 —— 字面量就是"半套注入"的复发形态。"""
        offenders = [
            line.strip()
            for line in RULES_TEMPLATE.read_text(encoding="utf-8").splitlines()
            if "--specs" in line
            and "$(SPECS)" not in line
            and not line.lstrip().startswith(("#", "SPECS"))
        ]
        self.assertEqual(offenders, [], f"模板里有硬编码的 --specs: {offenders}")

    def test_build_depends_on_the_self_check(self):
        text = RULES_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("$(OUT_DIR)/$(TARGET).elf: check-flags", text)
        self.assertIn("XT_FLAG_SELFCHECK", text)
        # 自检必须覆盖链接侧 (只查 CFLAGS 抓不到"只在链接出现"的历史形态)
        self.assertIn("XT_REQUIRE_IN,LDFLAGS", text)

    def test_default_goal_is_pinned(self):
        """裸 `make` 必须产出 elf/hex/bin。

        实测坑: 插在 `all:` 之前的普通目标会变成默认目标 —— 曾因此让裸 make 只编
        一个 .o 就"成功"返回, 链接从未发生 (构建报成功但产物不存在)。
        """
        text = RULES_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn(".DEFAULT_GOAL := all", text)
        self.assertLess(
            text.index(".DEFAULT_GOAL := all"),
            text.index("\nall:"),
            "默认目标声明必须排在 all 规则之前才有效",
        )

    def test_parameter_files_are_prerequisites_of_every_object(self):
        """参数文件 (config.mk/rules.mk) 变新 => 所有对象重编。"""
        text = RULES_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("$(OBJS): $(XT_PARAM_FILES)", text)
        self.assertIn("XT_PARAM_FILES := $(XT_DIR)/config.mk $(XT_DIR)/rules.mk", text)


class TestGeneratedSkeleton(unittest.TestCase):
    """生成的骨架必须把开关写进 config.mk (进指纹) 且落到编译数据库。"""

    def test_config_mk_and_compile_commands_carry_the_switches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
            self.assertEqual(code, 0, out)

            config = (root / "xtcli" / "config.mk").read_text(encoding="utf-8")
            self.assertIn("RUNTIME_LIB := nano", config)
            self.assertIn("SPECS       := --specs=nano.specs", config)
            self.assertIn("CSTD        := -std=gnu11", config)

            # ABI 探针必须与 rules.mk 一起落盘, 否则 abi-check 无处可编
            self.assertTrue((root / "xtcli" / "abi-probe.c").is_file())

            # 编译数据库要与真实构建同一套语义 (C 标准显式给出)
            cdb = (root / "compile_commands.json").read_text(encoding="utf-8")
            self.assertIn("-std=gnu11", cdb)


@_REQUIRE_REAL_BUILD
class TestSelfCheckFailsWhenFlagIsMissing(unittest.TestCase):
    """自检必须在构建期以明确消息失败, 而不是静默编出 ABI 不一致的固件。"""

    def test_make_aborts_when_specs_is_missing_from_cflags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                self.assertEqual(code, 0, out)
                rules = root / "xtcli" / "rules.mk"
                text = rules.read_text(encoding="utf-8")
                self.assertIn("$(CSTD) $(SPECS)", text, "模板结构变了, 这条测试需要同步")
                rules.write_text(text.replace("$(CSTD) $(SPECS)", "$(CSTD)"), encoding="utf-8")
                code, out = cli_output(["build", "-Target", "stm32"])
        self.assertNotEqual(code, 0, out)
        self.assertIn("编译/链接不对称", out)


@_REQUIRE_REAL_BUILD
@unittest.skipUnless(has_project("freertos_hal_template"), "参考工程不存在")
class TestAbiSentinelCatchesHalfInjectedSpecs(unittest.TestCase):
    """完整复现历史事故: 链接带 nano、编译不带 —— ABI 哨兵必须拦住。

    这是"黄金体积"抓不到的那一类: 参数不一致时 .text/.bss 几乎不变, 而运行期
    内存需求差了 1.8 KB (足以把静态堆顶爆)。
    """

    def test_build_fails_with_a_clear_diagnosis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_project("freertos_hal_template", Path(tmp))
            with in_dir(root):
                code, out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                self.assertEqual(code, 0, out)
                # 复现旧模板: SPECS 置空 (自检因此视为"没有这个开关"),
                # 但把 nano 以字面量塞回链接行 —— 正是当年那份 rules.mk 的形态。
                cfg = root / "xtcli" / "config.mk"
                cfg.write_text(
                    cfg.read_text(encoding="utf-8").replace("SPECS       := --specs=nano.specs", "SPECS       :="),
                    encoding="utf-8",
                )
                rules = root / "xtcli" / "rules.mk"
                rules.write_text(
                    rules.read_text(encoding="utf-8").replace(
                        "-static \\", "-static --specs=nano.specs \\"
                    ),
                    encoding="utf-8",
                )
                code, out = cli_output(["build", "-Target", "stm32", "-Clean"])
                self.assertEqual(code, 6, out)
                self.assertIn("ABI 哨兵失败", out)
                self.assertIn("_impure_data", out)
                # 逃生门: -NoAbiCheck 必须能跳过 (排障用), 且不再报错
                skipped_code, skipped_out = cli_output(["build", "-Target", "stm32", "-NoAbiCheck"])
        self.assertEqual(skipped_code, 0, skipped_out)
        self.assertNotIn("ABI 哨兵失败", skipped_out)


class TestLibraryObjectSizes(unittest.TestCase):
    """map 解析: 被 --gc-sections 丢弃的段 (address 0) 必须跳过。"""

    def test_discarded_sections_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_path = Path(tmp) / "x.map"
            map_path.write_text(
                "Discarded input sections\n"
                " .data._impure_data\n"
                "                0x00000000      0x100 /lib/libc_nano.a(libc_a-impure.o)\n"
                " .bss.ucHeap\n"
                "                0x2000007c     0x1000 build/FreeRTOS__Src__heap_4.o\n"
                " .data._impure_data\n"
                "                0x20000014       0x4c /lib/libc_nano.a(libc_a-impure.o)\n",
                encoding="utf-8",
            )
            sizes = gcc_common.library_object_sizes(map_path)
        self.assertEqual(sizes["_impure_data"][0], 0x4C, "必须取保留段, 不是被丢弃的那份")
        self.assertEqual(sizes["ucHeap"][0], 0x1000)
        self.assertIn("impure.o", sizes["_impure_data"][1])

    def test_missing_map_is_not_an_error(self):
        self.assertEqual(gcc_common.library_object_sizes(Path("does-not-exist.map")), {})


class TestEffectiveSwitches(unittest.TestCase):
    """日志/doctor 打印的开关必须以 config.mk 为准 (用户可能手改过)。"""

    def test_config_mk_wins_over_model_defaults(self):
        from xtcli.makefiles import config_mk

        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            model = gcc_make.build_model(root, {"config": "Debug"})
            # 还没 init -> 退回按模型推算
            self.assertIn("nano", config_mk.effective_abi_switches(model))
            (root / "xtcli").mkdir()
            (root / "xtcli" / "config.mk").write_text(
                "RUNTIME_LIB := std\nSPECS       :=\nCSTD        := -std=gnu11\n",
                encoding="utf-8",
            )
            summary = config_mk.effective_abi_switches(model)
        self.assertIn("运行时库 std", summary)
        self.assertIn("不用 specs", summary)


class TestStaleObjects(unittest.TestCase):
    """参数文件变新 ⇒ 对象必须重编; 否则固件里会混着两套参数(实测虚假通过)。"""

    def _model(self, tmp: str):
        root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
        model = gcc_make.build_model(root, {"config": "Debug"})
        (root / "xtcli").mkdir(parents=True, exist_ok=True)
        (root / "xtcli" / "config.mk").write_text("SPECS := --specs=nano.specs\n", encoding="utf-8")
        (root / "xtcli" / "rules.mk").write_text("$(SPECS)\nabi-check\n", encoding="utf-8")
        (root / "build").mkdir(parents=True, exist_ok=True)
        (root / "build" / "Core__Src__main.o").write_bytes(b"\x00")
        return root, model

    def test_objects_older_than_the_param_files_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            self.assertEqual(gcc_common.stale_objects(model), [])
            # 把对象的时间戳推到过去 = "它是用旧参数编的" (不用造未来时间, 避免时钟偏移分支)
            obj = root / "build" / "Core__Src__main.o"
            past = time.time() - 60
            os.utime(obj, (past, past))
            stale = [p.name for p in gcc_common.stale_objects(model)]
        self.assertEqual(stale, ["Core__Src__main.o"])

    def test_non_firmware_objects_are_ignored(self):
        """build/ 里的非固件对象 (例如 ABI 探针 abi-probe.o) 不能被算成"陈旧对象"。

        实测坑: 探针对象比刚重新生成的 config.mk 旧 -> 检查误判成构建失败, 而它每次
        都由 abi-check 重新生成, 与固件无关。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            probe = root / "build" / "abi-probe.o"
            probe.write_bytes(b"\x00")
            past = time.time() - 60
            os.utime(probe, (past, past))
            self.assertEqual(gcc_common.stale_objects(model), [])

    def test_no_build_dir_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            (root / "build" / "Core__Src__main.o").unlink()
            (root / "build").rmdir()
            self.assertEqual(gcc_common.stale_objects(model), [])

    @_REQUIRE_REAL_BUILD
    def test_touching_config_mk_forces_a_full_recompile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["init", "-Target", "stm32", "-NoBuild"])
                self.assertEqual(code, 0, out)
                code, out = cli_output(["build", "-Target", "stm32"])
                self.assertEqual(code, 0, out)
                obj = root / "build" / "Core__Src__main.o"
                built_at = obj.stat().st_mtime
                # 把对象时间戳推回过去 = 模拟"它比当前参数旧" (等价于参数文件被重新生成)
                past = built_at - 60
                os.utime(obj, (past, past))
                code, out = cli_output(["build", "-Target", "stm32"])
                self.assertEqual(code, 0, out)
                rebuilt_at = obj.stat().st_mtime
        self.assertGreater(rebuilt_at, built_at, "参数文件变新后对象必须重编 (否则固件混两套参数)")

    def test_future_timestamps_are_not_reported_as_stale(self):
        """时钟偏移/解压恢复会让参数文件时间戳在未来 —— 那时 make 每次都全量重编,
        产物不可能陈旧, 检查必须明说跳过而不是误判成失败。"""
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            future = time.time() + 3600
            os.utime(root / "xtcli" / "config.mk", (future, future))
            with captured() as buffer:
                stale = gcc_common.stale_objects(model)
            text = buffer.getvalue()
        self.assertEqual(stale, [])
        self.assertIn("时间戳在未来", text)


class TestRenamedProjectIsNotSilent(unittest.TestCase):
    """工程目录改名后产物名对不上: 哨兵不能静默跳过 (要说明原因)。"""

    def test_missing_artifact_with_foreign_elf_is_reported(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            model = gcc_make.build_model(root, {"config": "Debug"})
            (root / "xtcli").mkdir(parents=True, exist_ok=True)
            (root / "xtcli" / "rules.mk").write_text("$(SPECS)\nabi-check\n", encoding="utf-8")
            out_dir = root / "Debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "旧名字.elf").write_bytes(b"\x00")
            with captured() as buffer:
                result = gcc_common.check_abi_consistency(
                    model,
                    SimpleNamespace(make="make", gcc_root=None),
                    make_args=[],
                    path_prefix=[],
                )
            text = buffer.getvalue()
        self.assertIsNone(result)  # 不判失败, 但必须说清楚
        self.assertIn("没找到本工程名的产物", text)
        self.assertIn("xtcli-init", text)


class TestStalenessAndProvenance(unittest.TestCase):
    """burn 前的陈旧判定与"外来产物"提示 (都不碰硬件)。"""

    def _model(self, tmp: str):
        root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
        model = gcc_make.build_model(root, {"config": "Debug"})
        return root, model

    def test_artifact_is_stale_when_sources_are_newer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            artifact = root / "Debug" / f"{root.name}.elf"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"\x00")
            self.assertFalse(gcc_common.artifact_is_stale(model, artifact))
            self.assertFalse(gcc_common.artifact_is_stale(model, root / "不存在.elf"))
            future = time.time() + 30
            os.utime(root / "Core" / "Src" / "main.c", (future, future))
            self.assertTrue(gcc_common.artifact_is_stale(model, artifact))

    def test_foreign_artifacts_are_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            out_dir = root / "Debug"
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{root.name}.elf").write_bytes(b"\x00")
            (out_dir / "oldname.elf").write_bytes(b"\x00")
            (out_dir / "oldname.bin").write_bytes(b"\x00")
            (out_dir / "notes.txt").write_text("x", encoding="utf-8")
            names = [path.name for path in gcc_common.foreign_artifacts(model)]
        self.assertEqual(names, ["oldname.bin", "oldname.elf"])

    def test_stale_burn_builds_first_instead_of_flashing_old_firmware(self):
        from types import SimpleNamespace
        from unittest import mock

        from xtcli.errors import Result
        from xtcli.model import Context

        with tempfile.TemporaryDirectory() as tmp:
            root, model = self._model(tmp)
            artifact = root / "Debug" / f"{root.name}.elf"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_bytes(b"\x00")
            ctx = Context(
                root=root,
                opt={},
                tools=_TOOLS or SimpleNamespace(),
                model=model,
                backend=None,
            )
            calls: list[str] = []

            def fake_build(_ctx):
                calls.append("build")
                return Result(code=6, message="构建失败 (测试桩)")

            with mock.patch.object(gcc_burn, "artifact_is_stale", return_value=True), \
                 mock.patch("xtcli.backends.gcc_make.action_build", side_effect=fake_build):
                result = gcc_burn.action_burn(ctx)

        self.assertEqual(calls, ["build"], "固件陈旧时必须先构建, 不能烧旧固件")
        self.assertEqual(int(result.code), 6)


if __name__ == "__main__":
    unittest.main()
