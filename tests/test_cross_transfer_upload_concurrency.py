"""1.6.22 跨盘提速单测：115 分片大小、分片并发上传、fd 上限收敛。

对应 2026-10-10 线上事故：
* `partsize=-1` 只分到 400KB，3.8GB 文件 9317 片顺序 PUT，任一片抖动整单失败；
* 分片顺序上传 = 单连接，夸克单连接 100~200KB/s，跨盘直传被拖到 54.6KB/s；
* 下载线程数 256 撞爆容器 soft nofile 1024，出现 Errno 24 并掉速。
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core", "app.utils",
    "cloudsubscribe", "cloudsubscribe.core", "cloudsubscribe.drive",
    "cloudsubscribe.drive.p115",
)
ensure_package("cloudsubscribe.core").OwnerDelegator = OwnerDelegator

# 目录解析依赖用桩替换，避免引入真实 115 客户端。
files_module = ensure_package("cloudsubscribe.drive.p115.files")
files_module.P115DirectoryReader = MagicMock()
files_module.P115FileService = MagicMock()

upload_module = load_module(
    "cloudsubscribe.drive.p115.upload",
    "plugins.v2/cloudsubscribe/drive/p115/upload.py",
)


class FakeOwner:
    """最小 owner：只提供上传组件需要的能力。"""

    def __init__(self, upload_concurrency: int = 4):
        self.client = MagicMock()
        self._cross_transfer_upload_concurrency = upload_concurrency
        self._target_file_cache = MagicMock()
        self.rate_limited_calls = []

    def _get_component(self, _component):
        return MagicMock()

    def _rate_limited_call(self, func, *args, **kwargs):
        self.rate_limited_calls.append(func)
        return func(*args, **kwargs)


class FakeRangeReader:
    """替身源盘读取器：按 range 生成可校验的字节。"""

    instances = []

    def __init__(self, link, size, stop_requested=None):
        self.link = link
        self.size = size
        self.stop_requested = stop_requested
        self.ranges = []
        self.closed = False
        FakeRangeReader.instances.append(self)

    def read_range(self, start, length):
        end = min(self.size, start + length) - 1
        self.ranges.append((start, end))
        return bytes((start + index) % 251 for index in range(end - start + 1))

    def close(self):
        self.closed = True


class UploadPartSizeTest(unittest.TestCase):
    def test_default_part_size_is_16mb(self):
        self.assertEqual(upload_module.upload_part_size(0), 16 * 1024 * 1024)
        self.assertEqual(
            upload_module.upload_part_size(3_803_266_431), 16 * 1024 * 1024
        )

    def test_huge_file_keeps_parts_below_oss_limit(self):
        total = 16 * 1024 * 1024 * 9000 * 3 + 1
        part_size = upload_module.upload_part_size(total)
        parts = -(-total // part_size)
        self.assertLessEqual(parts, upload_module.P115_MAX_UPLOAD_PARTS)
        self.assertEqual(part_size % (16 * 1024 * 1024), 0)

    def test_three_point_eight_gb_no_longer_9317_parts(self):
        part_size = upload_module.upload_part_size(3_803_266_431)
        self.assertEqual(-(-3_803_266_431 // part_size), 227)


class Sha1CacheTest(unittest.TestCase):
    def test_same_range_reads_source_once(self):
        reader = upload_module._HttpRangeReader("https://example.com/f", {}, 4096)
        calls = []

        def fake_read(size=-1):
            calls.append(size)
            return b"x" * 8

        reader.read = fake_read
        expected = hashlib.sha1(b"x" * 8).hexdigest().upper()
        self.assertEqual(reader.read_range_sha1("0-7"), expected)
        self.assertEqual(reader.read_range_sha1("0-7"), expected)
        self.assertEqual(len(calls), 1, "同一段 sha1 应命中缓存，不重复读源盘")
        reader.read_range_sha1("8-15")
        self.assertEqual(len(calls), 2, "不同段仍需真实读取")
        reader.close()

    def test_out_of_range_rejected(self):
        reader = upload_module._HttpRangeReader("https://example.com/f", {}, 1024)
        with self.assertRaises(ValueError):
            reader.read_range_sha1("0-1024")
        reader.close()


class ConcurrentUploadTest(unittest.TestCase):
    def setUp(self):
        FakeRangeReader.instances = []
        self.original_reader = upload_module._RangeReader
        upload_module._RangeReader = FakeRangeReader
        self.oss_available = upload_module.P115OSS_AVAILABLE
        upload_module.P115OSS_AVAILABLE = True
        self.original_oss = (
            upload_module.oss_multipart_upload_init,
            upload_module.oss_multipart_upload_part,
            upload_module.oss_multipart_upload_complete,
            upload_module.oss_multipart_upload_cancel,
        )
        self.cancelled = []
        self.completed = []
        upload_module.oss_multipart_upload_init = lambda target, **kwargs: "upload-1"
        upload_module.oss_multipart_upload_part = self._fake_part
        upload_module.oss_multipart_upload_complete = self._fake_complete
        upload_module.oss_multipart_upload_cancel = lambda target, upload_id: (
            self.cancelled.append((target, upload_id))
        )
        self.service = upload_module.P115UploadService(FakeOwner())
        self.progress = []

    def tearDown(self):
        upload_module._RangeReader = self.original_reader
        upload_module.P115OSS_AVAILABLE = self.oss_available
        (
            upload_module.oss_multipart_upload_init,
            upload_module.oss_multipart_upload_part,
            upload_module.oss_multipart_upload_complete,
            upload_module.oss_multipart_upload_cancel,
        ) = self.original_oss

    def _fake_part(self, target, upload_id, data, part_number=1, reporthook=None):
        if part_number == 99:
            raise IOError("模拟分片失败")
        if reporthook:
            reporthook(len(data))
        return {
            "PartNumber": part_number,
            "ETag": f'"etag-{part_number}"',
            "Size": len(data),
        }

    def _fake_complete(self, target, upload_id, parts, callback, **kwargs):
        self.completed.append((target, upload_id, list(parts)))
        return {"state": True}

    def _call(self, file_size, part_size, concurrency=2, target="oss://bucket/key"):
        return self.service._upload_parts_concurrently(
            {"url": target, "callback": {"callback": "{}"}},
            "https://dl.example.com/f", {}, file_size, part_size,
            progress_callback=lambda done, total: self.progress.append((done, total)),
            concurrency=concurrency,
        )

    def test_parts_uploaded_concurrently_and_completed_in_order(self):
        result = self._call(2500, 1024, concurrency=3)
        self.assertTrue(result)
        self.assertEqual(len(self.completed), 1)
        target, upload_id, parts = self.completed[0]
        self.assertEqual((target, upload_id), ("oss://bucket/key", "upload-1"))
        self.assertEqual([part["PartNumber"] for part in parts], [1, 2, 3])
        self.assertEqual([part["Size"] for part in parts], [1024, 1024, 452])
        self.assertEqual(self.progress[-1], (2500, 2500))
        self.assertEqual(self.cancelled, [])
        readers = FakeRangeReader.instances
        self.assertEqual(len(readers), 3, "并发度 = min(配置, 分片数)")
        covered = sorted(rng for reader in readers for rng in reader.ranges)
        self.assertEqual(covered, [(0, 1023), (1024, 2047), (2048, 2499)])
        self.assertTrue(all(reader.closed for reader in readers))

    def test_reader_count_follows_concurrency_setting(self):
        self._call(4096, 1024, concurrency=1)
        self.assertEqual(len(FakeRangeReader.instances), 1)
        self.assertEqual(len(self.completed[0][2]), 4)

    def test_failure_cancels_multipart_and_raises(self):
        with self.assertRaises(IOError):
            self.service._upload_parts_concurrently(
                {"url": "oss://bucket/key", "callback": {"callback": "{}"}},
                "https://dl.example.com/f", {}, 99 * 1024, 1024,
                progress_callback=lambda done, total: self.progress.append(done),
                concurrency=2,
            )
        self.assertEqual(self.completed, [])
        self.assertEqual(len(self.cancelled), 1, "失败时必须取消分块上传任务")

    def test_missing_oss_info_rejected(self):
        with self.assertRaises(RuntimeError):
            self.service._upload_parts_concurrently(
                {}, "https://dl.example.com/f", {}, 1024, 1024, concurrency=2
            )


class LocalUploadPartSizeTest(unittest.TestCase):
    def test_local_upload_uses_explicit_part_size(self):
        upload_module.P115DirectoryReader = MagicMock(return_value=MagicMock(
            resolve_directory=MagicMock(
                return_value=SimpleNamespace(checked=True, directory_id=1)
            )
        ))
        upload_module.P115_AVAILABLE = True
        upload_module.check_response = MagicMock()
        owner = FakeOwner()
        service = upload_module.P115UploadService(owner)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "movie.mkv"
            path.write_bytes(b"0" * (16 * 1024 * 1024 + 1))
            self.assertTrue(service.upload_file(str(path), "/save", "movie.mkv"))
        kwargs = owner.client.upload_file.call_args.kwargs
        self.assertEqual(kwargs["partsize"], 16 * 1024 * 1024)
        self.assertNotEqual(kwargs["partsize"], -1)


if __name__ == "__main__":
    unittest.main()
