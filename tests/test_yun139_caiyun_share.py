"""1.6.40 回归：移动云盘 ``caiyun.139.com`` 分享链接识别、解析与转存归类。

线上现象（1.6.39 真机，用户反馈）：
1. 资源页「移动云盘」条目点「预览」→ 后端直接回 ``资源链接格式或类型无效``；
2. 同一条目点「转存」→ 没有任何反应（资源被判成 115 直存分享，转存链路不认）；
3. 根因：``TYPE_HOSTS['yun139']`` 只登记了 ``yun.139.com`` / ``caiyun.feixin.10086.cn``，
   而 PanSou 实际返回的移动云盘分享域名是 ``caiyun.139.com``（``/w/i/<id>`` 与
   ``/m/i?<id>`` 两种写法）。``resource_type_from_url`` 因此返回空串：
   * 预览侧 ``core/api/search.py`` 的 ``resource_type_from_url(url) != resource_type``
     校验必然失败；
   * 转存侧 ``ResourceTransferService._supported_resource_type({}, url)`` 兜底成 ``115``，
     139 资源被当成 115 直存，跨盘批次里永远轮不到它；
   * 另外 ``/m/i?<id>`` 移动端短链不在 ``_SHARE_ID_PATTERNS`` 内，解析不出 share_code，
     手动转存门槛校验也会拒绝。

本测试固定：
1. 两种 caiyun 分享链接都能识别为 ``yun139``（并保留老域名行为）；
2. 两种 caiyun 分享链接都能解析出 ``share_code``（含提取码写法）；
3. 手动转存门槛校验放行；
4. 转存归类不再把 139 资源误判为 115 直存。
"""

import os
import sys
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


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


# 用真实 search.types：本测试的核心就是它的域名识别表
register_module("cloudsubscribex", package=True)
register_module("cloudsubscribex.search", package=True)
types_module = load_module(
    "cloudsubscribex.search.types",
    "plugins.v2/cloudsubscribex/search/types.py",
)

# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_mock("app.core.metainfo", MetaInfo=MagicMock())
register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.db.subscribe_oper", SubscribeOper=MagicMock())
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_module("app.utils", package=True)
register_mock("app.utils.string")
register_module("app.schemas.types", MediaType=MagicMock())

# ---- cloudsubscribex 包层级与占位依赖 ----
register_mock(
    "cloudsubscribex.core",
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
    CloudDriveProvider=MagicMock(),
    CloudFile=MagicMock(),
    SearchCapability=MagicMock(),
)
register_mock(
    "cloudsubscribex.core.cloud",
    ShareLinkStatus=MagicMock(),
    CloudDriveCapability=MagicMock(),
)
register_mock("cloudsubscribex.core.media")
register_mock("cloudsubscribex.core.transfer", HttpFileDownloadService=MagicMock())
register_mock("cloudsubscribex.search.matching", positive_ints=MagicMock())
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

drive_common = ensure_package("cloudsubscribex.drive.common")
drive_common.iter_transfer_batches = lambda values, *a, **k: []
drive_common.extract_list = lambda data, keys: data if isinstance(data, list) else []
drive_common.CloudDriveFileServiceBase = object
drive_common.safe_int = lambda value, default=0: default

drive_package = ensure_package("cloudsubscribex.drive")
drive_package.__path__ = [_plugin_dir()]
ensure_package("cloudsubscribex.drive.yun139").__path__ = [_plugin_dir("yun139")]
register_mock("cloudsubscribex.drive.yun139.client")

Yun139ShareService = load_module(
    "cloudsubscribex.drive.yun139.share",
    "plugins.v2/cloudsubscribex/drive/yun139/share.py",
).Yun139ShareService

sync_module = load_module(
    "cloudsubscribex.core.api.sync",
    "plugins.v2/cloudsubscribex/core/api/sync.py",
)

# 转存归类模块（handlers/sync/resources.py）依赖：占位掉与本次回归无关的部分
register_mock(
    "cloudsubscribex.core.services.skip_stats",
    SkipStatsCollector=MagicMock(),
)
register_mock(
    "cloudsubscribex.utils",
    MediaFileParser=MagicMock(),
    parse_magnet_metadata=MagicMock(),
)
handlers_package = ensure_package("cloudsubscribex.handlers")
handlers_package.__path__ = [_plugin_dir("handlers")]
sync_package = ensure_package("cloudsubscribex.handlers.sync")
sync_package.__path__ = [_plugin_dir("handlers", "sync")]

resource_type_from_url = types_module.resource_type_from_url
resource_type_from_text = types_module.resource_type_from_text

WEB_SHARE_URL = "https://caiyun.139.com/w/i/2prAL5g3t5Pez"
MOBILE_SHARE_URL = "https://caiyun.139.com/m/i?2jexBVfL4N87f"


class TestCaiyunUrlRecognition(unittest.TestCase):
    """caiyun.139.com 分享链接必须识别为移动云盘。"""

    def test_web_share_link(self):
        self.assertEqual(resource_type_from_url(WEB_SHARE_URL), "yun139")

    def test_mobile_share_link(self):
        self.assertEqual(resource_type_from_url(MOBILE_SHARE_URL), "yun139")

    def test_legacy_hosts_unchanged(self):
        self.assertEqual(
            resource_type_from_url("https://yun.139.com/shareweb/#/w/i/abc123"),
            "yun139",
        )
        self.assertEqual(
            resource_type_from_url("https://caiyun.feixin.10086.cn/detail"),
            "yun139",
        )

    def test_text_marker(self):
        """PanSou 附言里带 caiyun 域名时也要能识别类型。"""
        self.assertEqual(
            resource_type_from_text(f"移动云盘 {MOBILE_SHARE_URL} 提取码: 1a2b"),
            "yun139",
        )

    def test_other_pan_links_not_affected(self):
        self.assertEqual(resource_type_from_url("https://pan.quark.cn/s/abc"), "quark")
        self.assertEqual(resource_type_from_url("https://115.com/s/abc"), "115")
        self.assertEqual(resource_type_from_url("https://example.com/share"), "")


class TestCaiyunShareInfoParsing(unittest.TestCase):
    """两种 caiyun 分享链接都要能解析出分享 ID。"""

    def setUp(self):
        self.service = Yun139ShareService(MagicMock(), MagicMock(), MagicMock())

    def test_web_share_link(self):
        info = self.service.extract_share_info(WEB_SHARE_URL)
        self.assertEqual(info.get("share_code"), "2prAL5g3t5Pez", info)
        self.assertEqual(info.get("link_id"), "2prAL5g3t5Pez", info)

    def test_mobile_share_link(self):
        """回归：/m/i?<id> 移动端短链此前完全解析不出分享 ID。"""
        info = self.service.extract_share_info(MOBILE_SHARE_URL)
        self.assertEqual(info.get("share_code"), "2jexBVfL4N87f", info)
        self.assertEqual(info.get("link_id"), "2jexBVfL4N87f", info)

    def test_mobile_share_link_with_password(self):
        info = self.service.extract_share_info(f"{MOBILE_SHARE_URL}#1a2b")
        self.assertEqual(info.get("share_code"), "2jexBVfL4N87f", info)
        self.assertEqual(info.get("password"), "1a2b", info)

    def test_legacy_shareweb_link(self):
        info = self.service.extract_share_info(
            "https://yun.139.com/shareweb/#/w/i/abc123"
        )
        self.assertEqual(info.get("share_code"), "abc123", info)

    def test_other_pan_link_rejected(self):
        self.assertEqual(self.service.extract_share_info("https://pan.quark.cn/s/abc"), {})


class TestPreviewGate(unittest.TestCase):
    """预览接口的类型校验（core/api/search.py）必须放行 caiyun 链接。"""

    def test_preview_type_gate(self):
        for url in (WEB_SHARE_URL, MOBILE_SHARE_URL):
            # 等价于 core/api/search.py 里的
            # ``if "\\" in url or resource_type_from_url(url) != resource_type``
            detected = resource_type_from_url(url)
            self.assertNotEqual(detected, "")
            self.assertEqual(detected, "yun139", url)


class TestManualTransferGate(unittest.TestCase):
    """手动转存门槛校验必须放行 caiyun 链接。"""

    def setUp(self):
        self.service = Yun139ShareService(MagicMock(), MagicMock(), MagicMock())

    def _valid(self, share_info):
        return sync_module.SyncApi._manual_share_info_valid("yun139", share_info)

    def test_web_share_link_passes(self):
        info = self.service.extract_share_info(WEB_SHARE_URL)
        self.assertTrue(self._valid(info), info)

    def test_mobile_share_link_passes(self):
        info = self.service.extract_share_info(MOBILE_SHARE_URL)
        self.assertTrue(self._valid(info), info)

    def test_empty_still_rejected(self):
        self.assertFalse(self._valid({}))


class TestTransferClassification(unittest.TestCase):
    """转存归类不能再把 139 分享兜底成 115 直存。"""

    def _service(self):
        module = load_module(
            "cloudsubscribex.handlers.sync.resources",
            "plugins.v2/cloudsubscribex/handlers/sync/resources.py",
        )
        return module.ResourceTransferService.__new__(module.ResourceTransferService)

    def test_supported_resource_type_from_url(self):
        service = self._service()
        for url in (WEB_SHARE_URL, MOBILE_SHARE_URL):
            self.assertEqual(
                service._supported_resource_type({}, url), "yun139", url
            )

    def test_explicit_resource_type_wins(self):
        service = self._service()
        self.assertEqual(
            service._supported_resource_type({"resource_type": "yun139"}, ""),
            "yun139",
        )

    def test_unknown_url_still_falls_back_to_115(self):
        """历史行为保留：完全识别不出的分享仍按 115 处理。"""
        service = self._service()
        self.assertEqual(
            service._supported_resource_type({}, "https://example.com/share"), "115"
        )


if __name__ == "__main__":
    unittest.main()
