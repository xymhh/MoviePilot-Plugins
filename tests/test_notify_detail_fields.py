"""通知详情的大小/来源字段与聚合累加（1.7.78）。"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.log",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")

_core = sys.modules["cloudsubscribex.core"]
_core.OwnerDelegator = OwnerDelegator
_core.CloudFile = object
sys.modules["app.log"].logger = MagicMock()

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryMixin = _history.HistoryService


def _service(drive_name="115网盘"):
    service = object.__new__(HistoryMixin)
    owner = types.SimpleNamespace()
    owner._cloud_drive = types.SimpleNamespace(name=drive_name)
    object.__setattr__(service, "_owner", owner)
    return service


class DetailSizeTest(unittest.TestCase):
    def test_prefers_file_size(self):
        self.assertEqual(
            HistoryMixin._detail_size({"file_size": 1024, "size": 9}, None), 1024
        )

    def test_falls_back_to_file_info(self):
        self.assertEqual(
            HistoryMixin._detail_size({}, {"size": 2048}), 2048
        )

    def test_reads_nested_file_size(self):
        self.assertEqual(
            HistoryMixin._detail_size(None, {"file": {"size": 4096}}), 4096
        )

    def test_missing_or_garbage(self):
        self.assertEqual(HistoryMixin._detail_size(None, None), 0)
        self.assertEqual(HistoryMixin._detail_size({"size": "abc"}, None), 0)


class DetailSourceTest(unittest.TestCase):
    def test_same_drive_uses_target_only(self):
        service = _service()
        self.assertEqual(
            service._detail_source_text(
                {"cloud_drive_name": "115网盘", "source_drive_name": "115网盘"}
            ),
            "115网盘",
        )

    def test_cross_drive_uses_arrow(self):
        service = _service()
        self.assertEqual(
            service._detail_source_text(
                {"cloud_drive_name": "115网盘", "source_drive_name": "夸克网盘"}
            ),
            "夸克网盘 → 115网盘",
        )

    def test_falls_back_to_current_drive(self):
        service = _service("115网盘")
        self.assertEqual(service._detail_source_text({}), "115网盘")
        self.assertEqual(service._detail_source_text(None), "115网盘")


class AggregateSizeTest(unittest.TestCase):
    def _tv(self, episode, size, source="115网盘"):
        return {
            "type": "电视剧",
            "title": "尚公主",
            "year": "2026",
            "season": 1,
            "episodes": [episode],
            "size": size,
            "source": source,
            "notification_kind": "transfer",
        }

    def test_sizes_are_summed_per_season(self):
        aggregated = HistoryMixin._aggregate_transfer_details([
            self._tv(1, 1024),
            self._tv(2, 2048),
            self._tv(3, 4096),
        ])
        self.assertEqual(len(aggregated), 1)
        self.assertEqual(aggregated[0]["size"], 1024 + 2048 + 4096)
        self.assertEqual(aggregated[0]["episodes"], [1, 2, 3])

    def test_source_kept_from_first_item(self):
        aggregated = HistoryMixin._aggregate_transfer_details([
            self._tv(1, 10, source=""),
            self._tv(2, 10, source="夸克网盘 → 115网盘"),
        ])
        self.assertEqual(aggregated[0]["source"], "夸克网盘 → 115网盘")

    def test_movies_are_not_merged(self):
        aggregated = HistoryMixin._aggregate_transfer_details([
            {"type": "电影", "title": "A", "size": 100},
            {"type": "电影", "title": "B", "size": 200},
        ])
        self.assertEqual([item["size"] for item in aggregated], [100, 200])


if __name__ == "__main__":
    unittest.main()
