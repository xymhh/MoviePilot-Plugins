"""v1.12.6 C：finalize 等待 MP 整理（MpOrganizeTracker 接线）与身份透传。

覆盖：
- MP 等待态（queued/running）下 fail_count 不涨、120 秒过去也不 abort；
- MP 成功后按历史 dest 精确嵌套路径定位（S01E234 回归：文件在
  /转存/<剧名>/Season 1/ 下也能找到），走完 STRM 并清 pending 与登记表；
- MP 成功但 dest 查无文件时只复查、不计 fail_count、不 abort；
- MP 失败后 abort 原因取 MP 原文；missing 超宽限 abort“未受理”；
  queued/running 超 90 分钟 abort“超时”；队列停滞 15 分钟只更新文案；
- 终态（abort/commit）集中清除 MP 整理中登记；
- C2：mp_organize 身份经 _queue_file_finalize/_build_pending_record
  写进 pending 记录，无身份时不留字段、行为与老路径一致。
"""

import json
import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package, install_app_mocks, install_core_delegation, load_module,
    register_mock, set_media_type,
)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.models", "app.db.models.subscribe",
    "app.db.subscribe_oper", "app.modules",
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

install_core_delegation()
pause_module = ensure_package("cloudsubscribex.core.sync_pause")
pause_module.is_paused = MagicMock(return_value=False)
ensure_package("cloudsubscribex.core").sync_pause = pause_module
mock_utils = ensure_package("cloudsubscribex.utils")

# 加载实际的 MediaFileParser
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
register_mock("cloudsubscribex.search", subs_filter=MagicMock())
register_mock("cloudsubscribex.search.subs_filter")
load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)

_postprocess = load_module(
    "cloudsubscribex.handlers.sync.postprocess",
    "plugins.v2/cloudsubscribex/handlers/sync/postprocess.py",
)
DirectoryFileIndex = _postprocess.DirectoryFileIndex
PostprocessBatchContext = _postprocess.PostprocessBatchContext
PostprocessService = _postprocess.PostprocessService

tracker_module = load_module(
    "cloudsubscribex.core.services.mp_organize_tracker",
    "plugins.v2/cloudsubscribex/core/services/mp_organize_tracker.py",
)


def _tracker():
    """取当前 sys.modules 里的 tracker 模块。

    全量收集时后加载的文件会用 load_module 重载它，模块级绑定会指向
    旧对象、与 postprocess 懒导入拿到的不是同一个登记表；断言与登记
    必须每次从 sys.modules 取当前模块。
    """
    return sys.modules["cloudsubscribex.core.services.mp_organize_tracker"]

_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler

FILE_NAME = "吞噬星空 {tmdb-101172} - S01E234 - 第234集.mkv"
SRC_PATH = "/downloads/" + FILE_NAME
DEST_PARENT = "/转存/吞噬星空 (2020) {tmdb-101172}/Season 1"
DEST_PATH = DEST_PARENT + "/" + FILE_NAME
MEDIA_DIR = "/媒体库/剧集/国产剧/吞噬星空 (2020) {tmdb-101172}/Season 1"
NOW = 2000000.0


def _mp_organize(triggered_at):
    return {
        "src_path": SRC_PATH,
        "src_storage": "local",
        "triggered_at": triggered_at,
        "target_dir": "/媒体库/剧集",
        "target_storage": "u115cs",
        "target_name": "剧集",
        "file_name": FILE_NAME,
    }


def _item(triggered_at, **overrides):
    item = {
        "task_type": "share",
        "file_name": FILE_NAME,
        "cloud_dir": MEDIA_DIR,
        "staging_dir": "/转存",
        "staging_name": FILE_NAME,
        "created_at": NOW - 1000,
        "download_completed_at": NOW - 1000,
        "skip_history": True,
        "mp_organize": _mp_organize(triggered_at),
    }
    item.update(overrides)
    return item


def _ctx(pending, now=NOW):
    return PostprocessBatchContext(
        pending=pending,
        pending_snapshot={},
        due_keys=list(pending),
        now=now,
        monitor_token="token",
        task_map={},
        tasks_valid=True,
        subscribe_cache={},
    )


class FakeTracker:
    """桩 MpOrganizeTracker：每次 poll 返回预设的三元组，并记录调用。"""

    def __init__(self, result):
        self.result = result
        self.polls = []

    def poll(self, mp_organize):
        self.polls.append(dict(mp_organize))
        return self.result


class DummyMpWaitService(PostprocessService):
    """复用 test_sync_postprocess 的 DummyPostprocessService 形态。"""

    def __init__(self):
        owner = types.SimpleNamespace()
        super().__init__(owner)
        self._organize_after_transfer = True
        self._upgrade_mode = "replace"
        self._FINALIZE_MAX_FAILURES = 3
        self._OFFLINE_CHECK_DELAYS = [60, 120, 300, 600]
        self._OFFLINE_TIMEOUT = 3600
        self._FILE_FINALIZE_TIMEOUT = 1800
        self._cloud_batch_mutations = MagicMock()
        self._offline_pending_lock = MagicMock()
        self._offline_pending_lock.__enter__ = MagicMock(return_value=None)
        self._offline_pending_lock.__exit__ = MagicMock(return_value=None)
        self._get_data_store = {}
        self._cloud_drive = MagicMock()
        self._postprocess_task_update = False
        self._FINALIZE_DEAD_LOG = "文件后处理失败超出重试限制：{file_name}"
        self._strm_generate_enabled = True
        self._strm_generator = MagicMock()
        self._local_resource_path = "/local/media"
        self._metadata_scraper = None
        self._nfo_scrape_enabled = False
        self._image_scrape_enabled = False
        self._organize_subtitles = False
        self._snapshot_map = {}
        self._snapshot_calls = []

    def _notify_offline_pending_changed(self, count):
        pass

    def _supported_hash_algorithms(self):
        return frozenset({"sha1", "md5"})

    def _cloud_directory_snapshot(self, path, cache=None):
        if cache and path in cache:
            return cache[path]
        self._snapshot_calls.append(path)
        entry = self._snapshot_map.get(path)
        if entry is None:
            return True, DirectoryFileIndex([])
        return True, entry

    def _get_data(self, key):
        return self._get_data_store.get(key, {})

    def _save_offline_pending(self, data):
        self._get_data_store[self._OFFLINE_PENDING_KEY] = data


def _stub(service, name, value):
    """在职责组件实例上打桩（组件方法不能用宿主属性赋值 stub）。"""
    object.__setattr__(service, name, value)
    return value


class FinalizeMpWaitTests(unittest.TestCase):
    def setUp(self):
        # MediaType 桩必须在每个用例执行前重注：收集阶段后加载的用例文件
        # 会覆盖 app.schemas.types 上的桩，模块级那次到执行时已失效。
        global MediaType
        MediaType = set_media_type()
        _tracker()._MP_ORGANIZING.clear()
        self.service = DummyMpWaitService()
        self.abort_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason}
            ),
        )

    def _run(self, item, tracker_result, now=NOW):
        tracker = FakeTracker(tracker_result)
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        pending = {"key1": item}
        ctx = _ctx(pending, now=now)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        return tracker, pending, ctx

    def test_queued_does_not_count_failure_past_120s(self):
        """MP 排队中：download_completed_at 已过 120 秒也不 abort、不计 fail。"""
        item = _item(NOW - 300)
        tracker, pending, ctx = self._run(
            item, ("queued", "MP 整理排队中", None)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertIn("key1", pending)
        self.assertGreater(float(item.get("next_check_at") or 0), NOW)
        self.assertEqual(len(tracker.polls), 1)
        self.assertEqual(tracker.polls[0]["src_path"], SRC_PATH)
        self.assertEqual(
            item.get("mp_organize_wait"),
            {"last_state": "queued", "last_change_at": NOW},
        )

    def test_running_does_not_count_failure(self):
        """MP 进行中：同样不计 fail、不 abort，只复查。"""
        item = _item(NOW - 300)
        _tracker, pending, ctx = self._run(
            item, ("running", "MP 整理进行中", None)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertIn("key1", pending)
        self.assertGreater(float(item.get("next_check_at") or 0), NOW)

    def test_failed_aborts_with_mp_reason_verbatim(self):
        """MP 失败：abort 原因取 MP 原文（通用句也原样透传）。"""
        errmsg = "⚠ 整理操作多次失败，请稍后重试"
        item = _item(NOW - 300)
        self._run(item, ("failed", errmsg, None))
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn(errmsg, self.abort_calls[0]["reason"])
        self.assertIn("MP 整理失败", self.abort_calls[0]["reason"])

    def test_missing_within_grace_retries(self):
        """触发 10 分钟宽限内无记录：只复查，不 abort、不计 fail。"""
        item = _item(NOW - 60)
        _tracker, pending, ctx = self._run(
            item, ("missing", "MP 整理队列与历史中均无记录", None)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertIn("key1", pending)

    def test_missing_beyond_grace_aborts_unaccepted(self):
        """超宽限仍 missing：abort“MP 整理未受理”。"""
        item = _item(NOW - 700)
        self._run(item, ("missing", "MP 整理队列与历史中均无记录", None))
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn("MP 整理未受理", self.abort_calls[0]["reason"])

    def test_queued_timeout_aborts_after_90_minutes(self):
        """queued 超 90 分钟：abort“MP 整理超时”。"""
        item = _item(NOW - 91 * 60)
        self._run(item, ("queued", "MP 整理排队中", None))
        self.assertEqual(len(self.abort_calls), 1)
        self.assertIn("MP 整理超时", self.abort_calls[0]["reason"])

    def test_stall_updates_registry_text_without_abort(self):
        """同一 state 满 15 分钟：登记表文案标停滞，但不 abort、不重试 MP。"""
        item = _item(NOW - 20 * 60)
        item["mp_organize_wait"] = {
            "last_state": "queued",
            "last_change_at": NOW - 16 * 60,
        }
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        self._run(item, ("queued", "MP 整理排队中", None))
        self.assertEqual(self.abort_calls, [])
        entry = _tracker()._MP_ORGANIZING.get("parent-1")
        self.assertEqual(
            entry["status_text"], "整理停滞（队头阻塞，后续任务排队中）"
        )

    def test_stall_not_triggered_before_15_minutes(self):
        """同一 state 不满 15 分钟：登记表文案不动。"""
        item = _item(NOW - 20 * 60)
        item["mp_organize_wait"] = {
            "last_state": "queued",
            "last_change_at": NOW - 5 * 60,
        }
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        self._run(item, ("queued", "MP 整理排队中", None))
        entry = _tracker()._MP_ORGANIZING.get("parent-1")
        self.assertEqual(entry["status_text"], "")

    def test_succeeded_locates_nested_dest_and_finalizes(self):
        """S01E234 回归：历史成功 + 文件在 /转存/<剧名>/Season 1/ 下，
        按 dest 精确父目录定位，走完 STRM、清 pending、清登记表。"""
        dest_file = SimpleNamespace(
            name=FILE_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            DEST_PARENT: DirectoryFileIndex([dest_file]),
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
        _stub(
            self.service, "_write_terminal_state",
            lambda item, key, status, reason="": True,
        )
        _stub(
            self.service, "_notify_pending_file_finalized",
            lambda *args, **kwargs: None,
        )
        item = _item(NOW - 600)
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        tracker, pending, ctx = self._run(
            item, ("succeeded", DEST_PATH, DEST_PATH)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(len(generate_calls), 1)
        cloud_dir, file_name, target_file = generate_calls[0]
        self.assertEqual(cloud_dir, MEDIA_DIR)
        self.assertEqual(file_name, FILE_NAME)
        self.assertIs(target_file, dest_file)
        self.assertNotIn("key1", pending)
        self.assertEqual(_tracker().list_mp_organizing(), {})
        self.assertIn(DEST_PARENT, self.service._snapshot_calls)

    def test_succeeded_dest_missing_retries_without_failure(self):
        """历史成功但 dest 查无文件：回落平铺定位仍找不到时只复查，
        不计 fail、不 abort（S01E06 库内查找不一致的前车之鉴）。"""
        self.service._snapshot_map = {}
        item = _item(NOW - 600)
        _tracker, pending, ctx = self._run(
            item, ("succeeded", DEST_PATH, DEST_PATH)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(int(item.get("fail_count") or 0), 0)
        self.assertIn("key1", pending)
        self.assertGreater(float(item.get("next_check_at") or 0), NOW)
        self.assertIn(DEST_PARENT, self.service._snapshot_calls)

    def test_locate_cloud_file_by_dest_nested_path(self):
        """_locate_cloud_file_by_dest 只对 dest 的精确父目录取单层快照。"""
        dest_file = SimpleNamespace(
            name=FILE_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        ctx = _ctx({})
        ctx.directory_snapshots[DEST_PARENT] = (
            True, DirectoryFileIndex([dest_file])
        )
        found = self.service._locate_cloud_file_by_dest(DEST_PATH, ctx)
        self.assertIs(found, dest_file)
        self.assertIsNone(
            self.service._locate_cloud_file_by_dest(
                "/转存/吞噬星空 (2020) {tmdb-101172}/Season 1/不存在.mkv", ctx
            )
        )
        self.assertIsNone(
            self.service._locate_cloud_file_by_dest("", ctx)
        )

    def test_real_abort_clears_registry(self):
        """真实 _abort_single_item 终态结算时按源路径清除登记表。"""
        service = DummyMpWaitService()
        _stub(
            service, "_write_terminal_state",
            lambda item, key, status, reason="": True,
        )
        _stub(service, "_notify_finalize_dead", lambda item, key: None)
        item = _item(NOW - 300)
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        pending = {"key1": item}
        ctx = _ctx(pending)
        service._abort_single_item(item, "key1", "测试原因", ctx)
        self.assertEqual(_tracker().list_mp_organizing(), {})
        self.assertNotIn("key1", pending)
        self.assertEqual(ctx.failed, 1)

    def test_real_commit_clears_registry(self):
        """真实 _commit_single_item 成功终态时按源路径清除登记表。"""
        service = DummyMpWaitService()
        _stub(
            service, "_write_terminal_state",
            lambda item, key, status, reason="": True,
        )
        _stub(
            service, "_notify_pending_file_finalized",
            lambda *args, **kwargs: None,
        )
        item = _item(NOW - 300)
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        pending = {"key1": item}
        ctx = _ctx(pending)
        service._commit_single_item(item, "key1", None, None, {}, ctx)
        self.assertEqual(_tracker().list_mp_organizing(), {})
        self.assertNotIn("key1", pending)
        self.assertEqual(ctx.completed, 1)

    def test_no_identity_keeps_legacy_behavior(self):
        """无 mp_organize 身份时走老路径：120 秒 / 2 次后 abort。

        这里不用 setUp 里的 abort recorder——删掉实例上的桩，恢复真实
        _abort_single_item（_write_terminal_state 与失败通知仍打桩）。
        """
        item = _item(NOW - 300)
        item.pop("mp_organize")
        item["fail_count"] = 1
        self.service._snapshot_map = {}
        self.service.__dict__.pop("_abort_single_item", None)
        _stub(
            self.service, "_write_terminal_state",
            lambda item, key, status, reason="": True,
        )
        _stub(
            self.service, "_notify_finalize_dead",
            lambda item, key: None,
        )
        pending = {"key1": item}
        ctx = _ctx(pending)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        self.assertNotIn("key1", pending)
        self.assertEqual(ctx.failed, 1)


class MpOrganizePendingRecordTests(unittest.TestCase):
    """C2：mp_organize 身份经 pending 记录透传给 finalize。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        _tracker()._MP_ORGANIZING.clear()

    @staticmethod
    def _fake_self():
        return SimpleNamespace(
            _upgrade_mode="replace",
            _OFFLINE_CHECK_DELAYS=(10, 20, 40, 60, 120, 300),
        )

    def _build(self, current=None, mp_organize=None, media_type=None):
        mediainfo = None
        if media_type is not None:
            mediainfo = SimpleNamespace(type=media_type)
        return SyncHandler._build_pending_record(
            self._fake_self(),
            current=current or {},
            pending_key="key1",
            task_type="share",
            info_hash="",
            source_hash="",
            share_url="https://115.com/s/test",
            cloud_dir=MEDIA_DIR,
            file_name=FILE_NAME,
            staging_dir="/转存",
            staging_name=FILE_NAME,
            file_size=123456,
            now=NOW,
            mediainfo=mediainfo,
            media_data={},
            mp_organize=mp_organize,
        )

    def test_build_record_carries_mp_organize(self):
        """有身份时 pending 记录带 mp_organize，且 JSON 可序列化。"""
        record = self._build(mp_organize=_mp_organize(NOW - 600))
        self.assertEqual(record["mp_organize"]["src_path"], SRC_PATH)
        self.assertEqual(record["mp_organize"]["triggered_at"], NOW - 600)
        json.dumps(record)

    def test_build_record_without_identity_has_no_field(self):
        """无身份时 pending 记录不留 mp_organize 字段，老路径一字不改。"""
        record = self._build()
        self.assertNotIn("mp_organize", record)

    def test_build_record_requeue_keeps_old_identity(self):
        """重登记且未传新身份时，沿用旧记录里的身份。"""
        old = {"mp_organize": _mp_organize(NOW - 900)}
        record = self._build(current=old)
        self.assertEqual(record["mp_organize"]["triggered_at"], NOW - 900)

    def test_build_record_new_identity_overrides_old(self):
        """重登记传了新身份时，新身份优先。"""
        old = {"mp_organize": _mp_organize(NOW - 900)}
        record = self._build(current=old, mp_organize=_mp_organize(NOW - 60))
        self.assertEqual(record["mp_organize"]["triggered_at"], NOW - 60)

    def test_build_record_tv_season_kept(self):
        """TV 记录的 season 仍按原规则算（身份透传不影响其它字段）。"""
        record = self._build(
            mp_organize=_mp_organize(NOW - 600),
            media_type=_service_module.MediaType.TV,
        )
        self.assertEqual(record["season"], 1)
        self.assertEqual(record["mp_organize"]["src_path"], SRC_PATH)

    def test_queue_file_finalize_persists_mp_organize(self):
        """_queue_file_finalize 落盘的 pending 记录带 mp_organize。"""
        store = {}
        handler = SyncHandler.__new__(SyncHandler)
        handler._get_data = lambda key: store.get(key, {})
        handler._save_data = lambda key, data: store.__setitem__(key, data)
        handler._offline_pending_lock = threading.Lock()
        handler._offline_pending_changed = None
        handler._organize_after_transfer = True
        handler._cloud_drive = SimpleNamespace(key="115")
        handler._upgrade_mode = "replace"
        handler._is_cloud_resource_url = lambda url: False
        handler._is_direct_cloud_resource_url = lambda url: False
        pending_key = handler._queue_file_finalize(
            "https://115.com/s/test",
            MEDIA_DIR,
            FILE_NAME,
            None,
            file_size=123456,
            staging_dir="/转存",
            staging_name=FILE_NAME,
            mp_organize=_mp_organize(NOW - 600),
        )
        self.assertTrue(pending_key)
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        self.assertEqual(record["mp_organize"]["src_path"], SRC_PATH)

    def test_queue_file_finalize_without_identity_has_no_field(self):
        """不传身份时落盘记录不带 mp_organize 字段。"""
        store = {}
        handler = SyncHandler.__new__(SyncHandler)
        handler._get_data = lambda key: store.get(key, {})
        handler._save_data = lambda key, data: store.__setitem__(key, data)
        handler._offline_pending_lock = threading.Lock()
        handler._offline_pending_changed = None
        handler._organize_after_transfer = True
        handler._cloud_drive = SimpleNamespace(key="115")
        handler._upgrade_mode = "replace"
        handler._is_cloud_resource_url = lambda url: False
        handler._is_direct_cloud_resource_url = lambda url: False
        pending_key = handler._queue_file_finalize(
            "https://115.com/s/test",
            MEDIA_DIR,
            FILE_NAME,
            None,
            file_size=123456,
            staging_dir="/转存",
            staging_name=FILE_NAME,
        )
        self.assertTrue(pending_key)
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        self.assertNotIn("mp_organize", record)
