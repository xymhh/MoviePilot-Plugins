"""v1.31.4 同视频组按规范名保留（防 STRM 全量校准再生）。

背景：v1.31.3 的批量删除对同视频组只删多余 .strm 指针；STRM 全量校准
按视频当前文件名补 .strm，删掉的那份若名字还对得上，下次校准就重建
回来。规范名 = 视频当前在网盘里的名字，即核对时按 video_name 查到、
video_size 为 int 的那份。旧 _suggest_keep 对同视频组直接 return 0
（默认带标记的第一份），带标记不等于规范名。

覆盖：
- 建议规则：能查到的没标记那份被建议；多份都能查到时建议带标记那份；
  一份都查不到时回落 items[0]；不同视频组留大不受影响；
- 删除纠正：keep 指向查不到的那份时纠正到能查到的那份（adjusted_keep
  标明 from/to），删旧名 .strm + .nfo、留现名 .strm + .nfo、视频不动；
- 不纠正：keep 本来就指向能查到的那份；一份都查不到；不同视频组；
- 批量：需纠正组 adjusted_keep 为 true、正常组为 false；
- 91B 无 file_name 的 .iso.strm 情形。

harness：复用 test_strm_duplicates_1_31_0 的假云盘/假变更/服务构造
（线上布局：_local_resource_path 就是 .strm 根、_CLOUD_MEDIA_ROOT
另设）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_strm_duplicates_1_31_0 import (  # noqa: E402
    REDIRECT,
    _FakeCloudFile,
    _FakeCloudQuery,
    _FakeMutations,
    _MetaInfoTestCase,
    _duplicates_service,
    _redirect_content,
    _write_strm,
)


class CanonicalKeepTests(_MetaInfoTestCase):
    """夹具：E06/E08 同视频（旧名/现名各一份），E07 不同视频。"""

    MEDIA_ROOT = "/Alist/影视/moviepilot/媒体库"

    def setUp(self):
        super().setUp()
        self.strm_root = self.root / "115网盘"
        self.season_dir = (
            self.strm_root / "剧集" / "国产剧" / "两京十五日" / "Season 1"
        )
        # E06 同视频：同一 pickcode。带标记那份内容是旧名（云盘里没有），
        # 没标记那份内容是当前名（云盘里有）——规范名是没标记那份。
        self.e06_tagged = _write_strm(
            self.season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content(
                "pc-e06",
                "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
            ),
        )
        self.e06_plain = _write_strm(
            self.season_dir / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content("pc-e06", "两京十五日 - S01E06 - 第6集.mkv"),
        )
        for stem in (
            "两京十五日 {tmdb-203483} - S01E06 - 第6集",
            "两京十五日 - S01E06 - 第6集",
        ):
            (self.season_dir / f"{stem}.nfo").write_text("<nfo/>", encoding="utf-8")
        # E07 不同视频：带标记 1GB、没标记 2GB，留大应保留没标记那份。
        self.e07_tagged = _write_strm(
            self.season_dir / "两京十五日 {tmdb-203483} - S01E07 - 第7集.strm",
            _redirect_content(
                "pc-e07-tagged",
                "两京十五日 {tmdb-203483} - S01E07 - 第7集.mkv",
            ),
        )
        self.e07_plain = _write_strm(
            self.season_dir / "两京十五日 - S01E07 - 第7集.strm",
            _redirect_content("pc-e07-plain", "两京十五日 - S01E07 - 第7集.mkv"),
        )
        # E08 同视频：91B 无 file_name 的 .iso.strm + 正常 .strm。
        # .iso.strm 的 .nfo 与正常那份同主干、是同一个文件（既有行为，
        # 这里只断言 .strm 状态，不对 E08 的 .nfo 做断言）。
        self.e08_iso = _write_strm(
            self.season_dir / "两京十五日 {tmdb-203483} - S01E08 - 第8集.iso.strm",
            f"{REDIRECT}?pickcode=pc-e08",
        )
        self.e08_plain = _write_strm(
            self.season_dir / "两京十五日 - S01E08 - 第8集.strm",
            _redirect_content("pc-e08", "两京十五日 - S01E08 - 第8集.mkv"),
        )
        (self.season_dir / "两京十五日 - S01E08 - 第8集.nfo").write_text(
            "<nfo/>", encoding="utf-8",
        )
        self.cloud_dir = self.MEDIA_ROOT + "/剧集/国产剧/两京十五日/Season 1"
        # fake 云盘里只有当前名（没标记那份）。
        self.query = _FakeCloudQuery({
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06", "x", 1_950_000_000, "pc-e06"),
            (self.cloud_dir, "两京十五日 - S01E08 - 第8集.mkv"):
                _FakeCloudFile("fid-e08", "x", 1_950_000_000, "pc-e08"),
        })
        # 旧名也在云盘里（多份都能查到的情形）+ E07 两份都在。
        self.query_both = _FakeCloudQuery({
            (self.cloud_dir, "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06-old", "x", 1_950_000_000, "pc-e06"),
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06", "x", 1_950_000_000, "pc-e06"),
            (self.cloud_dir, "两京十五日 {tmdb-203483} - S01E07 - 第7集.mkv"):
                _FakeCloudFile("fid-e07-tagged", "x", 1_000_000_000, "pc-e07-tagged"),
            (self.cloud_dir, "两京十五日 - S01E07 - 第7集.mkv"):
                _FakeCloudFile("fid-e07-plain", "x", 2_000_000_000, "pc-e07-plain"),
            (self.cloud_dir, "两京十五日 - S01E08 - 第8集.mkv"):
                _FakeCloudFile("fid-e08", "x", 1_950_000_000, "pc-e08"),
        })
        # 批量用：E06 只有现名在云盘（需纠正），E07 两份都在（正常删）。
        self.query_batch = _FakeCloudQuery({
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06", "x", 1_950_000_000, "pc-e06"),
            (self.cloud_dir, "两京十五日 {tmdb-203483} - S01E07 - 第7集.mkv"):
                _FakeCloudFile("fid-e07-tagged", "x", 1_000_000_000, "pc-e07-tagged"),
            (self.cloud_dir, "两京十五日 - S01E07 - 第7集.mkv"):
                _FakeCloudFile("fid-e07-plain", "x", 2_000_000_000, "pc-e07-plain"),
            (self.cloud_dir, "两京十五日 - S01E08 - 第8集.mkv"):
                _FakeCloudFile("fid-e08", "x", 1_950_000_000, "pc-e08"),
        })
        # E07 旧名情形：带标记那份是旧名（云盘里没有），没标记那份是现名。
        self.query_e07_stale = _FakeCloudQuery({
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06", "x", 1_950_000_000, "pc-e06"),
            (self.cloud_dir, "两京十五日 - S01E07 - 第7集.mkv"):
                _FakeCloudFile("fid-e07-plain", "x", 2_000_000_000, "pc-e07-plain"),
        })

    def _service(self, query=None, mutations=None):
        return _duplicates_service(
            self.strm_root, query=query, mutations=mutations,
            media_root=self.MEDIA_ROOT,
        )

    def _groups(self, query=None):
        groups = self._service().duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 3)
        enriched = self._service(query=query).duplicates_enrich(groups)
        self.assertTrue(enriched["success"], enriched)
        return enriched["data"]["groups"]

    @staticmethod
    def _group_for(groups, episode):
        for group in groups:
            if any(
                f"E{episode:02d}" in str(item.get("local_path") or "")
                for item in group["items"]
            ):
                return group
        raise AssertionError(f"no group for E{episode:02d}")


class CanonicalKeepSuggestTests(CanonicalKeepTests):
    """任务 1：建议规则——同视频组按规范名（能查到大小的那份）。"""

    def test_suggest_keep_prefers_resolvable_untagged(self):
        """能查到的没标记那份被建议（带标记那份是旧名）。"""
        groups = self._groups(query=self.query)
        e06 = self._group_for(groups, 6)
        self.assertEqual(e06["kind"], "same-video")
        self.assertIsNone(e06["items"][0]["video_size"])
        self.assertEqual(e06["items"][1]["video_size"], 1_950_000_000)
        self.assertEqual(e06["suggested_keep"], 1)

    def test_suggest_keep_prefers_tagged_when_both_resolvable(self):
        """多份都能查到时建议带标记的那份。"""
        groups = self._groups(query=self.query_both)
        e06 = self._group_for(groups, 6)
        self.assertEqual(e06["items"][0]["video_size"], 1_950_000_000)
        self.assertEqual(e06["items"][1]["video_size"], 1_950_000_000)
        self.assertEqual(e06["suggested_keep"], 0)

    def test_suggest_keep_falls_back_to_first_when_none_resolvable(self):
        """一份都查不到时回落 items[0]（旧行为）。"""
        groups = self._groups(query=_FakeCloudQuery({}))
        e06 = self._group_for(groups, 6)
        self.assertIsNone(e06["items"][0]["video_size"])
        self.assertIsNone(e06["items"][1]["video_size"])
        self.assertEqual(e06["suggested_keep"], 0)

    def test_suggest_keep_different_video_largest_unchanged(self):
        """不同视频组留大不受影响：保留最大的那份。"""
        groups = self._groups(query=self.query_both)
        e07 = self._group_for(groups, 7)
        self.assertEqual(e07["kind"], "different-video")
        self.assertEqual(e07["items"][0]["video_size"], 1_000_000_000)
        self.assertEqual(e07["items"][1]["video_size"], 2_000_000_000)
        self.assertEqual(e07["suggested_keep"], 1)

    def test_suggest_keep_iso_strm_partner(self):
        """91B 无 file_name 的 .iso.strm：建议能查到的那份。"""
        groups = self._groups(query=self.query)
        e08 = self._group_for(groups, 8)
        self.assertEqual(e08["kind"], "same-video")
        self.assertIsNone(e08["items"][0]["video_size"])
        self.assertEqual(e08["items"][1]["video_size"], 1_950_000_000)
        self.assertEqual(e08["suggested_keep"], 1)


class CanonicalKeepDeleteTests(CanonicalKeepTests):
    """任务 2：删除纠正 / 不纠正。"""

    def test_delete_corrects_stale_keep(self):
        """keep 指向查不到的那份 → 纠正到能查到的那份。"""
        groups = self._groups(query=self.query)
        e06 = self._group_for(groups, 6)
        mutations = _FakeMutations()
        service = self._service(query=self.query, mutations=mutations)
        result = service.duplicates_delete(e06, 0)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["adjusted_keep"], {"from": 0, "to": 1})
        self.assertIn("调整", result["message"])
        self.assertIn("第 2 份", result["message"])
        self.assertIn("第 1 份", result["message"])
        # 旧名 .strm + .nfo 没了，现名 .strm + .nfo 还在，视频不动。
        self.assertFalse(self.e06_tagged.exists())
        self.assertTrue(self.e06_plain.exists())
        self.assertFalse(
            (self.season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.nfo").exists()
        )
        self.assertTrue(
            (self.season_dir / "两京十五日 - S01E06 - 第6集.nfo").exists()
        )
        self.assertEqual(mutations.deleted, [])

    def test_delete_no_correction_when_keep_resolves(self):
        """keep 本来就指向能查到的那份——无标记，照传入的删。"""
        groups = self._groups(query=self.query)
        e06 = self._group_for(groups, 6)
        mutations = _FakeMutations()
        service = self._service(query=self.query, mutations=mutations)
        result = service.duplicates_delete(e06, 1)
        self.assertTrue(result["success"], result)
        self.assertNotIn("adjusted_keep", result["data"])
        self.assertNotIn("调整", result["message"])
        self.assertFalse(self.e06_tagged.exists())
        self.assertTrue(self.e06_plain.exists())
        self.assertEqual(mutations.deleted, [])

    def test_delete_no_correction_when_nothing_resolves(self):
        """一份都查不到——按传入的 keep 走，无标记。"""
        groups = self._groups(query=_FakeCloudQuery({}))
        e06 = self._group_for(groups, 6)
        mutations = _FakeMutations()
        service = self._service(query=_FakeCloudQuery({}), mutations=mutations)
        result = service.duplicates_delete(e06, 0)
        self.assertTrue(result["success"], result)
        self.assertNotIn("adjusted_keep", result["data"])
        self.assertTrue(self.e06_tagged.exists())
        self.assertFalse(self.e06_plain.exists())
        self.assertEqual(mutations.deleted, [])

    def test_delete_different_video_never_corrects(self):
        """不同视频组：keep 指向查不到的那份也不纠正，视频照删。"""
        groups = self._groups(query=self.query_e07_stale)
        e07 = self._group_for(groups, 7)
        self.assertEqual(e07["kind"], "different-video")
        mutations = _FakeMutations()
        service = self._service(query=self.query_e07_stale, mutations=mutations)
        result = service.duplicates_delete(e07, 0)
        self.assertTrue(result["success"], result)
        self.assertNotIn("adjusted_keep", result["data"])
        # 保留带标记（旧名）那份，没标记那份的视频照删。
        self.assertTrue(self.e07_tagged.exists())
        self.assertFalse(self.e07_plain.exists())
        self.assertEqual(mutations.deleted, ["fid-e07-plain"])

    def test_delete_corrects_iso_strm_keep(self):
        """91B .iso.strm（无 file_name）被纠正掉，留能查到的那份。"""
        groups = self._groups(query=self.query)
        e08 = self._group_for(groups, 8)
        self.assertEqual(e08["suggested_keep"], 1)
        mutations = _FakeMutations()
        service = self._service(query=self.query, mutations=mutations)
        result = service.duplicates_delete(e08, 0)
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["adjusted_keep"], {"from": 0, "to": 1})
        self.assertFalse(self.e08_iso.exists())
        self.assertTrue(self.e08_plain.exists())
        self.assertEqual(mutations.deleted, [])


class CanonicalKeepBatchTests(CanonicalKeepTests):
    """任务 3：批量透传 adjusted_keep。"""

    def test_batch_mixed_adjusted_and_normal(self):
        """需纠正的同视频组为 true，正常组为 false；文件状态正确。"""
        groups = self._groups(query=self.query_batch)
        e06 = self._group_for(groups, 6)
        e07 = self._group_for(groups, 7)
        mutations = _FakeMutations()
        service = self._service(query=self.query_batch, mutations=mutations)
        result = service.duplicates_delete_batch([
            {"group": e06, "keep_index": 0},
            {"group": e07, "keep_index": 1},
        ])
        self.assertTrue(result["success"], result)
        self.assertEqual(result["data"]["deleted"], 2)
        by_key = {item["key"]: item for item in result["data"]["results"]}
        self.assertTrue(by_key[e06["key"]]["adjusted_keep"])
        self.assertFalse(by_key[e07["key"]]["adjusted_keep"])
        # E06：旧名删、现名留、视频不动；E07：带标记视频删。
        self.assertFalse(self.e06_tagged.exists())
        self.assertTrue(self.e06_plain.exists())
        self.assertFalse(self.e07_tagged.exists())
        self.assertTrue(self.e07_plain.exists())
        self.assertEqual(mutations.deleted, ["fid-e07-tagged"])


if __name__ == "__main__":
    unittest.main()
