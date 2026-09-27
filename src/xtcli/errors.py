"""退出码契约与结果对象。

退出码是对外承诺, 脚本化调用（`xtcli-build && xtcli-burn`）依赖它,
不得随意改动（`tests/test_cli.py` 逐条锁定）。

自动识别与显式 ``-Target`` 都必须先通过工程的 detect 门槛: "只是恰好有用户
Makefile"的无关目录返回 5 (没有任何后端认得它), 而不是 4 (认出了工程形态但缺东西)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Exit(IntEnum):
    OK = 0           # 成功
    FAILURE = 1      # 未分类失败
    USAGE = 2        # 参数错误
    ENVIRONMENT = 3  # 工具链 / 探针等环境缺失
    PROJECT = 4      # 认出了工程形态, 但缺必要内容或被拒绝 (缺 .ioc / Core/Src ...)
    UNSUPPORTED = 5  # 没有任何后端认得这个目录 (-Target 也过不了工程识别门槛)
    BUILD = 6        # 构建失败
    BURN = 7         # 烧录失败


@dataclass
class Result:
    """动作的返回值; 由入口统一映射到退出码。"""

    code: int = int(Exit.OK)
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return int(self.code) == int(Exit.OK)


@dataclass
class Refusal:
    """确定性拒绝: 不是工程 / 工程被破坏 / 不支持该形态。

    `missing` 让用户知道"缺什么", `next_step` 给出下一步; 拒绝时绝不写盘。
    """

    message: str
    missing: list[str] = field(default_factory=list)
    next_step: str = ""
    code: int = int(Exit.PROJECT)


class XtError(Exception):
    """需要中止并映射到退出码的错误。"""

    def __init__(self, message: str, code: int = int(Exit.FAILURE), hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = int(code)
        self.hint = hint
