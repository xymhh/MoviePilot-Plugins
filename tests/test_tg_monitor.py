"""TG 频道监控单测：链接抽取、频道归一化、条目去重与入库。"""

import asyncio
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
extract_message_links = tg_module.extract_message_links
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

    def test_text_url_entity_used_when_body_hides_link(self):
        class Entity:
            url = "https://115cdn.com/s/sws1fkl3zrk?password=t58d"

        class Message:
            message = "链接： 点击跳转"
            entities = [Entity()]
            media = None
            reply_markup = None

        links = extract_message_links(Message())
        self.assertEqual(links, [(
            "https://115cdn.com/s/sws1fkl3zrk?password=t58d", "115",
        )])

    def test_inline_button_url_used(self):
        class Button:
            url = "https://pan.quark.cn/s/button123"

        class Row:
            buttons = [Button()]

        class Markup:
            rows = [Row()]

        class Message:
            message = "资源见按钮"
            entities = []
            media = None
            reply_markup = Markup()

        self.assertEqual(extract_message_links(Message()), [(
            "https://pan.quark.cn/s/button123", "quark",
        )])


class FloodAndOutcomeTest(unittest.TestCase):
    def test_flood_seconds_from_attribute_and_text(self):
        class Flood(Exception):
            seconds = 667
        self.assertEqual(tg_module._flood_wait_seconds(Flood("wait")), 667)
        self.assertEqual(
            tg_module._flood_wait_seconds(
                Exception("A wait of 42 seconds is required")
            ),
            42,
        )
        self.assertEqual(tg_module._flood_wait_seconds(Exception("other")), 0)

    def test_auto_transfer_outcome(self):
        self.assertEqual(
            tg_module._auto_transfer_outcome(
                {"success": True, "data": {"transferred": 2}}
            ),
            ("transferred", ""),
        )
        outcome, message = tg_module._auto_transfer_outcome(
            {"success": True, "data": {"transferred": 0},
             "message": "同步完成，共转存 0 个文件"}
        )
        self.assertEqual(outcome, "skipped")
        self.assertIn("0 个文件", message)
        outcome, message = tg_module._auto_transfer_outcome(
            {"success": False, "message": "订阅不存在"}
        )
        self.assertEqual(outcome, "failed")
        self.assertIn("订阅不存在", message)


class ScanCursorTest(unittest.TestCase):
    def test_old_cursor_forces_backfill_once(self):
        self.assertEqual(tg_module._scan_start_id({"123": 900}, "123"), 0)
        self.assertEqual(
            tg_module._scan_start_id(
                {"123": 900, "123:logic_version": tg_module.SCAN_LOGIC_VERSION},
                "123",
            ),
            900,
        )


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

    def test_channel_wrapped_in_list_or_double_quotes(self):
        rules = normalize_channel_rules([
            {"channel": ["https://t.me/+AbCdEf"]},
            {"channel": r'''"['https://t.me/+GhIjKl']"'''},
        ])
        self.assertEqual(
            [rule["channel"] for rule in rules],
            ["https://t.me/+AbCdEf", "https://t.me/+GhIjKl"],
        )

    def test_json_string_payload(self):
        rules = normalize_channel_rules(
            '[{"channel":"@a","keywords":"4K","scan_limit":3}]'
        )
        self.assertEqual(rules[0]["keywords"], ["4k"])
        self.assertEqual(rules[0]["scan_limit"], 3)

    def test_monitor_mode_mapping(self):
        rules = normalize_channel_rules([
            {"channel": "@a", "monitor_mode": "subscriptions"},
            {"channel": "@b", "monitor_mode": "messages"},
            {"channel": "@c", "auto_transfer": True},
            {"channel": "@d"},
        ])
        self.assertEqual(
            [rule["monitor_mode"] for rule in rules],
            ["subscriptions", "messages", "subscriptions", "messages"],
        )
        self.assertEqual(
            [rule["auto_transfer"] for rule in rules],
            [True, False, True, False],
        )

    def test_subscriptions_mode_matches_any_subscription(self):
        rules = normalize_channel_rules([{
            "channel": "@a",
            "monitor_mode": "subscriptions",
        }])
        self.assertEqual(rules[0]["monitor_mode"], "subscriptions")

        calls = []

        def handler(item, subscribe_id):
            calls.append(subscribe_id)
            return {"success": True, "data": {"transferred": 1}}

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=rules,
            auto_transfer_handler=handler,
            subscribe_provider=lambda: [
                {"id": 201, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
        )
        item = {
            "links": ["https://pan.quark.cn/s/a"],
            "title": "现在就出发 第四季(2026) 第4集 [旅行/综艺]",
            "status": "new",
        }
        stats = monitor._run_auto_transfers([(item, rules[0])])
        self.assertEqual(calls, [201])
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(stats["auto_transferred"], 1)

    def test_auto_transfer_marks_item_and_reports_failure(self):
        calls = []

        def handler(item, subscribe_id):
            calls.append((item["links"], subscribe_id))
            if subscribe_id == 7:
                return {"success": True, "data": {"transferred": 1}}
            return {"success": False, "message": "订阅不存在"}

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            auto_transfer_handler=handler,
            subscribe_provider=lambda: [
                {"id": 7, "name": "现在就出发", "season": 4, "type": "电视剧"},
                {"id": 9, "name": "极限挑战", "season": 1, "type": "电视剧"},
            ],
        )
        ok_item = {
            "links": ["https://pan.quark.cn/s/ok"],
            "title": "现在就出发 第四季(2026) 第4集 [旅行/综艺]",
            "status": "new",
        }
        bad_item = {
            "links": ["https://pan.quark.cn/s/bad"],
            "title": "极限挑战 第一季(2026) 第1集 [旅行/综艺]",
            "status": "new",
        }
        stats = monitor._run_auto_transfers([
            (ok_item, {"channel": "@a", "monitor_mode": "subscriptions"}),
            (bad_item, {"channel": "@b", "monitor_mode": "subscriptions"}),
        ])
        self.assertEqual(calls, [(["https://pan.quark.cn/s/ok"], 7),
                                 (["https://pan.quark.cn/s/bad"], 9)])
        self.assertEqual(ok_item["status"], "transferred")
        self.assertEqual(ok_item["subscribe_id"], 7)
        self.assertEqual(bad_item["status"], "new")
        self.assertEqual(bad_item["auto_transfer_error"], "订阅不存在")
        self.assertEqual(
            stats,
            {"auto_transferred": 1, "auto_skipped": 0,
             "auto_failed": 1, "auto_pending": 0, "auto_ignored": 0},
        )

    def test_rematch_caps_transfers_per_scan(self):
        calls = []

        def handler(item, subscribe_id):
            calls.append(item["id"])
            return {"success": True, "data": {"transferred": 1}}

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            auto_transfer_handler=handler,
            subscribe_provider=lambda: [
                {"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
        )
        rule = {"channel": "@a", "monitor_mode": "subscriptions"}
        items = [
            {
                "id": f"item-{index}",
                "links": [f"https://pan.quark.cn/s/{index}"],
                "title": f"现在就出发 第四季(2026) 第{index}集",
                "status": "new",
                "match_status": "pending",
            }
            for index in range(1, 7)
        ]
        stats = monitor._run_auto_transfers(
            [(item, rule) for item in items],
            rematch_ids={item["id"] for item in items},
        )
        self.assertEqual(len(calls), tg_module.MAX_REMATCH_TRANSFERS)
        self.assertEqual(stats["auto_transferred"], 5)
        self.assertEqual(stats["auto_pending"], 1)
        self.assertEqual(
            sum(1 for item in items if item["status"] == "transferred"), 5,
        )
        self.assertEqual(items[-1]["status"], "new")
        self.assertIn("待下轮补转", items[-1]["match_reason"])

    def test_provider_matches_title_and_leaves_ambiguity_pending(self):
        calls = []

        def handler(item, subscribe_id):
            calls.append(subscribe_id)
            return {"success": True, "data": {"transferred": 1}}

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            auto_transfer_handler=handler,
            subscribe_provider=lambda: [
                {"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"},
                {"id": 114, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
        )
        item = {
            "links": ["https://pan.quark.cn/s/a"],
            "title": "现在就出发 第四季(2026) 第4集 [旅行/综艺]",
            "status": "new",
        }
        rule = {"channel": "@a", "monitor_mode": "subscriptions"}
        stats = monitor._run_auto_transfers([(item, rule)])
        # 1.7.33 起：同名 + 同季 + 同类型的重复登记不再算「多候选」，
        # 自动取 id 更大（更新）的一条；否则真实资源会被永久挂起。
        self.assertEqual(calls, [114])
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(stats["auto_transferred"], 1)

        unique_monitor = TgChannelMonitor(self.owner)
        unique_monitor.configure(
            enabled=False, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@a", "monitor_mode": "subscriptions"}],
            auto_transfer_handler=handler,
            subscribe_provider=lambda: [
                {"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
        )
        unique = {
            "links": ["https://pan.quark.cn/s/b"],
            "title": "现在就出发 第四季(2026) 第5集 [旅行/综艺]",
            "status": "new",
        }
        calls.clear()
        stats = unique_monitor._run_auto_transfers([(unique, rule)])
        self.assertEqual(calls, [113])
        self.assertEqual(unique["status"], "transferred")
        self.assertEqual(stats["auto_transferred"], 1)


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

    def test_list_items_backfills_quality_for_legacy_items(self):
        monitor = self._monitor()
        monitor._store.save("items", [
            {
                "id": "legacy:1",
                "title": "玉簟秋 (2026)【4K.高码率】【内嵌简中】",
                "status": "skipped",
            },
            {
                "id": "legacy:2",
                "title": "PSYREN 决战游戏 (2026)【1080P】",
                "status": "transferred",
            },
            {
                "id": "legacy:3",
                "title": "美人余 (2026)【4K.HDR】",
                "status": "skipped",
                "quality": {"label": "手工标签", "rankable": True},
            },
        ])

        items = {item["id"]: item for item in monitor.list_items()}

        self.assertEqual(items["legacy:1"]["quality"]["label"], "4K")
        self.assertEqual(items["legacy:2"]["quality"]["label"], "1080P")
        # 已有有效标签不覆盖，避免读接口改变人工/新逻辑结果；
        # 但旧对象没有 tags 时补展示字段
        self.assertEqual(items["legacy:3"]["quality"]["label"], "手工标签")
        self.assertEqual(
            items["legacy:3"]["quality"]["tags"],
            ["4K", "HDR"],
        )

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

    def test_scan_rematches_pending_item_after_parser_fix(self):
        calls = []

        class FakeEntity:
            id = 999
            access_hash = 123
            title = "测试频道"
            username = "demo"

        class FakeClient:
            async def get_entity(self, raw):
                return FakeEntity()

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                return []

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=True, api_id=1, api_hash="hash", session="session",
            channel_rules=[{"channel": "@demo", "monitor_mode": "subscriptions"}],
            keywords=[], exclude_keywords=[],
            scan_limit=10, interval_minutes=10, max_items=100,
            auto_transfer_handler=lambda item, subscribe_id: (
                calls.append((item["id"], subscribe_id))
                or {"success": True, "data": {"transferred": 1}}
            ),
            subscribe_provider=lambda: [
                {"id": 113, "name": "现在就出发", "season": 4, "type": "电视剧"},
            ],
        )
        monitor.stop()
        monitor._store.save("state", {
            "resolved:@demo": "999",
            "resolved_hash:@demo": "123",
            "999": 2,
            "999:logic_version": tg_module.SCAN_LOGIC_VERSION,
        })
        monitor._store.save("items", [{
            "id": "999:1",
            "channel": "@demo",
            "channel_title": "测试频道",
            "channel_id": "999",
            "message_id": 1,
            "title": "名称：现在就出发 第四季(2026) 第1集",
            "links": ["https://pan.quark.cn/s/old"],
            "types": ["quark"],
            "status": "new",
            "match_status": "pending",
            "match_reason": "匹配度过低（88），留待手动选择",
        }])
        result = asyncio.run(monitor._scan_with_client(FakeClient(), "测试"))
        self.assertEqual(calls, [("999:1", 113)])
        self.assertEqual(result["auto_transferred"], 1)
        item = monitor.list_items()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(item["match_status"], "transferred")
        self.assertIn("标题精确", item["match_reason"])

    def test_incremental_scan_fetches_multiple_pages_to_catch_up(self):
        class FakeEntity:
            id = 999
            access_hash = 123
            title = "测试频道"
            username = "demo"

        class FakeMessage:
            def __init__(self, message_id):
                self.id = message_id
                self.message = f"资源 https://pan.quark.cn/s/{message_id}"
                self.date = None
                self.entities = []
                self.media = None
                self.reply_markup = None

        class FakeClient:
            async def get_entity(self, raw):
                return FakeEntity()

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                rows = [FakeMessage(index) for index in range(1, 8)]
                if reverse:
                    return [row for row in rows if row.id > min_id][:limit]
                return list(reversed(rows))[:limit]

        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=True, api_id=1, api_hash="hash", session="session",
            channels=["@demo"], keywords=[], exclude_keywords=[],
            scan_limit=2, interval_minutes=10, max_items=100,
        )
        monitor.stop()
        monitor._store.save("state", {
            "resolved:@demo": "999",
            "resolved_hash:@demo": "123",
            "999": 1,
            "999:logic_version": tg_module.SCAN_LOGIC_VERSION,
        })
        result = asyncio.run(monitor._scan_with_client(FakeClient(), "测试"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scanned"], 6)
        self.assertEqual(result["added"], 6)
        self.assertEqual(
            [item["message_id"] for item in monitor.list_items()],
            [7, 6, 5, 4, 3, 2],
        )
        state = monitor._store.load("state", {})
        self.assertEqual(state["999"], 7)

    def _monitor_for(self, channels):
        monitor = TgChannelMonitor(self.owner)
        monitor.configure(
            enabled=True, api_id=1, api_hash="hash", session="session",
            channels=channels, keywords=[], exclude_keywords=[],
            scan_limit=10, interval_minutes=10, max_items=100,
        )
        monitor.stop()
        return monitor

    def test_access_hash_cached_and_invite_link_never_rechecked(self):
        calls = []

        class FakeEntity:
            id = 4242
            access_hash = 777
            title = "私有频道"
            username = ""

        class FakeClient:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_dialogs(self, limit=None):
                calls.append("dialogs")
                return []

            async def get_entity(self, raw):
                calls.append(raw)
                return FakeEntity()

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                return []

        monitor = self._monitor_for(["https://t.me/+abc"])
        monitor._new_client = lambda session="": FakeClient()
        first = monitor.scan(trigger="测试")
        self.assertTrue(first["ok"])
        self.assertIn("https://t.me/+abc", calls)

        # 第二轮回合：已有 access_hash，直接构造 peer，不再调用 CheckChatInvite
        calls.clear()
        monitor._new_client = lambda session="": FakeClient()
        second = monitor.scan(trigger="测试")
        self.assertTrue(second["ok"])
        self.assertNotIn("https://t.me/+abc", calls)
        self.assertNotIn("dialogs", calls)
        self.assertTrue(any(
            getattr(item, "channel_id", None) == 4242 for item in calls
        ))

    def test_resolve_failure_backs_off_instead_of_retrying_every_scan(self):
        calls = []

        class FakeClient:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_dialogs(self, limit=None):
                return []

            async def get_entity(self, raw):
                calls.append(raw)
                raise ValueError("Cannot find any entity corresponding to +abc")

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                return []

        monitor = self._monitor_for(["https://t.me/+abc"])
        monitor._new_client = lambda session="": FakeClient()
        first = monitor.scan(trigger="测试")
        self.assertTrue(any("6h 内不再重试" in err for err in first["errors"]))
        attempts = len(calls)

        second = monitor.scan(trigger="测试")
        self.assertEqual(len(calls), attempts, "冷却期内不应再请求 Telegram")
        self.assertTrue(any(
            "resolve_retry_remaining" in entry for entry in second["debug"]
        ))

    def test_flood_wait_sets_cooldown_and_skips_next_scan(self):
        calls = []

        class FloodWait(Exception):
            seconds = 1725

        class FakeClient:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_dialogs(self, limit=None):
                return []

            async def get_entity(self, raw):
                calls.append(raw)
                raise FloodWait(
                    "A wait of 1725 seconds is required "
                    "(caused by CheckChatInviteRequest)"
                )

            async def get_messages(self, entity, limit, min_id=0, reverse=False):
                return []

        monitor = self._monitor_for(["https://t.me/+abc"])
        monitor._new_client = lambda session="": FakeClient()
        first = monitor.scan(trigger="测试")
        self.assertEqual(first["errors"], [])
        self.assertTrue(first["debug"][0].get("cooldown_remaining", 0) > 0)
        attempts = len(calls)

        monitor.scan(trigger="测试")
        self.assertEqual(len(calls), attempts, "限流冷却期内不应再请求 Telegram")

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
        # 旧配置可能被 str() 两次，外层再包一对引号
        self.assertEqual(
            tg_module.normalize_channel(
                r'''"['https://t.me/+Jc37JCr1diEzNDMx']"''',
            ),
            "https://t.me/+Jc37JCr1diEzNDMx",
        )
        self.assertEqual(
            tg_module.normalize_channel(
                " [ 'https://t.me/+Jc37JCr1diEzNDMx' ] "
            ),
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

class SharedMonitorRegistryTest(unittest.TestCase):
    """热重载后旧实例仍必须被登记表看见，否则旧线程停不掉。"""

    def test_registry_survives_module_reload(self):
        class _Fake:
            pass

        monitors, _lock = tg_module._shared_monitor_registry()
        stale = _Fake()
        monitors.add(stale)

        reloaded = load_module(
            "cloudsubscribex.core.tg_monitor",
            "plugins.v2/cloudsubscribex/core/tg_monitor.py",
        )
        try:
            monitors_after, _ = reloaded._shared_monitor_registry()
            self.assertIn(stale, monitors_after)
            self.assertIn("_cloudsubscribe_tg_monitor_registry", dir(sys))
        finally:
            monitors.discard(stale)

    def test_stop_discards_from_registry(self):
        class _Fake:
            pass

        monitors, _lock = tg_module._shared_monitor_registry()
        fake = _Fake()
        monitors.add(fake)
        monitors.discard(fake)
        self.assertNotIn(fake, monitors)
