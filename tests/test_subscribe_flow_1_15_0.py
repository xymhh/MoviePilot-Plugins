"""1.15.0 Phase 3a 第 3 项：订阅入口公共流程（SubscribeFlowService）。

TV 与电影入口原来各写一遍的开场（停止检查 + 积分预算键 + mediainfo
解析）与资源搜索（手动直进 / 启用源搜索 + 进度回调）收进本组件。
断言：
- 停止检查：停了记原因返回 True，没停清原因返回 False；
- 开场准备：返回 (track_points, sub_key, mediainfo)，手动不重置预算，
  mediainfo 解析不到返回 None；
- 查资源：手动直接进且不调搜索；TV 搜索带 season/target_episodes/
  target_episode_air_dates 且 get_enabled_sources 不传 media_type；
  电影搜索传 media_type=MediaType.MOVIE 且不带季集参数；没源时返回
  ([], {}) 且不调 search_sources；进度回调按 45%→65% 推阶段进度。
"""
import os
import sys
import unittest
from types import SimpleNamespace

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.context", "app.log", "app.schemas", "app.schemas.types",
)
set_media_type(movie="电影", tv="电视剧")
register_module("app.schemas", MediaInfo=SimpleNamespace)

install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
)
ensure_package("cloudsubscribex.core").OwnerDelegator = OwnerDelegator

_flow_module = sys.modules.get("cloudsubscribex.handlers.sync.subscribe_flow")
if _flow_module is None:
    _flow_module = load_module(
        "cloudsubscribex.handlers.sync.subscribe_flow",
        "plugins.v2/cloudsubscribex/handlers/sync/subscribe_flow.py",
    )
SubscribeFlowService = _flow_module.SubscribeFlowService
# 跨文件 set_media_type() 每次都建一个新 MediaType 类；服务模块在加载时
# 绑定的类未必是本文件 import 到的那个——测试里用的枚举一律以服务模块
# 的绑定为准，否则 media_type == MediaType.MOVIE 这类比较会跨类失效。
MediaType = _flow_module.MediaType


class _FlowService(SubscribeFlowService):
    """只测公共流程：停止/预算/mediainfo/搜索源全部桩掉。"""

    def __init__(
        self, owner=None, stopped=False, mediainfo=None,
        enabled_sources=None, search_results=None,
    ):
        super().__init__(
            owner or SimpleNamespace(_last_skip_reason="旧原因")
        )
        self._stopped = stopped
        self._mediainfo = mediainfo
        self._enabled_sources = enabled_sources
        self._search_results = search_results or {}
        self.budget_keys = []
        self.reset_calls = []
        self.mediainfo_calls = []
        self.phase_calls = []
        self.enabled_calls = []
        self.search_calls = []

    def _stop_requested(self):
        return self._stopped

    def subscription_budget_key(self, subscribe, media_type):
        key = f"k-{media_type.value}"
        self.budget_keys.append((subscribe, media_type))
        return key

    def _subscribe_mediainfo(self, subscribe, media_type, **kwargs):
        self.mediainfo_calls.append((subscribe, media_type, dict(kwargs)))
        return self._mediainfo

    def _set_task_phase(self, subscribe, phase, progress, **kwargs):
        self.phase_calls.append((phase, progress, dict(kwargs)))

    @property
    def _search_handler(self):
        return SimpleNamespace(
            reset_subscription_budgets=self.reset_calls.append,
            get_enabled_sources=self._get_enabled_sources,
            search_sources=self._search_sources,
        )

    def _get_enabled_sources(self, **kwargs):
        self.enabled_calls.append(dict(kwargs))
        return list(self._enabled_sources or [])

    def _search_sources(self, **kwargs):
        self.search_calls.append(dict(kwargs))
        return dict(self._search_results)


def _subscribe():
    return SimpleNamespace(name="玫瑰战争", id=1, season=1)


def _mediainfo():
    return SimpleNamespace(title="玫瑰战争", tmdb_id=299276)


class CheckStoppedTests(unittest.TestCase):
    def test_stopped_sets_reason_and_returns_true(self):
        service = _FlowService(stopped=True)
        self.assertTrue(service.check_subscribe_stopped())
        self.assertEqual(service._owner._last_skip_reason, "已手动停止")

    def test_not_stopped_clears_reason_and_returns_false(self):
        service = _FlowService(stopped=False)
        self.assertFalse(service.check_subscribe_stopped())
        self.assertEqual(service._owner._last_skip_reason, "")


class PrepareEntryTests(unittest.TestCase):
    def test_returns_tuple_and_resets_budget(self):
        """自动订阅：track_points=True，预算重置，mediainfo_kwargs 透传。"""
        service = _FlowService(mediainfo=_mediainfo())
        prepared = service.prepare_subscribe_entry(
            _subscribe(), MediaType.TV, None,
            mediainfo_kwargs={"cache": False},
        )
        self.assertIsNotNone(prepared)
        track_points, sub_key, mediainfo = prepared
        self.assertTrue(track_points)
        # sub_key 由 media_type.value 拼出，具体值取决于加载时绑定的
        # MediaType 类（跨文件每次 set_media_type 都换类），只断言
        # 预算重置用的就是返回的那个 key。
        self.assertEqual(service.reset_calls, [sub_key])
        self.assertTrue(sub_key.startswith("k-"))
        self.assertIs(mediainfo, service._mediainfo)
        self.assertEqual(
            service.mediainfo_calls[0][2], {"cache": False}
        )

    def test_manual_skips_budget_reset(self):
        """手动资源：track_points=False，不重置预算。"""
        service = _FlowService(mediainfo=_mediainfo())
        prepared = service.prepare_subscribe_entry(
            _subscribe(), MediaType.MOVIE,
            manual_resources=[{"url": "https://115.com/s/x"}],
        )
        self.assertIsNotNone(prepared)
        track_points, sub_key, _ = prepared
        self.assertFalse(track_points)
        self.assertEqual(service.reset_calls, [])

    def test_mediainfo_none_returns_none(self):
        """解析不到 mediainfo：返回 None，记原因。"""
        service = _FlowService(mediainfo=None)
        self.assertIsNone(
            service.prepare_subscribe_entry(_subscribe(), MediaType.TV, None)
        )
        self.assertEqual(
            service._owner._last_skip_reason, "无法识别媒体信息"
        )


class CollectResourcesTests(unittest.TestCase):
    def test_manual_returns_manual_batch_without_search(self):
        """手动资源直接进：不查启用源、不调搜索、阶段文案是手动。"""
        resource = {"url": "https://115.com/s/x", "title": "玫瑰战争"}
        service = _FlowService()
        order, results = service.collect_subscribe_resources(
            _subscribe(), _mediainfo(), MediaType.TV,
            manual_resources=[resource],
            phase_label="搜索缺失剧集", phase_progress=55,
        )
        self.assertEqual(order, ["manual"])
        self.assertEqual(results, {"manual": [resource]})
        self.assertIsNot(results["manual"][0], resource)  # 复制过，不是同一对象
        self.assertEqual(service.enabled_calls, [])
        self.assertEqual(service.search_calls, [])
        self.assertEqual(service.phase_calls[0][:2], ("处理手动网盘资源", 55))

    def test_tv_search_passes_season_and_episodes(self):
        """TV 搜索：带 season/target_episodes/日期，get_enabled_sources
        不传 media_type（旧口径）。"""
        service = _FlowService(
            enabled_sources=["pansou"],
            search_results={"pansou": [{"url": "https://115.com/s/a"}]},
        )
        order, results = service.collect_subscribe_resources(
            _subscribe(), _mediainfo(), MediaType.TV,
            season=1, target_episodes=[24, 25],
            target_episode_air_dates={24: "2026-01-01"},
            phase_label="搜索缺失剧集", phase_progress=55,
        )
        self.assertEqual(order, ["pansou"])
        self.assertEqual(results, {"pansou": [{"url": "https://115.com/s/a"}]})
        self.assertEqual(service.enabled_calls, [{}])
        self.assertEqual(len(service.search_calls), 1)
        call = service.search_calls[0]
        self.assertEqual(call["season"], 1)
        self.assertEqual(call["target_episodes"], [24, 25])
        self.assertEqual(
            call["target_episode_air_dates"], {24: "2026-01-01"}
        )
        self.assertEqual(call["media_type"], MediaType.TV)
        self.assertEqual(service.phase_calls[0][:2], ("搜索缺失剧集", 55))

    def test_movie_search_passes_media_type_without_season(self):
        """电影搜索：get_enabled_sources 传 media_type，search_sources
        不带季集参数。"""
        service = _FlowService(
            enabled_sources=["pansou"], search_results={"pansou": []},
        )
        order, _ = service.collect_subscribe_resources(
            _subscribe(), _mediainfo(), MediaType.MOVIE,
            phase_label="搜索候选资源", phase_progress=45,
        )
        self.assertEqual(order, ["pansou"])
        self.assertEqual(
            service.enabled_calls, [{"media_type": MediaType.MOVIE}]
        )
        call = service.search_calls[0]
        self.assertEqual(call["media_type"], MediaType.MOVIE)
        self.assertNotIn("season", call)
        self.assertNotIn("target_episodes", call)
        self.assertNotIn("target_episode_air_dates", call)

    def test_no_sources_returns_empty_without_search(self):
        """没启用源：返回 ([], {})，不调 search_sources。"""
        service = _FlowService(enabled_sources=[])
        self.assertEqual(
            service.collect_subscribe_resources(
                _subscribe(), _mediainfo(), MediaType.TV,
                phase_label="搜索缺失剧集", phase_progress=55,
            ),
            ([], {}),
        )
        self.assertEqual(service.search_calls, [])

    def test_progress_callback_updates_phase(self):
        """进度回调：45%→65%，无 summary 时用 phase_label，有则用 summary。"""
        service = _FlowService(
            enabled_sources=["pansou"], search_results={"pansou": []},
        )
        service.collect_subscribe_resources(
            _subscribe(), _mediainfo(), MediaType.TV,
            phase_label="搜索缺失剧集", phase_progress=55,
        )
        callback = service.search_calls[0]["progress_callback"]
        callback({
            "completed": 1, "total": 2, "summary": "",
            "active": True, "channels": ["pansou"], "total_results": 5,
        })
        self.assertEqual(
            service.phase_calls[-1],
            ("搜索缺失剧集", 55, {
                "search_active": True,
                "search_channels": ["pansou"],
                "search_total_results": 5,
            }),
        )
        callback({
            "completed": 2, "total": 2, "summary": "自定义进度",
            "active": False, "channels": [], "total_results": 9,
        })
        self.assertEqual(service.phase_calls[-1][0], "自定义进度")
        self.assertEqual(service.phase_calls[-1][1], 65)


if __name__ == "__main__":
    unittest.main()
