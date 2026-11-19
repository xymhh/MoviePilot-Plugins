"""v1.26.1：SyncHandler 转发覆盖回归测试。

根因：v1.19.0 删掉 SyncHandler.__getattr__ 隐式委托、只补了 28 个显式
转发，漏了 7 个方法——get_due_offline_task_groups（离线监控每轮取分
组，无守卫直接抛 AttributeError；监控自 2026-11-18 19:31 起每轮崩，
下载页 95% 卡攒起）与 6 个 STRM/媒体方法（generate/clean_share_strm、
refresh_media_servers、migrate_legacy_strm、rebuild_strm_files、
rebuild_strm_increment_by_tree；__init__.py 里有 hasattr 守卫，缺转发
时悄悄返回“同步服务未就绪”，STRM 增量/全量补齐自此停摆）。离线监控
测试一直用手写 _FakeSyncHandler，从没在真实 SyncHandler 上解析过这些
方法，所以全套测试全绿也拦不住；v1.26.0 的 try/except 把崩溃写进插件
日志才暴露出来。

本文件两道防线：
1. 调用点扫描：静态扫 plugins.v2/cloudsubscribex 下所有
   sync_handler.<name>( 与 _sync_handler.<name>( 调用，逐个断言真实
   SyncHandler 类上有该属性——以后再漏转发，测试先红。
2. 逐个委托断言：7 个新转发确实把调用（含关键字参数）递到具名组件。
"""

from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache", "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)

install_core_delegation()
install_real_ttl_cache()
ensure_package("cloudsubscribex.core.sync_pause").is_paused = lambda *a, **k: False

_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_ROOT = os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")
CALL_RE = re.compile(r"(?:sync_handler|_sync_handler)\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")

# v1.19.0 漏掉的 7 个转发（v1.26.1 补上）。
MISSING_IN_V1_19_0 = (
    "get_due_offline_task_groups",
    "generate_share_strm",
    "clean_share_strm",
    "refresh_media_servers",
    "migrate_legacy_strm",
    "rebuild_strm_files",
    "rebuild_strm_increment_by_tree",
)

# （新转发方法，具名服务字段，位置参数，关键字参数）
# 类名与字段的对应关系由 test_explicit_composition_1_19_0 的
# COMPONENT_FIELDS 断言覆盖；这里只验证转发确实递到具名服务字段。
NEW_FORWARDERS = (
    ("clean_share_strm", "strm_ops",
        ("url",), {"save_path": "p"}),
    ("generate_share_strm", "strm_ops",
        ("url",), {"save_path": "p", "overwrite": True}),
    ("get_due_offline_task_groups", "postprocess",
        (), {"force": True, "pending_keys": {"k"}}),
    ("migrate_legacy_strm", "strm_ops",
        (), {"root": "r", "dry_run": True}),
    ("rebuild_strm_files", "strm_ops",
        (), {"overwrite": True, "time_budget_seconds": 30.0, "state_store": None}),
    ("rebuild_strm_increment_by_tree", "strm_ops",
        (), {"timeout_seconds": 60.0}),
    ("refresh_media_servers", "media_refresh",
        (), {}),
)


def _scan_callsites():
    """扫出插件代码里所有 sync_handler.<方法>( 调用点。"""
    names = {}
    for dirpath, dirnames, filenames in os.walk(PLUGIN_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    for match in CALL_RE.finditer(line):
                        names.setdefault(match.group(1), []).append(
                            f"{path}:{lineno}"
                        )
    return names


class ForwarderCoverageTests(unittest.TestCase):
    def test_all_sync_handler_callsites_resolve(self):
        """插件代码里所有 sync_handler.<方法>( 调用，真实类上必须有。"""
        names = _scan_callsites()
        self.assertGreater(len(names), 0, "调用点扫描不应为空")
        for name in MISSING_IN_V1_19_0:
            self.assertIn(name, names, f"v1.19.0 漏掉的调用点 {name} 不应消失")
        missing = sorted(n for n in names if not hasattr(SyncHandler, n))
        self.assertEqual(
            missing, [],
            "以下方法在插件代码里被 sync_handler 调用，但真实 SyncHandler "
            "类上没有（v1.19.0 式漏转发）：" + ", ".join(missing),
        )

    def test_new_forwarders_delegate_to_named_fields(self):
        """7 个新转发把调用（含关键字参数）递到具名服务字段。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        for method_name, field_name, args, kwargs in NEW_FORWARDERS:
            # 直接给具名服务字段挂桩：不经过组件类实例化（加载顺序不同时
            # COMPONENT_FIELDS 里的类可能是 mock，__name__ 会抛；委托本身
            # 只依赖“转发读 self.<字段>.<方法>”这一条）。
            stub = MagicMock(return_value=f"ok-{method_name}")
            component = SimpleNamespace(**{method_name: stub})
            object.__setattr__(handler, field_name, component)
            result = getattr(handler, method_name)(*args, **kwargs)
            self.assertEqual(result, f"ok-{method_name}")
            stub.assert_called_once_with(*args, **kwargs)
