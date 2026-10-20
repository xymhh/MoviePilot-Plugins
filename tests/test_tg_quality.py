"""频道监控画质偏好：解析、排序与软回退。"""

import os
import sys
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

    def test_best_quality_first_and_lower_kept_pending(self):
        calls = []
        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=lambda item, subscribe_id: (
                calls.append(item["title"])
                or {"success": True, "data": {"transferred": 1}}
            ),
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
        stats = monitor._run_auto_transfers([(lower, {}), (better, {})])
        self.assertEqual(calls, [better["title"]])
        self.assertEqual(better["status"], "transferred")
        self.assertEqual(lower["status"], "new")
        self.assertEqual(lower["match_status"], "pending")
        self.assertIn("更优画质", lower["match_reason"])
        self.assertEqual(stats["auto_transferred"], 1)
        self.assertEqual(stats["auto_pending"], 1)

    def test_lower_quality_falls_back_after_best_fails(self):
        calls = []

        def handler(item, subscribe_id):
            calls.append(item["title"])
            if "2160p" in item["title"]:
                return {"success": False, "message": "最优画质转存失败"}
            return {"success": True, "data": {"transferred": 1}}

        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=handler,
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
        first = monitor._run_auto_transfers([(lower, {}), (better, {})])
        self.assertEqual(first["auto_failed"], 1)
        self.assertEqual(first["auto_pending"], 1)
        self.assertEqual(lower["status"], "new")

        second = monitor._run_auto_transfers([(lower, {})])
        self.assertEqual(calls, [better["title"], lower["title"]])
        self.assertEqual(second["auto_transferred"], 1)
        self.assertEqual(lower["status"], "transferred")

    def test_unknown_quality_passes_without_hold(self):
        calls = []
        monitor = self._monitor()
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            subscribe_provider=lambda: [
                {"id": 7, "name": "测试剧", "season": 1, "type": "电视剧"},
            ],
            quality_preferences={"enabled": True},
            auto_transfer_handler=lambda item, subscribe_id: (
                calls.append(item["title"])
                or {"success": True, "data": {"transferred": 1}}
            ),
        )
        item = {
            "id": "unknown", "links": ["https://pan.quark.cn/s/c"],
            "title": "测试剧 第一季 第1集 中字",
            "status": "new",
        }
        monitor._run_auto_transfers([(item, {})])
        self.assertEqual(calls, [item["title"]])
        self.assertEqual(item["status"], "transferred")


if __name__ == "__main__":
    unittest.main()
