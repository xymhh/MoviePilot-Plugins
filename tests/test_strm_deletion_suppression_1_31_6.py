"""v1.31.6 插件自删 .strm 的深度删除屏蔽（2026-11-23 误删事故修复）。

背景：批量删除 45 组同视频重复后，Emby 对消失的 .strm 发 library.deleted
事件，深度删除处理器按词干/季集匹配把 115 里的视频也删了（47 次，48 个
文件、32 个目录）。修复：插件主动删本地 .strm 后登记短期屏蔽（TTL 30
分钟），深度删除处理器在去重 claim 之前过滤屏蔽路径。

覆盖：
- 屏蔽表：登记/命中/未命中、归一化（斜杠/尾部斜杠/反斜杠）、TTL 过期、
  prune、1000 条上限丢最旧；
- SyncHandler 两方法：本地路径 → 媒体服务器口径翻译（映射后与原始两条
  都登记）、表缺失时返回 False；
- webhook 处理器：屏蔽命中跳过且不删不通知；TTL 过期后恢复删除；未屏蔽
  路径照常删除；部分屏蔽只删剩余路径；被屏蔽的路径不消耗去重窗口；
- 删除登记：单组删除与批量删除都登记屏蔽；已不存在的同视频 .strm 按成功计、
  不同视频组仍拒绝；_delete_local_strm_for_cloud_files 的删除也被屏蔽；
- _generate_strm 的兄弟清理登记屏蔽。

harness：复用 test_strm_duplicates_1_31_0 的假云盘/服务构造（线上布局）；
service.py 与 media_library.py 按 test_sync_handler_forwarder_coverage /
test_get_api 的打桩方式加载。
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path, PurePosixPath
from types import MethodType, SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import (  # noqa: E402
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
)

# 先注册真实 TTLCache：test_strm_duplicates_1_31_0 在模块级加载
# utils/cache.py，全量套件里靠 test_cache_json_codec 先跑才不爆；
# 单文件跑必须自己先注册（见 plugin_env.install_real_ttl_cache）。
install_real_ttl_cache()

from test_strm_duplicates_1_31_0 import (  # noqa: E402
    BASE_URL,
    StrmDuplicatesService,
    StrmGenerator,
    StrmOpsMixin,
    _FakeCloudQuery,
    _FakeMutations,
    _FakePlaybackReference,
    _FakePlaybackReferenceForOps,
    _MetaInfoTestCase,
    _OpsFakeCloudQuery,
    _redirect_content,
    _write_strm,
)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.db.downloadhistory_oper",
    "app.db.models",
    "app.db.models.mediaserver", "app.db.models.subscribe",
    "app.db.models.downloadhistory",
    "app.chain", "app.chain.mediaserver",
    "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.utils.string", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.adapters",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.api",
    "cloudsubscribex.core.media", "cloudsubscribex.core.services",
    "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache", "cloudsubscribex.utils.notify_template",
    "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)
install_core_delegation()
install_real_ttl_cache()
ensure_package("cloudsubscribex.core.sync_pause").is_paused = lambda *a, **k: False

_suppression_module = load_module(
    "cloudsubscribex.utils.strm_deletion_suppression",
    "plugins.v2/cloudsubscribex/utils/strm_deletion_suppression.py",
)
StrmDeletionSuppression = _suppression_module.StrmDeletionSuppression
normalize_media_server_path = _suppression_module.normalize_media_server_path
suppress_deleted_strm_path = _suppression_module.suppress_deleted_strm_path
is_deleted_strm_suppressed = _suppression_module.is_deleted_strm_suppressed

load_module(
    "cloudsubscribex.utils.path_map",
    "plugins.v2/cloudsubscribex/utils/path_map.py",
)
load_module(
    "cloudsubscribex.utils.deep_delete_dedupe",
    "plugins.v2/cloudsubscribex/utils/deep_delete_dedupe.py",
)
load_module(
    "cloudsubscribex.utils.deep_delete_match",
    "plugins.v2/cloudsubscribex/utils/deep_delete_match.py",
)

_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler

_media_library_module = load_module(
    "cloudsubscribex.core.api.media_library",
    "plugins.v2/cloudsubscribex/core/api/media_library.py",
)
MediaLibraryApi = _media_library_module.MediaLibraryApi

_cleanup_module = load_module(
    "cloudsubscribex.handlers.sync.cleanup",
    "plugins.v2/cloudsubscribex/handlers/sync/cleanup.py",
)
HistoryCleanupService = _cleanup_module.HistoryCleanupService

SERVER_STRM = "/media/strm/115网盘/电影/痴迷 (2026)/痴迷 (2026) - 2160p.strm"
LOCAL_STRM = "/strm/115网盘/电影/痴迷 (2026)/痴迷 (2026) - 2160p.strm"


class SuppressionTableTests(unittest.TestCase):
    def test_suppress_and_check(self):
        table = StrmDeletionSuppression()
        self.assertFalse(table.is_suppressed(SERVER_STRM))
        self.assertTrue(table.suppress(SERVER_STRM))
        self.assertTrue(table.is_suppressed(SERVER_STRM))
        self.assertFalse(table.is_suppressed(
            "/media/strm/115网盘/电影/另一部/另一部.strm"
        ))

    def test_suppress_empty_path_returns_false(self):
        table = StrmDeletionSuppression()
        self.assertFalse(table.suppress(""))
        self.assertFalse(table.suppress(None))
        self.assertFalse(table.is_suppressed(""))

    def test_normalize_matches_deep_delete_paths(self):
        # 与 MediaLibraryApi._normalize_media_path 同口径：反斜杠、重复
        # 斜杠、尾部斜杠归一后是同一条键。
        table = StrmDeletionSuppression()
        table.suppress("\\\\media\\strm\\115网盘\\电影\\痴迷 (2026)\\痴迷 (2026) - 2160p.strm")
        self.assertTrue(table.is_suppressed(SERVER_STRM))
        self.assertTrue(table.is_suppressed(SERVER_STRM + "/"))
        self.assertTrue(table.is_suppressed(
            "/media//strm/115网盘/电影/痴迷 (2026)/痴迷 (2026) - 2160p.strm"
        ))
        self.assertEqual(
            normalize_media_server_path("\\\\media\\strm\\x.strm/"),
            "/media/strm/x.strm",
        )

    def test_ttl_expiry(self):
        table = StrmDeletionSuppression(ttl_seconds=0.05)
        table.suppress(SERVER_STRM)
        self.assertTrue(table.is_suppressed(SERVER_STRM))
        time.sleep(0.08)
        self.assertFalse(table.is_suppressed(SERVER_STRM))
        self.assertEqual(len(table), 0)

    def test_prune_removes_expired(self):
        table = StrmDeletionSuppression(ttl_seconds=0.05)
        table.suppress(SERVER_STRM)
        table.suppress("/media/strm/115网盘/电影/另一部/另一部.strm")
        time.sleep(0.08)
        self.assertEqual(table.prune(), 2)
        self.assertEqual(len(table), 0)

    def test_max_entries_drops_oldest(self):
        table = StrmDeletionSuppression(ttl_seconds=3600, max_entries=3)
        table.suppress("/media/strm/a.strm")
        table.suppress("/media/strm/b.strm")
        table.suppress("/media/strm/c.strm")
        table.suppress("/media/strm/d.strm")
        self.assertEqual(len(table), 3)
        self.assertFalse(table.is_suppressed("/media/strm/a.strm"))
        self.assertTrue(table.is_suppressed("/media/strm/d.strm"))

    def test_suppress_many_counts(self):
        table = StrmDeletionSuppression()
        self.assertEqual(
            table.suppress_many([SERVER_STRM, "", LOCAL_STRM]), 2,
        )

    def test_defensive_helpers_without_methods(self):
        # owner 上没有屏蔽方法时一律按“未屏蔽”走，不抛错。
        self.assertFalse(suppress_deleted_strm_path(SimpleNamespace(), LOCAL_STRM))
        self.assertFalse(suppress_deleted_strm_path(None, LOCAL_STRM))
        self.assertFalse(is_deleted_strm_suppressed(SimpleNamespace(), SERVER_STRM))
        self.assertFalse(is_deleted_strm_suppressed(None, SERVER_STRM))


class SyncHandlerSuppressionTests(unittest.TestCase):
    def _bound(self, table, mappings):
        fake = SimpleNamespace(
            _strm_deletion_suppression=table,
            _media_server_path_mappings=mappings,
        )
        return (
            MethodType(SyncHandler.suppress_strm_deletion, fake),
            MethodType(SyncHandler.is_strm_deletion_suppressed, fake),
        )

    def test_suppress_registers_mapped_and_raw(self):
        table = StrmDeletionSuppression()
        suppress, is_suppressed = self._bound(table, "/media/strm=/strm")
        self.assertTrue(suppress(LOCAL_STRM))
        # 映射后（webhook 口径）与原始（本地口径）两条都登记。
        self.assertTrue(is_suppressed(SERVER_STRM))
        self.assertTrue(is_suppressed(LOCAL_STRM))
        self.assertTrue(table.is_suppressed(SERVER_STRM))

    def test_suppress_without_mappings_registers_raw(self):
        table = StrmDeletionSuppression()
        suppress, is_suppressed = self._bound(table, "")
        self.assertTrue(suppress(LOCAL_STRM))
        self.assertTrue(is_suppressed(LOCAL_STRM))

    def test_missing_table_returns_false(self):
        fake = SimpleNamespace(_media_server_path_mappings="/media/strm=/strm")
        suppress = MethodType(SyncHandler.suppress_strm_deletion, fake)
        is_suppressed = MethodType(SyncHandler.is_strm_deletion_suppressed, fake)
        self.assertFalse(suppress(LOCAL_STRM))
        self.assertFalse(is_suppressed(SERVER_STRM))

    def test_ttl_constant_covers_observed_delay(self):
        # 事故实测 Emby 事件最长延迟约 11 分钟，TTL 30 分钟须覆盖它。
        self.assertGreaterEqual(
            SyncHandler._STRM_DELETION_SUPPRESSION_TTL_SECONDS, 30 * 60,
        )


class _FakeSyncHandler:
    """只认屏蔽表的假 SyncHandler：记录删除调用，不碰网盘。"""

    def __init__(self, table):
        self.table = table
        self.deleted_calls = []

    def is_strm_deletion_suppressed(self, server_path):
        return self.table.is_suppressed(server_path)

    def delete_by_media_server_paths(self, paths):
        self.deleted_calls.append(list(paths))
        return {
            "matched": len(paths), "deleted": len(paths),
            "linked_deleted": len(paths), "cache_deleted": 0, "skipped": 0,
            "cloud_files_deleted": len(paths),
            "cloud_directories_deleted": len(paths),
            "strm_deleted": 0, "local_directories_deleted": 0,
        }


class DeepDeleteSuppressionHandlerTests(unittest.TestCase):
    def _api(self, table):
        handler = _FakeSyncHandler(table)
        owner = SimpleNamespace(
            _sync_handler=handler,
            _platform_deep_delete_enabled=True,
            _platform_deep_delete_title_keywords=[],
            _notify=False,
        )
        return MediaLibraryApi(owner), handler

    def _event(self, item_path, extra_path=None):
        event = {"event": "library.deleted", "item_path": item_path}
        if extra_path:
            event["json_object"] = {"Item": {"Path": extra_path}}
        return event

    def test_suppressed_path_skipped_without_delete_or_notify(self):
        table = StrmDeletionSuppression()
        table.suppress(SERVER_STRM)
        api, handler = self._api(table)
        self.assertFalse(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        self.assertEqual(handler.deleted_calls, [])

    def test_unsuppressed_path_deletes_normally(self):
        table = StrmDeletionSuppression()
        api, handler = self._api(table)
        self.assertTrue(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        self.assertEqual(handler.deleted_calls, [[SERVER_STRM]])

    def test_ttl_expiry_restores_delete(self):
        table = StrmDeletionSuppression(ttl_seconds=0.05)
        table.suppress(SERVER_STRM)
        api, handler = self._api(table)
        self.assertFalse(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        time.sleep(0.08)
        self.assertTrue(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        self.assertEqual(handler.deleted_calls, [[SERVER_STRM]])

    def test_partial_suppression_deletes_remaining_only(self):
        table = StrmDeletionSuppression()
        other = "/media/strm/115网盘/电影/另一部/另一部.strm"
        table.suppress(SERVER_STRM)
        api, handler = self._api(table)
        self.assertTrue(api.handle_platform_media_webhook(
            self._event(SERVER_STRM, extra_path=other)
        ))
        self.assertEqual(handler.deleted_calls, [[other]])

    def test_suppressed_path_does_not_consume_dedup_window(self):
        # 过滤在 claim 之前：被屏蔽的事件不占位，过期后同一路径仍能删；
        # 若顺序反了，第一次事件占位后第二次会被当重复丢掉。
        table = StrmDeletionSuppression(ttl_seconds=0.05)
        table.suppress(SERVER_STRM)
        api, handler = self._api(table)
        self.assertFalse(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        time.sleep(0.08)
        self.assertTrue(api.handle_platform_media_webhook(self._event(SERVER_STRM)))
        self.assertEqual(handler.deleted_calls, [[SERVER_STRM]])

    def test_suppression_check_defensive_without_handler(self):
        # 同步处理器取不到时按未屏蔽走（不让修复本身成为故障点）。
        owner = SimpleNamespace(
            _sync_handler=None,
            _platform_deep_delete_enabled=True,
            _platform_deep_delete_title_keywords=[],
            _notify=False,
        )
        api = MediaLibraryApi(owner)
        self.assertFalse(api.handle_platform_media_webhook(self._event(SERVER_STRM)))


def _recording_duplicates_service(root, query=None, mutations=None):
    """带屏蔽登记记录的重复扫描服务（线上布局：root 就是 .strm 根）。"""
    recorded = []
    owner = SimpleNamespace(
        _local_resource_path=str(root),
        _cloud_query=query,
        _cloud_mutations=mutations,
        _playback_reference=_FakePlaybackReference(),
        _CLOUD_MEDIA_ROOT="/Alist/影视/moviepilot/媒体库",
    )
    owner.suppress_strm_deletion = lambda path: recorded.append(str(path)) or True
    return StrmDuplicatesService(owner), recorded


def _same_video_group(root, pickcode="same-pc"):
    parent = Path(root) / "Season 1"
    kept = _write_strm(
        parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
        _redirect_content(pickcode, "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
    )
    extra = _write_strm(
        parent / "两京十五日 - S01E06 - 第6集.strm",
        _redirect_content(pickcode, "两京十五日 - S01E06 - 第6集.mkv"),
    )
    return parent, kept, extra


class DeleteRegistrationTests(_MetaInfoTestCase):
    def test_single_delete_registers_suppression(self):
        parent, kept, extra = _same_video_group(self.root)
        service, recorded = _recording_duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=_FakeMutations(),
        )
        groups = service.duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 1)
        result = service.duplicates_delete(groups[0], 0)
        self.assertTrue(result["success"], result)
        self.assertFalse(extra.exists())
        self.assertTrue(kept.exists())
        self.assertEqual(recorded, [str(extra)])

    def test_batch_delete_registers_suppression(self):
        first_parent, _, first_extra = _same_video_group(self.root, "pc-one")
        second_parent = self.root / "Season 2"
        _write_strm(
            second_parent / "两京十五日 {tmdb-203483} - S01E07 - 第7集.strm",
            _redirect_content("pc-two", "两京十五日 {tmdb-203483} - S01E07 - 第7集.mkv"),
        )
        second_extra = _write_strm(
            second_parent / "两京十五日 - S01E07 - 第7集.strm",
            _redirect_content("pc-two", "两京十五日 - S01E07 - 第7集.mkv"),
        )
        service, recorded = _recording_duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=_FakeMutations(),
        )
        groups = service.duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 2)
        result = service.duplicates_delete_batch([
            {"group": group, "keep_index": 0} for group in groups
        ])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["deleted"], 2)
        self.assertEqual(sorted(recorded), sorted([str(first_extra), str(second_extra)]))

    def test_already_gone_same_video_counts_as_deleted(self):
        # 事故复现：.strm 在读到删之间已被别处清掉（深度删除连锁），
        # 同视频组按成功计，不因此报整组失败。
        parent, kept, extra = _same_video_group(self.root)
        extra.unlink()
        service, recorded = _recording_duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=_FakeMutations(),
        )
        groups = service.duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 0)  # 只剩一份，不再是重复组
        group = {
            "key": "k", "title": "两京十五日", "kind": "same-video",
            "season": 1, "episode": 6, "suggested_keep": 0,
            "items": [
                {"file_name": kept.name, "local_path": "Season 1/" + kept.name,
                "pickcode": "same-pc", "share_id": "", "kind": "redirect",
                "video_name": "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
                "video_size": 100},
                {"file_name": extra.name, "local_path": "Season 1/" + extra.name,
                "pickcode": "same-pc", "share_id": "", "kind": "redirect",
                "video_name": "两京十五日 - S01E06 - 第6集.mkv",
                "video_size": 100},
            ],
        }
        result = service.duplicates_delete(group, 0)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["data"]["results"][1].get("already_gone"))
        self.assertIn("已不存在", result["data"]["results"][1]["message"])
        self.assertEqual(recorded, [str(extra)])

    def test_already_gone_different_video_still_refuses(self):
        # 不同视频组仍需删网盘视频：.strm 已不在、无法核对身份时按失败计，
        # 请重新扫描（宁可留着，不可误删保留的那份视频）。
        parent, kept, extra = _same_video_group(self.root)
        extra.unlink()
        group = {
            "key": "k", "title": "两京十五日", "kind": "different-video",
            "season": 1, "episode": 6, "suggested_keep": 0,
            "items": [
                {"file_name": kept.name, "local_path": "Season 1/" + kept.name,
                "pickcode": "pc-kept", "share_id": "", "kind": "redirect",
                "video_name": "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
                "video_size": 100},
                {"file_name": extra.name, "local_path": "Season 1/" + extra.name,
                "pickcode": "pc-gone", "share_id": "", "kind": "redirect",
                "video_name": "两京十五日 - S01E06 - 第6集.mkv",
                "video_size": 200},
            ],
        }
        service, _ = _recording_duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=_FakeMutations(),
        )
        result = service.duplicates_delete(group, 0)
        self.assertFalse(result["success"])
        self.assertIn("重新扫描", result["data"]["results"][1]["message"])


class _FakePathMapper:
    """云盘目录+文件名 → 本地 STRM 路径（与 utils/strm.py 同款映射）。"""

    def local_path(self, *, local_root, cloud_root, cloud_dir, file_name):
        relative = PurePosixPath(cloud_dir).relative_to(PurePosixPath(cloud_root))
        return (
            Path(local_root) / relative
            / (Path(file_name).stem + ".strm")
        )


class LocalStrmCleanupSuppressionTests(unittest.TestCase):
    def test_delete_local_strm_registers_suppression(self):
        # _delete_local_strm_for_cloud_files（深度删除自己清本地 STRM）
        # 的删除也被屏蔽，防循环触发。
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        cloud_dir = "/Alist/影视/moviepilot/媒体库/电影/外语电影/痴迷 (2026)"
        strm_path = (
            root / "电影" / "外语电影" / "痴迷 (2026)"
            / "痴迷 (2026) - 2160p.strm"
        )
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        strm_path.write_text("x", encoding="utf-8")
        recorded = []
        owner = SimpleNamespace(
            _local_resource_path=str(root),
            _path_mapper=_FakePathMapper(),
            _CLOUD_MEDIA_ROOT="/Alist/影视/moviepilot/媒体库",
        )
        owner.suppress_strm_deletion = lambda path: recorded.append(str(path)) or True
        service = HistoryCleanupService(owner)
        item = SimpleNamespace(name="痴迷 (2026) - 2160p.mkv")
        strm_deleted, _dirs = service._delete_local_strm_for_cloud_files(
            cloud_dir, [item], None,
        )
        self.assertEqual(strm_deleted, 1)
        self.assertFalse(strm_path.exists())
        self.assertEqual(recorded, [str(strm_path)])


class SiblingCleanupSuppressionTests(_MetaInfoTestCase):
    def _ops_service(self, query, recorded):
        owner = SimpleNamespace()
        owner.suppress_strm_deletion = lambda path: recorded.append(str(path)) or True
        service = StrmOpsMixin(owner)
        service._strm_generate_enabled = True
        service._strm_generator = StrmGenerator(
            base_url=BASE_URL,
            template=StrmGenerator.REDIRECT_TEMPLATE,
            provider_variables=["pickcode"],
        )
        service._local_resource_path = str(self.root)
        service._CLOUD_MEDIA_ROOT = "/"
        service._playback_reference = _FakePlaybackReferenceForOps()
        service._cloud_query = query
        return service

    def test_generate_registers_cleaned_sibling(self):
        cloud_dir = "/115网盘/剧集/国产剧/两京十五日/Season 1"
        recorded = []
        service = self._ops_service(_OpsFakeCloudQuery({}), recorded)
        first = service._generate_strm(
            cloud_dir, "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
            target_file=SimpleNamespace(
                name="两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
                playback_values={"pickcode": "pick-first"},
            ),
            lookup_target=False,
        )
        self.assertIsNotNone(first)
        self.assertEqual(recorded, [])
        second = service._generate_strm(
            cloud_dir, "两京十五日 - S01E06 - 第6集.mkv",
            target_file=SimpleNamespace(
                name="两京十五日 - S01E06 - 第6集.mkv",
                playback_values={"pickcode": "pick-second"},
            ),
            lookup_target=False,
        )
        self.assertIsNotNone(second)
        self.assertFalse(first.exists())
        # 清理掉的旧兄弟 .strm 已登记屏蔽。
        self.assertEqual(recorded, [str(first)])


if __name__ == "__main__":
    unittest.main()
