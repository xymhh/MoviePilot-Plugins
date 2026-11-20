"""v1.29.2：移动端 UI 修复的静态接线测试。

只做静态断言（读 .vue 源码文本）：桌面宽视口下推断出的手机问题逐项
核实过代码——其中多项此前已是响应式（历史卡片列表、配置弹窗全屏、
频道监控统计 2 列、渠道栅格、主页按钮图标化、“更多工具” bottom end），
本文件锁定本次真正改动的 7 处 + 4 处已有响应式行为不回归。
"""

from __future__ import annotations

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "frontend", "cloudsubscribex", "src")


def _read(relpath: str) -> str:
    with open(os.path.join(SRC, relpath), encoding="utf-8") as handle:
        return handle.read()


HISTORY = _read(os.path.join("components", "dashboard", "HistoryTable.vue"))
SIDEBAR = _read(os.path.join("components", "config", "ConfigSidebar.vue"))
TG_MONITOR = _read(os.path.join("components", "dashboard", "TgMonitor.vue"))
CHANNEL_CARD = _read(
    os.path.join("components", "dashboard", "channel", "ChannelCard.vue")
)
WORKBENCH = _read(os.path.join("components", "PluginWorkbench.vue"))
CONFIG_SECTION = _read(os.path.join("components", "config", "ConfigSection.vue"))


class HistorySizeColumnTests(unittest.TestCase):
    """大小列：桌面表格此前没有 file_size 插槽，直接渲染原始字节数。"""

    def test_desktop_table_has_file_size_slot_using_format_size(self):
        self.assertIn('#item.file_size="{ item }"', HISTORY)
        self.assertIn("{{ formatSize(item.file_size) }}", HISTORY)

    def test_mobile_card_still_uses_format_size(self):
        self.assertIn("{{ formatSize(record.file_size) }}", HISTORY)


class HistoryResourceTypeTests(unittest.TestCase):
    """类型列：重试索引行没有 resource_type 字段，但有等价的 source_drive_key。"""

    def test_resource_type_falls_back_to_source_drive_key(self):
        self.assertIn("record?.source_drive_key", HISTORY)
        self.assertIn("record?.resource_type || record?.source_drive_key", HISTORY)


class HistoryMobileLayoutTests(unittest.TestCase):
    """历史页手机布局：工具栏换行、卡片标题可收缩。"""

    def test_toolbar_head_wraps(self):
        head = HISTORY.split(".history-toolbar-head {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-wrap: wrap", head)

    def test_mobile_media_title_can_shrink(self):
        title = HISTORY.split(".history-mobile-media-title {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", title)

    def test_mobile_status_chip_does_not_shrink(self):
        self.assertIn('class="flex-shrink-0"', HISTORY)


class ConfigSidebarLabelTests(unittest.TestCase):
    """“显示高级设置”在 162px 侧栏里不许再折成两行；标签文案本身不动。"""

    def test_label_text_unchanged(self):
        self.assertIn('label="显示高级设置"', SIDEBAR)

    def test_label_does_not_wrap(self):
        toggle = SIDEBAR.split(".sidebar-advanced-toggle :deep(.v-label) {", 1)[1]
        toggle = toggle.split("}", 1)[0]
        self.assertIn("white-space: nowrap", toggle)


class TgMonitorGroupTitleTests(unittest.TestCase):
    """频道监控分组头：标题在 flex 行里可收缩、超长截断。"""

    def test_group_title_can_shrink_with_ellipsis(self):
        title = TG_MONITOR.split(".tg-group-title {", 1)[1].split("}", 1)[0]
        self.assertIn("min-width: 0", title)
        self.assertIn("text-overflow: ellipsis", title)
        self.assertIn("white-space: nowrap", title)


class ChannelCardTouchTargetTests(unittest.TestCase):
    """渠道卡片底部图标按钮触摸目标不小于 44px。"""

    def test_channel_action_buttons_min_44px(self):
        actions = CHANNEL_CARD.split(".channel-actions :deep(.v-btn) {", 1)[1]
        actions = actions.split("}", 1)[0]
        self.assertIn("min-width: 44px", actions)
        self.assertIn("min-height: 44px", actions)


class AlreadyResponsiveRegressionTests(unittest.TestCase):
    """此前已是响应式的行为不回归（桌面推断时误以为缺失的几项）。"""

    def test_config_dialog_is_fullscreen_on_mobile(self):
        self.assertIn(':fullscreen="isMobile"', WORKBENCH)

    def test_tg_stats_two_columns_on_small_screens(self):
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", TG_MONITOR)

    def test_history_has_mobile_card_list(self):
        self.assertIn('class="history-mobile-list"', HISTORY)

    def test_config_fields_full_width_on_xs(self):
        self.assertIn(':cols="12"', CONFIG_SECTION)


if __name__ == "__main__":
    unittest.main()
