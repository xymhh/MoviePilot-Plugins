"""1.12.17：115 API 调用计量（Phase 0-9，风控面验收的观测手段）。

覆盖：
1. ApiCallMeter 本身：计数、快照隔离、reset、快照格式化；
2. 上传路径：oss2 分片（每片记一次 + 字节数）、秒传命中/未命中、
   p115oss 回落（整文件记一次）、终态计量日志行；
3. 控制面：P115ClientManager._rate_limited_call 每次调用按函数名记数。

注意：api_meter 是进程内全局单例，各测试 setUp 先 reset，避免别的
测试文件跑上传时攒下的计数干扰断言。
"""
import hashlib
import importlib.util
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
)

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
_core_pkg = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_core_pkg.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.drive.p115", _plugin_dir("drive", "p115"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

p115_files_module = register_module(
    "cloudsubscribex.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

# 先加载 telemetry 再加载 upload：upload 顶层就 import api_meter，
# 必须拿到同一个单例（import 系统会复用 sys.modules 里的这份）。
telemetry_module = load_module(
    "cloudsubscribex.core.telemetry",
    "plugins.v2/cloudsubscribex/core/telemetry.py",
)
api_meter = telemetry_module.api_meter
format_snapshot = telemetry_module.format_snapshot

load_module(
    "cloudsubscribex.core.error_text",
    "plugins.v2/cloudsubscribex/core/error_text.py",
)
upload_module = load_module(
    "cloudsubscribex.drive.p115.upload",
    "plugins.v2/cloudsubscribex/drive/p115/upload.py",
)


PART_SIZE = 4
CONTENT = b"0123456789"  # 10 字节 → 3 片：4 / 4 / 2
EXPECTED_SHA1 = hashlib.sha1(CONTENT).hexdigest().upper()


def _token_response(expiration):
    return {
        "AccessKeyId": "AKID",
        "AccessKeySecret": "AKSECRET",
        "SecurityToken": "TOKEN",
        "Expiration": expiration,
    }


def _init_response(reuse=False):
    return {
        "state": True,
        "reuse": reuse,
        "data": {
            "bucket": "bucket-115",
            "object": "object-key",
            "callback": {"callback": "cb", "callback_var": "var"},
        },
    }


class _FakeBucket:
    def __init__(self, complete_status=200):
        self.complete_status = complete_status
        self.uploaded_parts = []
        self.completed = []

    def init_multipart_upload(self, key, params=None):
        return SimpleNamespace(upload_id="UPLOAD-ID-1")

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        content = data.read() if hasattr(data, "read") else b""
        self.uploaded_parts.append((part_number, content))
        return SimpleNamespace(etag=f"etag-{part_number}")

    def abort_multipart_upload(self, key, upload_id):
        pass

    def complete_multipart_upload(self, key, upload_id, parts, headers=None):
        self.completed.append((key, upload_id, list(parts), dict(headers or {})))
        return SimpleNamespace(status=self.complete_status)


class ApiCallMeterTests(unittest.TestCase):
    def setUp(self):
        api_meter.reset()

    def test_record_and_snapshot(self):
        api_meter.record("oss2:part", bytes_=4)
        api_meter.record("oss2:part", bytes_=4)
        api_meter.record("control:upload_file_init")
        snapshot = api_meter.snapshot()
        self.assertEqual(snapshot["oss2:part"], {"calls": 2, "bytes": 8})
        self.assertEqual(snapshot["control:upload_file_init"], {"calls": 1, "bytes": 0})
        # 快照是深拷贝：改它不影响计数器。
        snapshot["oss2:part"]["calls"] = 999
        self.assertEqual(api_meter.snapshot()["oss2:part"]["calls"], 2)

    def test_reset_clears(self):
        api_meter.record("rapid:hit")
        api_meter.reset()
        self.assertEqual(api_meter.snapshot(), {})

    def test_format_snapshot(self):
        api_meter.record("oss2:part", bytes_=1024 * 1024 * 3)
        api_meter.record("rapid:hit")
        text = format_snapshot(api_meter.snapshot())
        self.assertIn("oss2:part=1次/3.0MB", text)
        self.assertIn("rapid:hit=1次", text)
        self.assertEqual(format_snapshot({}), "无")

    def test_concurrent_record_is_exact(self):
        def worker():
            for _ in range(200):
                api_meter.record("oss2:part", bytes_=4)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(
            api_meter.snapshot()["oss2:part"], {"calls": 1000, "bytes": 4000}
        )


class UploadMeterTests(unittest.TestCase):
    def setUp(self):
        api_meter.reset()
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
            "Oss2Bucket": upload_module.Oss2Bucket,
            "Oss2StsAuth": upload_module.Oss2StsAuth,
            "oss2_determine_part_size": upload_module.oss2_determine_part_size,
        }
        upload_module.P115_AVAILABLE = True
        upload_module.OSS2_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = True
        upload_module.check_response = MagicMock()
        upload_module.oss2_determine_part_size = MagicMock(
            return_value=PART_SIZE
        )
        self._bucket = _FakeBucket()
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        upload_module.Oss2StsAuth = MagicMock(return_value=MagicMock())
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, init_side_effect=None, token_side_effect=None):
        client = MagicMock()
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        if init_side_effect is None:
            client.upload_file_init.side_effect = (
                lambda *args, **kwargs: _init_response()
            )
        else:
            client.upload_file_init.side_effect = init_side_effect
        if token_side_effect is None:
            client.upload_gettoken.side_effect = (
                lambda *args, **kwargs: _token_response(far_future)
            )
        else:
            client.upload_gettoken.side_effect = token_side_effect
        self._client = client
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def _write_source(self, tempdir):
        source = Path(tempdir) / "05.mkv"
        source.write_bytes(CONTENT)
        return source

    def _meter_log_lines(self, before):
        # 用 upload 模块绑定的 logger 读：全量套件里 app.log 会被别的
        # 用例重注册，sys.modules 里的 logger 未必是 upload 用的那只。
        logger = upload_module.logger
        return [
            str(call.args[0])
            for call in logger.info.call_args_list[before:]
            if call.args and "115 上传计量" in str(call.args[0])
        ]

    def test_multipart_success_records_parts_miss_and_success(self):
        service = self._make_service()
        logger = upload_module.logger
        before = len(logger.info.call_args_list)
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        snapshot = api_meter.snapshot()
        self.assertEqual(snapshot["oss2:part"], {"calls": 3, "bytes": 10})
        self.assertEqual(snapshot["rapid:miss"], {"calls": 1, "bytes": 0})
        self.assertEqual(snapshot["upload:success"], {"calls": 1, "bytes": 10})
        self.assertNotIn("rapid:hit", snapshot)
        lines = self._meter_log_lines(before)
        self.assertEqual(len(lines), 1)
        self.assertIn("成功", lines[0])
        self.assertIn("后端=oss2", lines[0])
        self.assertIn("秒传=否", lines[0])
        self.assertIn("分片=3片", lines[0])
        self.assertIn("oss2:part=3次", lines[0])

    def test_rapid_reuse_records_hit_without_parts(self):
        service = self._make_service(
            init_side_effect=[_init_response(reuse=True)]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        snapshot = api_meter.snapshot()
        self.assertEqual(snapshot["rapid:hit"], {"calls": 1, "bytes": 0})
        self.assertEqual(snapshot["upload:success"], {"calls": 1, "bytes": 10})
        self.assertNotIn("oss2:part", snapshot)
        self.assertNotIn("rapid:miss", snapshot)

    def test_p115oss_fallback_records_file(self):
        upload_module.OSS2_AVAILABLE = False
        service = self._make_service()
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.upload_file(str(source), "/"))
        snapshot = api_meter.snapshot()
        self.assertEqual(snapshot["p115oss:file"], {"calls": 1, "bytes": 10})
        self.assertEqual(snapshot["upload:success"], {"calls": 1, "bytes": 10})
        self.assertNotIn("oss2:part", snapshot)

    def test_try_rapid_upload_probe_records_hit_and_miss(self):
        service = self._make_service(
            init_side_effect=[_init_response(reuse=True), _init_response()]
        )
        with tempfile.TemporaryDirectory() as tempdir:
            source = self._write_source(tempdir)
            self.assertTrue(service.try_rapid_upload(
                str(source), "/", "05.mkv", "sha1", EXPECTED_SHA1, len(CONTENT)
            ))
            self.assertFalse(service.try_rapid_upload(
                str(source), "/", "05.mkv", "sha1", EXPECTED_SHA1, len(CONTENT)
            ))
        snapshot = api_meter.snapshot()
        self.assertEqual(snapshot["rapid:hit"], {"calls": 1, "bytes": 0})
        self.assertEqual(snapshot["rapid:miss"], {"calls": 1, "bytes": 0})


class ControlCallMeterTests(unittest.TestCase):
    """P115ClientManager._rate_limited_call 按函数名记控制面调用。"""

    @classmethod
    def setUpClass(cls):
        # 下面要替换 sys.modules 里的几个模块来加载 client.py：保存旧值，
        # tearDownClass 原样恢复。test_strm_export_cleanup 等文件在收集期
        # 就把 drive.common 的 DriveRateLimiter 置成 object，setdefault
        # 盖不掉它，必须直接覆盖；不恢复会污染后跑的用例。
        cls._saved_modules = {}
        for name in (
            "cloudsubscribex.drive.p115.files",
            "cloudsubscribex.drive.p115.share",
            "cloudsubscribex.drive.p115.offline",
            "cloudsubscribex.drive.p115.upload",
            "cloudsubscribex.drive.common",
            "cloudsubscribex.drive.p115.client",
        ):
            cls._saved_modules[name] = sys.modules.get(name)
        cls._saved_core_attrs = {}
        core_module = sys.modules.get("cloudsubscribex.core")
        if core_module is not None:
            for attr in ("get_component", "resolve_component"):
                cls._saved_core_attrs[attr] = getattr(core_module, attr, None)
        sys.modules["cloudsubscribex.drive.p115.files"] = MagicMock()
        sys.modules["cloudsubscribex.drive.p115.share"] = MagicMock()
        sys.modules["cloudsubscribex.drive.p115.offline"] = MagicMock()
        sys.modules["cloudsubscribex.drive.p115.upload"] = MagicMock()
        # 本文件的 core 是按真实包路径注册的空模块（只挂了 OwnerDelegator），
        # client.py 还要 from ...core import get_component/resolve_component，
        # 这里补上桩；utils.cache 在文件头已注册。
        if core_module is not None:
            core_module.get_component = MagicMock()
            core_module.resolve_component = MagicMock()
        common_stub = types.ModuleType("cloudsubscribex.drive.common")

        class _StubRateLimiter:
            def __init__(self, *args, **kwargs):
                pass

            @classmethod
            def shared(cls, *args, **kwargs):
                return cls()

        common_stub.DriveRateLimiter = _StubRateLimiter
        common_stub.create_directory_path_cache = lambda *args, **kwargs: None
        sys.modules["cloudsubscribex.drive.common"] = common_stub
        client_path = os.path.join(
            plugins_v2_path, "cloudsubscribex/drive/p115/client.py"
        )
        spec = importlib.util.spec_from_file_location(
            "cloudsubscribex.drive.p115.client", client_path
        )
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "cloudsubscribex.drive.p115"
        sys.modules["cloudsubscribex.drive.p115.client"] = module
        spec.loader.exec_module(module)
        cls.P115ClientManager = module.P115ClientManager

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        core_module = sys.modules.get("cloudsubscribex.core")
        if core_module is not None:
            for attr, value in cls._saved_core_attrs.items():
                if value is None:
                    try:
                        delattr(core_module, attr)
                    except AttributeError:
                        pass
                else:
                    setattr(core_module, attr, value)

    def setUp(self):
        api_meter.reset()

    def _manager(self):
        manager = self.P115ClientManager("fake_cookie_value")
        manager.rate_limiter = SimpleNamespace(
            call=lambda func, *args, **kwargs: func(*args, **kwargs)
        )
        return manager

    def test_rate_limited_call_records_operation_name(self):
        manager = self._manager()

        def upload_file_init(*args, **kwargs):
            return {"state": True}

        result = manager._rate_limited_call(upload_file_init, "x")
        self.assertEqual(result, {"state": True})
        self.assertEqual(
            api_meter.snapshot()["control:upload_file_init"],
            {"calls": 1, "bytes": 0},
        )

    def test_anonymous_callable_records_unknown(self):
        from functools import partial

        manager = self._manager()
        manager._rate_limited_call(partial(lambda: "ok"))
        self.assertEqual(
            api_meter.snapshot()["control:unknown"],
            {"calls": 1, "bytes": 0},
        )
