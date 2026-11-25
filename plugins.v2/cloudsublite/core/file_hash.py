"""文件 SHA1 分块计算（唯一实现，1.12.20 Phase 1 合一）。

此前三处各自实现：drive/p115/upload.py ``_file_sha1``（8 MiB，无停止
事件）、handlers/sync/pt_upgrade.py ``_file_sha1``（8 MiB，停止事件抛
``_PtUploadCancelled``）、handlers/sync/subtitles.py ``_sha1_file`` 及
``_local_file_matches`` 内联循环（1 MiB，无停止事件）。哈希结果与分块
大小无关，统一用 8 MiB；停止事件统一抛 ``FileHashCancelled``，调用方
按需翻译成本地取消异常。
"""


from __future__ import annotations

import hashlib
from pathlib import Path
from threading import Event
from typing import Optional, Union


#: 分块读取大小：8 MiB。
FILE_SHA1_CHUNK_SIZE = 8 * 1024 * 1024


class FileHashCancelled(RuntimeError):
    """stop_event 置位导致 SHA1 计算中途取消。"""


def file_sha1(
        path: Union[str, Path],
        stop_event: Optional[Event] = None,
        *,
        chunk_size: int = FILE_SHA1_CHUNK_SIZE,
) -> str:
    """按分块读取计算文件 SHA1，返回大写十六进制摘要。"""
    digest = hashlib.sha1()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(chunk_size):
            if stop_event is not None and stop_event.is_set():
                raise FileHashCancelled(f"文件 SHA1 计算已取消：{path}")
            digest.update(chunk)
    return digest.hexdigest().upper()
