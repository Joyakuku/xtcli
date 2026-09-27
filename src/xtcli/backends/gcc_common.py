"""构建与烧录共用的小工具 (make 计划 / 产物新鲜度 / 拒绝结果)。

放这里是为了让 gcc_make 与 gcc_burn 都能用, 而**不必互相导入** (会成环)。
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from .. import log
from ..errors import Result
from ..makefiles import config_mk
from ..model import ProjectModel
from .gcc_sources import collect_cpp_sources, collect_sources


# ===========================================================================
# build
# ===========================================================================
def _make_plan(model: ProjectModel) -> tuple[str, bool] | None:
    if (model.root / "xtcli" / "Makefile").is_file():
        return "xtcli/Makefile", True
    if (model.root / "Makefile").is_file():
        return "Makefile", False
    return None


def _check_fingerprint(model: ProjectModel) -> None:
    config_path = model.root / "xtcli" / "config.mk"
    if not config_path.is_file():
        return
    found = config_mk.read_fingerprint(config_path)
    expected = config_mk.fingerprint(model)
    if found is None:
        log.warn("xtcli/config.mk 由本工具旧版本生成 (无指纹), 建议重新运行 xtcli-init-pj")
    elif found != expected:
        log.warn("xtcli/config.mk 与工程当前配置不一致 (陈旧或被改过)")
        log.info(f"  文件指纹 {found}   当前 {expected}")
        log.info("  建议先运行 xtcli-init-pj 重新生成, 否则可能编出与预期不符的固件")


def _artifact_path(model: ProjectModel, ext: str) -> Path:
    return model.root / model.out_dir / f"{model.target}.{ext}"


def _check_artifact_fresh(model: ProjectModel, artifact: Path) -> None:
    """make 成功但产物缺失/比最新输入旧 → 告警, 防"假成功"。"""
    if not artifact.is_file():
        log.warn(f"构建报告成功, 但产物不存在: {artifact}")
        return
    try:
        artifact_mtime = artifact.stat().st_mtime
    except OSError:  # pragma: no cover
        return
    c_files, s_files = collect_sources(model)
    newest = 0.0
    for path in list(c_files) + list(s_files) + collect_cpp_sources(model):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    if model.ld_script and model.ld_script.is_file():
        with contextlib.suppress(OSError):
            newest = max(newest, model.ld_script.stat().st_mtime)
    if newest and artifact_mtime < newest:
        log.warn("产物比源文件旧 —— 可能没有真正重新编译 (检查 make 是否因陈旧目标而跳过)")


def _refusal_result(model: ProjectModel) -> Result | None:
    """有确定性拒绝时, 任何动作都应报告它, 而不是报"缺少构建入口"之类的次要原因。"""
    if not model.refusals:
        return None
    first = model.refusals[0]
    log.err(first.message)
    for item in first.missing:
        log.info(f"缺少: {item}")
    if first.next_step:
        log.info(f"建议: {first.next_step}")
    return Result(code=first.code, message=first.message)


