"""手动转存分享链接解析契约回归测试。

复现 Issue：在「手动资源」对话框粘贴阿里云盘分享链接时，后端固定返回
``第 1 行资源无效：无法解析有效的阿里云盘分享链接``（移动云盘同理）。

根因：手动转存的门槛校验 ``SyncApi._manual_share_info_valid`` 只认 ``share_code``
键，而阿里云盘（``share_id``/``share_pwd``）与移动云盘（``link_id``/``password``）
的 ``extract_share_info`` 当时不返回该键，导致这两个来源的分享链接在任何情况下都
过不了校验。

本测试固定：
1. 各驱动 ``extract_share_info`` 必须回填统一的 ``share_code``，且保留自己接口用的键；
2. 真实校验函数对阿里云盘 / 移动云盘的解析结果放行，对空结果仍然拒绝；
3. 阿里云盘分享的提取码支持「提取码: xxxx」附言写法。
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


def _iter_transfer_batches(values, batch_size, batch_interval, provider_limit):
    size = max(1, min(int(batch_size or 1), int(provider_limit or 1)))
    normalized = list(dict.fromkeys(str(v) for v in values))
    for offset in range(0, len(normalized), size):
        yield normalized[offset:offset + size]


# ---- cloudsubscribex 包层级与占位依赖 ----
register_module("cloudsubscribex", package=True)
register_module(
    "cloudsubscribex.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
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
register_module(
    "cloudsubscribex.search.types",
    normalize_resource_type=lambda value, *a, **k: str(value or "").strip().lower(),
    resource_type_from_text=lambda value, *a, **k: "",
    resource_type_from_url=lambda value, *a, **k: "",
    resource_type_name=lambda value, *a, **k: str(value),
)
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

drive_common = ensure_package("cloudsubscribex.drive.common")
drive_common.iter_transfer_batches = _iter_transfer_batches
drive_common.extract_list = lambda data, keys: data if isinstance(data, list) else []
drive_common.CloudDriveFileServiceBase = object

drive_package = ensure_package("cloudsubscribex.drive")
drive_package.__path__ = [_plugin_dir()]
ensure_package("cloudsubscribex.drive.alipan").__path__ = [_plugin_dir("alipan")]
ensure_package("cloudsubscribex.drive.yun139").__path__ = [_plugin_dir("yun139")]
register_mock("cloudsubscribex.drive.yun139.client")
AliPanShareService = load_module(
    "cloudsubscribex.drive.alipan.share",
    "plugins.v2/cloudsubscribex/drive/alipan/share.py",
).AliPanShareService
Yun139ShareService = load_module(
    "cloudsubscribex.drive.yun139.share",
    "plugins.v2/cloudsubscribex/drive/yun139/share.py",
).Yun139ShareService
sync_module = load_module(
    "cloudsubscribex.core.api.sync",
    "plugins.v2/cloudsubscribex/core/api/sync.py",
)


class TestShareInfoContract(unittest.TestCase):
    """各驱动 extract_share_info 的统一键契约。"""

    def setUp(self):
        self.service = AliPanShareService(MagicMock(), MagicMock())
        self.yun139 = Yun139ShareService(MagicMock(), MagicMock(), MagicMock())

    def test_alipan_returns_share_code(self):
        """阿里云盘分享链接必须回填 share_code，同时保留 share_id 供接口使用。"""
        info = self.service.extract_share_info("https://www.alipan.com/s/abc123")
        self.assertEqual(info.get("share_code"), "abc123", info)
        self.assertEqual(info.get("share_id"), "abc123", info)

    def test_alipan_legacy_domain_and_query_password(self):
        info = self.service.extract_share_info(
            "https://www.aliyundrive.com/s/xyz-9?pwd=abcd"
        )
        self.assertEqual(info.get("share_code"), "xyz-9", info)
        self.assertEqual(info.get("share_pwd"), "abcd", info)

    def test_alipan_text_password(self):
        """「提取码: xxxx」附言写法也要能解析出访问码。"""
        info = self.service.extract_share_info(
            "https://www.alipan.com/s/abc123 提取码: 9f2k"
        )
        self.assertEqual(info.get("share_code"), "abc123", info)
        self.assertEqual(info.get("share_pwd"), "9f2k", info)

    def test_alipan_ignores_other_pan_links(self):
        self.assertEqual(self.service.extract_share_info("https://pan.quark.cn/s/abc"), {})

    def test_yun139_returns_share_code(self):
        info = self.yun139.extract_share_info("https://yun.139.com/w/i/abcdef")
        self.assertEqual(info.get("share_code"), "abcdef", info)
        self.assertEqual(info.get("link_id"), "abcdef", info)


class TestManualShareInfoValidation(unittest.TestCase):
    """真实校验函数对各家解析结果的放行行为。"""

    def setUp(self):
        self.alipan = AliPanShareService(MagicMock(), MagicMock())
        self.yun139 = Yun139ShareService(MagicMock(), MagicMock(), MagicMock())

    def _valid(self, resource_type, share_info):
        return sync_module.SyncApi._manual_share_info_valid(resource_type, share_info)

    def test_alipan_link_passes(self):
        """阿里云盘链接解析结果必须通过手动转存校验（回归本次 Issue）。"""
        info = self.alipan.extract_share_info("https://www.alipan.com/s/abc123")
        self.assertTrue(self._valid("alipan", info), info)

    def test_yun139_link_passes(self):
        info = self.yun139.extract_share_info("https://yun.139.com/w/i/abcdef")
        self.assertTrue(self._valid("yun139", info), info)

    def test_empty_share_info_still_rejected(self):
        self.assertFalse(self._valid("alipan", {}))
        self.assertFalse(self._valid("alipan", {"share_code": ""}))


if __name__ == "__main__":
    unittest.main()
