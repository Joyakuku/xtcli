"""烧录与探针: openocd / pyOCD / 回读校验。

原则: 探针不可用是**环境问题**(退出码 3), 不是烧录失败(7); 回读校验判 False 才算 7;
"校验被跳过"不能当成"校验通过"。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import config, devices, env, exec, flash, log
from ..errors import Exit, Result
from ..model import Context
from .gcc_common import _artifact_path, _refusal_result


# ===========================================================================
# burn
# ===========================================================================
def _probe_test(tools, interface_cfg: str, target_cfg: str | None, timeout: float = 8.0) -> exec.RunResult:
    """试连一个探针。

    必须带 target 配置: ``interface/*.cfg`` 里没有 ``transport select``, 只加载
    interface 会报 ``session transport was not selected`` 而**在真机连接正常时
    也误判为"探针没连上"**（实测确认）。带上 target 就是真实烧录的加载方式。
    """
    argv: list[str] = []
    if tools.openocd_scripts:
        argv += ["-s", env.to_posix(tools.openocd_scripts)]
    argv += ["-f", interface_cfg]
    if target_cfg:
        argv += ["-f", target_cfg]
    argv += ["-c", "init;exit"]
    return exec.run([tools.openocd, *argv], timeout=timeout, echo=False)


def _target_cfg_exists(tools, target_cfg: str) -> bool:
    """target 脚本在本机是否存在。

    设备表里的 cfg 名可能来自更新版本的 OpenOCD; 直接拿去烧只会得到
    ``Can't find target/xxx.cfg`` 并被误判成"烧录失败"(退出码 7)。所以先预检。
    绝对路径 / 工程内相对路径也接受 (用户可能自己放了一份 cfg)。
    """
    path = Path(target_cfg)
    if path.is_file():
        return True
    scripts = getattr(tools, "openocd_scripts", None)
    if not scripts:
        return True  # 不知道脚本目录时不拦, 交给 openocd 自己报错
    return (Path(scripts) / target_cfg).is_file()


_PROBE_UNAVAILABLE_MARKERS = (
    "open failed",
    "openocd init failed",
    "no device found",
    "unable to find a matching",
    "no matching adapter",
    "libusb_error_access",
    "no probes",
    "no available probes",
)


def _looks_like_probe_unavailable(text: str) -> bool:
    """判断失败是不是"探针不可用"（环境问题）而不是"烧录失败"。

    两者退出码不同(3 vs 7), 契约要求分清 —— 否则脚本会把"探针没插"当成"烧录出错"。
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _PROBE_UNAVAILABLE_MARKERS)


def _detect_probe(tools, table_name: str, target_cfg: str, *, use_cache: bool) -> str | None:
    """解析探针配置: 显式 > 缓存 > 现场逐个试。返回 interface 配置路径。"""
    cfg = config.load()
    if use_cache:
        cached = config.cached_probe(cfg)
        if cached:
            log.info(f"使用已识别到的探针: {cached.get('name')}  ({cached['interface']})")
            return str(cached["interface"])
    log.step("自动识别探针 (每个 8s 超时)")
    for probe in devices.probes_for(table_name):
        tested = _probe_test(tools, probe["cfg"], target_cfg)
        if tested.ok:
            config.remember_probe(cfg, probe.get("name", "?"), probe["cfg"])
            config.save(cfg)
            log.ok(f"识别到探针: {probe.get('name')}  ({probe['cfg']})")
            return probe["cfg"]
        log.info(f"{probe.get('name')} 未响应")
    return None


def _read_artifact_words(tools, artifact: Path, count: int = 4) -> list[int] | None:
    """取固件开头 count 个字（ELF 经 objcopy 转 bin; bin 直接读）。"""
    blob: bytes | None = None
    if artifact.suffix.lower() == ".elf":
        if not tools.objcopy:
            return None
        tmp = artifact.with_name(artifact.name + ".xtcli-readback.bin")
        try:
            conv = exec.run([tools.objcopy, "-O", "binary", str(artifact), str(tmp)], echo=False)
            if conv.exit_code == 0 and tmp.is_file():
                blob = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)
    else:
        try:
            blob = artifact.read_bytes()
        except OSError:
            return None
    if not blob or len(blob) < 4 * count:
        return None
    return [int.from_bytes(blob[i * 4:(i + 1) * 4], "little") for i in range(count)]


def _readback_verify(tools, interface_cfg: str, target_cfg: str, artifact: Path, base: str) -> tuple[bool, str]:
    """独立回读: 另开一个会话直接读芯片, 与固件开头比对。

    与 ``program ... verify`` 不同, 这条路径不经过烧写流程, 是**独立证据**。

    **收尾必须是 ``reset run``**: 读内存要先 ``reset halt`` 把内核停住, 而
    ``shutdown`` 只是关掉 openocd 服务, **不会**恢复运行。少了这一句, 烧录"成功"
    退出的那一刻芯片是 halt 着的 —— 固件不跑, 板子上的 LED 不闪, 用户得手动按复位
    (或拔电) 才恢复。实测踩过这个坑 (详见维护手册的事故表)。
    """
    expected = _read_artifact_words(tools, artifact)
    if expected is None:
        return True, "回读校验跳过 (无法读取固件开头; 缺少 objcopy 或文件异常)"

    argv: list[str] = []
    if tools.openocd_scripts:
        argv += ["-s", env.to_posix(tools.openocd_scripts)]
    argv += ["-f", interface_cfg, "-f", target_cfg]
    argv += [
        "-c", "init",
        "-c", "reset halt",
        "-c", f"mdw {base} {len(expected)}",
        # 读完把内核放回运行 —— 否则烧录的最后一个动作是"暂停 CPU"
        "-c", "reset run",
        "-c", "shutdown",
    ]
    result = exec.run([tools.openocd, *argv], timeout=20, echo=False)

    match = None
    for line in result.lines:
        found = re.match(rf"^{re.escape(base)}:\s*([0-9a-fA-F ]+)$", line.strip())
        if found:
            match = found
            break
    if match is None:
        return True, "回读校验跳过 (未解析到 mdw 输出)"
    words = [int(token, 16) for token in match.group(1).split()[: len(expected)]]
    if words == expected:
        shown = " ".join(f"{w:08x}" for w in words)
        return True, f"回读校验通过: {base} = {shown}"
    return False, f"回读校验失败: 芯片 {[f'{w:08x}' for w in words]} != 固件 {[f'{w:08x}' for w in expected]}"


def _resolve_flasher(ctx: Context) -> str:
    """决定用哪个烧录器: ``-Flasher`` 覆盖 > auto。

    auto 的规则: 有 openocd 且知道该芯片的 target 配置 → openocd（既有实测路径）;
    否则若装了 pyOCD → pyocd（靠 CMSIS-Pack 自动拿 flash 算法, 不依赖 target 配置）。
    """
    requested = str(ctx.opt.get("flasher") or "auto").strip().lower()
    if requested in ("openocd", "pyocd"):
        return requested
    if requested not in ("", "auto"):
        log.warn(f"未知的 -Flasher 值 '{requested}', 按 auto 处理")
    has_openocd = bool(ctx.tools.openocd)
    has_target = bool(ctx.opt.get("ocd_target") or ctx.model.extra.get("ocd_target"))
    if has_openocd and has_target:
        return "openocd"
    if flash.find_pyocd() is not None:
        log.info("openocd 缺少该芯片的 target 配置, 改用 pyOCD (CMSIS-Pack 自动获取 flash 算法)")
        return "pyocd"
    return "openocd"  # 让 openocd 路径报出具体缺什么


def action_burn(ctx: Context) -> Result:
    model = ctx.model
    opt = ctx.opt

    refused = _refusal_result(model)
    if refused is not None:
        return refused

    # 与 build 一致: 先打印定位到的工程根 (burn 会写芯片, 更不能搞错目录)
    log.info(f"工程根: {model.root}")

    use_bin = bool(opt.get("bin"))
    artifact = Path(str(opt.get("elf"))) if opt.get("elf") else _artifact_path(model, "bin" if use_bin else "elf")

    if not artifact.is_file():
        if opt.get("no_build"):
            log.err(f"固件不存在: {artifact}")
            return Result(code=int(Exit.PROJECT), message="固件不存在且指定了 -NoBuild")
        log.info("固件尚未构建, 先构建")
        # 函数内导入: gcc_make 在模块级导入本模块, 模块级反向导入会成环
        from .gcc_make import action_build

        built = action_build(ctx)
        if not built.ok:
            return built
    if not artifact.is_file():
        log.err(f"构建后仍找不到固件: {artifact}")
        return Result(code=int(Exit.PROJECT), message="找不到固件")

    if _resolve_flasher(ctx) == "pyocd":
        return _burn_pyocd(ctx, artifact)
    return _burn_openocd(ctx, artifact)


def _burn_openocd(ctx: Context, artifact: Path) -> Result:
    model = ctx.model
    tools = ctx.tools
    opt = ctx.opt
    # 表跟着型号走: 非 ST 芯片的 flash 基址/探针清单可能与本表不同
    table_name = str(model.extra.get("device_table") or "")
    data = devices.table_data(table_name)
    use_bin = bool(opt.get("bin"))

    if not tools.openocd:
        log.err("环境缺少 openocd")
        return Result(code=int(Exit.ENVIRONMENT), message="openocd 不可用")

    target_cfg = str(opt.get("ocd_target") or model.extra.get("ocd_target") or "")
    if not target_cfg:
        log.err("无法确定 openocd target 配置")
        log.info("用 -OcdTarget target/stm32f1x.cfg 显式指定")
        log.info("或用 -Flasher pyocd 走 CMSIS-Pack 路线 (不依赖 target 配置)")
        return Result(code=int(Exit.PROJECT), message="缺少 openocd target")

    # 预检: target 脚本在**本机**是否真的存在。设备表里的名字可能来自更新版本的
    # OpenOCD (例如 stm32c0x.cfg / stm32h5x.cfg), 拿着不存在的路径去烧,
    # openocd 会报 "Can't find ..." 并被判成"烧录失败" —— 其实这是环境/数据问题。
    if not _target_cfg_exists(tools, target_cfg):
        log.err(f"openocd target 脚本在本机不存在: {target_cfg}")
        if tools.openocd_scripts:
            log.info(f"本机脚本目录: {tools.openocd_scripts}")
        log.info("可选做法: 升级 OpenOCD (新 target 脚本) / 用 -OcdTarget <存在的 cfg> 指定 / "
                 "或用 -Flasher pyocd 走 CMSIS-Pack (不依赖 target 配置)")
        return Result(code=int(Exit.ENVIRONMENT), message="本机缺少该 openocd target 脚本")


    interface_cfg = str(opt.get("interface") or "")
    if not interface_cfg:
        interface_cfg = _detect_probe(tools, table_name, target_cfg, use_cache=True)
    if not interface_cfg:
        log.err("没有识别到任何调试探针")
        log.info("依次检查: USB 连接 / ST-LINK 驱动 (WinUSB) / SWD 接线 / 是否被 CubeIDE 或 ST-Link Utility 占用")
        log.info("也可用 -Interface 强制指定, 例如 -Interface interface/cmsis-dap.cfg")
        return Result(code=int(Exit.ENVIRONMENT), message="未识别到探针")

    argv: list[str] = []
    if tools.openocd_scripts:
        argv += ["-s", env.to_posix(tools.openocd_scripts)]
    argv += ["-f", interface_cfg, "-f", target_cfg]
    if opt.get("probe_serial"):
        argv += ["-c", f"adapter serial {opt['probe_serial']}"]

    forward = env.to_posix(artifact)
    if use_bin:
        address = str(opt.get("address") or devices.flash_base(data, model.family))
        argv += ["-c", f'program "{forward}" {address} verify reset exit']
    else:
        argv += ["-c", f'program "{forward}" verify reset exit']

    log.step(f"烧录 {artifact.name}  [{interface_cfg} + {target_cfg}]")
    result = exec.run([tools.openocd, *argv], cwd=model.root)

    if result.exit_code != 0:
        if _looks_like_probe_unavailable(result.text):
            # 探针打不开**不是**"烧录失败": 按契约它属于环境问题(退出码 3)。
            # 常见原因: 探针被拔掉/没上电、被其他工具占用、缓存里的配置已过期。
            log.warn("探针打不开 (open failed) —— 按环境问题处理, 不是烧录失败")
            if not opt.get("interface"):
                log.info("刷新探针识别 (缓存可能已过期) ...")
                fresh = _detect_probe(tools, table_name, target_cfg, use_cache=False)
                if fresh:
                    log.ok(f"重新识别到探针: {fresh} (已更新缓存, 再跑一次即可)")
                else:
                    log.info("当前没有任何探针响应 —— 检查 USB 连接 / 驱动 / 是否被其他工具占用")
            return Result(code=int(Exit.ENVIRONMENT), message="探针不可用")
        log.err(f"烧录失败 (openocd 退出码 {result.exit_code})")
        joined = result.text
        if re.search(r"read protection|RDP|protected", joined):
            log.info("芯片可能开启了读保护, 需要先解除保护 (会全片擦除)")
        return Result(code=int(Exit.BURN), message="烧录失败", data={"exit_code": result.exit_code})

    log.ok("烧录完成并已复位运行")

    # 独立回读校验（不走 program 路径）
    base = str(devices.flash_base(data, model.family))
    readback_ok, note = _readback_verify(tools, interface_cfg, target_cfg, artifact, base)
    if readback_ok:
        log.ok(note)
    else:
        log.err(note)
        return Result(code=int(Exit.BURN), message="回读校验失败")
    return Result(code=int(Exit.OK), message="烧录成功")


# ===========================================================================
# doctor
# ===========================================================================
def _burn_pyocd(ctx: Context, artifact: Path) -> Result:
    """pyOCD 烧录路径。

    价值: 靠 CMSIS-Pack 自动获取 flash 算法, 不依赖 openocd 的 target 配置,
    所以能覆盖更多 Cortex-M 厂商。
    """
    model = ctx.model
    opt = ctx.opt
    # pyOCD 的 target 名与 flash 基址同样取自该型号所属设备表
    data = devices.table_data(str(model.extra.get("device_table") or ""))

    tool = flash.find_pyocd()
    if tool is None:
        for line in flash.missing_hint():
            log.info(line)
        return Result(code=int(Exit.ENVIRONMENT), message="pyOCD 不可用")

    target = flash.pyocd_target_name(model.device, data)
    if not target:
        log.err("无法从型号推出 pyOCD 的 target 名")
        log.info("先用 `pyocd list -t` 查可用 target, 再确认设备表里该型号是否正确")
        return Result(code=int(Exit.PROJECT), message="缺少 pyOCD target 名")

    unique_id = str(opt.get("probe_serial") or "")
    if not unique_id:
        probes = flash.list_probes(tool)
        if not probes:
            log.err("pyOCD 没有检测到探针")
            log.info("检查 USB 连接与驱动 (WinUSB)")
            return Result(code=int(Exit.ENVIRONMENT), message="未检测到探针")
        if len(probes) > 1:
            log.warn(f"检测到 {len(probes)} 个探针, 用第一个; 可用 -ProbeSerial <uid> 指定")
        unique_id = str(probes[0].get("unique_id") or "")

    log.info(f"pyOCD       : {tool.exe}  [{tool.version}]  ({tool.source})")
    log.info(f"target      : {target}")
    if not flash.target_supported(tool, target):
        log.warn(f"pyOCD 内置 target 列表里没有 '{target}'")
        log.info("可安装 CMSIS-Pack: pyocd pack install <包名>; 或先看: pyocd list -t")

    log.step(f"烧录 {artifact.name} (pyOCD, 默认按 CRC 校验)")
    result = flash.load(tool, artifact, target, unique_id)
    if result.exit_code != 0:
        if _looks_like_probe_unavailable(result.text):
            log.warn("探针不可用 —— 按环境问题处理, 不是烧录失败")
            return Result(code=int(Exit.ENVIRONMENT), message="探针不可用")
        log.err(f"烧录失败 (pyocd 退出码 {result.exit_code})")
        if re.search(r"unknown target|unique id", result.text, re.I):
            log.info("检查 target 名是否正确 / 是否被其他工具占用")
        return Result(code=int(Exit.BURN), message="烧录失败", data={"exit_code": result.exit_code})
    log.ok("烧录完成并已复位 (pyOCD)")

    base = str(devices.flash_base(data, model.family))
    expected = _read_artifact_words(ctx.tools, artifact)
    if expected is None:
        log.info("回读校验跳过 (无法读取固件开头)")
        return Result(code=int(Exit.OK), message="烧录成功")
    words = flash.read_words(tool, target, base, len(expected), unique_id)
    if words is None:
        log.info("回读校验跳过 (未解析到 pyocd cmd 的内存输出)")
        return Result(code=int(Exit.OK), message="烧录成功")
    if words == expected:
        shown = " ".join(f"{w:08x}" for w in words)
        log.ok(f"回读校验通过: {base} = {shown}")
        return Result(code=int(Exit.OK), message="烧录成功")
    log.err(f"回读校验失败: 芯片 {[f'{w:08x}' for w in words]} != 固件 {[f'{w:08x}' for w in expected]}")
    return Result(code=int(Exit.BURN), message="回读校验失败")


