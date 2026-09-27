"""归一化工程模型 + backend 注册表 + 运行上下文。

多芯片/多生态扩展点: 加一个生态 = 写一个 backend 模块并 :func:`register`。
core 不认识任何芯片, 只认识 Backend 的六个动作。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Refusal, Result


@dataclass
class ProjectModel:
    """跨后端归一化模型。

    ``src_files``（显式文件清单, 来自 .ewp 等 IDE 工程）非空时优先于
    ``src_dirs`` 的目录扫描 —— 精确清单比"按目录约定推导"更贴近原工程。
    """

    backend: str
    root: Path
    target: str = ""
    config: str = "Debug"
    device: str = ""
    family: str = ""
    cpu: str = ""
    fpu: str = ""
    arch: str = ""
    abi: str = ""
    toolchain: str = ""
    defines: list[str] = field(default_factory=list)
    includes: list[Path] = field(default_factory=list)
    src_dirs: list[Path] = field(default_factory=list)
    src_files: list[Path] = field(default_factory=list)
    asm_srcs: list[Path] = field(default_factory=list)
    ld_script: Path | None = None
    libs: list[str] = field(default_factory=list)
    lib_paths: list[Path] = field(default_factory=list)
    opt: str = "-O0"
    dbg: str = "-g3"
    out_dir: str = "Debug"
    build_dir: str = "build"
    source: str = ""
    warnings: list[str] = field(default_factory=list)
    refusals: list[Refusal] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def summary_lines(self) -> list[str]:
        lines = [
            f"后端        : {self.backend}",
            f"工程根      : {self.root}",
            f"目标名      : {self.target}",
            f"来源        : {self.source}",
            f"配置        : {self.config}",
            f"芯片        : {self.device}  (系列 {self.family})",
            f"CPU/FPU     : {self.cpu}  {self.fpu}",
            *([f"架构/ABI    : {self.arch} {self.abi}"] if self.arch else []),
            *([f"工具链前缀  : {self.toolchain}"] if self.toolchain else []),
            f"优化/调试   : {self.opt} {self.dbg}",
            f"宏定义      : {' '.join(self.defines)}",
            f"头文件路径  : {len(self.includes)} 条",
            f"源码目录    : {' '.join(str(p) for p in self.src_dirs)}",
        ]
        if self.src_files:
            lines.append(f"源文件清单  : {len(self.src_files)} 个 .c (来自原 IDE 工程, 精确)")
        lines += [
            f"启动文件    : {' '.join(str(p) for p in self.asm_srcs)}",
            f"链接脚本    : {self.ld_script}",
            f"库          : {' '.join([str(p) for p in self.lib_paths] + list(self.libs))}",
        ]
        return lines


@dataclass
class Context:
    root: Path
    opt: dict[str, Any]
    tools: Any
    model: ProjectModel
    backend: Backend


# ---------------------------------------------------------------------------
# Backend 契约
# ---------------------------------------------------------------------------
@dataclass
class Backend:
    """后端约定。

    detect  : (root) -> 0..100 置信度
    extract : (root, opt) -> ProjectModel
    init/build/burn/doctor : (Context) -> Result
    """

    name: str
    aliases: tuple[str, ...]
    detect: Callable[[Path], int]
    extract: Callable[[Path, dict], ProjectModel]
    init: Callable[[Context], Result]
    build: Callable[[Context], Result]
    burn: Callable[[Context], Result]
    doctor: Callable[[Context], Result]


_REGISTRY: list[Backend] = []


def register(backend: Backend) -> None:
    for existing in _REGISTRY:
        if existing.name == backend.name:
            raise ValueError(f"backend 重复注册: {backend.name}")
    _REGISTRY.append(backend)


def all_backends() -> list[Backend]:
    return list(_REGISTRY)


def by_name(name: str) -> Backend | None:
    for backend in _REGISTRY:
        if backend.name == name or name in backend.aliases:
            return backend
    return None


def resolve(target: str, root: Path) -> Backend | None:
    """``auto`` 按 detect 置信度选; **显式指定也要过 detect 门槛**。

    显式 ``-Target`` 不能绕过工程识别 —— 否则在一个"只是恰好有用户
    Makefile"的无关目录里, ``build -Clean -Target stm32`` 会让 make 在那个
    错误目录执行 ``clean``/``all``, 还报"构建成功" (实测踩过: 哨兵文件被清掉、
    又执行了 all 目标)。现在这种目录 detect=0, 直接拒绝而不是动手。
    """
    if target and target != "auto":
        backend = by_name(target)
        if backend is None:
            return None
        try:
            accepted = int(backend.detect(root)) > 0
        except Exception:
            accepted = False
        return backend if accepted else None

    best: Backend | None = None
    best_score = 0
    for backend in _REGISTRY:
        try:
            score = int(backend.detect(root))
        except Exception:
            score = 0
        if score > best_score:
            best, best_score = backend, score
    return best


def load_backends() -> None:
    """导入 backends 包 —— 各模块在导入时自行 register。"""
    from . import backends  # noqa: F401  (导入即注册)


def refusal_result(refusals: list[Refusal]) -> Result:
    first = refusals[0]
    return Result(code=first.code, message=first.message, data={"refusals": refusals})
