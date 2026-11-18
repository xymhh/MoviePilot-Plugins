"""1.9.97：搜索带 tmdb_id 贯通、提交信 tmdb_id、快照落盘。

回归背景：MP 搜索已定下媒体（空洞的十字架 326140），但下载提交
时把媒体身份丢了，又从分享标题重新推导——标题一脏（🗄）就报错。
本次三处改动：

1. 搜索时按关键词查一次 TMDB，唯一候选就把 tmdb_id 写进快照；
   提交时给了明确 tmdb_id 就直接按它取候选，跳过标题搜索、
   多媒体检测与分享预览（那些分支都在 selected_candidate is None
   时才跑）；
2. 订阅匹配按 tmdb_id 直查（第二处 _find_link_subscribe 本就支持
   tmdb_id，第一处标题匹配在可信路径上不再跑）；
   任务定位轮询也优先按 tmdb_id 认；
3. 快照落盘到插件数据目录（mp_search_snapshots.json，同样 1 小时
   过期）：插件重启/更新后已搜过的快照仍可下载，不用重新搜索。
"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from test_title_leading_emoji_1_9_96 import (  # noqa: E402
    agent_module,
    mp_search,
    platform_module,
)

MediaType = platform_module.MediaType


def _candidate(tmdb_id, media_type="tv", title="空洞的十字架"):
    return {
        "tmdb_id": tmdb_id,
        "media_type": media_type,
        "title": title,
        "original_title": "虚ろな十字架",
        "year": 2026,
    }


class _FakeSearchTmdb:
    """桩 api_vue_search_tmdb_candidates：按预设返回候选。"""

    def __init__(self, items=None, success=True, error=None):
        self.items = list(items or [])
        self.success = success
        self.error = error
        self.calls = []

    def __call__(self, payload):
        self.calls.append(dict(payload))
        if self.error is not None:
            raise self.error
        return {
            "success": self.success,
            "data": {"items": [dict(item) for item in self.items]},
        }


class ResolveSearchTmdbIdTest(unittest.TestCase):
    resolve = staticmethod(mp_search._resolve_search_tmdb_id)

    def test_single_candidate_returns_id_and_type(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb(
                [_candidate(326140)]
            )
        )
        self.assertEqual(
            self.resolve(plugin, "虚无的十字架", "tv"), (326140, "tv")
        )

    def test_single_movie_candidate_overrides_media_type(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb(
                [_candidate(123, "movie", "某电影")]
            )
        )
        self.assertEqual(
            self.resolve(plugin, "某电影", "tv"), (123, "movie")
        )

    def test_zero_candidates_returns_none(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb([])
        )
        self.assertIsNone(self.resolve(plugin, "虚无的十字架", "tv"))

    def test_multiple_candidates_returns_none(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb(
                [_candidate(1), _candidate(2)]
            )
        )
        self.assertIsNone(self.resolve(plugin, "虚无的十字架", "tv"))

    def test_failed_search_returns_none(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb(success=False)
        )
        self.assertIsNone(self.resolve(plugin, "虚无的十字架", "tv"))

    def test_missing_method_returns_none(self):
        self.assertIsNone(self.resolve(SimpleNamespace(), "虚无的十字架", "tv"))

    def test_raising_search_returns_none(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb(
                error=RuntimeError("tmdb down")
            )
        )
        self.assertIsNone(self.resolve(plugin, "虚无的十字架", "tv"))

    def test_invalid_tmdb_id_returns_none(self):
        plugin = SimpleNamespace(
            api_vue_search_tmdb_candidates=_FakeSearchTmdb([_candidate(0)])
        )
        self.assertIsNone(self.resolve(plugin, "虚无的十字架", "tv"))


class SnapshotPersistenceTest(unittest.TestCase):
    def _registry(self, tmpdir):
        return mp_search.SnapshotRegistry(
            file_path=os.path.join(tmpdir, "mp_search_snapshots.json")
        )

    def _snapshot(self, sid="abc123", tmdb_id=326140):
        return mp_search.ShareSnapshot(
            sid=sid,
            share_url="https://pan.quark.cn/s/abc",
            passcode="1234",
            resource_type="quark",
            title="虚无的十字架 虚ろな十字架 2026 1080p 日剧",
            media_type="tv",
            season=1,
            tmdb_id=tmdb_id,
        )

    def test_put_then_reload_from_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._registry(tmpdir)
            registry.put(self._snapshot())
            reloaded = self._registry(tmpdir)
            found = reloaded.get("abc123")
            self.assertIsNotNone(found)
            self.assertEqual(found.tmdb_id, 326140)
            self.assertEqual(found.title, "虚无的十字架 虚ろな十字架 2026 1080p 日剧")
            self.assertEqual(found.passcode, "1234")
            self.assertEqual(found.season, 1)

    def test_expired_entry_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            registry = self._registry(tmpdir)
            snapshot = self._snapshot()
            snapshot.created_at = time.time() - 4000
            registry._items[snapshot.sid] = snapshot
            registry._save_locked()
            reloaded = self._registry(tmpdir)
            self.assertIsNone(reloaded.get("abc123"))

    def test_corrupted_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "mp_search_snapshots.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            registry = self._registry(tmpdir)
            self.assertIsNone(registry.get("abc123"))

    def test_memory_only_without_file(self):
        registry = mp_search.SnapshotRegistry()
        registry.put(self._snapshot())
        self.assertIsNotNone(registry.get("abc123"))

    def test_to_from_dict_round_trip(self):
        snapshot = self._snapshot()
        restored = mp_search.ShareSnapshot.from_dict(snapshot.to_dict())
        self.assertEqual(restored.sid, snapshot.sid)
        self.assertEqual(restored.tmdb_id, snapshot.tmdb_id)
        self.assertEqual(restored.season, snapshot.season)
        self.assertEqual(restored.created_at, snapshot.created_at)

    def test_from_dict_missing_sid_raises(self):
        with self.assertRaises(ValueError):
            mp_search.ShareSnapshot.from_dict({"title": "x"})


class FakeMediaInfo:
    def __init__(self, tmdb_id, media_type, title="空洞的十字架", year=2026):
        self.tmdb_id = tmdb_id
        self.type = media_type
        self.title = title
        self.original_title = "虚ろな十字架"
        self.year = year


class _RecordingMetaInfo:
    """记录构造标题的 MetaInfo 替身：不依赖宿主 MetaInfo 是真是桩。"""

    instances = []

    def __init__(self, title, *args, **kwargs):
        self.title = title
        _RecordingMetaInfo.instances.append(self)


class CandidateForTmdbIdTest(unittest.TestCase):
    def setUp(self):
        self._original_recognize = agent_module.recognize_media
        self._original_metainfo = agent_module.MetaInfo
        self.calls = []

        def fake_recognize(chain, meta=None, mtype=None, tmdb_id=None,
                           cache=True, **kwargs):
            self.calls.append(
                {"chain": chain, "meta": meta, "mtype": mtype,
                 "tmdb_id": tmdb_id, "cache": cache}
            )
            return self._next_mediainfo

        agent_module.recognize_media = fake_recognize
        agent_module.MetaInfo = _RecordingMetaInfo
        _RecordingMetaInfo.instances.clear()
        self._next_mediainfo = FakeMediaInfo(326140, MediaType.TV)
        owner = SimpleNamespace(chain=SimpleNamespace(name="fake-chain"))
        self.service = platform_module.PlatformIntegrationService(owner)

    def tearDown(self):
        agent_module.recognize_media = self._original_recognize
        agent_module.MetaInfo = self._original_metainfo

    def test_returns_canonical_candidate(self):
        candidate = self.service._candidate_for_tmdb_id(
            326140, "tv", "🗄 虚无的十字架 虚ろな十字架 2026 1080p 日剧"
        )
        self.assertEqual(candidate["tmdb_id"], 326140)
        self.assertEqual(candidate["media_type"], "tv")
        self.assertEqual(candidate["title"], "空洞的十字架")
        self.assertEqual(candidate["year"], 2026)
        # 识别用的标题已去掉开头 emoji
        self.assertEqual(len(_RecordingMetaInfo.instances), 1)
        self.assertEqual(
            _RecordingMetaInfo.instances[0].title,
            "虚无的十字架 虚ろな十字架 2026 1080p 日剧",
        )
        self.assertEqual(self.calls[0]["tmdb_id"], 326140)
        self.assertIs(self.calls[0]["mtype"], MediaType.TV)

    def test_movie_type_passed_through(self):
        self._next_mediainfo = FakeMediaInfo(123, MediaType.MOVIE, "某电影")
        candidate = self.service._candidate_for_tmdb_id(123, "movie", "某电影")
        self.assertEqual(candidate["media_type"], "movie")
        self.assertIs(self.calls[0]["mtype"], MediaType.MOVIE)

    def test_mismatched_tmdb_id_returns_none(self):
        self._next_mediainfo = FakeMediaInfo(999, MediaType.TV)
        self.assertIsNone(
            self.service._candidate_for_tmdb_id(326140, "tv", "虚无的十字架")
        )

    def test_no_mediainfo_returns_none(self):
        self._next_mediainfo = None
        self.assertIsNone(
            self.service._candidate_for_tmdb_id(326140, "tv", "虚无的十字架")
        )

    def test_recognize_error_returns_none(self):
        def raising_recognize(*args, **kwargs):
            raise RuntimeError("chain down")

        agent_module.recognize_media = raising_recognize
        self.assertIsNone(
            self.service._candidate_for_tmdb_id(326140, "tv", "虚无的十字架")
        )

    def test_invalid_tmdb_id_returns_none(self):
        for bad in (0, None, "x"):
            self.assertIsNone(
                self.service._candidate_for_tmdb_id(bad, "tv", "虚无的十字架")
            )
        self.assertEqual(self.calls, [])


class TaskMatchesSnapshotTest(unittest.TestCase):
    def _snapshot(self, tmdb_id=326140, season=1, title="虚无的十字架"):
        return mp_search.ShareSnapshot(
            sid="abc",
            share_url="https://pan.quark.cn/s/abc",
            title=title,
            media_type="tv",
            season=season,
            tmdb_id=tmdb_id,
        )

    def test_tmdb_match_even_with_different_title(self):
        snapshot = self._snapshot()
        task = {"id": "t1", "title": "空洞的十字架", "season": 1,
                "tmdb_id": 326140}
        self.assertTrue(
            mp_search._task_matches_snapshot(task, snapshot, "虚无的十字架")
        )

    def test_tmdb_mismatch_rejected(self):
        snapshot = self._snapshot()
        task = {"id": "t1", "title": "虚无的十字架", "season": 1,
                "tmdb_id": 999}
        self.assertFalse(
            mp_search._task_matches_snapshot(task, snapshot, "虚无的十字架")
        )

    def test_tmdb_match_wrong_season_rejected(self):
        snapshot = self._snapshot()
        task = {"id": "t1", "title": "空洞的十字架", "season": 2,
                "tmdb_id": 326140}
        self.assertFalse(
            mp_search._task_matches_snapshot(task, snapshot, "虚无的十字架")
        )

    def test_task_without_tmdb_falls_back_to_title(self):
        snapshot = self._snapshot()
        task = {"id": "t1", "title": "虚无的十字架 S01", "season": 1}
        self.assertTrue(
            mp_search._task_matches_snapshot(task, snapshot, "虚无的十字架")
        )

    def test_no_tmdb_snapshot_uses_title(self):
        snapshot = self._snapshot(tmdb_id=None)
        task = {"id": "t1", "title": "虚无的十字架 S01", "season": 1,
                "tmdb_id": 326140}
        self.assertTrue(
            mp_search._task_matches_snapshot(task, snapshot, "虚无的十字架")
        )


if __name__ == "__main__":
    unittest.main()
