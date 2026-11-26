"""CloudSubscribe 对 MoviePilot 平台缓存工具的轻量封装。"""

import copy
import threading
import json
from contextlib import nullcontext
from dataclasses import fields, is_dataclass
from hashlib import sha256
from importlib import import_module
from typing import Any, Callable, Optional, Sequence

from app.core.cache import TTLCache

try:  # 单测环境可能只注入了 app.core.cache
    from app.log import logger
except Exception:  # noqa: BLE001 - 缺少宿主日志时退回标准库
    import logging

    logger = logging.getLogger(__name__)


def _cache_identity(value: Any) -> str:
    """提取稳定身份，避免不同账号或客户端实例共享缓存区。"""
    if value in (None, ""):
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).strip()
    for name in (
            "cache_namespace", "user_id", "drive_id", "session_key",
            "token", "cookies", "cookie", "_email", "email", "username",
    ):
        try:
            identity = str(getattr(value, name, "") or "").strip()
        except Exception:
            continue
        if identity:
            return identity
    return f"instance:{id(value)}"


def normalize_platform_cache_key(value: Any) -> str:
    """将复合键编码为平台缓存后端兼容的稳定字符串。"""
    if isinstance(value, str):
        return value
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str,
        separators=(",", ":"),
    )
    return sha256(payload.encode("utf-8")).hexdigest()


_CACHE_TAG = "__csx__"
# 缓存结构版本：编码格式变更时递增，旧区自动失效。
_CACHE_SCHEMA = "v2"

# 缓存健康度计数：线上曾因旧类对象进 Redis 导致 169 条 pickle 报错而无人察觉，
# 这里把「编码失败 / 解码失败 / 命中」记下来，供自检接口暴露。
_STATS_LOCK = threading.Lock()
_CACHE_STATS: dict = {
    "encode_ok": 0,
    "encode_skip": 0,
    "decode_fail": 0,
    "hit": 0,
    "miss": 0,
}


def _bump(name: str, amount: int = 1) -> None:
    with _STATS_LOCK:
        _CACHE_STATS[name] = _CACHE_STATS.get(name, 0) + amount


def cache_stats() -> dict:
    """返回当前进程的缓存健康度快照。"""
    with _STATS_LOCK:
        snapshot = dict(_CACHE_STATS)
    total_read = snapshot.get("hit", 0) + snapshot.get("miss", 0)
    snapshot["hit_rate"] = (
        round(snapshot.get("hit", 0) * 100.0 / total_read, 1) if total_read else 0.0
    )
    return snapshot

# 只允许这些插件内的数据类进入缓存；其余对象（含提供方原始响应）一律跳过写入，
# 避免 MoviePilot 的 Redis 后端在插件热重载后用旧类对象 pickle 失败。
_CACHE_DOMAIN_TYPES = (
    "CloudFile",
    "DirectoryLookup",
    "DirectoryListing",
    "RapidUploadResult",
    "ShareLinkStatus",
)

# 丢掉的字段：提供方原始响应体积大且不可序列化，关键标识已在 playback_values 中。
_CACHE_DROPPED_FIELDS = frozenset({"native"})


def _domain_type(name: str) -> Any:
    """按类名取回插件数据类，延迟导入避免循环依赖。"""
    module = import_module("..core.cloud", __package__)
    return getattr(module, name)


def _encode_cache_value(value: Any) -> Any:
    """把缓存值转成 JSON 兼容结构；不可缓存的对象抛 TypeError。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        name = type(value).__name__
        if name not in _CACHE_DOMAIN_TYPES:
            raise TypeError(f"不支持缓存的类型：{name}")
        payload: dict[str, Any] = {}
        for item in fields(value):
            if item.name in _CACHE_DROPPED_FIELDS:
                continue
            payload[item.name] = _encode_cache_value(getattr(value, item.name))
        return {_CACHE_TAG: name, "data": payload}
    if isinstance(value, tuple):
        return {_CACHE_TAG: "tuple", "items": [_encode_cache_value(v) for v in value]}
    if isinstance(value, list):
        return {_CACHE_TAG: "list", "items": [_encode_cache_value(v) for v in value]}
    if isinstance(value, dict):
        return {
            _CACHE_TAG: "dict",
            "items": [
                [_encode_cache_value(k), _encode_cache_value(v)]
                for k, v in value.items()
            ],
        }
    raise TypeError(f"不支持缓存的类型：{type(value).__name__}")


def _decode_cache_value(value: Any) -> Any:
    """把缓存中读到的 JSON 结构还原成插件对象；失败时上抛由调用方当作未命中。"""
    if not isinstance(value, dict) or _CACHE_TAG not in value:
        return value
    tag = value.get(_CACHE_TAG)
    if tag == "tuple":
        return tuple(_decode_cache_value(item) for item in value.get("items") or [])
    if tag == "list":
        return [_decode_cache_value(item) for item in value.get("items") or []]
    if tag == "dict":
        return {
            _decode_cache_value(pair[0]): _decode_cache_value(pair[1])
            for pair in value.get("items") or []
        }
    if tag in _CACHE_DOMAIN_TYPES:
        data = {
            key: _decode_cache_value(item)
            for key, item in (value.get("data") or {}).items()
        }
        return _domain_type(str(tag))(**data)
    raise ValueError(f"未知的缓存类型标记：{tag}")


class _PlatformTTLCache(TTLCache):
    """确保所有键在进入 MoviePilot 后端前都是稳定字符串。"""

    @staticmethod
    def _key(value: Any) -> str:
        return normalize_platform_cache_key(value)

    @staticmethod
    def _read(raw: Any) -> Any:
        """解码读出的值；无法识别（例如旧版 pickle 对象）时当作未命中。"""
        if raw is None:
            _bump("miss")
            return None
        if isinstance(raw, (str, int, float, bool)):
            _bump("hit")
            return raw
        if not isinstance(raw, (dict, list, tuple)):
            _bump("decode_fail")
            return None
        try:
            value = _decode_cache_value(raw)
            _bump("hit")
            return value
        except Exception as error:  # noqa: BLE001 - 旧数据或类型缺失都按未命中处理
            _bump("decode_fail")
            logger.debug(f"缓存值解码失败，按未命中处理：{error}")
            return None

    @staticmethod
    def _write_value(value: Any) -> tuple[Any, bool]:
        """返回可直接落库的值与是否允许写入。"""
        try:
            payload = _encode_cache_value(value)
            _bump("encode_ok")
            return payload, True
        except Exception as error:  # noqa: BLE001 - 不可缓存对象直接跳过写入
            _bump("encode_skip")
            logger.debug(f"缓存值不可序列化，跳过写入：{error}")
            return None, False

    def __getitem__(self, key: Any) -> Any:
        cached = self._read(super().get(self._key(key)))
        if cached is None:
            raise KeyError(key)
        return cached

    def __setitem__(self, key: Any, value: Any) -> None:
        payload, ok = self._write_value(value)
        if ok:
            super().__setitem__(self._key(key), payload)

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(self._key(key))

    def __contains__(self, key: Any) -> bool:
        return super().__contains__(self._key(key))

    def get(self, key: Any, **kwargs: Any) -> Any:
        return self._read(super().get(self._key(key), **kwargs))

    def set(self, key: Any, value: Any, **kwargs: Any) -> None:
        payload, ok = self._write_value(value)
        if ok:
            super().set(self._key(key), payload, **kwargs)

    def delete(self, key: Any, **kwargs: Any) -> None:
        super().delete(self._key(key), **kwargs)

    def exists(self, key: Any, **kwargs: Any) -> bool:
        return super().exists(self._key(key), **kwargs)

    def pop(self, key: Any, default: Any = None, **kwargs: Any) -> Any:
        try:
            return self._read(super().pop(self._key(key), **kwargs))
        except KeyError:
            return default

    def setdefault(self, key: Any, default: Any = None, **kwargs: Any) -> Any:
        cached = self.get(key, **kwargs)
        if cached is not None:
            return cached
        self.set(key, default, **kwargs)
        return default

    def update(self, other: Any, **kwargs: Any) -> None:
        for key, value in dict(other or {}).items():
            self.set(key, value)



def create_platform_ttl_cache(
        namespace: str,
        identity: Any = "",
        *,
        maxsize: int,
        ttl: int,
) -> TTLCache:
    """创建按业务命名、按客户端身份隔离的 MoviePilot TTL 缓存。"""
    identity_text = _cache_identity(identity)
    suffix = sha256(identity_text.encode("utf-8")).hexdigest()[:16]
    return _PlatformTTLCache(
        # schema 后缀隔离旧版 pickle 数据，格式变更时递增即可整区换代。
        region=f"cloudsubscribe:{str(namespace).strip(':')}:{suffix}:{_CACHE_SCHEMA}",
        maxsize=max(1, int(maxsize or 1)),
        ttl=max(1, int(ttl or 1)),
    )


def cached_resource_call(
        cache: TTLCache,
        key: Any,
        loader: Callable[[], Any],
        *,
        locks: Sequence[Any],
        access_lock: Optional[Any] = None,
        force_refresh: bool = False,
        on_hit: Optional[Callable[[Any], None]] = None,
        on_wait_hit: Optional[Callable[[Any], None]] = None,
) -> Any:
    """双重检查缓存并串行加载同一资源，失败结果不写入缓存。"""
    if not locks:
        raise ValueError("资源缓存至少需要一个分片锁")

    def read_cached() -> Any:
        if force_refresh:
            return None
        with access_lock if access_lock is not None else nullcontext():
            cached = cache.get(key)
        return copy.deepcopy(cached) if cached is not None else None

    cached = read_cached()
    if cached is not None:
        if on_hit:
            on_hit(cached)
        return cached

    normalized_key = normalize_platform_cache_key(key)
    cache_lock = locks[hash(normalized_key) % len(locks)]
    with cache_lock:
        cached = read_cached()
        if cached is not None:
            if on_wait_hit:
                on_wait_hit(cached)
            return cached

        value = loader()
        if value is not None:
            with access_lock if access_lock is not None else nullcontext():
                cache.set(key, copy.deepcopy(value))
        return value
