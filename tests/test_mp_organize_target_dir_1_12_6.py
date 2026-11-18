"""v1.12.6 A：trigger_mp_organize 显式传目标目录给 MP 整理。

不再只把文件交给 do_transfer 让 MP 按源路径猜目录：先用不带
src_path 的 DirectoryHelper.get_dir 按媒体类型解析目录条目，再把
该条目作为 target_directory / target_storage 传给 do_transfer，
否则本地 /downloads 的文件会被匹配到“夸克追更”条目、整理进 /转存/。

桩类复用 test_mp_organize_1_11_0 的同一套（FakeFileItem /
FakeStorageChain / FakeTransferChain / FakeMediaInfo /
FakeDirectoryHelper）：全量收集时各文件模块级 register_module 会
互相覆盖，另起一套桩会让旧文件的 FakeTransferChain.calls 断言
读到另一套桩的空列表，所以这里不另起桩，只复用。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

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


class TestTriggerMpOrganizeTargetDirectory(unittest.TestCase):
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
        self.mediainfo = MagicMock()
        self.mediainfo.type = MediaType.TV
        # 调用方传的 MediaInfo 默认带分类（真实 MP 识别结果有分类），
        # v1.12.7 的 trigger 会把它直传给 do_transfer。
        self.mediainfo.category = "国漫"
        self.mixin = MpOrganizeMixin(_owner())

    def test_media_only_get_dir_hits_tv_entry(self):
        """media-only 时 get_dir 只传 media，不带 src_path/storage。"""
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
        self.assertEqual(len(FakeDirectoryHelper.calls), 1)
        call = FakeDirectoryHelper.calls[0]
        self.assertEqual(call["kwargs"], {})
        self.assertEqual(call["include_unsorted"], False)
        self.assertIsInstance(call["media"], FakeMediaInfo)
        self.assertEqual(call["media"].type, MediaType.TV)
        self.assertEqual(call["media"].title, "凡人修仙传")
        self.assertEqual(call["media"].tmdb_id, 106449)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertIs(kwargs["target_directory"], FakeDirectoryHelper.last_conf)

    def test_do_transfer_receives_target_directory_and_storage(self):
        """do_transfer 收到 target_directory=剧集 conf 与 target_storage。"""
        ok, info = self.mixin.trigger_mp_organize(
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
        self.assertEqual(info["target_dir"], "/媒体库/剧集")
        self.assertEqual(info["target_storage"], "u115cs")
        self.assertEqual(info["target_name"], "剧集")
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertIs(kwargs["target_directory"], FakeDirectoryHelper.last_conf)
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")
        self.assertEqual(kwargs["target_storage"], "u115cs")

    def test_do_transfer_omits_built_mediainfo_without_category(self):
        """media 字典没有 category 时，不把拼好的最小 MediaInfo 传给 MP。

        v1.12.7：最小 MediaInfo 没有 category，传了会被 MP 的分类校验
        拒绝（“TMDB 信息未匹配到媒体分类”）；不传时 MP 按文件名识别
        并自己分类。目标目录/存储照常传。
        """
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "吞噬星空 {tmdb-101172} - S01E232 - 第232集.mkv",
            media={
                "media_title": "吞噬星空",
                "media_type": "电视剧",
                "tmdb_id": 101172,
                "season": 1,
            },
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertNotIn("mediainfo", kwargs)
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_media_dict_without_type_still_resolves(self):
        """mediainfo 为空但 media dict 有 TV 时仍解析到剧集。"""
        ok, _info = self.mixin.trigger_mp_organize(
            "/downloads",
            "吞噬星空 - S01E234 - 第234集.mkv",
            media={"title": "吞噬星空", "media_type": "电视剧集", "season": 1},
        )
        self.assertTrue(ok)
        self.assertEqual(len(FakeDirectoryHelper.calls), 1)
        self.assertEqual(
            FakeDirectoryHelper.calls[0]["media"].type, MediaType.TV
        )
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertEqual(kwargs["target_storage"], "u115cs")
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")

    def test_get_dir_none_returns_no_target_dir_without_do_transfer(self):
        """get_dir 返回 None 时不调 do_transfer，reason 为 no_target_dir。"""
        FakeDirectoryHelper.next_result = "missing"
        ok, info = self.mixin.trigger_mp_organize(
            "/downloads",
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
            mediainfo=self.mediainfo,
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_target_dir")
        self.assertEqual(FakeTransferChain.calls, [])

    def test_no_type_in_media_returns_no_target_dir(self):
        """mediainfo 为空且 media dict 拼不出 type 时不触发。"""
        ok, info = self.mixin.trigger_mp_organize(
            "/downloads",
            "未知 - S01E01.mkv",
            media={"title": "未知"},
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_target_dir")
        self.assertEqual(FakeDirectoryHelper.calls, [])
        self.assertEqual(FakeTransferChain.calls, [])

    def test_batch_path_passes_target_directory(self):
        """P1 批量路径同样给 do_transfer 传 target_directory。"""
        items = [
            {"file": {"id": "ep1"}, "target_name": "a - S01E01.mkv"},
            {"file": {"id": "ep2"}, "target_name": "b - S01E02.mkv"},
            {"file": {"id": "ep3"}, "target_name": "c - S01E03.mkv"},
        ]
        accepted = self.mixin._trigger_mp_organize_for_batch(
            items, {"ep1", "ep2"},
            mediainfo=self.mediainfo, season=1,
        )
        self.assertEqual(accepted, {"ep1", "ep2"})
        self.assertEqual(len(FakeTransferChain.calls), 2)
        for _fileitem, kwargs in FakeTransferChain.calls:
            # 每次 get_dir 返回一个新的 conf 对象，只按字段断言
            self.assertEqual(
                kwargs["target_directory"].library_path, "/媒体库/剧集"
            )
            self.assertEqual(kwargs["target_storage"], "u115cs")
