"""v1.13.3 瘦页失败记录重试端到端。

链路：失败行进重试索引 → 失败记录页 api_vue_page_data 列出它
（can_retry=True）→ 经 submit_history_retry 提交重试（行翻成处理中，
页上失败过滤为空）→ 后台重试成功删行 → 页上不再列出。

后台重试本身用桩模拟成功（生产上 _run_history_retry 跑真实转存；
转存执行由重试单测覆盖），本文件钉死页 ↔ 索引 ↔ 重试入口的接线。
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
    register_module,
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
register_package(
    f"{PKG}.core.api", paths=[os.path.join(plugin_dir, "core", "api")]
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
    """与 test_retry_index_1_13_1.py 同款：只弹 sqlalchemy 顶层替身。"""
    stale = sys.modules.get("sqlalchemy")
    if stale is None or getattr(stale, "__spec__", None):
        return
    sys.modules.pop("sqlalchemy", None)
    import sqlalchemy  # noqa: F401
    for name, module in list(sys.modules.items()):
        if not name.startswith("sqlalchemy."):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child and not hasattr(parent, child):
            setattr(parent, child, module)


CloudSubscribeDataStore = None
_DB_MODULES_LOADED = False


def _load_once(module_name: str, relative_path: str):
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


# ---- import 时加载：不碰 sqlalchemy 的模块 ----
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
_retry = _load_once(
    f"{PKG}.handlers.sync.retry",
    os.path.join(plugin_dir, "handlers", "sync", "retry.py"),
)
HistoryRetryService = _retry.HistoryRetryService
_retry_inflight_lock = _retry._retry_inflight_lock
_retry_inflight_keys = _retry._retry_inflight_keys

# page.py 顶层要真实 utils.cache，而真实 cache 在桩 app.core.cache 上
# 造缓存会 StopIteration（见 probe）。只在加载这一瞬间用内存桩顶替，
# 加载完立刻恢复——page.py 已绑定桩函数，不影响后续真实 cache 加载。
class _DictTTLCache:
    def __init__(self, *args, **kwargs):
        self._data = {}

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


_cache_before = sys.modules.get(f"{PKG}.utils.cache")
_cache_stub = register_module(
    f"{PKG}.utils.cache",
    create_platform_ttl_cache=lambda *args, **kwargs: _DictTTLCache(),
)
try:
    page_module = _load_once(
        f"{PKG}.core.api.page",
        os.path.join(plugin_dir, "core", "api", "page.py"),
    )
finally:
    if _cache_before is None:
        sys.modules.pop(f"{PKG}.utils.cache", None)
    else:
        sys.modules[f"{PKG}.utils.cache"] = _cache_before
    _utils_pkg = sys.modules.get(f"{PKG}.utils")
    if _utils_pkg is not None:
        if _cache_before is None:
            try:
                delattr(_utils_pkg, "cache")
            except AttributeError:
                pass
        else:
            setattr(_utils_pkg, "cache", _cache_before)
PageApi = page_module.PageApi


def _load_db_modules() -> None:
    """在 setUpClass 里加载 DB 链（manager / repositories / storage）；幂等。"""
    global _DB_MODULES_LOADED
    global CloudSubscribeDataStore
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


class _PageApi(PageApi):
    """真实 PageApi + 真实仓储的失败记录页桩。"""

    def __init__(self, store):
        super().__init__(None)
        object.__setattr__(self, "_data_store", store)

    def _get_data_store(self):
        return self._data_store


class _E2ERetryService(HistoryRetryService):
    """真实重试入口 + 桩后台：后台等放行信号再模拟转存成功删行。

    放行信号让测试能先稳定观测到“处理中”行（生产上重试转存耗时
    数分钟，处理中态肉眼可见；桩后台若立即删行，断言会竞态失败）。
    """

    def __init__(self, store):
        super().__init__(None)
        object.__setattr__(self, "_data_store", store)
        import threading

        object.__setattr__(self, "_release", threading.Event())

    def _get_data_store(self):
        return self._data_store

    def _is_ed2k_url(self, url: str) -> bool:
        # 生产上由同步服务经 _offline_download 判定；本用例只有夸克分享链接，
        # 按前缀判定即可（ed2k 链接走 STATUS_DOWNLOADING 分支，不影响断言）。
        return str(url or "").lower().startswith("ed2k://")

    def _run_history_retry(self, key, record_time, share_url, file_name):
        try:
            self._release.wait(timeout=5)
            record = self._find_history_record(
                record_time, share_url, file_name
            )
            if record is not None:
                self._delete_retry_index_row(record)
        finally:
            with _retry_inflight_lock:
                _retry_inflight_keys.discard(key)


class SlimHistoryPageRetryE2ETests(unittest.TestCase):
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
        self.page_api = _PageApi(self.store)
        self.retry_service = _E2ERetryService(self.store)

    def tearDown(self):
        # 在飞键是进程级集合：失败中途退出也不能污染后续用例。
        with _retry_inflight_lock:
            _retry_inflight_keys.clear()

    def _seed_failed(self, **overrides):
        record = _failed_record(**overrides)
        row = retry_index_module.record_to_index_row(record, "失败")
        self.assertTrue(self.repo.upsert_row(row))
        return record

    def _page_failures(self, **kwargs):
        result = self.page_api.api_vue_page_data(self.page_api, **kwargs)
        self.assertTrue(result["success"])
        return result["data"]

    def test_failed_row_listed_then_retried_then_gone(self):
        record = self._seed_failed()
        data = self._page_failures()
        self.assertEqual(data["history_page"]["total"], 1)
        failures = data["failures"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["file_name"], "Show - S01E01.mkv")
        self.assertEqual(failures[0]["status"], "失败")
        self.assertTrue(failures[0]["can_retry"])
        summary = self.page_api.api_vue_history_summary()
        self.assertEqual(summary["data"], {
            "failed": 1, "repair": 0, "processing": 0, "total": 1,
        })

        result = self.retry_service.submit_history_retry(
            record["time"], record["share_url"], record["file_name"],
        )
        self.assertEqual(result, {"status": "处理中"})
        # 提交后行翻成处理中：失败过滤为空，处理中过滤能看到它
        self.assertEqual(
            self._page_failures(status="失败")["history_page"]["total"], 0,
        )
        processing = self._page_failures(status="处理中")["failures"]
        self.assertEqual(len(processing), 1)
        self.assertEqual(processing[0]["status"], "处理中")
        self.assertFalse(processing[0]["can_retry"])

        # 放行后台桩（成功删行）：最多 5 秒。注意不能用 list_retryable()
        # 做等待条件——处理中行本就不在它里面，提交后立刻就是空。
        self.retry_service._release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.repo.list_all() == []:
                break
            time.sleep(0.05)
        self.assertEqual(self.repo.list_all(), [])
        data = self._page_failures()
        self.assertEqual(data["history_page"]["total"], 0)
        self.assertEqual(data["failures"], [])
        summary = self.page_api.api_vue_history_summary()
        self.assertEqual(summary["data"], {
            "failed": 0, "repair": 0, "processing": 0, "total": 0,
        })

    def test_permanent_failure_cannot_retry(self):
        record = self._seed_failed(
            failure_kind="share_invalid", failure_reason="分享已失效",
            failure_retryable=False,
        )
        failures = self._page_failures()["failures"]
        self.assertEqual(len(failures), 1)
        self.assertFalse(failures[0]["can_retry"])
        with self.assertRaises(ValueError):
            self.retry_service.submit_history_retry(
                record["time"], record["share_url"], record["file_name"],
            )
        # 拒绝重试不改行状态
        self.assertEqual(len(self.repo.list_retryable()), 1)


if __name__ == "__main__":
    unittest.main()
