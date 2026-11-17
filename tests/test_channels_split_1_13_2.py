"""1.13.2：渠道页拆三导航——频道监控从渠道页独立出来。

- 渠道页（AppPageChannels）只留 ChannelManager（渠道配置）；
- 频道监控（TgMonitor，运行态工作列表）独立成主侧栏导航 tg-monitor，
  新页 AppPageTgMonitor.vue 包一层 TgMonitor，联邦加载器按 navKey
  先试 AppPage{TgMonitor} 的命名约定（见 test_sidebar_channels_1_9_89.py）；
- 签到状态（CheckinDashboard）从渠道页删掉，不新建导航——MP 首页
  checkin widget（Dashboard.vue）已有一处，删了是去重不是丢功能；
- TgMonitor 内部的“登录 Telegram”与“转存到订阅”已是 v-dialog，不用再动。
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import OwnerDelegator, ensure_package, load_module  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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
    module = _load_registration_module()
    owner = SimpleNamespace(_enabled=enabled, _show_sidebar_nav=show_sidebar_nav)
    return module.MoviePilotRegistration(owner)


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _exists(rel_path: str) -> bool:
    return os.path.exists(os.path.join(REPO_ROOT, rel_path))


class TestTgMonitorNav(unittest.TestCase):
    def test_tg_monitor_entry_declared(self):
        nav = _registration().get_sidebar_nav()
        by_key = {item["nav_key"]: item for item in nav}
        self.assertEqual(
            [item["nav_key"] for item in nav],
            ["channels", "tg-monitor", "history"],
        )
        monitor = by_key["tg-monitor"]
        self.assertEqual(monitor["title"], "频道监控")
        self.assertEqual(monitor["section"], "system")
        self.assertEqual(monitor["permission"], "manage")
        self.assertEqual(monitor["order"], 31)
        self.assertTrue(str(monitor["icon"]).startswith("mdi-"))
        # 频道监控插在渠道管理与转存记录之间，转存记录顺延一位。
        self.assertEqual(by_key["channels"]["order"], 30)
        self.assertEqual(by_key["history"]["order"], 32)

    def test_all_entries_in_host_whitelist(self):
        nav = _registration().get_sidebar_nav()
        for item in nav:
            self.assertIn(item["section"], VALID_SECTIONS)
            self.assertIn(item["permission"], VALID_PERMISSIONS)


class TestTgMonitorPageShell(unittest.TestCase):
    def test_app_page_tg_monitor_is_tg_monitor_shell(self):
        self.assertTrue(
            _exists(
                "frontend/cloudsubscribex/src/components/AppPageTgMonitor.vue"
            )
        )
        source = _read(
            "frontend/cloudsubscribex/src/components/AppPageTgMonitor.vue"
        )
        self.assertIn("TgMonitor", source)
        self.assertIn('navKey: { type: String, default: "tg-monitor" }', source)
        self.assertNotIn("ChannelManager", source)
        self.assertNotIn("CheckinDashboard", source)
        self.assertNotIn("<v-tab", source)

    def test_vite_exposes_tg_monitor_page(self):
        vite = _read("frontend/cloudsubscribex/vite.config.js")
        self.assertIn(
            '"./AppPageTgMonitor": "./src/components/AppPageTgMonitor.vue"',
            vite,
        )

    def test_channels_page_is_channel_manager_only(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/AppPageChannels.vue"
        )
        self.assertIn("ChannelManager", source)
        self.assertNotIn("TgMonitor", source)
        self.assertNotIn("CheckinDashboard", source)
        self.assertNotIn("<v-tab", source)

    def test_checkin_dashboard_kept_on_homepage_only(self):
        # 签到只留 MP 首页 checkin widget：Dashboard.vue 仍渲染它，
        # 渠道页与监控页都不再挂它。
        dashboard = _read("frontend/cloudsubscribex/src/components/Dashboard.vue")
        self.assertIn("CheckinDashboard", dashboard)
        monitor = _read(
            "frontend/cloudsubscribex/src/components/AppPageTgMonitor.vue"
        )
        self.assertNotIn("CheckinDashboard", monitor)


if __name__ == "__main__":
    unittest.main()
