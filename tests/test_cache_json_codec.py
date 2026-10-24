"""平台缓存 JSON 编解码回归测试（1.7.28）。

回归背景：``_PlatformTTLCache`` 直接把插件数据类塞进 MoviePilot 的 TTL 缓存，
Redis 后端用 pickle 落盘。插件热重载后，旧模块里造出来的 ``CloudFile`` /
``ShareLinkStatus`` 实例与新模块的类对象不是同一个对象，pickle 抛
``Can't pickle <class ...>: it's not the same object as ...``，线上
``cloudsubscribex.log`` 10 小时内刷了 119 条该 ERROR，目录缓存整体失效。

修法：缓存读写统一按**类名**做 JSON 编解码，不再依赖 pickle 的类身份。
用例：
1. ``CloudFile`` 元组往返后字段等价、``native`` 被丢弃、``playback_values`` 保留；
2. 热重载场景：用同名但不同类对象构造的值也能正常缓存（不再依赖类身份）；
3. 不可缓存对象写入被静默跳过，读回按未命中（不抛异常）；
4. 旧版 pickle 数据读回按未命中处理；
5. 缓存区带上结构版本后缀。
"""

import pickle
import unittest
from dataclasses import dataclass
from typing import Any, Dict

from plugin_env import install_app_mocks, load_module, register_module

install_app_mocks()

# ---- 模拟 MoviePilot Redis 后端：写入即 pickle.dumps ----
_STORES: Dict[str, Dict[str, bytes]] = {}


class PicklingTTLCache:
    """最小复刻 ``app.core.cache.TTLCache``：按 region 隔离，值走 pickle。"""

    def __init__(self, region: str = "DEFAULT", maxsize: int = 1024, ttl: int = 60):
        self.region = region
        self.maxsize = maxsize
        self.ttl = ttl
        self._store = _STORES.setdefault(region, {})

    def set(self, key: str, value: Any, **kwargs) -> None:
        self._store[key] = pickle.dumps(value)

    def get(self, key: str, **kwargs) -> Any:
        raw = self._store.get(key)
        return None if raw is None else pickle.loads(raw)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def exists(self, key: str, **kwargs) -> bool:
        return key in self._store

    def delete(self, key: str, **kwargs) -> None:
        self._store.pop(key, None)

    def pop(self, key: str, **kwargs) -> Any:
        return pickle.loads(self._store.pop(key))

    def items(self):
        return [(key, pickle.loads(value)) for key, value in self._store.items()]

    def clear(self) -> None:
        self._store.clear()


register_module("app.core.cache", TTLCache=PicklingTTLCache)

cloud_module = load_module("cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py")
cache_module = load_module("cloudsubscribex.utils.cache", "plugins.v2/cloudsubscribex/utils/cache.py")
CloudFile = cloud_module.CloudFile
DirectoryListing = cloud_module.DirectoryListing
ShareLinkStatus = cloud_module.ShareLinkStatus


class CacheCodecTest(unittest.TestCase):
    def setUp(self):
        _STORES.clear()
        self.cache = cache_module.create_platform_ttl_cache(
            "drive:directories", "account-a", maxsize=16, ttl=60,
        )

    def test_cloudfile_tuple_roundtrip_drops_native(self):
        file_item = CloudFile(
            id="f1", name="第01集.mkv", is_directory=False, size=1024,
            sha1="sha", md5="md5", playback_values={"pickcode": "abc"},
            native={"raw": "provider payload"},
        )
        self.cache.set("dir-1", (file_item,))
        cached = self.cache.get("dir-1")
        self.assertIsInstance(cached, tuple)
        self.assertEqual(cached[0].id, "f1")
        self.assertEqual(cached[0].name, "第01集.mkv")
        self.assertEqual(cached[0].size, 1024)
        self.assertEqual(dict(cached[0].playback_values), {"pickcode": "abc"})
        # 原始响应体积大且不可序列化，缓存里不再保留
        self.assertIsNone(cached[0].native)

    def test_share_link_status_and_listing_roundtrip(self):
        status = ShareLinkStatus(is_valid=True, file_count=3, share_info={"share_id": "s1"})
        self.cache.set("status", (status, DirectoryListing(checked=True, files=())))
        cached_status, cached_listing = self.cache.get("status")
        self.assertTrue(cached_status.is_valid)
        self.assertEqual(cached_status.file_count, 3)
        self.assertEqual(dict(cached_status.share_info), {"share_id": "s1"})
        self.assertTrue(cached_listing.checked)
        self.assertEqual(cached_listing.files, ())

    def test_stale_class_instance_still_caches_by_name(self):
        """模拟热重载：同名类但不同类对象，改动后仍可写入/读回。"""

        @dataclass(frozen=True)
        class CloudFile:  # noqa: F811 - 故意重名，模拟旧模块里的类
            id: str
            name: str
            is_directory: bool
            size: int = 0
            sha1: str = ""
            md5: str = ""
            playback_values: Dict[str, str] = None
            native: Any = None

        stale = CloudFile(id="f2", name="旧实例", is_directory=False, playback_values={})
        self.cache.set("stale", (stale,))
        cached = self.cache.get("stale")
        self.assertEqual(len(cached), 1)
        # 读回来的已是当前模块里的正经类型（按名字还原，不再依赖 pickle 的类身份）
        self.assertEqual(type(cached[0]).__name__, "CloudFile")
        self.assertEqual(cached[0].name, "旧实例")

    def test_unpicklable_object_is_skipped(self):
        self.cache.set("bad", object())
        self.assertIsNone(self.cache.get("bad"))
        self.assertFalse(self.cache.exists("bad"))

    def test_legacy_pickled_payload_is_treated_as_miss(self):
        self.cache._store["legacy"] = pickle.dumps(object())
        self.assertIsNone(self.cache.get("legacy"))

    def test_region_carries_schema_suffix(self):
        self.assertTrue(self.cache.region.endswith(":" + cache_module._CACHE_SCHEMA))


if __name__ == "__main__":
    unittest.main()
