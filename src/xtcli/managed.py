"""受管文件登记与覆盖保护。

背景 —— 这些都是审查中**实测到的破坏性行为**:

* ``.cproject`` 里声明的 .ld 解析到工程外时, 旧实现把工程内同名的
  ``STM32F103RCTX_FLASH.ld`` 从 5376 字节覆盖成 55 字节, 日志还谎称"原先不存在";
* ``compile_commands.json`` 被静默覆盖 —— 它可能是 CMake / bear / compiledb 的产物;
* ``rules.mk`` / ``config.mk`` 无条件覆盖, 用户在里面的本地修改直接丢失。

做法: 工具写的每个文件都登记到 ``<root>/xtcli/.owned.json``（工程相对路径 +
SHA256）。覆盖前分三种情况处理:

===============  ==================================================
文件不存在        直接写, 登记
已登记且哈希未变  是我们生成且没被改过 -> 直接写
其它              外来文件, 或登记过但被用户改过 -> 先备份成
                  ``<名字>.xtcli-bak``（已有备份就不动它）, 告警, 再写
===============  ==================================================

备份文件本身**不登记**为受管 —— 它是用户的东西, 清理时不能删。
这个清单也是将来 ``clean`` / ``uninit`` 只删自己东西的依据。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from . import env, log

MANIFEST_NAME = ".owned.json"
MANIFEST_PARENT = "xtcli"
BAK_SUFFIX = ".xtcli-bak"
_MANIFEST_VERSION = 1


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_PARENT / MANIFEST_NAME


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load(root: Path) -> dict[str, str]:
    """读登记表; 缺失/损坏时返回空表（当作"什么都没登记过", 于是更保守）。"""
    try:
        data = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(key): str(value) for key, value in files.items()}


def save(root: Path, entries: dict[str, str]) -> None:
    path = manifest_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "version": _MANIFEST_VERSION,
            "tool": "xtcli",
            "comment": "xtcli 生成的文件清单 (路径 + SHA256); 覆盖前据此判断能不能直接写",
            "files": dict(sorted(entries.items())),
        },
        ensure_ascii=False,
        indent=2,
    )
    env.atomic_write_text(path, payload + "\n")


def is_owned_and_unmodified(root: Path, path: Path, entries: dict[str, str] | None = None) -> bool:
    """文件是否"由本工具生成且此后没被改过"。"""
    table = load(root) if entries is None else entries
    try:
        rel = env.relative_to(root, path)
    except ValueError:  # pragma: no cover - 工程外的文件不参与受管
        return False
    known = table.get(rel)
    if known is None or not path.is_file():
        return False
    try:
        return known == _sha256(path)
    except OSError:  # pragma: no cover
        return False


class Owned:
    """一次 init 期间的登记表; 用完 ``flush()`` 落盘。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.entries: dict[str, str] = load(root)
        self.backups: list[Path] = []
        self.created: list[Path] = []

    def _rel(self, path: Path) -> str:
        return env.relative_to(self.root, path)

    def _protect(self, path: Path, unchanged: bool = False) -> None:
        """覆盖前保护: 外来文件或被用户改过的文件先备份。

        ``unchanged`` 表示"要写入的内容与现有内容完全一致" —— 这时覆盖等于没变,
        没有任何东西会丢, 不必产生备份噪声 (升级后第一次 init 的常见情形)。
        """
        if not path.is_file() or unchanged:
            return
        rel = self._rel(path)
        known = self.entries.get(rel)
        try:
            current = _sha256(path)
        except OSError:  # pragma: no cover
            return
        if known is not None and known == current:
            return  # 是本工具生成且没被改过 -> 可以放心覆盖
        reason = "该文件不是 xtcli 生成的" if known is None else "该文件在生成后被改过"
        backup = path.with_name(path.name + BAK_SUFFIX)
        if backup.is_file():
            log.warn(f"{reason}; 已有备份 {backup.name} 保留不动, 继续覆盖: {rel}")
        else:
            shutil.copyfile(path, backup)
            log.warn(f"{reason}; 已备份为 {backup.name} 再覆盖: {rel}")
        self.backups.append(backup)

    @staticmethod
    def _same_bytes(path: Path, data: bytes) -> bool:
        """文件字节与将要写入的字节是否完全一致。

        ``env.atomic_write_text`` 用 ``newline=""`` 写, 不做换行翻译, 所以这里可以
        直接按字节比 —— 比文本比较更严谨(不受平台换行/编码差异影响)。
        比不出来就返回 False, 那就老老实实备份。
        """
        try:
            return path.read_bytes() == data
        except OSError:  # pragma: no cover
            return False

    def write_text(self, path: Path, text: str) -> None:
        existed = path.is_file()
        self._protect(path, unchanged=existed and self._same_bytes(path, text.encode("utf-8")))
        env.atomic_write_text(path, text)
        self.entries[self._rel(path)] = _sha256(path)
        if not existed:
            self.created.append(path)

    def copy_file(self, src: Path, dst: Path) -> None:
        existed = dst.is_file()
        unchanged = False
        if existed:
            try:
                unchanged = self._same_bytes(dst, src.read_bytes())
            except OSError:  # pragma: no cover
                unchanged = False
        self._protect(dst, unchanged=unchanged)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        self.entries[self._rel(dst)] = _sha256(dst)
        if not existed:
            self.created.append(dst)

    def own_existing(self, path: Path) -> None:
        """把已存在的文件登记为受管。

        只用于"上次生成过、这次沿用"的文件 —— 调用方必须先确认内容仍是原样的
        模板, 否则会把用户的修改当成我们的产物。
        """
        if path.is_file():
            self.entries[self._rel(path)] = _sha256(path)

    def flush(self) -> None:
        save(self.root, self.entries)
