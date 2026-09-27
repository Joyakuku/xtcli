"""ESP-IDF 后端 —— 薄 ``idf.py`` 包装, **不自研构建系统**。

为什么是"薄"的: ESP-IDF 的构建是 CMake + Ninja + Kconfig + 组件管理器的复杂体系,
而 ``idf.py`` 正是官方入口, 且它本身就是 Python。所以这里只做三件事:

1. **解析出 IDF 环境**（IDF_PATH / IDF_TOOLS_PATH / IDF_PYTHON_ENV_PATH /
   ESP_ROM_ELF_DIR / OPENOCD_SCRIPTS / PATH / IDF_TARGET）
2. **构造受控子进程环境**（复用 :func:`xtcli.exec.child_env` 的净化策略,
   额外注入 IDF 变量）
3. **调用** ``idf.py build`` / ``flash``, 把结果翻译成结构化结论

不解析 Kconfig、不解析 CMake、不自己拼 xtensa-gcc 命令行。

环境来源优先级（**每一级都校验路径真实存在** —— 实测用户级 VSCode 配置里
``idf.espIdfPathWin`` 指向的 ``E:\\env\\Espressif\\...`` 根本不存在, 是过期路径）::

    工程 .vscode/settings.json  (idf.currentSetup / idf.customExtraVars / ...)
      > EIM 清单 eim_idf.json   (idfSelectedId -> path / idfToolsPath / python)
      > 用户级 %APPDATA%/Code/User/settings.json
      > 环境变量 IDF_PATH / IDF_TOOLS_PATH

烧录的 ``flashType`` 是**可选**的, 解析顺序::

    -FlashType 覆盖 > 工程 settings 的 idf.flashType > 默认 UART

* UART -> ``idf.py -p PORT flash``（esptool; 对 ESP32-S3 的内置 USB-JTAG 串口同样适用）
* JTAG -> esp 版 openocd + 工程 ``idf.openOcdConfigs``, 按 ``flasher_args.json``
  逐个 ``program_esp``
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .. import env, log, managed
from .. import exec as xt_exec
from ..errors import Exit, Refusal, Result
from ..model import Backend, Context, ProjectModel, register
from .idf_env import (
    IdfEnv,
    _compiler_name,
    _eim_activation_vars,
    _load_jsonc,
    _major_minor,
    _settings,
    _string_list,
    _strip_jsonc,
    list_com_ports,
    resolve,
)

SCHEMA_VERSION = 1
FINGERPRINT_PREFIX = "xt_fingerprint"



# ===========================================================================
# 根 CMakeLists.txt 里出现这些才算"引用了 ESP-IDF 的构建入口"。
# 注意**不包含** "IDF_PATH"/"idf.py"/"idf_component_register": 前两个只是普通词
# (注释里提一句就会被误判), 第三个出现在 main/ 与组件的 CMakeLists 里, 不在根。
_IDF_STRONG_TOKENS = ("project.cmake", "idf.cmake", "idf_build_process", "idf_project_init")


def _strip_cmake_comments(text: str) -> str:
    """去掉 CMake 的注释行与行尾注释。

    实测踩过: 一个普通 CMake 工程只要在**注释**里写一行 IDF_PATH, 就会被判定成
    ESP-IDF 工程 (detect=80), 于是 init 会往那个目录写 xtcli/idf-env.json 并试跑
    idf.py build —— 对不是 IDF 的工程动手。
    """
    kept: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        cut = line.find("#")
        kept.append(line if cut < 0 else line[:cut])
    return "\n".join(kept)


def _idf_cmake(project_root: Path) -> bool:
    """根 CMakeLists.txt 是否引用了 ESP-IDF 的构建入口（强特征）。

    要求 ``include(...project.cmake/idf.cmake)`` 或 ``idf_build_process`` /
    ``idf_project_init``, 并且有 ``project(...)`` 或 IDF 的初始化调用。

    **``main/CMakeLists.txt`` 不是必需**（它只作为加分项）: IDF 自己的
    ``examples/build_system/cmakev2/*``（只有 components/）与 ``idf_as_lib``
    （main.c 在根）都合法 —— IDF 的 ``tools/cmake/project.cmake`` 也只在 main
    目录存在时才把它加入组件列表。实测这两个官方示例曾被旧判据误拒。
    """
    cmake = project_root / "CMakeLists.txt"
    if not cmake.is_file():
        return False
    try:
        text = _strip_cmake_comments(cmake.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False
    if not any(token in text for token in _IDF_STRONG_TOKENS):
        return False
    if re.search(r"\bproject\s*\(", text):
        return True
    return any(token in text for token in ("idf_build_process", "idf_project_init"))


def detect_score(project_root: Path) -> int:
    if not project_root.is_dir():
        return 0
    score = 0
    if _idf_cmake(project_root):
        score += 80
        # 常见形态加分, 但不作为判据
        if (project_root / "main" / "CMakeLists.txt").is_file():
            score += 5
        if (project_root / "components").is_dir():
            score += 5
    if (project_root / "sdkconfig").is_file():
        score += 10
    if (project_root / "sdkconfig.defaults").is_file():
        score += 5
    project_settings, _ = _settings(project_root)
    if project_settings.get("idf.currentSetup"):
        score += 5
    return min(score, 100)


def _fingerprint(project_root: Path, idf_env: IdfEnv) -> str:
    parts = [
        str(env.real_path(idf_env.idf_path)),
        str(env.real_path(idf_env.tools_path)),
        str(idf_env.python),
        idf_env.version,
        idf_env.variables.get("IDF_TARGET", ""),
        str(_idf_cmake(project_root)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def build_model(project_root: Path, opt: dict) -> ProjectModel:
    # 收字符串也认: extract 是后端对外入口, 库调用方不该因为类型吃到裸 TypeError
    project_root = Path(project_root)
    model = ProjectModel(
        backend="espidf",
        root=project_root,
        target=project_root.name,
        out_dir="build",
        build_dir="build",
        source="espidf",
    )
    if not _idf_cmake(project_root):
        model.refusals.append(Refusal(
            message="不是 ESP-IDF 工程: 根 CMakeLists.txt 没有引用 ESP-IDF 的构建入口",
            missing=[
                "include($ENV{IDF_PATH}/tools/cmake/project.cmake) 或 idf_build_process/idf_project_init",
                "sdkconfig",
            ],
            next_step="在 ESP-IDF 工程根目录运行; 新建工程可用 idf.py create-project",
        ))
        return model

    idf_env, warnings = resolve(project_root)
    model.warnings.extend(warnings)
    if idf_env is None:
        model.refusals.append(Refusal(
            message="找不到可用的 ESP-IDF 环境",
            missing=["IDF_PATH", "IDF_TOOLS_PATH", "IDF_PYTHON_ENV_PATH"],
            next_step="用 ESP-IDF 安装器装一次, 或先 source 它的 activate 脚本; "
                      "也可在工程 .vscode/settings.json 里设置 idf.currentSetup",
            code=int(Exit.ENVIRONMENT),
        ))
        return model

    project_settings, _ = _settings(project_root)
    model.device = str(idf_env.variables.get("IDF_TARGET") or "")
    model.extra["idf"] = idf_env
    model.extra["idf_version"] = idf_env.version
    model.extra["flash_type"] = str(project_settings.get("idf.flashType") or "")
    model.extra["port"] = str(project_settings.get("idf.portWin") or project_settings.get("idf.port") or "")
    model.extra["openocd_configs"] = _string_list(project_settings.get("idf.openOcdConfigs"))
    model.extra["fingerprint"] = _fingerprint(project_root, idf_env)
    model.extra["flasher_args"] = project_root / "build" / "flasher_args.json"
    return model


# ===========================================================================
# 动作
# ===========================================================================
def _refusal_result(model: ProjectModel) -> Result | None:
    if not model.refusals:
        return None
    first = model.refusals[0]
    log.err(first.message)
    for item in first.missing:
        log.info(f"缺少: {item}")
    if first.next_step:
        log.info(f"建议: {first.next_step}")
    return Result(code=first.code, message=first.message)


def _run_idf(ctx: Context, args: list[str], *, timeout: float | None = None) -> xt_exec.RunResult:
    idf_env: IdfEnv = ctx.model.extra["idf"]
    argv = [str(idf_env.python), str(idf_env.idf_py), *args]
    return xt_exec.run(
        argv,
        cwd=ctx.model.root,
        prepend_path=idf_env.path_entries,
        extra_env=idf_env.variables,
        timeout=timeout,
    )


def action_init(ctx: Context) -> Result:
    model = ctx.model
    refused = _refusal_result(model)
    if refused is not None:
        return refused

    idf_env: IdfEnv = model.extra["idf"]
    log.step(f"初始化 ESP-IDF 工程 {model.root}")
    log.info(f"IDF        : {idf_env.idf_path}  (v{idf_env.version or '?'}, 来源 {idf_env.source})")
    log.info(f"tools      : {idf_env.tools_path}")
    log.info(f"python env : {idf_env.python}")
    log.info(f"target     : {model.device or '(由 sdkconfig/IDF 决定)'}")

    payload = {
        "schema_version": SCHEMA_VERSION,
        FINGERPRINT_PREFIX: model.extra["fingerprint"],
        "idf_path": str(idf_env.idf_path),
        "idf_version": idf_env.version,
        "tools_path": str(idf_env.tools_path),
        "python": str(idf_env.python),
        "target": model.device,
        "variables": idf_env.variables,
        "path_entries": idf_env.path_entries,
        "flash_type": model.extra["flash_type"],
        "port": model.extra["port"],
        "openocd_configs": model.extra["openocd_configs"],
    }
    target_path = model.root / "xtcli" / "idf-env.json"
    # 走 managed.Owned: 工程里若已有**非 xtcli 生成**的 xtcli/idf-env.json, 先备份成
    # *.xtcli-bak 再写, 并登记进 xtcli/.owned.json —— 与 GCC 后端同一套接管规则。
    owned = managed.Owned(model.root)
    owned.write_text(target_path, json.dumps(payload, ensure_ascii=False, indent=2))
    owned.flush()
    log.ok(f"已写入 {env.relative_to(model.root, target_path)}")
    if owned.backups:
        log.info(
            f"原有文件已备份 {len(owned.backups)} 个为 *{managed.BAK_SUFFIX} (不是 xtcli 生成的)"
        )
    log.info("说明: ESP-IDF 用 CMake + Ninja 构建, 所以 init 不生成 Makefile, 只解析并固化环境")

    for warning in model.warnings:
        log.warn(warning)

    if ctx.opt.get("no_build"):
        log.info("已跳过试编译 (-NoBuild)")
        return Result(code=int(Exit.OK), message="初始化完成 (未试编译)")

    log.step("试编译")
    built = action_build(ctx)
    if built.ok:
        return Result(code=int(Exit.OK), message="初始化完成, 试编译通过")
    log.err("ESP-IDF 环境已固化, 但试编译失败 (见上面的构建输出)")
    return Result(code=built.code, message="初始化完成, 但试编译失败", data=built.data)


def _check_stale_env(model: ProjectModel) -> None:
    path = model.root / "xtcli" / "idf-env.json"
    if not path.is_file():
        log.info("提示: 尚未运行 stm32-init-pj/xtcli init, 直接用当前解析出的环境构建")
        return
    try:
        recorded = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        log.warn(f"{env.relative_to(model.root, path)} 解析失败, 建议重新 init")
        return
    stored = str(recorded.get(FINGERPRINT_PREFIX, ""))
    current = str(model.extra.get("fingerprint", ""))
    if stored and stored != current:
        log.warn("xtcli/idf-env.json 与当前 ESP-IDF 环境不一致 (IDF/tools/python/target 变了)")
        log.info(f"  记录 {stored}   当前 {current}")
        log.info("  建议重新运行 init 固化环境")


_TOOLCHAIN_CRASH_MARKERS = (
    "internal compiler error",
    "cc1: out of memory",
    "cc1plus: out of memory",
    "fatal error: killed",
    "internal error: segmentation fault",
)


def _looks_like_toolchain_crash(text: str) -> bool:
    """识别"编译器自身崩了"这类瞬时故障。

    实测遇到过 ``internal compiler error: Segmentation fault``（同一个工程在
    3 次构建中成功 2 次）。这类失败与工程无关, 重试一次通常就过, 所以这里显式
    识别并透明重试, 而不是把它当成普通的构建失败。
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _TOOLCHAIN_CRASH_MARKERS)


def action_build(ctx: Context) -> Result:
    model = ctx.model
    opt = ctx.opt
    refused = _refusal_result(model)
    if refused is not None:
        return refused

    _check_stale_env(model)

    # -Clean: ESP-IDF 的等价物是 fullclean (删掉整个 build/ 并让它重新生成)。
    # 旧实现把 -Clean 与 -MakeTarget **静默忽略** —— 用户以为清理过了, 实际没有。
    if opt.get("clean"):
        log.step("清理 (idf.py fullclean)")
        cleaned = _run_idf(ctx, ["fullclean"])
        if cleaned.timed_out:
            log.err("清理超时")
            return Result(code=int(Exit.BUILD), message="清理超时")
        if cleaned.exit_code != 0:
            log.err(f"清理失败 (idf.py 退出码 {cleaned.exit_code})")
            return Result(
                code=int(Exit.BUILD), message="清理失败", data={"exit_code": cleaned.exit_code}
            )
        log.ok("清理完成 (build/ 已移除)")

    # -MakeTarget: 原样当作 idf.py 的子命令 (size / size-components / app / bootloader ...)
    subcommand = str(opt.get("make_target") or "").strip() or "build"

    log.step(f"构建 (idf.py {subcommand})")
    result = _run_idf(ctx, [subcommand])
    if result.timed_out:
        log.err("构建超时")
        return Result(code=int(Exit.BUILD), message="构建超时")

    if result.exit_code != 0 and _looks_like_toolchain_crash(result.text):
        log.warn("检测到编译器自身崩溃 (internal compiler error) —— 这类瞬时故障重试一次通常通过")
        log.step("重试构建")
        retry = _run_idf(ctx, [subcommand])
        if retry.exit_code == 0:
            log.ok("重试后构建成功 (确认是瞬时故障, 与工程无关)")
            result = retry
        else:
            log.err("重试仍然失败")
            result = retry

    if result.exit_code != 0:
        log.err(f"构建失败 (idf.py 退出码 {result.exit_code})")
        if _looks_like_toolchain_crash(result.text):
            log.info("这是编译器崩溃, 不是工程配置问题; 可再试一次, 或检查杀毒软件/磁盘/内存")
        return Result(code=int(Exit.BUILD), message="构建失败", data={"exit_code": result.exit_code})

    if subcommand != "build":
        log.ok(f"idf.py {subcommand} 完成")
        return Result(code=int(Exit.OK), message=f"idf.py {subcommand} 完成")

    binary = model.root / "build" / f"{model.target}.bin"
    if binary.is_file():
        log.ok(f"构建成功: {binary.name} ({binary.stat().st_size} 字节)")
    else:
        log.ok("构建成功")
    return Result(code=int(Exit.OK), message="构建成功")


def _load_flasher_args(model: ProjectModel) -> dict[str, Any] | None:
    path: Path = model.extra["flasher_args"]
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_flash_type(model: ProjectModel, opt: dict) -> tuple[str, str]:
    """返回 (flashType, 来源说明)。flashType 可选: 覆盖 > 工程配置 > 默认 UART。"""
    override = str(opt.get("flash_type") or "").strip()
    if override:
        return override.upper(), "-FlashType 指定"
    configured = str(model.extra.get("flash_type") or "").strip()
    if configured:
        return configured.upper(), "工程 .vscode/settings.json 的 idf.flashType"
    return "UART", "默认 (未配置 flashType)"


def _resolve_port(model: ProjectModel, opt: dict) -> str:
    return str(opt.get("port") or model.extra.get("port") or "").strip()


def action_burn(ctx: Context) -> Result:
    model = ctx.model
    refused = _refusal_result(model)
    if refused is not None:
        return refused

    idf_env: IdfEnv = model.extra["idf"]
    flash_type, origin = _resolve_flash_type(model, ctx.opt)
    port = _resolve_port(model, ctx.opt)
    log.info(f"flashType  : {flash_type}  ({origin})")

    if not (model.root / "build" / "flasher_args.json").is_file():
        if ctx.opt.get("no_build"):
            log.err("尚未构建 (缺 build/flasher_args.json), 且指定了 -NoBuild")
            return Result(code=int(Exit.PROJECT), message="未构建")
        log.info("尚未构建, 先构建")
        built = action_build(ctx)
        if not built.ok:
            return built

    flasher = _load_flasher_args(model) or {}
    files = flasher.get("flash_files") if isinstance(flasher.get("flash_files"), dict) else {}
    if files:
        summary = ", ".join(f"{offset}={Path(name).name}" for offset, name in files.items())
        log.info(f"烧录内容   : {summary}")

    if flash_type in ("UART", "SERIAL", "USB", "USB-JTAG", "USB_SERIAL_JTAG"):
        if not port:
            ports = list_com_ports()
            log.err("UART 烧录需要一个串口, 但没有配置")
            log.info(f"当前可用串口: {', '.join(ports) if ports else '(未检测到)'}")
            log.info("用 -Port COM7 指定, 或在工程 .vscode/settings.json 里设置 idf.portWin")
            return Result(code=int(Exit.PROJECT), message="缺少串口")
        log.step(f"烧录 (idf.py -p {port} flash)")
        result = _run_idf(ctx, ["-p", port, "flash"])
        if result.exit_code != 0:
            log.err(f"烧录失败 (idf.py 退出码 {result.exit_code})")
            if re.search(r"could not open port|Access is denied|No such file", result.text, re.I):
                log.info("串口打不开: 检查是否被串口监视器/其他程序占用, 以及线缆是否支持数据")
            return Result(code=int(Exit.BURN), message="烧录失败", data={"exit_code": result.exit_code})
        log.ok(f"烧录完成并已复位 (esptool/UART, {port})")
        return Result(code=int(Exit.OK), message="烧录成功")

    if flash_type in ("JTAG", "OPENOCD"):
        openocd = idf_env.openocd
        if not openocd:
            log.err("未找到 esp 版 openocd (openocd-esp32)")
            return Result(code=int(Exit.ENVIRONMENT), message="缺少 openocd-esp32")
        configs = model.extra.get("openocd_configs") or []
        if not configs:
            configs = [f"board/{model.device}-builtin.cfg"]
        argv = [str(openocd), *[arg for cfg in configs for arg in ("-f", str(cfg))]]
        if not files:
            log.err("缺少 build/flasher_args.json, 无法确定 JTAG 烧录布局")
            return Result(code=int(Exit.PROJECT), message="缺少 flasher_args.json")
        argv += ["-c", "init"]
        for offset, name in files.items():
            target = model.root / "build" / str(name)
            if not target.is_file():
                log.err(f"固件分片不存在: {target}")
                return Result(code=int(Exit.PROJECT), message="固件分片缺失")
            argv += ["-c", f'program_esp "{env.to_posix(target)}" {offset} verify']
        argv += ["-c", "reset run", "-c", "shutdown"]
        log.step(f"烧录 (openocd JTAG, {' '.join(str(c) for c in configs)})")
        result = xt_exec.run(argv, cwd=model.root, extra_env=idf_env.variables)
        if result.exit_code != 0:
            log.err(f"烧录失败 (openocd 退出码 {result.exit_code})")
            return Result(code=int(Exit.BURN), message="烧录失败", data={"exit_code": result.exit_code})
        log.ok("烧录完成并已复位 (openocd/JTAG)")
        return Result(code=int(Exit.OK), message="烧录成功")

    log.err(f"不支持的 flashType: {flash_type}")
    log.info("可选: UART (esptool, 默认) 或 JTAG (openocd); 用 -FlashType 覆盖")
    return Result(code=int(Exit.USAGE), message=f"不支持的 flashType: {flash_type}")


def action_doctor(ctx: Context) -> Result:
    model = ctx.model
    idf_env = model.extra.get("idf")

    log.step("xtcli / ESP-IDF 环境")
    from .. import __version__

    log.info(f"xtcli       : {__version__}")
    if idf_env is None:
        for warning in model.warnings:
            log.warn(warning)
        return Result(code=int(Exit.ENVIRONMENT), message="ESP-IDF 环境不可用")
    log.info(f"IDF         : {idf_env.idf_path}")
    log.info(f"IDF 版本    : {idf_env.version or '(未知)'}   来源: {idf_env.source}")
    log.info(f"tools 根    : {idf_env.tools_path}")
    log.info(f"python env  : {idf_env.python}")
    log.info(f"openocd     : {idf_env.openocd}")
    log.info(f"PATH 条目   : {len(idf_env.path_entries)} 个工具目录")
    for key in ("IDF_TARGET", "OPENOCD_SCRIPTS", "ESP_ROM_ELF_DIR"):
        if idf_env.variables.get(key):
            log.info(f"{key:<12}: {idf_env.variables[key]}")

    log.step(f"工程: {model.root}")
    log.info("形态        : CMakeLists.txt (含 IDF 构建入口) ± main/CMakeLists.txt")
    log.info(f"target      : {model.device or '(未确定)'}")
    log.info(f"flashType   : {model.extra.get('flash_type') or '(未配置, 默认 UART)'}")
    log.info(f"配置串口    : {model.extra.get('port') or '(未配置)'}")
    log.info(f"openocd 配置: {', '.join(model.extra.get('openocd_configs') or []) or '(未配置)'}")
    # 诚实标注: 本机没有 ESP32 真机, burn 路径只做过命令构造与拒绝路径验证
    log.info("烧录        : 已实现但【未经真机验证】(无 ESP32 硬件; 用 -Flasher/-FlashType 可覆盖)")
    binary = model.root / "build" / f"{model.target}.bin"
    if binary.is_file():
        log.info(f"已构建产物  : build/{binary.name} ({binary.stat().st_size} 字节)")
    else:
        log.info("已构建产物  : (尚未构建)")

    ports = list_com_ports()
    log.info(f"可用串口    : {', '.join(ports) if ports else '(未检测到)'}")

    for warning in model.warnings:
        log.warn(warning)
    for refusal in model.refusals:
        log.err(refusal.message)
        for item in refusal.missing:
            log.info(f"缺少: {item}")

    code = int(Exit.OK) if idf_env else int(Exit.ENVIRONMENT)
    return Result(code=code, message="环境检查完成")


register(Backend(
    name="espidf",
    aliases=("esp32", "esp32s3", "esp32c3", "esp32c6", "esp32s2", "esp-idf", "idf"),
    detect=detect_score,
    extract=build_model,
    init=action_init,
    build=action_build,
    burn=action_burn,
    doctor=action_doctor,
))

# ---------------------------------------------------------------------------
# 环境解析已分到 idf_env.py; 这里的名字**原样 re-export** —— 调用方与测试一直用
# ``xtcli.backends.espidf.<名>`` (例如 test_espidf 直接取 _strip_jsonc/_major_minor)。
# ---------------------------------------------------------------------------
__all__ = [
    "FINGERPRINT_PREFIX",
    "SCHEMA_VERSION",
    "IdfEnv",
    "_compiler_name",
    "_eim_activation_vars",
    "_load_jsonc",
    "_major_minor",
    "_strip_jsonc",
    "action_build",
    "action_burn",
    "action_doctor",
    "action_init",
    "build_model",
    "detect_score",
    "list_com_ports",
    "register",
    "resolve",
]
