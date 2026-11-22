"""1.10.3：插件 UI 按流程复用 MP 界面，无法复用的新建导航。

- 追更列表走 MP 订阅页、网盘资源走 MP 探索页：插件主页不再有页签；
- 历史记录没有 MP 代替：独立成主侧栏导航（history），实现是
  PluginWorkbench；
- v1.23.0（§11 前端收敛第 1 项）：总览模式已删，插件主页直接进
  失败记录页（默认只看失败），mode prop 已删，StatsGrid/MediaHub
  已随孤儿文件删除（用户 2026-11-18 定夺），MP 首页 Dashboard widget 是唯一总览；
- 1.10.4：网盘整理不新增导航（1.10.3 的 organize 入口已撤），整理设置
  只留在配置页（organize 分组，高级设置里），整理台随导航一起删除；
- 频道监控与签到状态并进渠道页（AppPageChannels）——1.13.2 已废止该合并：
  频道监控独立成主侧栏导航（tg-monitor），签到只留 MP 首页 checkin widget，
  渠道页只留 ChannelManager；
- 网盘资源页与 resource/ 组件、useMediaData/useMediaDetail/
  useResourcePreview 一并删除；
- 配置页默认只露常用 6 组，其余 6 组折叠进高级设置（本地记忆开关）。
- v1.29.5：13 个技术分组按任务重排为 6 组（追更/转存/播放/通知与签到/
  渠道/网盘与基础）+ 常用设置 20 项 + 顶部搜索框；高级折叠改为字段级
  （origin 属于旧高级分组的字段默认收起）。
"""

from __future__ import annotations

import os
import re
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


class TestSidebarNavRestructure(unittest.TestCase):
    def test_history_entry_declared_no_organize(self):
        # 1.10.4：网盘整理不新增导航，侧栏只剩渠道管理与转存记录。
        # 1.13.2：频道监控从渠道页独立成导航，侧栏是渠道管理、频道监控、转存记录。
        # 1.13.3：转存记录页瘦身为失败记录页，导航标题同步改名。
        # v1.31.0：重复扫描独立成导航，侧栏是五项。
        nav = _registration().get_sidebar_nav()
        by_key = {item["nav_key"]: item for item in nav}
        self.assertEqual(
            [item["nav_key"] for item in nav],
            ["channels", "tg-monitor", "history", "funnel", "duplicates"],
        )
        self.assertNotIn("organize", by_key)
        monitor = by_key["tg-monitor"]
        self.assertEqual(monitor["title"], "频道监控")
        self.assertEqual(monitor["section"], "system")
        self.assertEqual(monitor["permission"], "manage")
        self.assertEqual(monitor["order"], 31)
        self.assertTrue(str(monitor["icon"]).startswith("mdi-"))
        history = by_key["history"]
        self.assertEqual(history["title"], "失败记录")
        self.assertEqual(history["section"], "system")
        self.assertEqual(history["permission"], "manage")
        self.assertEqual(history["order"], 32)
        self.assertTrue(str(history["icon"]).startswith("mdi-"))

    def test_all_entries_in_host_whitelist(self):
        nav = _registration().get_sidebar_nav()
        for item in nav:
            self.assertIn(item["section"], VALID_SECTIONS)
            self.assertIn(item["permission"], VALID_PERMISSIONS)

    def test_disabled_plugin_returns_empty(self):
        self.assertEqual(_registration(enabled=False).get_sidebar_nav(), [])

    def test_sidebar_switch_off_returns_empty(self):
        self.assertEqual(
            _registration(show_sidebar_nav=False).get_sidebar_nav(), []
        )


class TestWorkbenchThinShells(unittest.TestCase):
    def test_workbench_is_the_only_implementation(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        # v1.23.0：总览已删，只剩失败记录整表；统计卡片与转存动态不再挂载。
        self.assertIn("HistoryTable", source)
        self.assertNotIn("StatsGrid", source)
        self.assertNotIn("MediaHub", source)
        self.assertNotIn("<v-tab", source)
        self.assertNotIn("TgMonitor", source)
        self.assertNotIn("OrganizePanel", source)
        # mode prop 已删（v-model 里的 "mode" 子串不算）。
        self.assertNotIn("props.mode", source)
        self.assertNotIn("mode: {", source)
        self.assertNotIn('mode="', source)
        self.assertNotIn("overviewGroups", source)
        self.assertNotIn("overviewFilter", source)

    def test_app_page_is_history_shell(self):
        # v1.23.0：插件主页直接进失败记录页，不再是总览壳，不再传 mode。
        source = _read("frontend/cloudsubscribex/src/components/AppPage.vue")
        self.assertIn("PluginWorkbench", source)
        self.assertNotIn("mode=", source)
        self.assertNotIn("<v-tab", source)
        self.assertNotIn("MediaHub", source)
        self.assertNotIn("HistoryTable", source)

    def test_app_history_is_history_shell(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/AppPageHistory.vue"
        )
        self.assertIn("PluginWorkbench", source)
        self.assertNotIn("mode=", source)
        self.assertNotIn("<v-tab", source)

    def test_organize_page_and_panel_deleted(self):
        # 1.10.4：网盘整理不新增导航，整理台随导航一起删除。
        self.assertFalse(
            _exists(
                "frontend/cloudsubscribex/src/components/AppPageOrganize.vue"
            )
        )
        self.assertFalse(
            _exists(
                "frontend/cloudsubscribex/src/components/dashboard/OrganizePanel.vue"
            )
        )

    def test_channels_page_absorbs_tg_monitor_and_checkin(self):
        # 1.10.3 的合并已由 1.13.2 废止：渠道页只留 ChannelManager，
        # 频道监控独立成导航（见 test_channels_split_1_13_2.py），
        # 签到只留 MP 首页 checkin widget。
        source = _read(
            "frontend/cloudsubscribex/src/components/AppPageChannels.vue"
        )
        self.assertIn("ChannelManager", source)
        self.assertNotIn("TgMonitor", source)
        self.assertNotIn("CheckinDashboard", source)
        self.assertNotIn("<v-tab", source)

    def test_vite_exposes_new_pages_not_resource(self):
        vite = _read("frontend/cloudsubscribex/vite.config.js")
        self.assertIn(
            '"./AppPageHistory": "./src/components/AppPageHistory.vue"',
            vite,
        )
        # 1.10.4：网盘整理导航已撤，联邦不再暴露 AppPageOrganize。
        self.assertNotIn("AppPageOrganize", vite)
        self.assertNotIn("AppPageResource", vite)

    def test_resource_page_and_components_deleted(self):
        self.assertFalse(
            _exists(
                "frontend/cloudsubscribex/src/components/AppPageResource.vue"
            )
        )
        self.assertFalse(
            _exists("frontend/cloudsubscribex/src/components/resource")
        )
        for composable in (
            "useMediaData.js",
            "useMediaDetail.js",
            "useResourcePreview.js",
        ):
            self.assertFalse(
                _exists(
                    f"frontend/cloudsubscribex/src/composables/{composable}"
                )
            )


class TestConfigAdvancedFolding(unittest.TestCase):
    def test_advanced_field_origins_flagged_in_fields(self):
        # v1.29.5：高级折叠改为字段级——origin 属于旧高级分组的字段打上
        # advanced: true；其余 origin 的字段不带该标记。
        source = _read("frontend/cloudsubscribex/src/config/fields.js")
        advanced = re.search(
            r"ADVANCED_FIELD_ORIGINS = new Set\(\[(.*?)\]\)", source, re.S
        ).group(1)
        for value in ("organize", "checkin", "strm", "emby", "drive", "upgrade"):
            self.assertIn(f'"{value}"', advanced)
        for value in ("basic", "transfer", "subscribe", "search", "tg", "notify"):
            self.assertNotIn(f'"{value}"', advanced)
        self.assertIn("advanced: true", source)

    def test_config_filters_advanced_fields_and_common_reads_all(self):
        source = _read("frontend/cloudsubscribex/src/components/Config.vue")
        self.assertIn("allSections", source)
        self.assertIn("showAdvanced", source)
        self.assertIn("csx-config-show-advanced", source)
        # v1.29.5：字段级折叠由 filterAdvancedFields 在 sections computed
        # 里做；常用设置不受影响（仍读未折叠的 baseSections）。
        self.assertIn("function filterAdvancedFields(section)", source)
        self.assertIn("filterAdvancedFields(section)", source)
        self.assertIn("!field?.advanced", source)
        # v1.24.0：高级开关的 v-switch 随侧栏搬到 ConfigSidebar.vue，
        # 开关状态仍由 Config.vue 的 showAdvanced 持有（v-model:show-advanced）。
        sidebar = _read(
            "frontend/cloudsubscribex/src/components/config/ConfigSidebar.vue"
        )
        self.assertIn("显示高级设置", sidebar)
        # v1.29.3：向导已删，读全量的语义由“常用设置”继承——
        # pickFields 按 key 取字段必须读未折叠的 baseSections（全部 6 个
        # 任务 section），否则取不到 organize_source_path（网盘整理，高级）与
        # enable_cloud_upgrade（洗版设置，高级）。
        pick = re.search(
            r"function pickFields\(keys\) \{.*?for \(const section of (\w+)\.value",
            source,
            re.S,
        )
        self.assertIsNotNone(pick)
        self.assertEqual(pick.group(1), "baseSections")


if __name__ == "__main__":
    unittest.main()
