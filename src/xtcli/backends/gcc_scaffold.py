"""生成物与运行时桩: config.mk / rules.mk / xtcli-Makefile / 桩 / compile_commands.json。

所有写入都经 :class:`xtcli.managed.Owned` —— 外来文件或被改过的文件先备份成
``*.xtcli-bak``, 只有本工具生成且未被改动的才直接覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import env, managed
from ..makefiles import config_mk
from ..model import ProjectModel
from .gcc_sources import collect_cpp_sources, collect_sources

# ===========================================================================
# init
# ===========================================================================
_XT_MAKEFILE = """# xtcli:generated
# =============================================================================
#  xtcli 生成的构建入口 (请勿手改; 重新生成请运行 stm32-init-pj)
#    构建:  stm32-build-pj       等价于  make -C <工程根> -f xtcli/Makefile
#    清理:  stm32-build-pj -Clean
#  路径相对工程根解析, 所以必须在工程根目录调用。
# =============================================================================
ifeq ($(notdir $(CURDIR)),xtcli)
$(error 请在工程根目录执行 make, 或直接使用 stm32-build-pj)
endif

XT_DIR := $(patsubst %/,%,$(dir $(lastword $(MAKEFILE_LIST))))

include $(XT_DIR)/config.mk
include $(XT_DIR)/rules.mk
"""

_ROOT_MAKEFILE = """# xtcli:generated
# 转发到 xtcli/Makefile, 让裸 `make` 也能用。
# 不需要就删掉本文件 —— stm32-build-pj 不依赖它。
include xtcli/Makefile
"""


def _write_skeleton(model: ProjectModel, owned: managed.Owned) -> dict[str, object]:
    xt_dir = model.root / "xtcli"
    xt_dir.mkdir(parents=True, exist_ok=True)
    config_path = xt_dir / "config.mk"
    overwrote = config_path.is_file()

    # 所有写入都走 owned.* —— 覆盖外来文件/被改过的文件前先备份成 *.xtcli-bak
    owned.write_text(config_path, config_mk.render(model))
    rules_dst = xt_dir / "rules.mk"
    owned.copy_file(env.assets_dir() / "rules.mk", rules_dst)
    owned.write_text(xt_dir / "Makefile", _XT_MAKEFILE)

    root_makefile = model.root / "Makefile"
    root_created = False
    if not root_makefile.is_file():
        owned.write_text(root_makefile, _ROOT_MAKEFILE)
        root_created = True

    return {
        "xt_dir": xt_dir,
        "config_mk": config_path,
        "rules_mk": rules_dst,
        "makefile": xt_dir / "Makefile",
        "root_makefile": root_created,
        "overwrote": overwrote,
    }


# GCC 运行时桩: 按**符号**判定是否补齐, 不按文件名。
#
# 历史教训 (实测): 原先只看 ``Core/Src/syscalls.c`` 在不在, 于是用户在
# ``retarget.c`` 里自己实现 ``_sbrk``/``_write`` (CubeMX 世界里的常见写法) 时
# 会被重复定义:
#     Core/Src/sysmem.c:54: multiple definition of `_sbrk';
#     build/Core__Src__retarget.o:... first defined here
# 一个本来能编译的工程被弄坏, 而且文件已经落进用户的源码树, 重跑 init 也
# 不会自愈 —— 用户只能手工删除。
#
# 现在的做法:
#   1) 先扫工程源文件里有没有这些符号, 有就一个都不补;
#   2) 补也只补到 ``xtcli/stubs/``, 不污染用户源码树 (git 干净, 可整体删除)。
_STUB_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("syscalls.c", ("_write", "_read", "_close", "_fstat", "_isatty", "_lseek")),
    ("sysmem.c", ("_sbrk",)),
)

# sysmem.c 实现的 ``_sbrk`` 依赖链接脚本提供 ``_estack`` / ``_Min_Stack_Size``
# (CubeMX 的约定)。非 ARM 工程 (RISC-V) 或自定义 .ld 常用别的符号名 (``_sp`` /
# ``__stack_top`` 之类), 补进去只会得到 "undefined reference to `_estack'" 且指向
# 一个我们写进去的文件 —— 所以**先看 .ld 有没有这些符号**, 没有就不补, 并说清楚原因。
_STUB_LD_SYMBOLS: dict[str, tuple[str, ...]] = {
    "sysmem.c": ("_estack",),
}


def _ld_provides(model: ProjectModel, name: str) -> bool:
    """链接脚本里是否定义了该 stub 依赖的符号 (只对声明了依赖的 stub 检查)。

    "还没有 .ld" ≠ "这个 .ld 里没有该符号": 前者无从判断, 一律按老行为(补)处理;
    只有**拿到 .ld 且确实找不到符号**时才不补 —— 那种情况下补进去必然链接失败。
    """
    needed = _STUB_LD_SYMBOLS.get(name)
    if not needed:
        return True
    if model.ld_script is None or not model.ld_script.is_file():
        return True
    try:
        text = model.ld_script.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return True
    return all(re.search(r"\b" + re.escape(symbol) + r"\b", text) for symbol in needed)


def _defines_symbol(sources: list[Path], symbol: str) -> bool:
    """粗判源文件里是否出现该符号的定义/声明。

    宁可误判成"已有"而跳过补齐 —— 漏补只会得到一条明确的
    ``undefined reference to `_sbrk'``, 而重复定义会把本来能编译的工程弄坏。
    """
    pattern = re.compile(r"^[^\n#;]*\b" + re.escape(symbol) + r"\s*\(", re.MULTILINE)
    for path in sources:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        if pattern.search(text):
            return True
    return False


def _wanted_stubs(model: ProjectModel) -> list[str]:
    """返回需要补齐的 stub 文件名 (工程里没有任何相关符号时才补)。"""
    sources = list(collect_sources(model)[0]) + collect_cpp_sources(model)
    wanted: list[str] = []
    for name, symbols in _STUB_FILES:
        if any(_defines_symbol(sources, symbol) for symbol in symbols):
            continue
        if not _ld_provides(model, name):
            ld_name = model.ld_script.name if model.ld_script else "缺少 .ld"
            model.warnings.append(
                f"未补齐 GCC 运行时桩 {name}: 它需要链接脚本定义 "
                f"{'/'.join(_STUB_LD_SYMBOLS[name])}, 而 {ld_name} 里没有这些符号 "
                f"(非 CubeMX 风格的 .ld 常见, 例如 RISC-V 工程用 _sp/__stack_top)。"
                f"若链接时报缺 {'/'.join(symbols)}, 请自行实现并放进源码树"
            )
            continue
        wanted.append(name)
    return wanted


def _copy_runtime_stubs(
    model: ProjectModel, owned: managed.Owned, disabled: bool = False
) -> list[Path]:
    """把 GCC 运行时桩补到 ``xtcli/stubs/`` (绝不写进用户源码树)。"""
    if disabled:
        return []
    stub_dir = model.root / "xtcli" / "stubs"
    copied: list[Path] = []
    for name in _wanted_stubs(model):
        dst = stub_dir / name
        src = env.assets_dir() / "stm32" / name
        if not dst.is_file():
            if not src.is_file():  # pragma: no cover
                continue
            stub_dir.mkdir(parents=True, exist_ok=True)
            owned.copy_file(src, dst)
        elif src.is_file():
            # 已存在: 内容仍是原样模板时继续登记归属 (登记表丢了也能恢复)
            try:
                if src.read_bytes() == dst.read_bytes():
                    owned.own_existing(dst)
            except OSError:  # pragma: no cover
                pass
        copied.append(dst)
    model.extra["stubs"] = list(copied)
    return copied


def _write_compile_commands(model: ProjectModel, tools, owned: managed.Owned) -> tuple[Path, int] | None:
    c_files, _ = collect_sources(model)
    cpp_files = collect_cpp_sources(model)
    # xtcli 补齐的桩也要进数据库, 否则 clangd 打开 xtcli/stubs/syscalls.c 会满屏红
    c_files = list(c_files) + list(model.extra.get("stubs") or [])
    if (not c_files and not cpp_files) or not tools.cc:
        return None
    cflags: list[str] = []
    if model.cpu:
        cflags.append(f"-mcpu={model.cpu}")
    if model.arch:
        # 非 ARM (RISC-V): 用 -march/-mabi, **绝不能**带 -mthumb —— riscv gcc 会直接报错,
        # clangd 也会满屏红。这里的旗标必须与 config.mk 里 ARCH 的含义一致。
        cflags.append(f"-march={model.arch}")
        if model.abi:
            cflags.append(f"-mabi={model.abi}")
    else:
        cflags.append("-mthumb")
    if model.fpu:
        cflags.extend(part for part in model.fpu.split() if part)
    cflags.extend(f"-D{d}" for d in model.defines if d)
    cflags.extend(f"-I{env.relative_to(model.root, p)}" for p in model.includes if p)
    if model.opt:
        cflags.append(model.opt)
    if model.dbg:
        cflags.append(model.dbg)

    import json

    cxx = tools.cxx or tools.cc
    pairs = [(path, tools.cc) for path in c_files] + [(path, cxx) for path in cpp_files]
    entries = []
    for path, compiler in pairs:
        entries.append({
            "directory": env.to_posix(model.root),
            "file": env.to_posix(path),
            "arguments": [
                env.to_posix(compiler),
                *cflags,
                "-c",
                env.to_posix(path),
                "-o",
                env.to_posix(path.with_suffix(".o")),
            ],
        })
    out = model.root / "compile_commands.json"
    # 走 owned: 这个文件可能是 CMake/bear/compiledb 的产物, 覆盖前必须备份+告警
    owned.write_text(out, json.dumps(entries, ensure_ascii=False, indent=2))
    return out, len(entries)


