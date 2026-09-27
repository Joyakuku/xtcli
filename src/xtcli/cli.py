"""CLI 入口: 参数解析 / 后端分发 / 退出码。

对外契约逐字保持（与 PowerShell 版一致）:
  * 退出码 0..7
  * 命令名 stm32-*-pj / pj-* / xtcli-doctor
  * 选项拼写 -Clean / -NoBuild / ... （argparse 同时注册 -X 与 --x 两种写法）
  * **不接受"工程目录"位置参数** —— 一律作用于当前目录并自动向上找工程根
"""

from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

from . import __version__, discovery, env, log, model, project
from .errors import Exit, Result, XtError

VERBS = ("init", "build", "burn", "doctor")
_FREE_VERBS = ("help", "version")
# 这些选项的取值本身以 '-' 开头 (编译器旗标), argparse 会把它们当成选项 ——
# 实测 `-Fpu -mfloat-abi=soft` 报 "expected one argument"。见 _fold_dash_value()。
_DASH_VALUE_OPTIONS = ("-Fpu", "--fpu")


def _fold_dash_value(argv: list[str], parser: argparse.ArgumentParser) -> list[str]:
    """把 ``-Fpu <以 - 开头的值>`` 折成 ``-Fpu=<值>``。

    ``-Fpu`` 的取值就是编译器旗标（``-mfpu=... -mfloat-abi=...``），所以文档里的
    写法必然以 ``-`` 开头，而 argparse 会把它当成下一个选项、报
    ``expected one argument``。这里在解析前折一次，让文档写法可用。

    只在"下一个 token 不是已知选项"时才折 —— 否则 ``-Fpu -NoBuild``（用户漏了取值）
    会被误当成"取值是 -NoBuild"，把真正的用法错误掩盖掉。
    """
    known = {
        option
        for action in getattr(parser, "_actions", [])
        for option in getattr(action, "option_strings", [])
    }
    folded: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in _DASH_VALUE_OPTIONS and index + 1 < len(argv):
            value = argv[index + 1]
            if value.startswith("-") and value not in known:
                folded.append(f"{token}={value}")
                index += 2
                continue
        folded.append(token)
        index += 1
    return folded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xtcli", add_help=False, allow_abbrev=False)
    parser.add_argument("verb", nargs="?", default="help")

    parser.add_argument("-h", "--help", dest="help_flag", action="store_true")
    parser.add_argument("-v", "--version", dest="version_flag", action="store_true")
    parser.add_argument("-Target", "--target", dest="target", default="auto",
                        help="auto(默认) | stm32 | stm32f4 ...")
    parser.add_argument("-Root", "--root", dest="root", default="",
                        help="指定工程根目录 (默认从当前目录向上自动查找)")
    parser.add_argument("-Config", "--config", dest="config", default="Debug")
    parser.add_argument("-Clean", "--clean", dest="clean", action="store_true")
    parser.add_argument("-NoBuild", "--no-build", dest="no_build", action="store_true")
    parser.add_argument("-NoStubs", "--no-stubs", dest="no_stubs", action="store_true",
                        help="不补齐 GCC 运行时桩 (syscalls/sysmem)")
    parser.add_argument("-MakeTarget", "--make-target", dest="make_target", default="")
    parser.add_argument("-Interface", "--interface", dest="interface", default="")
    parser.add_argument("-OcdTarget", "--ocd-target", dest="ocd_target", default="")
    parser.add_argument("-ProbeSerial", "--probe-serial", dest="probe_serial", default="")
    parser.add_argument("-Bin", "--bin", dest="bin_mode", action="store_true")
    parser.add_argument("-Address", "--address", dest="address", default="")
    parser.add_argument("-Elf", "--elf", dest="elf", default="")
    parser.add_argument("-Probe", "--probe", dest="probe", action="store_true")
    parser.add_argument("-FlashType", "--flash-type", dest="flash_type", default="",
                        help="ESP32 烧录方式: UART(默认) | JTAG; 空 = 用工程配置")
    parser.add_argument("-Flasher", "--flasher", dest="flasher", default="auto",
                        help="烧录器: auto(默认) | openocd | pyocd")
    parser.add_argument("-Cpu", "--cpu", dest="cpu", default="",
                        help="覆盖 CPU 型号 (设备表没覆盖的厂商用), 如 cortex-m4")
    parser.add_argument("-Fpu", "--fpu", dest="fpu", default="",
                        help='覆盖 FPU 旗标, 如 "-mfpu=fpv4-sp-d16 -mfloat-abi=hard" '
                             "(取值以 - 开头, 也可写成 -Fpu=<值>)")
    parser.add_argument("-Port", "--port", dest="port", default="",
                        help="串口 (ESP32 UART 烧录), 覆盖工程配置")
    parser.add_argument("-Refresh", "--refresh", dest="refresh", action="store_true")
    parser.add_argument("-Quiet", "--quiet", dest="quiet", action="store_true")
    parser.add_argument("-LogFile", "--log-file", dest="log_file", default="")
    return parser


def show_help() -> None:
    text = f"""xtcli {__version__} —— 嵌入式工程 CLI

用法:
  xtcli-init-pj  [选项]    初始化直到可编译
  xtcli-build-pj [选项]    构建
  xtcli-burn-pj  [选项]    烧录
  xtcli-doctor   [选项]    体检环境与工程, 并给出确定性结论
  xtcli <动词>             同上 (动词: init / build / burn / doctor)

命令名**不带芯片前缀**: 同一套命令处理所有芯片, 后端由工程本身自动识别
(.cproject / *.ioc / *.ewp / *.uvprojx / Makefile / ESP-IDF CMakeLists.txt)。
要强制指定后端用 -Target (例如 -Target stm32 / -Target espidf), 但它也必须先通过识别。

全部命令作用于【当前目录】, 并会自动向上查找工程根。
所以先 cd 到工程目录 (或它的子目录) 再运行。

用于 init / build:
  -Config <名>       构建配置, 默认 Debug
  -Root <目录>       指定工程根 (默认从当前目录向上自动查找)
  -Clean             先清理
  -NoBuild           init 时不试编译 / burn 时不自动构建
  -NoStubs          不补齐 GCC 运行时桩 (syscalls.c/sysmem.c)
  -MakeTarget <t>    传给 make 的目标 (size / disasm / all ...)

用于 burn:
  -Interface <cfg>   强制指定 openocd 探针配置 (默认自动识别)
  -OcdTarget <cfg>   覆盖 openocd target 配置
  -ProbeSerial <sn>  指定探针序列号 (多探针时)
  -Bin               烧 .bin (需配合 -Address)
  -Address <addr>    bin 的烧录基址, 默认 0x08000000
  -Elf <路径>        指定要烧的固件
  -FlashType <方式>  ESP32: UART(默认) | JTAG; 留空则用工程 .vscode 配置
  -Port <COMx>       ESP32: 串口, 覆盖工程配置
  -Flasher <名>      烧录器: auto(默认) | openocd | pyocd (pyocd 需 xtcli-setup -Pyocd)
  -Cpu <型号>        覆盖 CPU (设备表没覆盖的厂商), 如 cortex-m4
  -Fpu <旗标>        覆盖 FPU 旗标, 如 "-mfpu=fpv4-sp-d16 -mfloat-abi=hard"

通用:
  -Target <名>       auto(默认) | stm32 | stm32f4 ...
  -Probe             doctor 时逐个试探针 (带超时)
  -Refresh           重新探测工具链版本
  -Quiet             安静模式
  -LogFile <路径>    把输出同时写入日志文件

环境:
  xtcli-setup        创建/修复项目 venv (独立 CPython, 拒绝 conda 底座)

退出码: 0 成功 | 1 失败 | 2 参数 | 3 环境 | 4 工程 | 5 不支持 | 6 构建 | 7 烧录
"""
    sys.stdout.write(text)


def show_version() -> None:
    prefix = Path(sys.prefix)
    try:
        in_venv = prefix.resolve() == env.venv_dir().resolve()
    except OSError:  # pragma: no cover
        in_venv = False
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    conda_note = "  ← conda 底座, 需 xtcli-setup -Force 重建" if env.looks_like_conda(base) else "  (非 conda)"
    for line in (
        f"xtcli {__version__}",
        f"实现        : Python {platform.python_version()}",
        f"解释器      : {sys.executable}",
        f"底座        : {base}{conda_note}",
        f"项目 venv   : {env.venv_dir()}",
        f"运行于 venv : {'是' if in_venv else '否'}",
    ):
        log.raw(line)


def _collect_opt(args: argparse.Namespace) -> dict[str, object]:
    return {
        "config": args.config,
        "clean": bool(args.clean),
        "no_build": bool(args.no_build),
        "no_stubs": bool(args.no_stubs),
        "make_target": args.make_target,
        "interface": args.interface,
        "ocd_target": args.ocd_target,
        "probe_serial": args.probe_serial,
        "bin": bool(args.bin_mode),
        "address": args.address,
        "elf": args.elf,
        "probe": bool(args.probe),
        "flash_type": args.flash_type,
        "flasher": args.flasher,
        "cpu": args.cpu,
        "fpu": args.fpu,
        "port": args.port,
    }


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    raw_argv = _fold_dash_value(raw_argv, parser)
    try:
        args, extras = parser.parse_known_args(raw_argv)
    except SystemExit as exc:
        # argparse 的用法错误 (例如选项缺取值) 会直接 sys.exit(2)。作为进程运行时
        # 退出码是对的, 但它会**逃出** cli.main —— 进程内调用 (测试/嵌入) 会被打断。
        # 这里统一折成返回值, 退出码保持不变。
        code = exc.code
        return code if isinstance(code, int) else int(Exit.USAGE)
    verb = args.verb

    log.configure(quiet=bool(args.quiet), log_file=(args.log_file or None))
    try:
        if args.help_flag or verb in ("help", "/?"):
            show_help()
            return int(Exit.OK)
        if args.version_flag or verb == "version":
            show_version()
            return int(Exit.OK)

        if extras:
            log.err(f"无法识别或不支持的参数: {' '.join(extras)}")
            log.info('本命令不接受"工程目录"参数 —— 始终作用于当前目录, 并自动向上查找工程根')
            log.info("用法: cd 到工程目录, 然后运行 xtcli-init-pj / xtcli-build-pj / xtcli-burn-pj")
            log.info("查看全部选项: xtcli help")
            return int(Exit.USAGE)

        if verb not in VERBS:
            log.err(f"未知动作: {verb}")
            log.info("可用动作: " + " / ".join(VERBS))
            log.info("查看全部选项: xtcli help")
            return int(Exit.USAGE)

        env.assert_running_in_venv()

        # -Root: 显式指定工程根。解决"在工程子目录里运行"被拒的情况 ——
        # 实测踩过: 在 ESP-IDF 工程的 main/ 下运行会把它当工程根 (main/ 里有
        # CMakeLists.txt), 于是报"没有 backend 能处理该目录"; 用 -Root .. 即可。
        root_opt = str(getattr(args, "root", "") or "").strip()
        if root_opt:
            candidate = Path(root_opt)
            if not candidate.is_dir():
                log.err(f"-Root 指定的目录不存在: {candidate}")
                return int(Exit.USAGE)
            os.chdir(candidate)

        root = project.find_root()
        if root is None:
            log.err(f"当前目录无效: {Path.cwd()}")
            # find_root 会说明"为什么没定位到": 是向上找到了工程标记但超出查找上限
            # (那就把真正的工程根报出来), 还是整条祖先链都没有标记。
            note = project.last_root_search_note()
            if note:
                log.info(note)
            log.info("提示: 也可以用 -Root <目录> 直接指定工程根")
            return int(Exit.PROJECT)

        model.load_backends()
        # -Target 拼错与"目录不被接受"是两件事, 分开报 (否则用户排查方向全错)
        if args.target and args.target != "auto" and model.by_name(args.target) is None:
            log.err(f"未知的 -Target 名: {args.target}")
            names = ["auto"]
            for item in model.all_backends():
                names.append(item.name)
                names.extend(item.aliases)
            log.info("可用取值: " + " / ".join(names))
            return int(Exit.USAGE)

        backend = model.resolve(args.target, root)
        if backend is None:
            log.err(f"没有 backend 能处理该目录 (-Target {args.target})")
            log.info(f"目录: {root}")
            if args.target and args.target != "auto":
                log.info("显式 -Target 也必须先通过工程识别 —— 该目录没有该后端认得的工程标记")
                log.info("(例如 .cproject / *.ioc / Core/Src / 带 ESP-IDF 特征的 CMakeLists.txt)")
            known = ", ".join(b.name for b in model.all_backends()) or "(尚未注册任何 backend)"
            log.info(f"已注册 backend: {known}")
            return int(Exit.UNSUPPORTED)

        tools = discovery.discover(refresh=bool(args.refresh))
        opt = _collect_opt(args)

        try:
            project_model = backend.extract(root, opt)
        except XtError as exc:
            log.err(f"工程解析失败: {exc.message}")
            if exc.hint:
                log.info(exc.hint)
            return int(exc.code)

        if verb == "doctor":
            log.info(f"backend: {backend.name}")

        # 型号要求的工具链前缀与预扫描的不同 (例如 RISC-V 的 riscv-none-elf-):
        # 按它重新查找 —— 找不到就在下面按"环境缺失"返回, 绝不退回 ARM 工具链。
        wanted = str(getattr(project_model, "toolchain", "") or "")
        if wanted and wanted != tools.prefix:
            log.info(f"该型号需要工具链前缀 '{wanted}', 按它重新查找工具链")
            tools = discovery.discover(refresh=bool(args.refresh), prefix=wanted)

        if verb in ("build", "burn") and tools.missing:
            for item in tools.missing:
                log.err(f"环境缺失: {item}")
            log.info("提示: 工具搜索根可用 XTCLI_ROOTS 环境变量或 xtcli.json 的 roots 覆盖")
            return int(Exit.ENVIRONMENT)

        ctx = model.Context(root=root, opt=opt, tools=tools, model=project_model, backend=backend)
        action = getattr(backend, verb)
        result = action(ctx)
        if not isinstance(result, Result):  # 后端违约的兜底
            result = Result(code=int(Exit.FAILURE), message="后端返回了非法结果")
        return int(result.code)

    except XtError as exc:
        log.err(exc.message)
        if exc.hint:
            log.info(exc.hint)
        return int(exc.code)
    except KeyboardInterrupt:
        log.err("已被用户中断 (Ctrl-C)")
        return 130
    except Exception as exc:
        log.err(f"未预期错误: {exc}")
        return int(Exit.FAILURE)
    finally:
        log.close()


__all__ = ["VERBS", "build_parser", "main", "show_help", "show_version"]
