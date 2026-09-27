"""工程定位: 从当前目录向上找工程根。"""

from __future__ import annotations

from pathlib import Path

MARKERS = (
    ".cproject",
    "Makefile",
    "*.ioc",
    "*.ewp",
    "*.uvprojx",
    "CMakeLists.txt",
    "platformio.ini",
)

_MAX_UP = 6

# 最近一次 find_root() 的说明: 命中标记时为 None。放在模块级是为了**不改**
# find_root() 的签名（其它调用方已经在用位置参数调用它）。
_LAST_SEARCH_NOTE: str | None = None

_MARKER_HINT = "Makefile/.cproject/*.ewp/*.ioc/.uvprojx/CMakeLists.txt 等"


def last_root_search_note() -> str | None:
    """最近一次 :func:`find_root` 的诊断说明; 命中标记（正常定位）时为 ``None``。

    每次 ``find_root()`` 开头都会重置, 所以拿到的永远是**本次**调用的结果。
    两种"没定位到"的说明刻意分开, 因为对用户的意义相反:

    * 更上层找到了标记（只是超出上限）::

        工程标记在 6 层之上 (E:\\...\\a) —— 超出查找上限 (6 层), 未把当前目录当作工程根

    * 整条祖先链都没有标记（目录确实不是工程）::

        向上 6 层未找到工程标记（Makefile/.cproject/... 等），已停止查找
    """
    return _LAST_SEARCH_NOTE


def _has_marker(directory: Path) -> bool:
    for marker in MARKERS:
        try:
            if next(directory.glob(marker), None) is not None:
                return True
        except OSError:  # pragma: no cover
            continue
    return False


def find_root(start: str | Path | None = None) -> Path | None:
    """确定工程根。

    * 传文件路径 -> 取其所在目录;
    * 从该目录向上最多 6 层找标记文件（.cproject / Makefile / *.ioc / ...）;
    * 层数用尽仍未命中时, 若**更上层还有标记** -> 返回 ``None``: 真正的工程根只是
      太远, 绝不能把起点目录冒充成工程根（调用方会报"当前目录无效"并退出 4）;
    * 层数用尽且整条祖先链都没有标记 -> 返回起点目录, 由后端给出确定性拒绝
      （退出码 5 = 没有任何后端认得这个目录; 这是既有对外契约, 见 errors.py）。

    诊断文本见 :func:`last_root_search_note`。
    """
    global _LAST_SEARCH_NOTE
    _LAST_SEARCH_NOTE = None

    base = Path.cwd() if start is None else Path(start)
    try:
        base = base.resolve()
    except OSError:
        return None
    if not base.exists():
        return None
    if base.is_file():
        base = base.parent

    current: Path | None = base
    depth = 0
    while depth < _MAX_UP and current is not None:
        if _has_marker(current):
            return current
        current = current.parent
        depth += 1

    # 上限之外继续探测（只探测, 不采用）: 命中说明"标记只是太远" —— 这条 note 的
    # 价值就是把真正的工程根指出来, 绝不能说成"未找到"（那会把用户引向
    # "目录确实不是工程"的错误结论）。
    probe: Path | None = current
    probe_depth = depth
    while probe is not None:
        if _has_marker(probe):
            _LAST_SEARCH_NOTE = (
                f"工程标记在 {probe_depth} 层之上 ({probe}) —— 超出查找上限 ({_MAX_UP} 层), "
                "未把当前目录当作工程根; 请到该目录再执行"
            )
            return None
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
        probe_depth += 1

    # 整条祖先链都没有标记: 目录确实不是工程, 保持旧行为交给后端拒绝（退出码 5）。
    _LAST_SEARCH_NOTE = f"向上 {depth} 层未找到工程标记（{_MARKER_HINT}），已停止查找"
    return base
