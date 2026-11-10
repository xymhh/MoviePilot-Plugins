"""TG 未转存原因：长文案压缩成「定性词 · 证据」短句。"""

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

tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)

shorten = tg_module.TgChannelMonitor._shorten_skip_reason


class ShortenSkipReasonTest(unittest.TestCase):
    def test_episode_miss(self):
        raw = "资源集号与缺失不符（S01）：资源含 E01-E10，缺 E11"
        self.assertEqual(shorten(raw), "集号不符 · 有 E01-E10，缺 E11")

    def test_episode_miss_keeps_missing_part_complete(self):
        raw = "资源集号与缺失不符（S01）：资源含 E01-E12，缺 E13-E14、E17-E27"
        out = shorten(raw)
        self.assertIn("缺 E13-E14、E17-E27", out)
        self.assertLessEqual(len(out), 26)

    def test_title_mismatch_keeps_evidence(self):
        raw = "分享根目录与订阅标题不符（根目录：【M】 ⺩攵 ⺩鬼 單戈 爭 [港劇]）"
        out = shorten(raw)
        self.assertTrue(out.startswith("剧名不符 · 分享是《"))
        self.assertLessEqual(len(out), 26)

    def test_unparsable(self):
        raw = "解析不出目标季集号（含 10 个视频）：跨盘分享文件名与目录都没有 S01 的集号"
        self.assertEqual(shorten(raw), "认不出集号 · 10 个视频")

    def test_search_job_text(self):
        self.assertEqual(shorten("订阅搜索完成，未发现需要转存的新资源"), "本轮无新集")

    def test_owned(self):
        self.assertEqual(shorten("未转存：库中已完整存在，无需转存"), "已入库")

    def test_empty_falls_back(self):
        self.assertEqual(shorten(""), "无可用资源")
        self.assertEqual(shorten("   "), "无可用资源")

    def test_unknown_long_text_is_truncated(self):
        raw = "分享里的文件与订阅目标完全对不上而且还有很多额外说明文字用来测试截断行为是否生效"
        out = shorten(raw)
        self.assertLessEqual(len(out), 26)
        self.assertTrue(out.endswith("…"))

    def test_short_unknown_text_is_kept(self):
        self.assertEqual(shorten("分享失效"), "分享失效")


if __name__ == "__main__":
    unittest.main()
