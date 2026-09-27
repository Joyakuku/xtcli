"""xtcli.json —— 工具版本缓存 / 探针缓存 / venv 环境记录。

兼容 PowerShell 版写下的旧文件（没有 schema_version 与 env 段）。
"""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Any

from . import env
from .log import warn

SCHEMA_VERSION = 1


def _empty() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "versions": {}, "probe": {}, "env": {}}


def load() -> dict[str, Any]:
    path = env.config_path()
    if not path.is_file():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"配置文件解析失败, 已忽略: {path} ({exc})")
        return _empty()
    if not isinstance(raw, dict):
        return _empty()

    cfg = _empty()
    for key in ("versions", "probe", "env"):
        value = raw.get(key)
        if isinstance(value, dict):
            cfg[key] = value
    if isinstance(raw.get("roots"), list):
        cfg["roots"] = raw["roots"]
    schema = raw.get("schema_version")
    cfg["schema_version"] = int(schema) if isinstance(schema, int) else 0
    return cfg


def save(cfg: dict[str, Any]) -> None:
    cfg["schema_version"] = SCHEMA_VERSION
    # 缓存写不进去不应该让主流程失败
    with contextlib.suppress(OSError):
        env.atomic_write_text(env.config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# 工具版本缓存
# ---------------------------------------------------------------------------
def cached_version(cfg: dict[str, Any], exe: str) -> str | None:
    versions = cfg.get("versions") or {}
    value = versions.get(exe.lower())
    return str(value) if value else None


def remember_version(cfg: dict[str, Any], exe: str, version: str) -> None:
    cfg.setdefault("versions", {})[exe.lower()] = version


# ---------------------------------------------------------------------------
# 探针缓存
# ---------------------------------------------------------------------------
def cached_probe(cfg: dict[str, Any]) -> dict[str, Any] | None:
    probe = cfg.get("probe")
    if isinstance(probe, dict) and probe.get("interface"):
        return probe
    return None


def remember_probe(cfg: dict[str, Any], name: str, interface: str) -> None:
    cfg["probe"] = {"name": name, "interface": interface, "at": _now()}


# ---------------------------------------------------------------------------
# venv 环境记录（由 setup.ps1 写入）
# ---------------------------------------------------------------------------
def remember_env(cfg: dict[str, Any], *, base: str, base_prefix: str, version: str) -> None:
    cfg["env"] = {
        "base": base,
        "base_prefix": base_prefix,
        "version": version,
        "created_at": _now(),
    }


def _now() -> str:
    """带时区的时间戳（避免 naive datetime）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
