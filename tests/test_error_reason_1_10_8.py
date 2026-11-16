"""1.10.8：失败原因拆包 + 脱敏；下载页季集回退 season_episode。

复现（2026-11-16 07:56，LOST10 S01E05 在 1.10.7 下重试仍失败）：
失败原因只剩一份上传参数字典，看不到 115 的真正报错——p115oss
会把上传失败包成 MultipartUploadAbort，消息体就是那份字典（还含
user_key 登录凭证），真报错藏在异常的 __cause__ 里，插件没去读；
且 1.10.7 的 300 字截断正好把字典从中间切断。本版：

- core/error_text.py（新）：describe_upload_error 沿 __cause__/
  __context__ 走到根因，从 OSError(errno, 响应字典) 或字典文本里
  抽 errno/error；sanitize_error_text 把 user_key/token 等凭证值
  换成 ***。
- drive/p115/upload.py：upload_file 的 except 用拆包后的原因写
  last_error 与日志行（同一句）。
- core/transfer.py：_describe_transfer_error 先脱敏再返回（任务
  error 落盘前的最后一道关）。
- core/downloader_view.py：_task_media 的 season/episode 一律留空，
  卡片模板据此回退到 season_episode（S01E09），不再拼裸数字 "1 9"。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

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

# p115 files 用占位模块（upload 只需要类名做依赖注入）。
p115_files_module = register_module(
    "cloudsubscribex.drive.p115.files", package=True
)
p115_files_module.P115DirectoryReader = MagicMock()
p115_files_module.P115FileService = MagicMock()

error_text_module = load_module(
    "cloudsubscribex.core.error_text",
    "plugins.v2/cloudsubscribex/core/error_text.py",
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
upload_module = load_module(
    "cloudsubscribex.drive.p115.upload",
    "plugins.v2/cloudsubscribex/drive/p115/upload.py",
)
view_module = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)

from p115oss.upload import MultipartUploadAbort  # noqa: E402

# 注意：以下 user_key 是测试用的假值（32 位十六进制占位），
# 形状与真凭据相同、值是假的；真凭据绝不能写进测试。
FAKE_USER_KEY = "FAKEUSERKEY0123456789ABCDEF0123"
UPLOAD_PARAMS = {
    "pid": 3538356044075894093,
    "filename": "05.mkv",
    "filesha1": "8BC9718D179B4F83343A595713A631DC842BDAFA",
    "filesize": 3239498038,
    "user_id": 101991011,
    "user_key": FAKE_USER_KEY,
    "partsize": 16777216,
    "callback": {},
    "upload_id": "",
}


def _abort_with_cause(cause):
    """复现 p115oss 的包法：参数字典当消息，真报错在 __cause__ 里。"""
    try:
        raise MultipartUploadAbort(dict(UPLOAD_PARAMS)) from cause
    except MultipartUploadAbort as error:
        return error


def _abort_without_cause():
    try:
        raise MultipartUploadAbort(dict(UPLOAD_PARAMS))
    except MultipartUploadAbort as error:
        return error


class DescribeUploadErrorTests(unittest.TestCase):
    def test_abort_unwraps_p115_response_dict(self):
        cause = OSError(
            5, {"state": False, "errno": 429, "error": "请求太频繁"},
        )
        text = error_text_module.describe_upload_error(
            _abort_with_cause(cause)
        )
        self.assertIn("429", text)
        self.assertIn("请求太频繁", text)
        self.assertNotIn(FAKE_USER_KEY, text)
        self.assertNotIn("'pid'", text)
        self.assertNotIn("05.mkv", text)

    def test_abort_unwraps_plain_network_error(self):
        cause = ConnectionError(
            "HTTPSConnectionPool(host='oss-cn-shenzhen.aliyuncs.com'): "
            "Max retries exceeded"
        )
        text = error_text_module.describe_upload_error(
            _abort_with_cause(cause)
        )
        self.assertIn("Max retries exceeded", text)
        self.assertNotIn(FAKE_USER_KEY, text)

    def test_abort_without_cause_is_not_stored_as_dict(self):
        text = error_text_module.describe_upload_error(
            _abort_without_cause()
        )
        self.assertEqual(text, "115 未返回具体报错（分片上传失败）")
        self.assertNotIn(FAKE_USER_KEY, text)

    def test_plain_p115_oserror_dict_is_extracted(self):
        error = OSError(
            5, {"state": False, "errno": 99, "error": "请重新登录"},
        )
        text = error_text_module.describe_upload_error(error)
        self.assertEqual(text, "errno 99：请重新登录")

    def test_plain_message_passes_through(self):
        text = error_text_module.describe_upload_error(
            ValueError("磁盘已满")
        )
        self.assertEqual(text, "磁盘已满")


class SanitizeErrorTextTests(unittest.TestCase):
    def test_user_key_single_quotes(self):
        text = error_text_module.sanitize_error_text(
            f"上传失败：{{'user_key': '{FAKE_USER_KEY}', 'pid': 1}}"
        )
        self.assertIn("'user_key': '***'", text)
        self.assertNotIn(FAKE_USER_KEY, text)
        self.assertIn("'pid': 1", text)

    def test_user_key_double_quotes(self):
        text = error_text_module.sanitize_error_text(
            f'{{"user_key": "{FAKE_USER_KEY}"}}'
        )
        self.assertIn('"user_key": "***"', text)
        self.assertNotIn(FAKE_USER_KEY, text)

    def test_token_assignment_and_bearer(self):
        text = error_text_module.sanitize_error_text(
            "token=abc123; Authorization: Bearer secret-token-xyz"
        )
        self.assertIn("token=***", text)
        self.assertNotIn("abc123", text)
        self.assertNotIn("secret-token-xyz", text)

    def test_clean_text_is_unchanged(self):
        self.assertEqual(
            error_text_module.sanitize_error_text("errno 429：请求太频繁"),
            "errno 429：请求太频繁",
        )


class P115UploadDescribeTests(unittest.TestCase):
    """drive 层：真实 MultipartUploadAbort 进来，last_error 要干净。"""

    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "P115OSS_AVAILABLE": upload_module.P115OSS_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
        }
        upload_module.P115_AVAILABLE = True
        upload_module.P115OSS_AVAILABLE = False
        upload_module.check_response = MagicMock()
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, upload_side_effect):
        owner = OwnerDelegator(SimpleNamespace(
            client=MagicMock(),
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        owner.client.upload_file.side_effect = upload_side_effect
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def test_abort_becomes_readable_last_error(self):
        cause = OSError(
            5, {"state": False, "errno": 429, "error": "请求太频繁"},
        )

        def _raise(local_path, **kwargs):
            raise _abort_with_cause(cause)

        service = self._make_service(_raise)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(b"0123456789")
            self.assertFalse(service.upload_file(str(source), "/"))
        self.assertIn("429", service.last_error)
        self.assertIn("请求太频繁", service.last_error)
        self.assertNotIn(FAKE_USER_KEY, service.last_error)
        self.assertNotIn("'pid'", service.last_error)


class DescribeTransferErrorTests(unittest.TestCase):
    def test_sanitizes_before_storing(self):
        error = RuntimeError(
            "秒传未命中且普通上传失败：115 本地文件上传失败：05.mkv，"
            f"{{'user_key': '{FAKE_USER_KEY}'}}"
        )
        text = transfer_module._describe_transfer_error(error)
        self.assertNotIn(FAKE_USER_KEY, text)
        self.assertIn("'user_key': '***'", text)

    def test_errno_24_hint_still_works(self):
        error = OSError(24, "Too many open files")
        text = transfer_module._describe_transfer_error(error)
        self.assertIn("Errno 24", text)
        self.assertIn("下载线程数", text)


class _Torrent:
    """桩 DownloaderTorrent：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class EpisodeDisplayTests(unittest.TestCase):
    """下载页投影：季/集留空，卡片回退到 season_episode（S01E09）。"""

    def setUp(self):
        view_module.clear_poster_cache()

    def _task(self, **overrides):
        task = {
            "id": "cross:abcdef123456",
            "title": "9.mp4",
            "media_type": "电视剧",
            "media_title": "假面骑士麦斯",
            "year": "2026",
            "season": 1,
            "tmdb_id": 326140,
            "target_episodes": [9],
            "status": "downloading",
            "progress": 0,
            "total": 1000,
        }
        task.update(overrides)
        return task

    def test_season_episode_empty_and_text_is_s01e09(self):
        torrent = view_module.task_to_torrent(
            self._task(), torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "",
        )
        self.assertIsNone(torrent.media["season"])
        self.assertIsNone(torrent.media["episode"])
        self.assertEqual(torrent.season_episode, "S01E09")
        self.assertEqual(torrent.name, "假面骑士麦斯 S01E09")

    def test_multi_episode_range_still_uses_season_episode(self):
        torrent = view_module.task_to_torrent(
            self._task(target_episodes=[21, 22]),
            torrent_cls=_Torrent,
            poster_resolver=lambda tmdb_id, media_type: "",
        )
        self.assertIsNone(torrent.media["season"])
        self.assertIsNone(torrent.media["episode"])
        self.assertEqual(torrent.season_episode, "S01E21-E22")


if __name__ == "__main__":
    unittest.main()
