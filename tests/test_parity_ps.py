"""PS-as-oracle 对等测试。

冻结的 PowerShell 实现（``ps-legacy/``）充当 oracle: 同一工程分别跑两版, 逐字段
比对归一化模型。这是移植等价性最强的证据, 也是本次移植特有的优势 —— 新旧两版
可以同时对跑。

不需要 pwsh 或 ps-legacy 时自动跳过。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import PS_LEGACY, has_project, in_dir, project_path  # noqa: F401

from xtcli.backends import gcc_make

PWSH = shutil.which("pwsh")
ORACLE = PS_LEGACY / "lib" / "xtcli.ps1"

_FIELD_RE = re.compile(r"^\s*(来源|芯片|宏定义|头文件路径|源文件清单|启动文件|链接脚本|可编译源文件)\s*:\s*(.+?)\s*$")


def oracle_fields(root: Path) -> dict[str, str]:
    """跑冻结的 PS 版 doctor, 抽出可比对的字段。"""
    result = subprocess.run(
        [PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ORACLE), "doctor"],
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    text = result.stdout.decode("utf-8", "replace")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line)
        if match:
            fields[match.group(1)] = match.group(2).strip()
    return fields


def python_fields(root: Path) -> dict[str, str]:
    model = gcc_make.build_model(root, {"config": "Debug"})
    c_files, s_files = gcc_make.collect_sources(model)
    fields = {
        "来源": model.source,
        "芯片": f"{model.device}  (系列 {model.family})",
        "宏定义": " ".join(model.defines),
        "头文件路径": f"{len(model.includes)} 条",
        "启动文件": " ".join(str(p) for p in model.asm_srcs),
        "链接脚本": str(model.ld_script),
        "可编译源文件": f"{len(c_files)} 个 .c, {len(s_files)} 个 .s",
    }
    if model.src_files:
        fields["源文件清单"] = f"{len(model.src_files)} 个 .c (来自原 IDE 工程, 精确)"
    return fields


@unittest.skipUnless(PWSH and ORACLE.is_file(), "缺少 pwsh 或 ps-legacy")
class TestOracleParity(unittest.TestCase):
    def _compare(self, name: str):
        if not has_project(name):
            self.skipTest(f"{name} 不存在")
        root = project_path(name)
        oracle = oracle_fields(root)
        python = python_fields(root)
        self.assertTrue(oracle, "oracle 没有输出可解析字段")
        for key, value in python.items():
            with self.subTest(field=key):
                self.assertIn(key, oracle, f"oracle 未输出字段 {key}")
                self.assertEqual(value, oracle[key], f"{name}: 字段 {key} 不一致")

    def test_demo(self):
        self._compare("demo")

    def test_1_led(self):
        self._compare("1_LED")

    def test_freertos_hal_template(self):
        self._compare("freertos_hal_template")

    def test_acceptance_set_is_covered(self):
        """三工程 + 三拒绝用例必须都有覆盖, 防止有人删测试。"""
        names = {p.name for p in Path(__file__).parent.glob("test_*.py")}
        for expected in ("test_extract.py", "test_refusal.py", "test_build.py", "test_env.py", "test_cli.py"):
            self.assertIn(expected, names)


if __name__ == "__main__":
    unittest.main()
