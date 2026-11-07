"""识别计划接入漏斗（M2-4）测试：``_apply_share_recognition`` 的真实行为。

只接管「剧集 + 有 TMDB 身份 + 知道季号 + 没手选文件」；其余一律原样返回。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_module,
    register_package,
)

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "plugins.v2", "cloudsubscribex")

install_app_mocks("app.db", "app.db.models", "app.db.models.subscribe",
                  "app.db.subscribe_oper", "app.core", "app.core.config",
                  "app.core.cache")


def _pkg(name):
    return sys.modules.get(name) or register_package(name)


core_package = _pkg("cloudsubscribex.core")
core_package.OwnerDelegator = OwnerDelegator
for _attr in ("CloudDriveCapability", "CloudDriveProvider", "CloudFile", "SearchCapability"):
    if _attr not in vars(core_package):
        setattr(core_package, _attr, MagicMock())
_pkg("cloudsubscribex.core.services")
_pkg("cloudsubscribex.search")
_pkg("cloudsubscribex.handlers")
_pkg("cloudsubscribex.handlers.sync")
utils_package = _pkg("cloudsubscribex.utils")
if "MediaFileParser" not in vars(utils_package):
    utils_package.MediaFileParser = MagicMock()
    utils_package.parse_magnet_metadata = MagicMock()

if "cloudsubscribex.core.services.runtime" not in sys.modules:
    register_module("cloudsubscribex.core.services.runtime", sync_lock=MagicMock())
if "cloudsubscribex.core.media" not in sys.modules:
    register_module("cloudsubscribex.core.media",
                    media_identity=MagicMock(), tmdb_id_of=MagicMock())

load_module("cloudsubscribex.handlers.sync.utils",
            "plugins.v2/cloudsubscribex/handlers/sync/utils.py")
load_module("cloudsubscribex.core.services.skip_stats",
            "plugins.v2/cloudsubscribex/core/services/skip_stats.py")
load_module("cloudsubscribex.handlers.sync.candidate_filter",
            "plugins.v2/cloudsubscribex/handlers/sync/candidate_filter.py")
resources_module = load_module(
    "cloudsubscribex.handlers.sync.resources",
    "plugins.v2/cloudsubscribex/handlers/sync/resources.py",
)
ResourceTransferService = resources_module.ResourceTransferService


class _StubMediaFileParser:
    @staticmethod
    def iter_files(files):
        return list(files or [])


resources_module.MediaFileParser = _StubMediaFileParser


def _service():
    return ResourceTransferService(MagicMock())


def _files(*pairs):
    return [{"relative_path": path, "size": size, "id": str(index)}
            for index, (path, size) in enumerate(pairs)]


class ApplyShareRecognitionTest(unittest.TestCase):
    def test_no_identity_keeps_existing_pipeline(self):
        files = _files(("剧/01.mkv", 1 << 30))
        self.assertEqual(_service()._apply_share_recognition(files, work_id=None, season=1), files)

    def test_movie_without_season_keeps_existing_pipeline(self):
        files = _files(("电影.mkv", 1 << 30))
        self.assertEqual(_service()._apply_share_recognition(files, work_id=1, season=None), files)

    def test_manual_selection_wins(self):
        files = _files(("剧/01.mkv", 1 << 30), ("剧/02.mkv", 1 << 30))
        kept = _service()._apply_share_recognition(
            files, work_id=1, season=1, explicit_selection=True
        )
        self.assertEqual(kept, files)

    def test_ready_keeps_only_selected_version(self):
        files = _files(
            ("剧/4K DV/01.mkv", 6 << 30), ("剧/4K DV/02.mkv", 6 << 30),
            ("剧/4K SDR/01.mkv", 1 << 30), ("剧/4K SDR/02.mkv", 1 << 30),
        )
        kept = _service()._apply_share_recognition(files, work_id=1, season=1)
        self.assertEqual([item["relative_path"] for item in kept],
                         ["剧/4K DV/01.mkv", "剧/4K DV/02.mkv"])

    def test_sidecar_follows_selected_media(self):
        files = _files(("剧/4K DV/01.mkv", 6 << 30), ("剧/4K DV/01.srt", 4096))
        kept = _service()._apply_share_recognition(files, work_id=1, season=1)
        self.assertEqual([item["relative_path"] for item in kept],
                         ["剧/4K DV/01.mkv", "剧/4K DV/01.srt"])

    def test_compressed_only_share_blocks_transfer(self):
        files = _files(("剧/01-07.zip", 8 << 30), ("剧/08-15.zip", 8 << 30))
        self.assertEqual(_service()._apply_share_recognition(files, work_id=1, season=1), [])

    def test_no_episode_blocks_transfer(self):
        files = _files(("剧/花絮.mkv", 1 << 30))
        self.assertEqual(_service()._apply_share_recognition(files, work_id=1, season=1), [])


if __name__ == "__main__":
    unittest.main()
