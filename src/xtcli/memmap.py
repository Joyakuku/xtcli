"""链接脚本 / 内存布局的解析与比对。

**为什么需要这个模块**：IAR 工程描述内存布局的**唯一出处**是 ``.icf``（ST 的 CMSIS
包里只有 IAR 的 linker 模板，没有 GCC 的 ``.ld``）。迁移到 GCC 时必须换一份 ``.ld``
—— 那是**另一份布局**。实测过的真实案例：

    .icf (stm32f103xe_flash.icf): 512K Flash / 64K RAM   ← 厂商通用模板
    .ewp 芯片 STM32F103RC      : 256K / 48K              ← 型号说的
    工程里的 .ld               : 256K / 48K              ← CubeMX 生成

三者不一致时，旧实现一句"已改用同芯片的 .ld"就过去了 —— 如果换上去的 ``.ld``
**比真机大**，链接会成功但固件在真机上跑不起来（栈顶落在不存在的 RAM 上，或代码
超出 Flash）；如果**比真机小**，则可能"以前 IAR 能编、现在报放不下"。

本模块只做三件事：解析 ``.icf``、解析 ``.ld``、把差异和风险说清楚。**不猜、不改写**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MemMap", "Region", "chip_exceeded", "compare", "human", "parse_icf", "parse_ld"]

_IDF_REGION = re.compile(
    r"define\s+(?:region\s+)?{name}\s*(?:=|\{)?\s*mem:\[\s*from\s+(?P<start>[^ ]+)\s+to\s+(?P<end>[^ \]]+)",
    re.IGNORECASE,
)
_ICF_SYMBOL = re.compile(r"define\s+symbol\s+(?P<name>\w+)\s*=\s*(?P<expr>[^;]+);", re.IGNORECASE)
_ICF_REGION_DEF = re.compile(
    r"define\s+region\s+(?P<name>\w+)\s*=\s*mem:\[(?P<body>[^\]]+)\]", re.IGNORECASE
)
_NUM = re.compile(r"0[xX][0-9A-Fa-f]+|\d+")
_LD_MEMORY = re.compile(
    r"(?P<name>\w+)\s*\((?P<attrs>[^)]*)\)\s*:\s*ORIGIN\s*=\s*(?P<origin>[^,]+),\s*LENGTH\s*=\s*(?P<length>[^\s,}]+)",
    re.IGNORECASE,
)
_LD_SYMBOL = re.compile(r"^\s*(?P<name>_Min_(?:Stack|Heap)_Size|__stack_size__|__heap_size__)\s*=\s*(?P<value>[^;]+);", re.MULTILINE)

# 这些构造一旦出现, 就说明"仅靠 region/stack/heap 无法忠实翻译" —— 必须说出来。
# 注意**不**列 `initialize by copy` (RW 拷贝语义 GCC 的 .data 就是) 与 `block CSTACK/HEAP`
# (它们的 size 我们读到了), 否则每个 ST 模板都会刷一堆无意义告警。
_ICF_UNHANDLED = (
    ("place at", "有段被放到固定地址 (place at ...) —— 换 .ld 后不再保证落回原地址"),
    ("do not initialize", "有段声明为不初始化 (do not initialize) —— 对应 IAR 的 __no_init, 换 .ld 后会被当普通变量清零"),
    ("keep ", "要求保留符号 (keep) —— GCC 侧若无人引用可能被 --gc-sections 丢掉"),
    ("with fixed", "有固定大小的自定义内存块 (block ... with fixed)"),
)


def human(value: int | None) -> str:
    if value is None:
        return "?"
    if value % 1024 == 0:
        return f"{value // 1024}K"
    return str(value)


def _to_int(text: str, symbols: dict[str, int]) -> int | None:
    """把 ``0x2000`` / ``256K`` / 纯符号 / 简单加减 表达式求值。"""
    expr = (text or "").strip().rstrip(";").strip()
    if not expr:
        return None
    # 先替换已知符号（多轮，允许符号引用符号）
    for _ in range(4):
        replaced = False
        for name, value in symbols.items():
            if re.search(rf"\b{re.escape(name)}\b", expr):
                expr = re.sub(rf"\b{re.escape(name)}\b", str(value), expr)
                replaced = True
        if not replaced:
            break
    expr = re.sub(r"(\d+)\s*[kK]\b", lambda m: str(int(m.group(1)) * 1024), expr)
    expr = re.sub(r"(\d+)\s*[mM]\b", lambda m: str(int(m.group(1)) * 1024 * 1024), expr)
    if not re.fullmatch(r"[0-9a-fA-FxX+\-\s()]+", expr):
        return None
    try:
        return int(eval(expr, {"__builtins__": {}}, {}))
    except Exception:
        return None


@dataclass
class Region:
    name: str
    start: int
    size: int

    @property
    def end(self) -> int:
        return self.start + self.size - 1


@dataclass
class MemMap:
    source: str
    flash: Region | None = None
    ram: Region | None = None
    stack: int | None = None
    heap: int | None = None
    unhandled: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"Flash {human(self.flash.size) if self.flash else '?'}"
            f" / RAM {human(self.ram.size) if self.ram else '?'}"
        )


def parse_icf(path: Path) -> MemMap:
    """解析 IAR ILINK 配置里我们关心的部分。解析不动的构造记进 ``unhandled``。"""
    result = MemMap(source=f".icf ({path.name})")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", "", text)

    symbols: dict[str, int] = {}
    for match in _ICF_SYMBOL.finditer(text):
        value = _to_int(match.group("expr"), symbols)
        if value is not None:
            symbols[match.group("name")] = value

    # 优先用 __ICFEDIT_region_ROM/RAM_start__/end__（ST 模板的统一写法）
    def region(prefix: str, name: str) -> Region | None:
        start = symbols.get(f"__ICFEDIT_region_{prefix}_start__")
        end = symbols.get(f"__ICFEDIT_region_{prefix}_end__")
        if start is None or end is None or end < start:
            return None
        return Region(name, start, end - start + 1)

    result.flash = region("ROM", "FLASH")
    result.ram = region("RAM", "RAM")

    # 兜底: 直接从 `define region X = mem:[from A to B]` 里读
    if result.flash is None or result.ram is None:
        for match in _ICF_REGION_DEF.finditer(text):
            body = match.group("body")
            bounds = re.split(r"\bto\b", body, flags=re.IGNORECASE)
            if len(bounds) != 2:
                continue
            start = _to_int(bounds[0].replace("from", ""), symbols)
            end = _to_int(bounds[1], symbols)
            if start is None or end is None or end < start:
                continue
            name = match.group("name").lower()
            candidate = Region(match.group("name"), start, end - start + 1)
            if result.ram is None and "ram" in name:
                result.ram = candidate
            elif result.flash is None and ("rom" in name or "flash" in name):
                result.flash = candidate

    result.stack = symbols.get("__ICFEDIT_size_cstack__")
    result.heap = symbols.get("__ICFEDIT_size_heap__")

    lowered = text.lower()
    for token, label in _ICF_UNHANDLED:
        if token in lowered:
            result.unhandled.append(label)
    return result


def parse_ld(path: Path) -> MemMap:
    """解析 GCC ``.ld`` 的 ``MEMORY`` 与 ``_Min_Stack_Size``/``_Min_Heap_Size``。"""
    result = MemMap(source=f".ld ({path.name})")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)

    for match in _LD_MEMORY.finditer(body):
        origin = _to_int(match.group("origin"), {})
        length = _to_int(match.group("length"), {})
        if origin is None or length is None:
            continue
        name = match.group("name")
        attrs = match.group("attrs").lower()
        upper = name.upper()
        candidate = Region(name, origin, length)
        # 先按名字判 (FLASH/ROM -> flash, RAM -> ram), 再看属性 —— 只看属性会踩坑:
        # `RAM (xrw)` 里也含 'r', 会把它当成 flash (实测踩过)。
        if "FLASH" in upper or "ROM" in upper:
            result.flash = result.flash or candidate
        elif "RAM" in upper or "w" in attrs:
            result.ram = result.ram or candidate
        else:
            result.flash = result.flash or candidate

    for match in _LD_SYMBOL.finditer(body):
        value = _to_int(match.group("value"), {})
        if value is None:
            continue
        if "Stack" in match.group("name") or "stack" in match.group("name"):
            result.stack = value
        else:
            result.heap = value
    return result


def compare(icf: MemMap, ld: MemMap, chip: tuple[int, int] | None, device: str) -> list[str]:
    """把 .icf / .ld / 芯片真实容量 摆在一起说清楚。返回若干条告警文本。"""
    lines: list[str] = []
    icf_flash = icf.flash.size if icf.flash else None
    icf_ram = icf.ram.size if icf.ram else None
    ld_flash = ld.flash.size if ld.flash else None
    ld_ram = ld.ram.size if ld.ram else None
    chip_flash, chip_ram = chip if chip else (None, None)

    if (icf_flash, icf_ram) != (ld_flash, ld_ram):
        parts = [
            f"IAR 布局 {icf.source}: Flash {human(icf_flash)} / RAM {human(icf_ram)}",
            f"构建用 {ld.source}: Flash {human(ld_flash)} / RAM {human(ld_ram)}",
        ]
        if chip:
            parts.append(f"芯片 {device} 实际: Flash {human(chip_flash)} / RAM {human(chip_ram)}")
        verdict = ""
        if icf_flash and chip_flash and icf_flash > chip_flash:
            verdict = " —— .icf 比真机大 (厂商通用模板的常见情形), 以芯片/工程 .ld 为准"
        elif icf_flash and ld_flash and icf_flash > ld_flash:
            verdict = " —— .icf 比构建用的 .ld 大, 可用容量变小, 若固件接近上限会链接失败"
        lines.append("; ".join(parts) + verdict)

    if (icf.stack, icf.heap) != (ld.stack, ld.heap):
        note = ""
        if icf.stack and ld.stack and ld.stack < icf.stack:
            note = " —— 栈保证变小了: 深调用/大局部数组需要留意 (栈实际从 _estack 往下长, 这只是下限)"
        lines.append(
            f"栈/堆保证: .icf cstack={human(icf.stack)} heap={human(icf.heap)}"
            f" -> .ld _Min_Stack_Size={human(ld.stack)} _Min_Heap_Size={human(ld.heap)}{note}"
        )

    if icf.unhandled:
        lines.append(
            ".icf 里有本工具**未处理**的构造, 换用 .ld 后这些语义不会自动保留: "
            + "; ".join(dict.fromkeys(icf.unhandled))
        )
    return lines


def chip_exceeded(ld: MemMap, chip: tuple[int, int] | None, device: str) -> str | None:
    """构建用的 ``.ld`` 是否超出芯片**真实**容量 —— 这是"链接成功但真机跑不了"的方向。"""
    if not chip:
        return None
    chip_flash, chip_ram = chip
    limits = (("Flash", ld.flash.size if ld.flash else None, chip_flash),
              ("RAM", ld.ram.size if ld.ram else None, chip_ram))
    problems = [
        f"{label} {human(size)} > {human(limit)}"
        for label, size, limit in limits
        if size is not None and size > limit
    ]
    if not problems:
        return None
    return (
        f"链接脚本声明的内存超出芯片真实容量 ({device}): " + "; ".join(problems)
        + " —— 这样链接出来的固件在真机上跑不起来 (代码越界 / 栈顶落在不存在的 RAM 上)"
    )
