"""1.9.93：任务字典带 tmdb_id、跨盘文件名合并、已完成任务短暂保留。

直接驱动真实的 `SyncRuntimeService`（不走整插件 harness：那里的
`_COMPONENT_TYPES` 是占位 Mock，`_serialize_sync_tasks` 等职责方法
无法经宿主委托解析）。宿主用 SimpleNamespace 打桩，只提供运行态
委托读取的状态字段。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from threading import Event as ThreadEvent
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    install_app_mocks,
    install_core_delegation,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.config", "app.log", "app.schemas.types",
    "apscheduler", "apscheduler.schedulers", "apscheduler.schedulers.background",
    "apscheduler.triggers", "apscheduler.triggers.interval", "pytz",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.services",
)
install_core_delegation()
_MEDIA_TYPE = set_media_type(movie="电影", tv="电视剧")

_runtime = load_module(
    "cloudsubscribex.core.services.runtime",
    "plugins.v2/cloudsubscribex/core/services/runtime.py",
)

TV_VALUE = _MEDIA_TYPE.TV.value


def _sync_handler(**extra):
    handler = SimpleNamespace(
        subscription_budget_key=lambda sub: "budget-key-1",
        _strm_generate_enabled=False,
        _metadata_scraper=None,
        _local_resource_path=None,
    )
    for key, value in extra.items():
        setattr(handler, key, value)
    return handler


def _owner(sync_handler=None, cross_transfer_manager=None):
    return SimpleNamespace(
        _sync_tasks={},
        _sync_tasks_lock=threading.RLock(),
        _sync_handler=sync_handler or _sync_handler(),
        _runtime_revision_lock=threading.RLock(),
        _runtime_revision=0,
        _history_revision=0,
        _cross_transfer_manager=cross_transfer_manager,
        _task_local=None,
    )


def _service(owner=None):
    return _runtime.SyncRuntimeService(owner or _owner())


def _subscribe(**extra):
    subscribe = SimpleNamespace(
        id=35,
        name="律界战争",
        type=TV_VALUE,
        year="2025",
        season=1,
        tmdbid=12345,
        _cloudsubscribe_preparation={"aired_target_episodes": [5]},
        _transient_target=False,
    )
    for key, value in extra.items():
        setattr(subscribe, key, value)
    return subscribe


def _task(task_id, status, now, finished_ago=None, started_ago=None):
    return {
        "id": task_id,
        "subscribe_id": 35,
        "title": "律界战争",
        "media_type": "电视剧",
        "season": 1,
        "target_episodes": [],
        "status": status,
        "phase": "等待调度",
        "progress": 0,
        "transferred": 0,
        "queued_at": now - 120,
        "started_at": now - started_ago if started_ago is not None else None,
        "finished_at": now - finished_ago if finished_ago is not None else None,
        "stop_event": ThreadEvent(),
    }


class TestRegisterSyncTasksTmdbId(unittest.TestCase):
    def test_subscribe_tmdb_id_is_recorded(self):
        owner = _owner()
        _service(owner)._register_sync_tasks([_subscribe()])
        task = owner._sync_tasks["subscribe:35"]
        self.assertEqual(task["tmdb_id"], 12345)
        self.assertEqual(task["title"], "律界战争")

    def test_transient_target_tmdb_id_is_recorded(self):
        owner = _owner()
        transient = _subscribe(
            id=None, tmdbid=67890, _transient_target=True,
            _cloudsubscribe_preparation={},
        )
        _service(owner)._register_sync_tasks([transient])
        task = owner._sync_tasks["media:budget-key-1"]
        self.assertEqual(task["tmdb_id"], 67890)

    def test_missing_tmdb_id_records_none(self):
        owner = _owner()
        _service(owner)._register_sync_tasks([_subscribe(tmdbid=None)])
        self.assertIsNone(owner._sync_tasks["subscribe:35"]["tmdb_id"])

    def test_next_sync_purges_terminal_tasks(self):
        # 已完成任务保留到下一轮同步登记时清掉（下载页短暂可见完成态）。
        owner = _owner()
        now = time.time()
        owner._sync_tasks["subscribe:99"] = _task(
            "subscribe:99", "completed", now, finished_ago=60
        )
        _service(owner)._register_sync_tasks([_subscribe()])
        self.assertNotIn("subscribe:99", owner._sync_tasks)
        self.assertIn("subscribe:35", owner._sync_tasks)


class TestSerializeRuntimeTasksFileNames(unittest.TestCase):
    def test_merge_carries_child_file_names_without_changing_progress(self):
        now = time.time()
        child = {
            "id": "cross:3ad2a315dddc43b8baf1e4c0cfc7bc3e",
            "parent_task_id": "subscribe:35",
            "status": "transferring",
            "title": "律界战争 S01E05.mkv",
            "result_file_name": "",
            "total": 100,
            "transferred": 35,
            "progress": 35,
            "speed_bytes_per_second": 0,
            "stage_total": 0,
            "stage_transferred": 0,
            "phase": "正在从夸克网盘下载到本地缓存",
        }
        manager = SimpleNamespace(
            ACTIVE={"transferring", "downloading"},
            list=lambda: [child],
        )
        owner = _owner(cross_transfer_manager=manager)
        owner._sync_tasks["subscribe:35"] = _task(
            "subscribe:35", "transferring", now, started_ago=10
        )
        tasks = _service(owner)._serialize_runtime_tasks()
        self.assertEqual(len(tasks), 1)
        parent = tasks[0]
        self.assertEqual(parent["progress"], 35)
        self.assertEqual(parent["transfer_file_names"], ["律界战争 S01E05.mkv"])
        self.assertEqual(parent["transfer_file_count"], 1)
        self.assertEqual(
            parent["transfer_task_ids"],
            ["cross:3ad2a315dddc43b8baf1e4c0cfc7bc3e"],
        )


class TestSerializeSyncTasksRetention(unittest.TestCase):
    def test_completed_recently_is_retained_and_stale_is_dropped(self):
        now = time.time()
        owner = _owner()
        owner._sync_tasks = {
            "subscribe:1": _task("subscribe:1", "transferring", now, started_ago=10),
            "subscribe:2": _task("subscribe:2", "completed", now, finished_ago=60),
            "subscribe:3": _task("subscribe:3", "failed", now, finished_ago=60),
            "subscribe:4": _task("subscribe:4", "completed", now, finished_ago=3600),
            "subscribe:5": _task("subscribe:5", "weird", now),
        }
        ids = [task["id"] for task in _service(owner)._serialize_sync_tasks()]
        self.assertIn("subscribe:1", ids)
        self.assertIn("subscribe:2", ids)
        self.assertIn("subscribe:3", ids)
        self.assertNotIn("subscribe:4", ids)
        self.assertNotIn("subscribe:5", ids)


if __name__ == "__main__":
    unittest.main()
