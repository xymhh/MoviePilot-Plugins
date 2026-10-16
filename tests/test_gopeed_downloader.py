"""1.6.43/1.6.44 Gopeed 外部下载器单测。

跨盘中继下载新增可选外部下载器 Gopeed（默认 builtin）。本文件固定：

1. Gopeed 正常完成 → 文件落在 local_path 并返回该路径；
2. 进度回调被调用且单调不减；
3. 任务失败 → 重新取链 + 重建任务（resolver 被调用 ≥2 次）；
4. stop_requested → 暂停 + 无条件 force 删除 Gopeed 任务并抛错（且不回落内置），
   preserve_partial=True 也照删（停止即清空中继）；
5. Gopeed 不可用（连不上/401/建任务失败）→ 回落内置下载器（断言内置服务被调用、
   恰好记一次 warning）；
6. gopeed_path_prefix 映射生效（create_task 收到换算后的 Gopeed 侧路径）；
7. 夸克 QuarkFileService.download_file 分流：builtin（默认）不碰 Gopeed、gopeed 走
   外部服务、gopeed 异常回落内置；gopeed_connections 全程透传（写死 0 即失败）；
8. GopeedClient 契约：X-Api-Token 鉴权、任务体形状（connections=0 不写 opts.extra，
   >0 写 opts.extra.connections）、DELETE ?force=true、401/非零 code/连接异常报错
   且消息不含 token；
9. 传输层取消：完整缓存/部分缓存 + stop_event 置位 → .bin/.meta.json/.parts.json
   全部删除且不碰同目录其它文件；
10. 1.6.47：建任务前清本任务中继残留（含 Gopeed 改名副本，范围不越界、不碰
    目录与非本任务文件）；校验容忍改名副本（大小相符时归位到规范路径，归位
    失败才按副本路径通过）；
    缺文件异常含期望路径 + Gopeed 侧映射目录 + 排查提示且不含令牌。
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugin_env import (
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


# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=MagicMock())
register_module("app.log", logger=MagicMock())

# ---- cloudsubscribex 包层级（core/drive 用真实路径，子模块按文件加载）----
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
gopeed_module = load_module(
    "cloudsubscribex.core.gopeed", "plugins.v2/cloudsubscribex/core/gopeed.py"
)
load_module("cloudsubscribex.drive.common", "plugins.v2/cloudsubscribex/drive/common.py")
files_module = load_module(
    "cloudsubscribex.drive.quark.files", "plugins.v2/cloudsubscribex/drive/quark/files.py"
)

CloudFile = cloud_module.CloudFile
GopeedClient = gopeed_module.GopeedClient
GopeedDownloadService = gopeed_module.GopeedDownloadService
GopeedError = gopeed_module.GopeedError
QuarkFileService = files_module.QuarkFileService

PAYLOAD = b"0123456789"


def make_file_item(size: int = len(PAYLOAD)) -> CloudFile:
    return CloudFile(
        id="fid1", name="movie.mkv", is_directory=False, size=size,
    )


class FakeGopeedClient:
    """脚本化 Gopeed 客户端：按建任务顺序出队进度脚本，done 时在本机落盘。

    脚本步骤：{"status": "...", "downloaded": n, "size": n, "write": bool}；
    ``write=True`` 表示模拟 Gopeed 把文件写到（映射后同一宿主文件的）本机路径。
    """

    def __init__(self, scripts, local_dir: str):
        self._scripts = [list(script) for script in scripts]
        self._local_dir = local_dir
        self._tasks = {}
        self._counter = 0
        self.created = []
        self.paused = []
        self.deleted = []

    def create_task(self, url, headers, path, name, connections=0):
        self._counter += 1
        task_id = f"task-{self._counter}"
        steps = self._scripts.pop(0) if self._scripts else [
            {"status": "done", "downloaded": 0, "size": 0, "write": True}
        ]
        self.created.append({
            "id": task_id, "url": url, "headers": dict(headers or {}),
            "path": path, "name": name, "connections": int(connections or 0),
        })
        self._tasks[task_id] = {"steps": steps, "index": 0, "name": name}
        return task_id

    def get_task(self, task_id):
        state = self._tasks[task_id]
        steps = state["steps"]
        index = min(state["index"], len(steps) - 1)
        state["index"] += 1
        step = steps[index]
        if step.get("write"):
            target = Path(self._local_dir) / state["name"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PAYLOAD)
        return {
            "status": step.get("status", "running"),
            "progress": {"downloaded": int(step.get("downloaded") or 0)},
            "meta": {"res": {"size": int(step.get("size") or 0)}},
        }

    def pause_task(self, task_id):
        self.paused.append(task_id)

    def delete_task(self, task_id, force=False):
        self.deleted.append((task_id, bool(force)))


class GopeedDownloadServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.local_path = str(Path(self.tempdir.name) / "relay" / "movie.mkv")
        self.resolver_calls = []
        self.file_item = make_file_item()

    def resolver(self, _file_item=None):
        self.resolver_calls.append(1)
        return "https://dl.example.com/movie.mkv", {"user-agent": "quark-pc-ua"}

    def make_service(self, client, fallback=None, **kwargs):
        kwargs.setdefault("gopeed_url", "http://gopeed:9999")
        kwargs.setdefault("gopeed_token", "unit-test-token")
        kwargs.setdefault("poll_interval", 0.01)
        kwargs.setdefault("backoff_base", 0.0)
        return GopeedDownloadService(
            self.resolver, fallback_service=fallback, client=client, **kwargs
        )

    def test_normal_completion_writes_file_and_returns_path(self):
        client = FakeGopeedClient(
            [[{"status": "running", "downloaded": 4, "size": 10},
              {"status": "done", "downloaded": 10, "size": 10, "write": True}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client)
        result = service.download_file(self.file_item, self.local_path)
        self.assertEqual(result, self.local_path)
        target = Path(self.local_path)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), PAYLOAD)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.created[0]["name"], "movie.mkv")
        self.assertEqual(
            client.created[0]["headers"].get("user-agent"), "quark-pc-ua"
        )
        # 完成后清理 Gopeed 任务记录（不删文件，force=False）
        self.assertEqual(client.deleted, [("task-1", False)])

    def test_progress_callback_invoked_and_monotonic(self):
        client = FakeGopeedClient(
            [[{"status": "running", "downloaded": 3, "size": 10},
              {"status": "running", "downloaded": 7, "size": 10},
              {"status": "running", "downloaded": 7, "size": 10},
              {"status": "done", "downloaded": 10, "size": 10, "write": True}]],
            str(Path(self.local_path).parent),
        )
        progress = []
        service = self.make_service(client)
        service.download_file(
            self.file_item, self.local_path,
            progress_callback=lambda done, total: progress.append((done, total)),
        )
        self.assertTrue(progress, "进度回调必须被调用")
        dones = [done for done, _ in progress]
        self.assertEqual(dones, sorted(dones), "进度上报不得回退")
        self.assertEqual(progress[-1], (10, 10))

    def test_task_failure_re_resolves_and_rebuilds(self):
        client = FakeGopeedClient(
            [
                [{"status": "error", "downloaded": 2, "size": 10}],
                [{"status": "done", "downloaded": 10, "size": 10, "write": True}],
            ],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client)
        result = service.download_file(self.file_item, self.local_path)
        self.assertEqual(result, self.local_path)
        self.assertGreaterEqual(
            len(self.resolver_calls), 2, "失败后必须重新取链再重建任务"
        )
        self.assertEqual(len(client.created), 2, "必须重建第二个 Gopeed 任务")
        self.assertEqual(
            client.deleted, [("task-1", False), ("task-2", False)],
            "失败任务与完成后的任务记录都应被清理（文件保留，force=False）",
        )

    def test_stop_requested_pauses_and_deletes_task(self):
        client = FakeGopeedClient(
            [[{"status": "running", "downloaded": 1, "size": 10}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, fallback=MagicMock())
        stop_calls = {"count": 0}

        def stop_requested():
            stop_calls["count"] += 1
            return stop_calls["count"] > 1

        with self.assertRaises(InterruptedError):
            service.download_file(
                self.file_item, self.local_path, stop_requested=stop_requested,
            )
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.paused, ["task-1"], "停止时必须暂停 Gopeed 任务")
        self.assertEqual(
            client.deleted, [("task-1", True)],
            "停止路径必须无条件 force 删除（连同 Gopeed 侧部分文件）",
        )
        service._fallback_service.download_file.assert_not_called()

    def test_stop_requested_force_deletes_even_with_preserve_partial(self):
        """preserve_partial=True 也不再保留 Gopeed 任务/部分文件（停止即清空中继）。"""
        client = FakeGopeedClient(
            [[{"status": "running", "downloaded": 1, "size": 10}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, fallback=MagicMock())
        stop_calls = {"count": 0}

        def stop_requested():
            stop_calls["count"] += 1
            return stop_calls["count"] > 1

        with self.assertRaises(InterruptedError):
            service.download_file(
                self.file_item, self.local_path, stop_requested=stop_requested,
                preserve_partial=True,
            )
        self.assertEqual(client.paused, ["task-1"])
        self.assertEqual(
            client.deleted, [("task-1", True)],
            "停止路径即使 preserve_partial=True 也必须 force 删除",
        )

    def test_service_passes_connections_to_create_task(self):
        """connections>0 时必须透传给 create_task（连接数用例：写死 0 即失败）。"""
        client = FakeGopeedClient(
            [[{"status": "done", "downloaded": 10, "size": 10, "write": True}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, connections=256)
        service.download_file(self.file_item, self.local_path)
        self.assertEqual(len(client.created), 1)
        self.assertEqual(
            client.created[0]["connections"], 256,
            "connections=256 必须原样透传给 GopeedClient.create_task",
        )

    def test_gopeed_unavailable_falls_back_to_builtin(self):
        broken = MagicMock()
        broken.create_task.side_effect = GopeedError(
            "连接 Gopeed 失败：Connection refused"
        )
        fallback = MagicMock()
        fallback.download_file.return_value = "/fallback/out.mkv"
        service = self.make_service(broken, fallback=fallback)
        progress = MagicMock()
        stop = MagicMock(return_value=False)
        with patch.object(gopeed_module, "logger") as fake_logger:
            result = service.download_file(
                self.file_item, self.local_path, progress, stop,
                preserve_partial=True,
            )
        self.assertEqual(result, "/fallback/out.mkv")
        fallback.download_file.assert_called_once_with(
            self.file_item, self.local_path, progress, stop,
            preserve_partial=True,
        )
        warnings = [
            call.args[0] for call in fake_logger.warning.call_args_list
            if call.args and "回落内置下载器" in str(call.args[0])
        ]
        self.assertEqual(len(warnings), 1, "回落必须恰好记一次 warning")
        self.assertNotIn("unit-test-token", warnings[0], "日志不得包含接口令牌")

    def test_path_prefix_mapping_applied_to_create_task(self):
        client = FakeGopeedClient(
            [[{"status": "done", "downloaded": 10, "size": 10, "write": True}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, path_prefix="/downloads=/tmp")
        result = service.download_file(self.file_item, self.local_path)
        self.assertEqual(result, self.local_path)
        parent = str(Path(self.local_path).parent)
        expected_dir = "/downloads" + parent[len("/tmp"):]
        self.assertEqual(client.created[0]["path"], expected_dir)
        self.assertEqual(client.created[0]["name"], "movie.mkv")

    def test_preserve_partial_keeps_failed_task(self):
        client = FakeGopeedClient(
            [
                [{"status": "error", "downloaded": 2, "size": 10}],
                [{"status": "error", "downloaded": 0, "size": 10}],
                [{"status": "error", "downloaded": 0, "size": 10}],
            ],
            str(Path(self.local_path).parent),
        )
        fallback = MagicMock()
        fallback.download_file.return_value = "/fallback/out.mkv"
        service = self.make_service(client, fallback=fallback)
        result = service.download_file(
            self.file_item, self.local_path, preserve_partial=True,
        )
        self.assertEqual(result, "/fallback/out.mkv")
        self.assertEqual(client.deleted, [], "preserve_partial 不得删除 Gopeed 任务")


    def test_external_pause_is_terminal_not_hang(self):
        """任务被外部暂停（status=pause）必须判定为失败，不能永久空转。"""
        client = FakeGopeedClient(
            [[{"status": "pause", "downloaded": 2, "size": 10}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, max_attempts=1, stall_timeout=0.2)
        with self.assertRaises(RuntimeError) as ctx:
            service.download_file(self.file_item, self.local_path)
        self.assertIn("pause", str(ctx.exception))

    def test_stall_timeout_fails_instead_of_hanging(self):
        """长时间零进度（Gopeed 卡死）必须超时失败，避免占住中继并发位。"""
        client = FakeGopeedClient(
            [[{"status": "running", "downloaded": 0, "size": 10}]],
            str(Path(self.local_path).parent),
        )
        service = self.make_service(client, max_attempts=1, stall_timeout=0.05)
        with self.assertRaises(RuntimeError) as ctx:
            service.download_file(self.file_item, self.local_path)
        self.assertIn("无进度", str(ctx.exception))


class GopeedClientTests(unittest.TestCase):
    def _response(self, status_code=200, payload=None):
        return SimpleNamespace(
            status_code=status_code,
            json=lambda: payload if payload is not None else {"code": 0, "data": {}},
        )

    def test_create_task_sends_token_header_and_body_shape(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)
            return self._response(payload={"code": 0, "data": "t-42"})

        client = GopeedClient("http://gopeed:9999/", "secret-token", timeout=5)
        with patch.object(gopeed_module.requests, "request", side_effect=fake_request):
            task_id = client.create_task(
                "https://dl.example.com/f", {"user-agent": "ua", "cookie": "c"},
                "/downloads", "f.mkv",
            )
        self.assertEqual(task_id, "t-42")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["url"], "http://gopeed:9999/api/v1/tasks")
        self.assertEqual(captured["headers"].get("X-Api-Token"), "secret-token")
        body = captured["json"]
        self.assertEqual(body["req"]["url"], "https://dl.example.com/f")
        self.assertEqual(body["req"]["extra"]["header"]["user-agent"], "ua")
        self.assertEqual(body["opts"]["path"], "/downloads")
        self.assertEqual(body["opts"]["name"], "f.mkv")
        self.assertEqual(body["opts"]["selectFiles"], [])
        # connections 默认 0 → 不写 opts.extra（沿用 Gopeed 全局连接数）
        self.assertNotIn("extra", body["opts"])

    def test_create_task_with_connections_writes_opts_extra(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return self._response(payload={"code": 0, "data": "t-43"})

        client = GopeedClient("http://gopeed:9999", "tok", timeout=5)
        with patch.object(gopeed_module.requests, "request", side_effect=fake_request):
            client.create_task(
                "https://dl.example.com/f", {"user-agent": "ua"},
                "/downloads", "f.mkv", connections=256,
            )
        self.assertEqual(captured["json"]["opts"]["extra"], {"connections": 256})

    def test_create_task_with_zero_connections_omits_extra(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            return self._response(payload={"code": 0, "data": "t-44"})

        client = GopeedClient("http://gopeed:9999", "tok", timeout=5)
        with patch.object(gopeed_module.requests, "request", side_effect=fake_request):
            client.create_task(
                "https://dl.example.com/f", {}, "/downloads", "f.mkv",
                connections=0,
            )
        self.assertNotIn("extra", captured["json"]["opts"])

    def test_delete_task_default_url_has_no_force(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return self._response()

        client = GopeedClient("http://gopeed:9999", "tok", timeout=5)
        with patch.object(gopeed_module.requests, "request", side_effect=fake_request):
            client.delete_task("t-1")
        self.assertEqual(captured["method"], "DELETE")
        self.assertEqual(captured["url"], "http://gopeed:9999/api/v1/tasks/t-1")

    def test_delete_task_force_appends_force_true(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            return self._response()

        client = GopeedClient("http://gopeed:9999", "tok", timeout=5)
        with patch.object(gopeed_module.requests, "request", side_effect=fake_request):
            client.delete_task("t-1", force=True)
        self.assertEqual(captured["method"], "DELETE")
        self.assertTrue(
            captured["url"].endswith("/api/v1/tasks/t-1?force=true"),
            f"force=true 必须以 ?force=true 结尾，实际：{captured['url']}",
        )

    def test_401_raises_without_token_in_message(self):
        client = GopeedClient("http://gopeed:9999", "secret-token")
        with patch.object(
            gopeed_module.requests, "request",
            return_value=self._response(status_code=401),
        ):
            with self.assertRaises(GopeedError) as ctx:
                client.info()
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_nonzero_code_raises(self):
        client = GopeedClient("http://gopeed:9999", "secret-token")
        with patch.object(
            gopeed_module.requests, "request",
            return_value=self._response(payload={"code": 500, "message": "boom"}),
        ):
            with self.assertRaises(GopeedError) as ctx:
                client.get_task("t-1")
        self.assertIn("500", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))

    def test_connection_error_wrapped(self):
        import requests as requests_lib

        client = GopeedClient("http://gopeed:9999", "secret-token")
        with patch.object(
            gopeed_module.requests, "request",
            side_effect=requests_lib.ConnectionError("refused"),
        ):
            with self.assertRaises(GopeedError) as ctx:
                client.pause_task("t-1")
        self.assertIn("连接 Gopeed 失败", str(ctx.exception))
        self.assertNotIn("secret-token", str(ctx.exception))


class QuarkDownload分流Tests(unittest.TestCase):
    """QuarkFileService.download_file 的 builtin/gopeed 分流与回落。"""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.local_path = str(Path(self.tempdir.name) / "movie.mkv")
        self.file_item = make_file_item()
        self.http_instance = MagicMock()
        self.http_instance.download_file.return_value = "/builtin/out.mkv"
        self.http_cls = MagicMock(return_value=self.http_instance)

    def make_quark(self, **kwargs):
        return QuarkFileService(client=MagicMock(), **kwargs)

    def test_default_builtin_never_touches_gopeed(self):
        quark = self.make_quark()
        gopeed_ctor = MagicMock()
        with patch.object(files_module, "HttpFileDownloadService", self.http_cls), \
                patch.object(files_module, "GopeedDownloadService", gopeed_ctor):
            result = quark.download_file(
                self.file_item, self.local_path, download_threads=7,
            )
        self.assertEqual(result, "/builtin/out.mkv")
        gopeed_ctor.assert_not_called()
        self.http_cls.assert_called_once()
        self.assertEqual(
            self.http_cls.call_args.kwargs.get("concurrency"), 7
        )
        self.http_instance.download_file.assert_called_once()

    def test_gopeed_mode_constructs_gopeed_service(self):
        quark = self.make_quark(
            cross_transfer_downloader="gopeed",
            gopeed_url="http://gopeed:9999",
            gopeed_token="tok",
        )
        gopeed_instance = MagicMock()
        gopeed_instance.download_file.return_value = "/gopeed/out.mkv"
        gopeed_ctor = MagicMock(return_value=gopeed_instance)
        with patch.object(files_module, "HttpFileDownloadService", self.http_cls), \
                patch.object(files_module, "GopeedDownloadService", gopeed_ctor):
            result = quark.download_file(
                self.file_item, self.local_path, download_threads=3,
            )
        self.assertEqual(result, "/gopeed/out.mkv")
        self.http_instance.download_file.assert_not_called()
        gopeed_ctor.assert_called_once()
        self.assertIs(
            gopeed_ctor.call_args.kwargs.get("fallback_service"),
            self.http_instance,
        )
        self.assertEqual(
            gopeed_ctor.call_args.kwargs.get("gopeed_url"), "http://gopeed:9999"
        )
        gopeed_instance.download_file.assert_called_once()

    def test_gopeed_connections_passed_to_gopeed_service(self):
        """QuarkFileService 必须把 gopeed_connections 透传给 GopeedDownloadService。"""
        quark = self.make_quark(
            cross_transfer_downloader="gopeed",
            gopeed_url="http://gopeed:9999",
            gopeed_token="tok",
            gopeed_connections=256,
        )
        gopeed_instance = MagicMock()
        gopeed_instance.download_file.return_value = "/gopeed/out.mkv"
        gopeed_ctor = MagicMock(return_value=gopeed_instance)
        with patch.object(files_module, "HttpFileDownloadService", self.http_cls), \
                patch.object(files_module, "GopeedDownloadService", gopeed_ctor):
            quark.download_file(self.file_item, self.local_path)
        gopeed_ctor.assert_called_once()
        self.assertEqual(
            gopeed_ctor.call_args.kwargs.get("connections"), 256,
            "gopeed_connections=256 必须透传到 GopeedDownloadService",
        )

    def test_gopeed_connections_default_zero_passthrough(self):
        """未配置时（默认 0）同样显式透传 0（表示沿用 Gopeed 全局连接数）。"""
        quark = self.make_quark(
            cross_transfer_downloader="gopeed",
            gopeed_url="http://gopeed:9999",
        )
        gopeed_instance = MagicMock()
        gopeed_instance.download_file.return_value = "/gopeed/out.mkv"
        gopeed_ctor = MagicMock(return_value=gopeed_instance)
        with patch.object(files_module, "HttpFileDownloadService", self.http_cls), \
                patch.object(files_module, "GopeedDownloadService", gopeed_ctor):
            quark.download_file(self.file_item, self.local_path)
        gopeed_ctor.assert_called_once()
        self.assertEqual(gopeed_ctor.call_args.kwargs.get("connections"), 0)

    def test_gopeed_error_falls_back_to_builtin_with_warning(self):
        quark = self.make_quark(
            cross_transfer_downloader="gopeed",
            gopeed_url="http://gopeed:9999",
            gopeed_token="unit-test-token",
        )
        broken_client = MagicMock()
        broken_client.create_task.side_effect = GopeedError(
            "Gopeed 鉴权失败（HTTP 401），请检查接口令牌"
        )
        with patch.object(files_module, "HttpFileDownloadService", self.http_cls), \
                patch.object(gopeed_module, "GopeedClient", return_value=broken_client), \
                patch.object(gopeed_module, "logger") as fake_logger:
            result = quark.download_file(self.file_item, self.local_path)
        self.assertEqual(result, "/builtin/out.mkv")
        self.http_instance.download_file.assert_called_once_with(
            self.file_item, self.local_path, None, None,
            preserve_partial=False,
        )
        warnings = [
            call.args[0] for call in fake_logger.warning.call_args_list
            if call.args and "回落内置下载器" in str(call.args[0])
        ]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("unit-test-token", warnings[0])


class TransferCancel中继清理Tests(unittest.TestCase):
    """传输层取消（B2）：取消即清空中继，含已下完的完整缓存与部分缓存。

    只删本任务的 cloud-transfer-<hash>.bin 与 .meta.json/.parts.json（含 .tmp
    变体），不碰同目录其它文件。
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.payload = PAYLOAD
        self.file_item = CloudFile(
            id="fid1", name="movie.mkv", is_directory=False,
            size=len(self.payload),
            sha1=hashlib.sha1(self.payload).hexdigest(),
            md5=hashlib.md5(self.payload).hexdigest(),
        )

    def _make_manager(self, download_file=None, upload_file=None):
        source = SimpleNamespace(
            name="夸克网盘",
            supports=lambda _capability: False,
            require=lambda _capability: SimpleNamespace(download_file=download_file),
        )

        def require_target(capability):
            cap = transfer_module.CloudDriveCapability
            if capability == cap.LOCAL_UPLOAD:
                return SimpleNamespace(
                    upload_file=upload_file, upload_from_link=None,
                    upload_progressive=None,
                )
            if capability == cap.FILE_QUERY:
                return SimpleNamespace(find_file=lambda *_args: None)
            raise AssertionError(f"测试不应 require {capability}")

        target = SimpleNamespace(
            name="115",
            supports=lambda _capability: False,
            require=require_target,
        )

        def resolver(key):
            return target if key == "115" else source

        manager = transfer_module.CrossTransferTaskManager(
            resolver, download_path=self.tempdir.name,
            download_threads=5, max_concurrent=1, direct_stream=False,
        )
        return manager

    def _seed_complete_cache(self, manager, with_stale_parts=True):
        cache_key = manager._cache_key("quark", self.file_item)
        cache_path = manager._cache_path(cache_key)
        cache_path.write_bytes(self.payload)
        manager._mark_complete_cache(cache_path, cache_key, self.file_item)
        if with_stale_parts:
            manager._cache_parts_path(cache_path).write_text(
                json.dumps({
                    "version": 1, "total": len(self.payload),
                    "part_size": 4, "completed": [0, 4],
                }), encoding="utf-8",
            )
        return cache_path

    def _wait_terminal(self, manager, task_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with manager._lock:
                task = manager._tasks.get(task_id)
                status = task["status"] if task else None
            if task and status not in manager.ACTIVE:
                return task
            time.sleep(0.05)
        self.fail("任务未在期限内进入终态")

    def test_cancel_during_upload_deletes_complete_cache_and_parts(self):
        """完整缓存 + 上传中取消 → .bin/.meta.json/.parts.json 全部删除。"""
        def upload_file(source_path, save_path, target_name, progress_callback=None):
            # 上传途中用户点停止：stop_event 置位后下一次进度回调立即中断
            with manager._lock:
                internal = next(iter(manager._tasks.values()))
            internal["stop_event"].set()
            progress_callback(1, len(self.payload))
            return True

        manager = self._make_manager(upload_file=upload_file)
        cache_path = self._seed_complete_cache(manager)
        meta_path = manager._cache_meta_path(cache_path)
        parts_path = manager._cache_parts_path(cache_path)
        bystander = Path(self.tempdir.name) / "keep-me.bin"
        bystander.write_bytes(b"bystander")

        public = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "canceled")
        self.assertIn("已清理中继缓存", task["message"])
        self.assertFalse(cache_path.exists(), "取消必须删除完整缓存 .bin")
        self.assertFalse(meta_path.exists(), "取消必须删除 .meta.json")
        self.assertFalse(parts_path.exists(), "取消必须删除 .parts.json")
        self.assertTrue(bystander.is_file(), "不得误删同目录其它文件")

    def test_cancel_during_download_deletes_partial_cache_and_parts(self):
        """下载中取消（部分缓存 + parts.json）→ 缓存与 parts.json 全部删除。"""
        manager_ref = {}

        def download_file(file_item, local_path, progress_callback=None,
                          stop_requested=None, preserve_partial=True,
                          download_threads=5):
            manager = manager_ref["manager"]
            cache_path = manager._cache_path(manager._cache_key("quark", file_item))
            cache_path.write_bytes(self.payload[:4])
            manager._cache_parts_path(cache_path).write_text(
                json.dumps({
                    "version": 1, "total": len(self.payload),
                    "part_size": 4, "completed": [0],
                }), encoding="utf-8",
            )
            with manager._lock:
                internal = next(iter(manager._tasks.values()))
            internal["stop_event"].set()
            raise InterruptedError

        manager = self._make_manager(download_file=download_file)
        manager_ref["manager"] = manager
        cache_path = manager._cache_path(manager._cache_key("quark", self.file_item))
        meta_path = manager._cache_meta_path(cache_path)
        parts_path = manager._cache_parts_path(cache_path)

        public = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "canceled")
        self.assertFalse(cache_path.exists(), "取消必须删除部分缓存 .bin")
        self.assertFalse(meta_path.exists(), "取消必须删除 .meta.json")
        self.assertFalse(parts_path.exists(), "取消必须删除 .parts.json")


class GopeedRelayResidueTests(unittest.TestCase):
    """1.6.47 Gopeed 中继残留清理与改名副本容忍（A3）。"""

    KEY = "cloud-transfer-abc123"

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.relay_dir = Path(self.tempdir.name)
        self.local_path = str(self.relay_dir / f"{self.KEY}.bin")
        self.file_item = make_file_item(size=len(PAYLOAD))

    def make_service(self, client=None, **kwargs):
        kwargs.setdefault("gopeed_url", "http://gopeed:9999")
        kwargs.setdefault("gopeed_token", "unit-test-token")
        kwargs.setdefault("poll_interval", 0.01)
        kwargs.setdefault("backoff_base", 0.0)
        return GopeedDownloadService(
            self.resolver, fallback_service=None, client=client, **kwargs
        )

    def resolver(self, _file_item=None):
        return "https://dl.example.com/f", {"user-agent": "ua"}

    def _touch(self, name, content=b"x"):
        path = self.relay_dir / name
        path.write_bytes(content)
        return path

    def test_clear_residue_strict_scope(self):
        """范围严格：只删本任务 bin/meta/parts（含 .tmp 与改名副本）。"""
        bin_path = self._touch(f"{self.KEY}.bin")
        parts = self._touch(f"{self.KEY}.bin.parts.json")
        parts_tmp = self._touch(f"{self.KEY}.bin.parts.json.tmp")
        meta_tmp = self._touch(f"{self.KEY}.bin.meta.json.tmp")
        renamed = self._touch(f"{self.KEY} (1).bin")
        meta_dir = self.relay_dir / f"{self.KEY}.bin.meta.json"
        meta_dir.mkdir()  # 同名目录不得删（is_file 守卫）
        keepers = [
            self._touch(f"{self.KEY}4.bin"),          # 不同 key
            self._touch("keep-me.bin"),               # 同目录其它文件
            self._touch("cloud-transfer-other.bin"),
            self._touch(f"{self.KEY}.bin.bak"),       # 变体之外的后缀
        ]

        service = self.make_service(client=MagicMock())
        removed = service._clear_relay_residue(Path(self.local_path))

        self.assertEqual(removed, 5, "应删除本任务 5 个残留文件")
        for gone in (bin_path, parts, parts_tmp, meta_tmp, renamed):
            self.assertFalse(gone.exists(), f"残留必须删除：{gone.name}")
        self.assertTrue(meta_dir.is_dir(), "同名目录不得删除")
        for kept in keepers:
            self.assertTrue(kept.is_file(), f"不得越界删除：{kept.name}")

    def test_clear_residue_non_relay_target_is_noop(self):
        """非中继目标（普通文件名）不做任何清理。"""
        target = self.relay_dir / "movie.mkv"
        keep = self._touch("movie.mkv")
        other = self._touch("cloud-transfer-abc123.txt")
        service = self.make_service(client=MagicMock())
        self.assertEqual(service._clear_relay_residue(target), 0)
        self.assertTrue(keep.is_file())
        self.assertTrue(other.is_file())

    def test_find_renamed_relay_requires_size_match(self):
        """改名副本：key 相符 + 大小相符才返回；size 未知（<=0）放行。"""
        service = self.make_service(client=MagicMock())
        target = Path(self.local_path)
        renamed = self._touch(f"{self.KEY} (1).bin", PAYLOAD)
        wrong_key = self._touch("cloud-transfer-zzz (1).bin", PAYLOAD)

        found = service._find_renamed_relay(target, len(PAYLOAD))
        self.assertIsNotNone(found)
        self.assertEqual(found.name, renamed.name)
        self.assertIsNone(
            service._find_renamed_relay(target, len(PAYLOAD) + 5),
            "大小不符必须拒绝",
        )
        self.assertIsNotNone(
            service._find_renamed_relay(target, 0),
            "源大小未知时允许按改名副本通过",
        )
        renamed.write_bytes(b"")
        self.assertIsNone(
            service._find_renamed_relay(target, len(PAYLOAD)),
            "改名副本大小不符时不得误认",
        )
        self.assertTrue(wrong_key.is_file())

    def test_verify_result_accepts_renamed_copy(self):
        """校验：精确路径缺失但改名副本大小相符 → 归位到规范路径后通过。"""
        service = self.make_service(client=MagicMock())
        target = Path(self.local_path)
        renamed = self._touch(f"{self.KEY} (1).bin", PAYLOAD)

        result = service._verify_result(self.file_item, target)
        self.assertEqual(
            result, str(target),
            "必须归位到规范路径（缓存标记/复用/清理都按规范路径操作）",
        )
        self.assertTrue(target.is_file(), "归位后规范路径必须存在")
        self.assertFalse(renamed.exists(), "改名副本不得残留")

    def test_verify_result_keeps_renamed_path_when_move_fails(self):
        """归位失败时按改名副本路径继续（不因清理动作把成功的下载判失败）。"""
        service = self.make_service(client=MagicMock())
        target = Path(self.local_path)
        renamed = self._touch(f"{self.KEY} (1).bin", PAYLOAD)

        with patch.object(Path, "replace", side_effect=OSError("busy")):
            result = service._verify_result(self.file_item, target)

        self.assertEqual(result, str(renamed), "归位失败必须回退到副本路径")
        self.assertTrue(renamed.is_file())

    def test_missing_file_raises_diagnostics(self):
        """缺文件异常：期望路径 + Gopeed 侧映射目录 + 排查提示，不含令牌。"""
        service = self.make_service(
            client=MagicMock(),
            path_prefix=f"/downloads={self.relay_dir}",
        )
        target = Path(self.local_path)

        with self.assertRaises(RuntimeError) as ctx:
            service._verify_result(self.file_item, target)
        message = str(ctx.exception)
        self.assertIn(str(target), message)
        self.assertIn("Gopeed 侧映射目录=/downloads", message)
        self.assertIn("同源任务并发", message)
        self.assertIn("gopeed_path_prefix", message)
        self.assertNotIn("unit-test-token", message)

    def test_full_download_wraps_diagnostics_after_retries(self):
        """三次都落空：最终异常仍携带可诊断信息（不是裸 Errno）。"""
        client = FakeGopeedClient(
            [
                [{"status": "done", "downloaded": 10, "size": 10, "write": False}],
                [{"status": "done", "downloaded": 10, "size": 10, "write": False}],
                [{"status": "done", "downloaded": 10, "size": 10, "write": False}],
            ],
            str(self.relay_dir),
        )
        service = self.make_service(client)

        with self.assertRaises(RuntimeError) as ctx:
            service.download_file(self.file_item, self.local_path)
        message = str(ctx.exception)
        self.assertIn("本机未见文件", message)
        self.assertIn(self.local_path, message)
        self.assertIn("Gopeed 侧映射目录=", message)
        self.assertNotIn("unit-test-token", message)
        self.assertEqual(len(client.created), 3, "耗尽重试次数")

    def test_residue_cleared_before_create_task(self):
        """每次建任务前先清本任务残留（含改名副本），Gopeed 侧不再 AutoRename。"""
        stale = self._touch(f"{self.KEY}.bin")
        renamed = self._touch(f"{self.KEY} (1).bin")
        keeper = self._touch("keep-me.bin")
        snapshots = []
        relay_dir = self.relay_dir
        base_client_cls = FakeGopeedClient

        class RecordingClient(base_client_cls):
            def create_task(self, url, headers, path, name, connections=0):
                snapshots.append(
                    sorted(entry.name for entry in relay_dir.iterdir())
                )
                return super().create_task(
                    url=url, headers=headers, path=path, name=name,
                    connections=connections,
                )

        client = RecordingClient(
            [[{"status": "done", "downloaded": 10, "size": 10, "write": True}]],
            str(self.relay_dir),
        )
        service = self.make_service(client)

        result = service.download_file(self.file_item, self.local_path)

        self.assertEqual(result, self.local_path)
        self.assertEqual(len(snapshots), 1)
        at_create = snapshots[0]
        self.assertNotIn(stale.name, at_create, "残留 .bin 必须在建任务前删除")
        self.assertNotIn(renamed.name, at_create, "改名副本必须在建任务前删除")
        self.assertIn(keeper.name, at_create, "不得误删同目录其它文件")


if __name__ == "__main__":
    unittest.main()
