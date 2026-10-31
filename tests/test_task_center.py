"""任务中心聚合与重试退避/死信单测（2026-10-31 任务书 S1/S2）。"""

import os
import sys
import types
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock


CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PLUGINS_V2 = os.path.join(PROJECT_ROOT, "plugins.v2")
if PLUGINS_V2 not in sys.path:
    sys.path.insert(0, PLUGINS_V2)


def _plugin_dir(*parts):
    return os.path.join(PLUGINS_V2, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = sys.modules.get(name)
    if not isinstance(module, types.ModuleType) or getattr(module, "__spec__", None) is None:
        module = types.ModuleType(name)
        module.__spec__ = __import__("importlib.machinery", fromlist=["ModuleSpec"]).ModuleSpec(name, loader=None)
        sys.modules[name] = module
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is not None:
            setattr(parent, child, module)
    module.__path__ = [path]
    return module


def _load_once(module_name, relative_path):
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None) is not None:
        return existing
    return load_module(module_name, relative_path)


if "app" not in sys.modules or not isinstance(sys.modules.get("app"), types.ModuleType):
    app_module = types.ModuleType("app")
    app_module.__path__ = []
    sys.modules["app"] = app_module
register_mock("app.core.config", settings=SimpleNamespace(DB_TIMEOUT=30))
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.core.database", _plugin_dir("core", "database"))

sync_job_state = _load_once(
    "cloudsubscribex.core.database.sync_job_state",
    "plugins.v2/cloudsubscribex/core/database/sync_job_state.py",
)
models = _load_once(
    "cloudsubscribex.core.database.models",
    "plugins.v2/cloudsubscribex/core/database/models.py",
)
manager_module = _load_once(
    "cloudsubscribex.core.database.manager",
    "plugins.v2/cloudsubscribex/core/database/manager.py",
)
create_engine = manager_module.create_engine
sessionmaker = manager_module.sessionmaker
_load_once(
    "cloudsubscribex.core.database.sync_jobs",
    "plugins.v2/cloudsubscribex/core/database/sync_jobs.py",
)
state = _load_once(
    "cloudsubscribex.core.state",
    "plugins.v2/cloudsubscribex/core/state.py",
)
retry_policy = _load_once(
    "cloudsubscribex.core.retry_policy",
    "plugins.v2/cloudsubscribex/core/retry_policy.py",
)
ledger_module = _load_once(
    "cloudsubscribex.core.database.sync_job_ledger",
    "plugins.v2/cloudsubscribex/core/database/sync_job_ledger.py",
)
task_center = _load_once(
    "cloudsubscribex.core.task_center",
    "plugins.v2/cloudsubscribex/core/task_center.py",
)


HistoryRecord = models.HistoryRecord
SyncJob = models.SyncJob
SyncJobRepository = ledger_module.SyncJobRepository
SyncJobLedger = ledger_module.SyncJobLedger


class _SessionManager:
    def __init__(self, session_factory):
        self._factory = session_factory

    @contextmanager
    def session(self, write=False):
        db = self._factory()
        try:
            yield db
            if write:
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def _history(
        record_id, status="处理中", stage="transferring", title="剧名",
        media_type="电视剧", updated_at=100, **payload):
    return HistoryRecord(
        record_id=record_id,
        sort_index=0,
        group_key=record_id,
        record_time=str(updated_at),
        status=status,
        stage=stage,
        resource_type="cloud",
        source="test",
        task_types="|",
        title=title,
        file_name=payload.get("file_name", "第1集.mkv"),
        media_type=media_type,
        tmdb_id="",
        season=1,
        episode=1,
        subscribe_id=None,
        payload={
            "updated_at": updated_at,
            "cloud_dir": payload.get("cloud_dir", "/剧集"),
            "share_url": "https://pan.example.com/s/abc",
            "failure_reason": payload.get("failure_reason", ""),
            "stage_transferred": payload.get("transferred", 0),
            "stage_total": payload.get("total", 0),
            **payload,
        },
    )


def _job(job_id, state_value="transferring", history_id=None, updated_at=100,
         last_error="", attempt=1, max_attempts=3, next_retry_at=0):
    return SyncJob(
        job_key=f"key-{job_id}",
        job_id=job_id,
        content_key="",
        source_provider="115",
        source_file_id="f1",
        source_name="源文件.mkv",
        source_size=1,
        source_sha1="a" * 40,
        source_md5="",
        target_provider="115",
        target_path="/目标",
        target_name="目标.mkv",
        subscribe_id=None,
        history_id=history_id,
        state=state_value,
        attempt=attempt,
        max_attempts=max_attempts,
        next_retry_at=next_retry_at,
        lease_owner=None,
        lease_until=None,
        result_provider="",
        result_file_id="",
        result_name="结果.mkv",
        result_size=0,
        result_method="",
        last_error=last_error,
        last_error_at=None,
        payload={},
        created_at=updated_at,
        updated_at=updated_at,
        finished_at=None,
    )


class TaskCenterTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.CloudSubscribeBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.db = self.session_factory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def add(self, *rows):
        self.db.add_all(rows)
        self.db.commit()

    def collect(self, state_filter="all"):
        return task_center.collect_tasks(self.db, state_filter=state_filter)

    def test_history_and_job_merge_into_one_row(self):
        self.add(
            _history("r1", title="剧名", updated_at=200,
                     transferred=10, total=100),
            _job("j1", history_id="r1", updated_at=250, attempt=2),
        )
        tasks = self.collect()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertEqual(task["task_key"], "history:r1")
        self.assertEqual(task["status"], "处理中")
        self.assertEqual(task["attempt"], 2)
        self.assertEqual(task["target_path"], "/目标")
        self.assertEqual(task["title"], "剧名")
        self.assertEqual(task["progress"], {"transferred": 10, "total": 100})

    def test_unmatched_history_and_job_each_occupy_a_row(self):
        self.add(
            _history("r1", updated_at=100),
            _job("j1", history_id=None, updated_at=200),
        )
        tasks = self.collect()
        keys = {task["task_key"] for task in tasks}
        self.assertEqual(keys, {"history:r1", "job:j1"})

    def test_job_history_id_matches_numeric_id(self):
        record = _history("r1", updated_at=100)
        self.add(record)
        self.db.flush()
        self.add(_job("j1", history_id=str(record.id), updated_at=100))
        tasks = self.collect()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["task_key"], "history:r1")

    def test_pure_job_state_and_stage_mapping(self):
        cases = {
            "queued": ("处理中", "queued"),
            "transferring": ("下载中", "transferring"),
            "verifying": ("下载中", "verifying"),
            "finalizing": ("下载中", "postprocessing"),
            "failed": ("失败", ""),
            "needs_attention": ("待确认", ""),
            "retry_wait": ("处理中", ""),
        }
        for index, (job_state, expected) in enumerate(cases.items()):
            self.add(_job(f"j{index}", state_value=job_state, updated_at=index))
        by_state = {}
        for task in self.collect():
            by_state[task["status"], task["stage"]] = task
        for job_state, (status, stage) in cases.items():
            self.assertIn((status, stage), by_state)

    def test_actions_follow_status_and_history_presence(self):
        self.add(
            _history("r1", status="待确认", updated_at=1),
            _history("r3", status="处理中", updated_at=3),
            _history("r4", status="下载中", updated_at=4),
            _job("j1", state_value="failed", last_error="x", updated_at=5),
            _job("j2", state_value="failed", history_id="r2",
                 last_error="x", updated_at=6),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["history:r1"]["actions"], ["retry"])
        self.assertEqual(by_key["history:r2"]["actions"], ["retry"])
        self.assertEqual(by_key["history:r3"]["actions"], ["stop"])
        self.assertEqual(by_key["history:r4"]["actions"], ["stop"])
        self.assertEqual(by_key["job:j1"]["actions"], [])

    def test_media_type_ignores_transfer_label(self):
        """历史记录的 media_type 常是「转存」，不能当成影视类型显示。"""
        self.add(
            _history("r1", media_type="转存", type="转存", updated_at=1),
            _history("r2", media_type="转存", type="转存",
                     tmdb_type="电视剧", updated_at=2),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["history:r1"]["media_type"], "")
        self.assertEqual(by_key["history:r2"]["media_type"], "电视剧")

    def test_filename_title_falls_back_to_series_name(self):
        self.add(
            _history("r1", title="S01E19.2025.2160p.WEB-DL.HQ.DV.H265.mp4",
                     file_name="沧元图 - S01E19 - 第19集.mp4", updated_at=1),
            _history("r2", title="沧元图", file_name="沧元图 - S01E20 - 第20集.mp4",
                     updated_at=2),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["history:r1"]["title"], "沧元图")
        self.assertEqual(by_key["history:r2"]["title"], "沧元图")

    def test_dead_letter_job_gets_retry_action(self):
        self.add(
            _job("j1", state_value="failed",
                 last_error="dead_letter: 重试耗尽", updated_at=1),
            _job("j2", state_value="failed", last_error="普通失败",
                 updated_at=2),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["job:j1"]["actions"], ["retry"])
        self.assertEqual(by_key["job:j2"]["actions"], [])

    def test_limit_200_and_updated_desc(self):
        self.add(*[
            _job(f"j{index}", state_value="queued", updated_at=index)
            for index in range(205)
        ])
        tasks = self.collect()
        self.assertEqual(len(tasks), 200)
        self.assertEqual(tasks[0]["updated_at"], 204)
        self.assertEqual(tasks[-1]["updated_at"], 5)

    def test_state_filters(self):
        self.add(
            _history("r1", status="处理中", updated_at=1),
            _history("r2", status="待确认", updated_at=2),
            _job("j1", state_value="retry_wait", updated_at=3,
                 next_retry_at=999),
            _job("j2", state_value="failed", last_error="dead_letter: x",
                 updated_at=4),
        )
        self.assertEqual({t["task_key"] for t in self.collect("active")},
                         {"history:r1"})
        self.assertEqual({t["task_key"] for t in self.collect("waiting")},
                         {"job:j1"})
        self.assertEqual({t["task_key"] for t in self.collect("failed")},
                         {"job:j2"})
        self.assertEqual({t["task_key"] for t in self.collect("pending")},
                         {"history:r2"})
        self.assertEqual(len(self.collect("all")), 4)

    def test_dead_letter_two_rules(self):
        self.add(
            _job("j1", state_value="failed",
                 last_error="dead_letter: 重试耗尽", updated_at=1),
            _job("j2", state_value="retry_wait", attempt=3,
                 max_attempts=3, updated_at=2),
            _job("j3", state_value="retry_wait", attempt=1,
                 max_attempts=3, updated_at=3),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertTrue(by_key["job:j1"]["dead_letter"])
        self.assertTrue(by_key["job:j2"]["dead_letter"])
        self.assertFalse(by_key["job:j3"]["dead_letter"])

    def test_error_truncated_to_300(self):
        self.add(_job("j1", state_value="failed", last_error="x" * 500,
                      updated_at=1))
        self.assertEqual(len(self.collect()[0]["error"]), 300)

    def test_empty_values_are_never_null(self):
        task = self.collect("all")
        self.assertEqual(task, [])
        self.add(_job("j1", state_value="queued", updated_at=1))
        task = self.collect()[0]
        for key in (
                "task_key", "kind", "title", "media_type", "status",
                "status_label", "stage", "stage_label", "target_provider",
                "target_path", "target_name", "source_provider",
                "failure_kind", "failure_label", "error",
        ):
            self.assertIsInstance(task[key], str)
        for key in (
                "attempt", "max_attempts", "next_retry_at", "created_at",
                "updated_at",
        ):
            self.assertIsInstance(task[key], int)
        self.assertEqual(task["progress"], {"transferred": 0, "total": 0})

    def test_failed_job_uses_status_label_and_failure_label(self):
        self.add(_job(
            "j1", state_value="failed", updated_at=1,
            last_error="分享链接无效：已过期",
        ))
        task = self.collect()[0]
        self.assertEqual(task["status"], "失败")
        self.assertEqual(task["failure_kind"], "share_invalid")
        self.assertEqual(task["failure_label"], "分享链接失效")

    def test_retry_wait_exposes_next_retry_at(self):
        self.add(_job(
            "j1", state_value="retry_wait", updated_at=1,
            next_retry_at=123456,
        ))
        task = self.collect()[0]
        self.assertEqual(task["status"], "处理中")
        self.assertEqual(task["next_retry_at"], 123456)

    def test_stage_label_from_history(self):
        self.add(_history("r1", stage="postprocessing", updated_at=1))
        task = self.collect()[0]
        self.assertEqual(task["stage"], "postprocessing")
        self.assertEqual(task["stage_label"], "后处理")

    def test_title_precedence(self):
        self.add(
            _history("r1", title="历史标题", updated_at=1),
            _job("j1", history_id="r1", updated_at=1),
        )
        self.assertEqual(self.collect()[0]["title"], "历史标题")

    def test_terminal_jobs_are_excluded(self):
        self.add(
            _job("j1", state_value="done", updated_at=1),
            _job("j2", state_value="canceled", updated_at=2),
        )
        self.assertEqual(self.collect(), [])


class RetryBackoffTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        models.CloudSubscribeBase.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False,
        )
        self.repo = SyncJobRepository(manager=_SessionManager(self.session_factory))
        self.ledger = SyncJobLedger(self.repo)

    def tearDown(self):
        self.engine.dispose()

    def begin(self, name="文件.mkv", sha1="a" * 40):
        return self.ledger.begin(
            target_provider="115",
            target_path="/目标",
            target_name=name,
            source={"sha1": sha1, "file_id": "f1", "size": 1},
            source_provider="115",
            source_name=name,
        )

    def row(self, key):
        with self.session_factory() as db:
            return self.repo.get(key, db=db)

    def test_backoff_delay_sequence_and_cap(self):
        self.assertEqual(
            [retry_policy.backoff_delay(i) for i in range(1, 5)],
            [60, 120, 240, 480],
        )
        self.assertEqual(retry_policy.backoff_delay(5), 960)
        self.assertEqual(retry_policy.backoff_delay(6), 1800)
        self.assertEqual(retry_policy.backoff_delay(20), 1800)

    def test_claim_waits_until_next_retry_at(self):
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        self.assertTrue(self.ledger.failure(lease, error="网络抖动", now=1000))
        row = self.row(lease.job_key)
        self.assertEqual(row.next_retry_at, 1060)
        self.assertFalse(self.repo.claim(lease.job_key, "o1", now=1059))
        self.assertTrue(self.repo.claim(lease.job_key, "o2", now=1060))
        self.assertEqual(self.row(lease.job_key).attempt, 2)

    def test_failure_sets_retry_delay_by_attempt(self):
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        self.assertTrue(self.ledger.failure(lease, error="第一次", now=1000))
        self.assertEqual(self.row(lease.job_key).next_retry_at, 1060)
        row = self.row(lease.job_key)
        with self.session_factory() as db:
            row.next_retry_at = 0
            db.add(row)
            db.commit()
        self.assertTrue(self.repo.claim(lease.job_key, "o2", now=2000))
        second = ledger_module.SyncJobLease(
            lease.job_key, lease.job_id, "o2",
        )
        self.assertTrue(self.ledger.failure(second, error="第二次", now=2000))
        self.assertEqual(self.row(lease.job_key).next_retry_at, 2120)

    def test_max_attempts_becomes_dead_letter(self):
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        with self.session_factory() as db:
            row = self.repo.get(lease.job_key, db=db)
            row.attempt = 3
            row.max_attempts = 3
            db.add(row)
            db.commit()
        self.assertTrue(self.ledger.failure(lease, error="坏源", now=1000))
        row = self.row(lease.job_key)
        self.assertEqual(row.state, "failed")
        self.assertTrue(row.last_error.startswith("dead_letter: "))
        self.assertEqual(row.next_retry_at, 0)

    def test_manual_reopen_resets_attempt_and_delay(self):
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        with self.session_factory() as db:
            row = self.repo.get(lease.job_key, db=db)
            row.attempt = 3
            row.max_attempts = 3
            db.add(row)
            db.commit()
        self.assertTrue(self.ledger.failure(lease, error="坏源", now=1000))
        self.assertTrue(self.repo.reopen_failed(
            lease.job_key, "人工重开", now=1100,
        ))
        row = self.row(lease.job_key)
        self.assertEqual(row.state, "queued")
        self.assertEqual(row.attempt, 0)
        self.assertEqual(row.next_retry_at, 0)
        self.assertTrue(self.repo.claim(lease.job_key, "o3", now=1100))
        self.assertEqual(self.row(lease.job_key).attempt, 1)

    def test_begin_never_reopens_dead_letter(self):
        """正常同步不许自动复活死信，否则永久失败会变成无限重试。"""
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        with self.session_factory() as db:
            row = self.repo.get(lease.job_key, db=db)
            row.attempt = 3
            row.max_attempts = 3
            db.add(row)
            db.commit()
        self.ledger.failure(lease, error="坏源", now=1000)
        again, _ = self.begin()
        self.assertIsNone(again)
        row = self.row(lease.job_key)
        self.assertEqual(row.state, "failed")
        self.assertEqual(row.attempt, 3)

    def test_dead_letter_retry_only_after_explicit_reopen(self):
        lease, reason = self.begin()
        self.assertIsNotNone(lease, reason)
        with self.session_factory() as db:
            row = self.repo.get(lease.job_key, db=db)
            row.attempt = 3
            row.max_attempts = 3
            db.add(row)
            db.commit()
        self.ledger.failure(lease, error="坏源", now=1000)
        self.assertTrue(self.repo.reopen_dead_letter(
            lease.job_key, "用户手动重试", now=1100,
        ))
        again, reason = self.begin()
        self.assertIsNotNone(again, reason)
        self.assertEqual(self.row(lease.job_key).attempt, 1)

    def test_is_dead_letter_old_retry_wait(self):
        self.assertTrue(retry_policy.is_dead_letter(
            "retry_wait", "", 3, 3,
        ))
        self.assertFalse(retry_policy.is_dead_letter(
            "retry_wait", "", 2, 3,
        ))
        self.assertTrue(retry_policy.is_dead_letter(
            "failed", "dead_letter: x", 1, 3,
        ))
        self.assertFalse(retry_policy.is_dead_letter(
            "failed", "普通失败", 1, 3,
        ))


if __name__ == "__main__":
    unittest.main()
