"""SeedHub 渠道回归测试（1.9.63）。

背景：站点 2026-11 起迁到 ``sidhub.cc``（旧域名整站 302），同时把
Cloudflare 防护升级成交互式 Turnstile。旧的单次浏览器快照拿到的仍是
质询页，线上表现为「SeedHub 浏览器仿真未通过 Cloudflare 验证」、
SeedHub 恒 0 条。
"""

import os
import unittest

from plugin_env import load_module

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_urls = load_module(
    "seedhub_urls",
    os.path.join(
        _project_root, "plugins.v2/cloudsubscribex/search/seedhub/urls.py"
    ),
)


class NormalizeBaseUrlTest(unittest.TestCase):
    def test_legacy_seedhub_cc_maps_to_sidhub(self):
        for url in (
            "https://www.seedhub.cc",
            "https://seedhub.cc",
            "http://www.seedhub.cc/",
            "https://www.seedhub.top",
            "https://www.seedhub.pro",
            "https://seedhub.icu",
        ):
            self.assertEqual(
                _urls.normalize_base_url(url), "https://sidhub.cc", url
            )

    def test_custom_url_kept(self):
        self.assertEqual(
            _urls.normalize_base_url("https://mirror.example.com/"), 
            "https://mirror.example.com",
        )

    def test_empty_url_falls_back(self):
        self.assertEqual(_urls.normalize_base_url(""), "https://sidhub.cc")
        self.assertEqual(_urls.normalize_base_url(None), "https://sidhub.cc")

    def test_sidhub_not_rewritten(self):
        self.assertEqual(
            _urls.normalize_base_url("https://sidhub.cc"), "https://sidhub.cc"
        )


if __name__ == "__main__":
    unittest.main()
