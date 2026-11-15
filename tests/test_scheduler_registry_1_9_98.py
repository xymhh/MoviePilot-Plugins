"""1.9.98：入口瘦身 + 调度器统一。

- SchedulerRegistry：一个 BackgroundScheduler + 按 id 注册的任务表。
  7 个分散的调度器（主任务、离线监控、残留清理、STRM 全量、STRM 增量、
  历史状态维护、115 整理）全部改走注册表；stop_service 一次停全部，
  包括以前漏停的历史状态维护与 115 整理。
- 任务中心 API 从插件入口搬到 RuntimeApi；残留清理搬到 ResidueService；
  配置归一化抽到 core.config.normalize_plugin_config。
"""

from __future__ import annotations

import inspect
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

# 真实 apscheduler / pytz 先导入：占位包复制公开属性时才能拿到真类，
# 否则插件入口里 BackgroundScheduler / CronTrigger / IntervalTrigger /
# pytz.timezone 全是 Mock。test_runtime_tasks_1_9_93 等文件在收集期就
# 装了 apscheduler 占位包（__path__ 为空，真子模块导不进来），sidebar
# 用例同理装了 pytz 占位；先清掉占位、导真包，后续 install_app_mocks
# 会把真类复制回占位包，不影响其它用例。
for _name in [
    name
    for name in list(sys.modules)
    if name in {"apscheduler", "pytz"}
    or name.startswith(("apscheduler.", "pytz."))
]:
    del sys.modules[_name]
import pytz  # noqa: F401
import apscheduler.schedulers.background  # noqa: F401
import apscheduler.triggers.cron  # noqa: F401
import apscheduler.triggers.date  # noqa: F401
import apscheduler.triggers.interval  # noqa: F401
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)
from test_downloader_plugin_1_9_86 import _load_plugin_module, _plugin


def _load_scheduler_module():
    module = sys.modules.get("cloudsubscribex.core.scheduler")
    if module is not None and hasattr(module, "SchedulerRegistry"):
        return module
    install_app_mocks("app.core", "app.core.config")
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        TZ="Asia/Shanghai", API_TOKEN="x"
    )
    return load_module(
        "cloudsubscribex.core.scheduler",
        "plugins.v2/cloudsubscribex/core/scheduler.py",
    )


def _load_config_module():
    module = sys.modules.get("cloudsubscribex.core.config_normalize")
    if module is not None and hasattr(module, "normalize_plugin_config"):
        return module
    install_app_mocks(
        "app.core", "app.core.config",
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
    )
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        TZ="Asia/Shanghai", API_TOKEN="x"
    )
    return load_module(
        "cloudsubscribex.core.config_normalize",
        "plugins.v2/cloudsubscribex/core/config_normalize.py",
    )


def _load_residue_module():
    module = sys.modules.get("cloudsubscribex.core.services.residue")
    if module is not None and hasattr(module, "ResidueService"):
        return module
    install_app_mocks(
        "app.core", "app.core.config",
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
    )
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        TZ="Asia/Shanghai", API_TOKEN="x"
    )
    return load_module(
        "cloudsubscribex.core.services.residue",
        "plugins.v2/cloudsubscribex/core/services/residue.py",
    )


class _FakeTTLCache(dict):
    """app.core.cache.TTLCache 的最小替身：utils.cache 会继承它。

    注意：不能用 MagicMock 顶替 TTLCache——utils/cache.py 在模块级
    ``class _PlatformTTLCache(TTLCache)``，继承 Mock 会得到一个 Mock 类，
    实例化时触发 StopIteration。
    """

    def __init__(self, region="", maxsize=128, ttl=300, **kwargs):
        super().__init__()
        self.region = region
        self.maxsize = maxsize
        self.ttl = ttl


def _load_runtime_api_module():
    module = sys.modules.get("cloudsubscribex.core.api.runtime")
    if module is not None and hasattr(module, "RuntimeApi"):
        return module
    # 先记下加载前 fastapi 的状态（可能已有 strm_redirect 的替身），
    # 加载完 runtime.py 后恢复，见函数末尾的 finally。
    _fastapi_saved = {
        name: sys.modules.get(name) for name in ("fastapi", "fastapi.responses")
    }
    install_app_mocks(
        "app.core", "app.core.config", "app.core.metainfo", "app.core.cache",
        "app.db", "app.db.site_oper", "app.db.subscribe_oper", "app.helper",
        "app.helper.mediaserver", "app.schemas.types", "app.modules",
        "app.modules.filemanager", "app.utils", "app.utils.http",
        "app.utils.string", "fastapi", "fastapi.responses",
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
    )
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        TZ="Asia/Shanghai", API_TOKEN="x"
    )
    ensure_package("app.core.cache").TTLCache = _FakeTTLCache
    # 其它测试文件在收集期会用 register_module 给 core.media 等子模块注册
    # 只有少量预置属性的占位（例如 channels 用例的 tmdb_id_of）：runtime.py
    # 经 page.py 相对导入 media 的 call_with_supported_kwargs 时会直接
    # ImportError。占位是无 __file__ 的 ModuleType 时，用真实模块替换；
    # MagicMock 占位（属性访问自动生成）不影响 from-import，留着不动。
    for _name, _path, _attrs in (
        (
            "cloudsubscribex.core.media",
            "plugins.v2/cloudsubscribex/core/media.py",
            ("call_with_supported_kwargs", "recognize_media"),
        ),
        (
            "cloudsubscribex.core.sync_pause",
            "plugins.v2/cloudsubscribex/core/sync_pause.py",
            ("snapshot", "set_paused"),
        ),
        (
            "cloudsubscribex.core.task_center",
            "plugins.v2/cloudsubscribex/core/task_center.py",
            ("collect_tasks",),
        ),
        (
            "cloudsubscribex.core.retry_policy",
            "plugins.v2/cloudsubscribex/core/retry_policy.py",
            ("is_dead_letter",),
        ),
    ):
        _existing = sys.modules.get(_name)
        if (
            _existing is not None
            and isinstance(_existing, types.ModuleType)
            and not hasattr(_existing, "__file__")
            and not all(hasattr(_existing, _attr) for _attr in _attrs)
        ):
            load_module(_name, _path)
    # tmdb 用例在收集期给 core.api.page 注册了只有 _platform_image_url
    # 的占位：runtime.py 开头 ``from .page import clear_ui_options_cache``
    # 会直接 ImportError。page.py 真模块依赖很重（config / drive /
    # search / utils.cache 全链路），本用例不走 UI 缓存清理，没必要为
    # 一个导入符号把它整个加载进来；在占位上补一个空实现即可。
    _page = sys.modules.get("cloudsubscribex.core.api.page")
    if (
        _page is not None
        and isinstance(_page, types.ModuleType)
        and not hasattr(_page, "__file__")
        and not hasattr(_page, "clear_ui_options_cache")
    ):
        _page.clear_ui_options_cache = lambda *args, **kwargs: None
    try:
        return load_module(
            "cloudsubscribex.core.api.runtime",
            "plugins.v2/cloudsubscribex/core/api/runtime.py",
        )
    finally:
        # fastapi 占位只在加载 runtime.py 这一刻需要：mp_search.py 在导入时
        # 用 ``try: from fastapi.responses import Response`` 探测 fastapi，
        # 占位留着会让它拿到 Mock Response 而不是最小替身。但直接删掉也不
        # 行——strm_redirect 用例在收集期就装好了自己的 fastapi 替身并在
        # 用例里直接取 sys.modules["fastapi"]。所以恢复加载前已有的状态：
        # 原先有替身就还回去，原先没有才删掉。
        for _name, _saved in _fastapi_saved.items():
            if _saved is None:
                sys.modules.pop(_name, None)
            else:
                sys.modules[_name] = _saved


def _load_sync_runtime_module():
    module = sys.modules.get("cloudsubscribex.core.services.runtime")
    if module is not None and hasattr(module, "SyncRuntimeService"):
        # 已加载的模块绑定的是加载时的 settings 桩（可能来自其它用例文件，
        # TZ 是 Mock）；直接改它绑定的对象，否则 _update_offline_monitor
        # 里 pytz.timezone(settings.TZ) 会拿到 Mock。
        bound_settings = getattr(module, "settings", None)
        if bound_settings is not None:
            bound_settings.TZ = "Asia/Shanghai"
        # 若模块是在其它用例收集期加载的，它绑定的 pytz / IntervalTrigger
        # 可能是占位 Mock（收集期真包还没导进来，timezone / IntervalTrigger
        # 全是 MagicMock）；这时强制重载——当前 sys.modules 里的占位已从
        # 真包复制了属性，重载后绑定的是真类。已跑完的其它用例持有旧模块
        # 对象的引用，不受替换影响。
        pytz_mod = getattr(module, "pytz", None)
        trigger = getattr(module, "IntervalTrigger", None)
        if inspect.isfunction(
            getattr(pytz_mod, "timezone", None)
        ) and isinstance(trigger, type):
            return module
        del sys.modules["cloudsubscribex.core.services.runtime"]
    install_app_mocks(
        "app.core", "app.core.config",
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
    )
    install_core_delegation()
    sys.modules["app.core.config"].settings = SimpleNamespace(
        TZ="Asia/Shanghai", API_TOKEN="x"
    )
    core = ensure_package("cloudsubscribex.core")

    class _Capability:
        OFFLINE_TASKS = "offline_tasks"

    core.CloudDriveCapability = _Capability
    return load_module(
        "cloudsubscribex.core.services.runtime",
        "plugins.v2/cloudsubscribex/core/services/runtime.py",
    )


class SchedulerRegistryTests(unittest.TestCase):
    def setUp(self):
        module = _load_scheduler_module()
        self.Registry = module.SchedulerRegistry
        self.registry = self.Registry(timezone="Asia/Shanghai")
        self.addCleanup(self.registry.shutdown)

    def test_ensure_job_registers_and_starts(self):
        self.assertFalse(self.registry.running)
        self.assertTrue(
            self.registry.ensure_job(
                "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=60)
            )
        )
        self.assertTrue(self.registry.running)
        self.assertEqual(self.registry.job_ids(), ["job_a"])

    def test_ensure_job_replaces_same_id(self):
        self.registry.ensure_job(
            "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=60)
        )
        self.registry.ensure_job(
            "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=30)
        )
        self.assertEqual(self.registry.job_ids(), ["job_a"])
        job = self.registry.get_job("job_a")
        self.assertEqual(job.trigger.interval.total_seconds(), 30)

    def test_remove_job(self):
        self.registry.ensure_job(
            "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=60)
        )
        self.registry.ensure_job(
            "job_b", func=lambda: None, trigger=IntervalTrigger(seconds=60)
        )
        self.assertTrue(self.registry.remove_job("job_a"))
        self.assertEqual(self.registry.job_ids(), ["job_b"])
        self.assertFalse(self.registry.remove_job("job_a"))
        self.assertFalse(self.registry.remove_job("missing"))

    def test_reschedule_job(self):
        self.registry.ensure_job(
            "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=60)
        )
        self.assertTrue(
            self.registry.reschedule_job("job_a", IntervalTrigger(seconds=30))
        )
        job = self.registry.get_job("job_a")
        self.assertEqual(job.trigger.interval.total_seconds(), 30)
        self.assertFalse(
            self.registry.reschedule_job("missing", IntervalTrigger(seconds=30))
        )

    def test_get_job_missing_returns_none(self):
        self.assertIsNone(self.registry.get_job("missing"))

    def test_modify_job_next_run(self):
        import datetime

        # 用真实 DateTrigger 对象而不是字符串 "date"：字符串别名要在
        # add_job 时从 sys.modules 里解析触发器类，其它测试文件会把
        # apscheduler.triggers.date 换成占位，解析到的就是 Mock。
        self.registry.ensure_job(
            "job_a", func=lambda: None,
            trigger=DateTrigger(
                run_date=datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=3600)
            ),
        )
        target = time.time() + 5
        run_time = datetime.datetime.fromtimestamp(
            target, tz=datetime.timezone.utc
        )
        self.assertTrue(self.registry.modify_job_next_run("job_a", run_time))
        job = self.registry.get_job("job_a")
        self.assertAlmostEqual(job.next_run_time.timestamp(), target, delta=1)
        self.assertFalse(
            self.registry.modify_job_next_run("missing", run_time)
        )

    def test_shutdown_idempotent_and_recreates(self):
        self.registry.ensure_job(
            "job_a", func=lambda: None, trigger=IntervalTrigger(seconds=60)
        )
        self.registry.shutdown()
        self.assertFalse(self.registry.running)
        self.assertEqual(self.registry.job_ids(), [])
        self.registry.shutdown()
        self.assertTrue(
            self.registry.ensure_job(
                "job_b", func=lambda: None, trigger=IntervalTrigger(seconds=60)
            )
        )
        self.assertTrue(self.registry.running)
        self.assertEqual(self.registry.job_ids(), ["job_b"])

    def test_job_actually_fires(self):
        fired = threading.Event()
        self.registry.ensure_job(
            "fire", func=fired.set, trigger=IntervalTrigger(seconds=0.2)
        )
        self.assertTrue(fired.wait(3))

    def test_ensure_job_failure_returns_false(self):
        self.assertFalse(
            self.registry.ensure_job("bad", func=lambda: None, trigger=12345)
        )


class NormalizeConfigTests(unittest.TestCase):
    def setUp(self):
        module = _load_config_module()
        self.normalize = module.normalize_plugin_config

    def test_legacy_takeover_booleans_migrate_to_policy(self):
        self.assertEqual(
            self.normalize({"takeover_platform_downloads": True}).get(
                "platform_download_policy"
            ),
            "cloud",
        )
        self.assertEqual(
            self.normalize({"block_platform_downloads": True}).get(
                "platform_download_policy"
            ),
            "block",
        )
        self.assertEqual(
            self.normalize(
                {
                    "block_platform_downloads": False,
                    "takeover_platform_downloads": False,
                }
            ).get("platform_download_policy"),
            "allow",
        )

    def test_existing_policy_kept_and_unknown_falls_back_to_block(self):
        self.assertEqual(
            self.normalize({"platform_download_policy": "allow"}).get(
                "platform_download_policy"
            ),
            "allow",
        )
        self.assertEqual(
            self.normalize({"platform_download_policy": "weird"}).get(
                "platform_download_policy"
            ),
            "block",
        )

    def test_removed_channel_prefix_dropped_case_insensitively(self):
        result = self.normalize(
            {
                "butailing_old": 1,
                "Butailing_Other": 2,
                "cron": "30 2 * * *",
            }
        )
        self.assertNotIn("butailing_old", result)
        self.assertNotIn("Butailing_Other", result)
        self.assertEqual(result["cron"], "30 2 * * *")

    def test_deprecated_keys_removed(self):
        result = self.normalize(
            {
                "platform_download_policy": "block",
                "unblock_start_time": "00:00",
                "unblock_end_time": "23:59",
            }
        )
        self.assertNotIn("unblock_start_time", result)
        self.assertNotIn("unblock_end_time", result)

    def test_input_not_mutated_and_none_is_empty(self):
        source = {"takeover_platform_downloads": True}
        result = self.normalize(source)
        self.assertEqual(result["platform_download_policy"], "cloud")
        self.assertNotIn("platform_download_policy", source)
        self.assertEqual(self.normalize(None), {})


class ResidueServiceTests(unittest.TestCase):
    def setUp(self):
        module = _load_residue_module()
        self.Service = module.ResidueService
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.owner = SimpleNamespace(
            _cross_transfer_download_path=str(self.root),
            _residue_retention_hours=6,
            _residue_disk_free_ratio=0.0,
            get_data_path=lambda: str(self.root),
        )
        self.service = self.Service(self.owner)

    def _make_old(self, name: str, content: bytes = b"x" * 100) -> Path:
        path = self.root / name
        path.write_bytes(content)
        old = time.time() - 7 * 3600
        os.utime(path, (old, old))
        return path

    def test_cleanup_deletes_expired_and_keeps_fresh(self):
        old_bin = self._make_old("cloud-transfer-" + "a" * 32 + ".bin")
        old_mkv = self._make_old("cloud-transfer-" + "b" * 32 + "-movie.mkv")
        old_meta = self._make_old(
            "cloud-transfer-" + "b" * 32 + "-movie.mkv.meta.json"
        )
        fresh = self.root / ("cloud-transfer-" + "c" * 32 + "-fresh.mkv")
        fresh.write_bytes(b"y" * 50)
        other = self.root / "random.txt"
        other.write_text("keep me")
        result = self.service.cleanup_transfer_residue("手动")
        self.assertFalse(old_bin.exists())
        self.assertFalse(old_mkv.exists())
        self.assertFalse(old_meta.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())
        self.assertEqual(result["deleted_files"], 3)
        self.assertEqual(result["freed_bytes"], 300)
        self.assertEqual(result["skipped_recent"], 1)

    def test_residue_roots_include_cache_and_data_dirs(self):
        roots = self.service._residue_roots()
        self.assertIn(str(self.root), roots)


if __name__ == "__main__":
    unittest.main()


class _FakePauseRepo:
    """sync_pause 仓储替身：load/save 按 (namespace, key) 存取。"""

    def __init__(self):
        self._data = {}

    def load(self, namespace, key, default=None):
        return self._data.get((namespace, key), default)

    def save(self, namespace, key, payload):
        self._data[(namespace, key)] = payload


class _FakeRow:
    def __init__(self, job_key, state="queued", last_error="", attempt=0,
                 max_attempts=3):
        self.job_key = job_key
        self.job_id = job_key
        self.target_path = "/转存"
        self.target_name = "测试"
        self.state = state
        self.last_error = last_error
        self.attempt = attempt
        self.max_attempts = max_attempts


class _FakeSyncJobs:
    def __init__(self):
        self.rows = {}

    def get_by_id(self, job_id):
        return self.rows.get(job_id)

    def get(self, job_key):
        return self.rows.get(job_key)

    def pause(self, job_key, reason):
        row = self.rows.get(job_key)
        if row is None or row.state not in ("queued", "retry_wait"):
            return False
        row.state = "paused"
        return True

    def resume(self, job_key, reason):
        row = self.rows.get(job_key)
        if row is None or row.state != "paused":
            return False
        row.state = "queued"
        return True

    def reopen_dead_letter(self, job_key, reason):
        row = self.rows.get(job_key)
        if row is None:
            return False
        row.state = "queued"
        row.attempt = 0
        return True


class _FakeSession:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


class _FakeStore:
    def __init__(self):
        self.sync_jobs = _FakeSyncJobs()
        self.pause_repo = _FakePauseRepo()
        self.repositories = SimpleNamespace(
            sync_jobs=self.sync_jobs, runtime=self.pause_repo
        )
        self.manager = SimpleNamespace(
            session=lambda write=False: _FakeSession(object())
        )
        self.initialized = False

    def initialize(self):
        self.initialized = True


class TaskApiTests(unittest.TestCase):
    """任务中心 API 从插件入口搬到 RuntimeApi 后行为不变。"""

    def setUp(self):
        self.module = _load_runtime_api_module()
        self.store = _FakeStore()
        self.owner = SimpleNamespace(
            _get_data_store=lambda: self.store,
        )
        self.api = self.module.RuntimeApi(self.owner)

    def test_tasks_lists_via_collect_tasks(self):
        original = self.module.collect_tasks
        self.module.collect_tasks = lambda db, state_filter: [
            {"job_key": "k1"}
        ]
        self.addCleanup(lambda: setattr(self.module, "collect_tasks", original))
        result = self.api.api_vue_tasks()
        self.assertTrue(result["success"])
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["data"][0]["job_key"], "k1")
        self.assertFalse(result["paused"]["paused"])

    def test_sync_pause_roundtrip(self):
        self.assertFalse(self.api.api_vue_sync_pause()["data"]["paused"])
        result = self.api.api_vue_set_sync_pause({"paused": True})
        self.assertTrue(result["success"])
        self.assertTrue(result["data"]["paused"])
        self.assertTrue(self.api.api_vue_sync_pause()["data"]["paused"])
        result = self.api.api_vue_set_sync_pause({"paused": False})
        self.assertFalse(result["data"]["paused"])

    def test_set_sync_pause_requires_paused_param(self):
        result = self.api.api_vue_set_sync_pause({})
        self.assertFalse(result["success"])

    def test_pause_and_resume_task(self):
        self.store.sync_jobs.rows["k1"] = _FakeRow("k1", state="queued")
        result = self.api.api_vue_pause_task({"job_key": "k1"})
        self.assertTrue(result["success"])
        self.assertEqual(self.store.sync_jobs.rows["k1"].state, "paused")
        result = self.api.api_vue_pause_task({"job_key": "k1"})
        self.assertFalse(result["success"])
        result = self.api.api_vue_resume_task({"job_key": "k1"})
        self.assertTrue(result["success"])
        self.assertEqual(self.store.sync_jobs.rows["k1"].state, "queued")
        result = self.api.api_vue_resume_task({"job_key": "k1"})
        self.assertFalse(result["success"])

    def test_pause_task_requires_identifier(self):
        result = self.api.api_vue_pause_task({})
        self.assertFalse(result["success"])

    def test_retry_dead_letter(self):
        self.store.sync_jobs.rows["k1"] = _FakeRow(
            "k1", state="failed", last_error="dead_letter: 转存失败",
            attempt=3, max_attempts=3,
        )
        result = self.api.api_vue_retry_task({"job_key": "k1"})
        self.assertTrue(result["success"])
        self.assertEqual(self.store.sync_jobs.rows["k1"].state, "queued")

    def test_retry_non_dead_letter_rejected(self):
        self.store.sync_jobs.rows["k1"] = _FakeRow("k1", state="queued")
        result = self.api.api_vue_retry_task({"job_key": "k1"})
        self.assertFalse(result["success"])
        self.assertIn("死信", result["message"])


class PluginWiringTests(unittest.TestCase):
    """插件入口的 _start_* 全部改走统一注册表，stop_service 一次停全部。"""

    def _enabled_plugin(self):
        plugin = _plugin()
        # 1.9.86 的 settings 桩没有 TZ：注册表构造时读 settings.TZ，先补上。
        # __init__.py 在导入时就绑定了 settings 对象（可能是收集期缓存的
        # 旧桩），必须直接改它绑定的那个对象；在 sys.modules 上改可能改
        # 到另一个 settings，_start_residue_cleanup 里读的仍是旧的 Mock。
        bound_settings = getattr(sys.modules.get("cloudsubscribex"), "settings", None)
        if bound_settings is not None:
            bound_settings.TZ = "Asia/Shanghai"
        sys.modules["app.core.config"].settings.TZ = "Asia/Shanghai"
        plugin._restore_subscribe_search_takeover = lambda: None
        plugin._strm_full_scan_enabled = True
        plugin._strm_generate_enabled = True
        plugin._strm_increment_enabled = True
        plugin._strm_increment_minutes = 10
        plugin._organize_schedule_enabled = True
        plugin._organize_source_path = "/转存"
        plugin._organize_schedule_cron = "0 3 * * *"
        plugin._organize_schedule_auto_execute = False
        plugin._residue_cleanup = True
        plugin._residue_interval_hours = 6
        return plugin

    def _start_all(self, plugin):
        plugin._start_strm_full_scan()
        plugin._start_strm_increment()
        plugin._start_history_state_maintenance()
        plugin._start_organize_schedule()
        plugin._start_residue_cleanup()

    def test_start_methods_register_all_jobs_on_one_registry(self):
        plugin = self._enabled_plugin()
        self._start_all(plugin)
        registry = plugin._scheduler_registry()
        self.addCleanup(registry.shutdown)
        self.assertTrue(registry.running)
        self.assertEqual(
            set(registry.job_ids()),
            {
                "cloudsubscribe_strm_full_scan",
                "cloudsubscribe_strm_increment",
                "cloudsubscribe_history_state_maintenance",
                "cloudsubscribe_organize",
                "cloudsubscribe_residue_cleanup",
                "cloudsubscribe_residue_cleanup_startup",
            },
        )

    def test_stop_service_shuts_down_all_jobs_including_formerly_leaked(self):
        plugin = self._enabled_plugin()
        self._start_all(plugin)
        registry = plugin._scheduler_registry()
        # 1.9.98 之前历史状态维护与 115 整理两个调度器不在 stop_service 里，
        # 停用/卸载后继续跑；现在它们与其它任务共用一个注册表。
        self.assertIn(
            "cloudsubscribe_history_state_maintenance", registry.job_ids()
        )
        self.assertIn("cloudsubscribe_organize", registry.job_ids())
        plugin.stop_service()
        self.assertIsNone(plugin.__dict__.get("_job_registry"))
        self.assertFalse(registry.running)
        self.assertEqual(registry.job_ids(), [])

    def test_disabled_switch_removes_job(self):
        plugin = self._enabled_plugin()
        self._start_all(plugin)
        registry = plugin._scheduler_registry()
        self.addCleanup(registry.shutdown)
        plugin._strm_full_scan_enabled = False
        plugin._start_strm_full_scan()
        self.assertNotIn(
            "cloudsubscribe_strm_full_scan", registry.job_ids()
        )
        self.assertIn(
            "cloudsubscribe_strm_increment", registry.job_ids()
        )


class OfflineMonitorWiringTests(unittest.TestCase):
    """离线监控改走统一注册表：有待处理文件时挂任务，清空时摘掉。"""

    def test_update_offline_monitor_registers_and_removes_job(self):
        module = _load_sync_runtime_module()
        registry_module = _load_scheduler_module()
        registry = registry_module.SchedulerRegistry(timezone="Asia/Shanghai")
        self.addCleanup(registry.shutdown)
        owner = SimpleNamespace(
            _scheduler_registry=lambda: registry,
            _offline_scheduler_lock=threading.Lock(),
            _sync_handler=None,
        )
        service = module.SyncRuntimeService(owner)
        object.__setattr__(
            service, "_refresh_postprocessing_sync_tasks", lambda: None
        )
        object.__setattr__(service, "monitor_offline_tasks", lambda: None)
        service._update_offline_monitor(3)
        self.assertIn("CloudSubscribe_OfflineMonitor", registry.job_ids())
        service._update_offline_monitor(3)
        self.assertEqual(
            [
                job_id
                for job_id in registry.job_ids()
                if job_id == "CloudSubscribe_OfflineMonitor"
            ],
            ["CloudSubscribe_OfflineMonitor"],
        )
        service._update_offline_monitor(0)
        self.assertNotIn("CloudSubscribe_OfflineMonitor", registry.job_ids())
