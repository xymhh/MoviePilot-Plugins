"""手动/频道转存的洗版判定（1.7.112）。

线上背景（2026-10-30）：``core/api/sync.py`` 对订阅绑定的手动提交无条件注入
``manual_upgrade=True``，导致洗版开关关闭后 Telegram/手动转存仍然整季重建基线。
修复后只有「订阅属于洗版范围」才注入 True，其余退回普通转存（只补缺集）。
"""

import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.core", "app.core.metainfo", "app.db", "app.db.subscribe_oper", "app.log",
)
MediaType = set_media_type()

ensure_package("cloudsubscribex")
_core = ensure_package("cloudsubscribex.core")
_core.OwnerDelegator = OwnerDelegator
for _attr in ("CloudDriveCapability", "SearchCapability"):
    setattr(_core, _attr, MagicMock())

register_module(
    "cloudsubscribex.core.media",
    media_identity=MagicMock(),
    recognize_media=MagicMock(),
    tmdb_id_of=MagicMock(),
)
register_module("cloudsubscribex.search.matching", positive_ints=MagicMock())
register_module(
    "cloudsubscribex.search.types",
    normalize_resource_type=MagicMock(),
    resource_type_from_text=MagicMock(),
    resource_type_from_url=MagicMock(),
    resource_type_name=MagicMock(),
)
register_module(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)
ensure_package("cloudsubscribex.utils")

_sync_api = load_module(
    "cloudsubscribex.core.api.sync",
    "plugins.v2/cloudsubscribex/core/api/sync.py",
)
SyncApi = _sync_api.SyncApi


class _ScopeOwner:
    """只提供 _is_cloud_upgrade_subscribe 的最小宿主体。"""

    def __init__(self, decision):
        self.decision = decision

    def _is_cloud_upgrade_subscribe(self, subscribe):
        if isinstance(self.decision, Exception):
            raise self.decision
        return self.decision


class _NoScopeOwner:
    """宿主未实现洗版范围判定（例如旧版本热重载残留）。"""


class ManualSyncUpgradeScopeTest(unittest.TestCase):
    def test_in_scope_subscribe_injects_upgrade(self):
        api = SyncApi(_ScopeOwner(True))
        self.assertIs(api._manual_upgrade_for_subscribe(object()), True)

    def test_out_of_scope_subscribe_falls_back_to_normal_transfer(self):
        """洗版开关关闭 / 订阅未启用 best_version 时必须退回普通转存。"""
        api = SyncApi(_ScopeOwner(False))
        self.assertIs(api._manual_upgrade_for_subscribe(object()), False)

    def test_missing_checker_never_force_upgrade(self):
        """宿主没有判定能力时保守选择普通转存，不能再退回强制洗版。"""
        api = SyncApi(_NoScopeOwner())
        self.assertIs(api._manual_upgrade_for_subscribe(object()), False)

    def test_checker_error_never_breaks_submit(self):
        api = SyncApi(_ScopeOwner(RuntimeError("boom")))
        self.assertIs(api._manual_upgrade_for_subscribe(object()), False)


if __name__ == "__main__":
    unittest.main()
