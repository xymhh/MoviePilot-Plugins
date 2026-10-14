"""1.6.40 回归：资源页与订阅搜索共用「资源类型优先级」口径。

用户反馈（1.6.39 真机）：资源类型只勾了移动云盘，资源页的 PanSou 渠道却显示所有类型。

根因：资源页走 ``resource_list_mode``，出站按设计请求**全部 9 种类型**，
后端也只按 ``SUPPORTED_RESOURCE_TYPES`` 过滤，两处都不看「资源类型优先级」。
现在出站与入站统一用共享的 ``configured_resource_types``：只保留配置里勾选、
且插件支持的类型；配置为空时退回全部支持类型（不误伤没配过该字段的用户）。

订阅搜索的既有收窄（配置 ∩ 目标盘能力）保持原样，本测试一并锁定。
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    load_module,
    register_mock,
    register_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


class _MediaType:
    MOVIE = "movie"
    TV = "tv"


register_module("cloudsubscribe", package=True)
register_module("cloudsubscribe.search", package=True)
types_module = load_module(
    "cloudsubscribe.search.types",
    "plugins.v2/cloudsubscribe/search/types.py",
)

register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True, MediaInfo=MagicMock())
register_module("app.schemas.types", MediaType=_MediaType)

register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator, SearchQuery=MagicMock())
register_mock("cloudsubscribe.search.magnet", clear_cache=MagicMock(), normalize_magnets=MagicMock())
register_mock("cloudsubscribe.search.matching", normalize_season=MagicMock())

PanSouSearchService = load_module(
    "cloudsubscribe.search.pansou.service",
    "plugins.v2/cloudsubscribe/search/pansou/service.py",
).PanSouSearchService

configured_resource_types = types_module.configured_resource_types
PANSOU_RESOURCE_TYPES = types_module.PANSOU_RESOURCE_TYPES


class _FakeClient:
    """记录每次请求的入参，返回空结果（触发一次降级请求也无妨）。"""

    def __init__(self):
        self.calls = []

    def request_search(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": []}


def _owner(config=(), drive_types=(), drive_key=""):
    owner = types.SimpleNamespace(
        _resource_type_order_config=tuple(config),
        _cloud_drive_resource_types=tuple(drive_types),
        _cloud_drive_key=drive_key,
        _pansou_client=None,
    )
    return owner


def _query(resource_list_mode, subscribe=None):
    mediainfo = types.SimpleNamespace(title="繁花", year="2023", original_title="")
    return types.SimpleNamespace(
        mediainfo=mediainfo,
        media_type=_MediaType.MOVIE,
        season=None,
        result_limit=50,
        resource_list_mode=resource_list_mode,
        subscribe=subscribe,
        keyword=None,
    )


class TestConfiguredResourceTypes(unittest.TestCase):
    """共享口径：配置里勾选的类型（保持顺序），空配置退回全部。"""

    def test_keeps_configured_order(self):
        self.assertEqual(
            configured_resource_types(["115", "yun139"]), ["115", "yun139"]
        )

    def test_normalizes_aliases(self):
        self.assertEqual(
            configured_resource_types(["139", "aliyun"]), ["yun139", "alipan"]
        )

    def test_drops_unsupported_values(self):
        self.assertEqual(
            configured_resource_types(["115", "baidu", ""]), ["115"]
        )

    def test_empty_falls_back_to_all_supported(self):
        self.assertEqual(
            list(configured_resource_types(())), list(types_module.RESOURCE_TYPE_ORDER)
        )
        self.assertEqual(
            list(configured_resource_types(None)), list(types_module.RESOURCE_TYPE_ORDER)
        )


class TestPansouResourcePageScope(unittest.TestCase):
    """资源页出站请求必须按「资源类型优先级」收窄。"""

    def _service(self, owner):
        return PanSouSearchService(
            client=_FakeClient(),
            owner=owner,
            result_limit=50,
            channels=["tg"],
            plugins=["p"],
            filter_config={"k": "v"},
        )

    def test_only_configured_types_requested(self):
        owner = _owner(config=["115", "yun139"])
        service = self._service(owner)
        service.search(_query(resource_list_mode=True))
        request = service._client.calls[0]
        self.assertEqual(request["cloud_types"], ["115", "yun139"], request)

    def test_resource_page_ignores_channels_and_filter(self):
        owner = _owner(config=["115", "yun139"])
        service = self._service(owner)
        service.search(_query(resource_list_mode=True))
        request = service._client.calls[0]
        self.assertEqual(request["channels"], [])
        self.assertEqual(request["plugins"], [])
        self.assertEqual(request["filter_config"], {})
        self.assertEqual(request["response_mode"], "merge")

    def test_empty_config_keeps_discovery_behaviour(self):
        owner = _owner(config=())
        service = self._service(owner)
        service.search(_query(resource_list_mode=True))
        request = service._client.calls[0]
        # 兜底退回全部支持类型（顺序按 RESOURCE_TYPE_ORDER，集合与 PanSou 词表一致）
        self.assertEqual(
            set(request["cloud_types"]), set(PANSOU_RESOURCE_TYPES), request
        )

    def test_alipan_mapped_for_pansou(self):
        owner = _owner(config=["alipan", "yun139"])
        service = self._service(owner)
        service.search(_query(resource_list_mode=True))
        self.assertEqual(service._client.calls[0]["cloud_types"], ["aliyun", "yun139"])


class TestPansouSubscribeScopeUnchanged(unittest.TestCase):
    """订阅搜索仍按「配置 ∩ 目标盘能力」收窄，并保留渠道/过滤参数。"""

    def _service(self, owner):
        return PanSouSearchService(
            client=_FakeClient(),
            owner=owner,
            result_limit=10,
            channels=["tg"],
            plugins=["p"],
            filter_config={"k": "v"},
        )

    def test_intersects_with_drive_capability(self):
        owner = _owner(
            config=["115", "yun139", "quark"],
            drive_types=["115", "yun139"],
            drive_key="115",
        )
        service = self._service(owner)
        service.search(_query(resource_list_mode=False, subscribe=object()))
        request = service._client.calls[0]
        self.assertEqual(request["cloud_types"], ["115", "yun139"], request)
        self.assertEqual(request["response_mode"], "results")
        self.assertEqual(request["channels"], ["tg"])

    def test_no_supported_type_returns_empty(self):
        owner = _owner(
            config=["yun139"],
            drive_types=["115"],
            drive_key="115",
        )
        service = self._service(owner)
        self.assertEqual(
            service.search(_query(resource_list_mode=False, subscribe=object())), []
        )
        self.assertEqual(service._client.calls, [])


if __name__ == "__main__":
    unittest.main()
