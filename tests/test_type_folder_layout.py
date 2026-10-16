"""媒体库一级目录（按媒体类型分层）开关的单测。

新增配置 ``type_folder_enabled``：打开后在网盘「媒体库目录」与「本地媒体根路径」下先建
媒体类型一级目录（默认 电影 / 剧集），再放二级分类（外语电影、国产剧…）；关闭时完全
保持原有行为。本文件把这几条固定住，防止后续改动悄悄改变目录层级。
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
    """宿主「设定 → 目录」条目替身。"""

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
    """返回固定目录条目的 DirectoryHelper 替身。"""

    directory = _FakeDirectory()

    def get_dir(self, media=None, include_unsorted=False):
        return self.directory


class _FakeTransHandler:
    """TransHandler 替身：按目录条目的类型/类别子目录开关拼路径。"""

    @staticmethod
    def get_dest_dir(mediainfo=None, target_dir=None):
        parts = [str(target_dir.library_path)]
        if getattr(target_dir, "library_type_folder", False):
            parts.append(getattr(target_dir, "preset_type_folder", None) or "电影")
        if getattr(target_dir, "library_category_folder", False):
            parts.append(getattr(target_dir, "preset_category", None) or "")
        return str(Path(*[part for part in parts if part]))


class _FakeService(SyncNamingService):
    """只暴露分类根目录计算所需的最小状态。"""

    def __init__(self, category="外语电影", **config):
        # 与插件配置解析保持一致：目录名缺省时回落 电影 / 剧集
        config.setdefault("_type_folder_enabled", False)
        config.setdefault("_type_folder_movie", "电影")
        config.setdefault("_type_folder_tv", "剧集")
        super().__init__(SimpleNamespace(**config))
        self._category = category

    def _resolve_classification_category(self, subscribe, mediainfo):
        return self._category

    def _effective_mediainfo(self, subscribe, mediainfo):
        return mediainfo


class TestTypeFolderLayout(unittest.TestCase):
    def setUp(self):
        self._original_directory_helper = naming.DirectoryHelper
        self._original_trans_handler = naming.TransHandler
        naming.DirectoryHelper = _FakeDirectoryHelper
        naming.TransHandler = _FakeTransHandler
        naming.FileManagerModule.recommend_name = (
            lambda meta, media: "测试 (2026)/Season 1/测试 - S01E01.mkv"
        )
        _FakeDirectoryHelper.directory = _FakeDirectory(preset_category="外语电影")

    def tearDown(self):
        naming.DirectoryHelper = self._original_directory_helper
        naming.TransHandler = self._original_trans_handler

    @staticmethod
    def _mediainfo(media_type):
        return SimpleNamespace(
            type=media_type,
            category=None,
            library_category=None,
            media_source="tmdb",
            media_id="1",
            tmdb_id=1,
            title="测试",
            year="2026",
        )

    def _root(self, media_type, root_path="/媒体库", **config):
        service = _FakeService(**config)
        return service._platform_classified_root(root_path, None, self._mediainfo(media_type))

    # ---- 关闭开关：保持原行为 ----

    def test_disabled_keeps_flat_category_layout(self):
        """开关关闭时，网盘根目录下直接是二级分类。"""
        resolved = self._root(MediaType.MOVIE, "/媒体库")
        self.assertEqual(Path("/媒体库/外语电影"), resolved)

    # ---- 打开开关：插入媒体类型一级目录 ----

    def test_enabled_movie_uses_movie_folder(self):
        resolved = self._root(MediaType.MOVIE, "/媒体库", _type_folder_enabled=True)
        self.assertEqual(Path("/媒体库/电影/外语电影"), resolved)

    def test_enabled_tv_uses_custom_tv_folder(self):
        resolved = self._root(
            MediaType.TV,
            "/媒体库",
            _type_folder_enabled=True,
            _type_folder_tv="剧集",
        )
        self.assertEqual(Path("/媒体库/剧集/外语电影"), resolved)

    def test_enabled_supports_custom_folder_names(self):
        resolved = self._root(
            MediaType.MOVIE,
            "/115/影视",
            _type_folder_enabled=True,
            _type_folder_movie="Movies",
            _type_folder_tv="TV",
        )
        self.assertEqual(Path("/115/影视/Movies/外语电影"), resolved)

    def test_enabled_is_idempotent_when_platform_already_adds_type_folder(self):
        """宿主目录已给出 电影/ 一级目录时不再重复插入。"""
        _FakeDirectoryHelper.directory = _FakeDirectory(
            library_type_folder=True,
            preset_type_folder="电影",
            library_category_folder=False,
        )
        resolved = self._root(MediaType.MOVIE, "/媒体库", _type_folder_enabled=True)
        self.assertEqual(Path("/媒体库/电影"), resolved)

    def test_enabled_applies_to_local_strm_root(self):
        """本地媒体根路径走同一套规则，本地 STRM 与网盘结构保持一致。"""
        service = _FakeService(_type_folder_enabled=True)
        resolved = service._platform_rename_path(
            root_path="/strm",
            subscribe=None,
            mediainfo=self._mediainfo(MediaType.TV),
            source_name="测试 2026 S01E01.mkv",
            season=1,
            episode=1,
        )
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.as_posix().startswith("/strm/剧集/"), resolved)

    # ---- 类型未知 / 兜底分支 ----

    def test_enabled_without_platform_directory_still_defaults_to_type_folder(self):
        """宿主目录不可用时，兜底路径同样带一级目录，且不与平台类型名叠层。"""
        naming.DirectoryHelper = None
        resolved = self._root(MediaType.TV, "/媒体库", _type_folder_enabled=True)
        self.assertEqual(Path("/媒体库/剧集/外语电影"), resolved)

    def test_unknown_media_type_keeps_original_path(self):
        resolved = self._root(SimpleNamespace(value="未知"), "/媒体库", _type_folder_enabled=True)
        self.assertEqual(Path("/媒体库/外语电影"), resolved)

    def test_outside_root_path_is_left_untouched(self):
        resolved = naming.SyncNamingService._with_type_folder(
            Path("/其他/外语电影"), "/媒体库", "电影"
        )
        self.assertEqual(Path("/其他/外语电影"), resolved)

    def test_root_only_path_gets_type_folder(self):
        resolved = naming.SyncNamingService._with_type_folder(Path("/媒体库"), "/媒体库", "电影")
        self.assertEqual(Path("/媒体库/电影"), resolved)


if __name__ == "__main__":
    unittest.main()
