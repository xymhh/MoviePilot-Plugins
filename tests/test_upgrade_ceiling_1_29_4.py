"""v1.29.4 第 3 步：质量上限筛掉不搜。

上限定义（见 ``handlers/sync/upgrade.py`` 的 ``QUALITY_CEILING_SCORE``）：
MP v3.2.6 ``app/modules/filter/__init__.py`` 的 ``FilterModule.__get_order``
规定“首层优先级为 100，后续每层减 1；数值越大越优先”，规则串按 ``>``
分层、首个命中的层决定评分，因此规则评分最高只能到 100。基线里评分
已达 100 的集在评分上不可能被超越，洗版搜索前直接跳过、省请求。

锁定：已达上限的洗版候选不进搜索、不进 postponed；未达上限的照常；
缺集与手动目标集不受上限影响；ceiling=0（或不传 scores）时行为与旧版
一致。
"""

import unittest

from test_upgrade_episode_priority import plan_upgrade_episode_order
from test_upgrade_episode_priority import _upgrade


class UpgradeCeilingTests(unittest.TestCase):
    def test_ceiling_value_is_100(self):
        self.assertEqual(_upgrade.QUALITY_CEILING_SCORE, 100)

    def test_at_ceiling_skipped_not_postponed(self):
        """已达上限的洗版候选：不进搜索、不进 postponed，单独记在 ceiling_skipped。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2, 3, 4],
            all_expected_episodes={1, 2, 3, 4, 5},
            target_episodes=None,
            scores={1: 100, 2: 99, 3: 100, 4: 50},
            ceiling=100,
        )
        self.assertEqual(missing, {5})
        self.assertEqual(ordered, [5, 2, 4])
        self.assertEqual(postponed, [])
        self.assertEqual(ceiling_skipped, {1, 3})

    def test_below_ceiling_searched_normally(self):
        """未达上限的照常进搜索；缺集仍在最前。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2],
            all_expected_episodes={1, 2, 3},
            target_episodes=None,
            scores={1: 99, 2: 0},
            ceiling=100,
        )
        self.assertEqual(ordered, [3, 1, 2])
        self.assertEqual(missing, {3})
        self.assertEqual(ceiling_skipped, set())

    def test_ceiling_zero_preserves_old_behavior(self):
        """ceiling=0 时不筛：与旧版一致，全部洗版候选都进搜索。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2, 3, 4],
            all_expected_episodes={1, 2, 3, 4, 5},
            target_episodes=None,
            scores={1: 100, 2: 99, 3: 100, 4: 50},
            ceiling=0,
        )
        self.assertEqual(ordered, [5, 1, 2, 3, 4])
        self.assertEqual(ceiling_skipped, set())

    def test_no_scores_preserves_old_behavior(self):
        """不传 scores 时不筛：与旧版一致。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2],
            all_expected_episodes={1, 2, 3},
            target_episodes=None,
            ceiling=100,
        )
        self.assertEqual(ordered, [3, 1, 2])
        self.assertEqual(ceiling_skipped, set())

    def test_manual_target_at_ceiling_not_skipped(self):
        """手动目标集不受上限影响：即使评分已达 100 也照常进搜索。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2, 3],
            all_expected_episodes={1, 2, 3},
            target_episodes={1, 2},
            scores={1: 100, 2: 100, 3: 100},
            ceiling=100,
        )
        self.assertEqual(ordered, [1, 2])
        self.assertEqual(missing, {1, 2})
        self.assertEqual(ceiling_skipped, set())

    def test_ceiling_skipped_do_not_consume_limit(self):
        """已达上限的集不占每轮洗版上限：上限只截断真正要搜的洗版候选。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[1, 2, 3, 4],
            all_expected_episodes=set(),
            target_episodes=None,
            limit=2,
            scores={1: 100, 2: 50, 3: 60, 4: 70},
            ceiling=100,
        )
        self.assertEqual(ordered, [2, 3])
        self.assertEqual(postponed, [4])
        self.assertEqual(ceiling_skipped, {1})

    def test_unknown_total_ceiling_applies(self):
        """总集数未知时：没有缺集概念，已达上限的本地集仍跳过。"""
        ordered, missing, postponed, ceiling_skipped = plan_upgrade_episode_order(
            local_episodes=[5, 3, 9],
            all_expected_episodes=set(),
            target_episodes=None,
            scores={3: 100, 5: 20, 9: 30},
            ceiling=100,
        )
        self.assertEqual(ordered, [5, 9])
        self.assertEqual(missing, set())
        self.assertEqual(ceiling_skipped, {3})


if __name__ == "__main__":
    unittest.main()
