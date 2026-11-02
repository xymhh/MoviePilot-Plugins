"""转存全流程三大核心重构专项测试（1.9.31）。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain",
    "app.core",
    "app.core.cache",
    "app.core.context",
    "app.core.metainfo",
    "app.db",
    "app.helper",
    "app.log",
    "app.schemas",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.utils",
    "cloudsubscribex.search",
    "cloudsubscribex.search.matching",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
)
set_media_type(movie="电影", tv="电视剧")

core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator

# 导入真实 search.matching 模块供 matching.py 使用
_search_matching = load_module(
    "cloudsubscribex.search.matching",
    "plugins.v2/cloudsubscribex/search/matching.py",
)
sys.modules["cloudsubscribex.search.matching"] = _search_matching

# 导入真实 file_matcher 供 matching.py 使用
_file_matcher = load_module(
    "cloudsubscribex.utils.file_matcher",
    "plugins.v2/cloudsubscribex/utils/file_matcher.py",
)
sys.modules["cloudsubscribex.utils.file_matcher"] = _file_matcher
sys.modules["cloudsubscribex.utils"].FileMatcher = _file_matcher.FileMatcher

_matching = load_module(
    "cloudsubscribex.handlers.sync.matching",
    "plugins.v2/cloudsubscribex/handlers/sync/matching.py",
)


class FakeMediaInfo:
    def __init__(self, title, year="2026", en_title=None, original_title=None):
        self.title = title
        self.year = str(year)
        self.en_title = en_title
        self.original_title = original_title or en_title
        self.original_name = None
        self.names = [title]
        if en_title:
            self.names.append(en_title)
        self.title_year = f"{title} ({year})"


class MovieMatchingFallbackTest(unittest.TestCase):
    def setUp(self):
        self.service = _matching.FileMatchingService.__new__(_matching.FileMatchingService)
        self.service._search_handler = MagicMock()
        self.service._search_handler.select_file_candidate.side_effect = (
            lambda candidates, mediainfo, subscribe: (candidates[0], 100) if candidates else (None, 0)
        )

    def test_movie_match_fallback_with_title_parentheses_year(self):
        """发布标题带全角年份括号《你能看到一切（2026） 中英字幕》，视频名混淆，能正确兜底匹配。"""
        mediainfo = FakeMediaInfo(title="你能看到一切", year="2026")
        files = [
            {
                "name": "2026.1080p中英字幕.mp4",
                "is_dir": False,
                "size": 4909142381,
                "_relative_path": "2026.1080p中英字幕.mp4",
            }
        ]
        matched, score = self.service._match_movie_file(
            files=files,
            mediainfo=mediainfo,
            subscribe=MagicMock(),
            resource_title="你能看到一切（2026） 中英字幕",
            require_media_match=True,
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "2026.1080p中英字幕.mp4")
        self.assertEqual(score, 100)

    def test_movie_match_fallback_with_parent_dir_title(self):
        """发布标题无关键信息，但父目录名为《2026犯罪《你-能-看-到-一-切》》，能根据父目录多维兜底匹配。"""
        mediainfo = FakeMediaInfo(title="你能看到一切", year="2026")
        files = [
            {
                "name": "movie.mp4",
                "is_dir": False,
                "size": 3000000000,
                "_relative_path": "2026犯罪《你-能-看-到-一-切》/movie.mp4",
            }
        ]
        matched, score = self.service._match_movie_file(
            files=files,
            mediainfo=mediainfo,
            subscribe=MagicMock(),
            resource_title="D 夸克大片更新",
            require_media_match=True,
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "movie.mp4")
        self.assertEqual(score, 100)

    def test_movie_match_fallback_multi_videos_main_size(self):
        """分享内有 1 个大主视频和 1 个小样品视频，能够选出大主视频而不是直接失败。"""
        mediainfo = FakeMediaInfo(title="你能看到一切", year="2026")
        files = [
            {
                "name": "main_movie.mp4",
                "is_dir": False,
                "size": 4000000000,
                "_relative_path": "main_movie.mp4",
            },
            {
                "name": "sample.mp4",
                "is_dir": False,
                "size": 50000000,
                "_relative_path": "sample.mp4",
            },
        ]
        matched, score = self.service._match_movie_file(
            files=files,
            mediainfo=mediainfo,
            subscribe=MagicMock(),
            resource_title="你能看到一切（2026） 4K超清",
            require_media_match=True,
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched["name"], "main_movie.mp4")


if __name__ == "__main__":
    unittest.main()
