"""历史页状态展示字段：「秒传失败」与其它失败分开显示。

运行：``cd <repo> && python -m pytest tests/test_history_status_fields.py -q``
"""

import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.cache",
    "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.log",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()
core_pkg.CloudFile = MagicMock()

failure = load_module(
    "cloudsubscribex.core.failure",
    "plugins.v2/cloudsubscribex/core/failure.py",
)

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = _history.HistoryService


class LabelMappingTest(unittest.TestCase):
    def test_checksum_miss_has_own_status_label(self):
        self.assertEqual(
            failure.status_label_of("失败", failure.KIND_CHECKSUM_MISS),
            "秒传失败",
        )
        self.assertEqual(failure.status_label_of("失败", "network"), "失败")
        self.assertEqual(failure.status_label_of("成功", ""), "成功")

    def test_pseudo_status_roundtrip(self):
        self.assertEqual(
            failure.kind_of_pseudo_status("秒传失败"),
            failure.KIND_CHECKSUM_MISS,
        )
        self.assertEqual(failure.kind_of_pseudo_status("失败"), "")

    def test_rapid_miss_message_classified(self):
        self.assertEqual(
            failure.classify_failure(
                "未命中秒传，已按设置中止（未上传任何数据）"
            ),
            failure.KIND_CHECKSUM_MISS,
        )


class HistoryPageFieldsTest(unittest.TestCase):
    def _fields(self, record):
        stub = HistoryService.__new__(HistoryService)
        return stub._history_page_fields(record)

    def test_failed_rapid_miss_gets_own_label(self):
        fields = self._fields({
            "status": "失败",
            "failure_reason": "未命中秒传，已按设置中止（未上传任何数据）",
            "file_name": "a.mp4",
            "type": "电影",
        })
        self.assertEqual(fields["status_label"], "秒传失败")
        self.assertEqual(fields["failure_kind"], failure.KIND_CHECKSUM_MISS)
        self.assertEqual(fields["failure_label"], "秒传未命中")

    def test_plain_failure_keeps_plain_label(self):
        fields = self._fields({
            "status": "失败",
            "failure_reason": "读取分享文件超时",
            "file_name": "b.mp4",
            "type": "电影",
        })
        self.assertEqual(fields["status_label"], "失败")
        self.assertEqual(fields["failure_label"], "网络异常")

    def test_success_has_no_failure_fields(self):
        fields = self._fields({
            "status": "成功", "file_name": "c.mp4", "type": "电影",
        })
        self.assertEqual(fields["status_label"], "成功")
        self.assertEqual(fields["failure_kind"], "")
        self.assertEqual(fields["failure_label"], "")

    def test_explicit_kind_wins_for_rapid_miss(self):
        """跨盘失败写入时已带 failure_kind，读回不再靠文本猜。"""
        fields = self._fields({
            "status": "失败",
            "failure_kind": failure.KIND_CHECKSUM_MISS,
            "failure_reason": "未命中秒传",
            "file_name": "d.mp4",
            "type": "电影",
        })
        self.assertEqual(fields["status_label"], "秒传失败")


if __name__ == "__main__":
    unittest.main()
