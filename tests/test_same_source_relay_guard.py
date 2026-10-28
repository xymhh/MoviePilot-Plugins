"""1.6.47 同源并发互删单测（A：中继引用计数 + cache_key 串行化 + 暂存引用）。

固定契约：

组1 同 cache_key 串行与复用：
  同一 source_file 的两个任务，第二个必须排队等第一个（同 cache_key 锁），
  且复用第一个留下的完整缓存（下载只发生 1 次），终态后缓存删除、无互删。

组2 兄弟在飞保留、release 后删除：
  手动持有引用（模拟兄弟任务在飞）时任务终态保留完整缓存；
  释放引用后，下一个任务终态删除缓存。

组3 delete_cache 在用跳过：
  引用计数 > 0 时 delete_cache 返回 0 且不删文件；归零后可删。

A2 跨盘暂存引用计数（service.py）：
  ``_staging_ref_key`` 形状、归零释放语义、``_cleanup_cross_transfer_staging``
  在引用未归零时跳过删除、归零后按原语义删除。
"""

import hashlib
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugin_env import (
    install_app_mocks,
    load_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

# ---- app.* 占位（transfer 依赖 app.log；service 依赖 app.core.config 等全链）----
install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.config",
    "app.core.context", "app.core.metainfo", "app.core.module", "app.core.modules",
    "app.db", "app.db.downloadhistory_oper", "app.db.subscribe_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.models.subscribe",
    "app.helper", "app.helper.mediaserver", "app.helper.directory",
    "app.application", "app.application.mediaserver", "app.application.directory",
    "app.log", "app.utils", "app.utils.string", "app.utils.path",
    "app.modules", "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.schemas", "app.schemas.types", "sqlalchemy", "tortoise",
)
set_media_type(movie="电影", tv="电视剧")

# ---- cloudsubscribex 包层级 ----
install_app_mocks(
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.core.history", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.drive.scanner", "cloudsubscribex.drive.types",
    "cloudsubscribex.search", "cloudsubscribex.search.magnet",
    "cloudsubscribex.search.types", "cloudsubscribex.search.matching",
    "cloudsubscribex.search.cache",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription",
    "cloudsubscribex.utils", "cloudsubscribex.utils.cache",
)
sync_siblings = (
    "baseline", "cleanup", "history", "matching", "metadata", "movie", "naming",
    "notify", "platform_history", "postprocess", "pt_upgrade", "resources",
    "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
)
for _name in sync_siblings:
    install_app_mocks(f"cloudsubscribex.handlers.sync.{_name}")

cloud_module = load_module(
    "cloudsubscribex.core.cloud", "plugins.v2/cloudsubscribex/core/cloud.py"
)
transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
service_module = load_module(
    "cloudsubscribex.handlers.sync.service",
    "plugins.v2/cloudsubscribex/handlers/sync/service.py",
)

CloudFile = cloud_module.CloudFile
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
SyncHandler = service_module.SyncHandler

PAYLOAD = b"0123456789"
SHA1 = hashlib.sha1(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()


def make_file_item(name="movie.mkv"):
    return CloudFile(
        id="fid1", name=name, is_directory=False, size=len(PAYLOAD),
        sha1=SHA1, md5=MD5,
    )


class _FakeMutationSource:
    """支持 FILE_MUTATION 的源盘替身：记录 delete_file 调用。"""

    def __init__(self):
        self.deleted = []

    def supports(self, _capability):
        return True

    def require(self, _capability):
        return SimpleNamespace(delete_file=lambda file_id: self.deleted.append(file_id))


class _RelayHarnessBase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.file_item = make_file_item()
        self.download_calls = []
        self.upload_calls = []

    def make_manager(self, download_file=None, upload_file=None, max_concurrent=2):
        source = SimpleNamespace(
            name="夸克网盘",
            supports=lambda _capability: False,
            require=lambda _capability: SimpleNamespace(download_file=download_file),
        )

        present = set()

        def wrapped_upload(source_path, save_path, target_name,
                           progress_callback=None):
            result = (
                upload_file(source_path, save_path, target_name,
                            progress_callback)
                if upload_file else False
            )
            if result:
                present.add(target_name)
            return result

        def find_file(_path, name, **_kwargs):
            if name not in present:
                return None
            return CloudFile(
                id="n1", name=name, is_directory=False, size=len(PAYLOAD),
                sha1=SHA1, md5=MD5,
            )

        def require_target(capability):
            cap = transfer_module.CloudDriveCapability
            if capability == cap.LOCAL_UPLOAD:
                return SimpleNamespace(
                    upload_file=wrapped_upload, upload_from_link=None,
                    upload_progressive=None,
                )
            if capability == cap.FILE_QUERY:
                # 上传完成后校验目标文件存在；写入前目标不存在。
                return SimpleNamespace(find_file=find_file)
            raise AssertionError(f"测试不应 require {capability}")

        target = SimpleNamespace(
            name="115",
            supports=lambda _capability: False,
            require=require_target,
        )

        def resolver(key):
            return target if key == "115" else source

        return CrossTransferTaskManager(
            resolver, download_path=self.tempdir.name,
            download_threads=5, max_concurrent=max_concurrent,
            direct_stream=False,
        )

    def _wait_terminal(self, manager, task_id, timeout=8.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            with manager._lock:
                task = manager._tasks.get(task_id)
                status = task["status"] if task else None
            if task and status not in manager.ACTIVE:
                return task
            time.sleep(0.05)
        self.fail("任务未在期限内进入终态")

    def _wait_cache_gone(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not list(Path(self.tempdir.name).glob("cloud-transfer-*")):
                return True
            time.sleep(0.05)
        return False

    def _seed_complete_cache(self, manager):
        cache_key = manager._cache_key("quark", self.file_item)
        cache_path = manager._cache_path(cache_key)
        cache_path.write_bytes(PAYLOAD)
        manager._mark_complete_cache(cache_path, cache_key, self.file_item)
        return cache_path

    def _ok_upload(self, source_path, save_path, target_name, progress_callback=None):
        self.upload_calls.append(str(source_path))
        if progress_callback:
            progress_callback(len(PAYLOAD), len(PAYLOAD))
        return True


class SameCacheKeySerializationTests(_RelayHarnessBase):
    """组1：同 cache_key 任务严格串行 + 复用完整缓存 + 终态互删防护。"""

    def test_same_cache_key_serializes_and_reuses_cache(self):
        started = threading.Event()
        release = threading.Event()

        def download_file(file_item, local_path, progress_callback=None,
                          stop_requested=None, preserve_partial=True,
                          download_threads=5):
            self.download_calls.append(str(local_path))
            target = Path(local_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PAYLOAD)
            started.set()
            if not release.wait(timeout=5.0):
                raise TimeoutError("释放事件未触发")
            return str(local_path)

        manager = self.make_manager(
            download_file=download_file, upload_file=self._ok_upload,
            max_concurrent=2,
        )
        public_a = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        self.assertTrue(started.wait(timeout=5.0), "任务A应先进入下载")

        public_b = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        time.sleep(0.2)
        with manager._lock:
            task_b = manager._tasks.get(public_b["id"])
        self.assertIn(task_b["status"], manager.ACTIVE, "任务B应仍在排队")
        self.assertEqual(
            len(self.download_calls), 1,
            "A 持同 cache_key 锁下载期间，B 必须排队（并发位为 2，只能靠 cache_key 锁串行）",
        )

        release.set()
        task_a = self._wait_terminal(manager, public_a["id"])
        task_b = self._wait_terminal(manager, public_b["id"])

        self.assertEqual(task_a["status"], "success")
        self.assertEqual(task_b["status"], "success")
        self.assertEqual(
            len(self.download_calls), 1,
            "B 必须复用 A 留下的完整缓存，不得重新下载（同源互删防护的核心路径）",
        )
        self.assertEqual(len(self.upload_calls), 1, "目标已存在后第二个任务不再上传")
        self.assertEqual(task_b["method"], "reuse")
        self.assertTrue(self._wait_cache_gone(), "两任务终态后中继缓存必须删除")

    def test_other_key_task_not_serialized(self):
        """不同 cache_key 不受该锁阻塞（锁只按 key 区分）。"""
        started = threading.Event()
        release = threading.Event()
        seen_keys = []

        def download_file(file_item, local_path, progress_callback=None,
                          stop_requested=None, preserve_partial=True,
                          download_threads=5):
            self.download_calls.append(str(local_path))
            seen_keys.append(file_item.name)
            target = Path(local_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PAYLOAD)
            started.set()
            release.wait(timeout=5.0)
            return str(local_path)

        manager = self.make_manager(
            download_file=download_file, upload_file=self._ok_upload,
            max_concurrent=1,
        )
        first_item = make_file_item(name="a.mkv")
        second_item = make_file_item(name="b.mkv")
        public_a = manager.create(
            source_key="quark", source_file=first_item,
            target_key="115", save_path="/", target_name="a.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        self.assertTrue(started.wait(timeout=5.0))
        # 第二个任务 key 不同：槽位被 A 占住（max_concurrent=1），仍必须
        # 排在槽位上而不是被 A 的 cache_key 锁挡死——释放 A 后两者都能完成。
        public_b = manager.create(
            source_key="quark", source_file=second_item,
            target_key="115", save_path="/", target_name="b.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        time.sleep(0.1)
        release.set()
        task_a = self._wait_terminal(manager, public_a["id"])
        task_b = self._wait_terminal(manager, public_b["id"])
        self.assertEqual(task_a["status"], "success")
        self.assertEqual(task_b["status"], "success")
        self.assertEqual(sorted(seen_keys), ["a.mkv", "b.mkv"])
        self.assertTrue(self._wait_cache_gone())


class CacheRefRetentionTests(_RelayHarnessBase):
    """组2/组3：引用计数保留与 delete_cache 在用跳过。"""

    def test_sibling_in_flight_retains_then_next_task_deletes(self):
        manager = self.make_manager(upload_file=self._ok_upload)
        cache_path = self._seed_complete_cache(manager)
        cache_key = manager._cache_key("quark", self.file_item)

        manager._acquire_cache_ref(cache_key)  # 模拟兄弟任务在飞
        public_first = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        first = self._wait_terminal(manager, public_first["id"])
        self.assertEqual(first["status"], "success")
        self.assertTrue(
            cache_path.is_file(),
            "兄弟任务在飞（引用未归零）时终态必须保留完整缓存",
        )
        self.assertEqual(self.download_calls, [], "首任务应复用完整缓存")
        self.assertEqual(
            manager._cache_ref_count(cache_key), 1, "任务终态后剩兄弟引用"
        )

        manager._release_cache_ref(cache_key)  # 兄弟完成
        public_second = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        second = self._wait_terminal(manager, public_second["id"])
        self.assertEqual(second["status"], "success")
        self.assertFalse(
            cache_path.exists(),
            "引用归零后，下一个任务终态必须删除缓存（不残留垃圾）",
        )
        self.assertEqual(self.download_calls, [], "第二任务同样复用缓存")

    def test_local_source_task_bypasses_lock_and_refcount(self):
        """本地源文件任务（source_file 为空）不加锁、不计数。"""
        manager = self.make_manager(upload_file=self._ok_upload)
        self.assertEqual(
            CrossTransferTaskManager._task_cache_key(
                {"source_file": None, "source_provider": "quark"}
            ),
            "",
            "本地源文件任务 cache_key 必须为空",
        )
        self.assertEqual(
            CrossTransferTaskManager._task_cache_key({}),
            "",
            "缺 source_file 字段同样不计数",
        )
        self.assertEqual(
            CrossTransferTaskManager._task_cache_key(
                {"source_file": self.file_item, "source_provider": "quark"}
            ),
            manager._cache_key("quark", self.file_item),
            "云端任务 cache_key 必须与 _cache_key 同源（串行与复用依赖同一 key）",
        )
        manager._acquire_cache_ref("")
        manager._release_cache_ref("")
        self.assertEqual(manager._cache_ref_count(""), 0, "空 cache_key 不进计数表")

    def test_delete_cache_skips_while_in_use(self):
        """组3：引用在用 delete_cache 返回 0 不删；归零后删除。"""
        manager = self.make_manager(upload_file=self._ok_upload)
        cache_path = self._seed_complete_cache(manager)
        cache_key = manager._cache_key("quark", self.file_item)
        meta_path = manager._cache_meta_path(cache_path)

        manager._acquire_cache_ref(cache_key)
        self.assertEqual(manager.delete_cache(cache_key), 0, "在用引用时必须跳过")
        self.assertTrue(cache_path.is_file())
        self.assertTrue(meta_path.is_file())

        manager._release_cache_ref(cache_key)
        self.assertGreaterEqual(manager.delete_cache(cache_key), 1, "归零后允许删除")
        self.assertFalse(cache_path.exists())
        self.assertFalse(meta_path.exists())

    def test_cancel_with_sibling_retains_and_changes_message(self):
        """取消时若兄弟在飞：不删缓存，且消息不含「已清理中继缓存」。"""
        manager_ref = {}

        def download_file(file_item, local_path, progress_callback=None,
                          stop_requested=None, preserve_partial=True,
                          download_threads=5):
            target = Path(local_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(PAYLOAD)
            raise InterruptedError

        manager = self.make_manager(download_file=download_file)
        manager_ref["manager"] = manager
        cache_path = manager._cache_path(
            manager._cache_key("quark", self.file_item)
        )
        cache_key = manager._cache_key("quark", self.file_item)
        manager._acquire_cache_ref(cache_key)  # 兄弟在飞

        public = manager.create(
            source_key="quark", source_file=self.file_item,
            target_key="115", save_path="/", target_name="movie.mkv",
            algorithm="sha1", checksum=SHA1,
        )
        task = self._wait_terminal(manager, public["id"])

        self.assertEqual(task["status"], "canceled")
        self.assertTrue(cache_path.is_file(), "兄弟在飞时取消不得删除中继缓存")
        self.assertIn("中继缓存保留", task["message"])
        self.assertNotIn("已清理中继缓存", task["message"])

        manager._release_cache_ref(cache_key)
        manager.delete_cache(cache_key)
        self.assertFalse(cache_path.exists(), "引用归零后可显式清理")


class StagingRefcountTests(unittest.TestCase):
    """A2：跨盘暂存文件引用计数（service.py）。"""

    def setUp(self):
        self._saved = dict(service_module._STAGING_REFS)
        with service_module._STAGING_REFS_LOCK:
            service_module._STAGING_REFS.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        with service_module._STAGING_REFS_LOCK:
            service_module._STAGING_REFS.clear()
            service_module._STAGING_REFS.update(self._saved)

    def test_staging_ref_key_shapes(self):
        item = SimpleNamespace(name="1.mp4")
        self.assertEqual(
            service_module._staging_ref_key("/moviepilot/转存", item),
            "/moviepilot/转存/1.mp4",
        )
        self.assertEqual(
            service_module._staging_ref_key("/moviepilot/转存/", item),
            "/moviepilot/转存/1.mp4",
            "路径尾斜杠归一",
        )
        self.assertEqual(service_module._staging_ref_key("", item), "")
        self.assertEqual(service_module._staging_ref_key("/a", None), "")
        self.assertEqual(
            service_module._staging_ref_key("/a", SimpleNamespace(name="")), ""
        )

    def test_refcount_release_semantics(self):
        key = "/staging/1.mp4"
        service_module._acquire_staging_ref(key)
        service_module._acquire_staging_ref(key)
        self.assertEqual(service_module._release_staging_ref(key), 1, "剩余引用 1 时不可删")
        self.assertEqual(service_module._release_staging_ref(key), 0, "归零返回 0 可删")
        self.assertEqual(service_module._release_staging_ref(key), 0, "空释放不为负")
        self.assertEqual(service_module._release_staging_ref(""), 0, "空键恒为 0")
        service_module._acquire_staging_ref("")
        self.assertNotIn("", service_module._STAGING_REFS, "空键不进计数表")

    def test_cleanup_skips_delete_while_referenced(self):
        source = _FakeMutationSource()
        item = SimpleNamespace(id="f1", name="1.mp4")
        key = service_module._staging_ref_key("/staging", item)
        service_module._acquire_staging_ref(key)
        service_module._acquire_staging_ref(key)

        SyncHandler._cleanup_cross_transfer_staging(source, "/staging", item)
        self.assertEqual(source.deleted, [], "仍有引用时必须跳过删除")

        SyncHandler._cleanup_cross_transfer_staging(source, "/staging", item)
        self.assertEqual(source.deleted, ["f1"], "引用归零后按原语义删除")
        self.assertEqual(service_module._release_staging_ref(key), 0)

    def test_cleanup_without_refs_keeps_original_delete(self):
        source = _FakeMutationSource()
        item = SimpleNamespace(id="f2", name="2.mp4")
        SyncHandler._cleanup_cross_transfer_staging(source, "/staging", item)
        self.assertEqual(source.deleted, ["f2"], "无引用计数时保持原删除语义")

    def test_cleanup_without_item_short_circuits(self):
        source = _FakeMutationSource()
        SyncHandler._cleanup_cross_transfer_staging(source, "/staging", None)
        self.assertEqual(source.deleted, [], "缺 item 不做删除（原语义）")
        SyncHandler._cleanup_cross_transfer_staging(None, "/staging",
                                                    SimpleNamespace(id="f3", name="x"))
        self.assertEqual(source.deleted, [], "缺 source 不做删除（原语义）")


if __name__ == "__main__":
    unittest.main()
