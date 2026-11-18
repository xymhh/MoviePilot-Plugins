"""MoviePilot 插件单测的公共 mock 与模块加载工具。

测试环境缺少 MoviePilot 运行时（``app.*``）依赖，各测试文件此前需要重复声明
``AutoMockModule``/``_ensure_mock_package``/``OwnerDelegator`` 并手写 importlib 加载。
本模块把这些公共逻辑集中一处：

* :class:`AutoMockModule` / :func:`ensure_package` / :func:`install_app_mocks`
  —— 用属性即返回 ``MagicMock`` 的占位模块补齐 ``app.*`` 依赖；
* :class:`OwnerDelegator` —— 与插件运行期一致的职责委托基类；
* :func:`set_media_type` —— 注入 ``app.schemas.types.MediaType`` 桩；
* :func:`load_module` —— 按仓库相对路径加载插件模块并保持包层级以支持相对导入。
"""

import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

# tests/ 的上级目录即仓库根目录
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AutoMockModule(types.ModuleType):
    """未定义属性自动返回 ``MagicMock`` 的占位模块。"""

    def __getattr__(self, name):
        value = MagicMock()
        setattr(self, name, value)
        return value


PLUGIN_ROOT = os.path.join(REPO_ROOT, "plugins.v2")


def _bind_real_path(module, parts: list[str]) -> None:
    """占位包的 __path__ 指向真实目录，使其下所有兄弟模块都能被正常 import。

    否则 ``handlers.sync`` 这类占位包 ``__path__ == []``，service.py 里任何
    新增的 ``from .xxx import`` 都会找不到模块（拆分重构踩过：新增
    media_refresh / strm_ops 后 4 个用例直接 import 失败）。
    """
    if not parts or parts[0] != "cloudsubscribex":
        return
    directory = os.path.join(PLUGIN_ROOT, *parts)
    if os.path.isdir(directory):
        module.__path__ = [directory]


def get_component(owner, component_type, cache_name: str):
    """与 ``core/delegation.py`` 等价：按类型创建并缓存职责组件。"""
    components = owner.__dict__.setdefault(cache_name, {})
    component = components.get(component_type)
    if component is None:
        component = component_type(owner)
        components[component_type] = component
    return component


def resolve_component(owner, component_types, name: str, cache_name: str):
    """与 ``core/delegation.py`` 等价：从职责组件中解析宿主未直接定义的属性。"""
    for component_type in component_types:
        if not isinstance(component_type, type):
            continue
        if hasattr(component_type, name):
            return getattr(get_component(owner, component_type, cache_name), name)
    raise AttributeError(
        f"{owner.__class__.__name__!s} object has no attribute {name!r}"
    )


def _component_field_name(component_type):
    """Phase 5（v1.19.0）：组件类型 -> SyncHandler 具名服务字段名。

    优先读已加载的 service 模块的 COMPONENT_FIELDS；模块未加载时按
    组件类的模块名 + 类名查后备映射；都查不到返回 None，调用方退回
    只写 _handler_components 缓存的旧行为。
    """
    module = sys.modules.get("cloudsubscribex.handlers.sync.service")
    fields = getattr(module, "COMPONENT_FIELDS", None) if module is not None else None
    if fields:
        for field_name, cls in fields:
            if cls is component_type:
                return field_name
    for field_name, (mod_name, cls_name) in _FIELD_MODULE.items():
        if (
            getattr(component_type, "__module__", None) == mod_name
            and getattr(component_type, "__name__", None) == cls_name
        ):
            return field_name
    return None


def _service_component_fields():
    """service.COMPONENT_FIELDS 读成 {字段名: 组件类}；未加载时为空。"""
    module = sys.modules.get("cloudsubscribex.handlers.sync.service")
    fields = getattr(module, "COMPONENT_FIELDS", None) if module is not None else None
    return dict(fields or ())


# service 模块未加载时的后备映射：字段名 -> （组件模块，类名）。
# 只在调用方已真实加载对应组件模块时可用（占位 Mock 顶替时不保证）。
_FIELD_MODULE = {
    "movie": ("cloudsubscribex.handlers.sync.movie", "MovieSyncProcessor"),
    "television": ("cloudsubscribex.handlers.sync.television", "TelevisionSyncProcessor"),
    "history_cleanup": ("cloudsubscribex.handlers.sync.cleanup", "HistoryCleanupService"),
    "history_retry": ("cloudsubscribex.handlers.sync.retry", "HistoryRetryService"),
    "transfer_details": ("cloudsubscribex.handlers.sync.transfer_details", "TransferDetailsService"),
    "media_targets": ("cloudsubscribex.handlers.sync.media_targets", "MediaTargetsService"),
    "file_matching": ("cloudsubscribex.handlers.sync.matching", "FileMatchingService"),
    "postprocess": ("cloudsubscribex.handlers.sync.postprocess", "PostprocessService"),
    "upgrade_baseline": ("cloudsubscribex.handlers.sync.baseline", "UpgradeBaselineService"),
    "upgrade_rule_scoring": ("cloudsubscribex.handlers.sync.rule_scoring", "UpgradeRuleScoringService"),
    "episode_dedup": ("cloudsubscribex.handlers.sync.episode_dedup", "EpisodeDedupService"),
    "landed_verify": ("cloudsubscribex.handlers.sync.landed", "LandedVerifyService"),
    "resource_transfer": ("cloudsubscribex.handlers.sync.resources", "ResourceTransferService"),
    "subtitles": ("cloudsubscribex.handlers.sync.subtitles", "SubtitleService"),
    "upgrade": ("cloudsubscribex.handlers.sync.upgrade", "UpgradeService"),
    "pt_upgrade": ("cloudsubscribex.handlers.sync.pt_upgrade", "PtUpgradeService"),
    "metadata": ("cloudsubscribex.handlers.sync.metadata", "SyncMetadataService"),
    "naming": ("cloudsubscribex.handlers.sync.naming", "SyncNamingService"),
    "notifications": ("cloudsubscribex.handlers.sync.notify", "SyncNotificationService"),
    "subscribe_flow": ("cloudsubscribex.handlers.sync.subscribe_flow", "SubscribeFlowService"),
    "platform_history": ("cloudsubscribex.handlers.sync.platform_history", "PlatformHistoryService"),
    "media_refresh": ("cloudsubscribex.handlers.sync.media_refresh", "MediaRefreshMixin"),
    "mp_organize": ("cloudsubscribex.handlers.sync.mp_organize", "MpOrganizeMixin"),
    "strm_ops": ("cloudsubscribex.handlers.sync.strm_ops", "StrmOpsMixin"),
    "transfer_ops": ("cloudsubscribex.handlers.sync.transfer_ops", "TransferOpsMixin"),
}


def _resolve_component_class(field_name):
    """字段名 -> 组件类：优先取 sys.modules 里当前加载的组件模块。

    测试文件常在 service 之后重载某个组件模块（load_module 会换掉
    sys.modules 里的模块对象）；service.COMPONENT_FIELDS 仍指向第一次
    加载的类，其模块级绑定（如 MetaInfo）是旧的。取当前模块的类，
    与测试直接调用的类是同一份。组件模块尚未加载时才退回
    service.COMPONENT_FIELDS。
    """
    mod_name, cls_name = _FIELD_MODULE[field_name]
    module = sys.modules.get(mod_name)
    if module is not None:
        return getattr(module, cls_name)
    fields = _service_component_fields()
    if field_name in fields:
        return fields[field_name]
    raise ImportError(
        f"组件模块 {mod_name} 未加载，无法解析具名服务字段 {field_name!r}"
    )


def materialize_handler_fields(handler, *field_names):
    """Phase 5：给 __new__ 造的 SyncHandler 补建具名服务字段（与 __init__ 同款）。

    每个字段是真实组件实例（owner=handler），与 _handler_components 缓存
    指向同一实例；不传字段名时补建全部 25 个。
    """
    names = field_names or tuple(_FIELD_MODULE)
    for field_name in names:
        component_type = _resolve_component_class(field_name)
        component = component_type(handler)
        object.__setattr__(handler, field_name, component)
        handler.__dict__.setdefault("_handler_components", {})[component_type] = component
    return handler


def migrate_handler_stubs(handler):
    """Phase 5 迁移：把 __new__ handler 实例字典里的方法桩搬到具名服务字段上。

    旧测试把跨组件方法桩直接赋在 handler 实例上（``handler._xxx = fake``），
    运行期经 OwnerDelegator → 宿主实例字典取到；显式组合后生产代码改走
    ``handler.<字段>.<方法>``，桩必须落在组件实例自己的 ``__dict__`` 才生效。
    只搬「恰好是某个组件方法名」的桩（按 COMPONENT_FIELDS 顺序取首个定义者，
    与旧 resolve_component 顺序一致）；普通配置属性与宿主自有方法不动，
    桩本身保留在 handler 字典里（直接调 handler._xxx 的旧写法仍可用）。
    """
    method_to_field = {}
    for field_name in _FIELD_MODULE:
        try:
            component_type = _resolve_component_class(field_name)
        except ImportError:
            continue
        for name in vars(component_type):
            if name.startswith("__"):
                continue
            if callable(getattr(component_type, name, None)):
                method_to_field.setdefault(name, field_name)
    for name in list(handler.__dict__):
        field_name = method_to_field.get(name)
        if field_name is None:
            continue
        if field_name not in handler.__dict__:
            materialize_handler_fields(handler, field_name)
        object.__setattr__(getattr(handler, field_name), name, handler.__dict__[name])
    return handler


def attach_component_fields(owner, host, *field_names):
    """Phase 5：给裸组件实例的假 owner 挂上具名服务字段（与运行期等价）。

    测试里裸造的组件实例（``EpisodeDedupService.__new__`` 等）用
    SimpleNamespace 当 _owner；运行期 SyncHandler.__init__ 会建的字段，
    在这里按需补建：每个字段是真实组件实例（owner=host），挂到 owner 上，
    并同步进 host 的 _handler_components 缓存（stub_component_method 可用）。
    """
    for field_name in field_names:
        component_type = _resolve_component_class(field_name)
        component = component_type(host)
        setattr(owner, field_name, component)
        host.__dict__.setdefault("_handler_components", {})[component_type] = component
    return owner


def stub_field_method(owner, field_name, method_name, value):
    """Phase 5：在具名服务字段的组件实例上打桩（字段不存在时先补建）。"""
    component = getattr(owner, field_name, None)
    if component is None:
        raise AttributeError(f"具名服务字段 {field_name!r} 不存在，先调 attach/materialize 补建")
    object.__setattr__(component, method_name, value)
    return component


def stub_component_method(owner, component_type, name: str, value):
    """在职责组件实例上打桩，模拟拆分前「方法就在宿主类上」的旧结构。

    拆分后方法挂在职责类上，直接给宿主赋值同名属性不会生效（类属性优先于
    ``__getattr__`` 委托）；必须写进组件实例自己的 ``__dict__``。

    Phase 5（v1.19.0）：业务代码改走 handler.<具名服务字段> 取组件；
    若打桩时现场新建了组件实例，同步把具名服务字段指到同一实例，
    否则 __new__ 造的 handler 上字段不存在，打桩就够不着。
    """
    components = owner.__dict__.setdefault("_handler_components", {})
    component = components.get(component_type)
    if component is None:
        component = component_type(owner)
        components[component_type] = component
        field_name = _component_field_name(component_type)
        if field_name is not None and field_name not in owner.__dict__:
            object.__setattr__(owner, field_name, component)
    object.__setattr__(component, name, value)
    return component


def install_real_ttl_cache() -> None:
    """给 ``app.core.cache.TTLCache`` 注册一个可无限实例化的真实类。

    ``utils/cache.py`` 的 ``_PlatformTTLCache(TTLCache)`` 以它为基类，
    并在多处模块级代码里调用 ``create_platform_ttl_cache``。TTLCache
    若只是 MagicMock，第一次实例化后第二次就 ``StopIteration``（Mock
    以 spec='str' 当基类时的行为）。全量套件里靠 test_cache_json_codec
    先注册了真实类才不爆；单文件运行时若尚未注册，在这里补一个最小
    dict 实现。已注册真实类时不覆盖。
    """
    existing = sys.modules.get("app.core.cache")
    current = getattr(existing, "TTLCache", None) if existing is not None else None
    if isinstance(current, type):
        return None

    class _DictTTLCache(dict):
        def __init__(self, region=None, maxsize=None, ttl=None, **kwargs):
            super().__init__()
            self.region = region
            self.maxsize = maxsize
            self.ttl = ttl

        def set(self, key, value):
            self[key] = value

    register_module("app.core.cache", TTLCache=_DictTTLCache)
    return None


def install_core_delegation() -> None:
    """把真实委托机制装进占位包 ``cloudsubscribex.core``。

    拆分后的方法都挂在职责组件上，走 ``SyncHandler.__getattr__`` → 委托解析；
    占位包若不装这两个函数，解析就是个 MagicMock，调用被静默吞掉。
    """
    core = ensure_package("cloudsubscribex.core")
    core.OwnerDelegator = OwnerDelegator
    core.get_component = get_component
    core.resolve_component = resolve_component


def ensure_package(name: str) -> AutoMockModule:
    """确保 ``name`` 已注册为占位包（保留 ``__path__`` 与既有属性）。"""
    existing = sys.modules.get(name)
    if isinstance(existing, AutoMockModule):
        _bind_real_path(existing, name.split("."))
        return existing
    module = AutoMockModule(name)
    module.__path__ = []
    # 插件自己的包若在磁盘上真实存在，__path__ 指向真实目录：
    # 否则 service.py 及其兄弟模块里的 ``from ...utils.xxx import`` 一律
    # ModuleNotFoundError（拆分新增模块时必踩，靠测试顺序碰巧通过不算数）。
    _bind_real_path(module, name.split("."))
    if existing is not None:
        # 已被其它测试加载过的模块，保留其公开属性避免重复加载时丢失真实实现。
        for key, value in list(vars(existing).items()):
            if not key.startswith("_"):
                setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is not None:
        setattr(parent, child, module)
    return module


def install_app_mocks(*extra_packages: str) -> None:
    """注册 ``app.*`` 占位包；``extra_packages`` 可追加项目内的占位包。"""
    for name in ("app", "app.log", "app.schemas", "app.schemas.types"):
        ensure_package(name)
    for name in extra_packages:
        ensure_package(name)
    install_core_failure()
    install_core_state()


def install_core_state():
    """加载真实的 ``core.state``，供各 handler 的状态迁移导入。"""
    existing = sys.modules.get("cloudsubscribex.core.state")
    if existing is not None:
        return existing
    ensure_package("cloudsubscribex")
    ensure_package("cloudsubscribex.core")
    return load_module(
        "cloudsubscribex.core.state",
        "plugins.v2/cloudsubscribex/core/state.py",
    )


def install_core_failure():
    """加载真实的 ``core.failure``。

    ``handlers.sync.history`` / ``retry`` / ``movie`` / ``television`` 都会
    相对导入这个模块；测试只给 ``cloudsubscribex.core`` 注册了空占位包，
    子模块不存在时这些模块加载即 ImportError。这里统一补上，避免每个用例
    文件重复声明（与 ``core.media`` 等显式占位不同，它没有任何外部依赖）。
    """
    existing = sys.modules.get("cloudsubscribex.core.failure")
    if existing is not None:
        return existing
    ensure_package("cloudsubscribex")
    ensure_package("cloudsubscribex.core")
    return load_module(
        "cloudsubscribex.core.failure",
        "plugins.v2/cloudsubscribex/core/failure.py",
    )


class OwnerDelegator:
    """职责对象状态委托（等价于 ``core/delegation.py``，``owner`` 可缺省）。"""

    def __init__(self, owner=None):
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        owner = self.__dict__.get("_owner")
        if owner is None:
            return None
        return getattr(owner, name)

    def __setattr__(self, name, value):
        owner = self.__dict__.get("_owner")
        if name == "_owner" or owner is None:
            # 没有宿主（测试里直接子类化、未调 super().__init__）时退化为普通属性
            object.__setattr__(self, name, value)
            return
        setattr(owner, name, value)


def _member(value):
    class _EnumMember:
        def __init__(self, raw):
            self.value = raw

        def __str__(self):
            return str(self.value)

        def __eq__(self, other):
            return other == self.value or other is self

        def __hash__(self):
            return hash(self.value)

    return _EnumMember(value)


def set_media_type(movie: str = "movie", tv: str = "tv"):
    """注入 ``app.schemas.types.MediaType`` 桩并返回该类。

    成员同时具备 ``.value`` 与字符串等价比较，兼容 ``MediaType.TV.value`` 和
    ``MediaType.TV == "电视剧"`` 两种写法。
    """
    media_type = type("MediaType", (), {
        "MOVIE": _member(movie),
        "TV": _member(tv),
    })
    ensure_package("app.schemas.types").MediaType = media_type
    return media_type


def _ensure_parents(name: str) -> None:
    parts = name.split(".")[:-1]
    for index in range(1, len(parts) + 1):
        parent = ".".join(parts[:index])
        module = sys.modules.get(parent)
        if module is None:
            module = ensure_package(parent)
        _bind_real_path(module, parts[:index])


def _register(name: str, module) -> None:
    _ensure_parents(name)
    sys.modules[name] = module
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name) if parent_name else None
    if parent is not None:
        setattr(parent, child, module)


def register_module(name: str, *, package: bool = False, **attrs):
    """创建并注册真实 ``types.ModuleType`` 占位模块，``attrs`` 为预置属性。"""
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    _register(name, module)
    return module


def register_mock(name: str, **attrs):
    """创建并注册 ``MagicMock`` 占位模块，未预置属性访问时自动返回 Mock。"""
    module = MagicMock()
    module.__path__ = []
    for key, value in attrs.items():
        setattr(module, key, value)
    _register(name, module)
    return module


def register_package(name: str, paths=(), **attrs):
    """注册占位包；``paths`` 非空时作为真实 ``__path__`` 供相对导入使用。"""
    module = ensure_package(name)
    if paths:
        module.__path__ = list(paths)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def load_module(module_name: str, relative_path: str):
    """加载仓库内插件模块；保持包层级以支持其相对导入。"""
    path = relative_path
    if not os.path.isabs(path):
        path = os.path.join(REPO_ROOT, relative_path)
    _ensure_parents(module_name)
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = module_name.rsplit(".", 1)[0]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
