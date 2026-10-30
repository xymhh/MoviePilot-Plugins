"""频道消息的集数范围解析与下发（1.7.110）。

线上背景（2026-10-30）：一条「小城良方 S01E20」消息进来，手动转存链路被强制
``manual_upgrade=True``，随按整季建立基线、整季洗版，单集消息重下了
E01–E20（4.3 GB/集）。本测试固定三件事：

1. ``parse_message_episode_scope`` 能从标题里读出单集 / 区间 / 更新进度 / 全集；
2. 队列条目带上 ``target_episodes``，同订阅同季的多个单元取并集；
3. 集数范围能一路下沉到订阅对象（``_target_episodes``），供洗版链裁剪基线。
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
    "app.db",
    "app.db.models",
    "app.db.models.subscribe",
    "app.db.subscribe_oper",
    "app.core",
    "app.core.config",
    "app.core.cache",
)
# 必须在加载 core/services/sync.py 之前注入（该模块在 import 时取 MediaType）
MEDIA_TYPE = set_media_type()

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
search = ensure_package("cloudsubscribex.search")
search.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "search")]
if not isinstance(getattr(core, "OwnerDelegator", None), type):
    core.OwnerDelegator = OwnerDelegator
for _attr in ("CloudDriveCapability", "CloudDriveProvider", "CloudFile", "SearchCapability"):
    if _attr not in vars(core):
        setattr(core, _attr, MagicMock())
ensure_package("cloudsubscribex.core.services")
ensure_package("cloudsubscribex.handlers")
ensure_package("cloudsubscribex.handlers.sync")
ensure_package("cloudsubscribex.utils")
if "MediaFileParser" not in vars(sys.modules["cloudsubscribex.utils"]):
    sys.modules["cloudsubscribex.utils"].MediaFileParser = MagicMock()

if "cloudsubscribex.core.services.runtime" not in sys.modules:
    register_module("cloudsubscribex.core.services.runtime", sync_lock=MagicMock())
if "cloudsubscribex.core.media" not in sys.modules:
    register_module(
        "cloudsubscribex.core.media",
        media_identity=MagicMock(),
        tmdb_id_of=MagicMock(),
    )
if "cloudsubscribex.utils.cache" not in sys.modules:
    register_module("cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock())
if "cloudsubscribex.handlers.sync.utils" not in sys.modules:
    load_module(
        "cloudsubscribex.handlers.sync.utils",
        "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
    )

tg_match_module = load_module(
    "cloudsubscribex.core.tg_match",
    "plugins.v2/cloudsubscribex/core/tg_match.py",
)
parse_message_episode_scope = tg_match_module.parse_message_episode_scope

tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)
TgChannelMonitor = tg_module.TgChannelMonitor

if "cloudsubscribex.core.services.skip_stats" not in sys.modules:
    load_module(
        "cloudsubscribex.core.services.skip_stats",
        "plugins.v2/cloudsubscribex/core/services/skip_stats.py",
)
load_module(
    "cloudsubscribex.search.types",
    "plugins.v2/cloudsubscribex/search/types.py",
)

sync_module = load_module(
    "cloudsubscribex.core.services.sync",
    "plugins.v2/cloudsubscribex/core/services/sync.py",
)
SyncExecutionService = sync_module.SyncExecutionService

MEDIA_TYPE = set_media_type()


class _FakeOwner:
    def __init__(self, root):
        self.root = root

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        pass


class EpisodeScopeParseTest(unittest.TestCase):

    def test_single_episode(self):
        scope = parse_message_episode_scope("小城良方 S01E20 4K HDR")
        self.assertEqual(scope["episodes"], [20])
        self.assertEqual(scope["season"], 1)
        self.assertEqual(scope["mode"], "single")

    def test_chinese_episode(self):
        self.assertEqual(
            parse_message_episode_scope("小城良方 第12集")["episodes"], [12]
        )

    def test_range(self):
        scope = parse_message_episode_scope("玉簟秋 19-20集 1080P")
        self.assertEqual(scope["episodes"], [19, 20])
        self.assertEqual(scope["mode"], "range")

    def test_plain_range_without_unit(self):
        self.assertEqual(
            parse_message_episode_scope("嫁金钗 1-8")["episodes"],
            list(range(1, 9)),
        )

    def test_update_progress(self):
        scope = parse_message_episode_scope("尚公主 更至28集")
        self.assertEqual(scope["episodes"], list(range(1, 29)))
        self.assertEqual(scope["mode"], "update")

    def test_full_season(self):
        scope = parse_message_episode_scope("茶是故乡浓 全29集 1080p")
        self.assertEqual(scope["episodes"], list(range(1, 30)))
        self.assertEqual(scope["mode"], "full")

    def test_variety_period_is_not_episode(self):
        """综艺按「期」更新，期号与订阅集数不同源，不做集数推断。"""
        scope = parse_message_episode_scope(
            "现在就出发 第四季(2026) 更至10.18期 [旅行/综艺]"
        )
        self.assertIsNone(scope["episodes"])
        self.assertEqual(scope["mode"], "variety")

    def test_unknown_title_returns_none(self):
        scope = parse_message_episode_scope("某部电影 2026 1080p BluRay")
        self.assertIsNone(scope["episodes"])

    def test_absurd_range_is_dropped(self):
        """误识别的超大范围（年份区间等）回退到不限集，而不是死循环。"""
        scope = parse_message_episode_scope("某剧 2024-2026")
        self.assertIsNone(scope["episodes"])


class QueueTargetEpisodesTest(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.monitor = TgChannelMonitor(_FakeOwner(Path(self.tempdir.name)))
        self.monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            auto_transfer_handler=lambda item, subscribe_id: {},
            subscribe_provider=lambda: [
                {"id": 7, "name": "小城良方", "season": 1, "type": "电视剧",
                 "lack_episode": 5},
            ],
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_item_carries_target_episodes(self):
        item = {"id": "e20", "links": ["https://pan.quark.cn/s/x"],
                "title": "小城良方 S01E20 4K", "status": "new"}
        self.assertEqual(self.monitor._target_episodes_for(item), [20])

    def test_merged_unit_unions_episodes(self):
        first = {"id": "e20", "links": ["https://pan.quark.cn/s/a"],
                 "title": "小城良方 S01E20 4K", "status": "new"}
        second = {"id": "e21", "links": ["https://pan.quark.cn/s/b"],
                  "title": "小城良方 S01E21 1080p", "status": "new"}
        stats, to_queue = self.monitor._match_and_enqueue(
            [(first, {}), (second, {})]
        )
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(len(to_queue), 1)
        self.assertEqual(to_queue[0]["target_episodes"], [20, 21])
        # 缺集订阅（lack_episode=5）→ 补集优先级，不再一律洗版
        self.assertEqual(
            to_queue[0]["queue_priority"], tg_module.QUEUE_PRIORITY_MISSING
        )

    def test_full_season_merges_upward(self):
        """「全24集」与单集消息合并 → 目标集取并集 1..24。"""
        first = {"id": "e20", "links": ["https://pan.quark.cn/s/a"],
                 "title": "小城良方 S01E20 4K", "status": "new"}
        second = {"id": "full", "links": ["https://pan.quark.cn/s/b"],
                  "title": "小城良方 全24集", "status": "new"}
        _stats, to_queue = self.monitor._match_and_enqueue(
            [(first, {}), (second, {})]
        )
        self.assertEqual(len(to_queue), 1)
        self.assertEqual(to_queue[0]["target_episodes"], list(range(1, 25)))

    def test_unrestricted_item_clears_episode_limit(self):
        """完全读不出集数的消息并进来 → 整个单元不再限制集数，避免漏转。"""
        first = {"id": "e20", "links": ["https://pan.quark.cn/s/a"],
                 "title": "小城良方 S01E20 4K", "status": "new"}
        second = {"id": "unknown", "links": ["https://pan.quark.cn/s/b"],
                  "title": "小城良方 4K 原盘", "status": "new"}
        _stats, to_queue = self.monitor._match_and_enqueue(
            [(first, {}), (second, {})]
        )
        self.assertEqual(len(to_queue), 1)
        self.assertNotIn("target_episodes", to_queue[0])


class TargetEpisodePlumbingTest(unittest.TestCase):

    def test_scope_applies_to_tv_only(self):
        tv = SimpleNamespace(id=1, type=MEDIA_TYPE.TV.value, _manual_upgrade=True)
        movie = SimpleNamespace(id=2, type=MEDIA_TYPE.MOVIE.value)
        scoped = SyncExecutionService._apply_target_episodes(
            [tv, movie], ["20", 21, 0, -3, "x"]
        )
        self.assertEqual(scoped, {20, 21})
        self.assertEqual(tv._target_episodes, {20, 21})
        self.assertFalse(hasattr(movie, "_target_episodes"))

    def test_empty_scope_is_noop(self):
        tv = SimpleNamespace(id=1, type=MEDIA_TYPE.TV.value)
        self.assertEqual(
            SyncExecutionService._apply_target_episodes([tv], []), set()
        )
        self.assertFalse(hasattr(tv, "_target_episodes"))


if __name__ == "__main__":
    unittest.main()
