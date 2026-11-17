"""运行期状态入库测试（runtime_records）。

背景：插件此前把 TG 监控池、STRM 续扫断点放在 ``tg_monitor/*.json`` 与
``strm_scan_state.json``。2026-10-28 统一到 ``cloudsubscribe.db``：

* ``runtime_records(namespace, record_key, payload, updated_at)`` —— TG 监控池/游标/状态、
  STRM 续扫断点。

老文件在 ``CloudSubscribeDataStore.initialize()`` 里一次性导入并改名 ``.migrated``。
（候选账本已随 v1.13.0 删除：``candidate_ledger_entries`` 表、``ledger/`` 旧文件
迁移与本文件的账本用例一并移除。）

运行：``cd <repo> && python -m pytest tests/test_state_db_migration.py -q``
"""

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from plugin_env import (
    install_app_mocks,
    load_module,
    register_package,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
plugin_dir = os.path.join(plugins_v2_path, "cloudsubscribex")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core",
    "app.core.cache",
    "app.core.config",
    "app.core.metainfo",
    "app.modules",
    "app.modules.themoviedb",
    "app.modules.themoviedb.scraper",
    "app.schemas.types",
    "app.utils",
    "app.utils.string",
)

sys.modules["app.core.config"].settings = types.SimpleNamespace(
    DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp")
)

PKG = "cloudsubscribex"
register_package(PKG, paths=[plugin_dir])
register_package(f"{PKG}.core", paths=[os.path.join(plugin_dir, "core")])
register_package(
    f"{PKG}.core.database", paths=[os.path.join(plugin_dir, "core", "database")]
)
register_package(f"{PKG}.drive", paths=[os.path.join(plugin_dir, "drive")])
register_package(f"{PKG}.search", paths=[os.path.join(plugin_dir, "search")])
register_package(f"{PKG}.utils", paths=[os.path.join(plugin_dir, "utils")])
register_package(
    f"{PKG}.core.services", paths=[os.path.join(plugin_dir, "core", "services")]
)


def _restore_real_sqlalchemy() -> None:
    """清理先跑用例塞进 sys.modules 的 sqlalchemy 替身，否则 alembic 导入失败。"""
    stale = sys.modules.get("sqlalchemy")
    if stale is None or getattr(stale, "__spec__", None):
        return
    for name in [n for n in list(sys.modules) if n == "sqlalchemy" or n.startswith("sqlalchemy.")]:
        sys.modules.pop(name, None)


_restore_real_sqlalchemy()


def _load_once(module_name: str, relative_path: str):
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


manager_module = _load_once(
    f"{PKG}.core.database.manager",
    os.path.join(plugin_dir, "core", "database", "manager.py"),
)
_db_pkg = sys.modules[f"{PKG}.core.database"]
_db_pkg.CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
repositories_module = _load_once(
    f"{PKG}.core.database.repositories",
    os.path.join(plugin_dir, "core", "database", "repositories.py"),
)
_db_pkg.CloudSubscribeRepositories = repositories_module.CloudSubscribeRepositories
storage_module = _load_once(
    f"{PKG}.core.storage", os.path.join(plugin_dir, "core", "storage.py")
)
tg_module = _load_once(
    f"{PKG}.core.tg_monitor",
    os.path.join(plugin_dir, "core", "tg_monitor.py"),
)

CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore
MonitorStore = tg_module._MonitorStore


class _Owner:
    """最小插件替身：只有数据目录，没有 _get_data_store。"""

    def __init__(self, path):
        self._path = path

    def get_data_path(self):
        return self._path


class RuntimeRecordTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.store = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(self.store.close)

    def test_runtime_store_roundtrip_and_delete(self):
        store = self.store.runtime_store("tg_monitor")
        self.assertEqual(store.load("items", []), [])
        items = [{"id": "1", "title": "剧集", "status": "pending"}]
        store.save("items", items)
        self.assertEqual(store.load("items"), items)
        store.delete("items")
        self.assertEqual(store.load("items", []), [])

    def test_namespaces_are_isolated(self):
        self.store.runtime_store("tg_monitor").save("state", {"cursor": 1})
        self.store.runtime_store("other").save("state", {"cursor": 2})
        self.assertEqual(self.store.runtime_store("tg_monitor").load("state"), {"cursor": 1})
        self.assertEqual(self.store.runtime_store("other").load("state"), {"cursor": 2})

    def test_scan_state_roundtrip(self):
        self.assertIsNone(self.store.load_scan_state())
        payload = {"cloud_root": "/媒体", "pending": [["cid", "/媒体/a"]]}
        self.assertTrue(self.store.save_scan_state(payload))
        self.assertEqual(self.store.load_scan_state(), payload)
        self.store.clear_scan_state()
        self.assertIsNone(self.store.load_scan_state())

    def test_monitor_store_falls_back_to_own_database(self):
        """单测替身没有 _get_data_store 时，自动按数据目录建库。"""
        store = MonitorStore(_Owner(self.data_dir))
        store.save("status", {"running": True, "scanned": 12})
        again = MonitorStore(_Owner(self.data_dir))
        self.assertEqual(again.load("status"), {"running": True, "scanned": 12})
        self.assertEqual(again.load("missing", {"x": 1}), {"x": 1})


class LegacyFileMigrationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        # 先在建库前铺好旧文件，模拟 1.7.97 的数据目录
        tg_dir = self.data_dir / "tg_monitor"
        tg_dir.mkdir(parents=True)
        (tg_dir / "items.json").write_text(
            json.dumps([{"id": "1", "title": "剧集"}]), encoding="utf-8"
        )
        (tg_dir / "state.json").write_text(
            json.dumps({"cursor": 7}), encoding="utf-8"
        )
        (self.data_dir / "strm_scan_state.json").write_text(
            json.dumps({"cloud_root": "/媒体", "pending": [["cid", "/媒体/a"]]}),
            encoding="utf-8",
        )

    def test_all_legacy_files_imported_and_renamed(self):
        store = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(store.close)
        store.initialize()

        tg = store.runtime_store("tg_monitor")
        self.assertEqual(tg.load("items"), [{"id": "1", "title": "剧集"}])
        self.assertEqual(tg.load("state"), {"cursor": 7})
        self.assertEqual(
            store.load_scan_state()["cloud_root"], "/媒体"
        )
        for legacy in [
            self.data_dir / "tg_monitor" / "items.json",
            self.data_dir / "tg_monitor" / "state.json",
            self.data_dir / "strm_scan_state.json",
        ]:
            self.assertFalse(legacy.exists(), f"{legacy.name} 迁移后必须改名")
            self.assertTrue(
                legacy.with_name(legacy.name + ".migrated").exists(),
                f"{legacy.name} 应留下 .migrated 副本",
            )

    def test_second_start_does_not_duplicate(self):
        first = CloudSubscribeDataStore(_Owner(self.data_dir))
        first.initialize()
        first.close()
        second = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(second.close)
        second.initialize()
        self.assertEqual(
            second.runtime_store("tg_monitor").load("state"), {"cursor": 7}
        )


if __name__ == "__main__":
    unittest.main()
