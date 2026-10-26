"""115「导出目录树」能力：一次导出拿全库路径，替代逐目录 list。

115 的 ``files/export_dir`` 让服务端把整棵目录树生成一个文本文件，
本地只需 1 次提交 + 若干次状态轮询 + 1 次下载解析即可拿到全部路径。
相比逐目录 ``list_directory``，稳定期请求数从「目录数」降到常数级。

注意：导出结果**不含 pickcode**，因此只用于「本地 STRM 与云端路径」的差异比较，
真正落盘时仍需对差异所在目录取一次文件列表。
"""

import time
from dataclasses import dataclass
from typing import Any, Iterator

from app.log import logger

from ..common import DriveRateLimiter  # noqa: F401  # 类型提示参考

#: 状态轮询间隔（秒）；导出是服务端任务，2 秒轮询足够且温和
_POLL_INTERVAL = 2.0
#: 单次导出最长等待（秒）
_EXPORT_TIMEOUT = 600.0
#: 115 导出目录树会在网盘根目录留下的文件名后缀
_STALE_EXPORT_SUFFIX = "_目录树.txt"
#: 只清理超过该年龄（秒）的残留，避免误删并发任务正在生成的导出文件
_STALE_EXPORT_MIN_AGE = 1800.0


@dataclass(frozen=True)
class P115StrmExport:
    """把 115 导出目录树包装成可迭代的路径来源。"""

    manager: Any

    @staticmethod
    def _cleanup_stale_exports(client: Any, call: Any) -> None:
        """清理网盘根目录里历史超时任务遗留的导出目录树文件。

        115 的 ``files/export_dir`` 会把结果写成根目录下的
        ``<目录名><时间戳>_目录树.txt``。正常完成时 ``export_dir_parse_iter(delete=True)``
        会删掉它，但导出一旦超时，本插件已放弃等待，服务端任务却仍在继续跑，
        几分钟后才落盘，于是文件永久残留。这里在每次导出前顺手清掉半小时以前的残留。
        """
        try:
            response = call(client.fs_files, {"cid": 0, "show_dir": 0, "limit": 1150})
            items = (response or {}).get("data") or []
            now = time.time()
            stale = []
            for item in items:
                name = str(item.get("n") or "")
                if not name.endswith(_STALE_EXPORT_SUFFIX):
                    continue
                modified = float(item.get("te") or item.get("pt") or 0)
                if modified and now - modified < _STALE_EXPORT_MIN_AGE:
                    continue
                file_id = item.get("fid")
                if file_id:
                    stale.append(int(file_id))
            if not stale:
                return
            call(client.fs_delete, stale)
            logger.info(f"115 已清理 {len(stale)} 个残留导出目录树文件（网盘根目录）")
        except Exception as error:  # noqa: BLE001 - 清理失败不影响主流程
            logger.warning(f"清理残留导出目录树文件失败（忽略）：{error}")

    def export_directory_tree(
            self, directory_id: str, *, timeout_seconds: float = _EXPORT_TIMEOUT,
    ) -> Iterator[str]:
        from p115client.tool.export_dir import (
            export_dir_parse_iter,
            export_dir_start,
            export_dir_status,
        )

        client = getattr(self.manager, "client", None)
        if client is None:
            raise RuntimeError("115 客户端未初始化，无法导出目录树")
        limiter = getattr(self.manager, "rate_limiter", None)
        call = limiter.call if limiter is not None else (lambda func, *args, **kwargs: func(*args, **kwargs))

        self._cleanup_stale_exports(client, call)

        export_id = call(export_dir_start, client, file_ids=str(directory_id))
        logger.info(f"115 导出目录树已提交：cid={directory_id} 任务={export_id}")

        deadline = time.monotonic() + float(timeout_seconds) if timeout_seconds else None
        polls = 0
        while True:
            status = call(export_dir_status, client, export_id)
            polls += 1
            if status.get("file_id"):
                break
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"115 导出目录树超时（{timeout_seconds:.0f}s，轮询 {polls} 次）"
                )
            time.sleep(_POLL_INTERVAL)

        logger.info(
            f"115 导出目录树完成：cid={directory_id} 任务={export_id} 轮询={polls} 次"
        )
        yield from export_dir_parse_iter(
            client, export_id, parse_iter="path", delete=True,
        )
