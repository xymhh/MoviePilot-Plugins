"""下载目标名带 {tmdb-<id>} 标记（1.12.4）。

背景（2026-11-16）：用户发现《凡人修仙传》S01E200 被识别成了电视剧
版本。实测：13:51 的整理记录里拟整理目标是
/转存/凡人修仙传 (2025) {tmdb-243224}/Season 1/……（2025 年国产剧），
而插件认的是 2020 年国漫版 tmdb-106449，此前 S01E193–S01E199 都是
按 106449 成功入库的。根因：下载目标名里只有标题没有 id，MP 只能靠
标题猜，同名多版本就可能猜错。

修法：目标文件名里加上 {tmdb-<id>} 标记。MP 的文件名识别会解析
{tmdb-...}/[tmdb-...] 标记并直接用该 id 认媒体（MP 源码
app/domain/metainfo.py 的 _EMBY_TMDB_RE_LIST），不靠标题猜。
标记插在标题（与年份）后面、集数前面：
“凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv”。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

_SYNC_COMPONENTS = ("baseline", "utils")
install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory", "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils", "cloudsubscribex.utils.cache",
    *[f"cloudsubscribex.handlers.sync.{name}" for name in _SYNC_COMPONENTS],
)

ensure_package("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
ensure_package("cloudsubscribex.core").CloudFile = MagicMock()

MediaType = set_media_type(movie="电影", tv="电视剧")
naming = load_module(
    "cloudsubscribex.handlers.sync.naming",
    "plugins.v2/cloudsubscribex/handlers/sync/naming.py",
)
SyncNamingService = naming.SyncNamingService
naming.media_identity = lambda value: (
    getattr(value, "media_source", None),
    getattr(value, "media_id", None),
)


class _FakeDirectory:
    def __init__(self, **kwargs):
        self.media_type = "电影"
        self.library_type_folder = False
        self.library_category_folder = True
        self.__dict__.update(kwargs)

    def model_copy(self, deep=False, update=None):
        clone = _FakeDirectory(**self.__dict__)
        if update:
            clone.__dict__.update(update)
        return clone

    def copy(self, deep=False, update=None):
        return self.model_copy(deep=deep, update=update)


class _FakeDirectoryHelper:
    directory = _FakeDirectory()

    def get_dir(self, media=None, include_unsorted=False):
        return self.directory


class _FakeTransHandler:
    @staticmethod
    def get_dest_dir(mediainfo=None, target_dir=None):
        parts = [str(target_dir.library_path)]
        if getattr(target_dir, "library_type_folder", False):
            parts.append(getattr(target_dir, "preset_type_folder", None) or "电影")
        if getattr(target_dir, "library_category_folder", False):
            parts.append(getattr(target_dir, "preset_category", None) or "")
        return str(Path(*[part for part in parts if part]))


class _FakeService(SyncNamingService):
    def __init__(self, category="外语电影", **config):
        config.setdefault("_type_folder_enabled", False)
        config.setdefault("_type_folder_movie", "电影")
        config.setdefault("_type_folder_tv", "剧集")
        super().__init__(SimpleNamespace(**config))
        self._category = category

    def _resolve_classification_category(self, subscribe, mediainfo):
        return self._category

    def _effective_mediainfo(self, subscribe, mediainfo):
        return mediainfo


class TmdbTagTest(unittest.TestCase):
    def setUp(self):
        self._original_directory_helper = naming.DirectoryHelper
        self._original_trans_handler = naming.TransHandler
        self._original_recommend = naming.FileManagerModule.recommend_name
        self._original_normalize_season = naming.normalize_season
        naming.DirectoryHelper = _FakeDirectoryHelper
        naming.TransHandler = _FakeTransHandler
        # 兜底拼接用的 normalize_season 在本 harness 里是自动 mock，
        # f"{s_num:02d}" 会炸；按真实语义 stub 成季号归一。
        naming.normalize_season = lambda season: int(season or 1)
        self._recommend_return = ""
        naming.FileManagerModule.recommend_name = (
            lambda meta, media: self._recommend_return
        )
        _FakeDirectoryHelper.directory = _FakeDirectory(preset_category="外语电影")

    def tearDown(self):
        naming.DirectoryHelper = self._original_directory_helper
        naming.TransHandler = self._original_trans_handler
        naming.FileManagerModule.recommend_name = self._original_recommend
        naming.normalize_season = self._original_normalize_season

    @staticmethod
    def _mediainfo(title, tmdb_id, year, media_type):
        return SimpleNamespace(
            type=media_type,
            category=None,
            library_category=None,
            media_source="tmdb",
            media_id=str(tmdb_id) if tmdb_id else None,
            tmdb_id=tmdb_id,
            title=title,
            year=year,
        )

    def _target_name(self, mediainfo, source_name, season=None, episode=None):
        service = _FakeService()
        _cloud_dir, target_name = service._platform_target(
            "/媒体库", None, mediainfo, source_name,
            season=season, episode=episode,
        )
        return target_name

    def test_tv_episode_gets_tmdb_tag_after_title(self):
        """凡人修仙传 S01E200：标题后加标记，MP 按 106449 认国漫版。"""
        self._recommend_return = "凡人修仙传 - S01E200 - 第200集.mkv"
        mediainfo = self._mediainfo("凡人修仙传", 106449, "2020", MediaType.TV)

        name = self._target_name(
            mediainfo, "S01E200.2020.2160p.WEB-DL.H.264.AAC.mkv",
            season=1, episode=200,
        )

        self.assertEqual(
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv", name
        )

    def test_tv_with_year_tag_goes_after_year(self):
        self._recommend_return = "十万狂花入梦来 (2026) - S01E06 - 第6集.mkv"
        mediainfo = self._mediainfo("十万狂花入梦来", 261315, "2026", MediaType.TV)

        name = self._target_name(
            mediainfo, "06.mkv", season=1, episode=6,
        )

        self.assertEqual(
            "十万狂花入梦来 (2026) {tmdb-261315} - S01E06 - 第6集.mkv", name
        )

    def test_movie_tag_goes_after_year_before_extension(self):
        self._recommend_return = "只要活下去 (2026) - 2160p.mkv"
        mediainfo = self._mediainfo("只要活下去", 1234567, "2026", MediaType.MOVIE)

        name = self._target_name(mediainfo, "Doing.Life.2026.2160p.mkv")

        self.assertEqual("只要活下去 (2026) {tmdb-1234567} - 2160p.mkv", name)

    def test_no_tmdb_id_leaves_name_unchanged(self):
        self._recommend_return = "十万狂花入梦来 - S01E06 - 第6集.mkv"
        mediainfo = self._mediainfo("十万狂花入梦来", None, "2026", MediaType.TV)

        name = self._target_name(mediainfo, "06.mkv", season=1, episode=6)

        self.assertEqual("十万狂花入梦来 - S01E06 - 第6集.mkv", name)

    def test_already_tagged_name_is_not_tagged_twice(self):
        self._recommend_return = "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv"
        mediainfo = self._mediainfo("凡人修仙传", 106449, "2020", MediaType.TV)

        name = self._target_name(
            mediainfo, "S01E200.2020.2160p.WEB-DL.H.264.AAC.mkv",
            season=1, episode=200,
        )

        self.assertEqual(
            "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv", name
        )

    def test_name_not_starting_with_title_is_left_alone(self):
        """自定义模板把标题放后面的，不硬改格式。"""
        self._recommend_return = "S01E200 - 凡人修仙传.mkv"
        mediainfo = self._mediainfo("凡人修仙传", 106449, "2020", MediaType.TV)

        name = self._target_name(
            mediainfo, "S01E200.2020.2160p.WEB-DL.H.264.AAC.mkv",
            season=1, episode=200,
        )

        self.assertEqual("S01E200 - 凡人修仙传.mkv", name)

    def test_fallback_name_also_gets_tag(self):
        """重命名模板取不到时走兜底拼接，文件名同样加标记。"""
        self._recommend_return = ""
        mediainfo = self._mediainfo("凡人修仙传", 106449, "2020", MediaType.TV)

        name = self._target_name(
            mediainfo, "S01E200.2020.2160p.WEB-DL.H.264.AAC.mkv",
            season=1, episode=200,
        )

        self.assertEqual("凡人修仙传 {tmdb-106449} - S01E200.mkv", name)


if __name__ == "__main__":
    unittest.main()
