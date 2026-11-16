"""1.10.12：搜索阶段的订阅父任务不上下载页。

同步一轮开始时每个订阅都会先建一张父任务卡（queued 0%“等待调度”
→ running 10%“检查缺失内容”→ 搜索 45~65%“搜索缺失剧集”），而
下载页的任务模型只有下载中/已暂停/已完成，这些卡片全被画成“下载中”，
实际上一个字节都还没动；搜完没找到资源的还会留一张 100%“已完成”
卡 10 分钟。本版起 project_tasks 拦掉没传任何文件的订阅父任务：
排队/搜索/搜完无结果态不上下载页；真开始传了（有活跃子任务或已传
文件数 > 0）照常显示；独立跨盘任务与 downloading / transferring /
postprocessing / stopped 的父任务不受此限。
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


def _rows(tasks, status=None, **kwargs):
    return _view.project_tasks(
        tasks, status=status, torrent_cls=_Torrent, **kwargs
    )


def _parent(task_id: str, title: str, status: str, progress: int = 0,
            **extra) -> dict:
    task = {
        "id": task_id,
        "title": title,
        "media_type": "电视剧",
        "year": "2026",
        "season": 1,
        "target_episodes": [5],
        "status": status,
        "progress": progress,
        "transferred": 0,
    }
    task.update(extra)
    return task


def _cross(task_id: str, title: str, status: str, progress: int = 30) -> dict:
    return {
        "id": task_id,
        "task_kind": "cross_transfer",
        "parent_task_id": "",
        "title": title,
        "status": status,
        "progress": progress,
        "total": 1000,
    }


class TestSearchPhaseParentsAreHidden(unittest.TestCase):
    def test_queued_parent_is_hidden(self):
        task = _parent("subscribe:301", "LOST10", "queued", 0)
        for status in (None, "all", "downloading"):
            rows = _rows([task], status=status)
            self.assertEqual(rows, [], f"status={status}")

    def test_running_search_parent_is_hidden(self):
        task = _parent(
            "subscribe:302", "吞噬星空", "running", 55,
            phase="搜索缺失剧集",
            search_active=True,
            search_channels=[{"source": "pansou", "state": "done"}],
        )
        rows = _rows([task], status="downloading")
        self.assertEqual(rows, [])

    def test_search_parent_hidden_even_by_explicit_hash(self):
        task = _parent(
            "subscribe:303", "凡人修仙传", "running", 45,
            phase="搜索缺失剧集",
        )
        wanted = _view.task_hash("subscribe:303")
        rows = _rows([task], hashs=[wanted])
        self.assertEqual(rows, [])

    def test_completed_parent_with_nothing_is_hidden(self):
        task = _parent(
            "subscribe:304", "金银岛", "completed", 100,
            phase="无需转存",
        )
        rows = _rows([task], status="completed")
        self.assertEqual(rows, [])

    def test_media_parent_search_is_hidden(self):
        task = _parent(
            "media:tmdb:999", "另一个妈妈", "queued", 0,
            media_type="电影",
        )
        rows = _rows([task])
        self.assertEqual(rows, [])


class TestParentsWithTransfersStillShow(unittest.TestCase):
    def test_running_parent_with_active_transfer_shows(self):
        task = _parent(
            "subscribe:311", "LOST10", "running", 60,
            transfer_active=True,
            transfer_file_count=1,
        )
        rows = _rows([task], status="downloading")
        self.assertEqual([row.title for row in rows], ["LOST10"])
        self.assertEqual(rows[0].state, "downloading")

    def test_completed_parent_with_transfers_shows(self):
        task = _parent(
            "subscribe:312", "吞噬星空", "completed", 100,
            phase="已转存 2 个文件", transferred=2,
        )
        rows = _rows([task], status="completed")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].state, "completed")

    def test_downloading_parent_without_transfer_flag_shows(self):
        # downloading / transferring / postprocessing 是转存或后处理活动态，
        # 不受搜索阶段规则限制（例如等磁力/离线下载的父任务）。
        for status in ("downloading", "transferring", "postprocessing"):
            task = _parent("subscribe:313", "谷雨", status, 40)
            rows = _rows([task], status="downloading")
            self.assertEqual(
                [row.title for row in rows], ["谷雨"], f"status={status}"
            )

    def test_standalone_cross_transfer_shows(self):
        task = _cross("cross:abcdef123456", "05.mkv", "running", 30)
        rows = _rows([task], status="downloading")
        self.assertEqual([row.title for row in rows], ["05.mkv"])

    def test_stopped_parent_unchanged(self):
        task = _parent("subscribe:314", "百日谎言", "stopped", 100)
        rows = _rows([task], status="paused")
        self.assertEqual([row.title for row in rows], ["百日谎言"])

    def test_transferred_zero_string_is_treated_as_zero(self):
        task = _parent(
            "subscribe:315", "走马上任", "running", 10,
            transferred="0",
        )
        rows = _rows([task])
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
