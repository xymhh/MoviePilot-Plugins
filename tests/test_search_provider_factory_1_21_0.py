"""搜索源瘦身 v1.21.0：build_search_provider 与 create_service 接线。

覆盖：14 个源经唯一 builder 产出的 key/name/resource_types/能力表/策略
与旧工厂逐项一致；真实 definition 的 create_service 建 service、
create_provider 用传入的 service（不再自建）；注册表三步调用把 service
透传进 provider；未知能力角色报 ValueError。
"""

import sys
import types
import unittest

from plugin_env import (
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.log",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.search",
)
set_media_type(movie="movie", tv="tv")

_SAVED_MODULES = {}
for _name in (
    "cloudsubscribex.core.search",
    "cloudsubscribex.core.definitions",
    "cloudsubscribex.search.provider",
    "cloudsubscribex.search.scanner",
    "cloudsubscribex.search.registry",
    "cloudsubscribex.search.piratebay.definition",
    "cloudsubscribex.search.juying.definition",
    "cloudsubscribex.search.pansou.definition",
    "cloudsubscribex.search.online_docs.definition",
    "cloudsubscribex.search.dian115.definition",
):
    _SAVED_MODULES[_name] = sys.modules.get(_name)


def tearDownModule():
    for _name, _mod in _SAVED_MODULES.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


core_search = load_module(
    "cloudsubscribex.core.search",
    "plugins.v2/cloudsubscribex/core/search.py",
)
SearchCapability = core_search.SearchCapability
SearchPolicy = core_search.SearchPolicy
load_module(
    "cloudsubscribex.core.definitions",
    "plugins.v2/cloudsubscribex/core/definitions.py",
)
provider_module = load_module(
    "cloudsubscribex.search.provider",
    "plugins.v2/cloudsubscribex/search/provider.py",
)
build_search_provider = provider_module.build_search_provider
MAGNET_CAPABILITIES = provider_module.MAGNET_CAPABILITIES
RESOLVABLE_MAGNET_CAPABILITIES = provider_module.RESOLVABLE_MAGNET_CAPABILITIES
DELEGATING_CAPABILITIES = provider_module.DELEGATING_CAPABILITIES
PIANKU_CAPABILITIES = provider_module.PIANKU_CAPABILITIES
POINTS_CAPABILITIES = provider_module.POINTS_CAPABILITIES
POINTS_PREVIEW_CAPABILITIES = provider_module.POINTS_PREVIEW_CAPABILITIES
PINGLIAN_CAPABILITIES = provider_module.PINGLIAN_CAPABILITIES


class FakeBudget:
    def configure_storage(self, get_data, save_data):
        pass

    def reset_task(self):
        pass

    def reset_subscription(self, key=""):
        return 0

    def clear_subscription(self, key):
        return False

    def has_budget(self, points):
        return True

    def clear_history(self):
        return 0

    def clear_cached_urls(self):
        return 0


class FakeService:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def search(self, query):
        return []

    def resolve(self, **kwargs):
        return {}

    def preview(self, candidate):
        return {}

    def unlock(self, candidate, search_label=""):
        return {}

    def clear_cache(self):
        return 0

    def close(self):
        pass


class FakeClient:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)

    def get_account_info(self):
        return {}

    def checkin(self, mode="normal"):
        return {}

    def close(self):
        pass


def _points_service():
    budget = FakeBudget()
    client = FakeClient()
    return FakeService(
        budget=budget,
        resource_types=frozenset({"magnet", "115"}),
        cache_context={"v": 2},
        get_client=lambda: client,
    ), client, budget


class BuildProviderTests(unittest.TestCase):

    def test_magnet_basic_sources(self):
        for key, name in [
            ("piratebay", "海盗湾"),
            ("uindex", "UIndex"),
            ("animegarden", "AnimeGarden"),
            ("mikan", "Mikan"),
        ]:
            service = FakeService()
            provider = build_search_provider(
                key=key, name=name, resource_types={"magnet"},
                service=service, capabilities=MAGNET_CAPABILITIES,
                policy=SearchPolicy(cache_context={"result_limit": 10}),
            )
            self.assertEqual(provider.key, key)
            self.assertEqual(provider.name, name)
            self.assertEqual(provider.resource_types, {"magnet"})
            self.assertEqual(
                set(provider.services),
                {SearchCapability.RESOURCE_SEARCH, SearchCapability.CACHE_MAINTENANCE},
            )
            self.assertIs(provider.services[SearchCapability.RESOURCE_SEARCH], service)
            self.assertTrue(provider.policy.cacheable)
            self.assertEqual(provider.policy.cache_context, {"result_limit": 10})

    def test_seedhub_adds_resolve(self):
        service = FakeService()
        provider = build_search_provider(
            key="seedhub", name="SeedHub", resource_types={"magnet"},
            service=service, capabilities=RESOLVABLE_MAGNET_CAPABILITIES,
            policy=SearchPolicy(cache_context={"result_limit": 20}),
        )
        self.assertIn(SearchCapability.RESOURCE_RESOLVE, provider.services)
        self.assertIs(provider.services[SearchCapability.RESOURCE_RESOLVE], service)

    def test_delegating_sources(self):
        for key, name in [("juying", "聚影"), ("woniu", "蜗牛")]:
            service = FakeService()
            client = FakeClient()
            provider = build_search_provider(
                key=key, name=name, resource_types=("magnet", "115"),
                service=service, client=client,
                capabilities=DELEGATING_CAPABILITIES,
                policy=SearchPolicy(
                    cacheable=False,
                    cache_context={"result_limit": 10, "resource_types": ["magnet", "115"]},
                ),
            )
            self.assertEqual(provider.resource_types, {"magnet", "115"})
            self.assertEqual(
                set(provider.services),
                {
                    SearchCapability.RESOURCE_SEARCH,
                    SearchCapability.RESOURCE_RESOLVE,
                    SearchCapability.ACCOUNT,
                    SearchCapability.CHECKIN,
                    SearchCapability.CACHE_MAINTENANCE,
                    SearchCapability.LIFECYCLE,
                },
            )
            self.assertIs(provider.services[SearchCapability.ACCOUNT], client)
            self.assertIs(provider.services[SearchCapability.LIFECYCLE], client)
            self.assertIs(provider.services[SearchCapability.RESOURCE_SEARCH], service)
            self.assertFalse(provider.policy.cacheable)

    def test_pianku_lifecycle_is_service(self):
        service = FakeService()
        provider = build_search_provider(
            key="pianku", name="片库",
            resource_types={"magnet", "ed2k", "quark"},
            service=service, capabilities=PIANKU_CAPABILITIES,
            policy=SearchPolicy(cache_context={"result_limit": 20, "detail_limit": 3}),
        )
        self.assertIs(provider.services[SearchCapability.LIFECYCLE], service)
        self.assertEqual(provider.policy.cache_context["detail_limit"], 3)

    def test_pansou_and_online_docs(self):
        service = FakeService()
        pansou = build_search_provider(
            key="pansou", name="PanSou", resource_types=("115", "quark"),
            service=service, capabilities=MAGNET_CAPABILITIES,
            policy=SearchPolicy(cache_context={
                "channels": ["c1"], "plugins": ["p1"], "concurrency": 4,
                "result_limit": 10, "refresh": True,
            }),
        )
        self.assertEqual(pansou.resource_types, {"115", "quark"})
        self.assertEqual(pansou.policy.cache_context["channels"], ["c1"])
        docs = build_search_provider(
            key="online_docs", name="在线文档", resource_types=("pdf",),
            service=service, capabilities=MAGNET_CAPABILITIES,
        )
        self.assertEqual(docs.policy, SearchPolicy())

    def test_points_sources_share_one_table(self):
        # dian115 / hdhaven / hdhive 共用 POINTS_CAPABILITIES；
        # hdhaven / hdhive 在此基础上多一个 RESOURCE_PREVIEW。
        for key, name, capabilities, cache_empty in [
            ("dian115", "Dian115", POINTS_CAPABILITIES, True),
            ("hdhaven", "HDHaven", POINTS_PREVIEW_CAPABILITIES, False),
            ("hdhive", "HDHive", POINTS_PREVIEW_CAPABILITIES, False),
        ]:
            service, client, budget = _points_service()
            provider = build_search_provider(
                key=key, name=name, resource_types=service.resource_types,
                service=service, client=client, capabilities=capabilities,
                policy=SearchPolicy(
                    cacheable=True, cache_empty_results=cache_empty,
                    cache_context=service.cache_context, max_concurrency=1,
                ),
            )
            self.assertEqual(provider.resource_types, {"magnet", "115"})
            self.assertIs(provider.services[SearchCapability.ACCOUNT], client)
            self.assertIs(provider.services[SearchCapability.POINT_BUDGET], budget)
            self.assertIs(provider.services[SearchCapability.LIFECYCLE], service)
            self.assertEqual(provider.policy.cache_empty_results, cache_empty)
            self.assertEqual(provider.policy.max_concurrency, 1)
        self.assertNotIn(
            SearchCapability.RESOURCE_PREVIEW,
            POINTS_CAPABILITIES,
        )
        self.assertIn(
            SearchCapability.RESOURCE_PREVIEW,
            POINTS_PREVIEW_CAPABILITIES,
        )

    def test_pinglian_lifecycle_is_client(self):
        service = FakeService(budget=FakeBudget())
        client = FakeClient()
        provider = build_search_provider(
            key="pinglian", name="盘链", resource_types=("115",),
            service=service, client=client,
            capabilities=PINGLIAN_CAPABILITIES,
            policy=SearchPolicy(cache_context={"result_limit": 10}),
        )
        self.assertIs(provider.services[SearchCapability.LIFECYCLE], client)
        self.assertIs(
            provider.services[SearchCapability.POINT_BUDGET], service.budget
        )
        self.assertIn(SearchCapability.RESOURCE_UNLOCK, provider.services)

    def test_unknown_role_raises(self):
        with self.assertRaises(ValueError):
            build_search_provider(
                key="x", name="X", resource_types={"magnet"},
                service=FakeService(),
                capabilities={SearchCapability.RESOURCE_SEARCH: "nope"},
            )


def _mock_submodule(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class RecordingService:
    """记录构造参数的 service 替身。"""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def search(self, query):
        return []

    def resolve(self, **kwargs):
        return {}

    def preview(self, candidate):
        return {}

    def unlock(self, candidate, search_label=""):
        return {}

    def clear_cache(self):
        return 0

    def close(self):
        pass


class DefinitionWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 全量套件里更早的文件会把 cloudsubscribex.core.search 注册成 mock；
        # 用例执行时 definition 的 `from ...core.search import` 会撞上那个桩，
        # 必须在加载 definition 之前先把真实模块重载回来，跑完再恢复。
        cls._saved_sys_modules = {}
        for _name in (
            "cloudsubscribex.core.search",
            "cloudsubscribex.core.definitions",
            "cloudsubscribex.search.provider",
            "cloudsubscribex.search.piratebay.client",
            "cloudsubscribex.search.piratebay.service",
            "cloudsubscribex.search.juying.client",
            "cloudsubscribex.search.juying.resource",
            "cloudsubscribex.search.juying.service",
            "cloudsubscribex.search.pansou.client",
            "cloudsubscribex.search.pansou.service",
            "cloudsubscribex.search.online_docs.client",
            "cloudsubscribex.search.online_docs.service",
            "cloudsubscribex.search.dian115.client",
            "cloudsubscribex.search.dian115.service",
        ):
            cls._saved_sys_modules[_name] = sys.modules.get(_name)
        load_module(
            "cloudsubscribex.core.search",
            "plugins.v2/cloudsubscribex/core/search.py",
        )
        load_module(
            "cloudsubscribex.core.definitions",
            "plugins.v2/cloudsubscribex/core/definitions.py",
        )
        load_module(
            "cloudsubscribex.search.provider",
            "plugins.v2/cloudsubscribex/search/provider.py",
        )
        # 真实 definition + 假 client/service 子模块（只验证接线，不加载真实客户端）。
        ensure_package("cloudsubscribex.search.piratebay")
        _mock_submodule(
            "cloudsubscribex.search.piratebay.client",
            PirateBayClient=type("PirateBayClient", (), {}),
            PirateBayError=type("PirateBayError", (RuntimeError,), {}),
        )
        _mock_submodule(
            "cloudsubscribex.search.piratebay.service",
            PirateBaySearchService=RecordingService,
        )
        cls.piratebay = load_module(
            "cloudsubscribex.search.piratebay.definition",
            "plugins.v2/cloudsubscribex/search/piratebay/definition.py",
        ).PirateBaySourceDefinition

        ensure_package("cloudsubscribex.search.juying")
        _mock_submodule(
            "cloudsubscribex.search.juying.client",
            JuyingClient=type("JuyingClient", (), {}),
            JuyingError=type("JuyingError", (RuntimeError,), {}),
        )
        _mock_submodule(
            "cloudsubscribex.search.juying.resource",
            JuyingResourceService=RecordingService,
        )
        _mock_submodule(
            "cloudsubscribex.search.juying.service",
            JuyingSearchService=RecordingService,
        )
        cls.juying = load_module(
            "cloudsubscribex.search.juying.definition",
            "plugins.v2/cloudsubscribex/search/juying/definition.py",
        ).JuyingSourceDefinition

        ensure_package("cloudsubscribex.search.pansou")
        _mock_submodule(
            "cloudsubscribex.search.pansou.client",
            PanSouClient=type("PanSouClient", (), {}),
        )
        _mock_submodule(
            "cloudsubscribex.search.pansou.service",
            PanSouSearchService=RecordingService,
        )
        cls.pansou = load_module(
            "cloudsubscribex.search.pansou.definition",
            "plugins.v2/cloudsubscribex/search/pansou/definition.py",
        ).PanSouSourceDefinition

        ensure_package("cloudsubscribex.search.online_docs")
        _mock_submodule(
            "cloudsubscribex.search.online_docs.client",
            OnlineDocumentClient=type("OnlineDocumentClient", (), {}),
        )
        _mock_submodule(
            "cloudsubscribex.search.online_docs.service",
            OnlineDocumentSearchService=RecordingService,
        )
        cls.online_docs = load_module(
            "cloudsubscribex.search.online_docs.definition",
            "plugins.v2/cloudsubscribex/search/online_docs/definition.py",
        ).OnlineDocsSourceDefinition

        ensure_package("cloudsubscribex.search.dian115")
        _mock_submodule(
            "cloudsubscribex.search.dian115.client",
            Dian115Error=type("Dian115Error", (RuntimeError,), {}),
        )

        class FakeDian115Service(RecordingService):
            def __init__(self, owner):
                super().__init__(owner)
                self.owner = owner
                self.budget = FakeBudget()

            @property
            def available(self):
                return True

            @property
            def resource_types(self):
                return frozenset({"magnet"})

            @property
            def cache_context(self):
                return {"v": 2}

            def get_client(self):
                return FakeClient()

        _mock_submodule(
            "cloudsubscribex.search.dian115.service",
            Dian115SearchService=FakeDian115Service,
        )
        cls.dian115 = load_module(
            "cloudsubscribex.search.dian115.definition",
            "plugins.v2/cloudsubscribex/search/dian115/definition.py",
        ).Dian115SourceDefinition

    @classmethod
    def tearDownClass(cls):
        for _name, _mod in cls._saved_sys_modules.items():
            if _mod is None:
                sys.modules.pop(_name, None)
            else:
                sys.modules[_name] = _mod

    def test_piratebay_create_service_then_provider(self):
        client = FakeClient()
        service = self.piratebay.create_service(
            client, {"piratebay_result_limit": 7}, {}
        )
        self.assertIsInstance(service, RecordingService)
        self.assertEqual(service.args, (client, 7))
        provider = self.piratebay.create_provider(
            service, client, {"piratebay_result_limit": 7}, {}
        )
        self.assertEqual(provider.key, "piratebay")
        self.assertIs(provider.services[SearchCapability.RESOURCE_SEARCH], service)
        self.assertEqual(provider.policy.cache_context, {"result_limit": 7})

    def test_piratebay_provider_rejects_missing_service(self):
        self.assertIsNone(
            self.piratebay.create_provider(None, FakeClient(), {}, {})
        )
        self.assertIsNone(self.piratebay.create_service(None, {}, {}))

    def test_juying_create_service_builds_resources(self):
        client = FakeClient(is_configured=True)
        service = self.juying.create_service(
            client, {"juying_result_limit": 5},
            {"resource_types": ("magnet", "115")},
        )
        self.assertIsInstance(service, RecordingService)
        # service 构造参数：client、资源服务、资源类型、上限。
        self.assertEqual(service.args[0], client)
        self.assertIsInstance(service.args[1], RecordingService)
        self.assertEqual(service.args[2], ("magnet", "115"))
        self.assertEqual(service.args[3], 5)
        provider = self.juying.create_provider(
            service, client, {"juying_result_limit": 5},
            {"resource_types": ("magnet", "115")},
        )
        self.assertEqual(provider.key, "juying")
        self.assertFalse(provider.policy.cacheable)
        self.assertEqual(
            provider.policy.cache_context,
            {"result_limit": 5, "resource_types": ["magnet", "115"]},
        )
        self.assertIs(provider.services[SearchCapability.ACCOUNT], client)

    def test_juying_gate_blocks_unconfigured_client(self):
        client = FakeClient(is_configured=False)
        self.assertIsNone(self.juying.create_service(
            client, {}, {"resource_types": ("magnet",)}
        ))

    def test_pansou_create_service_uses_config(self):
        client = FakeClient()
        config = {
            "pansou_channels": ["c1", " c2 "],
            "pansou_plugins": ["p1"],
            "pansou_filter_include": ["a"],
            "pansou_filter_exclude": [],
            "pansou_concurrency": "4",
            "pansou_result_limit": 10,
            "pansou_refresh": False,
        }
        service = self.pansou.create_service(
            client, config, {"resource_types": ("115", "quark")}
        )
        self.assertEqual(service.kwargs["channels"], ["c1", "c2"])
        self.assertEqual(service.kwargs["plugins"], ["p1"])
        self.assertEqual(service.kwargs["concurrency"], 4)
        self.assertEqual(service.kwargs["result_limit"], 10)
        self.assertIs(service.kwargs["refresh"], False)
        owner = types.SimpleNamespace()
        provider = self.pansou.create_provider(
            service, client, config,
            {"owner": owner, "resource_types": ("115", "quark")},
        )
        self.assertEqual(provider.key, "pansou")
        self.assertIs(owner._pansou_client, client)
        self.assertEqual(
            provider.policy.cache_context["channels"], ["c1", "c2"]
        )

    def test_online_docs_provider_uses_default_policy(self):
        client = FakeClient()
        service = self.online_docs.create_service(client, {}, {})
        self.assertIsInstance(service, RecordingService)
        provider = self.online_docs.create_provider(
            service, client, {}, {"resource_types": ("pdf",)}
        )
        self.assertEqual(provider.key, "online_docs")
        # 注意：定义在 setUpClass 里取的是最后加载进 sys.modules 的
        # provider 模块，其 SearchPolicy 类与本文件的不是同一对象，
        # dataclass 按类判等会 False；只比字段。
        default = SearchPolicy()
        for field in (
            "cacheable", "cache_empty_results", "cache_context",
            "max_concurrency",
        ):
            self.assertEqual(
                getattr(provider.policy, field), getattr(default, field),
                field,
            )
        self.assertIsNone(self.online_docs.create_provider(
            service, client, {}, {"resource_types": ()}
        ))

    def test_dian115_create_service_from_owner(self):
        owner = types.SimpleNamespace()
        service = self.dian115.create_service(None, {}, {"owner": owner})
        self.assertIsNotNone(service)
        self.assertIs(service.owner, owner)
        provider = self.dian115.create_provider(service, None, {}, {})
        self.assertEqual(provider.key, "dian115")
        self.assertIs(
            provider.services[SearchCapability.POINT_BUDGET], service.budget
        )
        self.assertEqual(provider.policy.cache_context, {"v": 2})
        self.assertEqual(provider.policy.max_concurrency, 1)
        self.assertIsNone(self.dian115.create_service(None, {}, {}))


class RegistryWiringTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 与 DefinitionWiringTests 同理：先把真实核心模块重载回来再加载
        # scanner/registry，跑完恢复，避免影响后续文件。
        cls._saved_sys_modules = {}
        for _name in (
            "cloudsubscribex.core.search",
            "cloudsubscribex.core.definitions",
            "cloudsubscribex.search.provider",
            "cloudsubscribex.search.scanner",
            "cloudsubscribex.search.registry",
        ):
            cls._saved_sys_modules[_name] = sys.modules.get(_name)
        load_module(
            "cloudsubscribex.core.search",
            "plugins.v2/cloudsubscribex/core/search.py",
        )
        load_module(
            "cloudsubscribex.core.definitions",
            "plugins.v2/cloudsubscribex/core/definitions.py",
        )
        load_module(
            "cloudsubscribex.search.provider",
            "plugins.v2/cloudsubscribex/search/provider.py",
        )
        cls.scanner_module = load_module(
            "cloudsubscribex.search.scanner",
            "plugins.v2/cloudsubscribex/search/scanner.py",
        )
        cls.registry_module = load_module(
            "cloudsubscribex.search.registry",
            "plugins.v2/cloudsubscribex/search/registry.py",
        )
        cls._saved_definitions = dict(
            cls.scanner_module.SearchSourceRegistry._definitions
        )
        cls._saved_ordered = cls.scanner_module.SearchSourceRegistry._ordered_definitions
        cls._saved_outcome = dict(
            cls.scanner_module.SearchSourceRegistry._last_registry_outcome
        )

    @classmethod
    def tearDownClass(cls):
        registry_cls = cls.scanner_module.SearchSourceRegistry
        registry_cls._definitions = cls._saved_definitions
        registry_cls._ordered_definitions = cls._saved_ordered
        registry_cls._last_registry_outcome = cls._saved_outcome
        for _name, _mod in cls._saved_sys_modules.items():
            if _mod is None:
                sys.modules.pop(_name, None)
            else:
                sys.modules[_name] = _mod

    def test_registry_passes_service_through(self):
        from plugin_env import load_module as _load

        definitions = _load(
            "cloudsubscribex.core.definitions",
            "plugins.v2/cloudsubscribex/core/definitions.py",
        )
        created = {}

        class FakeDef(definitions.SearchSourceDefinition):
            id = "fake-through"
            name = "Fake Through"

            @classmethod
            def create_client(cls, cfg, ctx):
                return FakeClient()

            @classmethod
            def create_service(cls, client, cfg, ctx):
                service = FakeService()
                created["service"] = service
                return service

            @classmethod
            def create_provider(cls, service, client, cfg, ctx):
                return build_search_provider(
                    key="fake-through", name="Fake Through",
                    resource_types={"magnet"}, service=service,
                    capabilities=MAGNET_CAPABILITIES,
                )

        self.scanner_module.SearchSourceRegistry.register(FakeDef)
        registry = self.registry_module.create_search_registry({})
        self.assertIn("fake-through", registry)
        provider = registry.get("fake-through")
        self.assertIs(
            provider.services[SearchCapability.RESOURCE_SEARCH],
            created["service"],
        )


if __name__ == "__main__":
    unittest.main()
