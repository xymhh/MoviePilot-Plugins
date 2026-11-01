"""全局转存暂停开关：落库、默认值、读失败降级。"""

import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PLUGINS_V2 = os.path.join(PROJECT_ROOT, "plugins.v2")
if PLUGINS_V2 not in sys.path:
    sys.path.insert(0, PLUGINS_V2)


def _plugin_dir(*parts):
    return os.path.join(PLUGINS_V2, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = sys.modules.get(name)
    if not isinstance(module, types.ModuleType) or getattr(module, "__spec__", None) is None:
        module = types.ModuleType(name)
        import importlib.machinery as machinery
        module.__spec__ = machinery.ModuleSpec(name, loader=None)
        sys.modules[name] = module
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is not None:
            setattr(parent, child, module)
    module.__path__ = [path]
    return module


if "app" not in sys.modules or not isinstance(sys.modules.get("app"), types.ModuleType):
    app_module = types.ModuleType("app")
    app_module.__path__ = []
    sys.modules["app"] = app_module
register_mock("app.core.config", settings=SimpleNamespace(DB_TIMEOUT=30))
register_mock("app.log", logger=MagicMock())
register_mock("app.core.cache", TTLCache=object)

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.core.database", _plugin_dir("core", "database"))

import cloudsubscribex.core.database.manager  # noqa: F401  (供 repositories 导入)
import cloudsubscribex.core.database.models  # noqa: F401

sync_pause = load_module(
    "cloudsubscribex.core.sync_pause",
    "plugins.v2/cloudsubscribex/core/sync_pause.py",
)


class _FakeRepo:
    def __init__(self, broken=False):
        self.store = {}
        self.broken = broken

    def load(self, namespace, key, default=None):
        if self.broken:
            raise RuntimeError("db down")
        return self.store.get((namespace, key), default)

    def save(self, namespace, key, payload):
        if self.broken:
            raise RuntimeError("db down")
        self.store[(namespace, key)] = payload
        return True


class SyncPauseTests(unittest.TestCase):
    def test_default_is_running(self):
        repo = _FakeRepo()
        snapshot = sync_pause.snapshot(repo)
        self.assertFalse(snapshot["paused"])
        self.assertFalse(sync_pause.is_paused(repo))

    def test_set_paused_persists_and_reports(self):
        repo = _FakeRepo()
        state = sync_pause.set_paused(True, actor="任务中心", repository=repo)
        self.assertTrue(state["paused"])
        self.assertGreater(state["updated_at"], 0)
        self.assertEqual(state["actor"], "任务中心")
        self.assertTrue(sync_pause.is_paused(repo))
        repo2 = _FakeRepo()
        repo2.store = repo.store
        self.assertTrue(sync_pause.is_paused(repo2), "暂停状态必须落库，重启后仍生效")

    def test_resume_clears_paused(self):
        repo = _FakeRepo()
        sync_pause.set_paused(True, repository=repo)
        state = sync_pause.set_paused(False, repository=repo)
        self.assertFalse(state["paused"])
        self.assertFalse(sync_pause.is_paused(repo))

    def test_read_failure_does_not_block_transfers(self):
        repo = _FakeRepo(broken=True)
        self.assertFalse(sync_pause.is_paused(repo))

    def test_missing_repository_reads_as_running(self):
        """没注入仓储时不能抛：1.8.7 在这里无参 new 仓储，把任务中心整条接口带崩。"""
        snapshot = sync_pause.snapshot()
        self.assertFalse(snapshot["paused"])
        self.assertEqual(snapshot["updated_at"], 0)
        self.assertFalse(sync_pause.is_paused())

    def test_missing_repository_rejects_write(self):
        """写操作没有仓储时要明确报错，不能静默「暂停成功」。"""
        with self.assertRaises(RuntimeError):
            sync_pause.set_paused(True)

    def test_repository_requires_manager(self):
        """固化事实：DbOper 子类必须带 manager，任何无参 new 都会抛 TypeError。"""
        from cloudsubscribex.core.database.repositories import RuntimeRecordRepository

        with self.assertRaises(TypeError):
            RuntimeRecordRepository()


if __name__ == "__main__":
    unittest.main()
