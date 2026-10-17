"""1.6.52 转存落盘自检 + landed 字段 + 跳过原因可视化单测。

对应任务书 docs/plan/2026-10-16-转存落盘自检与跳过原因可视化.md 5.1 的 a~h：

a. 目录里有同 file_id 文件 → landed=True、reason=file_id_match、状态仍「成功」；
b. 目录里只有同名文件（无 file_id）→ landed=True、回填 actual_file_id；
c. 目录实测存在但没这个文件 → landed=False、记「待确认」、platform_records 为空；
d. 列目录 checked=False → landed=None、维持 1.6.51 行为（状态仍「成功」）；
e. 开关 verify_landed_enabled=False → 与 1.6.51 输出逐字段一致（goldens）；
f. finalize 路径（_mark_offline_history_status_batch）不通过 → status="待确认" 且不进 platform_records；
g. 基线信任：landed=False 的插件历史条目被丢弃；无 landed 的老记录行为不变；
h. prepare_history_group：unverified_count 正确、failed_count 排除「待确认」、skip_reasons 分布正确。
"""

import os
import re
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
)

logger_ref = sys.modules["app.log"].logger
set_media_type(movie="电影", tv="电视剧")

sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)
register_module("app.schemas", MediaInfo=SimpleNamespace)


def _fake_metainfo(name, custom_words=None):
    text = str(name or "").strip()
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
        m2 = re.search(r"第\s*(\d{1,4})\s*集", text)
        if m2:
            episode = int(m2.group(1))
    return SimpleNamespace(
        begin_season=season,
        begin_episode=episode,
        episode_list=[episode] if episode else [],
        cn_name=text or None,
    )


register_module("app.core.metainfo", MetaInfo=_fake_metainfo)

# ---- cloudsubscribex 包层级 ----
install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
)

_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core").CloudDriveCapability = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(
    return_value=("themoviedb", "123")
)
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.media").download_history_identity_payload = MagicMock(
    return_value={}
)
_ensure("cloudsubscribex.core.media").media_server_tmdb_filters = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.history").history_group_key = MagicMock(
    return_value=""
)
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
_ensure("cloudsubscribex.utils.cache").create_platform_ttl_cache = MagicMock()
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"


class _StubMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))


_ensure("cloudsubscribex.utils.file_parser").MediaFileParser = _StubMediaFileParser

# 加载被测模块
episode_dedup_module = load_module(
    "cloudsubscribex.handlers.sync.episode_dedup",
    "plugins.v2/cloudsubscribex/handlers/sync/episode_dedup.py",
)
EpisodeDedupService = episode_dedup_module.EpisodeDedupService

landed_module = load_module(
    "cloudsubscribex.handlers.sync.landed",
    "plugins.v2/cloudsubscribex/handlers/sync/landed.py",
)
LandedVerifyService = landed_module.LandedVerifyService

history_module = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = history_module.HistoryService

baseline_module = load_module(
    "cloudsubscribex.handlers.sync.baseline",
    "plugins.v2/cloudsubscribex/handlers/sync/baseline.py",
)
UpgradeBaselineService = baseline_module.UpgradeBaselineService

platform_history_module = load_module(
    "cloudsubscribex.handlers.sync.platform_history",
    "plugins.v2/cloudsubscribex/handlers/sync/platform_history.py",
)
PlatformHistoryService = platform_history_module.PlatformHistoryService


class _FakeCloudFile:
    def __init__(self, file_id, name, size=0, is_directory=False):
        self.id = file_id
        self.name = name
        self.size = size
        self.is_directory = is_directory


class _FakeListing:
    def __init__(self, files, checked=True):
        self.files = tuple(files)
        self.checked = checked


class _FakeLookup:
    def __init__(self, directory_id="dir1", checked=True):
        self.directory_id = directory_id
        self.checked = checked


class _FakeDirectories:
    def __init__(self, files=None, checked=True, files_by_dir=None):
        self._files = files or []
        self._checked = checked
        self._files_by_dir = dict(files_by_dir or {})
        self.list_calls = 0
        self.list_dirs = []

    def resolve_directory(self, path, create=False):
        return _FakeLookup(directory_id=str(path) or "dir1")

    def list_directory(self, directory_id):
        self.list_calls += 1
        self.list_dirs.append(str(directory_id))
        if self._files_by_dir:
            return _FakeListing(
                self._files_by_dir.get(str(directory_id), []), checked=self._checked
            )
        return _FakeListing(self._files, checked=self._checked)


_SUB = SimpleNamespace(
    name="Show", year=2024, custom_words=None, id=1, tmdb_id=123
)
_MEDIA = SimpleNamespace(
    title_year="Show (2024)", title="Show", year=2024, tmdb_id=123,
    type=SimpleNamespace(value="电视剧"),
)

_SEASON_DIR = "/媒体/剧集/Show/Season 01"


class _DedupLandedHandler(EpisodeDedupService, LandedVerifyService):
    pass


def _make_verify_handler(dirs=None, checked=True, verify_enabled=True, dirs_by_dir=None):
    """构造只带季目录列目录能力的自检 handler。"""
    handler = _DedupLandedHandler(SimpleNamespace())
    owner = object.__getattribute__(handler, "_owner")
    owner._CLOUD_MEDIA_ROOT = "/"
    owner._cloud_directories = _FakeDirectories(
        files=dirs, checked=checked, files_by_dir=dirs_by_dir
    )
    owner._verify_landed_enabled = verify_enabled
    owner._resolve_resource_season_dir = (
        lambda root, subscribe, mediainfo, season: Path(_SEASON_DIR)
    )
    return handler


def _dirs(entries):
    return [
        _FakeCloudFile(file_id=file_id, name=name, size=size)
        for file_id, name, size in entries
    ]


class LandedVerifyItemsTests(unittest.TestCase):
    """a/b/d/e：verify_landed_items 判定与拿不准时的回退。"""

    def test_a_file_id_match_keeps_success(self):
        """a. 目录里有同 file_id 文件 → landed=True，状态仍「成功」。"""
        handler = _make_verify_handler(
            dirs=_dirs([("f1", "Show - S01E01.mkv", 100)])
        )
        infos = handler.verify_landed_items(_SUB, _MEDIA, 1, [
            {"key": "k", "file_id": "f1", "target_name": "Show - S01E01.mkv"},
        ])
        info = infos["k"]
        self.assertTrue(info["landed"])
        self.assertEqual(info["reason"], "file_id_match")
        fields = handler._landed_verification_fields(info)
        self.assertTrue(fields["landed"])
        self.assertIn("verified_at", fields)
        self.assertTrue(handler._landed_confirms_success(info))

    def test_b_name_match_backfills_actual_file_id(self):
        """b. 目录里只有同名文件（无 file_id）→ landed=True 并回填 actual_file_id。"""
        handler = _make_verify_handler(
            dirs=_dirs([("f9", "Show - S01E02.mkv", 100)])
        )
        infos = handler.verify_landed_items(_SUB, _MEDIA, 1, [
            {"key": "k", "file_id": "", "target_name": "Show - S01E02.mkv"},
        ])
        info = infos["k"]
        self.assertTrue(info["landed"])
        self.assertEqual(info["reason"], "name_match")
        self.assertEqual(info["actual_file_id"], "f9")

    def test_c_missing_file_is_not_confirmed(self):
        """c. 目录实测存在但没这个文件 → landed=False、不按成功处置。"""
        handler = _make_verify_handler(
            dirs=_dirs([("f4", "Show - S01E04.mp4", 100)])
        )
        infos = handler.verify_landed_items(_SUB, _MEDIA, 1, [
            {"key": "k", "file_id": "f1", "target_name": "Show - S01E01.mkv"},
        ])
        info = infos["k"]
        self.assertFalse(info["landed"])
        self.assertEqual(info["reason"], "missing_in_dir")
        self.assertFalse(handler._landed_confirms_success(info))
        fields = handler._landed_verification_fields(info)
        self.assertFalse(fields["landed"])
        self.assertEqual(fields["landed_reason"], "missing_in_dir")

    def test_d_listing_unavailable_keeps_legacy(self):
        """d. 列目录 checked=False → landed=None，维持 1.6.51 行为。"""
        handler = _make_verify_handler(
            dirs=_dirs([("f1", "Show - S01E01.mkv", 100)]), checked=False
        )
        infos = handler.verify_landed_items(_SUB, _MEDIA, 1, [
            {"key": "k", "file_id": "f1", "target_name": "Show - S01E01.mkv"},
        ])
        info = infos["k"]
        self.assertIsNone(info["landed"])
        self.assertEqual(info["reason"], "listing_unavailable")
        self.assertTrue(handler._landed_confirms_success(info))
        self.assertEqual(handler._landed_verification_fields(info), {})

    def test_e_switch_off_matches_legacy(self):
        """e. 开关关闭 → 不校验、不产生任何新字段（与 1.6.51 逐字段一致）。"""
        handler = _make_verify_handler(
            dirs=_dirs([("f1", "Show - S01E01.mkv", 100)]), verify_enabled=False
        )
        record = {
            "status": "成功", "title": "Show", "season": 1, "episode": 1,
            "file_name": "Show - S01E01.mkv", "cloud_dir": _SEASON_DIR,
        }
        original = dict(record)
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record, original)
        # 且没有触发列目录
        self.assertEqual(handler._cloud_directories.list_calls, 0)


class ApplyVerificationTests(unittest.TestCase):
    """i/j（后处理写成功记录接点）：查无实据记「待确认」，落在别处不误判。"""

    @staticmethod
    def _record(cloud_dir=_SEASON_DIR, file_name="Show - S01E02.mkv"):
        return {
            "status": "成功", "title": "Show", "type": "电视剧", "tmdb_id": 123,
            "season": 1, "episode": 2, "file_name": file_name,
            "cloud_dir": cloud_dir, "file_size": 10,
        }

    def test_i_missing_file_marks_unverified(self):
        """i. 季目录里没有该文件 → 记「待确认」+ 字段落盘。"""
        handler = _make_verify_handler(dirs=_dirs([("f4", "Show - S01E04.mp4", 100)]))
        record = self._record()
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record["status"], "待确认")
        self.assertFalse(record["landed"])
        self.assertEqual(record["landed_reason"], "missing_in_dir")
        self.assertIn("verified_at", record)

    def test_i_present_file_keeps_success(self):
        """i. 季目录里真有该文件 → 仍「成功」+ landed=True。"""
        handler = _make_verify_handler(dirs=_dirs([("f2", "Show - S01E02.mkv", 100)]))
        record = self._record()
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record["status"], "成功")
        self.assertTrue(record["landed"])
        self.assertIn("verified_at", record)

    def test_i_listing_unavailable_keeps_legacy(self):
        """i. 拿不到实测数据 → 记录逐字段不变（维持 1.6.51）。"""
        handler = _make_verify_handler(dirs=[], checked=False)
        record = self._record()
        original = dict(record)
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record, original)

    def test_i_manual_success_record_skipped(self):
        """i（补）. 用户手动标记成功的记录不参与自检，不会被翻回「待确认」。"""
        handler = _make_verify_handler(dirs=_dirs([("f4", "Show - S01E04.mp4", 100)]))
        record = self._record()
        record.update({
            "manual_success": True,
            "manual_success_at": "2026-10-17 12:00:00",
            "manual_success_from": "待确认",
        })
        original = dict(record)
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record, original)
        self.assertEqual(handler._cloud_directories.list_calls, 0)

    def test_j_record_landed_in_other_dir_not_marked_unverified(self):
        """j. 记录自称落在媒体库目录且那儿真有实物 → 仍按成功（不误记「待确认」）。"""
        library_dir = "/媒体/媒体库/Show (2024)/Season 01"
        handler = _make_verify_handler(
            dirs_by_dir={
                _SEASON_DIR: _dirs([]),
                library_dir: _dirs([("f7", "Show - S01E02.mkv", 100)]),
            },
        )
        record = self._record(cloud_dir=library_dir)
        handler.apply_landed_verification_to_records(_SUB, _MEDIA, 1, [record])
        self.assertEqual(record["status"], "成功")
        self.assertTrue(record["landed"])
        self.assertEqual(record["landed_reason"], "name_match")


class PlatformHistoryGateTests(unittest.TestCase):
    """c（续）：landed=False 的记录不写 MP 整理历史。"""

    def test_landed_false_excluded_from_platform_entries(self):
        service = PlatformHistoryService(SimpleNamespace())
        record = {
            "status": "成功", "type": "电影", "title": "X",
            "file_name": "x.mkv", "landed": False,
        }
        self.assertEqual(service._platform_history_entries(record), [])

    def test_landed_true_or_absent_not_excluded_by_gate(self):
        # landed=True 与老记录（无 landed 字段）都不能被该闸门丢弃。
        service = PlatformHistoryService(SimpleNamespace())
        base = {
            "status": "成功", "type": "电影", "title": "X",
            "file_name": "x.mkv", "cloud_dir": "/媒体/电影/X",
        }
        self.assertTrue(service._platform_history_entries({**base, "landed": True}))
        self.assertTrue(service._platform_history_entries(dict(base)))


class _HistoryHarness(HistoryService, LandedVerifyService):
    """带内存存储与 platform 写入捕获的历史处理器。"""

    def __init__(self, history=None, pending=None, dirs=None):
        super().__init__(SimpleNamespace())
        object.__setattr__(self, "_OFFLINE_PENDING_KEY", "pending_offline_strm")
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())
        object.__setattr__(self, "_store", {
            "history": list(history or []),
            "pending_offline_strm": dict(pending or {}),
        })
        object.__setattr__(self, "_recorded_platform", [])
        owner = object.__getattribute__(self, "_owner")
        owner._verify_landed_enabled = True
        owner._cloud_directories = _FakeDirectories(files=_dirs(dirs or []))

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value

    def _record_platform_transfer_histories(self, records, reconcile=False):
        self._recorded_platform.extend(records)
        return len(records)

    def _history_changed(self):
        pass


class FinalizeGateTests(unittest.TestCase):
    """f. finalize 路径不通过 → status="待确认" 且不进 platform_records。"""

    def test_mark_success_blocked_when_file_missing(self):
        record = {
            "status": "处理中", "finalize_key": "k1", "share_url": "",
            "title": "Show", "season": 1, "episode": 2,
            "file_name": "Show - S01E02.mkv", "cloud_dir": _SEASON_DIR,
            "type": "电视剧", "tmdb_id": 123,
        }
        harness = _HistoryHarness(
            history=[record], pending={}, dirs=[("f4", "Show - S01E04.mp4", 100)]
        )
        matched = harness._mark_offline_history_status_batch({"k1"}, "成功")
        self.assertEqual(matched, 1)
        self.assertEqual(record["status"], "待确认")
        self.assertFalse(record["landed"])
        self.assertEqual(record["landed_reason"], "missing_in_dir")
        self.assertEqual(harness._recorded_platform, [])

    def test_mark_success_passes_when_file_present(self):
        record = {
            "status": "处理中", "finalize_key": "k1", "share_url": "",
            "title": "Show", "season": 1, "episode": 2,
            "file_name": "Show - S01E02.mkv", "cloud_dir": _SEASON_DIR,
            "type": "电视剧", "tmdb_id": 123,
        }
        harness = _HistoryHarness(
            history=[record], pending={}, dirs=[("f2", "Show - S01E02.mkv", 100)]
        )
        harness._mark_offline_history_status_batch({"k1"}, "成功")
        self.assertEqual(record["status"], "成功")
        self.assertTrue(record["landed"])
        self.assertEqual(len(harness._recorded_platform), 1)

    def test_mark_success_keeps_legacy_when_listing_unavailable(self):
        record = {
            "status": "处理中", "finalize_key": "k1", "share_url": "",
            "title": "Show", "season": 1, "episode": 2,
            "file_name": "Show - S01E02.mkv", "cloud_dir": _SEASON_DIR,
        }
        harness = _HistoryHarness(history=[record], pending={}, dirs=[])
        owner = object.__getattribute__(harness, "_owner")
        owner._cloud_directories = _FakeDirectories(checked=False)
        harness._mark_offline_history_status_batch({"k1"}, "成功")
        self.assertEqual(record["status"], "成功")
        self.assertIsNone(record.get("landed"))
        self.assertEqual(len(harness._recorded_platform), 1)


class _BaselineHarness(UpgradeBaselineService):
    def __init__(self, history):
        super().__init__(SimpleNamespace())
        object.__setattr__(self, "_data", {"history": list(history or [])})
        object.__setattr__(self, "_baseline_cache_lock", threading.RLock())
        object.__setattr__(self, "_baseline_plugin_cache", _FakeTtlCache())

    def _get_data(self, key):
        return self._data.get(key)

    def _storage(self):
        return ""


class _FakeTtlCache:
    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value):
        self._store[key] = value


class BaselineTrustTests(unittest.TestCase):
    """g. landed=False 的插件历史条目被丢弃；无 landed 的老记录行为不变。"""

    def test_landed_false_plugin_history_dropped(self):
        harness = _BaselineHarness([
            {
                "tmdb_id": 123, "title": "Show", "season": 1, "episode": 1,
                "status": "成功", "file_name": "Show - S01E01.mkv",
                "source_file_name": "raw.mkv", "landed": False,
            },
        ])
        result = harness._plugin_history_episode_files(_SUB, 1)
        self.assertNotIn(1, result)

    def test_legacy_record_without_landed_kept(self):
        harness = _BaselineHarness([
            {
                "tmdb_id": 123, "title": "Show", "season": 1, "episode": 2,
                "status": "成功", "file_name": "Show - S01E02.mkv",
                "source_file_name": "raw.mkv",
            },
        ])
        result = harness._plugin_history_episode_files(_SUB, 1)
        self.assertIn(2, result)
        self.assertEqual(result[2][0]["target_file_name"], "Show - S01E02.mkv")


class PrepareHistoryGroupTests(unittest.TestCase):
    """h. unverified_count / failed_count / skip_reasons 聚合。"""

    @staticmethod
    def _record(status, episode, skip_reason="", landed_reason=""):
        return {
            "status": status, "season": 1, "episode": episode,
            "file_size": 10, "skip_reason": skip_reason,
            "landed_reason": landed_reason, "file_name": f"E{episode}.mkv",
        }

    def test_unverified_and_failed_and_skip_reasons(self):
        group = {
            "type": "电视剧",
            "records": [
                self._record("成功", 1),
                self._record("待确认", 2, landed_reason="missing_in_dir"),
                self._record("待确认", 3, landed_reason="missing_in_dir"),
                self._record("跳过", 4, skip_reason="同集已有更优版本：同评分且候选文件未更大"),
                self._record("跳过", 5, skip_reason="同集已有更优版本：评分降低"),
                self._record("失败", 6),
            ],
        }
        prepared = HistoryService.prepare_history_group(group)
        self.assertEqual(prepared["unverified_count"], 2)
        # 「待确认」不计入失败
        self.assertEqual(prepared["failed_count"], 1)
        self.assertEqual(prepared["success_count"], 1)
        self.assertEqual(prepared["skipped_count"], 2)
        self.assertEqual(
            prepared["skip_reasons"], {"同集已有更优版本": 2}
        )
        self.assertEqual(len(prepared["unverified_records"]), 2)
        self.assertEqual(prepared["unverified_records"][0]["reason"], "missing_in_dir")

    def test_skip_reason_without_colon_uses_full_text(self):
        group = {
            "type": "电视剧",
            "records": [self._record("跳过", 1, skip_reason="同评分且候选文件未更大")],
        }
        prepared = HistoryService.prepare_history_group(group)
        self.assertEqual(
            prepared["skip_reasons"], {"同评分且候选文件未更大": 1}
        )


if __name__ == "__main__":
    unittest.main()
