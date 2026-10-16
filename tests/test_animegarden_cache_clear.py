"""AnimeGarden 搜索缓存清理回归测试（1.6.38）。

回归背景：「清理插件缓存」时 animegarden 渠道抛
``'AnimeGardenSearchService' object has no attribute '_lock'``，异常一路冒到
``core/api/runtime.py`` 的「清理插件缓存失败：」分支，导致整个清缓存动作失败
（搜索结果缓存与其它渠道的缓存也一并没清掉）。原因是该类改为平台 TTL 缓存
``self._search_cache`` 之后，``clear_cache`` 仍引用旧的自建字典缓存
``self._cache`` 与配套锁 ``self._lock``。

用例：
1. ``clear_cache`` 返回清理条数，且真的清空（清完再搜会重新请求）；
2. 空缓存时返回 0；
3. 不同客户端（不同 base_url）的缓存区互不影响；
4. 走真实 ``SearchProvider`` 的 CACHE_MAINTENANCE 能力调用（线上链路）。
"""

import unittest
from typing import Any, Dict, Optional
from unittest.mock import MagicMock

from plugin_env import (
    install_app_mocks,
    load_module,
    register_mock,
    register_module,
)

# ---- app.* 占位依赖（service.py 只用 app.log.logger） ----
install_app_mocks()

# ---- 平台 TTL 缓存替身 ----
# 真实实现是 app.core.cache.TTLCache（按 region 共享存储），这里最小复刻
# 插件用到的那部分：region 命名空间 + 容量上限 + items()/clear()。
_REGION_STORES: Dict[str, Dict[str, Any]] = {}


class FakePlatformTTLCache:
    """按 region 共享存储的平台缓存替身。"""

    def __init__(self, region: str, maxsize: int, ttl: int):
        self.region = region
        self.maxsize = max(1, int(maxsize))
        self.ttl = ttl
        self._store = _REGION_STORES.setdefault(region, {})

    def __contains__(self, key: Any) -> bool:
        return key in self._store

    def __getitem__(self, key: Any) -> Any:
        return self._store[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._store.pop(key, None)
        self._store[key] = value
        while len(self._store) > self.maxsize:
            self._store.pop(next(iter(self._store)))

    def __len__(self) -> int:
        return len(self._store)

    def get(self, key: Any, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: Any, value: Any) -> None:
        self[key] = value

    def items(self):
        return list(self._store.items())

    def clear(self) -> None:
        self._store.clear()


def fake_create_platform_ttl_cache(namespace: str, identity: Any = "", *, maxsize: int, ttl: int):
    """对齐 ``cloudsubscribex.utils.cache.create_platform_ttl_cache`` 的签名。"""
    return FakePlatformTTLCache(f"cloudsubscribex:{namespace}:{identity}", maxsize=maxsize, ttl=ttl)


register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=fake_create_platform_ttl_cache,
)

# ---- 搜索渠道依赖占位（本用例只关心缓存清理，不关心候选构造） ----
register_mock("cloudsubscribex.search.magnet", media_titles=MagicMock(), normalize_magnets=MagicMock())
register_mock("cloudsubscribex.search.matching", unique_texts=MagicMock())
register_mock(
    "cloudsubscribex.search.subs_filter",
    build_anime_candidate=MagicMock(),
    extract_fansub_from_title=MagicMock(),
    filter_fansubs=MagicMock(),
    release_matches=MagicMock(),
)
register_mock(
    "cloudsubscribex.search.animegarden.client",
    AnimeGardenClient=MagicMock(),
    AnimeGardenClientError=type("AnimeGardenClientError", (Exception,), {}),
)

# ---- 真实模块：core/search.py、animegarden/service.py、animegarden/provider.py ----
SearchCapability = load_module(
    "cloudsubscribex.core.search",
    "plugins.v2/cloudsubscribex/core/search.py",
).SearchCapability
AnimeGardenSearchService = load_module(
    "cloudsubscribex.search.animegarden.service",
    "plugins.v2/cloudsubscribex/search/animegarden/service.py",
).AnimeGardenSearchService
create_animegarden_provider = load_module(
    "cloudsubscribex.search.animegarden.provider",
    "plugins.v2/cloudsubscribex/search/animegarden/provider.py",
).create_animegarden_provider


class FakeAnimeGardenClient:
    """记录调用次数的 AnimeGarden 客户端替身。"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.search_calls = []

    def search(self, keyword: str = "", exclude=None, page: int = 1, page_size: int = 200, **kwargs):
        self.search_calls.append({"keyword": keyword, "page": page, "page_size": page_size})
        if page != 1:
            return []
        return [{
            "title": f"[LoliHouse] {keyword} - 01 [1080p][简繁内封字幕]",
            "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
            "size": 1024 ** 3,
            "fansub": {"name": "LoliHouse"},
        }]


class TestAnimeGardenCacheClear(unittest.TestCase):
    """animegarden 渠道的 CACHE_MAINTENANCE 能力（清缓存）行为测试。"""

    def setUp(self):
        # 每个用例用独立 base_url，避免平台缓存替身的 region 共享造成串扰
        self.base_url = f"https://animes.garden/{self._testMethodName}/"
        self.client = FakeAnimeGardenClient(self.base_url)
        self.service = AnimeGardenSearchService(self.client, result_limit=10)

    def _cache_entries(self, service=None) -> list:
        return list((service or self.service)._search_cache.items())

    def test_clear_cache_counts_and_empties_entries(self):
        """清缓存返回条数，清空后再搜会重新请求。"""
        self.service._cached_search("葬送的芙莉莲")
        self.service._cached_search("葬送的芙莉莲")
        self.service._cached_search("药屋少女的呢喃")

        self.assertEqual(len(self.client.search_calls), 2)  # 第二个关键词：第一次请求
        self.assertEqual(len(self._cache_entries()), 2)

        self.assertEqual(self.service.clear_cache(), 2)
        self.assertEqual(self._cache_entries(), [])

        # 缓存确实清掉了：同一个关键词会重新发请求
        self.service._cached_search("葬送的芙莉莲")
        self.assertEqual(len(self.client.search_calls), 3)
        self.assertEqual(len(self._cache_entries()), 1)

    def test_clear_cache_on_empty_cache_returns_zero(self):
        """空缓存下清缓存返回 0，且不抛异常。"""
        self.assertEqual(self.service.clear_cache(), 0)
        self.assertEqual(self._cache_entries(), [])

    def test_clear_cache_is_isolated_per_client(self):
        """不同客户端（不同缓存区）互不影响。"""
        other_client = FakeAnimeGardenClient(self.base_url + "mirror/")
        other_service = AnimeGardenSearchService(other_client, result_limit=10)

        self.service._cached_search("葬送的芙莉莲")
        other_service._cached_search("葬送的芙莉莲")

        self.assertEqual(self.service.clear_cache(), 1)
        self.assertEqual(self._cache_entries(), [])
        self.assertEqual(len(self._cache_entries(other_service)), 1)

    def test_provider_cache_maintenance_path(self):
        """走真实 SearchProvider 能力表调用清缓存（对齐线上清理链路）。"""
        provider = create_animegarden_provider(
            self.service, {"base_url": self.base_url, "limit": 10}
        )
        self.assertIn(SearchCapability.CACHE_MAINTENANCE, provider.capabilities)

        self.service._cached_search("葬送的芙莉莲")
        self.assertEqual(provider.clear_cache(), 1)
        self.assertEqual(self._cache_entries(), [])
        # 二次调用不该再报 AttributeError
        self.assertEqual(provider.clear_cache(), 0)


if __name__ == "__main__":
    unittest.main()
