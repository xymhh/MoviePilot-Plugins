"""STRM 播放直链 302 跳转单测（pickcode → 直链）。"""

import asyncio
import os
import sys
import types
import unittest
from types import SimpleNamespace

from plugin_env import ensure_package, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _install_fastapi_stub():
    """测试环境无 fastapi，注入最小 Response 家族。"""
    if "fastapi" in sys.modules and hasattr(sys.modules["fastapi"], "responses"):
        return

    class Request:
        def __init__(self, method="GET"):
            self.method = method

    class Response:
        def __init__(self, status_code=200, headers=None, content=None):
            self.status_code = status_code
            self.headers = dict(headers or {})
            self.content = content

    class JSONResponse(Response):
        def __init__(self, status_code=400, content=None, headers=None):
            super().__init__(status_code, headers, content)

    class RedirectResponse(Response):
        def __init__(self, url="", status_code=302, headers=None):
            super().__init__(status_code, headers)
            self.url = url

    fastapi = types.ModuleType("fastapi")
    fastapi.Request = Request
    responses = types.ModuleType("fastapi.responses")
    responses.Response = Response
    responses.JSONResponse = JSONResponse
    responses.RedirectResponse = RedirectResponse
    fastapi.responses = responses
    sys.modules["fastapi"] = fastapi
    sys.modules["fastapi.responses"] = responses


_install_fastapi_stub()
ensure_package("app")
ensure_package("app.log")

package = ensure_package("cloudsubscribex")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py",
)
delegation_module = load_module(
    "cloudsubscribex.core.delegation",
    "plugins.v2/cloudsubscribex/core/delegation.py",
)

#: 用真实定义填充 core 包壳，避免触发 core/__init__.py 的 MoviePilot 依赖
core_module = ensure_package("cloudsubscribex.core")
core_module.CloudDriveCapability = cloud_module.CloudDriveCapability
core_module.CloudFile = cloud_module.CloudFile
core_module.OwnerDelegator = delegation_module.OwnerDelegator
core_module.CloudDrivePolicy = cloud_module.CloudDrivePolicy
core_module.CloudDriveProvider = cloud_module.CloudDriveProvider

api_package = ensure_package("cloudsubscribex.core.api")
api_package.__path__ = [
    os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")
]

redirect_module = load_module(
    "cloudsubscribex.core.api.redirect",
    "plugins.v2/cloudsubscribex/core/api/redirect.py",
)
RedirectApi = redirect_module.RedirectApi

PICKCODE = "akoocdeuj01e3hhcu"
DIRECT_URL = "https://cdnfhnfile.115cdn.net/example.mkv?t=1"


class _FakeDownloader:
    def __init__(self, url=DIRECT_URL, error=None):
        self.url = url
        self.error = error
        self.calls = []

    def resolve_download_link(self, file_item):
        self.calls.append(file_item)
        if self.error:
            raise self.error
        return self.url, {}


class _FakeProvider:
    def __init__(self, downloader=None):
        self.downloader = downloader

    def require(self, capability):
        if self.downloader is None:
            raise RuntimeError("capability missing")
        return self.downloader


def _owner(downloader=None, missing=False):
    return SimpleNamespace(
        _cloud_drive=None if missing else _FakeProvider(downloader)
    )


def _request(method="GET"):
    return sys.modules["fastapi"].Request(method=method)


class RedirectApiTest(unittest.TestCase):
    def setUp(self):
        RedirectApi._link_cache.clear()

    def _call(self, owner, pickcode=PICKCODE, file_name="", method="GET"):
        api = RedirectApi(owner)
        return asyncio.run(
            api.api_strm_redirect(_request(method), pickcode=pickcode, file_name=file_name)
        )

    def test_missing_pickcode(self):
        response = self._call(_owner(_FakeDownloader()), pickcode="")
        self.assertEqual(response.status_code, 400)

    def test_invalid_pickcode(self):
        for bad in ("short", "x" * 16, "x" * 18, "akoocdeuj01e3hhc!"):
            with self.subTest(bad=bad):
                response = self._call(_owner(_FakeDownloader()), pickcode=bad)
                self.assertEqual(response.status_code, 400)

    def test_provider_missing_returns_503(self):
        response = self._call(_owner(missing=True))
        self.assertEqual(response.status_code, 503)

    def test_capability_missing_returns_503(self):
        response = self._call(_owner(_FakeDownloader()), )
        self.assertEqual(response.status_code, 302)  # 有 downloader 时正常
        RedirectApi._link_cache.clear()
        response = self._call(SimpleNamespace(_cloud_drive=_FakeProvider(None)))
        self.assertEqual(response.status_code, 503)

    def test_upstream_error_returns_502(self):
        response = self._call(_owner(_FakeDownloader(error=RuntimeError("boom"))))
        self.assertEqual(response.status_code, 502)

    def test_empty_url_returns_502(self):
        response = self._call(_owner(_FakeDownloader(url="")))
        self.assertEqual(response.status_code, 502)

    def test_success_returns_302(self):
        downloader = _FakeDownloader()
        response = self._call(_owner(downloader), file_name="小城良方 - S01E07.mp4")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, DIRECT_URL)
        self.assertEqual(len(downloader.calls), 1)
        self.assertEqual(
            downloader.calls[0].playback_values["pickcode"], PICKCODE
        )

    def test_cache_hit_skips_upstream(self):
        downloader = _FakeDownloader()
        owner = _owner(downloader)
        self._call(owner)
        response = self._call(owner)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(downloader.calls), 1)

    def test_cache_ttl_is_45_minutes(self):
        """直链 TTL 收敛到 45 分钟：115 直链实测 ≤70 分钟就 request expired。"""
        self.assertEqual(redirect_module._CACHE_TTL_SECONDS, 45 * 60)

    def test_expired_cache_entry_is_reminted(self):
        """缓存条目过期后必须重新换链，不能把过期直链交给播放器。"""
        downloader = _FakeDownloader()
        owner = _owner(downloader)
        self._call(owner)
        key = f"{PICKCODE}|"
        url, _ = RedirectApi._link_cache[key]
        RedirectApi._link_cache[key] = (url, 1.0)  # 已过期
        response = self._call(owner)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(downloader.calls), 2)

    def test_head_returns_200(self):
        downloader = _FakeDownloader()
        response = self._call(_owner(downloader), method="HEAD")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Accept-Ranges", response.headers)


if __name__ == "__main__":
    unittest.main()
