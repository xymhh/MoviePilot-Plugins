"""Phase 4 附加小修：磁力路径 STRM 生成缺 pickcode 时按最终文件名刷新目标文件。

覆盖 StrmOpsMixin._generate_strm（真实 StrmGenerator，模板含 {pickcode}）：
- 传入的 target_file 没有 pickcode（move 接口返回的 matched 文件，
  2026-11-18 秒速5厘米实测）→ 按文件名 find_file 刷新一次，用带
  pickcode 的文件生成 STRM，文件落盘且内容含 pickcode；
- 传入的 target_file 已带 pickcode → 不刷新（find_file 不被调用）；
- 刷新后仍无 pickcode → 仍按原来走，生成失败返回 None，不抛异常。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (  # noqa: E402
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core", "app.core.context",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.utils",
)
install_core_delegation()

_utils = ensure_package("cloudsubscribex.utils")
_strm = load_module(
    "cloudsubscribex.utils.strm",
    "plugins.v2/cloudsubscribex/utils/strm.py",
)
_utils.StrmTemplateError = _strm.StrmTemplateError
_utils.plan_share_strm_files = _strm.plan_share_strm_files
_utils.rebuild_strm_tree = MagicMock()
_utils.rebuild_strm_by_export_tree = MagicMock()
load_module(
    "cloudsubscribex.utils.legacy_strm",
    "plugins.v2/cloudsubscribex/utils/legacy_strm.py",
)
# CloudFile 只出现在 strm_ops 的注解里，给一个真类型即可。
ensure_package("cloudsubscribex.core").CloudFile = type("CloudFile", (), {})

_strm_ops = load_module(
    "cloudsubscribex.handlers.sync.strm_ops",
    "plugins.v2/cloudsubscribex/handlers/sync/strm_ops.py",
)
StrmOpsMixin = _strm_ops.StrmOpsMixin
StrmGenerator = _strm.StrmGenerator

CLOUD_DIR = "/媒体库/电影/外语电影/秒速5厘米 (2025) {tmdbid=1359916}"
FILE_NAME = "秒速5厘米 (2025) {tmdb-1359916}.1080p.BluRay.x265 10bit.DTS.mkv"
PICKCODE = "abcdef1234567890"


class _FakePlaybackReference:
    """与 P115PlaybackReference 同契约：pickcode 是唯一模板变量。"""

    template_variables = frozenset({"pickcode"})

    @staticmethod
    def reference_values(file_item):
        return dict(file_item.playback_values)


class _FakeCloudQuery:
    def __init__(self, refreshed):
        self._refreshed = refreshed
        self.find_calls = []

    def find_file(self, cloud_dir, file_name):
        self.find_calls.append((cloud_dir, file_name))
        return self._refreshed


def _file(playback_values):
    return SimpleNamespace(
        name=FILE_NAME, playback_values=dict(playback_values),
    )


class StrmPickcodeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.generator = StrmGenerator(
            base_url="http://127.0.0.1:9527",
            template=StrmGenerator.REDIRECT_TEMPLATE,
            provider_variables=["pickcode"],
        )

    def _service(self, query):
        service = StrmOpsMixin(SimpleNamespace())
        service._strm_generate_enabled = True
        service._strm_generator = self.generator
        service._local_resource_path = str(Path(self.tempdir.name))
        service._CLOUD_MEDIA_ROOT = "/"
        service._playback_reference = _FakePlaybackReference()
        service._cloud_query = query
        return service

    def test_missing_pickcode_refreshes_and_writes_strm(self):
        """move 返回的文件无 pickcode → 刷新一次，STRM 落盘且含 pickcode。"""
        query = _FakeCloudQuery(_file({"pickcode": PICKCODE}))
        service = self._service(query)
        result = StrmOpsMixin._generate_strm(
            service, CLOUD_DIR, FILE_NAME,
            target_file=_file({}), lookup_target=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(query.find_calls, [(CLOUD_DIR, FILE_NAME)])
        self.assertTrue(Path(result).is_file())
        content = Path(result).read_text(encoding="utf-8")
        self.assertIn(f"pickcode={PICKCODE}", content)

    def test_pickcode_present_skips_refresh(self):
        """传入文件已有 pickcode → 不刷新，直接生成。"""
        query = _FakeCloudQuery(_file({"pickcode": "should-not-be-used"}))
        service = self._service(query)
        result = StrmOpsMixin._generate_strm(
            service, CLOUD_DIR, FILE_NAME,
            target_file=_file({"pickcode": PICKCODE}), lookup_target=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(query.find_calls, [])
        content = Path(result).read_text(encoding="utf-8")
        self.assertIn(f"pickcode={PICKCODE}", content)

    def test_refresh_still_missing_returns_none(self):
        """刷新后仍无 pickcode → 生成失败返回 None，不抛异常。"""
        query = _FakeCloudQuery(_file({}))
        service = self._service(query)
        result = StrmOpsMixin._generate_strm(
            service, CLOUD_DIR, FILE_NAME,
            target_file=_file({}), lookup_target=False,
        )
        self.assertIsNone(result)
        self.assertEqual(query.find_calls, [(CLOUD_DIR, FILE_NAME)])

    def test_refresh_not_found_returns_none(self):
        """刷新查无文件 → 生成失败返回 None，不抛异常。"""
        query = _FakeCloudQuery(None)
        service = self._service(query)
        result = StrmOpsMixin._generate_strm(
            service, CLOUD_DIR, FILE_NAME,
            target_file=_file({}), lookup_target=False,
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
