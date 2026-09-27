"""受管文件清单与覆盖保护。

背景 —— 审查中实测到的三种破坏性覆盖:
  * ``.cproject`` 声明的 .ld 解析到工程外时, 工程内同名 .ld 被从 5376 字节
    覆盖成 55 字节;
  * ``compile_commands.json``（可能是 CMake/bear/compiledb 的产物）被静默覆盖;
  * ``rules.mk`` / ``config.mk`` 无条件覆盖, 用户的本地修改丢失。

契约: 工具生成的文件登记进 ``xtcli/.owned.json``（路径 + SHA256）。
"我们自己且没被改过"才允许静默覆盖; 其余一律先备份成 ``<名字>.xtcli-bak``。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path)
from support import MAIN_C, cli_output, in_dir, make_min_project, toolchain_ready

from xtcli import managed


class TestOwnedManifest(unittest.TestCase):
    def _rel(self, root: Path, name: str = "xtcli/config.mk") -> Path:
        return root / name

    def test_manifest_records_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owned = managed.Owned(root)
            target = self._rel(root)
            owned.write_text(target, "hello\n")
            owned.flush()

            data = json.loads((root / "xtcli" / ".owned.json").read_text(encoding="utf-8"))
            self.assertIn("xtcli/config.mk", data["files"])
            self.assertTrue(managed.is_owned_and_unmodified(root, target))

    def test_foreign_file_is_backed_up_before_overwrite(self):
        """回归 H4: compile_commands.json 这类外来文件曾被静默覆盖。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root)
            target.parent.mkdir(parents=True)
            target.write_text("USER DATA\n", encoding="utf-8")

            owned = managed.Owned(root)
            owned.write_text(target, "generated\n")

            backup = target.with_name("config.mk.xtcli-bak")
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_text(encoding="utf-8"), "USER DATA\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "generated\n")

    def test_user_modified_file_is_backed_up_on_regeneration(self):
        """回归: rules.mk 的用户本地修改曾在下一次 init 时直接丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root, "xtcli/rules.mk")
            owned = managed.Owned(root)
            owned.write_text(target, "v1\n")
            owned.flush()

            target.write_text("v1 + my local edit\n", encoding="utf-8")  # 用户改了

            second = managed.Owned(root)
            second.write_text(target, "v2\n")
            backup = target.with_name("rules.mk.xtcli-bak")
            self.assertEqual(backup.read_text(encoding="utf-8"), "v1 + my local edit\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "v2\n")

    def test_our_own_unmodified_file_is_overwritten_without_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root)
            owned = managed.Owned(root)
            owned.write_text(target, "v1\n")
            owned.flush()

            second = managed.Owned(root)
            second.write_text(target, "v2\n")
            self.assertEqual(second.backups, [])
            self.assertFalse(target.with_name("config.mk.xtcli-bak").exists())

    def test_existing_backup_is_never_clobbered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root, "xtcli/rules.mk")
            target.parent.mkdir(parents=True)
            target.write_text("USER\n", encoding="utf-8")
            backup = target.with_name("rules.mk.xtcli-bak")
            backup.write_text("FIRST BACKUP\n", encoding="utf-8")

            managed.Owned(root).write_text(target, "gen\n")
            self.assertEqual(backup.read_text(encoding="utf-8"), "FIRST BACKUP\n")

    def test_broken_manifest_is_treated_as_empty(self):
        """登记表损坏时按"什么都没登记过"处理 —— 于是更保守 (会备份)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "xtcli" / ".owned.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(managed.load(root), {})

    def test_identical_text_needs_no_backup(self):
        """升级后第一次 init: 上版本生成的文件若与本次内容一致, 不该产生备份噪声。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root)
            target.parent.mkdir(parents=True)
            # 按 env.atomic_write_text 的写法落盘 (newline="" -> 不做换行翻译)
            target.write_bytes(b"same content\nline2\n")

            owned = managed.Owned(root)          # 没有登记表 -> 视为外来文件
            owned.write_text(target, "same content\nline2\n")

            self.assertEqual(owned.backups, [])
            self.assertFalse(target.with_name("config.mk.xtcli-bak").exists())

    def test_identical_copy_needs_no_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "asset.mk"
            src.write_text("body\n", encoding="utf-8")
            dst = self._rel(root, "xtcli/rules.mk")
            dst.parent.mkdir(parents=True)
            dst.write_text("body\n", encoding="utf-8")

            owned = managed.Owned(root)
            owned.copy_file(src, dst)
            self.assertEqual(owned.backups, [])

    def test_different_content_still_backs_up(self):
        """内容真的不同就仍然要备份 (上面两条是"没变"的特例, 不是放宽保护)。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._rel(root)
            target.parent.mkdir(parents=True)
            target.write_text("user version\n", encoding="utf-8")

            owned = managed.Owned(root)
            owned.write_text(target, "generated version\n")

            self.assertEqual(len(owned.backups), 1)
            self.assertTrue(target.with_name("config.mk.xtcli-bak").is_file())


@unittest.skipUnless(toolchain_ready(), "缺少 arm-none-eabi 工具链或 make")
class TestInitProtectsUserFiles(unittest.TestCase):
    def test_second_init_does_not_back_up_its_own_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            for _ in range(2):
                with in_dir(root):
                    code, out = cli_output(["init", "-NoBuild"])
                self.assertEqual(code, 0, out)
            backups = [str(p.relative_to(root)) for p in root.rglob("*.xtcli-bak")]
            self.assertEqual(backups, [], f"自己的文件不该备份: {backups}")
            self.assertTrue((root / "xtcli" / ".owned.json").is_file())

    def test_user_compile_commands_is_backed_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_min_project(Path(tmp), {"Core/Src/main.c": MAIN_C})
            (root / "compile_commands.json").write_text('{"mine": 1}\n', encoding="utf-8")
            with in_dir(root):
                code, out = cli_output(["init", "-NoBuild"])
            self.assertEqual(code, 0, out)
            backup = root / "compile_commands.json.xtcli-bak"
            self.assertTrue(backup.is_file(), out)
            self.assertEqual(backup.read_text(encoding="utf-8"), '{"mine": 1}\n')
            self.assertIn("xtcli", (root / "compile_commands.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
