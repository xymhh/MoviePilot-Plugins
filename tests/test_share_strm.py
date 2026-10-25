"""115 分享型 STRM 单测：换链参数、STRM 内容、目标路径规划。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

from plugin_env import ensure_package, load_module  # noqa: E402

ensure_package("app")
ensure_package("app.log")

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]

strm_module = load_module(
    "cloudsubscribex.utils.strm", "plugins.v2/cloudsubscribex/utils/strm.py",
)

build_share_strm_url = strm_module.build_share_strm_url
plan_share_strm_files = strm_module.plan_share_strm_files
StrmGenerator = strm_module.StrmGenerator

VIDEO_EXT = {".mkv", ".mp4", ".ts"}


class BuildShareStrmUrlTest(unittest.TestCase):
    def test_contains_redirect_path_and_params(self):
        url = build_share_strm_url(
            "http://moviepilot-v3:3000/", "sw1abc", "abcd", "9001", "S01E01.mkv",
        )
        self.assertEqual(
            url,
            "http://moviepilot-v3:3000/api/v1/plugin/CloudSubscribeX/redirect"
            "?share_code=sw1abc&receive_code=abcd&id=9001&file_name=S01E01.mkv",
        )

    def test_file_name_optional_and_quoted(self):
        url = build_share_strm_url("http://h:1", "s", "abcd", "1")
        self.assertNotIn("file_name=", url)
        url2 = build_share_strm_url("http://h:1", "s", "abcd", "1", "剧 集.mkv")
        self.assertIn("file_name=%E5%89%A7%20%E9%9B%86.mkv", url2)

    def test_empty_base_url_raises(self):
        with self.assertRaises(Exception):
            build_share_strm_url("", "s", "abcd", "1")


class PlanShareStrmFilesTest(unittest.TestCase):
    def test_maps_relative_path_and_skips_non_video(self):
        files = [
            {"id": "1", "name": "S01E01.mkv", "relative_path": "某剧/S01E01.mkv"},
            {"id": "2", "name": "cover.jpg", "relative_path": "某剧/cover.jpg"},
            {"id": "", "name": "broken.mkv", "relative_path": "broken.mkv"},
        ]
        planned, skipped = plan_share_strm_files(
            "/strm/115网盘分享", files, "http://h:3000", "s1", "abcd", VIDEO_EXT,
        )
        self.assertEqual(len(planned), 1)
        self.assertEqual(skipped, 1)
        path, content = planned[0]
        self.assertEqual(str(path), "/strm/115网盘分享/某剧/S01E01.strm")
        self.assertIn("share_code=s1", content)

    def test_uppercase_extension_accepted(self):
        files = [{"id": "1", "name": "A.MKV", "relative_path": "A.MKV"}]
        planned, skipped = plan_share_strm_files(
            "/tmp", files, "http://h", "s", "abcd", VIDEO_EXT,
        )
        self.assertEqual(len(planned), 1)
        self.assertEqual(skipped, 0)


if __name__ == "__main__":
    unittest.main()
