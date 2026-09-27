"""设备数据表。

数据外置: 新增芯片/系列只改 ``data/devices/*.json``, 不动代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import env
from .errors import Exit, XtError

SCHEMA_VERSION = 1


@dataclass
class DeviceInfo:
    family: str | None = None
    cpu: str | None = None
    fpu: str | None = None
    ocd_target: str | None = None
    arch: str | None = None
    abi: str | None = None
    toolchain: str | None = None


def _info_from_entry(family: str | None, entry: dict[str, Any]) -> DeviceInfo:
    return DeviceInfo(
        family=family,
        cpu=entry.get("cpu"),
        fpu=entry.get("fpu"),
        ocd_target=entry.get("ocd"),
        arch=entry.get("arch"),
        abi=entry.get("abi"),
        toolchain=entry.get("toolchain"),
    )


def _load(name: str) -> dict[str, Any]:
    path = env.data_dir() / "devices" / f"{name}.json"
    if not path.is_file():
        raise XtError(f"设备数据表缺失: {path}", code=int(Exit.ENVIRONMENT))
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise XtError(f"设备数据表损坏: {path} ({exc})", code=int(Exit.ENVIRONMENT)) from exc
    if not isinstance(data, dict):
        raise XtError(f"设备数据表格式错误: {path}", code=int(Exit.ENVIRONMENT))
    version = data.get("schema_version")
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise XtError(
            f"设备数据表 schema 版本过新: {path} (v{version} > v{SCHEMA_VERSION})",
            code=int(Exit.ENVIRONMENT),
            hint="升级 xtcli 或修正数据表",
        )
    return data


def load_stm32() -> dict[str, Any]:
    return _load("stm32")


def load_tables() -> dict[str, dict[str, Any]]:
    """加载 ``data/devices/*.json`` 的全部设备表 —— 多厂商扩展只加数据文件。"""
    directory = env.data_dir() / "devices"
    tables: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return tables
    for path in sorted(directory.glob("*.json")):
        try:
            tables[path.stem] = _load(path.stem)
        except XtError:
            continue
    return tables


def table_of(
    device: str, tables: dict[str, dict[str, Any]] | None = None
) -> tuple[str, dict[str, Any]]:
    """按型号定位它所属的设备表, 返回 ``(表名, 表数据)``。

    表的选择必须**跟着型号走**, 而不是固定用 STM32 表: flashBase / 探针 / 内存
    校验都取自这张表 —— 固定用 STM32 表会给 RP2040(0x10000000) 之类算错烧录基址,
    而"基址错"的烧录是最危险的一类错误。

    没有匹配表时退回 STM32 表: 那是历史默认行为, 也让"设备表没覆盖的厂商"
    走"CPU/FPU 无法确定 → 确定性拒绝"这条老路。
    """
    known = load_tables() if tables is None else tables
    for name, data in known.items():
        if family_by_prefix(device, data):
            return name, data
    return "stm32", known.get("stm32") or load_stm32()


def table_data(name: str) -> dict[str, Any]:
    """按表名取数据; 表名缺失/损坏时退回 STM32 表。"""
    if name and name != "stm32":
        try:
            return _load(name)
        except XtError:
            pass
    return load_stm32()


def _prefix_entries(data: dict[str, Any]) -> list[tuple[str, str]]:
    """所有可用来匹配型号的前缀 -> 规范系列键。

    除了 ``families`` 的键本身, 系列条目还可以声明 ``aliases``: 同一颗芯片在不同工具里
    的写法不同 (例如 Microchip 的 ``SAMD21`` 在 IDE 工程里写作 ``ATSAMD21J18A``)。
    别名让"数据的键"与"工程里的写法"对齐, 而不是把命名规则写进代码。
    """
    pairs: list[tuple[str, str]] = []
    for key, entry in (data.get("families") or {}).items():
        pairs.append((key, key))
        if isinstance(entry, dict):
            for alias in entry.get("aliases") or []:
                pairs.append((str(alias), key))
    # 最长前缀优先
    return sorted(pairs, key=lambda item: len(item[0]), reverse=True)


def family_by_prefix(device: str, data: dict[str, Any]) -> str | None:
    """按"最长匹配前缀"在设备表里找系列 (返回**规范**系列键)。

    不写死厂商命名规则（不像 STM32 那样用正则）, 所以**加一张表就支持一个厂商**:
    表里的 families 键 (及其 aliases) 直接当型号前缀用。``STM32F103RCTx`` 会命中
    ``STM32F1``, 比它更长的键优先。
    """
    upper = (device or "").upper()
    if not upper:
        return None
    for prefix, canonical in _prefix_entries(data):
        if upper.startswith(prefix.upper()):
            return canonical
    return None


def subseries_check(device: str, data: dict[str, Any]) -> tuple[list[str], bool]:
    """型号是否落在"系列键之后一个未登记的子系列"上。

    返回 ``(告警文本, 是否拒绝套用父系列参数)``。

    "最长前缀匹配"本身没有词边界: ``STM32WBA55CG`` 与 ``STM32WB55CG`` 都以
    ``STM32WB`` 开头, 但 WBA 是 Cortex-M33、WB 是 Cortex-M4。短键会把内核完全
    不同的子系列吃掉, 于是**静默**给出错误的 -mcpu/-mfpu（"能编但错"的固件）。

    **只有字母子系列位才拒绝**（``STM32WBA55CG`` → ``STM32WB`` + ``A``）。纯数字位
    （``STM32WL5M`` / ``STM32WB5M`` 这类模块与型号位，``STM32WB09KE`` 那种前导 0）
    只告警: 实测它们与父系列同内核, 一律拒绝会破坏本来能用的工程。

    形状判定不写死厂商命名, 系列条目的 ``subseries`` 用来登记"已确认同参数"的位:
    ``STM32WB`` 登记 ``["A","0"]``、``STM32WBA`` 登记 ``["6"]``、``STM32WL`` 登记
    ``["E"]``（WLE5 = Cortex-M4 无 FPU, 与 WL 条目一致 —— 依据 ST/RIOT 的
    LoRa-E5(STM32WLE5JC) 板级资料）。表里没有更长键、也没登记时才会告警。
    """
    families = data.get("families") or {}
    upper = (device or "").upper()
    matched = family_by_prefix(device, data)
    if not matched:
        return [], False
    # 守卫: 只有"以字母结尾"的系列键才可能带子系列位。
    # 键以数字结尾时 (STM32F1 / STM32L4 / STM32C0 ...), 后面的字符是系列号剩余位/
    # 引脚码/容量码, 第一位数字永远属于系列号本身 —— 少了这条守卫,
    # STM32F103RCTx 会被读成 "STM32F1 + 0 = STM32F10" 而拒绝, 把最普通的 F1 工程
    # 全部挡在门外 (实测踩过: 三个真实工程同时变成 cpu 无法确定)。
    if not matched[-1:].isalpha():
        return [], False
    marker = _subseries_marker(upper[len(matched):])
    if marker is None:
        return [], False
    entry = families.get(matched)
    declared = entry.get("subseries") if isinstance(entry, dict) else None
    declared = {str(item).upper() for item in declared} if isinstance(declared, list) else set()
    if marker in declared:
        return [], False  # 该子系列在表里有自己的条目/已登记, 不算歧义
    message = (
        f"器件 '{device}' 看起来属于系列 '{matched}' 的子系列 '{matched}{marker}' "
        f"（可能是内核不同的另一个子系列）, 但设备表里没有 '{matched}{marker}' 的条目"
    )
    fatal = not marker.isdigit()
    if fatal:
        message += " —— CPU/FPU 无法确定, 不要按父系列的参数编译"
    else:
        message += "; 已按父系列参数继续 (数字位通常是型号/模块码), 若有异常请核对型号"
    return [message], fatal


def ambiguity_of(device: str, data: dict[str, Any]) -> list[str]:
    """兼容入口: 只要告警文本。判定与拒绝逻辑见 :func:`subseries_check`。"""
    return subseries_check(device, data)[0]


def _subseries_marker(residue: str) -> str | None:
    """从"系列键之后的延伸段"里取出子系列标识, 取不到返回 None。

    只看三种"子系列位夹在系列名里"的形状（子系列位后面必须紧跟一位字母才算数）:

    * ``A55CG`` -> ``A``（STM32WB + A = STM32WBA: 字母子系列后跟型号位 5）
    * ``09KE``  -> ``0``（STM32WB + 0 = STM32WB0: 子系列位 0 后跟型号位 9）
    * ``6M``    -> ``6``（STM32WBA + 6: 紧跟系列键的一位数字型号位）

    反例（返回 None, 第一位属于系列号本身）: ``55CG``（STM32WB55）、``71RETx``
    （STM32L471）、``31C6``（STM32C031）、``43ZI``（STM32H743）—— 这些都是两位
    数字连排, 不是子系列位。
    """
    import re

    # 字母子系列: 系列字母后插了一位字母 + 型号位数字 (WBA 的 A55CG -> A)
    letter = re.match(r"^([A-Z])\d", residue)
    if letter:
        return letter.group(1)
    # 数字子系列加前导 0: WB0 的 09KE -> 子系列位 0 (后面 9 才是型号位)
    if re.match(r"^0\d[A-Z]", residue):
        return "0"
    # 一位数字子系列: WBA6M 的 6M -> 6。第二位是数字的 (55CG/71RETx/31C6/43ZI)
    # 说明第一位属于系列号本身, 不是子系列位
    digit = re.match(r"^(\d)[A-Z]", residue)
    return digit.group(1) if digit else None


def _entry_for(
    device: str, family: str | None, data: dict[str, Any], warnings: list[str] | None
) -> dict[str, Any] | None:
    """取系列条目; 命中键只是"前缀延伸"时拒绝启用, 而不是猜。

    ``STM32WBA55CG`` 命中 ``STM32WB``, 但延伸段 ``A`` 构成新系列 ``STM32WBA``:
    这时绝不能把 STM32WB 的 Cortex-M4 参数套上去。表里没有更长的精确键时返回
    ``None``（cpu 留空 → gcc_make 的"CPU/FPU 无法确定"会确定性拒绝）并给出告警。

    注意: 若表里真有更长的键, ``family_by_prefix`` 早就选中它了, 所以走到这里
    说明那次"更长子系列"的匹配确实无数据可用。
    """
    families = data.get("families") or {}
    entry = families.get(family or "")
    if not isinstance(entry, dict):
        return None
    ambiguous, fatal = subseries_check(device, data)
    if not ambiguous:
        return entry
    if warnings is not None:
        warnings.extend(ambiguous)
    # 只有"字母子系列位未登记"(WBA 之于 WB 那种内核不同的情形) 才拒绝;
    # 纯数字位只告警, 以免破坏本来能用的工程 (WLE5 / WB5M 之类, 已实测)。
    return None if fatal else entry


def _model_sizes(entry: Any) -> tuple[int, int] | None:
    if not isinstance(entry, dict):
        return None
    flash, ram = entry.get("flash"), entry.get("ram")
    if isinstance(flash, int) and isinstance(ram, int):
        return flash, ram
    return None


def _model_candidates(device: str, family: str | None, data: dict[str, Any]) -> list[str]:
    """精确型号查表的候选写法。

    工程里的型号常带厂商前缀 (Microchip 的 ``ATSAMD21J18A``), 而 memory 表的键是
    规范系列写法 (``SAMD21J18A``)。别名是数据里声明的, 所以这里只做"按别名替换前缀"
    这一件确定的事, 不猜命名规则。
    """
    upper = (device or "").upper()
    out = [upper]
    entry = (data.get("families") or {}).get(family or "")
    aliases = entry.get("aliases") if isinstance(entry, dict) else None
    for alias in aliases or []:
        alias_upper = str(alias).upper()
        if family and upper.startswith(alias_upper):
            out.append(family.upper() + upper[len(alias_upper):])
    return out


def memory_for(device: str, family: str | None, data: dict[str, Any]) -> tuple[int, int] | None:
    """由型号取芯片**真实**容量, 返回 (flash 字节, ram 字节); 查不到返回 None。

    用途: 校验链接脚本 (.ld/.icf) 是否超出真机 —— 超出的方向最危险 (链接能过、真机跑不了)。
    **查不到就跳过校验**: 宁可不查, 不误报。

    两种查法, 都来自设备表, 不写死厂商命名规则:

    1. ``memory.<系列>.models.<完整型号>`` —— 精确查表。命名不符合"容量码"约定的厂商
       (AT32 的容量码不在固定位置、RP2040 之类集成型号) 只能这样登记。
       该系列一旦写了 ``models`` 就以它为准: 精确表里没有的型号返回 None, 不再按容量码猜。
    2. ``memory.<系列>.<容量码>`` —— 由型号里的容量码取值。**必须**在表级或系列级显式
       声明 ``devicePattern``(第 3 个捕获组 = 容量码), 用错形状会取到引脚码/型号位,
       那就是"按错误容量做校验" —— 所以**没有 devicePattern 就不猜**, 直接返回 None。
    """
    import re

    table = (data.get("memory") or {}).get(family or "")
    if isinstance(table, dict):
        models = table.get("models")
        if isinstance(models, dict):
            exact = {str(k).upper(): v for k, v in models.items()}
            for candidate in _model_candidates(device, family, data):
                hit = _model_sizes(exact.get(candidate))
                if hit is not None:
                    return hit
            return None
        pattern = table.get("devicePattern") or data.get("devicePattern")
        if device and pattern:
            match = re.match(str(pattern), device.upper())
            if match and (match.lastindex or 0) >= 3:
                return _model_sizes(table.get(match.group(3)))
    return None


def device_info_any(
    device: str,
    tables: dict[str, dict[str, Any]],
    warnings: list[str] | None = None,
) -> tuple[str, DeviceInfo]:
    """在所有设备表里查型号。返回 (表名, 信息); 查不到时表名为空。"""
    for table_name, data in tables.items():
        family = family_by_prefix(device, data)
        if family:
            entry = _entry_for(device, family, data, warnings) or {}
            return table_name, _info_from_entry(family, entry)
    return "", DeviceInfo(family=family_of(device or ""))


def family_of(device: str) -> str | None:
    """STM32F103RCTx -> STM32F1; STM32WB55 -> STM32WB。"""
    if not device:
        return None
    import re

    match = re.match(r"^(STM32[A-Z]\d)", device)
    if match:
        return match.group(1)
    match = re.match(r"^(STM32[A-Z]{2})", device)
    if match:
        return match.group(1)
    return None


def density_define(
    device: str,
    family: str | None,
    data: dict[str, Any],
    warnings: list[str] | None = None,
) -> str | None:
    """由型号推导 HAL 密度宏（仅在配置里没有现成宏时使用）。

    STM32F103RCTx -> 系列段 STM32F103, 容量码 C -> STM32F103xE（F1 的 high density）。
    其他"A+数字"型系列按 HAL 约定退回 ``<系列>xx`` (STM32F407VGTx -> STM32F407xx)。

    容量码在型号里的位置**因系列而异**, 所以按容量码推导只对"A+数字"型系列成立;
    两位字母型系列 (WBA / WB0) 在 ``density.<family>`` 里用保留键 ``hal`` 声明:
    家族宏由 CMSIS 器件头自己定义（STM32WBA -> STM32WBA, STM32WB0 -> STM32WB0）,
    HAL 里根本没有按容量区分的宏, 所以**故意不加 -D**, 并通过 ``warnings`` 说明原因。

    任何推不出来的情形都**不静默返回 None**: 会往 ``warnings`` 里写一条说明
    （返回值都是 None, 调用方无法只靠返回值区分"这个系列本来就没有密度宏"
    与"没认出来"）。
    """
    import re

    settings = (data.get("density") or {}).get(family or "")
    table = settings if isinstance(settings, dict) else {}
    if not device:
        return None

    # hal 系列: HAL 家族宏由 CMSIS 器件头给出, 没有按容量区分的宏 —— 故意不加 -D
    if family and table.get("hal"):
        if warnings is not None:
            warnings.append(
                f"系列 {family} 的 HAL 家族宏由 CMSIS 器件头自行定义, 没有按容量区分的密度宏; "
                f"'{device}' 不推导密度宏 (需要时可显式指定 -D)"
            )
        return None

    # 通用约定: STM32 + <系列字母><系列号数字> + <引脚码字母> + <容量码> + <封装字母>...
    # 容量码是系列号之后的**第二**个字符 (STM32F103RCTx: R 是引脚码, C 是容量码),
    # 且**可能是数字** —— x4/x6/x8 的容量码就是数字 (STM32F103C8Tx 的 8 = 64K)。
    # 写成 [A-Z] 会让蓝板这类最常见的型号永远推导不出密度宏 (表里明明有 '8' 键)。
    match = re.match(r"^STM32([A-Z]+\d+)([A-Z])([A-Z0-9])", device)
    if match:
        series = f"STM32{match.group(1)}"
        flash_code = match.group(3)
        if table and flash_code in table:
            return f"{series}{table[flash_code]}"
        return f"{series}xx"

    if re.match(r"^STM32", device):
        if warnings is not None:
            warnings.append(
                f"无法由型号 '{device}' 推导 HAL 密度宏 (系列 {family or '未知'}): "
                f"型号不符合 <系列字母><系列号数字><容量码字母><封装字母> 的通用约定, 且设备表 "
                f"density['{family}'] 里没有可用条目 —— 请核对芯片型号, 或在 IDE/config.mk 里显式给出密度宏"
            )
        return None
    return None


def device_info(
    device: str, data: dict[str, Any], warnings: list[str] | None = None
) -> DeviceInfo:
    # 优先"最长前缀匹配"（与设备表内容无关, 加表即支持）; 退回 STM32 的正则
    family = family_by_prefix(device, data) or family_of(device or "")
    entry = _entry_for(device, family, data, warnings)
    if not isinstance(entry, dict):
        return DeviceInfo(family=family)
    return _info_from_entry(family, entry)


def probes(data: dict[str, Any]) -> list[dict[str, str]]:
    items = data.get("probes")
    if not isinstance(items, list):
        return []
    return [p for p in items if isinstance(p, dict) and p.get("cfg")]


def probes_for(table_name: str) -> list[dict[str, str]]:
    """探针接口配置: 用该厂商表里的; 表里没写就退回 STM32 表。

    接口配置(J-Link/CMSIS-DAP/ST-Link 的 cfg)与芯片厂商无关, 所以退回是安全的;
    但**表里写了就以表为准**, 免得给某厂商塞一个本机不存在的接口脚本。
    """
    return probes(table_data(table_name)) or probes(load_stm32())


def flash_base(data: dict[str, Any], family: str | None = None) -> str:
    """烧录/回读基址: 系列级 ``flashBase`` 优先, 其次表级, 最后 STM32 的 0x08000000。"""
    entry = (data.get("families") or {}).get(family or "")
    if isinstance(entry, dict) and entry.get("flashBase"):
        return str(entry["flashBase"])
    return str(data.get("flashBase") or "0x08000000")


def ram_base(data: dict[str, Any], family: str | None = None) -> str:
    """RAM 起始地址 (生成兜底链接脚本要用): 系列级 ``ramBase`` 优先, 其次表级。

    绝大多数 Cortex-M (ST/GD32/AT32/APM32/N32/RP2040/nRF) 都是 0x20000000,
    所以缺省值就是它; 特殊芯片 (例如带别名 RAM 的型号) 在表里显式给出。
    """
    entry = (data.get("families") or {}).get(family or "")
    if isinstance(entry, dict) and entry.get("ramBase"):
        return str(entry["ramBase"])
    return str(data.get("ramBase") or "0x20000000")


def assets_stm32() -> Path:
    return env.assets_dir() / "stm32"
