"""搜索渠道能力位（频道监控并入搜索渠道 · P1 底料）。

覆盖：频道监控声明为 push/notify 渠道、不出现在搜索组装里、
渠道目录对外暴露 capabilities。
"""

import unittest

from plugin_env import install_app_mocks, load_module

install_app_mocks(
    "app.log",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.search",
)

core_definitions = load_module(
    "cloudsubscribex.core.definitions",
    "plugins.v2/cloudsubscribex/core/definitions.py",
)
scanner_module = load_module(
    "cloudsubscribex.search.scanner",
    "plugins.v2/cloudsubscribex/search/scanner.py",
)
tg_definition = load_module(
    "cloudsubscribex.search.tgchannels.definition",
    "plugins.v2/cloudsubscribex/search/tgchannels/definition.py",
)

SearchSourceDefinition = core_definitions.SearchSourceDefinition
TgChannelsSourceDefinition = tg_definition.TgChannelsSourceDefinition


class _QuerySource(SearchSourceDefinition):
    id = "fake-query"
    name = "查询渠道"


class _PushSource(SearchSourceDefinition):
    id = "fake-push"
    name = "推送渠道"
    capabilities = frozenset({"push", "notify"})

    @classmethod
    def create_provider(cls, service, client, config, context=None):
        return None


class TestCapabilities(unittest.TestCase):

    def test_default_is_query_only(self):
        self.assertTrue(_QuerySource.has_capability("query"))
        self.assertFalse(_QuerySource.has_capability("push"))

    def test_tg_channels_is_push_notify_only(self):
        self.assertEqual(
            set(TgChannelsSourceDefinition.capabilities), {"push", "notify"}
        )
        self.assertFalse(TgChannelsSourceDefinition.has_capability("query"))

    def test_tg_channels_has_no_provider(self):
        self.assertIsNone(
            TgChannelsSourceDefinition.create_provider(None, None, {})
        )

    def test_tg_channels_declares_no_config_keys(self):
        # 配置仍由插件「频道监控」分组维护，避免两处表单写同一批 tg_* 键
        self.assertEqual(TgChannelsSourceDefinition.get_config_groups(), [])
        self.assertEqual(TgChannelsSourceDefinition.get_config_keys(), frozenset())


class TestCatalogExposesCapabilities(unittest.TestCase):

    def test_catalog_contains_capabilities(self):
        registry_cls = scanner_module.SearchSourceRegistry
        original = registry_cls.get_definitions.__func__

        def _fake_definitions(cls):
            return [_QuerySource, TgChannelsSourceDefinition]

        try:
            scanner_module.SearchSourceRegistry.get_definitions = classmethod(
                lambda cls: [_QuerySource, TgChannelsSourceDefinition]
            )
            catalog = scanner_module.SearchSourceRegistry.get_source_catalog()
        finally:
            scanner_module.SearchSourceRegistry.get_definitions = classmethod(original)
        by_key = {item["key"]: item for item in catalog}
        self.assertEqual(by_key["fake-query"]["capabilities"], ["query"])
        self.assertEqual(
            by_key["tgchannels"]["capabilities"], ["notify", "push"]
        )


if __name__ == "__main__":
    unittest.main()
