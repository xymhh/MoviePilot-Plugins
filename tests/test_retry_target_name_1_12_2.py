"""历史重试必须把目标名传给转存（1.12.2）。

复现现场（2026-11-16 13:17–13:21，用户更新到 v1.12.1 后重试 5 条失败记录）：

* ``retry_history_record`` 在 ``_resolve_history_retry_context`` 里已经算好
  了整理目标名（如“十万狂花入梦来 - S01E06 - 第6集.mkv”），但调
  ``_transfer_file`` 时第 4 个位置参数传的是 ``None``；
* ``create()`` 的 title 因此退回源文件名，1.12.1 的“下载时用目标名落盘”
  在重试上根本没生效：文件以 06.mkv、S01E200.2020.2160p.WEB-DL.H.264.AAC.mkv
  之类的源文件名落到 /downloads，MP 整理认不出剧名，5 条重试全以
  Downloads 命名失败，记录原因还是那句旧的“网盘文件定位失败”。

本测试锁定：重试传给转存的目标名必须是算好的整理目标名，不能是 None，
也不能退回源文件名。
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
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
)
set_media_type(movie="电影", tv="电视剧")
sys.modules["cloudsubscribex.core"].OwnerDelegator = OwnerDelegator
sys.modules["cloudsubscribex.core"].CloudFile = MagicMock()
sys.modules["cloudsubscribex.core"].CloudDriveCapability = MagicMock()

HistoryRetryService = load_module(
    "cloudsubscribex.handlers.sync.retry",
    "plugins.v2/cloudsubscribex/handlers/sync/retry.py",
).HistoryRetryService

RECORD_TIME = "2026-11-16 12:24:44"
SHARE_URL = "https://pan.quark.cn/s/4df950a3b4b6"
# 记录上的文件名是整理目标名；源文件名是网盘里的裸名（06.mkv），两者不同，
# 这样才能分清重试到底把哪个名字传给了转存。
RECORD_FILE = "十万狂花入梦来 - S01E06 - 第6集.mkv"
SOURCE_FILE = "06.mkv"
TARGET_NAME = "十万狂花入梦来 - S01E06 - 第6集.mkv"
CLOUD_DIR = "/Alist/影视/moviepilot/媒体库/剧集/国产剧/十万狂花入梦来 (2026)"


class HistoryRetryTargetNameTest(unittest.TestCase):
    """重试必须把整理目标名传给转存，而不是 None/源文件名。"""

    def setUp(self):
        self.owner = MagicMock()
        self.mediainfo = MagicMock()
        self.mediainfo.tmdb_id = 261315
        self.mediainfo.type = "电视剧"
        self.mediainfo.title = "十万狂花入梦来"
        self.mediainfo.year = "2026"
        self.mediainfo.get_poster_image.return_value = "https://img/x.jpg"
        self.record = {
            "time": RECORD_TIME,
            "share_url": SHARE_URL,
            "file_name": RECORD_FILE,
            "source_file_name": SOURCE_FILE,
            "source_sha1": "abc123",
            "file_size": 1024,
            "status": "失败",
            "failure_reason": "网盘文件定位失败（在转存路径 /Alist/影视/moviepilot/转存 及目标路径均未找到匹配文件，已尝试 2 次）",
            "transfer_mode": "cross",
            "source_drive_key": "quark",
            "target_drive_key": "115",
        }
        self.owner._get_data.return_value = [self.record]
        self.owner._cloud_drive.key = "115"
        self.owner._cloud_transfer_path = "/Alist/影视/moviepilot/转存"
        self.owner._cloud_query.find_file.return_value = None
        self.owner._cross_transfer_manager.cache_info.return_value = {}
        self.owner.transfer_ops._transfer_file.return_value = True
        self.owner.transfer_ops._transfer_failure_reason.return_value = ""
        self.owner.strm_ops._generate_or_queue_strm.return_value = (
            None, "pending-key-1",
        )
        self.owner._is_ed2k_url.return_value = False
        self.owner._share_transfer.check_share_status.return_value.is_valid = True
        self.owner._share_transfer.list_share_files.return_value = []
        self.retry_context = {
            "subscribe": None,
            "mediainfo": self.mediainfo,
            "season": 1,
            "episode": 6,
            "cloud_dir": CLOUD_DIR,
            "target_name": TARGET_NAME,
        }
        self.share_file = {
            "id": "file-id-1",
            "name": SOURCE_FILE,
            "size": 1024,
            "sha1": "abc123",
        }
        self.service = HistoryRetryService(self.owner)

    def _retry(self):
        # v1.13.3：重试入口只查重试索引，不再扫 _get_data("history")；
        # 这里直接把记录钉在查找入口上，专注验证目标名传递。
        with patch.object(
            HistoryRetryService,
            "_find_history_record",
            return_value=self.record,
        ), patch.object(
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

    def test_retry_passes_organized_target_name_to_transfer(self):
        """回归：重试传给 _transfer_file 的目标名必须是整理目标名。"""
        self._retry()

        self.assertEqual(
            1, self.owner.transfer_ops._transfer_file.call_count,
        )
        args, _kwargs = self.owner.transfer_ops._transfer_file.call_args
        self.assertEqual(TARGET_NAME, args[3])
        self.assertNotEqual(SOURCE_FILE, args[3])

    def test_retry_with_empty_target_name_falls_back_to_source_name(self):
        """目标名算不出来时（平台命名返回空），重试仍走源文件名，不崩。"""
        self.retry_context["target_name"] = ""

        self._retry()

        self.assertEqual(1, self.owner.transfer_ops._transfer_file.call_count)
        args, _kwargs = self.owner.transfer_ops._transfer_file.call_args
        # 传空串：转存层按既有兜底用源文件名（create() 的 title 退路），
        # 这里只锁定“不传 None 且不抛错”，不断言最终落盘名。
        self.assertEqual("", args[3])


if __name__ == "__main__":
    unittest.main()
