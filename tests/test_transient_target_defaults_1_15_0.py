"""v1.15.0 P0 回归：_transient_target_defaults 缺 @staticmethod。

2026-11-18 P3 磁力实测线上复现两次：无订阅媒体的手动提交在同步开始
0.4–0.6 秒即失败，``MediaTargetsService._transient_target_defaults()
takes 0 positional arguments but 1 was given``——1.13.3 从 history.py
拆出 media_targets.py 时丢了 @staticmethod，方法体又没写 self，
``self._transient_target_defaults()`` 调用时把 self 当成第 1 个位置参数。
修法：补 @staticmethod（方法不依赖实例状态；调用点与兼容性测试的空括号
签名正则都不用改）。
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
)

set_media_type(movie="电影", tv="电视剧")
register_module("app.schemas", MediaInfo=SimpleNamespace)

install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
)

_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core").CloudDriveCapability = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(
    return_value=("themoviedb", "123")
)
_ensure("cloudsubscribex.core.media").download_history_identity_payload = MagicMock(
    return_value={}
)
_ensure("cloudsubscribex.core.media").media_server_tmdb_filters = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.history").history_group_key = MagicMock(
    return_value=""
)
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"

# 别重载已加载的模块（会分叉模块对象）；只在未加载时加载。
_media_targets_module = sys.modules.get(
    "cloudsubscribex.handlers.sync.media_targets"
)
if _media_targets_module is None:
    _media_targets_module = load_module(
        "cloudsubscribex.handlers.sync.media_targets",
        "plugins.v2/cloudsubscribex/handlers/sync/media_targets.py",
    )
MediaTargetsService = _media_targets_module.MediaTargetsService


class TransientTargetDefaultsTests(unittest.TestCase):
    """无订阅电影调 build_transient_media_target 不得抛 TypeError。"""

    def setUp(self):
        self.handler = MediaTargetsService(SimpleNamespace())

    def test_defaults_is_staticmethod(self):
        """_transient_target_defaults 不依赖实例：类上直接调用应返回默认字段。"""
        defaults = MediaTargetsService._transient_target_defaults()
        self.assertIsInstance(defaults, dict)
        self.assertEqual(defaults["id"], 0)
        self.assertEqual(defaults["state"], "N")
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertIn(req, defaults)

    def test_movie_without_subscribe_returns_target(self):
        """无订阅电影（P3 磁力实测的《泥面人》场景）应返回临时目标，不抛错。"""
        target = self.handler.build_transient_media_target({
            "media_type": "movie",
            "title": "泥面人",
            "tmdb_id": 1400940,
            "year": 2026,
        })
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "泥面人")
        self.assertEqual(target.tmdbid, 1400940)
        self.assertEqual(target.type, "电影")
        self.assertTrue(getattr(target, "_transient_target", False))
        # 临时目标必须带 v3 订阅契约字段（get_params 属性访问不得 AttributeError）
        for req in ("quality", "resolution", "effect", "include", "exclude"):
            self.assertTrue(hasattr(target, req), f"缺字段 {req}")


if __name__ == "__main__":
    unittest.main()
