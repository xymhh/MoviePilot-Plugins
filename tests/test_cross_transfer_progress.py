"""跨盘传输进度：只增不减，避免订阅任务里的进度条来回蹦。

固定契约：
1. 同一 task_id 的 progress 不回退（分片重下、边下边传回退缓存都不能把它拉回 0）；
2. 已成功的子任务在父任务聚合里按 100% 计，避免「传完一个 → 下一个从 0」的断崖；
3. 上传阶段百分比不低于秒传阶段的 52，避免 52 → 50 的回落。
"""

import os
import sys
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module

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
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager


def make_manager():
    manager = CrossTransferTaskManager(
        lambda key: MagicMock(),
    )
    manager._notify_change = lambda: None
    manager._tasks["cross:t1"] = {
        "id": "cross:t1", "task_kind": "cross_transfer", "status": "running",
        "phase": "downloading", "progress": 0, "transferred": 0, "total": 1000,
        "stage_transferred": 0, "stage_total": 0,
    }
    return manager


def test_progress_never_goes_backwards():
    manager = make_manager()
    for value in (12, 40, 49, 0, 52, 50, 80):
        manager._update("cross:t1", progress=value)
    assert manager._tasks["cross:t1"]["progress"] == 80


def test_progress_zero_update_keeps_previous_value():
    manager = make_manager()
    manager._update("cross:t1", progress=37)
    manager._update("cross:t1", progress=0, phase="downloading")
    task = manager._tasks["cross:t1"]
    assert task["progress"] == 37
    assert task["phase"] == "downloading"


def test_progress_accepts_numeric_strings_and_garbage():
    manager = make_manager()
    manager._update("cross:t1", progress="30")
    manager._update("cross:t1", progress="bad")
    assert manager._tasks["cross:t1"]["progress"] == 30
