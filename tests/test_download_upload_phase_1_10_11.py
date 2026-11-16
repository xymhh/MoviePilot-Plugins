"""1.10.11：下载页按阶段展示进度与速度，复用卡片自带的 ↓/↑ 两路速度位。

覆盖 core/downloader_view.py 的阶段进度/速度投影与
core/services/runtime.py 的父任务合并：下载阶段进度=已下载占比、↓ 亮；
上传阶段进度=已上传占比、↑ 亮；校验/秒传尝试/缓存命中展示 100%；
没有字节口径的老格式字典仍走原有总进度；后处理阶段保持 95% 不跳 100%。
"""

from __future__ import annotations

import os
import sys
import unittest
from threading import Lock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_mock("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_mock("app.schemas.types", MediaType=MagicMock())
register_mock("app.utils", package=True)
register_mock("app.utils.http")

_register_real_package("cloudsubscribex", _plugin_dir())
_core_package = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
# 用真实能力枚举 / Owners 委托基类：mock 成 MagicMock 会污染后续用例的同名模块
_core_package.CloudDriveCapability = MagicMock()
_core_package.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock("cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock())

# 真实模块：其它用例会复用 sys.modules 里的同名模块，不能用 MagicMock 顶替
load_module(
    "cloudsubscribex.core.media",
    "plugins.v2/cloudsubscribex/core/media.py",
)

_view = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)

SyncRuntimeService = load_module(
    "cloudsubscribex.core.services.runtime",
    "plugins.v2/cloudsubscribex/core/services/runtime.py",
).SyncRuntimeService


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _cross_task(**extra):
    task = {
        "id": "cross:x",
        "title": "另一个妈妈.mkv",
        "status": "running",
        "total": 1000,
        "phase": "downloading",
        "downloaded_bytes": 0,
        "uploaded_bytes": 0,
        "download_speed_bytes_per_second": 0,
        "upload_speed_bytes_per_second": 0,
    }
    task.update(extra)
    return task


class DisplayProgressTests(unittest.TestCase):
    def test_downloading_uses_downloaded_bytes(self):
        task = _cross_task(downloaded_bytes=250)
        self.assertEqual(_view.display_phase(task), "downloading")
        self.assertEqual(_view.display_progress(task), 25.0)
        self.assertEqual(_view._task_progress(task), 25.0)

    def test_streaming_uses_downloaded_bytes(self):
        task = _cross_task(
            phase="streaming", downloaded_bytes=400, uploaded_bytes=380,
        )
        self.assertEqual(_view.display_phase(task), "downloading")
        self.assertEqual(_view.display_progress(task), 40.0)

    def test_uploading_uses_uploaded_bytes(self):
        task = _cross_task(
            phase="uploading", downloaded_bytes=1000, uploaded_bytes=600,
        )
        self.assertEqual(_view.display_phase(task), "uploading")
        self.assertEqual(_view.display_progress(task), 60.0)
        self.assertEqual(_view._task_progress(task), 60.0)

    def test_hashing_and_rapid_and_cache_show_100(self):
        for phase in ("hashing", "rapid_upload", "cache_ready"):
            task = _cross_task(phase=phase, downloaded_bytes=1000)
            self.assertEqual(_view.display_phase(task), "done")
            self.assertEqual(_view.display_progress(task), 100.0)

    def test_success_shows_100(self):
        task = _cross_task(
            status="success", phase="done",
            downloaded_bytes=1000, uploaded_bytes=1000, progress=100,
        )
        self.assertEqual(_view.display_progress(task), 100.0)

    def test_old_format_task_falls_back_to_stored_progress(self):
        # 老格式字典：阶段是文案、没有字节字段，按原有总进度展示。
        task = {
            "id": "cross:old",
            "title": "旧任务.mkv",
            "status": "running",
            "total": 100,
            "phase": "正在从夸克网盘下载到本地缓存",
            "progress": 35,
        }
        self.assertIsNone(_view.display_progress(task))
        self.assertEqual(_view._task_progress(task), 35.0)

    def test_no_total_falls_back_to_stored_progress(self):
        task = {"id": "subscribe:1", "status": "running", "progress": 10}
        self.assertIsNone(_view.display_progress(task))
        self.assertEqual(_view._task_progress(task), 10.0)

    def test_postprocessing_keeps_95_not_100(self):
        task = {
            "id": "subscribe:1",
            "title": "另一个妈妈",
            "status": "postprocessing",
            "progress": 95,
            "total": 1000,
            "downloaded_bytes": 1000,
            "uploaded_bytes": 1000,
            "transfer_phase": "done",
        }
        self.assertEqual(_view._task_progress(task), 95.0)


class TaskToTorrentSpeedTests(unittest.TestCase):
    def test_downloading_shows_download_speed_only(self):
        task = _cross_task(
            downloaded_bytes=250,
            download_speed_bytes_per_second=1048576,
            upload_speed_bytes_per_second=2097152,
        )
        torrent = _view.task_to_torrent(task, torrent_cls=_Torrent)
        self.assertEqual(torrent.progress, 25.0)
        self.assertEqual(torrent.dlspeed, "1.0 MB/s")
        self.assertIsNone(torrent.upspeed)

    def test_uploading_shows_upload_speed_only(self):
        task = _cross_task(
            phase="uploading",
            downloaded_bytes=1000,
            uploaded_bytes=600,
            download_speed_bytes_per_second=1048576,
            upload_speed_bytes_per_second=2097152,
        )
        torrent = _view.task_to_torrent(task, torrent_cls=_Torrent)
        self.assertEqual(torrent.progress, 60.0)
        self.assertIsNone(torrent.dlspeed)
        self.assertEqual(torrent.upspeed, "2.0 MB/s")

    def test_done_shows_no_speed(self):
        task = _cross_task(
            status="success", phase="done",
            downloaded_bytes=1000, uploaded_bytes=1000,
        )
        torrent = _view.task_to_torrent(task, torrent_cls=_Torrent)
        self.assertEqual(torrent.progress, 100.0)
        self.assertIsNone(torrent.dlspeed)
        self.assertIsNone(torrent.upspeed)


class FakeTransferManager:
    ACTIVE = frozenset({"pending", "running", "stopping"})

    def __init__(self, transfers):
        self._transfers = transfers

    def list(self):
        return [dict(item) for item in self._transfers]


class FakeOwner:
    """最小可用的宿主：只提供运行态序列化真正会读到的字段。"""

    def __init__(self, tasks, transfers):
        self._sync_tasks = {task["id"]: dict(task) for task in tasks}
        self._sync_tasks_lock = Lock()
        self._sync_handler = None
        self._cross_transfer_manager = FakeTransferManager(transfers)


def _parent():
    return {"id": "subscribe:1", "title": "另一个妈妈", "status": "transferring",
            "phase": "跨盘转存", "progress": 0, "total": 0, "transferred": 0,
            "queued_at": 0, "created_at": 0}


def _serialize(tasks, transfers):
    service = SyncRuntimeService(owner=FakeOwner(tasks, transfers))
    return service._serialize_runtime_tasks()


class MergePhaseProgressTests(unittest.TestCase):
    def test_downloading_parent_uses_downloaded_bytes(self):
        transfers = [
            {
                "id": "cross:a", "parent_task_id": "subscribe:1",
                "status": "running", "phase": "downloading",
                "total": 1000, "transferred": 250, "progress": 12,
                "downloaded_bytes": 250, "uploaded_bytes": 0,
                "download_speed_bytes_per_second": 1048576,
                "upload_speed_bytes_per_second": 0,
                "message": "正在下载",
            },
        ]
        result = _serialize([_parent()], transfers)
        parent = result[0]
        self.assertEqual(parent["progress"], 25)
        self.assertEqual(parent["transfer_phase"], "downloading")
        self.assertEqual(parent["downloaded_bytes"], 250)
        self.assertEqual(parent["download_speed_bytes_per_second"], 1048576)
        self.assertEqual(parent["upload_speed_bytes_per_second"], 0)

    def test_uploading_parent_uses_uploaded_bytes(self):
        transfers = [
            {
                "id": "cross:a", "parent_task_id": "subscribe:1",
                "status": "running", "phase": "uploading",
                "total": 1000, "transferred": 1000, "progress": 75,
                "downloaded_bytes": 1000, "uploaded_bytes": 600,
                "download_speed_bytes_per_second": 0,
                "upload_speed_bytes_per_second": 2097152,
                "message": "正在上传",
            },
        ]
        result = _serialize([_parent()], transfers)
        parent = result[0]
        self.assertEqual(parent["progress"], 60)
        self.assertEqual(parent["transfer_phase"], "uploading")
        self.assertEqual(parent["uploaded_bytes"], 600)
        self.assertEqual(parent["upload_speed_bytes_per_second"], 2097152)

    def test_success_child_counts_full_in_upload_phase(self):
        # A 秒传成功（字节按整份计），B 正在上传一半：父进度 (1000+500)/2000=75。
        transfers = [
            {
                "id": "cross:a", "parent_task_id": "subscribe:1",
                "status": "success", "phase": "done",
                "total": 1000, "transferred": 1000, "progress": 100,
                "message": "已秒传",
            },
            {
                "id": "cross:b", "parent_task_id": "subscribe:1",
                "status": "running", "phase": "uploading",
                "total": 1000, "transferred": 1000, "progress": 75,
                "downloaded_bytes": 1000, "uploaded_bytes": 500,
                "message": "正在上传",
            },
        ]
        result = _serialize([_parent()], transfers)
        parent = result[0]
        self.assertEqual(parent["progress"], 75)
        self.assertEqual(parent["transfer_phase"], "uploading")


if __name__ == "__main__":
    unittest.main()
