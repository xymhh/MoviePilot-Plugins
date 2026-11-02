"""光鸭下载链接字段兼容单测。

回归上游 1.7.5 修复：光鸭 API 在不同接口/时段返回的直链字段名不一致，
``signedURL``（全大写 URL）必须与 ``signedUrl`` / ``downloadUrl`` 同等对待，
否则 302 播放与跨盘转存都会抛「光鸭未返回有效下载地址」。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import load_module, register_mock, register_module

register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=dict)
register_module("app.log", logger=MagicMock())

register_module("cloudsubscribex", package=True)
register_module("cloudsubscribex.core", package=True)
register_module("cloudsubscribex.drive", package=True)
register_module("cloudsubscribex.drive.guangya", package=True)
register_module("cloudsubscribex.core.cloud", CloudFile=object)
register_module(
    "cloudsubscribex.core.transfer", HttpFileDownloadService=MagicMock()
)
register_module(
    "cloudsubscribex.drive.common",
    CloudDriveFileServiceBase=object,
    create_directory_cache=MagicMock(return_value=MagicMock()),
    safe_int=lambda value: int(value or 0),
)

GuangyaFileService = load_module(
    "cloudsubscribex.drive.guangya.files",
    "plugins.v2/cloudsubscribex/drive/guangya/files.py",
).GuangyaFileService


def _make_service(data, response=None):
    client = MagicMock()
    client.API_BASE_URL = "https://api.guangya.example"
    client.request.return_value = response if response is not None else {"code": 0}
    client.data.return_value = data
    return GuangyaFileService(client=client), client


def _file_item():
    return SimpleNamespace(id="f1", name="a.mp4", playback_values={})


class GuangyaSignedUrlTest(unittest.TestCase):
    def test_signed_url_upper_case(self):
        """signedURL（全大写 URL）是线上真实返回的字段名。"""
        service, _ = _make_service({"signedURL": "https://cdn.example/a.mp4?sig=1"})
        url, _headers = service.resolve_download_link(_file_item())
        self.assertEqual(url, "https://cdn.example/a.mp4?sig=1")

    def test_signed_url_camel_case(self):
        service, _ = _make_service({"signedUrl": "https://cdn.example/b.mp4"})
        url, _headers = service.resolve_download_link(_file_item())
        self.assertEqual(url, "https://cdn.example/b.mp4")

    def test_download_url_fallback(self):
        service, _ = _make_service({"downloadUrl": "https://cdn.example/c.mp4"})
        url, _headers = service.resolve_download_link(_file_item())
        self.assertEqual(url, "https://cdn.example/c.mp4")

    def test_share_access_token_uses_share_endpoint(self):
        """带 share access token 时走分享下载接口，且同样兼容 signedURL。"""
        service, client = _make_service({"signedURL": "https://cdn.example/d.mp4"})
        item = SimpleNamespace(
            id="f2", name="d.mp4",
            playback_values={"share_access_token": "tok"},
        )
        url, _headers = service.resolve_download_link(item)
        self.assertEqual(url, "https://cdn.example/d.mp4")
        self.assertIn("get_share_download_url", client.request.call_args[0][1])

    def test_empty_url_raises_platform_message(self):
        service, _ = _make_service(
            {"code": 403}, response={"code": 403, "msg": "权限不足"}
        )
        with self.assertRaises(RuntimeError) as ctx:
            service.resolve_download_link(_file_item())
        self.assertIn("权限不足", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
