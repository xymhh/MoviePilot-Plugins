"""职责组件装配回归：拆分后方法挂在组件类上，靠 ``SyncHandler.__getattr__`` 委托解析。

历史事故（v1.9.20 / v1.9.21）：``MediaRefreshMixin`` / ``StrmOpsMixin`` 忘了继承
``OwnerDelegator``，而 ``get_component`` 用 ``component_type(owner)`` 实例化 →
``TypeError: object() takes no arguments``，整条媒体库刷新 / STRM 链路在生产上直接抛异常。
单测只覆盖方法体，覆盖不到装配，所以把装配本身钉死在用例里。

本用例只针对拆分出来的三个职责类（``transfer_ops`` / ``media_refresh`` / ``strm_ops``）：
其余组件在测试环境里被 Mock 顶替，断言它们「是类」只会测到替身本身。
"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

from plugin_env import (
    AutoMockModule,
    ensure_package,
    install_app_mocks,
    install_core_delegation,
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
ensure_package("cloudsubscribex.core.sync_pause").is_paused = lambda *a, **k: False

_service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)
SyncHandler = _service_module.SyncHandler
OwnerDelegator = sys.modules["cloudsubscribex.core"].OwnerDelegator

SPLIT_MODULES = ("transfer_ops", "media_refresh", "strm_ops")


def _split_classes():
    """拆分出来的职责类：模块必须是真实加载的（不是占位包）。"""
    classes = []
    for name in SPLIT_MODULES:
        module = sys.modules[f"cloudsubscribex.handlers.sync.{name}"]
        if isinstance(module, AutoMockModule):
            raise AssertionError(f"{name} 被占位包顶替，装配测试失去意义")
        classes.append((name, module))
    return classes


class ComponentWiringTests(unittest.TestCase):
    def test_split_modules_load_real_classes(self):
        for name, module in _split_classes():
            with self.subTest(module=name):
                component_type = next(
                    value for value in vars(module).values()
                    if isinstance(value, type) and issubclass(value, OwnerDelegator)
                )
                self.assertTrue(issubclass(component_type, OwnerDelegator))

    def test_every_split_component_instantiates_with_owner(self):
        """``cls(owner)`` 必须成立：get_component 就是这样创建组件的。"""
        owner = SimpleNamespace()
        for name, module in _split_classes():
            component_type = next(
                value for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, OwnerDelegator)
            )
            with self.subTest(component=component_type.__name__):
                component = component_type(owner)
                self.assertIs(component._owner, owner)

    def test_split_components_delegate_state_to_owner(self):
        owner = SimpleNamespace(_shared_value=1)
        for name, module in _split_classes():
            component_type = next(
                value for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, OwnerDelegator)
            )
            with self.subTest(component=component_type.__name__):
                component = component_type(owner)
                self.assertEqual(component._shared_value, 1)
                component._written_by_component = name
                self.assertEqual(owner._written_by_component, name)

    def test_every_split_method_resolves_through_handler(self):
        """职责类上定义的每个方法，都必须能经 SyncHandler 委托取到。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        for name, module in _split_classes():
            component_type = next(
                value for value in vars(module).values()
                if isinstance(value, type) and issubclass(value, OwnerDelegator)
            )
            for attribute, value in vars(component_type).items():
                if attribute.startswith("__") or not callable(value):
                    continue
                with self.subTest(component=component_type.__name__, method=attribute):
                    resolved = getattr(handler, attribute)
                    self.assertIsInstance(resolved.__self__, component_type)

    def test_component_cache_is_reused(self):
        """同一宿主同一组件只实例化一次，组件内的状态缓存才不会丢。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        first = getattr(handler, "_transfer_file")
        second = getattr(handler, "_transfer_file")
        self.assertIs(first.__self__, second.__self__)

    def test_staging_refcount_state_is_shared(self):
        """引用计数只有一份：``service`` 的再导出必须与 ``transfer_ops`` 同一对象。"""
        transfer_ops = sys.modules["cloudsubscribex.handlers.sync.transfer_ops"]
        self.assertIs(_service_module._STAGING_REFS, transfer_ops._STAGING_REFS)
        self.assertIs(_service_module._STAGING_REFS_LOCK, transfer_ops._STAGING_REFS_LOCK)

    def test_stub_component_method_shadows_class_method(self):
        """测试打桩工具必须真盖住类方法（拆分后给宿主赋值已经盖不住）。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        transfer_ops = sys.modules["cloudsubscribex.handlers.sync.transfer_ops"]
        sentinel = object()
        stub_component_method(
            handler, transfer_ops.TransferOpsMixin, "_transfer_file", sentinel,
        )
        self.assertIs(getattr(handler, "_transfer_file"), sentinel)


if __name__ == "__main__":
    unittest.main()
