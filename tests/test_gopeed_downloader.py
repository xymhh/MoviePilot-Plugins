"""1.6.43 Gopeed 外部下载器单测。

跨盘中继下载新增可选外部下载器 Gopeed（默认 builtin）。本文件固定：

1. Gopeed 正常完成 → 文件落在 local_path 并返回该路径；
2. 进度回调被调用且单调不减；
3. 任务失败 → 重新取链 + 重建任务（resolver 被调用 ≥2 次）；
4. stop_requested → 暂停/删除 Gopeed 任务并抛错（且不回落内置）；
5. Gopeed 不可用（连不上/401/建任务失败）→ 回落内置下载器（断言内置服务被调用、
   恰好记一次 warning）；
6. gopeed_path_prefix 映射生效（create_task 收到换算后的 Gopeed 侧路径）；
7. 夸克 QuarkFileService.download_file 分流：builtin（默认）不碰 Gopeed、gopeed 走
   外部服务、gopeed 异常回落内置；
8. GopeedClient 契约：X-Api-Token 鉴权、任务体形状、401/非零 code/连接异常报错且
   消息不含 token。
"""

import os
import sys
import tempfile
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
    return os.path.join(plugins_v2_path, "cloudsubscribe", *parts)


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

# ---- cloudsubscribe 包层级（core/drive 用真实路径，子模块按文件加载）----
_register_real_package("cloudsubscribe", _plugin_dir())
_register_real_package("cloudsubscribe.core", _plugin_dir("core"))
_register_real_package("cloudsubscribe.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribe.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribe.utils.cache",
    create_platform_ttl_cache=MagicMock(return_value=MagicMock()),
)

cloud_module = load_module(
    "cloudsubscribe.core.cloud", "plugins.v2/cloudsubscribe/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribe.core.transfer", "plugins.v2/cloudsubscribe/core/transfer.py"
)
gopeed_module = load_module(
    "cloudsubscribe.core.gopeed", "plugins.v2/cloudsubscribe/core/gopeed.py"
)
load_module("cloudsubscribe.drive.common", "plugins.v2/cloudsubscribe/drive/common.py")
files_module = load_module(
    "cloudsubscribe.drive.quark.files", "plugins.v2/cloudsubscribe/drive/quark/files.py"
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

    def create_task(self, url, headers, path, name):
        self._counter += 1
        task_id = f"task-{self._counter}"
        steps = self._scripts.pop(0) if self._scripts else [
            {"status": "done", "downloaded": 0, "size": 0, "write": True}
        ]
        self.created.append({
            "id": task_id, "url": url, "headers": dict(headers or {}),
            "path": path, "name": name,
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

    def delete_task(self, task_id):
        self.deleted.append(task_id)


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
        # 完成后清理 Gopeed 任务记录（不删文件）
        self.assertEqual(client.deleted, ["task-1"])

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
            client.deleted, ["task-1", "task-2"],
            "失败任务与完成后的任务记录都应被清理（文件保留）",
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
        self.assertEqual(client.deleted, ["task-1"], "未保留断点时删除 Gopeed 任务")
        service._fallback_service.download_file.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
