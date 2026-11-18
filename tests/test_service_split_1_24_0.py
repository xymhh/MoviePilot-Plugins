"""v1.24.0（服务层拆分 + Config/ChannelManager 拆的后端部分）回归测试。

覆盖简报第 1–6 项的后端改动（前端拆分的静态接线由前端同事的测试覆盖）：

1. 离线任务拆分：``SyncRuntimeService`` 的 10 个离线方法已搬到
   ``core/services/offline.py``（``OfflineTaskService``），
   ``RuntimeApi`` 的包装仍能调到新服务的方法；
2. 媒体识别收敛：``core/services/media_resolve.py`` 的两个 helper；
3. 归一化收敛：年月钳制唯一实现 ``clamp_auto_subscribe_dates``、
   两处 ``_tmdb_id_of`` 合一到 ``core.media.tmdb_id_of``、
   hdhaven 待预览判定抽成 ``_is_pending_hdhaven``；
4. 三份 ``_is_cloud_upgrade_subscribe`` 合一到服务层唯一实现；
5. 补导出：``services/__init__.py`` 的 discovery / mp_organize_tracker /
   upload_quota / OfflineTaskService；
6. 钩子安装可观测性：``resolve_interaction_chain_class`` 的兜底扫描
   与安装汇总日志。

加载方式：沿用 test_get_api_1_23_0 的做法——setUpClass 里懒加载真实
模块，加载前对所有动过的 ``cloudsubscribex.*`` 模块与 ``fastapi`` 做快照，
tearDownClass 里恢复 ``sys.modules`` 与父包属性（app.*、pytz、
apscheduler 是全量套件共享的打桩基础设施，不动）。
"""

from __future__ import annotations

import datetime
import os
import sys
import types
import unittest
from threading import Lock, RLock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import (  # noqa: E402
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
    register_module,
    resolve_component,
    set_media_type,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_REAL_MODULES = (
    ("cloudsubscribex.utils.cache", "plugins.v2/cloudsubscribex/utils/cache.py"),
    ("cloudsubscribex.core.sync_pause", "plugins.v2/cloudsubscribex/core/sync_pause.py"),
    ("cloudsubscribex.core.task_center", "plugins.v2/cloudsubscribex/core/task_center.py"),
    ("cloudsubscribex.core.media", "plugins.v2/cloudsubscribex/core/media.py"),
    ("cloudsubscribex.core.config_normalize", "plugins.v2/cloudsubscribex/core/config_normalize.py"),
    ("cloudsubscribex.core.services.subscription", "plugins.v2/cloudsubscribex/core/services/subscription.py"),
    ("cloudsubscribex.core.services.runtime", "plugins.v2/cloudsubscribex/core/services/runtime.py"),
    ("cloudsubscribex.core.services.offline", "plugins.v2/cloudsubscribex/core/services/offline.py"),
    ("cloudsubscribex.core.services.media_resolve", "plugins.v2/cloudsubscribex/core/services/media_resolve.py"),
    ("cloudsubscribex.core.services.platform", "plugins.v2/cloudsubscribex/core/services/platform.py"),
    ("cloudsubscribex.core.config", "plugins.v2/cloudsubscribex/core/config.py"),
    ("cloudsubscribex.core.api.source_filter", "plugins.v2/cloudsubscribex/core/api/source_filter.py"),
    ("cloudsubscribex.core.api.title_parse", "plugins.v2/cloudsubscribex/core/api/title_parse.py"),
    ("cloudsubscribex.core.api.page", "plugins.v2/cloudsubscribex/core/api/page.py"),
    ("cloudsubscribex.core.api.search", "plugins.v2/cloudsubscribex/core/api/search.py"),
    ("cloudsubscribex.core.api.sync", "plugins.v2/cloudsubscribex/core/api/sync.py"),
    ("cloudsubscribex.core.api.runtime", "plugins.v2/cloudsubscribex/core/api/runtime.py"),
    ("cloudsubscribex.handlers.search.service", "plugins.v2/cloudsubscribex/handlers/search/service.py"),
    ("cloudsubscribex.handlers.sync.service", "plugins.v2/cloudsubscribex/handlers/sync/service.py"),
    ("cloudsubscribex.core.hook.subscription", "plugins.v2/cloudsubscribex/core/hook/subscription.py"),
    ("cloudsubscribex.core.hook.message", "plugins.v2/cloudsubscribex/core/hook/message.py"),
)


def _load_package_init(module_name, relative_path):
    """按包 __init__.py 的语义加载（plugin_env.load_module 会把 __package__
    设成父包，相对导入会错一层；这里 __package__ 就是包自身）。"""
    import importlib.util

    path = os.path.join(REPO_ROOT, relative_path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name
    module.__path__ = [os.path.dirname(path)]
    sys.modules[module_name] = module
    parent_name, _, child = module_name.rpartition(".")
    if parent_name and parent_name in sys.modules:
        setattr(sys.modules[parent_name], child, module)
    spec.loader.exec_module(module)
    return module

_MISSING = object()
_LOADED = {}


def _snapshot_modules(names):
    saved_modules = {}
    saved_attrs = {}
    for name in names:
        saved_modules[name] = sys.modules.get(name, _MISSING)
        parent_name, _, child = name.rpartition(".")
        if parent_name and parent_name in sys.modules:
            parent = sys.modules[parent_name]
            saved_attrs[(parent_name, child)] = getattr(parent, child, _MISSING)
    core = sys.modules.get("cloudsubscribex.core")
    if core is not None:
        for attr in ("OwnerDelegator", "get_component", "resolve_component"):
            saved_attrs[("cloudsubscribex.core", attr)] = getattr(core, attr, _MISSING)
    return saved_modules, saved_attrs


def _restore_modules(saved_modules, saved_attrs):
    for name, saved in saved_modules.items():
        if saved is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    for (parent_name, child), saved in saved_attrs.items():
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


def _install_fake_module(name, module):
    """把假模块同时放进 sys.modules 与父包属性，返回恢复函数。

    app.* 都是 AutoMockModule：``from app.chain import interaction`` 会先
    看父包上有没有 interaction 属性，mock 的 __getattr__ 恒返回 MagicMock，
    所以只改 sys.modules 会被父包属性遮住，必须两处都放。用 __dict__
    判断属性是否事先存在（直接 getattr 会触发 mock 自动创建）。
    """
    saved_module = sys.modules.get(name, _MISSING)
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    saved_attr = (
        parent.__dict__.get(child, _MISSING) if parent is not None else _MISSING
    )
    sys.modules[name] = module
    if parent is not None:
        setattr(parent, child, module)

    def _restore():
        if saved_module is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved_module
        if parent is not None:
            if saved_attr is _MISSING:
                try:
                    delattr(parent, child)
                except AttributeError:
                    pass
            else:
                setattr(parent, child, saved_attr)

    return _restore


def _load_real_modules():
    install_app_mocks(
        "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
        "app.core.security", "app.core.cache", "app.core.plugin",
        "app.db", "app.db.subscribe_oper", "app.db.models",
        "app.db.models.subscribe", "app.db.models.mediaserver",
        "app.chain", "app.chain.download", "app.chain.media",
        "app.chain.subscribe", "app.chain.mediaserver", "app.chain.message",
        "app.helper", "app.helper.mediaserver", "app.helper.browser", "app.application",
        "app.application.mediaserver", "app.adapters",
        "app.modules", "app.modules.filemanager",
        "app.modules.filemanager.transhandler",
        "app.utils", "app.utils.http", "app.utils.string", "app.utils.timer",
        "app.schemas", "app.schemas.file",
        "app.agent", "app.agent.tools", "app.agent.tools.base",
        "app.agent.tools.tags",
        "apscheduler", "apscheduler.triggers", "apscheduler.triggers.cron",
        "apscheduler.triggers.combining", "apscheduler.triggers.date",
        "apscheduler.triggers.interval",
        "fastapi", "fastapi.responses", "pytz",
        "cloudsubscribex", "cloudsubscribex.core",
        "cloudsubscribex.core.api", "cloudsubscribex.core.services",
        "cloudsubscribex.core.agent", "cloudsubscribex.core.config",
        "cloudsubscribex.core.cloud", "cloudsubscribex.core.media",
        "cloudsubscribex.core.channels", "cloudsubscribex.core.bypass_guard",
        "cloudsubscribex.core.downloader_view",
        "cloudsubscribex.core.database", "cloudsubscribex.core.database.models",
        "cloudsubscribex.core.subscribe", "cloudsubscribex.core.subscribe.models",
        "cloudsubscribex.core.subscribe.provider",
        "cloudsubscribex.core.subscribe.registry",
        "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
        "cloudsubscribex.handlers", "cloudsubscribex.handlers.search",
        "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.notification",
        "cloudsubscribex.handlers.subscription",
        "cloudsubscribex.search", "cloudsubscribex.search.scanner",
        "cloudsubscribex.search.types", "cloudsubscribex.search.hdhive",
        "cloudsubscribex.search.magnet", "cloudsubscribex.search.matching",
        "cloudsubscribex.utils", "cloudsubscribex.utils.http_client",
        "cloudsubscribex.utils.notify_template",
        "cloudsubscribex.utils.deep_delete_dedupe",
        "cloudsubscribex.utils.deep_delete_match",
    )
    install_core_delegation()
    install_real_ttl_cache()
    set_media_type()
    # bs4 不在 venv 里（mikan 客户端才用，测试不解析 HTML），只给个桩；
    # 其它搜索子模块保持真实加载（与 test_channels_1_9_88 的做法不同，
    # 那里是把整个搜索子模块都 mock 掉）。
    register_module("bs4", BeautifulSoup=MagicMock())
    for name, path in (
        ("cloudsubscribex.core.error_text", "plugins.v2/cloudsubscribex/core/error_text.py"),
        ("cloudsubscribex.core.failure", "plugins.v2/cloudsubscribex/core/failure.py"),
        ("cloudsubscribex.core.state", "plugins.v2/cloudsubscribex/core/state.py"),
    ):
        load_module(name, path)
    for name, path in _REAL_MODULES:
        load_module(name, path)
    # services/__init__.py 必须按包语义加载（见 _load_package_init），
    # 且放在最后：它会把 checkin/discovery/mp_organize_tracker/
    # upload_quota/organize/residue/scoring/skip_stats/sync 等子模块
    # 一并真实加载。
    _load_package_init(
        "cloudsubscribex.core.services",
        "plugins.v2/cloudsubscribex/core/services/__init__.py",
    )


class _FakeOfflineService:
    """115 离线任务能力桩：只返回固定快照并记录调用。"""

    def __init__(self):
        self.calls = []
        self.snapshot = {"tasks": [{"id": "ABC", "name": "x.mkv"}], "refresh_ok": True}

    def get_offline_tasks_snapshot(self, force=False):
        self.calls.append(("snapshot", force))
        return dict(self.snapshot)

    def get_offline_task_list_snapshot(self, force=False):
        self.calls.append(("list_snapshot", force))
        return {"tasks": [], "refresh_ok": True}

    def restart_offline_task(self, task_id):
        self.calls.append(("restart", task_id))

    def delete_offline_task(self, task_id, delete_source_file=False):
        self.calls.append(("delete", task_id, delete_source_file))

    def delete_offline_tasks(self, task_ids, delete_source_file=False):
        self.calls.append(("delete_many", list(task_ids), delete_source_file))
        return len(task_ids)


class _FakeDrive:
    key = "115"
    name = "115网盘"

    def __init__(self, offline=None):
        self._offline = offline

    def supports(self, cap):
        return self._offline is not None

    def require(self, cap):
        return self._offline


class _FakeRegistry:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def modify_job_next_run(self, job_id, when):
        self.calls.append(("modify_next_run", job_id))

    def ensure_job(self, job_id, **kwargs):
        self.calls.append(("ensure_job", job_id, kwargs))
        self.jobs[job_id] = kwargs

    def remove_job(self, job_id):
        self.calls.append(("remove_job", job_id))
        return self.jobs.pop(job_id, None) is not None


class _FakeSyncHandler:
    """只提供离线监控用到的三个方法。"""

    def __init__(self):
        self.groups = []
        self.monitor_calls = []
        self.pending = []

    def get_due_offline_task_groups(self, force=False, pending_keys=None):
        return list(self.groups)

    def get_pending_finalize_tasks(self):
        return list(self.pending)

    def begin_notification_batch(self):
        return False

    def finish_notification_batch(self):
        pass

    def monitor_offline_strm_tasks(self, **kwargs):
        self.monitor_calls.append(kwargs)
        return {"checked": 1, "completed": 1, "failed": 0, "pending": 0}


_COMPONENT_TYPES = ()


class _FakePluginHost:
    """假插件宿主：复现插件的委托链（__getattr__ → resolve_component）。

    运行态（_sync_tasks、锁、调度注册表）按 _apply_plugin_config 的
    reset_runtime 分支铺在实例上，与生产一致。
    """

    def __init__(self, drive=None):
        self._cloud_drive = drive or _FakeDrive()
        self._sync_handler = None
        self._sync_tasks = {}
        self._sync_tasks_lock = RLock()
        self._offline_monitor_lock = Lock()
        self._offline_scheduler_lock = Lock()
        self._registry = _FakeRegistry()
        self._sync_status = "idle"
        self._sync_task_text = "当前没有订阅处理任务"
        self._sync_progress = 0
        self._sync_running = False
        self._sync_run_started_at = 0
        self._sync_last_finished_at = 0
        self._sync_last_elapsed_ms = 0
        self._sync_context = {}

    def _scheduler_registry(self):
        return self._registry

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_fake_components")


def _subscribe(sub_id=1, best_version=True, manual_upgrade=False):
    sub = SimpleNamespace(id=sub_id, best_version=best_version)
    if manual_upgrade:
        sub._manual_upgrade = True
    return sub


class TestServiceSplit1240(unittest.TestCase):
    """v1.24.0 服务层拆分回归测试。"""

    @classmethod
    def setUpClass(cls):
        names = [name for name, _ in _REAL_MODULES]
        names += [
            "cloudsubscribex.core.error_text",
            "cloudsubscribex.core.failure",
            "cloudsubscribex.core.state",
            "fastapi",
        ]
        cls._saved = _snapshot_modules(names)
        # test_baseline_single_source_1_13_1 把 sqlalchemy 注册成 mock 且不
        # 恢复；task_center 真加载要 ``from sqlalchemy.orm import Session``，
        # 必须弹出 mock、让 venv 里的真实 sqlalchemy 导入。tearDown 时恢复。
        cls._sqlalchemy_saved = {
            key: value
            for key, value in sys.modules.items()
            if key == "sqlalchemy" or key.startswith("sqlalchemy.")
        }
        for key in cls._sqlalchemy_saved:
            sys.modules.pop(key, None)
        _load_real_modules()
        global _COMPONENT_TYPES
        _COMPONENT_TYPES = (
            sys.modules["cloudsubscribex.core.api.runtime"].RuntimeApi,
            sys.modules["cloudsubscribex.core.services.offline"].OfflineTaskService,
            sys.modules["cloudsubscribex.core.services.runtime"].SyncRuntimeService,
        )
        _LOADED["subscription"] = sys.modules["cloudsubscribex.core.services.subscription"]
        _LOADED["offline"] = sys.modules["cloudsubscribex.core.services.offline"]
        _LOADED["runtime"] = sys.modules["cloudsubscribex.core.services.runtime"]
        _LOADED["platform"] = sys.modules["cloudsubscribex.core.services.platform"]
        _LOADED["api_runtime"] = sys.modules["cloudsubscribex.core.api.runtime"]
        _LOADED["api_search"] = sys.modules["cloudsubscribex.core.api.search"]
        _LOADED["config_normalize"] = sys.modules["cloudsubscribex.core.config_normalize"]
        _LOADED["config"] = sys.modules["cloudsubscribex.core.config"]
        _LOADED["media"] = sys.modules["cloudsubscribex.core.media"]
        _LOADED["media_resolve"] = sys.modules["cloudsubscribex.core.services.media_resolve"]
        _LOADED["services_pkg"] = sys.modules["cloudsubscribex.core.services"]
        _LOADED["sync_handler"] = sys.modules["cloudsubscribex.handlers.sync.service"]
        _LOADED["search_handler"] = sys.modules["cloudsubscribex.handlers.search.service"]
        _LOADED["hook_subscription"] = sys.modules["cloudsubscribex.core.hook.subscription"]
        _LOADED["hook_message"] = sys.modules["cloudsubscribex.core.hook.message"]
        cls._logger = sys.modules["app.log"].logger

    @classmethod
    def tearDownClass(cls):
        _restore_modules(*cls._saved)
        for key, value in getattr(cls, "_sqlalchemy_saved", {}).items():
            sys.modules[key] = value
        _LOADED.clear()

    # ---------------------------------------------------------- 第 4 项：洗版范围
    def test_upgrade_scope_pure_function_matrix(self):
        fn = _LOADED["subscription"].is_cloud_upgrade_subscribe
        # 开关关闭：任何订阅都不属于洗版范围
        self.assertFalse(fn(False, [], _subscribe()))
        self.assertFalse(fn(False, [], _subscribe(manual_upgrade=True)))
        # 订阅为空 / 未启用 best_version
        self.assertFalse(fn(True, [], None))
        self.assertFalse(fn(True, [], _subscribe(best_version=False)))
        # 手动洗版快捷方式（默认认）：best_version 为 False 也返回 True
        self.assertTrue(fn(True, [], _subscribe(best_version=False, manual_upgrade=True)))
        # 不认快捷方式时：best_version 仍是必要条件
        self.assertFalse(
            fn(
                True, [], _subscribe(best_version=False, manual_upgrade=True),
                respect_manual_upgrade=False,
            )
        )
        # 指定范围：空列表 = 全部；在列表内 / 不在列表内
        self.assertTrue(fn(True, [], _subscribe(sub_id=7)))
        self.assertTrue(fn(True, [7, 8], _subscribe(sub_id=8)))
        self.assertFalse(fn(True, [7, 8], _subscribe(sub_id=9)))
        # id 集合也接受（处理器传预建的 set）
        self.assertTrue(fn(True, {"7"}, _subscribe(sub_id=7)))

    def test_upgrade_scope_service_adapter_keeps_no_shortcut(self):
        service_cls = _LOADED["subscription"].SubscriptionControlService
        owner = SimpleNamespace(_enable_cloud_upgrade=True, _upgrade_subscribe_ids=[7])
        service = service_cls(owner)
        # 服务版不认手动快捷方式（api/sync.py 的 _manual_upgrade_for_subscribe
        # 要求 best_version，见 is_cloud_upgrade_subscribe 的参数说明）
        self.assertFalse(
            service._is_cloud_upgrade_subscribe(
                _subscribe(sub_id=7, best_version=False, manual_upgrade=True)
            )
        )
        self.assertTrue(service._is_cloud_upgrade_subscribe(_subscribe(sub_id=7)))
        self.assertFalse(service._is_cloud_upgrade_subscribe(_subscribe(sub_id=9)))

    def test_upgrade_scope_handler_adapters_keep_shortcut(self):
        sync_handler_cls = _LOADED["sync_handler"].SyncHandler
        search_handler_cls = _LOADED["search_handler"].SearchHandler
        manual = _subscribe(sub_id=7, best_version=False, manual_upgrade=True)
        for cls in (sync_handler_cls, search_handler_cls):
            fake_self = SimpleNamespace(
                _enable_cloud_upgrade=True,
                _upgrade_subscribe_id_set={"7"},
            )
            bound = cls._is_cloud_upgrade_subscribe.__get__(fake_self, cls)
            self.assertTrue(bound(manual), cls.__name__)
            self.assertTrue(bound(_subscribe(sub_id=7)), cls.__name__)
            self.assertFalse(bound(_subscribe(sub_id=9)), cls.__name__)
            self.assertFalse(
                bound(_subscribe(sub_id=7, best_version=False)), cls.__name__
            )

    # ------------------------------------------------- 第 3 项：归一化收敛
    def test_clamp_auto_subscribe_dates(self):
        clamp = _LOADED["config_normalize"].clamp_auto_subscribe_dates
        now = datetime.datetime.now()
        config = {
            "auto_subscribe_douban_min_year": 0,
            "auto_subscribe_tmdb_min_year": -5,
            "auto_subscribe_anilist_min_year": "",
            "auto_subscribe_mikan_year": "abc",
            "auto_subscribe_maoyan_min_year": 2020,
            "auto_subscribe_douban_min_month": 0,
            "auto_subscribe_tmdb_min_month": 13,
            "auto_subscribe_mikan_min_month": "x",
            "auto_subscribe_bangumi_min_month": 6,
        }
        result = clamp(config)
        self.assertIs(result, config)
        # 0 / 负数 / 空 / 非数字年份都钳制到当前年（旧实现漏了 anilist 键与负数）
        for key in (
            "auto_subscribe_douban_min_year",
            "auto_subscribe_tmdb_min_year",
            "auto_subscribe_anilist_min_year",
            "auto_subscribe_mikan_year",
        ):
            self.assertEqual(config[key], now.year, key)
        # 合法值原样保留
        self.assertEqual(config["auto_subscribe_maoyan_min_year"], 2020)
        # 月份：0 / 13 / 非数字 → 当前月，合法值保留
        for key in (
            "auto_subscribe_douban_min_month",
            "auto_subscribe_tmdb_min_month",
            "auto_subscribe_mikan_min_month",
        ):
            self.assertEqual(config[key], now.month, key)
        self.assertEqual(config["auto_subscribe_bangumi_min_month"], 6)

    def test_normalize_config_and_dates_share_clamp(self):
        ui_config = _LOADED["config"].UIConfig
        now = datetime.datetime.now()
        payload = {
            "auto_subscribe_anilist_min_year": 0,
            "auto_subscribe_tmdb_min_year": -3,
            "auto_subscribe_douban_min_month": 99,
        }
        ui_config.normalize_config(dict(payload))
        normalized = ui_config.normalize_config(dict(payload))
        self.assertEqual(normalized["auto_subscribe_anilist_min_year"], now.year)
        self.assertEqual(normalized["auto_subscribe_tmdb_min_year"], now.year)
        self.assertEqual(normalized["auto_subscribe_douban_min_month"], now.month)
        dates = dict(payload)
        ui_config.normalize_auto_subscribe_dates(dates)
        self.assertEqual(dates["auto_subscribe_anilist_min_year"], now.year)
        self.assertEqual(dates["auto_subscribe_tmdb_min_year"], now.year)
        self.assertEqual(dates["auto_subscribe_douban_min_month"], now.month)

    def test_tmdb_id_of_converged(self):
        tmdb_id_of = _LOADED["media"].tmdb_id_of
        self.assertIsNone(tmdb_id_of(None))
        self.assertEqual(tmdb_id_of({"tmdbid": 223911}), 223911)
        self.assertEqual(tmdb_id_of({"tmdb_id": "123"}), 123)
        self.assertIsNone(tmdb_id_of({"tmdb_id": 0}))
        self.assertIsNone(tmdb_id_of({"tmdb_id": "abc"}))
        self.assertEqual(tmdb_id_of(SimpleNamespace(tmdbid=42)), 42)
        # 两处旧静态方法已删：类上不再定义 _tmdb_id_of，调用点改调 core.media
        self.assertNotIn(
            "_tmdb_id_of", vars(_LOADED["hook_message"].MessageRoutingHook)
        )
        self.assertNotIn(
            "_tmdb_id_of", vars(_LOADED["hook_subscription"].SubscriptionSearchHook)
        )
        message_src = open(
            os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core/hook/message.py"),
            encoding="utf-8",
        ).read()
        subscription_src = open(
            os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core/hook/subscription.py"),
            encoding="utf-8",
        ).read()
        for src in (message_src, subscription_src):
            self.assertNotIn("def _tmdb_id_of", src)
            self.assertIn("from ..media import tmdb_id_of", src)

    def test_hdhaven_pending_predicate(self):
        search_api_cls = _LOADED["api_search"].SearchApi
        is_pending = search_api_cls._is_pending_hdhaven
        self.assertTrue(is_pending("hdhaven", "some-slug", ""))
        self.assertFalse(is_pending("hdhaven", "some-slug", "https://x.example/1"))
        self.assertFalse(is_pending("hdhaven", "", ""))
        self.assertFalse(is_pending("hdhive", "some-ref", ""))
        self.assertFalse(is_pending("pansou", "some-ref", ""))

    # ------------------------------------------------- 第 1 项：离线任务拆分
    def test_offline_methods_moved(self):
        runtime_cls = _LOADED["runtime"].SyncRuntimeService
        offline_cls = _LOADED["offline"].OfflineTaskService
        moved = (
            "_monitor_offline_task_groups",
            "_run_offline_monitor",
            "api_offline_tasks",
            "_merge_pending_finalize_tasks",
            "api_retry_offline_tasks",
            "api_delete_offline_task",
            "api_delete_offline_tasks",
            "_update_offline_monitor",
            "monitor_offline_tasks",
            "_offline_task_service",
        )
        for name in moved:
            self.assertTrue(hasattr(offline_cls, name), name)
            self.assertFalse(hasattr(runtime_cls, name), name)
        # _run_subscription_group 仍在运行态服务里（它操作运行态任务表，
        # 硬搬到 sync.py 只会把运行态逻辑拆散，见汇报）
        self.assertTrue(hasattr(runtime_cls, "_run_subscription_group"))

    def test_offline_runtime_api_delegation(self):
        offline_service = _FakeOfflineService()
        host = _FakePluginHost(drive=_FakeDrive(offline=offline_service))
        runtime_api = _LOADED["api_runtime"].RuntimeApi(host)
        result = runtime_api.api_vue_offline_tasks()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["tasks"][0]["id"], "ABC")
        self.assertEqual(result["data"]["provider"], "115")
        self.assertEqual(result["data"]["pending_count"], 0)
        self.assertEqual(offline_service.calls[0], ("snapshot", False))
        # 刷新路径只是强制重拉快照（宿主无 sync_handler 时不带 monitor 键）
        result = runtime_api.api_vue_refresh_offline_tasks()
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "已手动刷新离线任务")
        self.assertEqual(result["data"]["provider"], "115")
        self.assertNotIn("monitor", result["data"])
        self.assertEqual(offline_service.calls[1], ("snapshot", True))

    def test_offline_no_capability(self):
        host = _FakePluginHost(drive=_FakeDrive(offline=None))
        runtime_api = _LOADED["api_runtime"].RuntimeApi(host)
        result = runtime_api.api_vue_offline_tasks()
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "当前网盘不支持离线任务")

    def test_offline_monitor_no_handler_returns_zeros(self):
        host = _FakePluginHost()
        service = _LOADED["offline"].OfflineTaskService(host)
        self.assertEqual(
            service.monitor_offline_tasks(),
            {"checked": 0, "completed": 0, "failed": 0, "pending": 0},
        )

    def test_offline_monitor_single_group(self):
        handler = _FakeSyncHandler()
        handler.groups = [{"pending_keys": {"k1"}, "needs_offline": False}]
        host = _FakePluginHost()
        host._sync_handler = handler
        service = _LOADED["offline"].OfflineTaskService(host)
        result = service._run_offline_monitor(force=True)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(handler.monitor_calls[0]["pending_keys"], {"k1"})

    def test_offline_update_monitor_start_stop(self):
        host = _FakePluginHost()
        service = _LOADED["offline"].OfflineTaskService(host)
        offline_module = _LOADED["offline"]
        # _update_offline_monitor 用真实时区算 next_run_time：pytz 与
        # settings 都是 mock（timezone() 返回 MagicMock 会让 datetime
        # 报错）；测试里把 timezone 换成返回真实 tzinfo 的桩。
        with (
            patch.object(
                offline_module,
                "pytz",
                SimpleNamespace(
                    timezone=lambda name: datetime.timezone.utc
                ),
            ),
            patch.object(
                offline_module, "settings", SimpleNamespace(TZ="UTC")
            ),
        ):
            service._update_offline_monitor(2)
            self.assertEqual(
                [call[0] for call in host._registry.calls], ["ensure_job"]
            )
            _, _, kwargs = host._registry.calls[0]
            self.assertEqual(kwargs["func"], service.monitor_offline_tasks)
            host._registry.calls.clear()
            service._update_offline_monitor(0)
            self.assertEqual(
                [call[0] for call in host._registry.calls], ["remove_job"]
            )

    # ------------------------------------------- 第 2 项：平台拆分与媒体识别
    def test_platform_split_wiring(self):
        platform_module = _LOADED["platform"]
        shell = platform_module.PlatformIntegrationService
        overview = sys.modules["cloudsubscribex.core.services.platform_overview"]
        agent = sys.modules["cloudsubscribex.core.services.platform_agent"]
        links = sys.modules["cloudsubscribex.core.services.platform_links"]
        for name in (
            "get_platform_overview", "clear_platform_cache",
            "get_runtime_performance", "api_platform_overview",
        ):
            self.assertTrue(hasattr(overview.PlatformOverviewMixin, name), name)
            self.assertTrue(hasattr(shell, name), name)
        for name in (
            "search_platform_resources", "select_platform_resources",
            "_recognize_subscribe_media", "_recognize_agent_media",
            "_candidate_for_tmdb_id",
        ):
            self.assertTrue(hasattr(agent.PlatformAgentMixin, name), name)
            self.assertTrue(hasattr(shell, name), name)
        for name in (
            "api_vue_resolve_manual_links", "submit_platform_links",
            "workflow_start_sync", "workflow_process_links",
        ):
            self.assertTrue(hasattr(links.PlatformLinkMixin, name), name)
            self.assertTrue(hasattr(shell, name), name)
        # 跨模块共用的 extract_resource_links 仍在壳上且是静态方法
        self.assertTrue(isinstance(
            vars(shell)["extract_resource_links"], staticmethod
        ))
        self.assertEqual(
            shell.extract_resource_links("看看 https://115.com/s/abc123 吧"),
            ["https://115.com/s/abc123"],
        )

    def test_media_resolve_helpers(self):
        media_resolve = _LOADED["media_resolve"]

        class _FakeMeta:
            def __init__(self, title):
                self.title = title
                self.type = None
                self.year = None
                self.begin_season = None
                self.begin_episode = None

        with patch.object(media_resolve, "MetaInfo", _FakeMeta):
            meta = media_resolve.build_recognition_meta("仙逆", "tv", year=2023, season=2)
            self.assertEqual(meta.type.value, "tv")
            self.assertEqual(meta.year, 2023)
            self.assertEqual(meta.begin_season, 2)
            # media_type 为空时不写 type（agent 路径原来如此）
            meta = media_resolve.build_recognition_meta("仙逆")
            self.assertIsNone(meta.type)
            # year 显式传 None 也赋值（订阅路径原来就是 meta.year = None）
            meta = media_resolve.build_recognition_meta("仙逆", "movie", year=None)
            self.assertIsNone(meta.year)
        # recognize_once：有识别器走识别器，没有走直调
        calls = []
        recognizer = lambda key, **kwargs: calls.append(("once", key)) or "hit"
        self.assertEqual(
            media_resolve.recognize_once(recognizer, None, ("k",), meta=None),
            "hit",
        )
        self.assertEqual(calls, [(("once", ("k",)))])
        direct = MagicMock(return_value="direct-hit")
        with patch.object(media_resolve, "recognize_media", direct):
            self.assertEqual(
                media_resolve.recognize_once(None, "chain", None, meta=None),
                "direct-hit",
            )
        direct.assert_called_once_with("chain", meta=None)

    # ---------------------------------------------------------- 第 5 项：导出
    def test_service_exports(self):
        services = _LOADED["services_pkg"]
        for name in (
            "Candidate", "SubmitResult", "submit_candidate",
            "MpOrganizeTracker", "UploadQuota", "clamp_limit_gb",
            "OfflineTaskService",
        ):
            self.assertTrue(hasattr(services, name), name)
            self.assertIn(name, services.__all__, name)
        self.assertIs(
            services.Candidate,
            sys.modules["cloudsubscribex.core.services.discovery"].Candidate,
        )
        self.assertIs(
            services.MpOrganizeTracker,
            sys.modules["cloudsubscribex.core.services.mp_organize_tracker"].MpOrganizeTracker,
        )
        self.assertIs(
            services.UploadQuota,
            sys.modules["cloudsubscribex.core.services.upload_quota"].UploadQuota,
        )
        # 核心门面不导出业务服务（core/__init__.py 的分层是能力契约/
        # 搜索/网盘/传输，服务层对象从 services/__init__ 拿；且 core
        # 反向引用 services 会成环）
        core_src = open(
            os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/core/__init__.py"),
            encoding="utf-8",
        ).read()
        self.assertNotIn("UploadQuota", core_src)
        self.assertNotIn("MpOrganizeTracker", core_src)
        self.assertNotIn("from .services", core_src)

    # ------------------------------------------------- 第 6 项：钩子可观测性
    def test_hook_resolve_fallback_scan(self):
        hook = _LOADED["hook_subscription"]

        def _interaction_module(classes):
            module = types.ModuleType("app.chain.interaction")
            for name, cls in classes.items():
                setattr(module, name, cls)
            return module

        class _LegacyChain:
            @staticmethod
            def _search_media_resources():
                return []

        class _NewChain:
            pass

        restore = _install_fake_module(
            "app.chain.interaction",
            _interaction_module({
                "MediaInteractionChain": _NewChain,
                "LegacyChain": _LegacyChain,
            }),
        )
        try:
            target, reason = hook.resolve_interaction_chain_class()
            self.assertIs(target, _LegacyChain)
            self.assertIn("按方法扫描命中", reason)
        finally:
            restore()

    def test_hook_resolve_named_class_and_not_found(self):
        hook = _LOADED["hook_subscription"]

        class _InteractionChain:
            @staticmethod
            def _search_media_resources():
                return []

        module = types.ModuleType("app.chain.interaction")
        module.InteractionChain = _InteractionChain
        restore = _install_fake_module("app.chain.interaction", module)
        try:
            target, reason = hook.resolve_interaction_chain_class()
            self.assertIs(target, _InteractionChain)
            self.assertEqual(reason, "InteractionChain")
        finally:
            restore()

        module2 = types.ModuleType("app.chain.interaction")

        class _Empty:
            pass

        module2.SomeChain = _Empty
        restore = _install_fake_module("app.chain.interaction", module2)
        try:
            target, reason = hook.resolve_interaction_chain_class()
            self.assertIsNone(target)
            self.assertIn("_search_media_resources", reason)
        finally:
            restore()

    def test_hook_install_summary_log(self):
        hook_module = _LOADED["hook_subscription"]
        self._logger.reset_mock()

        def _chain_class(methods, name="FakeChain"):
            namespace = {}
            for name_ in methods:
                namespace[name_] = staticmethod(lambda *a, **k: None)
            return type(name, (), namespace)

        search_chain = _chain_class(tuple(
            hook_module.SubscriptionSearchHook._PLATFORM_SEARCH_METHODS
        ))
        subscribe_chain = _chain_class(
            ("_process_search_subscription", "add", "async_add")
        )
        interaction_chain = _chain_class(
            ("_search_media_resources",), name="MediaInteractionChain"
        )

        def _module(name, **attrs):
            module = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            return module

        restores = []
        try:
            restores.append(_install_fake_module(
                "app.chain.search",
                _module("app.chain.search", SearchChain=search_chain),
            ))
            restores.append(_install_fake_module(
                "app.chain.subscribe",
                _module("app.chain.subscribe", SubscribeChain=subscribe_chain),
            ))
            restores.append(_install_fake_module(
                "app.chain.interaction",
                _module(
                    "app.chain.interaction",
                    MediaInteractionChain=interaction_chain,
                ),
            ))

            class _Scheduler:
                _instance = None

                @classmethod
                def get_existing_instance(cls):
                    return cls._instance

                def get_job(self, job_id):
                    return self._jobs.get(job_id)

            jobs = {
                job_id: {"func": lambda: None}
                for job_id in hook_module.SubscriptionSearchHook._JOB_IDS
            }
            scheduler = _Scheduler()
            scheduler._jobs = jobs
            _Scheduler._instance = scheduler
            restores.append(_install_fake_module(
                "app.scheduler", _module("app.scheduler", Scheduler=_Scheduler)
            ))

            # *_originals 必须是真 dict：安装器用 .get() 判“是否已装”，
            # MagicMock 的 .get() 恒返回 MagicMock，会让安装被跳过。
            owner = SimpleNamespace(
                _enabled=True,
                _platform_search_originals={},
                _subscribe_search_originals={},
                _interaction_originals={},
                _subscribe_chain_originals={},
            )
            hook = hook_module.SubscriptionSearchHook(owner)
            # _install_subscribe_add_expansion 用模块级全局 SubscribeChain
            #（加载时绑了 mock），其余安装器每次都重新 import；测试里把
            # 全局也换成假类，四处才一致。
            with patch.object(hook_module, "SubscribeChain", subscribe_chain):
                hook._install_subscribe_search_takeover()
        finally:
            for restore in reversed(restores):
                restore()

        info_calls = [
            str(call.args[0])
            for call in self._logger.info.call_args_list
            if call.args
        ]
        summary = [text for text in info_calls if "钩子安装汇总" in text]
        self.assertEqual(len(summary), 1)
        self.assertIn("SearchChain.search_by_id", summary[0])
        self.assertIn("SubscribeChain.add", summary[0])
        self.assertIn("MediaInteractionChain._search_media_resources", summary[0])
        self.assertIn("Scheduler.subscribe_search", summary[0])
        self.assertIn("缺失 0 个", summary[0])
