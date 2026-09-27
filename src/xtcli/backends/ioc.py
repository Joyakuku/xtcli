"""``.ioc``（STM32CubeMX）解析 —— 兜底来源。

只在没有 ``.cproject`` / ``.ewp`` 时使用: 由型号推导宏, 按目录约定收集源码与
头文件路径。相比 IDE 工程的"精确清单", 这条路会有偏差, 所以必须给警告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 限深查找 .ioc 的层数上限（子目录层数, 不含工程根）
_MAX_IOC_DEPTH = 3

# 工具/产物目录: 里面的 .ioc 不是工程配置（小写比较, Windows 大小写不敏感）
_SKIP_IOC_DIRS = frozenset({
    "xtcli", "build", "build-ord", "debug", "debug-ord", "release", "node_modules",
    "__pycache__", "venv", "dist", "out", "obj", "bin",
})


@dataclass
class IocInfo:
    path: Path
    project_name: str = ""
    device: str = ""
    toolchain: str = ""
    freertos: bool = False
    key_values: dict[str, str] = field(default_factory=dict)


def find_ioc(root: Path) -> Path | None:
    """找工程的 ``.ioc``: 先看工程根, 再限深（≤3 层）看子目录。

    实测缺陷: 只 ``root.glob("*.ioc")`` 时, ``.ioc`` 被放在子目录（如 ``CubeMX/``）
    就会被判成"有源码但缺芯片信息"而误拒。

    * 只跳过工具/产物目录（``xtcli/``、``build/``、``Debug/``、``node_modules`` …）
      与点目录 —— 那里的 ``.ioc`` 不是工程配置;
    * 同一工程有多个 ``.ioc`` 时返回**最浅层、同层内字母序最前**的那个, 保证确定性。
    """
    try:
        matches = sorted(root.glob("*.ioc"))
    except OSError:  # pragma: no cover - 不可访问的目录
        return None
    if matches:
        return matches[0]

    candidates = _iter_iocs_by_depth(root, _MAX_IOC_DEPTH)
    if not candidates:
        return None
    return min(candidates, key=lambda path: (len(path.parts), path.as_posix().casefold()))


def _iter_iocs_by_depth(root: Path, max_depth: int) -> list[Path]:
    """广度优先收集 root 之下 ``max_depth`` 层内的 ``*.ioc``（跳过工具/产物目录）。"""
    found: list[Path] = []
    queue: list[tuple[Path, int]] = [(root, 0)]
    while queue:
        directory, depth = queue.pop(0)
        if depth >= max_depth:
            continue
        try:
            children = sorted(child for child in directory.iterdir() if child.is_dir())
        except OSError:  # pragma: no cover - 不可访问的目录
            continue
        for child in children:
            name = child.name.lower()
            if name.startswith(".") or name in _SKIP_IOC_DIRS:
                continue
            try:
                found.extend(sorted(child.glob("*.ioc")))
            except OSError:  # pragma: no cover - 不可访问的目录
                continue
            queue.append((child, depth + 1))
    return found


def read(root: Path) -> IocInfo | None:
    path = find_ioc(root)
    if path is None:
        return None
    kv: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        kv[key.strip()] = value.strip()

    device = ""
    for key in ("ProjectManager.DeviceId", "Mcu.UserName", "Mcu.CPN"):
        if kv.get(key):
            device = kv[key]
            break

    freertos = any(
        key.startswith("Mcu.IP") and "FREERTOS" in (value or "").upper()
        for key, value in kv.items()
    )

    return IocInfo(
        path=path,
        project_name=kv.get("ProjectManager.ProjectName", ""),
        device=device,
        toolchain=kv.get("ProjectManager.TargetToolchain", ""),
        freertos=freertos,
        key_values=kv,
    )
