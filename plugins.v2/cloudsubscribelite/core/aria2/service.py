"""基于 aria2 daemon 的下载服务。

与 :class:`core.transfer.HttpFileDownloadService` 同签名，复用其
``_LinkRefresher`` / ``_ProgressGate``；只把"分段拼文件"交给 aria2 进程。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from app.log import logger

from .rpc import Aria2Error

#: aria2 进度轮询间隔（秒）。
POLL_INTERVAL = 0.8
#: 单任务最多换链次数。
MAX_LINK_REFRESH = 2
#: 随包分发的 aria2c 是 Aria2-Pro 补丁版（1.36.0），已解锁
#: --max-connection-per-server 上限，这里只夹到 96 防手滑。
ARIA2_CONNECTIONS_MAX = 96
#: 标准上游 aria2 的硬上限；补丁版拒绝时回落用。
ARIA2_CONNECTIONS_FALLBACK = 16
#: 触发换链的 aria2 errorCode：资源不存在 / 名称解析失败 / HTTP 授权失败。
REFRESH_ERROR_CODES = frozenset({3, 19, 24})


class Aria2FileDownloadService:
    """把 CloudFile 落到 local_path，行为语义对齐内置下载器。"""

    def __init__(
            self,
            resolver: Callable,
            timeout: int = 300,
            concurrency: int = 5,
            part_size: int = 0,
            link_refresh_interval: float = 30.0,
            link_max_age: float = 1200.0,
            max_retries: int = 6,
    ):
        self._resolver = resolver
        self._timeout = int(timeout or 300)
        self._concurrency = max(1, int(concurrency or 5))
        self._part_size = max(0, int(part_size or 0))
        self._link_refresh_interval = float(link_refresh_interval)
        self._link_max_age = float(link_max_age)
        self._max_retries = max(1, int(max_retries or 6))
        self._poll_interval = POLL_INTERVAL
        # 单测可注入假引擎；生产由模块级单例提供。
        self._engine = None

    @staticmethod
    def _control_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.aria2")

    @staticmethod
    def _safe_call(callback, *args) -> None:
        try:
            callback(*args)
        except Exception as error:  # noqa: BLE001 - 清理失败不影响主流程
            logger.debug(f"aria2 清理调用失败：{error}", exc_info=True)

    @staticmethod
    def _add_with_fallback(rpc, url: str, options: dict) -> str:
        """首次添加任务；标准上游 aria2 拒绝大连接数时回落 16 重试一次。"""
        try:
            return rpc.add_uri([url], options)
        except Aria2Error as error:
            text = str(error).lower()
            if "processing the option" not in text:
                raise
            if int(options.get("split", "0") or 0) <= ARIA2_CONNECTIONS_FALLBACK:
                raise
            logger.warning(
                f"aria2 拒绝 {options.get('split')} 连接（{error}），回落 "
                f"{ARIA2_CONNECTIONS_FALLBACK} 重试"
            )
            options["split"] = str(ARIA2_CONNECTIONS_FALLBACK)
            options["max-connection-per-server"] = str(ARIA2_CONNECTIONS_FALLBACK)
            return rpc.add_uri([url], options)

    def _cancel(self, rpc, gid: str, target: Path, preserve_partial: bool) -> None:
        self._safe_call(rpc.force_pause, gid)
        self._safe_call(rpc.remove, gid)
        self._safe_call(rpc.remove_download_result, gid)
        self._control_path(target).unlink(missing_ok=True)
        if not preserve_partial:
            target.unlink(missing_ok=True)
        # 与内置下载器的取消语义保持一致：调用方按 InterruptedError 识别"用户主动停止"，
        # 换成别的异常会被接缝层当成失败回落，反而又起一次下载。
        raise InterruptedError

    def download_file(
            self,
            file_item,
            local_path: str,
            progress_callback=None,
            stop_requested=None,
            preserve_partial: bool = False,
    ) -> str:
        from ..transfer import _LinkRefresher, _ProgressGate

        refresher = _LinkRefresher(
            lambda: self._resolver(file_item),
            refresh_interval=self._link_refresh_interval,
            max_age=self._link_max_age,
        )
        url, headers = refresher.get()
        if not url:
            raise RuntimeError("源网盘未返回下载地址")

        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        control_path = self._control_path(target)
        total = int(getattr(file_item, "size", 0) or 0)
        gate = _ProgressGate(progress_callback, total)

        from . import aria2_settings

        settings = aria2_settings()
        # 随包 aria2c 为 Aria2-Pro 补丁版，可吃 64+ 连接；96 是插件侧安全上限。
        # 若运行时换了标准上游二进制（上限 16），下面 _add_with_fallback 会回落。
        connections = max(
            1, min(int(settings.connections or 64), ARIA2_CONNECTIONS_MAX)
        )
        engine = self._engine
        if engine is None:
            from . import get_engine

            engine = get_engine()
        rpc = engine.rpc()

        options = {
            "split": str(connections),
            "max-connection-per-server": str(connections),
            "min-split-size": str(settings.min_split_size or "1M"),
            "dir": str(target.parent),
            "out": target.name,
            "continue": "true",
            "file-allocation": "none",
            "auto-file-renaming": "false",
            "allow-overwrite": "true",
            "header": [f"{key}: {value}" for key, value in headers.items()],
        }

        gid = ""
        succeeded = False
        try:
            gid = self._add_with_fallback(rpc, url, options)
            completed = 0
            refreshed = 0
            # 换链重下后 aria2 的 completedLength 会从续传点重报，用它重置进度基数，
            # 否则会按"两次累计"把进度顶过 100%。
            reseed = False
            while True:
                if stop_requested and stop_requested():
                    self._cancel(rpc, gid, target, preserve_partial)

                status = rpc.tell_status(gid, [
                    "completedLength",
                    "downloadSpeed",
                    "connections",
                    "status",
                    "errorCode",
                    "errorMessage",
                    "totalLength",
                ])
                current = int(status.get("completedLength") or 0)
                if reseed:
                    gate.seed(current)
                    reseed = False
                elif current > completed:
                    gate.add(current - completed)
                completed = current
                state = str(status.get("status") or "")
                try:
                    error_code = int(status.get("errorCode") or 0)
                except (TypeError, ValueError):
                    error_code = 0

                if state == "complete":
                    if total > 0 and completed != total:
                        raise RuntimeError(
                            f"aria2 下载文件大小不符：{completed}/{total}"
                        )
                    control_path.unlink(missing_ok=True)
                    gate.flush()
                    succeeded = True
                    return str(target)

                if state == "error" or error_code in REFRESH_ERROR_CODES:
                    if refreshed >= MAX_LINK_REFRESH:
                        raise RuntimeError(
                            f"aria2 下载失败且换链无效（gid={gid}，"
                            f"errorCode={error_code}）"
                        )
                    new_url, new_headers = refresher.get(refresh=True)
                    if not new_url:
                        raise RuntimeError(
                            f"aria2 下载失败且无法刷新直链（gid={gid}，"
                            f"errorCode={error_code}）"
                        )
                    # aria2 的 changeUri 只对"还在排队/在跑"的任务有效，失败任务会直接
                    # 报 400（实测 errorCode=1 Cannot remove URIs from GID）。这里改成
                    # 清掉失败任务后按同一 dir/out 重新 addUri：continue=true 会读
                    # .aria2 控制文件断点续传，已落盘字节不白下。
                    self._safe_call(rpc.remove, gid)
                    self._safe_call(rpc.remove_download_result, gid)
                    options["header"] = [
                        f"{key}: {value}" for key, value in (new_headers or {}).items()
                    ]
                    gid = rpc.add_uri([new_url], options)
                    reseed = True
                    refreshed += 1
                    continue

                if state in {"removed", "paused"}:
                    raise RuntimeError(
                        f"aria2 下载任务已停止（gid={gid}，状态 {state}）"
                    )
                time.sleep(self._poll_interval)
        finally:
            if gid:
                self._safe_call(rpc.remove_download_result, gid)
            if not succeeded and not preserve_partial:
                control_path.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
