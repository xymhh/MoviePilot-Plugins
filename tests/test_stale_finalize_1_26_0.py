"""v1.26.0：下载页 95% 残留卡修复（后处理收尾静默卡住）。

根因（见 ~/workspace/.pytest-tmp/stale95_root_cause.md）：
- (f) dest_missed 死循环：MP 历史已成功但 dest 查无文件时只复查，
  不计 fail、不 abort、不走任何超时；
- (e) 重登记刷新时钟：_build_pending_record 的 fresh_attempt 重置全部时钟。

覆盖：
- dest_missed：模糊定位命中时走正常流程不误 commit、命中不到时以 MP 历史
  为准直接 commit（STRM 尽量生成、生成不了记 WARNING）；
- 绝对过期：超 6 小时未结算进失败记录，first_seen_at 不被重登记重置，
  老记录缺 first_seen_at 时回填 created_at、本轮不直接过期；
- 单项异常不拖垮同组（计一次失败并调度重试，两次即判死 abort）；
- 批次初始化/分组/单组异常被记下（ERROR + 带待处理数的 result），不静默；
- INFO 心跳：完成/失败数>0 或待处理数变化时记，监控启动/停止提为 INFO；
- api_offline_tasks 合并分享类 pending 记录（合成项不带 id，走 pending: 键）。
"""

import datetime
import logging
import os
import sys
import unittest
from pathlib import Path
from threading import Lock, RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import load_module, set_media_type  # noqa: E402

try:
    from test_finalize_mp_wait_1_12_6 import (
        DEST_PARENT,
        DEST_PATH,
        FILE_NAME,
        MEDIA_DIR,
        NOW,
        DummyMpWaitService,
        FakeTracker,
        _ctx,
        _item,
        _service_module,
        _stub,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import (
        DEST_PARENT,
        DEST_PATH,
        FILE_NAME,
        MEDIA_DIR,
        NOW,
        DummyMpWaitService,
        FakeTracker,
        _ctx,
        _item,
        _service_module,
        _stub,
    )

SyncHandler = _service_module.SyncHandler
_postprocess_module = sys.modules["cloudsubscribex.handlers.sync.postprocess"]
DirectoryFileIndex = _postprocess_module.DirectoryFileIndex
PostprocessService = _postprocess_module.PostprocessService

_offline_module = load_module(
    "cloudsubscribex.core.services.offline",
    "plugins.v2/cloudsubscribex/core/services/offline.py",
)
OfflineTaskService = _offline_module.OfflineTaskService

SIX_HOURS = 6 * 3600
POSTPROCESS_LOGGER = "test.stale_finalize.postprocess"
OFFLINE_LOGGER = "test.stale_finalize.offline"


def _real_logger(name):
    return logging.getLogger(name)


def _patch_postprocess_logger(service, name):
    """把日志补丁打到服务实例方法的 __globals__ 上。

    不能按本文件 import 时捕获的 postprocess 模块打：全套收集时，
    test_finalize_mp_wait_1_12_6 与本文件之间收集的用例文件会重载
    postprocess 模块，DummyMpWaitService 继承的是旧一版类，本文件
    捕获的是新一版——按捕获打补丁会打空（全套 4 个 assertLogs 用例
    因此报 "no logs triggered"，子集里却全过）。按实例方法的
    __globals__ 打一定命中被测代码实际读的 logger。
    """
    # 从类上取（不用实例属性：个别用例会把该方法 stub 成普通函数，
    # 实例属性就不再是绑定方法；类属性一定是定义它的那个旧版函数）。
    globs = type(service)._organize_and_finalize_single_item.__globals__
    return patch.dict(globs, {"logger": _real_logger(name)})


class _FakeOfflineService:
    """115 离线任务能力桩：只返回固定快照并记录调用。"""

    def __init__(self, tasks=None):
        self.calls = []
        self.snapshot = {"tasks": list(tasks or []), "refresh_ok": True}

    def get_offline_tasks_snapshot(self, force=False):
        self.calls.append(("snapshot", force))
        return dict(self.snapshot)

    def get_offline_task_list_snapshot(self, force=False):
        self.calls.append(("list_snapshot", force))
        return {"tasks": [], "refresh_ok": True}

    def restart_offline_task(self, task_id):
        self.calls.append(("restart", task_id))

    def delete_offline_task(self, task_id, delete_source_file=False):
        self.calls.append(("delete", task_id, delete_source_file))

    def delete_offline_tasks(self, task_ids, delete_source_file=False):
        self.calls.append(("delete_many", list(task_ids), delete_source_file))
        return len(task_ids)


class _FakeDrive:
    key = "115"
    name = "115网盘"

    def __init__(self, offline=None):
        self._offline = offline

    def supports(self, cap):
        return self._offline is not None

    def require(self, cap):
        return self._offline


class _FakeRegistry:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def modify_job_next_run(self, job_id, when):
        self.calls.append(("modify_next_run", job_id))

    def ensure_job(self, job_id, **kwargs):
        self.calls.append(("ensure_job", job_id, kwargs))
        self.jobs[job_id] = kwargs

    def remove_job(self, job_id):
        self.calls.append(("remove_job", job_id))
        return self.jobs.pop(job_id, None) is not None


class _FakeSyncHandler:
    """只提供离线监控用到的几个方法。"""

    def __init__(self):
        self.groups = []
        self.monitor_calls = []
        self.pending = []
        self.monitor_result = {"checked": 1, "completed": 1, "failed": 0, "pending": 0}
        self.monitor_error = None
        self.groups_error = None

    def get_due_offline_task_groups(self, force=False, pending_keys=None):
        if self.groups_error is not None:
            raise self.groups_error
        return list(self.groups)

    def get_pending_finalize_tasks(self):
        return list(self.pending)

    def is_offline_task_finalized(self, task_id):
        return False

    def begin_notification_batch(self):
        return False

    def finish_notification_batch(self):
        pass

    def monitor_offline_strm_tasks(self, **kwargs):
        self.monitor_calls.append(kwargs)
        if self.monitor_error is not None:
            raise self.monitor_error
        return dict(self.monitor_result)


class _FakePluginHost:
    """假插件宿主：OfflineTaskService 委托链用到的运行态直接铺在实例上。"""

    def __init__(self, drive=None):
        self._cloud_drive = drive or _FakeDrive()
        self._sync_handler = None
        self._sync_tasks = {}
        self._sync_tasks_lock = RLock()
        self._offline_monitor_lock = Lock()
        self._offline_scheduler_lock = Lock()
        self._registry = _FakeRegistry()

    def _scheduler_registry(self):
        return self._registry

    def _refresh_postprocessing_sync_tasks(self):
        pass


def _postprocess_service():
    service = DummyMpWaitService()
    service._PENDING_ABSOLUTE_TIMEOUT_SECONDS = SIX_HOURS
    service._OFFLINE_PENDING_KEY = "pending_offline_strm"
    # 生产环境 SyncHandler 自带 _stop_requested（service.py:901），假宿主
    # 是裸 SimpleNamespace，这里补上，否则 _process_due_items 走不到分发。
    service._stop_requested = lambda: False
    return service


class DestMissedSettleTests(unittest.TestCase):
    """主修复 A：dest_missed 不再无限复查。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.service = _postprocess_service()
        self.abort_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason}
            ),
        )
        self.terminal_calls = []
        _stub(
            self.service, "_write_terminal_state",
            lambda item, key, status, reason="": self.terminal_calls.append(
                (status, reason)
            ) or True,
        )

    def _run(self, item, tracker_result, now=NOW):
        tracker = FakeTracker(tracker_result)
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        pending = {"key1": item}
        ctx = _ctx(pending, now=now)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        return tracker, pending, ctx

    def test_dest_missed_fuzzy_hash_hit_continues_normal_flow(self):
        """dest 精确查找失败、但父目录递归模糊定位按哈希命中：按已定位
        继续走正常流程（生成 STRM 并 commit），不走直接结算。"""
        fuzzy_file = SimpleNamespace(
            name="吞噬星空 {tmdb-101172} - S01E234 - 第234集 (1).mkv",
            id="f2", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            DEST_PARENT: DirectoryFileIndex([fuzzy_file]),
        }
        generate_calls = []
        _stub(
            self.service, "_generate_strm",
            lambda cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True: generate_calls.append(
                (cloud_dir, file_name, target_file)
            ) or Path("/tmp/x.strm"),
        )
        _stub(self.service, "_strm_file_ready", lambda path: True)
        _stub(
            self.service, "_restore_pending_media_context",
            lambda item, key: (None, {}),
        )
        commit_calls = []
        _stub(
            self.service, "_commit_mp_organize_success",
            lambda *args, **kwargs: commit_calls.append(args),
        )
        item = _item(NOW - 600, source_sha1="ABCDEF")
        _tracker, pending, ctx = self._run(
            item, ("succeeded", DEST_PATH, DEST_PATH)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(commit_calls, [])
        self.assertNotIn("key1", pending)
        self.assertEqual(len(generate_calls), 1)
        cloud_dir, file_name, target_file = generate_calls[0]
        self.assertEqual(cloud_dir, MEDIA_DIR)
        self.assertIs(target_file, fuzzy_file)
        self.assertEqual(
            file_name, "吞噬星空 {tmdb-101172} - S01E234 - 第234集 (1).mkv"
        )
        self.assertEqual(item.get("file_name"), file_name)
        self.assertTrue(item.get("moved_at"))
        self.assertEqual(self.terminal_calls, [("成功", "")])

    def test_dest_missed_fuzzy_name_variant_hit(self):
        """模糊定位按文件名变体命中同样走正常流程（无哈希时；
        前后缀/季集任一策略命中都走同一条正常流程）。"""
        fuzzy_file = SimpleNamespace(
            name="吞噬星空 S01E234 特别版.mkv",
            id="f3", size=123456,
        )
        self.service._snapshot_map = {
            DEST_PARENT: DirectoryFileIndex([fuzzy_file]),
        }
        generate_calls = []
        _stub(
            self.service, "_generate_strm",
            lambda cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True: generate_calls.append(
                (cloud_dir, file_name, target_file)
            ) or Path("/tmp/x.strm"),
        )
        _stub(self.service, "_strm_file_ready", lambda path: True)
        item = _item(NOW - 600, season=1, episode=234)
        _tracker, pending, ctx = self._run(
            item, ("succeeded", DEST_PATH, DEST_PATH)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertNotIn("key1", pending)
        self.assertEqual(len(generate_calls), 1)
        self.assertIs(generate_calls[0][2], fuzzy_file)

    def test_dest_missed_no_fuzzy_hit_commits_by_mp_history(self):
        """模糊定位也找不到：以 MP 历史成功为准直接 commit，不复查、不 abort、
        不计 fail；STRM 尽量生成，生成不了记 WARNING（待增量补齐）。"""
        self.service._snapshot_map = {}
        generate_calls = []
        _stub(
            self.service, "_generate_strm",
            lambda cloud_dir, file_name, *args, **kwargs: generate_calls.append(
                (cloud_dir, file_name)
            ) or None,
        )
        item = _item(NOW - 600)
        with _patch_postprocess_logger(self.service, POSTPROCESS_LOGGER):
            with self.assertLogs(POSTPROCESS_LOGGER, level="WARNING") as logs:
                _tracker, pending, ctx = self._run(
                    item, ("succeeded", DEST_PATH, DEST_PATH)
                )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertNotIn("key1", pending)
        self.assertEqual(item.get("file_name"), FILE_NAME)
        self.assertTrue(item.get("moved_at"))
        self.assertEqual(generate_calls, [(DEST_PARENT, FILE_NAME)])
        self.assertEqual(self.terminal_calls, [("成功", "")])
        self.assertIn("STRM 未生成", "\n".join(logs.output))
        self.assertIn(DEST_PARENT, self.service._snapshot_calls)


class AbsoluteExpiryTests(unittest.TestCase):
    """主修复 B：绝对过期（first_seen_at 时钟）。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.service = _postprocess_service()
        self.abort_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason}
            ),
        )

    def _plain_item(self, created_at, first_seen_at=None):
        item = {
            "task_type": "share",
            "file_name": "x.mkv",
            "cloud_dir": MEDIA_DIR,
            "staging_dir": "/转存",
            "staging_name": "x.mkv",
            "created_at": created_at,
        }
        if first_seen_at is not None:
            item["first_seen_at"] = first_seen_at
        return item

    def test_expired_item_aborts_with_timeout_reason(self):
        item = self._plain_item(NOW - 100, NOW - 7 * 3600)
        ctx = _ctx({"key1": item})
        expired = self.service._expire_overdue_pending_item(item, "key1", ctx)
        self.assertTrue(expired)
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn("6 小时", self.abort_calls[0]["reason"])

    def test_fresh_item_does_not_expire(self):
        item = self._plain_item(NOW - 100, NOW - 3600)
        ctx = _ctx({"key1": item})
        expired = self.service._expire_overdue_pending_item(item, "key1", ctx)
        self.assertFalse(expired)
        self.assertEqual(self.abort_calls, [])

    def test_missing_first_seen_backfills_from_created_at_without_abort(self):
        """老记录缺 first_seen_at：以 created_at 回填并记 INFO，本轮不直接
        过期（给主修复一次结算机会，下一轮再按回填值判定）。"""
        item = self._plain_item(NOW - 7 * 3600)
        ctx = _ctx({"key1": item})
        with _patch_postprocess_logger(self.service, POSTPROCESS_LOGGER):
            with self.assertLogs(POSTPROCESS_LOGGER, level="INFO") as logs:
                expired = self.service._expire_overdue_pending_item(
                    item, "key1", ctx
                )
        self.assertFalse(expired)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(item.get("first_seen_at"), NOW - 7 * 3600)
        self.assertIn("补记首次出现时间", "\n".join(logs.output))

    def test_expiry_runs_after_dispatch_and_yields_to_commit(self):
        """监控每轮先正常处理、再判过期：处理完已结算的项不再过期 abort；
        仍挂着的过期项 abort。created_at 很新、first_seen_at 很旧也能过期
        ——这正是 (e) 重登记重置 created_at 后仍要兜住的情况。"""
        # 仍挂着：定位失败只调度重试，随后过期 abort。
        item = self._plain_item(NOW - 100, NOW - 7 * 3600)
        pending = {"key1": item}
        ctx = _ctx(pending)
        self.service._process_due_items(ctx)
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn("6 小时", self.abort_calls[0]["reason"])
        self.assertEqual(int(item.get("fail_count") or 0), 1)
        # 已结算：分发时 commit，过期检查跳过。
        self.abort_calls.clear()
        settled = self._plain_item(NOW - 100, NOW - 7 * 3600)
        pending2 = {"key1": settled}

        def _fake_finalize(item_arg, key, ctx_arg):
            ctx_arg.pending.pop(key, None)
            ctx_arg.completed += 1

        _stub(
            self.service, "_organize_and_finalize_single_item", _fake_finalize
        )
        ctx2 = _ctx(pending2)
        self.service._process_due_items(ctx2)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(ctx2.completed, 1)


class BuildPendingRecordFirstSeenTests(unittest.TestCase):
    """first_seen_at 在 _build_pending_record 里重登记时保留旧值。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.handler = SyncHandler.__new__(SyncHandler)
        object.__setattr__(
            self.handler, "_OFFLINE_CHECK_DELAYS", (10, 20, 40, 60, 120, 300)
        )
        object.__setattr__(self.handler, "_upgrade_mode", "replace")
        object.__setattr__(
            self.handler, "_serialize_mediainfo", lambda mediainfo: {}
        )
        self.mediainfo = MagicMock()
        self.mediainfo.type = MediaType.TV

    def _build(self, current, now=NOW):
        return SyncHandler._build_pending_record(
            self.handler,
            current=current,
            pending_key="115:src:abc123",
            task_type="share",
            info_hash="",
            source_hash="",
            share_url="https://example.com/s/1",
            cloud_dir="/媒体库/电影",
            file_name="x.mkv",
            staging_dir="/转存",
            staging_name="x.mkv",
            file_size=100,
            now=now,
            mediainfo=self.mediainfo,
        )

    def test_new_record_first_seen_is_now(self):
        record = self._build({})
        self.assertEqual(record["first_seen_at"], NOW)

    def test_reregistration_preserves_first_seen_but_resets_created_at(self):
        """同 key 重登记：created_at 照旧重置为 now（全新尝试），
        first_seen_at 必须保留旧值——否则绝对过期永远到不了。"""
        old = NOW - 7 * 3600
        record = self._build(
            {"first_seen_at": old, "created_at": old, "fail_count": 3}
        )
        self.assertEqual(record["first_seen_at"], old)
        self.assertEqual(record["created_at"], NOW)
        self.assertEqual(record["fail_count"], 0)

    def test_reregistration_without_first_seen_takes_now(self):
        record = self._build({"created_at": NOW - 7 * 3600})
        self.assertEqual(record["first_seen_at"], NOW)


class PerItemIsolationTests(unittest.TestCase):
    """纵深防御：_process_due_items 单项异常不拖垮同组。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.service = _postprocess_service()
        self.abort_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason}
            ),
        )

    def _plain_item(self, first_seen_at=None):
        item = {
            "task_type": "share",
            "file_name": "x.mkv",
            "cloud_dir": MEDIA_DIR,
            "staging_dir": "/转存",
            "staging_name": "x.mkv",
            "created_at": NOW - 100,
            "first_seen_at": first_seen_at or NOW,
        }
        return item

    def test_single_item_exception_does_not_kill_group(self):
        """key1 抛异常：ERROR 里带 pending_key 与标题、fail_count+1 并调度
        重试；同组 key2 照常完成。"""
        pending = {"key1": self._plain_item(), "key2": self._plain_item()}

        def _boom_or_ok(item, key, ctx):
            if key == "key1":
                raise RuntimeError("boom")
            ctx.pending.pop(key, None)
            ctx.completed += 1

        _stub(self.service, "_organize_and_finalize_single_item", _boom_or_ok)
        ctx = _ctx(pending)
        with _patch_postprocess_logger(self.service, POSTPROCESS_LOGGER):
            with self.assertLogs(POSTPROCESS_LOGGER, level="ERROR") as logs:
                self.service._process_due_items(ctx)
        output = "\n".join(logs.output)
        self.assertIn("key1", output)
        self.assertIn("boom", output)
        self.assertEqual(ctx.completed, 1)
        self.assertNotIn("key2", pending)
        self.assertIn("key1", pending)
        self.assertEqual(int(pending["key1"].get("fail_count") or 0), 1)
        self.assertTrue(
            float(pending["key1"].get("next_check_at") or 0) > NOW
        )
        self.assertEqual(self.abort_calls, [])

    def test_repeated_item_exception_reaches_abort(self):
        """同一项再次抛异常：fail_count 达到阈值（分享类 2 次）即 abort，
        理由说明是后处理异常。"""
        pending = {"key1": self._plain_item()}

        def _always_boom(item, key, ctx):
            raise RuntimeError("boom")

        _stub(self.service, "_organize_and_finalize_single_item", _always_boom)
        self.service._process_due_items(_ctx(pending))
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(pending["key1"].get("fail_count") or 0), 1)
        self.service._process_due_items(_ctx(pending))
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn("后处理异常", self.abort_calls[0]["reason"])


class MonitorGuardTests(unittest.TestCase):
    """纵深防御：批次初始化异常被记下、不静默吞掉待处理数。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.service = _postprocess_service()
        self.service._get_data_store[
            self.service._OFFLINE_PENDING_KEY
        ] = {
            "k1": {"task_type": "share", "file_name": "a.mkv"},
            "k2": {"task_type": "share", "file_name": "b.mkv"},
        }

    def test_prepare_context_failure_returns_pending_count(self):
        _stub(
            self.service, "_prepare_postprocess_context",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db down")),
        )
        with _patch_postprocess_logger(self.service, POSTPROCESS_LOGGER):
            with self.assertLogs(POSTPROCESS_LOGGER, level="ERROR") as logs:
                result = self.service.monitor_offline_strm_tasks()
        output = "\n".join(logs.output)
        self.assertIn("后处理批次初始化失败", output)
        self.assertEqual(result["pending"], 2)
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(result["failed"], 0)


class OfflineMergeSharePendingTests(unittest.TestCase):
    """可见入口：分享类 pending 记录进 api_offline_tasks 快照。"""

    def setUp(self):
        self.host = _FakePluginHost(
            drive=_FakeDrive(offline=_FakeOfflineService())
        )
        self.handler = _FakeSyncHandler()
        self.host._sync_handler = self.handler
        self.service = OfflineTaskService(self.host)

    def test_share_pending_gets_synthetic_without_id(self):
        """分享类 pending 合成为对话框项：不带 id（对话框按 pending: 键
        选择/重试/删除），带 finalize_pending 标记，pending_count 计入。"""
        self.handler.pending = [
            {
                "pending_key": "115:src:aaa",
                "task_type": "share",
                "task_id": "subscribe:7",
                "file_name": "沙丘 (2021).mkv",
                "cloud_dir": "/媒体库/电影",
                "file_size": 123,
                "created_at": 1700000000,
            },
            {
                "pending_key": "115:mag:bbb",
                "task_type": "magnet",
                "task_id": "mag1",
                "file_name": "x.mkv",
                "cloud_dir": "/转存",
                "file_size": 456,
                "created_at": 1700000001,
            },
        ]
        result = self.service._merge_pending_finalize_tasks(
            {"tasks": [], "refresh_ok": True}
        )
        tasks = result["tasks"]
        self.assertEqual(len(tasks), 2)
        share = [t for t in tasks if t.get("pending_key") == "115:src:aaa"][0]
        self.assertTrue(share["finalize_pending"])
        self.assertNotIn("id", share)
        self.assertEqual(share["target_name"], "沙丘 (2021).mkv")
        self.assertEqual(share["add_time"], 1700000000)
        magnet = [t for t in tasks if t.get("pending_key") == "115:mag:bbb"][0]
        self.assertEqual(magnet["id"], "MAG1")
        self.assertEqual(result["pending_count"], 2)

    def test_share_pending_merges_into_existing_offline_task(self):
        """快照里已有同 task_id 的离线任务：pending 元数据合并进该任务，
        不另起合成项。"""
        self.handler.pending = [
            {
                "pending_key": "115:src:aaa",
                "task_type": "share",
                "task_id": "subscribe:7",
                "file_name": "沙丘 (2021).mkv",
                "cloud_dir": "/媒体库/电影",
                "created_at": 1700000000,
            }
        ]
        snapshot_task = {"id": "SUBSCRIBE:7", "name": "沙丘 (2021).mkv"}
        result = self.service._merge_pending_finalize_tasks(
            {"tasks": [snapshot_task], "refresh_ok": True}
        )
        self.assertEqual(len(result["tasks"]), 1)
        merged = result["tasks"][0]
        self.assertTrue(merged["finalize_pending"])
        self.assertEqual(merged["pending_key"], "115:src:aaa")
        self.assertEqual(result["pending_count"], 1)


class MonitorHeartbeatTests(unittest.TestCase):
    """可观测性：INFO 心跳（节流）与监控启动/停止 INFO。"""

    def setUp(self):
        self.host = _FakePluginHost(drive=_FakeDrive(offline=None))
        self.handler = _FakeSyncHandler()
        self.host._sync_handler = self.handler
        self.service = OfflineTaskService(self.host)
        self.handler.groups = [{"pending_keys": {"k1"}, "needs_offline": False}]
        self.handler.monitor_result = {
            "checked": 0, "completed": 0, "failed": 0, "pending": 0,
        }

    def _run(self, result=None):
        if result is not None:
            self.handler.monitor_result = dict(result)
        return self.service._run_offline_monitor(force=True)

    def test_heartbeat_logs_on_completion(self):
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ):
            with self.assertLogs(OFFLINE_LOGGER, level="INFO") as logs:
                result = self._run(
                    {"checked": 1, "completed": 1, "failed": 0, "pending": 0}
                )
        self.assertEqual(result["completed"], 1)
        self.assertIn("完成 1 个", "\n".join(logs.output))

    def test_heartbeat_silent_when_unchanged(self):
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ):
            self._run({"checked": 0, "completed": 0, "failed": 0, "pending": 0})
            with self.assertNoLogs(OFFLINE_LOGGER, level="INFO"):
                self._run(
                    {"checked": 0, "completed": 0, "failed": 0, "pending": 0}
                )

    def test_heartbeat_logs_on_pending_change(self):
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ):
            self._run({"checked": 0, "completed": 0, "failed": 0, "pending": 0})
            with self.assertLogs(OFFLINE_LOGGER, level="INFO") as logs:
                self._run(
                    {"checked": 0, "completed": 0, "failed": 0, "pending": 3}
                )
        self.assertIn("待处理 3 个", "\n".join(logs.output))

    def test_group_failure_returns_pending_count_with_error(self):
        self.handler.pending = [
            {"pending_key": "k1"}, {"pending_key": "k2"},
        ]
        self.handler.groups_error = RuntimeError("db down")
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ):
            with self.assertLogs(OFFLINE_LOGGER, level="ERROR") as logs:
                result = self._run()
        self.assertEqual(
            result, {"checked": 0, "completed": 0, "failed": 0, "pending": 2}
        )
        self.assertIn("媒体后处理分组失败", "\n".join(logs.output))

    def test_single_group_failure_returns_pending_count_with_error(self):
        self.handler.pending = [{"pending_key": "k1"}]
        self.handler.monitor_error = RuntimeError("boom")
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ):
            with self.assertLogs(OFFLINE_LOGGER, level="ERROR") as logs:
                result = self._run()
        self.assertEqual(
            result, {"checked": 0, "completed": 0, "failed": 0, "pending": 1}
        )
        self.assertIn("单组执行失败", "\n".join(logs.output))

    def test_update_monitor_start_stop_logs_info(self):
        with patch.object(
            _offline_module, "logger", _real_logger(OFFLINE_LOGGER)
        ), patch.object(
            _offline_module.pytz, "timezone",
            return_value=datetime.timezone.utc,
        ):
            with self.assertLogs(OFFLINE_LOGGER, level="INFO") as logs:
                self.service._update_offline_monitor(2)
            self.assertIn("监控已启动", "\n".join(logs.output))
            with self.assertLogs(OFFLINE_LOGGER, level="INFO") as logs:
                self.service._update_offline_monitor(0)
            self.assertIn("监控已停止", "\n".join(logs.output))
        self.assertIn(
            ("ensure_job", "CloudSubscribe_OfflineMonitor"),
            [c[:2] for c in self.host._registry.calls],
        )
        self.assertIn(
            ("remove_job", "CloudSubscribe_OfflineMonitor"),
            [c[:2] for c in self.host._registry.calls],
        )


if __name__ == "__main__":
    unittest.main()
