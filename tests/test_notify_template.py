"""通知模板渲染单测。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock

register_mock("app")
register_mock("app.log", logger=MagicMock())

notify_template = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)


class RenderTemplateTest(unittest.TestCase):
    def test_empty_template_falls_back(self):
        self.assertEqual(
            notify_template.render_template("", "原始文案", count=1), "原始文案"
        )
        self.assertEqual(notify_template.render_template(None, "原始文案"), "原始文案")
        self.assertEqual(notify_template.render_template("   ", "原始文案"), "原始文案")

    def test_renders_variables(self):
        text = notify_template.render_template(
            "共 {{ count }} 个\n{% for label in labels %}· {{ label }}\n{% endfor %}",
            "兜底",
            count=2,
            labels=["A", "B"],
        )
        self.assertIn("共 2 个", text)
        self.assertIn("· A", text)
        self.assertIn("· B", text)

    def test_syntax_error_falls_back(self):
        self.assertEqual(
            notify_template.render_template("{% for x in %}", "兜底"), "兜底"
        )

    def test_unknown_variable_renders_empty(self):
        self.assertEqual(
            notify_template.render_template("值={{ missing }}", "兜底"), "值="
        )

    def test_time_injected_automatically(self):
        text = notify_template.render_template("{{ time }}", "兜底")
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class DefaultTemplateTest(unittest.TestCase):
    CONTEXT = {
        "notify_template_transfer": dict(
            action="转存", count=2, labels=["尚公主 S01 E01"], items=[{"title": "尚公主"}]
        ),
        "notify_template_cross_transfer": dict(
            action="跨盘转存", count=1, labels=["某电影 (2024)"], items=[]
        ),
        "notify_template_upgrade": dict(
            action="洗版", count=1, labels=["某剧 S01 E03"], items=[]
        ),
        "notify_template_checkin": dict(
            provider="115", success=True, lines=["模式：普通签到", "状态：成功"]
        ),
        "notify_template_organize": dict(
            source="/115网盘/电影",
            groups=3,
            ready=2,
            done=1,
            skipped=1,
            failed=0,
            removed_dirs=2,
        ),
        "notify_template_delete": dict(message="删除历史 1 条、网盘文件 2 个"),
    }

    def test_all_defaults_render(self):
        self.assertEqual(
            set(notify_template.DEFAULT_TEMPLATES), set(self.CONTEXT)
        )
        for key, template in notify_template.DEFAULT_TEMPLATES.items():
            text = notify_template.render_template(template, "FALLBACK", **self.CONTEXT[key])
            self.assertNotEqual(text, "FALLBACK", key)
            self.assertTrue(text.strip(), key)

    def test_checkin_default_reflects_success_flag(self):
        ok = notify_template.render_template(
            notify_template.DEFAULT_TEMPLATES["notify_template_checkin"],
            "兜底",
            provider="115",
            success=True,
            lines=["x"],
        )
        bad = notify_template.render_template(
            notify_template.DEFAULT_TEMPLATES["notify_template_checkin"],
            "兜底",
            provider="115",
            success=False,
            lines=["x"],
        )
        self.assertIn("成功", ok)
        self.assertIn("失败", bad)

    def test_every_default_has_variable_hint(self):
        self.assertEqual(
            set(notify_template.DEFAULT_TEMPLATES),
            set(notify_template.TEMPLATE_VARIABLES),
        )


class ResolveTemplateTest(unittest.TestCase):
    def test_prefers_configured_template(self):
        self.assertEqual(
            notify_template.resolve_template(
                {"notify_template_transfer": "自定义 {{ count }}"}, "notify_template_transfer"
            ),
            "自定义 {{ count }}",
        )

    def test_defaults_when_blank_or_missing(self):
        default = notify_template.DEFAULT_TEMPLATES["notify_template_transfer"]
        self.assertEqual(
            notify_template.resolve_template({}, "notify_template_transfer"), default
        )
        self.assertEqual(
            notify_template.resolve_template(
                {"notify_template_transfer": "   "}, "notify_template_transfer"
            ),
            default,
        )
        self.assertEqual(notify_template.resolve_template(None, "notify_template_transfer"), default)


if __name__ == "__main__":
    unittest.main()
