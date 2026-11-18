"""v1.24.0：Config.vue / ChannelManager.vue 前端拆分的静态接线测试。

Config.vue（3427 行）拆成：
- components/config/ConfigSidebar.vue（侧栏导航 + 品牌 + 状态看板）
- components/config/ConfigWizard.vue（配置向导面板）
- components/config/ConfigSourceTestDialog.vue（搜索渠道测试弹窗）
- components/config/ConfigAutoSubscribeTestDialog.vue（榜单测试弹窗）
- components/config/ConfigResourcePreviewDialog.vue（资源内容预览弹窗）
- components/config/ConfigUnlockDialog.vue（解锁确认弹窗）
- utils/sourceTestHelpers.js（搜索测试弹窗的纯函数，供父子共用）

ChannelManager.vue（1056 行）拆成：
- components/dashboard/channel/ChannelCard.vue（渠道卡片）
- components/dashboard/channel/ChannelDetailDrawer.vue（详情抽屉）
- components/dashboard/channel/ChannelEditDialog.vue（新增/编辑弹窗，
  含 <ConfigSection> 与 v1.18.4 动态选项绑定）

本文件只做静态断言（import 路径错误由 vite 构建兜底）；行为不变由
构建 + 全量套件保证。
"""

from __future__ import annotations

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "components", "Config.vue"
)
CONFIG_DIR = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "components", "config"
)
CHANNEL_MANAGER = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "components",
    "dashboard",
    "ChannelManager.vue",
)
CHANNEL_DIR = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "components",
    "dashboard",
    "channel",
)
HELPERS = os.path.join(
    REPO_ROOT, "frontend", "cloudsubscribex", "src", "utils", "sourceTestHelpers.js"
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _config_child(name: str) -> str:
    return _read(os.path.join(CONFIG_DIR, name))


def _channel_child(name: str) -> str:
    return _read(os.path.join(CHANNEL_DIR, name))


class ConfigSplitTest(unittest.TestCase):
    def test_config_children_exist_and_are_mounted(self):
        config = _read(CONFIG)
        for name, tag in (
            ("ConfigSidebar.vue", "<ConfigSidebar"),
            ("ConfigWizard.vue", "<ConfigWizard"),
            ("ConfigSourceTestDialog.vue", "<ConfigSourceTestDialog"),
            ("ConfigAutoSubscribeTestDialog.vue", "<ConfigAutoSubscribeTestDialog"),
            ("ConfigResourcePreviewDialog.vue", "<ConfigResourcePreviewDialog"),
            ("ConfigUnlockDialog.vue", "<ConfigUnlockDialog"),
        ):
            self.assertTrue(
                os.path.exists(os.path.join(CONFIG_DIR, name)),
                f"缺少拆分出的子组件 {name}",
            )
            self.assertIn(tag, config, f"Config.vue 未挂载 {tag}")
            self.assertIn(
                f'from "./config/{name}"', config, f"Config.vue 未引入 {name}"
            )

    def test_moved_template_left_config(self):
        config = _read(CONFIG)
        for marker in (
            '<aside class="config-sidebar">',
            'class="config-window-section wizard-panel"',
            '<v-dialog v-model="sourceTestVisible"',
            '<v-dialog v-model="autoSubscribeTestVisible"',
            '<v-dialog v-model="previewVisible"',
            '<v-dialog v-model="unlockVisible"',
        ):
            self.assertNotIn(marker, config, f"模板片段仍留在 Config.vue：{marker}")

    def test_sidebar_child_owns_nav_and_status(self):
        sidebar = _config_child("ConfigSidebar.vue")
        self.assertIn('<aside class="config-sidebar">', sidebar)
        self.assertIn("sidebar-nav-item", sidebar)
        self.assertIn("显示高级设置", sidebar)
        self.assertIn("sidebar-status-panel", sidebar)
        self.assertIn('@click="emit(\'nav-click\'', sidebar)
        self.assertIn("update:showAdvanced", sidebar)

    def test_wizard_child_owns_panel_and_forwards_section_events(self):
        wizard = _config_child("ConfigWizard.vue")
        self.assertIn('class="config-window-section wizard-panel"', wizard)
        self.assertIn("<ConfigSection", wizard)
        for event in (
            "@scan=",
            "@test-source=",
            "@refresh-account=",
            "@load-options=",
            "@refresh-options=",
            "@notify=",
        ):
            self.assertIn(event, wizard, f"向导未转发 ConfigSection 事件 {event}")
        self.assertIn('@click="emit(\'next-step\')"', wizard)
        self.assertIn('@click="emit(\'finish\')"', wizard)

    def test_dialog_children_own_their_dialogs(self):
        source_test = _config_child("ConfigSourceTestDialog.vue")
        self.assertIn('class="source-test-dialog"', source_test)
        self.assertIn("search-tmdb", source_test)
        self.assertIn("tmdbCandidates", source_test)
        autosub = _config_child("ConfigAutoSubscribeTestDialog.vue")
        self.assertIn('class="auto-subscribe-test-dialog"', autosub)
        self.assertIn("providerName", autosub)
        preview = _config_child("ConfigResourcePreviewDialog.vue")
        self.assertIn('class="resource-preview-dialog"', preview)
        self.assertIn("previewBreadcrumbs", preview)
        self.assertIn("function formatPreviewSize", preview)
        unlock = _config_child("ConfigUnlockDialog.vue")
        self.assertIn("unlockItem", unlock)
        self.assertIn("confirm-unlock", unlock)

    def test_source_test_helpers_shared_module(self):
        helpers = _read(HELPERS)
        for name in (
            "export function formatElapsed",
            "export function tmdbCandidateSubtitle",
            "export function testItemStatus",
            "export function canPreviewResource",
            "export function previewResourceKey",
        ):
            self.assertIn(name, helpers)
        config = _read(CONFIG)
        # 父组件只留它在逻辑里还要用的两个（unlockResource / previewResource）。
        self.assertIn(
            'import { canPreviewResource, previewResourceKey } from "../utils/sourceTestHelpers.js"',
            config,
        )
        dialog = _config_child("ConfigSourceTestDialog.vue")
        self.assertIn("sourceTestHelpers.js", dialog)
        # 五个纯函数定义已搬出 Config.vue，不再有两份实现。
        for name in (
            "function formatElapsed(",
            "function tmdbCandidateSubtitle(",
            "function testItemStatus(",
            "function canPreviewResource(",
            "function previewResourceKey(",
            "function formatPreviewSize(",
            "function previewFileIcon(",
        ):
            self.assertNotIn(name, config, f"纯函数仍留在 Config.vue：{name}")

    def test_config_keeps_pinned_wiring(self):
        # 1.10.3 / 1.10.5 钉在 Config.vue 的逻辑不得随拆分搬走。
        config = _read(CONFIG)
        self.assertIn("allSections", config)
        self.assertIn("showAdvanced", config)
        self.assertIn("csx-config-show-advanced", config)
        self.assertIn('"subscribe", "subsources", "upgrade"', config)
        self.assertIn("const autoSubscribeProviderNames = computed(() => {", config)
        self.assertIn("defaults: {},", config)
        self.assertIn("options.defaults = data.defaults", config)
        self.assertIn("function pickFields(keys) {", config)


class ChannelManagerSplitTest(unittest.TestCase):
    def test_channel_children_exist_and_are_mounted(self):
        manager = _read(CHANNEL_MANAGER)
        for name, tag in (
            ("ChannelCard.vue", "<ChannelCard"),
            ("ChannelDetailDrawer.vue", "<ChannelDetailDrawer"),
            ("ChannelEditDialog.vue", "<ChannelEditDialog"),
        ):
            self.assertTrue(
                os.path.exists(os.path.join(CHANNEL_DIR, name)),
                f"缺少拆分出的子组件 {name}",
            )
            self.assertIn(tag, manager, f"ChannelManager.vue 未挂载 {tag}")
            self.assertIn(
                f'from "./channel/{name}"', manager, f"ChannelManager.vue 未引入 {name}"
            )

    def test_moved_template_left_channel_manager(self):
        manager = _read(CHANNEL_MANAGER)
        self.assertNotIn('<v-card variant="outlined" class="channel-card">', manager)
        self.assertNotIn("<v-navigation-drawer", manager)
        self.assertNotIn('<v-dialog v-model="dialogVisible"', manager)

    def test_card_child_owns_card_actions(self):
        card = _channel_child("ChannelCard.vue")
        self.assertIn('class="channel-card"', card)
        for event in (
            "toggle-select",
            "set-enabled",
            "open-detail",
            "test",
            "scan",
            "edit",
            "delete",
        ):
            self.assertIn(event, card, f"卡片未发出事件 {event}")

    def test_detail_drawer_child_owns_drawer(self):
        drawer = _channel_child("ChannelDetailDrawer.vue")
        self.assertIn("<v-navigation-drawer", drawer)
        self.assertIn("detailLines", drawer)
        self.assertIn("渠道详情", drawer)

    def test_edit_dialog_keeps_dynamic_options_binding(self):
        # v1.18.4 链路：绑定随弹窗搬到 ChannelEditDialog.vue，加载器仍在父组件。
        dialog = _channel_child("ChannelEditDialog.vue")
        self.assertIn("<ConfigSection", dialog)
        self.assertIn('@load-options="loadChannelDynamicOptions"', dialog)
        self.assertIn('@refresh-options="refreshChannelDynamicOptions"', dialog)
        manager = _read(CHANNEL_MANAGER)
        self.assertIn("resolveDynamicField(field, channelOptions)", manager)
        self.assertIn("pansou: {}", manager)
        self.assertIn("ui_options?${query}", manager)
        self.assertIn("channelOptions[scope] = scopeOptions", manager)
        self.assertGreaterEqual(
            manager.count("void preloadChannelDynamicOptions()"), 3
        )
        self.assertIn(
            ':load-channel-dynamic-options="loadChannelDynamicOptions"', manager
        )
        self.assertIn(
            ':refresh-channel-dynamic-options="refreshChannelDynamicOptions"',
            manager,
        )

    def test_edit_dialog_keeps_tg_and_config_branches(self):
        dialog = _channel_child("ChannelEditDialog.vue")
        self.assertIn("form.type === 'tg_channel'", dialog)
        self.assertIn("formSection.groups.length", dialog)
        self.assertIn("该搜索渠道没有可配置项", dialog)


if __name__ == "__main__":
    unittest.main()
