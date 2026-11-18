"""后处理任务完整测试套件（覆盖 DirectoryFileIndex、快照同步、文件定位、重命名与移动状态机、重试退避及关闭自动整理契约）。"""

import sys
import types
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_mock,
    register_package,
    set_media_type,
)

# 构造 mock 的基础依赖
install_app_mocks(
    "app.db",
    "app.db.models",
    "app.db.models.subscribe",
    "app.db.subscribe_oper",
)

# 构造 cloudsubscribex 包结构
register_mock("cloudsubscribex")
register_package(
    "cloudsubscribex.core",
    OwnerDelegator=OwnerDelegator,
    CloudDriveCapability=MagicMock(),
)
register_mock("cloudsubscribex.search", subs_filter=MagicMock())
register_mock("cloudsubscribex.search.subs_filter")

# 加载 file_parser
utils_file_parser = load_module(
    "cloudsubscribex.utils.file_parser",
    "plugins.v2/cloudsubscribex/utils/file_parser.py",
)
register_package(
    "cloudsubscribex.utils",
    MediaFileParser=utils_file_parser.MediaFileParser,
)

register_package("cloudsubscribex.handlers")
register_package("cloudsubscribex.handlers.sync")
load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)

# 终态写重试索引用真实 core.retry_index（行构造逻辑本身就是被测对象）。
load_module(
    "cloudsubscribex.core.retry_index",
    "plugins.v2/cloudsubscribex/core/retry_index.py",
)

_postprocess = load_module(
    "cloudsubscribex.handlers.sync.postprocess",
    "plugins.v2/cloudsubscribex/handlers/sync/postprocess.py",
)
DirectoryFileIndex = _postprocess.DirectoryFileIndex
PostprocessBatchContext = _postprocess.PostprocessBatchContext
PostprocessService = _postprocess.PostprocessService


class TestDirectoryFileIndex(unittest.TestCase):
    """测试 DirectoryFileIndex 目录索引与多算法哈希/季集反查功能。"""

    def test_basic_name_and_hash_lookup(self):
        file1 = types.SimpleNamespace(name="Blossoms.S01E01.mkv", sha1="A1B2C3D4", md5="MD5HASH1")
        file2 = {"name": "Blossoms.S01E02.mkv", "sha1": "E5F6A7B8", "md5": "MD5HASH2"}
        index = DirectoryFileIndex([file1, file2])

        # 文件名查找
        self.assertIn("Blossoms.S01E01.mkv", index)
        self.assertIn("Blossoms.S01E02.mkv", index)
        self.assertEqual(index["Blossoms.S01E01.mkv"], file1)

        # 哈希大小写不敏感反查
        self.assertEqual(index.get_by_hash("a1b2c3d4"), file1)
        self.assertEqual(index.get_by_sha1("A1B2C3D4"), file1)
        self.assertEqual(index.get_by_hash("MD5HASH2"), file2)

        # 季集提取与反查
        self.assertEqual(index.get_by_season_episode(1, 1), file1)
        self.assertEqual(index.get_by_season_episode(1, 2), file2)
        self.assertIsNone(index.get_by_season_episode(1, 3))

    def test_add_and_remove_file(self):
        index = DirectoryFileIndex()
        file_obj = types.SimpleNamespace(name="Movie.2024.S02E05.mp4", sha1="TESTSHA1")
        index.add_file(file_obj)

        self.assertIn("Movie.2024.S02E05.mp4", index)
        self.assertEqual(index.get_by_sha1("TESTSHA1"), file_obj)
        self.assertEqual(index.get_by_season_episode(2, 5), file_obj)

        # 删除后，名称、哈希与季集索引均应清除
        index.remove_file(file_obj)
        self.assertNotIn("Movie.2024.S02E05.mp4", index)
        self.assertIsNone(index.get_by_sha1("TESTSHA1"))
        self.assertIsNone(index.get_by_season_episode(2, 5))


class TestPostprocessBatchContext(unittest.TestCase):
    """测试 PostprocessBatchContext 目录快照状态机与变更同步。"""

    def setUp(self):
        self.ctx = PostprocessBatchContext(
            pending={},
            pending_snapshot={},
            due_keys=["key1"],
            now=1000.0,
            monitor_token="token_123",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        self.file1 = types.SimpleNamespace(name="old_name.mkv", sha1="HASH123")
        self.index_src = DirectoryFileIndex([self.file1])
        self.index_dst = DirectoryFileIndex([])
        self.ctx.directory_snapshots["/src"] = (True, self.index_src)
        self.ctx.directory_snapshots["/dst"] = (True, self.index_dst)

    def test_sync_rename(self):
        new_file = types.SimpleNamespace(name="new_name.mkv", sha1="HASH123")
        self.ctx.sync_rename("/src", self.file1, new_file)

        # 检查快照索引
        self.assertNotIn("old_name.mkv", self.index_src)
        self.assertIn("new_name.mkv", self.index_src)
        self.assertEqual(self.index_src.get_by_sha1("HASH123"), new_file)

    def test_sync_move(self):
        self.ctx.sync_move("/src", "/dst", self.file1)

        # 源目录应移除，目标目录应新增
        self.assertNotIn("old_name.mkv", self.index_src)
        self.assertIn("old_name.mkv", self.index_dst)
        self.assertEqual(self.index_dst.get_by_sha1("HASH123"), self.file1)


class DummyPostprocessService(PostprocessService):
    """用于测试 PostprocessService 各独立逻辑的派生类。"""

    def __init__(self, organize_after_transfer: bool = True):
        owner = types.SimpleNamespace()
        super().__init__(owner)
        self._organize_after_transfer = organize_after_transfer
        self._upgrade_mode = "replace"
        self._FINALIZE_MAX_FAILURES = 3
        self._FINALIZE_MAX_FAILURES_LOCAL = 2
        self._FINALIZE_LOCATE_TIMEOUT_SECONDS = 120.0
        self._OFFLINE_CHECK_DELAYS = [60, 120, 300, 600]
        self._OFFLINE_TIMEOUT = 3600
        self._cloud_batch_mutations = MagicMock()
        self._offline_pending_lock = MagicMock()
        self._offline_pending_lock.__enter__ = MagicMock(return_value=None)
        self._offline_pending_lock.__exit__ = MagicMock(return_value=None)
        self._get_data_store = {}
        self._cloud_drive = MagicMock()
        self._postprocess_task_update = False
        self._FINALIZE_DEAD_LOG = "文件后处理失败超出重试限制：{file_name}"

    def _notify_offline_pending_changed(self, count):
        pass

    def _supported_hash_algorithms(self):
        return frozenset({"sha1", "md5"})

    def _cloud_directory_snapshot(self, path, cache=None):
        if cache and path in cache:
            return cache[path]
        return True, DirectoryFileIndex()

    def _get_data(self, key):
        return self._get_data_store.get(key, {})

    def _save_offline_pending(self, data):
        self._get_data_store[self._OFFLINE_PENDING_KEY] = data


class TestPostprocessServiceHelpers(unittest.TestCase):
    """测试任务 ID、洗版备份命名、到期判断等辅助函数。"""

    def test_postprocess_task_id(self):
        # 具有 subscribe_id
        self.assertEqual(
            PostprocessService._postprocess_task_id({"subscribe_id": 123}),
            "subscribe:123"
        )
        # 具有 sub_key
        self.assertEqual(
            PostprocessService._postprocess_task_id({"sub_key": "sub_xyz"}),
            "media:sub_xyz"
        )
        # 均无
        self.assertEqual(
            PostprocessService._postprocess_task_id({}),
            ""
        )

    def test_upgrade_backup_name(self):
        backup_name = PostprocessService._upgrade_backup_name("Movie.Name.2024.1080p.mkv", "task-abc123xyz")
        self.assertTrue(backup_name.startswith("Movie.Name.2024.1080p-"))
        self.assertTrue(backup_name.endswith(".mkv"))
        self.assertIn("taskabc123", backup_name)

    @staticmethod
    def _dedupe_service(files):
        class StaticSnapshotService(DummyPostprocessService):
            def _cloud_directory_snapshot(self, path, cache=None):
                return True, DirectoryFileIndex(files)
        return StaticSnapshotService()

    def test_dedupe_moved_duplicate_deletes_same_content_copy(self):
        base = types.SimpleNamespace(
            id="base", name="Show - S01E17 - 第17集.mp4",
            size=4036123377, sha1="SAME",
        )
        moved = types.SimpleNamespace(
            id="dup", name="Show - S01E17 - 第17集 (1).mp4",
            size=4036123377, sha1="SAME",
        )
        service = self._dedupe_service([base])
        service._cloud_mutations = MagicMock()
        service._cloud_mutations.delete_file = MagicMock(return_value=True)

        result = service._dedupe_moved_duplicate(
            "/media/tv", "Show - S01E17 - 第17集.mp4", moved
        )

        self.assertIs(result, base)
        service._cloud_mutations.delete_file.assert_called_once_with("dup")

    def test_dedupe_moved_duplicate_keeps_different_content(self):
        base = types.SimpleNamespace(
            id="base", name="Show - S01E17 - 第17集.mp4",
            size=1000, sha1="OLD",
        )
        moved = types.SimpleNamespace(
            id="dup", name="Show - S01E17 - 第17集 (1).mp4",
            size=2000, sha1="NEW",
        )
        service = self._dedupe_service([base])
        service._cloud_mutations = MagicMock()
        service._cloud_mutations.delete_file = MagicMock(return_value=True)

        result = service._dedupe_moved_duplicate(
            "/media/tv", "Show - S01E17 - 第17集.mp4", moved
        )

        self.assertIs(result, moved)
        service._cloud_mutations.delete_file.assert_not_called()

    def test_due_pending_keys(self):
        now = 1000.0
        # v1.13.3：_history_ready_for_finalize 门已删除，这里只验证到期逻辑。
        pending = {
            "due_item": {"next_check_at": 900.0, "_monitor_until": 950.0, "skip_history": True},
            "future_item": {"next_check_at": 1100.0, "_monitor_until": 950.0, "skip_history": True},
            "monitored_item": {"next_check_at": 900.0, "_monitor_until": 1200.0, "skip_history": True},
        }
        # _due_pending_keys 已从静态方法改为实例方法（新增 pending_keys 参数），
        # 真实实现见 plugins.v2/cloudsubscribex/handlers/sync/postprocess.py:306。
        service = DummyPostprocessService()
        due = service._due_pending_keys(pending, now)
        self.assertEqual(due, ["due_item"])

        # 强制到期测试
        forced_due = service._due_pending_keys(pending, now, force=True)
        self.assertIn("due_item", forced_due)
        self.assertIn("future_item", forced_due)
        self.assertNotIn("monitored_item", forced_due)

        # 显式指定 pending_keys 时不再受 _monitor_until 限制
        self.assertEqual(
            service._due_pending_keys(pending, now, pending_keys={"monitored_item"}),
            ["monitored_item"],
        )

    def test_media_context_key(self):
        sub_item = {"subscribe_id": 42}
        self.assertEqual(PostprocessService._media_context_key(sub_item), ("subscribe", 42))

        key_item = {"sub_key": "my_sub_key"}
        self.assertEqual(PostprocessService._media_context_key(key_item), ("sub_key", "my_sub_key"))

        media_item = {
            "mediainfo": {"tmdb_id": 999, "type": "tv"},
            "season": 2,
        }
        self.assertEqual(PostprocessService._media_context_key(media_item), ("media", "tv", "999", 2))


class TestRetryAndLocateMechanism(unittest.TestCase):
    """测试重试退避调度、死任务判断与文件定位算法。"""

    def setUp(self):
        self.service = DummyPostprocessService()

    def test_schedule_finalize_retry(self):
        item = {"file_name": "Test.mkv", "check_index": 0}
        now = 1000.0
        self.service._schedule_finalize_retry(item, now)
        self.assertEqual(item["check_index"], 1)
        self.assertEqual(item["next_check_at"], 1000.0 + self.service._OFFLINE_CHECK_DELAYS[1])

    def test_finalize_failure_and_dead_letter(self):
        item = {"file_name": "DeadTask.mkv", "fail_count": 0, "task_type": "share"}
        # 阈值为 2
        is_dead = self.service._finalize_failure(item, "pending_key")
        self.assertFalse(is_dead)
        self.assertEqual(item["fail_count"], 1)

        is_dead = self.service._finalize_failure(item, "pending_key")
        self.assertTrue(is_dead)
        self.assertEqual(item["fail_count"], 2)
        self.assertTrue(item.get("finalize_dead"))

    def test_locate_cloud_file_strategies(self):
        target_file = types.SimpleNamespace(name="Episode.S01E03.mkv", sha1="MATCH_SHA1")
        file_index = DirectoryFileIndex([target_file])
        ctx = PostprocessBatchContext(
            pending={}, pending_snapshot={}, due_keys=[], now=0.0,
            monitor_token="", task_map={}, tasks_valid=True, subscribe_cache={}
        )
        ctx.directory_snapshots["/staging"] = (True, file_index)

        # 策略 1: 精确文件名匹配
        found1 = self.service._locate_cloud_file("/staging", ["Episode.S01E03.mkv"], "", ctx)
        self.assertEqual(found1, target_file)

        # 策略 2: 基于 SHA1 哈希反查匹配
        found2 = self.service._locate_cloud_file("/staging", ["wrong_name.mkv"], "MATCH_SHA1", ctx)
        self.assertEqual(found2, target_file)

        # 策略 3: 基于前缀/后缀模糊匹配
        found3 = self.service._locate_cloud_file("/staging", ["Episode.S01E03.1080p.mkv"], "", ctx)
        self.assertEqual(found3, target_file)


class TestBatchMutationsAndOrganizeContract(unittest.TestCase):
    """测试批量重命名与移动状态机，重点验证开启与关闭自动整理（Issue #8）的行为契约。"""

    def test_execute_batch_cloud_mutations_when_organize_enabled(self):
        """当开启自动整理时，待整理文件应被纳入批量重命名与批量移动队列。"""
        service = DummyPostprocessService(organize_after_transfer=True)
        source_file = types.SimpleNamespace(name="Source.EP01.mp4", sha1="FILEHASH")
        index = DirectoryFileIndex([source_file])

        ctx = PostprocessBatchContext(
            pending={
                "task_1": {
                    "pending_key": "task_1",
                    "file_name": "Target.S01E01.mp4",
                    "staging_name": "Source.EP01.mp4",
                    "staging_dir": "/staging",
                    "cloud_dir": "/media/tv",
                    "task_type": "share",
                }
            },
            pending_snapshot={},
            due_keys=["task_1"],
            now=1000.0,
            monitor_token="tok",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        ctx.directory_snapshots["/staging"] = (True, index)

        renamed_target = types.SimpleNamespace(name="Target.S01E01.mp4", sha1="FILEHASH")
        service._cloud_batch_mutations.rename_files.return_value = {"task_1": renamed_target}

        service._execute_batch_cloud_mutations(ctx)

        # 应调用批量重命名
        service._cloud_batch_mutations.rename_files.assert_called_once()
        # 且在移动组中存在目标目录 /media/tv
        self.assertEqual(ctx.pending["task_1"]["staging_name"], "Target.S01E01.mp4")

    def test_execute_batch_cloud_mutations_when_organize_disabled(self):
        """当关闭自动整理时（Issue #8），文件直接加入 prepared_files 与 moved_files，跳过重命名与跨目录移动。"""
        service = DummyPostprocessService(organize_after_transfer=False)
        source_file = types.SimpleNamespace(name="Source.EP01.mp4", sha1="FILEHASH")
        index = DirectoryFileIndex([source_file])

        ctx = PostprocessBatchContext(
            pending={
                "task_1": {
                    "pending_key": "task_1",
                    "file_name": "Source.EP01.mp4",
                    "staging_name": "Source.EP01.mp4",
                    "staging_dir": "/staging/繁花 (2023)",
                    "cloud_dir": "/media/tv",
                    "task_type": "share",
                }
            },
            pending_snapshot={},
            due_keys=["task_1"],
            now=1000.0,
            monitor_token="tok",
            task_map={},
            tasks_valid=True,
            subscribe_cache={},
        )
        ctx.directory_snapshots["/staging/繁花 (2023)"] = (True, index)

        service._execute_batch_cloud_mutations(ctx)

        # 绝不调用网盘批量重命名
        service._cloud_batch_mutations.rename_files.assert_not_called()

        # 文件应直接完成就绪与原位落盘
        self.assertEqual(ctx.prepared_files["task_1"], source_file)
        self.assertEqual(ctx.moved_files["task_1"], source_file)
        self.assertEqual(ctx.pending["task_1"]["cloud_dir"], "/staging/繁花 (2023)")
        self.assertEqual(ctx.pending["task_1"]["file_name"], "Source.EP01.mp4")
        self.assertIsNotNone(ctx.pending["task_1"].get("moved_at"))


class TestReconcilePendingState(unittest.TestCase):
    """测试状态调和持久化逻辑。"""
    def test_reconcile_removes_completed_items(self):
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = "offline_pending"

        # 初始持久化存储中包含 task_completed 和 task_still_pending
        service._get_data_store["offline_pending"] = {
            "task_completed": {
                "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share",
                "_monitor_token": "token_A"
            },
            "task_still_pending": {
                "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                "_monitor_token": "token_A", "fail_count": 0
            }
        }

        ctx = PostprocessBatchContext(
            pending={
                # task_completed 已经处理完成，从 ctx.pending 中移除（不在 pending 字典里）
                "task_still_pending": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                    "fail_count": 1
                }
            },
            pending_snapshot={
                "task_completed": {
                    "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share"
                },
                "task_still_pending": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share"
                }
            },
            due_keys=["task_completed", "task_still_pending"],
            now=1000.0,
            monitor_token="token_A",
            task_map={},
            tasks_valid=True,
            subscribe_cache={}
        )

        result = service._reconcile_pending_state(ctx)

        saved = service._get_data("offline_pending")
        # task_completed 应已从持久化中移除
        self.assertNotIn("task_completed", saved)
        # task_still_pending 应保留且 fail_count 已更新为 1
        self.assertIn("task_still_pending", saved)
        self.assertEqual(saved["task_still_pending"]["fail_count"], 1)
        self.assertEqual(result["pending"], 1)

    def test_reconcile_never_touches_retry_index(self):
        """Phase 4 第 1 项：调和只同步 pending 存储的字段差分，绝不碰
        重试索引——终态写索引只允许 finalize / persist_sync_results /
        重试执行三处。这里即使有失败项（fail_count 3）与已终态出队项，
        索引也不应多一行、不少一行。"""
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = "offline_pending"
        store = _FakeDataStore()
        # _FakeRetryIndex 只记 upsert；删行也记下来，防止静默删行。
        deletes = []
        store.retry_index.delete_by_record_ids = (
            lambda record_ids: deletes.extend(record_ids) or True
        )
        service._get_data_store = store
        service._get_data_store["offline_pending"] = {
            "task_completed": {
                "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share",
                "_monitor_token": "token_A"
            },
            "task_failed": {
                "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                "_monitor_token": "token_A", "fail_count": 2
            }
        }

        ctx = PostprocessBatchContext(
            pending={
                "task_failed": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share",
                    "fail_count": 3
                }
            },
            pending_snapshot={
                "task_completed": {
                    "created_at": 100.0, "share_url": "url1", "file_name": "f1", "task_type": "share"
                },
                "task_failed": {
                    "created_at": 200.0, "share_url": "url2", "file_name": "f2", "task_type": "share"
                }
            },
            due_keys=["task_completed", "task_failed"],
            now=1000.0,
            monitor_token="token_A",
            task_map={},
            tasks_valid=True,
            subscribe_cache={}
        )

        service._reconcile_pending_state(ctx)

        self.assertEqual(store.retry_index.rows, [])
        self.assertEqual(deletes, [])


# ---------------------------------------------------------------------------
# 1.6.46：历史终态文件级兜底匹配 + 重排队终止保证 + 日志降噪
# ---------------------------------------------------------------------------

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver",
    "app.utils", "app.utils.string", "sqlalchemy",
)
ensure_package("cloudsubscribex.core.history")
ensure_package("cloudsubscribex.core.media")
ensure_package("cloudsubscribex.drive")
ensure_package("cloudsubscribex.drive.common")
ensure_package("cloudsubscribex.search")
ensure_package("cloudsubscribex.search.types")
set_media_type(movie="电影", tv="电视剧")
sys.modules["cloudsubscribex.core"].CloudFile = MagicMock()

_OFFLINE_KEY = "pending_offline_strm"


class _FakeRetryIndex:
    """只记 upsert 的重试索引桩。"""

    def __init__(self):
        self.rows = []

    def upsert_row(self, row):
        self.rows.append(dict(row))
        return True

    def get_by_identity(self, record_time, share_url, file_name):
        for row in self.rows:
            if (
                    str(row.get("record_time") or "") == str(record_time or "")
                    and str(row.get("share_url") or "") == str(share_url or "")
                    and str(row.get("file_name") or "") == str(file_name or "")
            ):
                return dict(row)
        return None


class _FakeDataStore(dict):
    """既是 _get_data 的字典，又是 _get_data_store() 的可调用仓库。"""

    def __init__(self):
        super().__init__()
        self.retry_index = _FakeRetryIndex()
        self.repositories = types.SimpleNamespace(
            retry_index=self.retry_index,
        )

    def __call__(self):
        return self


class TestTerminalStateWritesRetryIndex(unittest.TestCase):
    """v1.13.3 §4.4：终态结算不再写 history、不再重排队。

    成功不写任何地方（MP 整理历史是唯一载体）；可重试的失败写进
    retry_index；永久失败不写；跨盘成功但落盘自检未通过写修复行。
    结算始终返回 True。
    """

    def _service(self):
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = _OFFLINE_KEY
        store = _FakeDataStore()
        service._get_data_store = store
        service._save_data = lambda key, value: None
        return service, store

    @staticmethod
    def _item(**overrides):
        item = {
            "file_name": "Show.S01E01.mkv",
            "staging_name": "Show.S01E01.mkv",
            "share_url": "https://pan.example.com/s/ABC",
            "cloud_dir": "/media/tv",
            "file_size": 123456,
            "source_sha1": "A1B2C3D4",
            "season": 1,
            "episode": 1,
            "created_at": 1_700_000_000.0,
            "mediainfo": {
                "tmdb_id": 123,
                "title": "测试剧",
                "type": "电视剧",
                "year": "2026",
            },
        }
        item.update(overrides)
        return item

    def test_failure_writes_retryable_row(self):
        service, store = self._service()
        item = self._item()
        self.assertTrue(
            service._write_terminal_state(item, "key1", "失败", "网络超时")
        )
        self.assertEqual(len(store.retry_index.rows), 1)
        row = store.retry_index.rows[0]
        self.assertEqual(row["status"], "失败")
        self.assertEqual(row["failure_reason"], "网络超时")
        self.assertEqual(row["failure_kind"], "network")
        self.assertTrue(row["failure_retryable"])
        self.assertEqual(row["share_url"], "https://pan.example.com/s/ABC")
        self.assertEqual(row["file_name"], "Show.S01E01.mkv")
        self.assertEqual(row["source_file_name"], "Show.S01E01.mkv")
        self.assertEqual(row["tmdb_id"], 123)
        self.assertEqual(row["title"], "测试剧")
        self.assertEqual(row["season"], 1)
        self.assertEqual(row["episode"], 1)
        self.assertTrue(str(row["record_id"] or ""))

    def test_permanent_failure_writes_nothing(self):
        service, store = self._service()
        item = self._item()
        self.assertTrue(
            service._write_terminal_state(item, "key1", "失败", "分享链接已失效")
        )
        self.assertEqual(store.retry_index.rows, [])

    def test_success_writes_nothing(self):
        service, store = self._service()
        item = self._item()
        self.assertTrue(service._write_terminal_state(item, "key1", "成功"))
        self.assertEqual(store.retry_index.rows, [])

    def test_cross_success_unlanded_writes_repair_row(self):
        service, store = self._service()
        # 跨盘：源盘 key 与目标盘不同。
        service._resource_provider_for_url = lambda url: types.SimpleNamespace(
            key="quark", name="夸克网盘",
        )
        service._cloud_drive = types.SimpleNamespace(key="u115cs")
        service.verify_landed_records = lambda records: {
            id(records[0]): {"landed": False, "reason": "missing_in_dir"},
        }
        item = self._item()
        self.assertTrue(service._write_terminal_state(item, "key1", "成功"))
        self.assertEqual(len(store.retry_index.rows), 1)
        row = store.retry_index.rows[0]
        self.assertEqual(row["status"], "成功")
        self.assertEqual(row["transfer_mode"], "cross")
        self.assertEqual(row["source_drive_key"], "quark")
        self.assertEqual(row["target_drive_key"], "u115cs")
        self.assertIs(row["landed"], False)

    def test_cross_success_landed_writes_nothing(self):
        service, store = self._service()
        service._resource_provider_for_url = lambda url: types.SimpleNamespace(
            key="quark", name="夸克网盘",
        )
        service._cloud_drive = types.SimpleNamespace(key="u115cs")
        service.verify_landed_records = lambda records: {
            id(records[0]): {"landed": True},
        }
        item = self._item()
        self.assertTrue(service._write_terminal_state(item, "key1", "成功"))
        self.assertEqual(store.retry_index.rows, [])

    def test_missing_identity_returns_true_without_write(self):
        service, store = self._service()
        item = self._item(share_url="")
        self.assertTrue(
            service._write_terminal_state(item, "key1", "失败", "网络超时")
        )
        self.assertEqual(store.retry_index.rows, [])


class _RecordingMonitor:
    """记录 apply_finalize_outcome 调用的监控替身。"""

    def __init__(self):
        self.calls = []

    def apply_finalize_outcome(
            self, share_url, subscribe_id, *, success, reason="", file_name="",
    ):
        self.calls.append({
            "share_url": share_url,
            "subscribe_id": subscribe_id,
            "success": success,
            "reason": reason,
            "file_name": file_name,
        })
        return 1


class TestTerminalStateSyncsMonitor(unittest.TestCase):
    """Phase 4 第 7 项：finalize 终态回写频道监控的接线。

    _write_terminal_state 成功/失败都调监控；用户主动删除/停止不调
    （那不是 finalize 的终态）；无 share_url / 无插件宿主时静默跳过。
    监控模块用 sys.modules 里的替身注入，避免加载真实 tg_monitor
    （它依赖 telethon 等重型模块）。
    """

    def setUp(self):
        self.monitor = _RecordingMonitor()
        self._real_tg_module = sys.modules.get("cloudsubscribex.core.tg_monitor")
        sys.modules["cloudsubscribex.core.tg_monitor"] = types.SimpleNamespace(
            get_channel_monitor=lambda plugin: self.monitor,
        )
        self.addCleanup(self._restore_tg_module)

    def _restore_tg_module(self):
        if self._real_tg_module is None:
            sys.modules.pop("cloudsubscribex.core.tg_monitor", None)
        else:
            sys.modules["cloudsubscribex.core.tg_monitor"] = self._real_tg_module

    def _service(self):
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = _OFFLINE_KEY
        store = _FakeDataStore()
        service._get_data_store = store
        service._save_data = lambda key, value: None
        # _plugin 经 OwnerDelegator 写到宿主 SimpleNamespace 上。
        service._plugin = types.SimpleNamespace()
        return service, store

    @staticmethod
    def _item(**overrides):
        item = {
            "file_name": "Show.S01E01.mkv",
            "staging_name": "Show.S01E01.mkv",
            "share_url": "https://pan.example.com/s/ABC",
            "cloud_dir": "/media/tv",
            "file_size": 123456,
            "source_sha1": "A1B2C3D4",
            "season": 1,
            "episode": 1,
            "subscribe_id": 101,
            "created_at": 1_700_000_000.0,
            "mediainfo": {
                "tmdb_id": 123,
                "title": "测试剧",
                "type": "电视剧",
                "year": "2026",
            },
        }
        item.update(overrides)
        return item

    def test_success_syncs_monitor(self):
        service, _store = self._service()
        self.assertTrue(
            service._write_terminal_state(self._item(), "key1", "成功")
        )
        self.assertEqual(len(self.monitor.calls), 1)
        call = self.monitor.calls[0]
        self.assertTrue(call["success"])
        self.assertEqual(call["share_url"], "https://pan.example.com/s/ABC")
        self.assertEqual(call["subscribe_id"], 101)
        self.assertEqual(call["file_name"], "Show.S01E01.mkv")

    def test_failure_syncs_monitor_with_reason(self):
        service, _store = self._service()
        self.assertTrue(
            service._write_terminal_state(
                self._item(), "key1", "失败", "网盘文件定位失败",
            )
        )
        self.assertEqual(len(self.monitor.calls), 1)
        call = self.monitor.calls[0]
        self.assertFalse(call["success"])
        self.assertEqual(call["reason"], "网盘文件定位失败")

    def test_user_delete_does_not_sync_monitor(self):
        service, _store = self._service()
        service._get_data_store[_OFFLINE_KEY] = {
            "key1": self._item(),
        }
        self.assertEqual(service.delete_pending_finalize_tasks({"key1"}), 1)
        self.assertEqual(self.monitor.calls, [])

    def test_user_stop_does_not_sync_monitor(self):
        service, _store = self._service()
        service._get_data_store[_OFFLINE_KEY] = {
            "key1": self._item(),
        }
        self.assertEqual(service.stop_pending_finalize_tasks({"key1"}), 1)
        self.assertEqual(self.monitor.calls, [])

    def test_no_share_url_skips_monitor(self):
        service, _store = self._service()
        self.assertTrue(
            service._write_terminal_state(
                self._item(share_url=""), "key1", "失败", "网络超时",
            )
        )
        self.assertEqual(self.monitor.calls, [])

    def test_no_plugin_skips_monitor_without_error(self):
        service = DummyPostprocessService()
        service._OFFLINE_PENDING_KEY = _OFFLINE_KEY
        service._get_data_store = _FakeDataStore()
        service._save_data = lambda key, value: None
        # 不设 _plugin：回写静默跳过，不阻塞终态结算。
        self.assertTrue(
            service._write_terminal_state(self._item(), "key1", "成功")
        )
        self.assertEqual(self.monitor.calls, [])


if __name__ == "__main__":
    unittest.main()
