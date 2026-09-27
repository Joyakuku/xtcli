"""Eclipse ``.cproject`` 厂商家族测试。

**重要说明**: 本机只有 STM32CubeIDE 的真实工程, 没有 CCS/MCUXpresso/Simplicity
等真实工程。所以这里的 fixture 是**按各厂商命名空间合成**的, 用来验证解析器
"与 IDE 前缀无关"这条设计, 以及"未识别选项必须报出来"这条诚实性要求 ——
不能据此宣称对真实厂商工程已支持。

真正的保护机制是: 解析器尽力提取 + 把没理解的构建选项明确报出来 + 试编译兜底。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from support import cli_output, has_project, in_dir, project_path, snapshot

from xtcli.backends import eclipse_cproject
from xtcli.errors import Exit

# 各厂商的 managedbuild 命名空间前缀（合成用的代表值）
VENDOR_PREFIXES = {
    "STM32CubeIDE": "com.st.stm32cube.ide.mcu.gnu.managedbuild",
    "TrueSTUDIO": "com.atollic.truestudio.managedbuild",
    "SW4STM32": "fr.ac6.mcu.managedbuild",
    "MCUXpresso": "com.nxp.mcuxpresso.tools.bin.managedbuild",
    "SimplicityStudio": "com.silabs.ide.si32.gcc.managedbuild",
    "e2studio": "com.renesas.cdt.managedbuild.gnuarm",
    "MounRiver": "com.mounriver.ide.mcu.gnu.managedbuild",
    "S32DS": "com.nxp.s32ds.cle.arm.mbs.arm32.bare.managedbuild",
}


def _option(super_class: str, *, value: str | None = None, values: list[str] | None = None,
            value_type: str = "string") -> str:
    if values is None:
        return f'<option id="{super_class}.1" superClass="{super_class}" value="{value}" valueType="{value_type}"/>'
    items = "".join(f'<listOptionValue builtIn="false" value="{item}"/>' for item in values)
    return f'<option id="{super_class}.1" superClass="{super_class}" valueType="{value_type}">{items}</option>'


def make_cproject(prefix: str, *, device: str = "STM32F407VGTx", extra_options: list[str] | None = None) -> str:
    options = [
        _option(f"{prefix}.option.target_mcu", value=device),
        _option(f"{prefix}.tool.c.compiler.option.definedsymbols", values=["USE_HAL_DRIVER", "STM32F407xx"],
                value_type="definedSymbols"),
        _option(f"{prefix}.tool.c.compiler.option.includepaths", values=["../Core/Inc"], value_type="includePath"),
        _option(f"{prefix}.tool.c.linker.option.script", value="${workspace_loc:/${ProjName}/app.ld}"),
        _option(f"{prefix}.tool.c.linker.option.libraries", values=[":mylib.a"], value_type="libs"),
        _option(f"{prefix}.tool.c.linker.option.directories", values=["../lib"], value_type="libPaths"),
    ]
    options.extend(extra_options or [])
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">
  <storageModule moduleId="org.eclipse.cdt.core.settings">
    <cconfiguration id="{prefix}.config.exe.debug.1">
      <storageModule buildSystemId="org.eclipse.cdt.managedbuilder.core.configurationDataProvider"
                     id="{prefix}.config.exe.debug.1" moduleId="org.eclipse.cdt.core.settings" name="Debug">
        <externalSettings/>
        <extensions/>
      </storageModule>
      <storageModule moduleId="cdtBuildSystem" version="4.0.0">
        <configuration artifactExtension="elf" artifactName="${{ProjName}}" name="Debug">
          <folderInfo id="{prefix}.config.exe.debug.1." name="/" resourcePath="">
            <toolChain id="{prefix}.toolchain.exe.debug.1" name="MCU ARM GCC"
                       superClass="{prefix}.toolchain.exe.debug">
              {''.join(options)}
            </toolChain>
          </folderInfo>
          <sourceEntries>
            <entry flags="VALUE_WORKSPACE_PATH|RESOLVED" kind="sourcePath" name="Core"/>
          </sourceEntries>
        </configuration>
      </storageModule>
    </cconfiguration>
  </storageModule>
</cproject>"""


def _write_project(tmp: str, name: str, cproject_xml: str, *, ccsproject: str | None = None) -> Path:
    root = Path(tmp) / name
    (root / "Core" / "Inc").mkdir(parents=True)
    (root / "Core").mkdir(exist_ok=True)
    (root / "lib").mkdir(exist_ok=True)
    (root / "app.ld").write_text("/* ld */\n", encoding="utf-8")
    (root / ".cproject").write_text(cproject_xml, encoding="utf-8")
    if ccsproject is not None:
        (root / ".ccsproject").write_text(ccsproject, encoding="utf-8")
    return root


def _make_path_fixture(tmp: str, name: str, include_expr: str, *, decoy_build: bool = True) -> Path:
    """造一个"工程根"与"配置目录(Debug/)"下**同名目录都存在**的工程。

    这正是缺陷 M1 的触发条件: 只看存在性时, ``Debug/Core/Inc`` 会先被选中,
    把工程根下那份真的头文件换成另一份并集内容不同的副本。
    """
    prefix = VENDOR_PREFIXES["STM32CubeIDE"]
    options = [
        _option(f"{prefix}.option.target_mcu", value="STM32F103RCTx"),
        _option(f"{prefix}.tool.c.compiler.option.includepaths", values=[include_expr], value_type="includePath"),
    ]
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">
 <storageModule moduleId="org.eclipse.cdt.core.settings">
  <cconfiguration id="{prefix}.config.exe.debug.1">
   <storageModule moduleId="cdtBuildSystem" version="4.0.0">
    <configuration artifactExtension="elf" artifactName="${{ProjName}}" name="Debug">
     <folderInfo id="f1" name="/" resourcePath="">
      <toolChain id="t1" name="MCU ARM GCC" superClass="{prefix}.toolchain.exe.debug">
       {''.join(options)}
      </toolChain>
     </folderInfo>
     <sourceEntries/>
    </configuration>
   </storageModule>
  </cconfiguration>
 </storageModule>
</cproject>"""
    root = Path(tmp) / name
    (root / "Core" / "Inc").mkdir(parents=True)
    (root / "Core" / "Inc" / "real.h").write_text("/* 工程根那份 */\n", encoding="utf-8")
    if decoy_build:
        (root / "Debug" / "Core" / "Inc").mkdir(parents=True)
        (root / "Debug" / "Core" / "Inc" / "decoy.h").write_text("/* Debug 副本 */\n", encoding="utf-8")
    (root / ".cproject").write_text(xml, encoding="utf-8")
    return root


def _write_project_with_configs(tmp: str, name: str, configs: list[str]) -> Path:
    """造一个 .cproject（含若干配置名）+ Core/Src 的最小工程。"""
    prefix = VENDOR_PREFIXES["STM32CubeIDE"]
    blocks = []
    for cfg_name in configs:
        blocks.append(f"""<cconfiguration id="{prefix}.{cfg_name}.1">
 <storageModule buildSystemId="org.eclipse.cdt.managedbuilder.core.configurationDataProvider"
                id="{prefix}.{cfg_name}.1" moduleId="org.eclipse.cdt.core.settings" name="{cfg_name}"/>
 <storageModule moduleId="cdtBuildSystem" version="4.0.0">
  <configuration artifactExtension="elf" artifactName="${{ProjName}}" name="{cfg_name}">
   <folderInfo id="f.{cfg_name}" name="/" resourcePath="">
    <toolChain id="t.{cfg_name}" name="MCU ARM GCC" superClass="{prefix}.toolchain.exe.debug">
     <option id="o1" superClass="{prefix}.option.target_mcu" value="STM32F103RCTx" valueType="string"/>
    </toolChain>
   </folderInfo>
   <sourceEntries><entry kind="sourcePath" name="Core"/></sourceEntries>
  </configuration>
 </storageModule>
</cconfiguration>""")
    xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">
 <storageModule moduleId="org.eclipse.cdt.core.settings">
  {''.join(blocks)}
 </storageModule>
</cproject>"""
    root = Path(tmp) / name
    (root / "Core" / "Src").mkdir(parents=True)
    (root / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    (root / ".cproject").write_text(xml, encoding="utf-8")
    return root


class TestVendorPrefixAgnostic(unittest.TestCase):
    """解析必须与 IDE 前缀无关 —— 这是"一个解析器覆盖多个 Eclipse 家族"的基础。"""

    def test_all_vendor_prefixes_extract_identically(self):
        for vendor, prefix in VENDOR_PREFIXES.items():
            with self.subTest(vendor=vendor), tempfile.TemporaryDirectory() as tmp:
                root = _write_project(tmp, vendor, make_cproject(prefix))
                cfg = eclipse_cproject.read(root, "Debug")

                self.assertEqual(cfg.device, "STM32F407VGTx", vendor)
                self.assertIn("USE_HAL_DRIVER", cfg.defines, vendor)
                self.assertIn("STM32F407xx", cfg.defines, vendor)
                self.assertEqual(len(cfg.includes), 1, vendor)
                self.assertEqual(cfg.includes[0], root / "Core" / "Inc", vendor)
                self.assertEqual([p.name for p in cfg.src_dirs], ["Core"], vendor)
                self.assertEqual(cfg.libs, [":mylib.a"], vendor)
                self.assertEqual(cfg.lib_dirs, [root / "lib"], vendor)
                self.assertEqual(cfg.ld_script, root / "app.ld", vendor)


class TestTiCcsNaming(unittest.TestCase):
    """TI CCS 用另一套命名（compilerID.* / linkerID.*）。"""

    CCS_PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<ccsproject>
  <deviceVariant>MSPM0G3507</deviceVariant>
  <outputType>executable</outputType>
</ccsproject>"""

    def _xml(self) -> str:
        prefix = "com.ti.ccstudio.buildDefinitions.TMS470_20.2"
        options = [
            _option(f"{prefix}.compilerID.DEFINE", values=["__MSPM0G3507__"], value_type="definedSymbols"),
            _option(f"{prefix}.compilerID.INCLUDE_PATH", values=["${PROJECT_ROOT}/source"],
                    value_type="includePath"),
            _option(f"{prefix}.linkerID.LIBRARY", values=["driverlib"], value_type="libs"),
            _option(f"{prefix}.linkerID.INCLUDE_PATH", values=["${PROJECT_ROOT}/lib"], value_type="libPaths"),
            _option(f"{prefix}.linkerID.FILE", value="${PROJECT_ROOT}/device_linker.cmd"),
        ]
        return f"""<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">
  <storageModule moduleId="org.eclipse.cdt.core.settings">
    <cconfiguration id="com.ti.ccstudio.buildDefinitions.config.1">
      <storageModule buildSystemId="org.eclipse.cdt.managedbuilder.core.configurationDataProvider"
                     id="com.ti.ccstudio.buildDefinitions.config.1"
                     moduleId="org.eclipse.cdt.core.settings" name="Debug"/>
      <storageModule moduleId="cdtBuildSystem" version="4.0.0">
        <configuration artifactExtension="out" name="Debug">
          <folderInfo id="x" name="/" resourcePath="">
            <toolChain id="chain.1" name="TI Build Tools"
                       superClass="com.ti.ccstudio.buildDefinitions.toolchain">
              {''.join(options)}
            </toolChain>
          </folderInfo>
          <sourceEntries>
            <entry kind="sourcePath" name="source"/>
          </sourceEntries>
        </configuration>
      </storageModule>
    </cconfiguration>
  </storageModule>
</cproject>"""

    def test_ccs_options_and_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project(tmp, "ccs", self._xml(), ccsproject=self.CCS_PROJECT)
            (root / "source").mkdir(exist_ok=True)
            cfg = eclipse_cproject.read(root, "Debug")

            self.assertIn("__MSPM0G3507__", cfg.defines)
            self.assertEqual(len(cfg.includes), 1)
            self.assertEqual(cfg.libs, ["driverlib"])
            # 器件型号来自 .ccsproject
            self.assertEqual(cfg.device, "MSPM0G3507")
            self.assertTrue(any(".ccsproject" in w for w in cfg.warnings))

    def test_ti_linker_cmd_is_rejected_not_used(self):
        """TI 的 .cmd 不能喂给 GNU ld —— 必须忽略并告知, 不能当成 .ld 用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project(tmp, "ccs2", self._xml(), ccsproject=self.CCS_PROJECT)
            cfg = eclipse_cproject.read(root, "Debug")
            self.assertIsNone(cfg.ld_script, "TI .cmd 不能被当成 GNU 链接脚本")
            self.assertTrue(any(".cmd" in w or "GNU ld" in w for w in cfg.warnings), cfg.warnings)


class TestUnrecognizedReporting(unittest.TestCase):
    """诚实性要求: 没理解的构建选项必须报出来, 而不是静默给出错误参数。"""

    def test_unknown_build_option_is_reported(self):
        prefix = VENDOR_PREFIXES["MCUXpresso"]
        extra = [_option(f"{prefix}.tool.c.compiler.option.someVendorSpecificThing", value="x")]
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project(tmp, "unknown", make_cproject(prefix, extra_options=extra))
            cfg = eclipse_cproject.read(root, "Debug")
            self.assertTrue(any("未被识别" in w for w in cfg.warnings), cfg.warnings)
            self.assertTrue(any("someVendorSpecificThing" in w for w in cfg.warnings))

    def test_non_build_options_are_not_reported(self):
        """convertbinary / cpuclock 这类与编译参数无关的选项不该变成噪音。"""
        prefix = VENDOR_PREFIXES["STM32CubeIDE"]
        extra = [
            _option(f"{prefix}.option.convertbinary", value="true", value_type="boolean"),
            _option(f"{prefix}.mcu.debug.option.cpuclock", value="72"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project(tmp, "noise", make_cproject(prefix, extra_options=extra))
            cfg = eclipse_cproject.read(root, "Debug")
            self.assertFalse(any("未被识别" in w for w in cfg.warnings), cfg.warnings)


class TestOptimizeDebugPrecedence(unittest.TestCase):
    """汇编器与 C++ 编译器也各有优化/调试等级; 取 C 编译器的那份。"""

    def test_c_compiler_wins_over_assembler(self):
        prefix = VENDOR_PREFIXES["STM32CubeIDE"]
        extra = [
            _option(f"{prefix}.tool.c.compiler.option.optimization.level", value=f"{prefix}.o2"),
            _option(f"{prefix}.tool.c.compiler.option.debuglevel", value=f"{prefix}.g3"),
            _option(f"{prefix}.tool.assembler.option.optimization.level", value=f"{prefix}.o0"),
            _option(f"{prefix}.tool.assembler.option.debuglevel", value=f"{prefix}.g0"),
            _option(f"{prefix}.tool.cpp.compiler.option.debuglevel", value=f"{prefix}.g0"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project(tmp, "prec", make_cproject(prefix, extra_options=extra))
            cfg = eclipse_cproject.read(root, "Debug")
            self.assertTrue(str(cfg.optimize).endswith(".o2"), cfg.optimize)
            self.assertTrue(str(cfg.debug_level).endswith(".g3"), cfg.debug_level)
            # 都识别了, 不该有"未被识别"噪音
            self.assertFalse(any("未被识别" in w for w in cfg.warnings), cfg.warnings)


class TestPathBaseNotationBeatsExistence(unittest.TestCase):
    """M1 回归: 路径基准**按写法**判定, 不许用"哪个候选存在就用哪个"来猜。

    实测过的错误形态: 工程里同时存在 ``root/Debug/Core/Inc``（构建副本）时,
    ``${workspace_loc:/${ProjName}/Core/Inc}`` 被解析成 ``<root>/Debug/Core/Inc``;
    正确结果是 ``<root>/Core/Inc``。后果是头文件/库目录/.ld 静默指向另一份
    同名但内容不同的副本。
    """

    def test_workspace_loc_is_project_root_even_if_build_copy_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_path_fixture(tmp, "ws", "${workspace_loc:/${ProjName}/Core/Inc}")
            includes = [eclipse_cproject.convert_path(
                "${workspace_loc:/${ProjName}/Core/Inc}", root, "Debug")]
            self.assertEqual(includes, [root / "Core" / "Inc"])
            self.assertNotEqual(includes[0], root / "Debug" / "Core" / "Inc")

            cfg = eclipse_cproject.read(root, "Debug")
            self.assertEqual(cfg.includes, [root / "Core" / "Inc"])
            self.assertTrue((root / "Debug" / "Core" / "Inc" / "decoy.h").is_file(),
                            "fixture 必须真的存在诱饵目录, 否则这条测试没意义")
            self.assertFalse(
                [w for w in cfg.warnings if "路径基准" in w],
                f"写法明确时不该产出基准告警: {cfg.warnings}",
            )

    def test_parent_relative_never_escapes_to_build_dir(self):
        """``../Core/Inc`` 必须落到工程根的 ``Core/Inc``, 不能指向 ``Debug/`` 下的副本。

        注意: ``../`` 的基准是 ``<工程根>/<配置名>``, 而 ``Debug/../Core/Inc``
        normpath 之后就是 ``<工程根>/Core/Inc`` —— 所以这两种解释在 ``../`` 形态下
        结果本来就一致（旧实现也是这个结果, 这次修改不得改变它）。这里把它钉死,
        防止有人把 ``../`` 也改成"先看存在性"从而指到构建副本上。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_path_fixture(tmp, "rel", "../Core/Inc")
            decoy = root / "Debug" / "Core" / "Inc"
            self.assertTrue((decoy / "decoy.h").is_file(), "诱饵必须存在")

            self.assertEqual(eclipse_cproject.convert_path("../Core/Inc", root, "Debug"),
                             root / "Core" / "Inc")
            cfg = eclipse_cproject.read(root, "Debug")
            self.assertEqual(cfg.includes, [root / "Core" / "Inc"])
            self.assertFalse(
                [w for w in cfg.warnings if "路径基准" in w],
                f"写法明确（../ 开头）时不该产出基准告警: {cfg.warnings}",
            )

    def test_absolute_path_kept_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "abs"
            root.mkdir(parents=True)
            foreign = Path(tmp) / "elsewhere" / "Inc"
            foreign.mkdir(parents=True)
            self.assertEqual(eclipse_cproject.convert_path(str(foreign), root, "Debug"), foreign)

    def test_ambiguous_notation_warns_when_both_candidates_exist(self):
        """写法判不出基准（既无工作区前缀, 也不以 ``../`` 开头）时:
        两个候选都存在 -> 必须告警说明选了哪个、另一个是什么。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_path_fixture(tmp, "amb", "Core/Inc")
            picked = root / "Debug" / "Core" / "Inc"
            other = root / "Core" / "Inc"

            collected: list[str] = []
            resolved = eclipse_cproject.convert_path("Core/Inc", root, "Debug", warnings=collected)
            self.assertEqual(resolved, picked)
            self.assertEqual(len(collected), 1, collected)
            self.assertIn("路径基准", collected[0])
            self.assertIn(str(picked), collected[0])
            self.assertIn(str(other), collected[0])

            cfg = eclipse_cproject.read(root, "Debug")
            self.assertEqual(cfg.includes, [picked])
            self.assertTrue(any("路径基准" in w for w in cfg.warnings), cfg.warnings)

    def test_ambiguous_notation_picks_only_existing_candidate_silently(self):
        """只有一个候选存在时不算歧义, 不该报警。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_path_fixture(tmp, "one", "Core/Inc", decoy_build=False)
            collected: list[str] = []
            resolved = eclipse_cproject.convert_path("Core/Inc", root, "Debug", warnings=collected)
            self.assertEqual(resolved, root / "Core" / "Inc")
            self.assertEqual(collected, [])


class TestMissingConfigIsNotMissingFile(unittest.TestCase):
    """M2 回归: .cproject **存在**但没有该配置名时, 不能报"找不到 .cproject"。

    两种原因的文案必须不同, 且"配置名不存在"要列出实际可用的配置名 + -Config 用法。
    """

    def test_read_raises_specific_error_listing_available_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project_with_configs(tmp, "cfgs", ["Debug", "Release"])
            with self.assertRaises(eclipse_cproject.CProjectConfigNotFound) as ctx:
                eclipse_cproject.read(root, "NoSuchConfig")
            exc = ctx.exception
            self.assertEqual(exc.available, ["Debug", "Release"])
            self.assertIn("Release", exc.message)
            self.assertIn("-Config", exc.message)
            # 仍然兼容 except ValueError 的老调用方
            self.assertIsInstance(exc, ValueError)

    def test_parse_error_is_a_different_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "broken"
            root.mkdir(parents=True)
            (root / ".cproject").write_text("<cproject><unclosed>", encoding="utf-8")
            with self.assertRaises(eclipse_cproject.CProjectParseError) as ctx:
                eclipse_cproject.read(root, "Debug")
            self.assertIn("解析失败", ctx.exception.message)

    def test_init_lists_available_configs_and_config_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project_with_configs(tmp, "init_cfg", ["Debug", "Release"])
            before = snapshot(root)
            with in_dir(root):
                code, output = cli_output(["init", "-Config", "NoSuchConfig", "-NoBuild"])
            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("Release", output)
            self.assertIn("-Config", output)
            self.assertIn("NoSuchConfig", output)
            # 关键: 不能把用户支去恢复一个其实存在的文件
            self.assertNotIn("找不到 .cproject", output)
            self.assertEqual(before, snapshot(root), "拒绝时必须零写入")

    def test_init_accepts_real_config_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project_with_configs(tmp, "init_ok", ["Debug", "Release"])
            with in_dir(root):
                code, output = cli_output(["init", "-Config", "Release", "-NoBuild"])
            self.assertEqual(code, int(Exit.OK), output)
            self.assertNotIn("配置 'Release' 不存在", output)

    def test_doctor_reports_missing_config_actionably(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_project_with_configs(tmp, "doctor_cfg", ["Debug", "Release"])
            # 有 .ioc 时不会额外冒出"有源码但缺芯片信息"那条兜底拒绝,
            # 这样断言的就是 M2 自己那条诊断
            (root / "doctor_cfg.ioc").write_text(
                "ProjectManager.DeviceId=STM32F103RCTx\n", encoding="utf-8"
            )
            with in_dir(root):
                code, output = cli_output(["doctor", "-Config", "NoSuchConfig"])
            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("确定性拒绝", output)
            self.assertIn("NoSuchConfig", output)
            self.assertIn("Release", output)
            self.assertIn("-Config", output)
            self.assertNotIn("找不到 .cproject", output)
            # warning 那一份也必须被打印出来（doctor 打印 warnings）
            self.assertIn(".cproject 不可用", output)

    def test_really_missing_cproject_still_says_not_found(self):
        """两种原因必须给不同文案: 真的没有 .cproject 时仍然报"找不到 .cproject"。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "no_cproject"
            (root / "Core" / "Src").mkdir(parents=True)
            (root / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
            with in_dir(root):
                code, output = cli_output(["init", "-Config", "NoSuchConfig", "-NoBuild"])
            self.assertEqual(code, int(Exit.PROJECT), output)
            self.assertIn("找不到 .cproject", output)


@unittest.skipUnless(has_project("demo") and has_project("freertos_hal_template"), "真实 STM32 工程不存在")
class TestRealCubeIdeProjectsHaveNoGaps(unittest.TestCase):
    """回归保护: 真实 CubeIDE 工程必须零"未识别选项" —— 否则说明映射表有洞。"""

    def test_no_unrecognized_options(self):
        for name in ("demo", "freertos_hal_template"):
            with self.subTest(project=name):
                cfg = eclipse_cproject.read(project_path(name), "Debug")
                gaps = [w for w in cfg.warnings if "未被识别" in w]
                self.assertEqual(gaps, [], f"{name} 出现未识别选项: {gaps}")

    def test_demo_include_and_ld_paths_unchanged(self):
        """M1 改了基准判定, 但已验证工程的解析结果必须逐条不变。

        这里写死 demo 的黄金路径集合（``../`` 相对配置目录 + ``${workspace_loc:}``
        相对工程根混用）, 就是为了防止"修一个静默错误又引入另一个"。
        """
        cfg = eclipse_cproject.read(project_path("demo"), "Debug")
        self.assertEqual(
            [p.relative_to(project_path("demo")).as_posix() for p in cfg.includes],
            [
                "Core/Inc",
                "Drivers/STM32F1xx_HAL_Driver/Inc/Legacy",
                "Drivers/STM32F1xx_HAL_Driver/Inc",
                "Drivers/CMSIS/Device/ST/STM32F1xx/Include",
                "Drivers/CMSIS/Include",
                "FreeRTOS/Inc",
                "App",
                "App/Drivers",
                "App/Tasks",
                "App/Tasks/common",
                "App/Tasks/key",
                "App/Tasks/lcd",
            ],
        )
        self.assertEqual(
            [p.relative_to(project_path("demo")).as_posix() for p in cfg.lib_dirs], ["App/Drivers"]
        )
        self.assertEqual(cfg.ld_script, project_path("demo") / "STM32F103RCTX_FLASH.ld")
        self.assertEqual(
            [p.relative_to(project_path("demo")).as_posix() for p in cfg.src_dirs],
            ["FreeRTOS", "App", "Core", "Drivers"],
        )
        self.assertEqual(cfg.warnings, [])


if __name__ == "__main__":
    unittest.main()
