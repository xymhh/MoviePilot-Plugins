"""通知体积校正：按历史台账逐集累加真实写入大小。

线上现象：13 集洗版转存完成通知写「合计 2.87GB」，而 115 目录里这 13 集
实写 46.75GB，2.87GB 恰好是其中一集的体积。这里固定「以历史台账为准」的契约。
"""

import os
import sys

from plugin_env import load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

notify_size = load_module(
    "cloudsubscribex.handlers.sync.notify_size",
    "plugins.v2/cloudsubscribex/handlers/sync/notify_size.py",
)


def _record(episode, size, status="成功", time="2026-10-28 15:48:18"):
    return {
        "title": "玉簟秋", "season": 1, "episode": episode, "file_size": size,
        "status": status, "time": time,
    }


def _detail(episodes, size):
    return {"type": "电视剧", "title": "玉簟秋", "season": 1,
            "episodes": episodes, "size": size}


def test_batch_size_is_summed_from_history():
    records = [
        _record(14, 2_738_000_000), _record(18, 5_860_000_000),
        _record(19, 6_100_000_000),
    ]
    detail = _detail([14, 18, 19], 3_085_704_678)
    assert notify_size.apply_history_size(detail, records) == (
        2_738_000_000 + 5_860_000_000 + 6_100_000_000
    )
    assert detail["size"] == 2_738_000_000 + 5_860_000_000 + 6_100_000_000


def test_failed_record_is_ignored():
    records = [_record(14, 2_738_000_000), _record(26, 2_851_472_089, status="失败")]
    detail = _detail([14, 26], 0)
    assert notify_size.apply_history_size(detail, records) == 2_738_000_000


def test_history_never_shrinks_reported_size():
    records = [_record(14, 100)]
    detail = _detail([14], 5_000)
    assert notify_size.apply_history_size(detail, records) == 5_000


def test_latest_record_per_episode_wins():
    records = [
        _record(14, 1_000, time="2026-10-28 10:00:00"),
        _record(14, 2_000, time="2026-10-28 15:00:00"),
    ]
    detail = _detail([14], 0)
    assert notify_size.apply_history_size(detail, records) == 2_000


def test_other_season_is_ignored():
    records = [dict(_record(14, 9_999_999), season=2)]
    detail = _detail([14], 0)
    assert notify_size.apply_history_size(detail, records) == 0


def test_movie_detail_without_episodes_untouched():
    detail = {"type": "电影", "title": "沙丘", "size": 0}
    assert notify_size.apply_history_size(detail, [_record(1, 5_000)]) == 0
