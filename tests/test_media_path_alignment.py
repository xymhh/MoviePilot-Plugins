"""1.6.53 媒体库路径前缀对齐 + 搜索诊断单测。

对应任务书 docs/plan/2026-10-16-媒体库路径前缀对齐与诊断.md：

1. 原样命中：root 下且存在 → 通过（回归保护）；
2. 显式映射命中：媒体服务器前缀 + media_server_path_mappings → 翻译后通过；
3. 尾部对齐兜底：未配映射时按尾部对齐通过；
4. 全不命中：丢前缀后同名但不存在 → 仍然 ValueError，不被尾部对齐误判；
5. list_media_server_content 诊断：命中但全被路径挡掉 → dropped_path>0 且 message 给建议。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
)

logger_ref = sys.modules["app.log"].logger
set_media_type(movie="电影", tv="电视剧")
register_module("app.schemas", MediaInfo=SimpleNamespace)

install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
)

_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core").CloudDriveCapability = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(
    return_value=("themoviedb", "123")
)
_ensure("cloudsubscribex.core.media").download_history_identity_payload = MagicMock(
    return_value={}
)
_ensure("cloudsubscribex.core.media").media_server_tmdb_filters = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.history").history_group_key = MagicMock(
    return_value=""
)
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"

history_module = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = history_module.HistoryService


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = list(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, *args, **kwargs):
        return _FakeQuery(self._rows)


class _FakeSessionFactory:
    rows = []

    def __call__(self):
        return _FakeSession(self.rows)


class _PathAlignmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "strm" / "115网盘"
        self.root.mkdir(parents=True)
        self.handler = HistoryService(SimpleNamespace())
        self.handler._local_resource_path = str(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _entry(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
        return path

    def test_1_literal_hit_passes(self):
        """1. 原样命中：root 下且存在 → 通过（回归保护）。"""
        entry = self._entry("电影/沙丘/沙丘.mkv")
        resolved, relative = self.handler._configured_media_path(str(entry))
        self.assertEqual(resolved, entry)
        self.assertEqual(relative, "电影/沙丘/沙丘.mkv")

    def test_2_explicit_mapping_passes(self):
        """2. 显式映射：媒体服务器前缀 = 本地前缀 → 翻译后通过。"""
        self._entry("电影/沙丘/沙丘.mkv")
        self.handler._media_server_path_mappings = "/media/strm=" + str(self.root)
        resolved, relative = self.handler._configured_media_path(
            "/media/strm/电影/沙丘/沙丘.mkv"
        )
        self.assertEqual(resolved, self.root / "电影/沙丘/沙丘.mkv")
        self.assertEqual(relative, "电影/沙丘/沙丘.mkv")

    def test_3_suffix_alignment_passes(self):
        """3. 尾部对齐兜底：未配映射时从右往左丢前缀段自动对齐。"""
        self._entry("剧集/国产剧/美人余/美人余.mkv")
        raw = "/media" + str(self.root / "剧集/国产剧/美人余/美人余.mkv")
        resolved, relative = self.handler._configured_media_path(raw)
        self.assertEqual(relative, "剧集/国产剧/美人余/美人余.mkv")
        logger_ref.info.assert_called()

    def test_4_all_miss_raises_value_error(self):
        """4. 全不命中：丢前缀后同名但不存在 → 仍 ValueError，不误判。"""
        raw = "/downloads" + str(self.root / "不存在.mkv")
        with self.assertRaises(ValueError) as ctx:
            self.handler._configured_media_path(raw)
        self.assertIn("media_server_path_mappings", str(ctx.exception))


    def test_6_suffix_landing_on_root_is_rejected(self):
        """6. 尾部对齐退化到 root 本身不算命中（不把整个媒体库当条目）。"""
        raw = "/media" + str(self.root)
        with self.assertRaises(ValueError):
            self.handler._configured_media_path(raw)


class _DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "strm" / "115网盘"
        self.root.mkdir(parents=True)
        self.handler = HistoryService(SimpleNamespace())
        self.handler._local_resource_path = str(self.root)
        self.handler._media_server_path_mappings = ""
        history_module.MediaServerHelper = lambda: SimpleNamespace(
            get_services=lambda: {
                "emby": SimpleNamespace(
                    type="emby",
                    instance=SimpleNamespace(is_inactive=lambda: False),
                ),
            }
        )
        history_module.SessionFactory = _FakeSessionFactory()
        history_module.tmdb_id_of = lambda row: getattr(row, "tmdb_id", None)
        _FakeSessionFactory.rows = [
            SimpleNamespace(
                server="emby", item_type="电视剧", media_source="themoviedb",
                media_id="294446", tmdb_id=294446, title="美人余", year=2026,
                item_id="655616", seasoninfo={},
                path="/downloads" + str(self.root / "剧集/美人余.mkv"),
            )
        ]

    def tearDown(self):
        self._tmp.cleanup()

    def test_5_diagnostics_and_message(self):
        """5. 命中但全被路径挡掉 → dropped_path>0，message 含路径前缀与映射建议。"""
        result = self.handler.list_media_server_content(server="emby", keyword="美人余")
        self.assertEqual(result["items"], [])
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["matched"], 1)
        self.assertEqual(diagnostics["dropped_path"], 1)
        self.assertTrue(diagnostics["dropped_path_example"])
        self.assertIn("路径前缀", result["message"])
        self.assertIn("/media/strm=/strm", result["message"])


if __name__ == "__main__":
    unittest.main()
