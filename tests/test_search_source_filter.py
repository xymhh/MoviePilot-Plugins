"""抽屉/资源页搜索的渠道口径回归测试（1.9.62）。

回归背景：线上后台只勾了 ``search_source_order=['pansou']``，但抽屉搜索
调用的是 ``get_available_sources_meta()``（返回全部已注册渠道），结果 106 条
候选里 35 条 HDHive + 21 条 Dian115，全是「需积分解锁、url 为空」的无效项，
点转存只会报错。这里锁住两件事：
1. 只保留后台启用的渠道，且顺序按启用顺序；
2. 未启用的渠道不出现在结果里，重复项不重复返回。
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

install_app_mocks()
package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core_package = ensure_package("cloudsubscribex.core")
core_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
api_package = ensure_package("cloudsubscribex.core.api")
api_package.__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")
]

_search_api = load_module(
    "cloudsubscribex.core.api.source_filter",
    "plugins.v2/cloudsubscribex/core/api/source_filter.py",
)


META = [
    {"key": "hdhive", "name": "HDHive"},
    {"key": "pansou", "name": "PanSou"},
    {"key": "dian115", "name": "Dian115"},
    {"key": "hdhaven", "name": "HDHaven"},
]


class FilterSourceMetaTest(unittest.TestCase):
    def test_only_enabled_sources_kept(self):
        kept = _search_api.filter_source_meta_by_enabled(META, ["pansou"])
        self.assertEqual([item["key"] for item in kept], ["pansou"])

    def test_order_follows_enabled_priority(self):
        kept = _search_api.filter_source_meta_by_enabled(
            META, ["pansou", "hdhive"]
        )
        self.assertEqual([item["key"] for item in kept], ["pansou", "hdhive"])

    def test_unknown_and_duplicate_keys_ignored(self):
        kept = _search_api.filter_source_meta_by_enabled(
            META, ["pansou", "pansou", "not-exist"]
        )
        self.assertEqual([item["key"] for item in kept], ["pansou"])

    def test_empty_enabled_returns_empty(self):
        self.assertEqual(_search_api.filter_source_meta_by_enabled(META, []), [])
        self.assertEqual(_search_api.filter_source_meta_by_enabled(META, None), [])

    def test_case_insensitive_match(self):
        kept = _search_api.filter_source_meta_by_enabled(META, ["PanSou"])
        self.assertEqual([item["key"] for item in kept], ["pansou"])


if __name__ == "__main__":
    unittest.main()
