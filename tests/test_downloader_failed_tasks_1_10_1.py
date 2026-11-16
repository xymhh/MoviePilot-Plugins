"""1.10.1：终态失败的任务不上下载页。

下载页的任务模型只有下载中/已暂停/已完成，没有失败态；此前
map_task_state 把 failed / canceled / aborted 一律按“已暂停”画，
失败的任务就变成一张进度停在失败那一刻的已暂停卡片，与历史记录
对不上，卡片上的“继续”还是死的（start_torrents 恒 False）。
本版起 project_tasks 直接拦掉终态失败的任务（它们归历史记录），
stopping（正在取消的瞬时态）改归进行中。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import load_module

_view = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _task(task_id: str, title: str, status: str, progress: int = 79) -> dict:
    return {
        "id": task_id,
        "title": title,
        "media_type": "电视剧",
        "year": "2026",
        "season": 1,
        "target_episodes": [18],
        "status": status,
        "progress": progress,
    }


FAILED = _task("subscribe:201", "今天是太阳雨", "failed")
CANCELED = _task("subscribe:202", "走马上任", "canceled")
ABORTED = _task("subscribe:203", "金银岛", "aborted")
ERRORED = _task("subscribe:204", "LOST10", "error")
CROSS_FAILED = {
    "id": "cross:deadbeef123456",
    "task_kind": "cross_transfer",
    "parent_task_id": "",
    "title": "凡人修仙传 S01E200.mkv",
    "status": "failed",
    "progress": 63,
    "total": 1000,
}
STOPPING = _task("subscribe:205", "现在就出发", "stopping", 40)
STOPPED = _task("subscribe:206", "学生会也有洞！", "stopped", 58)
# 进行中的对照任务：1.10.12 起，没传任何文件的订阅父任务不上下载页，
# 所以这里按真实转存中的任务给 transfer_active。
RUNNING = dict(
    _task("subscribe:207", "谷雨", "running", 45),
    transfer_active=True,
)
# 已完成的对照任务：1.10.12 起，搜完无结果的完成态父任务不上下载页，
# 所以这里按真实转存完成的任务给 transferred。
DONE = dict(_task("subscribe:208", "仙逆", "completed", 100), transferred=2)


class TestTerminalFailedTasksAreDropped(unittest.TestCase):
    def test_failed_tasks_never_reach_download_page(self):
        tasks = [FAILED, CANCELED, ABORTED, ERRORED, CROSS_FAILED, RUNNING]
        for status in (None, "all", "downloading"):
            rows = _view.project_tasks(tasks, status=status, torrent_cls=_Torrent)
            self.assertEqual(
                [row.title for row in rows], ["谷雨"], f"status={status}"
            )
        # paused / completed 查询下本来就没有进行中的任务，失败任务同样不出现。
        for status in ("paused", "completed"):
            rows = _view.project_tasks(tasks, status=status, torrent_cls=_Torrent)
            self.assertEqual(rows, [], f"status={status}")

    def test_failed_task_is_dropped_even_by_explicit_hash(self):
        wanted = _view.task_hash("subscribe:201")
        rows = _view.project_tasks(
            [FAILED, RUNNING], hashs=[wanted], torrent_cls=_Torrent
        )
        self.assertEqual(rows, [])

    def test_failed_status_is_not_mapped_to_paused_anymore(self):
        # map_task_state 的兜底仍是 paused（防御用），但 project_tasks
        # 不会让终态失败走到它那里；这里只锁行为，不锁兜底返回值。
        rows = _view.project_tasks([FAILED], status="paused", torrent_cls=_Torrent)
        self.assertEqual(rows, [])


class TestStoppingAndStopped(unittest.TestCase):
    def test_stopping_counts_as_downloading(self):
        self.assertEqual(_view.map_task_state("stopping"), "downloading")
        rows = _view.project_tasks(
            [STOPPING], status="downloading", torrent_cls=_Torrent
        )
        self.assertEqual([row.title for row in rows], ["现在就出发"])
        rows = _view.project_tasks(
            [STOPPING], status="paused", torrent_cls=_Torrent
        )
        self.assertEqual(rows, [])

    def test_user_stopped_task_stays_paused(self):
        self.assertEqual(_view.map_task_state("stopped"), "paused")
        rows = _view.project_tasks(
            [STOPPED], status="paused", torrent_cls=_Torrent
        )
        self.assertEqual([row.title for row in rows], ["学生会也有洞！"])


class TestCompletedUnchanged(unittest.TestCase):
    def test_completed_still_shows_under_completed(self):
        rows = _view.project_tasks([DONE], status="completed", torrent_cls=_Torrent)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "completed")


if __name__ == "__main__":
    unittest.main()
