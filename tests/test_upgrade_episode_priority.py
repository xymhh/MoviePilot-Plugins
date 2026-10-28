"""1.7.101 洗版排期：缺集优先 + 每轮洗版上限。

线上问题：凡人修仙传已入库 30+ 集、只剩最新几集没补，洗版订阅的订阅任务
把「已入库全集」按集号排在前面，每集几 GB 的洗版跑完整轮才轮到补集。
"""

import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.context", "app.core.metainfo", "app.db",
    "app.db.subscribe_oper", "app.utils", "app.utils.string", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "cloudsubscribex", "cloudsubscribex.core",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification",
)
set_media_type(movie="电影", tv="电视剧")

sys.modules["cloudsubscribex.core"].OwnerDelegator = OwnerDelegator

_notification = ensure_package("cloudsubscribex.handlers.notification")
_notification.MediaServerResolver = MagicMock()
_notification.MediaServerResolver.episode_numbers = MagicMock(
    return_value=(True, set())
)
_notification.MediaServerNotifier = MagicMock()

load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)
load_module(
    "cloudsubscribex.core.services.skip_stats",
    "plugins.v2/cloudsubscribex/core/services/skip_stats.py",
)
load_module(
    "cloudsubscribex.handlers.sync.candidate_filter",
    "plugins.v2/cloudsubscribex/handlers/sync/candidate_filter.py",
)
_upgrade = load_module(
    "cloudsubscribex.handlers.sync.upgrade",
    "plugins.v2/cloudsubscribex/handlers/sync/upgrade.py",
)
plan_upgrade_episode_order = _upgrade.plan_upgrade_episode_order


class UpgradeEpisodeOrderTests(unittest.TestCase):
    def test_missing_episodes_come_first(self):
        """已入库 1-34、订阅跑到 36 集：先补 35/36，再洗 1-34。"""
        ordered, missing, postponed = plan_upgrade_episode_order(
            local_episodes=range(1, 35),
            all_expected_episodes=set(range(1, 37)),
            target_episodes=None,
        )
        self.assertEqual(ordered[:2], [35, 36])
        self.assertEqual(sorted(ordered[2:]), list(range(1, 35)))
        self.assertEqual(missing, {35, 36})
        self.assertEqual(postponed, [])

    def test_limit_only_caps_upgrade_part(self):
        """上限只截断洗版，缺集永远全额处理。"""
        ordered, missing, postponed = plan_upgrade_episode_order(
            local_episodes=range(1, 35),
            all_expected_episodes=set(range(1, 37)),
            target_episodes=None,
            limit=3,
        )
        self.assertEqual(missing, {35, 36})
        self.assertEqual(ordered[:2], [35, 36])
        self.assertEqual(ordered[2:], [1, 2, 3])
        self.assertEqual(postponed[:3], [4, 5, 6])
        self.assertEqual(len(postponed), 31)

    def test_manual_target_episodes_not_capped(self):
        """手动指定集数（多为补集请求）全部优先，且不受上限约束。"""
        ordered, missing, postponed = plan_upgrade_episode_order(
            local_episodes=range(1, 11),
            all_expected_episodes=set(range(1, 11)),
            target_episodes={3, 4, 5},
            limit=1,
        )
        self.assertEqual(ordered, [3, 4, 5])
        self.assertEqual(missing, {3, 4, 5})
        self.assertEqual(postponed, [])

    def test_unknown_total_still_sorted(self):
        """总集数未知（连载未标 total）：没有缺集概念，按集号排序且受上限约束。"""
        ordered, missing, _ = plan_upgrade_episode_order(
            local_episodes=[5, 3, 9],
            all_expected_episodes=set(),
            target_episodes=None,
            limit=2,
        )
        self.assertEqual(ordered, [3, 5])
        self.assertEqual(missing, set())


if __name__ == "__main__":
    unittest.main()
