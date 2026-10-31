"""转存状态机与超时清扫单测（2026-10-31 任务书 S1/S3）。"""

import sys
import threading
import unittest

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
    "app.utils", "app.utils.string",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudFile = __import__("unittest.mock").mock.MagicMock()
core_pkg.CloudDriveCapability = __import__("unittest.mock").mock.MagicMock()

state = load_module(
    "cloudsubscribex.core.state",
    "plugins.v2/cloudsubscribex/core/state.py",
)
history_module = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = history_module.HistoryService


class TestStateMachine(unittest.TestCase):
    def _record(self, status, **extra):
        return {"status": status, **extra}

    def test_processing_to_downloading(self):
        record = self._record(state.STATUS_PROCESSING)
        self.assertTrue(state.transition(record, state.STATUS_DOWNLOADING))
        self.assertEqual(record["status"], state.STATUS_DOWNLOADING)

    def test_processing_to_success_writes_stage(self):
        record = self._record(state.STATUS_PROCESSING)
        self.assertTrue(state.transition(
            record, state.STATUS_SUCCESS, stage=state.STAGE_DONE,
        ))
        self.assertEqual(record["stage"], state.STAGE_DONE)

    def test_processing_other_targets(self):
        for target in (
                state.STATUS_FAILED, state.STATUS_SKIPPED,
                state.STATUS_PENDING_CONFIRM,
        ):
            record = self._record(state.STATUS_PROCESSING)
            self.assertTrue(state.transition(record, target))
            self.assertEqual(record["status"], target)

    def test_downloading_to_processing_and_terminals(self):
        for target in (
                state.STATUS_PROCESSING, state.STATUS_SUCCESS,
                state.STATUS_FAILED, state.STATUS_SKIPPED,
                state.STATUS_PENDING_CONFIRM,
        ):
            record = self._record(state.STATUS_DOWNLOADING)
            self.assertTrue(state.transition(record, target))
            self.assertEqual(record["status"], target)

    def test_success_to_pending_is_allowed(self):
        record = self._record(state.STATUS_SUCCESS)
        self.assertTrue(state.transition(
            record, state.STATUS_PENDING_CONFIRM,
            stage=state.STAGE_VERIFYING,
        ))
        self.assertEqual(record["stage"], state.STAGE_VERIFYING)

    def test_success_to_failed_is_rejected_and_untouched(self):
        record = self._record(state.STATUS_SUCCESS, stage=state.STAGE_DONE)
        self.assertFalse(state.transition(record, state.STATUS_FAILED))
        self.assertEqual(record["status"], state.STATUS_SUCCESS)
        self.assertEqual(record["stage"], state.STAGE_DONE)

    def test_success_to_skipped_is_rejected(self):
        record = self._record(state.STATUS_SUCCESS)
        self.assertFalse(state.transition(record, state.STATUS_SKIPPED))
        self.assertEqual(record["status"], state.STATUS_SUCCESS)

    def test_failed_to_success_is_rejected_in_general_table(self):
        record = self._record(state.STATUS_FAILED)
        self.assertFalse(state.transition(record, state.STATUS_SUCCESS))

    def test_skipped_to_success_is_rejected_in_general_table(self):
        record = self._record(state.STATUS_SKIPPED)
        self.assertFalse(state.transition(record, state.STATUS_SUCCESS))

    def test_skipped_to_processing_is_allowed_for_retry(self):
        record = self._record(state.STATUS_SKIPPED)
        self.assertTrue(state.transition(record, state.STATUS_PROCESSING))

    def test_manual_success_from_failed_and_skipped(self):
        for origin in (state.STATUS_FAILED, state.STATUS_SKIPPED):
            record = self._record(origin)
            self.assertTrue(state.transition_manual(
                record, state.STATUS_SUCCESS, stage=state.STAGE_DONE,
            ))
            self.assertEqual(record["status"], state.STATUS_SUCCESS)
            self.assertEqual(record["stage"], state.STAGE_DONE)

    def test_manual_rejects_non_success_target(self):
        record = self._record(state.STATUS_FAILED)
        self.assertFalse(state.transition_manual(record, state.STATUS_SKIPPED))
        self.assertEqual(record["status"], state.STATUS_FAILED)

    def test_invalid_target_rejected(self):
        record = self._record(state.STATUS_PROCESSING)
        self.assertFalse(state.transition(record, "不存在"))
        self.assertEqual(record["status"], state.STATUS_PROCESSING)

    def test_failure_fields_untouched_on_rejected_transition(self):
        record = self._record(
            state.STATUS_SUCCESS, failure_kind="checksum_miss",
            failure_retryable=False,
        )
        self.assertFalse(state.transition(record, state.STATUS_FAILED))
        self.assertEqual(record["failure_kind"], "checksum_miss")
        self.assertFalse(record["failure_retryable"])

    def test_same_status_only_updates_stage(self):
        record = self._record(state.STATUS_PROCESSING, stage=state.STAGE_SEARCHING)
        self.assertTrue(state.transition(
            record, state.STATUS_PROCESSING, stage=state.STAGE_TRANSFERRING,
        ))
        self.assertEqual(record["stage"], state.STAGE_TRANSFERRING)

    def test_stage_label(self):
        self.assertEqual(state.stage_label(state.STAGE_POSTPROCESSING), "后处理")
        self.assertEqual(state.stage_label(""), "")


class _SweepService(HistoryService):
    def __init__(self, records, landed_infos, active=False):
        super().__init__(None)
        object.__setattr__(self, "_store", {
            "history": [dict(record) for record in records],
        })
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())
        object.__setattr__(self, "_landed_infos", landed_infos)
        object.__setattr__(self, "_active", active)
        object.__setattr__(self, "save_count", 0)

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value
        object.__setattr__(self, "save_count", self.save_count + 1)

    def _history_has_active_sync_job(self, record):
        return self._active

    def verify_landed_records(self, records):
        return {
            id(record): dict(self._landed_infos.get(record["time"], {}))
            for record in records
        }

    def _record_platform_transfer_histories(self, records):
        return None


def _stale_record(time_value, status="处理中", **extra):
    return {"time": time_value, "updated_at": 1000, "status": status, **extra}


class TestSweepStaleHistory(unittest.TestCase):
    def test_landed_true_marks_success(self):
        service = _SweepService(
            [_stale_record("t1")],
            {"t1": {"landed": True, "reason": "file_id_match"}},
        )
        self.assertEqual(service.sweep_stale_history(now=1000 + 3601), 1)
        record = service._store["history"][0]
        self.assertEqual(record["status"], state.STATUS_SUCCESS)
        self.assertEqual(record["stage"], state.STAGE_DONE)

    def test_landed_false_marks_pending(self):
        service = _SweepService(
            [_stale_record("t1", "下载中")],
            {"t1": {"landed": False, "reason": "missing_in_dir"}},
        )
        self.assertEqual(service.sweep_stale_history(now=1000 + 3601), 1)
        record = service._store["history"][0]
        self.assertEqual(record["status"], state.STATUS_PENDING_CONFIRM)
        self.assertEqual(record["stage"], state.STAGE_VERIFYING)

    def test_unverifiable_keeps_status(self):
        service = _SweepService(
            [_stale_record("t1")],
            {"t1": {"landed": None, "reason": "list_failed"}},
        )
        self.assertEqual(service.sweep_stale_history(now=1000 + 3601), 0)
        record = service._store["history"][0]
        self.assertEqual(record["status"], state.STATUS_PROCESSING)
        self.assertEqual(service.save_count, 0)

    def test_active_job_and_recent_record_are_skipped(self):
        service = _SweepService(
            [_stale_record("t1")],
            {"t1": {"landed": True}},
            active=True,
        )
        self.assertEqual(service.sweep_stale_history(now=1000 + 3601), 0)
        service = _SweepService(
            [_stale_record("t1")], {"t1": {"landed": True}},
        )
        self.assertEqual(service.sweep_stale_history(now=1000 + 60), 0)


class TestHistoryPageStageFields(unittest.TestCase):
    def test_stage_and_label_are_exposed(self):
        service = _SweepService([], {})
        fields = service._history_page_fields({
            "type": "电影", "title": "测试片", "file_name": "a.mkv",
            "status": state.STATUS_PROCESSING, "stage": state.STAGE_VERIFYING,
        })
        self.assertEqual(fields["stage"], state.STAGE_VERIFYING)
        self.assertEqual(fields["stage_label"], "校验")

    def test_empty_stage_yields_empty_label(self):
        service = _SweepService([], {})
        fields = service._history_page_fields({
            "type": "电影", "title": "测试片", "file_name": "a.mkv",
            "status": state.STATUS_SUCCESS,
        })
        self.assertEqual(fields["stage"], "")
        self.assertEqual(fields["stage_label"], "")

    def test_unknown_stage_yields_empty_label(self):
        service = _SweepService([], {})
        fields = service._history_page_fields({
            "type": "电影", "title": "测试片", "file_name": "a.mkv",
            "status": state.STATUS_SUCCESS, "stage": "bogus",
        })
        self.assertEqual(fields["stage"], "bogus")
        self.assertEqual(fields["stage_label"], "")


if __name__ == "__main__":
    unittest.main()
