"""漏斗阶段 3：订阅与洗版进漏斗（v1.29.9）。

覆盖任务清单：
1. 配置：``funnel_phase3_enabled`` 默认关（阶段 1 的 6 键 + 阶段 2 的
   1 键 + 本键 = 8 键）；
2. 路由：订阅驱动同步判定 ``SyncHandler._funnel_route_for_subscribe_sync``
   真值表（阶段 1 总闸 + 阶段 3 开关）与 ``sync_subscribes`` / ``_do_sync``
   的接线（生产者标记 ``_funnel_producer``）；复核修补：路由覆盖整轮
   cron 与 MP 接管的订阅卡片搜索（单订阅/subscribe_ids/state），判定抽
   成纯函数 ``_should_route_subscribe_sync_to_funnel``（TG/手动/洗版入口
   各有自己的路由，不由阶段 3 判定）；
3. 订阅行入队：source 为 ``subscribe:<id>``（设计 §5.1），notification_kind
   走唯一派生点；洗版行 kind 为 upgrade 且 ``replaced_from`` 随行；
4. 同目录 STRM 分支：订阅轮次项与洗版项入队后不再当场生成 STRM，
   手动同目录项仍当场生成（阶段 4 再统）；
5. 集成（真实驱动，DummyMpWaitService + 真实 ``_process_due_items``）：
   - 订阅轮次缺集入队→改名→实查→MP 整理→终态 done；
   - 935 秒长上传窗口：第二份同集在在途登记层被拦（只留一行）；
   - 改名后的第一份仍能拦住第二份（双名实查）；
6. 终态回写：``_finish_pending_subscription`` 只对漏斗归属洗版项回写
   episode_priority（``_write_terminal_upgrade_priority``），旧流水线
   项与非洗版项不重复写；
7. 轮次结算守卫（真实 TelevisionSyncProcessor / MergeOwner 桩）：
   订阅仍有漏斗在途行时快路径与轮次末都不结算；无在途行时照常结算；
   洗版待终态项不计入本轮进度/评分（终态回写时再计）；
8. 整理队列 API：rows/status 返回 ``phase3_enabled``。

harness：复用 test_funnel_stage1_1_29_7 的 _DbMixin / _Fake* /
DummyMpWaitService 等；TV 流水线测试复用 test_tv_flow_merge_1_29_4
的 MergeOwner（真实 TelevisionSyncProcessor / UpgradeService）。
"""

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_funnel_stage1_1_29_7 import (  # noqa: E402
    SyncHandler,
    _DbMixin,
    _FakeDedup,
    _TV_CLOUD_DIR,
    _TV_FILE,
    _TV_SIZE,
    _TV_STAGING,
    _ctx,
    _load_once,
    _movie_item,
    _service_module,
    _stub,
    _tv_item,
    _tv_media,
    DummyMpWaitService,
)
from test_manual_share_dedupe import _manual_resource  # noqa: E402
from test_tv_flow_merge_1_29_4 import (  # noqa: E402
    MergeOwner,
    TelevisionSyncProcessor,
    UpgradeService,
    _baseline_entry,
    _subscribe,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBSCRIBE_ID = 444


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
class FunnelPhase3ConfigTests(unittest.TestCase):
    """_init_funnel_flags：阶段 3 键 funnel_phase3_enabled 默认关。"""

    def _flags(self, values):
        fake_self = SimpleNamespace()
        SyncHandler._init_funnel_flags(
            fake_self, lambda key, default: values.get(key, default)
        )
        return fake_self

    def test_phase3_default_off(self):
        """全缺省：三阶段开关都关（kill-switch 默认关）。"""
        flags = self._flags({})
        self.assertFalse(flags._funnel_phase1_enabled)
        self.assertFalse(flags._funnel_phase2_enabled)
        self.assertFalse(flags._funnel_phase3_enabled)

    def test_phase3_value_on(self):
        """显式开：阶段 3 标志照原值读出。"""
        flags = self._flags({"funnel_phase3_enabled": True})
        self.assertTrue(flags._funnel_phase3_enabled)

    def test_config_default_off(self):
        """core/config.py 默认值里有本键且为 False（前端开关有后端默认）。"""
        path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "config.py"
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('"funnel_phase3_enabled": False', text)

    def test_all_eight_funnel_keys_have_defaults(self):
        """8 个漏斗键都有后端默认（阶段 1 的 6 键 + 阶段 2/3 各 1 键）。"""
        path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "config.py"
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for key in (
            "funnel_phase1_enabled", "funnel_phase2_enabled",
            "funnel_phase3_enabled", "funnel_target_dir", "funnel_trigger",
            "funnel_inflight_lease_seconds", "funnel_max_attempts",
            "funnel_dedup_strict",
        ):
            self.assertIn(f'"{key}"', text)


# ----------------------------------------------------------------------
# 路由判定（真实 SyncHandler 静态方法 + 接线断言）
# ----------------------------------------------------------------------
class FunnelPhase3RoutingTests(unittest.TestCase):
    """_funnel_route_for_subscribe_sync 真值表 + sync_subscribes/_do_sync 接线。"""

    def _route(self, phase1, phase3):
        return SyncHandler._funnel_route_for_subscribe_sync(phase1, phase3)

    def test_both_on_enqueues(self):
        """阶段 1 + 阶段 3 都开 → 订阅同步入队。"""
        self.assertTrue(self._route(True, True))

    def test_phase3_off_old_pipeline(self):
        """阶段 3 关 → 订阅同步走旧流水线。"""
        self.assertFalse(self._route(True, False))

    def test_phase1_off_old_pipeline(self):
        """阶段 1 是总闸：阶段 1 关时即使阶段 3 开，同步也不入队。"""
        self.assertFalse(self._route(False, True))

    def test_all_off_no_route(self):
        """两开关都关：不入队。"""
        self.assertFalse(self._route(False, False))

    def test_sync_subscribes_wiring(self):
        """sync_subscribes 对全部订阅驱动的同步（整轮 cron 与 MP 接管的
        订阅卡片搜索）都调阶段 3 判定谓词，不再只对整轮订阅判定；显式
        传入的 funnel_route（手动提交/媒体服务器洗版入口）不受影响。"""
        path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex",
            "core", "services", "sync.py",
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("def sync_subscribes(")
        end = text.index("\n    def ", start + 10)
        body = text[start:end]
        self.assertIn("_should_route_subscribe_sync_to_funnel", body)
        self.assertIn("_funnel_phase3_enabled", body)
        # 复核修补的回归锁：判定不再只对整轮订阅生效。
        self.assertNotIn("is_full_sync and not funnel_route", body)
        self.assertNotIn("_funnel_route_for_full_sync", body)


class FunnelPhase3RoutePredicateTests(unittest.TestCase):
    """_should_route_subscribe_sync_to_funnel 纯判定真值表（复核修补）。

    判定只看传入的上下文与两开关，不读库、不碰锁，可直接单测。
    """

    def _decide(self, **overrides):
        kwargs = {
            "funnel_route": False,
            "discovered_via": "",
            "manual_resources": None,
            "manual_target": None,
            "upgrade_request": None,
            "manual_upgrade": False,
            "phase1_enabled": True,
            "phase3_enabled": True,
        }
        kwargs.update(overrides)
        return SyncHandler._should_route_subscribe_sync_to_funnel(**kwargs)

    def test_full_sync_both_on(self):
        """整轮 cron：两开关都开 → 入队。"""
        self.assertTrue(self._decide())

    def test_card_search_both_on(self):
        """卡片搜索（单订阅/subscribe_ids/state）与整轮同一判定：两开关
        都开 → 入队（判定本身不区分调用形态，形态由调用点保证）。"""
        self.assertTrue(self._decide())

    def test_phase3_off(self):
        """阶段 3 关 → 不入队。"""
        self.assertFalse(self._decide(phase3_enabled=False))

    def test_phase1_off(self):
        """阶段 1 关（总闸）→ 不入队。"""
        self.assertFalse(self._decide(phase1_enabled=False))

    def test_explicit_route_kept(self):
        """显式传入的 funnel_route=True 保持（手动提交/媒体服务器洗版
        入口的路由不受阶段 3 判定改写）。"""
        self.assertTrue(self._decide(
            funnel_route=True, phase1_enabled=False, phase3_enabled=False,
        ))

    def test_tg_via_not_routed_by_phase3(self):
        """TG 项（discovered_via 非空）不由阶段 3 判定——阶段 2 关时
        TG 仍走旧流水线，哪怕阶段 3 开着。"""
        self.assertFalse(self._decide(discovered_via="tg:@QukanMovie/12281"))
        self.assertFalse(self._decide(discovered_via="push"))

    def test_manual_resources_not_routed_by_phase3(self):
        """手动资源提交不由阶段 3 判定（走 _funnel_route_for_submit）。"""
        self.assertFalse(self._decide(manual_resources=[{"file": {}}]))

    def test_manual_target_not_routed_by_phase3(self):
        """手动目标提交不由阶段 3 判定。"""
        self.assertFalse(self._decide(manual_target={"path": "/x"}))

    def test_upgrade_request_not_routed_by_phase3(self):
        """洗版请求不由阶段 3 判定（洗版入口自带显式 funnel_route）。"""
        self.assertFalse(self._decide(upgrade_request={"source": "resolved"}))

    def test_manual_upgrade_not_routed_by_phase3(self):
        """手动洗版不由阶段 3 判定。"""
        self.assertFalse(self._decide(manual_upgrade=True))

    def test_do_sync_producer_marker_wiring(self):
        """_do_sync 在 funnel_route 置位时同时下沉生产者标记
        （subscribe / upgrade / manual），finalize 边界与 STRM 分支靠它
        区分订阅轮次项与手动同目录项。"""
        path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex",
            "core", "services", "sync.py",
        )
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('setattr(subscribe, "_funnel_producer", producer)', text)
        self.assertIn(
            '"subscribe" if not manual_resources and not manual_target',
            text,
        )


# ----------------------------------------------------------------------
# 订阅行入队（finalize 边界）
# ----------------------------------------------------------------------
class FunnelSubscribeEnqueueTests(_DbMixin):
    """enqueue_from_pending：订阅轮次行的 source 与 notification_kind。"""

    def _sub_item(self, **extra):
        item = _tv_item(
            funnel_route=True, funnel_producer="subscribe",
            result_key="f-sub-09",
        )
        item.update(extra)
        return item

    def test_subscribe_item_source_is_subscribe_id(self):
        """订阅轮次行 source 为 subscribe:<id>（设计 §5.1）。"""
        row, created = self.service.enqueue_from_pending(
            self._sub_item(), "pk-sub-09", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], f"subscribe:{SUBSCRIBE_ID}")
        self.assertEqual(row["subscribe_id"], SUBSCRIBE_ID)
        self.assertEqual(row["notification_kind"], "transfer")

    def test_subscribe_item_without_id_source_is_subscribe(self):
        """订阅 id 缺失时 source 退化为 subscribe（不写空 id）。"""
        row, created = self.service.enqueue_from_pending(
            self._sub_item(), "pk-sub-09", _tv_media(),
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], "subscribe")

    def test_tg_via_still_wins_over_subscribe_producer(self):
        """TG 项即使带 producer 标记，source 仍是 via（阶段 2 口径不变）。"""
        row, created = self.service.enqueue_from_pending(
            self._sub_item(discovered_via="tg:@QukanMovie/12281"),
            "pk-sub-09", _tv_media(), subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], "tg:@QukanMovie/12281")

    def test_movie_subscribe_item_source(self):
        """电影订阅轮次行同样 source 为 subscribe:<id>，去重键 (movie, tmdb, 0, 0)。"""
        item = _movie_item(
            funnel_route=True, funnel_producer="subscribe",
            result_key="f-mv-01",
        )
        row, created = self.service.enqueue_from_pending(
            item, "pk-mv-01",
            SimpleNamespace(type="movie", tmdb_id=123456, title="测试电影", year="2026"),
            subscribe_id=445, season=None,
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], "subscribe:445")
        self.assertEqual(row["media_type"], "movie")
        self.assertEqual((row["season"], row["episode"]), (0, 0))

    def test_wash_item_kind_upgrade_and_replaced_from(self):
        """洗版行：kind 走 upgrade；replaced_from 含主旧版 + 额外旧版。"""
        item = self._sub_item(
            upgrade=True,
            upgrade_old_file_id="old-1",
            upgrade_old_cloud_dir=_TV_CLOUD_DIR,
            upgrade_old_file_name="多喜一家人 - S01E09 - 第9集.mkv",
            upgrade_old_size=1500000000,
            dedup_extra_old_versions=[{
                "file_id": "old-2",
                "cloud_dir": _TV_CLOUD_DIR,
                "file_name": "多喜一家人 - S01E09 - 第9集 (1).mkv",
                "file_size": 1400000000,
            }],
        )
        row, created = self.service.enqueue_from_pending(
            item, "pk-sub-09", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created)
        self.assertTrue(row["is_upgrade"])
        self.assertEqual(row["notification_kind"], "upgrade")
        self.assertEqual(row["source"], f"subscribe:{SUBSCRIBE_ID}")
        replaced = row["replaced_from"] or []
        self.assertEqual(len(replaced), 2)
        self.assertEqual(replaced[0]["file_id"], "old-1")
        self.assertEqual(replaced[1]["file_id"], "old-2")

    def test_second_enqueue_same_episode_returns_existing(self):
        """935 秒长上传窗口：第一份在途时第二份同集入队被注册表拦住，
        不建新行——同一集永远只留一份。"""
        first, created_first = self.service.enqueue_from_pending(
            self._sub_item(), "pk-sub-09", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created_first)
        # 模拟第一份仍在途（processing + 租约未过期，如 935 秒分片上传中）。
        now = int(time.time())
        self.assertTrue(self.repo.claim(first["id"], now + 7200, now=now))
        second, created_second = self.service.enqueue_from_pending(
            self._sub_item(result_key="f-sub-09-b"),
            "pk-sub-09-b", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertFalse(created_second)
        self.assertEqual(second["id"], first["id"])
        counts = self.repo.count_by_status()
        self.assertEqual(
            counts.get("queued", 0) + counts.get("processing", 0), 1
        )


# ----------------------------------------------------------------------
# 同目录 STRM 分支（真实 StrmOps）
# ----------------------------------------------------------------------
class FunnelSubscribeSameDirTests(unittest.TestCase):
    """_generate_or_queue_strm_batch：订阅/洗版路由项不再当场生成 STRM。"""

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

    def test_subscribe_routed_same_dir_item_queued_not_generated(self):
        """订阅轮次项（同目录）：入队，不当场生成 STRM——随后由漏斗
        worker 经 MP 整理，当场生成的 STRM 会指向被移走的文件而失效。"""
        service = self._service()
        item = _tv_item(funnel_route=True, funnel_producer="subscribe")
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.queue_calls[0][0]["result_key"], "f-tv-09")
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))

    def test_subscribe_unrouted_same_dir_item_generates_inline(self):
        """订阅项但 funnel_route 为假（阶段 3 关）：仍走旧直出路。"""
        service = self._service()
        item = _tv_item(funnel_route=False, funnel_producer="subscribe")
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(
            self.generate_calls, [(_TV_CLOUD_DIR, _TV_FILE)]
        )
        self.assertEqual(
            [item for batch in self.queue_calls for item in batch], []
        )
        self.assertEqual(results["f-tv-09"][0], Path("/tmp/x.strm"))

    def test_upgrade_routed_same_dir_item_queued_not_generated(self):
        """洗版项（同目录、funnel_route）：入队，不当场生成 STRM。"""
        service = self._service()
        item = _tv_item(funnel_route=True, upgrade=True)
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))

    def test_manual_same_dir_item_queued_not_generated(self):
        """v1.30.0 阶段 4（d）：手动同目录项（producer=manual）也入队，
        不再当场生成（阶段 3 有意留着手动直出路等阶段 4 统一）。"""
        service = self._service()
        item = _tv_item(funnel_route=True, funnel_producer="manual")
        results = service._generate_or_queue_strm_batch([item], _tv_media())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.queue_calls[0][0]["result_key"], "f-tv-09")
        self.assertEqual(results["f-tv-09"], (None, "pk-f-tv-09"))


# ----------------------------------------------------------------------
# 集成：真实驱动
# ----------------------------------------------------------------------
class FunnelSubscribeIntegrationTests(_DbMixin):
    """订阅轮次端到端（真实 _process_due_items + 真实 _process_row）。"""

    def _real_mp(self, pending, snapshot):
        """真实 DummyMpWaitService：只桩 I/O 边界（目录快照）与终态分发；
        _process_due_items 本身是真实的（含漏斗跳过语义）。快照用可变
        dict，轮次之间可改（改名后第一份进库、第二份再来时快照已变）。"""
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
            lambda path, cache=None: (True, dict(snapshot)),
        )
        self.owner.postprocess = mp
        return ctx, dispatch_calls

    def _sub_pending(self, pending_key="pk-sub-09", **extra):
        item = _tv_item(
            funnel_route=True, funnel_producer="subscribe",
            result_key="f-sub-09",
        )
        item.update(extra)
        self.owner._pending[pending_key] = dict(item)
        return self.owner._pending[pending_key]

    def test_subscribe_round_end_to_end(self):
        """订阅轮次缺集全链路：入队（source subscribe:444）→改名→实查→
        MP 整理恰好一次→终态 done；pending 记录带 MP 整理身份。"""
        item = self._sub_pending()
        row, created = self.service.enqueue_from_pending(
            item, "pk-sub-09", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created)
        self.assertEqual(row["source"], f"subscribe:{SUBSCRIBE_ID}")
        snapshot = {_TV_STAGING: {"file_id": "f-sub-09"}}
        ctx, dispatch_calls = self._real_mp(self.owner._pending, snapshot)
        stats = self.service.run_round()
        self.assertEqual(stats, {
            "processed": 1, "done": 1, "failed": 0, "held": 0, "skipped": 0,
        })
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "done")
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(
            self.owner.mp_organize.calls[0], (_TV_CLOUD_DIR, _TV_FILE, 1)
        )
        self.assertEqual(dispatch_calls, ["pk-sub-09"])
        self.assertEqual(
            self.owner._pending["pk-sub-09"].get("mp_organize"), {}
        )
        # 整理前改名确实跑过：暂存名 → 规范名。
        self.assertIn(
            (_TV_CLOUD_DIR, {"file_id": "f-sub-09"}, _TV_FILE),
            self.owner._cloud_drive.calls,
        )

    def test_renamed_first_copy_blocks_second(self):
        """改名后的第一份仍能拦住第二份：第一份改名入库后，第二份同集
        入队（注册表已释放可建新行），落盘前实查看到库里已有带 {tmdb-}
        的第一份，整行拦截，不触发第二次 MP 整理、不写第二份终态。"""
        item = self._sub_pending()
        row, created = self.service.enqueue_from_pending(
            item, "pk-sub-09", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created)
        snapshot = {_TV_STAGING: {"file_id": "f-sub-09"}}
        ctx, dispatch_calls = self._real_mp(self.owner._pending, snapshot)
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "done")
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        # 第二份：另一文件（另一 file_id），第一份已改名进库。
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-sub-09",
            "file_name": _TV_FILE,
            "file_size": _TV_SIZE,
        }]})
        snapshot.clear()
        snapshot.update({_TV_FILE: {"file_id": "f-sub-09"}})
        sub_item = self._sub_pending(
            "pk-sub-09-b", result_key="f-sub-09-b",
        )
        row2, created2 = self.service.enqueue_from_pending(
            sub_item, "pk-sub-09-b", _tv_media(),
            subscribe_id=SUBSCRIBE_ID, season=1,
        )
        self.assertTrue(created2)
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(row2["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertIn("已在库", fetched["skip_reason"])
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(dispatch_calls, ["pk-sub-09"])
        self.assertNotIn("pk-sub-09-b", self.owner._pending)


# ----------------------------------------------------------------------
# 终态回写：洗版评分（真实 SyncHandler 方法 + 桩存储）
# ----------------------------------------------------------------------
class _RecordingOper:
    """SubscribeOper 记录桩：get 返回罐装订阅，update 记录调用。"""

    instances = []

    def __init__(self, db=None):
        self._db = db
        self.updates = []
        _RecordingOper.instances.append(self)

    def get(self, subscribe_id):
        return SimpleNamespace(
            id=SUBSCRIBE_ID, name="多喜一家人", season=1, type="电视剧",
            note=[1, 2, 3, 4, 5, 6, 7, 8], total_episode=10,
            start_episode=1, lack_episode=0,
        )

    def update(self, subscribe_id, values):
        self.updates.append((subscribe_id, values))
        return True


class _FakeSessionCM:
    """SessionFactory 记录桩：with 语句返回空 db（SubscribeOper 不用它）。"""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class FunnelWashTerminalTests(unittest.TestCase):
    """_write_terminal_upgrade_priority 与 _finish_pending_subscription 的
    洗版评分回写门（只对漏斗归属洗版项）。"""

    def setUp(self):
        super().setUp()
        _RecordingOper.instances.clear()
        self._saved_oper = sys.modules["app.db.subscribe_oper"].SubscribeOper
        sys.modules["app.db.subscribe_oper"].SubscribeOper = _RecordingOper
        self.addCleanup(self._restore_oper)
        self._saved_factory = _service_module.SessionFactory
        self._saved_service_oper = _service_module.SubscribeOper
        _service_module.SessionFactory = lambda: _FakeSessionCM()
        _service_module.SubscribeOper = _RecordingOper
        self.addCleanup(self._restore_service_module)

    def _restore_oper(self):
        sys.modules["app.db.subscribe_oper"].SubscribeOper = self._saved_oper

    def _restore_service_module(self):
        _service_module.SessionFactory = self._saved_factory
        _service_module.SubscribeOper = self._saved_service_oper

    def _all_updates(self):
        return [
            update
            for inst in _RecordingOper.instances
            for update in inst.updates
        ]

    def _fake_self(self, priorities=None):
        fake = SimpleNamespace()
        fake._read_ep_priority = lambda subscribe: dict(priorities or {})
        fake._write_terminal_upgrade_priority = (
            lambda sub, eps, score: SyncHandler._write_terminal_upgrade_priority(
                fake, sub, eps, score
            )
        )
        fake._subscribe_handler = MagicMock()
        fake.metadata = MagicMock()
        fake._search_handler = MagicMock()
        fake._cloud_drive = SimpleNamespace(name="115网盘")
        return fake

    def _finish_item(self, **extra):
        item = _tv_item(
            funnel_route=True, upgrade=True, dedup_new_score=90,
        )
        item.update(extra)
        return item

    def test_priority_written_when_score_differs(self):
        """终态成功：洗版新评分写进 episode_priority（旧 80→新 90）。"""
        fake = self._fake_self(priorities={"9": 80})
        subscribe = SimpleNamespace(id=SUBSCRIBE_ID, name="多喜一家人")
        written = SyncHandler._write_terminal_upgrade_priority(
            fake, subscribe, [9], 90
        )
        self.assertTrue(written)
        self.assertEqual(
            self._all_updates(),
            [(SUBSCRIBE_ID, {"episode_priority": {"9": 90}})],
        )

    def test_priority_not_written_when_same_score(self):
        """评分相同不写库（幂等，终态重跑不产生多余写）。"""
        fake = self._fake_self(priorities={"9": 90})
        subscribe = SimpleNamespace(id=SUBSCRIBE_ID, name="多喜一家人")
        written = SyncHandler._write_terminal_upgrade_priority(
            fake, subscribe, [9], 90
        )
        self.assertFalse(written)
        self.assertEqual(self._all_updates(), [])

    def test_priority_not_written_without_score_or_episodes(self):
        """无评分 / 无集数时不写库。"""
        fake = self._fake_self(priorities={})
        subscribe = SimpleNamespace(id=SUBSCRIBE_ID, name="多喜一家人")
        self.assertFalse(
            SyncHandler._write_terminal_upgrade_priority(fake, subscribe, [9], 0)
        )
        self.assertFalse(
            SyncHandler._write_terminal_upgrade_priority(fake, subscribe, [], 90)
        )
        self.assertEqual(self._all_updates(), [])

    def test_finish_writes_priority_for_funnel_wash_item(self):
        """终态回写：漏斗归属洗版项 → check_and_finish_subscribe 计进度，
        且评分回写被调到（90 分写进 episode_priority）。"""
        fake = self._fake_self(priorities={"9": 80})
        SyncHandler._finish_pending_subscription(
            fake, self._finish_item(), {}, _tv_media()
        )
        fake._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        call = fake._subscribe_handler.check_and_finish_subscribe.call_args
        self.assertEqual(call.kwargs["success_episodes"], [9])
        self.assertEqual(
            self._all_updates(),
            [(SUBSCRIBE_ID, {"episode_priority": {"9": 90}})],
        )

    def test_finish_skips_priority_for_old_pipeline_wash_item(self):
        """旧流水线洗版项（funnel_route 为假）：进度照常回写，评分不
        在这里重复写（轮次内已更新）。"""
        fake = self._fake_self(priorities={"9": 80})
        SyncHandler._finish_pending_subscription(
            fake, self._finish_item(funnel_route=False), {}, _tv_media()
        )
        fake._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        self.assertEqual(self._all_updates(), [])

    def test_finish_skips_priority_for_non_upgrade_item(self):
        """漏斗归属但非洗版项：不写评分。"""
        fake = self._fake_self(priorities={})
        SyncHandler._finish_pending_subscription(
            fake, self._finish_item(upgrade=False), {}, _tv_media()
        )
        fake._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        self.assertEqual(self._all_updates(), [])

    def test_finish_skips_priority_without_new_score(self):
        """漏斗洗版项但无新评分（dedup_new_score=0）：不写评分。"""
        fake = self._fake_self(priorities={})
        SyncHandler._finish_pending_subscription(
            fake, self._finish_item(dedup_new_score=0), {}, _tv_media()
        )
        fake._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        self.assertEqual(self._all_updates(), [])


# ----------------------------------------------------------------------
# 轮次结算守卫（真实 TelevisionSyncProcessor / MergeOwner 桩）
# ----------------------------------------------------------------------
class _PendingMergeOwner(MergeOwner):
    """MergeOwner + 管线返回待终态（pending_key），模拟漏斗归属项。"""

    def _transfer_episode_items(self, items, *args, **kwargs):
        self.transfer_items.append(list(items))
        return [
            {"item": item, "success": True, "pending_key": f"pk-{item['episode']}"}
            for item in items
        ]


class FunnelTvSettlementGuardTests(unittest.TestCase):
    """轮次结束结算守卫：有漏斗在途行时不结算，无在途行时照常结算；
    洗版待终态项不计入本轮进度/评分。"""

    def _search_stub(self, owner, resource=None):
        owner._search_handler.search_sources.return_value = {
            "manual": [resource or _manual_resource()],
        }

    def test_round_end_settlement_skipped_with_active_funnel_rows(self):
        """普通订阅：缺集已转（立即成功），但订阅仍有漏斗在途行 → 本轮
        不调 check_and_finish_subscribe，终态成功后由漏斗回写。"""
        owner = MergeOwner({}, cloud_upgrade_subscribe=False)
        owner.cloud_episodes = {1, 2, 3}
        self._search_stub(owner)
        owner.funnel_has_unsettled_rows = lambda subscribe_id: True
        subscribe = _subscribe(total_episode=5)
        subscribe.id = SUBSCRIBE_ID
        result = TelevisionSyncProcessor(owner).process_tv_subscribe(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 2)
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()

    def test_round_end_settlement_runs_without_active_funnel_rows(self):
        """对照：无在途行时照常结算（既有行为不变）。"""
        owner = MergeOwner({}, cloud_upgrade_subscribe=False)
        owner.cloud_episodes = {1, 2, 3}
        self._search_stub(owner)
        owner.funnel_has_unsettled_rows = lambda subscribe_id: False
        subscribe = _subscribe(total_episode=5)
        subscribe.id = SUBSCRIBE_ID
        result = TelevisionSyncProcessor(owner).process_tv_subscribe(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 2)
        owner._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        call = owner._subscribe_handler.check_and_finish_subscribe.call_args
        self.assertEqual(set(call.kwargs["success_episodes"]), {1, 2, 3, 4, 5})

    def test_fast_path_settlement_skipped_with_active_funnel_rows(self):
        """快路径：目标网盘目录已完整（含在途文件），但订阅仍有漏斗在途
        行 → 不结算；在途文件被算作“已存在”是开场扫描的口径，守卫兜底。"""
        owner = MergeOwner({}, cloud_upgrade_subscribe=False)
        owner.cloud_episodes = {1, 2, 3, 4, 5}
        self._search_stub(owner)
        owner.funnel_has_unsettled_rows = lambda subscribe_id: True
        subscribe = _subscribe(total_episode=5)
        subscribe.id = SUBSCRIBE_ID
        result = TelevisionSyncProcessor(owner).process_tv_subscribe(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 0)
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()

    def test_wash_pending_funnel_items_defer_settlement(self):
        """洗版：替换项待终态且走漏斗 → 不计入本轮 update_subscribe_progress，
        也不结算订阅；终态成功时由 _finish_pending_subscription 回写。"""
        baseline = {1: _baseline_entry(1, 5)}
        owner = _PendingMergeOwner(baseline, cloud_upgrade_subscribe=True)
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=1)
        subscribe.id = SUBSCRIBE_ID
        subscribe._funnel_route = True
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [1])
        self.assertTrue(items[0]["is_upgrade"])
        owner._subscribe_handler.update_subscribe_progress.assert_not_called()
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()

    def test_wash_immediate_items_still_settle_in_round(self):
        """对照：洗版项立即成功（无 pending_key）时本轮照常更新进度，
        递延只针对“待终态且走漏斗”的项。"""
        baseline = {1: _baseline_entry(1, 5)}
        owner = MergeOwner(baseline, cloud_upgrade_subscribe=True)
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=1)
        subscribe.id = SUBSCRIBE_ID
        subscribe._funnel_route = True
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 1)
        owner._subscribe_handler.update_subscribe_progress.assert_called_once()
        call = owner._subscribe_handler.update_subscribe_progress.call_args
        self.assertEqual(call.kwargs["success_episodes"], [1])


# ----------------------------------------------------------------------
# 整理队列 API：phase3_enabled
# ----------------------------------------------------------------------
class FunnelApiPhase3Tests(unittest.TestCase):
    """api_vue_funnel_rows / api_vue_funnel_status 返回 phase3_enabled。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.funnel_api_module = _load_once(
            "cloudsubscribex.core.api.funnel",
            "plugins.v2/cloudsubscribex/core/api/funnel.py",
        )
        cls.FunnelApi = cls.funnel_api_module.FunnelApi

    def _api(self, phase1=True, phase2=True, phase3=True):
        api = self.FunnelApi.__new__(self.FunnelApi)
        object.__setattr__(api, "_funnel_enabled", True)
        object.__setattr__(api, "_funnel_scan_dry_run", True)
        object.__setattr__(api, "_funnel_phase1_enabled", phase1)
        object.__setattr__(api, "_funnel_phase2_enabled", phase2)
        object.__setattr__(api, "_funnel_phase3_enabled", phase3)
        # _funnel_target_dir / _funnel_enabled / _funnel_scan_dry_run
        # 都要显式设：否则 getattr 会经 OwnerDelegator 委托到未设置的
        # _owner，递归爆栈。
        object.__setattr__(api, "_funnel_target_dir", "/Alist/影视/moviepilot/转存/")
        handler = SimpleNamespace(
            funnel_queue_counts=lambda: {
                "queued": 0, "processing": 0, "held": 0,
            },
            funnel_list_rows=lambda statuses, limit=200: [],
        )
        object.__setattr__(
            api, "_require_sync_handler", lambda *names: (handler, None)
        )
        object.__setattr__(api, "_scheduler_registry", lambda: None)
        return api

    def test_rows_returns_phase3_enabled(self):
        """rows 响应带 phase3_enabled（队列页徽标据此分两态）。"""
        result = self._api(phase3=True).api_vue_funnel_rows()
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["phase3_enabled"])
        self.assertTrue(result["data"]["phase1_enabled"])
        self.assertTrue(result["data"]["phase2_enabled"])

    def test_rows_phase3_off(self):
        """阶段 3 关时 rows 如实返回 False。"""
        result = self._api(phase3=False).api_vue_funnel_rows()
        self.assertTrue(result["success"])
        self.assertFalse(result["data"]["phase3_enabled"])

    def test_status_returns_phase3_enabled(self):
        """status 响应带 phase3_enabled。"""
        result = self._api(phase3=True).api_vue_funnel_status()
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["phase3_enabled"])
        self.assertFalse(result["data"]["worker_running"])


if __name__ == "__main__":
    unittest.main()
