"""深度删除通知模板：必须显示删的是哪部 + 分项计数（1.7.85）。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_mock

install_app_mocks("app", "app.log", "app.schemas", "app.schemas.types")
register_mock("app.log", logger=MagicMock())

_tpl = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)

TITLES = ["武神主宰 (2020)"]
LINES = ["历史记录：1 条", "网盘目录：1 个"]


class DeleteTemplateTest(unittest.TestCase):
    def test_default_shows_media_name_and_each_field_on_own_line(self):
        text = _tpl.render_template(
            _tpl.DEFAULT_TEMPLATES["notify_template_delete"],
            "兜底",
            titles=TITLES,
            lines=LINES,
            message="路径 1 个，匹配 1 条",
        )
        self.assertIn("深度联动删除完成 · 武神主宰 (2020)", text)
        self.assertIn("历史记录：1 条\n网盘目录：1 个", text)
        self.assertIn("🕐", text)

    def test_empty_titles_still_renders(self):
        text = _tpl.render_template(
            _tpl.DEFAULT_TEMPLATES["notify_template_delete"],
            "兜底",
            titles=[],
            lines=LINES,
        )
        self.assertIsInstance(text, str)
        self.assertIn("深度联动删除完成", text)

    def test_legacy_default_upgraded_to_multiline(self):
        upgraded = _tpl.upgrade_legacy_templates(
            {"notify_template_delete": _tpl.LEGACY_DEFAULT_TEMPLATES["notify_template_delete"][0]}
        )
        self.assertEqual(
            upgraded["notify_template_delete"],
            _tpl.DEFAULT_TEMPLATES["notify_template_delete"],
        )

    def test_single_line_default_upgraded(self):
        # 1.7.75–1.7.84 一代：{{ message }} 单行摘要
        legacy = _tpl.LEGACY_DEFAULT_TEMPLATES["notify_template_delete"][1]
        upgraded = _tpl.upgrade_legacy_templates({"notify_template_delete": legacy})
        self.assertIn("notify_template_delete", upgraded)

    def test_custom_template_untouched(self):
        upgraded = _tpl.upgrade_legacy_templates({"notify_template_delete": "我自己写的 {{ message }}"})
        self.assertEqual(upgraded, {})


class TransferTemplateTest(unittest.TestCase):
    def test_transfer_default_renders_cards_with_labels(self):
        text = _tpl.render_template(
            _tpl.DEFAULT_TEMPLATES["notify_template_transfer"],
            "兜底",
            action="转存",
            count=1,
            total_size="2.0 GB",
            cards=["🎬 杀手 (2020)\n类型：电影\n大小：2.0 GB\n来源：115网盘"],
        )
        self.assertIn("🎬 杀手 (2020)\n类型：电影\n大小：2.0 GB\n来源：115网盘", text)
        self.assertIn("本次转存 1 个文件 · 合计 2.0 GB", text)

    def test_single_line_defaults_are_upgraded(self):
        for key in (
            "notify_template_transfer",
            "notify_template_cross_transfer",
            "notify_template_upgrade",
        ):
            legacy = _tpl.LEGACY_DEFAULT_TEMPLATES[key][0]
            upgraded = _tpl.upgrade_legacy_templates({key: legacy})
            self.assertEqual(upgraded.get(key), _tpl.DEFAULT_TEMPLATES[key], key)


if __name__ == "__main__":
    unittest.main()
