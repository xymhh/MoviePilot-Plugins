"""1.10.4：网盘整理不新增导航，只在插件设置里保留设置。

- 侧栏导航只剩渠道管理与转存记录：get_sidebar_nav() 不再有 organize
  入口；AppPageOrganize.vue 与整理台 OrganizePanel.vue 一并删除；
  vite 联邦不再暴露 ./AppPageOrganize；
- 整理设置仍在配置页：fields/organize.js（整理范围 + 定时整理）
  照常注册进 fields.js，organize 分组仍在高级设置里；向导的落地
  位置那步仍能取到 organize 字段；
- 1.12.20：手动整理台的后端 API 路由（status/scan/execute/stop/
  清理空目录/history）已随整理台一起删除；整理服务本身仍在，
  定时整理按配置照常跑；删的只是手动整理台的前后端入口。
"""

from __future__ import annotations

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _exists(rel_path: str) -> bool:
    return os.path.exists(os.path.join(REPO_ROOT, rel_path))


class TestOrganizeNoNav(unittest.TestCase):
    def test_sidebar_nav_has_no_organize_entry(self):
        registration = _read(
            "plugins.v2/cloudsubscribex/core/api/registration.py"
        )
        self.assertNotIn('"nav_key": "organize"', registration)
        self.assertIn('"nav_key": "channels"', registration)
        self.assertIn('"nav_key": "history"', registration)

    def test_organize_page_and_panel_deleted(self):
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

    def test_vite_does_not_expose_organize_page(self):
        vite = _read("frontend/cloudsubscribex/vite.config.js")
        self.assertNotIn("AppPageOrganize", vite)
        self.assertIn(
            '"./AppPageHistory": "./src/components/AppPageHistory.vue"',
            vite,
        )


class TestOrganizeSettingsStayInConfig(unittest.TestCase):
    def test_organize_section_still_registered(self):
        fields = _read("frontend/cloudsubscribex/src/config/fields.js")
        self.assertIn("createOrganizeSection", fields)
        self.assertIn('"organize"', fields)

    def test_organize_section_fields_kept(self):
        section = _read(
            "frontend/cloudsubscribex/src/config/fields/organize.js"
        )
        self.assertIn("createOrganizeSection", section)
        # 整理范围：待整理目录 + 单次上限 + 冲突策略。
        self.assertIn("organize_source_path", section)
        self.assertIn("organize_max_files", section)
        self.assertIn("organize_conflict_policy", section)
        # 定时整理：开关 + Cron + 自动执行。
        self.assertIn("organize_schedule_enabled", section)
        self.assertIn("organize_schedule_cron", section)
        self.assertIn("organize_schedule_auto_execute", section)

    def test_organize_backend_api_removed(self):
        # 1.12.20：手动整理台的后端 API 路由已随整理台一起删除。
        registration = _read(
            "plugins.v2/cloudsubscribex/core/api/registration.py"
        )
        for path in (
            "/organize/status",
            "/organize/scan",
            "/organize/execute",
            "/organize/stop",
            "/organize/cleanup_empty_dirs",
            "/organize/history",
        ):
            self.assertNotIn(f'"{path}"', registration)

    def test_organize_service_still_scheduled(self):
        # 整理服务本身仍在：定时任务仍由插件启动时注册。
        plugin_init = _read("plugins.v2/cloudsubscribex/__init__.py")
        self.assertIn("def _start_organize_schedule", plugin_init)
        self.assertIn("self._start_organize_schedule()", plugin_init)
        self.assertIn("OrganizeService", plugin_init)


if __name__ == "__main__":
    unittest.main()
