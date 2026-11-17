"""v1.13.1 深度删除逐文件档（Phase 2 设计 §6.3 第 2 层）。

覆盖：单集按 MetaInfo 季集匹配只删一集、整目录与单文件混合目标、
目标文件名解析不出季集时的精确匹配回落（命中删 / 不命中跳过）、
季不一致不删、网盘批量删除服务缺失时跳过。
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
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
    register_module,
)


class _OwnerStub(_BaseDelegator):
    pass


install_app_mocks(
    "app.core.metainfo", "app.db", "app.db.subscribe_oper",
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


def _fake_metainfo(name, custom_words=None):
    text = str(name or "").strip()
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
        m2 = re.search(r"第\s*(\d{1,4})\s*集", text)
        if m2:
            episode = int(m2.group(1))
    return SimpleNamespace(
        begin_season=season,
        begin_episode=episode,
        episode_list=[episode] if episode else [],
        cn_name=text or None,
    )


register_module("app.core.metainfo", MetaInfo=_fake_metainfo)

cleanup_module = load_module(
    "cloudsubscribex.handlers.sync.cleanup",
    "plugins.v2/cloudsubscribex/handlers/sync/cleanup.py",
)
HistoryCleanupService = cleanup_module.HistoryCleanupService


class _FakeDirectories:
    """按路径识目录、按目录 id 列文件的网盘目录服务。"""

    def __init__(self, existing, files_by_dir=None):
        self.existing = dict(existing)
        self.files_by_dir = dict(files_by_dir or {})
        self.resolved = []
        self.listed = []

    def resolve_directory(self, path, create=False):
        self.resolved.append(str(path))
        directory_id = self.existing.get(str(path).rstrip("/"), "")
        return SimpleNamespace(checked=True, directory_id=directory_id)

    def list_directory(self, directory_id):
        self.listed.append(str(directory_id))
        return SimpleNamespace(
            checked=True,
            files=tuple(self.files_by_dir.get(str(directory_id), [])),
        )


class _FakeMutations:
    def __init__(self):
        self.deleted = []

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return True


class _FakeBatchMutations:
    def __init__(self):
        self.deleted_files = []

    def delete_files(self, file_ids):
        self.deleted_files.extend(list(file_ids or []))
        return list(file_ids or [])


class _FakePathMapper:
    """云盘目录+文件名 → 本地 STRM 路径（与 utils/strm.py 同款映射）。"""

    def local_path(self, *, local_root, cloud_root, cloud_dir, file_name):
        relative = PurePosixPath(cloud_dir).relative_to(PurePosixPath(cloud_root))
        stem = Path(file_name).stem
        return Path(local_root) / Path(*relative.parts) / f"{stem}.strm"


class _FakeNotifier:
    def __init__(self):
        self.notified = []

    def notify_deleted_path(self, path, record):
        self.notified.append((str(path), dict(record)))


def _cloud_file(file_id, name, size=1000):
    return SimpleNamespace(id=file_id, name=name, size=size, is_directory=False)


class DeepDeletePerFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "115网盘"
        self.root.mkdir(parents=True)
        self.media_root = "/Alist/影视/moviepilot/媒体库"
        self.series_rel = "剧集/国漫/Show (2024) {tmdb-123}"
        self.season_rel = f"{self.series_rel}/Season 1"
        self.season_cloud_dir = f"{self.media_root}/{self.season_rel}"
        self.dirs = _FakeDirectories({})
        self.mutations = _FakeMutations()
        self.batch_mutations = _FakeBatchMutations()
        self.notifier = _FakeNotifier()
        self.service = self._make_service()

    def tearDown(self):
        self.tmp.cleanup()

    def _make_service(self, with_batch=True, with_notifier=True, with_mapper=True):
        service = HistoryCleanupService.__new__(HistoryCleanupService)
        object.__setattr__(service, "_owner", SimpleNamespace())
        service._local_resource_path = str(self.root)
        service._CLOUD_MEDIA_ROOT = self.media_root
        service._cloud_transfer_path = "/转移"
        service._cloud_transfer_paths = {}
        service._cloud_directories = self.dirs
        service._cloud_mutations = self.mutations
        if with_batch:
            service._cloud_batch_mutations = self.batch_mutations
        service._media_server_notifier = self.notifier if with_notifier else None
        if with_mapper:
            service._path_mapper = _FakePathMapper()
        service._dedup_meta_info = _fake_metainfo

        def _configured_media_path(value):
            text = str(value).replace("/media/strm/115网盘", str(self.root), 1)
            return Path(text), "ok"

        service._configured_media_path = _configured_media_path
        return service

    def _season_listing(self, *names):
        files = []
        for index, name in enumerate(names, start=1):
            files.append(_cloud_file(f"f{index}", name))
        self.dirs.existing[self.season_cloud_dir] = "dir-season"
        self.dirs.files_by_dir["dir-season"] = files
        return files

    def _make_local_season(self, *stems):
        season_dir = self.root / self.season_rel
        season_dir.mkdir(parents=True, exist_ok=True)
        for stem in stems:
            (season_dir / f"{stem}.strm").write_text("http://x/1", encoding="utf-8")
        return season_dir

    def _target(self, name):
        return f"/media/strm/115网盘/{self.season_rel}/{name}"

    def test_deletes_single_episode_only(self):
        """单集目标：只删匹配的那一集，其余集不动，并清本地 STRM、发刷新通知。"""
        self._season_listing(
            "Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S01E03.mkv",
        )
        self._make_local_season(
            "Show - S01E01", "Show - S01E02", "Show - S01E03",
        )
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - S01E02.mkv")]
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["linked_deleted"], 1)
        self.assertEqual(result["cloud_files_deleted"], 1)
        self.assertEqual(result["cloud_directories_deleted"], 0)
        self.assertEqual(result["strm_deleted"], 1)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(self.batch_mutations.deleted_files, ["f2"])
        self.assertEqual(self.mutations.deleted, [])
        # 只清了 E02 的 STRM
        self.assertFalse((self.root / self.season_rel / "Show - S01E02.strm").exists())
        self.assertTrue((self.root / self.season_rel / "Show - S01E01.strm").exists())
        self.assertTrue((self.root / self.season_rel / "Show - S01E03.strm").exists())
        # 刷新通知只发了 E02 的 STRM 路径
        self.assertEqual(len(self.notifier.notified), 1)
        self.assertTrue(
            self.notifier.notified[0][0].endswith("Show - S01E02.strm"),
            f"通知路径不对：{self.notifier.notified}",
        )
        self.assertEqual(self.notifier.notified[0][1]["episode"], 2)
        self.assertEqual(self.notifier.notified[0][1]["tmdb_id"], "123")

    def test_mixed_dir_and_file_targets(self):
        """混合目标：整目录档与逐文件档各走一层，结果合并，互不干扰。"""
        # 整目录目标：另一部剧
        other_rel = "剧集/国产剧/Other (2024) {tmdb-999}"
        other_dir = self.root / other_rel
        other_dir.mkdir(parents=True, exist_ok=True)
        (other_dir / "Other - S01E01.strm").write_text("http://x/1", encoding="utf-8")
        other_cloud = f"{self.media_root}/{other_rel}"
        self.dirs.existing[other_cloud] = "dir-other"
        # 单文件目标：本季 E02
        self._season_listing(
            "Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S01E03.mkv",
        )
        result = self.service.delete_by_media_server_paths([
            f"/media/strm/115网盘/{other_rel}",
            self._target("Show - S01E02.mkv"),
        ])
        self.assertEqual(result["matched"], 2)
        self.assertEqual(result["deleted"], 2)
        self.assertEqual(result["cloud_directories_deleted"], 1)
        self.assertEqual(result["cloud_files_deleted"], 1)
        self.assertEqual(result["linked_deleted"], 1)
        self.assertEqual(result["strm_deleted"], 1)
        self.assertEqual(self.mutations.deleted, ["dir-other"])
        self.assertEqual(self.batch_mutations.deleted_files, ["f2"])

    def test_unparseable_target_exact_name_match(self):
        """目标文件名解析不出季集：文件名精确相等才删。"""
        self._season_listing("Show - 特别篇.mkv", "Show - S01E01.mkv")
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - 特别篇.mkv")]
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["linked_deleted"], 1)
        self.assertEqual(self.batch_mutations.deleted_files, ["f1"])

    def test_unparseable_target_no_match_skipped(self):
        """目标文件名解析不出季集、且无精确匹配：跳过，什么都不删。"""
        self._season_listing("Show - S01E01.mkv", "Show - S01E02.mkv")
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - 花絮.mkv")]
        )
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.batch_mutations.deleted_files, [])
        self.assertEqual(self.mutations.deleted, [])

    def test_season_mismatch_skipped(self):
        """同集不同季：S01E02 的目标不删 S02E02 的文件。"""
        self._season_listing("Show - S02E02.mkv")
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - S01E02.mkv")]
        )
        self.assertEqual(result["matched"], 0)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.batch_mutations.deleted_files, [])

    def test_missing_batch_mutations_skipped(self):
        """网盘批量删除服务未初始化：逐文件档整体跳过。"""
        self.service = self._make_service(with_batch=False)
        self._season_listing("Show - S01E01.mkv", "Show - S01E02.mkv")
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - S01E02.mkv")]
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self.batch_mutations.deleted_files, [])

    def test_remaining_only_metadata_deletes_whole_season_dir(self):
        """删完只剩生成元数据：整季目录删掉，并尝试清理父级目录。"""
        self._season_listing(
            "Show - S01E02.mkv", "Show - S01E02.nfo", "poster.jpg",
        )
        series_cloud = f"{self.media_root}/{self.series_rel}"
        self.dirs.existing[series_cloud] = "dir-series"
        self.dirs.files_by_dir["dir-series"] = [
            SimpleNamespace(id="d1", name="Season 1", size=0, is_directory=True),
        ]
        result = self.service.delete_by_media_server_paths(
            [self._target("Show - S01E02.mkv")]
        )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["linked_deleted"], 1)
        self.assertEqual(result["cloud_files_deleted"], 1)
        self.assertEqual(result["cloud_directories_deleted"], 2)
        self.assertEqual(self.mutations.deleted, ["dir-season", "dir-series"])
        self.assertEqual(self.batch_mutations.deleted_files, [])

    def test_one_target_fails_others_still_processed(self):
        """每个目标独立处理：一个跳过不影响另一个删除。"""
        self._season_listing(
            "Show - S01E01.mkv", "Show - S01E02.mkv", "Show - S01E03.mkv",
        )
        result = self.service.delete_by_media_server_paths([
            self._target("Show - 花絮.mkv"),
            self._target("Show - S01E03.mkv"),
        ])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(self.batch_mutations.deleted_files, ["f3"])


if __name__ == "__main__":
    unittest.main()
