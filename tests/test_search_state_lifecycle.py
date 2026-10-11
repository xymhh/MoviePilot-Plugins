"""搜索阶段中间态生命周期与状态机重置测试。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import OwnerDelegator, load_module, register_mock, register_module

# 构造 mock 的 app 包结构
register_module("app", package=True)
app_core = register_module("app.core", package=True)
app_core.config = register_mock(
    "app.core.config", global_vars=MagicMock(), settings=MagicMock()
)
app_core.context = register_mock("app.core.context")
app_core.metainfo = register_mock("app.core.metainfo")

register_module("app.db", package=True, SessionFactory=MagicMock())
register_mock("app.db.subscribe_oper")

register_module("app.log", logger=MagicMock())

register_module("app.schemas", package=True)
register_mock("app.schemas.types")

register_module("app.utils", package=True)
register_mock("app.utils.http")

# 构造 mock 的 cloudsubscribe 包结构
register_module("cloudsubscribe", package=True)
cloudsubscribe_core = register_module(
    "cloudsubscribe.core",
    package=True,
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
)
register_module(
    "cloudsubscribe.core.media",
    apply_media_identity=MagicMock(),
    legacy_media_ids=MagicMock(),
    media_identity=MagicMock(),
    recognize_media=MagicMock(),
    search_medias=MagicMock(),
    tmdb_id_of=MagicMock(),
    tmdb_identity_update=MagicMock(),
)

register_module("cloudsubscribe.utils", package=True)
register_mock(
    "cloudsubscribe.utils.cache", normalize_platform_cache_key=MagicMock()
)

# 动态加载 metadata.py
SyncMetadataService = load_module(
    "cloudsubscribe.handlers.sync.metadata",
    "plugins.v2/cloudsubscribe/handlers/sync/metadata.py",
).SyncMetadataService

# 动态加载 runtime.py
SyncRuntimeService = load_module(
    "cloudsubscribe.core.services.runtime",
    "plugins.v2/cloudsubscribe/core/services/runtime.py",
).SyncRuntimeService


class TestSearchStateLifecycle(unittest.TestCase):
    """测试搜索渠道状态的生命周期重置契约。"""

    def test_idle_search_state_structure(self):
        """验证搜索闲置状态的标准数据结构。"""
        state = SyncMetadataService._idle_search_state()
        self.assertEqual(state, {
            "search_active": False,
            "search_channels": [],
            "search_total_results": 0,
        })
        self.assertEqual(SyncRuntimeService._idle_search_state(), state)

    def test_set_task_phase_no_text_magic_sniffing(self):
        """验证 _set_task_phase 不再根据包含'转存'或'后处理'等文案偷偷清空搜索态。"""
        task_update_mock = MagicMock()
        service = SyncMetadataService(owner=MagicMock(_task_update=task_update_mock))

        subscribe = MagicMock()
        subscribe.id = 123
        subscribe._transient_target = False

        # 阶段名包含"转存"，但 clear_search 为 False（默认）
        service._set_task_phase(subscribe, "正在转存电影文件", 80)
        task_update_mock.assert_called_once_with(
            "subscribe:123",
            phase="正在转存电影文件",
            progress=80,
        )
        self.assertNotIn("search_active", task_update_mock.call_args[1])
        self.assertNotIn("search_channels", task_update_mock.call_args[1])

    def test_set_task_phase_explicit_clear_search(self):
        """验证 _set_task_phase 在 clear_search=True 时显式重置搜索态。"""
        task_update_mock = MagicMock()
        service = SyncMetadataService(owner=MagicMock(_task_update=task_update_mock))

        subscribe = MagicMock()
        subscribe.id = 123
        subscribe._transient_target = False

        service._set_task_phase(subscribe, "转存匹配文件", 90, clear_search=True)
        task_update_mock.assert_called_once_with(
            "subscribe:123",
            phase="转存匹配文件",
            progress=90,
            search_active=False,
            search_channels=[],
            search_total_results=0,
        )

    def test_clear_task_search_state(self):
        """验证 _clear_task_search_state 显式调用。"""
        task_update_mock = MagicMock()
        service = SyncMetadataService(owner=MagicMock(_task_update=task_update_mock))

        subscribe = MagicMock()
        subscribe.id = 456
        subscribe._transient_target = False

        service._clear_task_search_state(subscribe)
        task_update_mock.assert_called_once_with(
            "subscribe:456",
            search_active=False,
            search_channels=[],
            search_total_results=0,
        )

    def test_runtime_service_status_transition_safeguard(self):
        """验证 runtime 服务在状态进入 transferring/downloading/postprocessing 时自动兜底收敛搜索态。"""
        runtime = SyncRuntimeService(owner=MagicMock())
        task_id = "subscribe:789"
        runtime._sync_tasks = {
            task_id: {
                "id": task_id,
                "status": "running",
                "phase": "搜索候选资源",
                "search_active": True,
                "search_channels": [{"key": "quark", "name": "夸克网盘", "count": 2}],
                "search_total_results": 2,
            }
        }

        # 状态切换为 transferring，应触发状态机兜底清理
        runtime._update_sync_task(task_id, status="transferring", phase="转存中")
        updated_task = runtime._sync_tasks[task_id]

        self.assertEqual(updated_task["status"], "transferring")
        self.assertFalse(updated_task["search_active"])
        self.assertEqual(updated_task["search_channels"], [])
        self.assertEqual(updated_task["search_total_results"], 0)

    def test_create_search_registry_simplifies_skipped_logs(self):
        """验证搜索渠道注册时，未配置的多个渠道日志聚合为单条输出，避免刷屏。"""
        from app.log import logger as mock_logger
        load_module("cloudsubscribe.core.search", "plugins.v2/cloudsubscribe/core/search.py")
        load_module("cloudsubscribe.core.definitions", "plugins.v2/cloudsubscribe/core/definitions.py")
        load_module("cloudsubscribe.search.types", "plugins.v2/cloudsubscribe/search/types.py")
        scanner_mod = load_module("cloudsubscribe.search.scanner", "plugins.v2/cloudsubscribe/search/scanner.py")
        registry_mod = load_module("cloudsubscribe.search.registry", "plugins.v2/cloudsubscribe/search/registry.py")

        class _DummySource1:
            id = "dummy1"

            @classmethod
            def create_client(cls, cfg, ctx): return None

            @classmethod
            def create_service(cls, cli, cfg, ctx): return None

            @classmethod
            def create_provider(cls, svc, cli, cfg, ctx): return None

        class _DummySource2:
            id = "dummy2"

            @classmethod
            def create_client(cls, cfg, ctx): return None

            @classmethod
            def create_service(cls, cli, cfg, ctx): return None

            @classmethod
            def create_provider(cls, svc, cli, cfg, ctx): return None

        scanner_mod.SearchSourceRegistry.register(_DummySource1)
        scanner_mod.SearchSourceRegistry.register(_DummySource2)

        mock_logger.reset_mock()
        registry_mod.create_search_registry({})

        # 检查是否聚合成单条 debug 日志包含 dummy1 和 dummy2
        debug_messages = [call.args[0] for call in mock_logger.debug.call_args_list if call.args]
        skipped_lines = [msg for msg in debug_messages if "未配置搜索渠道已跳过" in msg]
        self.assertEqual(len(skipped_lines), 1)
        self.assertIn("dummy1", skipped_lines[0])
        self.assertIn("dummy2", skipped_lines[0])

if __name__ == "__main__":
    unittest.main()
