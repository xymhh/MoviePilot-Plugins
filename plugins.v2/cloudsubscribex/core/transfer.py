"""跨网盘传输编排：优先目标端秒传，失败后回退本地上传。"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock, Semaphore, Thread
from typing import Callable, Mapping, Optional

import requests
from requests.adapters import HTTPAdapter

from app.log import logger

from .cloud import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudFile,
    RapidUploadResult,
)
from .relay import RelayClient, RelayError


def _fd_safe_workers(requested: int, ceiling: int = 256) -> int:
    """按进程文件句柄软上限收敛分片并发数。

    每个分片至少占一个 socket，句柄上限低时高并发会直接撞爆
    ``Errno 24 Too many open files``（并把速度拖到接近单连接水平）。
    插件加载时已尝试把 soft 抬到 65535；万一抬不动，这里兜底收敛。
    """
    try:
        import resource

        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        soft = int(soft)
    except Exception:  # noqa: BLE001 - 取不到上限时按保守值处理
        return max(1, min(int(requested), 32))
    if soft <= 0:
        return max(1, min(int(requested), ceiling))
    return max(1, min(int(requested), max(8, soft // 8), ceiling))


class _RangeDownloadUnsupported(RuntimeError):
    pass


class RapidMissAborted(RuntimeError):
    """秒传未命中且用户设置为「中止并清理」：不真实上传、不留缓存。"""


class _RemoteSha1RangeReader:
    """abort 秒传探测：按 115 二次校验范围从源盘直链读取并返回大写 SHA1。

    sign_check 由 115 下发、形如 "start-end"（见 p115client upload_file_init）；
    只读取该小段并返回其 SHA1，不落本地文件、不产生上传流量。
    """

    def __init__(
            self, url: str, headers: Mapping | None, size: int,
            stop_requested: Optional[Callable[[], bool]] = None,
    ):
        self._url = str(url or "")
        self._headers = dict(headers or {})
        self._size = int(size or 0)
        self._stop_requested = stop_requested

    def read_range_sha1(self, value: str) -> str:
        if self._stop_requested and self._stop_requested():
            raise InterruptedError
        try:
            start, end = (int(part) for part in str(value).split("-", 1))
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效的 115 校验范围：{value}") from error
        if not self._url or start < 0 or end < start or end >= self._size:
            raise ValueError(f"115 校验范围不可用：{value}/{self._size}")
        headers = dict(self._headers)
        headers["Range"] = f"bytes={start}-{end}"
        headers.setdefault("Accept-Encoding", "identity")
        response = requests.get(self._url, headers=headers, timeout=(15, 60))
        if response.status_code != 206:
            raise IOError(f"源盘不支持 HTTP Range：HTTP {response.status_code}")
        data = response.content
        if len(data) != end - start + 1:
            raise IOError(f"源盘 Range 读取不完整：{len(data)}/{end - start + 1}")
        return hashlib.sha1(data).hexdigest().upper()


def _describe_transfer_error(error: BaseException) -> str:
    """把常见底层报错翻译成可操作的提示。"""
    message = str(error)
    lowered = message.lower()
    if "too many open files" in lowered or "errno 24" in lowered:
        return (
            f"{message[:180]}；提示：容器文件描述符已耗尽（Errno 24）。插件已尝试"
            "自动抬升上限并按上限收敛并发数，仍失败请把「下载线程数」调低到 32 "
            "以内（必要时给容器加 ulimits.nofile: 65535）后重试"
        )
    return message


class _SpeedSampler:
    """按固定时间窗口计算平滑速度，避免并发分块回调产生瞬时尖峰。"""

    def __init__(self, window_seconds: float = 1.0):
        self._window_seconds = max(0.5, float(window_seconds or 1.0))
        self._sample_bytes = 0
        self._sample_time = time.monotonic()
        self._speed = 0.0

    def update(self, done: int) -> float:
        current = max(0, int(done or 0))
        now = time.monotonic()
        if current < self._sample_bytes:
            self._sample_bytes = current
            self._sample_time = now
            self._speed = 0.0
            return self._speed
        elapsed = now - self._sample_time
        if elapsed < self._window_seconds:
            return self._speed
        instant = max(0.0, (current - self._sample_bytes) / elapsed)
        self._speed = (
            instant if self._speed <= 0
            else self._speed * 0.65 + instant * 0.35
        )
        self._sample_bytes = current
        self._sample_time = now
        return self._speed


def file_checksum(path: str, algorithm: str) -> str:
    digest = hashlib.new(algorithm.lower())
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _LinkRefresher:
    """按需刷新源盘直链。

    长任务（几十 GB）里直链可能被限速或过期，原先整段下载复用同一个直链，
    一旦失效所有分片都会失败。这里在分片读取失败后重新取链，并对刷新做节流，
    避免并发分片同时打爆取链接口。
    """

    def __init__(
            self,
            resolver: Callable[[], tuple[str, dict]],
            refresh_interval: float = 30.0,
            max_age: float = 20 * 60.0,
    ):
        self._resolver = resolver
        self._refresh_interval = max(0.0, float(refresh_interval))
        self._max_age = max(self._refresh_interval, float(max_age))
        self._url = ""
        self._headers: dict = {}
        self._resolved_at = 0.0
        self._lock = Lock()

    def get(self, refresh: bool = False) -> tuple[str, dict]:
        with self._lock:
            now = time.time()
            age = now - self._resolved_at
            should_refresh = (
                    not self._url
                    or age >= self._max_age
                    or (refresh and age >= self._refresh_interval)
            )
            if should_refresh:
                url, headers = self._resolver()
                if url:
                    if self._url and url != self._url:
                        logger.info("已刷新源盘直链，继续断点续传")
                    self._url = str(url)
                    self._headers = dict(headers or {})
                    # 分段/Range 下载必须拿原始字节，禁止传输层压缩
                    self._headers.setdefault("Accept-Encoding", "identity")
                    self._resolved_at = now
                elif not self._url:
                    self._resolved_at = now
            return self._url, dict(self._headers)


class HttpFileDownloadService:
    """把 Provider 的临时下载地址解析器适配为流式文件下载能力。"""

    def __init__(
            self,
            resolver: Callable[[CloudFile], tuple[str, dict]],
            timeout: int = 300,
            concurrency: int = 5,
            part_size: int = 10 * 1024 * 1024,
            link_refresh_interval: float = 30.0,
            link_max_age: float = 20 * 60.0,
    ):
        self._resolver = resolver
        self._timeout = timeout
        self._link_refresh_interval = float(link_refresh_interval)
        self._link_max_age = float(link_max_age)
        # 云盘 CDN 普遍按「单连接」限速（夸克约 100~200KB/s），提速只能靠并发连接数叠加，
        # 因此这里只做 256 的兜底保护，真正生效的并发由调用方（下载线程数）决定。
        self._concurrency = max(1, min(int(concurrency or 5), 256))
        self._part_size = max(1024 * 1024, int(part_size or 0))

    @property
    def _request_timeout(self) -> tuple[int, int]:
        # CDN 大文件分段可能长时间无数据，不能把调用方配置的 300 秒硬截断为 30 秒。
        return 15, min(max(30, self._timeout), 120)

    def _create_session(self) -> requests.Session:
        """复用连接的下载 session。

        每个分片都新建连接会耗尽容器文件描述符（Errno 24 Too many open files），
        这里按并发上限建连接池并复用，socket 数被池大小夹住。
        """
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=2,
            pool_maxsize=max(2, self._concurrency),
            max_retries=0,
            pool_block=True,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    @staticmethod
    def _parts_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.parts.json")

    @staticmethod
    def _write_parts(path: Path, payload: dict) -> None:
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load_completed_ranges(
            self, target: Path, total: int, ranges: list[tuple[int, int]],
    ) -> set[int]:
        parts_path = self._parts_path(target)
        if not target.is_file() or not parts_path.is_file():
            return set()
        try:
            payload = json.loads(parts_path.read_text(encoding="utf-8"))
            if (
                    int(payload.get("version") or 0) != 1
                    or int(payload.get("total") or 0) != total
                    or int(payload.get("part_size") or 0) != self._part_size
                    or target.stat().st_size != total
            ):
                return set()
            raw_completed = payload.get("completed") or []
            if not isinstance(raw_completed, list):
                return set()
            valid_starts = {start for start, _ in ranges}
            completed = {int(value) for value in raw_completed}
            if (
                    len(completed) != len(raw_completed)
                    or not completed.issubset(valid_starts)
            ):
                return set()
            return completed
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return set()

    def _download_serial(
            self, refresher: _LinkRefresher, session: requests.Session,
            file_item: CloudFile, target: Path,
            progress_callback=None, stop_requested=None,
    ) -> str:
        done = 0
        url, headers = refresher.get()
        with session.get(
                url,
                headers=headers,
                stream=True,
                timeout=self._request_timeout,
        ) as response:
            response.raise_for_status()
            total = int(
                response.headers.get("Content-Length") or file_item.size or 0
            )
            with target.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if stop_requested and stop_requested():
                        raise InterruptedError
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress_callback:
                        progress_callback(done, total)
        if done <= 0:
            raise RuntimeError("源网盘下载为空文件")
        return str(target)

    def _download_parallel(
            self, refresher: _LinkRefresher, session: requests.Session,
            total: int, target: Path,
            progress_callback=None, stop_requested=None,
    ) -> str:
        ranges = [
            (start, min(total - 1, start + self._part_size - 1))
            for start in range(0, total, self._part_size)
        ]
        worker_count = min(self._concurrency, len(ranges))
        safe_workers = _fd_safe_workers(worker_count)
        if safe_workers < worker_count:
            logger.warning(
                f"按文件句柄上限收敛分段下载并发数：{worker_count} -> {safe_workers}"
            )
            worker_count = safe_workers
        if worker_count <= 1:
            raise _RangeDownloadUnsupported
        logger.info(
            f"分段下载启用：{worker_count} 并发连接 × "
            f"{self._part_size // (1024 * 1024)}MB 分片 → {target.name}"
        )

        progress_lock = Lock()
        abort_event = Event()
        parts_path = self._parts_path(target)
        completed = self._load_completed_ranges(target, total, ranges)
        if not completed:
            with target.open("wb") as handle:
                handle.truncate(total)
            self._write_parts(parts_path, {
                "version": 1,
                "total": total,
                "part_size": self._part_size,
                "completed": [],
            })
        downloaded = sum(
            end - start + 1 for start, end in ranges if start in completed
        )
        pending_ranges = [value for value in ranges if value[0] not in completed]
        if progress_callback and downloaded:
            progress_callback(downloaded, total)
        if not pending_ranges:
            parts_path.unlink(missing_ok=True)
            return str(target)

        def download_part(byte_range: tuple[int, int]) -> None:
            nonlocal downloaded
            start, end = byte_range
            for attempt in range(3):
                received = 0
                url, headers = refresher.get(refresh=attempt > 0)
                request_headers = dict(headers)
                request_headers["Range"] = f"bytes={start}-{end}"
                try:
                    with session.get(
                            url,
                            headers=request_headers,
                            stream=True,
                            timeout=self._request_timeout,
                    ) as response:
                        if response.status_code != 206:
                            if (
                                    response.status_code >= 500
                                    or response.status_code in (401, 403, 404, 410, 429)
                            ):
                                # 链接过期/被风控/源站抖动：交给重试逻辑换取新直链
                                raise requests.RequestException(
                                    f"HTTP {response.status_code}"
                                )
                            raise _RangeDownloadUnsupported(
                                f"HTTP {response.status_code}"
                            )
                        content_range = str(response.headers.get("Content-Range") or "")
                        if not content_range.lower().startswith(
                                f"bytes {start}-{end}/".lower()
                        ):
                            raise _RangeDownloadUnsupported(
                                f"无效 Content-Range: {content_range or '空'}"
                            )
                        with target.open("r+b") as handle:
                            handle.seek(start)
                            for chunk in response.iter_content(256 * 1024):
                                if (
                                        abort_event.is_set()
                                        or (stop_requested and stop_requested())
                                ):
                                    raise InterruptedError
                                if not chunk:
                                    continue
                                handle.write(chunk)
                                received += len(chunk)
                                with progress_lock:
                                    downloaded += len(chunk)
                                    if progress_callback:
                                        progress_callback(downloaded, total)
                        if received != end - start + 1:
                            raise IOError(
                                f"分段下载不完整：{start}-{end}，实际 {received} 字节"
                            )
                    break
                except (_RangeDownloadUnsupported, InterruptedError):
                    raise
                except (requests.RequestException, OSError):
                    with progress_lock:
                        downloaded = max(0, downloaded - received)
                        if progress_callback:
                            progress_callback(downloaded, total)
                    if attempt >= 2:
                        raise
                    time.sleep(attempt + 1)
            with progress_lock:
                completed.add(start)
                self._write_parts(parts_path, {
                    "version": 1,
                    "total": total,
                    "part_size": self._part_size,
                    "completed": sorted(completed),
                })

        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="cloudsubscribe-range-download",
        )
        futures = [executor.submit(download_part, value) for value in pending_ranges]
        try:
            for future in as_completed(futures):
                future.result()
        except Exception:
            abort_event.set()
            for future in futures:
                future.cancel()
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        parts_path.unlink(missing_ok=True)
        return str(target)

    def download_file(self, file_item: CloudFile, local_path: str,
                      progress_callback=None, stop_requested=None,
                      preserve_partial: bool = False) -> str:
        refresher = _LinkRefresher(
            lambda: self._resolver(file_item),
            refresh_interval=self._link_refresh_interval,
            max_age=self._link_max_age,
        )
        if not refresher.get()[0]:
            raise RuntimeError("源网盘未返回下载地址")
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        total = int(file_item.size or 0)
        reported = 0

        def report(done: int, current_total: int) -> None:
            nonlocal reported
            reported = max(reported, int(done or 0))
            if progress_callback:
                progress_callback(reported, current_total)

        session = self._create_session()
        try:
            if self._concurrency > 1 and total > self._part_size:
                try:
                    return self._download_parallel(
                        refresher,
                        session,
                        total,
                        target,
                        report,
                        stop_requested,
                    )
                except _RangeDownloadUnsupported as error:
                    logger.warning(
                        f"源盘不支持分段下载（{error}），回落单线程顺序下载："
                        f"{file_item.name}（实际并发 {self._concurrency}）"
                    )
                    # 403/失效链接也会走到这里，回退前先尝试换取新链接
                    refresher.get(refresh=True)
                    target.unlink(missing_ok=True)
                    self._parts_path(target).unlink(missing_ok=True)
            return self._download_serial(
                refresher,
                session,
                file_item,
                target,
                report,
                stop_requested,
            )
        except Exception:
            if not preserve_partial or not self._parts_path(target).is_file():
                target.unlink(missing_ok=True)
                self._parts_path(target).unlink(missing_ok=True)
            raise
        finally:
            session.close()


class LocalRapidUploadAdapter:
    """把现有 Provider 的上传服务暴露为统一能力。

    Provider 必须单独实现 try_rapid_upload；秒传未命中后由编排器调用
    upload_file，避免通过进度回调猜测是否发生了普通上传。
    """

    def __init__(self, upload: object, files: object, algorithms: frozenset[str]):
        self._upload = upload
        self._files = files
        self._algorithms = algorithms

    @property
    def algorithms(self) -> frozenset[str]:
        return self._algorithms

    @property
    def requires_local_file(self) -> bool:
        return bool(getattr(self._upload, "rapid_requires_local_file", False))

    def upload_by_hash(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int = 0,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> RapidUploadResult:
        algorithm = str(algorithm or "").lower()
        if algorithm not in self._algorithms:
            return RapidUploadResult(False, message="目标网盘不支持该秒传算法")
        actual = str(checksum or "").lower()
        if local_path:
            calculated = file_checksum(local_path, algorithm)
            if actual and calculated.lower() != actual:
                raise ValueError(f"{algorithm} 校验和不匹配")
            actual = calculated.lower()
        elif self.requires_local_file:
            return RapidUploadResult(False, message="秒传探测需要本地文件")
        if not actual:
            return RapidUploadResult(False, message="缺少秒传校验和")
        rapid_upload = getattr(self._upload, "try_rapid_upload", None)
        if not callable(rapid_upload):
            return RapidUploadResult(False, message="目标网盘未实现独立秒传探测")
        reused = bool(rapid_upload(
            local_path, save_path, target_name, algorithm, actual, size
        ))
        if not reused:
            return RapidUploadResult(False, message="未命中秒传")
        item = self._files.find_file(save_path, target_name)
        if not item:
            raise RuntimeError("秒传成功后未找到目标文件")
        return RapidUploadResult(True, file=item, message="秒传命中")

    def probe_by_hash(
            self, save_path: str, target_name: str, algorithm: str,
            checksum: str, size: int,
            read_range_bytes_or_hash: Optional[Callable[[str], str]] = None,
    ) -> Optional[RapidUploadResult]:
        """无本地文件的秒传探测（abort 模式专用）。

        返回 RapidUploadResult=已探测（reused 表示命中）/ None=无法探测
        （提供方未实现或缺少探测条件），调用方在 None 时退回下载后再探测。
        """
        algorithm = str(algorithm or "").lower()
        if algorithm not in self._algorithms:
            return None
        probe = getattr(self._upload, "try_rapid_upload_by_hash", None)
        if not callable(probe):
            return None
        actual = str(checksum or "").lower()
        if not actual:
            return None
        outcome = probe(
            save_path, target_name, algorithm, actual.upper(),
            int(size or 0), read_range_bytes_or_hash,
        )
        if outcome is None:
            return None
        if not outcome:
            return RapidUploadResult(False, message="未命中秒传")
        item = self._files.find_file(save_path, target_name)
        if not item:
            raise RuntimeError("秒传成功后未找到目标文件")
        return RapidUploadResult(True, file=item, message="秒传命中")


@dataclass(frozen=True)
class CrossDriveTransferResult:
    file: CloudFile
    method: str


class CrossDriveTransfer:
    """执行单文件传输；源文件需先由调用层下载到本地临时路径。"""

    @staticmethod
    def select_algorithm(source: CloudFile, target: CloudDriveProvider) -> tuple[str, str]:
        """按目标能力选择源文件已有校验和，MD5 与 SHA1 绝不互换。"""
        if not target.supports(CloudDriveCapability.RAPID_UPLOAD):
            return "", ""
        algorithms = target.require(CloudDriveCapability.RAPID_UPLOAD).algorithms
        if "md5" in algorithms and source.md5:
            return "md5", source.md5
        if "sha1" in algorithms and source.sha1:
            return "sha1", source.sha1
        return "", ""

    def transfer(
            self, source_path: str, target: CloudDriveProvider, save_path: str,
            target_name: str = "", algorithm: str = "", checksum: str = "",
            fallback: bool = True,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> CrossDriveTransferResult | None:
        target_name = target_name or Path(source_path).name
        if target.supports(CloudDriveCapability.RAPID_UPLOAD) and algorithm:
            rapid = target.require(CloudDriveCapability.RAPID_UPLOAD)
            if algorithm.lower() in rapid.algorithms:
                rapid_result = rapid.upload_by_hash(
                    source_path, save_path, target_name, algorithm, checksum,
                    Path(source_path).stat().st_size, progress_callback,
                )
                if rapid_result.reused and rapid_result.file:
                    return CrossDriveTransferResult(rapid_result.file, "rapid")
        if not fallback:
            return None
        upload = target.require(CloudDriveCapability.LOCAL_UPLOAD)
        if not upload.upload_file(source_path, save_path, target_name, progress_callback=progress_callback):
            return None
        item = target.require(CloudDriveCapability.FILE_QUERY).find_file(
            save_path, target_name
        )
        return CrossDriveTransferResult(item, "fallback") if item else None


class CrossTransferTaskManager:
    """本地中继任务管理器。"""

    ACTIVE = frozenset({"pending", "running", "stopping"})
    _MIN_FREE_BYTES = 512 * 1024 * 1024

    def __init__(self, provider_resolver: Callable[[str], CloudDriveProvider],
                 download_path: str = "", download_threads: int = 5,
                 max_concurrent: int = 2,
                 direct_stream: bool = True,
                 rapid_miss_action: str = "fallback",
                 relay_client: Optional[RelayClient] = None,
                 relay_max_bytes: int = 0,
                 relay_timeout_seconds: int = 0,
                 relay_fallback_local: bool = True,
                 on_change: Optional[Callable[[], None]] = None):
        self._provider_resolver = provider_resolver
        self._download_path = str(download_path or "").strip()
        self._download_threads = max(1, min(int(download_threads or 5), 256))
        self._direct_stream = bool(direct_stream)
        # 秒传未命中处理：fallback=回落普通上传（默认，行为不变）；abort=中止不上传。
        # 非法值一律回退 fallback。
        self._rapid_miss_action = (
            "abort" if str(rapid_miss_action or "").strip().lower() == "abort" else "fallback"
        )
        # 深圳中继：只在「115 秒传确认未命中」时接管这一个文件的下载+上传；
        # 传完后由插件继续整理。地址/令牌缺失即视为关闭。
        self._relay_client = relay_client if getattr(relay_client, "enabled", False) else None
        self._relay_max_bytes = max(0, int(relay_max_bytes or 0))
        self._relay_timeout_seconds = max(60, int(relay_timeout_seconds or 0))
        self._relay_fallback_local = bool(relay_fallback_local)
        self._transfer_slots = Semaphore(max(1, min(int(max_concurrent or 2), 10)))
        self._tasks: dict[str, dict] = {}
        self._lock = Lock()
        self._on_change = on_change
        # 同一 cache_key 的任务严格串行 + 中继文件引用计数（1.6.47 同源并发互删）
        self._cache_key_locks: dict[str, Lock] = {}
        self._cache_key_locks_guard = Lock()
        self._cache_refs: dict[str, int] = {}
        self._cache_refs_lock = Lock()

    def _notify_change(self) -> None:
        if self._on_change:
            self._on_change()

    def _cache_root(self) -> Path:
        root = Path(self._download_path) if self._download_path else Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _cache_key(source_key: str, source: CloudFile) -> str:
        fingerprint = "|".join((
            str(source_key or ""),
            str(source.sha1 or "").lower(),
            str(source.md5 or "").lower(),
            str(source.size or 0),
            str(source.name or ""),
        ))
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:32]

    def _cache_path(self, cache_key: str) -> Path:
        return self._cache_root() / f"cloud-transfer-{cache_key}.bin"

    @staticmethod
    def _task_cache_key(task: dict | None) -> str:
        """任务级中继 cache_key：与 _temporary_path/_cache_key 同源。

        本地源文件任务（source_file 为空）不使用中继缓存，返回空串（不加锁、不计数）。
        """
        data = task or {}
        source_file = data.get("source_file")
        if source_file is None:
            return ""
        return CrossTransferTaskManager._cache_key(
            str(data.get("source_provider") or ""), source_file
        )

    @staticmethod
    def _cache_meta_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.meta.json")

    @staticmethod
    def _cache_parts_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.parts.json")

    def _delete_cache_path(self, path: Path) -> int:
        deleted = 0
        for candidate in (
                path,
                self._cache_meta_path(path),
                self._cache_meta_path(path).with_name(
                    f"{self._cache_meta_path(path).name}.tmp"
                ),
                self._cache_parts_path(path),
                self._cache_parts_path(path).with_name(
                    f"{self._cache_parts_path(path).name}.tmp"
                ),
        ):
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
                deleted += 1
        return deleted

    def _cache_key_lock(self, cache_key: str) -> Lock:
        """同一 cache_key 的任务串行执行：第二个任务排在第一个之后。"""
        with self._cache_key_locks_guard:
            lock = self._cache_key_locks.get(cache_key)
            if lock is None:
                lock = Lock()
                self._cache_key_locks[cache_key] = lock
            return lock

    def _acquire_cache_ref(self, cache_key: str) -> None:
        if not cache_key:
            return
        with self._cache_refs_lock:
            self._cache_refs[cache_key] = self._cache_refs.get(cache_key, 0) + 1

    def _release_cache_ref(self, cache_key: str) -> None:
        if not cache_key:
            return
        with self._cache_refs_lock:
            remaining = self._cache_refs.get(cache_key, 0) - 1
            if remaining > 0:
                self._cache_refs[cache_key] = remaining
            else:
                self._cache_refs.pop(cache_key, None)

    def _cache_ref_count(self, cache_key: str) -> int:
        if not cache_key:
            return 0
        with self._cache_refs_lock:
            return self._cache_refs.get(cache_key, 0)

    def _cache_retained_for_others(self, task: dict | None) -> bool:
        """任务自身持有 1 个引用；计数 > 1 说明还有兄弟任务在用同一中继文件。"""
        cache_key = self._task_cache_key(task)
        return bool(cache_key) and self._cache_ref_count(cache_key) > 1

    def _delete_cache_path_for_task(self, task: dict | None, path: Path) -> int:
        """任务级中继清理：仍有兄弟任务使用时保留文件（连同 meta/parts），供其复用。"""
        if self._cache_retained_for_others(task):
            logger.debug(
                f"中继缓存仍被其它任务使用，保留不删：{path.name}"
                f"（在用引用 {self._cache_ref_count(self._task_cache_key(task))}）"
            )
            return 0
        return self._delete_cache_path(path)

    def delete_cache(self, cache_key: str) -> int:
        normalized = str(cache_key or "").strip().lower()
        if len(normalized) != 32 or any(value not in "0123456789abcdef" for value in normalized):
            return 0
        in_use = self._cache_ref_count(normalized)
        if in_use > 0:
            logger.debug(
                f"中继缓存正在使用中，跳过清理：{normalized}（在用引用 {in_use}）"
            )
            return 0
        return self._delete_cache_path(self._cache_path(normalized))

    def _validate_complete_cache(
            self, path: Path, cache_key: str, source: CloudFile,
            verify_checksum: bool = True,
    ) -> bool:
        meta_path = self._cache_meta_path(path)
        if not path.is_file() or not meta_path.is_file():
            return False
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            expected_size = int(source.size or 0)
            if (
                    metadata.get("cache_key") != cache_key
                    or not metadata.get("complete")
                    or int(metadata.get("size") or 0) != expected_size
                    or path.stat().st_size != expected_size
            ):
                return False
            expected_sha1 = str(source.sha1 or "").lower()
            expected_md5 = str(source.md5 or "").lower()
            if (
                    str(metadata.get("sha1") or "").lower() != expected_sha1
                    or str(metadata.get("md5") or "").lower() != expected_md5
            ):
                return False
            if not verify_checksum:
                return expected_size > 0
            if expected_sha1:
                return file_checksum(str(path), "sha1").lower() == expected_sha1
            if expected_md5:
                return file_checksum(str(path), "md5").lower() == expected_md5
            return expected_size > 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _partial_cache_size(self, path: Path, source: CloudFile) -> int:
        parts_path = self._cache_parts_path(path)
        expected_size = int(source.size or 0)
        if expected_size <= 0 or not path.is_file() or not parts_path.is_file():
            return 0
        try:
            payload = json.loads(parts_path.read_text(encoding="utf-8"))
            part_size = int(payload.get("part_size") or 0)
            if (
                    int(payload.get("version") or 0) != 1
                    or int(payload.get("total") or 0) != expected_size
                    or part_size <= 0
                    or path.stat().st_size != expected_size
            ):
                return 0
            valid_starts = set(range(0, expected_size, part_size))
            completed = {
                int(value) for value in (payload.get("completed") or [])
                if int(value) in valid_starts
            }
            if len(completed) != len(payload.get("completed") or []):
                return 0
            return sum(
                min(part_size, expected_size - start) for start in completed
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _mark_complete_cache(
            self, path: Path, cache_key: str, source: CloudFile,
    ) -> None:
        expected_size = int(source.size or 0)
        if expected_size <= 0 or path.stat().st_size != expected_size:
            raise IOError(
                f"本地缓存文件大小不完整：{path.stat().st_size}/{expected_size}"
            )
        sha1 = str(source.sha1 or "").lower()
        md5 = str(source.md5 or "").lower()
        if sha1 and file_checksum(str(path), "sha1").lower() != sha1:
            raise IOError("本地缓存 SHA1 校验失败")
        if md5 and file_checksum(str(path), "md5").lower() != md5:
            raise IOError("本地缓存 MD5 校验失败")
        meta_path = self._cache_meta_path(path)
        temporary = meta_path.with_name(f"{meta_path.name}.tmp")
        temporary.write_text(json.dumps({
            "version": 1,
            "cache_key": cache_key,
            "complete": True,
            "size": expected_size,
            "sha1": sha1,
            "md5": md5,
        }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        temporary.replace(meta_path)

    def cache_info(
            self, source_key: str, source: CloudFile,
            verify_checksum: bool = True,
    ) -> dict:
        cache_key = self._cache_key(source_key, source)
        path = self._cache_path(cache_key)
        complete = self._validate_complete_cache(
            path, cache_key, source, verify_checksum=verify_checksum
        )
        partial_size = 0 if complete else self._partial_cache_size(path, source)
        return {
            "cache_key": cache_key,
            "cache_status": (
                "complete" if complete else "partial" if partial_size else "missing"
            ),
            "cache_size": int(source.size or 0) if complete else partial_size,
        }

    def _temporary_path(self, task: dict) -> str:
        source = task.get("source_file")
        cache_key = self._cache_key(task.get("source_provider"), source)
        return str(self._cache_path(cache_key))

    def create(self, source_path: str = "", target_key: str = "", save_path: str = "/",
               target_name: str = "", algorithm: str = "", checksum: str = "",
               fallback: bool = True, source_key: str = "", source_file: CloudFile | None = None,
               parent_task_id: str = "") -> dict:
        source = Path(source_path) if source_path else None
        if source is not None and not source.is_file():
            raise ValueError("源文件不存在或不是文件")
        if source is None and (not source_key or not source_file):
            raise ValueError("缺少源文件路径或源网盘文件")
        task_id = uuid.uuid4().hex
        now = time.time()
        total = int(source.stat().st_size) if source else int(source_file.size or 0)
        cache_root = self._cache_root()
        required = max(self._MIN_FREE_BYTES, int(total * 1.10))
        try:
            free_bytes = int(shutil.disk_usage(cache_root).free)
        except OSError as error:
            raise RuntimeError(f"无法检查中继磁盘空间：{error}") from error
        if free_bytes < required:
            raise RuntimeError(
                f"中继磁盘可用空间不足：剩余 {free_bytes} 字节，"
                f"至少需要 {required} 字节"
            )
        task = {
            "id": f"cross:{task_id}", "task_kind": "cross_transfer",
            "parent_task_id": str(parent_task_id or ""),
            "title": target_name or (source.name if source else source_file.name),
            "source_path": str(source) if source else "",
            "source_provider": source_key,
            "source_file": source_file,
            "target_provider": target_key, "target_path": save_path,
            "status": "pending", "phase": "pending", "progress": 0,
            "transferred": 0, "total": total,
            "stage_transferred": 0, "stage_total": 0,
            "downloaded_bytes": 0, "uploaded_bytes": 0,
            "download_speed_bytes_per_second": 0.0,
            "upload_speed_bytes_per_second": 0.0,
            "speed_bytes_per_second": 0.0, "message": "等待跨盘传输",
            "error": "", "method": "rapid" if algorithm else "fallback",
            "algorithm": algorithm.lower(), "fallback": bool(fallback),
            "queued_at": now, "started_at": None, "finished_at": None,
            "updated_at": now, "stop_event": Event(),
        }
        with self._lock:
            terminal = [key for key, value in self._tasks.items()
                        if value["status"] not in self.ACTIVE]
            for stale_id in terminal[:-49]:
                self._tasks.pop(stale_id, None)
            self._tasks[task["id"]] = task
        self._acquire_cache_ref(self._task_cache_key(task))
        self._notify_change()
        Thread(target=self._run, args=(task["id"], checksum), daemon=True).start()
        return self._public(task)

    def create_from_cloud_file(self, source_key: str, source_file: CloudFile,
                               target_key: str, save_path: str, target_name: str = "",
                               algorithm: str = "", fallback: bool = True,
                               parent_task_id: str = "") -> dict:
        selected = algorithm or ""
        if not selected:
            try:
                selected, _ = CrossDriveTransfer.select_algorithm(
                    source_file, self._provider_resolver(target_key)
                )
            except Exception:
                selected = ""
        checksum = source_file.md5 if selected == "md5" else source_file.sha1
        return self.create(
            target_key=target_key, save_path=save_path, target_name=target_name,
            algorithm=selected, checksum=checksum, fallback=fallback,
            source_key=source_key, source_file=source_file,
            parent_task_id=parent_task_id,
        )

    def list(self, active_only: bool = False) -> list[dict]:
        with self._lock:
            tasks = [self._public(task) for task in self._tasks.values()
                     if not active_only or task["status"] in self.ACTIVE]
        return sorted(tasks, key=lambda item: item["queued_at"], reverse=True)

    def wait(self, task_id: str, poll_seconds: float = 0.2,
             cancel_check: Optional[Callable[[], bool]] = None) -> bool:
        while True:
            with self._lock:
                task = self._tasks.get(task_id)
                if not task:
                    return False
                status = task["status"]
            if status not in self.ACTIVE:
                return status == "success"
            if cancel_check and cancel_check():
                self.cancel(task_id)
            time.sleep(max(0.05, poll_seconds))

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task["status"] not in self.ACTIVE:
                return False
            task["status"], task["phase"] = "stopping", "stopping"
            task["message"] = "等待当前网络操作安全停止"
            task["stop_event"].set()
        self._notify_change()
        return True

    def cancel_parent(self, parent_task_id: str) -> int:
        """取消属于同一订阅任务的全部跨盘子任务。"""
        parent_task_id = str(parent_task_id or "")
        if not parent_task_id:
            return 0
        canceled = 0
        with self._lock:
            for task in self._tasks.values():
                if (
                        task.get("parent_task_id") == parent_task_id
                        and task.get("status") in self.ACTIVE
                ):
                    task["status"], task["phase"] = "stopping", "stopping"
                    task["message"] = "等待当前网络操作安全停止"
                    task["stop_event"].set()
                    canceled += 1
        if canceled:
            self._notify_change()
        return canceled

    def _update(self, task_id: str, **values) -> None:
        changed = False
        with self._lock:
            task = self._tasks.get(task_id)
            if task and any(task.get(key) != value for key, value in values.items()):
                task.update(values)
                task["updated_at"] = time.time()
                changed = True
        if changed:
            self._notify_change()

    @staticmethod
    def _public(task: dict) -> dict:
        return {key: value for key, value in task.items()
                if key not in {"stop_event", "source_path", "source_file"}}

    @staticmethod
    def _ranges_cover(
            completed: list[int], part_size: int, total: int,
            start: int, end: int,
    ) -> bool:
        cursor = start
        for part_start in sorted(completed):
            part_end = min(total, part_start + part_size) - 1
            if part_end < cursor:
                continue
            if part_start > cursor:
                return False
            cursor = part_end + 1
            if cursor > end:
                return True
        return cursor > end

    def _progressive_transfer(
            self, task_id: str, task: dict, downloader: object,
            target: CloudDriveProvider, temporary_path: str,
            cache_key: str, target_name_text: str,
            algorithm: str, checksum: str,
    ) -> CrossDriveTransferResult | None:
        """下载 Range 落盘后立即交给支持该能力的目标盘上传。"""
        stop_event = task["stop_event"]
        source_file = task["source_file"]
        expected_size = int(source_file.size or task["total"] or 0)
        cache_path = Path(temporary_path)
        parts_path = self._cache_parts_path(cache_path)
        download_done = Event()
        progress_changed = Event()
        download_error: list[BaseException] = []
        download_speed = _SpeedSampler()
        upload_speed = _SpeedSampler()

        def update_progress(**values) -> None:
            with self._lock:
                current = self._tasks.get(task_id) or {}
                downloaded = int(values.get(
                    "downloaded_bytes", current.get("downloaded_bytes") or 0
                ))
                uploaded = int(values.get(
                    "uploaded_bytes", current.get("uploaded_bytes") or 0
                ))
                download_rate = float(values.get(
                    "download_speed_bytes_per_second",
                    current.get("download_speed_bytes_per_second") or 0,
                ))
                upload_rate = float(values.get(
                    "upload_speed_bytes_per_second",
                    current.get("upload_speed_bytes_per_second") or 0,
                ))
            progress = (
                min(99, int((downloaded + uploaded) * 50 / expected_size))
                if expected_size > 0 else 0
            )
            values.update(
                phase="streaming", progress=progress,
                transferred=int(expected_size * progress / 100),
                stage_transferred=uploaded, stage_total=expected_size,
                speed_bytes_per_second=(
                    upload_rate if uploaded > 0 else download_rate
                ),
                message=f"正在边下载边上传到{target_name_text}",
            )
            self._update(task_id, **values)

        def download_progress(done: int, total: int) -> None:
            if stop_event.is_set():
                raise InterruptedError
            speed = download_speed.update(done)
            update_progress(
                downloaded_bytes=done,
                download_speed_bytes_per_second=speed,
            )
            progress_changed.set()

        def run_download() -> None:
            try:
                path = downloader.download_file(
                    source_file, temporary_path, download_progress,
                    stop_event.is_set, preserve_partial=True,
                    download_threads=self._download_threads,
                )
                self._mark_complete_cache(Path(path), cache_key, source_file)
            except BaseException as error:
                download_error.append(error)
            finally:
                download_done.set()
                progress_changed.set()

        def wait_for_range(start: int, end: int) -> None:
            while True:
                if stop_event.is_set():
                    raise InterruptedError
                if download_error:
                    raise download_error[0]
                if parts_path.is_file():
                    try:
                        payload = json.loads(parts_path.read_text(encoding="utf-8"))
                        if self._ranges_cover(
                                [int(value) for value in payload.get("completed") or []],
                                int(payload.get("part_size") or 0),
                                int(payload.get("total") or expected_size),
                                start, end,
                        ):
                            return
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
                if download_done.is_set():
                    if download_error:
                        raise download_error[0]
                    if cache_path.is_file() and cache_path.stat().st_size == expected_size:
                        return
                    raise IOError("本地缓存下载完成但文件大小不完整")
                progress_changed.wait(0.1)
                progress_changed.clear()

        def upload_progress(done: int, total: int) -> None:
            if stop_event.is_set():
                raise InterruptedError
            speed = upload_speed.update(done)
            update_progress(
                uploaded_bytes=done,
                upload_speed_bytes_per_second=speed,
            )

        self._update(
            task_id, phase="streaming",
            message=f"准备边下载边上传到{target_name_text}",
        )
        download_thread = Thread(
            target=run_download,
            name="cloudsubscribe-progressive-download",
            daemon=True,
        )
        download_thread.start()
        uploaded = False
        upload_error: Exception | None = None
        try:
            uploaded = bool(target.require(
                CloudDriveCapability.LOCAL_UPLOAD
            ).upload_progressive(
                temporary_path, task["target_path"], task["title"],
                expected_size, algorithm, checksum, wait_for_range,
                upload_progress, stop_event.is_set,
            ))
        except InterruptedError:
            raise
        except Exception as error:
            upload_error = error
        finally:
            download_thread.join()
        if stop_event.is_set():
            raise InterruptedError
        if download_error:
            raise download_error[0]
        if upload_error:
            # 下载缓存已完整保留，后续由普通本地上传继续，不丢失本轮成果。
            self._update(task_id, message="流水线上传未完成，正在使用完整缓存重试")
            return None
        if not uploaded:
            return None
        item = target.require(CloudDriveCapability.FILE_QUERY).find_file(
            task["target_path"], task["title"]
        )
        return CrossDriveTransferResult(item, "progressive") if item else None

    def _relay_transfer(
            self, task: dict, task_id: str, target: CloudDriveProvider,
            target_path: str, target_name: str, target_name_text: str,
            checksum: str, total: int, stop_event: Event,
    ) -> bool:
        """把「秒传确认未命中」的单个文件交给深圳中继；成功返回 True。

        True  → 已由中继上传并在网盘里找到文件，调用方直接返回；
        False → 中继未完成且允许回落，调用方继续本机原链路。
        """
        client = self._relay_client
        if client is None:
            return False
        source = task.get("source_file")
        source_name = str(getattr(source, "name", "") or task.get("title") or "")
        payload = {
            "provider": str(task.get("source_provider") or "quark"),
            "file_id": str(getattr(source, "id", "") or ""),
            "file_name": source_name,
            "size": int(total or 0),
            "sha1": str(checksum or getattr(source, "sha1", "") or "").upper(),
            "target": {"path": str(target_path), "name": str(target_name)},
        }
        try:
            job = client.submit(payload)
        except RelayError as error:
            logger.warning(f"深圳中继提交失败，按配置决定是否回落：{error}")
            if self._relay_fallback_local:
                self._update(
                    task_id, phase="fallback",
                    message=f"深圳中继不可用，回落本机：{error}",
                )
                return False
            raise RuntimeError(f"深圳中继不可用：{error}") from error
        job_id = str(job.get("id") or "")
        if not job_id:
            if self._relay_fallback_local:
                return False
            raise RuntimeError("深圳中继未返回任务 ID")
        logger.info(
            f"115 秒传未命中，已交给深圳中继：{target_name} -> "
            f"{target_path}（job={job_id}）"
        )
        self._update(
            task_id, phase="relay", progress=6,
            message=f"115 秒传未命中，深圳中继处理中（{job_id}）",
        )
        deadline = time.time() + max(60, self._relay_timeout_seconds)
        last_message = ""
        while time.time() < deadline:
            if stop_event.is_set():
                client.cancel(job_id)
                raise InterruptedError
            try:
                current = client.get(job_id)
            except RelayError as error:
                logger.warning(f"深圳中继状态查询失败，稍后重试：{error}")
                time.sleep(5)
                continue
            status = str(current.get("status") or "")
            phase = str(current.get("phase") or "")
            message = str(current.get("message") or "")
            if message and message != last_message:
                last_message = message
                logger.info(f"深圳中继 {job_id}：{phase} {message}")
            percent = int(current.get("progress") or 0)
            transferred = int(current.get("transferred") or 0)
            speed = float(current.get("speed") or 0)
            if status == "running":
                self._update(
                    task_id, phase="relay", progress=max(6, min(98, percent)),
                    transferred=min(int(total or 0), transferred),
                    speed_bytes_per_second=speed,
                    message=f"深圳中继：{message or phase}",
                )
            if status == "success":
                result = current.get("result") or {}
                file_name = str(result.get("file_name") or target_name)
                result_file = None
                for _ in range(20):
                    try:
                        result_file = target.require(
                            CloudDriveCapability.FILE_QUERY
                        ).find_file(target_path, file_name)
                    except Exception as error:  # noqa: BLE001 - 查一次失败不代表没传成
                        logger.debug(f"中继完成后查询目标文件失败：{error}")
                    if result_file:
                        break
                    time.sleep(3)
                if not result_file:
                    raise RuntimeError("深圳中继已上传，但目标网盘未找到文件")
                self._update(
                    task_id, status="success", phase="done", progress=100,
                    transferred=int(total or result.get("size") or 0),
                    speed_bytes_per_second=0.0,
                    stage_transferred=0, stage_total=0,
                    message=f"已由深圳中继上传到{target_name_text}",
                    method="relay",
                    result_file_id=str(result_file.id or ""),
                    result_file_name=str(result_file.name or file_name),
                    result_file_size=int(result_file.size or result.get("size") or 0),
                    result_sha1=str(result_file.sha1 or result.get("sha1") or ""),
                    result_md5=str(result_file.md5 or ""),
                    finished_at=time.time(),
                )
                logger.info(
                    f"深圳中继完成：{target_name} -> {target_path}/{file_name}"
                )
                return True
            if status in {"failed", "cancelled"}:
                error = str(current.get("error") or status)
                if self._relay_fallback_local:
                    logger.warning(f"深圳中继失败，回落本机链路：{error}")
                    self._update(
                        task_id, phase="fallback",
                        message=f"深圳中继失败，回落本机：{error}",
                        error="", progress=6,
                    )
                    return False
                raise RuntimeError(f"深圳中继失败：{error}")
            time.sleep(3)
        client.cancel(job_id)
        if self._relay_fallback_local:
            logger.warning("深圳中继超时，回落本机链路")
            self._update(
                task_id, phase="fallback", message="深圳中继超时，回落本机",
                error="", progress=6,
            )
            return False
        raise RuntimeError("深圳中继超时")

    def _run(self, task_id: str, checksum: str) -> None:
        # 同一 cache_key 的任务严格串行：第二个任务排在第一个之后，
        # 兄弟任务先完成时由引用计数保留完整缓存供其复用（1.6.47 同源并发互删）。
        with self._lock:
            task = self._tasks.get(task_id)
        cache_key = self._task_cache_key(task)
        if not cache_key:
            self._run_locked(task_id, checksum, cache_key)
            return
        with self._cache_key_lock(cache_key):
            self._run_locked(task_id, checksum, cache_key)

    def _run_locked(self, task_id: str, checksum: str, cache_key: str) -> None:
        self._transfer_slots.acquire()
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                self._release_cache_ref(cache_key)
                self._transfer_slots.release()
                return
            source_path = task["source_path"]
            stop_event = task["stop_event"]
            target_key, target_path = task["target_provider"], task["target_path"]
            target_name, algorithm = task["title"], task["algorithm"]
            fallback = task["fallback"]
        started = time.time()
        self._update(
            task_id, status="running",
            phase="hashing" if source_path else "downloading",
            started_at=started,
            message="正在校验源文件" if source_path else "准备下载源文件",
            progress=2 if source_path else 0,
        )
        try:
            if stop_event.is_set():
                raise InterruptedError
            target = self._provider_resolver(target_key)
            target_name_text = str(
                getattr(target, "name", "") or target_key or "目标网盘"
            )
            rapid_miss_abort = self._rapid_miss_action == "abort"
            relay_active = bool(
                self._relay_client
                and not source_path
                and str(task.get("source_provider") or "") == "quark"
                and int(task["total"] or 0) > 0
                and self._relay_max_bytes
                and int(task["total"] or 0) <= self._relay_max_bytes
                and target.supports(CloudDriveCapability.FILE_QUERY)
            )
            remote_rapid_attempted = False
            if (
                    not source_path and algorithm and checksum
                    and target.supports(CloudDriveCapability.RAPID_UPLOAD)
            ):
                rapid_service = target.require(CloudDriveCapability.RAPID_UPLOAD)
                if not bool(getattr(rapid_service, "requires_local_file", True)):
                    remote_rapid_attempted = True
                    self._update(
                        task_id, phase="rapid_upload",
                        message=f"正在向{target_name_text}尝试秒传",
                        progress=5,
                    )
                    rapid_result = rapid_service.upload_by_hash(
                        "", target_path, target_name, algorithm, checksum,
                        int(task["total"] or 0),
                    )
                    if rapid_result.reused and rapid_result.file:
                        result_file = rapid_result.file
                        self._update(
                            task_id, status="success", phase="done", progress=100,
                            transferred=task["total"], speed_bytes_per_second=0.0,
                            stage_transferred=0, stage_total=0,
                            message=f"已秒传到{target_name_text}", method="rapid",
                            result_file_id=str(result_file.id or ""),
                            result_file_name=str(result_file.name or target_name),
                            result_file_size=int(result_file.size or task["total"] or 0),
                            result_sha1=str(result_file.sha1 or ""),
                            result_md5=str(result_file.md5 or ""),
                            finished_at=time.time(),
                        )
                        return
                    if rapid_miss_abort:
                        # abort 模式：下载前探测未命中即中止（不下载、不上传）
                        raise RapidMissAborted()
            source_provider = None
            source_name = ""
            if not source_path:
                source_provider = self._provider_resolver(task["source_provider"])
                source_name = str(
                    getattr(source_provider, "name", "")
                    or task["source_provider"] or "源网盘"
                )
            if (
                    (rapid_miss_abort or relay_active) and not source_path
                    and not remote_rapid_attempted
                    and algorithm and checksum
                    and target.supports(CloudDriveCapability.RAPID_UPLOAD)
            ):
                # abort 模式（先探测后下载）：115 这类探测器需要本地文件的提供方
                # 走无本地文件的秒传探测；源文件有校验和时零流量，≥1MB 时用源盘
                # 直链读取 115 二次校验小段（不落本地）；无法探测才继续下载。
                probe = getattr(
                    target.require(CloudDriveCapability.RAPID_UPLOAD),
                    "probe_by_hash", None,
                )
                if callable(probe):
                    expected_size = int(task["total"] or 0)
                    read_range = None
                    if (
                            expected_size >= 1024 * 1024
                            and source_provider.supports(
                                CloudDriveCapability.FILE_DOWNLOAD
                            )
                    ):
                        try:
                            download_url, download_headers = source_provider.require(
                                CloudDriveCapability.FILE_DOWNLOAD
                            ).resolve_download_link(task["source_file"])
                            read_range = _RemoteSha1RangeReader(
                                download_url, download_headers, expected_size,
                                stop_requested=stop_event.is_set,
                            ).read_range_sha1
                        except Exception as error:  # noqa: BLE001 - 取链失败退回下载后探测
                            logger.debug(f"秒传探测取链失败，转为下载后探测：{error}")
                            read_range = None
                    self._update(
                        task_id, phase="rapid_upload",
                        message=f"正在向{target_name_text}尝试秒传（先探测后下载）",
                        progress=5,
                    )
                    probe_result = probe(
                        target_path, target_name, algorithm, checksum,
                        expected_size, read_range,
                    )
                    if (
                            probe_result is not None and probe_result.reused
                            and probe_result.file
                    ):
                        result_file = probe_result.file
                        # 探测命中也可能已有完整缓存，先清理再标成功（避免观测到
                        # success 时缓存还在）；同 cache_key 兄弟任务在用时保留
                        self._delete_cache_path_for_task(
                            task, Path(self._temporary_path(task))
                        )
                        self._update(
                            task_id, status="success", phase="done", progress=100,
                            transferred=task["total"], speed_bytes_per_second=0.0,
                            stage_transferred=0, stage_total=0,
                            message=f"已秒传到{target_name_text}", method="rapid",
                            result_file_id=str(result_file.id or ""),
                            result_file_name=str(result_file.name or target_name),
                            result_file_size=int(result_file.size or task["total"] or 0),
                            result_sha1=str(result_file.sha1 or ""),
                            result_md5=str(result_file.md5 or ""),
                            finished_at=time.time(),
                        )
                        return
                    if probe_result is not None and not probe_result.reused:
                        if relay_active and self._relay_transfer(
                                task, task_id, target, target_path, target_name,
                                target_name_text, checksum, expected_size, stop_event,
                        ):
                            return
                        if rapid_miss_abort:
                            raise RapidMissAborted()
                    # probe_result is None：无法零流量探测 → 继续下载后再按 abort 处理
            if (
                    not source_path
                    and self._direct_stream
                    and not rapid_miss_abort
                    and fallback
                    and algorithm == "sha1"
                    and bool(checksum)
                    and int(task["total"] or 0) > 0
                    and source_provider.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and target.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and callable(getattr(
                target.require(CloudDriveCapability.LOCAL_UPLOAD),
                "upload_from_link", None,
            ))
            ):
                try:
                    download_url, download_headers = source_provider.require(
                        CloudDriveCapability.FILE_DOWNLOAD
                    ).resolve_download_link(task["source_file"])
                    remote_speed = _SpeedSampler()

                    def remote_progress(done: int, total: int) -> None:
                        if stop_event.is_set():
                            raise InterruptedError
                        percent = min(99, int(done * 100 / total)) if total else 0
                        speed = remote_speed.update(done)
                        self._update(
                            task_id, phase="streaming", progress=percent,
                            transferred=done, total=total,
                            downloaded_bytes=done, uploaded_bytes=done,
                            download_speed_bytes_per_second=speed,
                            upload_speed_bytes_per_second=speed,
                            speed_bytes_per_second=speed,
                            stage_transferred=done, stage_total=total,
                            message=f"正在从{source_name} Range 直传到{target_name_text}",
                        )

                    self._update(
                        task_id, phase="rapid_upload", progress=1,
                        message=(
                            f"正在从{source_name}向{target_name_text}尝试 SHA1 秒传"
                        ),
                    )
                    remote_uploaded = target.require(
                        CloudDriveCapability.LOCAL_UPLOAD
                    ).upload_from_link(
                        download_url, download_headers, target_path,
                        target_name, int(task["total"]), algorithm, checksum,
                        remote_progress, stop_event.is_set,
                        refresh_link=lambda: source_provider.require(
                            CloudDriveCapability.FILE_DOWNLOAD
                        ).resolve_download_link(task["source_file"]),
                    )
                    if remote_uploaded:
                        remote_rapid = remote_uploaded == "rapid"
                        result_file = target.require(
                            CloudDriveCapability.FILE_QUERY
                        ).find_file(target_path, target_name)
                        if not result_file:
                            raise RuntimeError("跨盘传输完成后未找到目标文件")
                        self._update(
                            task_id, status="success", phase="done", progress=100,
                            transferred=task["total"],
                            downloaded_bytes=task["total"],
                            uploaded_bytes=task["total"],
                            download_speed_bytes_per_second=0.0,
                            upload_speed_bytes_per_second=0.0,
                            speed_bytes_per_second=0.0,
                            stage_transferred=0, stage_total=0,
                            message=(
                                f"已从{source_name} SHA1 秒传到{target_name_text}"
                                if remote_rapid else
                                f"已从{source_name} Range 直传到{target_name_text}"
                            ),
                            method="rapid" if remote_rapid else "remote",
                            result_file_id=str(result_file.id or ""),
                            result_file_name=str(result_file.name or target_name),
                            result_file_size=int(
                                result_file.size or task["total"] or 0
                            ),
                            result_sha1=str(result_file.sha1 or ""),
                            result_md5=str(result_file.md5 or ""),
                            finished_at=time.time(),
                        )
                        return
                except InterruptedError:
                    raise
                except Exception as error:  # noqa: BLE001 - 直传失败回退本地缓存
                    logger.warning(
                        f"Range 直传失败，回退本地缓存下载："
                        f"{type(error).__name__}: {error}"
                    )
                    logger.debug("Range 直传异常详情", exc_info=True)
                    self._update(
                        task_id, phase="downloading", progress=0,
                        message="Range 直传不可用，正在回退本地缓存",
                    )
            temporary_path = ""
            cache_complete = False
            cache_reused = False
            progressive_result = None
            if not source_path:
                temporary_path = self._temporary_path(task)
                cache_path = Path(temporary_path)
                source_file = task["source_file"]
                cache_key = self._cache_key(task["source_provider"], source_file)
                expected_size = int(source_file.size or task["total"] or 0)
                cache_complete = self._validate_complete_cache(
                    cache_path, cache_key, source_file
                )
                if (
                        cache_path.exists() and not cache_complete
                        and not self._cache_parts_path(cache_path).is_file()
                ):
                    self._delete_cache_path(cache_path)
                if cache_complete:
                    cache_reused = True
                    source_path = temporary_path
                    self._update(task_id, phase="cache_ready", progress=50,
                                 transferred=int(expected_size * 0.5),
                                 stage_transferred=0, stage_total=0,
                                 speed_bytes_per_second=0.0,
                                 message="已找到完整本地缓存，继续目标盘上传")
                else:
                    download_message = f"正在从{source_name}下载到本地缓存"
                    downloader = source_provider.require(CloudDriveCapability.FILE_DOWNLOAD)
                    can_stream = (
                            fallback and not rapid_miss_abort
                            and algorithm == "md5"
                            and bool(checksum)
                            and expected_size > 0
                            and target.supports(CloudDriveCapability.LOCAL_UPLOAD)
                            and callable(getattr(
                        target.require(CloudDriveCapability.LOCAL_UPLOAD),
                        "upload_progressive", None,
                    ))
                    )
                    if can_stream:
                        progressive_result = self._progressive_transfer(
                            task_id, task, downloader, target, temporary_path,
                            cache_key, target_name_text,
                            algorithm, checksum,
                        )
                        source_path = temporary_path
                    else:
                        self._update(
                            task_id, phase="downloading",
                            message=download_message, progress=0,
                        )
                        download_speed = _SpeedSampler()

                        def download_progress(done: int, total: int) -> None:
                            if stop_event.is_set():
                                raise InterruptedError
                            percent = min(49, int(done * 50 / total)) if total else 0
                            display_total = int(task["total"] or total or 0)
                            speed = download_speed.update(done)
                            self._update(
                                task_id, progress=percent,
                                transferred=int(display_total * percent / 100),
                                stage_transferred=done, stage_total=total,
                                downloaded_bytes=done,
                                download_speed_bytes_per_second=speed,
                                speed_bytes_per_second=speed,
                                message=download_message,
                            )

                        source_path = downloader.download_file(
                            task["source_file"], temporary_path,
                            download_progress, stop_event.is_set,
                            preserve_partial=True,
                            download_threads=self._download_threads,
                        )
                        self._mark_complete_cache(
                            Path(source_path), cache_key, source_file
                        )
                    cache_complete = True
            if source_path and not Path(source_path).is_file():
                # 中继文件在传输途中被清理（历史删除联动删缓存/手工清缓存/插件重载）：
                # 明确报错并让下一次重试重新下载，避免抛出裸 Errno 2 让用户无从下手
                raise RuntimeError(
                    "本地中继缓存文件已被清理（可能来自历史删除联动清缓存或手工清理），"
                    "请重新点击重试，插件会重新下载"
                )
            if progressive_result:
                result_file = progressive_result.file
                self._update(
                    task_id, status="success", phase="done", progress=100,
                    transferred=task["total"],
                    downloaded_bytes=task["total"], uploaded_bytes=task["total"],
                    download_speed_bytes_per_second=0.0,
                    upload_speed_bytes_per_second=0.0,
                    speed_bytes_per_second=0.0,
                    stage_transferred=0, stage_total=0,
                    message=f"已边下载边上传到{target_name_text}",
                    method="progressive",
                    result_file_id=str(result_file.id or ""),
                    result_file_name=str(result_file.name or target_name),
                    result_file_size=int(result_file.size or task["total"] or 0),
                    result_sha1=str(result_file.sha1 or ""),
                    result_md5=str(result_file.md5 or ""),
                    finished_at=time.time(),
                )
                self._delete_cache_path_for_task(task, Path(temporary_path))
                return
            had_download = bool(temporary_path)
            hash_message = (
                "正在校验本地缓存"
                if cache_reused else (
                    f"正在校验从{source_name}下载的文件"
                    if had_download and source_name else "正在校验源文件"
                )
            )
            self._update(task_id, phase="hashing", message=hash_message,
                         progress=50 if had_download else 2,
                         stage_transferred=0, stage_total=0,
                         speed_bytes_per_second=0.0)
            upload_speed = _SpeedSampler()

            def progress(done: int, total: int) -> None:
                if stop_event.is_set():
                    raise InterruptedError
                raw_percent = min(99, int(done * 100 / total)) if total else 0
                percent = 50 + min(49, raw_percent // 2) if had_download else raw_percent
                display_total = int(task["total"] or total or 0)
                display_done = (
                    int(display_total * percent / 100) if had_download else done
                )
                speed = upload_speed.update(done)
                self._update(task_id, phase="uploading",
                             message=f"正在上传到{target_name_text}",
                             progress=percent, transferred=display_done,
                             total=display_total,
                             uploaded_bytes=done,
                             upload_speed_bytes_per_second=speed,
                             stage_transferred=done, stage_total=total,
                             speed_bytes_per_second=speed)

            self._update(task_id, phase="rapid_upload",
                         message=f"正在向{target_name_text}尝试秒传",
                         progress=52 if had_download else 5,
                         stage_transferred=0, stage_total=0)
            transfer_algorithm = "" if remote_rapid_attempted else algorithm
            transfer_checksum = "" if remote_rapid_attempted else checksum
            if (
                    not remote_rapid_attempted and not transfer_algorithm
                    and target.supports(CloudDriveCapability.RAPID_UPLOAD)
            ):
                supported = target.require(
                    CloudDriveCapability.RAPID_UPLOAD
                ).algorithms
                transfer_algorithm = (
                    "md5" if "md5" in supported
                    else "sha1" if "sha1" in supported else ""
                )
                if transfer_algorithm:
                    transfer_checksum = file_checksum(
                        source_path, transfer_algorithm
                    )
            result = CrossDriveTransfer().transfer(
                source_path, target, target_path, target_name,
                transfer_algorithm, transfer_checksum,
                # abort 模式：秒传未命中时不进入真实上传（fallback 模式参数不变）
                fallback=(fallback and not rapid_miss_abort),
                progress_callback=progress,
            )
            if not result:
                if rapid_miss_abort:
                    raise RapidMissAborted()
                raise RuntimeError("秒传未命中且普通上传失败" if fallback else "秒传未命中")
            result_file = result.file
            rapid = result.method == "rapid"
            self._update(task_id, status="success", phase="done", progress=100,
                         transferred=task["total"], speed_bytes_per_second=0.0,
                         stage_transferred=0, stage_total=0,
                         message=(f"已秒传到{target_name_text}"
                                  if rapid else f"已上传到{target_name_text}"),
                         method="rapid" if rapid else "fallback",
                         result_file_id=str(result_file.id or ""),
                         result_file_name=str(result_file.name or target_name),
                         result_file_size=int(result_file.size or task["total"] or 0),
                         result_sha1=str(result_file.sha1 or ""),
                         result_md5=str(result_file.md5 or ""),
                         finished_at=time.time())
            if temporary_path:
                self._delete_cache_path_for_task(task, Path(temporary_path))
        except InterruptedError:
            # 手动取消即清空中继（含已下完的完整缓存）：只删本任务的
            # cloud-transfer-<hash>.bin 及其 .meta.json/.parts.json（含 .tmp 变体），
            # 不碰同目录下其它文件；同 cache_key 还有任务在用时保留。
            retained = (
                'temporary_path' in locals() and temporary_path
                and self._cache_retained_for_others(task)
            )
            if 'temporary_path' in locals() and temporary_path and not retained:
                self._delete_cache_path(Path(temporary_path))
            if retained:
                logger.debug("任务取消：中继缓存仍有其它任务使用，保留文件")
            self._update(
                task_id, status="canceled", phase="canceled",
                message=("任务已取消，中继缓存保留（其它任务使用中）"
                         if retained else "任务已取消并已清理中继缓存"),
                speed_bytes_per_second=0.0, finished_at=time.time(),
            )
        except RapidMissAborted:
            # abort 模式（秒传未命中即中止）：无条件清理本任务中继缓存——即使
            # cache_complete 为真也不保留（与 fallback 保留完整缓存供重试不同）；
            # 探测在下载前发生时 temporary_path 尚未赋值，无需清理。
            if 'temporary_path' in locals() and temporary_path:
                self._delete_cache_path_for_task(task, Path(temporary_path))
            message = "未命中秒传，已按设置中止（未上传任何数据）"
            self._update(task_id, status="failed", phase="aborted",
                         message=message, error=message,
                         abort_reason="rapid_miss",
                         speed_bytes_per_second=0.0, finished_at=time.time())
        except Exception as error:
            if (
                    'temporary_path' in locals() and temporary_path
                    and not locals().get("cache_complete", False)
                    and not self._cache_parts_path(Path(temporary_path)).is_file()
            ):
                self._delete_cache_path_for_task(task, Path(temporary_path))
            self._update(task_id, status="failed", phase="failed", message="跨盘传输失败",
                         error=_describe_transfer_error(error),
                         speed_bytes_per_second=0.0, finished_at=time.time())
        finally:
            self._release_cache_ref(cache_key)
            self._transfer_slots.release()
