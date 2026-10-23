"""内置分段下载器（HttpFileDownloadService）的行为契约。

这些用例直连本地 HTTP 服务，不用 MP 运行时：
* 分片并发下载必须字节级正确；
* 分片大小自适应（8MB~64MB，按「每线程 2 片」）；
* 断点续传：中断后保留 .parts.json 已下字节，恢复时只补缺口；
* 5xx/断链 → 刷新直链 + 退避重试；
* 源不支持 Range → 回落单线程顺序下载；
* 进度回调必须节流（旧实现每 256KB 一次，写库写死）；
* 校验和缓存：下载后算过一次，秒传前不再重复读盘。
"""

import hashlib
import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_mock("app.log", logger=MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)

HttpFileDownloadService = transfer_module.HttpFileDownloadService
ProgressGate = transfer_module._ProgressGate
file_checksum = transfer_module.file_checksum


class FakeFile:
    def __init__(self, size, name="a.mkv"):
        self.id = "f1"
        self.name = name
        self.size = size
        self.sha1 = ""
        self.md5 = ""


class RangeServer:
    """支持 Range 的本地文件服务，可注入故障。"""

    def __init__(self, payload: bytes, *, ignore_range=False, fail_first=0):
        self.payload = payload
        self.ignore_range = ignore_range
        self.fail_first = fail_first
        self.requests = []
        self.bytes_served = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):  # 静音
                return

            def do_GET(self):
                with outer._lock:
                    outer.requests.append(self.headers.get("Range") or "")
                    if outer.fail_first > 0:
                        outer.fail_first -= 1
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                total = len(outer.payload)
                header = self.headers.get("Range") or ""
                start, end = 0, total - 1
                status = 200
                if header.lower().startswith("bytes=") and not outer.ignore_range:
                    spec = header.split("=", 1)[1].split(",")[0]
                    first, _, last = spec.partition("-")
                    start = int(first or 0)
                    end = int(last) if last else total - 1
                    end = min(end, total - 1)
                    status = 206
                body = outer.payload[start:end + 1]
                self.send_response(status)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes" if not outer.ignore_range else "none")
                if status == 206:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{total}"
                    )
                self.end_headers()
                self.wfile.write(body)
                with outer._lock:
                    outer.bytes_served += len(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/file.bin"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def _service(self, url, **kwargs):
        return HttpFileDownloadService(lambda _file: (url, {}), **kwargs)

    def _download(self, service, file_item, name="out.bin", **kwargs):
        target = Path(self.tempdir.name) / name
        return service.download_file(file_item, str(target), **kwargs), target

    def test_parallel_download_is_byte_exact(self):
        payload = os.urandom(5 * 1024 * 1024)
        server = RangeServer(payload)
        self.addCleanup(server.close)
        service = self._service(server.base_url, concurrency=4, part_size=1024 * 1024)
        _, target = self._download(service, FakeFile(len(payload)))
        self.assertEqual(target.read_bytes(), payload)
        self.assertGreaterEqual(len(server.requests), 4, "应当分段并发请求")

    def test_part_size_adapts_to_size_and_concurrency(self):
        service = self._service("http://x/", concurrency=8)
        self.assertEqual(service._part_size_for(64 * 1024 * 1024), 8 * 1024 * 1024)
        self.assertEqual(service._part_size_for(1024 ** 3), 64 * 1024 * 1024)
        # 100MB / (8×2) = 6.25MB 低于下限 → 抬到 8MB
        self.assertEqual(service._part_size_for(100 * 1024 * 1024), 8 * 1024 * 1024)
        # 512MB / (8×2) = 32MB，正好是 4MB 的整数倍
        self.assertEqual(service._part_size_for(512 * 1024 * 1024), 32 * 1024 * 1024)
        explicit = self._service("http://x/", concurrency=8, part_size=4 * 1024 * 1024)
        self.assertEqual(explicit._part_size_for(1024 ** 3), 4 * 1024 * 1024)

    def test_resume_downloads_only_missing_parts(self):
        payload = os.urandom(8 * 1024 * 1024)
        server = RangeServer(payload)
        self.addCleanup(server.close)
        service = self._service(server.base_url, concurrency=2, part_size=1024 * 1024)
        stop = threading.Event()
        started = threading.Event()

        def progress(done, total):
            started.set()
            if done >= 2 * 1024 * 1024:
                stop.set()

        target = Path(self.tempdir.name) / "resume.bin"
        with self.assertRaises(InterruptedError):
            service.download_file(
                FakeFile(len(payload)), str(target), progress, stop.is_set,
                preserve_partial=True,
            )
        parts_path = Path(str(target) + ".parts.json")
        self.assertTrue(parts_path.is_file(), "中断必须保留 .parts.json")
        completed = json.loads(parts_path.read_text())["completed"]
        self.assertTrue(completed, "中断前完成的分片必须记账")

        first_round = server.bytes_served
        resumed = self._service(server.base_url, concurrency=2, part_size=1024 * 1024)
        resumed.download_file(
            FakeFile(len(payload)), str(target), None, None, preserve_partial=True,
        )
        self.assertEqual(target.read_bytes(), payload)
        self.assertFalse(parts_path.exists(), "完成后要清掉 .parts.json")
        second_round = server.bytes_served - first_round
        self.assertLess(
            second_round, len(payload),
            "续传不应重下已完成的分片",
        )

    def test_retries_on_server_error(self):
        payload = os.urandom(3 * 1024 * 1024)
        server = RangeServer(payload, fail_first=2)
        self.addCleanup(server.close)
        service = self._service(
            server.base_url, concurrency=2, part_size=1024 * 1024, max_retries=5,
        )
        _, target = self._download(service, FakeFile(len(payload)))
        self.assertEqual(target.read_bytes(), payload)

    def test_falls_back_to_serial_without_range_support(self):
        payload = os.urandom(2 * 1024 * 1024)
        server = RangeServer(payload, ignore_range=True)
        self.addCleanup(server.close)
        service = self._service(server.base_url, concurrency=4, part_size=512 * 1024)
        _, target = self._download(service, FakeFile(len(payload)))
        self.assertEqual(target.read_bytes(), payload)

    def test_progress_gate_throttles_callbacks(self):
        calls = []
        gate = ProgressGate(lambda done, total: calls.append(done), 1000)
        for _ in range(1000):
            gate.add(1)
        # 节流规则：200ms 一次 + 每 1% 保底一次 → 1000 字节最多约 100 次，
        # 而不是每块一次（旧实现 256KB 一块，1GB 要写 4000 次任务表）
        self.assertLessEqual(len(calls), 105, "同一时刻的碎块写入必须合并回调")
        gate.flush()
        self.assertEqual(calls[-1], 1000)

    def test_checksum_cache_avoids_second_read(self):
        path = Path(self.tempdir.name) / "hash.bin"
        payload = b"checksum-cache-payload"
        path.write_bytes(payload)
        first = file_checksum(str(path), "sha1")
        self.assertEqual(first, hashlib.sha1(payload).hexdigest())
        os.remove(path)
        self.assertEqual(
            file_checksum(str(path), "sha1"), first,
            "同一文件第二次取校验和必须命中缓存（文件已删也能返回）",
        )


if __name__ == "__main__":
    unittest.main()
