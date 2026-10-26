"""转存通知富信息（类型/大小/来源）与模板升级单测（1.7.75）。"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_mock,
)

install_app_mocks(
    "app", "app.log", "app.schemas", "app.schemas.types",
    "cloudsubscribex", "cloudsubscribex.core",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
)
register_mock("app.log", logger=MagicMock())
register_mock(
    "app.schemas.types",
    NotificationType=type("NotificationType", (), {"Plugin": "Plugin"}),
)

_tpl = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)

_core = sys.modules["cloudsubscribex.core"]
_core.OwnerDelegator = OwnerDelegator
_core.render_template = _tpl.render_template
_core.resolve_template = _tpl.resolve_template

_drive = sys.modules["cloudsubscribex.drive.common"]


def _format_size(value):
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


_drive.format_size = _format_size

_notify = load_module(
    "cloudsubscribex.handlers.sync.notify",
    "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
)
Service = _notify.SyncNotificationService


def _service(templates=None):
    service = object.__new__(Service)
    object.__setattr__(service, "_owner", types.SimpleNamespace())
    service._post_message = MagicMock()
    service._notification_type = "Plugin"
    service._notify_templates = dict(templates or {})
    return service


def _sent_text(service):
    return service._post_message.call_args.kwargs["text"]


class MediaTypeLabelTest(unittest.TestCase):
    def test_icon_by_type(self):
        self.assertEqual(Service._media_type_icon("电影"), "🎬")
        self.assertEqual(Service._media_type_icon("电视剧"), "📺")
        self.assertEqual(Service._media_type_icon("动画"), "🍿")
        self.assertEqual(Service._media_type_icon(""), "📦")

    def test_name_by_type(self):
        self.assertEqual(Service._media_type_name("电视剧"), "剧集")
        self.assertEqual(Service._media_type_name("动画"), "动漫")
        self.assertEqual(Service._media_type_name(""), "")


class TransferDetailLineTest(unittest.TestCase):
    def test_movie_line_has_type_size_and_source(self):
        service = _service()
        service._send_transfer_notification_now([{
            "type": "电影",
            "title": "杀手",
            "year": "2020",
            "notification_kind": "transfer",
            "size": 2 * 1024 ** 3,
            "source": "115网盘",
        }])
        text = _sent_text(service)
        self.assertIn("🎬 杀手 (2020) · 电影 · 2.0 GB · 115网盘", text)
        self.assertIn("合计 2.0 GB", text)

    def test_tv_cross_transfer_shows_source_to_target(self):
        service = _service()
        service._send_transfer_notification_now([{
            "type": "电视剧",
            "title": "尚公主",
            "season": 1,
            "episodes": [9, 10],
            "notification_kind": "cross_transfer",
            "size": 1024 ** 3,
            "source": "夸克 → 115",
        }])
        text = _sent_text(service)
        self.assertIn("🔀 📺 尚公主 S01 E09, E10 · 剧集 · 1.0 GB · 夸克 → 115", text)

    def test_upgrade_line_uses_upgrade_icon(self):
        service = _service()
        service._send_transfer_notification_now([{
            "type": "电影",
            "title": "沙丘",
            "year": "2021",
            "notification_kind": "upgrade",
        }])
        text = _sent_text(service)
        self.assertIn("⬆️ 🎬 沙丘 (2021) · 电影", text)
        self.assertNotIn("合计", text)

    def test_missing_meta_degrades_gracefully(self):
        service = _service()
        service._send_transfer_notification_now([{
            "type": "电视剧",
            "title": "某剧",
            "season": 1,
            "episodes": [],
            "notification_kind": "transfer",
        }])
        text = _sent_text(service)
        self.assertIn("📺 某剧 S01 · 剧集", text)

    def test_custom_template_keeps_variables(self):
        service = _service({
            "notify_template_transfer": "自定 {{ count }} 项 {{ total_size }}\n{{ labels[0] }}",
        })
        service._send_transfer_notification_now([{
            "type": "电影",
            "title": "杀手",
            "year": "2020",
            "size": 1024 ** 3,
            "source": "115网盘",
        }])
        self.assertEqual(
            _sent_text(service),
            "自定 1 项 1.0 GB\n🎬 杀手 (2020) · 电影 · 1.0 GB · 115网盘",
        )


class DefaultTemplateTest(unittest.TestCase):
    def test_all_defaults_render(self):
        for key, template in _tpl.DEFAULT_TEMPLATES.items():
            text = _tpl.render_template(
                template,
                "兜底",
                action="转存",
                count=2,
                total_size="3.4 GB",
                labels=["🎬 A · 电影"],
                items=[],
                lines=["已签到"],
                provider="115",
                success=True,
                source="/媒体库",
                groups=3,
                ready=2,
                done=2,
                skipped=1,
                failed=0,
                removed_dirs=1,
                message="已删除 3 项",
            )
            self.assertNotEqual(text, "兜底", key)
            self.assertNotIn("{{", text, key)

    def test_transfer_default_has_body_and_time(self):
        text = _tpl.render_template(
            _tpl.DEFAULT_TEMPLATES["notify_template_transfer"],
            "兜底",
            action="转存",
            count=1,
            total_size="2.0 GB",
            labels=["🎬 杀手 (2020) · 电影 · 2.0 GB · 115网盘"],
        )
        self.assertTrue(text.startswith("📦 本次转存 1 个文件 · 合计 2.0 GB"))
        self.assertIn("🎬 杀手 (2020)", text)
        self.assertIn("🕐 ", text)


class LegacyUpgradeTest(unittest.TestCase):
    def test_legacy_default_is_replaced(self):
        config = {"notify_template_transfer": _tpl.LEGACY_DEFAULT_TEMPLATES["notify_template_transfer"][0]}
        updated = _tpl.upgrade_legacy_templates(config)
        self.assertEqual(
            updated["notify_template_transfer"],
            _tpl.DEFAULT_TEMPLATES["notify_template_transfer"],
        )

    def test_every_legacy_generation_is_upgraded(self):
        for key, variants in _tpl.LEGACY_DEFAULT_TEMPLATES.items():
            current = _tpl.DEFAULT_TEMPLATES[key]
            for index, variant in enumerate(variants):
                if variant.strip() == current.strip():
                    continue  # 本版未改这一类，旧值即新值，无需回写
                self.assertIn(
                    key,
                    _tpl.upgrade_legacy_templates({key: variant}),
                    f"{key} 第 {index} 代旧默认值未被升级",
                )

    def test_new_default_is_not_rewritten(self):
        for key, default in _tpl.DEFAULT_TEMPLATES.items():
            self.assertEqual(_tpl.upgrade_legacy_templates({key: default}), {}, key)

    def test_customized_template_is_kept(self):
        config = {"notify_template_transfer": "我的模板 {{ count }}"}
        self.assertEqual(_tpl.upgrade_legacy_templates(config), {})

    def test_empty_value_is_not_upgraded_here(self):
        self.assertEqual(
            _tpl.upgrade_legacy_templates({"notify_template_transfer": ""}), {}
        )

    def test_whitespace_differences_still_upgrade(self):
        legacy = _tpl.LEGACY_DEFAULT_TEMPLATES["notify_template_checkin"][0]
        noisy = "\n".join(
            line + "   " for line in legacy.splitlines()
        ) + "\n\n"
        updated = _tpl.upgrade_legacy_templates({"notify_template_checkin": noisy})
        self.assertIn("notify_template_checkin", updated)


if __name__ == "__main__":
    unittest.main()
