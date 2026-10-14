"""移动云盘（PanSou 接口侧类型键 ``mobile``）搜索与跨盘转存的回归测试。

复现问题：资源类型优先级设为「115 + 移动云盘」时，实际搜索结果里从来没有移动云盘，
全是夸克/阿里/磁力等其它类型。根因两条：

1. PanSou 接口侧的类型键与插件内部键不同名：移动云盘在接口侧叫 ``mobile``
   （阿里云盘叫 ``aliyun``），插件内部叫 ``yun139``/``alipan``。
   出站请求带着接口不认识的 ``yun139``（``res=merge`` 时接口按词表过滤，整桶丢弃），
   入站又拿原样的 ``mobile`` 去和内部允许集比对（必然不匹配）→ 移动云盘永远进不了候选。
2. ``CloudDriveManager`` 只读不写 ``_cloud_drive_resource_types``，订阅搜索收窄允许类型时
   只能走「目标盘自身 + 直通链接」的老兜底 ``{115, magnet, ed2k}``，需要跨盘转存的
   移动云盘被砍掉。

本测试固定：出站类型键映射、入站结果归一化、能力过滤回写、订阅搜索的收窄链。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribe", *parts)


def _register_real_package(name, path):
    """注册带真实 ``__path__`` 的包占位，供其子模块按文件加载。"""
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True, MediaInfo=MagicMock)
media_type = set_media_type()

# ---- cloudsubscribe 包层级（core / search 用真实路径，便于子模块按文件加载）----
plugin_package = _register_real_package("cloudsubscribe", _plugin_dir())
register_module("cloudsubscribe.core", package=True)
sys.modules["cloudsubscribe.core"].__path__ = [_plugin_dir("core")]
sys.modules["cloudsubscribe.core"].SearchQuery = MagicMock()
_register_real_package("cloudsubscribe.search", _plugin_dir("search"))
_register_real_package("cloudsubscribe.drive", _plugin_dir("drive"))
register_mock(
    "cloudsubscribe.search.magnet",
    clear_cache=MagicMock(),
    normalize_magnets=lambda candidates, source=None: candidates,
)
register_mock("cloudsubscribe.search.matching", normalize_season=MagicMock(return_value=None))

# 真实类型定义表：归一化与展示名必须是线上那一份
types_module = load_module(
    "cloudsubscribe.search.types",
    "plugins.v2/cloudsubscribe/search/types.py",
)

# 网盘能力/跨盘任务/驱动扫描器都用真实模块（同进程内其它测试可能已把它们注册成
# Mock，这里显式按文件加载覆盖，保证能力枚举是真实的）。
load_module("cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py")
load_module("cloudsubscribe.core.transfer", "plugins.v2/cloudsubscribe/core/transfer.py")
load_module("cloudsubscribe.drive.scanner", "plugins.v2/cloudsubscribe/drive/scanner.py")

# 出站请求捕获
_SEARCH_PAYLOADS = []
_GATED_RESPONSES = []


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _fake_gated_request(gate, func, url, **kwargs):
    _SEARCH_PAYLOADS.append(kwargs.get("json") or {})
    if _GATED_RESPONSES:
        return _GATED_RESPONSES.pop(0)
    return _FakeResponse({"code": 0, "data": {"total": 0, "results": []}})


import requests  # noqa: E402  —— 客户端要引用 requests.exceptions.Timeout

register_mock(
    "cloudsubscribe.search.http_client",
    RequestGate=MagicMock(),
    gated_request=_fake_gated_request,
    normalize_proxies=lambda value: value,
    requests=requests,
)
_register_real_package("cloudsubscribe.search.pansou", _plugin_dir("search", "pansou"))

client_module = load_module(
    "cloudsubscribe.search.pansou.client",
    "plugins.v2/cloudsubscribe/search/pansou/client.py",
)
service_module = load_module(
    "cloudsubscribe.search.pansou.service",
    "plugins.v2/cloudsubscribe/search/pansou/service.py",
)
drive_manager_module = load_module(
    "cloudsubscribe.core.drive_manager",
    "plugins.v2/cloudsubscribe/core/drive_manager.py",
)

PanSouClient = client_module.PanSouClient
PanSouSearchService = service_module.PanSouSearchService
CloudDriveManager = drive_manager_module.CloudDriveManager
CloudDriveCapability = drive_manager_module.CloudDriveCapability


class _FakeQuery:
    """极致精简的 SearchQuery 替身。"""

    def __init__(self, mediainfo, subscribe=True, resource_list_mode=False, result_limit=10):
        self.mediainfo = mediainfo
        self.media_type = media_type.MOVIE
        self.season = None
        self.subscribe = subscribe
        self.resource_list_mode = resource_list_mode
        self.result_limit = result_limit


class _FakeMediaInfo:
    def __init__(self, title="繁花", year="2023"):
        self.title = title
        self.original_title = ""
        self.cn_name = ""
        self.en_name = ""
        self.year = year


class _FakeSearchClient:
    """记录出站 cloud_types 并返回固定结果。"""

    def __init__(self, rows=None):
        self.calls = []
        self.rows = rows if rows is not None else []

    def request_search(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": self.rows, "keyword": kwargs.get("keyword")}


class _FakeOwner:
    def __init__(self, **attrs):
        for key, value in attrs.items():
            setattr(self, key, value)


class TestPanSouTypeVocabulary(unittest.TestCase):
    """插件内部键与 PanSou 接口键的双向映射。"""

    def test_api_aliases_normalize_to_internal_keys(self):
        self.assertEqual(types_module.normalize_resource_type("mobile"), "yun139")
        self.assertEqual(types_module.normalize_resource_type("others"), "other")
        self.assertEqual(types_module.normalize_resource_type("139"), "yun139")

    def test_internal_keys_map_to_api_keys(self):
        self.assertEqual(types_module.to_pansou_api_type("yun139"), "mobile")
        self.assertEqual(types_module.to_pansou_api_type("139"), "mobile")
        self.assertEqual(types_module.to_pansou_api_type("alipan"), "aliyun")
        self.assertEqual(types_module.to_pansou_api_type("aliyun"), "aliyun")
        self.assertEqual(types_module.to_pansou_api_type("115"), "115")

    def test_api_type_vocabulary_contains_mobile_not_yun139(self):
        self.assertIn("mobile", types_module.PANSOU_API_TYPES)
        self.assertNotIn("yun139", types_module.PANSOU_API_TYPES)


class TestPanSouClientOutboundTypes(unittest.TestCase):
    """出站必须发接口认识的键，否则接口整桶丢弃该类型。"""

    def setUp(self):
        _SEARCH_PAYLOADS.clear()
        _GATED_RESPONSES.clear()
        self.client = PanSouClient("https://so.example.com", auth_enabled=False)

    def test_mobile_cloud_sent_as_mobile(self):
        self.client.request_search(keyword="繁花", cloud_types=["115", "yun139"])
        payload = _SEARCH_PAYLOADS[-1]
        self.assertEqual(payload.get("cloud_types"), ["115", "mobile"])

    def test_alipan_sent_as_aliyun(self):
        self.client.request_search(keyword="繁花", cloud_types=["alipan", "115"])
        payload = _SEARCH_PAYLOADS[-1]
        self.assertEqual(payload.get("cloud_types"), ["aliyun", "115"])

    def test_unknown_type_dropped(self):
        self.client.request_search(keyword="繁花", cloud_types=["115", "definitely-unknown"])
        payload = _SEARCH_PAYLOADS[-1]
        self.assertEqual(payload.get("cloud_types"), ["115"])

    def test_merge_response_keeps_api_type_key(self):
        _GATED_RESPONSES.append(_FakeResponse({
            "code": 0,
            "data": {
                "total": 1,
                "merged_by_type": {
                    "mobile": [{"note": "繁花 2023", "url": "https://yun.139.com/shareweb/#/w/i/abc"}],
                },
            },
        }))
        response = self.client.request_search(
            keyword="繁花", cloud_types=["115", "yun139"], response_mode="merge"
        )
        link = response["results"][0]["links"][0]
        self.assertEqual(link.get("type"), "mobile")


class TestPanSouInboundNormalization(unittest.TestCase):
    """接口返回的 ``mobile`` 必须归一化成内部 ``yun139`` 才能进候选。"""

    def setUp(self):
        self.rows = [{
            "title": "繁花 2023 4K 全集",
            "datetime": "2026-10-14 10:00:00",
            "links": [
                {"type": "mobile", "url": "https://yun.139.com/shareweb/#/w/i/abc", "work_title": "繁花 2023 4K 全集"},
                {"type": "quark", "url": "https://pan.quark.cn/s/xyz", "work_title": "繁花 2023 4K 全集"},
            ],
        }]

    def _normalize(self, allowed_types):
        return PanSouSearchService._normalize_results(
            self.rows,
            "繁花 2023",
            ["繁花"],
            "2023",
            allowed_types,
            10,
            strict_year=True,
        )

    def test_mobile_link_kept_and_normalized(self):
        groups = self._normalize(["115", "yun139"])
        self.assertIn("移动云盘", groups)
        candidate = groups["移动云盘"][0]
        self.assertEqual(candidate.get("resource_type"), "yun139")
        self.assertEqual(candidate.get("url"), "https://yun.139.com/shareweb/#/w/i/abc")

    def test_mobile_link_dropped_when_not_allowed(self):
        groups = self._normalize(["115"])
        self.assertNotIn("移动云盘", groups)
        self.assertNotIn("quark", groups)


class TestSubscribeSearchNarrowing(unittest.TestCase):
    """订阅搜索的允许类型收窄必须用上跨盘能力集合。"""

    def _search(self, owner, rows):
        client = _FakeSearchClient(rows=rows)
        service = PanSouSearchService(
            client=client,
            resource_types=("115", "yun139"),
            owner=owner,
            result_limit=10,
        )
        results = service.search(_FakeQuery(_FakeMediaInfo()))
        return client, results

    def test_cross_drive_type_kept_when_capability_known(self):
        owner = _FakeOwner(
            _cloud_drive_key="115",
            _cloud_drive_resource_types=("115", "yun139", "magnet"),
            _resource_type_order=["115", "yun139"],
        )
        client, results = self._search(owner, [{
            "title": "繁花 2023 4K 全集",
            "datetime": "2026-10-14 10:00:00",
            "links": [
                {"type": "mobile", "url": "https://yun.139.com/shareweb/#/w/i/abc", "work_title": "繁花 2023 4K 全集"},
            ],
        }])
        self.assertEqual(client.calls[-1].get("cloud_types"), ["115", "yun139"])
        self.assertEqual(
            [row.get("resource_type") for row in results],
            ["yun139"],
        )

    def test_cross_drive_type_kept_via_runtime_order_fallback(self):
        owner = _FakeOwner(
            _cloud_drive_key="115",
            _resource_type_order=["115", "yun139"],
        )
        client, results = self._search(owner, [{
            "title": "繁花 2023 4K 全集",
            "datetime": "2026-10-14 10:00:00",
            "links": [
                {"type": "mobile", "url": "https://yun.139.com/shareweb/#/w/i/abc", "work_title": "繁花 2023 4K 全集"},
            ],
        }])
        self.assertEqual(client.calls[-1].get("cloud_types"), ["115", "yun139"])
        self.assertEqual(len(results), 1)

    def test_cross_drive_type_dropped_without_any_capability_hint(self):
        owner = _FakeOwner(_cloud_drive_key="115")
        client, results = self._search(owner, [{
            "title": "繁花 2023 4K 全集",
            "datetime": "2026-10-14 10:00:00",
            "links": [
                {"type": "mobile", "url": "https://yun.139.com/shareweb/#/w/i/abc", "work_title": "繁花 2023 4K 全集"},
            ],
        }])
        self.assertNotIn("yun139", client.calls[-1].get("cloud_types") or [])
        self.assertEqual(results, [])


class _FakeProvider:
    def __init__(self, capabilities=()):
        self._capabilities = set(capabilities)

    def supports(self, capability):
        return capability in self._capabilities


class _FakeActiveDrive:
    name = "115网盘"

    def __init__(self, own_types, capabilities=()):
        self._own_types = set(own_types)
        self._capabilities = set(capabilities)

    def supports_resource_type(self, value):
        return value in self._own_types

    def supports(self, capability):
        return capability in self._capabilities


class TestDriveManagerCapabilityWiring(unittest.TestCase):
    """能力过滤结果必须回写到 ``_cloud_drive_resource_types``。"""

    def _manager(self, plugin, registry):
        manager = CloudDriveManager.__new__(CloudDriveManager)
        manager._plugin = plugin
        manager.registry = registry
        manager.active_drive = None
        manager._clients = {}
        return manager

    def test_cross_capable_source_written_back(self):
        plugin = _FakeOwner(
            _cloud_drive_key="115",
            _cross_transfer_enabled=True,
            _resource_type_order=["115", "yun139", "baidu"],
        )
        registry = MagicMock()
        registry.get.side_effect = lambda key: (
            _FakeProvider({
                CloudDriveCapability.SHARE_TRANSFER,
                CloudDriveCapability.FILE_DOWNLOAD,
            }) if key == "yun139" else (_ for _ in ()).throw(KeyError(key))
        )
        manager = self._manager(plugin, registry)
        manager.active_drive = _FakeActiveDrive(
            {"115"}, {CloudDriveCapability.LOCAL_UPLOAD}
        )
        manager._filter_supported_resource_types()
        self.assertEqual(plugin._resource_type_order, ["115", "yun139"])
        self.assertEqual(plugin._cloud_drive_resource_types, ("115", "yun139"))

    def test_cross_capable_source_dropped_when_cross_disabled(self):
        plugin = _FakeOwner(
            _cloud_drive_key="115",
            _cross_transfer_enabled=False,
            _resource_type_order=["115", "yun139"],
        )
        registry = MagicMock()
        registry.get.side_effect = lambda key: (_ for _ in ()).throw(KeyError(key))
        manager = self._manager(plugin, registry)
        manager.active_drive = _FakeActiveDrive(
            {"115"}, {CloudDriveCapability.LOCAL_UPLOAD}
        )
        manager._filter_supported_resource_types()
        self.assertEqual(plugin._resource_type_order, ["115"])
        self.assertEqual(plugin._cloud_drive_resource_types, ("115",))

    def test_capability_set_cleared_without_active_drive(self):
        plugin = _FakeOwner(
            _cloud_drive_key="115",
            _cross_transfer_enabled=True,
            _resource_type_order=["115", "yun139"],
            _cloud_drive_resource_types=("115", "yun139"),
        )
        manager = self._manager(plugin, MagicMock())
        manager.active_drive = None
        manager._filter_supported_resource_types()
        self.assertEqual(plugin._cloud_drive_resource_types, ())


if __name__ == "__main__":
    unittest.main()
