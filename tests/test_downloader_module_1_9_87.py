"""1.9.87：补 get_module 声明，修复下载页任务不显示。

1.9.86 只登记了下载器条目、实现了 list_torrents 等方法，却没按模块
合同向宿主声明——MP 下载页查任务走模块调度器（DownloadChain →
DownloadTaskService → run_module("list_torrents")），只认插件
get_module() 返回的字典，调度器根本不调本插件的 list_torrents：
下载器条目已在、任务运行中，GET /download/ 仍返回 0 条。

本文件复用 test_downloader_plugin_1_9_86 的真实插件加载器，断言
get_module() 声明的 5 个下载器方法 + 1.9.91 新增的 search_torrents /
download（搜索接入 MP 原生搜索后，这两个入口必须在声明里，否则
搜索页与下载链路根本不到插件这一步），并通过声明的 list_torrents
入口跑一次投影，确认运行中任务能返回（含真机复现：玫瑰战争
running 52% 的跨盘任务）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from test_downloader_plugin_1_9_86 import _plugin  # noqa: E402

EXPECTED_KEYS = {
    "search_torrents",
    "download",
    "list_torrents",
    "torrent_files",
    "start_torrents",
    "stop_torrents",
    "remove_torrents",
}

# 真机复现：subscribe:372 玫瑰战争 running 52% 的跨盘转存。
ROSE_WAR_TASKS = [
    {
        "id": "subscribe:372",
        "title": "玫瑰战争",
        "media_type": "电视剧",
        "year": "2026",
        "season": 1,
        "target_episodes": [1],
        "status": "running",
        "progress": 52,
        "transferred": 520,
        "total": 1000,
        "speed_bytes_per_second": 2097152,
        "current_file": "",
    },
    {
        "id": "cross:rosewar000001",
        "task_kind": "cross_transfer",
        "parent_task_id": "subscribe:372",
        "title": "玫瑰战争 S01E01.mkv",
        "target_path": "/转存/电视剧/玫瑰战争 (2026)/Season 1/玫瑰战争 - S01E01.mkv",
        "status": "running",
        "progress": 52,
        "transferred": 520,
        "total": 1000,
        "speed_bytes_per_second": 2097152,
    },
]


class TestGetModuleDeclaration(unittest.TestCase):
    def test_declares_exactly_seven_module_methods(self):
        plugin = _plugin()
        module = plugin.get_module()
        self.assertEqual(set(module.keys()), EXPECTED_KEYS)
        for key, fn in module.items():
            self.assertTrue(callable(fn), f"{key} 不可调用")

    def test_declares_search_and_download(self):
        """1.9.91：搜索接入 MP 原生搜索，两个入口必须在声明里。"""
        module = _plugin().get_module()
        self.assertIn("search_torrents", module)
        self.assertIn("download", module)

    def test_list_torrents_via_declared_module(self):
        """经声明入口跑投影：运行中任务必须返回（1.9.86 的 bug 是
        调度器根本不到这一步）。"""
        plugin = _plugin()
        rows = plugin.get_module()["list_torrents"]()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].downloader, "网盘订阅助手")
        self.assertEqual(rows[0].title, "谷雨")
        self.assertEqual(rows[0].state, "downloading")

    def test_live_case_rose_war_running(self):
        """真机复现：玫瑰战争 running 52%，经声明入口必须出现。"""
        plugin = _plugin()
        plugin._serialize_runtime_tasks = lambda: [
            dict(task) for task in ROSE_WAR_TASKS
        ]
        rows = plugin.get_module()["list_torrents"]()
        self.assertEqual([row.title for row in rows], ["玫瑰战争", "玫瑰战争 S01E01.mkv"])
        self.assertTrue(all(row.state == "downloading" for row in rows))

    def test_stop_and_files_via_declared_module(self):
        plugin = _plugin()
        module = plugin.get_module()
        from cloudsubscribex.core.downloader_view import task_hash

        self.assertTrue(
            module["stop_torrents"](hashs=[task_hash("subscribe:123")])
        )
        self.assertEqual(
            plugin._stop_calls, [("test-token", "subscribe:123")]
        )
        files = module["torrent_files"](tid=task_hash("cross:abcdef123456"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "谷雨 S01E21.mkv")
        self.assertFalse(module["start_torrents"](hashs=["a" * 40]))


if __name__ == "__main__":
    unittest.main()
