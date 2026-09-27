"""后端包 —— 导入本包即自动注册全部后端。

按文件名顺序发现并导入 ``*.py``，所以"加一个生态"只需往这个目录放一个文件
（模块在导入时调用 :func:`xtcli.model.register`）。
"""

from __future__ import annotations

import importlib
import pkgutil


def _load_all() -> list[str]:
    loaded: list[str] = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{info.name}")
        loaded.append(info.name)
    return loaded


LOADED = _load_all()

__all__ = ["LOADED"]
