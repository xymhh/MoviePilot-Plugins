"""深度删除 webhook 去重验证脚本（手动执行，不进 pytest 收集）。

神医一次删除会连发 4 条 webhook；本脚本验证「同目标只处理一次、只发一条通知」。
因为需要给 app.chain/app.db 等打桩，跑在 pytest 共享进程里会污染其他用例
（实测 29 条无关失败），所以只作为手动验证脚本保留：

    PYTHONPATH=tests python3 tests/manual_deep_delete_webhook_dedup.py
"""

import os
import sys
import unittest
from types import SimpleNamespace

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

from plugin_env import (  # noqa: E402
    OwnerDelegator as StubOwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
)

install_app_mocks(
    "app.chain",
    "app.chain.mediaserver",
    "app.db",
    "app.db.models",
    "app.db.models.mediaserver",
    "app.helper",
    "app.helper.mediaserver",
)
ensure_package("app.chain.mediaserver").MediaServerChain = type("MediaServerChain", (), {})
ensure_package("app.db").SessionFactory = lambda: None
ensure_package("app.db.models.mediaserver").MediaServerItem = type("MediaServerItem", (), {})
ensure_package("app.helper.mediaserver").MediaServerHelper = type("MediaServerHelper", (), {})

# 用独立命名空间加载被测模块，避免污染其他用例共享的 cloudsubscribex.* 包属性
package = ensure_package("ddtest")
package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex")]
core_package = ensure_package("ddtest.core")
core_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core")]
api_package = ensure_package("ddtest.core.api")
api_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "core", "api")]
utils_package = ensure_package("ddtest.utils")
utils_package.__path__ = [os.path.join(plugins_v2_path, "cloudsubscribex", "utils")]

core_package.OwnerDelegator = StubOwnerDelegator
core_package.render_template = lambda template, *args, **kwargs: str(template or "")
core_package.resolve_template = lambda templates, key: (templates or {}).get(key, "")
core_package.media = load_module(
    "ddtest.core.media",
    "plugins.v2/cloudsubscribex/core/media.py",
)
core_package.media_scope = type(sys)("ddtest.core.media_scope")

dedupe_module = load_module(
    "ddtest.utils.deep_delete_dedupe",
    "plugins.v2/cloudsubscribex/utils/deep_delete_dedupe.py",
)
media_library = load_module(
    "ddtest.core.api.media_library",
    "plugins.v2/cloudsubscribex/core/api/media_library.py",
)

TARGET = "/media/strm/115网盘/剧集/国漫/武神主宰 (2020) {tmdb-110181}"


class _Clock:
    def __init__(self):
        self.now = 500.0

    def __call__(self):
        return self.now


class _SyncHandler:
    def __init__(self, matched=1):
        self.calls = []
        self.matched = matched

    def delete_by_media_server_paths(self, paths):
        self.calls.append(list(paths))
        return {
            "matched": self.matched,
            "deleted": 1 if self.matched else 0,
            "cloud_files_deleted": 0,
            "cloud_directories_deleted": 1 if self.matched else 0,
            "strm_deleted": 0,
            "skipped": 0,
        }


def _event():
    return {
        "channel": "emby",
        "event": "deep.delete",
        "item_path": TARGET,
        "json_object": {"Event": "deep.delete", "Item": {"Path": TARGET}},
    }


class DeepDeleteWebhookDedupTest(unittest.TestCase):
    def _build_api(self, matched=1):
        api = object.__new__(media_library.MediaLibraryApi)
        self.clock = _Clock()
        self.sync_handler = _SyncHandler(matched=matched)
        self.messages = []
        object.__setattr__(api, "_owner", SimpleNamespace())
        object.__setattr__(api, "_platform_deep_delete_enabled", True)
        object.__setattr__(api, "_sync_handler", self.sync_handler)
        object.__setattr__(api, "_notify", True)
        object.__setattr__(api, "_notify_templates", {})
        object.__setattr__(api, "_notification_type", "Plugin")
        object.__setattr__(api, "_deep_delete_dedup", dedupe_module.DeepDeleteDeduplicator(120, self.clock))
        object.__setattr__(api, "post_message", lambda **kwargs: self.messages.append(kwargs))
        return api

    def test_four_identical_events_handle_and_notify_once(self):
        api = self._build_api()
        for _ in range(4):
            api.handle_platform_media_webhook(_event())

        self.assertEqual(len(self.sync_handler.calls), 1)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("深度联动删除完成", self.messages[0]["title"])

    def test_retry_allowed_after_window(self):
        api = self._build_api()
        api.handle_platform_media_webhook(_event())
        self.clock.now += 121
        api.handle_platform_media_webhook(_event())

        self.assertEqual(len(self.sync_handler.calls), 2)
        self.assertEqual(len(self.messages), 2)

    def test_unmatched_event_releases_claim_for_retry(self):
        api = self._build_api(matched=0)
        api.handle_platform_media_webhook(_event())
        api.handle_platform_media_webhook(_event())

        self.assertEqual(len(self.sync_handler.calls), 2)
        self.assertEqual(len(self.messages), 0)


if __name__ == "__main__":
    unittest.main()
