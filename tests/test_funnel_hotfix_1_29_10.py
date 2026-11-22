"""v1.29.10 漏斗 P0 热修复（评审 B1/B2/B3/B4/B5）。

覆盖（按合并文档 §4）：
- B1：两处实查强制刷新——真实 6 小时 TTL 缓存两轮（V1 门禁）；
- 结算：守卫 fail-closed、held 挡结算、两层结算（轮次不提前完结、
  终态回写才完结、回写缺完成集数转 held）；
- B2：重复入队第二份终态为“已在途”跳过且无失败记录（V2 门禁）、洗版
  高分替换/低分跳过、留大（评分相等看体积）；
- 租约/退避/回收：退避表实际档位与文档一致、回收实查失败转 held、
  pending 过期联动 held、item is None 转 held、mark_failed 拒翻 held；
- 洗版：三条件缺一即不删旧版（复查门进删除点）、挂起行人工删旧版；
- V3 门禁：100 分钟长尾 tracker 下行不 held、订阅不结算、pending 不过期。

harness：复用 test_funnel_stage1_1_29_7 的 _DbMixin / _Fake*；
V1 用真实 EpisodeDedupService + 真实平台 TTL 缓存
（create_platform_ttl_cache，经 test_cache_json_codec 的
PicklingTTLCache 后端）+ 假目录；基线侧桩成 {}（测的是季目录
缓存行为，不是基线）。
"""

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_funnel_stage1_1_29_7 import (  # noqa: E402
    DummyMpWaitService,
    FakeTracker,
    SyncHandler,
    _DbMixin,
    _FakeDedup,
    _TV_CLOUD_DIR,
    _TV_FILE,
    _TV_SIZE,
    _ctx,
    _load_once,
    _payload,
    _stub,
    _tv_item,
    _tv_media,
    funnel_module,
)
from test_cache_json_codec import cache_module as _real_cache_module  # noqa: E402
from test_funnel_stage3_1_29_9 import (  # noqa: E402
    MergeOwner,
    SUBSCRIBE_ID,
    TelevisionSyncProcessor,
    _manual_resource,
    _subscribe,
)

FUNNEL_RETRY_DELAYS = funnel_module.FUNNEL_RETRY_DELAYS
PKG = "cloudsubscribex"

dedup_module = _load_once(
    f"{PKG}.handlers.sync.episode_dedup",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins.v2", "cloudsubscribex", "handlers", "sync", "episode_dedup.py",
    ),
)
EpisodeDedupService = dedup_module.EpisodeDedupService

movie_module = _load_once(
    f"{PKG}.handlers.sync.movie",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins.v2", "cloudsubscribex", "handlers", "sync", "movie.py",
    ),
)
MovieSyncProcessor = movie_module.MovieSyncProcessor

funnel_api_module = _load_once(
    f"{PKG}.core.api.funnel",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "plugins.v2", "cloudsubscribex", "core", "api", "funnel.py",
    ),
)
FunnelApi = funnel_api_module.FunnelApi

_TV_TMDB_ID = 294498
_TV_TITLE = "多喜一家人"
_TV_OLD_FILE = "多喜一家人 - S01E09 - 第9集.mkv"
_TV_NEW2_FILE = "多喜一家人 {tmdb-294498} - S01E09 - 第9集 2160p.mkv"


# ----------------------------------------------------------------------
# B1：真实缓存
# ----------------------------------------------------------------------
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
    def __init__(self, directory_id="dir-1", checked=True):
        self.directory_id = directory_id
        self.checked = checked


class _FakeDirectories:
    """季目录假网盘：files 可换（模拟落盘），list_calls 计实列次数。"""

    def __init__(self, files=None):
        self._files = list(files or [])
        self.list_calls = 0
        self.resolve_calls = 0

    def resolve_directory(self, path, create=False):
        self.resolve_calls += 1
        return _FakeLookup(directory_id="dir-1")

    def list_directory(self, directory_id):
        self.list_calls += 1
        return _FakeListing(self._files)


def _dedup_baseline_key(subscribe, season):
    return (
        int(getattr(subscribe, "tmdbid", 0) or 0),
        str(getattr(subscribe, "name", "") or "").casefold(),
        str(getattr(subscribe, "year", "") or ""),
        int(season or 1),
    )


def _real_season_cache(identity):
    """真实平台 TTL 缓存（生产同款 6 小时 TTL；后端是
    test_cache_json_codec 的 PicklingTTLCache，窗内行为与线上一致）。"""
    return _real_cache_module.create_platform_ttl_cache(
        "sync:season_listing", identity, maxsize=256, ttl=6 * 60 * 60,
    )


def _make_real_dedup(directories, cache):
    """真实 EpisodeDedupService：除季目录缓存外全是桩；基线侧桩成 {}。"""
    store = SimpleNamespace()
    store._season_listing_cache = cache
    store._baseline_key = _dedup_baseline_key
    store._CLOUD_MEDIA_ROOT = "/"
    store._cloud_directories = directories
    store._episode_dedup_enabled = True
    store._upgrade_mode = "largest"
    store.naming = SimpleNamespace(
        _resolve_resource_season_dir=lambda *args, **kwargs: Path(_TV_CLOUD_DIR),
    )
    store.upgrade_rule_scoring = SimpleNamespace(
        _get_mp_rule_score=lambda *args, **kwargs: 80,
    )
    store._build_episode_baseline = lambda *args, **kwargs: {}
    service = EpisodeDedupService.__new__(EpisodeDedupService)
    object.__setattr__(service, "_owner", store)
    return service


def _fake_subscribe():
    return SimpleNamespace(
        id=444, tmdbid=_TV_TMDB_ID, name=_TV_TITLE, year="2026",
        season=1, type="电视剧",
    )


class FunnelHotfixB1Tests(unittest.TestCase):
    """B1：强制刷新——真实缓存两轮（V1 门禁）。"""

    def test_season_dir_files_force_refresh_bypasses_stale_cache(self):
        """_season_dir_files：不强制刷新读 6 小时快照，强制刷新见新文件，
        刷新后缓存即新快照（后续不强制读也见新文件）。"""
        file_a = _FakeCloudFile("f-a", _TV_FILE, _TV_SIZE)
        file_b = _FakeCloudFile("f-b", _TV_NEW2_FILE, _TV_SIZE + 100)
        directories = _FakeDirectories([file_a])
        dedup = _make_real_dedup(directories, _real_season_cache("b1-a"))
        subscribe = _fake_subscribe()
        media = _tv_media()
        verified, _path, files = dedup._season_dir_files(subscribe, media, 1)
        self.assertTrue(verified)
        self.assertEqual([f["file_id"] for f in files], ["f-a"])
        self.assertEqual(directories.list_calls, 1)
        # 第一份落盘后：目录新增第二份。
        directories._files = [file_a, file_b]
        verified, _path, files = dedup._season_dir_files(subscribe, media, 1)
        self.assertTrue(verified)
        self.assertEqual([f["file_id"] for f in files], ["f-a"])
        self.assertEqual(directories.list_calls, 1)
        # 强制刷新：见新文件，且只实列一次。
        verified, _path, files = dedup._season_dir_files(
            subscribe, media, 1, force_refresh=True
        )
        self.assertTrue(verified)
        self.assertEqual(
            sorted(f["file_id"] for f in files), ["f-a", "f-b"]
        )
        self.assertEqual(directories.list_calls, 2)
        # 刷新后缓存即新快照：不强制读也见新文件。
        verified, _path, files = dedup._season_dir_files(subscribe, media, 1)
        self.assertEqual(
            sorted(f["file_id"] for f in files), ["f-a", "f-b"]
        )
        self.assertEqual(directories.list_calls, 2)

    def test_collect_two_rounds_force_refresh_sees_new_file(self):
        """V1 门禁：真实缓存跑两轮，第一轮落盘后第二轮强制刷新必须
        看到新文件；且一次 _collect 查询只实列一次目录。"""
        file_a = _FakeCloudFile("f-a", _TV_FILE, _TV_SIZE)
        file_b = _FakeCloudFile("f-b", _TV_NEW2_FILE, _TV_SIZE + 100)
        directories = _FakeDirectories([file_a])
        dedup = _make_real_dedup(directories, _real_season_cache("b1-b"))
        subscribe = _fake_subscribe()
        media = _tv_media()
        first = dedup._collect_episode_versions(
            subscribe, media, 1, force_refresh=True
        )
        self.assertEqual(
            [entry["file_id"] for entry in first.get(9, [])], ["f-a"]
        )
        self.assertEqual(directories.list_calls, 1)
        # 第一份落盘后：目录新增同集第二份。
        directories._files = [file_a, file_b]
        # 第二轮不强制刷新：仍读旧快照，只见第一份（B1 旧行为口径）。
        stale = dedup._collect_episode_versions(subscribe, media, 1)
        self.assertEqual(
            [entry["file_id"] for entry in stale.get(9, [])], ["f-a"]
        )
        self.assertEqual(directories.list_calls, 1)
        # 第二轮强制刷新：两份都见；一次查询仍只实列一次。
        fresh = dedup._collect_episode_versions(
            subscribe, media, 1, force_refresh=True
        )
        self.assertEqual(
            sorted(entry["file_id"] for entry in fresh.get(9, [])),
            ["f-a", "f-b"],
        )
        self.assertEqual(directories.list_calls, 2)


class FunnelHotfixB1CallSiteTests(_DbMixin):
    """B1 调用点：两处实查入口都传 force_refresh=True。"""

    def _setup_row(self, **item_extra):
        item = _tv_item(**item_extra)
        row, _ = self.service.enqueue_from_pending(
            item, "pk-tv-09", _tv_media()
        )
        pending = self._pending_item("pk-tv-09", **item_extra)
        return row, item, pending

    def test_pre_organize_check_uses_force_refresh(self):
        """落盘前实查调 _collect 时 force_refresh=True。"""
        _row, _item, _pending = self._setup_row()
        self.owner.episode_dedup = _FakeDedup(versions={
            9: [{"file_id": "f-other", "file_name": _TV_NEW2_FILE}],
        })
        ok, _reason = self.service._pre_organize_check(
            _row, _pending, _tv_media()
        )
        self.assertFalse(ok)
        self.assertEqual(len(self.owner.episode_dedup.collect_calls), 1)
        self.assertTrue(self.owner.episode_dedup.collect_calls[0][3])

    def test_precommit_recheck_uses_force_refresh(self):
        """提交前复查调 _collect 时 force_refresh=True。"""
        _row, _item, _pending = self._setup_row()
        self.owner.episode_dedup = _FakeDedup(versions={
            9: [{"file_id": "f-other", "file_name": _TV_NEW2_FILE}],
        })
        intercepted, _reason = self.service.precommit_recheck(
            _pending, "pk-tv-09", _tv_media()
        )
        self.assertTrue(intercepted)
        self.assertEqual(len(self.owner.episode_dedup.collect_calls), 1)
        self.assertTrue(self.owner.episode_dedup.collect_calls[0][3])


# ----------------------------------------------------------------------
# 结算：fail-closed + held 挡结算 + 两层结算
# ----------------------------------------------------------------------
class FunnelHotfixSettlementTests(_DbMixin):
    """结算守卫：held 计入未终态；查不到/异常 fail-closed。"""

    def test_has_unsettled_rows_held_blocks_active_does_not(self):
        """held 行：未终态守卫挡结算；在途守卫不含 held（两者口径分开，
        入队冲突与在途查询仍用 _active_filter）。"""
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media(), subscribe_id=444,
        )
        self.assertTrue(self.service.has_unsettled_rows(444))
        self.assertTrue(self.service.has_active_rows(444))
        self.repo.mark_held(row["id"], "人工挂起")
        self.assertTrue(self.service.has_unsettled_rows(444))
        self.assertFalse(self.service.has_active_rows(444))
        self.repo.mark_done(row["id"], "终态")
        self.assertFalse(self.service.has_unsettled_rows(444))
        self.assertFalse(self.service.has_active_rows(444))

    def test_has_unsettled_rows_empty_subscribe(self):
        """无行/无订阅 id 时不挡结算（守卫只对真实订阅生效）。"""
        self.assertFalse(self.service.has_unsettled_rows(444))
        self.assertFalse(self.service.has_unsettled_rows(0))
        self.assertFalse(self.service.has_unsettled_rows(None))

    def test_sync_handler_forwarder_fail_closed(self):
        """SyncHandler 转发层：漏斗组件缺失/查询抛异常时返回 True
        （暂缓结算），绝不按“无在途行”结算。"""
        handler = SyncHandler.__new__(SyncHandler)
        self.assertTrue(handler.funnel_has_unsettled_rows(444))
        self.assertTrue(handler.funnel_has_active_rows(444))
        broken = SimpleNamespace(
            has_unsettled_rows=MagicMock(side_effect=RuntimeError("db down")),
            has_active_rows=MagicMock(side_effect=RuntimeError("db down")),
        )
        object.__setattr__(handler, "funnel", broken)
        self.assertTrue(handler.funnel_has_unsettled_rows(444))
        self.assertTrue(handler.funnel_has_active_rows(444))

    def test_finish_pending_subscription_missing_media_holds_row(self):
        """两层结算：终态回写缺完成集数时记 warning 并把漏斗行置 held，
        订阅结算被 held 挡住，不静默跳过。"""
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        handler = SyncHandler.__new__(SyncHandler)
        object.__setattr__(
            handler, "_cloud_drive", SimpleNamespace(name="115网盘")
        )
        object.__setattr__(handler, "funnel", self.service)
        item = {
            "pending_key": "pk-tv-09",
            "success_episodes": [],
            "notification_episodes": [],
            "subscribe_id": 0,
            "file_name": _TV_FILE,
            "task_type": "share",
        }
        handler._finish_pending_subscription(item, {}, mediainfo=None)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")


class FunnelHotfixSettlementHelperTests(unittest.TestCase):
    """TV/电影结算 helper：组件缺失/异常时 fail-closed。"""

    def test_tv_helper_fail_closed_without_component(self):
        """TV helper：owner 上没有 funnel_has_unsettled_rows 时返回 True。"""
        from test_tv_flow_merge_1_29_4 import (  # noqa: E402
            TelevisionSyncProcessor as _TvProcessor,
        )
        processor = _TvProcessor(SimpleNamespace())
        subscribe = SimpleNamespace(id=444)
        self.assertTrue(processor._subscribe_has_active_funnel_rows(subscribe))
        processor = _TvProcessor(SimpleNamespace(
            funnel_has_unsettled_rows=MagicMock(
                side_effect=RuntimeError("db down")
            ),
        ))
        self.assertTrue(processor._subscribe_has_active_funnel_rows(subscribe))
        processor = _TvProcessor(SimpleNamespace(
            funnel_has_unsettled_rows=lambda sid: False,
        ))
        self.assertFalse(processor._subscribe_has_active_funnel_rows(subscribe))
        processor = _TvProcessor(SimpleNamespace(
            funnel_has_unsettled_rows=lambda sid: True,
        ))
        self.assertTrue(processor._subscribe_has_active_funnel_rows(subscribe))

    def test_movie_helper_fail_closed_without_component(self):
        """电影 helper：与 TV 同一口径（fail-closed + held 计入）。"""
        processor = MovieSyncProcessor(SimpleNamespace())
        subscribe = SimpleNamespace(id=445)
        self.assertTrue(processor._subscribe_has_active_funnel_rows(subscribe))
        processor = MovieSyncProcessor(SimpleNamespace(
            funnel_has_unsettled_rows=MagicMock(
                side_effect=RuntimeError("db down")
            ),
        ))
        self.assertTrue(processor._subscribe_has_active_funnel_rows(subscribe))
        processor = MovieSyncProcessor(SimpleNamespace(
            funnel_has_unsettled_rows=lambda sid: False,
        ))
        self.assertFalse(processor._subscribe_has_active_funnel_rows(subscribe))

    def test_tv_round_does_not_settle_with_unsettled_rows(self):
        """轮次不提前完结：有未终态行（含 held）时本轮不调
        check_and_finish_subscribe，等终态回写。"""
        owner = MergeOwner({}, cloud_upgrade_subscribe=False)
        owner.cloud_episodes = {1, 2, 3}
        owner._search_handler.search_sources.return_value = {
            "manual": [_manual_resource()],
        }
        owner.funnel_has_unsettled_rows = lambda subscribe_id: True
        subscribe = _subscribe(total_episode=5)
        subscribe.id = SUBSCRIBE_ID
        result = TelevisionSyncProcessor(owner).process_tv_subscribe(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 2)
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()


# ----------------------------------------------------------------------
# B2：重复入队
# ----------------------------------------------------------------------
class FunnelHotfixB2Tests(_DbMixin):
    """B2：created=False 必须处理；第二份有明确终态（V2 门禁）。"""

    def _batch_handler(self):
        handler = SyncHandler.__new__(SyncHandler)
        object.__setattr__(handler, "funnel", self.service)
        return handler

    def _second_item(self, **extra):
        item = _tv_item(
            file_name=_TV_NEW2_FILE,
            staging_name=_TV_NEW2_FILE,
            result_key="f-tv-09b",
            file_size=_TV_SIZE + 100,
            share_url="https://example.invalid/share-2",
        )
        item.update(extra)
        return item

    def test_duplicate_second_normal_skipped_no_failure_record(self):
        """V2 门禁：普通转存第二份记“已在途”跳过——pending 记录弹出
        （不持久化）、不写失败记录、第一份继续被驱动。"""
        handler = self._batch_handler()
        first = _tv_item(share_url="https://example.invalid/share-1")
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        pending = {
            "pk-tv-09": dict(first),
            "pk-tv-09b": dict(self._second_item()),
        }
        keep = handler._resolve_funnel_duplicate_enqueue(
            row, self._second_item(), "pk-tv-09b", pending, _tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertFalse(keep)
        self.assertNotIn("pk-tv-09b", pending)
        self.assertIn("pk-tv-09", pending)
        self.assertEqual(self.repo.count_by_status().get("queued"), 1)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")
        self.assertEqual(self.repo.get_by_id(row["id"])["file_id"], "f-tv-09")

    def test_duplicate_same_pending_key_keeps_record(self):
        """同一 pending_key 重提交：记录就是该行自己的，保留。"""
        handler = self._batch_handler()
        first = _tv_item()
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        pending = {"pk-tv-09": dict(first)}
        keep = handler._resolve_funnel_duplicate_enqueue(
            row, first, "pk-tv-09", pending, _tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertTrue(keep)
        self.assertIn("pk-tv-09", pending)

    def test_wash_higher_score_replaces(self):
        """洗版高分候选替换已入队行：行换成第二份身份，第一份 pending
        记录弹出，第二份接替。"""
        handler = self._batch_handler()
        first = _tv_item(dedup_new_score=80)
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        second = self._second_item(
            upgrade=True,
            dedup_new_score=90,
            dedup_new_size=_TV_SIZE + 100,
            upgrade_old_file_id="f-old-09",
            upgrade_old_file_name=_TV_OLD_FILE,
        )
        pending = {
            "pk-tv-09": dict(first),
            "pk-tv-09b": dict(second),
        }
        keep = handler._resolve_funnel_duplicate_enqueue(
            row, second, "pk-tv-09b", pending, _tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertTrue(keep)
        self.assertNotIn("pk-tv-09", pending)
        self.assertIn("pk-tv-09b", pending)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["file_id"], "f-tv-09b")
        self.assertEqual(fetched["pending_key"], "pk-tv-09b")
        self.assertEqual(fetched["score"], 90)
        self.assertEqual(fetched["attempts"], 0)

    def test_wash_lower_score_skips(self):
        """洗版低分候选跳过：第二份弹出，第一份不动。"""
        handler = self._batch_handler()
        first = _tv_item(dedup_new_score=80)
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        second = self._second_item(
            upgrade=True, dedup_new_score=70,
            dedup_new_size=_TV_SIZE + 100,
        )
        pending = {
            "pk-tv-09": dict(first),
            "pk-tv-09b": dict(second),
        }
        keep = handler._resolve_funnel_duplicate_enqueue(
            row, second, "pk-tv-09b", pending, _tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertFalse(keep)
        self.assertNotIn("pk-tv-09b", pending)
        self.assertEqual(self.repo.get_by_id(row["id"])["file_id"], "f-tv-09")
        self.assertEqual(self.repo.get_by_id(row["id"])["score"], 80)

    def test_wash_equal_score_larger_wins(self):
        """留大：评分相等时体积大者赢；体积小者跳过。"""
        handler = self._batch_handler()
        first = _tv_item(dedup_new_score=80)
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        bigger = self._second_item(
            upgrade=True, dedup_new_score=80,
            dedup_new_size=_TV_SIZE + 100,
        )
        pending = {"pk-tv-09": dict(first), "pk-tv-09b": dict(bigger)}
        self.assertTrue(handler._resolve_funnel_duplicate_enqueue(
            row, bigger, "pk-tv-09b", pending, _tv_media(),
            subscribe_id=444, season=1,
        ))
        self.assertEqual(self.repo.get_by_id(row["id"])["file_id"], "f-tv-09b")
        smaller = self._second_item(
            upgrade=True, dedup_new_score=80,
            dedup_new_size=_TV_SIZE - 100,
            result_key="f-tv-09c",
        )
        pending = {"pk-tv-09b": dict(bigger), "pk-tv-09c": dict(smaller)}
        current = self.repo.get_by_id(row["id"])
        self.assertFalse(handler._resolve_funnel_duplicate_enqueue(
            current, smaller, "pk-tv-09c", pending, _tv_media(),
            subscribe_id=444, season=1,
        ))
        self.assertNotIn("pk-tv-09c", pending)
        self.assertEqual(self.repo.get_by_id(row["id"])["file_id"], "f-tv-09b")

    def test_wash_processing_row_no_replace(self):
        """行已 processing 时换不了：高分候选也按“已在途”跳过，
        行身份不动。"""
        handler = self._batch_handler()
        first = _tv_item(dedup_new_score=80)
        row, _ = self.service.enqueue_from_pending(
            first, "pk-tv-09", _tv_media()
        )
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now + 7200, now=now))
        second = self._second_item(
            upgrade=True, dedup_new_score=95,
            dedup_new_size=_TV_SIZE + 100,
        )
        pending = {
            "pk-tv-09": dict(first),
            "pk-tv-09b": dict(second),
        }
        keep = handler._resolve_funnel_duplicate_enqueue(
            row, second, "pk-tv-09b", pending, _tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertFalse(keep)
        self.assertNotIn("pk-tv-09b", pending)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "processing")
        self.assertEqual(fetched["file_id"], "f-tv-09")

    def test_batch_wiring_duplicate_second_has_terminal_state(self):
        """V2 门禁（批量入口端到端）：同一批里第二份同集入队冲突时，
        result 里只有第一份、持久化的 pending 里没有第二份、库里只有一行
        queued、没有失败记录。"""
        handler = SyncHandler.__new__(SyncHandler)
        object.__setattr__(handler, "funnel", self.service)
        batch_pending = {}
        batch_saved = {}
        object.__setattr__(handler, "_get_data", lambda key: batch_pending)
        object.__setattr__(handler, "_offline_pending_lock", threading.RLock())
        object.__setattr__(handler, "_serialize_mediainfo", lambda media: {})
        object.__setattr__(handler, "_organize_after_transfer", True)
        object.__setattr__(
            handler, "_cloud_drive", SimpleNamespace(key="115", name="115网盘")
        )
        object.__setattr__(handler, "resource_transfer", SimpleNamespace(
            _is_cloud_resource_url=lambda url: False,
            _is_direct_cloud_resource_url=lambda url: False,
        ))
        object.__setattr__(handler, "_OFFLINE_CHECK_DELAYS", [60, 120, 300, 600])
        object.__setattr__(handler, "_upgrade_mode", "largest")
        object.__setattr__(
            handler, "_save_offline_pending",
            lambda data: batch_saved.update(dict(data)),
        )
        object.__setattr__(
            handler, "count_offline_monitor_pending", lambda pending: len(pending)
        )
        object.__setattr__(
            handler, "_notify_offline_pending_changed", lambda count: None
        )
        first = _tv_item(
            share_url="https://example.invalid/share-1",
            funnel_route=True, dedup_new_score=80,
        )
        second = self._second_item(funnel_route=True, dedup_new_score=80)
        result = handler._queue_file_finalize_batch(
            items=[first, second], mediainfo=_tv_media(),
            subscribe_id=444, season=1,
        )
        self.assertEqual(set(result), {"f-tv-09"})
        self.assertEqual(set(batch_saved), {result["f-tv-09"]})
        self.assertEqual(self.repo.count_by_status().get("queued"), 1)
        self.assertEqual(self.repo.count_by_status().get("failed") or 0, 0)


# ----------------------------------------------------------------------
# 租约/退避/回收：fail-closed
# ----------------------------------------------------------------------
class FunnelHotfixLeaseTests(_DbMixin):
    """退避表、回收实查、pending 过期联动、item is None、mark_failed 守卫。"""

    def test_retry_delays_match_design(self):
        """退避表四档 (10, 60, 300, 900)：旧表 3600 档永远取不到
        （第 5 次失败先挂起），总退避 1270 秒约 21 分钟，与设计 §8 一致。"""
        self.assertEqual(tuple(FUNNEL_RETRY_DELAYS), (10, 60, 300, 900))
        self.assertEqual(sum(FUNNEL_RETRY_DELAYS), 1270)

    def test_fail_row_delay_sequence_then_held(self):
        """退避档位：第 1..4 次失败分别等 10/60/300/900 秒，第 5 次挂起。"""
        row = self._enqueue_tv()
        now = int(time.time())
        for index, delay in enumerate((10, 60, 300, 900)):
            current = self.repo.get_by_id(row["id"])
            outcome = self.service._fail_row(self.repo, current, "boom")
            self.assertEqual(outcome, "failed")
            fetched = self.repo.get_by_id(row["id"])
            self.assertEqual(fetched["attempts"], index + 1)
            self.assertAlmostEqual(
                fetched["next_retry_at"], now + delay, delta=5
            )
        current = self.repo.get_by_id(row["id"])
        outcome = self.service._fail_row(self.repo, current, "boom")
        self.assertEqual(outcome, "held")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def test_mark_failed_refuses_held_row(self):
        """I5：mark_failed 只对 queued/processing/failed 行生效，
        held 行拒绝翻回 failed。"""
        row = self._enqueue_tv()
        self.repo.mark_held(row["id"], "拦截")
        self.assertFalse(
            self.repo.mark_failed(row["id"], int(time.time()) + 60, "boom")
        )
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def test_fail_row_stale_snapshot_held_stays_held(self):
        """驱动层拿过期快照调 _fail_row：行已被拦截路置 held 时，
        以库里实际状态为准，不翻回 failed。"""
        row = self._enqueue_tv()
        stale = self.repo.get_by_id(row["id"])
        self.repo.mark_held(row["id"], "拦截")
        outcome = self.service._fail_row(self.repo, stale, "boom")
        self.assertEqual(outcome, "held")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def _expired_processing_row(self, pending_key="pk-tv-09", **item_extra):
        item = self._pending_item(pending_key, **item_extra)
        row, _ = self.service.enqueue_from_pending(
            item, pending_key, _tv_media()
        )
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now - 5, now=now))
        return row, item, now

    def test_reclaim_missing_pending_item_holds(self):
        """回收：待终态记录已不在 → held，不回收重跑。"""
        row, _item, now = self._expired_processing_row()
        self.owner._pending.pop("pk-tv-09", None)
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def test_reclaim_mp_organize_running_requeues(self):
        """回收：MP 整理仍在跑 → 重回队列（不断点续跑、不 held）。"""
        row, _item, now = self._expired_processing_row(
            mp_organize={"src_path": "/downloads/x.mkv", "src_storage": "local"},
        )
        self.owner.postprocess._mp_organize_tracker = lambda: FakeTracker(
            ("running", "", None)
        )
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 1, "held": 0})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")

    def test_reclaim_mp_organize_failed_requeues(self):
        """回收：MP 整理已失败 → 重回队列（可重试，不 held）。"""
        row, _item, now = self._expired_processing_row(
            mp_organize={"src_path": "/downloads/x.mkv", "src_storage": "local"},
        )
        self.owner.postprocess._mp_organize_tracker = lambda: FakeTracker(
            ("failed", "MP 整理失败", None)
        )
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 1, "held": 0})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")

    def test_reclaim_mp_organize_succeeded_holds(self):
        """回收：MP 整理已成功 → held（需人工确认，绝不重跑）。"""
        row, _item, now = self._expired_processing_row(
            mp_organize={"src_path": "/downloads/x.mkv", "src_storage": "local"},
        )
        self.owner.postprocess._mp_organize_tracker = lambda: FakeTracker(
            ("succeeded", "/媒体库/剧集/x.mkv", "/媒体库/剧集/x.mkv")
        )
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertIn("MP 整理已成功", fetched["last_error"])

    def test_reclaim_mp_organize_unavailable_holds(self):
        """回收：MP 整理状态查不到 → held（查不清不重跑）。"""
        row, _item, now = self._expired_processing_row(
            mp_organize={"src_path": "/downloads/x.mkv", "src_storage": "local"},
        )
        self.owner.postprocess._mp_organize_tracker = lambda: FakeTracker(
            ("unavailable", "MP 整理链不可用", None)
        )
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def test_reclaim_no_identity_landed_holds(self):
        """回收（无 MP 身份）：严格实查见文件已落盘 → held。"""
        row, _item, now = self._expired_processing_row()
        self.owner.episode_dedup = _FakeDedup(versions={
            9: [{"file_id": "f-tv-09", "file_name": _TV_FILE}],
        })
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")

    def test_reclaim_no_identity_clear_requeues(self):
        """回收（无 MP 身份）：严格实查确认未落盘 → 重回队列。"""
        row, _item, now = self._expired_processing_row()
        self.owner.episode_dedup = _FakeDedup(versions={})
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 1, "held": 0})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")

    def test_reclaim_no_identity_uncheckable_holds(self):
        """回收（无 MP 身份）：实查异常查不到 → held（fail-closed）。"""
        row, _item, now = self._expired_processing_row()
        self.owner.episode_dedup = _FakeDedup(versions={}, fail_collect=True)
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 0, "held": 1})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")


class FunnelHotfixPendingExpiryTests(unittest.TestCase):
    """pending 绝对过期联动 held；未过期不联动。"""

    def _expiry_service(self):
        service = DummyMpWaitService()
        service._owner._PENDING_ABSOLUTE_TIMEOUT_SECONDS = 6 * 60 * 60
        return service

    def test_pending_expiry_holds_funnel_row(self):
        """pending 超 6 小时未完成：abort 后对应漏斗行同步转 held。"""
        service = self._expiry_service()
        held_calls = []
        abort_calls = []
        service._owner.funnel_mark_held_by_pending_key = (
            lambda key, reason="": held_calls.append(key) or True
        )
        _stub(
            service, "_abort_single_item",
            lambda item, key, reason, ctx: abort_calls.append(key),
        )
        now = 2000000.0
        item = {
            "file_name": _TV_FILE,
            "first_seen_at": now - 6 * 60 * 60 - 10,
        }
        ctx = _ctx({"pk-tv-09": item}, now=now)
        self.assertTrue(
            service._expire_overdue_pending_item(item, "pk-tv-09", ctx)
        )
        self.assertEqual(abort_calls, ["pk-tv-09"])
        self.assertEqual(held_calls, ["pk-tv-09"])

    def test_pending_not_expired_no_hold(self):
        """pending 未超 6 小时：不 abort、不联动 held。"""
        service = self._expiry_service()
        held_calls = []
        abort_calls = []
        service._owner.funnel_mark_held_by_pending_key = (
            lambda key, reason="": held_calls.append(key) or True
        )
        _stub(
            service, "_abort_single_item",
            lambda item, key, reason, ctx: abort_calls.append(key),
        )
        now = 2000000.0
        item = {
            "file_name": _TV_FILE,
            "first_seen_at": now - 100 * 60,
        }
        ctx = _ctx({"pk-tv-09": item}, now=now)
        self.assertFalse(
            service._expire_overdue_pending_item(item, "pk-tv-09", ctx)
        )
        self.assertEqual(abort_calls, [])
        self.assertEqual(held_calls, [])


# ----------------------------------------------------------------------
# 洗版：三条件确认才删旧版 + 人工删旧版
# ----------------------------------------------------------------------
class _FakeMutations:
    def __init__(self, succeed=True):
        self._succeed = succeed
        self.deleted = []

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return self._succeed


class FunnelHotfixWashTests(_DbMixin):
    """洗版实查（旧版本除外）与删旧版门。"""

    def _wash_row_and_item(self):
        item = _tv_item(
            upgrade=True,
            upgrade_old_file_id="f-old-09",
            upgrade_old_file_name=_TV_OLD_FILE,
            upgrade_old_cloud_dir=_TV_CLOUD_DIR,
            dedup_new_score=90,
        )
        row, _ = self.service.enqueue_from_pending(
            item, "pk-tv-09", _tv_media()
        )
        pending = self._pending_item(
            "pk-tv-09",
            upgrade=True,
            upgrade_old_file_id="f-old-09",
            upgrade_old_file_name=_TV_OLD_FILE,
            upgrade_old_cloud_dir=_TV_CLOUD_DIR,
            dedup_new_score=90,
        )
        return row, item, pending

    def test_upgrade_other_new_versions_excludes_old(self):
        """旧版本除外：本项文件、主旧版（id/名）、额外旧版都不算
        “另一份新版”，只有真正的新版才算。"""
        row = {
            "file_id": "f-tv-09",
            "replaced_from": [
                {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
            ],
        }
        item = _tv_item(
            upgrade=True,
            upgrade_old_file_id="f-old-09",
            upgrade_old_file_name=_TV_OLD_FILE,
            dedup_extra_old_versions=[
                {"file_id": "f-old-2", "file_name": "old2.mkv"},
            ],
        )
        versions = [
            {"file_id": "f-tv-09", "file_name": _TV_FILE},
            {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
            {"file_id": "f-old-2", "file_name": "old2.mkv"},
            {"file_id": "f-old-09-x", "file_name": _TV_OLD_FILE},
            {"file_id": "f-new-2", "file_name": _TV_NEW2_FILE},
        ]
        others = self.service._upgrade_other_new_versions(versions, item, row)
        self.assertEqual([v["file_id"] for v in others], ["f-new-2"])

    def test_pre_organize_check_wash_intercepts_other_new(self):
        """洗版落盘前实查：旧版之外还有另一份新版时拦截。"""
        row, _item, pending = self._wash_row_and_item()
        self.owner.episode_dedup = _FakeDedup(versions={9: [
            {"file_id": "f-tv-09", "file_name": _TV_FILE},
            {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
            {"file_id": "f-new-2", "file_name": _TV_NEW2_FILE},
        ]})
        ok, _reason = self.service._pre_organize_check(
            row, pending, _tv_media()
        )
        self.assertFalse(ok)

    def test_pre_organize_check_wash_passes_with_old_only(self):
        """洗版落盘前实查：只有本项新文件与旧版时放行（不再直通跳过）。"""
        row, _item, pending = self._wash_row_and_item()
        self.owner.episode_dedup = _FakeDedup(versions={9: [
            {"file_id": "f-tv-09", "file_name": _TV_FILE},
            {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
        ]})
        ok, _reason = self.service._pre_organize_check(
            row, pending, _tv_media()
        )
        self.assertTrue(ok)

    def test_precommit_recheck_wash_intercepts_other_new(self):
        """洗版提交前复查：旧版之外还有另一份新版时拦截。"""
        row, _item, pending = self._wash_row_and_item()
        self.owner.episode_dedup = _FakeDedup(versions={9: [
            {"file_id": "f-tv-09", "file_name": _TV_FILE},
            {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
            {"file_id": "f-new-2", "file_name": _TV_NEW2_FILE},
        ]})
        intercepted, _reason = self.service.precommit_recheck(
            pending, "pk-tv-09", _tv_media()
        )
        self.assertTrue(intercepted)

    def test_precommit_recheck_wash_passes_with_old_only(self):
        """洗版提交前复查：只有本项新文件与旧版时放行。"""
        row, _item, pending = self._wash_row_and_item()
        self.owner.episode_dedup = _FakeDedup(versions={9: [
            {"file_id": "f-tv-09", "file_name": _TV_FILE},
            {"file_id": "f-old-09", "file_name": _TV_OLD_FILE},
        ]})
        intercepted, _reason = self.service.precommit_recheck(
            pending, "pk-tv-09", _tv_media()
        )
        self.assertFalse(intercepted)

    def test_delete_old_version_success_clears_replaced_from(self):
        """人工删旧版成功：旧文件删掉，replaced_from 清掉（按钮消失）。"""
        mutations = _FakeMutations()
        self.owner._cloud_mutations = mutations
        row, _item, _pending = self._wash_row_and_item()
        self.repo.mark_held(row["id"], "挂起")
        result = self.service.delete_old_version([row["id"]])
        self.assertTrue(result["success"])
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(mutations.deleted, ["f-old-09"])
        self.assertFalse(self.repo.get_by_id(row["id"])["replaced_from"])

    def test_delete_old_version_partial_failure_keeps_replaced_from(self):
        """人工删旧版部分失败：失败的留着，replaced_from 不清（可重试）。"""
        mutations = _FakeMutations(succeed=False)
        self.owner._cloud_mutations = mutations
        row, _item, _pending = self._wash_row_and_item()
        self.repo.mark_held(row["id"], "挂起")
        result = self.service.delete_old_version([row["id"]])
        self.assertTrue(result["success"])
        self.assertFalse(result["results"][0]["success"])
        fetched = self.repo.get_by_id(row["id"])
        self.assertTrue(fetched["replaced_from"])

    def test_delete_old_version_rejects_wrong_states(self):
        """人工删旧版只认 held/failed 行：queued 行拒绝；无旧版行拒绝。"""
        self.owner._cloud_mutations = _FakeMutations()
        row, _item, _pending = self._wash_row_and_item()
        result = self.service.delete_old_version([row["id"]])
        self.assertFalse(result["results"][0]["success"])
        self.assertEqual(self.owner._cloud_mutations.deleted, [])
        plain, _ = self.service.enqueue_from_pending(
            _tv_item(
                episode=10,
                file_name="多喜一家人 {tmdb-294498} - S01E10 - 第10集.mkv",
                staging_name="多喜一家人 - S01E10 - 第10集.mkv",
                result_key="f-tv-10",
                success_episodes=[10],
                notification_episodes=[10],
            ),
            "pk-tv-10", _tv_media(),
        )
        self.repo.mark_held(plain["id"], "挂起")
        result = self.service.delete_old_version([plain["id"]])
        self.assertFalse(result["results"][0]["success"])


class FunnelHotfixWashDeleteGateTests(unittest.TestCase):
    """删旧版前的复查门（_finalize_ready_item 与批量回收）。"""

    def _pp_service(self, intercept):
        service = DummyMpWaitService()
        service._owner.funnel = SimpleNamespace(
            precommit_recheck=lambda item, key, media, ctx: (
                intercept, "提交前复查发现重复，已拦截",
            ),
        )
        held = []
        service._owner.funnel_mark_held_by_pending_key = (
            lambda key, reason="": held.append(key) or True
        )
        return service, held

    def _wash_item(self):
        return {
            "upgrade": True,
            "upgrade_mode": "replace",
            "file_name": _TV_FILE,
            "upgrade_old_backed_up": True,
            "upgrade_old_file_id": "",
            "upgrade_backup_name": "",
        }

    def test_finalize_ready_item_gate_blocks_delete_and_commit(self):
        """三条件缺一：复查拦截时旧版不动、不提交、行转 held。"""
        service, held = self._pp_service(True)
        abort = []
        deleted = []
        committed = []
        _stub(
            service, "_abort_single_item",
            lambda item, key, reason, ctx, notify=True: abort.append(key),
        )
        _stub(
            service, "_delete_upgrade_old_file",
            lambda item, snap: deleted.append(item["file_name"]) or True,
        )
        _stub(
            service, "_commit_single_item",
            lambda item, key, *args, **kwargs: committed.append(key),
        )
        service._finalize_ready_item(
            self._wash_item(), "pk-x", None, _tv_media(), {}, _ctx({}),
        )
        self.assertEqual(deleted, [])
        self.assertEqual(committed, [])
        self.assertEqual(abort, ["pk-x"])
        self.assertEqual(held, ["pk-x"])

    def test_finalize_ready_item_gate_passes_then_deletes(self):
        """三条件齐全：复查通过后删旧版并提交。"""
        service, held = self._pp_service(False)
        deleted = []
        committed = []
        _stub(
            service, "_delete_upgrade_old_file",
            lambda item, snap: deleted.append(item["file_name"]) or True,
        )
        _stub(
            service, "_commit_single_item",
            lambda item, key, *args, **kwargs: committed.append(key),
        )
        service._finalize_ready_item(
            self._wash_item(), "pk-x", None, _tv_media(), {}, _ctx({}),
        )
        self.assertEqual(deleted, [_TV_FILE])
        self.assertEqual(committed, ["pk-x"])
        self.assertEqual(held, [])

    def test_flush_batch_gate_intercepts_one_keeps_other(self):
        """批量删旧版：拦截的项不删、不提交、行转 held；其余项照常。"""
        service, held = self._pp_service(False)
        service._owner.funnel = SimpleNamespace(
            precommit_recheck=lambda item, key, media, ctx: (
                (True, "提交前复查发现重复，已拦截")
                if key == "pk-bad" else (False, "")
            ),
        )
        abort = []
        _stub(
            service, "_abort_single_item",
            lambda item, key, reason, ctx, notify=True: abort.append(key),
        )
        deleted_ids = []

        class _Mutations:
            def delete_files(self, ids):
                deleted_ids.extend(ids)
                return set(ids)

        _stub(service, "_cloud_batch_mutations", _Mutations())
        committed = []
        _stub(
            service, "_commit_single_item",
            lambda item, key, *args, **kwargs: committed.append(key),
        )
        ctx = _ctx({})
        ctx.upgrade_delete_batch = {
            "pk-bad": {
                "item": {"file_name": "bad.mkv", "upgrade": True},
                "file_id": "f-bad", "strm_path": None,
                "media": None, "media_data": {},
            },
            "pk-ok": {
                "item": {"file_name": "ok.mkv", "upgrade": True},
                "file_id": "f-ok", "strm_path": None,
                "media": None, "media_data": {},
            },
        }
        service._flush_batch_postprocess(ctx)
        self.assertEqual(deleted_ids, ["f-ok"])
        self.assertEqual(committed, ["pk-ok"])
        self.assertEqual(abort, ["pk-bad"])
        self.assertEqual(held, ["pk-bad"])


# ----------------------------------------------------------------------
# V3 门禁：100 分钟长尾
# ----------------------------------------------------------------------
class FunnelHotfixV3Tests(_DbMixin):
    """V3 门禁：100 分钟长尾 tracker 下行不 held、订阅不结算、
    pending 不过期。"""

    def test_long_tail_running_not_held_no_settle_no_expire(self):
        now = int(time.time())
        long_ago = now - 100 * 60
        item = self._pending_item(
            "pk-tv-09",
            mp_organize={
                "src_path": "/downloads/x.mkv",
                "src_storage": "local",
                "triggered_at": long_ago,
            },
            first_seen_at=float(long_ago),
        )
        row, _ = self.service.enqueue_from_pending(
            item, "pk-tv-09", _tv_media(), subscribe_id=444,
        )
        self.assertTrue(self.repo.claim(row["id"], now - 5, now=now))
        self.owner.postprocess._mp_organize_tracker = lambda: FakeTracker(
            ("running", "", None)
        )
        stats = self.service._reclaim_lease_expired(now)
        self.assertEqual(stats, {"requeued": 1, "held": 0})
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")
        # 订阅不结算：行未终态，守卫挡住。
        self.assertTrue(self.service.has_unsettled_rows(444))
        # pending 不过期：100 分钟 < 6 小时绝对超时。
        pp = DummyMpWaitService()
        pp._owner._PENDING_ABSOLUTE_TIMEOUT_SECONDS = 6 * 60 * 60
        abort = []
        _stub(pp, "_abort_single_item", lambda *args, **kwargs: abort.append(1))
        ctx = _ctx({"pk-tv-09": item}, now=float(now))
        self.assertFalse(
            pp._expire_overdue_pending_item(item, "pk-tv-09", ctx)
        )
        self.assertEqual(abort, [])


# ----------------------------------------------------------------------
# API：删旧版端点与行视图
# ----------------------------------------------------------------------
class FunnelHotfixApiTests(unittest.TestCase):
    """人工删旧版 API：行视图带 has_old_version；端点返回逐行结果。"""

    def test_row_view_exposes_old_version(self):
        view = FunnelApi._row_view(_payload(replaced_from=[
            {"file_id": "f-old", "file_name": "old.mkv"},
        ]))
        self.assertTrue(view["has_old_version"])
        self.assertEqual(view["old_version_count"], 1)
        plain = FunnelApi._row_view(_payload())
        self.assertFalse(plain["has_old_version"])
        self.assertEqual(plain["old_version_count"], 0)

    def test_api_delete_old_version(self):
        fake_handler = SimpleNamespace(
            funnel_delete_old_version=lambda ids: {
                "success": True,
                "results": [{"id": 1, "success": True, "deleted": 1}],
            },
        )
        api = FunnelApi.__new__(FunnelApi)
        object.__setattr__(
            api, "_owner",
            SimpleNamespace(
                _require_sync_handler=lambda *names: (fake_handler, None),
            ),
        )
        result = api.api_vue_funnel_delete_old_version({"ids": [1]})
        self.assertTrue(result["success"])
        self.assertIn("1", result["message"])


if __name__ == "__main__":
    unittest.main()
