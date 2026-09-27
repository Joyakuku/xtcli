"""源文件收集与编译规则的回归测试。

覆盖两个**实测踩过**的缺陷:

1. `.cpp` 被静默丢弃 —— 既不编译也不告警。若其符号被引用则链接报
   undefined reference; 若不被引用 (全局构造、函数表、按名引用的中断向量)
   就会"构建成功"却少了一整块逻辑。
2. 大写 `.S` 启动文件构建失败 —— `rules.mk` 用大小写敏感的 `patsubst %.s`
   转换对象名, 匹配不上就把 mangle 后的**源文件名**塞进 OBJS, make 于是报
   ``No rule to make target 'Core__Startup__startup_xxx.S'`` (文件明明存在)。

这两类问题只做文本断言是抓不到的 —— 必须真的跑一次 arm-none-eabi 构建,
所以真编译用例在缺工具链时 skip。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support  # noqa: F401  (import 顺序与其它测试一致, 便于单独运行本文件)
from support import (
    HELPER_CPP,
    MAIN_C,
    MAIN_CPP,
    cli_output,
    in_dir,
    make_min_project,
    min_elf,
    toolchain_ready,
)


class TestSourceClassification(unittest.TestCase):
    """C / C++ / 汇编 的分类 (纯单元, 不需要工具链)。"""

    def _model(self, root: Path):
        from xtcli.model import ProjectModel

        (root / "Core" / "Src").mkdir(parents=True, exist_ok=True)
        for name in ("a.c", "b.cpp", "c.cc", "d.cxx", "e.C", "f.s", "g.S", "h_template.cpp"):
            (root / "Core" / "Src" / name).write_text("", encoding="utf-8")
        model = ProjectModel(backend="gcc_make", root=root, target=root.name, config="Debug")
        model.src_dirs = [root / "Core" / "Src"]
        return model

    def test_cpp_suffixes_are_collected_as_cpp(self):
        from xtcli.backends import gcc_make

        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(Path(tmp))
            names = {p.name for p in gcc_make.collect_cpp_sources(model)}
            self.assertEqual(names, {"b.cpp", "c.cc", "d.cxx", "e.C"})

    def test_collect_sources_does_not_return_cpp(self):
        from xtcli.backends import gcc_make

        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(Path(tmp))
            c_files, s_files = gcc_make.collect_sources(model)
            self.assertEqual({p.name for p in c_files}, {"a.c"})
            # `.S` 必须当作汇编收进来 (Windows 上 wildcard 大小写不敏感)
            self.assertEqual({p.name for p in s_files}, {"f.s", "g.S"})

    def test_template_cpp_is_excluded(self):
        from xtcli.backends import gcc_make

        with tempfile.TemporaryDirectory() as tmp:
            model = self._model(Path(tmp))
            names = {p.name for p in gcc_make.collect_cpp_sources(model)}
            self.assertNotIn("h_template.cpp", names)


# 用户在 retarget.c 里自己实现 newlib 桩 —— CubeMX 世界里的常见写法
RETARGET_C = (
    "void *_sbrk(int inc) { (void)inc; return (void *)0; }\n"
    "int _write(int f, char *p, int n) { (void)f; (void)p; return n; }\n"
)


class TestRuntimeStubDecision(unittest.TestCase):
    """补齐运行时桩必须按**符号**判定, 不能按文件名 (纯单元, 不需要工具链)。"""

    def _wanted(self, files: dict[str, str]) -> list[str]:
        from xtcli.backends import gcc_make
        from xtcli.model import ProjectModel

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / "Core" / "Src").mkdir(parents=True)
            for name, text in files.items():
                (root / "Core" / "Src" / name).write_text(text, encoding="utf-8")
            model = ProjectModel(backend="gcc_make", root=root, target="proj", config="Debug")
            model.src_dirs = [root / "Core" / "Src"]
            return gcc_make._wanted_stubs(model)

    def test_bare_project_wants_both_stubs(self):
        self.assertEqual(self._wanted({"main.c": MAIN_C}), ["syscalls.c", "sysmem.c"])

    def test_user_retarget_suppresses_both_stubs(self):
        """回归: 之前按文件名判定 → 注入 sysmem.c 与 retarget.c 重复定义 `_sbrk`。"""
        self.assertEqual(self._wanted({"main.c": MAIN_C, "retarget.c": RETARGET_C}), [])

    def test_partial_user_definition_suppresses_only_that_stub(self):
        only_sbrk = "void *_sbrk(int inc) { (void)inc; return (void *)0; }\n"
        self.assertEqual(self._wanted({"main.c": MAIN_C, "heap.c": only_sbrk}), ["syscalls.c"])


@unittest.skipUnless(toolchain_ready(), "缺少 arm-none-eabi 工具链或 make")
class TestBuildWithCppAndUppercaseAsm(unittest.TestCase):
    def _init_and_build(self, root: Path) -> tuple[int, str]:
        with in_dir(root):
            init_code, init_out = cli_output(["init", "-NoBuild"])
            self.assertEqual(init_code, 0, init_out)
            build_code, build_out = cli_output(["build"])
        return build_code, build_out

    def test_uppercase_S_startup_is_assembled(self):
        """大写 .S 启动文件必须能构建 (修复前: No rule to make target)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C}, startup_suffix=".S")
            code, out = self._init_and_build(root)
            self.assertEqual(code, 0, out)
            self.assertTrue(min_elf(root).is_file(), out)
            objs = {p.name for p in (root / "build").glob("*.o")}
            self.assertIn("Core__Startup__startup_stm32f103rctx.o", objs)

    def test_cpp_file_is_compiled_not_silently_dropped(self):
        """未被引用的 .cpp 也必须被编译出目标文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(
                Path(tmp), {"Core/Src/main.c": MAIN_C, "Core/Src/helper.cpp": HELPER_CPP}
            )
            code, out = self._init_and_build(root)
            self.assertEqual(code, 0, out)
            objs = {p.name for p in (root / "build").glob("*.o")}
            self.assertIn("Core__Src__helper.o", objs, out)

    def test_cpp_main_links_and_lands_in_firmware(self):
        """C++ 主程序 + C++ 符号必须真正参与链接 (libstdc++ 已按需链接)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(
                Path(tmp), {"Core/Src/main.cpp": MAIN_CPP, "Core/Src/helper.cpp": HELPER_CPP}
            )
            code, out = self._init_and_build(root)
            self.assertEqual(code, 0, out)
            elf = min_elf(root)
            self.assertTrue(elf.is_file(), out)
            self.assertIn(b"cpp_helper", elf.read_bytes())

    def test_user_retarget_project_still_builds(self):
        """回归: 用户自备 _sbrk/_write 时不得再报 `multiple definition of '_sbrk'`。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(
                Path(tmp), {"Core/Src/main.c": MAIN_C, "Core/Src/retarget.c": RETARGET_C}
            )
            code, out = self._init_and_build(root)
            self.assertEqual(code, 0, out)
            self.assertTrue(min_elf(root).is_file(), out)
            self.assertFalse((root / "Core" / "Src" / "syscalls.c").exists(), out)
            self.assertFalse((root / "Core" / "Src" / "sysmem.c").exists(), out)
            self.assertEqual(list((root / "xtcli" / "stubs").glob("*.c")), [], out)

    def test_stubs_are_written_outside_source_tree(self):
        """补齐的桩必须落在 xtcli/stubs/, 不进用户源码树。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["init", "-NoBuild"])
            self.assertEqual(code, 0, out)
            names = {p.name for p in (root / "xtcli" / "stubs").glob("*.c")}
            self.assertEqual(names, {"syscalls.c", "sysmem.c"}, out)
            self.assertFalse((root / "Core" / "Src" / "syscalls.c").exists(), out)
            self.assertFalse((root / "Core" / "Src" / "sysmem.c").exists(), out)

    def test_no_stubs_flag_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                code, out = cli_output(["init", "-NoBuild", "-NoStubs"])
            self.assertEqual(code, 0, out)
            self.assertEqual(list((root / "xtcli" / "stubs").glob("*.c")), [], out)

    def test_fresh_build_does_not_report_stale_config(self):
        """回归: 补齐过运行时桩的工程, build 曾每次都报"config.mk 陈旧"。

        原因: ``stubs`` 只在 init 里被填进 ``model.extra``, 而 build 是**独立进程**、
        重新 extract 的 model 里它是空的 —— 一旦把它算进指纹, 两边必然对不上。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            with in_dir(root):
                init_code, init_out = cli_output(["init", "-NoBuild"])
                self.assertEqual(init_code, 0, init_out)
                build_code, build_out = cli_output(["build"])
            self.assertEqual(build_code, 0, build_out)
            self.assertNotIn("陈旧或被改过", build_out, build_out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
