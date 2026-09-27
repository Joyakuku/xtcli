"""core 层单测: 环境净化 / 路径工具 / venv 自检 / 原子写。

其中"子进程环境净化"是本次移植最重要的健壮性断言 —— 它把两个实测踩到的坑
（``GCC_ROOT`` 与 ``CONDA_*``）变成结构性禁止。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

# 让 `python -m unittest tests.test_env` 与 discover 两种跑法都能 import support
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (它会把 src/ 放进 sys.path —— 否则单独跑本文件会 ImportError)

from xtcli import env
from xtcli import exec as xt_exec
from xtcli.errors import Exit, XtError


class TestChildEnv(unittest.TestCase):
    def test_denies_gcc_root_and_conda_vars(self):
        injected = {
            "GCC_ROOT": r"E:\env\Arm\cortex-m\bin",
            "CONDA_PREFIX": r"C:\ProgramData\anaconda3",
            "CONDA_DEFAULT_ENV": "base",
            "CONDA_SHLVL": "1",
            "CONDA_EXE": r"C:/ProgramData/anaconda3\Scripts\conda.exe",
        }
        saved = {k: os.environ.get(k) for k in injected}
        try:
            os.environ.update(injected)
            child = xt_exec.child_env()
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertNotIn("GCC_ROOT", child, "GCC_ROOT 会让 Arm 工具链找不到 cc1")
        leftovers = [k for k in child if k.upper().startswith("CONDA")]
        self.assertEqual(leftovers, [], f"conda 变量必须全部剔除, 实际残留: {leftovers}")

    def test_path_drops_anaconda_dirs(self):
        child = xt_exec.child_env([r"E:\env\msys64\usr\bin"])
        lowered = child["PATH"].lower()
        self.assertNotIn("anaconda", lowered)
        self.assertNotIn("miniconda", lowered)
        self.assertTrue(child["PATH"].startswith(r"E:\env\msys64\usr\bin"))

    def test_utf8_and_dedup(self):
        child = xt_exec.child_env([r"C:\Windows", r"C:\Windows"])
        self.assertEqual(child["PYTHONUTF8"], "1")
        entries = [p for p in child["PATH"].split(os.pathsep) if p]
        self.assertEqual(len(entries), len({p.lower() for p in entries}))


class TestPathTools(unittest.TestCase):
    def test_posix_and_relative(self):
        base = Path(r"E:\proj")
        self.assertEqual(env.to_posix(base / "Core" / "Inc"), "E:/proj/Core/Inc")
        self.assertEqual(env.relative_to(base, base / "Core" / "Inc"), "Core/Inc")

    def test_is_within(self):
        base = Path(r"E:\proj")
        self.assertTrue(env.is_within(base / "Core" / "main.c", base))
        self.assertFalse(env.is_within(Path(r"E:\other\main.c"), base))

    def test_realpath_resolves_junction(self):
        """E:\\env\\Arm\\cortex-m 是指向版本化目录的 junction。

        不解析会让工具发现把同一套工具链算成两份 —— 这里直接断言解析结果与
        版本化真目录一致。
        """
        alias = Path(r"E:\env\Arm\cortex-m")
        if not alias.exists():
            self.skipTest("junction 不存在")
        resolved = env.real_path(alias)
        self.assertNotEqual(str(resolved).lower(), str(alias).lower())
        self.assertTrue((resolved / "bin" / "arm-none-eabi-gcc.exe").is_file())


class TestVenvSelfCheck(unittest.TestCase):
    def test_accepts_project_venv(self):
        if env.venv_dir() != Path(os.sys.prefix).resolve():
            self.skipTest("当前不在项目 venv 中")
        env.assert_running_in_venv()  # 不应抛异常

    def test_conda_base_detected(self):
        self.assertTrue(env.looks_like_conda(r"C:\ProgramData\anaconda3"))
        self.assertTrue(env.looks_like_conda(r"C:\miniconda3\python.exe"))
        self.assertFalse(env.looks_like_conda(r"C:\Python314"))

    def test_foreign_interpreter_refused(self):
        """非 venv 解释器必须被拒绝, 且给出 ENVIRONMENT 退出码。"""
        import sys

        original = sys.prefix
        try:
            sys.prefix = r"C:\ProgramData\anaconda3"
            with self.assertRaises(XtError) as ctx:
                env.assert_running_in_venv()
            self.assertEqual(ctx.exception.code, int(Exit.ENVIRONMENT))
        finally:
            sys.prefix = original


class TestAtomicWrite(unittest.TestCase):
    def test_atomic_write_replaces_and_leaves_no_temp(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "config.mk"
            env.atomic_write_text(target, "A := 1\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "A := 1\n")
            env.atomic_write_text(target, "A := 2\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "A := 2\n")
            leftovers = [p.name for p in target.parent.iterdir() if "xtcli-tmp" in p.name]
            self.assertEqual(leftovers, [])

    def test_concurrent_writers_never_mix_content(self):
        """两个同时运行的 xtcli 不能互相踩临时文件。

        回归: 临时文件名曾是固定的 ``<名字>.xtcli-tmp``, 于是并发写会落到同一个
        文件上, replace 上去的内容可能是两次写入的混合。现在每次唯一 (pid+序号)。
        """
        import tempfile
        import threading

        contents = [f"writer-{index}\n" * 80 for index in range(8)]
        failures: list[BaseException] = []
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "config.mk"

            def worker(text: str) -> None:
                try:
                    for _ in range(20):
                        env.atomic_write_text(target, text)
                except BaseException as exc:  # 线程里的异常 unittest 看不见, 必须自己收
                    failures.append(exc)

            threads = [threading.Thread(target=worker, args=(text,)) for text in contents]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(failures, [], f"并发写不应抛异常: {failures[:1]}")
            final = target.read_text(encoding="utf-8")
            self.assertIn(final, contents, "最终内容必须是某一次写入的完整结果")
            leftovers = [p.name for p in target.parent.iterdir() if "xtcli-tmp" in p.name]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
