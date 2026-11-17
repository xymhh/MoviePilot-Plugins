"""下载器不再写 .json 文件（1.12.19）。

固定契约：
1. 完整并行下载、串行下载全程不在磁盘上产生任何 .json 文件；
2. 下载前 _stamp_cache_owner 登记归属（complete=False），下载完
   _mark_complete_cache 标成完成，_validate_complete_cache 通过；
   全程只用进程内登记表，不写文件；
3. 进程内 parts 内存续传：中断后同进程第二次调用复用已完成分片，
   只补缺口；清掉内存字典（模拟重启）后从头下；
4. 改名后登记表条目跟随新路径，删缓存时登记表条目一起清；
5. residue：旧版 .meta.json / .parts.json 见到直接删，
   cloud-transfer-* 组仍按旧规则清，无伴随文件的普通文件不动。

本文件自包含（本地 Range 服务与 manager 桩都在文件内），不从其它
测试文件 import，避免 sys.modules 污染；只加载 transfer.py 与
residue.py，不碰真实 DB 模块。
"""

import hashlib
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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
register_mock("app.core.cache", TTLCache=mock.MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=mock.MagicMock())
register_mock("app.log", logger=mock.MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=mock.MagicMock(return_value=mock.MagicMock()),
)

# transfer.py 用相对导入拉 cloud.py：必须先把真实的 cloud 模块
# 显式加载进来，否则前面别的测试文件留下的占位模块会让导入失败
#（test_http_downloader.py 同款做法）。
cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
HttpFileDownloadService = transfer_module.HttpFileDownloadService
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
CloudFile = transfer_module.CloudFile

residue_module = load_module(
    "cloudsubscribex.core.residue", "plugins.v2/cloudsubscribex/core/residue.py"
)
scan_residue = residue_module.scan_residue

CONTENT = b"cache-bytes" * 100


def _make_source(name, content=CONTENT):
    return CloudFile(
        id="file-1", name=name, is_directory=False, size=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
        md5=hashlib.md5(content).hexdigest(),
    )


def _make_manager(download_path):
    manager = CrossTransferTaskManager(
        lambda key: mock.MagicMock(), download_path=download_path
    )
    manager._notify_change = lambda: None
    return manager


class FakeFile:
    def __init__(self, size, name="a.mkv"):
        self.id = "f1"
        self.name = name
        self.size = size
        self.sha1 = ""
        self.md5 = ""


class RangeServer:
    """支持 Range 的本地文件服务（最小实现，只够本文件用）。"""

    def __init__(self, payload: bytes, *, ignore_range=False):
        self.payload = payload
        self.ignore_range = ignore_range
        self.bytes_served = 0
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_GET(self):
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


def _wait_server_quiesced(server, timeout=5.0, stable_for=0.5):
    """等 RangeServer 的字节计数落定再采样基线。

    计数在服务端线程里加（do_GET 写完 body 后才 +=），中断的请求
    可能还在服务端线程里写，主线程的 download_file 已经返回。
    不等它落定就采样 first_round，上一轮的分片字节会算进下一轮——
    CI（1.12.20）上曾因此多出正好 1 个分片（9437184 != 8388608）。
    只等计数连续 stable_for 秒不变，最多 timeout 秒。
    """
    deadline = time.monotonic() + timeout
    last = server.bytes_served
    stable_since = time.monotonic()
    while True:
        time.sleep(0.05)
        now = server.bytes_served
        if now != last:
            last = now
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= stable_for:
            return
        if time.monotonic() > deadline:
            return


class NoJsonDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        # 各用例互不干扰：进出都清掉进程内分片内存。
        transfer_module._PARTS_MEMORY.clear()
        self.addCleanup(transfer_module._PARTS_MEMORY.clear)

    def _json_files(self):
        return [
            path for path in Path(self.tempdir.name).iterdir()
            if path.name.endswith(".json") or ".json." in path.name
        ]

    def _service(self, url, **kwargs):
        return HttpFileDownloadService(lambda _file: (url, {}), **kwargs)

    def test_parallel_download_writes_no_json_files(self):
        payload = os.urandom(5 * 1024 * 1024)
        server = RangeServer(payload)
        self.addCleanup(server.close)
        service = self._service(
            server.base_url, concurrency=4, part_size=1024 * 1024
        )
        target = Path(self.tempdir.name) / "out.bin"
        service.download_file(FakeFile(len(payload)), str(target))
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self._json_files(), [], "并行下载不得写任何 .json 文件")

    def test_serial_download_writes_no_json_files(self):
        payload = os.urandom(2 * 1024 * 1024)
        server = RangeServer(payload, ignore_range=True)
        self.addCleanup(server.close)
        service = self._service(
            server.base_url, concurrency=4, part_size=512 * 1024
        )
        target = Path(self.tempdir.name) / "out.bin"
        service.download_file(FakeFile(len(payload)), str(target))
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self._json_files(), [], "串行下载不得写任何 .json 文件")

    def test_in_process_parts_memory_resume(self):
        # 中断后同进程第二次调用复用已完成分片，只补缺口；全程无 .json。
        payload = os.urandom(8 * 1024 * 1024)
        server = RangeServer(payload)
        self.addCleanup(server.close)
        stop = threading.Event()

        def progress(done, total):
            if done >= 2 * 1024 * 1024:
                stop.set()

        target = Path(self.tempdir.name) / "resume.bin"
        service = self._service(
            server.base_url, concurrency=2, part_size=1024 * 1024
        )
        with self.assertRaises(InterruptedError):
            service.download_file(
                FakeFile(len(payload)), str(target), progress, stop.is_set,
                preserve_partial=True,
            )
        self.assertEqual(self._json_files(), [], "中断不得留下任何 .json 文件")
        self.assertTrue(target.is_file(), "preserve_partial 时半截文件必须保留")

        _wait_server_quiesced(server)
        first_round = server.bytes_served
        resumed = self._service(
            server.base_url, concurrency=2, part_size=1024 * 1024
        )
        resumed.download_file(
            FakeFile(len(payload)), str(target), None, None, preserve_partial=True,
        )
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(self._json_files(), [], "完成后不得留下任何 .json 文件")
        self.assertLess(
            server.bytes_served - first_round, len(payload),
            "续传不应重下已完成的分片",
        )

    def test_restart_redownloads_from_scratch(self):
        # 清掉内存字典模拟进程重启：下次调用从头下，不认半截稀疏文件。
        payload = os.urandom(8 * 1024 * 1024)
        server = RangeServer(payload)
        self.addCleanup(server.close)
        stop = threading.Event()

        def progress(done, total):
            if done >= 2 * 1024 * 1024:
                stop.set()

        target = Path(self.tempdir.name) / "restart.bin"
        service = self._service(
            server.base_url, concurrency=2, part_size=1024 * 1024
        )
        with self.assertRaises(InterruptedError):
            service.download_file(
                FakeFile(len(payload)), str(target), progress, stop.is_set,
                preserve_partial=True,
            )

        transfer_module._PARTS_MEMORY.clear()  # 模拟重启
        _wait_server_quiesced(server)
        first_round = server.bytes_served
        restarted = self._service(
            server.base_url, concurrency=2, part_size=1024 * 1024
        )
        restarted.download_file(
            FakeFile(len(payload)), str(target), None, None, preserve_partial=True,
        )
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(
            server.bytes_served - first_round, len(payload),
            "内存状态清空后必须从头下",
        )


class RegistryTests(unittest.TestCase):
    def test_stamp_mark_validate_roundtrip_writes_no_json(self):
        with TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            source = _make_source("06.mkv")
            key = manager._cache_key("quark", source)
            target = Path(tmp) / "目标剧 S01E01.mkv"
            target.write_bytes(CONTENT)

            manager._stamp_cache_owner(target, key, source)
            self.assertEqual(manager._read_cache_owner_key(target), key)
            self.assertFalse(
                manager._validate_complete_cache(target, key, source),
                "只 stamp 不算完整缓存",
            )
            self.assertTrue(manager._is_our_cache_file(target, key, source))

            manager._mark_complete_cache(target, key, source)
            self.assertTrue(manager._validate_complete_cache(target, key, source))
            self.assertEqual(
                manager.cache_info("quark", source, target_name=target.name)
                ["cache_status"],
                "complete",
            )
            self.assertEqual(
                [p for p in Path(tmp).iterdir() if p.name.endswith(".json")],
                [],
                "stamp/mark 全程不得写任何 .json 文件",
            )

    def test_mark_complete_rejects_wrong_size_or_checksum(self):
        with TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            source = _make_source("06.mkv")
            key = manager._cache_key("quark", source)
            target = Path(tmp) / "目标剧 S01E01.mkv"
            target.write_bytes(CONTENT[:100])
            with self.assertRaises(IOError):
                manager._mark_complete_cache(target, key, source)
            self.assertFalse(
                manager._validate_complete_cache(target, key, source)
            )

    def test_registry_follows_rename_and_delete(self):
        with TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            source = _make_source("06.mkv")
            key = manager._cache_key("quark", source)
            legacy = manager._legacy_cache_path(key)
            legacy.write_bytes(CONTENT)
            manager._mark_complete_cache(legacy, key, source)
            new_path = Path(tmp) / "目标剧 S01E01.mkv"

            self.assertTrue(manager._rename_cache_files(legacy, new_path))
            self.assertEqual(manager._read_cache_owner_key(new_path), key)
            self.assertEqual(manager._read_cache_owner_key(legacy), "")
            self.assertTrue(
                manager._validate_complete_cache(new_path, key, source)
            )

            self.assertEqual(manager._delete_cache_path(new_path), 1)
            self.assertFalse(new_path.exists())
            self.assertEqual(manager._read_cache_owner_key(new_path), "")

    def test_unregistered_file_is_never_ours(self):
        # 登记表里没有的路径（跨重启的旧文件、裸文件）一律不认。
        with TemporaryDirectory() as tmp:
            manager = _make_manager(tmp)
            source = _make_source("06.mkv")
            key = manager._cache_key("quark", source)
            target = Path(tmp) / "目标剧 S01E01.mkv"
            target.write_bytes(CONTENT)

            self.assertFalse(manager._is_our_cache_file(target, key, source))
            self.assertFalse(
                manager._validate_complete_cache(target, key, source)
            )
            self.assertEqual(
                manager.cache_info("quark", source, target_name=target.name)
                ["cache_status"],
                "missing",
            )


class ResidueLegacyJsonTests(unittest.TestCase):
    def test_legacy_json_deleted_cloud_transfer_group_kept_plain(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = "cloud-transfer-" + "a" * 32
            old_main = root / f"{key}-movie.mkv"
            old_main.write_bytes(b"x" * 100)
            old_stamp = time.time() - 3 * 86400
            os.utime(old_main, (old_stamp, old_stamp))
            legacy_meta = root / "movie.mkv.meta.json"
            legacy_meta.write_bytes(b"{}")
            legacy_parts = root / "movie.mkv.parts.json"
            legacy_parts.write_bytes(b"{}")
            keep = root / "movie.mkv"
            keep.write_bytes(b"x" * 20)

            result = scan_residue(root, now=time.time())

            self.assertEqual(result["deleted_files"], 3)
            self.assertFalse(old_main.exists())
            self.assertFalse(legacy_meta.exists())
            self.assertFalse(legacy_parts.exists())
            self.assertTrue(keep.exists())
            self.assertEqual(result["errors"], [])


if __name__ == "__main__":
    unittest.main()
