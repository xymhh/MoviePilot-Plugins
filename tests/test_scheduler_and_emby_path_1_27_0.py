"""v1.27.0：定时任务可观测性 + Emby 按路径发现新文件。

关键路径都用真实类跑（不手写假 Handler）：

- 真实 ``SchedulerRegistry``：登记一个会抛异常的任务，真跑一次，断言插件
  logger 收到带任务 id 与堆栈的 ERROR，且异常照样抛回 APScheduler；
- 真实插件类：``_run_scheduled_task`` 把异常 / ``internal_error`` 交给 MP，
  ``_require_sync_handler`` 区分“未就绪”与“缺方法”，``_maintenance_services``
  按开关声明 MP 服务；
- 真实 ``EmbyPathUpdater``：并发请求合成一批、404 只回退一次、路径不在
  Emby 媒体库下不发请求；
- 真实 ``MediaServerNotifier`` / ``MediaRefreshMixin``：Media/Updated 接受时
  不再按 TMDB 查条目刷新；接口 404 时回退原逻辑。
"""

from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# 与 test_scheduler_registry_1_9_98 同款：先清掉别的用例装的 apscheduler
# 占位包、导入真包（install_app_mocks 之后会把真类复制回占位包）。
for _name in [
    name for name in list(sys.modules)
    if name in {"apscheduler", "pytz"}
    or name.startswith(("apscheduler.", "pytz."))
]:
    del sys.modules[_name]
import pytz  # noqa: E402
import apscheduler.schedulers.background  # noqa: E402,F401
import apscheduler.triggers.cron  # noqa: E402,F401
import apscheduler.triggers.date  # noqa: E402,F401
import apscheduler.triggers.interval  # noqa: E402,F401

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import install_app_mocks, load_module  # noqa: E402

EMBY_UPDATE_NAME = "cloudsubscribex.handlers.notification.emby_path_update"
MEDIA_SERVER_NAME = "cloudsubscribex.handlers.notification.media_server"
MEDIA_REFRESH_NAME = "cloudsubscribex.handlers.sync.media_refresh"
SCHEDULER_NAME = "cloudsubscribex.core.scheduler"


class _ModuleSandbox:
    """按需加载真实模块，测试类结束时把 sys.modules 原样还回去。"""

    def __init__(self):
        self._saved = {}

    def load(self, name, path):
        if name not in self._saved:
            self._saved[name] = sys.modules.get(name)
        return load_module(name, path)

    def restore(self):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        self._saved.clear()


def _emby_update_module(sandbox):
    return sandbox.load(
        EMBY_UPDATE_NAME,
        "plugins.v2/cloudsubscribex/handlers/notification/emby_path_update.py",
    )


# ---------------------------------------------------------------- 第 2 项


class ObservedSchedulerRegistryTests(unittest.TestCase):
    """真实 SchedulerRegistry：异常进插件日志并原样抛回。"""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = _ModuleSandbox()
        install_app_mocks("app.core", "app.core.config")
        sys.modules["app.core.config"].settings = SimpleNamespace(TZ="Asia/Shanghai")
        cls.module = cls.sandbox.load(SCHEDULER_NAME, "plugins.v2/cloudsubscribex/core/scheduler.py")

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.restore()

    def setUp(self):
        self.logger = MagicMock()
        patcher = patch.dict(self.module.observed_job.__globals__, {"logger": self.logger})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.registry = self.module.SchedulerRegistry(timezone="Asia/Shanghai")
        self.addCleanup(self.registry.shutdown)

    def test_failing_job_logs_error_with_job_id_and_traceback(self):
        ran = threading.Event()

        def boom():
            ran.set()
            raise RuntimeError("监控取分组失败")

        import datetime

        self.assertTrue(self.registry.ensure_job(
            "CloudSubscribe_OfflineMonitor",
            func=boom,
            trigger="date",
            run_date=datetime.datetime.now(tz=pytz.timezone("Asia/Shanghai"))
            + datetime.timedelta(milliseconds=100),
        ))
        self.assertTrue(ran.wait(5), "任务没有被真调度器执行")
        deadline = time.time() + 3
        while not self.logger.error.called and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.logger.error.called, "异常没有写进插件日志")
        message = self.logger.error.call_args[0][0]
        self.assertIn("CloudSubscribe_OfflineMonitor", message)
        self.assertIn("监控取分组失败", message)
        self.assertIn("Traceback", message)

    def test_wrapper_reraises_and_keeps_return_value(self):
        wrapped = self.module.observed_job("job_x", lambda value: value * 2)
        self.assertEqual(wrapped(21), 42)
        failing = self.module.observed_job("job_y", lambda: 1 / 0)
        with self.assertRaises(ZeroDivisionError):
            failing()
        self.assertIn("job_y", self.logger.error.call_args[0][0])

    def test_registered_job_func_is_the_observed_wrapper(self):
        def original():
            return None

        self.registry.ensure_job("job_a", func=original, trigger="interval", seconds=3600)
        job = self.registry.get_job("job_a")
        self.assertEqual(getattr(job.func, "__observed_job__", None), "job_a")
        self.assertIs(job.func.__wrapped__, original)


def _real_maintenance_services(owner):
    """从 registration.py 取出真实的 ``_maintenance_services`` 源码执行。

    插件本体在测试里把 core.api 整个打桩，直接调拿不到真方法；这里只抽出
    这一个函数，注入真实的 CronTrigger / IntervalTrigger，以插件实例为 self 调用。
    """
    import ast
    import textwrap

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    def _cron_is_valid(cron):
        try:
            CronTrigger.from_crontab(cron)
            return True
        except Exception:
            return False

    owner._cron_is_valid = _cron_is_valid
    root = Path(__file__).resolve().parents[1] / "plugins.v2" / "cloudsubscribex"
    source = (root / "core" / "api" / "registration.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    func_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_maintenance_services":
            func_src = textwrap.dedent(ast.get_source_segment(source, node))
            break
    assert func_src, "registration.py 里找不到 _maintenance_services"
    namespace = {
        "CronTrigger": CronTrigger,
        "IntervalTrigger": IntervalTrigger,
        "logger": MagicMock(),
        "Any": object, "Dict": dict, "List": list, "Optional": object,
    }
    exec(compile(func_src, "registration._maintenance_services", "exec"), namespace)
    return namespace["_maintenance_services"](owner)


class PluginScheduledTaskTests(unittest.TestCase):
    """真实插件类上的 MP 调度入口、同步服务守卫与服务声明。"""

    def _plugin(self):
        from test_downloader_plugin_1_9_86 import _plugin

        plugin = _plugin()
        self.module = sys.modules[type(plugin).__module__]
        self.logger = MagicMock()
        func = type(plugin)._run_scheduled_task
        patcher = patch.dict(func.__globals__, {"logger": self.logger})
        patcher.start()
        self.addCleanup(patcher.stop)
        return plugin

    def test_unknown_task_raises(self):
        plugin = self._plugin()
        with self.assertRaises(ValueError):
            plugin._run_scheduled_task("nope")

    def test_runner_exception_logged_then_raised_to_mp(self):
        plugin = self._plugin()

        def broken():
            raise RuntimeError("purge 崩了")

        plugin._run_retry_index_maintenance = broken
        with self.assertRaises(RuntimeError):
            plugin._run_scheduled_task("retry_index_maintenance")
        message = self.logger.error.call_args[0][0]
        self.assertIn("retry_index_maintenance", message)
        self.assertIn("purge 崩了", message)

    def test_internal_error_result_becomes_exception(self):
        plugin = self._plugin()
        plugin._sync_handler = SimpleNamespace()  # 在，但一个方法都没有
        with self.assertRaises(RuntimeError) as ctx:
            plugin._run_scheduled_task("strm_increment")
        self.assertIn("rebuild_strm_files", str(ctx.exception))
        self.assertIn("rebuild_strm_increment_by_tree", str(ctx.exception))

    def test_require_sync_handler_not_ready_keeps_old_message(self):
        plugin = self._plugin()
        plugin._sync_handler = None
        handler, result = plugin._require_sync_handler("rebuild_strm_files")
        self.assertIsNone(handler)
        self.assertEqual(result, {"success": False, "message": "同步服务未就绪"})
        self.assertFalse(self.logger.error.called)

    def test_require_sync_handler_missing_method_is_loud(self):
        plugin = self._plugin()
        plugin._sync_handler = SimpleNamespace(generate_share_strm=lambda *a, **k: {})
        handler, result = plugin._require_sync_handler("clean_share_strm")
        self.assertIsNone(handler)
        self.assertTrue(result["internal_error"])
        self.assertIn("clean_share_strm", result["message"])
        self.assertTrue(self.logger.error.called)
        # 入口直接把明确报错回给前端，不再是“同步服务未就绪”
        api_result = plugin.clean_share_strm("https://115.com/s/abc?password=x")
        self.assertNotEqual(api_result["message"], "同步服务未就绪")
        self.assertTrue(api_result.get("internal_error"))

    def test_require_sync_handler_ok(self):
        plugin = self._plugin()
        real = SimpleNamespace(refresh_media_servers=lambda: {"Emby": "ok"})
        plugin._sync_handler = real
        handler, result = plugin._require_sync_handler("refresh_media_servers")
        self.assertIs(handler, real)
        self.assertIsNone(result)
        self.assertTrue(plugin.refresh_media_servers()["success"])

    def _maintenance(self, plugin, **flags):
        values = {
            "_strm_generate_enabled": True,
            "_strm_full_scan_enabled": True,
            "_strm_increment_enabled": True,
            "_strm_increment_minutes": 7,
            "_organize_schedule_enabled": True,
            "_organize_schedule_cron": "0 3 * * *",
            # v1.30.0 阶段 4：organize_source_path 已退役，04:00 任务
            # 注册条件改读漏斗总闸 + 阶段 1（这里默认全开）。
            "_organize_source_path": "/转存",
            "_funnel_enabled": True,
            "_funnel_phase1_enabled": True,
            "_organize_schedule_auto_execute": False,
        }
        values.update(flags)
        for key, value in values.items():
            setattr(plugin, key, value)
        return {item["id"]: item for item in _real_maintenance_services(plugin)}

    def test_maintenance_services_all_enabled(self):
        plugin = self._plugin()
        services = self._maintenance(plugin)
        self.assertEqual(
            set(services),
            {
                "CloudSubscribe_StrmFullScan",
                "CloudSubscribe_StrmIncrement",
                "CloudSubscribe_Organize",
                "CloudSubscribe_RetryIndexMaintenance",
            },
        )
        tasks = {key: item["func_kwargs"]["task"] for key, item in services.items()}
        self.assertEqual(tasks["CloudSubscribe_StrmFullScan"], "strm_full_scan")
        self.assertEqual(tasks["CloudSubscribe_StrmIncrement"], "strm_increment")
        self.assertEqual(tasks["CloudSubscribe_Organize"], "organize")
        self.assertEqual(tasks["CloudSubscribe_RetryIndexMaintenance"], "retry_index_maintenance")
        for item in services.values():
            # MP 用 func(**func_kwargs) 调用；kwargs 是 add_job 的调度参数
            self.assertEqual(getattr(item["func"], "__name__", ""), "_run_scheduled_task")
            self.assertEqual(item["kwargs"], {})
        trigger = services["CloudSubscribe_StrmIncrement"]["trigger"]
        self.assertEqual(getattr(trigger, "interval").total_seconds(), 7 * 60)
        full_scan = str(services["CloudSubscribe_StrmFullScan"]["trigger"])
        self.assertIn("hour='4'", full_scan)
        self.assertIn("minute='20'", full_scan)

    def test_maintenance_services_respect_switches(self):
        plugin = self._plugin()
        services = self._maintenance(
            plugin,
            _strm_generate_enabled=False,
            _organize_schedule_enabled=False,
        )
        self.assertEqual(set(services), {"CloudSubscribe_RetryIndexMaintenance"})
        # v1.30.0 阶段 4：04:00 任务注册条件改读漏斗总闸 + 阶段 1；
        # organize_source_path 已退役，不再参与判定。
        services = self._maintenance(plugin, _funnel_phase1_enabled=False)
        self.assertNotIn("CloudSubscribe_Organize", services)
        services = self._maintenance(plugin, _funnel_enabled=False)
        self.assertNotIn("CloudSubscribe_Organize", services)
        services = self._maintenance(plugin, _organize_source_path="")
        self.assertIn("CloudSubscribe_Organize", services)
        services = self._maintenance(plugin, _organize_schedule_cron="not a cron")
        self.assertNotIn("CloudSubscribe_Organize", services)

    def test_get_service_declares_maintenance_and_config_keys_refresh_mp(self):
        root = Path(__file__).resolve().parents[1] / "plugins.v2" / "cloudsubscribex"
        registration = (root / "core" / "api" / "registration.py").read_text(encoding="utf-8")
        body = registration[registration.index("    def get_service(self)"):]
        body = body[:body.index("    def _maintenance_services(self)")]
        self.assertIn("services.extend(self._maintenance_services())", body)
        init = (root / "__init__.py").read_text(encoding="utf-8")
        keys_block = init[init.index("service_config_keys = {"):]
        keys_block = keys_block[:keys_block.index("}")]
        for key in (
            "strm_generate_enabled", "strm_full_scan_enabled",
            "strm_increment_enabled", "strm_increment_minutes",
            "organize_schedule_enabled", "organize_schedule_cron",
            "organize_source_path", "organize_schedule_auto_execute",
        ):
            self.assertIn(f'"{key}"', keys_block)
        # 迁走的任务不得再登记到插件自建调度器
        for job_id in (
            "cloudsubscribe_strm_full_scan", "cloudsubscribe_strm_increment",
            "cloudsubscribe_retry_index_maintenance", '"cloudsubscribe_organize"',
        ):
            self.assertNotIn(job_id, init)
        # 不再有静默的 hasattr 守卫
        self.assertNotIn('return {"success": False, "message": "同步服务未就绪"}\n        try', init)
        self.assertEqual(init.count('hasattr(sync_handler, "'), 0)


# ---------------------------------------------------------------- 第 3 项


class EmbyPathUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sandbox = _ModuleSandbox()
        cls.module = _emby_update_module(cls.sandbox)

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.restore()

    def test_concurrent_submits_merge_into_one_request(self):
        updater = self.module.EmbyPathUpdater(window=0.3)
        sent = []
        results = []

        def send(payload):
            sent.append(payload)
            return 204

        def worker(path):
            results.append(updater.submit(("emby", "h", "k"), [(path, "Created")], send))

        threads = [
            threading.Thread(target=worker, args=(f"/strm/剧/S01E0{i}.strm",))
            for i in range(1, 4)
        ]
        for thread in threads:
            thread.start()
            time.sleep(0.02)
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(sent), 1)
        self.assertEqual(
            sorted(item["Path"] for item in sent[0]),
            [f"/strm/剧/S01E0{i}.strm" for i in range(1, 4)],
        )
        self.assertEqual(results, ["ok", "ok", "ok"])

    def test_unsupported_runs_fallback_once(self):
        updater = self.module.EmbyPathUpdater(window=0.2)
        fallbacks = []
        results = []

        def worker(path):
            results.append(updater.submit(
                ("emby", "h", "k"), [(path, "Created")],
                send=lambda payload: 404,
                fallback=lambda: fallbacks.append(1) or True,
            ))

        threads = [threading.Thread(target=worker, args=(f"/strm/a{i}.strm",)) for i in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(results, ["fallback_ok"] * 3)

    def test_path_outside_libraries_is_not_sent(self):
        updater = self.module.EmbyPathUpdater(window=0)
        sent = []
        result = updater.submit(
            "emby", [("/media/strm/剧/S01E01.strm", "Created")],
            send=lambda payload: sent.append(payload) or 204,
            library_roots=lambda: ["/strm/115网盘"],
        )
        self.assertEqual(result, "unsupported")
        self.assertEqual(sent, [])
        result = updater.submit(
            "emby", [("/strm/115网盘/剧集/S01E01.strm", "Created")],
            send=lambda payload: sent.append(payload) or 204,
            library_roots=lambda: ["/strm/115网盘/"],
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(sent), 1)

    def test_status_classification(self):
        self.assertEqual(self.module.classify_status(204), "ok")
        self.assertEqual(self.module.classify_status(405), "unsupported")
        self.assertEqual(self.module.classify_status(500), "failed")
        self.assertEqual(self.module.classify_status(None), "failed")
        self.assertFalse(self.module.paths_under_libraries(["/a/b"], []))


class _Response(SimpleNamespace):
    pass


class _RecordingRequests:
    """记录 Emby 请求的 RequestUtils 替身：按 URL 后缀给状态码。"""

    def __init__(self, media_updated_status=204):
        self.calls = []
        self.media_updated_status = media_updated_status
        outer = self

        class _Client:
            def __init__(self, timeout=15):
                self.timeout = timeout

            def post_res(self, url="", params=None, json=None, **kwargs):
                outer.calls.append(("post", url, json))
                if url.endswith("/Library/Media/Updated"):
                    return _Response(status_code=outer.media_updated_status)
                return _Response(status_code=204)

            def get_res(self, url="", params=None, **kwargs):
                outer.calls.append(("get", url, params))
                return None

        self.client = _Client


def _emby_service(roots):
    instance = SimpleNamespace(
        _host="http://emby.local:8096/",
        _apikey="k",
        get_emby_virtual_folders=lambda: [{"Id": "1", "Name": "剧集", "Path": list(roots)}],
        is_inactive=lambda: False,
        refresh_library_by_items=MagicMock(return_value=True),
    )
    return SimpleNamespace(
        type="emby",
        instance=instance,
        config=SimpleNamespace(config={"host": "http://emby.local:8096", "apikey": "k"}),
    )


class EmbyNotifierPathTests(unittest.TestCase):
    """真实 MediaServerNotifier._refresh_emby_entries。"""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = _ModuleSandbox()
        install_app_mocks(
            "app.chain", "app.chain.mediaserver", "app.helper", "app.helper.mediaserver",
            "app.utils", "app.utils.http", "app.schemas", "app.schemas.types",
        )
        cls.update_module = _emby_update_module(cls.sandbox)
        cls.sandbox.load(
            "cloudsubscribex.utils.path_map",
            "plugins.v2/cloudsubscribex/utils/path_map.py",
        )
        cls.module = cls.sandbox.load(
            MEDIA_SERVER_NAME,
            "plugins.v2/cloudsubscribex/handlers/notification/media_server.py",
        )

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.restore()

    def _patch(self, requests):
        globs = self.module.MediaServerNotifier._refresh_emby_entries.__globals__
        patcher = patch.dict(globs, {
            "RequestUtils": requests.client,
            "emby_path_updater": self.update_module.EmbyPathUpdater(window=0),
            "logger": MagicMock(),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entries(self):
        server_path = Path("/strm/115网盘/剧集/后会友期 (2026)/Season 1/S01E09.strm")
        return [{
            "item": SimpleNamespace(title="后会友期"),
            "title": "后会友期",
            "tmdb_id": 338938,
            "folder": server_path.parent,
            "paths": {server_path},
            "deleted_paths": set(),
        }], server_path

    def test_media_updated_accepted_skips_item_refresh(self):
        requests = _RecordingRequests(media_updated_status=204)
        self._patch(requests)
        notifier = self.module.MediaServerNotifier(enabled=True, mediaservers=["Emby"])
        self.addCleanup(notifier.close, False)
        entries, server_path = self._entries()
        service = _emby_service(["/strm/115网盘"])
        self.assertTrue(notifier._refresh_emby_entries("Emby", service, entries))
        posts = [call for call in requests.calls if call[0] == "post"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith("/emby/Library/Media/Updated"))
        self.assertEqual(
            posts[0][2],
            {"Updates": [{"Path": server_path.as_posix(), "UpdateType": "Created"}]},
        )
        # 不再按 TMDB 查条目、不再 /Items/{id}/Refresh
        self.assertFalse([call for call in requests.calls if call[0] == "get"])

    def test_media_updated_404_falls_back_to_item_refresh(self):
        requests = _RecordingRequests(media_updated_status=404)
        self._patch(requests)
        notifier = self.module.MediaServerNotifier(enabled=True, mediaservers=["Emby"])
        self.addCleanup(notifier.close, False)
        entries, _ = self._entries()
        service = _emby_service(["/strm/115网盘"])
        self.assertTrue(notifier._refresh_emby_entries("Emby", service, entries))
        gets = [call for call in requests.calls if call[0] == "get"]
        self.assertTrue(gets, "404 时应回退到按条目查找刷新")
        service.instance.refresh_library_by_items.assert_called_once()

    def test_path_outside_emby_library_falls_back_without_post(self):
        requests = _RecordingRequests(media_updated_status=204)
        self._patch(requests)
        notifier = self.module.MediaServerNotifier(enabled=True, mediaservers=["Emby"])
        self.addCleanup(notifier.close, False)
        entries, _ = self._entries()
        service = _emby_service(["/mnt/其它库"])
        notifier._refresh_emby_entries("Emby", service, entries)
        self.assertFalse([
            call for call in requests.calls
            if call[0] == "post" and call[1].endswith("/Library/Media/Updated")
        ])
        service.instance.refresh_library_by_items.assert_called_once()


class _PassthroughGuard:
    @staticmethod
    def call(name, func, *args, fallback=None, **kwargs):
        return func(*args, **kwargs)


class MediaRefreshPathTests(unittest.TestCase):
    """真实 MediaRefreshMixin.refresh_media_servers(paths=...)。"""

    @classmethod
    def setUpClass(cls):
        cls.sandbox = _ModuleSandbox()
        install_app_mocks(
            "app.chain", "app.chain.mediaserver", "app.helper", "app.helper.mediaserver",
            "app.utils", "app.utils.http", "app.schemas", "app.schemas.types",
            "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
        )
        cls.update_module = _emby_update_module(cls.sandbox)
        cls.sandbox.load(
            "cloudsubscribex.utils.path_map",
            "plugins.v2/cloudsubscribex/utils/path_map.py",
        )
        cls.media_server = cls.sandbox.load(
            MEDIA_SERVER_NAME,
            "plugins.v2/cloudsubscribex/handlers/notification/media_server.py",
        )
        cls.module = cls.sandbox.load(
            MEDIA_REFRESH_NAME,
            "plugins.v2/cloudsubscribex/handlers/sync/media_refresh.py",
        )

    @classmethod
    def tearDownClass(cls):
        cls.sandbox.restore()

    def _setup(self, status, roots=("/strm/115网盘",)):
        requests = _RecordingRequests(media_updated_status=status)
        notifier_cls = self.media_server.MediaServerNotifier
        for globs, values in (
            (notifier_cls.submit_emby_path_updates.__func__.__globals__, {
                "RequestUtils": requests.client,
                "emby_path_updater": self.update_module.EmbyPathUpdater(window=0),
                "logger": MagicMock(),
            }),
            (self.module.MediaRefreshMixin._refresh_media_servers_impl.__globals__, {
                "MediaServerNotifier": notifier_cls,
                "MediaServerResolver": SimpleNamespace(
                    _services=lambda: {"Emby": _emby_service(roots)},
                ),
                "bypass_guard": _PassthroughGuard(),
                "logger": MagicMock(),
            }),
        ):
            patcher = patch.dict(globs, values)
            patcher.start()
            self.addCleanup(patcher.stop)
        full = []
        notifier = notifier_cls(
            enabled=True, mediaservers=["Emby"],
            path_mappings="/strm/115网盘#/media/strm/115网盘",
        )
        self.addCleanup(notifier.close, False)
        owner = SimpleNamespace(_media_server_notifier=notifier)
        mixin = self.module.MediaRefreshMixin(owner)
        object.__setattr__(mixin, "_refresh_emby", lambda instance: full.append(1) or True)
        return mixin, requests, full

    def test_paths_are_mapped_and_sent_by_path(self):
        mixin, requests, full = self._setup(204)
        result = mixin.refresh_media_servers(
            paths=[Path("/media/strm/115网盘/剧集/A/S01E01.strm")], update_type="Created",
        )
        self.assertEqual(result, {"Emby": "ok"})
        self.assertEqual(full, [])
        post = [call for call in requests.calls if call[0] == "post"][0]
        self.assertEqual(
            post[2]["Updates"],
            [{"Path": "/strm/115网盘/剧集/A/S01E01.strm", "UpdateType": "Created"}],
        )

    def test_unsupported_falls_back_to_full_refresh(self):
        mixin, requests, full = self._setup(501)
        result = mixin.refresh_media_servers(
            paths=[Path("/media/strm/115网盘/剧集/A/S01E01.strm")], update_type="Deleted",
        )
        self.assertEqual(result, {"Emby": "ok"})
        self.assertEqual(full, [1])

    def test_no_paths_keeps_full_refresh(self):
        mixin, requests, full = self._setup(204)
        result = mixin.refresh_media_servers()
        self.assertEqual(result, {"Emby": "ok"})
        self.assertEqual(full, [1])
        self.assertFalse(requests.calls)

    def test_too_many_paths_uses_full_refresh(self):
        mixin, requests, full = self._setup(204)
        paths = [
            Path(f"/media/strm/115网盘/剧集/A/S01E{i:03d}.strm")
            for i in range(mixin._PATH_UPDATE_LIMIT + 1)
        ]
        mixin.refresh_media_servers(paths=paths, update_type="Modified")
        self.assertEqual(full, [1])
        self.assertFalse(requests.calls)


if __name__ == "__main__":
    unittest.main()
