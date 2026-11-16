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


register_module("app.chain.storage", StorageChain=FakeStorageChain)
register_module("app.chain.transfer", TransferChain=FakeTransferChain)
register_module("app.schemas.file", FileItem=FakeFileItem)

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
        FakeStorageChain.next_result = "found"
        FakeStorageChain.last_fileitem = None
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        self.mediainfo = MagicMock()
        self.mediainfo.type = MediaType.TV
        self.mixin = MpOrganizeMixin(_owner())

    def test_flag_off_returns_false_without_calling_chain(self):
        mixin = MpOrganizeMixin(_owner(_mp_organize_after_share_save=False))
        self.assertFalse(mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        ))
        self.assertEqual(FakeTransferChain.calls, [])

    def test_non_video_file_returns_false(self):
        self.assertFalse(self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.srt", mediainfo=self.mediainfo,
        ))
        self.assertEqual(FakeTransferChain.calls, [])

    def test_unresolved_file_returns_false(self):
        FakeStorageChain.next_result = "missing"
        self.assertFalse(self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        ))
        self.assertEqual(FakeTransferChain.calls, [])

    def test_success_calls_do_transfer_background_manual(self):
        self.assertTrue(self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "美国人质 - S01E01.mkv",
            mediainfo=self.mediainfo, season=1,
        ))
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

    def test_do_transfer_rejected_returns_false(self):
        FakeTransferChain.next_result = (False, "整理失败")
        self.assertFalse(self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv", mediainfo=self.mediainfo,
        ))

    def test_media_dict_maps_mtype_and_season(self):
        self.assertTrue(self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "a.mkv",
            media={"media_type": "TV", "season": 2},
        ))
        _fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertEqual(kwargs["mtype"], MediaType.TV)
        self.assertEqual(kwargs["season"], 2)
        self.assertIsNone(kwargs["mediainfo"])


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
            MagicMock(side_effect=[True, False, True]),
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
        trigger = MagicMock(return_value=True)
        object.__setattr__(self.mixin, "trigger_mp_organize", trigger)
        accepted = self.mixin._trigger_mp_organize_for_batch(
            self.items, {"ep2"}, mediainfo=MagicMock(), season=1,
        )
        self.assertEqual(accepted, {"ep2"})
        self.assertEqual(trigger.call_count, 1)

    def test_flag_off_returns_empty_without_triggering(self):
        mixin = MpOrganizeMixin(_owner(_mp_organize_after_share_save=False))
        trigger = MagicMock(return_value=True)
        object.__setattr__(mixin, "trigger_mp_organize", trigger)
        accepted = mixin._trigger_mp_organize_for_batch(
            self.items, {"ep1", "ep2", "ep3"},
            mediainfo=MagicMock(), season=1,
        )
        self.assertEqual(accepted, set())
        self.assertEqual(trigger.call_count, 0)


if __name__ == "__main__":
    unittest.main()
