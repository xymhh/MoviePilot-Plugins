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
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

import requests
from app.log import logger
from requests.adapters import HTTPAdapter

from .files import P115DirectoryReader, P115FileService
from .upload_policy import (
    UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS,
    UPLOAD_BREAKER_DEFAULT_THRESHOLD,
    UPLOAD_DATA_PUT_MIN_INTERVAL,
    UPLOAD_RETRY_POLICY,
    UploadCircuitBreaker,
    retry_delay,
)
from .upload_proxy import oss_request_kwargs
from ...core import OwnerDelegator
from ...core.error_text import describe_upload_error
from ...core.failure import is_rate_limit_error
from ...core.file_hash import file_sha1
from ...core.telemetry import api_meter, format_snapshot

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

try:
    # 本地文件上传走 oss2 自管分片（p115disk 同款路子）：分片级重试 +
    # token 续期。缺失时回退到 p115oss 的 client.upload_file（旧路子）。
    from oss2 import Bucket as Oss2Bucket
    from oss2 import StsAuth as Oss2StsAuth
    from oss2 import determine_part_size as oss2_determine_part_size
    from oss2.exceptions import ServerError as OssServerError
    from oss2.models import PartInfo as OssPartInfo
    from oss2.utils import SizedFileAdapter as OssSizedFileAdapter
    from oss2.utils import b64encode_as_string as oss2_b64encode

    OSS2_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时的降级分支
    OSS2_AVAILABLE = False
    Oss2Bucket = None
    Oss2StsAuth = None
    oss2_determine_part_size = None
    OssServerError = None
    OssPartInfo = None
    OssSizedFileAdapter = None
    oss2_b64encode = None

# oss2 自管分片上传的分片大小：10MB（p115disk 同款口径）。
OSS2_UPLOAD_PART_SIZE = 10 * 1024 * 1024
# OSS STS token 提前刷新阈值（分钟）：分片前若 token 在该时间内过期，先刷新。
OSS2_TOKEN_REFRESH_THRESHOLD_MINUTES = 5
# 单片失败重试次数、init 重试次数与初始退避秒数：统一重试策略表
# （upload_policy.UPLOAD_RETRY_POLICY）是唯一事实来源，下面只是旧名
# 别名（p115disk 同款口径：单片最多传 2 次、init 3 次 2 秒起翻倍）。
OSS2_PART_MAX_RETRIES = int(UPLOAD_RETRY_POLICY["part"]["attempts"])
OSS2_INIT_MAX_RETRIES = int(UPLOAD_RETRY_POLICY["init"]["attempts"])
OSS2_INIT_RETRY_DELAY_SECONDS = float(UPLOAD_RETRY_POLICY["init"]["base_delay"])
# 115 OSS 上传域名（p115disk 同款）。1.18.3：由 http 改 https——
# 2026-11-18 线上实测：oss2 走 http 时每片 10MB 传完等响应都会
# 60 秒读超时、连接丢掉、下一片重开连接，有效速度被拖垮；而
# p115client 默认 https 的 upload_file 走 CONNECT 隧道一条连接
# 流到底（2.2GB、4MB/s）。两条路打的是同一主机
# fhnfile.oss-cn-shenzhen.aliyuncs.com，差别就是 scheme。
OSS2_ENDPOINT = "https://oss-cn-shenzhen.aliyuncs.com"
# token 过期类错误码（oss2 ServerError.code）。
OSS2_TOKEN_EXPIRED_CODES = ("InvalidAccessKeyId", "SecurityTokenExpired")
# iOS UA（与 drive/p115/client.py 的 IOS_USER_AGENT 同值；upload.py 不能
# 从 client 导入它，会循环引用）。upload_gettoken 时带上，取 p115disk 同款。
OSS2_IOS_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
    "115wangpan_ios/36.2.20"
)

# 115 分块上传的分片大小：默认 16MB（官方建议下限 10MB）。
# 旧实现用 partsize=-1 自动分块，实测只有 400KB，单个 3.8GB 文件要 9317 片，
# 顺序 PUT 时任意一片抖动即整单失败（2026-10-10 线上事故）。
P115_UPLOAD_PART_SIZE = 16 * 1024 * 1024
# OSS 单次分块上传最多 10000 片，留出余量后按 9000 片控制分片数。
P115_MAX_UPLOAD_PARTS = 9000
# 分片并发上传的默认并发度（可在插件配置里用 cross_transfer_upload_concurrency
# 覆盖；计划 §14：先从 2 起步实测，不要直接拉满）。
P115_DEFAULT_UPLOAD_CONCURRENCY = 2
P115_MAX_UPLOAD_CONCURRENCY = 16

#: 驱动层整份重传次数（与 MP 自带 u115 存储的 3 次重试同一口径），
#: 退避语义见统一重试策略表 UPLOAD_RETRY_POLICY["file"]。
_UPLOAD_MAX_ATTEMPTS = int(UPLOAD_RETRY_POLICY["file"]["attempts"])

#: manager 未提供数据面限流器时的兜底实例（测试桩 owner 等），
#: 首次使用时懒创建（见 _fallback_data_limiter）。
_DATA_PUT_LIMITER: Any = None


def _monotonic_progress_callback(
    progress_callback: Optional[Callable[[int, int], None]],
) -> Optional[Callable[[int, int], None]]:
    """把上传进度回调包一层单调递增保护（并发分片与重传用）。

    并发分片里各 worker 的分片内进度会交错上报、重传时分片内
    consumed 从 0 重来，直接报给登记表会让卡片百分比来回跳。
    只放行严格递增的值；传完（uploaded >= total）永远放行，
    登记表的完成清理不能被拦住。回调在锁内执行，保证多 worker
    上报顺序与单调值一致。1.18.3。
    """
    if progress_callback is None:
        return None
    state: Dict[str, Any] = {"max": 0, "lock": Lock()}

    def _report(uploaded: int, total: int) -> None:
        try:
            value = int(uploaded or 0)
            total_value = int(total or 0)
        except (TypeError, ValueError):
            return
        with state["lock"]:
            if value <= state["max"] and value < total_value:
                return
            if value > state["max"]:
                state["max"] = value
            progress_callback(value, total_value)

    return _report


def _oss2_part_progress_callback(
    progress_callback: Optional[Callable[[int, int], None]],
    part_offset: int,
    file_size: int,
) -> Optional[Callable[[int, int], None]]:
    """把 oss2 分片内进度回调 (consumed, total) 映射成全局已传字节。

    oss2 的 progress_callback 是按本片流算的（consumed/total 都是
    本片口径）；全局已传 = 分片起始偏移 + 本片已传，file_size 封顶。
    progress_callback 为空时返回 None。1.18.3。
    """
    if progress_callback is None:
        return None
    try:
        offset = int(part_offset or 0)
        size = int(file_size or 0)
    except (TypeError, ValueError):
        return None
    if size <= 0:
        return None

    def _on_part_progress(consumed: int, _total: int) -> None:
        try:
            value = min(offset + int(consumed or 0), size)
        except (TypeError, ValueError):
            return
        if value <= 0:
            return
        progress_callback(value, size)

    return _on_part_progress


class _NullRateLimiter:
    """数据面 pacing 空操作兜底：owner 没给限流器且真类取不到时用。

    只影响测试桩 owner；线上 owner 永远是带 data_rate_limiter 的 manager。
    """

    def acquire(self) -> None:
        return None


def _fallback_data_limiter() -> Any:
    """manager 未提供数据面限流器时的兜底（测试桩 owner 等）。

    这里不用模块级 ``from ..common import DriveRateLimiter``：全量测试里
    别的测试文件会把 sys.modules 里的 drive.common 换成桩
    （DriveRateLimiter 被置成 object），模块级绑定会在 import 时就炸掉；
    函数内现取现用，取不到真类时退回空操作限流。
    """
    global _DATA_PUT_LIMITER
    if _DATA_PUT_LIMITER is not None:
        return _DATA_PUT_LIMITER
    try:
        from ..common import DriveRateLimiter

        candidate = DriveRateLimiter(
            min_interval=UPLOAD_DATA_PUT_MIN_INTERVAL, jitter_ratio=0.2
        )
        # 真类才有 acquire；被桩成 object 的 DriveRateLimiter 没有。
        if not callable(getattr(candidate, "acquire", None)):
            raise TypeError("DriveRateLimiter 被桩替换")
    except Exception:
        candidate = _NullRateLimiter()
    _DATA_PUT_LIMITER = candidate
    return candidate


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
            # 源盘侧重试也走统一策略表（0.5 秒起步，指数 + 抖动）。
            time.sleep(retry_delay("range", attempt + 1))
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

    @property
    def last_error(self) -> str:
        """最近一次 ``upload_file`` 失败的原因原文。

        upload_file 只返回 bool，调用方（CrossDriveTransfer）拿不到
        原因；这里把每次失败的原因留下来，供上层写进任务 error 与
        历史记录失败原因——否则页面上永远只能看到一句通用的
        「秒传未命中且普通上传失败」，查不到到底是哪一步错了。
        """
        return str(getattr(self, "_last_upload_error", "") or "")

    def _upload_stat(self) -> Dict[str, Any]:
        """本次尝试的计量小字典（backend/rapid/parts/rate_limited）。

        upload_file 每轮尝试开始时重置；内层方法（_upload_via_oss2 /
        _upload_via_p115oss）边跑边填。直接调内层方法时也可能没有
        初始化过，这里兜底建一个，绝不因计量抛错。
        """
        stats = getattr(self, "_upload_stats", None)
        if not isinstance(stats, dict):
            stats = {"backend": "", "rapid": False, "parts": 0, "rate_limited": 0}
            self._upload_stats = stats
        return stats

    def _data_rate_limiter(self) -> Any:
        """数据面 PUT 起步 pacing 门控：优先用 manager 上的共享实例。

        manager 未提供时（测试桩 owner 等）用模块级兜底实例。只平滑
        PUT 起步突发，不节流吞吐；控制面 1.5 秒不在此列。
        判据用鸭子类型（有可调用的 acquire 即可）：全量测试里
        drive.common 可能被桩替换，isinstance 会误判。
        """
        limiter = getattr(self._owner, "data_rate_limiter", None)
        if limiter is not None and callable(getattr(limiter, "acquire", None)):
            return limiter
        return _fallback_data_limiter()

    def _note_rate_limited(self, error: BaseException) -> bool:
        """429/限流：记一次计量（upload:429 + 本次统计）并返回 True；
        非限流返回 False。调用方据此选 429 退避还是普通退避。"""
        if not is_rate_limit_error(error):
            return False
        api_meter.record("upload:429")
        stats = self._upload_stat()
        stats["rate_limited"] = int(stats.get("rate_limited") or 0) + 1
        return True

    def _breaker_threshold(self) -> int:
        """熔断阈值（连续失败几次后打开）：每次从配置现读，改配置即生效。"""
        try:
            value = int(
                getattr(
                    self._owner, "_upload_breaker_threshold",
                    UPLOAD_BREAKER_DEFAULT_THRESHOLD,
                )
            )
        except (TypeError, ValueError):
            value = UPLOAD_BREAKER_DEFAULT_THRESHOLD
        return max(1, min(value, 100))

    def _breaker_cooldown_seconds(self) -> float:
        """熔断冷却秒数：每次从配置现读，改配置即生效。"""
        try:
            value = float(
                getattr(
                    self._owner, "_upload_breaker_cooldown_seconds",
                    UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS,
                )
            )
        except (TypeError, ValueError):
            value = UPLOAD_BREAKER_DEFAULT_COOLDOWN_SECONDS
        return max(5.0, min(value, 3600.0))

    def _upload_breaker(self) -> UploadCircuitBreaker:
        """上传熔断器：懒创建，落在 owner（manager）上，等价于每账号一个。

        配置每次使用时重读，改配置不用重启。秒传探针
        （try_rapid_upload / try_rapid_upload_by_hash）不走这里，
        熔断打开时仍可放行。
        """
        breaker = getattr(self, "_upload_circuit_breaker", None)
        if not isinstance(breaker, UploadCircuitBreaker):
            breaker = UploadCircuitBreaker()
            self._upload_circuit_breaker = breaker
        breaker.configure(self._breaker_threshold(), self._breaker_cooldown_seconds())
        return breaker

    def _put_part_with_retry(
            self,
            put: Callable[[], Any],
            *,
            stop_requested: Optional[Callable[[], bool]] = None,
            on_retry: Optional[Callable[[BaseException, int], bool]] = None,
    ) -> Any:
        """单片 PUT 带统一重试：oss2 顺序/并发、远程并发共用。

        ``put``：无参可调用，执行一次 PUT 并返回结果；失败抛异常。
        ``on_retry``：可选回调 ``on_retry(error, attempt)``，每次重试前
        调用，返回 False 立即放弃；可用于 token 刷新等补救。
        429 用 5 秒起步的限流退避，其余按策略表；重试耗尽抛最后一次异常。
        """
        attempts = int(UPLOAD_RETRY_POLICY["part"]["attempts"])
        last_error: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            if stop_requested is not None and stop_requested():
                raise InterruptedError
            try:
                self._data_rate_limiter().acquire()
                return put()
            except InterruptedError:
                raise
            except Exception as error:
                last_error = error
                if attempt >= attempts:
                    break
                rate_limited = self._note_rate_limited(error)
                if on_retry is not None and not on_retry(error, attempt):
                    break
                time.sleep(retry_delay("part", attempt, rate_limited=rate_limited))
        raise last_error or IOError("115 分片上传失败")

    def _run_parts_concurrently(
            self,
            jobs: Any,
            worker_fn: Callable[[Any], Any],
            *,
            workers: int,
    ) -> list:
        """并发分片执行器：本地 oss2 并发、远程 Range 直传共用。

        ThreadPoolExecutor + 首错取消剩余任务。``jobs`` 是任务标识
        （分片号），``worker_fn(job)`` 执行一片并返回结果；返回按
        完成顺序排列的结果列表（调用方按分片号自行排序）。
        任一任务失败即取消其余任务并抛出首个异常。
        """
        job_list = list(jobs)
        pool_size = max(
            1,
            min(int(workers or 1), P115_MAX_UPLOAD_CONCURRENCY, len(job_list) or 1),
        )
        results = []
        with ThreadPoolExecutor(
                max_workers=pool_size, thread_name_prefix="p115-upload-part"
        ) as executor:
            futures = [executor.submit(worker_fn, job) for job in job_list]
            try:
                for future in as_completed(futures):
                    results.append(future.result())
            except Exception:
                for future in futures:
                    future.cancel()
                raise
        return results

    def _oss_proxy_kwargs(self) -> Dict[str, Any]:
        """OSS 数据面（分片 PUT / 元数据）的代理参数；未配置时为空字典。

        2026-11-14 真机修复：本类的 ``_owner`` 是 ``P115ClientManager``，不是插件实例。
        ``P115ClientManager.__getattr__`` 只解析职责组件、**不会**转发到插件，
        因此旧写法 ``getattr(self._owner, "_cross_transfer_upload_proxy", "")`` 恒为空
        （异常被吞），代理从来没生效过——日志里一条「数据面已启用代理」都没有。

        manager 在 ``create_client`` 时已把配置里的代理规范化存进 ``_upload_proxies``，
        这里直接读它；同时保留对 owner 上 ``_cross_transfer_upload_proxy`` 的兼容回退
        （测试桩/未来若把插件直接挂到 manager 上）。
        """
        proxies = getattr(self._owner, "_upload_proxies", None)
        if isinstance(proxies, dict) and proxies:
            # normalize_proxies 已给出 {"http": ..., "https": ...}，取 https 作代理地址
            url = str(proxies.get("https") or proxies.get("http") or "").strip()
            if url:
                kwargs = oss_request_kwargs(url)
                # 只在第一次真正拿到 session 时打一次，便于真机核对代理是否生效
                if kwargs and not getattr(self, "_oss_proxy_logged", False):
                    self._oss_proxy_logged = True
                    logger.info(
                        f"115 上传数据面已启用代理（OSS 分片走代理，API 直连）：{url}"
                    )
                return kwargs
            return {}
        try:
            proxy = str(
                getattr(self._owner, "_cross_transfer_upload_proxy", "") or ""
            ).strip()
        except Exception:
            return {}
        return oss_request_kwargs(proxy)

    @staticmethod
    def _file_sha1(path: Path) -> str:
        """文件 SHA1：唯一实现见 core/file_hash.py（此处保留薄壳，因测试桩点）。"""
        return file_sha1(path)

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
        api_meter.record("rapid:hit" if response.get("reuse") else "rapid:miss")
        if not response.get("reuse"):
            return False
        self._target_file_cache.clear()
        return True

    def try_rapid_upload_by_hash(
            self, save_path: str, target_name: str, algorithm: str,
            checksum: str, size: int,
            read_range_bytes_or_hash: Optional[Callable[[str], str]] = None,
    ) -> Optional[bool]:
        """无本地文件的秒传探测（abort 模式专用），只走 ``upload_file_init``。

        返回 True=命中 / False=确认未命中 / None=无法探测（缺少二次校验数据源
        或探测过程异常）。探测本身不产生真实上传；未命中时若 115 侧返回了可用
        的上传会话信息，尽力取消后再返回（没有残留就跳过）。
        """
        if str(algorithm or "").lower() != "sha1" or not P115_AVAILABLE or not self.client:
            return None
        actual = str(checksum or "").strip().upper()
        total = int(size or 0)
        if not actual or total <= 0:
            return None
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(save_path, create=True)
        if not lookup.checked or lookup.directory_id is None:
            return None
        # p115client：filesize >= 1MB 时 upload_file_init 会要求二次校验范围
        # （sign_check，形如 "start-end"）的 SHA1；没有数据源就无法零流量探测。
        if total >= 1024 * 1024 and read_range_bytes_or_hash is None:
            return None
        try:
            response = self._rate_limited_call(
                self.client.upload_file_init,
                filename=target_name,
                filesize=total,
                filesha1=actual,
                pid=int(lookup.directory_id or 0),
                read_range_bytes_or_hash=read_range_bytes_or_hash,
            )
            check_response(response)
        except Exception as error:  # noqa: BLE001 - 探测异常按无法判定处理，不误伤任务
            logger.debug(f"115 秒传探测异常（{target_name}）：{error}")
            return None
        if response.get("reuse"):
            self._target_file_cache.clear()
            return True
        self._cancel_probe_residue(response)
        return False

    def _cancel_probe_residue(self, response: Mapping) -> None:
        """探测未命中后清理 115 侧可能的上传会话；没有残留就跳过。"""
        data = (response or {}).get("data") or {}
        upload_id = data.get("upload_id") or data.get("UploadId")
        target = data.get("url") or data.get("target")
        if not (upload_id and target and P115OSS_AVAILABLE):
            return
        with contextlib.suppress(Exception):
            oss_multipart_upload_cancel(
                target, upload_id, **self._oss_proxy_kwargs()
            )

    def _upload_proxy_url(self) -> str:
        """上传代理地址（原始 URL）；未配置时返回空字符串。"""
        proxies = getattr(self._owner, "_upload_proxies", None)
        if isinstance(proxies, dict) and proxies:
            url = str(proxies.get("https") or proxies.get("http") or "").strip()
            if url:
                return url
        try:
            return str(
                getattr(self._owner, "_cross_transfer_upload_proxy", "") or ""
            ).strip()
        except Exception:
            return ""

    def _oss2_proxy_dict(self) -> Dict[str, str]:
        """oss2 数据面用的代理字典；未配置时返回空字典（直连）。"""
        url = self._upload_proxy_url()
        return {"http": url, "https": url} if url else {}

    @staticmethod
    def _oss2_token_expiring(
            expiration: Any,
            threshold_minutes: int = OSS2_TOKEN_REFRESH_THRESHOLD_MINUTES,
    ) -> bool:
        """STS token 是否在阈值内过期（或已过期）；解析失败时按快过期处理。"""
        try:
            from datetime import datetime, timezone

            if expiration is None:
                return True
            if isinstance(expiration, str):
                expiration = datetime.fromisoformat(
                    expiration.replace("Z", "+00:00")
                )
            now = datetime.now(timezone.utc)
            remaining = (expiration - now).total_seconds()
            return remaining < threshold_minutes * 60
        except Exception:
            return True

    @staticmethod
    def _is_oss2_token_error(error: BaseException) -> bool:
        """是否是 oss2 的 token 过期类错误（可刷新 token 后重传该片）。"""
        if not OSS2_AVAILABLE or OssServerError is None:
            return False
        if not isinstance(error, OssServerError):
            return False
        return str(getattr(error, "code", "") or "") in OSS2_TOKEN_EXPIRED_CODES

    def _oss2_get_token(self) -> Tuple[str, str, str, str, Any]:
        """拿 OSS STS 凭证；返回 (endpoint, key_id, key_secret, token, expiration)。"""
        token_resp = self._rate_limited_call(
            self.client.upload_gettoken,
            headers={"user-agent": OSS2_IOS_USER_AGENT},
        )
        check_response(token_resp)
        data = token_resp.get("data") or {}

        def _pick(*keys: str) -> Any:
            for key in keys:
                value = token_resp.get(key)
                if value:
                    return value
                value = data.get(key)
                if value:
                    return value
            return ""

        access_key_id = _pick("AccessKeyId")
        access_key_secret = _pick("AccessKeySecret")
        security_token = _pick("SecurityToken")
        expiration = _pick("Expiration")
        if not (access_key_id and access_key_secret and security_token):
            raise RuntimeError(f"115 OSS 凭证不完整：{token_resp}")
        return (
            OSS2_ENDPOINT,
            str(access_key_id),
            str(access_key_secret),
            str(security_token),
            expiration,
        )

    def _oss2_bucket(
            self,
            key_id: str,
            key_secret: str,
            token: str,
            bucket_name: str,
            proxies: Optional[Dict[str, str]] = None,
    ):
        """按 STS 凭证建一个 oss2 Bucket（服务端点固定为 OSS2_ENDPOINT）。"""
        auth = Oss2StsAuth(key_id, key_secret, token)
        return Oss2Bucket(
            auth, OSS2_ENDPOINT, bucket_name,
            proxies=proxies if proxies is not None else self._oss2_proxy_dict(),
        )

    def _upload_oss2_parts_concurrently(
            self,
            source: Path,
            bucket_holder: Dict[str, Any],
            bucket_name: str,
            object_name: str,
            upload_id: str,
            part_size: int,
            file_size: int,
            expiration: Any,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> list:
        """oss2 本地文件并发分片：每 worker 独立文件句柄读一片、PUT 一片。

        与远程并发引擎共用 ``_run_parts_concurrently``；单片 PUT 走
        ``_put_part_with_retry``（策略表重试 + 429 退避 + 起步 pacing）。
        token 过期时单 worker 加锁刷新并重建 bucket（写进 bucket_holder，
        调用方的 abort/complete 用的就是最新 bucket）后重传该片。
        成功返回按分片号排序的 parts（OssPartInfo 列表）；失败抛异常，
        由调用方 abort 掉该上传任务。
        """
        parts_total = -(-int(file_size) // int(part_size))
        workers = max(
            1,
            min(
                self._upload_concurrency(), P115_MAX_UPLOAD_CONCURRENCY,
                parts_total,
            ),
        )
        token_state = {"expiration": expiration}
        refresh_lock = Lock()
        progress_lock = Lock()
        uploaded = {"bytes": 0}

        def _rebuild_bucket() -> None:
            _, key_id, key_secret, token, new_expiration = self._oss2_get_token()
            token_state["expiration"] = new_expiration
            bucket_holder["bucket"] = self._oss2_bucket(
                key_id, key_secret, token, bucket_name
            )

        # 起步时 token 已快过期：先刷新一次再开工，避免每个 worker 各刷一次。
        if self._oss2_token_expiring(token_state["expiration"]):
            logger.info("115 OSS Token 即将过期，正在刷新...")
            _rebuild_bucket()
            logger.info("115 OSS Token 刷新成功")

        def put_part(part_number: int):
            start = (part_number - 1) * part_size
            length = min(part_size, file_size - start)
            attempt_bucket: Dict[str, Any] = {"bucket": None}

            def _put_once():
                # 本次尝试用的 bucket 只读一次：刷新与否按“是不是同一个
                # bucket 对象”判断，不依赖 expiration 时间戳（token 报错
                # 时时间戳未必已过期，仍需刷新）。
                bucket = bucket_holder["bucket"]
                attempt_bucket["bucket"] = bucket
                with source.open("rb") as handle:
                    handle.seek(start)
                    data = handle.read(length)
                if len(data) != length:
                    raise IOError(
                        f"115 本地文件第 {part_number} 分片读取不完整："
                        f"{len(data)}/{length}"
                    )
                return bucket.upload_part(
                    object_name, upload_id, part_number,
                    data=OssSizedFileAdapter(io.BytesIO(data), length),
                    # 流式进度：分片传的时候边传边报，百分比连续走，
                    # 不再等整片传完才跳 10MB。单调保护在外层回调上。
                    progress_callback=_oss2_part_progress_callback(
                        progress_callback, start, file_size
                    ),
                )

            def _on_retry(error: BaseException, _attempt: int) -> bool:
                if self._is_oss2_token_error(error):
                    with refresh_lock:
                        # 别的 worker 可能刚刷新过：holder 已换新 bucket
                        # 就不再重复刷。
                        if bucket_holder["bucket"] is attempt_bucket["bucket"]:
                            logger.warning(
                                "115 上传分片遇到 Token 过期，刷新后重传该片"
                            )
                            _rebuild_bucket()
                return True

            result = self._put_part_with_retry(_put_once, on_retry=_on_retry)
            api_meter.record("oss2:part", bytes_=length)
            with progress_lock:
                uploaded["bytes"] += length
                done = uploaded["bytes"]
            if progress_callback:
                progress_callback(min(done, file_size), file_size)
            return OssPartInfo(part_number, result.etag)

        parts = self._run_parts_concurrently(
            range(1, parts_total + 1), put_part, workers=workers
        )
        parts.sort(key=lambda part: part.part_number)
        return parts

    def _upload_via_oss2(
            self,
            source: Path,
            directory_id: int,
            upload_name: str,
            checksum: str,
            file_size: int,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        """oss2 自管分片上传一次（不含整文件重传，由调用方循环）。

        p115disk 同款流程：upload_file_init（3 次重试 + sig invalid 处理）
        → 秒传 reuse → 拿 OSS token → oss2 分片上传（每片前检查 token、
        单片失败只重传该片）→ complete 回调。成功（含秒传）返回 True，
        失败抛异常（由调用方决定是否整文件重传）。
        """
        self._upload_stat().update({"backend": "oss2"})
        # -- Step 1: 初始化上传（带重试）--
        def read_range_hash(range_str: str) -> str:
            start, end = (int(part) for part in range_str.split("-", 1))
            with source.open("rb") as handle:
                handle.seek(start)
                chunk = handle.read(end - start + 1)
            digest = hashlib.sha1()
            digest.update(chunk)
            return digest.hexdigest().upper()

        init_resp = None
        sig_invalid_handled = False
        for init_attempt in range(OSS2_INIT_MAX_RETRIES):
            init_resp = None
            try:
                init_resp = self._rate_limited_call(
                    self.client.upload_file_init,
                    filename=upload_name,
                    filesize=file_size,
                    filesha1=checksum,
                    pid=int(directory_id or 0),
                    read_range_bytes_or_hash=read_range_hash,
                )
                check_response(init_resp)
                break
            except Exception as error:
                if (
                        not sig_invalid_handled
                        and isinstance(init_resp, dict)
                        and init_resp.get("statusmsg") == "sig invalid"
                ):
                    # p115disk 同款：清掉缓存的 userkey 后重试一次。
                    try:
                        from p115client.const import _CACHE_DIR

                        userkey_points = _CACHE_DIR / "userkey_stable_points.json"
                        if userkey_points.exists():
                            userkey_points.unlink(missing_ok=True)
                    except Exception:
                        logger.debug("清理 userkey 缓存失败，继续重试", exc_info=True)
                    sig_invalid_handled = True
                    continue
                if init_attempt < OSS2_INIT_MAX_RETRIES - 1:
                    # 429 用 5 秒起步的限流退避，其余按策略表指数退避 + 抖动。
                    rate_limited = self._note_rate_limited(error)
                    delay = retry_delay(
                        "init", init_attempt + 1, rate_limited=rate_limited
                    )
                    logger.warning(
                        f"115 上传初始化失败，第 {init_attempt + 1}/"
                        f"{OSS2_INIT_MAX_RETRIES} 次重试：{error}"
                    )
                    time.sleep(delay)
                else:
                    logger.error(f"115 上传初始化重试用尽：{error}")
                    raise
        if init_resp is None:
            raise RuntimeError("115 上传初始化失败：无响应")
        if not init_resp.get("state"):
            raise RuntimeError(
                f"115 上传初始化失败：{init_resp.get('error') or init_resp}"
            )
        # 秒传命中：不用传，直接成功。
        if init_resp.get("reuse"):
            api_meter.record("rapid:hit")
            self._upload_stat()["rapid"] = True
            logger.info(f"115 秒传成功：{upload_name}")
            return True
        api_meter.record("rapid:miss")

        # 上传会话信息：顶层与 data 里都可能放，两边都看。
        data = init_resp.get("data") or {}
        bucket_name = init_resp.get("bucket") or data.get("bucket")
        object_name = init_resp.get("object") or data.get("object")
        callback_info = init_resp.get("callback") or data.get("callback") or {}
        if not (bucket_name and object_name and callback_info):
            raise RuntimeError(f"115 上传信息不完整：{init_resp}")

        # -- Step 2: OSS token --
        _endpoint, key_id, key_secret, token, expiration = self._oss2_get_token()
        bucket_holder = {
            "bucket": self._oss2_bucket(key_id, key_secret, token, bucket_name)
        }
        part_size = oss2_determine_part_size(
            file_size, preferred_size=OSS2_UPLOAD_PART_SIZE
        )
        logger.info(
            f"115 开始分片上传（oss2）：{source.name} -> {upload_name}，"
            f"分片 {part_size // 1024 // 1024}MB"
        )
        upload_id = bucket_holder["bucket"].init_multipart_upload(
            object_name, params={"encoding-type": "url", "sequential": ""}
        ).upload_id
        parts_total = -(-int(file_size) // int(part_size))
        # 流式进度与分片内重传共用一个单调保护：并发分片各 worker
        # 交错上报、重传时 consumed 回落，都只放行递增的值，卡片
        # 百分比不来回跳；传完永远放行。1.18.3。
        monotonic_callback = _monotonic_progress_callback(progress_callback)
        # 并发度 > 1 且多于一片时走并发分片（与远程直传共用执行器）；
        # 否则走原来的顺序循环（单片文件、并发度 0/1 时的行为不变）。
        concurrency = self._upload_concurrency()
        parts = []
        try:
            if concurrency > 1 and parts_total > 1:
                logger.info(
                    f"115 oss2 分片并发上传：{concurrency} 路，"
                    f"共 {parts_total} 片"
                )
                parts = self._upload_oss2_parts_concurrently(
                    source, bucket_holder, bucket_name, object_name, upload_id,
                    part_size, file_size, expiration,
                    progress_callback=monotonic_callback,
                )
                if monotonic_callback:
                    monotonic_callback(file_size, file_size)
            else:
                with source.open("rb") as fileobj:
                    part_number = 1
                    offset = 0
                    while offset < file_size:
                        # token 快过期就先刷新，再传这一片。
                        if self._oss2_token_expiring(expiration):
                            logger.info("115 OSS Token 即将过期，正在刷新...")
                            _endpoint, key_id, key_secret, token, expiration = (
                                self._oss2_get_token()
                            )
                            bucket_holder["bucket"] = self._oss2_bucket(
                                key_id, key_secret, token, bucket_name
                            )
                            logger.info("115 OSS Token 刷新成功")
                        num_to_upload = min(part_size, file_size - offset)
                        # 单片失败只重传该片；token 过期类错误先刷新 token。
                        last_part_error: Optional[Exception] = None
                        for part_attempt in range(OSS2_PART_MAX_RETRIES):
                            try:
                                self._data_rate_limiter().acquire()
                                result = bucket_holder["bucket"].upload_part(
                                    object_name,
                                    upload_id,
                                    part_number,
                                    data=OssSizedFileAdapter(fileobj, num_to_upload),
                                    # 流式进度：分片传的时候边传边报，
                                    # 百分比连续走，不再等整片传完才跳 10MB。
                                    progress_callback=(
                                        _oss2_part_progress_callback(
                                            monotonic_callback, offset, file_size
                                        )
                                    ),
                                )
                                parts.append(OssPartInfo(part_number, result.etag))
                                api_meter.record("oss2:part", bytes_=num_to_upload)
                                last_part_error = None
                                break
                            except Exception as error:
                                last_part_error = error
                                if part_attempt >= OSS2_PART_MAX_RETRIES - 1:
                                    break
                                rate_limited = self._note_rate_limited(error)
                                if self._is_oss2_token_error(error):
                                    logger.warning(
                                        "115 上传分片遇到 Token 过期，刷新后重传该片"
                                    )
                                    _endpoint, key_id, key_secret, token, expiration = (
                                        self._oss2_get_token()
                                    )
                                    bucket_holder["bucket"] = self._oss2_bucket(
                                        key_id, key_secret, token, bucket_name
                                    )
                                else:
                                    logger.warning(
                                        f"115 上传分片失败，重传该片 "
                                        f"({part_attempt + 1}/{OSS2_PART_MAX_RETRIES})："
                                        f"{error}"
                                    )
                                # 429 用 5 秒起步的限流退避，其余按策略表
                                # （旧行为是不睡觉直接重传，v1.18.0 起统一
                                # 为 1 秒起步的指数退避 + 抖动）。
                                time.sleep(
                                    retry_delay(
                                        "part", part_attempt + 1,
                                        rate_limited=rate_limited,
                                    )
                                )
                                fileobj.seek(offset)
                        if last_part_error is not None:
                            raise last_part_error
                        offset += num_to_upload
                        part_number += 1
                        if monotonic_callback:
                            monotonic_callback(offset, file_size)
        except Exception:
            with contextlib.suppress(Exception):
                bucket_holder["bucket"].abort_multipart_upload(object_name, upload_id)
            raise

        self._upload_stat()["parts"] = len(parts)
        # -- Step 3: 完成上传并回调 115 --
        headers = {
            "X-oss-callback": oss2_b64encode(
                str(callback_info.get("callback") or "")
            ),
            "x-oss-callback-var": oss2_b64encode(
                str(callback_info.get("callback_var") or "")
            ),
            "x-oss-forbid-overwrite": "false",
        }
        result = bucket_holder["bucket"].complete_multipart_upload(
            object_name, upload_id, parts, headers=headers
        )
        if result.status != 200:
            raise RuntimeError(f"115 上传完成失败，状态码：{result.status}")
        logger.info(f"115 本地文件上传完成（oss2）：{source.name} -> {upload_name}")
        return True

    def _upload_via_p115oss(
            self,
            source: Path,
            directory_id: int,
            upload_name: str,
            checksum: str,
            file_size: int,
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """p115oss 旧路子上传一次（1.12.9 之前的做法）。

        oss2 路子失败时的回落通道：走 client.upload_file，数据面
        session 带代理（见 _oss_proxy_kwargs）。失败抛异常。
        """
        self._upload_stat().update({"backend": "p115oss", "parts": -1})
        part_size = upload_part_size(file_size)
        # p115oss.upload_file 会把 request_kwargs 透传给 OSS 分片 PUT，
        # 在这里传 session=<ProxyManager> 才能真正让上传数据走代理。
        upload_kwargs = dict(self._oss_proxy_kwargs(), max_retries=0)
        if progress_callback:
            with source.open("rb") as file:
                response = self._rate_limited_call(
                    self.client.upload_file,
                    _ProgressReader(file, file_size, progress_callback),
                    pid=int(directory_id or 0),
                    filename=upload_name,
                    filesha1=checksum,
                    filesize=file_size,
                    partsize=part_size,
                    **upload_kwargs,
                )
        else:
            response = self._rate_limited_call(
                self.client.upload_file,
                source,
                pid=int(directory_id or 0),
                filename=upload_name,
                filesha1=checksum,
                filesize=file_size,
                partsize=part_size,
                **upload_kwargs,
            )
        check_response(response)
        # p115oss 内部分片在外部库里看不到，只按整文件记一次。
        api_meter.record("p115oss:file", bytes_=file_size)

    def upload_file(
            self,
            local_path: str,
            save_path: str,
            target_name: str = "",
            file_sha1: str = "",
            progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> bool:
        if not P115_AVAILABLE or not self.client:
            self._last_upload_error = "115 本地上传不可用：客户端未初始化"
            logger.error("115 本地上传不可用：客户端未初始化")
            return False
        source = Path(str(local_path or ""))
        if not source.is_file():
            self._last_upload_error = f"115 本地上传文件不存在：{source}"
            logger.error(f"115 本地上传文件不存在：{source}")
            return False
        files = self._owner._get_component(P115FileService)
        lookup = P115DirectoryReader(files).resolve_directory(
            save_path, create=True
        )
        if not lookup.checked or lookup.directory_id is None:
            self._last_upload_error = f"115 本地上传目录不可用：{save_path}"
            logger.error(f"115 本地上传目录不可用：{save_path}")
            return False
        upload_name = str(target_name or source.name).strip()
        checksum = str(file_sha1 or "").strip().upper() or self._file_sha1(source)
        # p115oss 分片上传没有重试：凡人修仙传 S01E200 两次失败在不同分块
        # （服务端收到的分块 md5 与客户端算出的不一致，属传输抖动），一次
        # 失败就杀死整个任务。1.12.9 起本地文件改走 oss2 自管分片（p115disk
        # 同款）：单片失败只重传该片、token 快过期先刷新；整文件重传
        # （最多 3 次、按统一重试策略表指数退避 2 秒起 + 抖动，429 时
        # 5 秒起，与 MP 自带 u115 存储同一口径）只作外层兜底。SHA1 只算一次，不随重试重复读整份文件。
        # 1.12.13：oss2 路子在线上全量失败（原因未明，代理已排除——
        # cd2 走同一代理正常），先回落旧路子（p115oss）保证上传能走通，
        # 同时把 oss2 的原错记下来以便定位。
        # 上传熔断器（Phase 3b-10）：连续失败达到阈值后打开，冷却期内
        # 新上传直接跳过（只放行秒传探针）；秒传探针不走 upload_file，
        # 不受熔断影响。预检失败（客户端/文件/目录）不计入熔断。
        breaker = self._upload_breaker()
        if not breaker.can_upload():
            remaining = breaker.cooldown_remaining()
            self._last_upload_error = (
                "115 上传熔断器开启"
                f"（连续失败 {self._breaker_threshold()} 次后冷却中，"
                f"剩余约 {remaining:.0f} 秒），本次上传已跳过，只放行秒传探针"
            )
            logger.warning(f"115 本地上传已跳过：{self._last_upload_error}")
            return False
        last_error: Optional[Exception] = None
        started_at = time.monotonic()
        file_size = 0
        for attempt in range(1, _UPLOAD_MAX_ATTEMPTS + 1):
            # 本次尝试的后端/秒传/分片数/429 数：内层方法边跑边填，终态日志只读
            # 最后一次尝试的结果（重试会覆盖）。
            self._upload_stats = {
                "backend": "", "rapid": False, "parts": 0, "rate_limited": 0,
            }
            try:
                file_size = source.stat().st_size
                if OSS2_AVAILABLE:
                    try:
                        self._upload_via_oss2(
                            source, int(lookup.directory_id or 0), upload_name,
                            checksum, file_size, progress_callback,
                        )
                    except Exception as oss2_error:
                        if not P115OSS_AVAILABLE:
                            raise
                        # 先把 oss2 原错的描述取出来：回落失败时要报它，
                        # 但不能直接重抛原异常对象——在 except 块里重抛
                        # 会把 __context__ 链到 p115oss 的错上，
                        # describe_upload_error 会沿链走到 p115oss。
                        oss2_detail = describe_upload_error(oss2_error)
                        logger.warning(
                            f"115 oss2 分片上传失败，回落 p115oss 旧路子："
                            f"{source.name}，{oss2_detail}"
                        )
                        try:
                            self._upload_via_p115oss(
                                source, int(lookup.directory_id or 0), upload_name,
                                checksum, file_size, progress_callback,
                            )
                        except Exception:
                            # 回落也失败：报 oss2 的原错（主路子的原因
                            # 更能定位问题），p115oss 的错只记日志。
                            # 抛一个新异常带 oss2 的描述，避免异常链
                            # 把原因带偏到 p115oss。
                            logger.warning(
                                f"115 p115oss 回落也失败，仍按 oss2 原错处理："
                                f"{source.name}"
                            )
                            raise RuntimeError(
                                f"115 oss2 分片上传失败：{oss2_detail}"
                            ) from None
                else:
                    # oss2 缺失时回退旧路子（p115oss）。
                    self._upload_via_p115oss(
                        source, int(lookup.directory_id or 0), upload_name,
                        checksum, file_size, progress_callback,
                    )
                self._target_file_cache.clear()
                if progress_callback:
                    progress_callback(file_size, file_size)
                logger.info(f"115 本地文件上传完成：{source.name} -> {save_path}/{upload_name}")
                self._last_upload_error = ""
                api_meter.record("upload:success", bytes_=file_size)
                breaker.record_success()
                self._log_upload_telemetry(
                    source.name, file_size, time.monotonic() - started_at, ok=True
                )
                return True
            except Exception as error:
                last_error = error
                if attempt >= _UPLOAD_MAX_ATTEMPTS:
                    break
                # 429 用 5 秒起步的限流退避，其余按策略表指数退避 + 抖动。
                rate_limited = self._note_rate_limited(error)
                delay = retry_delay("file", attempt, rate_limited=rate_limited)
                logger.warning(
                    f"115 本地文件上传第 {attempt} 次失败，"
                    f"{delay:.1f} 秒后重传："
                    f"{source.name}，{describe_upload_error(error)}"
                )
                time.sleep(delay)
        # p115oss 会把上传失败包成 MultipartUploadAbort（消息是上传
        # 参数字典，含 user_key），真报错在 __cause__ 里；这里拆包
        # 取原错并脱敏，日志行与 last_error 用同一句，下次失败时
        # 历史记录里直接就是 115 的原错。
        detail = describe_upload_error(
            last_error if last_error is not None else IOError("115 本地上传失败")
        )
        self._last_upload_error = (
            f"115 本地文件上传失败：{source.name}，{detail}"
        )
        api_meter.record("upload:failure", bytes_=file_size)
        # 终态失败记一次熔断：连续失败达到阈值后打开，冷却期内新上传跳过。
        breaker.record_failure()
        self._log_upload_telemetry(
            source.name, file_size, time.monotonic() - started_at, ok=False
        )
        logger.error(f"115 本地文件上传失败：{source.name}，{detail}")
        return False

    def _log_upload_telemetry(
            self, name: str, file_size: int, seconds: float, *, ok: bool
    ) -> None:
        """终态计量行：一次上传花了多久、走哪个后端、秒传与否、几片、
        429 出现几次，以及进程累计快照（风控面验收用，见 core/telemetry.py）。"""
        stats = getattr(self, "_upload_stats", None) or {}
        backend = stats.get("backend") or "未知"
        rapid = "是" if stats.get("rapid") else "否"
        parts = stats.get("parts")
        parts_text = f"{parts}片" if isinstance(parts, int) and parts >= 0 else "未知"
        rate_limited = int(stats.get("rate_limited") or 0)
        rate_text = f" 429={rate_limited}次" if rate_limited else ""
        logger.info(
            f"115 上传计量：{name} {file_size / 1048576:.2f}MB "
            f"{seconds:.1f}s {'成功' if ok else '失败'} 后端={backend} "
            f"秒传={rapid} 分片={parts_text}{rate_text} "
            f"累计={format_snapshot(api_meter.snapshot())}"
        )

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
                api_meter.record("rapid:hit")
                self._target_file_cache.clear()
                logger.info(
                    f"115 SHA1 秒传完成：{target_name} -> "
                    f"{save_path}/{target_name}"
                )
                # 秒传探针是熔断的放行项：115 正常应答本身就是一次健康信号。
                self._upload_breaker().record_success()
                return "rapid"
            api_meter.record("rapid:miss")
            # 熔断器（Phase 3b-10）：秒传探针已放行，从这里起是真实上传，
            # 熔断打开时直接返回 False（调用方回退本地缓存下载 + upload_file，
            # 那边同样被熔断拦住，不会再起新上传）。
            breaker = self._upload_breaker()
            if not breaker.can_upload():
                logger.warning(
                    "115 Range 直传已跳过：上传熔断器开启"
                    f"（冷却剩余约 {breaker.cooldown_remaining():.0f} 秒），"
                    "只放行秒传探针"
                )
                return False

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
                    breaker.record_success()
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
                **dict(self._oss_proxy_kwargs(), max_retries=0),
            )
            check_response(response)
            self._target_file_cache.clear()
            if progress_callback:
                progress_callback(file_size, file_size)
            logger.info(
                f"115 Range 直传完成：{target_name} -> {save_path}/{target_name}"
            )
            breaker.record_success()
            return "remote"
        except InterruptedError:
            raise
        except Exception as error:
            # 源盘侧失败（读源盘/链失效）不是 115 的错，不计熔断；
            # 只有 429/限流才记一次。
            if is_rate_limit_error(error):
                self._note_rate_limited(error)
                self._upload_breaker().record_failure()
            logger.warning(f"115 Range 直传失败，将回退本地缓存：{error}")
            return False
        finally:
            reader.close()


    def _upload_concurrency(self) -> int:
        """115 分片并发上传的并发度；``0`` 表示关闭（回退顺序上传）。

        直传与本地上传共用：远程 Range 直传（_upload_parts_concurrently）
        与 oss2 本地分片（_upload_oss2_parts_concurrently）都按它取
        worker 数。默认 2（计划 §14：先从 2 起步实测，不要直接拉满）。
        """
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
        proxy_kwargs = self._oss_proxy_kwargs()
        upload_id = oss_multipart_upload_init(target, **proxy_kwargs)
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

                def _put_once() -> dict:
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
                        **proxy_kwargs,
                    )

                result = self._put_part_with_retry(
                    _put_once, stop_requested=stop_requested
                )
                api_meter.record("p115oss:part", bytes_=length)
                return result
            finally:
                pool.put(reader)

        try:
            parts: list = self._run_parts_concurrently(
                range(1, parts_total + 1), put_part, workers=workers
            )
            if len(parts) != parts_total:
                raise IOError(
                    f"115 分块上传分片数不符：{len(parts)}/{parts_total}"
                )
            parts.sort(key=lambda part: int(part["PartNumber"]))
            response = oss_multipart_upload_complete(
                target, upload_id, parts, callback, **proxy_kwargs
            )
            if P115_AVAILABLE:
                check_response(response)
        except BaseException:
            with contextlib.suppress(Exception):
                oss_multipart_upload_cancel(target, upload_id, **proxy_kwargs)
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
                # 源盘侧重试也走统一策略表（0.5 秒起步，指数 + 抖动）。
                time.sleep(retry_delay("range", attempt + 1))
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
