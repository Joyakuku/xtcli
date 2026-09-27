"""回归: 工程根定位（find_root）与 ``.ioc`` 限深查找（find_ioc）。

两个缺陷的共同形态都是"静默给错结果", 所以断言尽量落在**调用方能看到的后果**上:

* ``find_root``: 标记在 6 层之外时曾把起点目录当工程根 —— 用户看到的是
  "没有 backend 能处理该目录 + 目录: <当前目录>", 无法判断是"标记太远"
  还是"目录确实不是工程"。现在前者返回 ``None``（CLI 报"当前目录无效"、退出 4）,
  并给出指向真正工程根的诊断说明; 后者保持旧的退出码 5 契约（见 errors.py）。
* ``find_ioc``: 只看工程根 —— ``.ioc`` 放在 ``CubeMX/`` 子目录时被判成
  "有源码但缺芯片信息"而误拒。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import cli_output, in_dir

from xtcli import project
from xtcli.backends import gcc_make, ioc
from xtcli.errors import Exit

# 距起点 6 层（= _MAX_UP, 恰好超出向上查找上限）的目录链
_CHAIN = ("b", "c", "d", "e", "f", "g")

_IOC_TEXT = (
    "ProjectManager.DeviceId=STM32F103RCTx\n"
    "ProjectManager.ProjectName=fixture\n"
    "ProjectManager.TargetToolchain=STM32CubeIDE\n"
)


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _chain(root: Path, names: tuple[str, ...]) -> Path:
    path = root
    for name in names:
        path = path / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ancestor(directory: Path, level: int) -> Path:
    path = directory
    for _ in range(level):
        parent = path.parent
        if parent == path:
            return path
        path = parent
    return path


class TestFindRootUpwardLimit(unittest.TestCase):
    """缺陷 1: 层数上限到了但没有命中时的行为。"""

    def test_marker_beyond_limit_returns_none_and_points_at_real_root(self):
        """标记在 6 层之上（上限之外）-> 返回 None, 且说明要指出真正的工程根。

        实测缺陷: 旧实现返回起点目录 ``g`` 自己, 于是 CLI 报的"目录: 当前目录"
        完全是误导 —— 工程根其实是 ``a``。
        """
        with tempfile.TemporaryDirectory() as tmp:
            real_root = (Path(tmp) / "a").resolve()
            deep = _chain(real_root, _CHAIN)
            _write(real_root / "Makefile", "all:\n\t@echo hi\n")

            self.assertIsNone(project.find_root(deep), "上限之外命中不得返回起点目录")

            note = project.last_root_search_note()
            self.assertIsNotNone(note, "这一种情况必须给诊断说明")
            self.assertIn("超出查找上限", note)
            self.assertIn(str(real_root), note, "说明里要写出真正的工程根在哪")
            # 标记是**找到了**、只是太远; 说成"未找到"会把用户引向错误结论
            self.assertNotIn("未找到", note)

    def test_marker_within_limit_still_returned(self):
        """上限内的命中行为不得改变: 第 1 层与第 5 层都要命中, 且不给说明。"""
        for levels in (1, project._MAX_UP - 1):
            with self.subTest(levels=levels), tempfile.TemporaryDirectory() as tmp:
                real_root = (Path(tmp) / "a").resolve()
                start = _chain(real_root, _CHAIN[:levels])
                _write(real_root / "Makefile", "all:\n")

                self.assertEqual(project.find_root(start), real_root)
                self.assertIsNone(project.last_root_search_note())

    def test_source_file_path_is_resolved_to_its_directory(self):
        """传文件路径（旧行为）仍然取其所在目录再向上找。"""
        with tempfile.TemporaryDirectory() as tmp:
            real_root = (Path(tmp) / "a").resolve()
            deep = _chain(real_root, _CHAIN[:2])
            source = _write(deep / "main.c", "int main(void){return 0;}\n")
            _write(real_root / "CMakeLists.txt", "project(x)\n")

            self.assertEqual(project.find_root(source), real_root)

    def test_no_marker_anywhere_keeps_unsupported_contract(self):
        """整条祖先链都没有标记 -> 仍返回起点目录（退出码 5 的既有契约）。

        与"标记太远"刻意区分: 那一种现在返回 None。这条差异是既定对外行为,
        见 ``tests/test_refusal.py`` 与 ``errors.py`` 对 5 的解释。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = (Path(tmp) / "not-a-project").resolve()
            root.mkdir(parents=True)
            _write(root / "readme.txt", "not a project\n")
            if any(project._has_marker(_ancestor(root, level)) for level in range(project._MAX_UP + 1)):
                self.skipTest("临时目录落在某个真实工程内部, 本用例不成立")

            self.assertEqual(project.find_root(root), root)
            note = project.last_root_search_note()
            self.assertIsNotNone(note)
            self.assertIn("未找到", note)
            self.assertIn("已停止查找", note)

    def test_note_is_reset_on_every_call(self):
        """上一次"没找到"的说明不得粘到下一次成功定位上。"""
        with tempfile.TemporaryDirectory() as tmp:
            real_root = (Path(tmp) / "a").resolve()
            deep = _chain(real_root, _CHAIN)
            _write(real_root / "Makefile", "all:\n")

            self.assertIsNone(project.find_root(deep))
            self.assertIsNotNone(project.last_root_search_note())

            self.assertEqual(project.find_root(real_root), real_root)
            self.assertIsNone(project.last_root_search_note())

    def test_cli_refuses_invalid_cwd_instead_of_using_it_as_root(self):
        """CLI 层后果: 退出 4 + "当前目录无效", 而不是把当前目录当工程根。"""
        with tempfile.TemporaryDirectory() as tmp:
            real_root = (Path(tmp) / "a").resolve()
            deep = _chain(real_root, _CHAIN)
            _write(real_root / "Makefile", "all:\n\t@echo hi\n")

            with in_dir(deep):
                code, output = cli_output(["build", "-Target", "stm32"])

            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("当前目录无效", output)


class TestFindIocInSubdirectory(unittest.TestCase):
    """缺陷 2: ``.ioc`` 只在工程根找 -> 子目录里的 ``.ioc`` 被忽略而误拒。"""

    def test_ioc_in_subdirectory_is_found(self):
        """实测场景: ``.ioc`` 放在 ``CubeMX/`` 子目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "Core" / "Src" / "main.c", "int main(void){return 0;}\n")
            target = _write(root / "CubeMX" / "test.ioc", _IOC_TEXT)

            self.assertEqual(ioc.find_ioc(root), target)

    def test_root_ioc_wins_over_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            at_root = _write(root / "test.ioc", _IOC_TEXT)
            _write(root / "CubeMX" / "aaa.ioc", _IOC_TEXT)

            self.assertEqual(ioc.find_ioc(root), at_root)

    def test_shallowest_then_alphabetical(self):
        """多个 ``.ioc``: 最浅层优先; 同层按字母序, 保证确定性。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "CubeMX" / "deep" / "x.ioc", _IOC_TEXT)
            shallow = _write(root / "zz" / "y.ioc", _IOC_TEXT)
            self.assertEqual(ioc.find_ioc(root), shallow)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "bb" / "y.ioc", _IOC_TEXT)
            first = _write(root / "aa" / "z.ioc", _IOC_TEXT)
            self.assertEqual(ioc.find_ioc(root), first)

    def test_depth_limit_is_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inside = _write(root / "a" / "b" / "c" / "test.ioc", _IOC_TEXT)
            self.assertEqual(ioc.find_ioc(root), inside)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "a" / "b" / "c" / "d" / "test.ioc", _IOC_TEXT)
            self.assertIsNone(ioc.find_ioc(root))

    def test_tool_and_build_dirs_are_skipped(self):
        """``xtcli/`` / ``build/`` / ``Debug/`` / ``node_modules`` 里的 ``.ioc``
        是工具/产物, 不能当工程配置。"""
        for segment in ("xtcli", "build", "build-ord", "Debug", "node_modules", ".hidden"):
            with self.subTest(segment=segment), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                _write(root / segment / "test.ioc", _IOC_TEXT)

                self.assertIsNone(ioc.find_ioc(root))

    def test_subdirectory_ioc_is_parsed_and_not_refused(self):
        """找到之后必须仍能被解析: ``ioc.read()`` 读得出设备名, 且不再误拒。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "Core" / "Src" / "main.c", "int main(void){return 0;}\n")
            target = _write(root / "CubeMX" / "test.ioc", _IOC_TEXT)

            info = ioc.read(root)
            self.assertIsNotNone(info)
            self.assertEqual(info.path, target)
            self.assertEqual(info.device, "STM32F103RCTx")

            model = gcc_make.build_model(root, {})
            self.assertEqual(model.refusals, [], "子目录里的 .ioc 不该再被判成缺芯片信息")
            self.assertEqual(model.device, "STM32F103RCTx")


if __name__ == "__main__":
    unittest.main()
