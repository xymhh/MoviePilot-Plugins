"""Netflix 非富模式国家榜 TSV 回归测试（v1.22.0）。

修的是静默丢失：非富模式此前只走全球榜，country_selections 被直接
忽略（has_listening() 却因它返回 True，用户 global=False + 国家选择
+ 富模式关时会拿到 0 条且无任何提示）。v1.22.0 起非富模式在全球块
之后按富模式尾部同样处理国家 TSV：拉一次国家周榜、按 country_iso2
分国家、用 service.candidates 出候选。
"""
import os
import sys
import threading
import unittest

from plugin_env import (
    REPO_ROOT,
    install_app_mocks,
    load_module,
    register_module,
    register_package,
)

install_app_mocks(
    "app.chain",
    "app.chain.download",
    "app.chain.media",
    "app.chain.subscribe",
    "app.helper",
    "app.helper.mediaserver",
    "app.application",
    "app.application.mediaserver",
    "app.db",
    "app.db.site_oper",
    "app.db.subscribe_oper",
    "app.db.systemconfig_oper",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
)

register_package("cloudsubscribex", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex")])
register_package("cloudsubscribex.core", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core")])
register_package("cloudsubscribex.core.subscribe",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core/subscribe")])
register_package("cloudsubscribex.subscribe",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe")])
register_package("cloudsubscribex.subscribe.netflix",
                paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/subscribe/netflix")])
register_package("cloudsubscribex.search", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/search")])
register_package("cloudsubscribex.utils", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/utils")])

_models_module = load_module(
    "cloudsubscribex.core.subscribe.models", "plugins.v2/cloudsubscribex/core/subscribe/models.py"
)
_provider_module = load_module(
    "cloudsubscribex.core.subscribe.provider", "plugins.v2/cloudsubscribex/core/subscribe/provider.py"
)
load_module("cloudsubscribex.core.subscribe.registry", "plugins.v2/cloudsubscribex/core/subscribe/registry.py")

_subscribe_pkg = sys.modules["cloudsubscribex.core.subscribe"]
_subscribe_pkg.MediaCandidate = _models_module.MediaCandidate
_subscribe_pkg.SubscribeContext = _provider_module.SubscribeContext
_subscribe_pkg.SubscribeProvider = _provider_module.SubscribeProvider
_subscribe_pkg.RankingProvider = _provider_module.RankingProvider

_service_module = load_module(
    "cloudsubscribex.subscribe.netflix.service",
    "plugins.v2/cloudsubscribex/subscribe/netflix/service.py",
)


class _StubNetflixClient:
    """只供 provider 取 BASE_URL；真实请求走下面的 FakeClient。"""

    BASE_URL = "https://www.netflix.com"


register_module("cloudsubscribex.subscribe.netflix.client", NetflixClient=_StubNetflixClient)

_netflix_module = load_module(
    "cloudsubscribex.subscribe.netflix.provider",
    "plugins.v2/cloudsubscribex/subscribe/netflix/provider.py",
)
NetflixSubscribeProvider = _netflix_module.NetflixSubscribeProvider
SubscribeContext = _provider_module.SubscribeContext

GLOBAL_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv"
COUNTRIES_URL = "https://www.netflix.com/tudum/top10/data/all-weeks-countries.tsv"

GLOBAL_ROWS = [
    {"week": "2026-W01", "category": "Films (English)", "show_title": "Global Film", "rank": 1},
    {"week": "2026-W01", "category": "TV (English)", "show_title": "Global Show",
    "season_title": "Season 1", "rank": 2},
    {"week": "2025-W52", "category": "Films (English)", "show_title": "Old Film", "rank": 1},
]

COUNTRY_ROWS = [
    {"week": "2026-W01", "country_iso2": "JP", "category": "Films", "show_title": "JP Film", "rank": 1},
    {"week": "2026-W01", "country_iso2": "JP", "category": "TV", "show_title": "JP Show",
    "season_title": "Season 3", "rank": 2},
    {"week": "2026-W01", "country_iso2": "KR", "category": "TV", "show_title": "KR Show",
    "season_title": "Season 2", "rank": 1},
    {"week": "2026-W01", "country_iso2": "KR", "category": "Films", "show_title": "KR Film", "rank": 2},
    {"week": "2026-W01", "country_iso2": "US", "category": "Films", "show_title": "US Film", "rank": 1},
    {"week": "2025-W52", "country_iso2": "JP", "category": "Films", "show_title": "Old JP Film", "rank": 1},
]


class _FakeClient:
    base_url = "https://www.netflix.com"
    most_popular_url = "https://www.netflix.com/tudum/top10/data/most-popular.tsv"
    global_weekly_url = GLOBAL_URL
    countries_weekly_url = COUNTRIES_URL

    def __init__(self):
        self.loaded_urls = []

    def load_tsv(self, url, proxy=None):
        self.loaded_urls.append(url)
        if url == GLOBAL_URL:
            return [dict(row) for row in GLOBAL_ROWS]
        if url == COUNTRIES_URL:
            return [dict(row) for row in COUNTRY_ROWS]
        raise AssertionError(f"unexpected url: {url}")

    def load_html(self, url, proxy=None):
        raise AssertionError("non-rich path must not load html")


class NetflixCountryTsvTests(unittest.TestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.provider = NetflixSubscribeProvider(
            client=self.client, service=_service_module.NetflixService()
        )

    def _fetch(self, options):
        merged = {"use_cache": False, "limit": 20}
        merged.update(options)
        return list(self.provider.fetch(merged, SubscribeContext(event=threading.Event())))

    def test_global_off_country_selections_yield_only_selected(self):
        results = self._fetch({
            "global": False,
            "country_selections": {"JP": ["Films"], "KR": ["TV"]},
        })
        self.assertEqual([c.title for c in results], ["JP Film", "KR Show"])
        self.assertEqual(results[0].media_type, "movie")
        self.assertEqual(results[1].media_type, "tv")
        self.assertEqual(results[1].season, 2)
        self.assertEqual(results[1].source_meta["scope"], "KR")
        # 国家 TSV 只拉一次；全球 TSV 不拉
        self.assertEqual(self.client.loaded_urls, [COUNTRIES_URL])

    def test_global_on_plus_selections_yields_both(self):
        results = self._fetch({
            "global": True,
            "country_selections": {"JP": ["Films"], "KR": ["TV"]},
        })
        self.assertEqual(
            [c.title for c in results],
            ["Global Film", "Global Show", "JP Film", "KR Show"],
        )
        self.assertEqual(self.client.loaded_urls, [GLOBAL_URL, COUNTRIES_URL])

    def test_selections_as_json_string(self):
        results = self._fetch({
            "global": False,
            "country_selections": '{"JP": ["Films"]}',
        })
        self.assertEqual([c.title for c in results], ["JP Film"])

    def test_dict_form_categories_with_on_flag(self):
        results = self._fetch({
            "global": False,
            "country_selections": {
                "JP": {"cats": ["Films", "TV"], "on": True},
                "KR": {"cats": ["TV"], "on": False},
            },
        })
        self.assertEqual([c.title for c in results], ["JP Film", "JP Show"])
        jp_show = results[1]
        self.assertEqual(jp_show.season, 3)

    def test_global_off_no_selections_yields_nothing(self):
        self.assertEqual(self._fetch({"global": False}), [])
        self.assertEqual(self.client.loaded_urls, [])

    def test_global_on_no_selections_unchanged(self):
        results = self._fetch({"global": True})
        self.assertEqual([c.title for c in results], ["Global Film", "Global Show"])
        self.assertEqual(self.client.loaded_urls, [GLOBAL_URL])

    def test_unknown_category_skipped(self):
        results = self._fetch({
            "global": False,
            "country_selections": {"JP": ["Documentaries"]},
        })
        self.assertEqual(results, [])

    def test_has_listening_with_selections(self):
        self.assertTrue(self.provider.has_listening({"global": False, "country_selections": {"JP": ["Films"]}}))


if __name__ == "__main__":
    unittest.main()
