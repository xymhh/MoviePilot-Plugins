"""常驻浏览器会话的空闲回收与片库生命周期回收测试。

回归背景（2026-10-23 资源审计）：

1. ``BrowserPageSession`` 一旦拉起就不再释放，片库验证页的脚本轮询在标签页里
   持续空转——实测一个 renderer 进程 11 分钟烧掉 27 分钟 CPU、两套常驻
   Chromium 占约 650MB。用例保证空闲阈值到点后上下文真的关闭，且空闲未到
   时不误杀。
2. ``pianku`` provider 未声明 ``LIFECYCLE``，``SearchHandler.close()`` 因此
   不会关闭片库 client（内含反盾浏览器会话）——每次配置保存/插件重载都会
   泄漏一套浏览器。用例保证 provider 声明能力且 close 真的透传到底层 client。

运行：``cd <repo> && python -m pytest tests/test_browser_session_lifecycle.py -q``
"""

import os
import sys
import time
import unittest

from plugin_env import (
    install_app_mocks,
    load_module,
    register_package,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.cache",
    "app.core.metainfo",
    "app.modules",
    "app.modules.themoviedb",
    "app.modules.themoviedb.scraper",
    "app.helper",
    "app.helper.browser",
    "app.utils",
    "app.utils.http",
)

register_package(
    "cloudsubscribex",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex")],
)
register_package(
    "brwsessx.search",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex", "search")],
)
register_package(
    "brwsessx.search.pianku",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex", "search", "pianku")],
)

# 插件 utils 包的 __init__ 会拉起重模块；单测只需要其中的代理归一化函数。
register_package(
    "brwsessx.core",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex", "core")],
)
register_package(
    "brwsessx.utils",
    paths=[os.path.join(plugins_v2_path, "cloudsubscribex", "utils")],
)
load_module(
    "brwsessx.utils.http_client",
    os.path.join(plugins_v2_path, "cloudsubscribex", "utils", "http_client.py"),
)

cloudflare = load_module(
    "brwsessx.search.cloudflare",
    os.path.join(plugins_v2_path, "cloudsubscribex", "search", "cloudflare.py"),
)
pianku_service_module = load_module(
    "brwsessx.search.pianku.service",
    os.path.join(plugins_v2_path, "cloudsubscribex", "search", "pianku", "service.py"),
)
pianku_provider_module = load_module(
    "brwsessx.search.pianku.provider",
    os.path.join(plugins_v2_path, "cloudsubscribex", "search", "pianku", "provider.py"),
)
search_core = load_module(
    "brwsessx.core.search",
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "search.py"),
)

BrowserPageSession = cloudflare.BrowserPageSession
browser_idle_seconds = cloudflare.browser_idle_seconds
park_page = cloudflare.park_page
PiankuSearchService = pianku_service_module.PiankuSearchService
create_pianku_provider = pianku_provider_module.create_pianku_provider
SearchCapability = search_core.SearchCapability


class FakeContext:
    """浏览器上下文替身：只记录 close 次数。"""

    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class FakePage:
    """页面替身：记录 goto 调用，可模拟异常。"""

    def __init__(self, raise_on_goto: bool = False) -> None:
        self.goto_calls = []
        self._raise = raise_on_goto
        self._closed = False

    def goto(self, url, **kwargs):
        if self._raise:
            raise RuntimeError("页面已失效")
        self.goto_calls.append((url, kwargs))

    def is_closed(self) -> bool:
        return self._closed


class BrowserIdleReaperTest(unittest.TestCase):
    """空闲阈值到点关闭上下文，未到点保持存活。"""

    def _session(self, idle_seconds: float) -> BrowserPageSession:
        session = BrowserPageSession("单测会话")
        session._idle_seconds = idle_seconds
        self.addCleanup(session.close)
        return session

    def test_idle_timeout_closes_context(self) -> None:
        session = self._session(0.05)
        context = FakeContext()
        session._context = context
        session._last_used = time.monotonic() - 1.0
        session._close_if_idle()
        self.assertEqual(context.close_count, 1)
        self.assertIsNone(session._context)

    def test_recent_activity_keeps_context(self) -> None:
        session = self._session(600)
        context = FakeContext()
        session._context = context
        session._last_used = time.monotonic()
        session._close_if_idle()
        self.assertEqual(context.close_count, 0)
        self.assertIs(session._context, context)

    def test_timer_armed_after_run_fires_close(self) -> None:
        session = self._session(0.05)
        context = FakeContext()
        session._context = context
        session._last_used = time.monotonic() - 1.0
        session._arm_idle_timer()
        self.addCleanup(session._cancel_idle_timer)
        self.assertIsNotNone(session._idle_timer)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and context.close_count == 0:
            time.sleep(0.02)
        self.assertEqual(context.close_count, 1)

    def test_disabled_idle_never_arms_timer(self) -> None:
        session = self._session(0)
        session._arm_idle_timer()
        self.assertIsNone(session._idle_timer)

    def test_close_cancels_pending_timer(self) -> None:
        session = self._session(600)
        session._context = FakeContext()
        session._arm_idle_timer()
        session.close()
        self.assertIsNone(session._idle_timer)

    def test_env_override_parsing(self) -> None:
        original = os.environ.get("CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS")
        try:
            os.environ["CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS"] = "90"
            self.assertEqual(browser_idle_seconds(), 90)
            os.environ["CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS"] = "0"
            self.assertEqual(browser_idle_seconds(), 0)
            os.environ["CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS"] = "abc"
            self.assertEqual(
                browser_idle_seconds(), cloudflare.DEFAULT_BROWSER_IDLE_SECONDS
            )
        finally:
            if original is None:
                os.environ.pop("CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS", None)
            else:
                os.environ["CLOUDSUBSCRIBE_BROWSER_IDLE_SECONDS"] = original


class ParkPageTest(unittest.TestCase):
    """快照取完把页面停到空白页，异常时静默。"""

    def test_park_navigates_to_blank(self) -> None:
        page = FakePage()
        park_page(page)
        self.assertEqual(len(page.goto_calls), 1)
        self.assertEqual(page.goto_calls[0][0], "about:blank")

    def test_park_swallows_errors(self) -> None:
        park_page(FakePage(raise_on_goto=True))
        park_page(None)


class PiankuLifecycleTest(unittest.TestCase):
    """片库 provider 声明 LIFECYCLE，close 透传到 client。"""

    def test_provider_declares_lifecycle(self) -> None:
        service = PiankuSearchService(client=None, resources=None, resource_types=())
        provider = create_pianku_provider(service, (), {})
        self.assertTrue(provider.supports(SearchCapability.LIFECYCLE))
        self.assertIs(provider.require(SearchCapability.LIFECYCLE), service)

    def test_service_close_delegates_to_client(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        client = FakeClient()
        service = PiankuSearchService(client=client, resources=None, resource_types=())
        service.close()
        self.assertEqual(client.closed, 1)

    def test_service_close_survives_client_errors(self) -> None:
        class BrokenClient:
            def close(self) -> None:
                raise RuntimeError("浏览器已退出")

        service = PiankuSearchService(
            client=BrokenClient(), resources=None, resource_types=()
        )
        service.close()


if __name__ == "__main__":
    unittest.main()
