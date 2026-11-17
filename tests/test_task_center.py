"""任务中心聚合单测（v1.13.3 起聚合重试索引行：失败/处理中/跨盘成功待修复）。"""

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
state = _load_once(
    "cloudsubscribex.core.state",
    "plugins.v2/cloudsubscribex/core/state.py",
)
task_center = _load_once(
    "cloudsubscribex.core.task_center",
    "plugins.v2/cloudsubscribex/core/task_center.py",
)


RetryIndex = models.RetryIndex


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


def _index_row(
        record_id, status="处理中", stage="transferring", title="剧名",
        media_type="电视剧", updated_at=100, landed=None, **fields):
    return RetryIndex(
        record_id=record_id,
        record_time=str(updated_at),
        share_url="https://pan.example.com/s/abc",
        file_name=fields.get("file_name", "第1集.mkv"),
        tmdb_id=None,
        title=title,
        type=media_type,
        season=1,
        episode=1,
        year=None,
        source_file_name=fields.get("source_file_name", ""),
        file_size=fields.get("file_size", 0),
        source_sha1="",
        source_md5="",
        transfer_mode=fields.get("transfer_mode", ""),
        source_drive_key="",
        target_drive_key="115",
        cloud_dir=fields.get("cloud_dir", "/剧集"),
        finalize_key=None,
        status=status,
        stage=stage,
        failure_kind=fields.get("failure_kind", ""),
        failure_reason=fields.get("failure_reason", ""),
        failure_retryable=True,
        landed=landed,
        created_at=updated_at,
        updated_at=updated_at,
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

    def test_actions_follow_status(self):
        # 失败与待确认（跨盘成功待修复）可重试；处理中行没有可停的后处理任务，不给 stop。
        self.add(
            _index_row("r1", status="失败", updated_at=1),
            _index_row("r2", status="成功", landed=False, updated_at=2),
            _index_row("r3", status="处理中", updated_at=3),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["retry_index:r1"]["actions"], ["retry"])
        self.assertEqual(by_key["retry_index:r2"]["actions"], ["retry"])
        self.assertEqual(by_key["retry_index:r3"]["actions"], [])

    def test_repair_row_maps_to_pending_confirm(self):
        self.add(_index_row("r1", status="成功", landed=False, updated_at=1))
        task = self.collect()[0]
        self.assertEqual(task["status"], "待确认")
        self.assertEqual(task["actions"], ["retry"])

    def test_media_type_ignores_transfer_label(self):
        """索引行的 type 有时是「转存」，不能当成影视类型显示。"""
        self.add(
            _index_row("r1", media_type="转存", updated_at=1),
            _index_row("r2", media_type="电视剧", updated_at=2),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["retry_index:r1"]["media_type"], "电影")
        self.assertEqual(by_key["retry_index:r2"]["media_type"], "电视剧")

    def test_filename_title_falls_back_to_series_name(self):
        self.add(
            _index_row("r1", title="S01E19.2025.2160p.WEB-DL.HQ.DV.H265.mp4",
                     file_name="沧元图 - S01E19 - 第19集.mp4", updated_at=1),
            _index_row("r2", title="沧元图", file_name="沧元图 - S01E20 - 第20集.mp4",
                     updated_at=2),
        )
        by_key = {task["task_key"]: task for task in self.collect()}
        self.assertEqual(by_key["retry_index:r1"]["title"], "沧元图")
        self.assertEqual(by_key["retry_index:r2"]["title"], "沧元图")

    def test_limit_200_and_updated_desc(self):
        self.add(*[
            _index_row(f"r{index}", updated_at=index)
            for index in range(205)
        ])
        tasks = self.collect()
        self.assertEqual(len(tasks), 200)
        self.assertEqual(tasks[0]["updated_at"], 204)
        self.assertEqual(tasks[-1]["updated_at"], 5)

    def test_state_filters(self):
        self.add(
            _index_row("r1", status="处理中", updated_at=1),
            _index_row("r2", status="成功", landed=False, updated_at=2),
            _index_row("r3", status="失败", updated_at=3),
        )
        self.assertEqual({t["task_key"] for t in self.collect("active")},
                         {"retry_index:r1"})
        self.assertEqual({t["task_key"] for t in self.collect("waiting")},
                         set())
        self.assertEqual({t["task_key"] for t in self.collect("failed")},
                         {"retry_index:r3"})
        self.assertEqual({t["task_key"] for t in self.collect("pending")},
                         {"retry_index:r2"})
        self.assertEqual(len(self.collect("all")), 3)

    def test_empty_values_are_never_null(self):
        task = self.collect("all")
        self.assertEqual(task, [])
        self.add(_index_row("r1", status="处理中", updated_at=1))
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

    def test_stage_label_from_index(self):
        self.add(_index_row("r1", stage="postprocessing", updated_at=1))
        task = self.collect()[0]
        self.assertEqual(task["stage"], "postprocessing")
        self.assertEqual(task["stage_label"], "后处理")

    def test_title_precedence(self):
        self.add(_index_row("r1", title="历史标题", updated_at=1))
        self.assertEqual(self.collect()[0]["title"], "历史标题")

    def test_terminal_rows_are_excluded(self):
        # 跨盘成功且已落盘、成功但 landed 未知都不再列出；失败行仍列出（可重试）。
        self.add(
            _index_row("r1", status="成功", landed=True, updated_at=1),
            _index_row("r2", status="成功", landed=None, updated_at=2),
            _index_row("r3", status="失败", updated_at=3),
        )
        tasks = self.collect()
        self.assertEqual([t["task_key"] for t in tasks], ["retry_index:r3"])


if __name__ == "__main__":
    unittest.main()
