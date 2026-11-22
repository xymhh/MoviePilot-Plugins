"""漏斗阶段 4（v1.30.0，修订范围）测试。

覆盖（设计文档 §12 阶段 4 + §13 测试计划）：
- funnel_enabled 总闸：OFF 时生产者回旧流水线，ON 时走漏斗；
- organize_source_path / organize_after_transfer 静默退役（v1.29.4
  quality_prefer_enabled 做法：默认里删、normalize 静默 pop、不报警告）；
- 失败记录页手动重试重新入队（attempts 清零）；
- 手动同目录 STRM 分支统一入队；
- held 行生命周期：不翻 failed、重新入队清零、人工认领后入队；
- 轮次历史（最近 20 轮）；
- 崩溃恢复：processing 行租约过期后回收重跑、终态只提交一次；
- 孤儿 .mkv/STRM 人工清理（删 .mkv 连带 .strm；.strm 删失败时标记保留，
  行视图展示原因去掉 orphan_location= 机器标记）；
- 失败记录页口径：pending_keys_with_rows 批量查行（source 标
  “整理漏斗”的依据）与漏斗 API 路由注册。

扫描对账 dry-run 另见 tests/test_funnel_scan_dryrun_1_30_0.py。

脚手架复用 tests/test_funnel_stage1_1_29_7.py（_DbMixin 真实临时
SQLite + _FakeOwner + FunnelService），退役断言复用
tests/test_config_whitelist_1_12_20.py 的 _plugin/_default_keys，
SyncApi 路由加载复用 tests/test_funnel_stage2_1_29_8.py 的
setUpClass 保存/恢复模式。
"""

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_mock,
    register_module,
)
# 子集跑时本文件可能在 baseline 之前被收集：自己补齐脚手架链要的
# app.* 桩（test_finalize_mp_wait 加载 service.py 的导入链要这些，
# 全量里由更早收集的文件提供；install_app_mocks 幂等）。settings 的
# DB_TIMEOUT 在 import 完成后由 _ensure_db_settings 原地补（见下）。
install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.core.cache",
    "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.subscribe",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver",
)
_config_module = sys.modules.get("app.core.config")
_settings_obj = getattr(_config_module, "settings", None)
if not isinstance(getattr(_settings_obj, "DB_TIMEOUT", None), (int, float)):
    register_mock(
        "app.core.config",
        settings=SimpleNamespace(
            DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp"),
        ),
    )
from test_config_whitelist_1_12_20 import (  # noqa: E402
    _default_keys,
    _plugin as _bare_plugin,
)
from test_funnel_stage1_1_29_7 import (  # noqa: E402
    FunnelService,
    SyncHandler,
    _DbMixin,
    _TV_CLOUD_DIR,
    _TV_FILE,
    _ctx,
    _load_once,
    _stub,
    _tv_media,
)

# retry.py 真实模块（失败记录页重试路径）。
_retry_module = _load_once(
    "cloudsubscribex.handlers.sync.retry",
    "plugins.v2/cloudsubscribex/handlers/sync/retry.py",
)
HistoryRetryService = _retry_module.HistoryRetryService

# strm_ops.py 真实模块（同目录 STRM 分支统一；加载方式同
# tests/test_strm_pickcode_refresh_1_17_0.py）。
install_core_delegation()
_utils = ensure_package("cloudsubscribex.utils")
_strm = load_module(
    "cloudsubscribex.utils.strm",
    "plugins.v2/cloudsubscribex/utils/strm.py",
)
_utils.StrmTemplateError = _strm.StrmTemplateError
_utils.plan_share_strm_files = _strm.plan_share_strm_files
_utils.rebuild_strm_tree = MagicMock()
_utils.rebuild_strm_by_export_tree = MagicMock()
load_module(
    "cloudsubscribex.utils.legacy_strm",
    "plugins.v2/cloudsubscribex/utils/legacy_strm.py",
)
ensure_package("cloudsubscribex.core").CloudFile = type("CloudFile", (), {})
_strm_ops = load_module(
    "cloudsubscribex.handlers.sync.strm_ops",
    "plugins.v2/cloudsubscribex/handlers/sync/strm_ops.py",
)
StrmOpsMixin = _strm_ops.StrmOpsMixin

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")
FRONTEND_DIR = os.path.join(REPO_ROOT, "frontend", "cloudsubscribex", "src")

# core/api/funnel.py 真实模块（行视图：展示原因去标记）。
_funnel_api_module = _load_once(
    "cloudsubscribex.core.api.funnel",
    os.path.join(PLUGIN_DIR, "core", "api", "funnel.py"),
)
FunnelApi = _funnel_api_module.FunnelApi


def _fake_build_pending_record(calls):
    """忠实迷你版 _build_pending_record：只按 kwargs 建记录字典。

    真实方法签名见 handlers/sync/service.py:_build_pending_record；
    这里只复刻 ensure_pending_record 用到的字段（task_type/share_url/
    first_seen_at/next_check_at/funnel_route 等），调用记进 calls 供
    “重建只在记录缺失时发生”的断言用。
    """

    def build(*, current=None, pending_key="", task_type="share",
            info_hash="", source_hash="", share_url="", cloud_dir="",
            file_name="", staging_dir="", staging_name="", file_size=0,
            now=None, mediainfo=None, media_data=None, subscribe_id=None,
            success_episodes=None, notification_episodes=None, season=None,
            upgrade=False, upgrade_old_cloud_dir="",
            upgrade_old_file_name="", upgrade_old_file_id="",
            upgrade_old_size=0, dedup_extra_old_versions=None,
            dedup_new_score=0, funnel_route=False, **_ignored):
        started = now if now is not None else time.time()
        calls.append({
            "pending_key": pending_key,
            "task_type": task_type,
            "share_url": share_url,
            "source_hash": source_hash,
            "funnel_route": funnel_route,
        })
        return {
            "pending_key": pending_key,
            "task_type": task_type,
            "info_hash": info_hash,
            "source_hash": source_hash,
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "file_size": file_size,
            "first_seen_at": started,
            "next_check_at": started + 10,
            "fail_count": 0,
            "funnel_route": funnel_route,
            "mediainfo": mediainfo,
            "media_data": media_data,
            "subscribe_id": subscribe_id,
            "success_episodes": list(success_episodes or []),
            "notification_episodes": list(notification_episodes or []),
            "season": season,
            "upgrade": upgrade,
            "dedup_new_score": dedup_new_score,
        }

    return build


def _ensure_db_settings():
    """DB 模块首次加载前，保证 settings.DB_TIMEOUT/TZ 为真值。

    test_downloader_plugin（经 whitelist 链）在加载真实插件 __init__
    时把 app.core.config.settings 换成只有 API_TOKEN 的桩；manager.py
    在 _DbMixin.setUpClass 里 ``from app.core.config import settings``
    绑定当时对象。原地补属性（不换对象，已绑定的模块不受影响）；全量
    里 DB 模块早被更早的文件加载并绑定好桩，这里不动。
    """
    module = sys.modules.get("app.core.config")
    settings_obj = getattr(module, "settings", None) if module is not None else None
    if settings_obj is None:
        return
    if not isinstance(getattr(settings_obj, "DB_TIMEOUT", None), (int, float)):
        settings_obj.DB_TIMEOUT = 30
    if not isinstance(getattr(settings_obj, "TZ", None), str):
        settings_obj.TZ = "Asia/Shanghai"


class _Stage4DbMixin(_DbMixin):
    """带待终态记录重建桩的 _DbMixin：重试/放回/认领路径共用。"""

    @classmethod
    def setUpClass(cls):
        _ensure_db_settings()
        super().setUpClass()

    def setUp(self):
        super().setUp()
        self.build_calls = []
        self.owner._build_pending_record = _fake_build_pending_record(
            self.build_calls
        )
        # 重试服务经 owner.funnel 找漏斗行。
        self.owner.funnel = self.service


# ----------------------------------------------------------------------
# 总闸 funnel_enabled
# ----------------------------------------------------------------------
class FunnelMasterGateTests(unittest.TestCase):
    """funnel_enabled 总闸：OFF 时阶段键全灭、生产者回旧流水线。"""

    def _flags(self, values):
        fake_self = SimpleNamespace()
        SyncHandler._init_funnel_flags(
            fake_self, lambda key, default: values.get(key, default)
        )
        return fake_self

    def test_master_off_folds_all_phases_off(self):
        """总闸关：阶段 1/2/3 原始值全开也被折叠为关。"""
        flags = self._flags({
            "funnel_enabled": False,
            "funnel_phase1_enabled": True,
            "funnel_phase2_enabled": True,
            "funnel_phase3_enabled": True,
        })
        self.assertFalse(flags._funnel_enabled)
        self.assertFalse(flags._funnel_phase1_enabled)
        self.assertFalse(flags._funnel_phase2_enabled)
        self.assertFalse(flags._funnel_phase3_enabled)

    def test_master_on_phases_follow_raw(self):
        """总闸开：阶段键按原始值生效（各 kill-switch 独立）。"""
        flags = self._flags({
            "funnel_enabled": True,
            "funnel_phase1_enabled": True,
            "funnel_phase2_enabled": False,
            "funnel_phase3_enabled": True,
        })
        self.assertTrue(flags._funnel_enabled)
        self.assertTrue(flags._funnel_phase1_enabled)
        self.assertFalse(flags._funnel_phase2_enabled)
        self.assertTrue(flags._funnel_phase3_enabled)

    def test_master_missing_defaults_on(self):
        """总闸缺失（老配置）：默认开，阶段键按原始值生效。"""
        flags = self._flags({
            "funnel_phase1_enabled": True,
            "funnel_phase2_enabled": True,
            "funnel_phase3_enabled": False,
        })
        self.assertTrue(flags._funnel_enabled)
        self.assertTrue(flags._funnel_phase1_enabled)
        self.assertTrue(flags._funnel_phase2_enabled)
        self.assertFalse(flags._funnel_phase3_enabled)
        self.assertTrue(flags._funnel_scan_dry_run)

    def test_master_off_routes_producers_to_old_pipeline(self):
        """总闸关→阶段 1 有效值关→三路生产者判定全为 False（回旧流水线）。"""
        # 手动提交 / TG 提交：core/api/sync.py 的纯判定。
        self.assertFalse(
            self.SyncApi._funnel_route_for_submit(
                phase1_enabled=False, phase2_enabled=True,
                discovered_via="", wait=False,
            )
        )
        self.assertFalse(
            self.SyncApi._funnel_route_for_submit(
                phase1_enabled=False, phase2_enabled=True,
                discovered_via="tg:@QukanMovie/1", wait=True,
            )
        )
        # 订阅驱动同步：SyncHandler 的纯判定。
        self.assertFalse(
            SyncHandler._funnel_route_for_subscribe_sync(False, True)
        )
        self.assertFalse(
            SyncHandler._should_route_subscribe_sync_to_funnel(
                funnel_route=False, phase1_enabled=False,
                phase3_enabled=True,
            )
        )

    def test_master_on_routes_producers_to_funnel(self):
        """总闸开→阶段 1 有效值开→各路按自己的阶段开关判定。"""
        self.assertTrue(
            self.SyncApi._funnel_route_for_submit(
                phase1_enabled=True, phase2_enabled=True,
                discovered_via="", wait=False,
            )
        )
        self.assertTrue(
            self.SyncApi._funnel_route_for_submit(
                phase1_enabled=True, phase2_enabled=True,
                discovered_via="tg:@QukanMovie/1", wait=True,
            )
        )
        self.assertFalse(
            self.SyncApi._funnel_route_for_submit(
                phase1_enabled=True, phase2_enabled=False,
                discovered_via="tg:@QukanMovie/1", wait=True,
            )
        )
        self.assertTrue(
            SyncHandler._funnel_route_for_subscribe_sync(True, True)
        )
        self.assertFalse(
            SyncHandler._funnel_route_for_subscribe_sync(True, False)
        )
        self.assertTrue(
            SyncHandler._should_route_subscribe_sync_to_funnel(
                funnel_route=False, phase1_enabled=True,
                phase3_enabled=True,
            )
        )

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._saved_modules = {}
        cls._saved_attrs = {}
        for name in (
            "cloudsubscribex.core.api.sync",
            "cloudsubscribex.core.cloud",
            "cloudsubscribex.core.search",
            "cloudsubscribex.search.matching",
        ):
            cls._saved_modules[name] = sys.modules.get(name, None)
            parent_name, _, child = name.rpartition(".")
            if parent_name and parent_name in sys.modules:
                parent = sys.modules[parent_name]
                cls._saved_attrs[(parent_name, child)] = getattr(
                    parent, child, None
                )
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
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
        for (parent_name, child), saved in cls._saved_attrs.items():
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if saved is None:
                try:
                    delattr(parent, child)
                except AttributeError:
                    pass
            else:
                setattr(parent, child, saved)
        super().tearDownClass()


# ----------------------------------------------------------------------
# 两个废弃键静默清理
# ----------------------------------------------------------------------
class FunnelRetiredKeysTests(unittest.TestCase):
    """organize_source_path / organize_after_transfer 静默退役。

    v1.29.4 quality_prefer_enabled 的做法：默认里删、normalize 在白
    名单循环前静默 pop、后端不再读配置字面量、前端字段删掉。
    """

    def test_retired_keys_dropped_silently_by_normalize(self):
        """旧值进 normalize 即消失；真实 _apply 不落属性、不报警告。"""
        plugin, module = _bare_plugin()
        config = {
            "organize_source_path": "/Alist/旧目录",
            "organize_after_transfer": False,
            "funnel_enabled": True,
        }
        cleaned = module.normalize_plugin_config(dict(config))
        self.assertNotIn("organize_source_path", cleaned)
        self.assertNotIn("organize_after_transfer", cleaned)
        self.assertTrue(cleaned["funnel_enabled"])
        warning = module.logger.warning
        warning.reset_mock()
        plugin._apply_plugin_config(dict(config), reset_runtime=True)
        self.assertNotIn("_organize_source_path", plugin.__dict__)
        self.assertNotIn("_organize_after_transfer", plugin.__dict__)
        self.assertTrue(plugin.__dict__["_funnel_enabled"])
        for call in warning.call_args_list:
            text = str(call.args[0]) if call.args else ""
            self.assertNotIn("organize_source_path", text)
            self.assertNotIn("organize_after_transfer", text)

    def test_retired_keys_not_in_defaults_or_whitelist(self):
        """默认与白名单里都没有退役键；两个新漏斗键都在。"""
        _, module = _bare_plugin()
        defaults = _default_keys()
        for key in ("organize_source_path", "organize_after_transfer"):
            self.assertNotIn(key, defaults)
            self.assertFalse(module.is_plugin_config_key(key))
        for key in ("funnel_enabled", "funnel_scan_dry_run"):
            self.assertIn(key, defaults)
            self.assertTrue(module.is_plugin_config_key(key))

    def test_no_config_get_or_get_val_literals_for_retired_keys(self):
        """源码里不再有读这两个键的 config.get / get_val 字面量。"""
        for root, _dirs, files in os.walk(PLUGIN_DIR):
            for name in files:
                if not name.endswith(".py"):
                    continue
                source = open(
                    os.path.join(root, name), encoding="utf-8"
                ).read()
                for key in (
                    "organize_source_path", "organize_after_transfer",
                ):
                    self.assertNotIn(f'config.get("{key}"', source)
                    self.assertNotIn(f"get_val(\"{key}\"", source)

    def test_retired_frontend_fields_gone_new_funnel_fields_present(self):
        """前端：退役字段定义删掉，两个新漏斗键在转存组与常用设置里。"""
        transfer = open(
            os.path.join(FRONTEND_DIR, "config", "fields", "transfer.js"),
            encoding="utf-8",
        ).read()
        organize = open(
            os.path.join(FRONTEND_DIR, "config", "fields", "organize.js"),
            encoding="utf-8",
        ).read()
        config_vue = open(
            os.path.join(FRONTEND_DIR, "components", "Config.vue"),
            encoding="utf-8",
        ).read()
        self.assertNotIn("organize_after_transfer", transfer)
        self.assertNotIn("organize_source_path", organize)
        for key in ("funnel_enabled", "funnel_scan_dry_run"):
            self.assertIn(f'key: "{key}"', transfer)
            self.assertIn(f'"{key}"', config_vue)


# ----------------------------------------------------------------------
# 手动重试重新入队
# ----------------------------------------------------------------------
class FunnelRetryRequeueTests(_Stage4DbMixin):
    """失败记录页手动重试：漏斗行重新入队（attempts 清零），非漏斗行不变。"""

    def _held_row(self, **overrides):
        row = self._enqueue_tv(**overrides)
        self.repo.mark_held(row["id"], "待人工复核")
        return self.repo.get_by_id(row["id"])

    def _retry_service(self):
        return HistoryRetryService(self.owner)

    def _record(self, finalize_key):
        return {
            "time": "2026-11-22 10:00:00",
            "share_url": "https://pan.quark.cn/s/abcdef123456",
            "file_name": _TV_FILE,
            "status": "失败",
            "failure_reason": "转存失败",
            "finalize_key": finalize_key,
        }

    def test_retry_funnel_row_requeues_held_row(self):
        """held 行重试：放回 queued、attempts 清零、待终态记录已重建。"""
        row = self._held_row()
        service = self._retry_service()
        self.assertTrue(service._retry_funnel_row(self._record("pk-tv-09")))
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(self.build_calls[0]["task_type"], "share")
        self.assertEqual(self.build_calls[0]["share_url"], "")
        self.assertTrue(self.build_calls[0]["funnel_route"])
        self.assertIn("pk-tv-09", self.owner._pending)

    def test_retry_funnel_row_refuses_queued_row(self):
        """行已在处理中：拒绝重试（ValueError），不改状态。"""
        row = self._enqueue_tv()
        service = self._retry_service()
        with self.assertRaises(ValueError):
            service._retry_funnel_row(self._record("pk-tv-09"))
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "queued"
        )

    def test_retry_funnel_row_no_row_falls_back(self):
        """记录有 finalize_key 但无漏斗行：返回 False，走旧重试路。"""
        service = self._retry_service()
        self.assertFalse(service._retry_funnel_row(self._record("pk-missing")))
        self.assertEqual(self.build_calls, [])

    def test_retry_funnel_row_no_finalize_key_falls_back(self):
        """记录无 finalize_key：返回 False，走旧重试路。"""
        row = self._held_row()
        service = self._retry_service()
        record = self._record("")
        record.pop("finalize_key")
        self.assertFalse(service._retry_funnel_row(record))
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )

    def test_retry_funnel_row_funnel_off_falls_back(self):
        """漏斗关：held 行也不入队，返回 False，走旧重试路。"""
        row = self._held_row()
        self.owner._funnel_phase1_enabled = False
        service = self._retry_service()
        self.assertFalse(service._retry_funnel_row(self._record("pk-tv-09")))
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )

    def test_retry_history_record_returns_early_for_funnel_row(self):
        """整条重试入口：漏斗行直接返回“已重新入队”，旧路一次都不跑。"""
        row = self._held_row()
        service = self._retry_service()
        record = self._record("pk-tv-09")
        _stub(service, "_find_history_record", lambda *a, **k: record)
        _stub(
            service, "_resolve_history_retry_context",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("旧重试路不应被调用")
            ),
        )
        result = service.retry_history_record(
            record["time"], record["share_url"], record["file_name"],
            force=True,
        )
        self.assertEqual(
            result,
            {"success": True, "message": "已重新入队，整理漏斗将继续处理"},
        )
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)

    def test_mark_retry_failed_keeps_finalize_key_while_funnel_row_exists(self):
        """后台重试异常：漏斗行还在时保留 finalize_key（下次重试的线索）。"""
        self._held_row()
        service = self._retry_service()
        record = self._record("pk-tv-09")
        _stub(service, "_find_history_record", lambda *a, **k: record)
        upserted = []
        _stub(
            service, "_upsert_retry_index_row",
            lambda rec, status: upserted.append((dict(rec), status)) or True,
        )
        service._mark_history_retry_failed(
            record["time"], record["share_url"], record["file_name"], "boom",
        )
        self.assertEqual(record["finalize_key"], "pk-tv-09")
        self.assertEqual(upserted[0][1], "失败")

    def test_mark_retry_failed_pops_finalize_key_without_funnel_row(self):
        """无漏斗行：stale finalize_key 照旧清掉（旧路重传无重复入库风险）。"""
        service = self._retry_service()
        record = self._record("pk-missing")
        _stub(service, "_find_history_record", lambda *a, **k: record)
        _stub(
            service, "_upsert_retry_index_row",
            lambda rec, status: True,
        )
        service._mark_history_retry_failed(
            record["time"], record["share_url"], record["file_name"], "boom",
        )
        self.assertNotIn("finalize_key", record)


# ----------------------------------------------------------------------
# 手动同目录 STRM 分支统一
# ----------------------------------------------------------------------
class _FakeCloudQuery:
    def get_cached_file(self, cloud_dir, file_name):
        return {"file_id": "f-manual", "name": file_name}


class FunnelSameDirStrmTests(unittest.TestCase):
    """同目录 STRM 分支：funnel_route 为真就入队（含手动同目录）。"""

    def _mixin(self):
        owner = SimpleNamespace(
            _organize_after_transfer=True,
            _cloud_query=_FakeCloudQuery(),
        )
        mixin = StrmOpsMixin(owner)
        self.generate_calls = []
        self.queue_calls = []

        def _generate(*args, **kwargs):
            self.generate_calls.append((args, kwargs))
            return Path("/tmp/fake.strm")

        def _queue(items, mediainfo, **kwargs):
            self.queue_calls.append(list(items))
            return {
                str(item["result_key"]): "pk-" + str(item["result_key"])
                for item in items
            }

        object.__setattr__(mixin, "_generate_strm", _generate)
        object.__setattr__(mixin, "_queue_file_finalize_batch", _queue)
        object.__setattr__(
            mixin, "_scrape_metadata_batch", lambda *a, **k: None
        )
        return mixin

    def _item(self, **extra):
        item = {
            "result_key": "f-manual-1",
            "staging_dir": _TV_CLOUD_DIR,
            "cloud_dir": _TV_CLOUD_DIR,
            "file_name": _TV_FILE,
        }
        item.update(extra)
        return item

    def test_manual_same_dir_funnel_routed_enqueues(self):
        """手动同目录 + funnel_route：入队，不当场生成 STRM。"""
        mixin = self._mixin()
        results = mixin._generate_or_queue_strm_batch(
            [self._item(funnel_route=True)], MagicMock()
        )
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.queue_calls), 1)
        self.assertEqual(self.queue_calls[0][0]["result_key"], "f-manual-1")
        self.assertEqual(results["f-manual-1"], (None, "pk-f-manual-1"))

    def test_manual_same_dir_unrouted_generates_inline(self):
        """手动同目录 + 无 funnel_route：仍走旧直出路（当场生成）。"""
        mixin = self._mixin()
        results = mixin._generate_or_queue_strm_batch(
            [self._item()], MagicMock()
        )
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.queue_calls, [[]])
        self.assertEqual(results["f-manual-1"][0], Path("/tmp/fake.strm"))
        self.assertEqual(results["f-manual-1"][1], "")

    def test_tg_and_subscribe_routed_still_enqueue(self):
        """TG/订阅路由项（funnel_route）：统一判定下仍入队，不回归直出。"""
        mixin = self._mixin()
        items = [
            self._item(
                result_key="f-tg-1", funnel_route=True,
                discovered_via="tg:@QukanMovie/1",
            ),
            self._item(
                result_key="f-sub-1", funnel_route=True,
                funnel_producer="subscribe:444",
            ),
        ]
        results = mixin._generate_or_queue_strm_batch(items, MagicMock())
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(
            {item["result_key"] for item in self.queue_calls[0]},
            {"f-tg-1", "f-sub-1"},
        )
        self.assertEqual(results["f-tg-1"], (None, "pk-f-tg-1"))
        self.assertEqual(results["f-sub-1"], (None, "pk-f-sub-1"))


# ----------------------------------------------------------------------
# held 行生命周期
# ----------------------------------------------------------------------
class FunnelHeldLifecycleTests(_Stage4DbMixin):
    """held 行：不翻 failed、重新入队清零、人工认领后入队。"""

    def _held_row(self, **overrides):
        row = self._enqueue_tv(**overrides)
        self.repo.mark_held(row["id"], "待人工复核")
        return self.repo.get_by_id(row["id"])

    def test_held_never_flips_to_failed(self):
        """held 是终态：仓储记失败被拒，服务层 _fail_row 也不翻回 failed。"""
        row = self._held_row()
        self.assertFalse(
            self.repo.mark_failed(row["id"], int(time.time()) + 10, "boom")
        )
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )
        outcome = self.service._fail_row(self.repo, row, "boom")
        self.assertEqual(outcome, "held")
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )

    def test_requeue_resets_attempts_and_rebuilds_pending(self):
        """放回 held 行：attempts 清零，缺失的待终态记录按行重建。"""
        row = self._enqueue_tv()
        self.repo.mark_held(row["id"], "待人工复核")
        # 终态失败的旧行记录常已被弹出：先确认记录确实不在。
        self.assertNotIn("pk-tv-09", self.owner._pending)
        before = int(time.time())
        self.assertEqual(self.service.requeue_rows([row["id"]]), 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)
        self.assertEqual(len(self.build_calls), 1)
        call = self.build_calls[0]
        self.assertEqual(call["pending_key"], "pk-tv-09")
        self.assertEqual(call["task_type"], "share")
        self.assertEqual(call["share_url"], "")
        self.assertTrue(call["funnel_route"])
        record = self.owner._pending["pk-tv-09"]
        self.assertEqual(record["result_key"], "f-tv-09")
        self.assertTrue(record["first_seen_at"] >= before)
        self.assertTrue(record["next_check_at"] > record["first_seen_at"])

    def test_requeue_keeps_existing_pending_record(self):
        """记录还在时放回不重建（_build_pending_record 不被调用）。"""
        row = self._enqueue_tv()
        self._pending_item()
        self.repo.mark_held(row["id"], "待人工复核")
        self.assertEqual(self.service.requeue_rows([row["id"]]), 1)
        self.assertEqual(self.build_calls, [])
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "queued"
        )

    def test_claim_row_reenqueues_held_row(self):
        """人工认领：TMDB 直查成功→身份写行→重建记录→放回队列。"""
        row = self._held_row(
            tmdb_id=None, file_id="f-unknown",
            file_name="看不懂的名字.mkv", pending_key="pk-unknown",
        )
        _stub(
            self.service, "_resolve_claim_media",
            lambda tmdb_id, media_type, title: _tv_media(),
        )
        result = self.service.claim_row(
            row["id"], 294498, "tv", season=1, episode=9
        )
        self.assertTrue(result["success"])
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)
        self.assertEqual(fetched["tmdb_id"], 294498)
        self.assertEqual(fetched["media_type"], "tv")
        self.assertEqual((fetched["season"], fetched["episode"]), (1, 9))
        self.assertEqual(fetched["title"], "多喜一家人")
        self.assertEqual(len(self.build_calls), 1)
        self.assertIn("pk-unknown", self.owner._pending)

    def test_claim_row_refuses_bad_media(self):
        """TMDB 查不到：拒绝认领，行仍 held、身份不动。"""
        row = self._held_row(
            tmdb_id=None, file_id="f-unknown",
            file_name="看不懂的名字.mkv", pending_key="pk-unknown",
        )
        _stub(
            self.service, "_resolve_claim_media",
            lambda tmdb_id, media_type, title: None,
        )
        result = self.service.claim_row(
            row["id"], 294498, "tv", season=1, episode=9
        )
        self.assertFalse(result["success"])
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertIsNone(fetched["tmdb_id"])
        self.assertEqual(self.build_calls, [])

    def test_claim_row_refuses_non_held_row(self):
        """只有 held/failed 行能认领：queued 行拒绝。"""
        row = self._enqueue_tv()
        _stub(
            self.service, "_resolve_claim_media",
            lambda tmdb_id, media_type, title: _tv_media(),
        )
        result = self.service.claim_row(
            row["id"], 294498, "tv", season=1, episode=9
        )
        self.assertFalse(result["success"])
        self.assertIn("只有挂起/失败行能认领", result["message"])

    def test_claim_row_tv_requires_episode(self):
        """剧集认领必须指定集号；电影不要求。"""
        row = self._held_row(
            tmdb_id=None, file_id="f-unknown",
            file_name="看不懂的名字.mkv", pending_key="pk-unknown",
        )
        _stub(
            self.service, "_resolve_claim_media",
            lambda tmdb_id, media_type, title: _tv_media(),
        )
        result = self.service.claim_row(row["id"], 294498, "tv", season=1)
        self.assertFalse(result["success"])
        self.assertIn("必须指定集号", result["message"])
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )


# ----------------------------------------------------------------------
# 孤儿 .mkv/STRM 人工清理
# ----------------------------------------------------------------------
class FunnelOrphanCleanupTests(_Stage4DbMixin):
    """孤儿清理：默认不自动删；held 行人工删 .mkv 时连带 .strm。"""

    def _held_row_with_orphan(self, **overrides):
        row = self._enqueue_tv(**overrides)
        orphan = f"{row['cloud_dir']}/{row['file_name']}"
        self.repo.mark_held(
            row["id"], f"落盘后拦截：已在库；orphan_location={orphan}"
        )
        return self.repo.get_by_id(row["id"]), orphan

    def test_orphan_location_parsed_from_reason(self):
        """标记解析：位置取到分号前；无分号取到末尾；无标记为空。"""
        self.assertEqual(
            FunnelService._orphan_location_from_reason(
                "拦截；orphan_location=/库/剧/E09.mkv；其他"
            ),
            "/库/剧/E09.mkv",
        )
        self.assertEqual(
            FunnelService._orphan_location_from_reason(
                "拦截；orphan_location=/库/剧/E09.mkv"
            ),
            "/库/剧/E09.mkv",
        )
        self.assertEqual(
            FunnelService._orphan_location_from_reason("拦截；无位置"),
            "",
        )
        self.assertEqual(
            FunnelService._orphan_location_from_reason(""), ""
        )

    def test_delete_orphan_files_removes_mkv_and_strm(self):
        """人工删孤儿：.mkv 按 file_id 删，匹配的 .strm 一起删，标记清除。"""
        row, orphan = self._held_row_with_orphan()
        deleted_ids = []
        self.owner._cloud_mutations = SimpleNamespace(
            delete_file=lambda file_id: deleted_ids.append(file_id) or True
        )
        strm_path = Path(self._tmp.name) / "orphan.strm"
        strm_path.write_text("https://115.com/xxx", encoding="utf-8")
        self.owner._strm_generator = SimpleNamespace(
            local_path=lambda *a, **k: str(strm_path)
        )
        result = self.service.delete_orphan_files([row["id"]])
        self.assertTrue(result["success"])
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(result["results"][0]["deleted"], orphan)
        self.assertEqual(deleted_ids, ["f-tv-09"])
        self.assertFalse(strm_path.exists())
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertNotIn("orphan_location=", fetched["skip_reason"])

    def test_delete_orphan_files_refuses_non_held_and_missing_location(self):
        """非 held 行拒绝；held 行无孤儿位置拒绝；行不存在记失败。"""
        queued = self._enqueue_tv()
        held, _orphan = self._held_row_with_orphan(
            episode=10, file_id="f-tv-10",
            file_name="多喜一家人 {tmdb-294498} - S01E10 - 第10集.mkv",
            pending_key="pk-tv-10",
        )
        no_location = self._enqueue_tv(
            episode=11, file_id="f-tv-11",
            file_name="多喜一家人 {tmdb-294498} - S01E11 - 第11集.mkv",
            pending_key="pk-tv-11",
        )
        self.repo.mark_held(no_location["id"], "待人工复核")
        self.owner._cloud_mutations = SimpleNamespace(
            delete_file=lambda file_id: True
        )
        result = self.service.delete_orphan_files(
            [queued["id"], held["id"], no_location["id"], 99999]
        )
        by_id = {item["id"]: item for item in result["results"]}
        self.assertFalse(by_id[queued["id"]]["success"])
        self.assertIn("只有挂起行能删孤儿文件", by_id[queued["id"]]["message"])
        # held 行有位置且 mutations 就绪：删成功（不影响本用例的拒绝断言）。
        self.assertTrue(by_id[held["id"]]["success"])
        self.assertFalse(by_id[no_location["id"]]["success"])
        self.assertIn("没有孤儿文件位置", by_id[no_location["id"]]["message"])
        self.assertFalse(by_id[99999]["success"])
        self.assertIn("行不存在", by_id[99999]["message"])

    def test_delete_orphan_files_failure_keeps_marker(self):
        """网盘删失败：返回失败，标记保留、可重试；.strm 不动。"""
        row, orphan = self._held_row_with_orphan()
        self.owner._cloud_mutations = SimpleNamespace(
            delete_file=lambda file_id: False
        )
        strm_path = Path(self._tmp.name) / "orphan.strm"
        strm_path.write_text("https://115.com/xxx", encoding="utf-8")
        self.owner._strm_generator = SimpleNamespace(
            local_path=lambda *a, **k: str(strm_path)
        )
        result = self.service.delete_orphan_files([row["id"]])
        self.assertTrue(result["success"])
        self.assertFalse(result["results"][0]["success"])
        self.assertTrue(strm_path.exists())
        fetched = self.repo.get_by_id(row["id"])
        self.assertIn("orphan_location=", fetched["skip_reason"])

    def test_delete_orphan_files_strm_failure_keeps_marker(self):
        """.mkv 已删但匹配 .strm 删失败：本行记失败，标记保留、可重试。

        v1.30.0 复核补丁：此前 .strm 删失败只记一条警告就清标记，
        残留 .strm 再也删不掉（.mkv 已删，位置无从再得）。
        """
        row, orphan = self._held_row_with_orphan()
        deleted_ids = []
        self.owner._cloud_mutations = SimpleNamespace(
            delete_file=lambda file_id: deleted_ids.append(file_id) or True
        )

        def _raise(*args, **kwargs):
            raise RuntimeError("unlink failed")

        self.owner._strm_generator = SimpleNamespace(local_path=_raise)
        result = self.service.delete_orphan_files([row["id"]])
        self.assertTrue(result["success"])
        self.assertFalse(result["results"][0]["success"])
        self.assertIn("匹配 .strm 未删", result["results"][0]["message"])
        self.assertEqual(deleted_ids, ["f-tv-09"])
        fetched = self.repo.get_by_id(row["id"])
        self.assertIn("orphan_location=", fetched["skip_reason"])


# ----------------------------------------------------------------------
# 行视图：展示原因去标记
# ----------------------------------------------------------------------
class FunnelRowViewOrphanTests(unittest.TestCase):
    """行视图：展示原因里去掉 orphan_location= 机器标记，位置另走字段。

    v1.30.0 复核补丁：标记是给“删孤儿”按钮解析用的，DB 里的原因保留
    标记（解析仍走它），但队列页展示的原因不能带机器文本。
    """

    def test_row_view_strips_orphan_marker_from_reason(self):
        view = FunnelApi._row_view({
            "media_type": "tv", "season": 1, "episode": 9,
            "skip_reason": "落盘后拦截：已在库；orphan_location=/库/剧/E09.mkv",
            "file_name": "E09.mkv",
        })
        self.assertTrue(view["has_orphan"])
        self.assertEqual(view["orphan_location"], "/库/剧/E09.mkv")
        self.assertNotIn("orphan_location=", view["reason"])
        self.assertEqual(view["reason"], "落盘后拦截：已在库")

    def test_row_view_keeps_text_after_marker(self):
        view = FunnelApi._row_view({
            "media_type": "tv", "season": 1, "episode": 9,
            "skip_reason": "拦截；orphan_location=/库/剧/E09.mkv；其他原因",
            "file_name": "E09.mkv",
        })
        self.assertEqual(view["orphan_location"], "/库/剧/E09.mkv")
        self.assertEqual(view["reason"], "拦截；其他原因")

    def test_row_view_reason_without_marker_unchanged(self):
        view = FunnelApi._row_view({
            "media_type": "tv", "season": 1, "episode": 9,
            "skip_reason": "待人工复核",
            "file_name": "E09.mkv",
        })
        self.assertFalse(view["has_orphan"])
        self.assertEqual(view["orphan_location"], "")
        self.assertEqual(view["reason"], "待人工复核")


# ----------------------------------------------------------------------
# 轮次历史
# ----------------------------------------------------------------------
class FunnelRoundHistoryTests(_Stage4DbMixin):
    """轮次历史：每轮记摘要，只保留最近 20 条，新在前。"""

    def _run_happy_path(self, service=None):
        service = service or self.service
        item = self._pending_item()
        row, created = service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.assertTrue(created)
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.postprocess.ctx = _ctx({"pk-tv-09": item})
        return row

    def test_round_history_records_worker_rounds(self):
        """worker 跑完一轮：历史里有一条 kind=worker 的摘要。"""
        self._run_happy_path()
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        history = self.service.round_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["kind"], "worker")
        self.assertEqual(history[0]["processed"], 1)
        self.assertEqual(history[0]["done"], 1)

    def test_round_history_capped_at_20(self):
        """记 25 轮：只留最近 20 条，新在前。"""
        for index in range(25):
            self.service._record_round({
                "kind": "worker", "trigger": "worker",
                "started_at": index, "finished_at": index,
                "processed": index, "done": 0, "failed": 0,
                "held": 0, "skipped": 0,
            })
        history = self.service.round_history()
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0]["processed"], 24)
        self.assertEqual(history[-1]["processed"], 5)


# ----------------------------------------------------------------------
# 崩溃恢复
# ----------------------------------------------------------------------
class FunnelRestartRecoveryTests(_Stage4DbMixin):
    """崩溃恢复（§8 末段）：行在 DB 里，不需要人工重建队列。"""

    def _run_happy_path(self, service=None):
        service = service or self.service
        item = self._pending_item()
        row, created = service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.assertTrue(created)
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.postprocess.ctx = _ctx({"pk-tv-09": item})
        return row

    def test_crash_recovery_processing_row_reclaimed_after_restart(self):
        """处理中途中断：新实例+租约过期→回收重跑→done，终态只提交一次。"""
        row = self._run_happy_path()
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now - 10, now=now))
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "processing"
        )
        resumed = FunnelService(self.owner)
        stats = resumed.run_round()
        self.assertEqual(stats["done"], 1)
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "done"
        )
        second = resumed.run_round()
        self.assertEqual(second["processed"], 0)
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(self.owner.postprocess.process_calls, 1)

    def test_crash_recovery_held_rows_untouched_by_restart(self):
        """重启后：queued 行照常跑完，held 行不动、不自动重试。"""
        queued = self._run_happy_path()
        held = self._enqueue_tv(
            episode=10, file_id="f-tv-10",
            file_name="多喜一家人 {tmdb-294498} - S01E10 - 第10集.mkv",
            pending_key="pk-tv-10",
        )
        self.repo.mark_held(held["id"], "待人工复核")
        resumed = FunnelService(self.owner)
        stats = resumed.run_round()
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["done"], 1)
        self.assertEqual(
            self.repo.get_by_id(queued["id"])["status"], "done"
        )
        self.assertEqual(
            self.repo.get_by_id(held["id"])["status"], "held"
        )


# ----------------------------------------------------------------------
# 失败记录页口径 + API 注册
# ----------------------------------------------------------------------
class FunnelFailurePageTests(_Stage4DbMixin):
    """失败记录页：漏斗行按 finalize_key 批量查到，标“整理漏斗”。"""

    def test_pending_keys_with_rows_bulk_lookup(self):
        """pending_keys_with_rows：只返回存在的键（api_vue_history 的依据）。"""
        self._enqueue_tv()
        self._enqueue_tv(
            episode=10, file_id="f-tv-10",
            file_name="多喜一家人 {tmdb-294498} - S01E10 - 第10集.mkv",
            pending_key="pk-tv-10",
        )
        found = self.repo.pending_keys_with_rows(
            ["pk-tv-09", "pk-tv-10", "pk-missing"]
        )
        self.assertEqual(set(found), {"pk-tv-09", "pk-tv-10"})
        self.assertEqual(found["pk-tv-09"]["file_id"], "f-tv-09")
        self.assertEqual(found["pk-tv-10"]["episode"], 10)
        self.assertEqual(self.repo.pending_keys_with_rows([]), {})

    def test_funnel_api_routes_registered(self):
        """阶段 4 新增的 4 条漏斗路由已在 registration.py 注册。"""
        source = open(
            os.path.join(PLUGIN_DIR, "core", "api", "registration.py"),
            encoding="utf-8",
        ).read()
        for route in (
            "/funnel/rounds", "/funnel/scan",
            "/funnel/claim", "/funnel/delete-orphan",
        ):
            self.assertIn(route, source)
