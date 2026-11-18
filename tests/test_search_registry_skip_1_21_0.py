"""搜索源瘦身 v1.21.0：注册表跳过原因与来源目录标注。

覆盖：create_search_registry 对三种跳过原因分别打 info 日志
（no-query-capability / not-configured / registration-failed），
且 get_source_catalog() 给每个来源标注 disabled_reason（已注册为 None）。
"""

import logging
import sys
import unittest

from plugin_env import (
    install_app_mocks,
    load_module,
)

install_app_mocks(
    "app.log",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.search",
)

_SAVED_MODULES = {}
for _name in (
    "cloudsubscribex.core.search",
    "cloudsubscribex.core.definitions",
    "cloudsubscribex.search.provider",
    "cloudsubscribex.search.scanner",
    "cloudsubscribex.search.registry",
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
definitions = load_module(
    "cloudsubscribex.core.definitions",
    "plugins.v2/cloudsubscribex/core/definitions.py",
)
provider_module = load_module(
    "cloudsubscribex.search.provider",
    "plugins.v2/cloudsubscribex/search/provider.py",
)
scanner_module = load_module(
    "cloudsubscribex.search.scanner",
    "plugins.v2/cloudsubscribex/search/scanner.py",
)
registry_module = load_module(
    "cloudsubscribex.search.registry",
    "plugins.v2/cloudsubscribex/search/registry.py",
)
# 注册表从 app.log 取 logger（测试里是 MagicMock）；换成真 logger 才能断言级别。
registry_module.logger = logging.getLogger("app.plugins.cloudsubscribex")


class _FakeService:
    def search(self, query):
        return []

    def clear_cache(self):
        return 0


class _Base(definitions.SearchSourceDefinition):
    capabilities = frozenset({"query"})

    @classmethod
    def create_client(cls, config, context):
        return object()

    @classmethod
    def create_service(cls, client, config, context):
        return _FakeService()

    @classmethod
    def create_provider(cls, service, client, config, context):
        return provider_module.build_search_provider(
            key=cls.id, name=cls.name, resource_types={"magnet"},
            service=service,
            capabilities=provider_module.MAGNET_CAPABILITIES,
        )


class _NoQuery(_Base):
    id = "skip-noquery"
    name = "Skip NoQuery"
    capabilities = frozenset({"push"})


class _Unconfigured(_Base):
    id = "skip-unconfigured"
    name = "Skip Unconfigured"

    @classmethod
    def create_provider(cls, service, client, config, context):
        return None


class _Broken(_Base):
    id = "skip-broken"
    name = "Skip Broken"

    @classmethod
    def create_provider(cls, service, client, config, context):
        raise RuntimeError("boom")


class _Ok(_Base):
    id = "skip-ok"
    name = "Skip Ok"


class RegistrySkipTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        registry_cls = scanner_module.SearchSourceRegistry
        cls._saved_definitions = dict(registry_cls._definitions)
        cls._saved_ordered = registry_cls._ordered_definitions
        cls._saved_outcome = dict(registry_cls._last_registry_outcome)
        for def_cls in (_NoQuery, _Unconfigured, _Broken, _Ok):
            registry_cls.register(def_cls)

    @classmethod
    def tearDownClass(cls):
        registry_cls = scanner_module.SearchSourceRegistry
        registry_cls._definitions = cls._saved_definitions
        registry_cls._ordered_definitions = cls._saved_ordered
        registry_cls._last_registry_outcome = cls._saved_outcome

    def test_skip_reasons_logged_at_info(self):
        with self.assertLogs("app.plugins.cloudsubscribex", level="INFO") as logs:
            registry = registry_module.create_search_registry({})
        output = "\n".join(logs.output)
        self.assertIn("skip-noquery（no-query-capability）", output)
        self.assertIn("skip-unconfigured（not-configured）", output)
        self.assertIn("skip-broken（registration-failed）", output)
        self.assertIn("skip-ok", registry)

    def test_catalog_annotates_disabled_reason(self):
        registry_module.create_search_registry({})
        catalog = scanner_module.SearchSourceRegistry.get_source_catalog()
        by_key = {entry["key"]: entry for entry in catalog}
        self.assertEqual(
            by_key["skip-noquery"]["disabled_reason"], "no-query-capability"
        )
        self.assertEqual(
            by_key["skip-unconfigured"]["disabled_reason"], "not-configured"
        )
        self.assertEqual(
            by_key["skip-broken"]["disabled_reason"], "registration-failed"
        )
        self.assertIsNone(by_key["skip-ok"]["disabled_reason"])


if __name__ == "__main__":
    unittest.main()
