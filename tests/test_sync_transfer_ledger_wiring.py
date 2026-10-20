"""SyncHandler 转存入口的账本接线测试（P1）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import ensure_package, install_app_mocks, load_module

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


class OwnerDelegator:
    pass


ensure_package("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
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
SyncHandler = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
).SyncHandler


class FakeLedger:
    def __init__(self, lease=None, reason="", success_result=True):
        self.lease = lease
        self.reason = reason
        self.success_result = success_result
        self.begin_kwargs = None
        self.success_kwargs = None
        self.failure_kwargs = None

    def begin(self, **kwargs):
        self.begin_kwargs = kwargs
        return self.lease, self.reason

    def heartbeat(self, lease, **kwargs):
        return True

    def success(self, lease, **kwargs):
        self.success_kwargs = kwargs
        return self.success_result

    def failure(self, lease, **kwargs):
        self.failure_kwargs = kwargs
        return True


def _lease(reused: bool = False):
    return SimpleNamespace(
        job_key="k" * 64, job_id="job-1", owner="owner-1", reused=reused,
    )


class SyncTransferLedgerWiringTests(unittest.TestCase):
    def _handler(
            self, *, enabled=True, lease=None, reason="", impl_result=True,
            impl_error=None):
        handler = SyncHandler.__new__(SyncHandler)
        handler._sync_job_ledger_enabled = enabled
        handler._cloud_drive = SimpleNamespace(key="115")
        handler._stop_requested = lambda: False
        handler._resource_provider_for_url = lambda url: SimpleNamespace(key="115")
        handler._transfer_file_impl = MagicMock(
            return_value=impl_result, side_effect=impl_error,
        )
        ledger = FakeLedger(lease=lease, reason=reason)
        handler._sync_job_ledger_instance = ledger
        return handler, ledger

    @staticmethod
    def _args():
        return (
            "https://115.com/s/example",
            {
                "id": "f1",
                "name": "第17集.mp4",
                "size": 1024,
                "sha1": "a" * 40,
                "subscribe_id": "42",
            },
            "/待整理",
            "第17集.mp4",
        )

    def test_flag_off_keeps_legacy_path(self):
        handler, ledger = self._handler(enabled=False)
        self.assertTrue(handler._transfer_file(*self._args()))
        handler._transfer_file_impl.assert_called_once()
        self.assertIsNone(ledger.begin_kwargs)

    def test_unclaimed_job_does_not_transfer(self):
        handler, ledger = self._handler(lease=None, reason="作业由其他 worker 持有")
        self.assertFalse(handler._transfer_file(*self._args()))
        handler._transfer_file_impl.assert_not_called()
        self.assertIn("作业由其他 worker 持有", ledger.reason)

    def test_reused_done_job_skips_transfer(self):
        handler, ledger = self._handler(lease=_lease(reused=True))
        self.assertTrue(handler._transfer_file(*self._args()))
        handler._transfer_file_impl.assert_not_called()
        self.assertIsNone(ledger.success_kwargs)

    def test_success_marks_job_done(self):
        handler, ledger = self._handler(lease=_lease())
        self.assertTrue(handler._transfer_file(*self._args()))
        handler._transfer_file_impl.assert_called_once()
        self.assertEqual(ledger.begin_kwargs["target_provider"], "115")
        self.assertEqual(ledger.begin_kwargs["source_provider"], "115")
        self.assertEqual(ledger.begin_kwargs["source"]["sha1"], "a" * 40)
        self.assertEqual(ledger.begin_kwargs["subscribe_id"], 42)
        self.assertEqual(
            ledger.begin_kwargs["verify_done"], handler._verify_sync_job_result,
        )
        self.assertEqual(ledger.success_kwargs["result_name"], "第17集.mp4")
        self.assertEqual(ledger.success_kwargs["result_size"], 1024)
        self.assertIsNone(ledger.failure_kwargs)

    def test_done_verifier_reports_exists_and_mismatch(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._cloud_directories = SimpleNamespace(
            resolve_directory=lambda _: SimpleNamespace(
                checked=True, directory_id="d1",
            ),
            list_directory=lambda _: SimpleNamespace(
                checked=True,
                files=(
                    SimpleNamespace(
                        name="第17集.mp4", size=1024, id="f1", is_directory=False,
                    ),
                ),
            ),
        )
        result = SimpleNamespace(
            target_path="/待整理", target_name="第17集.mp4",
            result_name="第17集.mp4", result_size=1024,
            result_file_id="f1",
        )
        self.assertEqual(handler._verify_sync_job_result(result), "exists")
        result.result_size = 2048
        self.assertEqual(handler._verify_sync_job_result(result), "mismatch")

    def test_done_verifier_reports_missing_and_unknown(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._cloud_directories = SimpleNamespace(
            resolve_directory=lambda _: SimpleNamespace(
                checked=True, directory_id="d1",
            ),
            list_directory=lambda _: SimpleNamespace(
                checked=True, files=(),
            ),
        )
        result = SimpleNamespace(
            target_path="/待整理", target_name="第17集.mp4",
            result_name="第17集.mp4", result_size=1024,
            result_file_id="",
        )
        self.assertEqual(handler._verify_sync_job_result(result), "missing")
        handler._cloud_directories.resolve_directory = lambda _: SimpleNamespace(
            checked=False, directory_id=None,
        )
        self.assertEqual(handler._verify_sync_job_result(result), "unknown")

    def test_false_result_is_retryable(self):
        handler, ledger = self._handler(lease=_lease(), impl_result=False)
        self.assertFalse(handler._transfer_file(*self._args()))
        self.assertTrue(ledger.failure_kwargs["retryable"])
        self.assertFalse(ledger.failure_kwargs["canceled"])

    def test_exception_marks_retryable_and_reraises(self):
        handler, ledger = self._handler(
            lease=_lease(), impl_error=RuntimeError("网络断开"),
        )
        with self.assertRaisesRegex(RuntimeError, "网络断开"):
            handler._transfer_file(*self._args())
        self.assertTrue(ledger.failure_kwargs["retryable"])
        self.assertIn("RuntimeError", ledger.failure_kwargs["error"])


if __name__ == "__main__":
    unittest.main()
