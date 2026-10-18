"""媒体库忽略路径过滤单测。"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    install_app_mocks,
    load_module,
    register_module,
    register_package,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.db", "app.db.models", "app.db.models.mediaserver",
    "app.chain", "app.chain.download", "app.chain.media",
    "app.core", "app.core.metainfo",
)


class _DummyMediaServerItem:
    media_source = None
    media_id = None
    path = None
    item_type = None
    seasoninfo = None


register_package("app.db", paths=[])
register_package("app.db.models", paths=[])
register_module(
    "app.db.models.mediaserver", MediaServerItem=_DummyMediaServerItem
)
sys.modules["app.db"].SessionFactory = MagicMock

register_package(
    "cloudsubscribex",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex")],
)
register_package(
    "cloudsubscribex.core",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex", "core")],
)

media_scope = load_module(
    "cloudsubscribex.core.media_scope",
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "media_scope.py"),
)
seasons = load_module(
    "cloudsubscribex.core.subscribe.seasons",
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "subscribe", "seasons.py"),
)


class TestMediaPathIgnore(unittest.TestCase):
    def tearDown(self):
        media_scope.configure_media_path_ignores("")

    def test_exact_and_child_paths_ignored(self):
        media_scope.configure_media_path_ignores(
            "/media/strm/115网盘分享\n/media/strm/other/"
        )
        self.assertTrue(
            media_scope.is_ignored_media_path("/media/strm/115网盘分享")
        )
        self.assertTrue(
            media_scope.is_ignored_media_path(
                "/media/strm/115网盘分享/剧集/第 1 集.mkv"
            )
        )
        self.assertTrue(
            media_scope.is_ignored_media_path("\\media\\strm\\115网盘分享\\a.mkv")
        )
        self.assertFalse(
            media_scope.is_ignored_media_path("/media/strm/115网盘分享2/a.mkv")
        )
        self.assertFalse(
            media_scope.is_ignored_media_path("/media/strm/transfer/115网盘分享/a.mkv")
        )

    def test_filter_media_rows_keeps_transfer_library(self):
        media_scope.configure_media_path_ignores("/media/strm/115网盘分享")
        rows = [
            SimpleNamespace(path="/media/strm/115网盘分享/剧集/E01.mkv"),
            SimpleNamespace(path="/media/strm/115网盘/剧集/E01.mkv"),
        ]
        kept = media_scope.filter_media_rows(rows)
        self.assertEqual(len(kept), 1)
        self.assertIn("115网盘/", kept[0].path)

    def test_season_episodes_only_from_allowed_rows(self):
        media_scope.configure_media_path_ignores("/media/strm/115网盘分享")
        rows = [
            SimpleNamespace(
                item_type="Series",
                path="/media/strm/115网盘分享/剧集",
                seasoninfo={1: [1, 2, 3]},
            ),
            SimpleNamespace(
                item_type="Series",
                path="/media/strm/115网盘/剧集",
                seasoninfo={"1": ["3"]},
            ),
        ]
        original = media_scope._scoped_rows
        media_scope._scoped_rows = lambda mediainfo: media_scope.filter_media_rows(rows)
        try:
            episodes = media_scope.season_episodes_in_scope(
                SimpleNamespace(media_source="themoviedb", media_id="1"), 1
            )
        finally:
            media_scope._scoped_rows = original
        self.assertEqual(episodes, {3})

    def test_probe_returns_only_transfer_episodes(self):
        media_scope.configure_media_path_ignores("/media/strm/115网盘分享")
        original = seasons.season_episodes_in_scope
        seasons.season_episodes_in_scope = lambda mediainfo, season: {3}
        try:
            progress = seasons.probe_season_progress(
                SimpleNamespace(
                    title="测试剧",
                    year=2024,
                    type=None,
                    media_source="themoviedb",
                    media_id="1",
                    seasons={1: [1, 2, 3]},
                ),
                1,
            )
        finally:
            seasons.season_episodes_in_scope = original
        self.assertFalse(progress.complete)
        self.assertEqual(progress.missing_episodes, [1, 2])
        self.assertEqual(progress.total_episode, 3)

    def test_probe_complete_when_transfer_library_has_all(self):
        media_scope.configure_media_path_ignores("/media/strm/115网盘分享")
        original = seasons.season_episodes_in_scope
        seasons.season_episodes_in_scope = lambda mediainfo, season: {1, 2, 3}
        try:
            progress = seasons.probe_season_progress(
                SimpleNamespace(
                    title="测试剧",
                    year=2024,
                    type=None,
                    media_source="themoviedb",
                    media_id="1",
                    seasons={1: [1, 2, 3]},
                ),
                1,
            )
        finally:
            seasons.season_episodes_in_scope = original
        self.assertTrue(progress.complete)
        self.assertEqual(progress.missing_episodes, [])


if __name__ == "__main__":
    unittest.main()
