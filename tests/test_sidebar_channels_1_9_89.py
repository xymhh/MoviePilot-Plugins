"""1.9.89：渠道管理改成 MP 主侧栏顶级导航，与站点管理平级。

1.9.88 的渠道管理页是插件页 AppPage.vue 内的一个标签；MP 侧栏导航走
后端 GET /plugin/sidebar_nav 聚合各插件 get_sidebar_nav() 的声明，前端
DefaultLayout 按 section 分组插入主侧栏，点开走 plugin-app 路由
（pluginId + navKey），联邦加载器按 navKey 先试 AppPage{NavKeyPascal}
再回退 AppPage/Page。本次把渠道管理挪到 system 分组（与站点管理同组），
插件页内不再保留 channels 标签。

本文件加载真实的 core/api/registration.py（app.* 与第三方依赖全部打桩），
在 MoviePilotRegistration 上验证侧栏声明；另做前端静态断言：vite
exposes 含 AppPageChannels、新组件渲染 ChannelManager、AppPage.vue 内
无 channels 标签残留。
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import OwnerDelegator, ensure_package, load_module  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# MP 后端 sidebar 投影（app/runtime/extensions/plugin/projection.py::sidebar）
# 的白名单：分区与权限超出白名单会被规整掉，声明必须落在里面。
VALID_SECTIONS = {"start", "discovery", "subscribe", "organize", "system"}
VALID_PERMISSIONS = {"subscribe", "discovery", "search", "manage", "admin"}


def _load_registration_module():
    """打桩后加载真实的 registration.py（只加载一次）。"""
    module = sys.modules.get("cloudsubscribex.core.api.registration")
    if isinstance(getattr(module, "MoviePilotRegistration", None), type):
        return module
    for name in (
        "app", "app.core", "app.core.config", "app.core.security",
        "app.log", "app.schemas", "app.schemas.types", "app.utils",
        "app.utils.timer", "pytz",
        "apscheduler", "apscheduler.triggers",
        "apscheduler.triggers.combining", "apscheduler.triggers.cron",
        "apscheduler.triggers.date", "fastapi",
    ):
        ensure_package(name)
    delegation = ensure_package("cloudsubscribex.core.delegation")
    delegation.OwnerDelegator = OwnerDelegator
    ensure_package("cloudsubscribex.core.agent")
    ensure_package("cloudsubscribex.core.config")
    ensure_package("cloudsubscribex.core.api")
    ensure_package("cloudsubscribex.core.api.emby_proxy")
    return load_module(
        "cloudsubscribex.core.api.registration",
        "plugins.v2/cloudsubscribex/core/api/registration.py",
    )


def _registration(enabled=True, show_sidebar_nav=True):
    """带假宿主的 MoviePilotRegistration：_enabled/_show_sidebar_nav 走委托。"""
    module = _load_registration_module()
    owner = SimpleNamespace(_enabled=enabled, _show_sidebar_nav=show_sidebar_nav)
    return module.MoviePilotRegistration(owner)


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


class TestSidebarNavChannels(unittest.TestCase):
    def test_channels_entry_declared(self):
        nav = _registration().get_sidebar_nav()
        channels = [item for item in nav if item.get("nav_key") == "channels"]
        self.assertEqual(len(channels), 1)
        item = channels[0]
        self.assertEqual(item["title"], "渠道管理")
        self.assertEqual(item["section"], "system")
        self.assertEqual(item["permission"], "manage")
        self.assertEqual(item["order"], 30)
        self.assertTrue(str(item["icon"]).startswith("mdi-"))

    def test_channels_section_and_permission_in_host_whitelist(self):
        nav = _registration().get_sidebar_nav()
        item = next(i for i in nav if i.get("nav_key") == "channels")
        self.assertIn(item["section"], VALID_SECTIONS)
        self.assertIn(item["permission"], VALID_PERMISSIONS)

    def test_existing_entries_unchanged(self):
        nav = _registration().get_sidebar_nav()
        by_key = {item["nav_key"]: item for item in nav}
        self.assertEqual(by_key["main"]["title"], "网盘订阅")
        self.assertEqual(by_key["main"]["section"], "subscribe")
        self.assertEqual(by_key["resource"]["title"], "网盘资源")
        self.assertEqual(by_key["resource"]["section"], "discovery")

    def test_disabled_plugin_returns_empty(self):
        self.assertEqual(_registration(enabled=False).get_sidebar_nav(), [])

    def test_sidebar_switch_off_returns_empty(self):
        self.assertEqual(
            _registration(show_sidebar_nav=False).get_sidebar_nav(), []
        )


class TestChannelsStandalonePage(unittest.TestCase):
    def test_vite_exposes_app_page_channels(self):
        vite = _read("frontend/cloudsubscribex/vite.config.js")
        self.assertIn(
            '"./AppPageChannels": "./src/components/AppPageChannels.vue"',
            vite,
        )

    def test_new_component_renders_channel_manager(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/AppPageChannels.vue"
        )
        self.assertIn("ChannelManager", source)
        self.assertIn(':active="true"', source)
        for prop in ("api", "pluginId", "navKey"):
            self.assertIn(prop, source)
        self.assertNotIn("v-tab", source)

    def test_app_page_has_no_channels_tab_left(self):
        source = _read("frontend/cloudsubscribex/src/components/AppPage.vue")
        self.assertNotIn('value="channels"', source)
        self.assertNotIn("ChannelManager", source)
        self.assertNotIn("channels-pane", source)

    def test_channel_manager_component_kept(self):
        self.assertTrue(
            os.path.exists(
                os.path.join(
                    REPO_ROOT,
                    "frontend/cloudsubscribex/src/components/dashboard/ChannelManager.vue",
                )
            )
        )


if __name__ == "__main__":
    unittest.main()
