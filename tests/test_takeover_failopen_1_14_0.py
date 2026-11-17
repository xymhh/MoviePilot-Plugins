"""接管失败不取消 MP 下载（1.14.0，P3 缺口 2）。

回归：policy=cloud 时，`on_resource_download` 的接管分支在
`_takeover_platform_download` 返回 False（或抛异常）时仍然把
`event_data.cancel` 置 True，原因文案甚至写着“平台资源下载接管
失败，已阻止平台下载”。这一集既没进 115 离线、又被拦下，只能等
下一轮订阅搜索；失败若是系统性的（如下游取不到种子元数据），
就永远到不了库。

固定契约（只改 cloud 分支，block/allow 行为不变）：
1. 接管成功 → 取消 MP 下载，source 为“CloudSubscribe-平台资源
   下载接管”；
2. 接管返回 False → 不取消，让 MP 自己下载；
3. 接管抛异常 → 不取消，让 MP 自己下载；
4. 无法匹配季集（TV 且无 episode_list）→ 不尝试接管，也不取消。
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

PLATFORM_TORRENT = "https://tracker.example.com/abc123.torrent"


def _make_event(episodes=(1, 2, 3, 4)):
    """构造一次 ResourceDownload 事件：已订阅剧集 S01E01-E04 的下载。"""
    torrent = SimpleNamespace(
        title="虚无的十字架 虚ろな十字架 2026 1-4",
        enclosure=PLATFORM_TORRENT,
    )
    media = SimpleNamespace(tmdb_id=326140, type="电视剧")
    meta = SimpleNamespace(season_list=[1], episode_list=list(episodes))
    context = SimpleNamespace(
        torrent_info=torrent, media_info=media, meta_info=meta
    )
    event_data = SimpleNamespace(
        context=context,
        episodes=list(episodes),
        channel=None,
        origin=None,
        downloader=None,
        options={},
        cancel=False,
        source="",
        reason="",
    )
    return SimpleNamespace(event_data=event_data)


class TakeoverFailOpenTest(unittest.TestCase):
    def setUp(self):
        self.sub = SimpleNamespace(id=35, name="空洞的十字架")
        events.list_subscribes_by_tmdb_id = (
            lambda oper, tmdbid, season: [self.sub]
        )
        self.handler = events.PluginEventHandler()
        self.handler._enabled = True
        self.handler._sync_handler = MagicMock()
        self.handler._platform_download_policy = "cloud"
        self.handler._is_takeover_active = lambda: True
        self.handler._is_subscribe_excluded = lambda sid: False
        self.handler._has_pending_cloud_target = lambda *a, **k: False

    def test_takeover_success_cancels_download(self):
        self.handler._takeover_platform_download = lambda **kwargs: True
        event = _make_event()
        self.handler.on_resource_download(event)
        self.assertTrue(event.event_data.cancel)
        self.assertEqual(
            event.event_data.source, "CloudSubscribe-平台资源下载接管"
        )
        self.assertEqual(
            event.event_data.reason, "平台资源已提交插件离线下载"
        )

    def test_takeover_failure_does_not_cancel_download(self):
        self.handler._takeover_platform_download = lambda **kwargs: False
        event = _make_event()
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)
        self.assertEqual(event.event_data.source, "")
        self.assertEqual(event.event_data.reason, "")

    def test_takeover_exception_does_not_cancel_download(self):
        def _boom(**kwargs):
            raise RuntimeError("下游取不到种子元数据")

        self.handler._takeover_platform_download = _boom
        event = _make_event()
        self.handler.on_resource_download(event)
        self.assertFalse(event.event_data.cancel)

    def test_takeover_not_attempted_without_episodes(self):
        called = []
        self.handler._takeover_platform_download = (
            lambda **kwargs: called.append(True) or True
        )
        event = _make_event(episodes=())
        self.handler.on_resource_download(event)
        self.assertEqual(called, [])
        self.assertFalse(event.event_data.cancel)


if __name__ == "__main__":
    unittest.main()
