"""失效分享 / 无内容候选被跳过后的汇总可见化（1.6.35）测试。

覆盖任务书 docs/plan/2026-10-13-失效分享跳过汇总通知.md 列出的 6 条：

1. 失效采集 + 同链接去重；
2. empty 与 invalid 两类并存时的 by_kind / by_provider 计数；
3. take() 后清零（第二次 take() 为空）；
4. 无跳过记录时 summary_text() 返回 ""（日志与通知文案与现状逐字一致）；
5. _notify=False 时不发通知；
6. 采集器缺失（_skip_stats 为 None）时主流程不报错。

第二轮追加内容不匹配（mismatch）采集类型：

7. 三类并存时的 by_kind / by_kind_provider 计数与两段文案的类型顺序；
8. 4 个新增 mismatch 采集点（电视剧 2 处、电影 2 处）接线 + 触发即记录。
"""

import ast
import os
import sys
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_module,
    register_package,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- app.* 占位依赖 ----
install_app_mocks(
    "app.db",
    "app.db.models",
    "app.db.models.subscribe",
    "app.db.subscribe_oper",
    "app.core",
    "app.core.config",
    "app.core.cache",
)


def _ensure_package(name):
    """复用测试进程中已注册的占位包，避免覆盖其它用例的注册结果。"""
    if name in sys.modules:
        return sys.modules[name]
    return register_package(name)


def _ensure_attrs(name, **attrs):
    """补齐模块上缺失的导出名（已存在的名字保持原样）。"""
    module = _ensure_package(name)
    for attr, value in attrs.items():
        if attr not in vars(module):
            setattr(module, attr, value)
    return module


def _ensure_leaf(name, **attrs):
    """叶子模块缺失时才注册（已存在则原样复用）。"""
    if name in sys.modules:
        return sys.modules[name]
    return register_module(name, **attrs)


# ---- cloudsubscribex 占位包 ----
_ensure_package("cloudsubscribex")
core_package = _ensure_package("cloudsubscribex.core")
if not isinstance(getattr(core_package, "OwnerDelegator", None), type):
    core_package.OwnerDelegator = OwnerDelegator
for _attr in ("CloudDriveCapability", "CloudDriveProvider", "CloudFile", "SearchCapability"):
    if _attr not in vars(core_package):
        setattr(core_package, _attr, MagicMock())
_ensure_package("cloudsubscribex.core.services")
_ensure_package("cloudsubscribex.search")
_ensure_attrs(
    "cloudsubscribex.utils",
    MediaFileParser=MagicMock(),
    parse_magnet_metadata=MagicMock(),
)
_ensure_package("cloudsubscribex.handlers")
_ensure_package("cloudsubscribex.handlers.sync")
_ensure_leaf("cloudsubscribex.core.services.runtime", sync_lock=MagicMock())
_ensure_leaf(
    "cloudsubscribex.core.media",
    media_identity=MagicMock(),
    tmdb_id_of=MagicMock(),
)
_ensure_leaf("cloudsubscribex.utils.cache", create_platform_ttl_cache=MagicMock())
if "cloudsubscribex.handlers.sync.utils" not in sys.modules:
    load_module(
        "cloudsubscribex.handlers.sync.utils",
        "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
    )

# ---- 被测模块 ----
skip_stats_module = load_module(
    "cloudsubscribex.core.services.skip_stats",
    "plugins.v2/cloudsubscribex/core/services/skip_stats.py",
)
SkipStatsCollector = skip_stats_module.SkipStatsCollector

# 网盘中文名走真实实现（与候选摘要「光鸭网盘 1（可用 1）」同一套命名）。
search_types_module = load_module(
    "cloudsubscribex.search.types",
    "plugins.v2/cloudsubscribex/search/types.py",
)

load_module(
    "cloudsubscribex.handlers.sync.candidate_filter",
    "plugins.v2/cloudsubscribex/handlers/sync/candidate_filter.py",
)
resources_module = load_module(
    "cloudsubscribex.handlers.sync.resources",
    "plugins.v2/cloudsubscribex/handlers/sync/resources.py",
)
ResourceTransferService = resources_module.ResourceTransferService


class _StubMediaFileParser:
    """MediaFileParser 桩：原样回吐，避免依赖平台侧媒体解析规则。"""

    @staticmethod
    def iter_files(files):
        return list(files or [])


resources_module.MediaFileParser = _StubMediaFileParser

sync_module = load_module(
    "cloudsubscribex.core.services.sync",
    "plugins.v2/cloudsubscribex/core/services/sync.py",
)
SyncExecutionService = sync_module.SyncExecutionService


class _StubShareService:
    """check_share_status / list_share_files 桩。"""

    def __init__(self, invalid_urls=(), files=None):
        self.invalid_urls = set(invalid_urls)
        self.files = list(files or [])

    def check_share_status(self, share_url):
        invalid = share_url in self.invalid_urls
        return SimpleNamespace(
            is_valid=not invalid,
            status_text="分享已失效" if invalid else "正常",
            file_count=0 if invalid else len(self.files),
        )

    def list_share_files(self, share_url, **kwargs):
        return list(self.files)


class _StubOwner:
    """职责委托宿主：只提供采集与通知所需的运行态。"""

    def __init__(
            self,
            notify=True,
            collector=None,
            share_service=None,
            skip_stats_present=True,
    ):
        self._notify = notify
        self._notification_type = "plugin"
        self._share_transfer = share_service or _StubShareService()
        self._cloud_drive = None
        self._cloud_drive_registry = None
        self.messages = []
        if skip_stats_present:
            self._skip_stats = collector

    def post_message(self, mtype=None, title=None, text=None, **kwargs):
        self.messages.append({"mtype": mtype, "title": title, "text": text})

    @staticmethod
    def _resource_log_reference(share_url):
        # 脱敏引用：测试里按 ? 前截断，真实实现见 handlers/sync/service.py:1338。
        return str(share_url or "").split("?")[0]

    @staticmethod
    def _timed_sync_call(phase, func, *args, **kwargs):
        return func(*args, **kwargs)


class TestInvalidCollectionAndDedupe(unittest.TestCase):
    """1. 失效采集 + 同链接去重。"""

    def test_invalid_recorded_once_for_same_link(self):
        collector = SkipStatsCollector()
        owner = _StubOwner(
            collector=collector,
            share_service=_StubShareService(invalid_urls={"https://115.com/s/abc"}),
        )
        service = ResourceTransferService(owner)

        share_url = "https://115.com/s/abc"
        self.assertFalse(service._validate_resource_url(share_url))
        self.assertFalse(service._validate_resource_url(share_url))

        snapshot = collector.take()
        self.assertEqual(snapshot["total"], 1)
        self.assertEqual(snapshot["by_kind"], {SkipStatsCollector.KIND_INVALID: 1})
        self.assertEqual(snapshot["by_provider"], {"115网盘": 1})

    def test_invalid_reference_is_masked_reference(self):
        collector = SkipStatsCollector()
        owner = _StubOwner(
            collector=collector,
            share_service=_StubShareService(invalid_urls={"https://115.com/s/abc?pwd=secret"}),
        )
        service = ResourceTransferService(owner)

        service._validate_resource_url("https://115.com/s/abc?pwd=secret")

        snapshot = collector.take()
        self.assertEqual(snapshot["total"], 1)
        # 只允许出现脱敏后的引用，不得带 query/token。
        self.assertEqual(
            snapshot["by_kind_provider"][SkipStatsCollector.KIND_INVALID],
            {"115网盘": 1},
        )
        summary = SkipStatsCollector.format_summary(snapshot)
        self.assertNotIn("pwd=secret", summary)
        self.assertNotIn("?pwd", summary)


class TestKindAndProviderCounts(unittest.TestCase):
    """2. empty 与 invalid 同时存在时的 by_kind / by_provider 计数。"""

    def test_both_kinds_counted(self):
        collector = SkipStatsCollector()
        share_service = _StubShareService(
            invalid_urls={"https://115.com/s/dead"},
            files=[],
        )
        owner = _StubOwner(collector=collector, share_service=share_service)
        service = ResourceTransferService(owner)

        self.assertFalse(
            service._validate_resource_url("https://115.com/s/dead")
        )
        self.assertEqual(
            service._validated_resource_files(
                "https://www.guangyapan.com/s/xyz",
                resource_title="玉簟秋",
            ),
            [],
        )

        snapshot = collector.take()
        self.assertEqual(snapshot["total"], 2)
        self.assertEqual(
            snapshot["by_kind"],
            {
                SkipStatsCollector.KIND_INVALID: 1,
                SkipStatsCollector.KIND_EMPTY: 1,
            },
        )
        self.assertEqual(snapshot["by_provider"], {"115网盘": 1, "光鸭网盘": 1})
        self.assertEqual(
            snapshot["by_kind_provider"],
            {
                SkipStatsCollector.KIND_INVALID: {"115网盘": 1},
                SkipStatsCollector.KIND_EMPTY: {"光鸭网盘": 1},
            },
        )
        self.assertEqual(
            SkipStatsCollector.format_summary(snapshot),
            "本次跳过 2 个候选资源：分享链接失效 1、无可用文件 1（115网盘 1、光鸭网盘 1）",
        )
        self.assertEqual(
            SkipStatsCollector.format_notify(snapshot),
            "跳过 2 个候选 · 分享链接失效 1 个（115网盘）；无可用文件 1 个（光鸭网盘）",
        )


class TestTakeClears(unittest.TestCase):
    """3. take() 后清零（第二次 take() 为空）。"""

    def test_take_clears_records(self):
        collector = SkipStatsCollector()
        collector.record(
            SkipStatsCollector.KIND_INVALID, "115网盘", reason="分享已失效",
            reference="https://115.com/s/abc",
        )

        first = collector.take()
        self.assertEqual(first["total"], 1)
        second = collector.take()
        self.assertEqual(second["total"], 0)
        self.assertEqual(second["by_kind"], {})
        self.assertEqual(second["by_provider"], {})
        self.assertTrue(collector.is_empty())
        self.assertEqual(collector.summary_text(), "")

    def test_reset_clears_before_take(self):
        collector = SkipStatsCollector()
        collector.record(SkipStatsCollector.KIND_EMPTY, "光鸭网盘", reference="r1")
        collector.reset()
        self.assertEqual(collector.take()["total"], 0)

    def test_single_kind_summary_omits_other_kind(self):
        collector = SkipStatsCollector()
        collector.record(
            SkipStatsCollector.KIND_INVALID, "光鸭网盘", reference="https://guangyapan.com/s/a"
        )
        self.assertEqual(
            collector.summary_text(),
            "本次跳过 1 个候选资源：分享链接失效 1（光鸭网盘 1）",
        )


class TestEmptySummaryKeepsLegacyText(unittest.TestCase):
    """4. 无跳过记录时 summary_text() 返回 ""，通知正文与现状逐字一致。"""

    def test_summary_text_empty(self):
        collector = SkipStatsCollector()
        self.assertEqual(collector.summary_text(), "")
        self.assertEqual(collector.notify_text(), "")
        self.assertEqual(SkipStatsCollector.format_summary({}), "")
        self.assertEqual(SkipStatsCollector.format_summary(None), "")
        self.assertEqual(SkipStatsCollector.format_notify({}), "")
        self.assertEqual(SkipStatsCollector.format_notify(None), "")

    def test_completion_notify_identical_to_legacy(self):
        collector = SkipStatsCollector()
        owner = _StubOwner(notify=True, collector=collector)
        service = SyncExecutionService(owner)

        service._notify_sync_completed(
            "转存", 0, False, 0, collector.take()
        )

        self.assertEqual(len(owner.messages), 1)
        self.assertEqual(owner.messages[0]["title"], "✅ 网盘订阅助手 · 同步完成")
        self.assertEqual(
            owner.messages[0]["text"],
            "本次无需转存。",
        )

    def test_duplicate_empty_notify_suppressed_within_window(self):
        owner = _StubOwner(notify=True, collector=SkipStatsCollector())
        service = SyncExecutionService(owner)

        service._notify_sync_completed("转存", 0, False, 0, {})
        service._notify_sync_completed("转存", 0, False, 0, {})
        service._notify_sync_completed("洗版", 0, False, 0, {})

        self.assertEqual(len(owner.messages), 2)
        self.assertEqual(owner.messages[0]["text"], "本次无需转存。")
        self.assertEqual(owner.messages[1]["text"], "本次无需洗版。")

    def test_completion_notify_appends_skip_line(self):
        collector = SkipStatsCollector()
        collector.record(
            SkipStatsCollector.KIND_INVALID, "115网盘", reference="https://115.com/s/a"
        )
        owner = _StubOwner(notify=True, collector=collector)
        service = SyncExecutionService(owner)

        service._notify_sync_completed("转存", 0, False, 0, collector.take())

        self.assertEqual(len(owner.messages), 1)
        self.assertEqual(
            owner.messages[0]["text"],
            "本次无需转存。\n"
            "跳过 1 个候选 · 分享链接失效（115网盘）",
        )


class TestNotifyDisabled(unittest.TestCase):
    """5. _notify=False 时不发通知。"""

    def test_no_notification_when_notify_disabled(self):
        collector = SkipStatsCollector()
        collector.record(
            SkipStatsCollector.KIND_INVALID, "115网盘", reference="https://115.com/s/a"
        )
        owner = _StubOwner(notify=False, collector=collector)
        service = SyncExecutionService(owner)

        service._notify_sync_completed(
            "转存", 0, False, 0, collector.take()
        )

        self.assertEqual(owner.messages, [])

    def test_notify_disabled_without_skips(self):
        owner = _StubOwner(notify=False, collector=SkipStatsCollector())
        service = SyncExecutionService(owner)

        service._notify_sync_completed("转存", 0, False, 0, {})

        self.assertEqual(owner.messages, [])


class TestMissingCollector(unittest.TestCase):
    """6. 采集器缺失（_skip_stats 为 None）时主流程不报错。"""

    def test_collection_without_collector_is_silent(self):
        owner = _StubOwner(
            share_service=_StubShareService(invalid_urls={"https://115.com/s/dead"}),
            skip_stats_present=False,
        )
        service = ResourceTransferService(owner)
        share_url = "https://115.com/s/dead"

        self.assertFalse(service._validate_resource_url(share_url))
        self.assertFalse(
            service._record_skip_candidate(
                SkipStatsCollector.KIND_INVALID, share_url, reason="分享已失效"
            )
        )

    def test_empty_branch_without_collector_is_silent(self):
        owner = _StubOwner(
            share_service=_StubShareService(files=[]),
            skip_stats_present=False,
        )
        service = ResourceTransferService(owner)

        self.assertEqual(
            service._validated_resource_files(
                "https://www.guangyapan.com/s/xyz", resource_title="玉簟秋"
            ),
            [],
        )

    def test_sync_helpers_without_collector_are_silent(self):
        owner = _StubOwner(skip_stats_present=False)
        service = SyncExecutionService(owner)

        self.assertEqual(service._take_skip_stats(), {})
        service._reset_skip_stats()
        service._notify_sync_completed("转存", 0, False, 0, {})

        self.assertEqual(len(owner.messages), 1)
        self.assertEqual(
            owner.messages[0]["text"],
            "本次无需转存。",
        )


class TestCollectorConcurrency(unittest.TestCase):
    """线程安全：并发记录仍按 (kind, provider, reference) 去重。"""

    def test_concurrent_records_deduplicated(self):
        import threading

        collector = SkipStatsCollector()
        errors = []

        def worker():
            try:
                for _ in range(50):
                    collector.record(
                        SkipStatsCollector.KIND_INVALID,
                        "115网盘",
                        reference="https://115.com/s/abc",
                    )
            except Exception as error:  # pragma: no cover - 失败即用例失败
                errors.append(error)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(collector.take()["total"], 1)


# ---------------------------------------------------------------------------
# 第二轮：内容不匹配（mismatch）采集类型与 4 个新增采集点
# ---------------------------------------------------------------------------


def _read_plugin_source(relative_path: str) -> str:
    with open(os.path.join(REPO_ROOT, relative_path), encoding="utf-8") as handle:
        return handle.read()


TELEVISION_SOURCE = _read_plugin_source(
    "plugins.v2/cloudsubscribex/handlers/sync/television.py"
)
MOVIE_SOURCE = _read_plugin_source(
    "plugins.v2/cloudsubscribex/handlers/sync/movie.py"
)


class _ProbeLogger:
    """采集点探针 logger：只记录 debug 文案，warning/error 直接判失败。"""

    def __init__(self):
        self.debugs = []

    def debug(self, message):
        self.debugs.append(str(message))

    def info(self, message):
        raise AssertionError(f"采集点不得写 info：{message}")

    def warning(self, message):
        raise AssertionError(f"不得新增 warning：{message}")

    def error(self, message):
        raise AssertionError(f"不得新增 error：{message}")

    def exception(self, message):
        raise AssertionError(f"不得新增 exception：{message}")


def _find_branch(source: str, test_source: str):
    """按 if 条件表达式定位唯一分支（不依赖行号，源码位移不受影响）。"""
    tree = ast.parse(source)
    branches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and ast.unparse(node.test) == test_source
    ]
    if len(branches) != 1:
        raise AssertionError(
            f"期望唯一 {test_source!r} 分支，实际命中 {len(branches)} 个"
        )
    return branches[0]


def _branch_calls(branch, attr_name):
    return [
        node
        for node in ast.walk(branch)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr_name
    ]


def _assert_mismatch_wiring(case, branch):
    """结构断言：只按 KIND_MISMATCH + share_url + reason= 采集，debug 原样且不升级。"""
    records = _branch_calls(branch, "_record_skip_candidate")
    case.assertEqual(len(records), 1, "分支必须恰好采集一次")
    record = records[0]
    case.assertEqual(
        [ast.unparse(arg) for arg in record.args],
        ["SkipStatsCollector.KIND_MISMATCH", "share_url"],
    )
    case.assertTrue(
        any(keyword.arg == "reason" for keyword in record.keywords),
        "采集必须带 reason",
    )
    case.assertEqual(
        ast.unparse(branch.body[-1]), "continue", "采集后仍需 continue"
    )
    debugs = [
        call
        for call in _branch_calls(branch, "debug")
        if ast.unparse(call.func) == "logger.debug"
    ]
    case.assertGreaterEqual(len(debugs), 1, "原 logger.debug 必须原样保留")
    case.assertTrue(all(record.lineno > debug.lineno for debug in debugs))
    for level in ("warning", "error", "exception", "critical"):
        case.assertEqual(
            _branch_calls(branch, level), [], f"不得调用 logger.{level}"
        )


def _run_skip_branch(case, source: str, test_source: str, **namespace):
    """抽取真实分支源码，用最小 stub 触发一次；返回作用域 / 采集器 / 日志探针。"""
    branch = _find_branch(source, test_source)
    _assert_mismatch_wiring(case, branch)
    collector = SkipStatsCollector()
    probe_logger = _ProbeLogger()
    scope = {
        "self": ResourceTransferService(_StubOwner(collector=collector)),
        "logger": probe_logger,
        "SkipStatsCollector": SkipStatsCollector,
        # 候选账本明细会带上资源标题，未显式传入时给空串即可。
        "resource_title": "",
    }
    scope.update(namespace)
    loop_source = (
        "for _skip_probe in range(1):\n"
        + textwrap.indent(ast.unparse(branch), "    ")
        + "\n"
    )
    exec(compile(loop_source, "<skip-branch>", "exec"), scope)
    return scope, collector, probe_logger


def _assert_mismatch_recorded(case, collector, provider: str, reason: str) -> None:
    # take() 快照不含 reason，明细直接读采集器内部记录（仅测试读取）。
    reasons = [record["reason"] for record in collector._records.values()]
    snapshot = collector.take()
    case.assertEqual(snapshot["by_kind"], {SkipStatsCollector.KIND_MISMATCH: 1})
    case.assertEqual(snapshot["by_provider"], {provider: 1})
    case.assertEqual(reasons, [reason])


class TestThreeKindsOrder(unittest.TestCase):
    """7. 三类并存的计数与两段文案的类型顺序（失效 → 无可用文件 → 内容不匹配）。"""

    def test_mismatch_kind_registered(self):
        self.assertEqual(SkipStatsCollector.KIND_MISMATCH, "mismatch")
        self.assertEqual(SkipStatsCollector.kind_label("mismatch"), "内容不匹配")
        self.assertEqual(
            SkipStatsCollector.KIND_ORDER,
            (
                SkipStatsCollector.KIND_INVALID,
                SkipStatsCollector.KIND_EMPTY,
                SkipStatsCollector.KIND_MISMATCH,
            ),
        )

    def test_three_kinds_counted_in_kind_order(self):
        collector = SkipStatsCollector()
        # 故意先记 mismatch：文案顺序必须来自 KIND_ORDER，而非插入顺序。
        collector.record(
            SkipStatsCollector.KIND_MISMATCH, "夸克网盘",
            reference="https://pan.quark.cn/s/1",
        )
        collector.record(
            SkipStatsCollector.KIND_INVALID, "115网盘",
            reference="https://115.com/s/2",
        )
        collector.record(
            SkipStatsCollector.KIND_EMPTY, "光鸭网盘",
            reference="https://www.guangyapan.com/s/3",
        )

        snapshot = collector.take()
        self.assertEqual(snapshot["total"], 3)
        self.assertEqual(
            snapshot["by_kind"],
            {"invalid": 1, "empty": 1, "mismatch": 1},
        )
        self.assertEqual(
            snapshot["by_provider"],
            {"夸克网盘": 1, "115网盘": 1, "光鸭网盘": 1},
        )
        self.assertEqual(
            snapshot["by_kind_provider"],
            {
                SkipStatsCollector.KIND_MISMATCH: {"夸克网盘": 1},
                SkipStatsCollector.KIND_INVALID: {"115网盘": 1},
                SkipStatsCollector.KIND_EMPTY: {"光鸭网盘": 1},
            },
        )
        self.assertEqual(
            SkipStatsCollector.format_summary(snapshot),
            "本次跳过 3 个候选资源：分享链接失效 1、无可用文件 1、内容不匹配 1"
            "（夸克网盘 1、115网盘 1、光鸭网盘 1）",
        )
        self.assertEqual(
            SkipStatsCollector.format_notify(snapshot),
            "跳过 3 个候选 · 分享链接失效 1 个（115网盘）；无可用文件 1 个（光鸭网盘）；"
            "内容不匹配 1 个（夸克网盘）",
        )


class TestMismatchCollectionBranches(unittest.TestCase):
    """8. 4 个新增 mismatch 采集点：真实分支 + 最小 stub 触发即记录。"""

    def test_tv_share_without_season_episodes_recorded(self):
        scope, collector, probe = _run_skip_branch(
            self,
            TELEVISION_SOURCE,
            "video_count and (not share_episodes)",
            video_count=3,
            share_episodes=set(),
            is_cross_batch=False,
            mediainfo=SimpleNamespace(title_year="玉簟秋 (2024)"),
            season=3,
            share_url="https://115.com/s/tv-unmatched",
        )
        self.assertEqual(scope["reason"], "目标网盘分享未识别到目标季集数")
        self.assertEqual(
            probe.debugs,
            ["目标网盘分享未识别到目标季集数：玉簟秋 (2024) S03，已跳过该资源"],
        )
        _assert_mismatch_recorded(
            self, collector, "115网盘", "目标网盘分享未识别到目标季集数"
        )

        scope, collector, probe = _run_skip_branch(
            self,
            TELEVISION_SOURCE,
            "video_count and (not share_episodes)",
            video_count=3,
            share_episodes=set(),
            is_cross_batch=True,
            mediainfo=SimpleNamespace(title_year="玉簟秋 (2024)"),
            season=3,
            share_url="https://pan.quark.cn/s/tv-cross",
        )
        self.assertEqual(scope["reason"], "跨盘分享内容未被平台识别为目标媒体")
        self.assertEqual(
            probe.debugs,
            ["跨盘分享内容未被平台识别为目标媒体：玉簟秋 (2024) S03，已跳过该资源"],
        )
        _assert_mismatch_recorded(
            self, collector, "夸克网盘", "跨盘分享内容未被平台识别为目标媒体"
        )

    def test_tv_unmatched_missing_episodes_recorded(self):
        scope, collector, probe = _run_skip_branch(
            self,
            TELEVISION_SOURCE,
            "not matched_items",
            matched_items=[],
            resource_input_label="夸克网盘分享",
            season=3,
            share_url="https://115.com/s/tv-no-match",
        )
        self.assertEqual(
            probe.debugs,
            ["该夸克网盘分享未匹配到 S3 的任何缺失剧集，可能是季数不匹配或文件名无法识别"],
        )
        _assert_mismatch_recorded(
            self, collector, "115网盘", "未匹配到 S3 的缺失剧集"
        )

    def test_movie_magnet_priority_rule_recorded(self):
        scope, collector, probe = _run_skip_branch(
            self,
            MOVIE_SOURCE,
            "not matched",
            matched=False,
            provider_name="某某压制组",
            share_url="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567",
        )
        self.assertEqual(
            probe.debugs,
            ["Magnet 元数据未通过平台优先级规则：某某压制组"],
        )
        _assert_mismatch_recorded(
            self, collector, "磁力链接", "未通过平台优先级规则"
        )

    def test_movie_unmatched_file_recorded(self):
        scope, collector, probe = _run_skip_branch(
            self,
            MOVIE_SOURCE,
            "not matched_file",
            matched_file=None,
            require_media_match=True,
            mediainfo=SimpleNamespace(title_year="玉簟秋 (2024)"),
            share_url="https://www.guangyapan.com/s/mv-cross",
        )
        self.assertEqual(scope["reason"], "跨盘分享内容未被平台识别为目标电影")
        self.assertEqual(
            probe.debugs,
            ["跨盘分享内容未被平台识别为目标电影：玉簟秋 (2024)，已跳过该资源"],
        )
        _assert_mismatch_recorded(
            self, collector, "光鸭网盘", "跨盘分享内容未被平台识别为目标电影"
        )

        scope, collector, probe = _run_skip_branch(
            self,
            MOVIE_SOURCE,
            "not matched_file",
            matched_file=None,
            require_media_match=False,
            mediainfo=SimpleNamespace(title_year="玉簟秋 (2024)"),
            share_url="https://115.com/s/mv-none",
        )
        self.assertEqual(scope["reason"], "目标网盘分享未找到符合规则的电影文件")
        self.assertEqual(
            probe.debugs,
            ["目标网盘分享未找到符合规则的电影文件：玉簟秋 (2024)，已跳过该资源"],
        )
        _assert_mismatch_recorded(
            self, collector, "115网盘", "目标网盘分享未找到符合规则的电影文件"
        )


class TestCollectorViaPluginFallback(unittest.TestCase):
    """组件宿主是 SyncHandler 时，采集器挂在 owner._plugin 上也要能记录。"""

    def test_collector_reached_through_owner_plugin(self):
        collector = SkipStatsCollector()
        owner = _StubOwner(skip_stats_present=False)
        owner._plugin = SimpleNamespace(_skip_stats=collector)
        service = ResourceTransferService(owner)

        service._record_skip_candidate(
            SkipStatsCollector.KIND_MISMATCH,
            "https://115.com/s/through-plugin",
            reason="未匹配到 S3 的缺失剧集",
        )

        snapshot = collector.take()
        self.assertEqual(snapshot["by_kind"], {SkipStatsCollector.KIND_MISMATCH: 1})
        self.assertEqual(snapshot["by_provider"], {"115网盘": 1})

    def test_missing_collector_on_plugin_is_silent(self):
        owner = _StubOwner(skip_stats_present=False)
        owner._plugin = SimpleNamespace()
        service = ResourceTransferService(owner)

        self.assertIsNone(
            service._record_skip_candidate(
                SkipStatsCollector.KIND_MISMATCH,
                "https://115.com/s/none",
                reason="未通过平台优先级规则",
            )
        )


if __name__ == "__main__":
    unittest.main()
