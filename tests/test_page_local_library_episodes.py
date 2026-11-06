"""追更列表「已入库」口径：本地媒体库实测集号。

历史故障（v1.9.53 线上，订阅 35「律界战争」）：Emby 里 E1/E2 真实存在，
但卡片显示「待补齐 4 集」。原因是进度只取 MoviePilot 订阅 ``note`` ∪ 插件转存历史，
而该订阅 ``start_episode=3``（订阅从第 3 集才追），1、2 集是在订阅之外进的库，
两处都不含 → 永远补不上。

本用例钉死新口径：本地 STRM 目录里真实存在的集一定算已入库，且只在命中时做并集
（不改变原有结果、不把不存在的集算进来）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

# 这里注册的是 placeholders；加载完 page.py 必须把它们从 sys.modules 里撤掉，
# 否则同进程内后续用例（如 test_subscribe_media_type_filter）拿到的是缺属性的
# 假 config，`from ..config import DEFAULT_AUTO_SUBSCRIBE_USERNAME` 会 ImportError。
_MODULES_BEFORE_STUBS = set(sys.modules)

register_module("pytz", timezone=MagicMock(), utc=MagicMock())
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_mock("app.core.metainfo")
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_module("app.schemas.types", MediaType=MagicMock(), NotificationType=MagicMock())
register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.db.subscribe_oper")

register_module("cloudsubscribex", package=True)
register_module(
    "cloudsubscribex.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
)
register_module("cloudsubscribex.core.config", UIConfig=MagicMock())
register_module(
    "cloudsubscribex.core.api.season_rules",
    _season_total_episodes=MagicMock(return_value=0),
    _select_season_numbers=MagicMock(return_value=[1]),
)
register_module(
    "cloudsubscribex.core.media",
    call_with_supported_kwargs=MagicMock(),
    recognize_media=MagicMock(),
)
register_module("cloudsubscribex.drive", package=True)
register_module("cloudsubscribex.drive.scanner", DriverRegistry=MagicMock())
register_module("cloudsubscribex.search", package=True)
register_module("cloudsubscribex.search.matching", is_anime_media=MagicMock(return_value=False))
register_module("cloudsubscribex.search.scanner", SearchSourceRegistry=MagicMock())
register_module(
    "cloudsubscribex.search.types",
    resource_type_aliases=MagicMock(return_value={}),
    resource_type_catalog=MagicMock(return_value=[]),
)
register_module("cloudsubscribex.utils", package=True)
register_module(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

page_module = load_module(
    "cloudsubscribex.core.api.page",
    "plugins.v2/cloudsubscribex/core/api/page.py",
)

# 撤掉本文件注册的占位模块（保留已加载的 page 模块），恢复对其它用例的零污染
for _name in [name for name in sys.modules if name not in _MODULES_BEFORE_STUBS]:
    if _name != "cloudsubscribex.core.api.page":
        sys.modules.pop(_name, None)


class _Owner:
    def __init__(self, root):
        self._local_resource_path = str(root)


class TestLocalLibraryEpisodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        page_module._LOCAL_LIBRARY_STATE.update({"at": 0.0, "root": "", "index": {}})

    def tearDown(self):
        self._tmp.cleanup()
        page_module._LOCAL_LIBRARY_STATE.update({"at": 0.0, "root": "", "index": {}})

    def _make_season(self, title_dir: str, season: int, episodes, suffix=".strm"):
        season_dir = self.root / "剧集" / "欧美剧" / title_dir / f"Season {season}"
        season_dir.mkdir(parents=True, exist_ok=True)
        for number in episodes:
            (season_dir / f"某剧 - S{season:02d}E{number:02d} - 第{number}集{suffix}").write_text("x")

    def test_season_dir_patterns(self):
        self.assertEqual(page_module._match_season_dir("Season 1"), 1)
        self.assertEqual(page_module._match_season_dir("S02"), 2)
        self.assertEqual(page_module._match_season_dir("第3季"), 3)
        self.assertIsNone(page_module._match_season_dir("Season 0"))
        self.assertIsNone(page_module._match_season_dir("Specials"))

    def test_parse_episode_names_multi_version(self):
        names = [
            "某剧 - S01E01 - 第1集.strm",
            "某剧 - S01E01 - 第1集.2160p.strm",
            "某剧 - S01E02 - 第2集.strm",
            "某剧 - S02E01 - 第1集.strm",
            "readme.txt",
        ]
        self.assertEqual(page_module._parse_episodes_from_names(names, 1), {1, 2})
        self.assertEqual(page_module._parse_episodes_from_names(names, 2), {1})

    def test_local_episodes_hit_by_tmdb(self):
        self._make_season("律界战争 (2026) {tmdb-298852}", 1, [1, 2, 3, 4, 5, 6])
        api = page_module.PageApi(_Owner(self.root))
        found = api._local_library_episodes(298852, "律界战争", "2026", 1)
        self.assertEqual(found, {1, 2, 3, 4, 5, 6})

    def test_local_episodes_fallback_by_title(self):
        self._make_season("律界战争 (2026)", 1, [1, 2])
        api = page_module.PageApi(_Owner(self.root))
        self.assertEqual(api._local_library_episodes(None, "律界战争", "2026", 1), {1, 2})
        # 年份写法不同也要兜底命中
        self.assertEqual(api._local_library_episodes(None, "律界战争", "2025", 1), {1, 2})

    def test_missing_root_returns_empty(self):
        api = page_module.PageApi(_Owner(self.root / "不存在"))
        self.assertEqual(api._local_library_episodes(298852, "律界战争", "2026", 1), set())

    def test_other_season_not_merged(self):
        self._make_season("律界战争 (2026) {tmdb-298852}", 2, [1, 2])
        api = page_module.PageApi(_Owner(self.root))
        self.assertEqual(api._local_library_episodes(298852, "律界战争", "2026", 1), set())


if __name__ == "__main__":
    unittest.main()
