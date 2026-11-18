"""v1.18.4：渠道管理 PanSou 动态选项缺失的回归测试。

根因（2026-11-18，用户截图实锤）：渠道管理编辑弹窗复用配置页的表单结构，
但没接动态选项加载链路——ChannelManager.vue 的 <ConfigSection> 没绑
@load-options/@refresh-options，formSection 没对字段做 resolveDynamicField
注入，channelOptions 里也没有 pansou 数据。于是限定频道/限定插件的勾选弹窗
里 field.items 为空，显示“没有可选项”（标题“已选择 126 / 0 项”），而卡片上
显示的 126/27 只是已保存的选中值。

本文件只做静态断言（前端 wiring 的回归守卫；import 路径错误由 vite 构建兜底）。
"""

from __future__ import annotations

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANNEL_MANAGER = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "components",
    "dashboard",
    "ChannelManager.vue",
)
SEARCH_FIELDS = os.path.join(
    REPO_ROOT,
    "frontend",
    "cloudsubscribex",
    "src",
    "config",
    "fields",
    "search",
    "index.js",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ChannelDynamicOptionsTest(unittest.TestCase):
    def test_resolve_dynamic_field_is_exported_for_reuse(self):
        source = _read(SEARCH_FIELDS)
        self.assertIn(
            "export function resolveDynamicField",
            source,
            "resolveDynamicField 必须导出，供 ChannelManager 与配置页共用，"
            "否则两处实现会再次漂移",
        )

    def test_channel_manager_imports_resolver(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn("resolveDynamicField", source)
        self.assertIn(
            'from "../../config/fields/search/index.js"',
            source,
            "ChannelManager 必须从配置页同一份 fields/search/index.js 引入解析器",
        )

    def test_form_section_resolves_dynamic_fields(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn("resolveDynamicField(field, channelOptions)", source)

    def test_config_section_binds_dynamic_option_events(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn('@load-options="loadChannelDynamicOptions"', source)
        self.assertIn('@refresh-options="refreshChannelDynamicOptions"', source)

    def test_loader_fetches_ui_options_by_scope(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn("ui_options?${query}", source)
        self.assertIn("channelOptions[scope] = scopeOptions", source)
        self.assertIn("loadedDynamicScopes", source)

    def test_channel_options_holds_pansou_state(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn("pansou: {}", source)
        self.assertIn("dynamicOptionLoading: {}", source)
        self.assertIn("dynamicOptionErrors: {}", source)

    def test_edit_dialog_preloads_dynamic_options(self):
        source = _read(CHANNEL_MANAGER)
        self.assertIn("preloadChannelDynamicOptions", source)
        # 三个打开编辑的入口都要预拉：新建、切换类型、编辑已有渠道。
        self.assertGreaterEqual(source.count("void preloadChannelDynamicOptions()"), 3)


if __name__ == "__main__":
    unittest.main()
