import os
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    REPO_ROOT,
    install_app_mocks,
    load_module,
    register_package,
)

# 基础 Mock
install_app_mocks(
    "app.chain",
    "app.chain.download",
    "app.chain.media",
    "app.chain.subscribe",
    "app.helper",
    "app.helper.mediaserver",
    "app.application",
    "app.application.mediaserver",
    "app.db",
    "app.db.site_oper",
    "app.db.subscribe_oper",
    "app.db.systemconfig_oper",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
)

register_package("cloudsubscribe", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribe")])
register_package("cloudsubscribe.core", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribe/core")])
register_package("cloudsubscribe.core.subscribe",
                 paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribe/core/subscribe")])
register_package("cloudsubscribe.search", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribe/search")])
register_package("cloudsubscribe.utils", paths=[os.path.join(REPO_ROOT, "plugins.v2/cloudsubscribe/utils")])

# 预先加载依赖模块
load_module("cloudsubscribe.core.subscribe.models", "plugins.v2/cloudsubscribe/core/subscribe/models.py")
load_module("cloudsubscribe.core.subscribe.provider", "plugins.v2/cloudsubscribe/core/subscribe/provider.py")
load_module("cloudsubscribe.core.subscribe.registry", "plugins.v2/cloudsubscribe/core/subscribe/registry.py")
load_module("cloudsubscribe.core.media", "plugins.v2/cloudsubscribe/core/media.py")
load_module("cloudsubscribe.search.matching", "plugins.v2/cloudsubscribe/search/matching.py")

AutoSubscribeService = load_module(
    "cloudsubscribe.core.subscribe.service",
    "plugins.v2/cloudsubscribe/core/subscribe/service.py",
).AutoSubscribeService


class TestSubscribeMediaTypeFilter(unittest.TestCase):
    def setUp(self):
        self.owner = MagicMock()
        self.service = AutoSubscribeService(owner=self.owner)

    def test_provider_options_preserves_media_types(self):
        """测试 provider options 正确读取 global media_types，不再被硬转为 all"""
        config = {
            "auto_subscribe_douban_limit": 30,
            "auto_subscribe_media_types": ["movie", "tv"],
            # 模拟历史残留
            "auto_subscribe_media_type": "all",
            "auto_subscribe_douban_media_type": "all",
        }
        options = self.service._provider_options(config, "douban")
        self.assertEqual(options["media_types"], ["movie", "tv"])
        # 不应强行给 options 设置 media_type = all
        self.assertNotIn("media_type", options)

    def test_parse_selected_media_types_prioritizes_media_types(self):
        """测试解析选中类型时，优先读取 media_types，忽略遗留的单值 media_type"""
        options_with_both = {
            "media_types": ["movie", "tv"],
            "media_type": "all",  # 遗留残留
        }
        selected = self.service._parse_selected_media_types(options_with_both)
        self.assertEqual(selected, {"movie", "tv"})

        options_only_legacy = {
            "media_type": "tv",
        }
        selected_legacy = self.service._parse_selected_media_types(options_only_legacy)
        self.assertEqual(selected_legacy, {"tv"})

    def test_anime_filtered_when_anime_tv_not_selected(self):
        """测试用户只勾选电影和电视剧（未勾选动漫番剧）时，番剧在后过滤阶段被拦截"""
        selected_types = {"movie", "tv"}

        # 普通电视剧：放行
        accepted, reason = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=False,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertTrue(accepted)
        self.assertEqual(reason, "")

        # 动漫番剧：拦截！
        accepted, reason = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=True,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertFalse(accepted)
        self.assertIn("未勾选动漫番剧类型", reason)

        # 动漫电影：拦截！
        accepted, reason = self.service._is_media_type_accepted(
            item_media_type="movie",
            is_anime=True,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertFalse(accepted)
        self.assertIn("未勾选动漫电影类型", reason)

    def test_pre_filter_allows_potential_anime_candidates(self):
        """测试用户只勾选动漫番剧（未勾选电视剧）时，普通剧集源在预过滤阶段不被误杀，后过滤精准过滤"""
        selected_types = {"anime_tv"}

        # 预过滤阶段：普通源未知剧集允许放行进入识别
        accepted_pre, _ = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=False,
            selected_types=selected_types,
            source="douban",
            pre_filter=True,
        )
        self.assertTrue(accepted_pre)

        # 后过滤阶段：识别出不是动漫，则被拦截
        accepted_post_normal, reason = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=False,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertFalse(accepted_post_normal)
        self.assertIn("未勾选该媒体类型", reason)

        # 后过滤阶段：识别出是动漫，则放行
        accepted_post_anime, _ = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=True,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertTrue(accepted_post_anime)

    def test_all_media_types_accepted(self):
        """测试勾选全部或保留 all 时全部放行"""
        selected_types = {"all"}
        accepted, _ = self.service._is_media_type_accepted(
            item_media_type="tv",
            is_anime=True,
            selected_types=selected_types,
            source="douban",
            pre_filter=False,
        )
        self.assertTrue(accepted)


if __name__ == "__main__":
    unittest.main()
