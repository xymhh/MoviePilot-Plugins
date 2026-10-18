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
normalize_channel_rules = tg_module.normalize_channel_rules
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


class ChannelRulesTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_rules_are_normalized_per_channel(self):
        rules = normalize_channel_rules([
            {
                "channel": "https://t.me/demo",
                "enabled": False,
                "keywords": "4K，合集\nHDR",
                "exclude_keywords": ["广告", "拉群"],
                "scan_limit": 999,
            },
            {"channel": "demo", "keywords": ["重复频道"]},
            {"channel": ""},
        ])
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertEqual(rule["channel"], "@demo")
        self.assertFalse(rule["enabled"])
        self.assertEqual(rule["keywords"], ["4k", "合集", "hdr"])
        self.assertEqual(rule["exclude_keywords"], ["广告", "拉群"])
        self.assertEqual(rule["scan_limit"], 200)

    def test_legacy_config_expands_to_each_channel(self):
        rules = normalize_channel_rules(
            [], channels=["@a", "https://t.me/b"], keywords=["4K"],
            exclude_keywords=["广告"], scan_limit=15,
        )
        self.assertEqual([rule["channel"] for rule in rules], ["@a", "@b"])
        for rule in rules:
            self.assertEqual(rule["keywords"], ["4k"])
            self.assertEqual(rule["exclude_keywords"], ["广告"])
            self.assertEqual(rule["scan_limit"], 15)

    def test_json_string_payload(self):
        rules = normalize_channel_rules(
            '[{"channel":"@a","keywords":"4K","scan_limit":3}]'
        )
        self.assertEqual(rules[0]["keywords"], ["4k"])
        self.assertEqual(rules[0]["scan_limit"], 3)

    def test_auto_transfer_fields_are_parsed(self):
        rules = normalize_channel_rules([{
            "channel": "@a",
            "auto_transfer": True,
            "auto_subscribe_id": "42",
        }])
        self.assertTrue(rules[0]["auto_transfer"])
        self.assertEqual(rules[0]["auto_subscribe_id"], 42)
        self.assertFalse(normalize_channel_rules(["@b"])[0]["auto_transfer"])

    def test_auto_transfer_marks_item_and_reports_failure(self):
        calls = []

        def handler(item, subscribe_id):
            calls.append((item["links"], subscribe_id))
            if subscribe_id == 7:
                return {"success": True}
            return {"success": False, "message": "订阅不存在"}

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "auto_transfer": True,
                            "auto_subscribe_id": 7}],
            auto_transfer_handler=handler,
        )
        ok_item = {"links": ["https://pan.quark.cn/s/ok"], "status": "new"}
        bad_item = {"links": ["https://pan.quark.cn/s/bad"], "status": "new"}
        stats = monitor._run_auto_transfers([
            (ok_item, {"channel": "@a", "auto_subscribe_id": 7}),
            (bad_item, {"channel": "@b", "auto_subscribe_id": 9}),
        ])
        self.assertEqual(calls, [(["https://pan.quark.cn/s/ok"], 7),
                                 (["https://pan.quark.cn/s/bad"], 9)])
        self.assertEqual(ok_item["status"], "transferred")
        self.assertEqual(ok_item["subscribe_id"], 7)
        self.assertEqual(bad_item["status"], "new")
        self.assertEqual(bad_item["auto_transfer_error"], "订阅不存在")
        self.assertEqual(stats, {"auto_transferred": 1, "auto_failed": 1})


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
            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_entity(self, raw):
                return FakeEntity()

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                # 模拟 Telethon：默认倒序（最新在前），reverse=True 时升序
                rows = [
                    FakeMessage(1, "https://pan.quark.cn/s/aaa"),
                    FakeMessage(2, "https://pan.quark.cn/s/bbb"),
                ]
                return rows if reverse else list(reversed(rows))

        monitor = self._monitor()
        monitor._new_client = lambda session="": FakeClient()
        first = monitor.scan(trigger="测试")
        self.assertTrue(first["ok"])
        self.assertEqual(first["added"], 2)
        items = monitor.list_items()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["links"], ["https://pan.quark.cn/s/bbb"])
        self.assertEqual(items[0]["types"], ["quark"])

        # 同一批消息再扫一次：靠 last_id + 链接去重，不应重复入库
        monitor._new_client = lambda session="": FakeClient()
        second = monitor.scan(trigger="测试")
        self.assertEqual(second["added"], 0)
        self.assertEqual(len(monitor.list_items()), 2)

    def test_per_channel_rules_are_independent(self):
        class FakeMessage:
            def __init__(self, mid, text):
                self.id = mid
                self.message = text
                self.date = None

        class FakeEntity:
            def __init__(self, eid, username):
                self.id = eid
                self.title = username
                self.username = username

        entities = {"@a": FakeEntity(1, "a"), "@b": FakeEntity(2, "b")}
        messages = {
            "a": [FakeMessage(1, "https://pan.quark.cn/s/a1080"),
                  FakeMessage(2, "4K https://pan.quark.cn/s/a4k")],
            "b": [FakeMessage(1, "https://pan.quark.cn/s/bnormal"),
                  FakeMessage(2, "4K 广告 https://pan.quark.cn/s/bad")],
        }
        limits = {}

        class FakeClient:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_entity(self, raw):
                return entities[raw]

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                rows = list(messages[entity.username])
                limits[entity.username] = limit
                if reverse:
                    return [row for row in rows if row.id > min_id]
                return list(reversed(rows))[:limit]

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=True, api_id=1, api_hash="hash", session="session",
            channel_rules=[
                {"channel": "@a", "keywords": "4K", "scan_limit": 1},
                {"channel": "@b", "exclude_keywords": "广告", "scan_limit": 2},
            ],
            interval_minutes=10, max_items=100,
        )
        monitor.stop()
        monitor._new_client = lambda session="": FakeClient()
        result = monitor.scan(trigger="测试")
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["channels"], 2)
        self.assertEqual(limits, {"a": 1, "b": 2})
        links = sorted(item["links"][0] for item in monitor.list_items())
        self.assertEqual(links, [
            "https://pan.quark.cn/s/a4k",
            "https://pan.quark.cn/s/bnormal",
        ])

    def test_disabled_scan_is_noop(self):
        monitor = TgChannelMonitor(self.owner)
        result = monitor.scan()
        self.assertFalse(result["ok"])


class NormalizeChannelTest(unittest.TestCase):
    def test_invite_link_and_list_repr(self):
        self.assertEqual(
            tg_module.normalize_channel("https://t.me/+Jc37JCr1diEzNDMx"),
            "https://t.me/+Jc37JCr1diEzNDMx",
        )
        # MP 把多行字段存成列表字符串时，不能把括号引号带进去
        self.assertEqual(
            tg_module.normalize_channel("['https://t.me/+Jc37JCr1diEzNDMx']"),
            "https://t.me/+Jc37JCr1diEzNDMx",
        )
        self.assertEqual(tg_module.normalize_channel("@some"), "@some")
        self.assertEqual(tg_module.normalize_channel("t.me/some"), "@some")


class CodeDeliveryTest(unittest.TestCase):
    def test_mapping(self):
        class T:
            def __init__(self, name):
                self.__class__.__name__ = name

        for name, expected in [
            ("SentCodeTypeApp", "app"),
            ("SentCodeTypeSms", "sms"),
            ("SentCodeTypeCall", "call"),
            ("SentCodeTypeFlashCall", "flashcall"),
            ("SomethingElse", "unknown"),
        ]:
            sent = type("Sent", (), {"type": T(name)})()
            self.assertEqual(tg_module._code_delivery(sent), expected)


class TgMonitorApiImportTest(unittest.TestCase):
    """回归（1.6.57 真机加载失败）：api.tg 必须从 core 取 OwnerDelegator。

    1.6.57 里写成 ``from . import OwnerDelegator``，MP 加载插件时
    ``core.api`` 尚未初始化完成 → ImportError: cannot import name
    'OwnerDelegator' from partially initialized module ...core.api，
    整个插件加载失败、无法更新。
    """

    def test_tg_api_imports_owner_delegator_from_core(self):
        import types

        ensure_package("app.db")
        ensure_package("app.db.subscribe_oper")
        core.OwnerDelegator = type("OwnerDelegator", (), {
            "__init__": lambda self, owner=None: None,
        })
        api_pkg = types.ModuleType("cloudsubscribex.core.api")
        api_pkg.__path__ = [
            os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")
        ]
        sys.modules["cloudsubscribex.core.api"] = api_pkg
        setattr(core, "api", api_pkg)
        sync_stub = types.ModuleType("cloudsubscribex.core.api.sync")
        sync_stub.SyncApi = type("SyncApi", (), {})
        sys.modules["cloudsubscribex.core.api.sync"] = sync_stub
        setattr(api_pkg, "sync", sync_stub)

        module = load_module(
            "cloudsubscribex.core.api.tg",
            "plugins.v2/cloudsubscribex/core/api/tg.py",
        )
        self.assertTrue(hasattr(module, "TgMonitorApi"))


if __name__ == "__main__":
    unittest.main()
