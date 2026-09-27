"""测试公共设施。

设计约束:
* 测试用 ``unittest.TestCase`` 写 —— 保证 ``python -m unittest discover`` 在
  **零第三方安装**下就能跑（pytest 装了也能跑）。
* 抽取类测试跑在**真实工程目录**上（只读）; 构建类测试先把工程**复制到临时目录**
  再操作, 绝不改动用户的工程目录。
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PROJECTS = Path(r"E:\Code_workspace\freertos_workspace\software")

# 三个参考工程的黄金值（text/data/bss）, 1.0.0 重新记录后的基线。
#
# 【2026-09-27 更新: 全局开关成套化】
# 旧 rules.mk 把 --specs=nano.specs **只**放在 LDFLAGS: 链接的是 libc_nano
# (struct _reent = 76 B), 编译却按标准 newlib 头 (512 B)。改成编译+链接成套注入后:
#   * .text 变小 (sizeof(TCB_t) 的立即数/memset 变小): demo -104, 模板 -88;
#   * **.bss/.data 一个字节都没变** —— FreeRTOS 堆是定长数组 (ucHeap[4096]),
#     TCB 从 616 B 回到 180 B 在这里完全看不出来。这就是"体积一致证明不了
#     ABI/内存一致"的直接证据, 也正是必须加 ABI 哨兵的原因
#     (见 backends/gcc_common.check_abi_consistency 与 tests/test_flag_contract.py);
#   * 1_LED 不用 FreeRTOS, 体积逐字节不变。
GOLDEN_SIZE = {
    "demo": (87972, 124, 25928),
    "freertos_hal_template": (16816, 96, 6488),
    "1_LED": (5088, 12, 1068),
}

_IGNORE = shutil.ignore_patterns(
    "build", "Debug", "build-ord", "Debug-ord", "Debug-cubeide-ref", ".metadata", "*.o", "*.d"
)


def project_path(name: str) -> Path:
    return PROJECTS / name


def has_project(name: str) -> bool:
    path = project_path(name)
    return path.is_dir() and any(path.glob("*.ioc"))


@contextlib.contextmanager
def in_dir(path: Path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


@contextlib.contextmanager
def captured():
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


def run_cli(argv: list[str]) -> int:
    """在进程内跑 CLI（走真实的 argv → 分发 → 退出码路径）。"""
    from xtcli import cli

    return cli.main(list(argv))


def cli_output(argv: list[str]) -> tuple[int, str]:
    with captured() as buffer:
        code = run_cli(argv)
    return code, buffer.getvalue()


def copy_project(name: str, dest: Path) -> Path:
    """把工程复制到临时目录（排除构建产物, 强制真实重编）。"""
    target = dest / name
    shutil.copytree(project_path(name), target, ignore=_IGNORE)
    return target


def make_fixture(dest: Path, kind: str) -> Path:
    """造拒绝用例 fixture（自包含, 不依赖仓库外的 .selftest 目录）。"""
    root = dest / kind
    if kind == "ioc-only":
        root.mkdir(parents=True)
        (root / "test.ioc").write_text(
            "ProjectManager.DeviceId=STM32F103RCTx\n"
            "ProjectManager.ProjectName=fixture\n"
            "ProjectManager.TargetToolchain=STM32CubeIDE\n",
            encoding="utf-8",
        )
    elif kind == "not-a-project":
        root.mkdir(parents=True)
        (root / "readme.txt").write_text("not a project\n", encoding="utf-8")
    elif kind == "no-chip":
        (root / "Core" / "Src").mkdir(parents=True)
        (root / "Core" / "Src" / "main.c").write_text("int main(void){return 0;}\n", encoding="utf-8")
    else:  # pragma: no cover
        raise ValueError(kind)
    return root


def snapshot(root: Path) -> set[str]:
    """目录树快照（相对路径集合）, 用于断言"拒绝时零写入"。"""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def elf_size(tools, elf: Path) -> tuple[int, int, int]:
    from xtcli import exec as xt_exec

    result = xt_exec.run([tools.size, str(elf)], echo=False)
    for line in reversed(result.lines):
        parts = line.split()
        if len(parts) >= 3 and all(p.isdigit() for p in parts[:3]):
            return int(parts[0]), int(parts[1]), int(parts[2])
    raise AssertionError(f"无法解析 size 输出: {result.lines}")


# ---------------------------------------------------------------------------
# 最小可构建工程 fixture
#
# 用途: 验证"源文件收集 / 编译规则"这类问题, 必须真的跑一次 arm-none-eabi
# 构建 —— 只做文本断言抓不到 `patsubst %.s` 匹配不上 `.S` 这类的 make 层问题。
#
# 它是**自造**的最小工程 (自写 .ld + 启动文件 + .cproject), 因此:
#   * 不依赖仓库外的任何真实工程;
#   * 结论只说明"构建规则/源收集"这一层, 不代表真实厂商工程能被解析
#     (那一层由 test_extract / test_eclipse_family 覆盖)。
# .ld 里保留了 .ARM.exidx/.ARM.extab —— C++ 的展开库需要 __exidx_start,
# CubeMX 生成的 .ld 本来就有这两段。
# ---------------------------------------------------------------------------
_MIN_LD = """ENTRY(Reset_Handler)
_estack = 0x20005000;
MEMORY
{
  RAM (xrw)   : ORIGIN = 0x20000000, LENGTH = 20K
  FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 256K
}
SECTIONS
{
  .isr_vector : { KEEP(*(.isr_vector)) } >FLASH
  .text : { *(.text*) *(.rodata*) } >FLASH
  .ARM.extab : { *(.ARM.extab* .gnu.linkonce.armextab.*) } >FLASH
  .ARM : { __exidx_start = .; *(.ARM.exidx*) __exidx_end = .; } >FLASH
  _sidata = LOADADDR(.data);
  .data : { _sdata = .; *(.data*) _edata = .; } >RAM AT> FLASH
  .bss  : { _sbss = .; *(.bss*) *(COMMON) _ebss = .; } >RAM
}
"""

_MIN_STARTUP = """  .syntax unified
  .cpu cortex-m3
  .thumb
  .global g_pfnVectors
  .global Reset_Handler
  .section .isr_vector,"a",%progbits
g_pfnVectors:
  .word _estack
  .word Reset_Handler
  .section .text.Reset_Handler,"ax",%progbits
  .thumb_func
Reset_Handler:
  bl SystemInit
  bl main
1: b 1b
"""

_MIN_CPROJECT = """<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<cproject storage_type_id="org.eclipse.cdt.core.XmlProjectDescriptionStorage">
 <storageModule moduleId="org.eclipse.cdt.core.settings">
  <cconfiguration id="com.st.stm32cube.ide.mcu.gnu.managedbuild.config.exe.debug.1">
   <storageModule buildSystemId="org.eclipse.cdt.managedbuilder.core.configurationDataProvider" id="c1" moduleId="org.eclipse.cdt.core.settings" name="Debug"/>
   <storageModule moduleId="cdtBuildSystem" version="4.0.0">
    <configuration artifactExtension="elf" artifactName="${ProjName}" name="Debug">
     <folderInfo id="f1" name="/" resourcePath="">
      <toolChain id="t1" name="MCU ARM GCC" superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.toolchain.exe.debug">
       <option id="o1" superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.option.target_mcu" value="STM32F103RCTx" valueType="string"/>
       <tool id="t2" name="c" superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.tool.c.compiler">
        <option id="o2" superClass="com.st.stm32cube.ide.mcu.gnu.managedbuild.tool.c.compiler.option.includepaths" valueType="includePath">
         <listOptionValue builtIn="false" value="../Core/Inc"/>
        </option>
       </tool>
      </toolChain>
     </folderInfo>
     <sourceEntries><entry kind="sourcePath" name="Core"/></sourceEntries>
    </configuration>
   </storageModule>
  </cconfiguration>
 </storageModule>
</cproject>
"""

MAIN_C = "void SystemInit(void){}\nint main(void){return 0;}\n"
# SystemInit 必须 extern "C" —— 否则 C++ 会名字修饰, 与启动文件里的符号对不上
MAIN_CPP = (
    'extern "C" void SystemInit(void);\n'
    'extern "C" int cpp_helper(void);\n'
    'extern "C" void SystemInit(void) {}\n'
    "int main(void) { return cpp_helper(); }\n"
)
HELPER_CPP = 'extern "C" int cpp_helper(void){return 1;}\n'


def make_min_project(dest: Path, files: dict[str, str], startup_suffix: str = ".s") -> Path:
    """造一个最小可构建工程; ``files`` 是相对工程根的 路径 -> 内容。"""
    root = dest / "minproj"
    for sub in ("Core/Inc", "Core/Src", "Core/Startup"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / "Core" / "Startup" / f"startup_stm32f103rctx{startup_suffix}").write_text(
        _MIN_STARTUP, encoding="utf-8"
    )
    (root / "STM32F103RCTX_FLASH.ld").write_text(_MIN_LD, encoding="utf-8")
    (root / ".cproject").write_text(_MIN_CPROJECT, encoding="utf-8")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def min_elf(root: Path) -> Path:
    return root / "Debug" / f"{root.name}.elf"


def toolchain_ready() -> bool:
    """arm-none-eabi 工具链 + make 都在才跑真编译用例。"""
    from xtcli import discovery

    with captured():
        tools = discovery.discover()
    return tools.gcc_path is not None and tools.make is not None
