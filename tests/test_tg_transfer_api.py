"""手动转存接口单测：1.7.113 起沿用扫描阶段匹配到的订阅，不再强制重选。"""

import os
import sys
import types
import unittest

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()
ensure_package("app.db")
ensure_package("app.db.subscribe_oper")

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
package.OwnerDelegator = OwnerDelegator
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
core.OwnerDelegator = OwnerDelegator

api_pkg = types.ModuleType("cloudsubscribex.core.api")
api_pkg.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")]
sys.modules["cloudsubscribex.core.api"] = api_pkg
setattr(core, "api", api_pkg)


class FakeSyncApi:
    """记录手动同步载荷，不真正触发转存。"""

    calls = []

    def __init__(self, owner=None):
        self._owner = owner

    def api_vue_start_manual_sync(self, payload):
        FakeSyncApi.calls.append(dict(payload))
        return {"success": True, "message": "已提交转存"}


sync_stub = types.ModuleType("cloudsubscribex.core.api.sync")
sync_stub.SyncApi = FakeSyncApi
sys.modules["cloudsubscribex.core.api.sync"] = sync_stub
setattr(api_pkg, "sync", sync_stub)

tg_api = load_module(
    "cloudsubscribex.core.api.tg",
    "plugins.v2/cloudsubscribex/core/api/tg.py",
)


class FakeMonitor:
    def __init__(self, items):
        self.items = items
        self.updated = []

    def list_items(self, status=""):
        return list(self.items)

    def update_items(self, item_ids, **changes):
        self.updated.append(([str(item) for item in item_ids], dict(changes)))
        return len(self.updated)


def make_api(items):
    owner = types.SimpleNamespace()
    monitor = FakeMonitor(items)

    class _Api(tg_api.TgMonitorApi):
        # 直接覆盖类方法：TgMonitorApi._monitor 是真实方法，
        # 挂在实例/owner 上的同名属性会被类方法遮住。
        def _monitor(self):
            return monitor

    return _Api(owner), monitor


ITEM = {
    "id": "chan:15",
    "title": "小城良方 S01E05 4K",
    "links": ["https://pan.quark.cn/s/abcdef123456"],
    "status": "new",
    "matched_subscribe_id": 42,
}


class TgTransferUsesMatchedSubscribeTest(unittest.TestCase):
    def setUp(self):
        FakeSyncApi.calls = []

    def test_missing_subscribe_id_falls_back_to_matched(self):
        api, monitor = make_api([dict(ITEM)])
        result = api.api_vue_tg_transfer({"id": "chan:15"})
        self.assertTrue(result.get("success"))
        self.assertEqual(FakeSyncApi.calls[0]["subscribe_id"], 42)
        self.assertEqual(monitor.updated[0][1]["subscribe_id"], 42)

    def test_explicit_subscribe_id_wins(self):
        api, _monitor = make_api([dict(ITEM)])
        api.api_vue_tg_transfer({"id": "chan:15", "subscribe_id": 7})
        self.assertEqual(FakeSyncApi.calls[0]["subscribe_id"], 7)

    def test_no_match_still_requires_choice(self):
        item = dict(ITEM)
        item.pop("matched_subscribe_id")
        api, monitor = make_api([item])
        result = api.api_vue_tg_transfer({"id": "chan:15"})
        self.assertFalse(result.get("success"))
        self.assertIn("请选择要转存到的订阅", result.get("message") or "")
        self.assertEqual(FakeSyncApi.calls, [])
        self.assertEqual(monitor.updated, [])

    def test_invalid_matched_value_requires_choice(self):
        item = dict(ITEM)
        item["matched_subscribe_id"] = "abc"
        api, _monitor = make_api([item])
        result = api.api_vue_tg_transfer({"id": "chan:15", "subscribe_id": ""})
        self.assertFalse(result.get("success"))
        self.assertEqual(FakeSyncApi.calls, [])

    def test_missing_item_reports_cleanly(self):
        api, _monitor = make_api([])
        result = api.api_vue_tg_transfer({"id": "chan:99"})
        self.assertFalse(result.get("success"))
        self.assertIn("资源不存在", result.get("message") or "")


if __name__ == "__main__":
    unittest.main()
