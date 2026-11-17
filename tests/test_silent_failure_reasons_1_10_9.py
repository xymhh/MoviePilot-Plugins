"""1.10.9：静默失败点补原因——重试不再只剩一句通用话。

复现（2026-11-16 08:25，LOST10 S01E05 在 1.10.8 下重试）：
记录原因从 1.10.7 的详细字典变成了通用的「重试转存失败」，分类
也从「秒传未命中」变回「未分类失败」。机制：点重试提交时
submit_history_retry 先 clear_failure 清掉旧原因，后台线程再跑；
这次尝试失败在了能记原因之前的步骤——_transfer_file_impl 里几处
提前 return False 都不留原因（跨盘前置条件、分享转存返回失败、
暂存文件不可见、媒体类型未启用），账本包装里暂停/未认领/账本
不可用也一样；重试的兜底链（新原因 > 记录旧原因 > 「重试转存
失败」）两头都空，就只剩通用句。本版：

- handlers/sync/transfer_ops.py：新增 _fail_transfer（记原因再
  返回 False），所有静默出口都走它；前置条件逐项点名缺了哪块；
  _remember_transfer_failure 记之前先脱敏（失败原因进历史记录
  的必经口）。
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin_env import (
    ensure_package, install_app_mocks, install_core_delegation, load_module,
)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache", "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)




install_core_delegation()
pause_module = ensure_package("cloudsubscribex.core.sync_pause")
pause_module.is_paused = MagicMock(return_value=False)
ensure_package("cloudsubscribex.core").sync_pause = pause_module
mock_utils = ensure_package("cloudsubscribex.utils")
MediaFileParser = load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
).MediaFileParser
mock_utils.MediaFileParser = MediaFileParser
mock_utils.parse_magnet_metadata = MagicMock()

load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
load_module(
    "cloudsubscribex.utils.magnet",
    "plugins.v2/cloudsubscribex/utils/magnet.py",
)
load_module(
    "cloudsubscribex.search.magnet",
    "plugins.v2/cloudsubscribex/search/magnet.py",
)
_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler
TransferOpsMixin = _service_module.TransferOpsMixin
# transfer_ops 里用的能力枚举（与 service 模块同一份 mock 对象，
# 按 is 区分 SHARE_TRANSFER / FILE_QUERY）。
_CAP = sys.modules["cloudsubscribex.handlers.sync.transfer_ops"].CloudDriveCapability


def _provider(*, key, name, supports=True, share_transfer=None, file_query=None):
    def _supports(capability):
        if isinstance(supports, bool):
            return supports
        return capability in supports

    def _require(capability):
        if share_transfer is not None and capability is _CAP.SHARE_TRANSFER:
            return share_transfer
        if file_query is not None and capability is _CAP.FILE_QUERY:
            return file_query
        return MagicMock()

    provider = SimpleNamespace(
        key=key, name=name, supports=_supports, require=_require,
    )
    return provider


class FailTransferTests(unittest.TestCase):
    def _handler(self):
        handler = SyncHandler.__new__(SyncHandler)
        return handler

    def test_fail_transfer_returns_false_and_remembers(self):
        handler = self._handler()
        file_item = {"id": "fid1", "name": "05.mkv"}
        self.assertFalse(handler._fail_transfer(file_item, "夸克分享转存失败"))
        self.assertEqual(
            handler._transfer_failure_reason("fid1"), "夸克分享转存失败",
        )

    def test_remember_sanitizes_credentials(self):
        handler = self._handler()
        file_item = {"id": "fid1", "name": "05.mkv"}
        handler._remember_transfer_failure(
            file_item, "上传失败：{'user_key': 'FAKEUSERKEY0123456789ABCDEF0123'}",
        )
        reason = handler._transfer_failure_reason("fid1")
        self.assertIn("'user_key': '***'", reason)
        self.assertNotIn("FAKEUSERKEY0123456789ABCDEF0123", reason)

    def test_remember_ignores_empty_file_id(self):
        handler = self._handler()
        handler._remember_transfer_failure({"name": "05.mkv"}, "某原因")
        self.assertEqual(handler._transfer_failure_reason(""), "")


class ImplSilentGapTests(unittest.TestCase):
    """_transfer_file_impl 的静默出口：返回 False 时必须留下原因。"""

    def _handler(self, *, enabled=True, manager=True):
        handler = SyncHandler.__new__(SyncHandler)
        handler._cross_transfer_enabled = enabled
        handler._cross_transfer_manager = MagicMock() if manager else None
        handler._cross_transfer_media_types = {"电视剧", "电影"}
        handler._cloud_drive = _provider(key="115", name="115网盘")
        handler._stop_requested = lambda: False
        handler._is_cloud_resource_url = lambda url: False
        handler._normalize_cross_transfer_media_type = lambda mt: mt
        # 注意：_cross_transfer_staging_path 是 TransferOpsMixin 自身的方法，
        # 直接给宿主赋值同名属性不会生效（类属性优先于委托）；改给宿主挂
        # 真实方法要读的 _cloud_transfer_paths，让真实方法自己算。
        handler._cloud_transfer_paths = {"quark": "/staging/quark"}
        return handler

    def _source(self, *, share_transfer=None, file_query=None):
        return _provider(
            key="quark", name="夸克网盘",
            share_transfer=share_transfer, file_query=file_query,
        )

    def _file_item(self, **overrides):
        item = {
            "id": "fid1", "name": "05.mkv", "media_type": "电视剧",
            "size": 3239498038, "sha1": "A" * 40,
        }
        item.update(overrides)
        return item

    def _run_impl(self, handler, source, file_item):
        handler._resource_provider_for_url = lambda url: source
        return handler._transfer_file_impl(
            "https://pan.quark.cn/s/abc", file_item, "/转存", "05.mkv",
            source_sha1="A" * 40,
        )

    def test_cross_disabled_names_the_missing_piece(self):
        handler = self._handler(enabled=False)
        file_item = self._file_item()
        self.assertFalse(self._run_impl(handler, self._source(), file_item))
        reason = handler._transfer_failure_reason("fid1")
        self.assertIn("无法跨盘转存", reason)
        self.assertIn("跨盘转存未启用", reason)

    def test_media_type_not_enabled_is_remembered(self):
        handler = self._handler()
        file_item = self._file_item(media_type="音乐")
        self.assertFalse(self._run_impl(handler, self._source(), file_item))
        reason = handler._transfer_failure_reason("fid1")
        self.assertIn("媒体类型", reason)
        self.assertIn("未启用跨盘转存", reason)

    def test_share_staging_false_is_remembered(self):
        handler = self._handler()
        share_transfer = SimpleNamespace(
            transfer_file=MagicMock(return_value=False),
        )
        file_item = self._file_item()
        self.assertFalse(
            self._run_impl(
                handler, self._source(share_transfer=share_transfer), file_item,
            )
        )
        reason = handler._transfer_failure_reason("fid1")
        self.assertIn("夸克网盘分享转存失败", reason)
        self.assertIn("未返回暂存文件", reason)

    def test_staged_file_invisible_is_remembered(self):
        handler = self._handler()
        share_transfer = SimpleNamespace(
            transfer_file=MagicMock(return_value=True),
        )
        file_query = SimpleNamespace(find_file=MagicMock(return_value=None))
        file_item = self._file_item()
        with patch("time.sleep"):
            self.assertFalse(
                self._run_impl(
                    handler,
                    self._source(
                        share_transfer=share_transfer, file_query=file_query,
                    ),
                    file_item,
                )
            )
        reason = handler._transfer_failure_reason("fid1")
        self.assertIn("跨盘临时文件尚未可见", reason)
        self.assertIn("/staging/quark/05.mkv", reason)


class SameDriveMpOrganizeIdentityTests(unittest.TestCase):
    """P1 单文件同盘直存：触发成功时把 MP 整理身份写回 file_item（v1.12.6）。

    补 C 波留下的已知缺口：同盘单文件路径调了 trigger 却没把结果写回
    file_item，同盘重试走老定位。跨盘分支已在 A+B 波处理，这里只补同盘。
    """

    _INFO = {
        "src_path": "/转存/凡人修仙传 - S01E200 - 第200集.mkv",
        "src_storage": "local",
        "triggered_at": 1763270400.0,
        "target_dir": "/媒体库/剧集",
        "target_storage": "u115cs",
        "target_name": "剧集",
        "file_name": "凡人修仙传 - S01E200 - 第200集.mkv",
    }

    def _handler(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._cloud_drive = _provider(key="115", name="115网盘")
        handler._stop_requested = lambda: False
        handler._is_cloud_resource_url = lambda url: False
        return handler

    def _run_same_drive(self, handler, file_item, *, trigger_result, parent_task_id=""):
        object.__setattr__(
            handler, "trigger_mp_organize",
            MagicMock(return_value=trigger_result),
        )
        share_transfer = SimpleNamespace(
            transfer_file=MagicMock(return_value=True),
        )
        source = _provider(key="115", name="115网盘", share_transfer=share_transfer)
        handler._resource_provider_for_url = lambda url: source
        transfer_ops_mod = sys.modules["cloudsubscribex.handlers.sync.transfer_ops"]
        with patch.object(transfer_ops_mod, "_mark_mp_organizing") as mock_mark:
            result = handler._transfer_file_impl(
                "https://115.com/s/abc", file_item, "/转存",
                "凡人修仙传 - S01E200 - 第200集.mkv",
                source_sha1="A" * 40, parent_task_id=parent_task_id,
                media={"media_type": "tv", "tmdb_id": 106449, "season": 1},
            )
        return result, mock_mark

    def test_trigger_success_writes_identity_and_marks(self):
        handler = self._handler()
        file_item = {"id": "fid1", "name": "凡人修仙传 - S01E200 - 第200集.mkv"}
        result, mock_mark = self._run_same_drive(
            handler, file_item,
            trigger_result=(True, dict(self._INFO)),
            parent_task_id="subscribe:123",
        )
        self.assertTrue(result)
        self.assertEqual(file_item.get("mp_organize"), self._INFO)
        self.assertTrue(file_item.get("_mp_organized"))
        mock_mark.assert_called_once_with("subscribe:123", self._INFO)

    def test_trigger_decline_keeps_success_without_identity(self):
        handler = self._handler()
        file_item = {"id": "fid1", "name": "凡人修仙传 - S01E200 - 第200集.mkv"}
        result, mock_mark = self._run_same_drive(
            handler, file_item,
            trigger_result=(
                False, {"reason": "not_accepted", "message": "MP 整理未接受"},
            ),
            parent_task_id="subscribe:123",
        )
        self.assertTrue(result)
        self.assertNotIn("mp_organize", file_item)
        mock_mark.assert_not_called()

    def test_trigger_success_without_parent_task_skips_mark(self):
        handler = self._handler()
        file_item = {"id": "fid1", "name": "凡人修仙传 - S01E200 - 第200集.mkv"}
        result, mock_mark = self._run_same_drive(
            handler, file_item,
            trigger_result=(True, dict(self._INFO)),
            parent_task_id="",
        )
        self.assertTrue(result)
        self.assertEqual(file_item.get("mp_organize"), self._INFO)
        mock_mark.assert_not_called()


if __name__ == "__main__":
    unittest.main()
