"""命令链表 —— 命令名 → (动词 + 预设选项)。

为什么要有这张表: 命令名原先散落在四处 (``setup.ps1`` 的 shim 表、``cli.show_help``、
两份手册、``tests/test_architecture.py``), 改名时必然漏掉一处。现在**只有这里**定义,
``tests/test_commands.py`` 断言 setup.ps1 生成的 shim / 帮助 / 文档与它逐项一致。

约定 (让两种写法都自然):

* ``name`` 是**短名** (``build-all``): ``xtcli build-all`` 直接可用;
* shim 名由它派生 (``xtcli-build-all``), 也就是 PATH 上的命令;
* ``by_name()`` 两种写法都认, 且不区分是否带 ``xtcli-`` 前缀。

设计约束:

* **预设选项只是前缀**: 用户后面写的参数照旧生效 ——
  ``xtcli-build-all -MakeTarget size`` = ``build -Clean -MakeTarget size``。
* **不加动词**: 这张表只加"命令名"这一层糖, 不扩大 ``cli.VERBS``。
* ``burn`` 默认链是 ``auto`` (优先 openocd; 本机没有可用 openocd 时回落 pyOCD 并说明
  原因), ``burn-openocd`` / ``burn-flash`` 才是"锁死某一条链"。
* v1.0.0 起**不再保留历史命令名**: 更早的 ``xtcli-*-pj`` / ``stm32-*-pj`` /
  ``esp32-*-pj`` / ``pj-*`` 由 ``xtcli-setup`` 从 PATH 上清掉 (见 setup.ps1 的
  ``$staleShims``), 而不是继续当别名。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chain:
    """一个命令名: 展开成 ``[verb, *preset, *用户参数]``。"""

    name: str  # 短名 (不带 xtcli- 前缀)
    verb: str
    preset: tuple[str, ...] = ()
    summary: str = ""

    @property
    def command(self) -> str:
        """PATH 上的命令名 / 文档里写的名字。"""
        return f"xtcli-{self.name}"


CHAINS: tuple[Chain, ...] = (
    Chain("init", "init", summary="初始化到可编译 (必要时试编译一次)"),
    Chain("build", "build", summary="增量构建 (已是最新就只重链接)"),
    Chain("build-all", "build", preset=("-Clean",), summary="全量重建 (先清理再编译)"),
    Chain("burn", "burn", summary="烧录 + 回读校验 (默认 openocd 链, 不可用时说明原因)"),
    Chain("burn-openocd", "burn", preset=("-Flasher", "openocd"), summary="烧录: 锁 openocd 链"),
    Chain("burn-flash", "burn", preset=("-Flasher", "pyocd"),
          summary="烧录: CMSIS-Pack flash 算法链 (pyOCD, 不需要 openocd target 配置)"),
    Chain("doctor", "doctor", summary="只读体检: 环境 + 工程 + 全局开关"),
)


def by_name(token: str) -> Chain | None:
    """按短名或命令名 (``build-all`` / ``xtcli-build-all``) 找命令。"""
    bare = token[6:] if token.startswith("xtcli-") else token
    for chain in CHAINS:
        if chain.name == bare:
            return chain
    return None


def shim_names() -> tuple[str, ...]:
    """由 setup.ps1 生成 shim 的命令名 (与 CHAINS 同序, 便于人读)。"""
    return tuple(chain.command for chain in CHAINS)


def expand(token: str, rest: list[str]) -> list[str] | None:
    """命令名 → ``[verb, *preset, *rest]``; 不认识的名字返回 ``None``。"""
    chain = by_name(token)
    if chain is None:
        return None
    return [chain.verb, *chain.preset, *rest]
