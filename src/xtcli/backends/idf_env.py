"""ESP-IDF 环境解析: 从 EIM/VSCode 设置/环境变量里找出可用的 IDF 与工具。

只负责**发现与解析**, 不构建、不烧录 —— 后者在 ``espidf.py``。
解析优先级(实测踩过的坑都在注释里): 工程级设置 > 环境变量 > EIM 记录 > 用户级设置,
且每个候选路径都要做**存在性校验** (用户级 VSCode 设置里常有指向已删除目录的过期路径)。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import env

_DEFAULT_TOOLS_ROOTS = (
    r"C:\Espressif\tools",
    r"E:\env\Espressif\tools",
    r"E:\env\Espressif",
    r"E:\env\.espressif",
)
_DEFAULT_IDF_TARGET = "esp32"

# 工具族 -> 要在里面找的可执行文件名（用于定位 bin 目录, 版本号无关）
_TOOL_EXES = (
    "xtensa-esp*-elf-gcc.exe",
    "riscv32-esp-elf-gcc.exe",
    "xtensa-esp*-elf-gdb.exe",
    "riscv32-esp-elf-gdb.exe",
    "esp32ulp-elf-gcc.exe",
    "openocd.exe",
    "ninja.exe",
    "cmake.exe",
    "ccache.exe",
    "dfu-util.exe",
    "clang.exe",
)
_SEARCH_PATTERNS = ("{p}", "*/{p}", "*/*/{p}", "*/*/*/{p}")


# ===========================================================================
# 小工具: 容忍 JSONC（VSCode settings.json 里常有注释与尾逗号）
# ===========================================================================
def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    index, length = 0, len(text)
    in_string = False
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "/":
            while index < length and text[index] != "\n":
                index += 1
            continue
        if char == "/" and index + 1 < length and text[index + 1] == "*":
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                index += 1
            index += 2
            continue
        out.append(char)
        index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _load_jsonc(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        data = json.loads(_strip_jsonc(text))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _existing(path_value: Any) -> Path | None:
    if not path_value:
        return None
    try:
        path = Path(str(path_value))
    except (OSError, ValueError):
        return None
    return path if path.exists() else None


# ===========================================================================
# 环境
# ===========================================================================
@dataclass
class IdfEnv:
    idf_path: Path
    tools_path: Path
    python: Path
    version: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    path_entries: list[str] = field(default_factory=list)
    source: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def idf_py(self) -> Path:
        return self.idf_path / "tools" / "idf.py"

    @property
    def openocd(self) -> Path | None:
        for entry in self.path_entries:
            candidate = Path(entry) / "openocd.exe"
            if candidate.is_file():
                return candidate
        return None


def _settings(project_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    project = _load_jsonc(project_root / ".vscode" / "settings.json")
    appdata = os.environ.get("APPDATA")
    user: dict[str, Any] = {}
    if appdata:
        user = _load_jsonc(Path(appdata) / "Code" / "User" / "settings.json")
    return project, user


def _tools_roots() -> list[Path]:
    candidates: list[str] = []
    if os.environ.get("IDF_TOOLS_PATH"):
        candidates.append(os.environ["IDF_TOOLS_PATH"])
    if os.environ.get("ESP_IDF_TOOLS_PATH"):
        candidates.append(os.environ["ESP_IDF_TOOLS_PATH"])
    candidates.extend(_DEFAULT_TOOLS_ROOTS)
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(str(Path(userprofile) / ".espressif"))
    roots: list[Path] = []
    for item in candidates:
        path = Path(item)
        if path.is_dir() and path not in roots:
            roots.append(path)
    return roots


def _load_eim(tools_root: Path) -> list[dict[str, Any]]:
    manifest = tools_root / "eim_idf.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    installed = data.get("idfInstalled") if isinstance(data, dict) else None
    if not isinstance(installed, list):
        return []
    return [entry for entry in installed if isinstance(entry, dict)]


def _find_bin_dirs(root: Path, exe_pattern: str, max_hits: int = 2) -> list[Path]:
    """在工具族目录下找含指定可执行文件的 bin 目录（对版本号与嵌套层级都不敏感）。"""
    if not root.is_dir():
        return []
    found: list[Path] = []
    for version_dir in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        for pattern in _SEARCH_PATTERNS:
            for hit in version_dir.glob(pattern.format(p=exe_pattern)):
                if hit.is_file() and hit.parent not in found:
                    found.append(hit.parent)
            if found:
                break
        if len(found) >= max_hits:
            break
    return found[:max_hits]


def _tool_path_entries(tools_path: Path) -> list[str]:
    entries: list[str] = []
    for family in sorted((p for p in tools_path.iterdir() if p.is_dir()), key=lambda p: p.name):
        for pattern in _TOOL_EXES:
            for bin_dir in _find_bin_dirs(family, pattern, max_hits=2):
                text = str(bin_dir)
                if text not in entries:
                    entries.append(text)
    # python venv 的 Scripts 必须在 PATH 上（idf.py 与 esptool 都在里面）
    for venv in sorted(tools_path.glob("python/*/venv/Scripts"), reverse=True):
        if venv.is_dir() and str(venv) not in entries:
            entries.append(str(venv))
    return entries


def _idf_version(idf_path: Path) -> str:
    version_file = idf_path / "version.txt"
    if version_file.is_file():
        try:
            return version_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[0]
        except OSError:
            pass
    cmake_version = idf_path / "tools" / "cmake" / "version.cmake"
    if cmake_version.is_file():
        try:
            text = cmake_version.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        major = re.search(r"^set\(IDF_VERSION_MAJOR\s+(\d+)", text, re.M)
        minor = re.search(r"^set\(IDF_VERSION_MINOR\s+(\d+)", text, re.M)
        patch = re.search(r"^set\(IDF_VERSION_PATCH\s+(\d+)", text, re.M)
        if major and minor:
            parts = [major.group(1), minor.group(1)]
            if patch:
                parts.append(patch.group(1))
            return ".".join(parts)
    return ""


def resolve(project_root: Path) -> tuple[IdfEnv | None, list[str]]:
    """解析可用的 IDF 环境。返回 (env, warnings); 解析不到返回 (None, ...)。

    ``project_root`` 收字符串也认 (内部统一转 ``Path``): 这个函数是后端的对外入口,
    被库调用方按字符串传进来时不该抛裸 ``TypeError``。
    """
    project_root = Path(project_root)
    warnings: list[str] = []
    project_settings, user_settings = _settings(project_root)

    # --- IDF 路径: 工程级 > 环境变量 > 用户级 ---
    idf_path = _existing(project_settings.get("idf.currentSetup"))
    if idf_path is None:
        idf_path = _existing(os.environ.get("IDF_PATH"))
    if idf_path is None:
        idf_path = _existing(user_settings.get("idf.espIdfPathWin") or user_settings.get("idf.espIdfPath"))
    if idf_path is None and (raw := user_settings.get("idf.espIdfPathWin") or user_settings.get("idf.espIdfPath")):
        warnings.append(f"VSCode 用户配置里的 ESP-IDF 路径不存在, 已忽略: {raw}")

    # --- tools 根 + EIM 清单 ---
    eim_entries: list[tuple[Path, dict[str, Any]]] = []
    for tools_root in _tools_roots():
        for entry in _load_eim(tools_root):
            eim_entries.append((tools_root, entry))

    tools_path: Path | None = _existing(os.environ.get("IDF_TOOLS_PATH"))
    python_exe: Path | None = None
    source = ""

    # 优先选与 idf_path 匹配的 EIM 条目（同一次安装的 tools/python 才配套）
    matched: dict[str, Any] | None = None
    for _tools_root, entry in eim_entries:
        entry_path = _existing(entry.get("path"))
        if idf_path is not None and entry_path is not None and env.real_path(entry_path) == env.real_path(idf_path):
            matched = entry
            break
    if matched is None and eim_entries:
        for tools_root, entry in eim_entries:
            manifest = tools_root / "eim_idf.json"
            try:
                selected = json.loads(manifest.read_text(encoding="utf-8", errors="replace")).get("idfSelectedId")
            except (OSError, ValueError):
                selected = None
            if selected and entry.get("id") == selected:
                matched = entry
                break
    if matched is None and eim_entries:
        matched = eim_entries[0][1]

    if matched:
        source = "eim"
        if idf_path is None:
            idf_path = _existing(matched.get("path"))
        if tools_path is None:
            tools_path = _existing(matched.get("idfToolsPath"))
        python_exe = _existing(matched.get("python"))
        if python_exe is None and tools_path is not None:
            for venv in sorted(tools_path.glob("python/*/venv/Scripts/python.exe"), reverse=True):
                python_exe = venv
                break

    if idf_path is None:
        return None, [*warnings, "找不到可用的 ESP-IDF 安装 (IDF_PATH)"]

    if tools_path is None:
        tools_path = _existing(user_settings.get("idf.toolsPathWin"))
        if tools_path is None:
            tools_path = idf_path.parent  # 兜底: 与 IDF 同级
        source = source or "settings"

    if python_exe is None:
        python_exe = _existing(user_settings.get("idf.pythonInstallPath"))
    if python_exe is None and tools_path is not None:
        for venv in sorted(tools_path.glob("python/*/venv/Scripts/python.exe"), reverse=True):
            python_exe = venv
            break
    if python_exe is None:
        return None, [*warnings, f"找不到 ESP-IDF 的 python 环境 (在 {tools_path} 下未找到 python/*/venv)"]

    if not (idf_path / "tools" / "idf.py").is_file():
        return None, [*warnings, f"IDF_PATH 下没有 tools/idf.py: {idf_path}"]

    idf_env = IdfEnv(idf_path=idf_path, tools_path=tools_path, python=python_exe, source=source)
    idf_env.version = _idf_version(idf_path)
    idf_env.path_entries = _tool_path_entries(tools_path)

    variables: dict[str, str] = {
        "IDF_PATH": str(idf_path),
        "IDF_TOOLS_PATH": str(tools_path),
        "IDF_PYTHON_ENV_PATH": str(python_exe.parent.parent),
    }
    rom_elfs = sorted(tools_path.glob("esp-rom-elfs/*"), reverse=True)
    if rom_elfs:
        variables["ESP_ROM_ELF_DIR"] = str(rom_elfs[0])
    scripts = sorted(tools_path.glob("openocd-esp32/*/openocd-esp32/share/openocd/scripts"), reverse=True)
    if scripts:
        variables["OPENOCD_SCRIPTS"] = str(scripts[0])

    custom_vars = project_settings.get("idf.customExtraVars")
    target = ""
    if isinstance(custom_vars, dict) and custom_vars.get("IDF_TARGET"):
        target = str(custom_vars["IDF_TARGET"])
    if not target:
        target = _sdkconfig_target(project_root)
    if target:
        variables["IDF_TARGET"] = target

    # ESP_IDF_VERSION 必须显式设置: IDF v6.x 的组件管理器会直接 coerce 它,
    # 缺失时会在 idf_component_manager 深处抛 TypeError（实测, 报错完全指不到原因）
    if idf_env.version:
        variables["ESP_IDF_VERSION"] = _major_minor(idf_env.version)
    else:
        warnings.append("未能确定 IDF 版本 (ESP_IDF_VERSION 将缺失, idf.py 可能报 TypeError)")

    # 组件管理器离线解析用（EIM 会设, 这里做兜底）
    variables.setdefault("IDF_COMPONENT_LOCAL_STORAGE_URL", f"file://{tools_path}")

    # 再并入 EIM 激活脚本里其余的变量（我们已推导的键优先）
    activation = matched.get("activationScript") if matched else None
    extra_script = Path(str(activation)) if activation else None
    for key, value in _eim_activation_vars(extra_script).items():
        variables.setdefault(key, value)

    idf_env.variables = variables

    # --- 校验关键工具在位 ---
    if not idf_env.openocd:
        warnings.append("未找到 openocd-esp32 (JTAG 烧录会不可用; UART/esptool 不受影响)")
    if target:
        compiler = _compiler_name(target)
        if compiler and not any((Path(entry) / compiler).is_file() for entry in idf_env.path_entries):
            warnings.append(f"PATH 里没找到 {compiler}（该 target 的工具链可能未装全）")

    return idf_env, warnings


def _compiler_name(target: str) -> str:
    """由 IDF target 推出编译器可执行名（用于校验工具链是否装全）。"""
    if not target:
        return ""
    if target.startswith(("esp32c", "esp32h", "esp32p")):
        return "riscv32-esp-elf-gcc.exe"
    return f"xtensa-{target}-elf-gcc.exe"


def _major_minor(version: str) -> str:
    parts = [p for p in re.split(r"[.\-+]", version or "") if p.isdigit()]
    return ".".join(parts[:2]) if len(parts) >= 2 else (version or "")


_ENV_PAIR_RE = re.compile(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*=\s*"([^"]*)"')


def _eim_activation_vars(script: Path | None) -> dict[str, str]:
    """读出 EIM 激活脚本里设置的**全部**环境变量。

    刻意"照搬 EIM 设的每一个变量", 而不是只挑我们认为需要的: 实测 IDF v6.0.1 的
    组件管理器扩展会直接 ``Version.coerce(os.getenv('ESP_IDF_VERSION'))``, 缺这个
    变量会在 ``idf_component_manager`` 深处抛
    ``TypeError: expected string or bytes-like object, got 'NoneType'`` —— 报错完全
    指不到真正原因。凡是 EIM 设的我们照搬, 未来 EIM 新增变量也能自动跟上。
    """
    if script is None:
        return {}
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    block = re.search(r"\$env_var_pairs\s*=\s*@\{(.*?)\n\s*\}", text, re.S)
    if not block:
        return {}
    return {
        key: value
        for key, value in _ENV_PAIR_RE.findall(block.group(1))
        if key not in ("PATH", "SYSTEMPATH", "SYSTEM_PATH")
    }


def _sdkconfig_target(project_root: Path) -> str:
    for name in ("sdkconfig", "sdkconfig.defaults"):
        path = project_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'^CONFIG_IDF_TARGET="([^"]+)"', text, re.M)
        if match:
            return match.group(1)
    return ""


def list_com_ports() -> list[str]:
    """枚举串口（用 winreg, 不引入 pyserial）。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            ports: list[str] = []
            index = 0
            while True:
                try:
                    _, value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                ports.append(str(value))
                index += 1
            return sorted(set(ports))
    except OSError:
        return []


# ===========================================================================
# 工程特征
