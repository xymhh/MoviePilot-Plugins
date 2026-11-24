"""1.33.0：同集重复入库治理（A 陈旧 .strm 清扫 / B 入队时间窗 / C 上传前查重）。

背景：现网 85 组重复 .strm（48 组同集两份不同视频、37 组同一视频两个
.strm、46 组至少一份已对不上 115 视频），Emby 里同一集出现 3 个条目；
同时同一集被反复真实转存（无声的回响 S01E07 一小时内传了三次）。

覆盖：
A. `sweep_orphan_strms`：删「本地有 .strm、115 目录树里已无对应视频」的
   陈旧指针；期望集合内的保留；待删比例超安全阀时整轮放弃。
B（独立文件 `test_funnel_recent_done_1_33_0.py`，需真实 SQLite 夹具）：
   `FunnelQueueRepository.enqueue(recent_done_window_seconds=...)`。
C. `TargetWriter._probe_same_stem`：精确名未命中时按同主干查一次，
   已有同大小或更大版本判 reused（不重复上传），只有更小版本则继续上传。
"""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from plugin_env import ensure_package, install_app_mocks, load_module

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()
ensure_package("cloudsubscribex")
ensure_package("cloudsubscribex.core")
ensure_package("cloudsubscribex.utils")

strm_export = load_module(
    "cloudsubscribex.utils.strm_export",
    "plugins.v2/cloudsubscribex/utils/strm_export.py",
)
cloud_module = load_module(
    "cloudsubscribex.core.cloud",
    "plugins.v2/cloudsubscribex/core/cloud.py",
)
target_writer_module = load_module(
    "cloudsubscribex.core.target_writer",
    "plugins.v2/cloudsubscribex/core/target_writer.py",
)

LOCAL_ROOT = "/local/strm/115"
CLOUD_ROOT = "/Alist/媒体库"
SEASON_CLOUD_DIR = f"{CLOUD_ROOT}/剧集/某剧 (2026) {{tmdb-1}}/Season 1"
SEASON_LOCAL_DIR = "/local/strm/115/剧集/某剧 (2026) {tmdb-1}/Season 1"


class _FakeGenerator:
    """把 cloud_dir 按 cloud_root 镜像到 local_root 下。"""

    @staticmethod
    def local_path(local_root, cloud_root, cloud_dir, file_name):
        cloud_dir = str(cloud_dir or "")
        cloud_root = str(cloud_root or "")
        rel = cloud_dir[len(cloud_root):] if cloud_dir.startswith(cloud_root) else cloud_dir
        base = Path(local_root) / rel.strip("/") if rel.strip("/") else Path(local_root)
        return str(base / str(file_name or ""))


def _write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class OrphanStrmSweepTest(unittest.TestCase):
    def test_stale_strm_is_deleted(self):
        with TemporaryDirectory() as tmp:
            gen = _FakeGenerator()
            local_root = os.path.join(tmp, "115")
            keep = _write(
                Path(local_root) / "剧集/某剧 (2026) {tmdb-1}/Season 1/某剧 - S01E01 - 第1集.strm"
            )
            stale = _write(
                Path(local_root) / "剧集/某剧 (2026) {tmdb-1}/Season 1/某剧 - S01E01.1080p.strm"
            )

            deleted = strm_export.sweep_orphan_strms(
                gen, local_root, CLOUD_ROOT, [str(keep.resolve())],
            )

            self.assertEqual(deleted, [stale])
            self.assertTrue(keep.exists())
            self.assertFalse(stale.exists())

    def test_expected_files_are_kept(self):
        with TemporaryDirectory() as tmp:
            gen = _FakeGenerator()
            local_root = os.path.join(tmp, "115")
            one = _write(Path(local_root) / "a/S01/某剧 - S01E01 - 第1集.strm")
            two = _write(Path(local_root) / "a/S01/某剧 - S01E02 - 第2集.strm")

            deleted = strm_export.sweep_orphan_strms(
                gen, local_root, CLOUD_ROOT,
                [str(one.resolve()), str(two.resolve())],
            )

            self.assertEqual(deleted, [])
            self.assertTrue(one.exists())
            self.assertTrue(two.exists())

    def test_ratio_guard_aborts_when_tree_looks_partial(self):
        # 样本足够大（>=20）且待删比例超 30% 时整轮放弃：目录树可能没导全，
        # 这时删就是把能播的指针删掉。小样本（<20）不受安全阀限制——
        # 库里只有两三份时按树删是安全的。
        with TemporaryDirectory() as tmp:
            gen = _FakeGenerator()
            local_root = os.path.join(tmp, "115")
            season = Path(local_root) / "a/S01"
            keep = _write(season / "某剧 - S01E01 - 第1集.strm")
            for index in range(2, 26):
                _write(season / f"某剧 - S01E{index:02d} - 第{index}集.strm")

            deleted = strm_export.sweep_orphan_strms(
                gen, local_root, CLOUD_ROOT, [str(keep.resolve())],
            )

            self.assertEqual(deleted, [])
            self.assertEqual(len(list(season.glob("*.strm"))), 25)

    def test_empty_expected_is_noop(self):
        with TemporaryDirectory() as tmp:
            gen = _FakeGenerator()
            local_root = os.path.join(tmp, "115")
            path = _write(Path(local_root) / "a/S01/某剧 - S01E01 - 第1集.strm")

            self.assertEqual(
                strm_export.sweep_orphan_strms(gen, local_root, CLOUD_ROOT, []), []
            )
            self.assertTrue(path.exists())


class SameStemUploadProbeTest(unittest.TestCase):
    def _writer(self, listing, found=None):
        service = SimpleNamespace(
            find_file=mock.MagicMock(return_value=found),
            list_files_recursive=mock.MagicMock(return_value=listing),
        )
        provider = SimpleNamespace(require=mock.MagicMock(return_value=service))
        writer = target_writer_module.TargetWriter(lambda key: provider)
        return writer, service

    @staticmethod
    def _plan(size=100):
        return target_writer_module.TargetPlan(
            provider_key="p115",
            path=SEASON_CLOUD_DIR,
            name="某剧 - S01E07 - 第7集.mkv",
            size=size,
        )

    def test_larger_same_stem_version_is_reused(self):
        bigger = SimpleNamespace(
            name="某剧 - S01E07 - 第7集.mp4", size=200,
            is_directory=False, sha1="", md5="",
        )
        writer, _service = self._writer([bigger])

        result = writer.probe(self._plan(100))

        self.assertEqual(result.status, "reused")
        self.assertEqual(result.actual_name, "某剧 - S01E07 - 第7集.mp4")
        self.assertIn("同集已有", result.reason)

    def test_smaller_same_stem_version_still_uploads(self):
        smaller = SimpleNamespace(
            name="某剧 - S01E07 - 第7集.mp4", size=50,
            is_directory=False, sha1="", md5="",
        )
        writer, _service = self._writer([smaller])

        result = writer.probe(self._plan(100))

        self.assertEqual(result.status, "not_written")

    def test_other_episodes_do_not_interfere(self):
        other = SimpleNamespace(
            name="某剧 - S01E08 - 第8集.mkv", size=500,
            is_directory=False, sha1="", md5="",
        )
        writer, _service = self._writer([other])

        result = writer.probe(self._plan(100))

        self.assertEqual(result.status, "not_written")


if __name__ == "__main__":
    unittest.main()
