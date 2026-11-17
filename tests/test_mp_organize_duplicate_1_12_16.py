"""1.15.0 Phase 3a 第 12 项：入库前用 MP 自己的 get_no_exists_info 查库。

根因：同一集从两个来源各下一份时，两边都会触发 MP 整理，文件名
一不一样（带 {tmdb-} 标记/不带、mp4/mkv）就互相覆盖不了，全进
库了（玫瑰战争 E24/E25、多喜一家人 E05/E06）。v1.12.16 的
_library_episode_exists 只列媒体库根目录一层，子文件夹里的文件
看不见，等于没拦。MP 的整理去重只看算出的目标路径，剧集文件夹
是按重命名格式算出来的，插件不自己钻文件夹；MP 下载链的
get_no_exists_info 按 tmdb id + 季问 Emby，是 MP 自己的按集查库
口径。trigger_mp_organize 是全部 MP 整理的唯一入口，在这里入库
前先问 MP，查到已存在就返回 already_in_library、不触发、不记
失败、不重试；查不出来（身份缺失/解析不出/宿主异常）就放行整理。

覆盖 handlers/sync/mp_organize.py 的 _episode_exists_in_library_via_mp
与 trigger_mp_organize 的已入库分支，以及 handlers/sync/transfer_ops.py
跨盘调用方对 already_in_library 的处理（只断言原因分流，不跑
整条跨盘流程）。
"""

import re
import unittest
from types import SimpleNamespace
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
    "app.chain.download",
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


class FakeDownloadChain:
    """MP 下载链替身：按 next_result 回答 get_no_exists_info。"""

    next_result = (False, {})
    calls = []

    def get_no_exists_info(self, meta, mediainfo):
        FakeDownloadChain.calls.append((meta, mediainfo))
        if isinstance(FakeDownloadChain.next_result, Exception):
            raise FakeDownloadChain.next_result
        return FakeDownloadChain.next_result


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


def _install_stubs():
    register_module("app.chain.storage", StorageChain=FakeStorageChain)
    register_module("app.chain.transfer", TransferChain=FakeTransferChain)
    register_module("app.chain.download", DownloadChain=FakeDownloadChain)
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

    def __init__(self):
        object.__setattr__(self, "_mp_organize_after_share_save", True)
        object.__setattr__(
            self, "_video_extensions", [".mkv", ".mp4", ".avi", ".iso"]
        )
        object.__setattr__(
            self, "_cloud_transfer_path", "/Alist/影视/moviepilot/转存"
        )


def _media():
    return {
        "tmdb_id": 299276,
        "media_type": "电视剧",
        "season": 1,
        "year": "2026",
        "title": "玫瑰战争",
    }


def _missing(*episodes):
    """MP 的 no_exists 形状：{media_key: {季: 缺集信息}}。"""
    return {"tmdb-299276": {1: SimpleNamespace(episodes=list(episodes))}}


class TestEpisodeExistsViaMp(unittest.TestCase):
    def setUp(self):
        _install_stubs()
        FakeDownloadChain.calls = []
        FakeDownloadChain.next_result = (False, {})

    def test_episode_not_in_missing_list_is_in_library(self):
        """缺集里只有 E23，待查的 E24 已在库。"""
        FakeDownloadChain.next_result = (False, _missing(23))
        service = _Service()
        self.assertTrue(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_episode_in_missing_list_is_not_in_library(self):
        """缺集里有 E24，待查的 E24 不在库。"""
        FakeDownloadChain.next_result = (False, _missing(24))
        service = _Service()
        self.assertFalse(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_mp_says_whole_media_exists(self):
        """MP 说整部已存在（exists=True），直接算已在库。"""
        FakeDownloadChain.next_result = (True, {})
        service = _Service()
        self.assertTrue(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_string_season_key_in_missing(self):
        """no_exists 的季键是字符串 "1" 时同样能比对。"""
        FakeDownloadChain.next_result = (
            False,
            {"tmdb-299276": {"1": SimpleNamespace(episodes=[24])}},
        )
        service = _Service()
        self.assertFalse(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_whole_season_missing_entry_is_not_in_library(self):
        """MP 的缺集条目 episodes=[] 表示整季缺失，待查集不在库。"""
        FakeDownloadChain.next_result = (
            False,
            {"tmdb-299276": {1: SimpleNamespace(episodes=[])}},
        )
        service = _Service()
        self.assertFalse(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_empty_no_exists_fails_open(self):
        """MP 返回 (False, {}) 是没查出来（识别失败等），放行（None）。"""
        FakeDownloadChain.next_result = (False, {})
        service = _Service()
        self.assertIsNone(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_no_tmdb_id_fails_open(self):
        """没有 tmdb 身份就查不出来，放行（None），不调 MP。"""
        service = _Service()
        media = _media()
        del media["tmdb_id"]
        self.assertIsNone(
            service._episode_exists_in_library_via_mp(
                media=media,
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )
        self.assertEqual(FakeDownloadChain.calls, [])

    def test_chain_error_fails_open(self):
        """MP 查库抛异常时放行（None），不拦正常入库。"""
        FakeDownloadChain.next_result = RuntimeError("Emby 不可用")
        service = _Service()
        self.assertIsNone(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争 - S01E24 - 第24集.mp4",
            )
        )

    def test_unparsable_file_name_fails_open(self):
        """文件名解析不出集号时放行（None）。"""
        service = _Service()
        self.assertIsNone(
            service._episode_exists_in_library_via_mp(
                media=_media(),
                file_name="玫瑰战争.mp4",
            )
        )
        self.assertEqual(FakeDownloadChain.calls, [])

    def test_movie_exists_flag(self):
        """电影没有季集：MP 说整部存在即已在库，说不存在即不在库。"""
        movie = {
            "tmdb_id": 123,
            "media_type": "电影",
            "year": "2026",
            "title": "测试电影",
        }
        service = _Service()
        FakeDownloadChain.next_result = (True, {})
        self.assertTrue(
            service._episode_exists_in_library_via_mp(
                media=movie, file_name="测试电影.mp4"
            )
        )
        FakeDownloadChain.next_result = (False, {})
        self.assertFalse(
            service._episode_exists_in_library_via_mp(
                media=movie, file_name="测试电影.mp4"
            )
        )


class TestTriggerSkipsDuplicate(unittest.TestCase):
    def setUp(self):
        # set_media_type 每次调用都新建一个 MediaType 枚举类：
        # 先绑定再装桩，否则测试里赋的 mediainfo.type 与方法内现取
        # 的 MediaType.TV 是两个类的成员，判重会 fail-open。
        global MediaType
        MediaType = set_media_type(movie="电影", tv="电视剧")
        _install_stubs()
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        FakeStorageChain.next_result = "found"
        FakeDirectoryHelper.next_result = "found"
        FakeDownloadChain.calls = []
        FakeDownloadChain.next_result = (False, {})


    def test_duplicate_returns_already_in_library_without_do_transfer(self):
        """E24 已在库：返回 already_in_library，不调 do_transfer。"""
        FakeDownloadChain.next_result = (False, _missing(23))
        service = _Service()
        ok, info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media=_media(),
        )
        self.assertFalse(ok)
        self.assertEqual(info.get("reason"), "already_in_library")
        self.assertEqual(FakeTransferChain.calls, [])

    def test_not_duplicate_still_triggers_do_transfer(self):
        """E24 不在库：正常触发 do_transfer。"""
        FakeDownloadChain.next_result = (False, _missing(24))
        service = _Service()
        ok, info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media=_media(),
        )
        self.assertTrue(ok)
        self.assertEqual(len(FakeTransferChain.calls), 1)

    def test_check_failure_fails_open_and_triggers(self):
        """MP 查库失败时放行整理，不拦正常入库。"""
        FakeDownloadChain.next_result = RuntimeError("Emby 不可用")
        service = _Service()
        ok, _info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media=_media(),
        )
        self.assertTrue(ok)
        self.assertEqual(len(FakeTransferChain.calls), 1)

    def test_batch_counts_already_in_library_as_accepted(self):
        """批量触发：已在库的项同样计入已处理（skip_history）。"""
        FakeDownloadChain.next_result = (False, _missing(23))
        service = _Service()
        mediainfo = MagicMock()
        mediainfo.type = MediaType.TV
        mediainfo.tmdb_id = 299276
        mediainfo.title = "玫瑰战争"
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
