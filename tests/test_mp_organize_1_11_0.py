"""P1：115 同盘直存后触发 MP 整理（v1.11.0）。

直存成功后调 MP 的 TransferChain.do_transfer（后台、手动整理），记录
进 MP 媒体整理；触发失败时返回 False，调用方保留插件自己的历史记录。
"""

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
    "app.chain.storage",
    "app.chain.transfer",
    "app.schemas.file",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
)
install_core_delegation()
MediaType = set_media_type()


class FakeFileItem:
    """只记构造参数的 FileItem 替身。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageChain:
    """get_item 可控：found 返回原项，missing 返回 None。"""

    next_result = "found"
    last_fileitem = None

    def get_item(self, fileitem):
        FakeStorageChain.last_fileitem = fileitem
        if FakeStorageChain.next_result == "found":
            return fileitem
        return None


class FakeTransferChain:
    """do_transfer 可控，记下每一次调用。"""

    calls = []
    next_result = (True, "")

    def do_transfer(self, fileitem, **kwargs):
        FakeTransferChain.calls.append((fileitem, kwargs))
        return FakeTransferChain.next_result


class FakeMediaInfo:
    """app.core.context.MediaInfo 替身：只存构造参数。"""

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
    """DirectoryHelper.get_dir 返回的目录条目替身（剧集条目）。"""

    def __init__(self, **kwargs):
        self.library_path = "/媒体库/剧集"
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
    """get_dir 记下调用参数，默认返回剧集条目。"""

    calls = []
    next_result = "found"
    last_conf = None

    def get_dir(self, media=None, include_unsorted=False, **kwargs):
        FakeDirectoryHelper.calls.append(
            {
                "media": media,
                "include_unsorted": include_unsorted,
                "kwargs": dict(kwargs),
            }
        )
        if FakeDirectoryHelper.next_result == "found":
            FakeDirectoryHelper.last_conf = FakeDirectoryConf()
            return FakeDirectoryHelper.last_conf
        FakeDirectoryHelper.last_conf = None
        return None


register_module("app.chain.storage", StorageChain=FakeStorageChain)
register_module("app.chain.transfer", TransferChain=FakeTransferChain)
register_module("app.schemas.file", FileItem=FakeFileItem)
register_module("app.core.context", MediaInfo=FakeMediaInfo)
# _resolve 先试 app.helper.directory 再试 app.application.directory：
# 全量套件里先跑的文件常把 app.helper.directory 注册成 AutoMock，
# 只桩 application 那边会被 MagicMock 截住，所以两边都桩同一个替身。
register_module("app.helper.directory", DirectoryHelper=FakeDirectoryHelper)
register_module("app.application.directory", DirectoryHelper=FakeDirectoryHelper)


def _install_stubs():
    """重注本文件的全部宿主桩。

    必须在每个用例执行前调一次（见 setUp）：全量收集时排在本文件
    之后的文件（如 test_search_state_lifecycle、
    test_tmdb_candidate_type_fallback）会用 register_mock 覆盖
    app.core.context，模块级那次注册到执行时已被抹掉——与
    set_media_type() 必须在 setUp 里重注是同一个道理。
    """
    register_module("app.chain.storage", StorageChain=FakeStorageChain)
    register_module("app.chain.transfer", TransferChain=FakeTransferChain)
    register_module("app.schemas.file", FileItem=FakeFileItem)
    register_module("app.core.context", MediaInfo=FakeMediaInfo)
    register_module("app.helper.directory", DirectoryHelper=FakeDirectoryHelper)
    register_module("app.application.directory", DirectoryHelper=FakeDirectoryHelper)


mp_organize = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize.MpOrganizeMixin


def _owner(**overrides):
    values = {
        "_mp_organize_after_share_save": True,
        "_video_extensions": [".mkv", ".mp4", ".avi", ".iso"],
        "_cloud_transfer_path": "/Alist/影视/moviepilot/转存",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestTriggerMpOrganize(unittest.TestCase):
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

    def test_flag_off_returns_false_without_calling_chain(self):
        mixin = MpOrganizeMixin(_owner(_mp_organize_after_share_save=False))
        ok, info = mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "disabled")
        self.assertEqual(FakeTransferChain.calls, [])

    def test_non_video_file_returns_false(self):
        ok, info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.srt", mediainfo=self.mediainfo,
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "not_video")
        self.assertEqual(FakeTransferChain.calls, [])

    def test_unresolved_file_returns_false(self):
        FakeStorageChain.next_result = "missing"
        ok, info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_source_file")
        self.assertEqual(FakeTransferChain.calls, [])

    def test_success_calls_do_transfer_background_manual(self):
        ok, info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "美国人质 - S01E01.mkv",
            mediainfo=self.mediainfo, season=1,
        )
        self.assertTrue(ok)
        self.assertEqual(info["src_path"], "/Alist/影视/moviepilot/转存/美国人质 - S01E01.mkv")
        self.assertEqual(info["src_storage"], "local")
        self.assertEqual(info["target_dir"], "/媒体库/剧集")
        self.assertEqual(info["target_storage"], "u115cs")
        self.assertEqual(info["target_name"], "剧集")
        self.assertEqual(info["file_name"], "美国人质 - S01E01.mkv")
        fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertEqual(fileitem.storage, "local")
        self.assertEqual(
            fileitem.path,
            "/Alist/影视/moviepilot/转存/美国人质 - S01E01.mkv",
        )
        self.assertEqual(fileitem.name, "美国人质 - S01E01.mkv")
        self.assertEqual(fileitem.extension, "mkv")
        self.assertIs(kwargs["mediainfo"], self.mediainfo)
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertTrue(kwargs["manual"])
        self.assertTrue(kwargs["background"])
        self.assertEqual(kwargs["season"], 1)
        self.assertEqual(kwargs["target_directory"].library_path, "/媒体库/剧集")
        self.assertEqual(kwargs["target_storage"], "u115cs")

    def test_do_transfer_rejected_returns_false(self):
        FakeTransferChain.next_result = (False, "整理失败")
        ok, info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        )
        self.assertFalse(ok)
        self.assertEqual(info["reason"], "not_accepted")
        self.assertIn("整理失败", info["message"])

    def test_media_dict_maps_mtype_and_season(self):
        ok, _info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv",
            media={"media_type": "TV", "season": 2},
        )
        self.assertTrue(ok)
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertEqual(kwargs["season"], 2)
        # v1.12.7：media 字典没有 category 时，拼出的最小 MediaInfo
        # 不传给 do_transfer（MP 分类校验会拒），MP 按文件名识别并
        # 自己分类；目标目录/存储照常传。
        self.assertNotIn("mediainfo", kwargs)
        self.assertEqual(kwargs["target_storage"], "u115cs")


class TestTriggerMpOrganizeForBatch(unittest.TestCase):
    def setUp(self):
        self.mixin = MpOrganizeMixin(_owner())
        self.items = [
            {"file": {"id": "ep1"}, "target_name": "a - S01E01.mkv"},
            {"file": {"id": "ep2"}, "target_name": "b - S01E02.mkv"},
            {"file": {"id": "ep3"}, "target_name": "c - S01E03.mkv"},
        ]

    def test_collects_accepted_ids_only(self):
        object.__setattr__(
            self.mixin, "trigger_mp_organize",
            MagicMock(side_effect=[
                (True, {"src_path": "/Alist/影视/moviepilot/转存/a - S01E01.mkv"}),
                (False, {"reason": "not_accepted", "message": "MP 整理未接受"}),
                (True, {"src_path": "/Alist/影视/moviepilot/转存/c - S01E03.mkv"}),
            ]),
        )
        accepted = self.mixin._trigger_mp_organize_for_batch(
            self.items, {"ep1", "ep2", "ep3"},
            mediainfo=MagicMock(), season=1,
        )
        self.assertEqual(accepted, {"ep1", "ep3"})
        self.assertEqual(
            self.mixin.trigger_mp_organize.call_count, 3,
        )

    def test_skips_items_not_in_success_set(self):
        trigger = MagicMock(return_value=(True, {"src_path": "x"}))
        object.__setattr__(self.mixin, "trigger_mp_organize", trigger)
        accepted = self.mixin._trigger_mp_organize_for_batch(
            self.items, {"ep2"}, mediainfo=MagicMock(), season=1,
        )
        self.assertEqual(accepted, {"ep2"})
        self.assertEqual(trigger.call_count, 1)

    def test_flag_off_returns_empty_without_triggering(self):
        mixin = MpOrganizeMixin(_owner(_mp_organize_after_share_save=False))
        trigger = MagicMock(return_value=(True, {"src_path": "x"}))
        object.__setattr__(mixin, "trigger_mp_organize", trigger)
        accepted = mixin._trigger_mp_organize_for_batch(
            self.items, {"ep1", "ep2", "ep3"},
            mediainfo=MagicMock(), season=1,
        )
        self.assertEqual(accepted, set())
        self.assertEqual(trigger.call_count, 0)


if __name__ == "__main__":
    unittest.main()
