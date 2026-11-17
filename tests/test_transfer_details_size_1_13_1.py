"""v1.13.1 通知体积重锚（Phase 2 设计 §3.3）。

transfer_details 在创建时就要带真实体积（从转存结果按集累加），
不再事后从 history 里补；体积未知记 size_unknown=True，卡片不显示数字、
注明“部分集数体积未知”。本文件覆盖 _append（按集对齐 file_names /
episode_sizes / size_unknown）、_aggregate（合并、逐集校验修正、未知
传播）与卡片渲染（未知时不显示大小数字）。

旧的 test_notify_size_reconcile.py（apply_history_size 回填）已按设计
删除——回填逻辑本身（notify_size.py）留到 v1.13.2 再删，本版只测新契约。
"""
import os
import sys
import types
import unittest
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

sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)
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
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(
    return_value=[]
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
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
_ensure("cloudsubscribex.utils.cache").create_platform_ttl_cache = MagicMock()
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"


class _StubMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))


_ensure("cloudsubscribex.utils.file_parser").MediaFileParser = _StubMediaFileParser

_tpl = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)
_core = sys.modules["cloudsubscribex.core"]
_core.render_template = _tpl.render_template
_core.resolve_template = _tpl.resolve_template


def _format_size(value):
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


_ensure("cloudsubscribex.drive.common").format_size = _format_size

transfer_details_module = load_module(
    "cloudsubscribex.handlers.sync.transfer_details",
    "plugins.v2/cloudsubscribex/handlers/sync/transfer_details.py",
)
HistoryService = transfer_details_module.TransferDetailsService

_notify = load_module(
    "cloudsubscribex.handlers.sync.notify",
    "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
)
SyncNotificationService = _notify.SyncNotificationService


def _mediainfo(title="玉簟秋", year=2024):
    return SimpleNamespace(
        title=title, year=year, get_poster_image=lambda: "",
    )


def _append(details, episodes, sizes=None, names=None, size_bytes=0,
            kind="transfer", source_text="夸克 → 115"):
    HistoryService._append_tv_transfer_detail(
        details, _mediainfo(), 1, episodes,
        notification_kind=kind, size_bytes=size_bytes,
        source_text=source_text, file_names=names, episode_sizes=sizes,
    )


def _tv_detail(episodes, size, file_names=None, episode_sizes=None,
                size_unknown=False, kind="transfer"):
    detail = {
        "type": "电视剧", "title": "玉簟秋", "year": 2024, "season": 1,
        "episodes": list(episodes), "size": size,
        "size_unknown": size_unknown, "source": "夸克 → 115",
        "notification_kind": kind,
    }
    if file_names is not None:
        detail["file_names"] = list(file_names)
    if episode_sizes is not None:
        detail["episode_sizes"] = dict(episode_sizes)
    return detail


class AppendTransferDetailTests(unittest.TestCase):
    """_append：按集累加真实体积、按集对齐文件名、未知记标记。"""

    def test_append_sums_episode_sizes_and_aligns_file_names(self):
        """13 集洗版旧案：三集分两批追加，体积按集求和，文件名按集对齐。"""
        details = []
        _append(details, [14], sizes={14: 2_738_000_000},
                names=["玉簟秋 - S01E14.mkv"])
        _append(details, [18, 19],
                sizes={18: 5_860_000_000, 19: 6_100_000_000},
                names=["玉簟秋 - S01E18.mkv", "玉簟秋 - S01E19.mkv"])
        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["episodes"], [14, 18, 19])
        self.assertEqual(
            detail["size"], 2_738_000_000 + 5_860_000_000 + 6_100_000_000
        )
        self.assertEqual(detail["file_names"], [
            "玉簟秋 - S01E14.mkv",
            "玉簟秋 - S01E18.mkv",
            "玉簟秋 - S01E19.mkv",
        ])
        self.assertEqual(detail["episode_sizes"], {
            14: 2_738_000_000, 18: 5_860_000_000, 19: 6_100_000_000,
        })
        self.assertFalse(detail["size_unknown"])
        self.assertEqual(detail["source"], "夸克 → 115")

    def test_append_marks_size_unknown_when_episode_size_missing(self):
        details = []
        _append(details, [14], sizes={14: 0},
                names=["玉簟秋 - S01E14.mkv"])
        self.assertTrue(details[0]["size_unknown"])
        self.assertEqual(details[0]["size"], 0)

    def test_append_later_batch_wins_per_episode(self):
        """同一集重传：后一批的体积覆盖前一批，不做累加。"""
        details = []
        _append(details, [14], sizes={14: 1000},
                names=["玉簟秋 - S01E14.mkv"])
        _append(details, [14], sizes={14: 2000},
                names=["玉簟秋 - S01E14.mkv"])
        self.assertEqual(details[0]["episode_sizes"], {14: 2000})
        self.assertEqual(details[0]["size"], 2000)
        self.assertFalse(details[0]["size_unknown"])

    def test_append_without_episode_sizes_uses_batch_size(self):
        """没给逐集体积时沿用整批字节；0 字节记未知。"""
        details = []
        _append(details, [14, 15], size_bytes=5000)
        self.assertEqual(details[0]["size"], 5000)
        self.assertFalse(details[0]["size_unknown"])
        empty = []
        _append(empty, [16], size_bytes=0)
        self.assertTrue(empty[0]["size_unknown"])


class AggregateTransferDetailTests(unittest.TestCase):
    """_aggregate：合并 file_names / episode_sizes，逐集校验修正汇总。"""

    def _aggregate(self, details):
        service = HistoryService.__new__(HistoryService)
        return service._aggregate_transfer_details(details)

    def test_aggregate_merges_file_names_and_episode_sizes(self):
        merged = self._aggregate([
            _tv_detail([14], 1000, ["玉簟秋 - S01E14.mkv"], {14: 1000}),
            _tv_detail([18, 19], 5000,
                        ["玉簟秋 - S01E18.mkv", "玉簟秋 - S01E19.mkv"],
                        {18: 2000, 19: 3000}),
        ])
        self.assertEqual(len(merged), 1)
        detail = merged[0]
        self.assertEqual(detail["episodes"], [14, 18, 19])
        self.assertEqual(detail["size"], 6000)
        self.assertEqual(detail["file_names"], [
            "玉簟秋 - S01E14.mkv",
            "玉簟秋 - S01E18.mkv",
            "玉簟秋 - S01E19.mkv",
        ])
        self.assertEqual(detail["episode_sizes"], {14: 1000, 18: 2000, 19: 3000})
        self.assertFalse(detail["size_unknown"])

    def test_aggregate_corrects_size_mismatch(self):
        """汇总与逐集之和不一致（线上 2.87GB 旧案）：按逐集之和修正。"""
        merged = self._aggregate([
            _tv_detail([14, 18], 999, ["玉簟秋 - S01E14.mkv", "玉簟秋 - S01E18.mkv"],
                        {14: 1000, 18: 2000}),
        ])
        self.assertEqual(merged[0]["size"], 3000)

    def test_aggregate_keeps_finalize_size_without_episode_sizes(self):
        """finalize 路径的明细不带逐集体积：不修正汇总，只保留原值。"""
        merged = self._aggregate([
            _tv_detail([14], 5000, ["玉簟秋 - S01E14.mkv"]),
        ])
        self.assertEqual(merged[0]["size"], 5000)
        self.assertFalse(merged[0].get("size_unknown", False))

    def test_aggregate_propagates_size_unknown(self):
        merged = self._aggregate([
            _tv_detail([14], 1000, ["玉簟秋 - S01E14.mkv"], {14: 1000}),
            _tv_detail([15], 0, ["玉簟秋 - S01E15.mkv"], {15: 0},
                        size_unknown=True),
        ])
        self.assertEqual(merged[0]["episodes"], [14, 15])
        self.assertTrue(merged[0]["size_unknown"])
        self.assertEqual(merged[0]["size"], 1000)


def _notify_service():
    service = object.__new__(SyncNotificationService)
    object.__setattr__(service, "_owner", types.SimpleNamespace())
    service._post_message = MagicMock()
    service._notification_type = "Plugin"
    service._notify_templates = {}
    return service


class TransferNotificationCardTests(unittest.TestCase):
    """卡片：体积未知时不显示大小数字，注明“部分集数体积未知”。

    v1.13.3：通知只剩洗版一种，卡片用 upgrade 详情走同一渲染路径。
    """

    def test_card_hides_size_and_notes_unknown(self):
        service = _notify_service()
        service._send_transfer_notification_now([
            _tv_detail([14, 18, 19], 3000,
                        ["玉簟秋 - S01E14.mkv", "玉簟秋 - S01E18.mkv",
                        "玉簟秋 - S01E19.mkv"],
                        {14: 1000, 18: 2000, 19: 0}, size_unknown=True,
                        kind="upgrade"),
        ])
        text = service._post_message.call_args.kwargs["text"]
        self.assertNotIn("大小：", text)
        self.assertIn("部分集数体积未知", text)
        # 合计是已知部分的和（3000 字节），配注说明它不是全量
        self.assertIn("合计 2.9 KB", text)

    def test_card_shows_size_when_known(self):
        service = _notify_service()
        service._send_transfer_notification_now([
            _tv_detail([14], 3000, ["玉簟秋 - S01E14.mkv"], {14: 3000},
                        kind="upgrade"),
        ])
        text = service._post_message.call_args.kwargs["text"]
        self.assertIn("大小：2.9 KB", text)
        self.assertNotIn("部分集数体积未知", text)


if __name__ == "__main__":
    unittest.main()
