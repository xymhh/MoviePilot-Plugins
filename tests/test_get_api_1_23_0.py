"""v1.23.0（§11 前端收敛第 8 项）：get_api() 注册表回归测试。

断言三件事：

1. 注册表自身合法——``(path, method)`` 唯一，每个 endpoint 都是
   可调用的 bound method；
2. 注册表与前端调用清单一致——扫描前端 ``src/`` 里
   ``plugin/<插件 id>/...`` 与 ``/api/v1/plugin/<插件 id>/...`` 形式
   的调用，与注册表做差集；“注册但前端零调用”的路由必须进带原因的
   ``ALLOWLIST`` 才能通过，allowlist 之外的差集出现即失败；反过来
   前端调了未注册的路由也失败（例如删掉的路由又被前端叫起）；
3. 第 4/5/6 项的删除落实——``/aired_counts`` 与 GET ``/qrcode/check``
   已不在注册表；7 个服务层方法不再收 ``apikey``。

加载方式：插件本体 ``__init__.py`` 太重（会把全部组件都以 Mock 顶掉，
见 test_downloader_plugin_1_9_86），直接调它的 ``get_api()`` 拿不到
真实 bound method。这里只加载真实的 ``core/api/*``、``core/services/
runtime.py``、``core/services/platform.py``、``core/subscribe/service.py``
等少数模块（其余依赖全部打桩，沿用 test_channels_split_1_13_2 的
做法），再用一个假宿主 ``_Owner`` 复现插件的委托链
（``__getattr__`` → ``resolve_component``），由它实例化真实的
``MoviePilotRegistration`` 并调 ``get_api()``。``api_torrent`` 是唯一
定义在插件本体上的端点，假宿主上放了一份与之一致的 3 行包装（惰性
导入 ``core/mp_search.api_torrent``，与 ``__init__.py`` 同款）。

``setUpClass`` 里懒加载，不在 import 时加载；加载前对所有动过的
``cloudsubscribex.*`` 模块与 ``fastapi`` 做快照，``tearDownClass`` 里
恢复 ``sys.modules`` 与父包属性（app.*、pytz、apscheduler 是全量套件
共享的打桩基础设施，不动）。

前端调用的写法有五种，扫描器都要认：

* ``api.get/post/put/delete(`plugin/${pluginId}/...`)`` 模板字符串；
* ``request("get", `plugin/${pluginId}/...`)``（ChannelManager 的通用
  ``request(method, url, body)``，动词在第一个参数）；
* 字符串拼接（CheckinTimeline 的
  ``"plugin/CloudSubscribeX/checkin/" + encodeURIComponent(key) +
  "/history?limit=60"``，以及只有一次拼接的 POST 版）；
* 动态 ``${endpoint}``（DirectoryDialog 的目录浏览/创建，值来自同函数
  内最近的 ``const endpoint = ... ? "local/..." : "cloud/..."`` 三元赋值）；
* 配置字段声明的 ``endpoint: "plugin/CloudSubscribeX/..."`` 与
  ``field?.endpoint || "plugin/CloudSubscribeX/residue/cleanup"``
  兜底——两者都由 ConfigSection 的通用 handler 以 POST 发出。

孤儿文件处理：``MediaHub.vue`` / ``MediaDrawer.vue`` / ``StatsGrid.vue``
v1.23.0 起不再被任何页面挂载，用户 2026-11-18 定夺三个文件一并删除；
扫描不再排除任何文件，本文件另有单测断言这三个文件已不存在。
"""

from __future__ import annotations

import inspect
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import (  # noqa: E402
    install_app_mocks,
    install_core_delegation,
    install_real_ttl_cache,
    load_module,
    resolve_component,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "frontend", "cloudsubscribex", "src")

# v1.23.0 起已删除的孤儿文件：扫描不再排除任何文件；见
# test_deleted_orphan_files_are_gone 对删除本身的断言。
DELETED_ORPHAN_REL_PATHS = frozenset({
    "components/dashboard/MediaHub.vue",
    "components/dashboard/MediaDrawer.vue",
    "components/dashboard/StatsGrid.vue",
})

# 插件 id 在前端里的两种写法：字面量 CloudSubscribeX 与 ${...} 模板变量。
_PLUGIN_ID = r"(?:\$\{[^}]+\}|CloudSubscribeX)"
_PLUGIN_URL_PREFIX = re.compile(r"^(?:/api/v1/)?plugin/" + _PLUGIN_ID + r"/")

_VERB_CALL = re.compile(
    r"(?:props\.api|api)\.(get|post|put|delete|patch)\s*\(\s*[`'\"]([^`'\"]+)[`'\"]"
)
_REQUEST_CALL = re.compile(
    r"\brequest\(\s*[\"'](get|post|put|delete|patch)[\"']\s*,\s*[`'\"]([^`'\"]+)[`'\"]"
)
_EVENT_SOURCE = re.compile(
    r"new\s+(?:window\.)?EventSource\(\s*[`'\"]([^`'\"]+)[`'\"]"
)
# 配置字段声明的端点与 ConfigSection 的兜底端点：都走通用 POST handler。
_ENDPOINT_FIELD = re.compile(r"endpoint\s*:\s*[\"'](plugin/[^\"']+)[\"']")
_ENDPOINT_FALLBACK = re.compile(r"\|\|\s*[\"'](plugin/[^\"']+)[\"']")
# 字符串拼接 "a" + expr + "b" 接成 "a/b"（变量段随后补成 {param}）。
_CONCAT_SEAM = re.compile(r"\"\s*\+\s*[^\"\n]+\s*\+\s*\"")
_ENDPOINT_ASSIGN = re.compile(r"const\s+endpoint\s*=\s*([^;]+);")
_STRING_LITERAL = re.compile(r"[\"']([^\"']+)[\"']")
_ANY_PLUGIN_URL = re.compile(
    r"(plugin/" + _PLUGIN_ID + r"/[A-Za-z0-9_/$.{}-]+"
    r"|/api/v1/plugin/" + _PLUGIN_ID + r"/[A-Za-z0-9_/$.{}-]+)"
)

# （注册 path，方法）-> 保留原因。allowlist 条目是“允许例外”不是“必须
# 成员”：某条路由日后又被前端调用也不会失败，只是不再需要这条例外。
ALLOWLIST = {
    # 匿名路由：MP 搜索页拉合成种子时不带登录态，安全靠 sid + HMAC 签名。
    ("/torrent", "GET"): "匿名端点：MP 搜索页拉合成种子（见 registration.py 注释），前端不调",
    # 匿名路由：STRM 文件内容指向它（后端生成播放地址），Emby/播放器直接
    # 请求；前端只在 strm.js 的 hint 文案里提到，不发起调用。
    ("/redirect", "GET"): "匿名端点：STRM 播放直链 302 跳转，前端不调",
    ("/redirect", "HEAD"): "匿名端点：同上，播放器预检用 HEAD",
    # 匿名路由：前端经 EventSource 直连（/api/v1/plugin/.../runtime/stream），
    # 扫描器能认出这条调用；仍列入 allowlist 说明它不走 bear 鉴权的原因。
    ("/runtime/stream", "GET"): "匿名 SSE 推送：前端走 EventSource 直连，鉴权靠 resource token（见 registration.py）",
    # 手动触发榜单自动订阅的预留入口：当前前端无按钮，定时服务直接调
    # run_auto_subscribe；保留供 MP 远程命令与后续前端调用。
    ("/auto_subscribe/run", "POST"): "手动触发榜单自动订阅的预留入口，前端暂无按钮",
    # 统一任务中心聚合接口：当前前端走 /runtime + SSE 推送，不再拉取。
    ("/tasks", "GET"): "任务中心聚合接口，前端改走 /runtime + SSE，保留供后续前端与诊断调用",
    # v1.23.0 前端总览统计卡片已删，不再拉取；接口保留供失败记录页后续
    # 统计与后端测试调用（test_slim_history_page_retry_e2e_1_13_3 直接调
    # api_vue_history_summary）。
    ("/history/summary", "GET"): "v1.23.0 总览统计卡片已删，前端零调用；接口保留",
    # 媒体库洗版入口已随前端收敛删除（ManualResourceDialog 注释）；接口保留
    # 供后续恢复或手动调用。
    ("/history/upgrade", "POST"): "洗版入口已删，前端零调用；接口保留",
    ("/media/servers", "GET"): "洗版浏览入口已删，前端零调用；接口保留",
    ("/media/content", "GET"): "洗版浏览入口已删，前端零调用；接口保留",
    # 重复订阅检查接口：当前前端无调用入口，保留供诊断与后续前端调用。
    ("/duplicate/list", "GET"): "重复订阅检查接口，前端暂无调用入口",
    # Emby 302 反代运行状态与重启：配置页只有开关与端口字段，无状态查询
    # 按钮；保留供手动诊断调用。
    ("/emby_proxy/status", "GET"): "Emby 反代状态查询，前端无按钮；保留供手动诊断调用",
    ("/emby_proxy/restart", "POST"): "Emby 反代手动重启，前端无按钮；保留供手动诊断调用",
    # MediaDrawer 资源搜索已删，前端零调用；保留供 MP 探索页与后续前端
    # 恢复调用。
    ("/resource/search_resources", "POST"): "MediaDrawer 资源搜索已删，前端零调用；接口保留",
}

# 注册总数：有意写死，路由增减必须同步改这里（与 ALLOWLIST 一起审）。
# 78 条 dict；/redirect 一条 dict 带 GET+HEAD 两个方法，所以
# (path, method) 对是 79。v1.22.0 是 80 条 dict / 81 对，减掉
# /aired_counts 与 GET /qrcode/check 各一对。
EXPECTED_REGISTERED_ENTRIES = 78
EXPECTED_REGISTERED_PAIRS = 79

# 第 5 项：7 个服务层方法的新签名（去掉 apikey 后的剩余参数名）。
SERVICE_METHOD_PARAMS = {
    "api_runtime_status": (),
    "api_stop_sync": (),
    "api_stop_sync_task": ("task_id",),
    "api_offline_tasks": ("refresh",),
    "api_retry_offline_tasks": ("pending_keys", "task_ids"),
    "api_delete_offline_task": ("task_id", "pending_key", "delete_source_file"),
    "api_delete_offline_tasks": ("task_ids", "pending_keys", "delete_source_file"),
}

# --- 真实加载的模块（按依赖顺序）与打桩的包 ---------------------------
_REAL_MODULES = (
    ("cloudsubscribex.utils.cache", "plugins.v2/cloudsubscribex/utils/cache.py"),
    ("cloudsubscribex.core.sync_pause", "plugins.v2/cloudsubscribex/core/sync_pause.py"),
    ("cloudsubscribex.core.task_center", "plugins.v2/cloudsubscribex/core/task_center.py"),
    ("cloudsubscribex.core.services.runtime", "plugins.v2/cloudsubscribex/core/services/runtime.py"),
    ("cloudsubscribex.core.services.platform", "plugins.v2/cloudsubscribex/core/services/platform.py"),
    ("cloudsubscribex.core.subscribe.service", "plugins.v2/cloudsubscribex/core/subscribe/service.py"),
    ("cloudsubscribex.core.api.source_filter", "plugins.v2/cloudsubscribex/core/api/source_filter.py"),
    ("cloudsubscribex.core.api.title_parse", "plugins.v2/cloudsubscribex/core/api/title_parse.py"),
    ("cloudsubscribex.core.api.emby_proxy_rules", "plugins.v2/cloudsubscribex/core/api/emby_proxy_rules.py"),
    ("cloudsubscribex.core.api.emby_proxy", "plugins.v2/cloudsubscribex/core/api/emby_proxy.py"),
    ("cloudsubscribex.core.api.page", "plugins.v2/cloudsubscribex/core/api/page.py"),
    ("cloudsubscribex.core.api.account", "plugins.v2/cloudsubscribex/core/api/account.py"),
    ("cloudsubscribex.core.api.doctor", "plugins.v2/cloudsubscribex/core/api/doctor.py"),
    ("cloudsubscribex.core.api.duplicate", "plugins.v2/cloudsubscribex/core/api/duplicate.py"),
    ("cloudsubscribex.core.api.qrcode", "plugins.v2/cloudsubscribex/core/api/qrcode.py"),
    ("cloudsubscribex.core.api.redirect", "plugins.v2/cloudsubscribex/core/api/redirect.py"),
    ("cloudsubscribex.core.api.channels", "plugins.v2/cloudsubscribex/core/api/channels.py"),
    ("cloudsubscribex.core.api.checkin", "plugins.v2/cloudsubscribex/core/api/checkin.py"),
    ("cloudsubscribex.core.api.config", "plugins.v2/cloudsubscribex/core/api/config.py"),
    ("cloudsubscribex.core.api.media_library", "plugins.v2/cloudsubscribex/core/api/media_library.py"),
    ("cloudsubscribex.core.api.sync", "plugins.v2/cloudsubscribex/core/api/sync.py"),
    ("cloudsubscribex.core.api.search", "plugins.v2/cloudsubscribex/core/api/search.py"),
    ("cloudsubscribex.core.api.tg", "plugins.v2/cloudsubscribex/core/api/tg.py"),
    ("cloudsubscribex.core.api.runtime", "plugins.v2/cloudsubscribex/core/api/runtime.py"),
    ("cloudsubscribex.core.api.registration", "plugins.v2/cloudsubscribex/core/api/registration.py"),
)

# get_api() 解析 api_* 名字时按此顺序找定义者（与插件 _COMPONENT_TYPES
# 里相关类的顺序一致；其余组件不定义 api_* 方法，不用列）。
_COMPONENT_MODULE_CLASSES = (
    ("cloudsubscribex.core.api.page", "PageApi"),
    ("cloudsubscribex.core.api.doctor", "DoctorApi"),
    ("cloudsubscribex.core.api.account", "AccountApi"),
    ("cloudsubscribex.core.api.channels", "ChannelsApi"),
    ("cloudsubscribex.core.api.search", "SearchApi"),
    ("cloudsubscribex.core.api.config", "ConfigApi"),
    ("cloudsubscribex.core.api.checkin", "CheckinApi"),
    ("cloudsubscribex.core.api.sync", "SyncApi"),
    ("cloudsubscribex.core.api.runtime", "RuntimeApi"),
    ("cloudsubscribex.core.api.media_library", "MediaLibraryApi"),
    ("cloudsubscribex.core.api.qrcode", "QRCodeService"),
    ("cloudsubscribex.core.api.redirect", "RedirectApi"),
    ("cloudsubscribex.core.api.duplicate", "DuplicateApi"),
    ("cloudsubscribex.core.api.tg", "TgMonitorApi"),
    ("cloudsubscribex.core.services.runtime", "SyncRuntimeService"),
    ("cloudsubscribex.core.services.platform", "PlatformIntegrationService"),
    ("cloudsubscribex.core.subscribe.service", "AutoSubscribeService"),
)

_MISSING = object()


class _Owner:
    """假插件宿主：属性查找走真实的委托链。

    ``api_torrent`` 是唯一定义在插件本体上的端点，这里放一份与
    ``CloudSubscribeX.api_torrent`` 一致的包装（惰性导入，用时才解析）。
    """

    def api_torrent(self, sid: str = "", sig: str = ""):
        from cloudsubscribex.core.mp_search import api_torrent as _mp_api_torrent

        return _mp_api_torrent(self, sid=sid, sig=sig)

    def __getattr__(self, name):
        return resolve_component(
            self, _COMPONENT_TYPES, name, "_plugin_components"
        )


_COMPONENT_TYPES = ()


def _snapshot_modules(names):
    """记下 sys.modules 与父包属性，供 tearDownClass 恢复。"""
    saved_modules = {}
    saved_attrs = {}
    for name in names:
        saved_modules[name] = sys.modules.get(name, _MISSING)
        parent_name, _, child = name.rpartition(".")
        if parent_name and parent_name in sys.modules:
            parent = sys.modules[parent_name]
            saved_attrs[(parent_name, child)] = getattr(parent, child, _MISSING)
    # install_core_delegation() 会往 core 包上写三个名字，也记下来。
    core = sys.modules.get("cloudsubscribex.core")
    if core is not None:
        for attr in ("OwnerDelegator", "get_component", "resolve_component"):
            saved_attrs[("cloudsubscribex.core", attr)] = getattr(
                core, attr, _MISSING
            )
    return saved_modules, saved_attrs


def _restore_modules(saved_modules, saved_attrs):
    for name, saved in saved_modules.items():
        if saved is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    for (parent_name, child), saved in saved_attrs.items():
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        if saved is _MISSING:
            try:
                delattr(parent, child)
            except AttributeError:
                pass
        else:
            setattr(parent, child, saved)


def _load_real_api_modules():
    """打桩后加载真实 API 模块，返回 (registration 模块，组件类元组)。"""
    install_app_mocks(
        "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
        "app.core.security", "app.core.cache", "app.core.plugin",
        "app.db", "app.db.subscribe_oper", "app.db.models",
        "app.db.models.subscribe", "app.db.models.mediaserver",
        "app.chain", "app.chain.download", "app.chain.media",
        "app.chain.subscribe", "app.chain.mediaserver",
        "app.helper", "app.helper.mediaserver", "app.application",
        "app.application.mediaserver", "app.adapters",
        "app.modules", "app.modules.filemanager",
        "app.modules.filemanager.transhandler",
        "app.utils", "app.utils.http", "app.utils.string", "app.utils.timer",
        "app.agent", "app.agent.tools", "app.agent.tools.base",
        "app.agent.tools.tags",
        "apscheduler", "apscheduler.triggers", "apscheduler.triggers.cron",
        "apscheduler.triggers.combining", "apscheduler.triggers.date",
        "apscheduler.triggers.interval",
        "fastapi", "fastapi.responses", "pytz",
        "cloudsubscribex", "cloudsubscribex.core",
        "cloudsubscribex.core.api", "cloudsubscribex.core.services",
        "cloudsubscribex.core.agent", "cloudsubscribex.core.config",
        "cloudsubscribex.core.cloud", "cloudsubscribex.core.media",
        "cloudsubscribex.core.channels", "cloudsubscribex.core.bypass_guard",
        "cloudsubscribex.core.downloader_view",
        "cloudsubscribex.core.database", "cloudsubscribex.core.database.models",
        "cloudsubscribex.core.subscribe", "cloudsubscribex.core.subscribe.models",
        "cloudsubscribex.core.subscribe.provider",
        "cloudsubscribex.core.subscribe.registry",
        "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
        "cloudsubscribex.handlers", "cloudsubscribex.handlers.search",
        "cloudsubscribex.search", "cloudsubscribex.search.scanner",
        "cloudsubscribex.search.types", "cloudsubscribex.search.hdhive",
        "cloudsubscribex.search.magnet", "cloudsubscribex.search.matching",
        "cloudsubscribex.utils", "cloudsubscribex.utils.http_client",
        "cloudsubscribex.utils.notify_template",
        "cloudsubscribex.utils.deep_delete_dedupe",
        "cloudsubscribex.utils.deep_delete_match",
    )
    install_core_delegation()
    install_real_ttl_cache()
    # core/state 与 core/failure 必须强制加载真实模块：全量套件收集期
    # test_yun139_caiyun_share 会把 core.state 换成只有 5 个 STATUS 常量
    # 的空占位（无 STATUS_PENDING_CONFIRM），install_core_state() 碰到
    # 已有条目就直接返回，不会纠正；task_center 导入即挂。error_text 是
    # failure 的纯 stdlib 依赖，一并强制加载。
    for name, path in (
        ("cloudsubscribex.core.error_text", "plugins.v2/cloudsubscribex/core/error_text.py"),
        ("cloudsubscribex.core.failure", "plugins.v2/cloudsubscribex/core/failure.py"),
        ("cloudsubscribex.core.state", "plugins.v2/cloudsubscribex/core/state.py"),
    ):
        load_module(name, path)
    for name, path in _REAL_MODULES:
        load_module(name, path)
    return (
        sys.modules["cloudsubscribex.core.api.registration"],
        tuple(
            getattr(sys.modules[mod_name], cls_name)
            for mod_name, cls_name in _COMPONENT_MODULE_CLASSES
        ),
    )


def _normalize_plugin_path(raw: str) -> str:
    """把前端调用里的 URL 归一化成注册表里的 path 形式。"""
    path = raw.split("?", 1)[0].rstrip("/")
    path = re.sub(r"^/api/v1/plugin/" + _PLUGIN_ID, "", path)
    path = re.sub(r"^plugin/" + _PLUGIN_ID, "", path)
    # 拼接缝合后留下的 "//" 是被拼掉的变量段。
    path = path.replace("//", "/{param}/")
    path = re.sub(r"\$\{[^}]+\}", "{param}", path)
    if not path.startswith("/"):
        path = "/" + path
    return path or "/"


def _is_plugin_url(raw: str) -> bool:
    """只认本插件命名空间下的调用；MP 宿主端点（plugin/、plugin/form/…、
    plugin/history/…）不是插件路由，不参与差集。"""
    return bool(_PLUGIN_URL_PREFIX.match(raw))


def _paths_match(called: str, registered: str) -> bool:
    """段数相同且每段相等，或任一侧是 {占位} 段即算匹配。"""
    called_parts = called.strip("/").split("/")
    registered_parts = registered.strip("/").split("/")
    if len(called_parts) != len(registered_parts):
        return False
    for left, right in zip(called_parts, registered_parts):
        if left == right:
            continue
        if left.startswith("{") and left.endswith("}"):
            continue
        if right.startswith("{") and right.endswith("}"):
            continue
        return False
    return True


def _diff_registered_vs_called(registered, called):
    """注册表与前端调用清单做差集。

    registered/called 都是 (path, METHOD) 集合；返回
    (uncovered, unknown)：注册但前端零调用、前端调了但未注册。
    """
    uncovered = {
        (path, method)
        for (path, method) in registered
        if not any(
            method == called_method and _paths_match(called_path, path)
            for (called_path, called_method) in called
        )
    }
    unknown = {
        (path, method)
        for (path, method) in called
        if not any(
            method == reg_method and _paths_match(path, reg_path)
            for (reg_path, reg_method) in registered
        )
    }
    return uncovered, unknown


def _scan_frontend(src_dir):
    """扫描前端 src/，返回（前端调用集合， bare 提及但未调用的 path 集合）。

    调用集合是 (path, METHOD)；bare 提及指只在文案里出现、没有对应
    api 调用的 path（目前应只有 /redirect，见 strm.js 的 hint 文案）。
    """
    calls = set()
    mentioned = set()
    for dirpath, _dirnames, filenames in os.walk(src_dir):
        for name in filenames:
            if not name.endswith((".vue", ".js")):
                continue
            full_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(full_path, src_dir)
            text = open(full_path, encoding="utf-8").read()
            joined = _CONCAT_SEAM.sub("", text)

            def add_call(match, verb, raw_url):
                if "${endpoint}" in raw_url:
                    return  # 由下面的展开分支处理，不记原始模板
                if not _is_plugin_url(raw_url):
                    return
                url = raw_url
                tail = joined[match.end():match.end() + 120]
                if re.match(r"\s*[`'\"]?\s*\+\s*[^,\n)\]]+", tail):
                    # 字面量后还跟 " + <变量>" 且无第二段字面量：变量是一段 path。
                    url = url.rstrip("/") + "/{param}"
                calls.add((_normalize_plugin_path(url), verb))

            for match in _VERB_CALL.finditer(joined):
                add_call(match, match.group(1).upper(), match.group(2))
            for match in _REQUEST_CALL.finditer(joined):
                add_call(match, match.group(1).upper(), match.group(2))
            for match in _EVENT_SOURCE.finditer(joined):
                if _is_plugin_url(match.group(1)):
                    calls.add((_normalize_plugin_path(match.group(1)), "GET"))
            # 配置字段声明的端点与兜底端点：都由通用 handler 以 POST 发出。
            for match in _ENDPOINT_FIELD.finditer(joined):
                calls.add((_normalize_plugin_path(match.group(1)), "POST"))
            for match in _ENDPOINT_FALLBACK.finditer(joined):
                calls.add((_normalize_plugin_path(match.group(1)), "POST"))
            # ${endpoint} 展开：取同函数内最近的 const endpoint 三元赋值。
            for match in re.finditer(
                r"(?:props\.api|api)\.(get|post|put|delete|patch)"
                r"\s*\(\s*`([^`]*\$\{endpoint\}[^`]*)`",
                joined,
            ):
                window = joined[max(0, match.start() - 2000):match.start()]
                assigns = list(_ENDPOINT_ASSIGN.finditer(window))
                if not assigns:
                    raise AssertionError(
                        f"{rel_path} 有 ${{endpoint}} 调用但找不到 const endpoint 赋值："
                        f"{match.group(0)[:80]}"
                    )
                for literal in _STRING_LITERAL.findall(assigns[-1].group(1)):
                    calls.add((
                        _normalize_plugin_path("/" + literal),
                        match.group(1).upper(),
                    ))
            for match in _ANY_PLUGIN_URL.finditer(joined):
                if "${endpoint}" in match.group(1):
                    continue  # 已由展开分支处理
                rest = joined[match.end():match.end() + 8]
                if re.match(r"\s*[`'\"]\s*\+", rest):
                    continue  # 拼接前缀片段，不是完整 URL
                mentioned.add(_normalize_plugin_path(match.group(1)))
    called_paths = {path for (path, _method) in calls}
    mentioned_only = {
        path for path in mentioned
        if not any(_paths_match(path, called) for called in called_paths)
    }
    return calls, mentioned_only


def _registered_from_api(api_entries):
    """把 get_api() 的返回值转成 (path, METHOD) 集合，并校验条目结构。"""
    registered = set()
    for entry in api_entries:
        for key in ("path", "methods", "endpoint", "summary"):
            if key not in entry:
                raise AssertionError(f"get_api() 条目缺 {key}：{entry!r}")
        if "auth" not in entry and "allow_anonymous" not in entry:
            raise AssertionError(f"get_api() 条目缺 auth/allow_anonymous：{entry!r}")
        for method in entry["methods"]:
            registered.add((entry["path"], method))
    return registered


class GetApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 快照：只恢复本文件动过的 cloudsubscribex.* 与 fastapi；
        # app.*、pytz、apscheduler 是共享打桩基础设施，不动。
        touched = (
            [name for name, _path in _REAL_MODULES]
            + [
                "cloudsubscribex", "cloudsubscribex.core",
                "cloudsubscribex.core.api", "cloudsubscribex.core.services",
                "cloudsubscribex.core.agent", "cloudsubscribex.core.config",
                "cloudsubscribex.core.cloud", "cloudsubscribex.core.media",
                "cloudsubscribex.core.channels", "cloudsubscribex.core.bypass_guard",
                "cloudsubscribex.core.downloader_view",
                "cloudsubscribex.core.database",
                "cloudsubscribex.core.database.models",
                "cloudsubscribex.core.subscribe",
                "cloudsubscribex.core.subscribe.models",
                "cloudsubscribex.core.subscribe.provider",
                "cloudsubscribex.core.subscribe.registry",
                "cloudsubscribex.core.failure", "cloudsubscribex.core.state",
                "cloudsubscribex.core.error_text",
                "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
                "cloudsubscribex.handlers", "cloudsubscribex.handlers.search",
                "cloudsubscribex.search", "cloudsubscribex.search.scanner",
                "cloudsubscribex.search.types", "cloudsubscribex.search.hdhive",
                "cloudsubscribex.search.magnet", "cloudsubscribex.search.matching",
                "cloudsubscribex.utils", "cloudsubscribex.utils.http_client",
                "cloudsubscribex.utils.notify_template",
                "cloudsubscribex.utils.deep_delete_dedupe",
                "cloudsubscribex.utils.deep_delete_match",
                "fastapi", "fastapi.responses",
            ]
        )
        cls._saved_modules, cls._saved_attrs = _snapshot_modules(touched)
        try:
            registration_module, components = _load_real_api_modules()
        except Exception:
            # setUpClass 失败时 tearDownClass 不会跑：这里先恢复快照，
            # 否则半加载的模块会留在 sys.modules 里毒害后面的测试文件。
            _restore_modules(cls._saved_modules, cls._saved_attrs)
            raise
        global _COMPONENT_TYPES
        _COMPONENT_TYPES = components
        cls.owner = _Owner()
        cls.registration = registration_module.MoviePilotRegistration(cls.owner)
        cls.api_entries = cls.registration.get_api()

    @classmethod
    def tearDownClass(cls):
        cls.api_entries = None
        cls.registration = None
        cls.owner = None
        _restore_modules(cls._saved_modules, cls._saved_attrs)
        cls._saved_modules = None
        cls._saved_attrs = None

    def test_entries_are_unique_and_callable(self):
        """(path, method) 唯一；每个 endpoint 都是可调用的 bound method。"""
        seen = set()
        for entry in self.api_entries:
            endpoint = entry["endpoint"]
            self.assertTrue(
                callable(endpoint), f"{entry['path']} 的 endpoint 不可调用：{endpoint!r}"
            )
            self.assertTrue(
                inspect.ismethod(endpoint),
                f"{entry['path']} 的 endpoint 应是 bound method：{endpoint!r}",
            )
            for method in entry["methods"]:
                key = (entry["path"], method)
                self.assertNotIn(key, seen, f"重复注册：{key}")
                seen.add(key)
        self.assertEqual(
            len(self.api_entries), EXPECTED_REGISTERED_ENTRIES,
            "注册条目数变了：增减路由时同步改 EXPECTED_REGISTERED_* 与 ALLOWLIST",
        )
        self.assertEqual(
            len(seen), EXPECTED_REGISTERED_PAIRS,
            "注册 (path, method) 对数变了：增减路由时同步改 EXPECTED_REGISTERED_* 与 ALLOWLIST",
        )

    def test_registered_matches_frontend_calls(self):
        """注册表与前端调用清单做差集：allowlist 之外不得有差集。"""
        registered = _registered_from_api(self.api_entries)
        called, mentioned_only = _scan_frontend(SRC_DIR)
        uncovered, unknown = _diff_registered_vs_called(registered, called)
        unexplained = {
            item for item in uncovered if item not in ALLOWLIST
        }
        self.assertEqual(
            unexplained, set(),
            "注册但前端零调用、且不在 ALLOWLIST 里的路由（新增路由要么补前端调用、"
            f"要么进 ALLOWLIST 写明原因）：{sorted(unexplained)}",
        )
        self.assertEqual(
            unknown, set(),
            f"前端调了未注册的路由（可能调了已删的路由）：{sorted(unknown)}",
        )
        # allowlist 是“允许例外”不是“必须成员”：已被前端调用的条目不强制
        # 出现在 uncovered 里；这里只要求它没有被拼错（key 必须真实存在）。
        for key in ALLOWLIST:
            self.assertIn(
                key, registered, f"ALLOWLIST 里的 {key} 已不在注册表，请删掉这条例外",
            )
        # 前端只在文案里提到、没有 api 调用的 path 应只有 /redirect
        #（strm.js 的 hint 文案；STRM 文件与播放器才真正请求它）。
        self.assertEqual(
            mentioned_only, {"/redirect"},
            f"前端只提及未调用的 path 变了：{sorted(mentioned_only)}",
        )

    def test_deleted_orphan_files_are_gone(self):
        """孤儿文件已删除：MediaHub / MediaDrawer / StatsGrid 不应再存在
        于 src/ 里（用户 2026-11-18 定夺随 v1.23.0 一并删除）。"""
        for rel_path in sorted(DELETED_ORPHAN_REL_PATHS):
            self.assertFalse(
                os.path.exists(os.path.join(SRC_DIR, rel_path)),
                f"孤儿文件 {rel_path} 应已删除",
            )

    def test_deleted_routes_are_gone(self):
        """第 4/6 项：第 4 项删 /aired_counts，第 6 项删 GET /qrcode/check；
        POST 版 qrcode/check 必须还在。"""
        registered = _registered_from_api(self.api_entries)
        self.assertNotIn(("/aired_counts", "POST"), registered)
        self.assertNotIn(("/qrcode/check", "GET"), registered)
        self.assertIn(("/qrcode/check", "POST"), registered)
        names = {entry["endpoint"].__name__ for entry in self.api_entries}
        self.assertNotIn("api_vue_aired_counts", names)
        self.assertNotIn("api_vue_check_qrcode", names)
        self.assertIn("api_vue_check_qrcode_post", names)

    def test_runtime_service_methods_take_no_apikey(self):
        """第 5 项：7 个服务层方法不再收 apikey 参数（走 bear 鉴权）。"""
        for name, params in SERVICE_METHOD_PARAMS.items():
            bound = getattr(self.owner, name)
            self.assertTrue(inspect.ismethod(bound), name)
            self.assertEqual(
                type(bound.__self__).__name__, "SyncRuntimeService", name
            )
            signature = inspect.signature(bound)
            self.assertNotIn("apikey", signature.parameters, name)
            self.assertEqual(
                list(signature.parameters), list(params),
                f"{name} 签名变了：{signature}",
            )

    def test_apikey_check_is_gone_from_runtime_modules(self):
        """第 5 项：两处手动比对已删；page.py 的 Emby webhook token 不动。"""
        for rel_path in (
            "plugins.v2/cloudsubscribex/core/services/runtime.py",
            "plugins.v2/cloudsubscribex/core/api/runtime.py",
        ):
            source = open(
                os.path.join(REPO_ROOT, rel_path), encoding="utf-8"
            ).read()
            self.assertNotIn("apikey", source, rel_path)
            self.assertNotIn("API_TOKEN", source, rel_path)
        page_source = open(
            os.path.join(
                REPO_ROOT, "plugins.v2/cloudsubscribex/core/api/page.py"
            ),
            encoding="utf-8",
        ).read()
        # Emby webhook URL 里的 token 是 MP 自身 webhook 鉴权，不是插件自检。
        self.assertIn('"token": settings.API_TOKEN', page_source)

    def test_diff_logic_catches_unexplained_route(self):
        """差集逻辑本身的合成验证：不靠改真实注册表。"""
        registered = {("/a", "GET"), ("/b", "POST"), ("/c", "GET")}
        called = {("/a", "GET"), ("/b", "POST")}
        uncovered, unknown = _diff_registered_vs_called(registered, called)
        self.assertEqual(uncovered, {("/c", "GET")})
        self.assertEqual(unknown, set())
        # allowlist 里的例外不算差集；allowlist 之外的仍要失败。
        self.assertEqual(uncovered - {("/c", "GET")}, set())
        self.assertEqual(uncovered - {("/a", "GET")}, {("/c", "GET")})
        # 前端调了未注册路由也要抓到。
        _uncovered, unknown = _diff_registered_vs_called(
            registered, called | {("/nope", "DELETE")}
        )
        self.assertEqual(unknown, {("/nope", "DELETE")})
        # {占位} 段按段匹配：/channels/{id} 的调用覆盖 /channels/{channel_id}。
        uncovered, unknown = _diff_registered_vs_called(
            {("/channels/{channel_id}", "PUT")},
            {("/channels/{param}", "PUT")},
        )
        self.assertEqual((uncovered, unknown), (set(), set()))


if __name__ == "__main__":
    unittest.main()
