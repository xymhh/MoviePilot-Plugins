"""v1.29.3：配置向导删除 + “常用设置”简化配置 + 设置页手机布局优化。

静态接线测试：向导引用全清、常用设置 section 存在且排第一、含指定
8 个 key、手机导航滚动逻辑存在；不拼 UI 行为，行为由 vite 构建 +
全量套件保证。
"""

import glob
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


CONFIG = "frontend/cloudsubscribex/src/components/Config.vue"
SIDEBAR = "frontend/cloudsubscribex/src/components/config/ConfigSidebar.vue"
WIZARD = "frontend/cloudsubscribex/src/components/config/ConfigWizard.vue"
ENTRY_CHECK = "scripts/check_frontend_entry.py"

COMMON_KEYS = (
    "enabled",
    "cloud_drive",
    "organize_source_path",
    "organize_after_transfer",
    "cron",
    "platform_download_policy",
    "cross_transfer_enabled",
    "enable_cloud_upgrade",
)


class WizardRemovedTest(unittest.TestCase):
    def test_wizard_file_deleted(self):
        self.assertFalse(
            os.path.exists(os.path.join(REPO_ROOT, WIZARD)),
            "ConfigWizard.vue 应已删除",
        )

    def test_no_wizard_references_in_config_files(self):
        for rel in (CONFIG, SIDEBAR):
            text = _read(rel)
            self.assertNotIn("wizard", text.lower(), f"{rel} 仍有向导引用")
            self.assertNotIn("Wizard", text, f"{rel} 仍有向导引用")
        config = _read(CONFIG)
        for marker in ("wizardMode", "toggle-wizard", "ConfigWizard", "wizardStep"):
            self.assertNotIn(marker, config, f"Config.vue 仍有向导引用：{marker}")
        sidebar = _read(SIDEBAR)
        for marker in ("wizardMode", "toggle-wizard", "ConfigWizard"):
            self.assertNotIn(marker, sidebar, f"ConfigSidebar.vue 仍有向导引用：{marker}")
        # 入口校验只断言“不再要求挂载 ConfigWizard”（其 v1.29.3 注释里
        # 提到删除历史是允许的）。
        entry = _read(ENTRY_CHECK)
        self.assertNotIn('            "ConfigWizard",', entry)

    def test_doctor_endpoint_still_used_by_doctor_dialog(self):
        # 后端 plugin/CloudSubscribeX/doctor 端点不动：插件体检弹窗仍在用它。
        dialog = _read(
            "frontend/cloudsubscribex/src/components/dialogs/DoctorDialog.vue"
        )
        self.assertIn("plugin/${props.pluginId}/doctor", dialog)
        workbench = _read("frontend/cloudsubscribex/src/components/PluginWorkbench.vue")
        self.assertIn("<DoctorDialog", workbench)


class CommonSectionTest(unittest.TestCase):
    def test_common_section_defined_first_and_default(self):
        config = _read(CONFIG)
        self.assertIn('value: "common"', config)
        self.assertIn('title: "常用设置"', config)
        # 导航最前面：核心配置分组第一位是 common，默认打开它。
        self.assertIn('"common", "basic"', config)
        self.assertIn('const activeTab = ref("common")', config)
        # 常用设置不是高级 section，默认可见（buildCommonSection 返回体里无 advanced）。
        body = re.search(
            r"function buildCommonSection\(list\) \{(.*?)return \{",
            config,
            re.S,
        )
        self.assertIsNotNone(body)
        self.assertNotIn("advanced", body.group(1))
        self.assertIn(
            "[commonSection.value, ...(baseSections.value || [])]", config
        )

    def test_common_section_has_exactly_the_8_keys(self):
        config = _read(CONFIG)
        keys_block = re.search(
            r"const COMMON_FIELD_KEYS = Object\.freeze\(\[(.*?)\]\)",
            config,
            re.S,
        )
        self.assertIsNotNone(keys_block, "缺少 COMMON_FIELD_KEYS")
        found = re.findall(r'"([a-z_]+)"', keys_block.group(1))
        self.assertEqual(list(COMMON_KEYS), found)

    def test_common_keys_all_exist_in_field_definitions(self):
        # 8 个 key 必须在既有 sections 的字段定义里找得到（不复制定义）。
        field_sources = ""
        for path in glob.glob(
            os.path.join(
                REPO_ROOT, "frontend", "cloudsubscribex", "src", "config", "fields", "**", "*.js"
            ),
            recursive=True,
        ):
            with open(path, encoding="utf-8") as fh:
                field_sources += fh.read()
        for key in COMMON_KEYS:
            self.assertIn(f'key: "{key}"', field_sources, f"字段定义里找不到 {key}")

    def test_common_picks_from_unfiltered_base_sections(self):
        # 常用设置按 key 取字段必须读未折叠的 baseSections（全部 13 个
        # section），否则取不到高级 section 里的 organize_source_path
        # 与 enable_cloud_upgrade。
        config = _read(CONFIG)
        pick = re.search(
            r"function pickFields\(keys\) \{.*?for \(const section of (\w+)\.value",
            config,
            re.S,
        )
        self.assertIsNotNone(pick)
        self.assertEqual(pick.group(1), "baseSections")
        self.assertIn(
            "const baseSections = computed(() => createConfigSections(options, config))",
            config,
        )

    def test_common_section_groups_cover_all_keys(self):
        # 两组分栏：基础组 4 个 + 转存与洗版组 4 个，8 个 key 全覆盖。
        config = _read(CONFIG)
        self.assertIn(
            'pick(["enabled", "cloud_drive", "cron", "platform_download_policy"])',
            config,
        )
        self.assertIn(
            'pick([\n    "organize_source_path",\n    "organize_after_transfer",\n    "cross_transfer_enabled",\n    "enable_cloud_upgrade",\n  ])',
            config,
        )


class MobileNavLayoutTest(unittest.TestCase):
    def test_active_nav_item_scrolls_into_view(self):
        sidebar = _read(SIDEBAR)
        self.assertIn("mobileNavItemsRef", sidebar)
        self.assertIn('ref="mobileNavItemsRef"', sidebar)
        self.assertIn("scrollActiveNavItemIntoView", sidebar)
        self.assertIn('inline: "nearest"', sidebar)
        self.assertIn("scroll-padding-inline: 10px", sidebar)

    def test_two_row_nav_tightened(self):
        # 两行 chip 导航收紧间距/高度，省纵向空间。
        sidebar = _read(SIDEBAR)
        self.assertIn("height: 30px", sidebar)  # 分组 tab：34px → 30px
        self.assertIn("height: 40px", sidebar)  # 子项：44px → 40px
        self.assertIn("gap: 4px", sidebar)

    def test_show_advanced_toggle_untouched(self):
        # v1.29.2 的“显示高级设置”一行显示不动坏。
        sidebar = _read(SIDEBAR)
        self.assertIn("显示高级设置", sidebar)
        self.assertIn("white-space: nowrap", sidebar)


if __name__ == "__main__":
    unittest.main()
