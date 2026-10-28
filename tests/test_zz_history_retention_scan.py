"""历史台账有界化 / 快照缓存 / STRM 分批续扫测试。

对应 2026-10-23 资源审计后的第 4、5 项优化：

* 历史台账此前无上限，``get_data("history")`` 每次全表加载并深拷贝；
* STRM 全量校准一跑到底，夜间尖峰明显。

运行：``cd <repo> && python -m pytest tests/test_history_retention_scan.py -q``
"""

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

# MoviePilot 配置替身：真实数值，避免 MagicMock 参与算术。
sys.modules["app.core.config"].settings = types.SimpleNamespace(
    DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp")
)

# 与其它用例共用 ``cloudsubscribex`` 包名，避免同一批 ORM 文件被加载两次
# （两套映射类会触发 SQLAlchemy 的 InternalTraversal 冲突）。
PKG = "cloudsubscribex"
register_package(PKG, paths=[plugin_dir])
register_package(f"{PKG}.core", paths=[os.path.join(plugin_dir, "core")])
register_package(
    f"{PKG}.core.database", paths=[os.path.join(plugin_dir, "core", "database")]
)
register_package(f"{PKG}.drive", paths=[os.path.join(plugin_dir, "drive")])
register_package(f"{PKG}.search", paths=[os.path.join(plugin_dir, "search")])
register_package(f"{PKG}.utils", paths=[os.path.join(plugin_dir, "utils")])

def _load_once(module_name: str, relative_path: str):
    """优先复用已被其它用例加载的同名模块，避免重复映射 ORM 类。"""
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
strm_scan_module = _load_once(
    f"{PKG}.utils.strm_scan", os.path.join(plugin_dir, "utils", "strm_scan.py")
)

HistoryRepository = repositories_module.HistoryRepository
history_retention_limit = repositories_module.history_retention_limit
CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore
rebuild_strm_tree = strm_scan_module.rebuild_strm_tree


def _record(index: int) -> dict:
    return {
        "time": f"2026-10-{(index % 28) + 1:02d} 12:{index % 60:02d}:00",
        "title": f"测试剧集 {index}",
        "file_name": f"test-{index}.mp4",
        "status": "success",
        "source": "pansou",
    }


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = CloudSubscribeDatabaseManager(
            Path(self._tmp.name) / "cloudsubscribe.db"
        )
        self.manager.open()
        self.manager.init_db()
        self.addCleanup(self.manager.close)
        self.repo = HistoryRepository(self.manager)

    def test_env_override(self) -> None:
        original = os.environ.get("CLOUDSUBSCRIBE_HISTORY_LIMIT")
        try:
            os.environ["CLOUDSUBSCRIBE_HISTORY_LIMIT"] = "123"
            self.assertEqual(history_retention_limit(), 123)
            os.environ["CLOUDSUBSCRIBE_HISTORY_LIMIT"] = "0"
            self.assertEqual(history_retention_limit(), 0)
            os.environ["CLOUDSUBSCRIBE_HISTORY_LIMIT"] = "坏值"
            self.assertEqual(
                history_retention_limit(),
                repositories_module.DEFAULT_HISTORY_RETENTION,
            )
        finally:
            if original is None:
                os.environ.pop("CLOUDSUBSCRIBE_HISTORY_LIMIT", None)
            else:
                os.environ["CLOUDSUBSCRIBE_HISTORY_LIMIT"] = original

    def test_prune_keeps_newest(self) -> None:
        self.repo.replace_all([_record(i) for i in range(12)])
        deleted = self.repo.prune(limit=5)
        self.assertEqual(deleted, 7)
        rows = self.repo.list_all()
        self.assertEqual(len(rows), 5)
        self.assertIsNotNone(rows[0].get("record_id"))

    def test_prune_noop_when_under_limit(self) -> None:
        self.repo.replace_all([_record(i) for i in range(3)])
        self.assertEqual(self.repo.prune(limit=10), 0)

    def test_prune_zero_means_unbounded(self) -> None:
        self.repo.replace_all([_record(i) for i in range(3)])
        self.assertEqual(self.repo.prune(limit=0), 0)
        self.assertEqual(len(self.repo.list_all()), 3)


class _Owner:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.calls = 0

    def get_data_path(self) -> Path:
        return self._path


class SnapshotCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.owner = _Owner(Path(self._tmp.name))
        self.store = CloudSubscribeDataStore(self.owner)
        self.addCleanup(self.store.close)
        self.store.save("history", [_record(i) for i in range(4)])

    def test_second_load_hits_cache(self) -> None:
        calls = {"n": 0}
        original = self.store.repositories.history.list_all

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        self.store.repositories.history.list_all = counting
        first = self.store.load("history")
        second = self.store.load("history")
        self.assertEqual(len(first), 4)
        self.assertEqual(len(second), 4)
        self.assertEqual(calls["n"], 1, "第二次读取应命中快照缓存")

    def test_cached_copy_is_isolated(self) -> None:
        first = self.store.load("history")
        first[0]["title"] = "被就地改坏"
        second = self.store.load("history")
        self.assertNotEqual(second[0]["title"], "被就地改坏")

    def test_save_invalidates_cache(self) -> None:
        self.store.load("history")
        self.store.save("history", [_record(i) for i in range(2)])
        rows = self.store.load("history")
        self.assertEqual(len(rows), 2)


class _Dir:
    checked = True
    directory_id = "root"
    files = []


class _Listing:
    checked = True

    def __init__(self, files) -> None:
        self.files = files


class _File:
    def __init__(self, cid, name, is_directory=False) -> None:
        self.id = cid
        self.name = name
        self.is_directory = is_directory


class _Provider:
    """两层目录 + 每层一个视频，用来验证断点续扫。"""

    def __init__(self) -> None:
        self.list_calls = []

    def resolve_directory(self, path):
        return _Dir()

    def list_directory(self, cid):
        self.list_calls.append(cid)
        if cid == "root":
            return _Listing([
                _File("d1", "剧集A", True),
                _File("d2", "剧集B", True),
                _File("f0", "root.mp4"),
            ])
        return _Listing([_File(f"{cid}-file", f"{cid}.mp4")])


class _Generator:
    def __init__(self, root: Path) -> None:
        self._root = root

    def render(self, *, file_name, file_path, template_values):
        return f"http://115/{file_path}"

    def local_path(self, *, local_root, cloud_root, cloud_dir, file_name):
        target = self._root / cloud_dir.strip("/") / file_name
        return target.with_suffix(".strm")

    def write(self, *, local_root, cloud_root, cloud_dir, file_name, template_values):
        path = self.local_path(
            local_root=local_root, cloud_root=cloud_root,
            cloud_dir=cloud_dir, file_name=file_name,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(
            file_name=file_name, file_path="", template_values=template_values
        ), encoding="utf-8")


class _StoreOwner:
    def __init__(self, path):
        self._path = path

    def get_data_path(self):
        return self._path


class StrmResumeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.store = CloudSubscribeDataStore(_StoreOwner(self.root))
        self.addCleanup(self.store.close)

    def _run(self, provider, budget):
        return rebuild_strm_tree(
            provider=provider,
            generator=_Generator(self.root / "out"),
            local_root=str(self.root / "out"),
            cloud_root="/媒体",
            video_extensions=[".mp4"],
            reference_values=lambda item: {"url": f"http://115/{item.name}"},
            interval_seconds=0.0,
            time_budget_seconds=budget,
            state_store=self.store,
        )

    def test_time_budget_writes_resume_state(self) -> None:
        stats = self._run(_Provider(), budget=1e-9)
        self.assertEqual(stats.get("interrupted"), 1)
        payload = self.store.load_scan_state()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["cloud_root"], "/媒体")
        self.assertGreaterEqual(len(payload["pending"]), 1)

    def test_resume_finishes_and_clears_state(self) -> None:
        self._run(_Provider(), budget=1e-9)
        stats = self._run(_Provider(), budget=0)
        self.assertIsNone(stats.get("interrupted"))
        self.assertIsNone(self.store.load_scan_state())
        self.assertGreaterEqual(stats["directories"], 3)

    def test_state_from_other_root_ignored(self) -> None:
        self.store.save_scan_state(
            {"cloud_root": "/别的库", "pending": [["x", "/别的库/x"]]}
        )
        stats = self._run(_Provider(), budget=0)
        self.assertIsNone(stats.get("interrupted"))
        self.assertGreaterEqual(stats["directories"], 3)


if __name__ == "__main__":
    unittest.main()
