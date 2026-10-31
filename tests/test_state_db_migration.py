"""运行期状态与候选账本入库测试（runtime_records / candidate_ledger_entries）。

背景：插件此前把 TG 监控池、STRM 续扫断点放在 ``tg_monitor/*.json`` 与
``strm_scan_state.json``，候选账本放在 ``ledger/candidate-ledger-*.jsonl``。
2026-10-28 统一到 ``cloudsubscribe.db``：

* ``runtime_records(namespace, record_key, payload, updated_at)`` —— TG 监控池/游标/状态、
  STRM 续扫断点；
* ``candidate_ledger_entries(day, ts, run_id, kind, provider, payload)`` —— 候选账本，
  按天分桶、按天清理。

老文件在 ``CloudSubscribeDataStore.initialize()`` 里一次性导入并改名 ``.migrated``。

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
ledger_module = _load_once(
    f"{PKG}.core.services.candidate_ledger",
    os.path.join(plugin_dir, "core", "services", "candidate_ledger.py"),
)
tg_module = _load_once(
    f"{PKG}.core.tg_monitor",
    os.path.join(plugin_dir, "core", "tg_monitor.py"),
)

CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore
MonitorStore = tg_module._MonitorStore
append_records = ledger_module.append_records
read_records = ledger_module.read_records
summarize = ledger_module.summarize
purge_old = ledger_module.purge_old


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


class LedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.store = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(self.store.close)
        self.ledger = self.store.ledger_store()

    def test_append_and_read(self):
        written = append_records(
            self.ledger,
            [
                {"kind": "transferred", "provider": "夸克", "resource_title": "尚公主 4K"},
                {"kind": "skipped", "provider": "115", "reason": "标题不符"},
            ],
            run_id="run-1",
        )
        self.assertEqual(written, 2)
        result = read_records(self.ledger, days=1, limit=10)
        self.assertEqual(result["total"], 2)
        # 最新在后：倒序展示时 skipped 在前
        self.assertEqual(result["items"][0]["kind"], "skipped")
        self.assertEqual(result["items"][0]["run_id"], "run-1")
        self.assertTrue(result["items"][0]["ts"])
        only_skipped = read_records(self.ledger, days=1, kind="skipped")
        self.assertEqual(only_skipped["total"], 1)
        by_keyword = read_records(self.ledger, days=1, keyword="尚公主")
        self.assertEqual(by_keyword["total"], 1)

    def test_summary_counts_by_kind_provider_day(self):
        append_records(self.ledger, [
            {"kind": "skipped", "provider": "夸克"},
            {"kind": "skipped", "provider": "夸克"},
            {"kind": "transferred", "provider": "115"},
        ])
        data = summarize(self.ledger, days=7)
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_kind"]["skipped"], 2)
        self.assertEqual(data["by_provider"]["夸克"], 2)
        self.assertEqual(sum(data["by_day"].values()), 3)

    def test_purge_drops_old_days(self):
        self.store.repositories.ledger.append(
            [{"kind": "skipped", "provider": "夸克"}], day="2020-01-01"
        )
        append_records(self.ledger, [{"kind": "skipped", "provider": "夸克"}])
        self.assertGreaterEqual(purge_old(self.ledger, keep_days=14), 1)
        result = read_records(self.ledger, days=1)
        self.assertEqual(result["total"], 1)

    def test_clean_filters_and_truncates(self):
        appended = append_records(self.ledger, [{
            "kind": "skipped",
            "provider": "夸克",
            "reason": "x" * 500,
            "raw": {"big": True},
        }])
        self.assertEqual(appended, 1)
        item = read_records(self.ledger, days=1)["items"][0]
        self.assertEqual(len(item["reason"]), 300)
        self.assertNotIn("raw", item)


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
        ledger_dir = self.data_dir / "ledger"
        ledger_dir.mkdir(parents=True)
        (ledger_dir / "candidate-ledger-2026-10-27.jsonl").write_text(
            "\n".join([
                json.dumps({"kind": "skipped", "provider": "夸克", "reason": "失效"}),
                json.dumps({"kind": "transferred", "provider": "115"}),
            ]) + "\n",
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
        days = read_records(store.ledger_store(), days=3650, offset=0, limit=50)
        self.assertEqual(days["total"], 2)

        for legacy in [
            self.data_dir / "tg_monitor" / "items.json",
            self.data_dir / "tg_monitor" / "state.json",
            self.data_dir / "strm_scan_state.json",
            self.data_dir / "ledger" / "candidate-ledger-2026-10-27.jsonl",
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
            read_records(second.ledger_store(), days=3650, limit=50)["total"], 2
        )
        self.assertEqual(
            second.runtime_store("tg_monitor").load("state"), {"cursor": 7}
        )


if __name__ == "__main__":
    unittest.main()
