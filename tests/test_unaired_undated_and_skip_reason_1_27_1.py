"""v1.27.1：无播出日期的集按分享实际文件判定 + 未转存原因单一来源。

复现（2026-11-19，《多喜一家人》TMDB 294498）：库里 E01–E08，TG 频道多条
含 E09/E10 的分享，每轮却是「跳过未播出剧集：E09-E34（其中 26 集 TMDB 无播
出日期）」「当前缺集均未播出」，转存 0；频道监控只显示「本轮无新集」。

修复 1（用户定：不推算日期，按分享里实际有的集来算）：
- TMDB 日期在未来的集 → 仍算未播出；
- 无日期的集 → 留在缺集里，转不转存只看分享里真实解析出的文件集号；
- 网盘里已有的无日期集按物理事实计入已有；整季无日期的旧口径不变。

修复 2：原因由 SyncHandler 的组件写在 SyncHandler 上（线程隔离），运行态
服务 ``_run_subscription_group`` 读 ``last_skip_reason()`` 并随组结果返回，
同步执行层汇总进 ``_sync_context["skip_reason"]``，执行结果的
``data.skip_reason`` 交给频道监控 ``_auto_transfer_outcome``。v1.27.0 及以前
读的是插件实例上的同名属性（每组开头清空、组件从不写那里），恒为空。
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import threading
import types
import unittest
from threading import Lock
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 复用既有测试已加载的真实模块（模块级加载在各自文件里已经发生，这里只取引用）。
import test_manual_share_dedupe as _tv_env  # noqa: E402
import test_explicit_composition_1_19_0 as _handler_env  # noqa: E402
import test_tg_skip_reason_short as _tg_env  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ROOT = os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")

TelevisionSyncProcessor = _tv_env.TelevisionSyncProcessor
SubscribeFlowService = _tv_env.SubscribeFlowService
FakeOwner = _tv_env.FakeOwner
SyncHandler = _handler_env.SyncHandler
auto_transfer_outcome = _tg_env.tg_module._auto_transfer_outcome
shorten = _tg_env.tg_module.TgChannelMonitor._shorten_skip_reason

PAST = "2020-01-01"
FUTURE = "2999-01-01"


def _files(episodes):
    return [
        {
            "id": str(episode),
            "name": f"兰香如故.S01E{episode:02d}.mkv",
            "size": 3_000_000_000 + episode,
            "url": "https://115cdn.com/s/resource",
            "is_dir": False,
        }
        for episode in episodes
    ]


class _ShareOwner(FakeOwner):
    """分享里实际存在的文件 = share_episodes；网盘已有 = cloud_episodes。"""

    def __init__(self, share_episodes, cloud_episodes):
        super().__init__(enable_cloud_upgrade=False, cloud_episodes=cloud_episodes)
        self.share_episodes = list(share_episodes)
        self._last_skip_reason = ""

    def _validated_resource_files(self, url, resource_title="", target_season=None, log_prefix="",
            target_file_ids=None, target_file_names=None,
            recognition_work_id=None, recognition_season=None):
        return _files(self.share_episodes)


def _run_tv(owner, air_dates, total_episode=34):
    processor = TelevisionSyncProcessor(owner)
    with patch.object(
            TelevisionSyncProcessor,
            "_season_episode_air_dates",
            staticmethod(lambda mediainfo, season: dict(air_dates)),
    ):
        processor.process_tv_subscribe(
            subscribe=_tv_env._subscribe(total_episode=total_episode),
            history=[],
            transfer_details=[],
            transferred_count=0,
            manual_resources=[_tv_env._manual_resource()],
            manual_upgrade=False,
            transient_target=False,
        )
    return sorted(
        int(item["episode"])
        for batch in owner.transfer_items
        for item in batch
    )


def _dated(episodes, value):
    return {episode: value for episode in episodes}


class UndatedEpisodesFollowShareFilesTest(unittest.TestCase):
    def test_undated_episodes_transfer_when_share_has_real_files(self):
        """E01-E08 有日期（已播），E09 起无日期；分享里真有 E09/E10 → 转这两集。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 9))
        transferred = _run_tv(owner, _dated(range(1, 9), PAST))
        self.assertEqual(transferred, [9, 10])
        self.assertNotEqual(owner._last_skip_reason, "缺集尚未播出，等待更新")

    def test_undated_episodes_absent_from_share_not_transferred(self):
        """分享里只有 E01-E08：无日期的 E09-E34 不会凭空转存。"""
        owner = _ShareOwner(share_episodes=range(1, 9), cloud_episodes=range(1, 9))
        self.assertEqual(_run_tv(owner, _dated(range(1, 9), PAST)), [])

    def test_future_dated_episode_still_blocked(self):
        """E09 日期在未来 → 仍挡；E10 无日期、分享里有 → 转。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 9))
        dates = _dated(range(1, 9), PAST)
        dates[9] = FUTURE
        self.assertEqual(_run_tv(owner, dates), [10])

    def test_all_missing_future_dated_reports_reason(self):
        """缺集全是未来日期：不搜不转，原因写成「缺集尚未播出」。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 9))
        dates = _dated(range(1, 9), PAST)
        dates.update(_dated(range(9, 35), FUTURE))
        self.assertEqual(_run_tv(owner, dates), [])
        self.assertEqual(owner._last_skip_reason, "缺集尚未播出，等待更新")

    def test_whole_season_without_dates_keeps_old_rule(self):
        """整季一条日期都没有：与旧口径一致，按分享实际文件转缺集。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 9))
        self.assertEqual(_run_tv(owner, {}), [9, 10])

    def test_undated_episodes_already_in_cloud_count_as_existing(self):
        """网盘里已有无日期的 E09/E10：按物理事实计入已有，不重复转存。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 11))
        self.assertEqual(_run_tv(owner, _dated(range(1, 9), PAST)), [])

    def test_future_dated_episode_in_cloud_not_counted(self):
        """日期在未来但网盘里已有的集（泄露/错包）仍不计入已有（旧行为保留）。"""
        owner = _ShareOwner(share_episodes=range(1, 11), cloud_episodes=range(1, 10))
        dates = _dated(range(1, 9), PAST)
        dates[9] = FUTURE
        # E09 被当作未播出：既不计入已有，也不进缺集；E10 无日期、分享有 → 转。
        self.assertEqual(_run_tv(owner, dates), [10])


class SearchLayerHasNoUndatedFilterTest(unittest.TestCase):
    def test_hdhive_stale_filter_ignores_undated_episodes(self):
        """搜索层唯一用到分集日期的是影巢的「资源早于最早播出日」过滤，只看有日期的集。"""
        parser_path = os.path.join(PLUGIN_ROOT, "search", "hdhive", "web", "parser.py")
        tree = ast.parse(open(parser_path, encoding="utf-8").read())
        func = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "earliest_target_air_time"
        )
        namespace = {"Dict": dict, "resource_timestamp": lambda value: 100.0 if value else 0}
        exec(compile(ast.Module(body=[func], type_ignores=[]), parser_path, "exec"), namespace)
        earliest = namespace["earliest_target_air_time"]
        self.assertEqual(earliest({9, 10}, {1: PAST}), 0)
        self.assertEqual(earliest({8, 9}, {8: PAST}), 100.0)


def _load_runtime_probe():
    """以独立模块名加载真实 core/services/runtime.py（不顶替全量套件里的同名模块）。"""
    touched = (
        "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.services",
        "cloudsubscribex.core.media", "cloudsubscribex.core.downloader_view",
        "app.core.config", "app.log", "app.schemas.types",
    )
    snapshot = {name: sys.modules.get(name) for name in touched}
    parent_attrs = {}
    for name in touched:
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is not None:
            parent_attrs[(parent_name, child)] = getattr(parent, child, None)
    # runtime.py 顶部的 MP 依赖：全量套件里这几个占位模块形态各异（有的缺
    # MediaType），加载期间换成只含所需名字的临时模块，加载完原样恢复。
    sys.modules["app.core.config"] = types.SimpleNamespace(
        global_vars=SimpleNamespace(is_system_stopped=False)
    )
    sys.modules["app.log"] = types.SimpleNamespace(logger=MagicMock())
    sys.modules["app.schemas.types"] = types.SimpleNamespace(
        MediaType=SimpleNamespace(MOVIE=SimpleNamespace(value="电影"), TV=SimpleNamespace(value="电视剧"))
    )
    for name in ("cloudsubscribex.core.media", "cloudsubscribex.core.downloader_view"):
        if sys.modules.get(name) is None:
            module = types.ModuleType(name)
            module.media_identity = MagicMock()
            module.tmdb_id_of = MagicMock()
            module.display_phase = MagicMock()
            module.display_progress = MagicMock()
            sys.modules[name] = module
    for name in ("cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.services"):
        if sys.modules.get(name) is None:
            package = types.ModuleType(name)
            package.__path__ = []
            sys.modules[name] = package
    core = sys.modules["cloudsubscribex.core"]
    saved_core = {key: core.__dict__.get(key, None) for key in ("OwnerDelegator", "CloudDriveCapability")}
    delegation_spec = importlib.util.spec_from_file_location(
        "_csx_real_delegation_1_27_1", os.path.join(PLUGIN_ROOT, "core", "delegation.py")
    )
    delegation = importlib.util.module_from_spec(delegation_spec)
    delegation_spec.loader.exec_module(delegation)
    core.OwnerDelegator = delegation.OwnerDelegator
    if saved_core["CloudDriveCapability"] is None:
        core.CloudDriveCapability = MagicMock()
    probe_name = "cloudsubscribex.core.services._runtime_probe_1_27_1"
    spec = importlib.util.spec_from_file_location(
        probe_name, os.path.join(PLUGIN_ROOT, "core", "services", "runtime.py")
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "cloudsubscribex.core.services"
    try:
        spec.loader.exec_module(module)
    finally:
        for key, value in saved_core.items():
            if value is None:
                core.__dict__.pop(key, None)
            else:
                setattr(core, key, value)
        for name, value in snapshot.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value
        for (parent_name, child), value in parent_attrs.items():
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if value is None:
                parent.__dict__.pop(child, None)
            else:
                setattr(parent, child, value)
    return module, delegation.OwnerDelegator


def _sync_execution_result():
    """真实 SyncExecutionService._sync_execution_result（纯函数，按源码取出执行）。"""
    path = os.path.join(PLUGIN_ROOT, "core", "services", "sync.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    cls = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "SyncExecutionService"
    )
    func = next(
        node for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "_sync_execution_result"
    )
    func.decorator_list = []
    namespace = {"Any": object, "Dict": dict, "Optional": lambda *a: object}
    namespace["Optional"] = type("O", (), {"__class_getitem__": classmethod(lambda c, i: object)})
    exec(compile(ast.Module(body=[func], type_ignores=[]), path, "exec"), namespace)
    return namespace["_sync_execution_result"]


class _PluginHost:
    """插件宿主：运行态服务的 _owner。未声明的能力按 MagicMock 处理。"""

    def __init__(self, sync_handler):
        self._sync_handler = sync_handler
        self._sync_tasks_lock = Lock()
        self._sync_tasks = {}
        self._stop_event = None
        self._task_local = threading.local()

    def _direct_cloud_manual_resources(self, manual_resources):
        return []

    def _is_cloud_upgrade_subscribe(self, subscribe):
        return False

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        value = MagicMock()
        object.__setattr__(self, name, value)
        return value


def _real_handler(stopped=True):
    handler = SyncHandler.__new__(SyncHandler)
    handler._plugin = SimpleNamespace()
    handler._should_stop = lambda: stopped
    handler.subscribe_flow = SubscribeFlowService(handler)
    handler.television = TelevisionSyncProcessor(handler)
    return handler


class SkipReasonSingleSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime, cls.OwnerDelegator = _load_runtime_probe()

    def test_component_write_lands_on_sync_handler(self):
        handler = _real_handler(stopped=True)
        self.assertNotIn("__setattr__", vars(SyncHandler))
        processed = handler.process_tv_subscribe(
            _tv_env._subscribe(total_episode=34), [], [], 0,
        )
        self.assertEqual(processed, 0)
        self.assertEqual(handler.last_skip_reason(), "已手动停止")

    def test_reason_is_isolated_per_thread(self):
        handler = _real_handler(stopped=False)
        seen = {}

        def worker(name, reason):
            SubscribeFlowService(handler)._owner._last_skip_reason = reason
            barrier.wait()
            seen[name] = handler.last_skip_reason()

        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=worker, args=("a", "缺集尚未播出，等待更新")),
            threading.Thread(target=worker, args=("b", "库中已完整存在，无需转存")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(seen, {"a": "缺集尚未播出，等待更新", "b": "库中已完整存在，无需转存"})

    def test_reason_flows_from_television_to_tg_outcome(self):
        handler = _real_handler(stopped=True)
        host = _PluginHost(handler)
        service = self.runtime.SyncRuntimeService(host)
        runtime_cls = self.runtime.SyncRuntimeService
        with patch.object(self.runtime, "global_vars", SimpleNamespace(is_system_stopped=False)), \
                patch.object(runtime_cls, "_update_sync_task", lambda self, *a, **k: None), \
                patch.object(runtime_cls, "_pending_finalize_items", lambda self, s: []), \
                patch.object(runtime_cls, "_postprocessing_text", lambda self, items: ("", "")):
            group = service._run_subscription_group(
                [_tv_env._subscribe(total_episode=34)], [], None,
            )
        self.assertEqual(group["transferred"], 0)
        self.assertEqual(group["skip_reason"], "已手动停止")
        # 插件实例上不再有共享的 _last_skip_reason（并发组互相覆盖的来源）。
        self.assertNotIn("_last_skip_reason", host.__dict__)

        result = _sync_execution_result()(
            True, "订阅搜索完成，未发现需要转存的新资源",
            context={"transferred": 0, "skip_reason": group["skip_reason"]},
        )
        self.assertEqual(result["data"]["skip_reason"], "已手动停止")
        self.assertEqual(auto_transfer_outcome(result), ("skipped", "未转存：已手动停止"))

    def test_do_sync_aggregates_group_reason_into_context(self):
        """_do_sync 汇总各组 skip_reason 写进最终 _sync_context；执行层不再读插件属性。"""
        source = open(
            os.path.join(PLUGIN_ROOT, "core", "services", "sync.py"), encoding="utf-8"
        ).read()
        self.assertIn('group_reason = str(result.get("skip_reason") or "").strip()', source)
        self.assertIn('"skip_reason": run_skip_reason,', source)
        self.assertNotIn('getattr(self, "_last_skip_reason"', source)
        runtime_source = open(
            os.path.join(PLUGIN_ROOT, "core", "services", "runtime.py"), encoding="utf-8"
        ).read()
        self.assertNotIn("self._last_skip_reason", runtime_source)


class ShortReasonTextTest(unittest.TestCase):
    def test_unaired_reason_fits_channel_monitor(self):
        self.assertEqual(shorten("未转存：缺集尚未播出，等待更新"), "缺集尚未播出，等待更新")
        self.assertLessEqual(len(shorten("未转存：缺集尚未播出，等待更新")), 26)
        self.assertEqual(shorten("未转存：库中已完整存在，无需转存"), "已入库")


if __name__ == "__main__":
    unittest.main()
