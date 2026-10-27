"""频道监控画质偏好：解析、排序与软回退。"""

import os
import sys
import threading
import unittest

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
search = ensure_package("cloudsubscribex.search")
search.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "search")]

quality_module = load_module(
    "cloudsubscribex.core.tg_quality",
    "plugins.v2/cloudsubscribex/core/tg_quality.py",
)
tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)

parse_quality = quality_module.parse_quality
normalize_quality_preferences = quality_module.normalize_quality_preferences
TgChannelMonitor = tg_module.TgChannelMonitor


class QualityParseTest(unittest.TestCase):
    def test_aliases_and_label(self):
        quality = parse_quality(
            "测试电影 2026 2160p HDR10 H.265 DDP5.1 FRDS"
        )
        self.assertTrue(quality["rankable"])
        self.assertEqual(quality["resolution"], "4K")
        self.assertEqual(quality["hdr"], "HDR10")
        self.assertEqual(quality["codec"], "HEVC")
        self.assertEqual(quality["audio"], "DDP")
        self.assertEqual(quality["label"], "4K.HDR10.HEVC")

    def test_chinese_dv_subtitle_and_atmos_tags(self):
        quality = parse_quality(
            "黑帮领地 1-2季(2026)【更04集】【4K.DV 杜比视界 "
            "内封简繁英 杜比全景声】"
        )
        self.assertEqual(quality["hdr"], "DOLBY VISION")
        self.assertEqual(quality["subtitle"], "内封简繁英")
        self.assertEqual(quality["audio"], "ATMOS")
        self.assertEqual(
            quality["tags"],
            ["4K", "杜比视界", "内封简繁英", "杜比全景声"],
        )

    def test_free_and_audio_channel_tags(self):
        free = parse_quality("免费 4K 中字")
        self.assertEqual(free["tags"], ["免费", "4K", "中字"])
        audio = parse_quality("名称：各取所需 (2026) 4K WEB-DL "
                              "HDR10 H265 DDP 5.1")
        self.assertEqual(audio["tags"], ["4K", "HDR10", "DDP5.1"])
        aac = parse_quality("名称：冰雪尖刀连 (2023) 4K WEB-DL H265 AAC 2.0")
        self.assertEqual(aac["tags"], ["4K", "AAC2.0"])

    def test_unknown_quality_is_not_rankable(self):
        quality = parse_quality("测试剧 第一季 第1集 中字")
        self.assertFalse(quality["rankable"])
        self.assertEqual(quality["label"], "未知")

    def test_hdrip_is_not_mistaken_for_hdr(self):
        quality = parse_quality("测试电影 1080p HDRip AVC")
        self.assertEqual(quality["hdr"], None)

    def test_preferred_group_breaks_tie(self):
        prefs = normalize_quality_preferences({
            "preferred_groups": "FRDS\nQHstudIo",
        })
        preferred = parse_quality("测试 1080p HEVC FRDS", prefs)
        other = parse_quality("测试 1080p HEVC OTHER", prefs)
        self.assertEqual(preferred["group"], "FRDS")
        self.assertLess(
            preferred["rank_key"], other["rank_key"],
        )

    def test_quality_order_from_config(self):
        prefs = normalize_quality_preferences({
            "resolution_order": "1080P,4K,720P",
        })
        hd = parse_quality("测试 1080p", prefs)
        uhd = parse_quality("测试 2160p", prefs)
        self.assertLess(hd["rank_key"], uhd["rank_key"])


class QualitySelectionTest(unittest.TestCase):
    class Owner:
        pass

    def _monitor(self):
        monitor = TgChannelMonitor(self.Owner())
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={
                "enabled": True,
                "resolution_order": "4K,1080P,720P",
                "hdr_order": "HDR10,HDR,SDR",
                "codec_order": "HEVC,AVC,AV1",
            },
            auto_transfer_handler=lambda item, subscribe_id: (
                {"success": True, "data": {"transferred": 1}}
            ),
        )
        return monitor

    def test_best_quality_queued_lower_ignored(self):
        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=lambda item, subscribe_id: {},
        )
        better = {
            "id": "better", "links": ["https://pan.quark.cn/s/a"],
            "title": "测试剧 第一季 第1集 2160p HDR10 HEVC",
            "status": "new", "published_at": 200,
        }
        lower = {
            "id": "lower", "links": ["https://pan.quark.cn/s/b"],
            "title": "测试剧 第一季 第1集 1080p AVC",
            "status": "new", "published_at": 100,
        }
        stats, to_queue = monitor._match_and_enqueue([(lower, {}), (better, {})])
        self.assertEqual(better["status"], "queued")
        self.assertEqual(lower["status"], "ignored")
        self.assertIn("更优画质", lower["match_reason"])
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["ignored"], 1)
        self.assertEqual([row["id"] for row in to_queue], ["better"])

    def test_failed_best_is_reported_not_silently_retried(self):
        """最优候选失败后状态为 failed，由用户决定是否手动补转。"""
        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=lambda item, subscribe_id: {
                "success": False, "message": "最优画质转存失败",
            },
        )
        better = {
            "id": "better", "status": "queued", "matched_subscribe_id": 7,
            "links": ["https://pan.quark.cn/s/a"],
            "title": "测试剧 第一季 第1集 2160p HDR10 HEVC",
        }
        class _MemStore:
            def __init__(self, data):
                self.data = data

            def load(self, key, default=None):
                return self.data.get(key, default)

            def save(self, key, value):
                self.data[key] = value

        monitor._store = _MemStore({"items": [better]})
        monitor._process_transfer_items(["better"], threading.Event())
        row = monitor.list_items()[0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["auto_transfer_error"], "最优画质转存失败")

    def test_unknown_quality_passes_without_hold(self):
        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=lambda item, subscribe_id: {},
        )
        item = {
            "id": "unknown", "links": ["https://pan.quark.cn/s/c"],
            "title": "测试剧 第一季 第1集 中字",
            "status": "new",
        }
        monitor._match_and_enqueue([(item, {})])
        self.assertEqual(item["status"], "queued")

