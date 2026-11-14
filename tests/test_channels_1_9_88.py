"""1.9.88：渠道管理改成网盘直链（QuarkChannel）模式。

搜索渠道 + TG 监控频道统一成渠道对象（channels.json），覆盖：
1. ChannelStore 的 CRUD / 校验 / 原子落盘 / 损坏重建；
2. 迁移：旧配置 → channels.json（含 search_source_order 默认只有 pansou）；
3. 搜索与 TG 监控按启用 + 优先级取渠道；
4. 渠道 CRUD API 行为；
5. 插件胶水：_migrate_channels_from_config / _refresh_channel_runtime /
   _apply_tg_monitor_config 以渠道库为准。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

install_app_mocks()
MEDIA_TYPE = set_media_type()

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS_V2 = os.path.join(REPO_ROOT, "plugins.v2")

ensure_package("cloudsubscribex").__path__ = [
    os.path.join(PLUGINS_V2, "cloudsubscribex")
]
ensure_package("cloudsubscribex.core").__path__ = [
    os.path.join(PLUGINS_V2, "cloudsubscribex", "core")
]
ensure_package("cloudsubscribex.search").__path__ = [
    os.path.join(PLUGINS_V2, "cloudsubscribex", "search")
]
ensure_package("cloudsubscribex.core.api").__path__ = [
    os.path.join(PLUGINS_V2, "cloudsubscribex", "core", "api")
]
ensure_package("cloudsubscribex.handlers.search").__path__ = [
    os.path.join(PLUGINS_V2, "cloudsubscribex", "handlers", "search")
]

# 真实 tg_monitor：store 的 TG 归一化与插件的 _apply_tg_monitor_config 都复用它。
tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)


def _current_tg_module():
    """取当前 sys.modules 里的 tg_monitor。

    其它测试文件（如 test_tg_monitor.py）在收集期会重新 load_module
    同名模块，模块级 tg_module 变量会变旧；打补丁必须以运行时的
    sys.modules 为准，否则补丁打到旧对象上不生效。
    """
    return sys.modules["cloudsubscribex.core.tg_monitor"]
channels_module = load_module(
    "cloudsubscribex.core.channels",
    "plugins.v2/cloudsubscribex/core/channels.py",
)
ChannelStore = channels_module.ChannelStore

# ------------------------------------------------------------------ 假定义
class _FakeDefinition:
    """搜索源定义桩：只保留 store 迁移/校验用到的属性。"""

    def __init__(self, id, name, order, keys):
        self.id = id
        self.name = name
        self.order = order
        self._keys = frozenset(keys)

    def get_config_keys(self):
        return self._keys

    def get_config_groups(self):
        return []


FAKE_DEFS = {
    "pansou": _FakeDefinition(
        "pansou", "盘搜", 10, {"pansou_url", "pansou_token"}
    ),
    "hdhive": _FakeDefinition(
        "hdhive", "HDHive", 20, {"hdhive_token"}
    ),
    "mikan": _FakeDefinition(
        "mikan", "蜜柑计划", 30, {"mikan_username", "mikan_password"}
    ),
}


class _FakeRegistry:
    """channels.py 所依赖的 SearchSourceRegistry 桩。"""

    @classmethod
    def get_definitions(cls):
        return [FAKE_DEFS[key] for key in ("pansou", "hdhive", "mikan")]

    @classmethod
    def get_source_catalog(cls):
        return [
            {
                "key": definition.id,
                "name": definition.name,
                "icon": "mdi-magnify",
                "color": "blue",
                "capabilities": ["query"],
            }
            for definition in cls.get_definitions()
        ]

    @classmethod
    def get_search_schemas(cls):
        return [
            {
                "id": definition.id,
                "subtab": {
                    "value": definition.id,
                    "title": definition.name,
                    "icon": "mdi-magnify",
                },
                "groups": [],
            }
            for definition in cls.get_definitions()
        ]


register_mock("cloudsubscribex.search.scanner", SearchSourceRegistry=_FakeRegistry)

api_channels_module = load_module(
    "cloudsubscribex.core.api.channels",
    "plugins.v2/cloudsubscribex/core/api/channels.py",
)
ChannelsApi = api_channels_module.ChannelsApi


def _tmpdir(test_case):
    tmp = tempfile.TemporaryDirectory()
    test_case.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def _seed_store(directory):
    store = ChannelStore(directory, definitions=FAKE_DEFS)
    pansou = store.create({
        "name": "盘搜", "type": "pansou", "enabled": True, "priority": 100,
        "config": {"pansou_url": "http://pansou:8888", "pansou_token": "tok"},
    })
    hdhive = store.create({
        "name": "HDHive", "type": "hdhive", "enabled": False, "priority": 90,
        "config": {"hdhive_token": "hive"},
    })
    tg = store.create({
        "name": "@yunpanx", "type": "tg_channel", "enabled": True,
        "priority": 50,
        "config": {
            "channel": "@yunpanx", "keywords": ["4k"],
            "exclude_keywords": [], "scan_limit": 20,
        },
    })
    return store, pansou, hdhive, tg


# ------------------------------------------------------------------ Store
class ChannelStoreTest(unittest.TestCase):
    def test_crud_and_priority_order(self):
        directory = _tmpdir(self)
        store, pansou, hdhive, tg = _seed_store(directory)
        items = store.list()
        self.assertEqual(
            [item["type"] for item in items], ["pansou", "hdhive", "tg_channel"]
        )
        self.assertEqual(len(pansou["id"]), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in pansou["id"]))
        self.assertEqual(pansou["stats"], {"last_scan": 0.0, "last_error": "", "hits": 0})

        got = store.get(pansou["id"])
        self.assertEqual(got["name"], "盘搜")
        self.assertIsNone(store.get("nope"))

        updated = store.update(hdhive["id"], {"enabled": True, "priority": 200})
        self.assertTrue(updated["enabled"])
        self.assertEqual(store.list()[0]["type"], "hdhive")
        self.assertIsNone(store.update("nope", {"enabled": True}))

        self.assertTrue(store.delete(tg["id"]))
        self.assertFalse(store.delete(tg["id"]))
        self.assertEqual(len(store.list()), 2)

    def test_create_rejects_bad_type_and_empty_name(self):
        directory = _tmpdir(self)
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        with self.assertRaises(ValueError):
            store.create({"name": "x", "type": "nope"})
        with self.assertRaises(ValueError):
            store.create({"name": "   ", "type": "pansou"})
        with self.assertRaises(ValueError):
            store.create({"name": "x", "type": "tg_channel", "config": {}})

    def test_search_config_only_keeps_declared_keys(self):
        directory = _tmpdir(self)
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        item = store.create({
            "name": "盘搜", "type": "pansou",
            "config": {
                "pansou_url": "http://x", "pansou_token": "t",
                "search_proxy": "http://proxy", "unknown_key": 1,
            },
        })
        # 全局搜索设置（代理等）与未知键都不进渠道配置。
        self.assertEqual(
            item["config"], {"pansou_url": "http://x", "pansou_token": "t"}
        )

    def test_tg_config_normalized(self):
        directory = _tmpdir(self)
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        item = store.create({
            "name": "t", "type": "tg_channel",
            "config": {
                "channel": "https://t.me/yunpanx",
                "keywords": "4K, 1080p",
                "exclude_keywords": ["生肉"],
                "scan_limit": 500,
            },
        })
        self.assertEqual(item["config"]["channel"], "@yunpanx")
        self.assertEqual(item["config"]["keywords"], ["4k", "1080p"])
        self.assertEqual(item["config"]["exclude_keywords"], ["生肉"])
        self.assertEqual(item["config"]["scan_limit"], 200)

    def test_atomic_save_and_reload(self):
        directory = _tmpdir(self)
        store, pansou, _, _ = _seed_store(directory)
        payload = json.loads((directory / "channels.json").read_text(encoding="utf-8"))
        self.assertEqual(len(payload["items"]), 3)
        self.assertFalse((directory / "channels.json.tmp").exists())
        reloaded = ChannelStore(directory, definitions=FAKE_DEFS)
        self.assertEqual(
            [item["id"] for item in reloaded.list()],
            [item["id"] for item in store.list()],
        )

    def test_record_stat(self):
        directory = _tmpdir(self)
        store, pansou, _, _ = _seed_store(directory)
        updated = store.record_stat(pansou["id"], hits=3)
        self.assertEqual(updated["stats"]["hits"], 3)
        self.assertGreater(updated["stats"]["last_scan"], 0)
        updated = store.record_stat(pansou["id"], hits=2, error="boom")
        self.assertEqual(updated["stats"]["hits"], 5)
        self.assertEqual(updated["stats"]["last_error"], "boom")
        self.assertIsNone(store.record_stat("nope", hits=1))

    def test_corrupt_file_loads_empty_and_migrates(self):
        directory = _tmpdir(self)
        (directory / "channels.json").write_text("{not json", encoding="utf-8")
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        self.assertEqual(store.list(), [])
        self.assertTrue(store.needs_migration)
        added = store.migrate_from_config({"search_source_order": ["pansou"]})
        self.assertEqual(added, 3)
        items = store.list()
        self.assertEqual(
            [item["type"] for item in items], ["pansou", "hdhive", "mikan"]
        )
        self.assertTrue(items[0]["enabled"])
        self.assertFalse(items[1]["enabled"])

    def test_migrate_default_only_pansou_enabled(self):
        directory = _tmpdir(self)
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        # 配置里没有 search_source_order：默认只有 pansou 启用。
        added = store.migrate_from_config({})
        self.assertEqual(added, 3)
        by_type = {item["type"]: item for item in store.list()}
        self.assertTrue(by_type["pansou"]["enabled"])
        self.assertEqual(by_type["pansou"]["priority"], 100)
        self.assertFalse(by_type["hdhive"]["enabled"])
        self.assertFalse(by_type["mikan"]["enabled"])
        self.assertLess(by_type["hdhive"]["priority"], 0)
        self.assertLess(by_type["mikan"]["priority"], 0)
        # 未启用源排在启用源之后（priority 倒序）。
        self.assertEqual(
            [item["type"] for item in store.list()],
            ["pansou", "hdhive", "mikan"],
        )

    def test_migrate_order_and_tg_rules(self):
        directory = _tmpdir(self)
        store = ChannelStore(directory, definitions=FAKE_DEFS)
        added = store.migrate_from_config({
            "search_source_order": ["hdhive", "pansou"],
            "pansou_url": "http://pansou:8888",
            "pansou_token": "tok",
            "tg_channel_rules": [
                {
                    "channel": "@yunpanx", "enabled": True,
                    "keywords": ["4K"], "exclude_keywords": ["生肉"],
                    "scan_limit": 5,
                },
                {"channel": "@other", "enabled": False, "scan_limit": 30},
            ],
        })
        self.assertEqual(added, 5)
        items = store.list()
        self.assertEqual(
            [item["type"] for item in items],
            ["hdhive", "pansou", "tg_channel", "tg_channel", "mikan"],
        )
        by_type = {}
        for item in items:
            by_type.setdefault(item["type"], []).append(item)
        self.assertEqual(by_type["hdhive"][0]["priority"], 100)
        self.assertEqual(by_type["pansou"][0]["priority"], 90)
        # 迁移时把插件配置里该源现有的值抄进渠道。
        self.assertEqual(by_type["pansou"][0]["config"], {
            "pansou_url": "http://pansou:8888", "pansou_token": "tok",
        })
        tg_items = by_type["tg_channel"]
        self.assertEqual(tg_items[0]["config"], {
            "channel": "@yunpanx", "keywords": ["4k"],
            "exclude_keywords": ["生肉"], "scan_limit": 5,
        })
        self.assertEqual(tg_items[0]["priority"], 50)
        self.assertTrue(tg_items[0]["enabled"])
        self.assertEqual(tg_items[1]["config"]["channel"], "@other")
        self.assertFalse(tg_items[1]["enabled"])
        self.assertEqual(tg_items[1]["priority"], 40)

    def test_migrate_noop_when_valid_file_exists(self):
        directory = _tmpdir(self)
        store, _, _, _ = _seed_store(directory)
        self.assertEqual(store.migrate_from_config({"search_source_order": []}), 0)
        # 用户在渠道页删空也不复活。
        for item in store.list():
            store.delete(item["id"])
        self.assertEqual(store.list(), [])
        self.assertEqual(store.migrate_from_config({"search_source_order": ["pansou"]}), 0)
        self.assertEqual(store.list(), [])


# ------------------------------------------------------------------ SearchHandler
def _load_search_service():
    """打桩后加载真实 handlers/search/service.py。"""
    core = ensure_package("cloudsubscribex.core")
    core.SEARCH_CIRCUIT_BREAKER = MagicMock()
    core.SearchCapability = MagicMock()
    core.SearchQuery = MagicMock()
    core.format_search_label = MagicMock()
    core.format_search_log_prefix = MagicMock()
    core.resolve_component = MagicMock()
    register_module("cloudsubscribex.core.media", tmdb_id_of=MagicMock())
    register_module(
        "cloudsubscribex.handlers.search.platform_rules",
        PlatformRuleService=MagicMock(),
    )
    register_mock("cloudsubscribex.search.dian115", Dian115SearchService=MagicMock())
    register_mock("cloudsubscribex.search.hdhaven", HDHavenSearchService=MagicMock())
    register_mock("cloudsubscribex.search.hdhive", HDHiveSearchService=MagicMock())
    register_module(
        "cloudsubscribex.search.http_client", RequestGateCooldown=MagicMock()
    )
    register_mock(
        "cloudsubscribex.search.matching",
        is_anime_media=MagicMock(return_value=False),
        positive_ints=MagicMock(return_value=[]),
        unique_texts=MagicMock(side_effect=lambda value: list(value or [])),
    )
    register_mock("cloudsubscribex.search.mikan.service", filter_fansubs=MagicMock())
    register_mock("cloudsubscribex.search.pansou", PanSouSearchService=MagicMock())
    register_mock(
        "cloudsubscribex.search.registry", create_search_registry=MagicMock()
    )
    register_mock(
        "cloudsubscribex.search.types", normalize_resource_type=MagicMock()
    )
    register_mock(
        "cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock()
    )
    return load_module(
        "cloudsubscribex.handlers.search.service",
        "plugins.v2/cloudsubscribex/handlers/search/service.py",
    )


_service_module = _load_search_service()
SearchHandler = _service_module.SearchHandler
_resolve_order = _service_module._resolve_enabled_source_order


class _FakeSearchRegistry:
    def __init__(self, keys):
        self._keys = list(keys)

    def available(self):
        return [SimpleNamespace(key=key) for key in self._keys]


def _bare_handler(**attrs):
    handler = object.__new__(SearchHandler)
    for key, value in attrs.items():
        object.__setattr__(handler, key, value)
    return handler


class ResolveOrderTest(unittest.TestCase):
    def test_explicit_param_wins_over_store(self):
        plugin = SimpleNamespace(
            get_enabled_search_sources=lambda: ["pansou", "mikan"]
        )
        self.assertEqual(
            _resolve_order(plugin, {"search_source_order": ["hdhive"]}),
            ["hdhive"],
        )

    def test_reads_channel_store(self):
        plugin = SimpleNamespace(
            get_enabled_search_sources=lambda: ["pansou", "mikan"]
        )
        self.assertEqual(_resolve_order(plugin, {}), ["pansou", "mikan"])

    def test_falls_back_to_legacy_order(self):
        plugin = SimpleNamespace(_search_source_order=["pansou"])
        self.assertEqual(_resolve_order(plugin, {}), ["pansou"])
        plugin = SimpleNamespace()
        self.assertEqual(
            _resolve_order(plugin, {"search_source_order": ["hdhive"]}),
            ["hdhive"],
        )


class SearchHandlerChannelTest(unittest.TestCase):
    def test_get_enabled_sources_from_store_order(self):
        handler = _bare_handler(
            _search_source_order=["pansou", "mikan", "hdhive"],
            _search_registry=_FakeSearchRegistry({"pansou", "mikan"}),
        )
        self.assertEqual(handler.get_enabled_sources(), ["pansou", "mikan"])
        # 电影排除纯番剧源。
        self.assertEqual(
            handler.get_enabled_sources(MEDIA_TYPE.MOVIE), ["pansou"]
        )

    def test_refresh_enabled_sources_rereads_store_and_clears_cache(self):
        handler = _bare_handler(
            _search_source_order=["pansou"],
            _search_registry=_FakeSearchRegistry({"pansou", "hdhive"}),
            _plugin=SimpleNamespace(
                get_enabled_search_sources=lambda: ["hdhive", "pansou"]
            ),
        )
        handler.clear_search_cache = MagicMock()
        handler.refresh_enabled_sources()
        self.assertEqual(handler._search_source_order, ["hdhive", "pansou"])
        handler.clear_search_cache.assert_called_once_with()

    def test_configured_source_order_reads_store(self):
        handler = _bare_handler(
            _search_source_order=["pansou"],
            _plugin=SimpleNamespace(
                get_search_channel_order=lambda: ["mikan", "pansou", "hdhive"]
            ),
        )
        self.assertEqual(
            handler._configured_source_order(), ["mikan", "pansou", "hdhive"]
        )
        handler = _bare_handler(
            _search_source_order=["pansou"], _plugin=SimpleNamespace()
        )
        self.assertEqual(handler._configured_source_order(), ["pansou"])

    def test_auto_remove_disables_channel_in_store(self):
        disabler = MagicMock(return_value=True)
        handler = _bare_handler(
            _search_source_order=["pansou", "hdhive"],
            _plugin=SimpleNamespace(disable_search_channel=disabler),
        )
        handler._auto_remove_search_source("pansou", "账号已被封禁")
        self.assertEqual(handler._search_source_order, ["hdhive"])
        disabler.assert_called_once_with("pansou")


# ------------------------------------------------------------------ ChannelsApi
class _StubOwner:
    """ChannelsApi 的宿主桩：只提供渠道库与刷新/持久化钩子。"""

    def __init__(self, directory):
        self._data_dir = Path(directory)
        self._channels = ChannelStore(self._data_dir, definitions=FAKE_DEFS)
        self._applied_config = {}
        self._tg_api_id = 0
        self._tg_api_hash = ""
        self._tg_session = ""
        self._tg_monitor_enabled = False
        self.refresh_calls = []
        self.persisted = {}

    def get_data_path(self):
        return self._data_dir

    def _channel_store(self):
        return self._channels

    def _refresh_channel_runtime(self, search_config_updates=None):
        self.refresh_calls.append(search_config_updates)

    def _persist_config_values(self, **updates):
        self.persisted.update(updates)
        self._applied_config.update(updates)

    def get_config(self):
        return dict(self._applied_config)


class ChannelsApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = _tmpdir(self)
        self.owner = _StubOwner(self.directory)
        self.store, self.pansou, self.hdhive, self.tg = _seed_store(self.directory)
        # _seed_store 建了新 store 实例；让宿主用同一个。
        self.owner._channels = self.store
        self.api = ChannelsApi(self.owner)

    def test_list_returns_items_catalog_schemas(self):
        result = self.api.api_channels()
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(
            [entry["key"] for entry in data["catalog"]],
            ["pansou", "hdhive", "mikan"],
        )
        self.assertEqual(
            [schema["id"] for schema in data["schemas"]],
            ["pansou", "hdhive", "mikan"],
        )

    def test_list_merges_live_plugin_config_values(self):
        self.owner._applied_config = {"pansou_url": "http://new:8888"}
        items = self.api.api_channels()["data"]["items"]
        pansou = next(item for item in items if item["type"] == "pansou")
        self.assertEqual(pansou["config"]["pansou_url"], "http://new:8888")

    def test_create_duplicate_search_type_rejected(self):
        result = self.api.api_channel_create({
            "name": "盘搜2", "type": "pansou", "config": {},
        })
        self.assertFalse(result["success"])
        self.assertIn("已存在", result["message"])
        self.assertEqual(len(self.store.list()), 3)

    def test_create_update_delete_refresh_runtime(self):
        created = self.api.api_channel_create({
            "name": "@new", "type": "tg_channel",
            "config": {"channel": "@new", "scan_limit": 10},
        })
        self.assertTrue(created["success"])
        self.assertEqual(self.owner.refresh_calls, [None])

        updated = self.api.api_channel_update(
            created["data"]["id"], {"enabled": False}
        )
        self.assertTrue(updated["success"])
        self.assertFalse(updated["data"]["enabled"])
        self.assertEqual(len(self.owner.refresh_calls), 2)

        self.assertFalse(self.api.api_channel_update("nope", {})["success"])
        self.assertTrue(
            self.api.api_channel_delete(created["data"]["id"])["success"]
        )
        self.assertFalse(self.api.api_channel_delete("nope")["success"])
        self.assertEqual(len(self.store.list()), 3)

    def test_update_persists_changed_search_config(self):
        updated = self.api.api_channel_update(self.pansou["id"], {
            "config": {
                "pansou_url": "http://pansou:8888",
                "pansou_token": "new-token",
            },
        })
        self.assertTrue(updated["success"])
        self.assertEqual(self.owner.persisted, {"pansou_token": "new-token"})
        self.assertEqual(
            self.owner.refresh_calls, [{"pansou_token": "new-token"}]
        )
        # 没变的键不写回。
        self.api.api_channel_update(self.pansou["id"], {
            "config": {
                "pansou_url": "http://pansou:8888",
                "pansou_token": "new-token",
            },
        })
        self.assertEqual(len(self.owner.refresh_calls), 2)
        self.assertEqual(self.owner.refresh_calls[1], None)

    def test_create_search_channel_persists_config(self):
        self.store.delete(self.hdhive["id"])
        created = self.api.api_channel_create({
            "name": "HDHive", "type": "hdhive",
            "config": {"hdhive_token": "abc"},
        })
        self.assertTrue(created["success"])
        self.assertEqual(self.owner.persisted, {"hdhive_token": "abc"})

    def test_test_tg_channel_without_credentials(self):
        result = self.api.api_channel_test(self.tg["id"])
        self.assertFalse(result["success"])
        self.assertIn("未登录", result["message"])
        self.assertEqual(
            self.store.get(self.tg["id"])["stats"]["last_error"], result["message"]
        )

    def test_test_tg_channel_with_credentials(self):
        self.owner._tg_api_id = 123
        self.owner._tg_api_hash = "hash"
        self.owner._tg_session = "session"
        self.owner._tg_monitor_enabled = True
        result = self.api.api_channel_test(self.tg["id"])
        self.assertTrue(result["success"])
        self.assertIn("@yunpanx", result["message"])

    def test_test_search_channel_uses_isolated_handler(self):
        calls = {}

        class _FakeSearchApi:
            def __init__(self, owner):
                self._owner = owner

            def _test_search_config(self, source, overrides):
                calls["config"] = (source, dict(overrides))
                return {"source": source, **overrides}

            def _build_test_search_handler(self, source, config):
                calls["handler"] = (source, dict(config))

                class _Handler:
                    closed = False

                    def test_source(self, src, mediainfo, media_type, season):
                        calls["test"] = (src, mediainfo.title, season)
                        return [{"title": "a"}, {"title": "b"}]

                    def close(self, release_cache=False):
                        _Handler.closed = True

                return _Handler()

        saved = sys.modules.get("cloudsubscribex.core.api.search")
        fake_module = MagicMock()
        fake_module.SearchApi = _FakeSearchApi
        sys.modules["cloudsubscribex.core.api.search"] = fake_module
        try:
            result = self.api.api_channel_test(self.pansou["id"])
        finally:
            if saved is None:
                sys.modules.pop("cloudsubscribex.core.api.search", None)
            else:
                sys.modules["cloudsubscribex.core.api.search"] = saved
        self.assertTrue(result["success"])
        self.assertIn("2 条", result["message"])
        self.assertEqual(calls["config"][0], "pansou")
        self.assertEqual(calls["config"][1]["pansou_url"], "http://pansou:8888")
        self.assertEqual(calls["test"][0], "pansou")
        self.assertEqual(self.store.get(self.pansou["id"])["stats"]["hits"], 2)

    def test_test_unknown_channel(self):
        self.assertFalse(self.api.api_channel_test("nope")["success"])

    def test_scan_rejects_search_channel(self):
        result = self.api.api_channel_scan(self.pansou["id"])
        self.assertFalse(result["success"])
        self.assertIn("不支持扫描", result["message"])

    def test_scan_single_tg_channel(self):
        calls = {}

        class _FakeMonitor:
            def scan(self, trigger="", channels=None, **kwargs):
                calls["scan"] = (trigger, list(channels or []))
                return {"ok": True, "added": 2}

        _tg = _current_tg_module()
        original = _tg.get_channel_monitor
        _tg.get_channel_monitor = lambda owner: _FakeMonitor()
        try:
            result = self.api.api_channel_scan(self.tg["id"])
        finally:
            _tg.get_channel_monitor = original
        self.assertTrue(result["success"])
        self.assertEqual(result["data"], {"added": 2})
        self.assertEqual(calls["scan"], ("手动", ["@yunpanx"]))
        self.assertEqual(self.store.get(self.tg["id"])["stats"]["hits"], 2)

    def test_scan_failed_monitor(self):
        class _FakeMonitor:
            def scan(self, trigger="", channels=None, **kwargs):
                return {"ok": False, "message": "频道监控未启用"}

        _tg = _current_tg_module()
        original = _tg.get_channel_monitor
        _tg.get_channel_monitor = lambda owner: _FakeMonitor()
        try:
            result = self.api.api_channel_scan(self.tg["id"])
        finally:
            _tg.get_channel_monitor = original
        self.assertFalse(result["success"])
        self.assertIn("未启用", result["message"])

    def test_summary_counts(self):
        data = self.api.api_channels_summary()["data"]
        self.assertEqual(data, {
            "total": 3, "enabled": 2,
            "search_total": 2, "search_enabled": 1,
            "tg_total": 1, "tg_enabled": 1,
            "tg_monitor_enabled": False,
        })


# ------------------------------------------------------------------ 插件胶水
class _PluginChannelTest(unittest.TestCase):
    """复用 1.9.86 的真实插件加载器，只测渠道相关的胶水方法。"""

    @classmethod
    def setUpClass(cls):
        from test_downloader_plugin_1_9_86 import _load_plugin_module, _plugin

        cls._make_plugin = staticmethod(_plugin)
        # 插件加载器把 search.scanner 打了桩：这里换成假注册表，
        # 让 get_search_channel_order 能走到“补齐未入库的已注册源”分支。
        cls._plugin_module = _load_plugin_module()
        cls._plugin_module.SearchSourceRegistry = _FakeRegistry

    def _plugin_with_store(self, config=None):
        plugin = self._make_plugin()
        directory = _tmpdir(self)
        plugin._channels = ChannelStore(directory, definitions=FAKE_DEFS)
        plugin._applied_config = dict(config or {})
        plugin._search_handler = SimpleNamespace(
            refresh_enabled_sources=MagicMock(),
            clear_search_cache=MagicMock(),
            close=MagicMock(),
        )
        return plugin, directory

    def test_enabled_sources_and_order_from_store(self):
        plugin, _ = self._plugin_with_store()
        store, _, _, _ = _seed_store(plugin._channels._dir)
        plugin._channels = store
        self.assertEqual(plugin.get_enabled_search_sources(), ["pansou"])
        self.assertEqual(
            plugin.get_search_channel_order(), ["pansou", "hdhive", "mikan"]
        )

    def test_tg_rules_from_store(self):
        plugin, _ = self._plugin_with_store()
        store, _, _, _ = _seed_store(plugin._channels._dir)
        plugin._channels = store
        rules = plugin._tg_channel_rules_from_store()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["channel"], "@yunpanx")
        self.assertEqual(rules[0]["keywords"], ["4k"])
        self.assertEqual(rules[0]["scan_limit"], 20)
        self.assertTrue(rules[0]["auto_transfer"])
        self.assertEqual(rules[0]["monitor_mode"], "subscriptions")

    def test_disable_search_channel(self):
        plugin, _ = self._plugin_with_store()
        store, pansou, _, _ = _seed_store(plugin._channels._dir)
        plugin._channels = store
        plugin._search_source_order = ["pansou"]
        self.assertTrue(plugin.disable_search_channel("pansou"))
        self.assertFalse(store.get(pansou["id"])["enabled"])
        self.assertEqual(plugin._search_source_order, [])
        plugin._search_handler.clear_search_cache.assert_called_once_with()
        self.assertFalse(plugin.disable_search_channel("pansou"))
        self.assertFalse(plugin.disable_search_channel("nope"))

    def test_migrate_channels_from_config(self):
        plugin, directory = self._plugin_with_store()
        added = plugin._migrate_channels_from_config({
            "search_source_order": ["hdhive"],
            "tg_channel_rules": [{"channel": "@a", "keywords": ["4k"]}],
        })
        self.assertEqual(added, 4)
        by_type = {
            item["type"]: item for item in plugin._channels.list()
        }
        self.assertTrue(by_type["hdhive"]["enabled"])
        self.assertFalse(by_type["pansou"]["enabled"])
        self.assertIn("tg_channel", [item["type"] for item in plugin._channels.list()])
        # 已落盘后再次迁移不再重复。
        self.assertEqual(plugin._migrate_channels_from_config({}), 0)

    def test_migrate_channels_from_legacy_global_config(self):
        plugin, _ = self._plugin_with_store()
        added = plugin._migrate_channels_from_config({
            "tg_channels": "@a\n@b",
            "tg_monitor_keywords": "4K",
            "tg_monitor_exclude_keywords": "生肉",
            "tg_monitor_scan_limit": 10,
        })
        self.assertEqual(added, 5)
        tg_items = [
            item for item in plugin._channels.list()
            if item["type"] == "tg_channel"
        ]
        self.assertEqual(len(tg_items), 2)
        self.assertEqual(tg_items[0]["config"]["channel"], "@a")
        self.assertEqual(tg_items[0]["config"]["keywords"], ["4k"])
        self.assertEqual(tg_items[0]["config"]["exclude_keywords"], ["生肉"])
        self.assertEqual(tg_items[0]["config"]["scan_limit"], 10)

    def test_refresh_channel_runtime_light(self):
        plugin, _ = self._plugin_with_store()
        plugin._applied_config = {"tg_monitor_enabled": False}
        store, _, _, _ = _seed_store(plugin._channels._dir)
        plugin._channels = store
        plugin._apply_tg_monitor_config = MagicMock()
        plugin._refresh_channel_runtime()
        plugin._search_handler.refresh_enabled_sources.assert_called_once_with()
        plugin._apply_tg_monitor_config.assert_called_once_with(
            {"tg_monitor_enabled": False}
        )
        self.assertEqual(plugin._search_source_order, ["pansou"])

    def test_apply_tg_monitor_config_reads_store_not_config(self):
        plugin, _ = self._plugin_with_store()
        store, _, _, _ = _seed_store(plugin._channels._dir)
        plugin._channels = store
        seen = {}

        class _FakeMonitor:
            def configure(self, **kwargs):
                seen.update(kwargs)

        _tg = _current_tg_module()
        original = _tg.get_channel_monitor
        _tg.get_channel_monitor = lambda owner: _FakeMonitor()
        try:
            # 配置里的规则是旧的/错误的，运行态必须以渠道库为准。
            plugin._apply_tg_monitor_config({
                "tg_monitor_enabled": True,
                "tg_api_id": 1,
                "tg_api_hash": "h",
                "tg_session": "s",
                "tg_channel_rules": [{"channel": "@stale"}],
            })
        finally:
            _tg.get_channel_monitor = original
        rules = seen.get("channel_rules") or []
        self.assertEqual([rule["channel"] for rule in rules], ["@yunpanx"])


if __name__ == "__main__":
    unittest.main()
