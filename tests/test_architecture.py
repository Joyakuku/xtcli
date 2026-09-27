"""架构约束测试 —— 把"零依赖 / 分层 / 单一入口 / 命令面冻结"变成可执行的断言。

这一层不是功能测试, 而是**防止架构腐化的闸门**。它回答的问题是:
"这次改动有没有破坏项目赖以稳定的那几条结构性事实?"

不变量:
1. **零第三方运行时依赖**: ``pyproject.toml`` 的 dependencies 为空, 且源码只 import
   标准库或 ``xtcli`` 自己。(核心价值: 换台机器/断网也能跑, 不被任何包管理器绑住。)
2. **分层**: 后端只被注册表按需导入, 核心模块不得反向依赖后端。
3. **模块规模**: 单个模块 ≤ 700 行 —— 1286 行的 ``gcc_make.py`` 是上一次的教训。
4. **数据/资源单一入口**: 只有 ``env.py`` 决定 data/assets 的位置, 其它模块只允许
   通过 ``env.data_dir()/assets_dir()`` 取路径。
5. **命令面冻结**: 动词与选项集合必须与文档一致 —— 新增命令要显式改这个测试,
   避免"顺手加个选项"悄悄扩大接口。
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (把 src/ 放进 sys.path)

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "xtcli"

# 冻结的命令面（改动这里等于改公开接口, 必须是有意的）
FROZEN_VERBS = ("init", "build", "burn", "doctor")
FROZEN_OPTIONS = {
    "-h", "--help",
    "-v", "--version",
    "-Target", "--target",
    "-Root", "--root",
    "-Config", "--config",
    "-Clean", "--clean",
    "-NoBuild", "--no-build",
    "-NoStubs", "--no-stubs",
    "-MakeTarget", "--make-target",
    "-Interface", "--interface",
    "-OcdTarget", "--ocd-target",
    "-ProbeSerial", "--probe-serial",
    "-Bin", "--bin",
    "-Address", "--address",
    "-Elf", "--elf",
    "-Probe", "--probe",
    "-FlashType", "--flash-type",
    "-Flasher", "--flasher",
    "-Cpu", "--cpu",
    "-Fpu", "--fpu",
    "-Port", "--port",
    "-Refresh", "--refresh",
    "-Quiet", "--quiet",
    "-LogFile", "--log-file",
}

MAX_MODULE_LINES = 700

# 允许在模块层导入 xtcli.backends 的核心模块（注册表本身就是干这个的）
BACKEND_IMPORT_ALLOWED = {"model.py"}


def _modules() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(tree: ast.AST) -> set[str]:
    """收集 ``import x.y`` / ``from x.y import z`` 里的顶层模块名。"""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # 相对导入 = 包内导入
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


class TestZeroDependencies(unittest.TestCase):
    """运行时依赖必须为空; 开发工具 (可选 extras) 不算运行时依赖。"""

    # 允许出现在 extras 里的开发工具 —— 它们只在开发/CI 环境用, 不进运行时
    ALLOWED_DEV_EXTRAS = ("pytest", "ruff", "mypy", "coverage")

    def test_pyproject_declares_no_runtime_dependencies(self):
        data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"].get("dependencies"), [])
        # extras 只允许开发工具 (零依赖的核心承诺是关于**运行时**的)
        for name, extra in (data["project"].get("optional-dependencies") or {}).items():
            for item in extra:
                package = re.split(r"[<>=!\[;]", item, maxsplit=1)[0].strip().lower()
                self.assertIn(
                    package,
                    self.ALLOWED_DEV_EXTRAS,
                    f"extras[{name}] 里出现了非开发工具依赖: {item}",
                )

    def test_only_stdlib_and_self_are_imported(self):
        allowed = set(sys.stdlib_module_names) | {"xtcli"}
        offenders: list[str] = []
        for path in _modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover
                self.fail(f"{path} 语法错误: {exc}")
            for root in sorted(_imported_roots(tree)):
                if root not in allowed:
                    offenders.append(f"{path.relative_to(SRC)} -> {root}")
        self.assertEqual(offenders, [], f"出现第三方导入: {offenders}")


class TestLayering(unittest.TestCase):
    def test_core_does_not_import_backends(self):
        """核心模块不得在模块层反向依赖 backends (只有注册表例外)。"""
        for path in _modules():
            rel = path.relative_to(SRC)
            if rel.parts[0] == "backends" or path.name in BACKEND_IMPORT_ALLOWED:
                continue
            text = path.read_text(encoding="utf-8")
            hits = re.findall(r"^\s*from\s+\.?backends[.\s]|^\s*from\s+xtcli\.backends", text, re.M)
            for hit in hits:
                # 允许"函数内按需导入"（注册表就是这么干的）
                line_no = text[: text.index(hit)].count("\n")
                indent = len(hit) - len(hit.lstrip())
                if indent > 0:
                    continue
                self.fail(f"{rel}:{line_no + 1} 核心模块在模块层导入了 backends")

    def test_cli_imports_only_core(self):
        text = (SRC / "cli.py").read_text(encoding="utf-8")
        self.assertNotIn("from .backends", text)
        self.assertNotIn("import backends", text)

    def test_backends_are_reachable_through_the_registry(self):
        from xtcli import model as model_mod

        model_mod.load_backends()
        names = {b.name for b in model_mod.all_backends()}
        self.assertIn("gcc_make", names)
        self.assertIn("espidf", names)


class TestModuleSize(unittest.TestCase):
    def test_no_module_exceeds_the_limit(self):
        oversized = {
            str(path.relative_to(SRC)): len(path.read_text(encoding="utf-8").splitlines())
            for path in _modules()
            if len(path.read_text(encoding="utf-8").splitlines()) > MAX_MODULE_LINES
        }
        self.assertEqual(oversized, {}, f"模块超过 {MAX_MODULE_LINES} 行: {oversized}")


class TestSingleEntryPoints(unittest.TestCase):
    def test_only_env_decides_data_and_assets_locations(self):
        """只有 env.py 可以拼 data/assets 的物理路径。"""
        offenders: list[str] = []
        for path in _modules():
            rel = path.relative_to(SRC)
            if rel.name in ("env.py", "devices.py"):
                continue  # devices.py 通过 env.data_dir() 取, 属于允许的"数据层"
            text = path.read_text(encoding="utf-8")
            for pattern in (r'["\']data["\']\s*/', r'/["\']data["\']', r'["\']assets["\']\s*/'):
                if re.search(pattern, text):
                    offenders.append(f"{rel} ({pattern})")
        self.assertEqual(offenders, [], f"绕过了 env 的路径入口: {offenders}")

    def test_assets_are_referenced_through_env(self):
        text = (SRC / "backends" / "gcc_chip.py").read_text(encoding="utf-8")
        self.assertIn("env.assets_dir()", text)


class TestCommandNamesAreUnified(unittest.TestCase):
    """命令名统一为 ``xtcli-{init,build,burn}-pj``（不带芯片前缀）。

    为什么值得断言: 带前缀的名字 (`stm32-init-pj`) 会让人以为"只能编 STM32",
    而它其实对**所有芯片**都适用 (后端按工程识别)。统一之后:
    * 旧名字不许再出现在源码/文档里 —— 否则用户照着敲会 command not found;
    * `setup.ps1` 生成的 shim 必须正好是这套新名字, 并且会**清理**旧 shim。
    """

    NEW_NAMES = ("xtcli-init-pj", "xtcli-build-pj", "xtcli-burn-pj")
    OLD_NAMES = (
        "stm32-init-pj", "stm32-build-pj", "stm32-burn-pj",
        "esp32-init-pj", "esp32-build-pj", "esp32-burn-pj",
    )

    def _files(self) -> list[Path]:
        return [
            *SRC.rglob("*.py"),
            REPO / "setup.ps1",
            REPO / "README.md",
            REPO / "docs" / "使用手册.md",
            REPO / "docs" / "维护手册.md",
        ]

    def test_old_names_are_gone(self):
        offenders: list[str] = []
        for path in self._files():
            text = path.read_text(encoding="utf-8")
            # setup.ps1 里的 $staleShims 是**清理名单**: 它必须提到旧名字才能删掉它们
            if path.name == "setup.ps1":
                start = text.index("$staleShims = @(")
                end = text.index("\n)\n", start) + 3
                text = text[:start] + text[end:]
            for old in self.OLD_NAMES:
                if old in text:
                    offenders.append(f"{path.relative_to(REPO)} -> {old}")
        self.assertEqual(offenders, [], f"仍在使用已取消的旧命令名: {offenders}")

    def test_new_names_are_documented(self):
        for rel in ("README.md", "docs/使用手册.md"):
            text = (REPO / rel).read_text(encoding="utf-8")
            for name in self.NEW_NAMES:
                with self.subTest(doc=rel, name=name):
                    self.assertIn(name, text)

    def test_setup_generates_exactly_the_new_shims(self):
        text = (REPO / "setup.ps1").read_text(encoding="utf-8")
        block = text[text.index("$shims = [ordered]@{"): text.index("foreach ($name in $shims.Keys)")]
        self.assertIn("'xtcli-init-pj.cmd'", block)
        self.assertIn("'xtcli-build-pj.cmd'", block)
        self.assertIn("'xtcli-burn-pj.cmd'", block)
        # 新 shim 不带 -Target: 默认 auto, 由工程识别后端
        self.assertNotIn("-Target", block)
        # 旧 shim 必须被清理 (否则 PATH 上两套入口并存)
        self.assertIn("$staleShims", text)
        for old in self.OLD_NAMES:
            with self.subTest(old=old):
                self.assertIn(f"'{old}'", text)

    def test_help_lists_the_new_names(self):
        import contextlib
        import io

        from xtcli import cli

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.show_help()
        help_text = buffer.getvalue()
        for name in self.NEW_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, help_text)


class TestCommandSurfaceFrozen(unittest.TestCase):
    def test_verbs(self):
        from xtcli import cli

        self.assertEqual(tuple(cli.VERBS), FROZEN_VERBS)

    def test_options(self):
        from xtcli import cli

        actual = {
            option
            for action in cli.build_parser()._actions
            for option in getattr(action, "option_strings", [])
        }
        self.assertEqual(actual, FROZEN_OPTIONS)

    def test_help_lists_every_option(self):
        """文档与实现必须一致: help 里出现的选项都得是真实存在的。"""

        text = (SRC / "cli.py").read_text(encoding="utf-8")
        help_body = text[text.index("def show_help"): text.index("def show_version")]
        for option in sorted(FROZEN_OPTIONS):
            if option.startswith("--") or option in ("-h", "-v"):
                continue
            with self.subTest(option=option):
                self.assertIn(option, help_body, f"help 文本里没有 {option}")


if __name__ == "__main__":
    unittest.main()
