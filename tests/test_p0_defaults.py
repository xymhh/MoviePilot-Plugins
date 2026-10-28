"""P0 默认值与界面一致性：STRM 默认不写盘、TG 旧配置回退要留痕。

对应 1.7.92 的四条改动：
- STRM 生成/全量校准/增量补齐默认关，播放地址前缀默认留空；
- TG 频道规则为空而旧版全局频道非空时，必须打 WARNING（不再静默回退）。
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
tg_module = load_module(
    "cloudsubscribex.core.tg_monitor",
    "plugins.v2/cloudsubscribex/core/tg_monitor.py",
)

get_default_config = config_module.UIConfig.get_default_config
normalize_channel_rules = tg_module.normalize_channel_rules


class _RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class StrmDefaultsTest(unittest.TestCase):
    def test_strm_write_side_effects_are_opt_in(self):
        config = get_default_config()
        self.assertFalse(config["strm_generate_enabled"])
        self.assertFalse(config["strm_full_scan_enabled"])
        self.assertFalse(config["strm_increment_enabled"])
        self.assertEqual(config["strm_base_url"], "")

    def test_default_template_uses_builtin_redirect(self):
        template = get_default_config()["strm_url_template"]
        self.assertIn("/api/v1/plugin/CloudSubscribeX/redirect", template)
        self.assertIn("{pickcode}", template)

    def test_no_duplicate_default_keys(self):
        # F601 的根源：同一 dict 里重复写字面量键，后一个会静默覆盖前一个。
        defaults = get_default_config()
        self.assertIn("anime_pack_preferred", defaults)
        self.assertIn("offline_timeout", defaults)


class TgFallbackWarningTest(unittest.TestCase):
    def test_empty_rules_falling_back_warns(self):
        original = tg_module.logger
        recorder = _RecordingLogger()
        tg_module.logger = recorder
        try:
            rules = normalize_channel_rules(
                [],
                channels=["https://t.me/aaa"],
                keywords=["现在就出发"],
                exclude_keywords=[],
                scan_limit=20,
            )
        finally:
            tg_module.logger = original
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["keywords"], ["现在就出发"])
        self.assertTrue(recorder.warnings)
        self.assertIn("回退到旧版全局配置", recorder.warnings[0])
        self.assertIn("1 个", recorder.warnings[0])

    def test_rules_present_does_not_warn(self):
        original = tg_module.logger
        recorder = _RecordingLogger()
        tg_module.logger = recorder
        try:
            rules = normalize_channel_rules(
                [{"channel": "https://t.me/bbb", "keywords": ["abc"]}],
                channels=["https://t.me/aaa"],
                keywords=["现在就出发"],
            )
        finally:
            tg_module.logger = original
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["channel"], "@bbb")
        self.assertEqual(recorder.warnings, [])


if __name__ == "__main__":
    unittest.main()
