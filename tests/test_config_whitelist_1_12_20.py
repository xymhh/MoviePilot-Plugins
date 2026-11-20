"""1.12.20 Phase 1：配置键白名单 + 并发/冷却参数表。

白名单：_apply_plugin_config 的 setattr 循环只把白名单键落到实例属性上；
白名单外的新键直接忽略并记 warning，绝不抛错。白名单 = 默认键（AST 从
core/config.py 的 get_default_config() 顶层键 + utils/notify_template.py
的 DEFAULT_TEMPLATES 键）+ 非默认键（config.get 字面量里不在默认键中的 +
签到契约 credential_keys 等动态键）+ 签到动态键后缀规则。
参数表：CONCURRENCY_AND_COOLDOWN_PARAMS 是九个值的唯一事实来源；
get_default_config() 与 _apply_*_config 的兜底默认值都从表里取，数值不变。

测试用真实 _apply_plugin_config 在裸实例上跑一份完整配置断言落位；
默认键/非默认键的集合由 AST/正则从源码重新算出，与实现里的白名单
交叉校验——实现漏列任何一个键都会让测试失败。
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from test_downloader_plugin_1_9_86 import _load_plugin_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")

#: 签到动态键：{provider}_checkin_enabled / {provider}_checkin_mode，
#: 由签到契约在运行期按渠道生成；后缀规则覆盖，不枚举。
CHECKIN_SUFFIX_KEYS = ("p115_checkin_enabled", "p115_checkin_mode")

#: 非默认键中无 config.get 字面量的动态键：normalize 迁移掉的历史键
#: （normalize 会先删掉它们，进白名单只是兜底）与签到契约的
#: credential_keys（只出现在 definition 里）。
KNOWN_DYNAMIC_EXTRA_KEYS = {
    "unblock_start_time",
    "unblock_end_time",
    "dian115_email",
    "dian115_password",
    "hdhaven_username",
    "hdhaven_password",
    "juying_username",
    "juying_password",
    "pinglian_username",
    "pinglian_password",
    "woniu_username",
    "woniu_password",
}

#: normalize 迁移掉的历史键：白名单允许，但 normalize 会先删掉，
#: 所以 _apply_plugin_config 跑完后不应落到实例属性上。
NORMALIZE_DROPPED_KEYS = {
    "block_platform_downloads",
    "takeover_platform_downloads",
    "unblock_start_time",
    "unblock_end_time",
}

PARAM_KEYS = (
    "cross_transfer_upload_concurrency",
    "cross_transfer_upload_breaker_threshold",
    "cross_transfer_upload_breaker_cooldown_seconds",
    "cross_transfer_max_concurrent",
    "subscription_concurrency",
    "batch_interval",
    "transfer_risk_cooldown",
    "transfer_task_batch_size",
    "cross_transfer_daily_upload_limit_gb",
)

EXPECTED_PARAM_TABLE = {
    "cross_transfer_upload_concurrency": {"default": 2, "kind": "speed"},
    "cross_transfer_upload_breaker_threshold": {"default": 5, "kind": "risk"},
    "cross_transfer_upload_breaker_cooldown_seconds": {"default": 300, "kind": "risk"},
    "cross_transfer_max_concurrent": {"default": 2, "kind": "speed"},
    "subscription_concurrency": {"default": 2, "kind": "speed"},
    "batch_interval": {"default": 3, "kind": "risk"},
    "transfer_risk_cooldown": {"default": 1800, "kind": "risk"},
    "transfer_task_batch_size": {"default": 50, "kind": "speed"},
    "cross_transfer_daily_upload_limit_gb": {"default": 900, "kind": "risk"},
}


def _default_keys() -> set:
    """AST 提取 get_default_config() 的顶层键 + DEFAULT_TEMPLATES 的键。"""
    config_path = os.path.join(PLUGIN_DIR, "core", "config.py")
    tree = ast.parse(open(config_path, encoding="utf-8").read())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "get_default_config":
            for stmt in node.body:
                target = getattr(stmt, "target", None)
                if isinstance(target, ast.Name) and target.id == "config":
                    value = stmt.value
                    if isinstance(value, ast.Dict):
                        for key_node in value.keys:
                            if isinstance(key_node, ast.Constant):
                                keys.add(key_node.value)
    templates_path = os.path.join(PLUGIN_DIR, "utils", "notify_template.py")
    tree = ast.parse(open(templates_path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_TEMPLATES":
                    if isinstance(node.value, ast.Dict):
                        for key_node in node.value.keys:
                            if isinstance(key_node, ast.Constant):
                                keys.add(key_node.value)
    return keys


def _config_get_literals() -> set:
    """正则提取全插件 config.get("...") 字面量键。"""
    pattern = re.compile(r"""config\.get\(\s*["']([^"']+)["']""")
    keys = set()
    for root, _dirs, files in os.walk(PLUGIN_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            keys |= set(pattern.findall(open(path, encoding="utf-8").read()))
    return keys


def _plugin():
    """裸插件实例：_apply_plugin_config 的外围方法全部打桩。"""
    module = _load_plugin_module()
    plugin = module.CloudSubscribeX.__new__(module.CloudSubscribeX)
    plugin._sync_handler = None
    plugin.update_config = lambda config: None
    plugin._get_component = lambda *args, **kwargs: SimpleNamespace(
        install=lambda: None
    )
    plugin._is_takeover_active = lambda: False
    for name in (
        "stop_service",
        "_init_clients",
        "_init_handlers",
        "_migrate_channels_from_config",
        "_apply_base_config",
        "_apply_notification_config",
        "_apply_search_config",
        "_apply_sync_config",
        "_apply_tg_monitor_config",
        "_apply_cloud_config",
        "_apply_download_config",
        "_apply_transfer_config",
        "_apply_subscribe_config",
        "_apply_media_config",
        "_apply_platform_config",
        "_update_offline_monitor",
        "_refresh_platform_services",
        "_install_subscribe_search_takeover",
        "_start_residue_cleanup",
        "_start_history_state_maintenance",
    ):
        setattr(plugin, name, lambda *args, **kwargs: None)
    return plugin, module


class TestConfigWhitelist(unittest.TestCase):
    def test_whitelist_covers_all_known_keys(self):
        """白名单 = 默认键 + 非默认键 + 签到后缀；实现漏列任何一键都失败。"""
        _, module = _plugin()
        defaults = _default_keys()
        self.assertGreater(len(defaults), 200, "默认键集合异常小，AST 提取可能失效")
        extras = (_config_get_literals() - defaults) | KNOWN_DYNAMIC_EXTRA_KEYS
        for key in defaults | extras:
            self.assertTrue(
                module.is_plugin_config_key(key), f"白名单漏键：{key}"
            )
        self.assertEqual(
            set(module.PLUGIN_CONFIG_EXTRA_KEYS), extras,
            "PLUGIN_CONFIG_EXTRA_KEYS 与源码重算的非默认键集合不一致",
        )
        for key in CHECKIN_SUFFIX_KEYS:
            self.assertTrue(module.is_plugin_config_key(key))
        self.assertFalse(module.is_plugin_config_key("__not_a_real_config_key__"))
        self.assertFalse(module.is_plugin_config_key(""))
        self.assertFalse(module.is_plugin_config_key(None))

    def test_complete_config_applies_all_declared_attrs(self):
        """完整配置跑真实 _apply_plugin_config：每个声明属性都落位。"""
        plugin, module = _plugin()
        table = module.CONCURRENCY_AND_COOLDOWN_PARAMS
        defaults = _default_keys()
        extras = (_config_get_literals() - defaults) | KNOWN_DYNAMIC_EXTRA_KEYS
        config = {key: f"__test__{key}" for key in defaults | extras}
        for key in CHECKIN_SUFFIX_KEYS:
            config[key] = True
        # 避免走一次性订阅调度分支；参数表九键用表值，避免哨兵串改行为。
        config["auto_subscribe_onlyonce"] = False
        for key in PARAM_KEYS:
            config[key] = table[key]["default"]
        # media_server_ignore_paths 是哨兵禁区：真实 _apply_plugin_config 会
        # 把它写进 media_scope 的模块级全局 _IGNORE_PREFIXES，之后别的用例
        # 的 hook 路径会走到 _identity_filters，污染全量；用空值。
        config["media_server_ignore_paths"] = ""
        config["__not_a_real_config_key__"] = "evil"
        # 同样是防御：跑完把忽略前缀复位，不留全局副作用。
        self.addCleanup(module.configure_media_path_ignores, "")

        plugin._apply_plugin_config(config, reset_runtime=True)

        surviving = set(module.normalize_plugin_config(config))
        # normalize 只删历史键、不过滤未知键；过滤未知键的是白名单循环。
        landed_expected = {
            key for key in surviving if module.is_plugin_config_key(key)
        }
        self.assertNotIn("__not_a_real_config_key__", landed_expected)
        for key in landed_expected:
            self.assertIn(
                f"_{key}", plugin.__dict__, f"声明属性未落位：_{key}"
            )
        # 白名单外的键绝不落到实例上。
        self.assertNotIn("___not_a_real_config_key__", plugin.__dict__)
        # normalize 删掉的历史键：白名单允许，但不应落位。
        for key in NORMALIZE_DROPPED_KEYS:
            self.assertTrue(module.is_plugin_config_key(key))
            self.assertNotIn(f"_{key}", plugin.__dict__)
        # 参数表九键落位且值就是表值。
        for key in PARAM_KEYS:
            self.assertEqual(plugin.__dict__[f"_{key}"], table[key]["default"])

    def test_quality_prefer_enabled_retired_silently(self):
        """v1.29.4：quality_prefer_enabled 随判定器统一删除。

        旧值由 normalize_plugin_config 在白名单循环之前静默丢弃（不报
        “忽略未知插件配置键”警告、不落实例属性）；默认配置与白名单里
        不再有该键；后端不再读该键；前端 upgrade.js 里不再有该开关；
        阈值键 quality_min_gain_percent / quality_min_gain_mb 仍保留
        （现为评分相等时的阈值参数）。
        """
        plugin, module = _plugin()
        config = {
            "quality_prefer_enabled": True,
            "quality_min_gain_percent": 10,
            "quality_min_gain_mb": 200,
        }
        self.assertNotIn(
            "quality_prefer_enabled",
            module.normalize_plugin_config(dict(config)),
        )
        self.assertNotIn("quality_prefer_enabled", _default_keys())
        self.assertFalse(module.is_plugin_config_key("quality_prefer_enabled"))
        self.assertTrue(module.is_plugin_config_key("quality_min_gain_percent"))
        self.assertTrue(module.is_plugin_config_key("quality_min_gain_mb"))
        plugin._apply_plugin_config(dict(config), reset_runtime=True)
        self.assertNotIn("_quality_prefer_enabled", plugin.__dict__)
        for root, _dirs, files in os.walk(PLUGIN_DIR):
            for name in files:
                if not name.endswith(".py"):
                    continue
                source = open(
                    os.path.join(root, name), encoding="utf-8"
                ).read()
                self.assertNotIn(
                    'config.get("quality_prefer_enabled"', source
                )
                self.assertNotIn(
                    'get_val("quality_prefer_enabled"', source
                )
        with open(
            os.path.join(
                REPO_ROOT,
                "frontend/cloudsubscribex/src/config/fields/upgrade.js",
            ),
            encoding="utf-8",
        ) as handle:
            self.assertNotIn("quality_prefer_enabled", handle.read())

    def test_param_table_values_unchanged(self):
        """参数表九个值与 kind 一个都不许变（只收拢定义位置）。"""
        _, module = _plugin()
        table = module.CONCURRENCY_AND_COOLDOWN_PARAMS
        self.assertEqual(set(table), set(EXPECTED_PARAM_TABLE))
        for key, expected in EXPECTED_PARAM_TABLE.items():
            self.assertEqual(table[key]["default"], expected["default"], key)
            self.assertEqual(table[key]["kind"], expected["kind"], key)

    def test_defaults_and_fallbacks_read_from_table(self):
        """源码级防护：默认值与 _apply 兜底必须从表里取，不能再写死字面量。"""
        config_path = os.path.join(PLUGIN_DIR, "core", "config.py")
        config_text = open(config_path, encoding="utf-8").read()
        for key in PARAM_KEYS:
            self.assertIn(
                f'"{key}": CONCURRENCY_AND_COOLDOWN_PARAMS["{key}"]["default"]',
                config_text,
                f"get_default_config 的 {key} 未从参数表取",
            )
        init_path = os.path.join(PLUGIN_DIR, "__init__.py")
        init_text = open(init_path, encoding="utf-8").read()
        for key, literal in (
            ("cross_transfer_daily_upload_limit_gb", "900"),
            ("transfer_task_batch_size", "50"),
            ("cross_transfer_max_concurrent", "2"),
            ("subscription_concurrency", "2"),
            ("batch_interval", "3"),
            ("transfer_risk_cooldown", "1800"),
        ):
            self.assertNotIn(
                f'config.get("{key}", {literal})', init_text,
                f"_apply 兜底仍写死字面量：{key}",
            )
            self.assertIn(
                f'CONCURRENCY_AND_COOLDOWN_PARAMS["{key}"]["default"]',
                init_text,
                f"_apply 兜底未从参数表取：{key}",
            )


if __name__ == "__main__":
    unittest.main()
