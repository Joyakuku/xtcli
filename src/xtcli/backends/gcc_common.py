"""构建与烧录共用的小工具 (make 计划 / 产物新鲜度 / 拒绝结果)。

放这里是为了让 gcc_make 与 gcc_burn 都能用, 而**不必互相导入** (会成环)。
"""

from __future__ import annotations

import contextlib
import re
import time
from pathlib import Path

from .. import exec, log
from ..errors import Exit, Result
from ..makefiles import config_mk
from ..model import ProjectModel
from .gcc_sources import collect_cpp_sources, collect_sources


# ===========================================================================
# build
# ===========================================================================
def _make_plan(model: ProjectModel) -> tuple[str, bool] | None:
    if (model.root / "xtcli" / "Makefile").is_file():
        return "xtcli/Makefile", True
    if (model.root / "Makefile").is_file():
        return "Makefile", False
    return None


def _check_fingerprint(model: ProjectModel) -> None:
    config_path = model.root / "xtcli" / "config.mk"
    if not config_path.is_file():
        return
    found = config_mk.read_fingerprint(config_path)
    expected = config_mk.fingerprint(model)
    if found is None:
        # v1.0.0 起 config.mk 一定带指纹: 没有指纹说明它不是本版本生成的 (外来文件或
        # 历史版本的残留), 必须重新生成, 否则参数可能与工程脱节。
        log.warn("xtcli/config.mk 没有指纹 (不是本版本生成的), 请重新运行 xtcli-init")
    elif found != expected:
        log.warn("xtcli/config.mk 与工程当前配置不一致 (陈旧或被改过)")
        log.info(f"  文件指纹 {found}   当前 {expected}")
        log.info("  建议先运行 xtcli-init 重新生成, 否则可能编出与预期不符的固件")


def _artifact_path(model: ProjectModel, ext: str) -> Path:
    return model.root / model.out_dir / f"{model.target}.{ext}"


def _newest_input_mtime(model: ProjectModel) -> float:
    """工程里"决定固件"的最新输入时间 (源文件 / 头文件 / 链接脚本)。"""
    newest = 0.0
    for path in list(collect_sources(model)[0]) + list(collect_sources(model)[1]) + collect_cpp_sources(model):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    if model.ld_script and model.ld_script.is_file():
        with contextlib.suppress(OSError):
            newest = max(newest, model.ld_script.stat().st_mtime)
    return newest


def artifact_is_stale(model: ProjectModel, artifact: Path) -> bool:
    """固件是否比它的输入旧 (= 改了代码却没重新构建)。"""
    if not artifact.is_file():
        return False
    try:
        artifact_mtime = artifact.stat().st_mtime
    except OSError:  # pragma: no cover
        return False
    newest = _newest_input_mtime(model)
    return bool(newest) and artifact_mtime < newest


def _check_artifact_fresh(model: ProjectModel, artifact: Path) -> None:
    """make 成功但产物缺失/比最新输入旧 → 告警, 防"假成功"。"""
    if not artifact.is_file():
        log.warn(f"构建报告成功, 但产物不存在: {artifact}")
        return
    if artifact_is_stale(model, artifact):
        log.warn("产物比源文件旧 —— 可能没有真正重新编译 (检查 make 是否因陈旧目标而跳过)")


# ---------------------------------------------------------------------------
# 陈旧对象：参数变了但 .o 没重编
# ---------------------------------------------------------------------------
_PARAM_FILES = ("config.mk", "rules.mk")
_CLOCK_SKEW_SECONDS = 2.0


def _object_paths(model: ProjectModel) -> set[Path]:
    """本工程源文件对应的对象路径。

    命名跟着 ``rules.mk`` 的 ``mangle`` 走 (``/`` → ``__``, 去掉扩展名), 这样只会
    覆盖"由源文件编出来的"对象 —— ``build/abi-probe.o`` 这类非固件对象不参与。
    """
    build = model.root / model.build_dir
    c_files, s_files = collect_sources(model)
    paths: set[Path] = set()
    for path in [*c_files, *s_files, *collect_cpp_sources(model)]:
        try:
            relative = path.relative_to(model.root).with_suffix("").as_posix()
        except ValueError:  # 工程外的源文件 (理论上不该有)
            continue
        paths.add(build / f"{relative.replace('/', '__')}.o")
    return paths


def stale_objects(model: ProjectModel) -> list[Path]:
    """``build/`` 里比参数文件 (``config.mk`` / ``rules.mk``) 更旧的对象。

    实测缺口: 重新 init 换上成套开关后直接 build, make 认为旧 ``.o`` 仍是最新的,
    于是**只重链接** —— 固件里混着旧参数编出来的对象, 而新编译的 ABI 探针只能证明
    "当前参数自洽", 证明不了"固件里每个对象都是当前参数编的"。
    """
    build = model.root / model.build_dir
    if not build.is_dir():
        return []
    newest = 0.0
    for name in _PARAM_FILES:
        with contextlib.suppress(OSError):
            newest = max(newest, (model.root / "xtcli" / name).stat().st_mtime)
    if not newest:
        return []
    if newest > time.time() + _CLOCK_SKEW_SECONDS:
        log.info("xtcli/config.mk 的时间戳在未来 (时钟偏移/解压恢复) —— 跳过陈旧对象检查")
        return []
    stale: list[Path] = []
    for path in sorted(_object_paths(model)):
        try:
            if path.stat().st_mtime < newest:
                stale.append(path)
        except OSError:  # pragma: no cover
            continue
    return stale


# ---------------------------------------------------------------------------
# 产物归属 / ABI 一致性
# ---------------------------------------------------------------------------
_ARTIFACT_SUFFIXES = (".elf", ".hex", ".bin", ".map", ".lst", ".lst")

_LD_MAP_SECTION_RE = re.compile(r"^\s*\.(?:data|bss)\.([A-Za-z_]\w*)\s*$")
_LD_MAP_SIZE_RE = re.compile(r"^\s*(0x[0-9a-fA-F]+)\s+(0x[0-9a-fA-F]+)\s+(\S.*)$")

ABI_SENTINEL_SYMBOL = "_impure_data"


def foreign_artifacts(model: ProjectModel) -> list[Path]:
    """``out_dir`` 里不属于本 target 的固件产物 (工程改名后残留的旧产物)。

    实测困惑: 同一目录里同时躺着两套 ``*.elf/.bin/.hex``, 烧的到底是哪一份说不清。
    """
    out_dir = model.root / model.out_dir
    if not out_dir.is_dir():
        return []
    own = {f"{model.target}{suffix}" for suffix in _ARTIFACT_SUFFIXES}
    return sorted(
        path
        for path in out_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _ARTIFACT_SUFFIXES and path.name not in own
    )


def library_object_sizes(map_path: Path) -> dict[str, tuple[int, str]]:
    """从 GNU ld 的 ``.map`` 里抓 ``.<section>.<符号>`` 的实际大小 -> (字节数, 归属文件)。

    ``address == 0`` 的条目是 ``--gc-sections`` 丢弃的段, 必须跳过 —— 它们会顶着
    同样的符号名出现。
    """
    sizes: dict[str, tuple[int, str]] = {}
    try:
        lines = map_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return sizes
    for index, line in enumerate(lines[:-1]):
        section = _LD_MAP_SECTION_RE.match(line)
        if not section:
            continue
        size_match = _LD_MAP_SIZE_RE.match(lines[index + 1])
        if not size_match:
            continue
        address = int(size_match.group(1), 16)
        size = int(size_match.group(2), 16)
        if address == 0 or size == 0:
            continue
        sizes[section.group(1)] = (size, size_match.group(3).strip())
    return sizes


def check_abi_consistency(
    model: ProjectModel,
    tools,
    *,
    make_args: list[str],
    path_prefix: list[str],
) -> Result | None:
    """ABI 哨兵: 编译期看到的库结构体大小必须等于链接进来的库对象大小。

    为什么必须单独做这条闸门: "黄金体积"证明不了 ABI 一致 —— FreeRTOS 的堆是
    ``.bss`` 里的定长数组 (``ucHeap[configTOTAL_HEAP_SIZE]``), 每个 TCB 多背几百
    字节**不会改变任何 section**, 于是体积比对全绿而固件根本跑不起来 (实测事故:
    编译按标准 newlib 头 512 B, 链接 libc_nano 76 B, 4 个任务的堆需求从 3.0 KB
    涨到 4.8 KB, 顶爆 4096 B 的静态堆 → ``vTaskStartScheduler`` 死在 configASSERT)。

    返回 ``None`` 表示通过/跳过; 返回 ``Result`` 表示必须中止构建。
    """
    elf = _artifact_path(model, "elf")
    map_path = _artifact_path(model, "map")
    if not elf.is_file() or not map_path.is_file():
        # 找不到"本工程名"的产物时必须说清楚: 产物名跟着**目录名**走, 而工程可能被
        # 改过名 (config.mk 里的 TARGET 还是旧名) —— 那时哨兵会静默跳过, 等于没守。
        others = foreign_artifacts(model)
        if others:
            log.warn(f"没找到本工程名的产物 {elf.name}, 但 {model.out_dir}/ 里有: "
                     + ", ".join(path.name for path in others[:3]))
            log.info("  通常是工程目录被改名了 (产物名跟目录名走): 重新运行 xtcli-init 同步;")
            log.info("  在此之前 ABI 哨兵与产物新鲜度检查都会跳过 —— 等于没有守。")
        return None
    stale = stale_objects(model)
    if stale:
        shown = ", ".join(path.name for path in stale[:5])
        more = f" 等 {len(stale)} 个" if len(stale) > 5 else ""
        log.err("构建产物里有比参数文件更旧的对象 —— 它们是用**旧参数**编的")
        log.info(f"  {shown}{more}  (参数文件: xtcli/config.mk / xtcli/rules.mk)")
        log.info("  含义: 固件里混着两套编译参数 (典型表现: TCB 仍按旧 ABI 布局), 而新")
        log.info("        编译的探针看不出这一点 —— 所以这里单独判失败。")
        log.info("  修法: xtcli-build -Clean。rules.mk 已把参数文件设为所有对象的")
        log.info("        前置依赖, 正常情况下重新 init 之后的 build 会自动全量重编。")
        return Result(code=int(Exit.BUILD), message="构建产物与当前参数不一致")
    entry = library_object_sizes(map_path).get(ABI_SENTINEL_SYMBOL)
    if entry is None:
        log.info("ABI 哨兵: 链接产物里没有 newlib reent 对象, 跳过")
        return None
    expected, origin = entry
    result = exec.run(
        [tools.make, *make_args, "abi-check", f"XT_ABI_EXPECT_REENT={expected}"],
        cwd=model.root,
        prepend_path=path_prefix,
        echo=False,
    )
    if result.exit_code == 0:
        log.ok(f"ABI 一致: sizeof(struct _reent) == {ABI_SENTINEL_SYMBOL} ({expected} B)")
        return None
    log.err("ABI 哨兵失败: 编译期看到的库结构 与 链接进来的库对象 不是同一套")
    log.info(f"  链接产物: {ABI_SENTINEL_SYMBOL} = {expected} B   ({origin})")
    log.info("  含义: 编译用了 A 套头文件、链接用了 B 套库。任何通过 _impure_ptr 交换")
    log.info("        指针的 libc 调用都会按错误偏移读写; 若 TCB 内嵌了 struct _reent")
    log.info("        (configUSE_NEWLIB_REENTRANT=1), 还会白吃几百字节 RAM。")
    log.info("  修法: 让全局开关成套 —— 看 `make show-flags`, 确认 SPECS 同时出现在")
    log.info("        CFLAGS/CXXFLAGS/ASFLAGS/LDFLAGS (当前 config.mk: "
             f"{config_mk.effective_abi_switches(model)})")
    for line in [item for item in result.lines if item.strip()][-3:]:
        log.info(f"  {line}")
    return Result(code=int(Exit.BUILD), message="ABI 一致性校验失败")


def _refusal_result(model: ProjectModel) -> Result | None:
    """有确定性拒绝时, 任何动作都应报告它, 而不是报"缺少构建入口"之类的次要原因。"""
    if not model.refusals:
        return None
    first = model.refusals[0]
    log.err(first.message)
    for item in first.missing:
        log.info(f"缺少: {item}")
    if first.next_step:
        log.info(f"建议: {first.next_step}")
    return Result(code=first.code, message=first.message)


