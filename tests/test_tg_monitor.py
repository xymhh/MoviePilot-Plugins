"""TG 频道监控单测：链接抽取、频道归一化、条目去重与入库。"""

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

# 真实包层级：tg_monitor 通过 ``..search.types`` 复用域名表
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

extract_share_links = tg_module.extract_share_links
normalize_channel = tg_module.normalize_channel
TgChannelMonitor = tg_module.TgChannelMonitor


class ExtractLinksTest(unittest.TestCase):
    def test_quark_and_115(self):
        text = (
            "【4K】沙丘2\n"
            "夸克：https://pan.quark.cn/s/abcdef123456\n"
            "115：https://115.com/s/swx1abcd?password=1234\n"
            "闲聊 t.me/somechannel"
        )
        links = extract_share_links(text)
        self.assertEqual([url for url, _ in links], [
            "https://pan.quark.cn/s/abcdef123456",
            "https://115.com/s/swx1abcd?password=1234",
        ])
        self.assertEqual([rtype for _, rtype in links], ["quark", "115"])

    def test_magnet_and_ed2k(self):
        text = (
            "magnet:?xt=urn:btih:ABCDEF1234567890ABCDEF1234567890ABCDEF12 "
            "ed2k://|file|movie.mkv|12345|ABCDEF|/"
        )
        links = extract_share_links(text)
        self.assertEqual([rtype for _, rtype in links], ["magnet", "ed2k"])

    def test_unknown_domain_ignored(self):
        self.assertEqual(extract_share_links("https://example.com/s/abc"), [])

    def test_trailing_punctuation_trimmed(self):
        links = extract_share_links("链接：https://pan.quark.cn/s/abcd，速存。")
        self.assertEqual(links[0][0], "https://pan.quark.cn/s/abcd")


class NormalizeChannelTest(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(normalize_channel("@abc"), "@abc")
        self.assertEqual(normalize_channel("abc"), "@abc")
        self.assertEqual(normalize_channel("https://t.me/abc"), "@abc")
        self.assertEqual(normalize_channel("t.me/abc"), "t.me/abc")
        self.assertEqual(normalize_channel("-1001234567890"), "-1001234567890")
        self.assertEqual(normalize_channel("https://t.me/+AbCdEf"), "https://t.me/+AbCdEf")
        self.assertEqual(normalize_channel("https://t.me/c/123456/789"), "c/123456/789")


class FakeOwner:
    """最小插件替身：提供数据目录和配置持久化。"""

    def __init__(self, root):
        self.root = root
        self.persisted = {}

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        self.persisted.update(updates)


class ScanTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def _monitor(self):
        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=True, api_id=1, api_hash="hash", session="session",
            channels=["@demo"], keywords=[], exclude_keywords=[],
            scan_limit=10, interval_minutes=10, max_items=100,
        )
        monitor.stop()  # 不走后台线程，测试里手动调用 scan
        return monitor

    def test_scan_dedup_and_store(self):
        class FakeMessage:
            def __init__(self, mid, text):
                self.id = mid
                self.message = text
                self.date = None

        class FakeEntity:
            id = 999
            title = "测试频道"
            username = "demo"

        class FakeClient:
            def get_entity(self, raw):
                return FakeEntity()

            def iter_messages(self, entity, limit, min_id=0, reverse=False):
                # 模拟 Telethon：默认倒序（最新在前），reverse=True 时升序
                rows = [
                    FakeMessage(1, "https://pan.quark.cn/s/aaa"),
                    FakeMessage(2, "https://pan.quark.cn/s/bbb"),
                ]
                return rows if reverse else list(reversed(rows))

        monitor = self._monitor()
        monitor._client = FakeClient()
        first = monitor.scan(trigger="测试")
        self.assertTrue(first["ok"])
        self.assertEqual(first["added"], 2)
        items = monitor.list_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["links"], ["https://pan.quark.cn/s/bbb"])
        self.assertEqual(items[0]["types"], ["quark"])

        # 同一批消息再扫一次：靠 last_id + 链接去重，不应重复入库
        monitor._client = FakeClient()
        second = monitor.scan(trigger="测试")
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(monitor.list_items()), 2)

    def test_disabled_scan_is_noop(self):
        monitor = TgChannelMonitor(self.owner)
        result = monitor.scan()
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
