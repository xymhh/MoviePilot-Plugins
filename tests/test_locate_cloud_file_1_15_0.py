"""1.15.0 Phase 3a 第 5 项：“文件在哪”唯一入口。

_locate_cloud_file 是唯一实现：dest 策略（MP 历史精确路径，只精确
查、不进模糊兜底）+ 单层 1–4 策略（精确名/哈希/前后缀容错/季集
索引）+ 递归树策略（recursive=True 时对整棵树复用同一套 1–4
策略）。_locate_cloud_file_by_dest 只是 dest 策略的薄包装；
magnet finalize 的两处 list_files_recursive 改走同一树原语
_locate_cloud_files_in_tree。
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_finalize_mp_rename_sync_1_15_0 import (  # noqa: E402
    DirectoryFileIndex,
    DummyMpWaitService,
    _ctx,
)

from plugin_env import set_media_type  # noqa: E402


def _file(name, file_id="f1", sha1=""):
    return SimpleNamespace(
        name=name, id=file_id, size=1000, sha1=sha1, md5="",
    )


class LocateCloudFileTests(unittest.TestCase):
    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        self.service = DummyMpWaitService()
        self.service._cloud_query = MagicMock()
        self.service._cloud_query.list_files_recursive = MagicMock(
            return_value=[]
        )

    def _snapshot(self, mapping):
        self.service._snapshot_map = dict(mapping)

    def test_exact_name(self):
        """策略 1：单层快照精确名 O(1) 命中。"""
        target = _file("a.mkv")
        self._snapshot({"/dir": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file(
            "/dir", ["a.mkv"], "", _ctx({})
        )
        self.assertIs(found, target)

    def test_hash_match(self):
        """策略 2：哈希 O(1) 命中（名对不上也能找到）。"""
        target = _file("other.mkv", sha1="ABCDEF")
        self._snapshot({"/dir": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file(
            "/dir", ["a.mkv"], "abcdef", _ctx({})
        )
        self.assertIs(found, target)

    def test_fuzzy_copy_match(self):
        """策略 3：115 重名副本 "(1)" 容错命中。"""
        target = _file("a (1).mkv")
        self._snapshot({"/dir": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file(
            "/dir", ["a.mkv"], "", _ctx({})
        )
        self.assertIs(found, target)

    def test_season_episode_match(self):
        """策略 4：季集索引命中（候选名里没有集号时靠 item 的季集）。"""
        target = _file("剧名 - S01E05 - 第5集.mkv")
        self._snapshot({"/dir": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file(
            "/dir", [], "", _ctx({}),
            item={"season": 1, "episode": 5},
        )
        self.assertIs(found, target)

    def test_by_dest_exact_only(self):
        """by_dest 是 dest 策略的薄包装：只精确查，模糊名不命中。"""
        target = _file("a.mkv")
        self._snapshot({"/dir/sub": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file_by_dest(
            "/dir/sub/a.mkv", _ctx({})
        )
        self.assertIs(found, target)
        self.assertIsNone(
            self.service._locate_cloud_file_by_dest("/dir/sub/b.mkv", _ctx({}))
        )
        self.assertIsNone(
            self.service._locate_cloud_file_by_dest("", _ctx({}))
        )

    def test_dest_strategy_does_not_fuzzy_match(self):
        """dest 策略不进模糊兜底：精确名不在，"(1)" 副本也不能顶。"""
        target = _file("a (1).mkv")
        self._snapshot({"/dir/sub": DirectoryFileIndex([target])})
        found = self.service._locate_cloud_file(
            "", [], "", _ctx({}), dest_path="/dir/sub/a.mkv"
        )
        self.assertIsNone(found)

    def test_recursive_tree_strategy(self):
        """策略 5：单层没有、树里有——recursive=True 找到，False 找不到。"""
        nested = _file("a.mkv", file_id="nested-1")
        self._snapshot({"/dir": DirectoryFileIndex([])})
        self.service._cloud_query.list_files_recursive = MagicMock(
            return_value=[nested]
        )
        found = self.service._locate_cloud_file(
            "/dir", ["a.mkv"], "", _ctx({}), recursive=True
        )
        self.assertIs(found, nested)
        self.service._cloud_query.list_files_recursive = MagicMock(
            return_value=[nested]
        )
        self.assertIsNone(
            self.service._locate_cloud_file(
                "/dir", ["a.mkv"], "", _ctx({}), recursive=False
            )
        )

    def test_recursive_tree_uses_same_season_index(self):
        """递归树里的文件同样走季集索引（同一套 1–4 策略）。"""
        nested = _file("剧名 - S01E05 - 第5集.mkv", file_id="nested-1")
        self._snapshot({"/dir": DirectoryFileIndex([])})
        self.service._cloud_query.list_files_recursive = MagicMock(
            return_value=[nested]
        )
        found = self.service._locate_cloud_file(
            "/dir", [], "", _ctx({}),
            item={"season": 1, "episode": 5}, recursive=True,
        )
        self.assertIs(found, nested)

    def test_tree_primitive_returns_list_and_swallow_errors(self):
        """树原语：正常返回列表；列目录抛异常时返回 []（不抛）。"""
        nested = _file("a.mkv")
        self.service._cloud_query.list_files_recursive = MagicMock(
            return_value=[nested]
        )
        self.assertEqual(
            self.service._locate_cloud_files_in_tree("/dir"), [nested]
        )
        self.service._cloud_query.list_files_recursive = MagicMock(
            side_effect=RuntimeError("网盘抖动")
        )
        self.assertEqual(
            self.service._locate_cloud_files_in_tree("/dir"), []
        )
        self.assertEqual(
            self.service._locate_cloud_files_in_tree(""), []
        )


if __name__ == "__main__":
    unittest.main()
