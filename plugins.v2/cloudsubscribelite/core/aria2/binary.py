"""aria2c 内嵌二进制落地与校验。

压缩包随插件包分发（``bin/aria2c-linux-x86_64.xz``），运行时解压到插件
数据目录后 chmod 755，并用 ``--version`` 做一次冒烟探测。目标文件已存在
且 sha256 一致时直接复用，避免每次启动重复写 9MB。
"""

from __future__ import annotations

import hashlib
import lzma
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

BINARY_SHA256 = "c7944f5e27212e0bd37b761f888039eea6e5cf29342e2f86d3f0075118586432"
BINARY_NAME = "aria2c"


def _archive_path() -> Path:
    """随包压缩包位置：``<插件根>/bin/aria2c-linux-x86_64.xz``。"""
    return Path(__file__).resolve().parents[2] / "bin" / "aria2c-linux-x86_64.xz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _supported_platform() -> bool:
    if sys.platform != "linux":
        return False
    machine = str(platform.machine() or "").strip().lower()
    return machine in {"x86_64", "amd64"}


def ensure_binary(target_dir: Path) -> tuple[Optional[Path], str]:
    """把内嵌 aria2c 落到 ``target_dir`` 并校验。

    成功返回 ``(path, "")``；失败返回 ``(None, 原因)``，原因限定为
    ``unsupported-platform`` / ``archive-missing`` / ``checksum-mismatch`` /
    ``probe-failed``，便于日志与单测断言。
    """
    if not _supported_platform():
        return None, "unsupported-platform"

    target = Path(target_dir) / BINARY_NAME
    try:
        if not (target.is_file() and _sha256(target) == BINARY_SHA256):
            archive = _archive_path()
            if not archive.is_file():
                return None, "archive-missing"
            payload = lzma.decompress(archive.read_bytes())
            if hashlib.sha256(payload).hexdigest() != BINARY_SHA256:
                return None, "checksum-mismatch"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        target.chmod(0o755)
        probe = subprocess.run(
            [str(target), "--version"],
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except Exception:  # noqa: BLE001 - 解压/探测失败统一按探针失败处理
        return None, "probe-failed"

    if probe.returncode != 0 or b"aria2 version" not in (probe.stdout or b""):
        return None, "probe-failed"
    return target, ""
