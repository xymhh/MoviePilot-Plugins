"""源文件 SHA1 缓存入库（source_sha1_cache 表）测试。

背景：1.7.97 之前缓存写在 ``source_sha1_cache.json``，读-改-写无锁，并发会丢更新，
且与插件其它数据（cloudsubscribe.db）分裂成两套持久化。改为同库单表后：

* 键 = 「来源网盘|体积|文件名」，主键唯一，原子 upsert；
* 30 天 TTL（读取时判定）、2000 条上限（写入后按 updated_at 淘汰最旧）；
* 老装环境靠 ``init_db()`` 的 ``create_all`` 自动补建缺失表，无需迁移脚本。

运行：``cd <repo> && python -m pytest tests/test_source_sha1_cache_db.py -q``
"""

import os
import sys
import tempfile
import time
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
models_module = _load_once(
    f"{PKG}.core.database.models",
    os.path.join(plugin_dir, "core", "database", "models.py"),
)
storage_module = _load_once(
    f"{PKG}.core.storage", os.path.join(plugin_dir, "core", "storage.py")
)

CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
SourceSha1CacheRepository = repositories_module.SourceSha1CacheRepository
CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore
SourceSha1Cache = models_module.SourceSha1Cache
SourceMissCacheRepository = repositories_module.SourceMissCacheRepository
SourceMissCache = models_module.SourceMissCache

SHA1_A = "A" * 40
SHA1_B = "B" * 40
SHA1_C = "C" * 40


class _Owner:
    def __init__(self, path):
        self._path = path

    def get_data_path(self):
        return self._path


class SourceSha1CacheTableTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "cloudsubscribe.db"
        self.manager = CloudSubscribeDatabaseManager(self.db_path)
        self.manager.open()
        self.manager.init_db()
        self.addCleanup(self.manager.close)
        self.repo = SourceSha1CacheRepository(self.manager)

    def test_table_created_by_init_db(self):
        """老库升级靠 create_all 补建缺失表。"""
        from sqlalchemy import text

        with self.manager.session() as db:
            names = {
                row[0]
                for row in db.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).all()
            }
        self.assertIn("source_sha1_cache", names)

    def test_put_then_get_roundtrip(self):
        key = "quark|5136261590|S【上@供#住】.mkv"
        self.assertTrue(
            self.repo.put(key, "a" * 40, size=5136261590, name="S【上@供#住】.mkv")
        )
        entry = self.repo.get(key)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["sha1"], SHA1_A)
        self.assertEqual(entry["size"], 5136261590)
        self.assertEqual(entry["name"], "S【上@供#住】.mkv")
        self.assertGreater(entry["updated_at"], 0)

    def test_put_overwrites_same_key(self):
        key = "quark|100|movie.mkv"
        self.repo.put(key, SHA1_A, size=100, name="movie.mkv")
        first = self.repo.get(key)["updated_at"]
        self.repo.put(key, SHA1_B, size=100, name="movie.mkv")
        entry = self.repo.get(key)
        self.assertEqual(entry["sha1"], SHA1_B)
        self.assertGreaterEqual(entry["updated_at"], first)
        with self.manager.session() as db:
            from sqlalchemy import func, select

            total = db.scalar(select(func.count()).select_from(SourceSha1Cache))
        self.assertEqual(total, 1)

    def test_invalid_input_ignored(self):
        self.assertFalse(self.repo.put("", SHA1_A))
        self.assertFalse(self.repo.put("quark|1|a.mkv", ""))
        self.assertIsNone(self.repo.get(""))
        self.assertIsNone(self.repo.get("quark|1|a.mkv"))

    def test_ttl_expiry_on_read(self):
        key = "quark|200|old.mkv"
        self.repo.put(key, SHA1_A, size=200, name="old.mkv")
        # 把更新时间挪到 31 天前，模拟过期记录。
        with self.manager.session(write=True) as db:
            row = db.get(SourceSha1Cache, key)
            row.updated_at = int(row.updated_at) - 31 * 86400
        self.assertIsNotNone(self.repo.get(key))
        self.assertIsNone(self.repo.get(key, ttl_seconds=30 * 86400))

    def test_limit_prunes_oldest(self):
        for index in range(3):
            self.repo.put(
                f"quark|{index}|m{index}.mkv", SHA1_A, size=index, name=f"m{index}.mkv"
            )
        self.repo.put("quark|9|new.mkv", SHA1_C, size=9, name="new.mkv", limit=2)
        from sqlalchemy import func, select

        with self.manager.session() as db:
            total = db.scalar(select(func.count()).select_from(SourceSha1Cache))
        self.assertEqual(total, 2, "超过上限后必须淘汰到只剩 limit 条")
        self.assertIsNotNone(self.repo.get("quark|9|new.mkv"), "刚写入的记录不能被淘汰")

    def test_purge_expired(self):
        self.repo.put("quark|300|keep.mkv", SHA1_A, size=300, name="keep.mkv")
        self.repo.put("quark|301|drop.mkv", SHA1_B, size=301, name="drop.mkv")
        with self.manager.session(write=True) as db:
            row = db.get(SourceSha1Cache, "quark|301|drop.mkv")
            row.updated_at = int(row.updated_at) - 40 * 86400
        self.assertEqual(self.repo.purge_expired(30 * 86400), 1)
        self.assertIsNotNone(self.repo.get("quark|300|keep.mkv"))
        self.assertIsNone(self.repo.get("quark|301|drop.mkv"))


class SourceMissCacheTableTest(unittest.TestCase):
    """1.7.101 负缓存表：同一 (目标盘, SHA1) 24h 内不再下载重试。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db_path = Path(tmp.name) / "cloudsubscribe.db"
        self.manager = CloudSubscribeDatabaseManager(self.db_path)
        self.manager.open()
        self.manager.init_db()
        self.addCleanup(self.manager.close)
        self.repo = SourceMissCacheRepository(self.manager)

    def test_table_created_by_init_db(self):
        from sqlalchemy import text

        with self.manager.session() as db:
            names = {
                row[0]
                for row in db.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).all()
            }
        self.assertIn("source_miss_cache", names)

    def test_put_then_get_roundtrip(self):
        self.assertTrue(self.repo.put("115", SHA1_A))
        missed_at = self.repo.get("115", SHA1_A.lower(), ttl_seconds=86400)
        self.assertGreater(missed_at, 0)
        # 换目标盘/换哈希都不得串味
        self.assertEqual(self.repo.get("quark", SHA1_A, ttl_seconds=86400), 0)
        self.assertEqual(self.repo.get("115", SHA1_B, ttl_seconds=86400), 0)

    def test_repeat_miss_updates_timestamp(self):
        self.repo.put("115", SHA1_A, missed_at=1000)
        self.repo.put("115", SHA1_A, missed_at=2000)
        self.assertEqual(self.repo.get("115", SHA1_A), 2000)
        with self.manager.session() as db:
            from sqlalchemy import func, select

            total = db.scalar(select(func.count()).select_from(SourceMissCache))
        self.assertEqual(total, 1)

    def test_ttl_expiry_and_purge(self):
        self.repo.put("115", SHA1_A, missed_at=int(time.time()) - 90000)
        self.assertEqual(self.repo.get("115", SHA1_A, ttl_seconds=86400), 0)
        self.assertGreater(self.repo.get("115", SHA1_A), 0, "未带 TTL 时仍可取到")
        self.assertEqual(self.repo.purge_expired(86400), 1)

    def test_invalid_input_ignored(self):
        self.assertFalse(self.repo.put("", SHA1_A))
        self.assertFalse(self.repo.put("115", ""))
        self.assertEqual(self.repo.get("", SHA1_A), 0)
        self.assertEqual(self.repo.get("115", ""), 0)


class DataStoreSha1Test(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = CloudSubscribeDataStore(_Owner(Path(tmp.name)))
        self.addCleanup(self.store.close)

    def test_datastore_roundtrip(self):
        key = "115|1024|ep01.mkv"
        self.assertTrue(
            self.store.put_source_sha1(key, SHA1_A, size=1024, name="ep01.mkv")
        )
        entry = self.store.get_source_sha1(key)
        self.assertEqual(entry["sha1"], SHA1_A)
        self.assertEqual(entry["size"], 1024)
        self.assertIsNone(self.store.get_source_sha1("115|2048|missing.mkv"))

    def test_datastore_miss_roundtrip(self):
        self.assertEqual(self.store.get_source_miss("115", SHA1_A), 0)
        self.assertTrue(self.store.put_source_miss("115", SHA1_A))
        self.assertGreater(self.store.get_source_miss("115", SHA1_A), 0)
        self.assertEqual(
            self.store.get_source_miss("115", SHA1_B, ttl_seconds=86400), 0
        )

    def test_legacy_json_migrated_then_renamed(self):
        """1.7.97 的 JSON 缓存升级后自动并入表，原文件改名不重复迁移。"""
        data_dir = self.store.manager.db_path.parent
        legacy = data_dir / "source_sha1_cache.json"
        legacy.write_text(
            '{"quark|5136261590|E09.mkv": {"sha1": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
            ' "size": 5136261590, "name": "E09.mkv", "updated_at": "2026-10-27 12:05:03"}}',
            encoding="utf-8",
        )
        self.store.close()
        reopened = CloudSubscribeDataStore(_Owner(data_dir))
        self.addCleanup(reopened.close)
        entry = reopened.get_source_sha1("quark|5136261590|E09.mkv")
        self.assertEqual(entry["sha1"], SHA1_A)
        self.assertEqual(entry["size"], 5136261590)
        self.assertFalse(legacy.exists(), "迁移后旧文件必须改名，避免重复导入")
        self.assertTrue((data_dir / "source_sha1_cache.json.migrated").exists())

    def test_datastore_persists_across_reopen(self):
        key = "115|4096|ep02.mkv"
        self.store.put_source_sha1(key, SHA1_B, size=4096, name="ep02.mkv")
        path = self.store.manager.db_path
        self.store.close()
        reopened = CloudSubscribeDataStore(_Owner(path.parent))
        self.addCleanup(reopened.close)
        entry = reopened.get_source_sha1(key)
        self.assertEqual(entry["sha1"], SHA1_B)


if __name__ == "__main__":
    unittest.main()
