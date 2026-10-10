"""离线任务「已入库」标注的数据源判定测试。

现场（2026-10-10 Onslaught.2026.2160p.iT.WEB-DL...BYNDR，16.4GB）：

* 07:48:35 插件提交 115 离线任务；
* 07:50:11 日志显示「Magnet 下载后文件匹配完成：移动 1 个文件」——已经入库；
* 但 115 云下载任务条目 ``status`` 仍为 0，列表 UI 一直显示「等待下载 0.0%」。

原因是 115 秒传命中时文件立即可用、任务条目状态却可能长期不翻转，而插件的完成判定
只看网盘暂存目录（``handlers/sync/postprocess.py::_poll_offline_ready``），不依赖该状态。

本测试锁定展示层新判定 ``HistoryService.is_offline_task_finalized`` 的匹配规则：
同一 info_hash 且历史状态为「成功」才算已入库；失败、缺失、大小写、空值都要正确处理。
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
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.log",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.core.history",
    "cloudsubscribe.core.media", "cloudsubscribe.drive",
    "cloudsubscribe.drive.common", "cloudsubscribe.search",
    "cloudsubscribe.search.types",
    "cloudsubscribe.handlers",
    "cloudsubscribe.handlers.sync", "cloudsubscribe.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")
sys.modules["cloudsubscribe.core"].OwnerDelegator = OwnerDelegator
sys.modules["cloudsubscribe.core"].CloudFile = MagicMock()

HistoryService = load_module(
    "cloudsubscribe.handlers.sync.history",
    "plugins.v2/cloudsubscribe/handlers/sync/history.py",
).HistoryService

INFO_HASH = "3F1A4C2B5D6E7F80912A3B4C5D6E7F8091A2B3C4"
MAGNET_URL = (
    "magnet:?xt=urn:btih:3f1a4c2b5d6e7f80912a3b4c5d6e7f8091a2b3c4"
    "&dn=Onslaught.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-BYNDR"
)


class OfflineFinalizedFlagTest(unittest.TestCase):
    """历史匹配必须区分成功/失败，且对异常输入保持沉默。"""

    def setUp(self):
        self.owner = MagicMock()
        self.service = HistoryService(self.owner)

    def _with_history(self, records):
        self.owner._get_data.return_value = records

    def test_successful_record_marks_finalized(self):
        self._with_history([
            {"share_url": MAGNET_URL, "status": "成功"},
        ])
        self.assertTrue(self.service.is_offline_task_finalized(INFO_HASH))

    def test_failed_record_is_not_finalized(self):
        self._with_history([
            {"share_url": MAGNET_URL, "status": "失败"},
            {"share_url": MAGNET_URL, "status": "处理中"},
        ])
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))

    def test_unrelated_record_is_not_finalized(self):
        self._with_history([
            {"share_url": "https://pan.quark.cn/s/4df950a3b4b6", "status": "成功"},
        ])
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))

    def test_lowercase_hash_matches_uppercase_record(self):
        self._with_history([
            {"share_url": MAGNET_URL.upper(), "status": "成功"},
        ])
        self.assertTrue(self.service.is_offline_task_finalized(INFO_HASH.lower()))

    def test_empty_hash_is_not_finalized(self):
        self._with_history([
            {"share_url": MAGNET_URL, "status": "成功"},
        ])
        self.assertFalse(self.service.is_offline_task_finalized(""))
        self.assertFalse(self.service.is_offline_task_finalized(None))

    def test_missing_history_source_is_not_finalized(self):
        self.service = HistoryService(None)
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))

    def test_malformed_records_are_ignored(self):
        self._with_history([
            None,
            "not-a-dict",
            {"share_url": MAGNET_URL, "status": "成功"},
        ])
        self.assertTrue(self.service.is_offline_task_finalized(INFO_HASH))

    def test_blank_records_are_not_finalized(self):
        self._with_history([])
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))


class OfflineFinalizedApiContractTest(unittest.TestCase):
    """列表接口必须只加展示字段，不得污染完成判定字段。"""

    RUNTIME_FILE = "plugins.v2/cloudsubscribe/core/services/runtime.py"

    def setUp(self):
        with open(self.RUNTIME_FILE, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_finalized_fields_are_set(self):
        self.assertIn('task["finalized"] = True', self.source)
        self.assertIn('task["finalized_text"] = "已入库"', self.source)

    def test_completion_fields_are_not_touched(self):
        self.assertNotIn('task["completed"] = True', self.source)
        self.assertNotIn('task["state"] = "completed"', self.source)

    def test_pending_tasks_are_skipped(self):
        self.assertIn("if task.get(\"finalize_pending\"):", self.source)


if __name__ == "__main__":
    unittest.main()
