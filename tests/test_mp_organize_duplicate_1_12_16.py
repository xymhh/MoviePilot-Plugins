"""1.12.16：入库前先查库里有没有这集，重复来源不再重复入库。

根因：同一集从两个来源各下一份时，两边都会触发 MP 整理，文件名
一不一样（带 {tmdb-} 标记/不带、mp4/mkv）就互相覆盖不了，全进
库了（玫瑰战争 E24：00:43 插件转存落盘带标记的 1.24GB，01:05
MP 整理又移入不带标记的 1.32GB；E25：MP 整理把 mp4 与 mkv 各
整理了一次）。trigger_mp_organize 是全部 MP 整理的唯一入口，
在这里入库前先列目标库目录、按 MetaInfo 季集比对，查到已存在
就返回 already_in_library、不触发、不记失败、不重试；跨盘调用
方把该原因当成功处理，批量调用方把该项计入已处理（skip_history）。

覆盖 handlers/sync/mp_organize.py 的 _library_episode_exists 与
trigger_mp_organize 的已入库分支，以及 handlers/sync/transfer_ops.py
跨盘调用方对 already_in_library 的处理（只断言原因分流，不跑
整条跨盘流程）。
"""

import re
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.chain",
    "app.chain.storage",
    "app.chain.transfer",
    "app.core.metainfo",
    "app.schemas.file",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser",
)
install_core_delegation()
MediaType = set_media_type()


class FakeFileItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageChain:
    next_result = "found"

    def get_item(self, fileitem):
        if FakeStorageChain.next_result == "found":
            return fileitem
        return None


class FakeTransferChain:
    calls = []
    next_result = (True, "")

    def do_transfer(self, fileitem, **kwargs):
        FakeTransferChain.calls.append((fileitem, kwargs))
        return FakeTransferChain.next_result


class FakeMediaInfo:
    title = None
    type = None
    tmdb_id = None
    year = None
    season = None
    category = ""
    library_category = ""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeDirectoryConf:
    def __init__(self, **kwargs):
        self.library_path = (
            "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
            "玫瑰战争 (2026) {tmdb-299276}/Season 1"
        )
        self.library_storage = "u115cs"
        self.name = "剧集"
        self.library_type_folder = False
        self.library_category_folder = False
        self.__dict__.update(kwargs)

    def model_copy(self, deep=False, update=None):
        clone = FakeDirectoryConf(**self.__dict__)
        if update:
            clone.__dict__.update(update)
        return clone

    def copy(self, deep=False, update=None):
        return self.model_copy(deep=deep, update=update)


class FakeDirectoryHelper:
    next_result = "found"

    def get_dir(self, media=None, include_unsorted=False, **kwargs):
        if FakeDirectoryHelper.next_result == "found":
            return FakeDirectoryConf()
        return None


class FakeMetaInfo:
    """只解析 SxxExx 的 MetaInfo 替身。"""

    def __init__(self, name, custom_words=None):
        match = re.search(r"[Ss](\d+)[Ee](\d+)", str(name or ""))
        self.begin_season = int(match.group(1)) if match else None
        self.begin_episode = int(match.group(2)) if match else None
        self.episode_list = (
            [self.begin_episode] if self.begin_episode else []
        )


class FakeMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name or "").lower().endswith(
            (".mkv", ".mp4", ".avi", ".iso")
        )


class _FakeDirItem:
    def __init__(self, name, is_directory=False):
        self.name = name
        self.is_directory = is_directory
        self.id = f"id-{name}"
        self.size = 1000


class _FakeLookup:
    def __init__(self, checked=True, directory_id="dir-1"):
        self.checked = checked
        self.directory_id = directory_id


class _FakeListing:
    def __init__(self, files, checked=True):
        self.files = list(files)
        self.checked = checked


class FakeCloudDirectories:
    """resolve/list 可控：rows 为目标库目录里的文件。"""

    rows = []
    resolve_checked = True
    list_checked = True
    resolve_raises = False

    def resolve_directory(self, path):
        if FakeCloudDirectories.resolve_raises:
            raise OSError("resolve 失败")
        if not FakeCloudDirectories.resolve_checked:
            return _FakeLookup(checked=False, directory_id=None)
        return _FakeLookup(checked=True, directory_id="dir-1")

    def list_directory(self, directory_id):
        if not FakeCloudDirectories.list_checked:
            return _FakeListing([], checked=False)
        return _FakeListing(FakeCloudDirectories.rows, checked=True)


def _install_stubs():
    register_module("app.chain.storage", StorageChain=FakeStorageChain)
    register_module("app.chain.transfer", TransferChain=FakeTransferChain)
    register_module("app.schemas.file", FileItem=FakeFileItem)
    register_module("app.core.context", MediaInfo=FakeMediaInfo)
    register_module("app.helper.directory", DirectoryHelper=FakeDirectoryHelper)
    register_module(
        "app.application.directory", DirectoryHelper=FakeDirectoryHelper
    )
    register_module("app.core.metainfo", MetaInfo=FakeMetaInfo)
    register_module(
        "cloudsubscribex.utils.file_parser",
        MediaFileParser=FakeMediaFileParser,
    )


_install_stubs()

mp_organize = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize.MpOrganizeMixin


class _Service(MpOrganizeMixin):
    """只测整理触发与入库检查，不依赖插件宿主。"""

    def __init__(self, directories=None):
        object.__setattr__(self, "_mp_organize_after_share_save", True)
        object.__setattr__(
            self, "_video_extensions", [".mkv", ".mp4", ".avi", ".iso"]
        )
        object.__setattr__(
            self, "_cloud_transfer_path", "/Alist/影视/moviepilot/转存"
        )
        object.__setattr__(self, "_cloud_directories", directories)


def _media():
    return {
        "tmdb_id": 299276,
        "media_type": "电视剧",
        "season": 1,
        "year": "2026",
        "title": "玫瑰战争",
    }


class TestLibraryEpisodeExists(unittest.TestCase):
    def setUp(self):
        _install_stubs()
        FakeCloudDirectories.rows = []
        FakeCloudDirectories.resolve_checked = True
        FakeCloudDirectories.list_checked = True
        FakeCloudDirectories.resolve_raises = False

    def test_same_episode_different_name_is_duplicate(self):
        """库里已有带标记的 E24，待整理的不带标记 E24 算重复。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4"),
        ]
        service = _Service(FakeCloudDirectories())
        found = service._library_episode_exists(
            "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
            "玫瑰战争 (2026) {tmdb-299276}/Season 1",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            1,
        )
        self.assertEqual(
            found, "玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4"
        )

    def test_different_episode_is_not_duplicate(self):
        """库里只有 E23，待整理的 E24 不算重复。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 - S01E23 - 第23集.mkv"),
        ]
        service = _Service(FakeCloudDirectories())
        self.assertIsNone(
            service._library_episode_exists(
                "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
                "玫瑰战争 (2026) {tmdb-299276}/Season 1",
                "玫瑰战争 - S01E24 - 第24集.mp4",
                1,
            )
        )

    def test_same_episode_different_format_is_duplicate(self):
        """库里已有 E25 mp4，待整理的 E25 mkv 算重复。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 - S01E25 - 第25集.mp4"),
        ]
        service = _Service(FakeCloudDirectories())
        self.assertEqual(
            service._library_episode_exists(
                "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
                "玫瑰战争 (2026) {tmdb-299276}/Season 1",
                "玫瑰战争 - S01E25 - 第25集.mkv",
                1,
            ),
            "玫瑰战争 - S01E25 - 第25集.mp4",
        )

    def test_listing_failure_fails_open(self):
        """列目录失败时放行（返回 None），不拦正常入库。"""
        FakeCloudDirectories.list_checked = False
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4"),
        ]
        service = _Service(FakeCloudDirectories())
        self.assertIsNone(
            service._library_episode_exists(
                "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
                "玫瑰战争 (2026) {tmdb-299276}/Season 1",
                "玫瑰战争 - S01E24 - 第24集.mp4",
                1,
            )
        )

    def test_no_directories_service_fails_open(self):
        """没有目录服务时放行（返回 None）。"""
        service = _Service(None)
        self.assertIsNone(
            service._library_episode_exists(
                "/Alist/影视/moviepilot/媒体库/剧集/国产剧/"
                "玫瑰战争 (2026) {tmdb-299276}/Season 1",
                "玫瑰战争 - S01E24 - 第24集.mp4",
                1,
            )
        )


class TestTriggerSkipsDuplicate(unittest.TestCase):
    def setUp(self):
        _install_stubs()
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        FakeStorageChain.next_result = "found"
        FakeDirectoryHelper.next_result = "found"
        FakeCloudDirectories.rows = []
        FakeCloudDirectories.resolve_checked = True
        FakeCloudDirectories.list_checked = True
        FakeCloudDirectories.resolve_raises = False
        set_media_type(movie="电影", tv="电视剧")

    def test_duplicate_returns_already_in_library_without_do_transfer(self):
        """E24 已在库：返回 already_in_library，不调 do_transfer。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4"),
        ]
        service = _Service(FakeCloudDirectories())
        ok, info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media=_media(),
        )
        self.assertFalse(ok)
        self.assertEqual(info.get("reason"), "already_in_library")
        self.assertEqual(
            info.get("existing_file"),
            "玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4",
        )
        self.assertEqual(FakeTransferChain.calls, [])

    def test_not_duplicate_still_triggers_do_transfer(self):
        """E24 不在库：正常触发 do_transfer。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 - S01E23 - 第23集.mkv"),
        ]
        service = _Service(FakeCloudDirectories())
        ok, info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media=_media(),
        )
        self.assertTrue(ok)
        self.assertEqual(len(FakeTransferChain.calls), 1)

    def test_batch_counts_already_in_library_as_accepted(self):
        """批量触发：已在库的项同样计入已处理（skip_history）。"""
        FakeCloudDirectories.rows = [
            _FakeDirItem("玫瑰战争 {tmdb-299276} - S01E24 - 第24集.mp4"),
        ]
        service = _Service(FakeCloudDirectories())
        mediainfo = MagicMock()
        mediainfo.type = MediaType.TV
        mediainfo.category = "国产剧"
        items = [
            {
                "file": {"id": "file-24"},
                "target_name": "玫瑰战争 - S01E24 - 第24集.mp4",
            },
        ]
        accepted = service._trigger_mp_organize_for_batch(
            items, {"file-24"}, mediainfo=mediainfo, season=1,
        )
        self.assertEqual(accepted, {"file-24"})
        self.assertEqual(FakeTransferChain.calls, [])


if __name__ == "__main__":
    unittest.main()
