"""父任务进度聚合：已成功的子任务按 100% 计入，避免进度条断崖。

固定契约：同一媒体下多个文件的跨盘转存，成功文件的 progress 计入 100，
不再把「传完但停在 49%」的旧值混进加权平均；仍在跑的文件用自身百分比。
"""

import os
import sys
from threading import Lock
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

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

SyncRuntimeService = load_module(
    "cloudsubscribex.core.services.runtime",
    "plugins.v2/cloudsubscribex/core/services/runtime.py",
).SyncRuntimeService


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
    return {"id": "subscribe:1", "title": "尚公主", "status": "running",
            "phase": "跨盘转存", "progress": 0, "total": 0, "transferred": 0,
            "queued_at": 0, "created_at": 0}


def _serialize(tasks, transfers):
    service = SyncRuntimeService(owner=FakeOwner(tasks, transfers))
    return service._serialize_runtime_tasks()


def test_finished_child_counts_as_full_progress():
    transfers = [
        {"id": "cross:a", "parent_task_id": "subscribe:1", "status": "success",
         "total": 1000, "transferred": 1000, "progress": 49,
         "message": "已完成"},
        {"id": "cross:b", "parent_task_id": "subscribe:1", "status": "running",
         "total": 1000, "transferred": 0, "progress": 10,
         "message": "正在跨盘转存"},
    ]
    result = _serialize([_parent()], transfers)
    assert result[0]["progress"] == 55
    assert result[0]["total"] == 2000


def test_running_child_keeps_own_percent():
    transfers = [
        {"id": "cross:a", "parent_task_id": "subscribe:1", "status": "running",
         "total": 2000, "transferred": 1000, "progress": 40,
         "message": "正在跨盘转存"},
    ]
    result = _serialize([_parent()], transfers)
    assert result[0]["progress"] == 40
    assert result[0]["phase"] == "正在跨盘转存"
    assert result[0]["transfer_active"] is True
