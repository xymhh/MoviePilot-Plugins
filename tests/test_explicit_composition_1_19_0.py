"""Phase 5（v1.19.0）：SyncHandler 显式组合的回归测试。

删掉 ``SyncHandler.__getattr__`` + ``resolve_component`` 的隐式解析后：
- 26 个职责组件以 COMPONENT_FIELDS 的具名服务字段持有；
- 插件宿主与 core/api、core/hook 经 ``self._sync_handler`` 调用的 35 个
  组件方法，由 SyncHandler 上的显式转发方法承接（调用方不用改）；
- 组件间调用走 ``self.<字段>.<方法>``（组件内走 ``self._owner.<字段>.<方法>``）；
- ``_effective_mediainfo`` 的重名按调用点分流：MpOrganizeMixin 内用本类版本，
  其余位置用 SyncNamingService 版本。
"""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
    stub_component_method,
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

EXPECTED_FIELDS = {
    "movie", "television", "history_cleanup", "history_retry",
    "transfer_details", "media_targets", "file_matching", "postprocess",
    "upgrade_baseline", "upgrade_rule_scoring", "episode_dedup",
    "landed_verify", "resource_transfer", "subtitles", "upgrade",
    "pt_upgrade", "metadata", "naming", "notifications", "subscribe_flow",
    "platform_history", "media_refresh", "mp_organize", "strm_ops",
    "transfer_ops", "history_backfill",
}

# （转发方法，具名服务字段，组件类名，调用参数）
# 只选本用例里真实加载的组件（media_refresh）；其余转发由全量套件覆盖。
FORWARDERS = (
    ("begin_notification_batch", "media_refresh", "MediaRefreshMixin", ()),
    ("finish_notification_batch", "media_refresh", "MediaRefreshMixin", ()),
    (
        "update_notification_config",
        "media_refresh",
        "MediaRefreshMixin",
        (True, "x", True, ["emby"], "{}", 0, False),
    ),
)


class ExplicitCompositionTests(unittest.TestCase):
    def test_no_implicit_getattr_on_sync_handler(self):
        self.assertNotIn("__getattr__", vars(SyncHandler))
        source = open(
            os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribex/handlers/sync/service.py"),
            encoding="utf-8",
        ).read()
        # 注释里仍会提到 resolve_component（说明删掉了它）；只断言代码里没有
        # 导入与调用。
        self.assertNotIn("resolve_component,", source)
        self.assertNotRegex(source, r"resolve_component\s*\(")

    def test_component_fields_cover_all_services(self):
        # 本用例里其余组件被 Mock 顶替，只断言字段名集合与个数；
        # “是真实类”由 test_component_wiring 与全量套件覆盖。
        fields = dict(_service_module.COMPONENT_FIELDS)
        self.assertEqual(set(fields), EXPECTED_FIELDS)
        self.assertEqual(len(_service_module.COMPONENT_FIELDS), 26)
        for field_name, component_type in _service_module.COMPONENT_FIELDS:
            self.assertIsNotNone(component_type, field_name)

    def test_forwarders_delegate_to_named_fields(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        for method_name, field_name, class_name, args in FORWARDERS:
            component_type = next(
                cls for f, cls in _service_module.COMPONENT_FIELDS if f == field_name
            )
            self.assertEqual(component_type.__name__, class_name)
            stub = MagicMock(return_value=f"ok-{method_name}")
            stub_component_method(handler, component_type, method_name, stub)
            self.assertIs(getattr(handler, field_name)._owner, handler)
            result = getattr(handler, method_name)(*args)
            self.assertEqual(result, f"ok-{method_name}")
            stub.assert_called_once_with(*args)

    def test_effective_mediainfo_split_by_callsite(self):
        """重名方法按调用点分流：mp_organize 用本类版本，其余用 naming 版本。"""
        sync_root = os.path.join(
            REPO_ROOT, "plugins.v2/cloudsubscribex/handlers/sync",
        )
        mp_source = open(os.path.join(sync_root, "mp_organize.py"), encoding="utf-8").read()
        self.assertIn("self._effective_mediainfo(", mp_source)
        self.assertNotIn("self._owner.naming._effective_mediainfo(", mp_source)
        details_source = open(
            os.path.join(sync_root, "transfer_details.py"), encoding="utf-8",
        ).read()
        self.assertIn("self._owner.naming._effective_mediainfo(", details_source)
        self.assertNotIn("self._effective_mediainfo(", details_source)


if __name__ == "__main__":
    unittest.main()
