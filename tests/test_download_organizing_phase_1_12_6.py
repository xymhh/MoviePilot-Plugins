"""1.12.6 D：下载页“整理中”态。

覆盖 core/downloader_view.py 的 organizing 档与
core/services/runtime.py 的登记表保留合并：
- download_only 成功后父任务仍在下载页，transfer_phase="organizing"，
  display_phase="organizing"，进度不伪造（不是 100%，速度为空）；
- 登记表 status_text 为停滞文案时下载页显示该文案；
- 登记表清掉（且 finalize 把父任务结算为终态）后任务离开下载页
  （downloading  tab 不再出现；600 秒完成态保留是既有设计，不测）；
- 无登记表条目时行为与现有一模一样（回归基线）。

harness 复用 test_download_upload_phase_1_10_11.py：FakeTransferManager /
FakeOwner / _parent / _serialize 不另起一套桩。登记表模块在全量收集时
会被后收集的文件重载，所以 setUp 里每次从 sys.modules 取当前模块，
不要用模块级绑定的旧引用。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from threading import Lock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    set_media_type,
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
_core_package.CloudDriveCapability = MagicMock()
_core_package.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock("cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock())

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

# 登记表模块只保证已加载；用例里每次从 sys.modules 取当前模块
# （后收集的文件会重载它，模块级引用会指向旧表）。
load_module(
    "cloudsubscribex.core.services.mp_organize_tracker",
    "plugins.v2/cloudsubscribex/core/services/mp_organize_tracker.py",
)

TRACKER_MODULE_NAME = "cloudsubscribex.core.services.mp_organize_tracker"

STALL_TEXT = "整理停滞（队头阻塞，后续任务排队中）"


def _tracker():
    """取当前登记表模块（重载后 sys.modules 里的才是最新表）。"""
    return sys.modules[TRACKER_MODULE_NAME]


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


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


def _parent(**extra):
    task = {
        "id": "subscribe:1", "title": "另一个妈妈",
        "status": "postprocessing",
        "phase": "文件后处理已结束", "progress": 95,
        "total": 0, "transferred": 0,
        "queued_at": 0, "created_at": 0,
    }
    task.update(extra)
    return task


def _success_child(**extra):
    """download_only 已成功的跨盘子任务：下载完成、MP 整理已触发。"""
    task = {
        "id": "cross:a", "parent_task_id": "subscribe:1",
        "status": "success", "phase": "done",
        "total": 1000, "transferred": 1000, "progress": 100,
        "downloaded_bytes": 1000, "uploaded_bytes": 1000,
        "message": "已下载到本地",
    }
    task.update(extra)
    return task


def _serialize(tasks, transfers):
    service = SyncRuntimeService(owner=FakeOwner(tasks, transfers))
    return service._serialize_runtime_tasks()


def _organizing_info():
    return {
        "src_path": "/downloads/另一个妈妈.mkv",
        "src_storage": "local",
        "target_dir": "/媒体库/剧集",
        "target_storage": "u115cs",
        "target_name": "另一个妈妈",
        "file_name": "另一个妈妈.mkv",
    }


class OrganizingDisplayTests(unittest.TestCase):
    """D1：downloader_view 的 organizing 档。"""

    def setUp(self):
        set_media_type()

    def test_organizing_phase_progress_and_speed(self):
        task = {
            "id": "subscribe:1", "title": "另一个妈妈",
            "status": "transferring", "progress": 100,
            "total": 1000, "downloaded_bytes": 1000, "uploaded_bytes": 0,
            "transfer_phase": "organizing",
            "download_speed_bytes_per_second": 1048576,
            "upload_speed_bytes_per_second": 2097152,
            "speed_bytes_per_second": 1048576,
        }
        self.assertEqual(_view.display_phase(task), "organizing")
        # 已下载=总量也不伪造成 100%，速度两路都不给。
        self.assertIsNone(_view.display_progress(task))
        self.assertEqual(_view._task_progress(task), 95.0)
        self.assertIsNone(_view._phase_speed(task, "downloading"))
        self.assertIsNone(_view._phase_speed(task, "uploading"))
        torrent = _view.task_to_torrent(task, torrent_cls=_Torrent)
        self.assertEqual(torrent.progress, 95.0)
        self.assertIsNone(torrent.dlspeed)
        self.assertIsNone(torrent.upspeed)

    def test_organizing_via_phase_field(self):
        task = {
            "id": "subscribe:1", "status": "transferring",
            "total": 1000, "downloaded_bytes": 1000, "uploaded_bytes": 0,
            "phase": "organizing", "progress": 95,
        }
        self.assertEqual(_view.display_phase(task), "organizing")
        self.assertIsNone(_view.display_progress(task))
        self.assertEqual(_view._task_progress(task), 95.0)

    def test_organizing_not_in_download_upload_done_sets(self):
        # organizing 不塞进已有的三档：下载/上传/已完成的行为原样。
        self.assertIn("downloading", _view._DOWNLOAD_PHASES)
        self.assertIn("uploading", _view._UPLOAD_PHASES)
        self.assertIn("done", _view._DOWNLOAD_DONE_PHASES)
        self.assertNotIn("organizing", _view._DOWNLOAD_PHASES)
        self.assertNotIn("organizing", _view._UPLOAD_PHASES)
        self.assertNotIn("organizing", _view._DOWNLOAD_DONE_PHASES)
        downloading = {
            "id": "cross:x", "status": "running", "total": 1000,
            "phase": "downloading", "downloaded_bytes": 250,
            "uploaded_bytes": 0,
        }
        self.assertEqual(_view.display_phase(downloading), "downloading")
        self.assertEqual(_view.display_progress(downloading), 25.0)

    def test_organizing_postprocessing_progress_falls_back_capped(self):
        task = {
            "id": "subscribe:1", "status": "postprocessing",
            "transfer_phase": "organizing", "postprocess_progress": 95,
        }
        self.assertEqual(_view._task_progress(task), 95.0)


class OrganizingRuntimeTests(unittest.TestCase):
    """D2：runtime 按登记表保留父任务并以 organizing 展示。"""

    def setUp(self):
        set_media_type()
        _tracker()._MP_ORGANIZING.clear()

    def tearDown(self):
        _tracker()._MP_ORGANIZING.clear()

    def test_download_only_success_parent_stays_on_download_page(self):
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        result = _serialize([_parent()], [_success_child()])
        self.assertEqual(len(result), 1)
        parent = result[0]
        self.assertEqual(parent["transfer_phase"], "organizing")
        self.assertEqual(parent["phase"], "MP 整理中")
        self.assertTrue(parent["transfer_active"])
        # 子任务已下载=总量（1000/1000），父进度仍是自带的 95，不是 100%。
        self.assertEqual(parent["progress"], 95)
        self.assertEqual(parent["total"], 1000)
        self.assertEqual(parent["downloaded_bytes"], 1000)
        torrent = _view.task_to_torrent(parent, torrent_cls=_Torrent)
        self.assertEqual(torrent.state, "downloading")
        self.assertEqual(torrent.progress, 95.0)
        self.assertIsNone(torrent.dlspeed)
        self.assertIsNone(torrent.upspeed)

    def test_organizing_parent_with_no_transfers_at_all(self):
        # 子任务记录已被跨盘管理器清理，只剩登记表条目时父任务仍保留。
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        result = _serialize([_parent()], [])
        self.assertEqual(len(result), 1)
        parent = result[0]
        self.assertEqual(parent["transfer_phase"], "organizing")
        self.assertEqual(parent["phase"], "MP 整理中")
        self.assertTrue(parent["transfer_active"])
        self.assertEqual(parent["progress"], 95)

    def test_organizing_covers_postprocessing_phase_text(self):
        # 父任务同时在后处理：整理中文案覆盖后处理文案，不打架。
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        result = _serialize(
            [_parent(phase="文件后处理已结束", status="postprocessing")],
            [_success_child()],
        )
        parent = result[0]
        self.assertEqual(parent["transfer_phase"], "organizing")
        self.assertEqual(parent["phase"], "MP 整理中")
        self.assertEqual(parent["progress"], 95)

    def test_organizing_caps_stale_full_progress(self):
        # 父任务自带进度被人写成 100 时，整理中也不显示 100%。
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        result = _serialize([_parent(progress=100)], [_success_child()])
        self.assertEqual(result[0]["progress"], 95)
        torrent = _view.task_to_torrent(result[0], torrent_cls=_Torrent)
        self.assertEqual(torrent.progress, 95.0)

    def test_stall_status_text_shown_on_download_page(self):
        _tracker().mark_mp_organizing(
            "subscribe:1", _organizing_info(), STALL_TEXT
        )
        result = _serialize([_parent()], [_success_child()])
        parent = result[0]
        self.assertEqual(parent["transfer_phase"], "organizing")
        self.assertEqual(parent["phase"], STALL_TEXT)

    def test_registry_cleared_removes_organizing_display(self):
        # 登记表清掉后合并不再打 organizing 标记：只剩成功子任务的
        # 父任务不再带 transfer_phase="organizing"。
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        self.assertEqual(
            _serialize([_parent()], [_success_child()])[0]["transfer_phase"],
            "organizing",
        )
        _tracker().clear_mp_organizing("subscribe:1")
        parent = _serialize([_parent()], [_success_child()])[0]
        self.assertNotEqual(parent.get("transfer_phase"), "organizing")
        self.assertNotEqual(parent.get("phase"), "MP 整理中")

    def test_terminal_parent_leaves_downloading_tab(self):
        # finalize 终态：登记表清掉 + 父任务结算为 completed，下载页的
        # downloading 查询不再返回它（完成态 600 秒保留是既有设计，
        # 这里只断言它离开“下载中”列表）。
        _tracker().mark_mp_organizing("subscribe:1", _organizing_info())
        self.assertEqual(
            _serialize([_parent()], [_success_child()])[0]["transfer_phase"],
            "organizing",
        )
        _tracker().clear_mp_organizing("subscribe:1")
        finished = _parent(
            status="completed", phase="已转存 1 个文件",
            progress=100, finished_at=time.time(),
        )
        # 刚完成的父任务在 600 秒保留期内仍在序列化结果里，但
        # project_tasks 的 downloading 查询只返回进行中态，不再出现。
        result = _serialize([finished], [_success_child()])
        self.assertEqual(len(result), 1)
        torrents = _view.project_tasks(
            result, status="downloading", torrent_cls=_Torrent,
        )
        self.assertEqual(torrents, [])

    def test_no_registry_entry_behaves_as_before(self):
        # 回归基线：无登记表条目时合并行为与 1.10.11 一模一样。
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
        result = _serialize([_parent(status="transferring", progress=0)], transfers)
        parent = result[0]
        self.assertEqual(parent["progress"], 25)
        self.assertEqual(parent["transfer_phase"], "downloading")
        self.assertEqual(parent["downloaded_bytes"], 250)
        self.assertEqual(
            parent["download_speed_bytes_per_second"], 1048576
        )

    def test_no_registry_and_no_active_transfers_skips_merge(self):
        # 无登记表、无活跃子任务：合并跳过，父任务保持原样（不带
        # transfer_active / transfer_phase），与旧行为一致。
        result = _serialize([_parent()], [_success_child()])
        parent = result[0]
        self.assertNotIn("transfer_phase", parent)
        self.assertNotIn("transfer_active", parent)
        self.assertEqual(parent["phase"], "文件后处理已结束")


if __name__ == "__main__":
    unittest.main()
