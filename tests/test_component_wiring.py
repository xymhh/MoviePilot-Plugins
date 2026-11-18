"""职责组件装配回归：拆分后方法挂在组件类上，经具名服务字段显式持有。

历史事故（v1.9.20 / v1.9.21）：``MediaRefreshMixin`` / ``StrmOpsMixin`` 忘了继承
``OwnerDelegator``，而 ``get_component`` 用 ``component_type(owner)`` 实例化 →
``TypeError: object() takes no arguments``，整条媒体库刷新 / STRM 链路在生产上直接抛异常。
单测只覆盖方法体，覆盖不到装配，所以把装配本身钉死在用例里。

Phase 5（v1.19.0）：``SyncHandler.__getattr__`` + ``resolve_component`` 的隐式
解析已删；25 个组件以 COMPONENT_FIELDS 的具名服务字段持有，
与 ``_handler_components`` 缓存指向同一实例。本用例只针对拆分出来的三个
职责类（``transfer_ops`` / ``media_refresh`` / ``strm_ops``）：
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
    install_real_ttl_cache,
    load_module,
    materialize_handler_fields as _materialize_fields,
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


def _split_component_type(module):
    """模块内拆分出的职责类（排除导入进来的 OwnerDelegator 基类本身）。"""
    return next(
        value for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, OwnerDelegator)
        and value is not OwnerDelegator
    )


def _field_of(component_type):
    """组件类 -> 具名服务字段名：按类名查（模块重载后类对象会换，类名不变）。
    COMPONENT_FIELDS 里被占位 Mock 顶替的条目没有 __name__，跳过。"""
    for field_name, cls in _service_module.COMPONENT_FIELDS:
        try:
            cls_name = cls.__name__
        except AttributeError:
            continue
        if cls_name == component_type.__name__:
            return field_name
    raise KeyError(component_type)


class ComponentWiringTests(unittest.TestCase):
    def test_split_modules_load_real_classes(self):
        for name, module in _split_classes():
            with self.subTest(module=name):
                component_type = _split_component_type(module)
                self.assertTrue(issubclass(component_type, OwnerDelegator))

    def test_every_split_component_instantiates_with_owner(self):
        """``cls(owner)`` 必须成立：get_component 就是这样创建组件的。"""
        owner = SimpleNamespace()
        for name, module in _split_classes():
            component_type = _split_component_type(module)
            with self.subTest(component=component_type.__name__):
                component = component_type(owner)
                self.assertIs(component._owner, owner)

    def test_split_components_delegate_state_to_owner(self):
        owner = SimpleNamespace(_shared_value=1)
        for name, module in _split_classes():
            component_type = _split_component_type(module)
            with self.subTest(component=component_type.__name__):
                component = component_type(owner)
                self.assertEqual(component._shared_value, 1)
                component._written_by_component = name
                self.assertEqual(owner._written_by_component, name)

    def test_every_split_method_resolves_through_named_field(self):
        """职责类上定义的每个方法，都必须能经具名服务字段取到；宿主不再隐式解析。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        for name, module in _split_classes():
            component_type = _split_component_type(module)
            field_name = _field_of(component_type)
            _materialize_fields(handler, field_name)
            component = getattr(handler, field_name)
            for attribute, value in vars(component_type).items():
                if attribute.startswith("__") or not callable(value):
                    continue
                with self.subTest(component=component_type.__name__, method=attribute):
                    resolved = getattr(component, attribute)
                    if isinstance(value, staticmethod):
                        # 静态方法不绑定实例：经字段取到的是类上的同一个函数。
                        self.assertIs(resolved, value.__func__)
                    else:
                        self.assertIsInstance(resolved.__self__, component_type)
                    if attribute not in vars(SyncHandler):
                        self.assertIsNone(getattr(handler, attribute, None))

    def test_component_cache_is_reused(self):
        """同一宿主同一组件只实例化一次，组件内的状态缓存才不会丢。"""
        handler = SyncHandler.__new__(SyncHandler)
        handler._plugin = SimpleNamespace()
        _materialize_fields(handler, "transfer_ops")
        first = handler.transfer_ops
        second = handler.transfer_ops
        self.assertIs(first, second)
        transfer_ops = sys.modules["cloudsubscribex.handlers.sync.transfer_ops"]
        self.assertIs(
            handler.__dict__["_handler_components"][transfer_ops.TransferOpsMixin],
            first,
        )
        self.assertIs(first._transfer_file.__self__, first)

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
        self.assertIs(handler.transfer_ops._transfer_file, sentinel)
        # 宿主不再隐式解析同名方法
        self.assertIsNone(getattr(handler, "_transfer_file", None))


if __name__ == "__main__":
    unittest.main()
