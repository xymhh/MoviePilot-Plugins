"""手动转存接口单测：1.7.113 起沿用扫描阶段匹配到的订阅，不再强制重选。

v1.28.0：/tg/transfer 不再直接调手动同步、也不再提交即标「已转存」，
改为调监控的 manual_transfer_items 把条目放回转存队列（真实状态机见
test_tg_monitor_manual_transfer_1_28_0.py），这里只校验接口层的参数与文案。
"""

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


tg_api = load_module(
    "cloudsubscribex.core.api.tg",
    "plugins.v2/cloudsubscribex/core/api/tg.py",
)


class FakeMonitor:
    """接口层只关心：传给 manual_transfer_items 的参数与返回的文案。"""

    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {
            "submitted": 1,
            "results": [{"id": "chan:15", "ok": True, "reason": "", "message": "已加入转存队列"}],
        }

    def manual_transfer_items(self, item_ids, subscribe_id=0):
        self.calls.append((list(item_ids), subscribe_id))
        return self.outcome


def make_api(outcome=None):
    owner = types.SimpleNamespace()
    monitor = FakeMonitor(outcome)

    class _Api(tg_api.TgMonitorApi):
        # 直接覆盖类方法：TgMonitorApi._monitor 是真实方法，
        # 挂在实例/owner 上的同名属性会被类方法遮住。
        def _monitor(self):
            return monitor

    return _Api(owner), monitor


def rejected(reason, message, item_id="chan:15"):
    return {
        "submitted": 0,
        "results": [{"id": item_id, "ok": False, "reason": reason, "message": message}],
    }


class TgTransferApiTest(unittest.TestCase):
    def test_single_id_goes_to_monitor_queue(self):
        api, monitor = make_api()
        result = api.api_vue_tg_transfer({"id": "chan:15"})
        self.assertTrue(result.get("success"))
        self.assertEqual(monitor.calls, [(["chan:15"], 0)])
        self.assertIn("已加入转存队列 1 条", result.get("message") or "")

    def test_explicit_subscribe_id_is_passed(self):
        api, monitor = make_api()
        api.api_vue_tg_transfer({"id": "chan:15", "subscribe_id": 7})
        self.assertEqual(monitor.calls, [(["chan:15"], 7)])

    def test_multiple_ids(self):
        api, monitor = make_api()
        api.api_vue_tg_transfer({"ids": ["a", "b"]})
        self.assertEqual(monitor.calls, [(["a", "b"], 0)])

    def test_no_subscribe_asks_for_choice(self):
        api, _monitor = make_api(rejected("no_subscribe", "没有匹配到订阅，请选择要转存到的订阅"))
        result = api.api_vue_tg_transfer({"id": "chan:15"})
        self.assertFalse(result.get("success"))
        self.assertEqual(result.get("message"), "请选择要转存到的订阅")

    def test_invalid_subscribe_value_is_ignored(self):
        api, monitor = make_api()
        api.api_vue_tg_transfer({"id": "chan:15", "subscribe_id": "abc"})
        self.assertEqual(monitor.calls, [(["chan:15"], 0)])

    def test_missing_item_reports_cleanly(self):
        api, _monitor = make_api(rejected("not_found", "记录不存在，可能已被清理", "chan:99"))
        result = api.api_vue_tg_transfer({"id": "chan:99"})
        self.assertFalse(result.get("success"))
        self.assertIn("记录不存在", result.get("message") or "")

    def test_missing_id(self):
        api, monitor = make_api()
        result = api.api_vue_tg_transfer({})
        self.assertFalse(result.get("success"))
        self.assertEqual(monitor.calls, [])

    def test_partial_rejection_is_summarised(self):
        api, _monitor = make_api({
            "submitted": 1,
            "results": [
                {"id": "a", "ok": True, "reason": "", "message": "已加入转存队列"},
                {"id": "b", "ok": False, "reason": "transferred", "message": "已转存，无需重复转存"},
            ],
        })
        result = api.api_vue_tg_transfer({"ids": ["a", "b"]})
        self.assertTrue(result.get("success"))
        self.assertIn("未提交 1 条已转存", result.get("message") or "")


if __name__ == "__main__":
    unittest.main()
