"""1.33.0：漏斗入队时间窗（同集近期已入库不再重复转存）。

背景：终态 done 会释放入队去重键（`_active_filter` 只含 queued/processing/
failed），于是同一集换个来源又能再入队并真实转存——2026-11-24 实测
无声的回响 S01E07 一小时内被传了三次（4694MB + 4820MB + 272MB）。

覆盖：
1. 窗口内同集已 done → 入队返回旧行、`created=False`（调用方据此跳过）；
2. 窗口 0 → 关闭该闸门，照旧新建行；
3. 洗版候选（is_upgrade=True）不受窗口限制。

harness：复用 test_funnel_stage1_1_29_7 的真实临时 SQLite（funnel_queue
由 create_all 建表）与 `_payload` 构造器。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 与 test_funnel_hotfix_1_29_10 同款：直接复用阶段 1 的真实 SQLite 夹具。
# 注意这类夹具依赖全量套件里的模块注册顺序，单文件运行会在收集阶段
# ModuleNotFoundError（存量测试同样如此，非本次引入）。

from test_funnel_stage1_1_29_7 import _DbMixin, _payload  # noqa: E402

WINDOW = 12 * 3600


class FunnelRecentDoneWindowTest(_DbMixin):
    def test_recent_done_row_blocks_duplicate_enqueue(self):
        row, created = self.repo.enqueue(_payload())
        self.assertTrue(created)
        self.assertTrue(self.repo.mark_done(int(row["id"])))

        again, created_again = self.repo.enqueue(
            _payload(file_name="第二份.mp4", file_id="f-tv-09-b"),
            recent_done_window_seconds=WINDOW,
        )

        self.assertFalse(created_again)
        self.assertEqual(int(again["id"]), int(row["id"]))
        self.assertEqual(again["status"], "done")

    def test_window_zero_disables_the_gate(self):
        row, created = self.repo.enqueue(_payload())
        self.assertTrue(created)
        self.assertTrue(self.repo.mark_done(int(row["id"])))

        _again, created_again = self.repo.enqueue(
            _payload(file_name="第二份.mp4", file_id="f-tv-09-b"),
            recent_done_window_seconds=0,
        )

        self.assertTrue(created_again)

    def test_upgrade_candidate_bypasses_the_window(self):
        row, created = self.repo.enqueue(_payload())
        self.assertTrue(created)
        self.assertTrue(self.repo.mark_done(int(row["id"])))

        _again, created_again = self.repo.enqueue(
            _payload(
                is_upgrade=True,
                score=99,
                file_size=9_000_000_000,
                file_id="f-tv-09-4k",
            ),
            recent_done_window_seconds=WINDOW,
        )

        self.assertTrue(created_again)

    def test_list_recent_done_episodes_returns_window_episodes(self):
        """1.34.0 下载前挡：同剧同季窗口内已 done 的集号集合。"""
        row, created = self.repo.enqueue(_payload())
        self.assertTrue(created)
        self.assertTrue(self.repo.mark_done(int(row["id"])))

        episodes = self.repo.list_recent_done_episodes(
            "tv", _payload()["tmdb_id"], 1, WINDOW,
        )

        self.assertEqual(episodes, [_payload()["episode"]])

    def test_list_recent_done_episodes_window_zero_returns_empty(self):
        row, _created = self.repo.enqueue(_payload())
        self.repo.mark_done(int(row["id"]))

        self.assertEqual(
            self.repo.list_recent_done_episodes("tv", _payload()["tmdb_id"], 1, 0),
            [],
        )

    def test_list_recent_done_episodes_ignores_in_flight(self):
        self.repo.enqueue(_payload())

        self.assertEqual(
            self.repo.list_recent_done_episodes(
                "tv", _payload()["tmdb_id"], 1, WINDOW,
            ),
            [],
        )

    def test_in_flight_row_still_wins_over_done_window(self):
        """在途行优先：同集已有 queued 行时仍返回在途行（语义不变）。"""
        first, created = self.repo.enqueue(_payload())
        self.assertTrue(created)

        second, created_again = self.repo.enqueue(
            _payload(file_name="第二份.mp4", file_id="f-tv-09-b"),
            recent_done_window_seconds=WINDOW,
        )

        self.assertFalse(created_again)
        self.assertEqual(int(second["id"]), int(first["id"]))
        self.assertNotEqual(second["status"], "done")


if __name__ == "__main__":
    unittest.main()
