import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock

# 构造 mock 的 app 模块依赖
mock_subscribe_chain = MagicMock()
mock_subscribe_oper = MagicMock()
mock_logger = MagicMock()

register_mock("app")
register_mock("app.log", logger=mock_logger)
register_mock("app.scheduler")
register_mock("app.chain.subscribe", SubscribeChain=mock_subscribe_chain)
register_mock("app.db.subscribe_oper", SubscribeOper=mock_subscribe_oper)

# 构造 mock 的 cloudsubscribe 包结构
register_mock("cloudsubscribe")
register_mock("cloudsubscribe.core", OwnerDelegator=OwnerDelegator)
register_mock("cloudsubscribe.core.hook")

SubscriptionSearchHook = load_module(
    "cloudsubscribe.core.hook.subscription",
    "plugins.v2/cloudsubscribe/core/hook/subscription.py",
).SubscriptionSearchHook


class TestSubscriptionHook(unittest.TestCase):
    def setUp(self):
        self.owner = MagicMock()
        self.owner._subscribe_search_originals = {}
        self.hook = SubscriptionSearchHook(self.owner)

    def test_dispatch_subscribe_search_takeover_auto(self):
        """测试在接管状态下，自动定时任务触发（传入 scheduled_interval），正常返回 True 不报错"""
        self.hook._is_takeover_active = MagicMock(return_value=True)
        self.hook._enabled = True
        self.hook._takeover_new_subscribes = False
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        # 模拟 MoviePilot v3 调度器调用: func(state="R", scheduled_interval=300)
        res = self.hook._dispatch_subscribe_search(state="R", scheduled_interval=300)
        self.assertTrue(res)

        # 模拟额外未来可能出现的调度器参数
        res2 = self.hook._dispatch_subscribe_search(state="R", scheduled_interval=300, unknown_arg="test")
        self.assertTrue(res2)

    def test_dispatch_subscribe_search_non_takeover_with_supported_search(self):
        """测试未接管状态下，若 SubscribeChain.search 支持 scheduled_interval，参数被透传"""
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        # 模拟支持 scheduled_interval 的 search 方法
        mock_chain_instance = MagicMock()

        def mock_search(sid=None, state="R", manual=False, progress_callback=None, scheduled_interval=None):
            return {"sid": sid, "state": state, "scheduled_interval": scheduled_interval}

        mock_chain_instance.search = mock_search
        mock_subscribe_chain.return_value = mock_chain_instance

        res = self.hook._dispatch_subscribe_search(state="R", scheduled_interval=300)
        self.assertEqual(res, {"sid": None, "state": "R", "scheduled_interval": 300})

    def test_dispatch_subscribe_search_non_takeover_with_legacy_search(self):
        """测试未接管状态下，若 SubscribeChain.search 为旧版签名（不支持 scheduled_interval），自动过滤不受支持参数"""
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        # 模拟老版本 search 方法签名（不支持 scheduled_interval，也不支持 **kwargs）
        mock_chain_instance = MagicMock()

        def mock_search(sid=None, state="R", manual=False, progress_callback=None):
            return {"sid": sid, "state": state}

        mock_chain_instance.search = mock_search
        mock_subscribe_chain.return_value = mock_chain_instance

        # 传入 scheduled_interval 和其他未知 kwargs，不应抛出 TypeError
        res = self.hook._dispatch_subscribe_search(state="R", scheduled_interval=300, extra_param=123)
        self.assertEqual(res, {"sid": None, "state": state_value} if False else {"sid": None, "state": "R"})

    def test_dispatch_subscribe_refresh_with_kwargs(self):
        """测试 _dispatch_subscribe_refresh 接收未知 kwargs 不报错"""
        self.hook._is_takeover_active = MagicMock(return_value=True)
        res = self.hook._dispatch_subscribe_refresh(extra_job_meta="meta")
        self.assertTrue(res)

    def test_dispatch_all_subscribe_search_forwarding(self):
        """测试 _dispatch_all_subscribe_search 将 scheduled_interval 和 kwargs 透传给原生 search"""
        calls = []
        mock_chain_instance = MagicMock()

        def mock_search(**kwargs):
            calls.append(kwargs)
            return True

        mock_chain_instance.search = mock_search
        mock_subscribe_chain.return_value = mock_chain_instance

        # 模拟有 2 个订阅，其中 1 个被排除需原生处理，1 个被接管
        sub1 = MagicMock()
        sub1.id = 1
        sub2 = MagicMock()
        sub2.id = 2

        mock_subscribe_oper.return_value.list.return_value = [sub1, sub2]
        self.hook.queue_subscribe_search = MagicMock()
        # id=1 接管，id=2 原生
        self.hook._is_subscribe_excluded = lambda sid: sid == 2

        res = self.hook._dispatch_all_subscribe_search(
            state="R",
            manual=True,
            progress_callback=None,
            scheduled_interval=300,
            future_meta="test",
        )
        self.assertTrue(res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["sid"], 2)
        self.assertEqual(calls[0]["scheduled_interval"], 300)
        self.assertEqual(calls[0]["future_meta"], "test")

    def test_dispatch_subscribe_search_with_sids(self):
        """测试 _dispatch_subscribe_search 支持 sids 参数"""
        self.hook._is_takeover_active = MagicMock(return_value=True)
        self.hook.queue_subscribe_search = MagicMock()
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        res = self.hook._dispatch_subscribe_search(sids=(10, 20), manual=True)
        self.assertTrue(res)
        self.assertEqual(self.hook.queue_subscribe_search.call_count, 2)

    def test_dispatch_subscribe_refresh_with_mtype(self):
        """测试 _dispatch_subscribe_refresh 支持 mtype 并透传"""
        self.hook._is_takeover_active = MagicMock(return_value=False)
        mock_chain_instance = MagicMock()
        refreshed_args = {}

        def mock_refresh(progress_callback=None, *, mtype=None):
            refreshed_args["mtype"] = mtype
            return True

        mock_chain_instance.refresh = mock_refresh
        mock_subscribe_chain.return_value = mock_chain_instance

    def test_v2_environment_search_with_sids(self):
        """测试 v2 环境（search 不支持 sids）下，未接管传入 sids 会被安全降级为逐个 sid 调用"""
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        called_sids = []
        mock_chain_instance = MagicMock()

        def mock_v2_search(sid=None, state='N', manual=False, progress_callback=None):
            called_sids.append(sid)
            return None  # v2 返回 None

        mock_chain_instance.search = mock_v2_search
        mock_subscribe_chain.return_value = mock_chain_instance

        res = self.hook._dispatch_subscribe_search(sids=(101, 102), state="R", manual=True)
        self.assertEqual(called_sids, [101, 102])
        self.assertIsNone(res)

    def test_v2_scheduler_takeover_and_restore(self):
        """测试在 v2 环境下（Scheduler 无 get_existing_instance），通过 Scheduler() 正常安装与恢复"""

        class MockV2Scheduler:
            _instance = None

            def __new__(cls):
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._jobs = {
                        "subscribe_search": {"func": lambda: None},
                        "new_subscribe_search": {"func": lambda: None},
                        "subscribe_refresh": {"func": lambda: None},
                    }
                return cls._instance

        sys.modules["app.scheduler"].Scheduler = MockV2Scheduler

        self.hook._enabled = True
        self.hook._install_platform_search_block = MagicMock()
        self.hook._install_subscribe_chain_takeover = MagicMock()
        self.hook._restore_platform_search_block = MagicMock()
        self.hook._restore_subscribe_chain_takeover = MagicMock()

        # 安装接管
        self.hook._install_subscribe_search_takeover()
        v2_inst = MockV2Scheduler()
        self.assertEqual(v2_inst._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)
        self.assertEqual(v2_inst._jobs["subscribe_refresh"]["func"], self.hook._dispatch_subscribe_refresh)

        # 恢复接管
        self.hook._restore_subscribe_search_takeover()
        self.assertNotEqual(v2_inst._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)


if __name__ == "__main__":
    unittest.main()
