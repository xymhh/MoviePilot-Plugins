"""115 本地文件上传能力。"""

import contextlib
import hashlib
import io
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Callable, Dict, Mapping, Optional, Tuple

import requests
from app.log import logger
from requests.adapters import HTTPAdapter

from .files import P115DirectoryReader, P115FileService
from ...core import OwnerDelegator

try:
    from p115client import check_response

    P115_AVAILABLE = True
except ImportError:
    P115_AVAILABLE = False

try:
    # 分块并发上传走 p115oss 的 OSS 分块接口；缺失时自动回退顺序上传。
    from p115oss import (
        oss_multipart_upload_cancel,
        oss_multipart_upload_complete,
        oss_multipart_upload_init,
        oss_multipart_upload_part,
    )

    P115OSS_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    P115OSS_AVAILABLE = False

    def oss_multipart_upload_cancel(*args, **kwargs):
        raise RuntimeError("p115oss 不可用")

    def oss_multipart_upload_complete(*args, **kwargs):
        raise RuntimeError("p115oss 不可用")

    def oss_multipart_upload_init(*args, **kwargs):
        raise RuntimeError("p115oss 不可用")

    def oss_multipart_upload_part(*args, **kwargs):
        raise RuntimeError("p115oss 不可用")

# 115 分块上传的分片大小：默认 16MB（官方建议下限 10MB）。
# 旧实现用 partsize=-1 自动分块，实测只有 400KB，单个 3.8GB 文件要 9317 片，
# 顺序 PUT 时任意一片抖动即整单失败（2026-10-10 线上事故）。
P115_UPLOAD_PART_SIZE = 16 * 1024 * 1024
# OSS 单次分块上传最多 10000 片，留出余量后按 9000 片控制分片数。
P115_MAX_UPLOAD_PARTS = 9000
# 分片并发上传的默认并发度（可在插件配置里用 cross_transfer_upload_concurrency 覆盖）。
P115_DEFAULT_UPLOAD_CONCURRENCY = 4
P115_MAX_UPLOAD_CONCURRENCY = 16


def upload_part_size(file_size: int) -> int:
    """按文件大小选择 115 分片大小：默认 16MB，超大文件放大以控制分片数。"""
    size = max(0, int(file_size or 0))
    part_size = P115_UPLOAD_PART_SIZE
    if size > part_size * P115_MAX_UPLOAD_PARTS:
        multiple = int(math.ceil(size / (part_size * P115_MAX_UPLOAD_PARTS)))
        part_size *= max(1, multiple)
    return part_size


def _new_session(pool_size: int = 2) -> requests.Session:
    """建一个连接池有硬上限的 session，避免并发把容器文件句柄打满。"""
    session = requests.Session()
    adapter = HTTPAdapter(
        pool_connections=max(1, int(pool_size)),
        pool_maxsize=max(1, int(pool_size)),
        max_retries=0,
        pool_block=True,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class _SourceLink:
    """源盘直链状态：线程安全快照 + 刷新节流。"""

    def __init__(
            self, url: str, headers: Mapping[str, str],
            refresh_link: Optional[Callable[[], tuple]] = None,
            interval: float = 30.0,
    ):
        self._lock = Lock()
        self._url = str(url or "")
        self._headers = dict(headers or {})
        self._refresh_link = refresh_link
        self._interval = float(interval)
        self._refreshed_at = time.time()

    def snapshot(self) -> Tuple[str, Dict[str, str]]:
        with self._lock:
            return self._url, dict(self._headers)

    def refresh(self) -> bool:
        """按节流间隔重新取链；返回是否真的刷新成功。"""
        refresh_link = self._refresh_link
        if not refresh_link:
            return False
        with self._lock:
            now = time.time()
            if now - self._refreshed_at < self._interval:
                return False
            self._refreshed_at = now
        try:
            url, headers = refresh_link()
        except Exception as error:  # noqa: BLE001 - 取链失败沿用旧链接重试
            logger.warning(f"刷新源盘直链失败，沿用原链接重试：{error}")
            return False
        if not url:
            return False
        with self._lock:
            self._url = str(url)
            self._headers = dict(headers or {})
        return True


def read_source_range(
        session: requests.Session, link: _SourceLink, start: int, end: int,
        stop_requested: Optional[Callable[[], bool]] = None, attempts: int = 3,
) -> bytes:
    """从源盘读取 ``[start, end]`` 字节，失败时刷新直链后重试。"""
    expected = int(end) - int(start) + 1
    last_error: Optional[Exception] = None
    for attempt in range(max(1, int(attempts))):
        if stop_requested and stop_requested():
            raise InterruptedError
        if attempt:
            link.refresh()
        url, headers = link.snapshot()
        request_headers = dict(headers)
        request_headers["Range"] = f"bytes={start}-{end}"
        request_headers.setdefault("Accept-Encoding", "identity")
        try:
            with session.get(
                    url, headers=request_headers, timeout=(15, 120), stream=True,
            ) as response:
                if response.status_code != 206:
                    raise IOError(
                        f"源盘不支持 HTTP Range：HTTP {response.status_code}"
                    )
                content_range = str(response.headers.get("Content-Range") or "")
                if not content_range.lower().startswith(
                        f"bytes {start}-{end}/".lower()
                ):
                    raise IOError(
                        f"源盘返回无效 Content-Range：{content_range or '空'}"
                    )
                data = response.content
            if len(data) != expected:
                raise IOError(f"源盘 Range 读取不完整：{len(data)}/{expected}")
            return data
        except (requests.RequestException, IOError) as error:
            last_error = error
            if attempt >= int(attempts) - 1:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise last_error or IOError("源盘 Range 读取失败")


class _RangeReader:
    """并发分片上传用的源盘读取器（每实例独占一个受限连接池）。"""

    def __init__(
            self, link: _SourceLink, size: int,
            stop_requested: Optional[Callable[[], bool]] = None,
    ):
        self._link = link
        self._size = int(size)
        self._stop_requested = stop_requested
        self._session = _new_session(2)

    def read_range(self, start: int, length: int) -> bytes:
        begin = max(0, int(start))
        end = min(self._size, begin + max(0, int(length))) - 1
        if end < begin:
            return b""
        return read_source_range(
            self._session, self._link, begin, end, self._stop_requested
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._session.close()


class P115UploadService(OwnerDelegator):
    """使用 p115client 原生上传接口将本地文件写入 115。"""

    rapid_requires_local_file = True

    @staticmethod
    def _file_sha1(path: Path) -> str:
        digest = hashlib.sha1()
        with path.open("rb") as file:
            while chunk := file.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest().upper()

    def try_rapid_upload(
            self, local_path: str, save_path: str, target_name: str,
            algorithm: str, checksum: str, size: int,
    ) -> bool:
        if algorithm != "sha1" or not P115_AVAILABLE or not self.client:
            return False
        source = Path(local_path)
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(save_path, create=True)
        if not source.is_file() or not lookup.checked or lookup.directory_id is None:
            return False

        def read_range(value: str):
            start, end = (int(part) for part in value.split("-", 1))
            with source.open("rb") as handle:
                handle.seek(start)
                return handle.read(end - start + 1)

        response = self._rate_limited_call(
            self.client.upload_file_init,
            target_name,
            checksum.upper(),
            int(size),
            read_range_bytes_or_hash=read_range,
            pid=int(lookup.directory_id or 0),
        )
        check_response(response)
        if not response.get("reuse"):
            return False
        self._target_file_cache.clear()
        return True

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not P115_AVAILABLE or not self.client:
            logger.error("115 本地上传不可用：客户端未初始化")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            logger.error(f"115 本地上传文件不存在：{source}")
            return False
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(
            save_path, create=True
        )
        if not lookup.checked or lookup.directory_id is None:
            logger.error(f"115 本地上传目录不可用：{save_path}")
            return False
        upload_name = str(target_name or source.name).strip()
        checksum = str(file_sha1 or "").strip().upper() or self._file_sha1(source)
        try:
            file_size = source.stat().st_size
            part_size = upload_part_size(file_size)
            if progress_callback:
                with source.open("rb") as file:
                    response = self._rate_limited_call(
                        self.client.upload_file,
                        _ProgressReader(file, file_size, progress_callback),
                        pid=int(lookup.directory_id or 0),
                        filename=upload_name,
                        filesha1=checksum,
                        filesize=file_size,
                        partsize=part_size,
                        max_retries=0,
                    )
            else:
                response = self._rate_limited_call(
                    self.client.upload_file,
                    source,
                    pid=int(lookup.directory_id or 0),
                    filename=upload_name,
                    filesha1=checksum,
                    filesize=file_size,
                    partsize=part_size,
                    max_retries=0,
                )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(f"115 本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
            return True
        except Exception as error:
            logger.error(f"115 本地文件上传失败：{source.name}，{error}")
            return False

    def upload_from_link(
            self,
            download_url: str,
            download_headers: Mapping[str, str],
            save_path: str,
            target_name: str,
            file_size: int,
            algorithm: str,
            checksum: str,
            progress_callback: Optional[Callable[[int, int], None]] = None,
            stop_requested: Optional[Callable[[], bool]] = None,
            refresh_link: Optional[Callable[[], tuple[str, Mapping[str, str]]]] = None,
    ) -> bool | str:
        """优先用远程 Range 完成 SHA1 秒传，未命中时继续流式上传。"""
        if (
                not P115_AVAILABLE
                or not self.client
                or algorithm.lower() != "sha1"
                or not checksum
                or int(file_size or 0) <= 0
                or not download_url
        ):
            return False
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(
            save_path, create=True
        )
        if not lookup.checked or lookup.directory_id is None:
            raise RuntimeError(f"115 远程上传目录不可用：{save_path}")
        reader = _HttpRangeReader(
            download_url, download_headers, file_size, stop_requested,
            refresh_link=refresh_link,
        )
        uploaded = 0

        def report(size: int) -> None:
            nonlocal uploaded
            if stop_requested and stop_requested():
                raise InterruptedError
            uploaded = min(file_size, uploaded + max(0, int(size or 0)))
            if progress_callback:
                progress_callback(uploaded, file_size)

        try:
            init_response = self._rate_limited_call(
                self.client.upload_file_init,
                filename=target_name,
                filesize=file_size,
                filesha1=checksum.upper(),
                pid=int(lookup.directory_id or 0),
                read_range_bytes_or_hash=reader.read_range_sha1,
            )
            check_response(init_response)
            if init_response.get("reuse"):
                self._target_file_cache.clear()
                logger.info(
                    f"115 SHA1 秒传完成：{target_name} -> "
                    f"{save_path}/{target_name}"
                )
                return "rapid"

            part_size = upload_part_size(file_size)
            concurrency = self._upload_concurrency()
            if (
                    P115OSS_AVAILABLE
                    and P115_AVAILABLE
                    and concurrency > 1
                    and int(file_size) > part_size
                    and (init_response.get("data") or {}).get("url")
            ):
                try:
                    self._upload_parts_concurrently(
                        init_response.get("data") or {},
                        reader.url, reader.headers, file_size, part_size,
                        progress_callback=progress_callback,
                        stop_requested=stop_requested,
                        refresh_link=refresh_link,
                        concurrency=concurrency,
                    )
                    self._target_file_cache.clear()
                    if progress_callback:
                        progress_callback(file_size, file_size)
                    logger.info(
                        f"115 Range 直传完成（{concurrency} 路分片并发）："
                        f"{target_name} -> {save_path}/{target_name}"
                    )
                    return "remote"
                except InterruptedError:
                    raise
                except Exception as error:  # noqa: BLE001 - 失败后回退顺序上传
                    logger.warning(
                        f"115 分片并发直传失败，回退顺序上传（{target_name}）：{error}"
                    )
            reader.seek(0)
            response = self._rate_limited_call(
                self.client.upload_file,
                reader,
                pid=int(lookup.directory_id or 0),
                filename=target_name,
                filesha1=checksum.upper(),
                filesize=file_size,
                partsize=part_size,
                reporthook=report,
                max_retries=0,
            )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(
                f"115 Range 直传完成：{target_name} -> {save_path}/{target_name}"
            )
            return "remote"
        except InterruptedError:
            raise
        except Exception as error:
            logger.warning(f"115 Range 直传失败，将回退本地缓存：{error}")
            return False
        finally:
            reader.close()


    def _upload_concurrency(self) -> int:
        """115 分片并发上传的并发度；``0`` 表示关闭（回退顺序上传）。"""
        value = getattr(
            getattr(self, "_owner", None),
            "_cross_transfer_upload_concurrency",
            P115_DEFAULT_UPLOAD_CONCURRENCY,
        )
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = P115_DEFAULT_UPLOAD_CONCURRENCY
        return max(0, min(value, P115_MAX_UPLOAD_CONCURRENCY))

    def _upload_parts_concurrently(
            self,
            upload_data: Mapping[str, object],
            source_url: str,
            source_headers: Mapping[str, str],
            file_size: int,
            part_size: int,
            progress_callback: Optional[Callable[[int, int], None]] = None,
            stop_requested: Optional[Callable[[], bool]] = None,
            refresh_link: Optional[Callable[[], tuple]] = None,
            concurrency: int = P115_DEFAULT_UPLOAD_CONCURRENCY,
    ) -> bool:
        """把源盘数据分片并发 PUT 到 115 的分块上传任务。

        旧实现由 ``p115oss`` 顺序分片上传（单连接），夸克单连接只有
        100~200KB/s，导致跨盘直传被拉到几十 KB/s。这里改为：多线程各自
        从源盘 Range 读一片、立即 PUT 到 115 的 OSS 分块接口，最后 complete。

        任一步失败都会取消该上传任务并抛出异常，由调用方回退顺序上传。
        """
        if not P115OSS_AVAILABLE:
            raise RuntimeError("p115oss 不可用，无法并发分片上传")
        target = str(upload_data.get("url") or "")
        callback = upload_data.get("callback") or {}
        if not target or not callback:
            raise RuntimeError("115 分块上传缺少 OSS 目标地址或回调信息")
        total = int(file_size or 0)
        size = int(part_size or 0) or upload_part_size(total)
        if total <= 0 or size <= 0:
            raise ValueError("115 分片上传参数无效")
        parts_total = int(math.ceil(total / size))
        workers = max(1, min(int(concurrency or P115_DEFAULT_UPLOAD_CONCURRENCY),
                             P115_MAX_UPLOAD_CONCURRENCY, parts_total))
        upload_id = oss_multipart_upload_init(target)
        if not upload_id:
            raise RuntimeError("115 分块上传初始化失败")
        link = _SourceLink(source_url, source_headers, refresh_link)
        readers = [_RangeReader(link, total, stop_requested) for _ in range(workers)]
        pool: Queue = Queue()
        for reader in readers:
            pool.put(reader)
        lock = Lock()
        reported: Dict[int, int] = {}
        uploaded = 0

        def report(part_number: int, count: int) -> None:
            nonlocal uploaded
            with lock:
                previous = reported.get(part_number, 0)
                current = max(previous, int(count or 0))
                reported[part_number] = current
                uploaded += current - previous
                done = min(total, uploaded)
            if progress_callback:
                progress_callback(done, total)

        def put_part(part_number: int) -> dict:
            reader = pool.get()
            try:
                start = (part_number - 1) * size
                length = min(size, total - start)
                last_error: Optional[Exception] = None
                for attempt in range(2):
                    if stop_requested and stop_requested():
                        raise InterruptedError
                    try:
                        data = reader.read_range(start, length)
                        if len(data) != length:
                            raise IOError(
                                f"源盘第 {part_number} 分片读取不完整："
                                f"{len(data)}/{length}"
                            )
                        return oss_multipart_upload_part(
                            target, upload_id, data, part_number=part_number,
                            reporthook=lambda count, number=part_number: report(
                                number, count
                            ),
                        )
                    except InterruptedError:
                        raise
                    except Exception as error:  # noqa: BLE001 - 单片失败重试一次
                        last_error = error
                        if attempt:
                            raise
                        logger.warning(
                            f"115 第 {part_number} 分片上传失败，重试一次：{error}"
                        )
                        time.sleep(1)
                raise last_error or IOError(f"115 第 {part_number} 分片上传失败")
            finally:
                pool.put(reader)

        try:
            parts: list = []
            with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="p115-upload-part",
            ) as executor:
                futures = [
                    executor.submit(put_part, number)
                    for number in range(1, parts_total + 1)
                ]
                try:
                    for future in as_completed(futures):
                        parts.append(future.result())
                except Exception:
                    for future in futures:
                        future.cancel()
                    raise
            if len(parts) != parts_total:
                raise IOError(
                    f"115 分块上传分片数不符：{len(parts)}/{parts_total}"
                )
            parts.sort(key=lambda part: int(part["PartNumber"]))
            response = oss_multipart_upload_complete(
                target, upload_id, parts, callback
            )
            if P115_AVAILABLE:
                check_response(response)
        except BaseException:
            with contextlib.suppress(Exception):
                oss_multipart_upload_cancel(target, upload_id)
            raise
        finally:
            for reader in readers:
                reader.close()
        return True


class _ProgressReader:
    """为 p115client 的文件读取过程补充字节进度回调。"""

    def __init__(self, file, total: int, callback: Callable[[int, int], None]):
        self._file = file
        self._total = total
        self._callback = callback

    def read(self, size: int = -1):
        chunk = self._file.read(size)
        self._callback(self._file.tell(), self._total)
        return chunk

    def __getattr__(self, name):
        return getattr(self._file, name)


class _HttpRangeReader(io.RawIOBase):
    """把远程下载地址适配为 p115oss 所需的 read/seek 文件对象。"""

    def __init__(
            self, url: str, headers: Mapping[str, str], size: int,
            stop_requested: Optional[Callable[[], bool]] = None,
            refresh_link: Optional[Callable[[], tuple[str, Mapping[str, str]]]] = None,
    ):
        self._url = url
        self._headers = dict(headers or {})
        self._refresh_link = refresh_link
        self._refreshed_at = time.time()
        self._size = int(size)
        self._position = 0
        self._stop_requested = stop_requested
        # 连接池设硬上限：并发分片时不允许无限新建 socket（避免 Errno 24）。
        self._session = _new_session(4)
        # 115 初始化与校验会对同一段重复取 sha1，缓存后不再重复读源盘。
        self._sha1_cache: Dict[Tuple[int, int], str] = {}

    @property
    def url(self) -> str:
        return self._url

    @property
    def headers(self) -> Dict[str, str]:
        return dict(self._headers)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_CUR:
            offset += self._position
        elif whence == io.SEEK_END:
            offset += self._size
        elif whence != io.SEEK_SET:
            raise ValueError(f"不支持的 seek whence：{whence}")
        if offset < 0:
            raise ValueError("seek 位置不能小于 0")
        self._position = min(int(offset), self._size)
        return self._position

    def _refresh_source_link(self) -> None:
        """直链限速/失效时重新取链（30 秒节流），让长任务能继续跑下去。"""
        if not self._refresh_link:
            return
        now = time.time()
        if now - self._refreshed_at < 30:
            return
        self._refreshed_at = now
        try:
            url, headers = self._refresh_link()
        except Exception as error:  # noqa: BLE001 - 取链失败沿用旧链接重试
            logger.warning(f"刷新源盘直链失败，沿用原链接重试：{error}")
            return
        if url:
            self._url = str(url)
            self._headers = dict(headers or {})
            logger.info("已刷新源盘直链，继续 Range 直传")

    def read(self, size: int = -1) -> bytes:
        if self._stop_requested and self._stop_requested():
            raise InterruptedError
        if self._position >= self._size:
            return b""
        length = self._size - self._position if size is None or size < 0 else size
        end = min(self._size, self._position + int(length)) - 1
        if end < self._position:
            return b""
        start = self._position
        expected = end - start + 1
        last_error: Optional[Exception] = None
        for attempt in range(3):
            if attempt:
                self._refresh_source_link()
            headers = dict(self._headers)
            headers["Range"] = f"bytes={start}-{end}"
            headers.setdefault("Accept-Encoding", "identity")
            try:
                with self._session.get(
                        self._url, headers=headers, timeout=(15, 120), stream=True,
                ) as response:
                    if response.status_code != 206:
                        raise IOError(
                            f"源盘不支持 HTTP Range：HTTP {response.status_code}"
                        )
                    content_range = str(response.headers.get("Content-Range") or "")
                    if not content_range.lower().startswith(
                            f"bytes {start}-{end}/".lower()
                    ):
                        raise IOError(
                            f"源盘返回无效 Content-Range：{content_range or '空'}"
                        )
                    data = response.content
                if len(data) != expected:
                    raise IOError(
                        f"源盘 Range 读取不完整：{len(data)}/{expected}"
                    )
            except (requests.RequestException, IOError) as error:
                last_error = error
                if attempt >= 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
                continue
            self._position += len(data)
            return data
        raise last_error or IOError("源盘 Range 读取失败")

    def read_range_sha1(self, value: str) -> str:
        """按 115 的二次校验范围读取源盘并返回大写 SHA1（同段命中缓存）。"""
        try:
            start, end = (int(part) for part in str(value).split("-", 1))
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效的 115 校验范围：{value}") from error
        if start < 0 or end < start or end >= self._size:
            raise ValueError(f"115 校验范围越界：{value}/{self._size}")
        key = (start, end)
        cached = self._sha1_cache.get(key)
        if cached:
            return cached
        current = self._position
        try:
            self.seek(start)
            content = self.read(end - start + 1)
        finally:
            self._position = current
        digest = hashlib.sha1(content).hexdigest().upper()
        if len(self._sha1_cache) >= 4096:
            self._sha1_cache.clear()
        self._sha1_cache[key] = digest
        return digest

    def close(self) -> None:
        if not self.closed:
            self._session.close()
        super().close()
