"""Bangumi 总集数交叉核对单测。

回归上游修复：Bangumi 条目的 ``total_episodes`` 含 OP/ED/SP，若订阅总集数抄了这个
数字，TMDB S01 会一直算出「缺 E13-E15」这类虚假缺集，导致反复搜索无效资源。
只有三重相等（TMDB 已播最大集号 == BGM 正片 eps < BGM 总条目 == 订阅总集数）才修正。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

register_module("app", package=True)
register_module("app.core", package=True)
app_core = register_module("app.core", package=True)
app_core.config = register_mock(
    "app.core.config", global_vars=MagicMock(), settings=MagicMock()
)
app_core.context = register_mock("app.core.context")
app_core.metainfo = register_mock("app.core.metainfo")

register_module("app.db", package=True, SessionFactory=MagicMock())
subscribe_oper_module = register_mock("app.db.subscribe_oper")
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_mock("app.schemas.types")
register_module("app.utils", package=True)
register_mock("app.utils.http")
bangumi_module = register_mock("app.modules.bangumi")

register_module("cloudsubscribex", package=True)
register_module(
    "cloudsubscribex.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
)
media_module = register_module(
    "cloudsubscribex.core.media",
    apply_media_identity=MagicMock(),
    legacy_media_ids=MagicMock(),
    media_identity=MagicMock(return_value=("themoviedb", "100")),
    recognize_media=MagicMock(),
    search_medias=MagicMock(),
    tmdb_id_of=MagicMock(),
    tmdb_identity_update=MagicMock(),
)
register_module("cloudsubscribex.handlers", package=True)
register_module("cloudsubscribex.handlers.sync", package=True)
register_module(
    "cloudsubscribex.handlers.sync.utils",
    normalize_season=lambda value, default=1: int(value or default),
)
register_module("cloudsubscribex.utils", package=True)
register_mock(
    "cloudsubscribex.utils.cache", normalize_platform_cache_key=MagicMock()
)

SyncMetadataService = load_module(
    "cloudsubscribex.handlers.sync.metadata",
    "plugins.v2/cloudsubscribex/handlers/sync/metadata.py",
).SyncMetadataService


def _subscribe(**kwargs):
    base = {
        "id": 7, "name": "某番剧", "bangumiid": 555,
        "season": 1, "total_episode": 15,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


def _mediainfo(episodes, source="themoviedb"):
    media_module.media_identity.return_value = (source, "100")
    return SimpleNamespace(seasons={1: list(episodes)})


class CorrectBangumiEpisodeTotalTest(unittest.TestCase):
    def setUp(self):
        self.service = SyncMetadataService(owner=MagicMock())
        self.update = MagicMock(return_value=True)
        subscribe_oper_module.SubscribeOper.return_value.update = self.update
        bangumi_module.BangumiModule.return_value.bangumi_info = MagicMock(
            return_value={"eps": 12, "total_episodes": 15}
        )

    def test_mixed_total_is_corrected(self):
        subscribe = _subscribe()
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_called_once_with(7, {"total_episode": 12})
        self.assertEqual(subscribe.total_episode, 12)

    def test_non_contiguous_episodes_untouched(self):
        """TMDB 集号不连续（如缺 E5）说明不是纯正片序列，保持原配置。"""
        subscribe = _subscribe()
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo([1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12])
        )
        self.update.assert_not_called()
        self.assertEqual(subscribe.total_episode, 15)

    def test_season_not_one_untouched(self):
        subscribe = _subscribe(season=2)
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_not_called()

    def test_non_tmdb_source_untouched(self):
        subscribe = _subscribe()
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13), source="bangumi")
        )
        self.update.assert_not_called()

    def test_total_already_main_untouched(self):
        """订阅总集数已经等于正片集数，无需修正也不应查 BGM。"""
        subscribe = _subscribe(total_episode=12)
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_not_called()
        bangumi_module.BangumiModule.return_value.bangumi_info.assert_not_called()

    def test_bgm_main_not_equal_untouched(self):
        """BGM 正片集数与 TMDB 已播最大集号不一致时不动（可能停更/加更）。"""
        bangumi_module.BangumiModule.return_value.bangumi_info = MagicMock(
            return_value={"eps": 11, "total_episodes": 15}
        )
        subscribe = _subscribe()
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_not_called()

    def test_bgm_api_error_swallowed(self):
        bangumi_module.BangumiModule.return_value.bangumi_info = MagicMock(
            side_effect=RuntimeError("bangumi down")
        )
        subscribe = _subscribe()
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_not_called()
        self.assertEqual(subscribe.total_episode, 15)

    def test_no_bangumi_id_untouched(self):
        subscribe = _subscribe(bangumiid=None)
        self.service._correct_bangumi_episode_total(
            subscribe, _mediainfo(range(1, 13))
        )
        self.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
