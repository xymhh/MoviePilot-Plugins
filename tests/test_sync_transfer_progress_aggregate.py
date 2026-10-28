"""父任务进度聚合：已成功的子任务按 100% 计入，避免进度条断崖。

固定契约：同一媒体下多个文件的跨盘转存，成功文件的 progress 计入 100，
不再把「传完但停在 49%」的旧值混进加权平均；仍在跑的文件用自身百分比。
"""

from threading import Lock
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

register_module("app", package=True)
app_core = register_module("app.core", package=True)
app_core.config = register_mock(
    "app.core.config", global_vars=MagicMock(), settings=MagicMock()
)
register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_mock("app.schemas.types")
register_module("app.utils", package=True)
register_mock("app.utils.http")

register_module("cloudsubscribex", package=True)
register_module(
    "cloudsubscribex.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
)
register_mock("cloudsubscribex.core.media", media_identity=MagicMock())
register_module("cloudsubscribex.utils", package=True)

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
