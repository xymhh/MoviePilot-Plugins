"""同步作业账本：幂等键、状态机、CAS 与租约。"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import types
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module


def _import_real_sqlalchemy():
    """其他用例会把 ``sqlalchemy`` 注册成占位包，这里恢复真实依赖。"""
    mocked = sys.modules.get("sqlalchemy")
    if mocked is not None and (
        not isinstance(mocked, types.ModuleType)
        or getattr(mocked, "__spec__", None) is None
    ):
        for name in list(sys.modules):
            if name == "sqlalchemy" or name.startswith("sqlalchemy."):
                sys.modules.pop(name, None)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    return create_engine, sessionmaker


create_engine, sessionmaker = _import_real_sqlalchemy()

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PLUGINS_V2 = os.path.join(PROJECT_ROOT, "plugins.v2")
if PLUGINS_V2 not in sys.path:
    sys.path.insert(0, PLUGINS_V2)


def _plugin_dir(*parts):
    return os.path.join(PLUGINS_V2, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_mock("app.core.config", settings=SimpleNamespace(DB_TIMEOUT=30))
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.core.database", _plugin_dir("core", "database"))

state_module = load_module(
    "cloudsubscribex.core.database.sync_job_state",
    "plugins.v2/cloudsubscribex/core/database/sync_job_state.py",
)
models_module = load_module(
    "cloudsubscribex.core.database.models",
    "plugins.v2/cloudsubscribex/core/database/models.py",
)
load_module(
    "cloudsubscribex.core.database.manager",
    "plugins.v2/cloudsubscribex/core/database/manager.py",
)
sync_jobs_module = load_module(
    "cloudsubscribex.core.database.sync_jobs",
    "plugins.v2/cloudsubscribex/core/database/sync_jobs.py",
)
sync_job_ledger_module = load_module(
    "cloudsubscribex.core.database.sync_job_ledger",
    "plugins.v2/cloudsubscribex/core/database/sync_job_ledger.py",
)

CloudSubscribeBase = models_module.CloudSubscribeBase
SyncJobRepository = sync_jobs_module.SyncJobRepository
SyncJobLedger = sync_job_ledger_module.SyncJobLedger
JOB_QUEUED = state_module.JOB_QUEUED
JOB_TRANSFERRING = state_module.JOB_TRANSFERRING
JOB_VERIFYING = state_module.JOB_VERIFYING
JOB_FINALIZING = state_module.JOB_FINALIZING
JOB_RETRY_WAIT = state_module.JOB_RETRY_WAIT
JOB_DONE = state_module.JOB_DONE
JOB_FAILED = state_module.JOB_FAILED
JOB_NEEDS_ATTENTION = state_module.JOB_NEEDS_ATTENTION
JOB_CANCELED = state_module.JOB_CANCELED
DONE_CHECK_EXISTS = sync_job_ledger_module.DONE_CHECK_EXISTS
DONE_CHECK_MISSING = sync_job_ledger_module.DONE_CHECK_MISSING
DONE_CHECK_MISMATCH = sync_job_ledger_module.DONE_CHECK_MISMATCH
DONE_CHECK_UNKNOWN = sync_job_ledger_module.DONE_CHECK_UNKNOWN


class SyncJobKeyTests(unittest.TestCase):
    def test_normalize_path_is_stable(self):
        normalize_path = state_module.normalize_path
        self.assertEqual(normalize_path("  //剧集//第1季/  "), "/剧集/第1季")
        self.assertEqual(normalize_path("/"), "/")
        self.assertEqual(normalize_path(""), "/")
        self.assertEqual(normalize_path(None), "/")

    def test_normalize_name_only_accepts_basename(self):
        normalize_name = state_module.normalize_name
        self.assertEqual(normalize_name("  第17集.mp4  "), "第17集.mp4")
        self.assertEqual(normalize_name("Show.S01E01.mkv"), "Show.S01E01.mkv")
        for value in ("", "  ", "a/b.mp4", "a\\b.mp4", "..", "."):
            with self.assertRaises(ValueError, msg=value):
                normalize_name(value)

    def test_content_key_prefers_sha1_then_md5_then_id(self):
        content_key = state_module.content_key
        sha1 = "A" * 40
        md5 = "B" * 32
        self.assertEqual(
            content_key({"sha1": sha1, "md5": md5}, provider="115"),
            f"sha1:{sha1.lower()}",
        )
        self.assertEqual(
            content_key({"sha1": "bad", "md5": md5}, provider="115"),
            f"md5:{md5.lower()}",
        )
        self.assertEqual(
            content_key({"file_id": "f1", "size": "1024"}, provider="115"),
            "id:115:f1:1024",
        )
        with self.assertRaises(ValueError):
            content_key({}, provider="115")

    def test_same_content_maps_to_same_key(self):
        build_job_key = state_module.build_job_key
        source_a = {"sha1": "C" * 40, "file_id": "a"}
        source_b = {"sha1": "c" * 40, "file_id": "b"}
        first = build_job_key("115", "/待整理/", "第17集.mp4", source_a)
        second = build_job_key("115", "待整理", " 第17集.mp4 ", source_b)
        self.assertEqual(first, second)

    def test_target_name_and_content_change_key(self):
        build_job_key = state_module.build_job_key
        source = {"sha1": "C" * 40}
        base = build_job_key("115", "/待整理", "第17集.mp4", source)
        renamed = build_job_key("115", "/待整理", "第18集.mp4", source)
        other_content = build_job_key(
            "115", "/待整理", "第17集.mp4", {"sha1": "D" * 40}
        )
        other_target = build_job_key("aliyun", "/待整理", "第17集.mp4", source)
        self.assertEqual(len({base, renamed, other_content, other_target}), 4)
        self.assertEqual(len(base), 64)

    def test_transition_table(self):
        allowed = {
            (JOB_QUEUED, JOB_TRANSFERRING),
            (JOB_QUEUED, JOB_CANCELED),
            (JOB_TRANSFERRING, JOB_VERIFYING),
            (JOB_TRANSFERRING, JOB_RETRY_WAIT),
            (JOB_TRANSFERRING, JOB_FAILED),
            (JOB_TRANSFERRING, JOB_NEEDS_ATTENTION),
            (JOB_VERIFYING, JOB_FINALIZING),
            (JOB_VERIFYING, JOB_RETRY_WAIT),
            (JOB_VERIFYING, JOB_NEEDS_ATTENTION),
            (JOB_FINALIZING, JOB_DONE),
            (JOB_FINALIZING, JOB_RETRY_WAIT),
            (JOB_FINALIZING, JOB_NEEDS_ATTENTION),
            (JOB_RETRY_WAIT, JOB_QUEUED),
            (JOB_RETRY_WAIT, JOB_FAILED),
            (JOB_RETRY_WAIT, JOB_CANCELED),
            (JOB_DONE, JOB_QUEUED),
            (JOB_DONE, JOB_NEEDS_ATTENTION),
        }
        for from_state, to_state in allowed:
            self.assertTrue(
                state_module.can_transition(from_state, to_state),
                f"{from_state} -> {to_state}",
            )
        for from_state, to_state in {
            (JOB_QUEUED, JOB_DONE),
            (JOB_DONE, JOB_FAILED),
            (JOB_FAILED, JOB_RETRY_WAIT),
            (JOB_NEEDS_ATTENTION, JOB_QUEUED),
            (JOB_CANCELED, JOB_TRANSFERRING),
            ("unknown", JOB_QUEUED),
        }:
            self.assertFalse(state_module.can_transition(from_state, to_state))
            with self.assertRaises(ValueError):
                state_module.assert_transition(from_state, to_state)

    def test_terminal_states(self):
        for state in (
            JOB_DONE, JOB_FAILED, JOB_NEEDS_ATTENTION, JOB_CANCELED,
        ):
            self.assertTrue(state_module.is_terminal(state))
        self.assertFalse(state_module.is_terminal(JOB_QUEUED))
        self.assertFalse(state_module.is_terminal(JOB_RETRY_WAIT))


class SyncJobRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        CloudSubscribeBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()
        self.repo = SyncJobRepository(manager=None)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _values(self, key="a", **overrides):
        values = {
            "job_key": key * 64,
            "job_id": str(uuid.uuid4()),
            "content_key": f"sha1:{'a' * 40}",
            "source_provider": "115",
            "source_file_id": "f1",
            "source_name": "第17集.mp4",
            "source_size": 1024,
            "source_sha1": "a" * 40,
            "target_provider": "115",
            "target_path": "/待整理",
            "target_name": "第17集.mp4",
            "state": JOB_QUEUED,
            "attempt": 0,
            "max_attempts": 3,
            "payload": {},
        }
        values.update(overrides)
        return values

    def _create(self, values):
        row, created = self.repo.create_or_get(values, db=self.db)
        self.db.commit()
        self.assertTrue(created)
        return row

    def test_create_is_idempotent(self):
        values = self._values()
        first = self._create(values)
        second, created = self.repo.create_or_get(
            {**values, "job_id": str(uuid.uuid4())}, db=self.db
        )
        self.db.commit()
        self.assertFalse(created)
        self.assertEqual(second.job_id, first.job_id)
        self.assertEqual(self.repo.get(values["job_key"], db=self.db).job_id, first.job_id)

    def test_claim_is_exclusive(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        self.db.commit()
        row = self.repo.get(key, db=self.db)
        self.assertEqual(row.state, JOB_TRANSFERRING)
        self.assertEqual(row.attempt, 1)
        self.assertEqual(row.lease_until, 1060)
        self.assertFalse(
            self.repo.claim(key, "worker-b", ttl_seconds=60, now=1001, db=self.db)
        )

    def test_expired_lease_can_be_reclaimed(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        self.db.commit()
        self.assertEqual(self.repo.reclaim_expired(now=1061, db=self.db), 1)
        self.db.commit()
        self.assertEqual(self.repo.get(key, db=self.db).state, JOB_RETRY_WAIT)
        self.assertTrue(
            self.repo.claim(key, "worker-b", ttl_seconds=60, now=1062, db=self.db)
        )
        self.db.commit()
        row = self.repo.get(key, db=self.db)
        self.assertEqual(row.state, JOB_TRANSFERRING)
        self.assertEqual(row.attempt, 2)

    def test_exhausted_lease_marks_failed(self):
        values = self._values(max_attempts=1)
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        self.db.commit()
        self.assertEqual(self.repo.reclaim_expired(now=1061, db=self.db), 1)
        self.db.commit()
        row = self.repo.get(key, db=self.db)
        self.assertEqual(row.state, JOB_FAILED)
        self.assertEqual(row.finished_at, 1061)
        self.assertFalse(
            self.repo.claim(key, "worker-b", ttl_seconds=60, now=1062, db=self.db)
        )

    def test_transition_requires_owner_and_valid_edge(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        self.db.commit()
        self.assertTrue(
            self.repo.transition(
                key, JOB_TRANSFERRING, JOB_VERIFYING,
                owner="worker-a", now=1001, db=self.db,
            )
        )
        self.db.commit()
        self.assertFalse(
            self.repo.transition(
                key, JOB_VERIFYING, JOB_FINALIZING,
                owner="worker-b", now=1002, db=self.db,
            )
        )
        self.assertTrue(
            self.repo.transition(
                key, JOB_VERIFYING, JOB_FINALIZING,
                owner="worker-a", now=1003, db=self.db,
            )
        )
        self.assertTrue(
            self.repo.transition(
                key, JOB_FINALIZING, JOB_DONE,
                owner="worker-a", now=1004, db=self.db,
                result_file_id="file-1",
                result_name="第17集.mp4",
                result_size=1024,
                result_method="relay",
            )
        )
        self.db.commit()
        row = self.repo.get(key, db=self.db)
        self.assertEqual(row.state, JOB_DONE)
        self.assertEqual(row.result_file_id, "file-1")
        self.assertEqual(row.finished_at, 1004)
        with self.assertRaises(ValueError):
            self.repo.transition(
                key, JOB_DONE, JOB_FAILED, owner="worker-a", db=self.db
            )

    def test_reopen_done_requires_observed_version(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        for from_state, to_state in (
            (JOB_TRANSFERRING, JOB_VERIFYING),
            (JOB_VERIFYING, JOB_FINALIZING),
            (JOB_FINALIZING, JOB_DONE),
        ):
            self.assertTrue(
                self.repo.transition(
                    key, from_state, to_state,
                    owner="worker-a", now=1004, db=self.db,
                    result_name="第17集.mp4", result_size=1024,
                )
            )
        self.db.commit()

        # 回查后别人又改过这条记录：旧观察结果不能重开，避免把新结果再传一遍。
        self.assertFalse(
            self.repo.reopen_done(
                key, "目标文件不存在", now=1005,
                expected_updated_at=999, db=self.db,
            )
        )
        self.db.commit()
        self.assertEqual(self.repo.get(key, db=self.db).state, JOB_DONE)

        self.assertTrue(
            self.repo.reopen_done(
                key, "目标文件不存在", now=1006,
                expected_updated_at=1004, db=self.db,
            )
        )
        self.db.commit()
        row = self.repo.get(key, db=self.db)
        self.assertEqual(row.state, JOB_QUEUED)
        self.assertEqual(row.attempt, 0)
        self.assertEqual(row.result_name, "")
        self.assertIsNone(row.finished_at)

    def test_heartbeat_only_for_owner(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.claim(key, "worker-a", ttl_seconds=60, now=1000, db=self.db)
        )
        self.db.commit()
        self.assertTrue(
            self.repo.heartbeat(key, "worker-a", ttl_seconds=60, now=1030, db=self.db)
        )
        self.db.commit()
        self.assertEqual(self.repo.get(key, db=self.db).lease_until, 1090)
        self.assertFalse(
            self.repo.heartbeat(key, "worker-b", ttl_seconds=60, now=1031, db=self.db)
        )

    def test_delete_terminal_before_cutoff(self):
        values = self._values()
        self._create(values)
        key = values["job_key"]
        self.assertTrue(
            self.repo.transition(
                key, JOB_QUEUED, JOB_CANCELED, now=1000, db=self.db,
            )
        )
        self.db.commit()
        self.assertEqual(self.repo.delete_terminal_before(1001, db=self.db), 1)
        self.db.commit()
        self.assertIsNone(self.repo.get(key, db=self.db))

    def test_job_key_is_sha256_shape(self):
        values = self._values()
        self._create(values)
        self.assertEqual(len(values["job_key"]), 64)
        int(values["job_key"], 16)

    def test_content_key_matches_state_helper(self):
        values = self._values()
        expected = state_module.content_key(
            {"sha1": values["source_sha1"]}, provider="115"
        )
        self.assertEqual(values["content_key"], expected)


class _SessionManager:
    """给 SyncJobLedger 提供与生产一致的短会话。"""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    @contextmanager
    def session(self, write: bool = False):
        db = self._session_factory()
        try:
            yield db
            if write:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class SyncJobLedgerTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        CloudSubscribeBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.repo = SyncJobRepository(
            manager=_SessionManager(self.session_factory)
        )
        self.ledger = SyncJobLedger(self.repo)

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _begin(
            ledger, *, target_name="第17集.mp4", sha1="a" * 40,
            verify_done=None,
    ):
        return ledger.begin(
            target_provider="115",
            target_path="/待整理",
            target_name=target_name,
            source={"sha1": sha1, "file_id": "f1", "size": 1024},
            source_provider="115",
            source_name="第17集.mp4",
            verify_done=verify_done,
        )

    def _done(self):
        lease, reason = self._begin(self.ledger)
        self.assertIsNotNone(lease, reason)
        self.assertTrue(
            self.ledger.success(
                lease, result_name="第17集.mp4", result_size=1024,
            )
        )
        return lease.job_key

    def _row(self, job_key):
        with self.session_factory() as db:
            return self.repo.get(job_key, db=db)

    def test_begin_claims_once_and_success_is_reused(self):
        first, reason = self._begin(self.ledger)
        self.assertIsNotNone(first, reason)
        self.assertFalse(first.reused)
        self.assertEqual(self._row(first.job_key).state, JOB_TRANSFERRING)

        second, reason = self._begin(self.ledger)
        self.assertIsNone(second, reason)
        self.assertIn("持有", reason)

        self.assertTrue(
            self.ledger.success(
                first, result_name="第17集.mp4", result_size=1024,
            )
        )
        row = self._row(first.job_key)
        self.assertEqual(row.state, JOB_DONE)
        self.assertEqual(row.result_name, "第17集.mp4")
        self.assertEqual(row.result_size, 1024)

        reused, reason = self._begin(self.ledger)
        self.assertIsNotNone(reused, reason)
        self.assertTrue(reused.reused)

    def test_done_exists_is_reused_after_verification(self):
        key = self._done()
        seen = []

        def verify(result):
            seen.append(result)
            return DONE_CHECK_EXISTS

        lease, reason = self._begin(self.ledger, verify_done=verify)
        self.assertIsNotNone(lease, reason)
        self.assertTrue(lease.reused)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].result_name, "第17集.mp4")
        self.assertEqual(seen[0].result_size, 1024)
        self.assertEqual(self._row(key).state, JOB_DONE)

    def test_done_missing_reopens_and_claims(self):
        key = self._done()
        lease, reason = self._begin(
            self.ledger, verify_done=lambda _: DONE_CHECK_MISSING,
        )
        self.assertIsNotNone(lease, reason)
        self.assertFalse(lease.reused)
        row = self._row(key)
        self.assertEqual(row.state, JOB_TRANSFERRING)
        self.assertEqual(row.attempt, 1)
        self.assertEqual(row.result_name, "")
        self.assertEqual(row.result_size, 0)
        self.assertIn("目标文件不存在", row.last_error or "")
        self.assertIsNone(row.finished_at)

        self.assertTrue(
            self.ledger.success(
                lease, result_name="第17集.mp4", result_size=1024,
            )
        )
        row = self._row(key)
        self.assertEqual(row.state, JOB_DONE)
        self.assertEqual(row.last_error, "")

    def test_done_unknown_does_not_reuse_or_retransfer(self):
        key = self._done()
        lease, reason = self._begin(
            self.ledger, verify_done=lambda _: DONE_CHECK_UNKNOWN,
        )
        self.assertIsNone(lease)
        self.assertIn("无法确认", reason)
        row = self._row(key)
        self.assertEqual(row.state, JOB_DONE)
        self.assertEqual(row.attempt, 1)

    def test_done_mismatch_marks_needs_attention(self):
        key = self._done()
        lease, reason = self._begin(
            self.ledger, verify_done=lambda _: DONE_CHECK_MISMATCH,
        )
        self.assertIsNone(lease)
        self.assertIn("待处理", reason)
        row = self._row(key)
        self.assertEqual(row.state, JOB_NEEDS_ATTENTION)
        self.assertIn("不一致", row.last_error or "")

    def test_done_verifier_exception_does_not_reuse(self):
        key = self._done()

        def verify(_):
            raise RuntimeError("网盘查询超时")

        lease, reason = self._begin(self.ledger, verify_done=verify)
        self.assertIsNone(lease)
        self.assertIn("无法确认", reason)
        self.assertEqual(self._row(key).state, JOB_DONE)

    def test_heartbeat_extends_lease(self):
        lease, reason = self._begin(self.ledger)
        self.assertIsNotNone(lease, reason)
        self.assertTrue(self.ledger.heartbeat(lease, ttl_seconds=60))
        self.assertEqual(
            self._row(lease.job_key).lease_owner, lease.owner,
        )

    def test_retryable_failure_can_be_claimed_again(self):
        first, reason = self._begin(self.ledger)
        self.assertIsNotNone(first, reason)
        self.assertTrue(
            self.ledger.failure(first, error="网络抖动", retryable=True)
        )
        self.assertEqual(self._row(first.job_key).state, JOB_RETRY_WAIT)

        second, reason = self._begin(self.ledger)
        self.assertIsNotNone(second, reason)
        self.assertFalse(second.reused)
        self.assertEqual(self._row(second.job_key).attempt, 2)

    def test_canceled_failure_is_terminal(self):
        lease, reason = self._begin(self.ledger)
        self.assertIsNotNone(lease, reason)
        self.assertTrue(
            self.ledger.failure(
                lease, error="用户停止", retryable=False, canceled=True,
            )
        )
        self.assertEqual(self._row(lease.job_key).state, JOB_CANCELED)

    def test_invalid_source_is_rejected_without_enqueue(self):
        lease, reason = self.ledger.begin(
            target_provider="115",
            target_path="/待整理",
            target_name="第17集.mp4",
            source={},
            source_provider="115",
        )
        self.assertIsNone(lease)
        self.assertIn("身份无效", reason)

    def test_empty_target_name_is_rejected(self):
        lease, reason = self._begin(self.ledger, target_name="")
        self.assertIsNone(lease)
        self.assertIn("目标文件名不能为空", reason)

    def test_same_content_different_target_gets_different_key(self):
        first, reason = self._begin(self.ledger)
        self.assertIsNotNone(first, reason)
        second, reason = self._begin(self.ledger, target_name="第18集.mp4")
        self.assertIsNotNone(second, reason)
        self.assertNotEqual(first.job_key, second.job_key)


class SyncJobLedgerConcurrencyTests(unittest.TestCase):
    """真实多线程抢同一目标：只能有一个 worker 拿到租约。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmpdir.name, "sync_jobs.db")
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        with self.engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        CloudSubscribeBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.repo = SyncJobRepository(
            manager=_SessionManager(self.session_factory)
        )
        self.ledger = SyncJobLedger(self.repo)

    def tearDown(self):
        self.engine.dispose()
        self.tmpdir.cleanup()

    def _begin(self):
        return self.ledger.begin(
            target_provider="115",
            target_path="/待整理",
            target_name="第17集.mp4",
            source={"sha1": "e" * 40, "file_id": "f1", "size": 1024},
            source_provider="115",
            source_name="第17集.mp4",
        )

    def test_two_threads_claim_same_job_once(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(self._begin())
            except Exception as error:  # pragma: no cover - 失败时打印原始异常
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertFalse(errors, errors)
        self.assertEqual(len(results), 2)
        claimed = [item for item in results if item[0] is not None]
        waiting = [item for item in results if item[0] is None]
        self.assertEqual(len(claimed), 1, results)
        self.assertEqual(len(waiting), 1, results)
        self.assertIn("持有", waiting[0][1])

        with self.session_factory() as db:
            row = self.repo.get(claimed[0][0].job_key, db=db)
        self.assertEqual(row.state, JOB_TRANSFERRING)
        self.assertEqual(row.attempt, 1)

        self.assertTrue(
            self.ledger.success(
                claimed[0][0], result_name="第17集.mp4", result_size=1024,
            )
        )
        reused, reason = self._begin()
        self.assertIsNotNone(reused, reason)
        self.assertTrue(reused.reused)


if __name__ == "__main__":
    unittest.main()
