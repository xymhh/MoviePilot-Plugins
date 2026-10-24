"""STRM 全量校准单测（按 cid 遍历 + 只补缺失/可选覆盖）。"""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from plugin_env import ensure_package, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
utils = ensure_package("cloudsubscribex.utils")
utils.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "utils")]

strm_module = load_module(
    "cloudsubscribex.utils.strm", "plugins.v2/cloudsubscribex/utils/strm.py",
)
scan_module = load_module(
    "cloudsubscribex.utils.strm_scan",
    "plugins.v2/cloudsubscribex/utils/strm_scan.py",
)
StrmGenerator = strm_module.StrmGenerator
rebuild_strm_tree = scan_module.rebuild_strm_tree


class Node:
    def __init__(self, id, name, is_directory=False, pickcode=""):
        self.id = id
        self.name = name
        self.is_directory = is_directory
        self.playback_values = {"pickcode": pickcode} if pickcode else {}


TREE = {
    "root": [
        Node("1", "Show", is_directory=True),
        Node("m1", "poster.jpg"),
    ],
    "1": [
        Node("2", "Season 01", is_directory=True),
        Node("f0", "Show.strm"),
    ],
    "2": [
        Node("f1", "E01.mkv", pickcode="pc1"),
        Node("f2", "E02.mkv", pickcode="pc2"),
        Node("f3", "notes.txt"),
    ],
}


class FakeProvider:
    def resolve_directory(self, path):
        return SimpleNamespace(checked=True, directory_id="root")

    def list_directory(self, cid):
        return SimpleNamespace(checked=True, files=tuple(TREE[cid]))


def values(item):
    return dict(item.playback_values)


class ContainerProvider:
    """模拟 CloudDriveProvider 能力容器：自身没有目录方法，只能 require 取服务。"""

    def __init__(self, inner):
        self.inner = inner
        self.required = []

    def supports(self, capability):
        return True

    def require(self, capability):
        self.required.append(capability)
        return self.inner


class StrmRenderEncodingTest(unittest.TestCase):
    """1.7.46 回归：文件名/路径必须 URL 编码，否则播放请求行非法。"""

    def test_chinese_and_space_file_name_is_encoded(self):
        generator = StrmGenerator(
            base_url="http://mp:3000/api/v1/plugin/CloudSubscribeX/redirect",
            template="{base_url}?pickcode={pickcode}&file_name={file_name}",
            provider_variables=("pickcode",),
        )
        url = generator.render(
            file_name="伟大的长征 - S01E25 - 第25集.mkv",
            file_path="/Alist/影视/伟大的长征 - S01E25.mkv",
            template_values={"pickcode": "abcdefghijklmno12"},
        )
        self.assertNotIn(" ", url)
        self.assertTrue(url.startswith("http://mp:3000/api/v1/plugin/CloudSubscribeX/redirect?pickcode="))
        self.assertIn("%20", url)
        self.assertIn("S01E25", url)

    def test_file_path_keeps_slashes_when_encoded(self):
        generator = StrmGenerator(
            base_url="http://mp:3000",
            template="{base_url}{file_path}",
        )
        url = generator.render(
            file_name="A B.mkv",
            file_path="/影视/剧集/A B.mkv",
        )
        self.assertIn("/%E5%BD%B1%E8%A7%86/", url)
        self.assertIn("/A%20B.mkv", url)
        self.assertNotIn("%2F", url)


class StrmFullScanTest(unittest.TestCase):
    def _generator(self):
        return StrmGenerator(
            base_url="http://proxy:9527",
            template="{base_url}/d/{pickcode}?/{file_name}",
            provider_variables=("pickcode",),
        )

    def test_fills_missing_and_skips_existing(self):
        with TemporaryDirectory() as tmp:
            existing = Path(tmp) / "Show" / "Season 01" / "E02.strm"
            existing.parent.mkdir(parents=True)
            existing.write_text("old", encoding="utf-8")
            stats = rebuild_strm_tree(
                provider=FakeProvider(),
                generator=self._generator(),
                local_root=tmp,
                cloud_root="/media",
                video_extensions=[".mkv"],
                reference_values=values,
            )
            self.assertEqual(stats["directories"], 3)
            self.assertEqual(stats["files"], 2)
            self.assertEqual(stats["created"], 1)
            self.assertEqual(stats["skipped"], 1)
            created = Path(tmp) / "Show" / "Season 01" / "E01.strm"
            self.assertEqual(
                created.read_text(encoding="utf-8"),
                "http://proxy:9527/d/pc1?/E01.mkv",
            )
            # 未开启覆盖：旧内容保持原样
            self.assertEqual(existing.read_text(encoding="utf-8"), "old")

    def test_provider_container_is_unwrapped_before_scan(self):
        """1.7.44 回归：provider 是能力容器时必须先 require(DIRECTORY_READ)。

        线上症状：STRM 增量补齐每轮抛
        `'CloudDriveProvider' object has no attribute 'resolve_directory'`。
        """
        container = ContainerProvider(FakeProvider())
        with TemporaryDirectory() as tmp:
            stats = rebuild_strm_tree(
                provider=container,
                generator=self._generator(),
                local_root=tmp,
                cloud_root="/media",
                video_extensions=[".mkv"],
                reference_values=values,
            )
            self.assertEqual(stats["created"], 2)
            self.assertTrue(container.required)
            self.assertTrue((Path(tmp) / "Show" / "Season 01" / "E01.strm").is_file())

    def test_overwrite_rewrites_changed_content(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "Show" / "Season 01" / "E01.strm"
            target.parent.mkdir(parents=True)
            target.write_text("old", encoding="utf-8")
            stats = rebuild_strm_tree(
                provider=FakeProvider(),
                generator=self._generator(),
                local_root=tmp,
                cloud_root="/media",
                video_extensions=[".mkv"],
                reference_values=values,
                overwrite=True,
            )
            self.assertEqual(stats["updated"], 1)
            self.assertEqual(stats["created"], 1)
            self.assertIn("/d/pc1?", target.read_text(encoding="utf-8"))

    def test_unresolvable_root_reports_error(self):
        class Broken(FakeProvider):
            def resolve_directory(self, path):
                return SimpleNamespace(checked=False, directory_id=None)

        stats = rebuild_strm_tree(
            provider=Broken(),
            generator=self._generator(),
            local_root="/tmp",
            cloud_root="/media",
            video_extensions=[".mkv"],
            reference_values=values,
        )
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["created"], 0)


if __name__ == "__main__":
    unittest.main()
