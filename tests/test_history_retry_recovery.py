"""历史记录重试「转存已成功但记录卡在失败」的回归测试。

复现现场（2026-10-09 只要活下去 14.7GB 跨盘转存）：

* 22:14 本地中继传输**成功**后，``retry_history_record`` 在装饰性元数据更新处抛出
  ``NameError: name 'target_subscribe' is not defined``，崩溃发生在写入
  ``finalize_key`` 之前；
* 记录因此既没关联后处理任务、也没刷新状态，后处理完成后找不到记录，
  只能「放弃写入终态」，用户在历史里看到的是「失败」。

本测试锁定两点：成功路径必须把 ``finalize_key``/状态先落定，且装饰性字段异常
不得再让整条重试失败。
"""

import sys
import unittest
from unittest.mock import MagicMock, patch

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.context", "app.core.metainfo", "app.db",
    "app.db.subscribe_oper", "app.utils", "app.utils.string",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.core.media",
    "cloudsubscribe.handlers", "cloudsubscribe.handlers.sync",
)
set_media_type(movie="电影", tv="电视剧")
sys.modules["cloudsubscribe.core"].OwnerDelegator = OwnerDelegator
sys.modules["cloudsubscribe.core"].CloudFile = MagicMock()
sys.modules["cloudsubscribe.core"].CloudDriveCapability = MagicMock()

HistoryRetryService = load_module(
    "cloudsubscribe.handlers.sync.retry",
    "plugins.v2/cloudsubscribe/handlers/sync/retry.py",
).HistoryRetryService

RECORD_TIME = "2026-10-09 21:26:34"
SHARE_URL = "https://pan.quark.cn/s/4df950a3b4b6"
RECORD_FILE = "只要活下去 (2026) - 2160p.mkv"
TARGET_NAME = "只要活下去 (2026) - 2160p.mkv"
CLOUD_DIR = "/Alist/影视/moviepilot/媒体库/电影/外语电影/只要活下去 (2026)"


class HistoryRetryRecoveryTest(unittest.TestCase):
    """跨盘转存成功后的终态关联必须早于装饰性字段更新。"""

    def setUp(self):
        self.owner = MagicMock()
        self.mediainfo = MagicMock()
        self.mediainfo.tmdb_id = 1458857
        self.mediainfo.type = "电影"
        self.mediainfo.title = "只要活下去"
        self.mediainfo.year = "2026"
        self.mediainfo.get_poster_image.return_value = "https://img/x.jpg"
        self.record = {
            "time": RECORD_TIME,
            "share_url": SHARE_URL,
            "file_name": RECORD_FILE,
            "source_file_name": "Doing.Life.2026.2160p.mkv",
            "source_sha1": "abc123",
            "file_size": 1024,
            "status": "失败",
            "failure_reason": "重试转存失败",
            "transfer_mode": "cross",
            "source_drive_key": "quark",
            "target_drive_key": "115",
        }
        self.owner._get_data.return_value = [self.record]
        self.owner._cloud_drive.key = "115"
        self.owner._cloud_transfer_path = "/Alist/影视/moviepilot/转存"
        self.owner._cloud_query.find_file.return_value = None
        self.owner._cross_transfer_manager.cache_info.return_value = {}
        self.owner._transfer_file.return_value = True
        self.owner._generate_or_queue_strm.return_value = (None, "pending-key-1")
        self.owner._is_ed2k_url.return_value = False
        self.owner._share_transfer.check_share_status.return_value.is_valid = True
        self.owner._share_transfer.list_share_files.return_value = []
        self.retry_context = {
            "subscribe": None,
            "mediainfo": self.mediainfo,
            "season": 1,
            "episode": None,
            "cloud_dir": CLOUD_DIR,
            "target_name": TARGET_NAME,
        }
        self.share_file = {
            "id": "file-id-1",
            "name": "Doing.Life.2026.2160p.mkv",
            "size": 1024,
            "sha1": "abc123",
        }
        self.service = HistoryRetryService(self.owner)

    def _retry(self):
        # 上下文还原与分享文件定位属于另外的职责链，这里替换为固定结果，
        # 专注验证「转存成功后」的终态处理
        with patch.object(
            HistoryRetryService,
            "_resolve_history_retry_context",
            return_value=self.retry_context,
        ), patch.object(
            HistoryRetryService,
            "_find_share_file_for_history",
            return_value=self.share_file,
        ):
            return self.service.retry_history_record(
                RECORD_TIME, SHARE_URL, RECORD_FILE, force=True
            )

    def test_success_path_links_finalize_key_before_decorating(self):
        """回归：转存成功后必须写入 finalize_key 并置处理中（此前抛 NameError）。"""
        result = self._retry()

        self.assertEqual("pending-key-1", result["pending_key"])
        self.assertEqual("处理中", result["status"])
        self.assertEqual("pending-key-1", self.record["finalize_key"])
        self.assertEqual("处理中", self.record["status"])
        self.assertEqual(TARGET_NAME, self.record["file_name"])
        self.assertNotIn("failure_reason", self.record)
        self.owner.append_history_records.assert_called_once()
        _, kwargs = self.owner.append_history_records.call_args
        self.assertTrue(kwargs.get("reopen_terminal"))

    def test_decorative_metadata_failure_keeps_terminal_link(self):
        """回归：标题/封面等装饰性更新异常不得让整条重试失败。"""
        self.mediainfo.get_poster_image.side_effect = RuntimeError("海报解析失败")

        result = self._retry()

        self.assertEqual("处理中", result["status"])
        self.assertEqual("pending-key-1", self.record["finalize_key"])

    def test_missing_source_file_still_reports_failure(self):
        """转存本身失败时仍要写回失败原因，不能因为顺序调整而丢掉。"""
        self.owner._transfer_file.return_value = False

        with self.assertRaises(RuntimeError):
            self._retry()

        self.assertEqual("失败", self.record["status"])
        self.assertEqual("重试转存失败", self.record["failure_reason"])


if __name__ == "__main__":
    unittest.main()
