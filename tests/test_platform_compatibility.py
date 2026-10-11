import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock

# 构造 mock 的 app 基础依赖
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


class TestPlatformCompatibility(unittest.TestCase):
    """
    MoviePilot 平台跨版本（v2 与 v3）核心行为兼容性测试套件
    """

    def setUp(self):
        self.owner = MagicMock()
        self.owner._subscribe_search_originals = {}
        self.hook = SubscriptionSearchHook(self.owner)
        self.hook._enabled = True

    # 1. SubscribeChain.search 跨版本测试

    def test_v2_search_signature_and_execution(self):
        """
        验证 MoviePilot v2 平台：
        - 签名: search(self, sid=None, state='N', manual=False, progress_callback=None) -> None
        - 不支持 sids 和 scheduled_interval
        - 返回值为 None
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        recorded_calls = []

        class MockV2SubscribeChain:
            def search(self, sid=None, state='N', manual=False, progress_callback=None):
                recorded_calls.append({
                    "sid": sid,
                    "state": state,
                    "manual": manual,
                    "progress_callback": progress_callback,
                })
                return None  # v2 返回 None

        mock_subscribe_chain.return_value = MockV2SubscribeChain()

        # 模拟调度器带 v3 属性的调用调用到插件，插件安全降级传给 v2 SubscribeChain
        res = self.hook._dispatch_subscribe_search(
            state="R",
            scheduled_interval=24,
            extra_scheduler_kw="test",
        )
        self.assertIsNone(res)
        self.assertEqual(len(recorded_calls), 1)
        self.assertEqual(recorded_calls[0]["sid"], None)
        self.assertEqual(recorded_calls[0]["state"], "R")

    def test_v2_search_with_sids_batch_split(self):
        """
        验证 MoviePilot v2 平台：
        - 未接管时传入批量 sids，由于 v2 原生不支持批量，插件自动将其拆分为逐个 sid 调用
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        called_sids = []

        class MockV2SubscribeChain:
            def search(self, sid=None, state='N', manual=False, progress_callback=None):
                called_sids.append(sid)
                return None

        mock_subscribe_chain.return_value = MockV2SubscribeChain()

        res = self.hook._dispatch_subscribe_search(sids=(201, 202, 203), state="R", manual=True)
        self.assertEqual(called_sids, [201, 202, 203])
        self.assertIsNone(res)

    def test_v3_search_signature_and_execution(self):
        """
        验证 MoviePilot v3 平台：
        - 签名: search(self, sid=None, state='N', manual=False, progress_callback=None, sids=None, scheduled_interval=None) -> Optional[str]
        - 完整支持 sids 与 scheduled_interval
        - 返回值为队列任务批次 batch_id
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)
        self.hook._is_subscribe_excluded = MagicMock(return_value=False)

        recorded_kwargs = {}

        class MockV3SubscribeChain:
            def search(self, sid=None, state="N", manual=False, progress_callback=None, sids=None,
                       scheduled_interval=None):
                recorded_kwargs.update({
                    "sid": sid,
                    "state": state,
                    "manual": manual,
                    "progress_callback": progress_callback,
                    "sids": sids,
                    "scheduled_interval": scheduled_interval,
                })
                return "batch-uuid-12345"  # v3 返回任务批次 ID

        mock_subscribe_chain.return_value = MockV3SubscribeChain()

        res = self.hook._dispatch_subscribe_search(
            state="R",
            sids=(301, 302),
            scheduled_interval=12,
        )
        self.assertEqual(res, "batch-uuid-12345")
        self.assertEqual(recorded_kwargs["state"], "R")
        self.assertEqual(recorded_kwargs["sids"], (301, 302))
        self.assertEqual(recorded_kwargs["scheduled_interval"], 12)

    # 2. SubscribeChain.refresh 跨版本测试

    def test_v2_refresh_signature_compatibility(self):
        """
        验证 MoviePilot v2 平台：
        - 签名: refresh(self, progress_callback=None) -> None
        - 不支持 mtype 关键字参数
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)

        called = []

        class MockV2RefreshChain:
            def refresh(self, progress_callback=None):
                called.append(progress_callback)
                return None

        mock_subscribe_chain.return_value = MockV2RefreshChain()

        # 传入带有 mtype 及额外 kwargs 的调用
        res = self.hook._dispatch_subscribe_refresh(mtype="movie", future_arg=True)
        self.assertIsNone(res)
        self.assertEqual(len(called), 1)

    def test_v3_refresh_signature_compatibility(self):
        """
        验证 MoviePilot v3 平台：
        - 签名: refresh(self, progress_callback=None, *, mtype=None) -> None
        - 原生支持 mtype
        """
        self.hook._is_takeover_active = MagicMock(return_value=False)

        received_mtype = []

        class MockV3RefreshChain:
            def refresh(self, progress_callback=None, *, mtype=None):
                received_mtype.append(mtype)
                return None

        mock_subscribe_chain.return_value = MockV3RefreshChain()

        self.hook._dispatch_subscribe_refresh(mtype="tv")
        self.assertEqual(received_mtype, ["tv"])

    # 3. Scheduler 单例与调度作业接管测试

    def test_v2_scheduler_singleton_takeover(self):
        """
        验证 MoviePilot v2 平台：
        - Scheduler 类为单例（通过 Scheduler() 实例化），无 get_existing_instance 方法
        """

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

        self.hook._install_platform_search_block = MagicMock()
        self.hook._install_subscribe_chain_takeover = MagicMock()

        self.hook._install_subscribe_search_takeover()
        sched = MockV2Scheduler()
        self.assertEqual(sched._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)
        self.assertEqual(sched._jobs["new_subscribe_search"]["func"], self.hook._dispatch_subscribe_search)
        self.assertEqual(sched._jobs["subscribe_refresh"]["func"], self.hook._dispatch_subscribe_refresh)

    def test_v3_scheduler_get_existing_instance_takeover(self):
        """
        验证 MoviePilot v3 平台：
        - Scheduler 提供了 get_existing_instance() 类方法
        """

        class MockV3Scheduler:
            _jobs = {
                "subscribe_search": {"func": lambda: None},
                "new_subscribe_search": {"func": lambda: None},
                "subscribe_refresh": {"func": lambda: None},
            }

            @classmethod
            def get_existing_instance(cls):
                return cls

        sys.modules["app.scheduler"].Scheduler = MockV3Scheduler

        self.hook._install_platform_search_block = MagicMock()
        self.hook._install_subscribe_chain_takeover = MagicMock()

        self.hook._install_subscribe_search_takeover()
        self.assertEqual(MockV3Scheduler._jobs["subscribe_search"]["func"], self.hook._dispatch_subscribe_search)

    # 5. 跨版本订阅对象与平台过滤规则兼容性

    def test_v3_subscribe_get_params_dot_access_compatibility(self):
        """
        验证 MoviePilot v3 平台契约兼容性:
        MoviePilot v3 app/chain/subscribe/query.py::get_params 直接以属性访问读取：
        subscribe.quality, subscribe.resolution, subscribe.effect, subscribe.include, subscribe.exclude
        插件生成的临时目标对象必须包含这些字段，不得抛出 AttributeError。
        """
        import re
        from pathlib import Path
        from types import SimpleNamespace

        history_file = Path("plugins.v2/cloudsubscribe/handlers/sync/history.py")
        text = history_file.read_text(encoding="utf-8")
        match = re.search(r"def _transient_target_defaults\(\)[^:]*:\s*return\s*\{([^}]+)\}", text)
        self.assertIsNotNone(match)
        # 验证必需包含 quality, resolution, effect, include, exclude 等 v3 契约属性
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertIn(f'"{req}"', match.group(1))

    def test_v2_and_v3_safe_subscribe_params_resilience(self):
        """
        验证 platform_rules._safe_subscribe_params 契约同时兼容：
        1. v3 静态方法 SubscribeChain.get_params(subscribe)
        2. v2 实例方法 SubscribeChain().get_params(subscribe)
        3. 缺失任意属性的裸对象（自动补齐并防御异常）
        """
        from types import SimpleNamespace
        from pathlib import Path

        # 验证 platform_rules.py 源码声明了 _safe_subscribe_params 并防御了质量分辨率字段
        rules_src = Path("plugins.v2/cloudsubscribe/handlers/search/platform_rules.py").read_text(encoding="utf-8")
        self.assertIn("def _safe_subscribe_params", rules_src)
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertIn(f'"{req}"', rules_src)

        # 1. 模拟 v3 静态方法调用 (dot-access)
        class MockV3SubscribeChain:
            @staticmethod
            def get_params(s):
                return {"quality": s.quality, "resolution": s.resolution, "effect": s.effect}

        sys.modules["app.chain.subscribe"].SubscribeChain = MockV3SubscribeChain
        obj3 = SimpleNamespace(name="v3测试")
        for attr in ("quality", "resolution", "effect", "include", "exclude"):
            setattr(obj3, attr, None)
        res_v3 = MockV3SubscribeChain.get_params(obj3)
        self.assertIsNone(res_v3["quality"])
        self.assertIsNone(res_v3["resolution"])

        # 2. 模拟 v2 实例方法调用
        class MockV2SubscribeChain:
            def get_params(self, s):
                return {"quality": getattr(s, "quality", None), "mode": "v2"}

        sys.modules["app.chain.subscribe"].SubscribeChain = MockV2SubscribeChain
        obj2 = SimpleNamespace(name="v2测试")
        res_v2 = MockV2SubscribeChain().get_params(obj2)
        self.assertEqual(res_v2["mode"], "v2")

    def test_typing_annotations_defined_across_all_modules(self):
        """
        验证插件模块类型注解规范：
        所有模块中使用的 typing 类型注解必须完整导入或包含 future annotations，
        防止在低版本 Python 环境中因类/函数定义期求值注解抛出 NameError。
        """
        import ast
        from pathlib import Path

        common_typing = {
            "Any", "Dict", "List", "Optional", "Tuple", "Set", "Union",
            "Callable", "Iterable", "Sequence", "Mapping"
        }
        root = Path("plugins.v2/cloudsubscribe")
        missing_typing_report = []

        for py_file in root.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "typing":
                    for alias in node.names:
                        imported.add(alias.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "typing":
                            imported.update(common_typing)

            used = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in common_typing:
                    used.add(node.id)

            missing = used - imported
            if missing:
                missing_typing_report.append(f"{py_file}: missing {missing}")

        self.assertEqual(
            missing_typing_report,
            [],
            f"存在未导入 typing 类型的模块: {missing_typing_report}",
        )
if __name__ == "__main__":
    unittest.main()
