"""v1.13.1 retry_index 上线（Phase 2 设计 §2 / §8 第 3 步）。

覆盖：仓储 CRUD、启动一次性导入（只导失败可重试 + 跨盘成功待修复、
幂等）、save 落库同步桥（失败进表、成功出表、处理中只更新已存在行）、
留存清理（失败 90 天、待修复 30 天、处理中不清理）、重试查找先查索引
（有 history 记录时返回它、history 已裁剪时用索引行拼记录、索引不可用
时回落 history 扫描）、Alembic 往返（04 建表→降级删表→再升级重建，
history_records 及其数据全程不动）。

DB 模块加载方式与 test_state_db_migration.py 同款：先恢复真实
sqlalchemy 再 _load_once；重试查找桩与 test_history_retry_batch.py 同款。
加载分两步（全量套件的收集顺序逼出来的，缺一不可）：
① import 时只加载不碰 sqlalchemy 的模块（core.history、drive.common、
search.types、core.retry_index、handlers/sync/history.py、retry.py）——
那时 cloudsubscribex.core.state 还是真实的（test_yun139_caiyun_share
在收集末尾才把它换成空占位），handler 的相对导入能成功；history.py 的
`from sqlalchemy import func, or_` 只是在 mock 上绑属性，不碍事。
② DB 链（manager / repositories / storage）留在 setUpClass 里加载：若在
import 时就加载 manager.py，test_state_db_migration.py 的 _load_once 会
命中缓存、跳过 manager.py 的首次导入，sqlalchemy.orm 就没人重新导入，
后面的 test_task_center.py 在收集时 `from sqlalchemy.orm import Session`
会失败（全量套件收集错误）。DB 链加载前先把 sqlalchemy 替身挪开，加载
后不再放回去——真实 sqlalchemy 一旦恢复就留在原地（与基线行为一致；
放回去会让后续 create_engine 找不到 sqlite 方言）。
"""
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_package,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
plugin_dir = os.path.join(plugins_v2_path, "cloudsubscribex")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core", "app.core.cache", "app.core.config", "app.core.metainfo",
    "app.modules", "app.modules.themoviedb", "app.modules.themoviedb.scraper",
    "app.schemas.types", "app.utils", "app.utils.string",
    "app.chain", "app.chain.mediaserver", "app.core.context",
    "app.db", "app.db.downloadhistory_oper", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.subscribe_oper", "app.helper",
    "app.helper.mediaserver", "app.application", "app.application.mediaserver",
    "app.log",
    "cloudsubscribex", "cloudsubscribex.core",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.search",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)

sys.modules["app.core.config"].settings = types.SimpleNamespace(
    DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp")
)
set_media_type(movie="电影", tv="电视剧")

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
register_package(
    f"{PKG}.handlers", paths=[os.path.join(plugin_dir, "handlers")]
)
register_package(
    f"{PKG}.handlers.sync",
    paths=[os.path.join(plugin_dir, "handlers", "sync")],
)
_core_pkg = ensure_package(f"{PKG}.core")
_core_pkg.OwnerDelegator = OwnerDelegator
_core_pkg.CloudDriveCapability = MagicMock()
_core_pkg.CloudFile = MagicMock()


def _restore_real_sqlalchemy() -> None:
    """清理先跑用例塞进 sys.modules 的 sqlalchemy 顶层替身，恢复真实包。

    只弹 "sqlalchemy" 本身，绝不碰 "sqlalchemy.*" 子模块：全量套件里
    test_state_db_migration 已在收集时加载过真实 sqlalchemy，manager /
    models 早已绑定那一份的类对象；把真实子模块也弹掉，下一次 import 会
    重新加载第二份拷贝，旧 engine 造出的 Connection 与新 dialect 的
    Connection 身份不一致（sqlalchemy.exc.ArgumentError）。

    弹掉顶层后必须重新导入真实包，并给已在 sys.modules 里的真实子模块
    补上父属性：已加载的子模块不会再走导入流程，父包上也就没有它们的
    属性（最要命的是 .dialects——sqlalchemy/__init__.py 不主动导入它），
    否则方言加载 `__import__("sqlalchemy.dialects.sqlite").dialects` 会
    报 AttributeError: module 'sqlalchemy' has no attribute 'dialects'。
    """
    stale = sys.modules.get("sqlalchemy")
    if stale is None or getattr(stale, "__spec__", None):
        return
    sys.modules.pop("sqlalchemy", None)
    import sqlalchemy  # noqa: F401  (真实包；子模块复用 sys.modules 里的真实模块)
    for name, module in list(sys.modules.items()):
        if not name.startswith("sqlalchemy."):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child and not hasattr(parent, child):
            setattr(parent, child, module)


# 惰性加载的占位：DB 链（manager / repositories / storage）在 setUpClass 里
# 由 _load_db_modules() 填实；handler 模块在 import 时已加载（见下）。
CloudSubscribeDataStore = None
RETRY_INDEX_FAILED_RETENTION_SECONDS = None
RETRY_INDEX_REPAIR_RETENTION_SECONDS = None
_DB_MODULES_LOADED = False


def _load_once(module_name: str, relative_path: str):
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


# ---- import 时加载：不碰 sqlalchemy 的模块 ----
# handlers/sync/history.py 虽然 `from sqlalchemy import func, or_`，但那只是
# 在 mock 上绑两个属性，不会失败、也不会导入 sqlalchemy.orm；真正碰
# sqlalchemy（create_engine / Session）的 manager / repositories / storage
# 绝不能在这里加载——否则 test_state_db_migration.py 的 _load_once 会命中
# 缓存、跳过 manager.py 的首次导入，sqlalchemy.orm 就没人重新导入，后面
# 的 test_task_center.py 在收集时 `from sqlalchemy.orm import Session` 失败。
# 注意：下面三处必须是真实模块——HistoryRepository.normalize 在落库时
# 直接调用 history_group_key / positive_int / normalize_resource_type，
# 先行文件若把它们 mock 成占位模块，这里的 _load_once 会用真实实现覆盖。
_load_once(
    f"{PKG}.core.history",
    os.path.join(plugin_dir, "core", "history.py"),
)
_load_once(
    f"{PKG}.drive.common",
    os.path.join(plugin_dir, "drive", "common.py"),
)
_load_once(
    f"{PKG}.search.types",
    os.path.join(plugin_dir, "search", "types.py"),
)
retry_index_module = _load_once(
    f"{PKG}.core.retry_index",
    os.path.join(plugin_dir, "core", "retry_index.py"),
)
_history = _load_once(
    f"{PKG}.handlers.sync.history",
    os.path.join(plugin_dir, "handlers", "sync", "history.py"),
)
HistoryService = _history.HistoryService
_retry = _load_once(
    f"{PKG}.handlers.sync.retry",
    os.path.join(plugin_dir, "handlers", "sync", "retry.py"),
)
HistoryRetryService = _retry.HistoryRetryService


def _load_db_modules() -> None:
    """在 setUpClass 里加载 DB 链（manager / repositories / storage）；幂等。

    只加载碰 sqlalchemy 的三块：manager 与 repositories 直接 import
    sqlalchemy，storage 经 .database 包拿它俩。加载前先把 sqlalchemy 替身
    挪开；注意不要再把它放回去——真实 sqlalchemy 一旦恢复就留在原地（与
    基线行为一致：test_source_sha1_cache_db 等 DB 测试也是恢复后不再放回；
    放回去会让后续 create_engine 找不到 sqlite 方言，
    sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects:sqlite）。
    """
    global _DB_MODULES_LOADED
    global CloudSubscribeDataStore
    global RETRY_INDEX_FAILED_RETENTION_SECONDS
    global RETRY_INDEX_REPAIR_RETENTION_SECONDS
    if _DB_MODULES_LOADED:
        return
    _restore_real_sqlalchemy()
    manager_module = _load_once(
        f"{PKG}.core.database.manager",
        os.path.join(plugin_dir, "core", "database", "manager.py"),
    )
    _db_pkg = sys.modules[f"{PKG}.core.database"]
    _db_pkg.CloudSubscribeDatabaseManager = (
        manager_module.CloudSubscribeDatabaseManager
    )
    repositories_module = _load_once(
        f"{PKG}.core.database.repositories",
        os.path.join(plugin_dir, "core", "database", "repositories.py"),
    )
    _db_pkg.CloudSubscribeRepositories = (
        repositories_module.CloudSubscribeRepositories
    )
    storage_module = _load_once(
        f"{PKG}.core.storage", os.path.join(plugin_dir, "core", "storage.py")
    )
    CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore
    RETRY_INDEX_FAILED_RETENTION_SECONDS = (
        repositories_module.RETRY_INDEX_FAILED_RETENTION_SECONDS
    )
    RETRY_INDEX_REPAIR_RETENTION_SECONDS = (
        repositories_module.RETRY_INDEX_REPAIR_RETENTION_SECONDS
    )
    _DB_MODULES_LOADED = True


class _Owner:
    """最小插件替身：只有数据目录。"""

    def __init__(self, path):
        self._path = path

    def get_data_path(self):
        return self._path


def _failed_record(**overrides):
    record = {
        "time": "2026-11-17 10:00:00",
        "share_url": "https://pan.quark.cn/s/aaa111",
        "file_name": "Show - S01E01.mkv",
        "source_file_name": "Show - S01E01.mkv",
        "tmdb_id": 123, "title": "Show", "type": "tv",
        "season": 1, "episode": 1, "year": "2024",
        "status": "失败", "stage": "transfer",
        "failure_kind": "network", "failure_reason": "网络超时",
        "failure_retryable": True,
        "transfer_mode": "cross",
        "source_drive_key": "quark", "target_drive_key": "u115cs",
        "cloud_dir": "/媒体/剧集/Show (2024) {tmdb-123}/Season 1",
        "file_size": 1000,
    }
    record.update(overrides)
    return record


def _repair_record(**overrides):
    record = _failed_record(
        time="2026-11-17 10:05:00",
        file_name="Show - S01E02.mkv",
        source_file_name="Show - S01E02.mkv",
        episode=2, status="成功", stage="finalize",
        failure_kind="", failure_reason="", failure_retryable=True,
        landed=False,
    )
    record.update(overrides)
    return record


class RetryIndexStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _load_db_modules()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.store = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.repo = self.store.repositories.retry_index

    def test_startup_import_only_eligible_and_idempotent(self):
        """启动导入：只导失败可重试 + 跨盘成功待修复；跑两次不重复；重启仍不重复。"""
        failed = _failed_record()
        repair = _repair_record()
        permanent = _failed_record(
            time="2026-11-17 10:10:00", file_name="Show - S01E03.mkv",
            episode=3, failure_kind="share_invalid",
            failure_reason="分享已失效", failure_retryable=False,
        )
        landed = _repair_record(
            time="2026-11-17 10:15:00", file_name="Show - S01E04.mkv",
            episode=4, landed=True,
        )
        processing = _failed_record(
            time="2026-11-17 10:20:00", file_name="Show - S01E05.mkv",
            episode=5, status="处理中",
        )
        # 直接写 history 表，绕过 save 同步桥（索引此时应为空）
        self.store.repositories.history.replace_all(
            [failed, repair, permanent, landed, processing]
        )
        self.assertEqual(self.repo.list_all(), [])
        imported = self.store._import_retry_index()
        self.assertEqual(imported, 2)
        rows = {row["file_name"]: row for row in self.repo.list_all()}
        self.assertEqual(set(rows), {"Show - S01E01.mkv", "Show - S01E02.mkv"})
        self.assertEqual(rows["Show - S01E01.mkv"]["status"], "失败")
        self.assertEqual(rows["Show - S01E01.mkv"]["failure_kind"], "network")
        self.assertEqual(rows["Show - S01E02.mkv"]["status"], "成功")
        self.assertIs(rows["Show - S01E02.mkv"]["landed"], False)
        # 再跑一次：不重复
        self.assertEqual(self.store._import_retry_index(), 2)
        self.assertEqual(len(self.repo.list_all()), 2)
        # 新 store 实例走完整 initialize（启动路径）：仍不重复
        self.store.close()
        again = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(again.close)
        again.initialize()
        self.assertEqual(len(again.repositories.retry_index.list_all()), 2)

    def test_save_sync_bridge(self):
        """save 落库同步桥：失败进表、成功出表、处理中只更新已存在行。"""
        failed = _failed_record()
        repair = _repair_record()
        success = _failed_record(
            time="2026-11-17 10:30:00", file_name="Show - S01E06.mkv",
            episode=6, status="成功", landed=True,
        )
        processing = _failed_record(
            time="2026-11-17 10:35:00", file_name="Show - S01E07.mkv",
            episode=7, status="处理中",
        )
        self.store.save("history", [failed, repair, success, processing])
        rows = {row["file_name"]: row for row in self.repo.list_all()}
        self.assertEqual(
            set(rows), {"Show - S01E01.mkv", "Show - S01E02.mkv"},
            "成功与处理中行不应进索引",
        )
        # 失败行翻成成功：索引行删除
        flipped = _failed_record(status="成功", landed=True)
        self.store.save("history", [flipped, repair, success, processing])
        rows = {row["file_name"]: row for row in self.repo.list_all()}
        self.assertEqual(set(rows), {"Show - S01E02.mkv"})
        # 处理中行翻成失败：新建索引行
        now_failed = _failed_record(
            time="2026-11-17 10:35:00", file_name="Show - S01E07.mkv",
            episode=7, status="失败",
        )
        self.store.save("history", [flipped, repair, success, now_failed])
        rows = {row["file_name"]: row for row in self.repo.list_all()}
        self.assertEqual(
            set(rows), {"Show - S01E02.mkv", "Show - S01E07.mkv"},
        )
        # 失败行翻回处理中：索引行保留，状态改成处理中
        back_to_processing = _failed_record(
            time="2026-11-17 10:35:00", file_name="Show - S01E07.mkv",
            episode=7, status="处理中",
        )
        self.store.save("history", [flipped, repair, success, back_to_processing])
        rows = {row["file_name"]: row for row in self.repo.list_all()}
        self.assertEqual(rows["Show - S01E07.mkv"]["status"], "处理中")
        # 处理中行不进 list_retryable（重试页只列失败/待修复）
        retryable = {row["file_name"] for row in self.repo.list_retryable()}
        self.assertEqual(retryable, {"Show - S01E02.mkv"})

    def test_repository_crud(self):
        """仓储：无 record_id 拒绝 upsert；按 id/三键查；按 id 删。"""
        self.assertFalse(self.repo.upsert_row({"file_name": "x.mkv"}))
        row = retry_index_module.record_to_index_row(_failed_record(), "失败")
        self.assertTrue(self.repo.upsert_row(row))
        record_id = row["record_id"]
        found = self.repo.get_by_record_id(record_id)
        self.assertEqual(found["file_name"], "Show - S01E01.mkv")
        self.assertEqual(found["status"], "失败")
        by_identity = self.repo.get_by_identity(
            "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
            "Show - S01E01.mkv",
        )
        self.assertEqual(by_identity["record_id"], record_id)
        self.assertIsNone(
            self.repo.get_by_identity(
                "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
                "Show - S01E99.mkv",
            )
        )
        self.assertEqual(
            [item["file_name"] for item in self.repo.list_retryable()],
            ["Show - S01E01.mkv"],
        )
        self.assertEqual(self.repo.delete_by_record_ids([record_id]), 1)
        self.assertEqual(self.repo.list_all(), [])
        self.assertEqual(self.repo.delete_by_record_ids([record_id]), 0)

    def test_retention_purge(self):
        """留存：失败行 90 天、待修复行 30 天、处理中行不清理。"""
        now = int(time.time())
        old_failed = retry_index_module.record_to_index_row(_failed_record(), "失败")
        old_failed["created_at"] = now - RETRY_INDEX_FAILED_RETENTION_SECONDS - 60
        old_repair = retry_index_module.record_to_index_row(_repair_record(), "成功")
        old_repair["created_at"] = now - RETRY_INDEX_REPAIR_RETENTION_SECONDS - 60
        fresh_failed = retry_index_module.record_to_index_row(
            _failed_record(
                time="2026-11-17 11:00:00", file_name="Show - S01E08.mkv",
                episode=8,
            ),
            "失败",
        )
        fresh_failed["created_at"] = now - 60
        old_processing = retry_index_module.record_to_index_row(
            _failed_record(
                time="2026-11-17 11:05:00", file_name="Show - S01E09.mkv",
                episode=9,
            ),
            "处理中",
        )
        old_processing["created_at"] = now - RETRY_INDEX_FAILED_RETENTION_SECONDS - 60
        for row in (old_failed, old_repair, fresh_failed, old_processing):
            self.assertTrue(self.repo.upsert_row(row))
        self.assertEqual(self.repo.purge_old(now=now), 2)
        remaining = {row["file_name"] for row in self.repo.list_all()}
        self.assertEqual(
            remaining, {"Show - S01E08.mkv", "Show - S01E09.mkv"},
            "新鲜失败行与过期处理中行应保留",
        )

    def test_alembic_round_trip(self):
        """Alembic 往返：04 建表→降级到 03 删表→再升级重建；history 表与数据全程不动。"""
        # 本测试要拿真实 sqlalchemy：先把替身挪开（不再放回去，见 _load_db_modules）。
        _restore_real_sqlalchemy()
        from sqlalchemy import create_engine, inspect

        from alembic import command
        from alembic.config import Config as AlembicConfig
        from configparser import ConfigParser

        history_rows = [_failed_record(), _repair_record()]
        self.store.save("history", history_rows)
        self.assertEqual(len(self.repo.list_all()), 2)
        db_path = self.store.manager.db_path
        self.store.close()

        def _tables():
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                return set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        def _history_count():
            engine = create_engine(f"sqlite:///{db_path}")
            try:
                with engine.connect() as conn:
                    return int(conn.exec_driver_sql(
                        "SELECT COUNT(*) FROM history_records"
                    ).scalar())
            finally:
                engine.dispose()

        def _alembic_config():
            config = AlembicConfig()
            config.file_config = ConfigParser(interpolation=None)
            config.set_main_option(
                "script_location",
                str(Path(plugin_dir) / "core" / "database" / "alembic"),
            )
            config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
            # env.py 要求 config.attributes["target_metadata"]（与 manager.update_db 同款）
            models_module = sys.modules.get(f"{PKG}.core.database.models")
            if models_module is not None:
                config.attributes["target_metadata"] = (
                    models_module.CloudSubscribeBase.metadata
                )
            return config

        self.assertIn("retry_index", _tables())
        self.assertEqual(_history_count(), 2)
        command.downgrade(_alembic_config(), "20261117_03")
        self.assertNotIn("retry_index", _tables())
        self.assertEqual(_history_count(), 2)
        command.upgrade(_alembic_config(), "head")
        self.assertIn("retry_index", _tables())
        self.assertEqual(_history_count(), 2)
        # 重建后新 store 启动能正常导入（索引表可用）
        reopened = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(reopened.close)
        reopened.initialize()
        self.assertEqual(len(reopened.repositories.retry_index.list_all()), 2)


class RetryIndexLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _load_db_modules()

        class _BaseRetryService(HistoryRetryService):
            """内存 history + 真实 retry_index 仓储的重试查找桩。"""

            def __init__(self, records, store):
                super().__init__(None)
                object.__setattr__(self, "_store", {"history": list(records)})
                object.__setattr__(self, "_data_store", store)
                object.__setattr__(
                    self, "_history_retry_state", HistoryService._history_retry_state
                )

            def _get_data(self, key):
                return self._store.get(key)

            def _get_data_store(self):
                return self._data_store

        class _NoStoreRetryService(HistoryRetryService):
            """无 _get_data_store 的桩（索引不可用时回落 history 扫描）。"""

            def __init__(self, records):
                super().__init__(None)
                object.__setattr__(self, "_store", {"history": list(records)})
                object.__setattr__(
                    self, "_history_retry_state", HistoryService._history_retry_state
                )

            def _get_data(self, key):
                return self._store.get(key)

        cls._BaseRetryService = _BaseRetryService
        cls._NoStoreRetryService = _NoStoreRetryService

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name)
        self.store = CloudSubscribeDataStore(_Owner(self.data_dir))
        self.addCleanup(self.store.close)
        self.store.initialize()
        self.repo = self.store.repositories.retry_index

    def _service_with_index(self, history_records, index_records):
        for record, status in index_records:
            row = retry_index_module.record_to_index_row(record, status)
            self.assertTrue(self.repo.upsert_row(row))
        return self._BaseRetryService(history_records, self.store)

    def test_index_first_returns_history_record(self):
        """索引命中且 history 记录还在：返回 history 记录本身。"""
        record = _failed_record()
        record["image"] = "https://example.invalid/poster.jpg"
        # 生产上 history 记录落库时会被 _ensure_history_record_id 赋 record_id，
        # 索引行用同一规则算主键，查找时靠它定位 history 记录。
        record["record_id"] = retry_index_module.retry_record_id(record)
        service = self._service_with_index([record], [(record, "失败")])
        found = service._find_history_record(
            "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
            "Show - S01E01.mkv",
        )
        self.assertIs(found, record)

    def test_index_row_converted_when_history_pruned(self):
        """索引命中但 history 记录已被裁剪：用索引行拼一条可重试的记录。"""
        record = _failed_record()
        service = self._service_with_index([], [(record, "失败")])
        found = service._find_history_record(
            "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
            "Show - S01E01.mkv",
        )
        self.assertIsNotNone(found)
        self.assertEqual(found["file_name"], "Show - S01E01.mkv")
        self.assertEqual(found["status"], "失败")
        self.assertEqual(found["failure_kind"], "network")
        self.assertEqual(found["image"], "")

    def test_falls_back_to_history_scan_when_no_index(self):
        """索引里没有：回落 history 扫描，照常找到记录。"""
        record = _failed_record()
        service = self._service_with_index([record], [])
        found = service._find_history_record(
            "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
            "Show - S01E01.mkv",
        )
        self.assertIs(found, record)

    def test_falls_back_when_store_unavailable(self):
        """_get_data_store 不可用（测试桩/异常）：回落 history 扫描，不抛错。"""
        record = _failed_record()
        service = self._NoStoreRetryService([record])
        found = service._find_history_record(
            "2026-11-17 10:00:00", "https://pan.quark.cn/s/aaa111",
            "Show - S01E01.mkv",
        )
        self.assertIs(found, record)


if __name__ == "__main__":
    unittest.main()
