"""ESP-IDF 后端测试。

重点覆盖本次实测踩到的坑:
* ``ESP_IDF_VERSION`` 缺失会让 IDF v6.x 的组件管理器在深处抛
  ``TypeError: expected string or bytes-like object, got 'NoneType'``;
* 用户级 VSCode 配置可能是过期路径, 必须校验存在性;
* VSCode 的 settings.json 是 JSONC（有注释/尾逗号）, 必须容忍。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import PROJECTS, cli_output, in_dir

from xtcli.backends import espidf

ESP_STUDY = Path(r"E:\Code_workspace\esp_study")
_smoke_env, _smoke_warnings = (None, [])
if ESP_STUDY.is_dir():
    # 环境解析失败就跳过依赖它的测试, 不影响其它用例
    with contextlib.suppress(Exception):
        _smoke_env, _smoke_warnings = espidf.resolve(ESP_STUDY)
_HAS_IDF = _smoke_env is not None


def _idf_project() -> Path | None:
    """找一个真实 ESP-IDF 工程（优先最简单的）。"""
    if not ESP_STUDY.is_dir():
        return None
    for name in ("0_helloworld", "5_lcd", "1_LED"):
        candidate = ESP_STUDY / name
        if (candidate / "main" / "CMakeLists.txt").is_file():
            return candidate
    for candidate in sorted(ESP_STUDY.iterdir()):
        if candidate.is_dir() and (candidate / "main" / "CMakeLists.txt").is_file():
            return candidate
    return None


class TestJsoncParsing(unittest.TestCase):
    def test_strips_comments_and_trailing_commas(self):
        text = """{
  // 行注释
  "a": "http://example.com/x",   /* 块注释 */
  "b": [1, 2,],
}"""
        import json

        data = json.loads(espidf._strip_jsonc(text))
        self.assertEqual(data["a"], "http://example.com/x")
        self.assertEqual(data["b"], [1, 2])

    def test_missing_or_broken_settings_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(espidf._load_jsonc(Path(tmp) / "nope.json"), {})


class TestEimVariableParsing(unittest.TestCase):
    def _script(self) -> Path | None:
        for candidate in (Path(r"C:\Espressif\tools"), Path(r"E:\env\Espressif\tools")):
            if candidate.is_dir():
                found = sorted(candidate.glob("Microsoft.v*.PowerShell_profile.ps1"))
                if found:
                    return found[0]
        return None

    def test_reads_hashtable_vars_and_skips_path(self):
        """解析 EIM 激活脚本的 ``$env_var_pairs`` 哈希表。

        注意 ``ESP_IDF_VERSION`` **不在**这个哈希表里 —— EIM 用单独一行
        ``$env:ESP_IDF_VERSION = "$IdfVersion"``（插值）赋值, 所以由 xtcli 自己从
        ``<IDF_PATH>/tools/cmake/version.cmake`` 推导（与 EIM 同一算法）。这也正是
        它必须显式设置的原因: IDF 组件管理器会直接 coerce 它。
        """
        script = self._script()
        if script is None:
            self.skipTest("没有 EIM 激活脚本")
        variables = espidf._eim_activation_vars(script)
        self.assertIn("IDF_PATH", variables)
        self.assertIn("IDF_TOOLS_PATH", variables)
        self.assertNotIn("PATH", variables, "PATH 由 xtcli 自己构造")
        self.assertNotIn("SYSTEM_PATH", variables, "那只是系统 PATH 快照")

    def test_missing_script_returns_empty(self):
        self.assertEqual(espidf._eim_activation_vars(None), {})
        self.assertEqual(espidf._eim_activation_vars(Path(r"C:\does\not\exist.ps1")), {})


@unittest.skipUnless(_HAS_IDF, "未找到可用的 ESP-IDF 安装")
class TestEnvResolution(unittest.TestCase):
    def setUp(self):
        self.env_obj = _smoke_env

    def test_paths_exist(self):
        self.assertTrue(self.env_obj.idf_path.is_dir())
        self.assertTrue(self.env_obj.idf_py.is_file())
        self.assertTrue(self.env_obj.python.is_file())
        self.assertTrue(self.env_obj.tools_path.is_dir())

    def test_required_variables_present(self):
        """坑: ESP_IDF_VERSION 缺失 → IDF 组件管理器深处 TypeError。"""
        variables = self.env_obj.variables
        self.assertIn("ESP_IDF_VERSION", variables)
        self.assertTrue(variables["ESP_IDF_VERSION"], "不能是空串")
        self.assertRegex(variables["ESP_IDF_VERSION"], r"^\d+\.\d+")
        self.assertIn("IDF_PATH", variables)
        self.assertIn("IDF_TOOLS_PATH", variables)
        self.assertIn("IDF_PYTHON_ENV_PATH", variables)
        self.assertIn("IDF_COMPONENT_LOCAL_STORAGE_URL", variables)

    def test_path_entries_have_toolchain(self):
        entries = [Path(entry) for entry in self.env_obj.path_entries]
        self.assertTrue(entries, "PATH 条目不能为空")
        self.assertTrue(any((entry / "ninja.exe").is_file() for entry in entries), "缺 ninja")
        self.assertTrue(any((entry / "cmake.exe").is_file() for entry in entries), "缺 cmake")
        target = self.env_obj.variables.get("IDF_TARGET", "esp32s3")
        compiler = espidf._compiler_name(target)
        self.assertTrue(
            any((entry / compiler).is_file() for entry in entries),
            f"PATH 里没有 {compiler}",
        )

    def test_version_and_helpers(self):
        self.assertTrue(self.env_obj.version)
        self.assertEqual(espidf._major_minor("6.0.1"), "6.0")
        self.assertEqual(espidf._major_minor("6.0"), "6.0")
        self.assertEqual(espidf._compiler_name("esp32s3"), "xtensa-esp32s3-elf-gcc.exe")
        self.assertEqual(espidf._compiler_name("esp32c6"), "riscv32-esp-elf-gcc.exe")


class TestDetect(unittest.TestCase):
    def test_synthetic_idf_project_scores(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            (root / "main").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text(
                'cmake_minimum_required(VERSION 3.16)\ninclude($ENV{IDF_PATH}/tools/cmake/project.cmake)\n'
                "project(demo)\n",
                encoding="utf-8",
            )
            (root / "main" / "CMakeLists.txt").write_text(
                "idf_component_register(SRCS \"main.c\")\n", encoding="utf-8"
            )
            self.assertGreater(espidf.detect_score(root), 0)

    def test_plain_cmake_is_not_idf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"
            root.mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("project(plain)\n", encoding="utf-8")
            self.assertEqual(espidf.detect_score(root), 0)

    def test_root_cmake_without_idf_entry_is_not_idf(self):
        """根 CMakeLists 里只有 idf_component_register、没有 IDF 构建入口 -> 不是 IDF 工程。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "halfway"
            root.mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("idf_component_register()\n", encoding="utf-8")
            self.assertEqual(espidf.detect_score(root), 0)

    def test_idf_project_without_main_cmake_is_detected(self):
        """回归 H5: IDF 官方的 cmakev2 形态没有 main/CMakeLists.txt, 也必须能识别。

        依据: IDF 的 ``tools/cmake/project.cmake`` 只在 ``main`` 目录存在时才把 main
        作为组件加入, 组件也可以来自 ``components/`` 与 ``EXTRA_COMPONENT_DIRS`` ——
        官方示例 ``examples/build_system/cmakev2/*`` 就是这样, 旧判据把它误拒了。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cmakev2"
            (root / "components" / "app").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "include($ENV{IDF_PATH}/tools/cmakev2/idf.cmake)\n"
                "idf_project_init(my_app)\n"
                "idf_build_executable(main)\n",
                encoding="utf-8",
            )
            self.assertGreater(espidf.detect_score(root), 0)

    def test_idf_as_lib_style_is_detected(self):
        """官方 ``idf_as_lib`` 形态: main.c 在根, 用 include(idf.cmake) + project()。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "aslib"
            root.mkdir(parents=True)
            (root / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(idf_as_lib C CXX ASM)\n"
                'include($ENV{IDF_PATH}/tools/cmake/idf.cmake)\n',
                encoding="utf-8",
            )
            self.assertGreater(espidf.detect_score(root), 0)

    def test_idf_path_in_a_comment_is_not_a_marker(self):
        """回归 H6: **注释里**提到 IDF_PATH 不算 IDF 工程。

        实测危害: 普通 CMake 工程 + 一行提到 IDF_PATH 的注释 + main/CMakeLists.txt
        会被判成 IDF (detect=80), 于是 init 会往该目录写 xtcli/idf-env.json 并试跑
        ``idf.py build`` —— 对不是 IDF 的工程动手。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"
            (root / "main").mkdir(parents=True)
            (root / "CMakeLists.txt").write_text(
                "cmake_minimum_required(VERSION 3.16)\n"
                "# 本工程不使用 ESP-IDF; 只是参考 $ENV{IDF_PATH} 里的组织方式\n"
                "project(plain C)\n",
                encoding="utf-8",
            )
            (root / "main" / "CMakeLists.txt").write_text(
                "add_library(app main.c)\n", encoding="utf-8"
            )
            self.assertEqual(espidf.detect_score(root), 0)


class TestRefusal(unittest.TestCase):
    def test_plain_cmake_project_is_refused(self):
        """普通 CMake 工程不得被当成 ESP-IDF 工程。

        detect 门槛现在会先把它挡掉 -> UNSUPPORTED(5); 关键是**不动手**
        (曾实测: 根 CMakeLists 里一行注释提到 IDF_PATH 就会被误判成 IDF 工程,
        init 会写 xtcli/idf-env.json 并试跑 idf.py build)。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"
            root.mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("project(plain)\n", encoding="utf-8")
            with in_dir(root):
                code, output = cli_output(["init", "-Target", "espidf"])
            self.assertEqual(code, 5)  # UNSUPPORTED
            self.assertIn("没有 backend 能处理该目录", output)

    def test_no_writes_on_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plain"
            root.mkdir(parents=True)
            (root / "CMakeLists.txt").write_text("project(plain)\n", encoding="utf-8")
            before = {str(p.relative_to(root)) for p in root.rglob("*")}
            with in_dir(root):
                cli_output(["init", "-Target", "espidf"])
            after = {str(p.relative_to(root)) for p in root.rglob("*")}
            self.assertEqual(before, after)


@unittest.skipUnless(_HAS_IDF, "未找到可用的 ESP-IDF 安装")
class TestInitTakesOverSafely(unittest.TestCase):
    """回归: espidf 的 init 曾用 ``env.atomic_write_text`` **绕过** managed.Owned。

    后果是工程里已有的 ``xtcli/idf-env.json``(比如用户手工记的、或别的工具放的)
    会被无声覆盖, 也不登记进 ``xtcli/.owned.json`` —— GCC 后端早已走受管通道,
    ESP-IDF 后端却漏了。这里断言"先备份成 *.xtcli-bak 再写 + 登记清单"。
    """

    def test_foreign_idf_env_is_backed_up_and_owned(self):
        from xtcli import managed

        project = _idf_project()
        if project is None:
            self.skipTest("没有真实 ESP-IDF 工程")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / project.name
            shutil.copytree(project, target, ignore=shutil.ignore_patterns("build", "xtcli"))
            stray = target / "xtcli" / "idf-env.json"
            stray.parent.mkdir(parents=True, exist_ok=True)
            original = '{"mine": true}\n'
            stray.write_text(original, encoding="utf-8")

            with in_dir(target):
                code, output = cli_output(["init", "-Target", "espidf", "-NoBuild"])
            self.assertEqual(code, 0, output)

            backup = stray.with_name(stray.name + managed.BAK_SUFFIX)
            self.assertTrue(backup.is_file(), "原有文件必须先备份再覆盖")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            self.assertIn("xtcli/idf-env.json", managed.load(target), "必须登记进受管清单")
            self.assertIn("schema_version", stray.read_text(encoding="utf-8"))


@unittest.skipUnless(_HAS_IDF, "未找到可用的 ESP-IDF 安装")
class TestCleanAndMakeTargetAreHonored(unittest.TestCase):
    """回归: ESP32 侧的 -Clean 与 -MakeTarget 曾被**静默忽略**。

    用户以为清理过 / 以为换了目标, 实际什么都没发生。这里用 mock 断言
    ``idf.py`` 的实际 argv 构造（不需要真 IDF 环境）。
    """

    @staticmethod
    def _ctx(**opt):
        from types import SimpleNamespace

        from xtcli.model import Context, ProjectModel

        model = ProjectModel(
            backend="espidf",
            root=Path("."),
            target="app",
            out_dir="build",
            build_dir="build",
            source="espidf",
        )
        return Context(
            root=Path("."),
            opt={"clean": False, "make_target": "", **opt},
            tools=SimpleNamespace(),
            model=model,
            backend=None,
        )

    def _run(self, **opt) -> list[list[str]]:
        from types import SimpleNamespace

        calls: list[list[str]] = []

        def fake_run(_ctx, args, **_kw):
            calls.append(list(args))
            return SimpleNamespace(exit_code=0, timed_out=False, text="")

        with mock.patch.object(espidf, "_run_idf", side_effect=fake_run), \
             mock.patch.object(espidf, "_check_stale_env", lambda _model: None), \
             mock.patch.object(espidf, "_looks_like_toolchain_crash", lambda _text: False):
            espidf.action_build(self._ctx(**opt))
        return calls

    def test_clean_maps_to_fullclean_then_build(self):
        self.assertEqual(self._run(clean=True), [["fullclean"], ["build"]])

    def test_make_target_maps_to_idf_subcommand(self):
        self.assertEqual(self._run(make_target="size"), [["size"]])

    def test_default_is_build_only(self):
        self.assertEqual(self._run(), [["build"]])


class TestDoctorOnRealProject(unittest.TestCase):
    def test_doctor_reports_idf(self):
        project = _idf_project()
        if project is None:
            self.skipTest("没有真实 ESP-IDF 工程")
        with in_dir(project):
            code, output = cli_output(["doctor"])
        self.assertEqual(code, 0, output)
        self.assertIn("ESP-IDF", output)
        self.assertIn("IDF 版本", output)
        self.assertIn("可用串口", output)


@unittest.skipUnless(os.environ.get("XTCLI_IDF_E2E") == "1", "需要 XTCLI_IDF_E2E=1 (会真的构建, 较慢)")
class TestEndToEndBuild(unittest.TestCase):
    """端到端: 复制真实工程到临时目录后 init + build。绝不改动原工程。"""

    def test_init_and_build(self):
        project = _idf_project()
        if project is None:
            self.skipTest("没有真实 ESP-IDF 工程")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / project.name
            shutil.copytree(project, target, ignore=shutil.ignore_patterns("build"))
            with in_dir(target):
                code, output = cli_output(["init", "-Target", "espidf"])
                self.assertEqual(code, 0, output)
                self.assertTrue((target / "xtcli" / "idf-env.json").is_file())
                build_code, build_output = cli_output(["build", "-Target", "espidf"])
            self.assertEqual(build_code, 0, build_output)
            binary = target / "build" / f"{project.name}.bin"
            self.assertTrue(binary.is_file(), "构建后应有 .bin")
            self.assertGreater(binary.stat().st_size, 1024)
            self.assertTrue((target / "build" / "flasher_args.json").is_file())


@unittest.skipUnless(PROJECTS.is_dir(), "工作区不存在")
class TestNoRegressionOnStm32(unittest.TestCase):
    """ESP-IDF 后端不得影响既有 STM32 路径: 后端选择必须仍然正确。"""

    def test_stm32_project_still_selects_gcc_make(self):
        from xtcli import model as model_mod

        model_mod.load_backends()
        target = PROJECTS / "demo"
        if not (target / ".cproject").is_file():
            self.skipTest("demo 工程不存在")
        backend = model_mod.resolve("auto", target)
        self.assertIsNotNone(backend)
        self.assertEqual(backend.name, "gcc_make")


if __name__ == "__main__":
    unittest.main()
