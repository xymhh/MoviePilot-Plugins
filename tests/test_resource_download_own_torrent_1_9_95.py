"""ResourceDownload 拦截放行插件自有种子（1.9.95）。

回归：空洞的十字架（TMDB 326140）已订阅、策略 block、接管恒激活时，
MP 搜索页点插件 PanSou 结果下载一直报“任务添加失败”——插件自己的
ResourceDownload 拦截器把自有合成种子也当成平台资源取消了
（MP 只回“任务添加失败”，取消只记 DEBUG 日志）。

固定契约：
1. enclosure 指向本插件 /torrent 接口的种子一律放行，不论策略是
   block/cloud，还是 allow 下已有同季集网盘任务在跑；
2. 平台资源（PT/站点/RSS）按原策略：block 恒拦截、allow 只在同季集
   已有网盘任务在跑时拦截；
3. enclosure 为空/磁力链/普通种子链接时按平台资源处理。
"""

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_mock,
    set_media_type,
)


class _TorfError(Exception):
    pass


install_app_mocks(
    "app.core.event",
    "app.db.subscribe_oper",
    "app.schemas.event",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.core.hook",
    "cloudsubscribex.core.media",
    "cloudsubscribex.search",
    "cloudsubscribex.search.types",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
register_mock("torf", Torrent=MagicMock(), TorfError=_TorfError)

events = load_module(
    "cloudsubscribex.core.hook.events",
    "plugins.v2/cloudsubscribex/core/hook/events.py",
)

PLUGIN_TORRENT = (
    "http://127.0.0.1:3000/api/v1/plugin/CloudSubscribeX/torrent"
    "?sid=c8a055db0d9d54bd&sig=8674f2a1e5d189736e5a1d1a0f204ddf"
)
PLATFORM_TORRENT = "https://tracker.example.com/abc123.torrent"


def _make_event(enclosure):
    """构造一次 ResourceDownload 事件：已订阅剧集 S01E01-E04 的下载。"""
    torrent = SimpleNamespace(
        title="虚无的十字架 虚ろな十字架 2026 1-4",
        enclosure=enclosure,
    )
    media = SimpleNamespace(tmdb_id=326140, type="电视剧")
    meta = SimpleNamespace(season_list=[1], episode_list=[1, 2, 3, 4])
    context = SimpleNamespace(
        torrent_info=torrent, media_info=media, meta_info=meta
    )
    event_data = SimpleNamespace(
        context=context,
        episodes=[1, 2, 3, 4],
        channel=None,
        origin=None,
        downloader=None,
        options={},
        cancel=False,
        source="",
        reason="",
    )
    return SimpleNamespace(event_data=event_data)


class OwnTorrentBypassTest(unittest.TestCase):
    def setUp(self):
        self.sub = SimpleNamespace(id=35, name="空洞的十字架")
        events.list_subscribes_by_tmdb_id = (
            lambda oper, tmdbid, season: [self.sub]
        )
        self.handler = events.PluginEventHandler()
        self.handler._enabled = True
        self.handler._sync_handler = MagicMock()
        self.handler._platform_download_policy = "block"
        self.handler._is_takeover_active = lambda: True
        self.handler._is_subscribe_excluded = lambda sid: False
        self.handler._has_pending_cloud_target = lambda *a, **k: False

    def test_own_torrent_bypassed_under_block_policy(self):
        event = _make_event(PLUGIN_TORRENT)
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)

    def test_own_torrent_bypassed_even_with_pending_target(self):
        self.handler._platform_download_policy = "allow"
        self.handler._has_pending_cloud_target = lambda *a, **k: True
        event = _make_event(PLUGIN_TORRENT)
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)

    def test_own_torrent_bypassed_with_public_base_url(self):
        event = _make_event(
            "https://mp.xymhh.eu.org/api/v1/plugin/CloudSubscribeX/torrent"
            "?sid=c8a055db0d9d54bd&sig=deadbeef"
        )
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)

    def test_platform_torrent_blocked_under_block_policy(self):
        event = _make_event(PLATFORM_TORRENT)
        self.handler.on_resource_download(event)
        self.assertTrue(event.event_data.cancel)
        self.assertEqual(
            event.event_data.source, "CloudSubscribe-平台资源下载拦截"
        )
        self.assertIn("已拦截平台资源下载", event.event_data.reason)

    def test_platform_torrent_allowed_when_no_pending(self):
        self.handler._platform_download_policy = "allow"
        event = _make_event(PLATFORM_TORRENT)
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)

    def test_platform_torrent_blocked_when_pending_under_allow(self):
        self.handler._platform_download_policy = "allow"
        self.handler._has_pending_cloud_target = lambda *a, **k: True
        event = _make_event(PLATFORM_TORRENT)
        self.handler.on_resource_download(event)
        self.assertTrue(event.event_data.cancel)
        self.assertEqual(
            event.event_data.source, "CloudSubscribe-重复资源拦截"
        )

    def test_empty_enclosure_treated_as_platform_resource(self):
        event = _make_event("")
        self.handler.on_resource_download(event)
        self.assertTrue(event.event_data.cancel)


class IsOwnPluginTorrentTest(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure=PLUGIN_TORRENT)))
        self.assertTrue(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure=(
                "https://mp.xymhh.eu.org/api/v1/plugin/CloudSubscribeX"
                "/torrent?sid=x&sig=y"))))
        self.assertFalse(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure=PLATFORM_TORRENT)))
        self.assertFalse(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure="magnet:?xt=urn:btih:deadbeef")))
        self.assertFalse(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure="")))
        self.assertFalse(events._is_own_plugin_torrent(
            SimpleNamespace(enclosure=None)))
        self.assertFalse(events._is_own_plugin_torrent(SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
