"""手动转存（一键转存）媒体身份兜底测试。

场景：新片/中文片名未被 TMDB 收录时，前端反查不到 tmdb_id，后端此前直接返回
「请选择订阅或有效的 TMDB 媒体」，导致「豆瓣能认、TMDB 认不出」的片完全无法转存。

本测试固定新行为：
1. 带豆瓣身份（或明确媒体上下文）时，后端用 ``recognize_media`` 兜底识别并继续；
2. 识别结果自带类型时以真实类型为准（前端无媒体上下文时默认传 tv，电影会被纠正）；
3. 完全没有任何可用身份时，仍然返回原来的错误提示。
"""

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_mock,
    register_module,
    set_media_type,
)

# ---- app.* 占位依赖 ----
register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.config", global_vars=MagicMock(), settings=MagicMock())
register_mock("app.core.metainfo")
register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.db.subscribe_oper")
register_module("app.log", logger=MagicMock())
register_module("app.schemas", package=True)
register_module("app.utils", package=True)
register_mock("app.utils.string")

MEDIA_TYPE = set_media_type()


class _FakeCache(dict):
    def __init__(self, *args, **kwargs):
        super().__init__()


class _FakeShareService:
    def extract_share_info(self, link):
        return SimpleNamespace(share_key="abcdefg", title="巴黎绽放的星辰 2026 1080p")


_capture_owner = None


def _fake_tmdb_id_of(value):
    candidate = getattr(value, "tmdb_id", None)
    try:
        parsed = int(candidate or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _fake_media_identity(value):
    return (
        getattr(value, "media_source", None),
        getattr(value, "media_id", None),
    )


# ---- cloudsubscribe.* 占位依赖 ----
register_module("cloudsubscribe", package=True)
register_module(
    "cloudsubscribe.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    SearchCapability=MagicMock(),
    CloudDriveCapability=MagicMock(),
)
register_module("cloudsubscribe.core.cloud", CloudDriveCapability=MagicMock())
register_module(
    "cloudsubscribe.core.media",
    recognize_media=MagicMock(),
    tmdb_id_of=_fake_tmdb_id_of,
    media_identity=_fake_media_identity,
)
register_module("cloudsubscribe.search.matching", positive_ints=MagicMock())
register_module(
    "cloudsubscribe.search.types",
    normalize_resource_type=MagicMock(side_effect=lambda value, *a, **k: value),
    resource_type_from_text=MagicMock(side_effect=lambda value, *a, **k: "quark"),
    resource_type_from_url=MagicMock(side_effect=lambda value, *a, **k: "quark"),
    resource_type_name=MagicMock(side_effect=lambda value, *a, **k: str(value)),
)
register_module("cloudsubscribe.utils.cache", create_platform_ttl_cache=_FakeCache)

sync_module = load_module(
    "cloudsubscribe.core.api.sync",
    "plugins.v2/cloudsubscribe/core/api/sync.py",
)


class TestManualSyncMediaIdentity(unittest.TestCase):
    def setUp(self):
        sync_module._RECENT_MANUAL_SUBMITS.clear()
        global _capture_owner
        self.submitted = []
        _capture_owner = self
        self.movie = SimpleNamespace(
            title="巴黎绽放的星辰",
            year="2026",
            type=MEDIA_TYPE.MOVIE,
            tmdb_id=0,
            media_source="douban",
            media_id="37473691",
            seasons={},
            number_of_seasons=0,
        )
        sync_module.recognize_media = MagicMock(return_value=self.movie)
        # OwnerDelegator 会把属性读写转发给 owner，所以打桩要落在类或 owner 上
        self.owner = SimpleNamespace(
            chain=MagicMock(),
            _search_handler=MagicMock(),
            _cloud_drive=SimpleNamespace(
                key="115", supports=MagicMock(return_value=True), require=MagicMock(),
            ),
            supports=MagicMock(return_value=True),
            _resource_type_order=["magnet", "ed2k", "quark", "115"],
        )
        self.api = sync_module.SyncApi(self.owner)
        sync_module.SyncApi._manual_resource_type = staticmethod(
            lambda link, default="115": "quark"
        )
        sync_module.SyncApi._manual_share_service = staticmethod(
            lambda resource_type: _FakeShareService()
        )
        sync_module.SyncApi._manual_share_info_valid = staticmethod(
            lambda resource_type, share_info: True
        )
        sync_module.SyncApi._submit_sync_operation = self._capture

    def _capture(api_self, kwargs, label):
        _capture_owner.submitted.append((kwargs, label))
        return SimpleNamespace(result=lambda: None)

    def _payload(self, media):
        return {
            "media": media,
            "resources": [
                {"url": "https://pan.quark.cn/s/abcdefg", "title": "巴黎绽放的星辰 2026 1080p"}
            ],
        }

    def test_douban_identity_is_accepted(self):
        """没有 TMDB ID 但有豆瓣身份 → 兜底识别后继续转存。"""
        result = self.api.api_vue_start_manual_sync(
            self._payload({
                "tmdb_id": None,
                "media_type": "movie",
                "title": "巴黎绽放的星辰",
                "year": "2026",
                "douban_id": "37473691",
                "seek_by_title": True,
            })
        )
        self.assertTrue(result["success"], result)
        self.assertEqual(len(self.submitted), 1, "应当提交一条转存任务")
        kwargs, _label = self.submitted[0]
        target = kwargs["manual_target"]
        self.assertIsNone(target["tmdb_id"], "没有 TMDB ID 时应留空而不是报错")
        self.assertEqual(target["media_source"], "douban")
        self.assertEqual(target["media_id"], "37473691")
        self.assertEqual(target["title"], "巴黎绽放的星辰")
        self.assertEqual(target["media_type"], "movie")

    def test_recognized_type_overrides_wrong_frontend_type(self):
        """前端默认按电视剧传、实际是电影时，以识别结果类型为准。"""
        result = self.api.api_vue_start_manual_sync(
            self._payload({
                "tmdb_id": None,
                "media_type": "tv",
                "title": "巴黎绽放的星辰",
                "year": "2026",
                "douban_id": "37473691",
            })
        )
        self.assertTrue(result["success"], result)
        kwargs, _label = self.submitted[0]
        self.assertEqual(kwargs["manual_target"]["media_type"], "movie")

    def test_prefixed_media_id_is_normalized(self):
        """前端若回传「douban:37473691」这类带来源前缀的 media_id，要拆成规范身份再识别。"""
        result = self.api.api_vue_start_manual_sync(
            self._payload({
                "tmdb_id": None,
                "media_type": "movie",
                "title": "巴黎绽放的星辰",
                "year": "2026",
                "media_source": "douban",
                "media_id": "douban:37473691",
                "seek_by_title": True,
            })
        )
        self.assertTrue(result["success"], result)
        kwargs = sync_module.recognize_media.call_args.kwargs
        self.assertEqual(kwargs["media_id"], "37473691")
        self.assertEqual(kwargs["media_source"], "douban")

    def test_without_any_identity_still_rejected(self):
        """没有任何可用身份时保持原报错，不猜测媒体。"""
        sync_module.recognize_media = MagicMock(return_value=None)
        result = self.api.api_vue_start_manual_sync(
            self._payload({
                "tmdb_id": None,
                "media_type": "tv",
                "title": "[ANi] 某番 - 01 [1080P]",
                "year": "",
            })
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "请选择订阅或有效的 TMDB 媒体")
        self.assertEqual(self.submitted, [], "不应提交转存任务")


if __name__ == "__main__":
    unittest.main()
