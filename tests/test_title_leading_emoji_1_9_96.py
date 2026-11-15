"""标题开头装饰 emoji 毒化 TMDB 搜索（1.9.96）。

回归：MP 搜索页点“🗄 虚无的十字架 虚ろな十字架 2026 1080p 日剧”
下载，接口回“任务添加失败”，日志是提交侧报错
“未识别到“🗄 虚无的十字架 虚ろな十字架 2026 1080p 日剧”的 TMDB 媒体”：
PanSou 标题开头的 🗄 让 TMDB 标题搜索 0 候选（去掉 🗄 后同一标题
能查到空洞的十字架 326140，订阅标题匹配也因标题不同而失败）。

固定契约：
1. clean_title 去掉标题开头的装饰符号/emoji（🗄 📁 ✨ 等），剧名、
   年份、分辨率等识别信息保留；去掉后为空则保留原串；
2. submit_platform_links 里 recognized_title（预览标题与传入标题都
   可能带 emoji）在用于订阅匹配与 TMDB 搜索前同样去掉开头装饰符；
3. 不带 emoji 的标题、数字开头的标题不受影响。
"""

import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)
from test_mp_search_1_9_91 import _load_mp_search  # noqa: E402

install_app_mocks(
    "app.core.config",
    "app.core.metainfo",
    "app.db",
    "app.db.models.subscribe",
    "app.db.subscribe_oper",
    "app.log",
    "app.schemas.types",
    "app.utils.string",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.core.media",
    "cloudsubscribex.core.services",
    "cloudsubscribex.search",
    "cloudsubscribex.search.types",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()

platform_module = load_module(
    "cloudsubscribex.core.services.platform",
    "plugins.v2/cloudsubscribex/core/services/platform.py",
)
PlatformIntegrationService = platform_module.PlatformIntegrationService

mp_search = _load_mp_search()

TITLE_1080P = "🗄 虚无的十字架 虚ろな十字架 2026 1080p 日剧"
TITLE_1080P_CLEAN = "虚无的十字架 虚ろな十字架 2026 1080p 日剧"


class CleanTitleTest(unittest.TestCase):
    def test_strips_leading_cabinet_emoji(self):
        self.assertEqual(mp_search.clean_title(TITLE_1080P), TITLE_1080P_CLEAN)

    def test_strips_leading_folder_emoji(self):
        self.assertEqual(
            mp_search.clean_title("📁 虚无的十字架 2026"),
            "虚无的十字架 2026",
        )

    def test_strips_multiple_leading_emoji(self):
        self.assertEqual(
            mp_search.clean_title("✨🔥 虚无的十字架 2026"),
            "虚无的十字架 2026",
        )

    def test_clean_title_unchanged(self):
        self.assertEqual(
            mp_search.clean_title("虚无的十字架 虚ろな十字架 2026 1-4"),
            "虚无的十字架 虚ろな十字架 2026 1-4",
        )

    def test_leading_digit_kept(self):
        self.assertEqual(
            mp_search.clean_title("2026 虚无的十字架"),
            "2026 虚无的十字架",
        )

    def test_all_emoji_title_kept(self):
        self.assertEqual(mp_search.clean_title("🗄"), "🗄")

    def test_pack_title_strips_leading_emoji(self):
        self.assertEqual(mp_search.pack_title(TITLE_1080P), TITLE_1080P_CLEAN)


class StripLeadingDecorationsTest(unittest.TestCase):
    def test_strips_leading_cabinet_emoji(self):
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations(TITLE_1080P),
            TITLE_1080P_CLEAN,
        )

    def test_strips_leading_folder_emoji(self):
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations(
                "📁 虚无的十字架 2026"
            ),
            "虚无的十字架 2026",
        )

    def test_clean_title_unchanged(self):
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations(
                "虚无的十字架 虚ろな十字架 2026 1-4"
            ),
            "虚无的十字架 虚ろな十字架 2026 1-4",
        )

    def test_empty_and_none(self):
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations(""), ""
        )
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations(None), ""
        )

    def test_all_emoji_title_kept(self):
        self.assertEqual(
            PlatformIntegrationService._strip_leading_decorations("🗄"), "🗄"
        )


if __name__ == "__main__":
    unittest.main()
