"""漏斗阶段 2：TG 进漏斗（v1.29.8）。

覆盖任务清单：
1. 路由判定 ``SyncApi._funnel_route_for_submit`` 真值表（阶段 1 总闸 +
   阶段 2 TG 分支）与 ``api_vue_start_manual_sync`` 的接线；
2. 配置：``funnel_phase2_enabled`` 默认关（阶段 1 的 6 键 + 本键 = 7 键）；
3. TG 行入队：source 原样为 ``Candidate.via()``（tg:{channel}/{message_id}
   或 push），notification_kind 走唯一派生点
   ``_derive_notification_kind``（组件不可用时回退等价内联公式）；
4. 同目录 TG 项不再当场生成 STRM（只对 TG 路由项；手动同目录仍按阶段 1
   当场生成；阶段 2 开关关时 TG 仍走旧直出路）；
5. 集成（真实驱动，DummyMpWaitService + 真实 ``_process_due_items``，
   不用“完成数+1”式假驱动掩盖逻辑）：
   - 同一集 TG 与订阅先后入队只留一份（注册表去重 + 落盘前实查拦截）；
   - TG 单集异步全链路（入队→改名→实查→MP 整理→终态提交）；
   - Emby 滞后时本轮集合兜底（第二份同键行被本轮集合拦住，不依赖 Emby）。

harness：复用 test_funnel_stage1_1_29_7 的 _DbMixin / _Fake* /
_DummyMpWaitService 等；路由测试加载真实 core/api/sync.py 时对碰到的
sys.modules 做快照并在 tearDownClass 恢复（test_service_split 模式）；
service 实例上的桩用 object.__setattr__（OwnerDelegator.__setattr__
会把普通赋值转发给 owner，打不上实例桩）。
"""

import inspect
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import register_module  # noqa: E402

from test_funnel_stage1_1_29_7 import (  # noqa: E402
    DummyMpWaitService,
    SyncHandler,
    _DbMixin,
    _FakeDedup,
    _FakeDrive,
    _TV_CLOUD_DIR,
    _TV_FILE,
    _TV_SIZE,
    _TV_STAGING,
    _ctx,
    _load_once,
    _payload,
    _stub,
    _tv_item,
    _tv_media,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TG_VIA = "tg:@QukanMovie/12281"


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
class FunnelPhase2ConfigTests(unittest.TestCase):
    """_init_funnel_flags：阶段 2 键 funnel_phase2_enabled 默认关。"""

    def _flags(self, values):
        fake_self = SimpleNamespace()
        SyncHandler._init_funnel_flags(
            fake_self, lambda key, default: values.get(key, default)
        )
        return fake_self

    def test_phase2_default_off(self):
        """全缺省：阶段 1 与阶段 2 开关都关（kill-switch 默认关）。"""
        flags = self._flags({})
        self.assertFalse(flags._funnel_phase1_enabled)
        self.assertFalse(flags._funnel_phase2_enabled)

    def test_phase2_value_on(self):
        """显式开：阶段 2 标志照原值读出。"""
        flags = self._flags({"funnel_phase2_enabled": True})
        self.assertTrue(flags._funnel_phase2_enabled)

    def test_config_default_off(self):
        """core/config.py 默认值里有本键且为 False（前端开关有后端默认）。"""
        path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "config.py"
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"funnel_phase2_enabled": False', text)


# ----------------------------------------------------------------------
# 路由判定（真实 SyncApi）
# ----------------------------------------------------------------------
_MISSING = object()
_SYNC_TOUCHED = (
    "cloudsubscribex.core.api.sync",
    "cloudsubscribex.core.cloud",
    "cloudsubscribex.core.search",
    "cloudsubscribex.search.matching",
)


class FunnelPhase2RoutingTests(unittest.TestCase):
    """_funnel_route_for_submit 真值表 + 入口接线。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._saved_modules = {}
        cls._saved_attrs = {}
        for name in _SYNC_TOUCHED:
            cls._saved_modules[name] = sys.modules.get(name, _MISSING)
            parent_name, _, child = name.rpartition(".")
            if parent_name and parent_name in sys.modules:
                parent = sys.modules[parent_name]
                cls._saved_attrs[(parent_name, child)] = getattr(
                    parent, child, _MISSING
                )
        # sync.py 导入闭包里本文件不关心的能力接口：只给桩，不加载真实实现。
        register_module(
            "cloudsubscribex.core.cloud", CloudDriveCapability=MagicMock()
        )
        register_module(
            "cloudsubscribex.core.search", SearchCapability=MagicMock()
        )
        register_module(
            "cloudsubscribex.search.matching",
            positive_ints=lambda value, *a, **k: value,
        )
        cls.sync_module = _load_once(
            "cloudsubscribex.core.api.sync",
            "plugins.v2/cloudsubscribex/core/api/sync.py",
        )
        cls.SyncApi = cls.sync_module.SyncApi

    @classmethod
    def tearDownClass(cls):
        for name, saved in cls._saved_modules.items():
            if saved is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
        for (parent_name, child), saved in cls._saved_attrs.items():
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if saved is _MISSING:
                try:
                    delattr(parent, child)
                except AttributeError:
                    pass
            else:
                setattr(parent, child, saved)
        super().tearDownClass()

    def _route(self, **kwargs):
        params = {
            "phase1_enabled": True,
            "phase2_enabled": True,
            "discovered_via": "",
            "wait": False,
        }
        params.update(kwargs)
        return self.SyncApi._funnel_route_for_submit(**params)

    def test_tg_item_phase2_on_enqueues(self):
        """TG 项 + 阶段 2 开 → 入队（TG 提交恒为 wait=True 也不挡）。"""
        self.assertTrue(
            self._route(discovered_via=TG_VIA, wait=True)
        )

    def test_push_item_phase2_on_enqueues(self):
        """push 口径的 TG 项同样入队。"""
        self.assertTrue(self._route(discovered_via="push", wait=True))

    def test_tg_item_phase2_off_old_pipeline(self):
        """TG 项 + 阶段 2 关 → 旧流水线（kill-switch 关回直出路）。"""
        self.assertFalse(
            self._route(phase2_enabled=False, discovered_via=TG_VIA, wait=True)
        )

    def test_tg_item_phase1_off_old_pipeline(self):
        """阶段 1 是总闸：阶段 1 关时即使阶段 2 开，TG 也不入队。"""
        self.assertFalse(
            self._route(phase1_enabled=False, discovered_via=TG_VIA, wait=True)
        )

    def test_manual_item_stage1_preserved(self):
        """界面手动提交（无 via、非 wait）仍按阶段 1 入队。"""
        self.assertTrue(self._route(phase2_enabled=False))

    def test_workflow_item_stays_old_pipeline(self):
        """工作流/代理提交（wait=True、无 via）仍走旧流水线。"""
        self.assertFalse(self._route(wait=True))

    def test_all_off_no_route(self):
        """两开关都关：任何提交都不入队。"""
        self.assertFalse(
            self._route(phase1_enabled=False, phase2_enabled=False)
        )

    def test_entry_calls_helper(self):
        """_do_api_vue_start_manual_sync 经 helper 判定，不内联另一套规则。"""
        source = inspect.getsource(self.SyncApi._do_api_vue_start_manual_sync)
        self.assertIn("_funnel_route_for_submit", source)


# ----------------------------------------------------------------------
# TG 行入队（finalize 边界）
# ----------------------------------------------------------------------
class FunnelTgEnqueueTests(_DbMixin):
    """enqueue_from_pending：TG 行的 source 与 notification_kind。"""

    def _tg_item(self, **extra):
        item = _tv_item(
            discovered_via=TG_VIA, funnel_route=True, result_key="f-tg-09"
        )
        item.update(extra)
        return item

    def test_tg_item_enqueues_with_via_source(self):
        """TG 行 source 原样为 via；无派生组件时 kind 回退为 transfer。"""
        row, created = self.service.enqueue_from_pending(
            self._tg_item(), "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], TG_VIA)
        self.assertEqual(row["notification_kind"], "transfer")

    def test_push_item_enqueues_with_push_source(self):
        """push 口径的 TG 行 source 为 push。"""
        row, created = self.service.enqueue_from_pending(
            self._tg_item(discovered_via="push"), "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], "push")

    def test_notification_kind_uses_single_derivation_point(self):
        """transfer_details 可用时 kind 走 _derive_notification_kind。"""
        calls = []

        class _Details:
            def _derive_notification_kind(
                self, *, is_upgrade=False, replaced_from=None,
                index_record=None, cross=False,
            ):
                calls.append({
                    "is_upgrade": is_upgrade,
                    "replaced_from": replaced_from,
                    "cross": cross,
                })
                return "upgrade"

        self.owner.transfer_details = _Details()
        item = self._tg_item(
            upgrade=True,
            upgrade_old_file_id="f-old-09",
            upgrade_old_cloud_dir=_TV_CLOUD_DIR,
            upgrade_old_file_name=_TV_FILE,
            upgrade_old_size=1000,
        )
        row, created = self.service.enqueue_from_pending(
            item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["is_upgrade"])
        self.assertEqual(calls[0]["replaced_from"][0]["file_id"], "f-old-09")
        self.assertFalse(calls[0]["cross"])
        self.assertEqual(row["notification_kind"], "upgrade")

    def test_notification_kind_fallback_cross(self):
        """无派生组件时回退公式：跨盘任务为 cross_transfer。"""
        row, created = self.service.enqueue_from_pending(
            _tv_item(task_type="cross_cloud"), "pk-tv-09", _tv_media()
        )
        self.assertTrue(created)
        self.assertEqual(row["notification_kind"], "cross_transfer")

    def test_second_enqueue_same_episode_returns_existing(self):
        """同一集第二行入队：注册表去重，返回已有的 TG 行、不新建。"""
        row1, created1 = self.service.enqueue_from_pending(
            self._tg_item(), "pk-tg-09", _tv_media()
        )
        self.assertTrue(created1)
        row2, created2 = self.service.enqueue_from_pending(
            _tv_item(result_key="f-sub-09", funnel_route=True),
            "pk-sub-09", _tv_media(),
        )
        self.assertFalse(created2)
        self.assertEqual(row2["id"], row1["id"])


# ----------------------------------------------------------------------
# 同目录分支（真实 StrmOpsMixin）
# ----------------------------------------------------------------------
class FunnelTgSameDirTests(unittest.TestCase):
    """_generate_or_queue_strm_batch：TG 路由项不再当场生成 STRM。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _load_once(
            "cloudsubscribex.core.bypass_guard",
            "plugins.v2/cloudsubscribex/core/bypass_guard.py",
        )
        _load_once(
            "cloudsubscribex.utils.legacy_strm",
            "plugins.v2/cloudsubscribex/utils/legacy_strm.py",
        )
        cls.strm_ops = _load_once(
            "cloudsubscribex.handlers.sync.strm_ops",
            "plugins.v2/cloudsubscribex/handlers/sync/strm_ops.py",
        )
        cls.StrmOpsMixin = cls.strm_ops.StrmOpsMixin

    def _service(self):
        service = self.StrmOpsMixin(SimpleNamespace())
        # 注意：StrmOpsMixin 是 OwnerDelegator，普通赋值会转发给 owner，
        # 方法桩必须用 object.__setattr__（_stub）打在实例上，否则类方法
        # 仍优先命中、桩不生效。
        _stub(service, "_organize_after_transfer", True)
        _stub(
            service, "_cloud_query",
            SimpleNamespace(
                get_cached_file=lambda cloud_dir, file_name: SimpleNamespace(
                    name=file_name
                ),
            ),
        )
        self.generate_calls = []
        self.queue_calls = []

        def _generate(
            cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True,
        ):
            self.generate_calls.append((cloud_dir, file_name))
            return Path("/tmp/x.strm")

        def _queue(
            items, mediainfo, subscribe_id=None, season=None, sub_key="",
            transient_target=False, target_subscribe=None,
        ):
            self.queue_calls.append(list(items))
            return {
                str(item["result_key"]): "pk-" + str(item["result_key"])
                for item in items
            }

        _stub(service, "_generate_strm", _generate)
        _stub(service, "_scrape_metadata_batch", lambda *args, **kwargs: None)
        _stub(service, "_queue_file_finalize_batch", _queue)
        return service

    def test_tg_routed_same_dir_item_queued_not_generated(self):
        """TG 路由项（同目录）：入队，不当场生成 STRM。"""
        service = self._service()
        item = _tv_item(discovered_via=TG_VIA, funnel_route=True)
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.queue_calls[0][0]["result_key"], "f-tv-09")
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))

    def test_tg_unrouted_same_dir_item_generates_inline(self):
        """TG 项但 funnel_route 为假（阶段 2 关）：仍走旧直出路。"""
        service = self._service()
        item = _tv_item(discovered_via=TG_VIA, funnel_route=False)
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(
            self.generate_calls, [(_TV_CLOUD_DIR, _TV_FILE)]
        )
        # 末尾无条件调一次 _queue_file_finalize_batch（空批），但没有项入队。
        self.assertEqual(
            [item for batch in self.queue_calls for item in batch], []
        )
        self.assertEqual(results["f-tv-09"][0], Path("/tmp/x.strm"))

    def test_manual_same_dir_item_queued_not_generated(self):
        """v1.30.0 阶段 4（d）：手动同目录项（无 via）也入队，不再当场生成。

        阶段 2/3 有意留着手动直出路等阶段 4 统一；本版以 funnel_route
        为唯一判定，手动同目录与 TG/订阅/洗版一致。"""
        service = self._service()
        item = _tv_item(funnel_route=True)
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.queue_calls[0][0]["result_key"], "f-tv-09")
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))

    def test_tg_routed_staging_differs_queued(self):
        """TG 路由项（暂存目录不同）：同样入队（原有分支行为不变）。"""
        service = self._service()
        item = _tv_item(
            discovered_via=TG_VIA, funnel_route=True,
            staging_dir="/Alist/影视/moviepilot/转存/",
        )
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))


# ----------------------------------------------------------------------
# 集成：真实驱动
# ----------------------------------------------------------------------
class FunnelTgIntegrationTests(_DbMixin):
    """TG 全链路与去重（真实 _process_due_items + 真实 _process_row）。"""

    def _real_mp(self, pending):
        """真实 DummyMpWaitService：只桩 I/O 边界（目录快照）与终态分发；
        _process_due_items 本身是真实的（含漏斗跳过语义）。"""
        mp = DummyMpWaitService()
        mp._owner._funnel_phase1_enabled = True
        mp._owner._stop_requested = lambda: False
        ctx = _ctx(dict(pending))
        dispatch_calls = []

        def _dispatch(item, key, c):
            dispatch_calls.append(key)
            c.completed += 1
            c.pending.pop(key, None)
            c.committed_keys.add(key)

        _stub(
            mp, "_prepare_postprocess_context",
            lambda force=True, pending_keys=None: ctx,
        )
        _stub(mp, "_dispatch_due_item", _dispatch)
        _stub(mp, "_flush_batch_postprocess", lambda c: None)
        _stub(mp, "_reconcile_pending_state", lambda c: None)
        _stub(
            mp, "_cloud_directory_snapshot",
            lambda path, cache=None: (
                True, {_TV_FILE: {"file_id": "f-tg-09"}}
            ),
        )
        self.owner.postprocess = mp
        return ctx, dispatch_calls

    def _tg_pending(self, pending_key="pk-tg-09", **extra):
        item = _tv_item(
            discovered_via=TG_VIA, funnel_route=True, result_key="f-tg-09"
        )
        item.update(extra)
        self.owner._pending[pending_key] = dict(item)
        return self.owner._pending[pending_key]

    def test_tg_then_subscribe_same_episode_leaves_one_copy(self):
        """同一集 TG 与订阅先后入队：TG 行终态 1 次整理；订阅行入队后被
        落盘前实查拦截，不触发第二次 MP 整理、不写第二份终态。"""
        tg_item = self._tg_pending()
        tg_row, created = self.service.enqueue_from_pending(
            tg_item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self.assertEqual(tg_row["source"], TG_VIA)
        ctx, dispatch_calls = self._real_mp(self.owner._pending)
        stats = self.service.run_round()
        self.assertEqual(stats, {
            "processed": 1, "done": 1, "failed": 0, "held": 0, "skipped": 0,
        })
        self.assertEqual(
            self.repo.get_by_id(tg_row["id"])["status"], "done"
        )
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(
            self.owner.mp_organize.calls[0][1], _TV_FILE
        )
        self.assertEqual(dispatch_calls, ["pk-tg-09"])
        # 订阅路第二份：TG 行已终态，注册表放行新建行；但落盘前实查看到
        # TG 那份已在库，整行拦截。
        # 1.33.0：入队时间窗会先把这种「同集窗口内已 done」的再来一份挡在
        # 入队处；本用例要验的是第二道防线（落盘前实查），故这里把窗口关掉。
        # 窗口本身由 test_funnel_recent_done_1_33_0.py 与文末用例覆盖。
        self.owner._funnel_recent_done_window_hours = 0
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-tg-09",
            "file_name": _TV_FILE,
            "file_size": _TV_SIZE,
        }]})
        sub_item = _tv_item(result_key="f-sub-09", funnel_route=True)
        self.owner._pending["pk-sub-09"] = dict(sub_item)
        sub_row, sub_created = self.service.enqueue_from_pending(
            sub_item, "pk-sub-09", _tv_media()
        )
        self.assertTrue(sub_created)
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(sub_row["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertIn("已在库", fetched["skip_reason"])
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        # 1.33.0：窗口打开时，同集再来一份直接在入队处挡掉（不进队列、
        # 不下载、不上传），返回的是那条已完成的行。
        self.owner._funnel_recent_done_window_hours = 12
        third = _tv_item(result_key="f-sub-09-c", funnel_route=True)
        self.owner._pending["pk-sub-09-c"] = dict(third)
        third_row, third_created = self.service.enqueue_from_pending(
            third, "pk-sub-09-c", _tv_media(),
        )
        self.assertFalse(third_created)
        self.assertEqual(third_row["status"], "done")
        # 注：本用例直接调 service.enqueue_from_pending，不经过
        # SyncHandler 的重复入队收尾（弹 pending），故不断言 pending 被清。
        self.assertEqual(dispatch_calls, ["pk-tg-09"])
        self.assertNotIn("pk-sub-09", self.owner._pending)

    def test_tg_single_episode_async_full_chain(self):
        """TG 单集异步全链路：入队→MP 整理恰好一次→终态 done；
        pending 记录带 MP 整理身份（走 MP 整理轮询分支，非直出写盘）。"""
        tg_item = self._tg_pending()
        tg_row, created = self.service.enqueue_from_pending(
            tg_item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        ctx, dispatch_calls = self._real_mp(self.owner._pending)
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(tg_row["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertEqual(fetched["source"], TG_VIA)
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(
            self.owner.mp_organize.calls[0],
            (_TV_CLOUD_DIR, _TV_FILE, 1),
        )
        self.assertEqual(dispatch_calls, ["pk-tg-09"])
        self.assertEqual(
            self.owner._pending["pk-tg-09"].get("mp_organize"), {}
        )

    def test_emby_lag_in_pass_set_blocks_second_episode(self):
        """Emby 滞后兜底：本轮内两行同键，第二行被本轮集合拦住，
        _process_row 只调一次——不依赖 Emby 的集数识别。"""
        repo = self.service._repository()
        row_a = _payload(id=1)
        row_b = _payload(id=2)
        calls = []
        object.__setattr__(
            repo, "list_due", lambda limit=50: [dict(row_a), dict(row_b)]
        )
        self.addCleanup(lambda: object.__delattr__(repo, "list_due"))
        orig_process = self.service._process_row

        def _record(row):
            calls.append(row)
            return "done"

        object.__setattr__(self.service, "_process_row", _record)
        self.addCleanup(
            lambda: object.__setattr__(self.service, "_process_row", orig_process)
        )
        stats = self.service.run_round()
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], 1)


if __name__ == "__main__":
    unittest.main()


# ----------------------------------------------------------------------
# 漏斗行终态回写频道监控（v1.29.8 复核修补）
# ----------------------------------------------------------------------
_TG_SHARE_URL = "https://115.com/s/tgtest09"
_TG_MONITOR_MODULE = "cloudsubscribex.core.tg_monitor"


class _RecordingMonitor:
    """apply_finalize_outcome 记录桩。"""

    def __init__(self):
        self.calls = []

    def apply_finalize_outcome(self, share_url, subscribe_id, *, success,
                                reason="", file_name=""):
        self.calls.append({
            "share_url": share_url,
            "subscribe_id": subscribe_id,
            "success": success,
            "reason": reason,
            "file_name": file_name,
        })
        return 1


class FunnelTgMonitorWritebackTests(_DbMixin):
    """拦截/挂起路的监控回写：这两条路不经过 _write_terminal_state。

    不修之前：TG 项入队时标 transferred，拦截后仍是 transferred（缺终态
    戳），挂起后更是 transferred、而文件根本没入库。
    """

    def setUp(self):
        super().setUp()
        self.monitor = _RecordingMonitor()
        self._saved_monitor_module = sys.modules.get(
            _TG_MONITOR_MODULE, _MISSING
        )
        register_module(
            _TG_MONITOR_MODULE,
            get_channel_monitor=lambda plugin: self.monitor,
        )

    def tearDown(self):
        if self._saved_monitor_module is _MISSING:
            sys.modules.pop(_TG_MONITOR_MODULE, None)
        else:
            sys.modules[_TG_MONITOR_MODULE] = self._saved_monitor_module
        super().tearDown()

    def _drive_setup(self, pending, snapshot):
        """真实 DummyMpWaitService：目录快照按给定 index，其余同集成测试。"""
        mp = DummyMpWaitService()
        mp._owner._funnel_phase1_enabled = True
        mp._owner._stop_requested = lambda: False
        ctx = _ctx(dict(pending))
        _stub(
            mp, "_prepare_postprocess_context",
            lambda force=True, pending_keys=None: ctx,
        )
        _stub(mp, "_flush_batch_postprocess", lambda c: None)
        _stub(mp, "_reconcile_pending_state", lambda c: None)
        _stub(
            mp, "_cloud_directory_snapshot",
            lambda path, cache=None: (True, dict(snapshot)),
        )
        self.owner.postprocess = mp
        return ctx

    def _tg_pending(self, pending_key="pk-tg-09", **extra):
        item = _tv_item(
            discovered_via=TG_VIA, funnel_route=True, result_key="f-tg-09",
            share_url=_TG_SHARE_URL,
        )
        item.update(extra)
        self.owner._pending[pending_key] = dict(item)
        return self.owner._pending[pending_key]

    def test_intercept_syncs_monitor_success(self):
        """落盘前实查拦截：行 done + 监控回写 success（集已在库）。"""
        tg_item = self._tg_pending()
        tg_row, created = self.service.enqueue_from_pending(
            tg_item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self._drive_setup(
            self.owner._pending, {_TV_FILE: {"file_id": "f-tg-09"}}
        )
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-other-09",
            "file_name": "其他文件.mkv",
            "file_size": 999,
        }]})
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(tg_row["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertIn("已在库", fetched["skip_reason"])
        self.assertEqual(len(self.monitor.calls), 1)
        call = self.monitor.calls[0]
        self.assertTrue(call["success"])
        self.assertEqual(call["share_url"], _TG_SHARE_URL)
        self.assertEqual(call["subscribe_id"], 444)

    def test_held_row_syncs_monitor_failure(self):
        """改名失败 5 次挂起：行 held + 监控回写 failure（文件没入库）。"""
        tg_item = self._tg_pending()
        tg_row, created = self.service.enqueue_from_pending(
            tg_item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        for _ in range(4):
            self.repo.mark_failed(tg_row["id"], int(time.time()), "x")
        self.owner._cloud_drive = _FakeDrive(ok=False)
        self._drive_setup(
            self.owner._pending, {_TV_STAGING: {"file_id": "f-tg-09"}}
        )
        stats = self.service.run_round()
        self.assertEqual(stats["held"], 1)
        fetched = self.repo.get_by_id(tg_row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertEqual(len(self.monitor.calls), 1)
        call = self.monitor.calls[0]
        self.assertFalse(call["success"])
        self.assertEqual(call["share_url"], _TG_SHARE_URL)
        self.assertIn("改名失败", call["reason"])

    def test_non_held_failure_does_not_sync_monitor(self):
        """未耗尽次数的失败（仍在重试）：行 failed，不回写监控。"""
        tg_item = self._tg_pending()
        tg_row, created = self.service.enqueue_from_pending(
            tg_item, "pk-tg-09", _tv_media()
        )
        self.assertTrue(created)
        self.owner._cloud_drive = _FakeDrive(ok=False)
        self._drive_setup(
            self.owner._pending, {_TV_STAGING: {"file_id": "f-tg-09"}}
        )
        stats = self.service.run_round()
        self.assertEqual(stats["failed"], 1)
        fetched = self.repo.get_by_id(tg_row["id"])
        self.assertEqual(fetched["status"], "failed")
        self.assertEqual(self.monitor.calls, [])

    def test_intercept_without_share_url_skips_monitor(self):
        """拦截项无 share_url（手动提交）：行 done，不碰监控。"""
        item = _tv_item(funnel_route=True, result_key="f-tv-09")
        self.owner._pending["pk-tv-09"] = dict(item)
        row, created = self.service.enqueue_from_pending(
            item, "pk-tv-09", _tv_media()
        )
        self.assertTrue(created)
        self._drive_setup(
            self.owner._pending, {_TV_FILE: {"file_id": "f-tv-09"}}
        )
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-other-09",
            "file_name": "其他文件.mkv",
            "file_size": 999,
        }]})
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        self.assertEqual(self.monitor.calls, [])
