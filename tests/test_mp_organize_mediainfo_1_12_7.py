"""v1.12.7：trigger_mp_organize 只在 MediaInfo 自带分类时才传给 MP。

v1.12.6 的回归：trigger 无条件把拼好的最小 MediaInfo 传给
do_transfer，但最小 MediaInfo 没有 category；而 MP 的剧集目录条目
开了 library_category_folder（媒体库下有 国漫/、国产剧/ 子目录）
且无固定分类时，MP 的分类校验要求 mediainfo 必须带 category，
否则在动文件前就拒绝（“TMDB 信息未匹配到媒体分类”）。S01E200
与 S01E232 在 2026-11-16 17:07 先后因此失败。

修法：只在 MediaInfo 自带非空 category 时才传；没有时不传，让
MP 按文件名识别（文件名带 {tmdb-<id>} 标记）并自己分类。目标
目录/存储的显式传参不变，/转存/ 落点修复不受影响。

桩类复用 test_mp_organize_1_11_0 的同一套（FakeFileItem /
FakeStorageChain / FakeTransferChain / FakeMediaInfo /
FakeDirectoryHelper），理由同 test_mp_organize_target_dir_1_12_6。
"""

import os
import sys
import unittest

from plugin_env import load_module, set_media_type

try:
    from test_mp_organize_1_11_0 import (
        FakeDirectoryHelper,
        FakeMediaInfo,
        FakeStorageChain,
        FakeTransferChain,
        _install_stubs,
        _owner,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_mp_organize_1_11_0 import (
        FakeDirectoryHelper,
        FakeMediaInfo,
        FakeStorageChain,
        FakeTransferChain,
        _install_stubs,
        _owner,
    )

mp_organize = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize.MpOrganizeMixin


class TestTriggerMpOrganizeMediainfoCategory(unittest.TestCase):
    def setUp(self):
        # MediaType 桩必须在每个用例执行前重注：收集阶段后加载的用例文件
        # 会覆盖 app.schemas.types 上的桩，模块级那次到执行时已失效。
        global MediaType
        MediaType = set_media_type()
        _install_stubs()
        FakeStorageChain.next_result = "found"
        FakeStorageChain.last_fileitem = None
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        FakeDirectoryHelper.calls = []
        FakeDirectoryHelper.next_result = "found"
        FakeDirectoryHelper.last_conf = None
        self.mixin = MpOrganizeMixin(_owner())

    def test_media_dict_without_category_omits_mediainfo(self):
        """回归：media 字典没有 category 时不传 MediaInfo 给 MP。"""
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
            media={
                "media_title": "凡人修仙传",
                "media_type": "TV",
                "tmdb_id": 106449,
                "season": 1,
            },
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertNotIn("mediainfo", kwargs)
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_media_dict_with_category_passes_built_mediainfo(self):
        """media 字典带 category 时，拼好的 MediaInfo 照常传给 MP。"""
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
            media={
                "media_title": "凡人修仙传",
                "media_type": "TV",
                "tmdb_id": 106449,
                "season": 1,
                "category": "国漫",
            },
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertIsInstance(kwargs["mediainfo"], FakeMediaInfo)
        self.assertEqual(kwargs["mediainfo"].type, MediaType.TV)
        self.assertEqual(kwargs["mediainfo"].title, "凡人修仙传")
        self.assertEqual(kwargs["mediainfo"].tmdb_id, 106449)
        self.assertEqual(kwargs["mediainfo"].category, "国漫")
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_caller_mediainfo_with_category_is_passed_through(self):
        """调用方传的 MediaInfo 自带分类时直传给 MP。"""
        mediainfo = FakeMediaInfo(
            type=MediaType.TV,
            title="凡人修仙传",
            tmdb_id=106449,
            season=1,
            category="国漫",
        )
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
            mediainfo=mediainfo,
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertIs(kwargs["mediainfo"], mediainfo)
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_caller_mediainfo_without_category_is_omitted(self):
        """调用方传的 MediaInfo 没有分类时不传，让 MP 自己识别分类。"""
        mediainfo = FakeMediaInfo(
            type=MediaType.TV,
            title="凡人修仙传",
            tmdb_id=106449,
            season=1,
        )
        self.assertEqual(mediainfo.category, "")
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
            mediainfo=mediainfo,
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertNotIn("mediainfo", kwargs)
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_batch_path_passes_mediainfo_with_category(self):
        """P1 批量路径：MediaInfo 带分类时同样直传给 MP。"""
        mediainfo = FakeMediaInfo(
            type=MediaType.TV,
            title="凡人修仙传",
            tmdb_id=106449,
            season=1,
            category="国漫",
        )
        items = [
            {"file": {"id": "ep1"}, "target_name": "a - S01E01.mkv"},
            {"file": {"id": "ep2"}, "target_name": "b - S01E02.mkv"},
        ]
        accepted = self.mixin._trigger_mp_organize_for_batch(
            items, {"ep1", "ep2"},
            mediainfo=mediainfo, season=1,
        )
        self.assertEqual(accepted, {"ep1", "ep2"})
        self.assertEqual(len(FakeTransferChain.calls), 2)
        for _fileitem, kwargs in FakeTransferChain.calls:
            self.assertIs(kwargs["mediainfo"], mediainfo)
            self.assertEqual(
                kwargs["target_directory"].library_path, "/媒体库/剧集"
            )
            self.assertEqual(kwargs["target_storage"], "u115cs")


if __name__ == "__main__":
    unittest.main()
