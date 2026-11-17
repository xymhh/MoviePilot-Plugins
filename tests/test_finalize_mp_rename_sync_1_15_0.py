"""1.15.0 Phase 3a 第 11 项：入库文件名唯一事实来源。

MP 整理已按 MP 自己的格式改名/移走后，插件按 MP 历史的 dest 文件名
反向同步实际落盘名：_poll_mp_organize_state 的 located 分支把
item["file_name"] 写成 dest 的真实值，_organize_and_finalize_single_item
里本地 file_name 随后重读，后续 STRM/字幕/终态都用真实名——不再用
插件自己算的目标名去定位（否则 MP 已入库、插件定位不到就误判失败，
正是 2026-11-17「先通知后处理失败、然后入库成功」的根因之一）。
只同步文件名，不动 cloud_dir：cloud_dir 是 STRM 落点与终态记录的
媒体库目录，dest 父目录可能是 /转存/ 下的嵌套路径（S01E234 回归
用例），同步过去会把 STRM 写错地方。

桩与 DummyMpWaitService 复用 test_finalize_mp_wait_1_12_6 的形态
（不直接 import 该文件：它的模块级加载依赖全量收集顺序，子集跑会
因 app.chain 未装桩而失败；这里自带一份完整桩）。
"""
import sys
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
    """取当前 sys.modules 里的 tracker 模块（见 test_finalize_mp_wait）。"""
    return sys.modules["cloudsubscribex.core.services.mp_organize_tracker"]


FILE_NAME = "吞噬星空 {tmdb-101172} - S01E234 - 第234集.mkv"
SRC_PATH = "/downloads/" + FILE_NAME
MEDIA_DIR = "/媒体库/剧集/国产剧/吞噬星空 (2020) {tmdb-101172}/Season 1"
NOW = 2000000.0
PLUGIN_NAME = FILE_NAME
MP_NAME = "吞噬星空 - S01E234 - 第234集.mkv"
DEST_PATH = MEDIA_DIR + "/" + MP_NAME


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
    """桩 MpOrganizeTracker：每次 poll 返回预设的三元组。"""

    def __init__(self, result):
        self.result = result
        self.polls = []

    def poll(self, mp_organize):
        self.polls.append(dict(mp_organize))
        return self.result


class DummyMpWaitService(PostprocessService):
    """复用 test_finalize_mp_wait_1_12_6 的 DummyMpWaitService 形态。"""

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


class MpRenameSyncTests(unittest.TestCase):
    def setUp(self):
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

    def test_poll_located_syncs_actual_landed_name(self):
        """located 分支把 pending 项的文件名同步成 dest 真实值。

        只同步文件名，不动 cloud_dir：cloud_dir 是 STRM 落点与终态
        记录的媒体库目录，dest 父目录可能是 /转存/ 下的嵌套路径
        （S01E234 回归用例），同步过去会把 STRM 写错地方。
        """
        dest_file = SimpleNamespace(
            name=MP_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            MEDIA_DIR: DirectoryFileIndex([dest_file]),
        }
        item = _item(NOW - 600)
        item["file_name"] = PLUGIN_NAME
        item["cloud_dir"] = "/媒体库/剧集/国产剧/其它"
        ctx = _ctx({"key1": item})
        tracker = FakeTracker(("succeeded", "ok", DEST_PATH))
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        action, found, dest_dir = self.service._poll_mp_organize_state(
            item, "key1", item["mp_organize"], ctx
        )
        self.assertEqual(action, "located")
        self.assertIs(found, dest_file)
        self.assertEqual(dest_dir, MEDIA_DIR)
        self.assertEqual(item["file_name"], MP_NAME)
        self.assertEqual(item["cloud_dir"], "/媒体库/剧集/国产剧/其它")

    def test_finalize_uses_mp_renamed_name_for_strm(self):
        """MP 改名后 finalize：STRM 用 MP 的真实落盘名生成，不 abort。"""
        dest_file = SimpleNamespace(
            name=MP_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            MEDIA_DIR: DirectoryFileIndex([dest_file]),
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
        item["file_name"] = PLUGIN_NAME
        _tracker().mark_mp_organizing("parent-1", item["mp_organize"])
        tracker, pending, ctx = self._run(
            item, ("succeeded", DEST_PATH, DEST_PATH)
        )
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(len(generate_calls), 1)
        cloud_dir, file_name, target_file = generate_calls[0]
        self.assertEqual(cloud_dir, MEDIA_DIR)
        self.assertEqual(file_name, MP_NAME)
        self.assertIs(target_file, dest_file)
        self.assertEqual(item["file_name"], MP_NAME)
        self.assertNotIn("key1", pending)

    def test_same_name_unchanged(self):
        """MP 没改名时同步结果与原来一致（不引入回归）。"""
        dest_file = SimpleNamespace(
            name=PLUGIN_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            MEDIA_DIR: DirectoryFileIndex([dest_file]),
        }
        item = _item(NOW - 600)
        item["file_name"] = PLUGIN_NAME
        dest_path = MEDIA_DIR + "/" + PLUGIN_NAME
        ctx = _ctx({"key1": item})
        tracker = FakeTracker(("succeeded", "ok", dest_path))
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        action, found, dest_dir = self.service._poll_mp_organize_state(
            item, "key1", item["mp_organize"], ctx
        )
        self.assertEqual(action, "located")
        self.assertEqual(item["file_name"], PLUGIN_NAME)
        self.assertEqual(item["cloud_dir"], MEDIA_DIR)


if __name__ == "__main__":
    unittest.main()
