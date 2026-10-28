"""历史记录来源标识 discovered_via（P2b）。

覆盖：频道推送候选把 `tg:<频道>/<消息id>` 落到历史；显式字段优先；
没有来源时不写空键（老记录保持干净）。
"""

import sys
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
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()
core_pkg.CloudFile = MagicMock()

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
HistoryService = _history.HistoryService
# media_identity 由媒体模块提供，单测里给确定性返回值
_history.media_identity = lambda mediainfo: ("tmdb", "1")


class _Service(HistoryService):
    """只测构建逻辑，其余依赖全部走桩。"""

    def __init__(self):
        # OwnerDelegator 会把属性写穿到 _owner，这里只给只读桩
        object.__setattr__(self, "_cloud_drive", MagicMock(key="115", name="115网盘"))

    def _serialize_mediainfo(self, mediainfo):
        return {"media_id": "tmdb:1"}

    def _effective_mediainfo(self, subscribe, mediainfo):
        return mediainfo

    def _resource_history_meta(self, resource, share_url):
        return {}

    def _resource_provider_for_url(self, share_url):
        return None

    def _history_task_types(self, item):
        return ["追更"]


def _mediainfo():
    info = MagicMock()
    info.title = "凡人修仙传"
    info.year = "2026"
    info.tmdb_id = 1
    info.imdb_id = None
    info.tvdb_id = None
    info.douban_id = None
    info.bangumi_id = None
    info.anilist_id = None
    info.category = None
    info.episode_group = None
    info.get_poster_image.return_value = ""
    info.type.value = "电视剧"
    return info


class TestDiscoveredVia(unittest.TestCase):

    def _build(self, subscribe, **fields):
        return _Service()._build_transfer_history_item(
            mediainfo=_mediainfo(),
            subscribe=subscribe,
            status="成功",
            share_url="https://pan.quark.cn/s/abc",
            file_name="凡人修仙传.S01E04.2160p.mkv",
            source_file_name="凡人修仙传.S01E04.2160p.mkv",
            cloud_dir="/影视",
            **fields,
        )

    def test_value_from_subscribe_attribute(self):
        subscribe = MagicMock(_discovered_via="tg:@QukanMovie/12281")
        item = self._build(subscribe)
        self.assertEqual(item["discovered_via"], "tg:@QukanMovie/12281")

    def test_explicit_field_wins(self):
        subscribe = MagicMock(_discovered_via="tg:@QukanMovie/1")
        item = self._build(subscribe, discovered_via="tg:@other/9")
        self.assertEqual(item["discovered_via"], "tg:@other/9")

    def test_absent_when_no_source(self):
        subscribe = MagicMock(spec=[])
        item = self._build(subscribe)
        self.assertNotIn("discovered_via", item)

    def test_blank_attribute_ignored(self):
        subscribe = MagicMock(_discovered_via="   ")
        item = self._build(subscribe)
        self.assertNotIn("discovered_via", item)


if __name__ == "__main__":
    unittest.main()
