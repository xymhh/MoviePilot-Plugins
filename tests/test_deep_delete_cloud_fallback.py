"""深度删除兜底：无转存历史时按媒体库路径反推网盘目录删除。

覆盖安全边界：媒体根/分类目录拒绝、层级过浅拒绝、网盘不存在跳过、
单文件目标跳过、正常整剧目录删除并清理本地 STRM 目录。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

from plugin_env import (  # noqa: E402
    OwnerDelegator as _BaseDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
)


class _OwnerStub(_BaseDelegator):
    pass


install_app_mocks(
    "app.db", "app.db.subscribe_oper",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.utils", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils.deep_delete_match",
)
ensure_package("cloudsubscribex.core").OwnerDelegator = _OwnerStub
ensure_package("cloudsubscribex.core.media").list_subscribes_by_tmdb_id = lambda *a, **k: []
ensure_package("cloudsubscribex.utils.deep_delete_match").match_history_record_to_targets = (
    lambda *a, **k: False
)

cleanup_module = load_module(
    "cloudsubscribex.handlers.sync.cleanup",
    "plugins.v2/cloudsubscribex/handlers/sync/cleanup.py",
)
HistoryCleanupService = cleanup_module.HistoryCleanupService


class _FakeDirectories:
    """只认识给定目录的网盘目录服务。"""

    def __init__(self, existing):
        self.existing = dict(existing)
        self.resolved = []

    def resolve_directory(self, path, create=False):
        self.resolved.append(path)
        directory_id = self.existing.get(str(path).rstrip("/"), "")
        return SimpleNamespace(checked=True, directory_id=directory_id)


class _FakeMutations:
    def __init__(self):
        self.deleted = []

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return True


class DeepDeleteCloudFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "115网盘"
        self.root.mkdir(parents=True)
        self.media_root = "/Alist/影视/moviepilot/媒体库"
        self.dirs = _FakeDirectories({})
        self.mutations = _FakeMutations()
        self.service = self._make_service()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_service(self):
        service = HistoryCleanupService.__new__(HistoryCleanupService)
        object.__setattr__(service, "_owner", SimpleNamespace())
        service._local_resource_path = str(self.root)
        service._CLOUD_MEDIA_ROOT = self.media_root
        service._cloud_transfer_path = "/转移"
        service._cloud_transfer_paths = {}
        service._cloud_directories = self.dirs
        service._cloud_mutations = self.mutations

        def _configured_media_path(value):
            text = str(value).replace("/media/strm/115网盘", str(self.root), 1)
            return Path(text), "ok"

        service._configured_media_path = _configured_media_path
        return service

    def _make_series(self, relative):
        series_dir = self.root / relative
        (series_dir / "Season 1").mkdir(parents=True, exist_ok=True)
        (series_dir / "Season 1" / "剧集 - S01E01.strm").write_text("http://x/1", encoding="utf-8")
        return series_dir

    def test_deletes_series_directory_without_history(self):
        self._make_series("剧集/国漫/武神主宰 (2020) {tmdb-110181}")
        self.dirs.existing[
            f"{self.media_root}/剧集/国漫/武神主宰 (2020) {{tmdb-110181}}"
        ] = "dir-1"

        result = self.service._delete_cloud_media_by_server_paths(
            ["/media/strm/115网盘/剧集/国漫/武神主宰 (2020) {tmdb-110181}"]
        )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["cloud_directories_deleted"], 1)
        self.assertEqual(result["strm_deleted"], 1)
        self.assertEqual(self.mutations.deleted, ["dir-1"])
        self.assertFalse((self.root / "剧集/国漫/武神主宰 (2020) {tmdb-110181}").exists())

    def test_rejects_shallow_path(self):
        (self.root / "剧集").mkdir(parents=True, exist_ok=True)
        result = self.service._delete_cloud_media_by_server_paths(
            ["/media/strm/115网盘/剧集"]
        )
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.mutations.deleted, [])

    def test_skips_missing_cloud_directory(self):
        self._make_series("剧集/国漫/不存在的剧 (2020) {tmdb-1}")
        result = self.service._delete_cloud_media_by_server_paths(
            ["/media/strm/115网盘/剧集/国漫/不存在的剧 (2020) {tmdb-1}"]
        )
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.mutations.deleted, [])

    def test_skips_single_file_target(self):
        episode = self.root / "剧集/国漫/某剧 (2020) {tmdb-2}/Season 1/某剧 - S01E01.strm"
        episode.parent.mkdir(parents=True)
        episode.write_text("http://x/1", encoding="utf-8")
        result = self.service._delete_cloud_media_by_server_paths(
            [str(episode).replace(str(self.root), "/media/strm/115网盘")]
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.mutations.deleted, [])

    def test_rejects_protected_media_root(self):
        self.dirs.existing[self.media_root] = "root-dir"
        result = self.service._delete_cloud_media_by_server_paths([str(self.root)])
        self.assertEqual(result["matched"], 0)
        self.assertEqual(self.mutations.deleted, [])


if __name__ == "__main__":
    unittest.main()
