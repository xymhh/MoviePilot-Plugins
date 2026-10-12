import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, set_media_type

mock_logger = MagicMock()


class FakeTorrentInfo:
    """最小可用的 TorrentInfo 桩。"""

    model_fields = {"site_name": None, "title": None, "enclosure": None, "media_id": None}

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


register_mock("app")
register_mock("app.log", logger=mock_logger)
register_mock("app.core.event", Event=MagicMock())
register_mock("app.db.subscribe_oper", SubscribeOper=MagicMock())
set_media_type("movie", "tv")
register_mock("app.schemas", Notification=MagicMock(), TorrentInfo=FakeTorrentInfo)
register_mock("app.schemas.event", ResourceDownloadEventData=MagicMock())
register_mock("torf", Torrent=MagicMock(), TorfError=Exception)
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock(
    "cloudsubscribe.core.media",
    coerce_media_source=lambda value: value,
    list_subscribes_by_tmdb_id=MagicMock(return_value=[]),
)
register_mock("cloudsubscribe.core.hook")
register_mock(
    "cloudsubscribe.search.types",
    resource_type_from_url=MagicMock(return_value="115"),
    resource_type_name=MagicMock(return_value="115网盘"),
)

load_module(
    "cloudsubscribe.core.hook.mp_module",
    "plugins.v2/cloudsubscribe/core/hook/mp_module.py",
)
PluginEventHandler = load_module(
    "cloudsubscribe.core.hook.events",
    "plugins.v2/cloudsubscribe/core/hook/events.py",
).PluginEventHandler


class FakeEventData:
    """ResourceDownloadEventData 桩：只保留接管逻辑用到的字段。"""

    def __init__(self, torrent, media=None, meta=None):
        self.context = SimpleNamespace(
            torrent_info=torrent,
            media_info=media,
            meta_info=meta,
        )
        self.cancel = False
        self.source = None
        self.reason = None
        self.options = {}


def cloud_torrent(**overrides):
    fields = {
        "site_name": "网盘订阅助手｜pansou",
        "title": "环形使者 2012 1080p",
        "enclosure": "https://115.com/s/abcdef",
        "media_id": "59967",
    }
    fields.update(overrides)
    return FakeTorrentInfo(**fields)


def platform_torrent():
    return FakeTorrentInfo(
        site_name="馒头",
        title="环形使者 2012 1080p",
        enclosure="https://pt.example/download/1",
        media_id=None,
    )


class TestModuleResourceDownloadHandoff(unittest.TestCase):
    """平台搜索里选中的网盘候选，必须由插件转存且拦截平台下载。"""

    def setUp(self):
        self.owner = MagicMock()
        self.owner._enabled = True
        self.owner._sync_handler = object()
        self.owner.submit_platform_links = MagicMock(return_value={"success": True})
        self.handler = PluginEventHandler(self.owner)

    def test_cloud_resource_is_submitted_and_platform_download_cancelled(self):
        media = SimpleNamespace(title="环形使者", type="电影", tmdb_id=59967)
        meta = SimpleNamespace(season_list=[])
        event_data = FakeEventData(cloud_torrent(), media=media, meta=meta)

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        self.assertTrue(event_data.cancel)
        self.owner.submit_platform_links.assert_called_once()
        kwargs = self.owner.submit_platform_links.call_args.kwargs
        self.assertEqual(kwargs["resource_links"], ["https://115.com/s/abcdef"])
        self.assertEqual(kwargs["title"], "环形使者")
        self.assertEqual(kwargs["media_type"], "movie")
        self.assertEqual(kwargs["tmdb_id"], 59967)
        self.assertIn("已提交", event_data.reason)

    def test_pt_resource_is_not_taken_over(self):
        media = SimpleNamespace(title="环形使者", type="电影", tmdb_id=59967)
        meta = SimpleNamespace(season_list=[])
        event_data = FakeEventData(platform_torrent(), media=media, meta=meta)

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        self.assertFalse(event_data.cancel)
        self.owner.submit_platform_links.assert_not_called()

    def test_missing_link_still_cancels_platform_download(self):
        event_data = FakeEventData(cloud_torrent(enclosure=""))

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        self.assertTrue(event_data.cancel)
        self.owner.submit_platform_links.assert_not_called()
        self.assertIn("缺少可用链接", event_data.reason)

    def test_transfer_failure_notifies_user_and_cancels(self):
        self.owner.submit_platform_links = MagicMock(
            return_value={"success": False, "message": "无法识别媒体"}
        )
        media = SimpleNamespace(title="环形使者", type="电影", tmdb_id=None)
        meta = SimpleNamespace(season_list=[])
        event_data = FakeEventData(cloud_torrent(media_id=None), media=media, meta=meta)

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        self.assertTrue(event_data.cancel)
        self.assertIn("无法识别媒体", event_data.reason)
        self.owner.post_message.assert_called_once()

    def test_transfer_exception_is_swallowed(self):
        self.owner.submit_platform_links = MagicMock(side_effect=RuntimeError("boom"))
        media = SimpleNamespace(title="环形使者", type="电影", tmdb_id=59967)
        meta = SimpleNamespace(season_list=[])
        event_data = FakeEventData(cloud_torrent(), media=media, meta=meta)

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        self.assertTrue(event_data.cancel)
        self.owner.post_message.assert_called_once()

    def test_tv_handoff_passes_seasons(self):
        media = SimpleNamespace(title="仙逆", type="电视剧", tmdb_id=223911)
        meta = SimpleNamespace(season_list=[2])
        event_data = FakeEventData(cloud_torrent(), media=media, meta=meta)

        self.handler.on_resource_download(SimpleNamespace(event_data=event_data))

        kwargs = self.owner.submit_platform_links.call_args.kwargs
        self.assertEqual(kwargs["media_type"], "tv")
        self.assertEqual(kwargs["seasons"], [2])


if __name__ == "__main__":
    unittest.main()
