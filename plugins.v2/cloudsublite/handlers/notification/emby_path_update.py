"""Emby 按文件路径发现新文件（``POST /Library/Media/Updated``）的批量合并。

v1.27.0：新文件入库/STRM 变动不再让 Emby 刷新整部剧或全库，而是把
具体文件路径报给 Emby（``UpdateType`` = Created / Modified / Deleted），
由 Emby 只处理这些文件。做法参照 p115strmhelper
（helper/mediaserver/emby.py ``notify_media_updated``、scan.py ``LibraryScan``）。

本模块只管两件事，HTTP 请求与路径映射由调用方注入：

1. **合并**：同一台 Emby（按 ``key`` 区分）在 ``window`` 秒内到达的请求
   合成一批，只发一次 ``Media/Updated``；先到的调用方当“领头”，等窗口结束
   后发送，其余调用方同步等待同一批的结果。窗口开始发送后才到的请求进入
   下一批，不会漏掉。
2. **结果归类**：返回 ``"ok"``（Emby 接受）、``"unsupported"``（接口不存在：
   404/405/501，或路径不在 Emby 任何媒体库下，报了也是空操作）、
   ``"failed"``（其它状态码或异常）。``fallback`` 给了的话，
   ``unsupported``/``failed`` 时由领头在本批内只执行一次（例如全库刷新），
   结果记为 ``"fallback_ok"`` / ``"fallback_failed"``。
"""

from __future__ import annotations

import time
from threading import Event, Lock
from typing import Any, Callable, Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

#: Emby 不支持该接口时的状态码（与 p115strmhelper 一致）。
UNSUPPORTED_STATUS_CODES = frozenset({404, 405, 501})
#: Emby 接受请求的状态码。
ACCEPTED_STATUS_CODES = frozenset({200, 202, 204})
#: 合法的 UpdateType。
UPDATE_TYPES = ("Created", "Modified", "Deleted")

Update = Tuple[str, str]  # (Emby 视角的绝对路径, UpdateType)


def _normalize(path: str) -> str:
    text = str(path or "").replace("\\", "/")
    return text.rstrip("/") or "/"


def paths_under_libraries(paths: Iterable[str], library_roots: Iterable[str]) -> bool:
    """所有路径都在某个 Emby 媒体库目录之下才返回 True。

    Emby 对不在任何媒体库下的路径也返回 204，但什么都不做；不先核对就会
    “报成功、实际没入库”。媒体库目录取不到（空）时返回 False，由调用方回退。
    """
    roots = [_normalize(root) for root in library_roots if str(root or "").strip()]
    if not roots:
        return False
    for path in paths:
        current = _normalize(path)
        if not any(current == root or current.startswith(f"{root}/") for root in roots):
            return False
    return True


def classify_status(status_code: Optional[int]) -> str:
    """把 ``Media/Updated`` 的状态码归成 ok / unsupported / failed。"""
    if status_code in ACCEPTED_STATUS_CODES:
        return "ok"
    if status_code in UNSUPPORTED_STATUS_CODES:
        return "unsupported"
    return "failed"


class _Batch:
    def __init__(self) -> None:
        self.updates: Dict[str, str] = {}
        self.fallback: Optional[Callable[[], Any]] = None
        self.done = Event()
        self.result = "failed"


class EmbyPathUpdater:
    """按 Emby 服务器合并 ``Media/Updated`` 请求；线程安全，同步返回结果。"""

    def __init__(self, window: float = 1.0) -> None:
        self._window = max(0.0, float(window))
        self._lock = Lock()
        self._pending: Dict[Hashable, _Batch] = {}

    def submit(
            self,
            key: Hashable,
            updates: Sequence[Update],
            send: Callable[[List[Dict[str, str]]], Optional[int]],
            library_roots: Optional[Callable[[], Iterable[str]]] = None,
            fallback: Optional[Callable[[], Any]] = None,
    ) -> str:
        """提交一组路径更新，返回本批结果（见模块说明）。

        :param key: 区分 Emby 服务器（含地址与密钥），配置变化后不会误合并
        :param updates: ``(Emby 路径, UpdateType)`` 列表
        :param send: 发送一批 ``Updates`` 并返回 HTTP 状态码（无响应返回 None）
        :param library_roots: 取 Emby 媒体库目录的回调；给了就先核对路径
        :param fallback: 不支持/失败时本批只执行一次的回退
        """
        cleaned = [
            (str(path), kind if kind in UPDATE_TYPES else "Created")
            for path, kind in updates
            if str(path or "").strip()
        ]
        if not cleaned:
            return "ok"
        with self._lock:
            batch = self._pending.get(key)
            leader = batch is None
            if leader:
                batch = _Batch()
                self._pending[key] = batch
            for path, kind in cleaned:
                # 同一路径同批里以最后一次为准（先建后删 → 删）。
                batch.updates[path] = kind
            if fallback is not None and batch.fallback is None:
                batch.fallback = fallback
        if not leader:
            batch.done.wait()
            return batch.result
        try:
            if self._window:
                time.sleep(self._window)
            with self._lock:
                # 窗口结束：之后到达的请求开新批。
                if self._pending.get(key) is batch:
                    self._pending.pop(key)
                payload = [
                    {"Path": path, "UpdateType": kind}
                    for path, kind in batch.updates.items()
                ]
                batch_fallback = batch.fallback
            batch.result = self._send(payload, send, library_roots)
            if batch.result != "ok" and batch_fallback is not None:
                try:
                    batch.result = "fallback_ok" if batch_fallback() else "fallback_failed"
                except Exception:  # noqa: BLE001 - 回退失败只记结果，不外抛
                    batch.result = "fallback_failed"
            return batch.result
        finally:
            with self._lock:
                if self._pending.get(key) is batch:
                    self._pending.pop(key)
            batch.done.set()

    @staticmethod
    def _send(
            payload: List[Dict[str, str]],
            send: Callable[[List[Dict[str, str]]], Optional[int]],
            library_roots: Optional[Callable[[], Iterable[str]]],
    ) -> str:
        if library_roots is not None:
            try:
                roots = list(library_roots() or [])
            except Exception:  # noqa: BLE001 - 取不到媒体库目录按不支持处理
                roots = []
            if not paths_under_libraries((item["Path"] for item in payload), roots):
                return "unsupported"
        try:
            status_code = send(payload)
        except Exception:  # noqa: BLE001 - 网络异常按失败处理，由调用方回退
            return "failed"
        return classify_status(status_code)


#: 插件内共享的合并器：两条调用路径（入库通知器、STRM/分享刷新）共用同一批次。
emby_path_updater = EmbyPathUpdater()
