"""P1 死配置清理：删掉的三条死键不许再回来。

对应 1.7.93：
- ``auto_subscribe_maoyan_categories``：只有默认值，全仓无读取点；
- ``auto_subscribe_douban_rsshub_base`` / ``auto_subscribe_douban_rss_urls``：
  只在配置归一化里流转，豆瓣实际走宿主 ``RecommendChain``，值从未被消费；
- 顺带删掉同样无人调用的 ``UIConfig._normalize_rsshub_instance_url``。

保留这些键只会让人以为「配了有效」，实际什么都不发生 —— 所以用测试钉住。
"""

import os
import sys
import unittest

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.db", "app.db.site_oper", "app.db.subscribe_oper",
    "app.helper", "app.helper.mediaserver",
)
sys.modules["app.db"].SessionFactory = lambda: None
sys.modules["app.db.site_oper"].SiteOper = type("SiteOper", (), {})
sys.modules["app.db.subscribe_oper"].SubscribeOper = type("SubscribeOper", (), {})

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core = ensure_package("cloudsubscribex.core")
core.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
utils = ensure_package("cloudsubscribex.utils")
utils.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "utils")]

load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)
load_module(
    "cloudsubscribex.core.media",
    "plugins.v2/cloudsubscribex/core/media.py",
)

config_module = load_module(
    "cloudsubscribex.core.config",
    "plugins.v2/cloudsubscribex/core/config.py",
)

DEAD_KEYS = (
    "auto_subscribe_maoyan_categories",
    "auto_subscribe_douban_rsshub_base",
    "auto_subscribe_douban_rss_urls",
)


class DeadConfigKeysTest(unittest.TestCase):
    def test_dead_keys_not_in_defaults(self):
        defaults = config_module.UIConfig.get_default_config()
        for key in DEAD_KEYS:
            self.assertNotIn(key, defaults, f"{key} 是死键，不应再出现在默认配置里")

    def test_dead_helper_removed(self):
        self.assertFalse(
            hasattr(config_module.UIConfig, "_normalize_rsshub_instance_url"),
            "无人调用的 _normalize_rsshub_instance_url 应已删除",
        )

    def test_normalize_tolerates_legacy_payload(self):
        """老配置里残留了死键时，normalize_config 不能报错、也不能把值当真。"""
        payload = {
            "auto_subscribe_maoyan_categories": ["tv"],
            "auto_subscribe_douban_rsshub_base": "https://rsshub.app",
            "auto_subscribe_douban_rss_urls": ["https://rsshub.app/douban/movie/1"],
        }
        normalized = config_module.UIConfig.normalize_config(dict(payload))
        self.assertIsInstance(normalized, dict)
        # 归一化确实跑过了（会补齐 mikan 订阅源默认值），但不会消费这些死键
        self.assertTrue(normalized.get("mikan_fansub_order"))


if __name__ == "__main__":
    unittest.main()
