"""失败原因分类测试（1.7.109）。

覆盖：文本分类、永久性判定、写入/清空 helper、老记录回溯推断、
历史记录重试准入（失效链接禁用重试）、夸克分享失效细分状态。
"""

import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_module,
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


class ClassifyTest(unittest.TestCase):
    def test_share_invalid_variants(self):
        for reason in (
            "夸克网盘分享链接无效：分享不可用",
            "分享链接无效：已过期",
            "分享链接失效或已取消",
            "分享已删除",
            "提取码错误",
        ):
            self.assertEqual(
                failure.classify_failure(reason),
                failure.KIND_SHARE_INVALID,
                reason,
            )

    def test_transient_kinds(self):
        cases = {
            "请求过于频繁，请稍后再试": failure.KIND_RISK_CONTROL,
            "目标网盘空间不足": failure.KIND_TARGET_SPACE,
            "提交离线下载失败": failure.KIND_OFFLINE_SUBMIT,
            "文件已转存但后处理任务登记失败": failure.KIND_POSTPROCESS,
            "秒传未命中，已中止": failure.KIND_CHECKSUM_MISS,
            "读取分享文件超时": failure.KIND_NETWORK,
        }
        for reason, kind in cases.items():
            self.assertEqual(failure.classify_failure(reason), kind, reason)

    def test_unknown_reason(self):
        self.assertEqual(failure.classify_failure(""), failure.KIND_UNKNOWN)
        self.assertEqual(
            failure.classify_failure("莫名其妙的问题"), failure.KIND_UNKNOWN
        )

    def test_permanent_only_share_and_missing(self):
        self.assertTrue(failure.is_permanent(failure.KIND_SHARE_INVALID))
        self.assertTrue(failure.is_permanent(failure.KIND_FILE_MISSING))
        self.assertFalse(failure.is_permanent(failure.KIND_NETWORK))
        self.assertFalse(failure.is_permanent(""))


class RecordHelperTest(unittest.TestCase):
    def test_apply_failure_fills_three_fields(self):
        record = {"status": "处理中"}
        failure.apply_failure(record, "夸克网盘分享链接无效：已过期")
        self.assertEqual(record["status"], "失败")
        self.assertEqual(
            record["failure_kind"], failure.KIND_SHARE_INVALID
        )
        self.assertFalse(record["failure_retryable"])
        self.assertIn("分享链接无效", record["failure_reason"])

    def test_apply_failure_trusts_explicit_kind(self):
        record = {}
        failure.apply_failure(record, "读取分享文件失败", failure.KIND_SHARE_INVALID)
        self.assertEqual(record["failure_kind"], failure.KIND_SHARE_INVALID)
        self.assertFalse(record["failure_retryable"])

    def test_apply_failure_keeps_text_when_reason_empty(self):
        record = {}
        failure.apply_failure(record, "", failure.KIND_NETWORK)
        self.assertEqual(record["failure_reason"], failure.failure_label(
            failure.KIND_NETWORK
        ))
        self.assertTrue(record["failure_retryable"])

    def test_clear_failure_removes_all_three(self):
        record = {
            "failure_reason": "x", "failure_kind": "network",
            "failure_retryable": True,
        }
        failure.clear_failure(record)
        for key in ("failure_reason", "failure_kind", "failure_retryable"):
            self.assertNotIn(key, record)

    def test_legacy_record_inferred_from_text(self):
        """1.7.108 及以前的老记录只有 failure_reason。"""
        legacy = {"status": "失败", "failure_reason": "夸克网盘分享链接无效：分享不可用"}
        self.assertEqual(
            failure.failure_kind_of(legacy), failure.KIND_SHARE_INVALID
        )
        self.assertFalse(failure.failure_retryable_of(legacy))

    def test_explicit_field_wins_over_text(self):
        record = {
            "failure_reason": "读取分享文件超时",
            "failure_kind": failure.KIND_SHARE_INVALID,
        }
        self.assertEqual(
            failure.failure_kind_of(record), failure.KIND_SHARE_INVALID
        )

    def test_kinds_of_counts_failures_only(self):
        records = [
            {"status": "失败", "failure_reason": "分享链接无效"},
            {"status": "失败", "failure_kind": "network"},
            {"status": "成功"},
        ]
        counts = failure.kinds_of(records)
        self.assertEqual(counts, {"share_invalid": 1, "network": 1})


class RetryStateTest(unittest.TestCase):
    def _state(self, record):
        return HistoryService._history_retry_state(record)

    def test_expired_share_disables_retry(self):
        record = {
            "status": "失败",
            "share_url": "https://pan.quark.cn/s/abc",
            "failure_kind": failure.KIND_SHARE_INVALID,
            "failure_retryable": False,
        }
        can_retry, title = self._state(record)
        self.assertFalse(can_retry)
        self.assertEqual(title, "原分享已失效，需重新搜源")

    def test_legacy_expired_share_also_disabled(self):
        record = {
            "status": "失败",
            "share_url": "https://pan.quark.cn/s/abc",
            "failure_reason": "夸克网盘分享链接无效：已过期",
        }
        can_retry, _ = self._state(record)
        self.assertFalse(can_retry)

    def test_missing_file_disables_retry(self):
        record = {
            "status": "失败",
            "share_url": "https://pan.quark.cn/s/abc",
            "failure_reason": "原分享中未找到历史记录对应的源文件",
        }
        can_retry, title = self._state(record)
        self.assertFalse(can_retry)
        self.assertEqual(title, "分享内已找不到该文件，需重新搜源")

    def test_transient_failure_still_retryable(self):
        record = {
            "status": "失败",
            "share_url": "https://pan.quark.cn/s/abc",
            "failure_reason": "请求过于频繁，请稍后再试",
        }
        can_retry, title = self._state(record)
        self.assertTrue(can_retry)
        self.assertEqual(title, "重试此记录")

    def test_cross_failure_still_retryable(self):
        record = {
            "status": "失败",
            "transfer_mode": "cross",
            "share_url": "https://pan.quark.cn/s/abc",
            "failure_kind": failure.KIND_NETWORK,
            "failure_retryable": True,
        }
        can_retry, title = self._state(record)
        self.assertTrue(can_retry)
        self.assertEqual(title, "重新执行跨盘转存")


class _FakeTokenCache:
    def __init__(self):
        self._data = {}

    def get(self, key):
        return self._data.get(key)

    def set(self, key, value):
        self._data[key] = value


class _FakeClient:
    """按预置响应模拟夸克分享接口。"""

    SHARE_PAGE_BASE_URL = "https://share.example"
    SHARE_BASE_URL = "https://share.example"
    request_timeout = 3

    def __init__(self, response):
        self._response = response

    def request(self, *args, **kwargs):
        return dict(self._response)

    @staticmethod
    def data(response):
        return dict(response.get("data") or {})

    @staticmethod
    def is_success(response):
        return bool(response.get("success"))

    def wait_for_task(self, task_id):  # pragma: no cover - 本测试不触发
        return True


class QuarkShareStatusTest(unittest.TestCase):
    #: 本用例会覆盖这些模块名；用完后必须原样还原，避免污染后续用例
    #: （``cloudsubscribex.utils`` 被换成空包会让 resources.py 的
    #: ``MediaFileParser`` 导入失败）。
    _TOUCHED = (
        "cloudsubscribex.utils",
        "cloudsubscribex.utils.cache",
        "cloudsubscribex.core.cloud",
        "cloudsubscribex.drive.common",
        "cloudsubscribex.drive.quark",
        "cloudsubscribex.drive.quark.files",
        "cloudsubscribex.drive.quark.share",
    )

    @classmethod
    def setUpClass(cls):
        cls._saved = {
            key: sys.modules[key] for key in cls._TOUCHED
            if key in sys.modules
        }
        register_module("cloudsubscribex.utils", package=True)
        register_module(
            "cloudsubscribex.utils.cache",
            create_platform_ttl_cache=lambda *a, **k: _FakeTokenCache(),
        )
        load_module(
            "cloudsubscribex.core.cloud",
            "plugins.v2/cloudsubscribex/core/cloud.py",
        )
        load_module(
            "cloudsubscribex.drive.common",
            "plugins.v2/cloudsubscribex/drive/common.py",
        )
        register_module(
            "cloudsubscribex.drive.quark.files",
            page_size=100,
            cloud_file=MagicMock(),
            list_data=MagicMock(),
        )
        cls.share = load_module(
            "cloudsubscribex.drive.quark.share",
            "plugins.v2/cloudsubscribex/drive/quark/share.py",
        )

    @classmethod
    def tearDownClass(cls):
        for key in cls._TOUCHED:
            module = sys.modules.pop(key, None)
            parent_name, _, child = key.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is None or module is None:
                continue
            if key in cls._saved:
                continue
            if getattr(parent, child, None) is module:
                try:
                    delattr(parent, child)
                except AttributeError:  # pragma: no cover - 占位模块兜底
                    pass
        for key, module in cls._saved.items():
            sys.modules[key] = module
            parent_name, _, child = key.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is not None:
                setattr(parent, child, module)

    def _service(self, response):
        files = MagicMock()
        files.page_size = 100
        return self.share.QuarkShareService(_FakeClient(response), files)

    def test_expired_message_sets_flag(self):
        service = self._service({
            "success": False, "code": 41004, "message": "分享已过期",
        })
        status = service.check_share_status("https://pan.quark.cn/s/abc")
        self.assertFalse(status.is_valid)
        self.assertTrue(status.is_expired)
        self.assertEqual(status.status_text, "已过期")
        self.assertEqual(status.error_code, 41004)

    def test_cancelled_message_sets_flag(self):
        service = self._service({
            "success": False, "code": 41005, "message": "分享已取消",
        })
        status = service.check_share_status("https://pan.quark.cn/s/abc")
        self.assertTrue(status.is_cancelled)
        self.assertEqual(status.status_text, "已取消")

    def test_deleted_message_sets_flag(self):
        service = self._service({
            "success": False, "code": 41006, "message": "分享文件已删除",
        })
        status = service.check_share_status("https://pan.quark.cn/s/abc")
        self.assertTrue(status.is_deleted)
        self.assertEqual(status.status_text, "文件已删除")

    def test_valid_share(self):
        response = {
            "success": True,
            "data": {"stoken": "tok", "total": 7, "title": "示例剧"},
        }
        status = self._service(response).check_share_status(
            "https://pan.quark.cn/s/abc"
        )
        self.assertTrue(status.is_valid)
        self.assertEqual(status.file_count, 7)
        self.assertEqual(status.share_info["share_title"], "示例剧")

    def test_mark_reason_blank_message_is_noop(self):
        status = self.share.ShareLinkStatus()
        self.share.QuarkShareService._mark_share_reason(status, "   ")
        self.assertEqual(status.error_message, "")


if __name__ == "__main__":
    unittest.main()
