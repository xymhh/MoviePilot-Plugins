"""v1.29.7 阶段 1：网盘整理漏斗骨架（队列 + worker + 提交前复查）。

覆盖（按设计 §12 阶段 1）：
- 仓储：队列写/取/状态机、去重键唯一约束冲突、租约过期回收/未过期不回收、
  终态释放去重键、断点续跑；
- 入队：手动提交在 finalize 边界入队（funnel_route 标记透传）、重复入队返回旧行；
- 准备：整理前改名在实查与 MP 整理之前、改名失败不进整理、已有整理身份不改名；
- 落盘前实查：双名拦截（带/不带 {tmdb-} 都算）、查不到 fail-open、洗版跳过库检查；
- 单轮：入队→整理→终态端到端、拦截第二份且不删文件、租约过期行回收重跑、
  终态只提交一次、停止标志截断本轮、kill-switch 关时不建 worker job；
- 提交前复查：precommit_recheck 拦截在途/库内第二份、_commit_single_item
  入口拦截后走 abort（notify=False）且不写终态、复查异常 fail-open。

harness 说明：
- DB 用真实临时 SQLite（CloudSubscribeDatabaseManager + init_db 的
  create_all 补建 funnel_queue 表）；真实 DB 模块（manager/models/
  repositories/storage）在 setUpClass 里懒加载并复用已加载的对象——
  不要在 import 时 restore sqlalchemy 或加载它们：提前加载会改动
  全量套件的 sqlalchemy 状态（state_db_migration 的 import 时 restore
  会删掉真实 SA 子模块，后续桩安装留空 __path__ 的 SA 桩，task_center
  收集失败），且两代 sqlalchemy 对象混用会在 ORM 操作上抛
  ArgumentError（模型类与 update/select 分属不同代）。
- FunnelService 用 SyncHandler 替身做宿主（OwnerDelegator 把配置/存储/
  组件读数委托给宿主；替身上直接放属性即可）。
- _commit_single_item 与 _queue_file_finalize_batch 用 test_finalize_mp_wait_1_12_6
  已有的真实模块加载模式（PostprocessService / SyncHandler 真类 +
  __new__ 替身宿主），不拼全量插件。
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import (  # noqa: E402
    install_app_mocks,
    install_core_delegation,
    load_module,
    materialize_handler_fields,
    migrate_handler_stubs,
    set_media_type,
)


# 先装 test_finalize_mp_wait_1_12_6 加载真实 service/postprocess 模块所需的
# app 桩（否则按收集顺序先导入本文件时会因缺 app.chain 等而失败）。
install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.models", "app.db.models.subscribe",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver",
    "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache", "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)

# 先加载 test_finalize_mp_wait_1_12_6 的真实 postprocess/service 模块
# （它自己的 install_app_mocks 不碰 sqlalchemy/apscheduler/pytz）。
try:
    from test_finalize_mp_wait_1_12_6 import (  # noqa: E402
            PostprocessService,
        _ctx,
        _service_module,
        _stub,
        DummyMpWaitService,
        FakeTracker,
        DirectoryFileIndex,
        _item as _mp_item,
        FILE_NAME as _MP_FILE_NAME,
        DEST_PARENT as _MP_DEST_PARENT,
        DEST_PATH as _MP_DEST_PATH,
        NOW as _MP_NOW,
    )
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import (  # noqa: E402
            PostprocessService,
        _ctx,
        _service_module,
        _stub,
        DummyMpWaitService,
        FakeTracker,
        DirectoryFileIndex,
        _item as _mp_item,
        FILE_NAME as _MP_FILE_NAME,
        DEST_PARENT as _MP_DEST_PARENT,
        DEST_PATH as _MP_DEST_PATH,
        NOW as _MP_NOW,
    )

SyncHandler = _service_module.SyncHandler

install_app_mocks(
    "app", "app.log", "app.core", "app.core.config", "app.db",
    "app.db.subscribe_oper", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.database",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
)
MediaType = set_media_type(movie="电影", tv="电视剧")
install_core_delegation()

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugin_dir = os.path.join(project_root, "plugins.v2", "cloudsubscribex")
PKG = "cloudsubscribex"


def _restore_real_sqlalchemy() -> None:
    """清理 sys.modules 里的 sqlalchemy 顶层替身（只在 DB 模块尚未加载时调用）。

    只弹 ``sqlalchemy`` 本身，绝不碰 ``sqlalchemy.*`` 子模块（沿用
    test_retry_index_1_13_1 的做法）：全量套件里真实 sqlalchemy 早已由
    先收集的文件加载并绑定到 manager/models；把真实子模块弹掉，下一次
    import 会重新加载第二份拷贝，旧 engine 造出的对象与新 dialect 身份
    不一致（sqlalchemy.exc.ArgumentError，source_sha1 与 alembic 往返
    用例在全量跑里就是这么被拖垮的）。弹掉顶层后重新导入真实包，并给
    已在 sys.modules 里的真实子模块补上父属性（最要命的是 .dialects）。
    """
    stale = sys.modules.get("sqlalchemy")
    if stale is None or getattr(stale, "__spec__", None):
        return
    sys.modules.pop("sqlalchemy", None)
    import sqlalchemy  # noqa: F401  (真实包；子模块复用 sys.modules 里的真实模块)
    for name, module in list(sys.modules.items()):
        if not name.startswith("sqlalchemy."):
            continue
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name)
        if parent is not None and child and not hasattr(parent, child):
            setattr(parent, child, module)


def _load_once(module_name, relative_path):
    import types

    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


# 真实 DB 模块句柄：setUpClass 里懒加载（见 _ensure_db_modules）。
manager_module = None
models_module = None
repositories_module = None
storage_module = None
CloudSubscribeDatabaseManager = None
FunnelQueueRepository = None
CloudSubscribeDataStore = None


def _ensure_db_modules():
    """加载真实 DB 链（manager/models/repositories/storage）；幂等。

    全量套件里 test_source_sha1_cache_db.py 在收集时已加载，直接复用，
    不再 restore（它的模块对象与套件的 sqlalchemy 是同一代）。
    子集跑（未收集 source_sha1）时先清掉 sqlalchemy 替身再加载。
    """
    global manager_module, models_module, repositories_module, storage_module
    global CloudSubscribeDatabaseManager, FunnelQueueRepository, CloudSubscribeDataStore
    if manager_module is not None:
        return
    _restore_real_sqlalchemy()
    manager_module = _load_once(
        f"{PKG}.core.database.manager",
        os.path.join(plugin_dir, "core", "database", "manager.py"),
    )
    _db_pkg = sys.modules[f"{PKG}.core.database"]
    _db_pkg.CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
    models_module = _load_once(
        f"{PKG}.core.database.models",
        os.path.join(plugin_dir, "core", "database", "models.py"),
    )
    repositories_module = _load_once(
        f"{PKG}.core.database.repositories",
        os.path.join(plugin_dir, "core", "database", "repositories.py"),
    )
    _db_pkg.CloudSubscribeRepositories = repositories_module.CloudSubscribeRepositories
    storage_module = _load_once(
        f"{PKG}.core.storage", os.path.join(plugin_dir, "core", "storage.py")
    )
    CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
    FunnelQueueRepository = repositories_module.FunnelQueueRepository
    CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore


load_module(
    f"{PKG}.core.media",
    os.path.join(plugin_dir, "core", "media.py"),
)

funnel_module = load_module(
    f"{PKG}.handlers.sync.funnel",
    os.path.join(plugin_dir, "handlers", "sync", "funnel.py"),
)
FunnelService = funnel_module.FunnelService
FUNNEL_WORKER_JOB_ID = funnel_module.FUNNEL_WORKER_JOB_ID

# funnel.py 顶层 import 的 app 替身：时区给真值，SessionFactory/SubscribeOper
# 换成可控替身（模块级 _FAKE_SUBSCRIBES 供用例填订阅）。
funnel_module.settings.TZ = "Asia/Shanghai"

_FAKE_SUBSCRIBES = {}


class _FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_session_factory():
    return _FakeSession()


class _FakeSubscribeOper:
    def __init__(self, db=None):
        self._db = db

    def get(self, subscribe_id):
        return _FAKE_SUBSCRIBES.get(int(subscribe_id or 0))

    def list(self):
        return list(_FAKE_SUBSCRIBES.values())


funnel_module.SessionFactory = _fake_session_factory
funnel_module.SubscribeOper = _FakeSubscribeOper

_TV_TMDB_ID = 294498
_TV_TITLE = "多喜一家人"
_TV_CLOUD_DIR = "/Alist/影视/moviepilot/媒体库/剧集/国产剧/多喜一家人 (2026) {tmdb-294498}/Season 1"
_TV_FILE = "多喜一家人 {tmdb-294498} - S01E09 - 第9集.mkv"
_TV_STAGING = "多喜一家人 - S01E09 - 第9集.mkv"
_TV_SIZE = 1585446912
_TARGET_DIR = "/Alist/影视/moviepilot/转存/"
_MOVIE_TMDB_ID = 123456
_MOVIE_TITLE = "测试电影"
_MOVIE_CLOUD_DIR = "/Alist/影视/moviepilot/媒体库/电影/测试电影 (2026) {tmdb-123456}"
_MOVIE_FILE = "测试电影 {tmdb-123456} (2026).mkv"
_MOVIE_SIZE = 5000000000


def _tv_media():
    return SimpleNamespace(
        type=MediaType.TV,
        tmdb_id=_TV_TMDB_ID,
        title=_TV_TITLE,
        year="2026",
        season=1,
        category="国产剧",
    )


def _movie_media():
    return SimpleNamespace(
        type=MediaType.MOVIE,
        tmdb_id=123456,
        title="测试电影",
        year="2026",
    )


def _tv_item(**extra):
    item = {
        "cloud_dir": _TV_CLOUD_DIR,
        "staging_dir": _TV_CLOUD_DIR,
        "file_name": _TV_FILE,
        "staging_name": _TV_STAGING,
        # result_key 即转存管线里的 115 文件 id（transfer_ops:582）；
        # 入队时 file_id 就取它，pending 项本身没有 file_id 字段。
        "result_key": "f-tv-09",
        "file_size": _TV_SIZE,
        "season": 1,
        "episode": 9,
        "subscribe_id": 444,
        "success_episodes": [9],
        "notification_episodes": [9],
        "source": "manual",
    }
    item.update(extra)
    return item


def _payload(**extra):
    payload = {
        "media_type": "tv",
        "tmdb_id": _TV_TMDB_ID,
        "season": 1,
        "episode": 9,
        "title": _TV_TITLE,
        "file_id": "f-tv-09",
        "file_name": _TV_FILE,
        "file_size": _TV_SIZE,
        "sha1": "a" * 40,
        "cloud_dir": _TV_CLOUD_DIR,
        "staging_dir": _TV_CLOUD_DIR,
        "staging_name": _TV_STAGING,
        "source": "manual",
        "subscribe_id": 444,
        "score": 80,
        "quality": "2160p",
        "is_upgrade": False,
        "replaced_from": [],
        "notification_kind": "transfer",
        "pending_key": "pk-tv-09",
        "media_data": {"tmdb_id": _TV_TMDB_ID, "type": "电视剧", "title": _TV_TITLE},
    }
    payload.update(extra)
    return payload


def _movie_item(**extra):
    item = {
        "cloud_dir": _MOVIE_CLOUD_DIR,
        "staging_dir": _MOVIE_CLOUD_DIR,
        "file_name": _MOVIE_FILE,
        "staging_name": _MOVIE_FILE,
        # result_key 即转存管线里的 115 文件 id；入队时 file_id 就取它。
        "result_key": "f-mv-01",
        "file_size": _MOVIE_SIZE,
        "subscribe_id": 445,
        "source": "manual",
    }
    item.update(extra)
    return item


def _movie_payload(**extra):
    payload = {
        "media_type": "movie",
        "tmdb_id": _MOVIE_TMDB_ID,
        "season": 0,
        "episode": 0,
        "title": _MOVIE_TITLE,
        "file_id": "f-mv-01",
        "file_name": _MOVIE_FILE,
        "file_size": _MOVIE_SIZE,
        "sha1": "b" * 40,
        "cloud_dir": _MOVIE_CLOUD_DIR,
        "staging_dir": _MOVIE_CLOUD_DIR,
        "staging_name": _MOVIE_FILE,
        "source": "manual",
        "subscribe_id": 445,
        "score": 80,
        "quality": "2160p",
        "is_upgrade": False,
        "replaced_from": [],
        "notification_kind": "transfer",
        "pending_key": "pk-mv-01",
        "media_data": {"tmdb_id": _MOVIE_TMDB_ID, "type": "电影", "title": _MOVIE_TITLE},
    }
    payload.update(extra)
    return payload


class _FakeRegistry:
    """SchedulerRegistry 替身：只记 job，不跑调度。"""

    def __init__(self):
        self.jobs = {}
        self.calls = []

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def ensure_job(self, job_id, func=None, trigger=None, next_run_time=None, **kwargs):
        self.jobs[job_id] = {
            "func": func,
            "trigger": trigger,
            "next_run_time": next_run_time,
        }
        self.calls.append(("ensure_job", job_id))
        return self.jobs[job_id]

    def modify_job_next_run(self, job_id, when):
        if job_id in self.jobs:
            self.jobs[job_id]["next_run_time"] = when
        self.calls.append(("modify_next_run", job_id))

    def remove_job(self, job_id):
        self.calls.append(("remove_job", job_id))
        return self.jobs.pop(job_id, None) is not None


class _FakePostprocess:
    """后处理替身：目录快照 + 驱动上下文（按预设终态结算）。"""

    def __init__(self):
        self.snapshots = {}
        self.snapshot_calls = []
        self.ctx = None
        self.process_calls = 0
        self.funnel_drive_calls = []

    def _cloud_directory_snapshot(self, cloud_dir, _cache):
        self.snapshot_calls.append(cloud_dir)
        index = self.snapshots.get(cloud_dir)
        if index is None:
            return False, {}
        return True, dict(index)

    def _prepare_postprocess_context(self, force=True, pending_keys=None):
        return self.ctx

    def _process_due_items(self, ctx, funnel_drive=False):
        self.process_calls += 1
        self.funnel_drive_calls.append(funnel_drive)
        for key in list(ctx.due_keys or []):
            ctx.completed += 1
            ctx.committed_keys.add(key)

    def _flush_batch_postprocess(self, ctx):
        return None

    def _reconcile_pending_state(self, ctx):
        return None


class _FakeDedup:
    def __init__(
            self, versions=None, active=True, fail_collect=False,
            movie_versions=None, fail_movie=False,
    ):
        self.versions = versions or {}
        self.active = active
        self.fail_collect = fail_collect
        self.collect_calls = []
        self.decider_reason = "评分降低 90→80"
        self.movie_versions = movie_versions or []
        self.fail_movie = fail_movie
        self.movie_calls = []

    def _episode_dedup_active(self):
        return self.active

    def _collect_episode_versions(self, subscribe, media, season, *, force_refresh=False):
        # v1.29.10 热修复：签名与真实方法逐字一致（force_refresh 旁路）；
        # 调用记下 force_refresh，实查入口的测试据此断言强制刷新。
        self.collect_calls.append((subscribe, media, season, force_refresh))
        if self.fail_collect:
            raise RuntimeError("library lookup down")
        return dict(self.versions)

    def _find_cloud_movie_versions(self, subscribe, media):
        self.movie_calls.append((subscribe, media))
        if self.fail_movie:
            raise RuntimeError("movie library lookup down")
        return list(self.movie_versions)

    def _episode_dedup_decision(self, subscribe, media, season, episode, score, size, **kwargs):
        return ("skip", "skip", self.decider_reason)


class _FakeMpOrganize:
    def __init__(self, accepted=True, info=None):
        self.accepted = accepted
        self.info = info or {}
        self.calls = []

    def trigger_mp_organize(self, cloud_dir, file_name, mediainfo=None, season=1):
        self.calls.append((cloud_dir, file_name, season))
        return self.accepted, dict(self.info)


class _FakeDrive:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def rename_file(self, dir_path, file_obj, target_name):
        self.calls.append((dir_path, file_obj, target_name))
        return self.ok


class _FakePlugin:
    def __init__(self, store, registry):
        self._store = store
        self._registry = registry

    def _get_data_store(self):
        return self._store

    def _scheduler_registry(self):
        return self._registry


class _FakeOwner:
    """SyncHandler 替身：FunnelService 经 OwnerDelegator 委托读它。"""

    def __init__(self, store, registry):
        self._plugin = _FakePlugin(store, registry)
        self._OFFLINE_PENDING_KEY = "pending_offline_strm"
        self._funnel_phase1_enabled = True
        self._funnel_target_dir = _TARGET_DIR
        self._funnel_trigger = "immediate"
        self._funnel_inflight_lease_seconds = 7200
        self._funnel_max_attempts = 5
        self._funnel_dedup_strict = True
        self._funnel_stop_requested = False
        self._pending = {}
        self.postprocess = _FakePostprocess()
        self.episode_dedup = _FakeDedup()
        self.mp_organize = _FakeMpOrganize()
        self._cloud_drive = _FakeDrive()
        self._get_data = lambda key: self._pending if key == self._OFFLINE_PENDING_KEY else {}
        self._save_offline_pending = lambda data: None
        self._serialize_mediainfo = lambda media: {
            "tmdb_id": _TV_TMDB_ID, "type": "电视剧", "title": _TV_TITLE,
        }
        self._deserialize_mediainfo = lambda data: _tv_media()


class _OwnerWithPath:
    def __init__(self, path):
        self._path = Path(path)

    def get_data_path(self):
        return self._path


class _DbMixin(unittest.TestCase):
    """真实临时 SQLite + CloudSubscribeDataStore（funnel_queue 表由 create_all 建）。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_db_modules()

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "cloudsubscribe.db"
        self.manager = CloudSubscribeDatabaseManager(self.db_path)
        self.manager.open()
        self.manager.init_db()
        self.addCleanup(self.manager.close)
        self.repo = FunnelQueueRepository(self.manager)
        self.store = CloudSubscribeDataStore(_OwnerWithPath(self._tmp.name))
        # 存储已由 manager.init_db() 建好表：initialize() 只做幂等标记，
        # 跳过 alembic update_db（测试环境不跑迁移链）。
        self.store.manager.open()
        self.store.manager.init_db()
        self.store._initialized = True
        self.addCleanup(self.store.manager.close)
        self.registry = _FakeRegistry()
        self.owner = _FakeOwner(self.store, self.registry)
        self.service = FunnelService(self.owner)
        self._prev_subscribes = dict(_FAKE_SUBSCRIBES)
        _FAKE_SUBSCRIBES.clear()
        _FAKE_SUBSCRIBES[444] = SimpleNamespace(
            id=444, tmdbid=_TV_TMDB_ID, name=_TV_TITLE, season=1, type="电视剧",
        )
        self.addCleanup(self._restore_subscribes)

    def _restore_subscribes(self):
        _FAKE_SUBSCRIBES.clear()
        _FAKE_SUBSCRIBES.update(self._prev_subscribes)

    def _enqueue_tv(self, **overrides):
        row, created = self.repo.enqueue(_payload(**overrides))
        self.assertTrue(created)
        return row

    def _pending_item(self, pending_key="pk-tv-09", **extra):
        item = _tv_item(**extra)
        self.owner._pending[pending_key] = dict(item)
        return item


class FunnelQueueRepositoryTests(_DbMixin):
    def test_enqueue_and_get_by_id(self):
        """队列写/取：入队返回新建行，get_by_id 能取回。"""
        row, created = self.repo.enqueue(_payload())
        self.assertTrue(created)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["attempts"], 0)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["file_name"], _TV_FILE)
        self.assertEqual(fetched["pending_key"], "pk-tv-09")

    def test_enqueue_same_key_returns_existing(self):
        """去重键唯一约束：同键第二次入队不建新行，返回旧行。"""
        first, created_first = self.repo.enqueue(_payload())
        second, created_second = self.repo.enqueue(_payload(pending_key="pk-tv-09-dup"))
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(self.repo.count_by_status().get("queued"), 1)

    def test_enqueue_movie_key_uses_zero_season_episode(self):
        """电影去重键是 (movie, tmdb_id, 0, 0)：同片两次入队只留一行。"""
        movie = _payload(
            media_type="movie", tmdb_id=123456, season=0, episode=0,
            title="测试电影", file_id="f-mv-1", file_name="测试电影 (2026).mkv",
            pending_key="pk-mv-1",
        )
        first, created_first = self.repo.enqueue(movie)
        second, created_second = self.repo.enqueue(dict(movie, pending_key="pk-mv-2"))
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(second["id"], first["id"])

    def test_enqueue_tmdb_none_rows_use_file_id(self):
        """tmdb_id 为空的行不撞唯一键：不同 file_id 各建一行。"""
        first, created_first = self.repo.enqueue(_payload(tmdb_id=None, file_id="f-a", pending_key="pk-a"))
        second, created_second = self.repo.enqueue(_payload(tmdb_id=None, file_id="f-b", pending_key="pk-b"))
        self.assertTrue(created_first)
        self.assertTrue(created_second)
        self.assertNotEqual(second["id"], first["id"])

    def test_claim_sets_processing_and_second_claim_fails(self):
        """认领：queued→processing 带租约；同一行第二次认领失败。"""
        row = self._enqueue_tv()
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now + 7200, now=now))
        self.assertFalse(self.repo.claim(row["id"], now + 7200, now=now))
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "processing")
        self.assertEqual(fetched["lease_until"], now + 7200)

    def test_claim_future_next_retry_not_claimable(self):
        """未到下次重试时间的行认领失败（退避中不抢跑）。"""
        row = self._enqueue_tv()
        self.repo.mark_failed(row["id"], int(time.time()) + 3600, "boom")
        self.assertFalse(self.repo.claim(row["id"], int(time.time()) + 7200))

    def test_mark_done_releases_key(self):
        """终态释放：done 后同键可重新入队（新行）。"""
        row = self._enqueue_tv()
        self.assertTrue(self.repo.mark_done(row["id"]))
        second, created = self.repo.enqueue(_payload(pending_key="pk-tv-09-again"))
        self.assertTrue(created)
        self.assertNotEqual(second["id"], row["id"])

    def test_mark_failed_increments_attempts(self):
        """失败记账：attempts+1，带下次重试时间与原因。"""
        row = self._enqueue_tv()
        now = int(time.time())
        self.assertTrue(self.repo.mark_failed(row["id"], now + 60, "改名失败"))
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "failed")
        self.assertEqual(fetched["attempts"], 1)
        self.assertEqual(fetched["next_retry_at"], now + 60)
        self.assertEqual(fetched["last_error"], "改名失败")

    def test_mark_held_is_terminal(self):
        """挂起是终态：释放去重键，不再进 list_due。"""
        row = self._enqueue_tv()
        self.assertTrue(self.repo.mark_held(row["id"], "已在库（实查）"))
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertEqual(fetched["skip_reason"], "已在库（实查）")
        self.assertEqual(self.repo.list_due(), [])
        second, created = self.repo.enqueue(_payload(pending_key="pk-tv-09-held"))
        self.assertTrue(created)

    def test_lease_expired_reclaimed(self):
        """租约过期回收：processing + lease 过期 → 回 queued。"""
        row = self._enqueue_tv()
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now - 10, now=now))
        self.assertEqual(self.repo.requeue_lease_expired(now=now), 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["lease_until"], 0)

    def test_lease_not_expired_not_reclaimed(self):
        """租约未过期不回收：processing + lease 未到 → 保持 processing。"""
        row = self._enqueue_tv()
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now + 7200, now=now))
        self.assertEqual(self.repo.requeue_lease_expired(now=now), 0)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "processing")

    def test_list_due_and_counts(self):
        """list_due 只给已到期行；count_by_status 按状态计数。"""
        self._enqueue_tv()
        self._enqueue_tv(episode=10, file_id="f-tv-10", file_name="多喜一家人 {tmdb-294498} - S01E10 - 第10集.mkv", pending_key="pk-tv-10")
        self.assertEqual(len(self.repo.list_due()), 2)
        counts = self.repo.count_by_status()
        self.assertEqual(counts.get("queued"), 2)

    def test_requeue_and_delete_by_ids(self):
        """放回/删除：held 行可放回 queued；删除只删行。"""
        row = self._enqueue_tv()
        self.repo.mark_held(row["id"], "待人工复核")
        self.assertEqual(self.repo.requeue_by_ids([row["id"]]), 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)
        self.assertEqual(self.repo.delete_by_ids([row["id"]]), 1)
        self.assertIsNone(self.repo.get_by_id(row["id"]))

    def test_get_by_pending_key_active_only(self):
        """按 pending_key 查行：done 后查不到（只认在途行）。"""
        row = self._enqueue_tv()
        self.assertEqual(self.repo.get_by_pending_key("pk-tv-09")["id"], row["id"])
        self.repo.mark_done(row["id"])
        self.assertIsNone(self.repo.get_by_pending_key("pk-tv-09"))


class FunnelEnqueueTests(_DbMixin):
    def test_enqueue_from_pending_creates_row(self):
        """手动提交入队：TV 项写一行，字段齐全，返回新建。"""
        item = _tv_item()
        row, created = self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.assertTrue(created)
        self.assertEqual(row["media_type"], "tv")
        self.assertEqual(row["tmdb_id"], _TV_TMDB_ID)
        self.assertEqual(row["season"], 1)
        self.assertEqual(row["episode"], 9)
        self.assertEqual(row["file_id"], "f-tv-09")
        self.assertEqual(row["status"], "queued")

    def test_enqueue_duplicate_returns_existing_row(self):
        """重复入队：同集第二份返回旧行，不新建。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        row, created = self.service.enqueue_from_pending(
            _tv_item(file_id="f-tv-09-b"), "pk-tv-09-b", _tv_media()
        )
        self.assertFalse(created)
        self.assertEqual(row["pending_key"], "pk-tv-09")

    def test_enqueue_store_unavailable_fails_open(self):
        """存储不可用时返回 (None, False)：调用方按旧流水线继续。"""
        broken_owner = _FakeOwner(self.store, self.registry)
        broken_owner._plugin = SimpleNamespace(
            _get_data_store=lambda: (_ for _ in ()).throw(RuntimeError("store down")),
            _scheduler_registry=lambda: self.registry,
        )
        service = FunnelService(broken_owner)
        row, created = service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.assertIsNone(row)
        self.assertFalse(created)

    def test_enqueue_movie_row(self):
        """电影入队：media_type=movie，season/episode 记 0。"""
        item = _tv_item(
            file_name="测试电影 (2026).mkv", staging_name="测试电影 (2026).mkv",
            season=0, episode=0, subscribe_id=0, success_episodes=[], notification_episodes=[],
        )
        row, created = self.service.enqueue_from_pending(item, "pk-mv-1", _movie_media())
        self.assertTrue(created)
        self.assertEqual(row["media_type"], "movie")
        self.assertEqual((row["season"], row["episode"]), (0, 0))


class FunnelPrepareTests(_DbMixin):
    def _row(self, **overrides):
        return self._enqueue_tv(**overrides)

    def test_already_canonical_no_rename(self):
        """文件已在规范名下：不改名，直接通过。"""
        row = self._row()
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        ok, reason = self.service._prepare_row(row, _tv_item())
        self.assertTrue(ok)
        self.assertEqual(reason, "")
        self.assertEqual(self.owner._cloud_drive.calls, [])

    def test_rename_called_before_organize_inputs(self):
        """暂存名文件：整理前改名，改名只调一次。"""
        row = self._row()
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_STAGING: {"file_id": "f-tv-09"}}
        ok, reason = self.service._prepare_row(row, _tv_item())
        self.assertTrue(ok)
        self.assertEqual(len(self.owner._cloud_drive.calls), 1)
        dir_path, file_obj, target = self.owner._cloud_drive.calls[0]
        self.assertEqual(dir_path, _TV_CLOUD_DIR)
        self.assertEqual(file_obj, {"file_id": "f-tv-09"})
        self.assertEqual(target, _TV_FILE)

    def test_rename_failure_blocks_organize(self):
        """改名失败：返回失败，不进整理（驱动不调 trigger）。"""
        row = self._row()
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_STAGING: {"file_id": "f-tv-09"}}
        self.owner._cloud_drive.ok = False
        ok, reason = self.service._prepare_row(row, _tv_item())
        self.assertFalse(ok)
        self.assertIn("改名失败", reason)
        self.assertEqual(self.owner.mp_organize.calls, [])

    def test_mp_organize_identity_skips_rename(self):
        """已有 MP 整理身份：不改名（MP 已接手）。"""
        row = self._row()
        item = _tv_item(mp_organize={"task_id": "t1"})
        ok, reason = self.service._prepare_row(row, item)
        self.assertTrue(ok)
        self.assertEqual(self.owner.postprocess.snapshot_calls, [])
        self.assertEqual(self.owner._cloud_drive.calls, [])

    def test_missing_file_reports_failure(self):
        """目录可读但两名都不在：失败，不进整理。"""
        row = self._row()
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {"其它文件.mkv": {"file_id": "x"}}
        ok, reason = self.service._prepare_row(row, _tv_item())
        self.assertFalse(ok)
        self.assertIn("未找到文件", reason)

    def test_unreadable_target_dir_reports_failure(self):
        """目标目录暂不可读：失败重试，不进整理。"""
        row = self._row()
        ok, reason = self.service._prepare_row(row, _tv_item())
        self.assertFalse(ok)
        self.assertIn("暂不可读", reason)


class FunnelPreCheckTests(_DbMixin):
    def _check(self, versions, item_extra=None, row_extra=None, **service_overrides):
        row = self._enqueue_tv(**(row_extra or {}))
        item = _tv_item(**(item_extra or {}))
        self.owner.episode_dedup = _FakeDedup(versions=versions)
        for key, value in service_overrides.items():
            setattr(self.owner, key, value)
        return self.service._pre_organize_check(row, item, _tv_media())

    def test_other_version_tagged_name_intercepts(self):
        """落盘前实查双名拦截：库里已有带 {tmdb-} 标记的另一份 → 拦截。"""
        other = {
            "file_id": "f-other",
            "file_name": "多喜一家人 {tmdb-294498} - S01E09 - 第9集.mkv",
            "file_size": 1200000000,
        }
        ok, reason = self._check({9: [other]})
        self.assertFalse(ok)
        self.assertIn("已在库", reason)

    def test_other_version_untagged_name_intercepts(self):
        """双名另一侧：库里已有不带标记的另一份 → 同样拦截。"""
        other = {
            "file_id": "f-other",
            "file_name": "多喜一家人 - S01E09 - 第9集.mkv",
            "file_size": 1200000000,
        }
        ok, reason = self._check({9: [other]})
        self.assertFalse(ok)
        self.assertIn("已在库", reason)

    def test_own_file_only_passes(self):
        """库里只有本项自己的文件（同 file_id）→ 放行。"""
        own = {"file_id": "f-tv-09", "file_name": _TV_FILE, "file_size": _TV_SIZE}
        ok, reason = self._check({9: [own]})
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_lookup_failure_fail_open(self):
        """查不到 fail-open：实查抛异常时放行，不挡整理。"""
        dedup = _FakeDedup(fail_collect=True)
        ok, reason = self._check({}, episode_dedup=dedup)
        self.assertTrue(ok)

    def test_dedup_off_fail_open(self):
        """判重关闭时不实查，直接放行。"""
        dedup = _FakeDedup(active=False)
        other = {"file_id": "f-other", "file_name": _TV_FILE, "file_size": 1}
        ok, _ = self._check({9: [other]}, episode_dedup=dedup)
        self.assertTrue(ok)

    def test_upgrade_old_version_only_passes(self):
        """洗版项落盘前实查：库里只有旧版时放行（旧版按 replaced_from /
        upgrade_old_* 除外；v1.29.10 热修复：洗版不再跳过库检查，
        只把旧版除外，另一份新版仍拦截）。"""
        other = {"file_id": "f-old", "file_name": _TV_FILE, "file_size": 1}
        ok, _ = self._check(
            {9: [other]},
            item_extra={
                "upgrade": True,
                "upgrade_old_file_id": "f-old",
                "upgrade_old_file_name": _TV_FILE,
            },
            row_extra={
                "is_upgrade": True,
                "replaced_from": [dict(other)],
            },
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.owner.episode_dedup.collect_calls), 1)
        self.assertTrue(self.owner.episode_dedup.collect_calls[0][3])

    def test_upgrade_other_new_version_intercepts(self):
        """洗版项落盘前实查：旧版之外还有另一份新版时拦截。"""
        old = {"file_id": "f-old", "file_name": _TV_FILE, "file_size": 1}
        new = {
            "file_id": "f-new",
            "file_name": "多喜一家人 {tmdb-294498} - S01E09 - 第9集 2160p.mkv",
            "file_size": 2000000000,
        }
        ok, reason = self._check(
            {9: [old, new]},
            item_extra={
                "upgrade": True,
                "upgrade_old_file_id": "f-old",
                "upgrade_old_file_name": _TV_FILE,
            },
            row_extra={
                "is_upgrade": True,
                "replaced_from": [dict(old)],
            },
        )
        self.assertFalse(ok)
        self.assertIn("已在库", reason)

    def test_strict_false_ignores_different_name(self):
        """排障模式（strict=False）：不同名的另一份不算重复。"""
        self.owner._funnel_dedup_strict = False
        other = {"file_id": "f-other", "file_name": "别的剧 - S01E09.mkv", "file_size": 1}
        ok, _ = self._check({9: [other]})
        self.assertTrue(ok)

    def test_strict_false_same_name_still_intercepts(self):
        """排障模式：同名不同 file_id 仍是另一份 → 拦截。"""
        self.owner._funnel_dedup_strict = False
        other = {"file_id": "f-other", "file_name": _TV_FILE, "file_size": 1}
        ok, reason = self._check({9: [other]})
        self.assertFalse(ok)
        self.assertIn("已在库", reason)


class FunnelRoundTests(_DbMixin):
    def _run_happy_path(self, service=None):
        service = service or self.service
        item = self._pending_item()
        row, created = service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.assertTrue(created)
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.postprocess.ctx = _ctx({"pk-tv-09": item})
        return row

    def test_round_happy_path_enqueue_to_terminal(self):
        """端到端：入队→整理→终态；改名在前、MP 整理恰好触发一次、行终态。"""
        row = self._run_happy_path()
        stats = self.service.run_round()
        self.assertEqual(stats["processed"], 1)
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(self.owner.mp_organize.calls[0][1], _TV_FILE)
        self.assertEqual(self.owner.postprocess.process_calls, 1)
        # 复核修补 1（第二轮）：worker 驱动必须带 funnel_drive=True，
        # 否则真实 _process_due_items 的跳过会把行拦住（死锁）。
        self.assertEqual(self.owner.postprocess.funnel_drive_calls, [True])
        # MP 整理身份写回 pending 记录，供驱动轮询用。
        self.assertEqual(
            self.owner._pending["pk-tv-09"].get("mp_organize"), {}
        )

    def test_round_intercepts_duplicate_and_deletes_nothing(self):
        """落盘前实查拦截：第二份不进整理、不写终态、不删任何文件。"""
        item = self._pending_item()
        row, _ = self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-other",
            "file_name": "多喜一家人 {tmdb-294498} - S01E09 - 第9集.mkv",
            "file_size": 1200000000,
        }]})
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "done")
        self.assertIn("已在库", fetched["skip_reason"])
        self.assertEqual(self.owner.mp_organize.calls, [])
        self.assertEqual(self.owner._cloud_drive.calls, [])
        self.assertNotIn("pk-tv-09", self.owner._pending)

    def test_round_rename_failure_marks_failed_with_backoff(self):
        """改名失败：行记失败（attempts+1，退避 10 秒），不进整理。"""
        item = self._pending_item()
        row, _ = self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_STAGING: {"file_id": "f-tv-09"}}
        self.owner._cloud_drive.ok = False
        before = int(time.time())
        stats = self.service.run_round()
        self.assertEqual(stats["failed"], 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "failed")
        self.assertEqual(fetched["attempts"], 1)
        self.assertGreaterEqual(fetched["next_retry_at"], before + 10)
        self.assertIn("改名失败", fetched["last_error"])
        self.assertEqual(self.owner.mp_organize.calls, [])

    def test_round_failed_five_times_holds(self):
        """失败 5 次后挂起：attempts 累到 5 次即 held，不再自动重试。"""
        # 用仓储模块已绑定的 update / 模型类：测试内再 from sqlalchemy
        # import 会拿到另一代 sqlalchemy 对象，与仓储的模型类混用抛
        # ArgumentError（见文件头 harness 说明）。
        update = repositories_module.update
        FunnelQueueRow = models_module.FunnelQueueRow

        item = self._pending_item()
        row, _ = self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_STAGING: {"file_id": "f-tv-09"}}
        self.owner._cloud_drive.ok = False
        for round_no in range(5):
            stats = self.service.run_round()
            fetched = self.repo.get_by_id(row["id"])
            if round_no < 4:
                self.assertEqual(stats["failed"], 1)
                self.assertEqual(fetched["status"], "failed")
                self.assertEqual(fetched["attempts"], round_no + 1)
                # 下一轮：只复位状态与到期时间，保留 attempts（requeue_by_ids
                # 会清零 attempts，是人工放回的语义，这里不用它）。
                with self.manager.session() as db:
                    db.execute(
                        update(FunnelQueueRow)
                        .where(FunnelQueueRow.id == row["id"])
                        .values(status="queued", next_retry_at=0, lease_until=0)
                    )
                    db.commit()
            else:
                self.assertEqual(stats["held"], 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "held")
        self.assertEqual(self.repo.list_due(), [])

    def test_lease_expired_row_reclaimed_and_rerun(self):
        """断点续跑：processing 行租约过期后回收重跑，终态 done。"""
        row = self._run_happy_path()
        now = int(time.time())
        self.assertTrue(self.repo.claim(row["id"], now - 10, now=now))
        stats = self.service.run_round()
        self.assertEqual(stats["done"], 1)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "done")

    def test_restart_keeps_queue_rows(self):
        """重启后续跑：新服务实例用同一库，队列行不丢、照常整理。"""
        row = self._run_happy_path()
        resumed = FunnelService(self.owner)
        stats = resumed.run_round()
        self.assertEqual(stats["done"], 1)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "done")

    def test_terminal_committed_exactly_once(self):
        """终态只提交一次：第二轮无到期行，MP 整理不再触发。"""
        self._run_happy_path()
        self.service.run_round()
        stats = self.service.run_round()
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(len(self.owner.mp_organize.calls), 1)
        self.assertEqual(self.owner.postprocess.process_calls, 1)

    def test_stop_flag_halts_round(self):
        """停止按钮：stop 标志置位后本轮不处理任何行。"""
        row = self._run_happy_path()
        self.owner._funnel_stop_requested = True
        stats = self.service.run_round()
        self.assertEqual(stats["processed"], 0)
        self.assertEqual(self.owner.mp_organize.calls, [])
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")

    def test_missing_pending_record_marks_held(self):
        """待终态记录已不在：v1.29.10 热修复（评审 B3）——行转 held 待人工
        核对，绝不直接终态（行终态会把“没入库的集”当成已完成结算掉）。"""
        row, _ = self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        stats = self.service.run_round()
        self.assertEqual(stats["held"], 1)
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "held")
        self.assertEqual(self.owner.mp_organize.calls, [])

    def test_unsettled_row_released_without_attempt_cost(self):
        """未结算行：驱动未终态时放回 queued，不耗 attempts。"""
        item = self._pending_item()
        row, _ = self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        ctx = _ctx({"pk-tv-09": item})
        self.owner.postprocess.ctx = ctx
        self.owner.postprocess._process_due_items = lambda c, funnel_drive=False: None
        stats = self.service.run_round()
        self.assertEqual(stats["processed"], 1)
        fetched = self.repo.get_by_id(row["id"])
        self.assertEqual(fetched["status"], "queued")
        self.assertEqual(fetched["attempts"], 0)


class FunnelWorkerTests(_DbMixin):
    def test_ensure_worker_creates_job_when_rows_queued(self):
        """入队后起轮：有待处理行时建 worker job。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.service.ensure_worker()
        job = self.registry.get_job(FUNNEL_WORKER_JOB_ID)
        self.assertIsNotNone(job)
        self.assertEqual(job["func"], self.service.run_round)

    def test_ensure_worker_no_job_when_switch_off(self):
        """kill-switch 关：不建 job，队列行保留。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.owner._funnel_phase1_enabled = False
        self.service.ensure_worker()
        self.assertIsNone(self.registry.get_job(FUNNEL_WORKER_JOB_ID))
        self.assertEqual(self.repo.count_by_status().get("queued"), 1)

    def test_worker_removed_when_queue_empty(self):
        """队列空即停 job：终态后 worker 移除。"""
        item = self._pending_item()
        self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.postprocess.ctx = _ctx({"pk-tv-09": item})
        self.service.ensure_worker()
        self.assertIsNotNone(self.registry.get_job(FUNNEL_WORKER_JOB_ID))
        self.service.run_round()
        self.assertIsNone(self.registry.get_job(FUNNEL_WORKER_JOB_ID))

    def test_run_now_clears_stop_and_runs_round(self):
        """“立即整理一轮”：清 stop 标志并跑一轮。"""
        item = self._pending_item()
        self.service.enqueue_from_pending(item, "pk-tv-09", _tv_media())
        self.owner.postprocess.snapshots[_TV_CLOUD_DIR] = {_TV_FILE: {"file_id": "f-tv-09"}}
        self.owner.postprocess.ctx = _ctx({"pk-tv-09": item})
        self.owner._funnel_stop_requested = True
        result = self.service.run_now()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["done"], 1)
        self.assertFalse(self.owner._funnel_stop_requested)

    def test_request_stop_sets_flag_and_removes_job(self):
        """停止按钮：置 stop 标志、移除 worker job。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.service.ensure_worker()
        self.assertIsNotNone(self.registry.get_job(FUNNEL_WORKER_JOB_ID))
        result = self.service.request_stop()
        self.assertTrue(result["success"])
        self.assertTrue(self.owner._funnel_stop_requested)
        self.assertIsNone(self.registry.get_job(FUNNEL_WORKER_JOB_ID))


class FunnelPrecommitTests(_DbMixin):
    def _recheck(self, item, pending_key, media=None, ctx=None):
        return self.service.precommit_recheck(
            item, pending_key, media or _tv_media(), ctx
        )

    def test_inflight_other_row_intercepts(self):
        """提交前复查：在途登记有同集另一行 → 拦截。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        intercepted, reason = self._recheck(_tv_item(), "pk-tv-09-b")
        self.assertTrue(intercepted)
        self.assertIn("在途", reason)

    def test_own_row_does_not_intercept(self):
        """提交前复查：只有本项自己的在途行 → 放行。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        intercepted, reason = self._recheck(_tv_item(), "pk-tv-09")
        self.assertFalse(intercepted)
        self.assertEqual(reason, "")

    def test_library_other_copy_intercepts(self):
        """提交前复查：库里已有另一份同集文件 → 拦截。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-other",
            "file_name": "多喜一家人 - S01E09 - 第9集.mkv",
            "file_size": 1200000000,
        }]})
        intercepted, reason = self._recheck(_tv_item(), "pk-tv-09")
        self.assertTrue(intercepted)
        self.assertIn("媒体库已有另一份", reason)

    def test_upgrade_old_version_only_passes(self):
        """提交前复查：洗版项库里只有旧版时放行（旧版按 replaced_from 除外；
        v1.29.10 热修复：洗版不再跳过库检查）。"""
        self.service.enqueue_from_pending(
            _tv_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_TV_FILE,
            ),
            "pk-tv-09", _tv_media(),
        )
        self.owner.episode_dedup = _FakeDedup(versions={9: [{
            "file_id": "f-old",
            "file_name": _TV_FILE,
            "file_size": 1,
        }]})
        intercepted, _ = self._recheck(
            _tv_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_TV_FILE,
            ),
            "pk-tv-09",
        )
        self.assertFalse(intercepted)

    def test_upgrade_other_new_version_intercepts(self):
        """提交前复查：洗版项旧版之外还有另一份新版时拦截。"""
        self.service.enqueue_from_pending(
            _tv_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_TV_FILE,
            ),
            "pk-tv-09", _tv_media(),
        )
        self.owner.episode_dedup = _FakeDedup(versions={9: [
            {"file_id": "f-old", "file_name": _TV_FILE, "file_size": 1},
            {
                "file_id": "f-new",
                "file_name": "多喜一家人 {tmdb-294498} - S01E09 - 第9集 2160p.mkv",
                "file_size": 2000000000,
            },
        ]})
        intercepted, reason = self._recheck(
            _tv_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_TV_FILE,
            ),
            "pk-tv-09",
        )
        self.assertTrue(intercepted)
        self.assertIn("媒体库已有另一份", reason)

    def test_lookup_failure_fail_open(self):
        """提交前复查：实查异常 fail-open，不挡提交。"""
        self.service.enqueue_from_pending(_tv_item(), "pk-tv-09", _tv_media())
        self.owner.episode_dedup = _FakeDedup(fail_collect=True)
        intercepted, _ = self._recheck(_tv_item(), "pk-tv-09")
        self.assertFalse(intercepted)

    def test_no_row_no_media_fail_open(self):
        """提交前复查：无漏斗行、无媒体信息时放行（旧流水线不受影响）。"""
        intercepted, _ = self.service.precommit_recheck(_tv_item(), "pk-none", None, None)
        self.assertFalse(intercepted)


class FunnelCommitInterceptTests(unittest.TestCase):
    """_commit_single_item 入口的提交前复查（真实 PostprocessService 类）。"""

    def _service(self, precommit):
        service = PostprocessService.__new__(PostprocessService)
        owner = SimpleNamespace()
        owner.funnel = SimpleNamespace(precommit_recheck=precommit)
        held = []
        owner.funnel_mark_held_by_pending_key = lambda key, reason: held.append((key, reason))
        object.__setattr__(service, "_owner", owner)
        return service, owner, held

    def test_intercept_calls_abort_without_notify_and_no_terminal(self):
        """拦截：走 abort（notify=False）、挂起漏斗行、不写终态。"""
        reason = "提交前复查发现重复，已拦截：测试"
        service, owner, held = self._service(
            lambda item, key, media, ctx: (True, reason)
        )
        item = _tv_item()
        ctx = _ctx({"pk-tv-09": item})
        with (
            patch.object(PostprocessService, "_abort_single_item") as abort,
            patch.object(PostprocessService, "_update_postprocess_progress") as progress,
        ):
            service._commit_single_item(item, "pk-tv-09", None, _tv_media(), {}, ctx)
        abort.assert_called_once()
        args, kwargs = abort.call_args
        self.assertEqual(args[0], item)
        self.assertEqual(args[1], "pk-tv-09")
        self.assertEqual(args[2], reason)
        self.assertIs(args[3], ctx)
        self.assertFalse(kwargs.get("notify", True))
        self.assertEqual(held, [("pk-tv-09", reason)])
        progress.assert_not_called()

    def test_recheck_exception_fails_open_to_commit(self):
        """复查异常 fail-open：继续走终态提交，不拦截。"""
        service, owner, held = self._service(
            lambda item, key, media, ctx: (_ for _ in ()).throw(RuntimeError("recheck down"))
        )
        item = _tv_item()
        ctx = _ctx({"pk-tv-09": item})
        with (
            patch.object(PostprocessService, "_abort_single_item") as abort,
            patch.object(
                PostprocessService, "_update_postprocess_progress",
                side_effect=RuntimeError("reached-commit-body"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "reached-commit-body"):
                service._commit_single_item(item, "pk-tv-09", None, _tv_media(), {}, ctx)
        abort.assert_not_called()
        self.assertEqual(held, [])


class FunnelQueueBatchRoutingTests(unittest.TestCase):
    """_queue_file_finalize_batch：funnel_route 标记透传 + 入队调用。

    复用 test_upgrade_replace_step_1_29_1.py 的 SyncHandler.__new__ 模式。
    """

    def _handler(self):
        store = {}
        handler = SyncHandler.__new__(SyncHandler)
        handler._get_data = lambda key: store.get(key, {})
        handler._save_data = lambda key, data: store.__setitem__(key, data)
        handler._offline_pending_lock = threading.Lock()
        handler._offline_pending_changed = None
        handler._organize_after_transfer = True
        handler._cloud_drive = SimpleNamespace(key="115")
        handler._upgrade_mode = "replace"
        handler._is_cloud_resource_url = lambda url: False
        handler._is_direct_cloud_resource_url = lambda url: False
        materialize_handler_fields(handler, "resource_transfer")
        migrate_handler_stubs(handler)
        return handler, store

    def _item(self, **extra):
        item = {
            "result_key": "single",
            "share_url": "https://115.com/s/test",
            "cloud_dir": "/d",
            "file_name": "多喜一家人 - S01E09 - 第9集.mkv",
            "staging_dir": "/d",
            "staging_name": "多喜一家人 - S01E09 - 第9集.mkv",
            "source_sha1": "",
            "file_size": 900,
            "success_episodes": [9],
            "notification_episodes": [9],
            "subtitles": [],
            "skip_history": False,
            "mp_organize": None,
        }
        item.update(extra)
        return item

    def test_funnel_route_item_enqueues(self):
        """funnel_route=True 的项：调入队、pending 记录带标记。"""
        handler, store = self._handler()
        calls = []
        handler.enqueue_funnel_from_pending = lambda *args, **kwargs: calls.append((args, kwargs))
        result = handler._queue_file_finalize_batch(
            [self._item(funnel_route=True)], None, season=1, sub_key=""
        )
        pending_key = result.get("single", "")
        self.assertTrue(pending_key)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][1], pending_key)
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        self.assertTrue(record.get("funnel_route"))

    def test_old_pipeline_item_does_not_enqueue(self):
        """无 funnel_route 的项：不调入队，走旧流水线。"""
        handler, store = self._handler()
        calls = []
        handler.enqueue_funnel_from_pending = lambda *args, **kwargs: calls.append((args, kwargs))
        result = handler._queue_file_finalize_batch(
            [self._item()], None, season=1, sub_key=""
        )
        pending_key = result.get("single", "")
        self.assertTrue(pending_key)
        self.assertEqual(calls, [])
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        self.assertFalse(record.get("funnel_route"))


class FunnelConfigTests(unittest.TestCase):
    """_init_funnel_flags：6 个阶段 1 配置键的默认值与夹取。"""

    def _flags(self, values):
        fake_self = SimpleNamespace()
        SyncHandler._init_funnel_flags(
            fake_self, lambda key, default: values.get(key, default)
        )
        return fake_self

    def test_defaults(self):
        """全缺省：开关关、目标目录为现有资源目录、立即触发、租约 2 小时、5 次、双名实查。"""
        flags = self._flags({})
        self.assertFalse(flags._funnel_phase1_enabled)
        self.assertEqual(flags._funnel_target_dir, "/Alist/影视/moviepilot/转存/")
        self.assertEqual(flags._funnel_trigger, "immediate")
        self.assertEqual(flags._funnel_inflight_lease_seconds, 7200)
        self.assertEqual(flags._funnel_max_attempts, 5)
        self.assertTrue(flags._funnel_dedup_strict)

    def test_values_and_clamps(self):
        """越界值夹取：租约 60–86400 秒、次数 1–20 次；非法值回默认。"""
        flags = self._flags({
            "funnel_phase1_enabled": True,
            "funnel_target_dir": "/Alist/影视/moviepilot/转存/剧集/",
            "funnel_trigger": "IMMEDIATE",
            "funnel_inflight_lease_seconds": 10,
            "funnel_max_attempts": 99,
            "funnel_dedup_strict": False,
        })
        self.assertTrue(flags._funnel_phase1_enabled)
        self.assertEqual(flags._funnel_target_dir, "/Alist/影视/moviepilot/转存/剧集/")
        self.assertEqual(flags._funnel_trigger, "immediate")
        self.assertEqual(flags._funnel_inflight_lease_seconds, 60)
        self.assertEqual(flags._funnel_max_attempts, 20)
        self.assertFalse(flags._funnel_dedup_strict)
        bad = self._flags({
            "funnel_inflight_lease_seconds": "not-a-number",
            "funnel_max_attempts": None,
        })
        self.assertEqual(bad._funnel_inflight_lease_seconds, 7200)
        self.assertEqual(bad._funnel_max_attempts, 5)


class FunnelMoviePreCheckTests(_DbMixin):
    """v1.29.7 阶段 1 修补 1：电影落盘前实查（_pre_organize_check 电影分支）。"""

    def setUp(self):
        super().setUp()
        _FAKE_SUBSCRIBES[445] = SimpleNamespace(
            id=445, tmdbid=_MOVIE_TMDB_ID, name=_MOVIE_TITLE, season=1, type="电影",
        )

    def _check(self, movie_versions, item_extra=None, row_extra=None, **overrides):
        row, created = self.repo.enqueue(_movie_payload(**(row_extra or {})))
        self.assertTrue(created)
        item = _movie_item(**(item_extra or {}))
        self.owner.episode_dedup = _FakeDedup(movie_versions=movie_versions)
        for key, value in overrides.items():
            setattr(self.owner, key, value)
        return self.service._pre_organize_check(row, item, _movie_media())

    def test_other_version_different_name_intercepts(self):
        """电影目标目录已有另一份不同名文件 → 拦截，原因就是“已在库（实查）”。"""
        other = {
            "file_id": "f-other",
            "file_name": "测试电影 (2026).mkv",
            "file_size": 4000000000,
        }
        ok, reason = self._check([other])
        self.assertFalse(ok)
        self.assertEqual(reason, "已在库（实查）")

    def test_other_version_same_name_intercepts(self):
        """电影目标目录已有同名不同 file_id 的另一份 → 拦截。"""
        other = {
            "file_id": "f-other",
            "file_name": _MOVIE_FILE,
            "file_size": 4000000000,
        }
        ok, reason = self._check([other])
        self.assertFalse(ok)
        self.assertEqual(reason, "已在库（实查）")

    def test_own_file_only_passes(self):
        """电影目标目录只有本项自己的文件（同 file_id）→ 放行。"""
        own = {
            "file_id": "f-mv-01",
            "file_name": _MOVIE_FILE,
            "file_size": _MOVIE_SIZE,
        }
        ok, reason = self._check([own])
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_subscribe_missing_fail_open(self):
        """订阅解析不到时 fail-open：即使库里有另一份也不拦截。"""
        other = {
            "file_id": "f-other",
            "file_name": "测试电影 (2026).mkv",
            "file_size": 4000000000,
        }
        ok, _ = self._check([other], item_extra={"subscribe_id": 0})
        self.assertTrue(ok)

    def test_upgrade_old_version_only_passes(self):
        """洗版电影项落盘前实查：库里只有旧版时放行（旧版按 replaced_from
        除外；v1.29.10 热修复：洗版不再跳过库检查）。"""
        other = {"file_id": "f-old", "file_name": _MOVIE_FILE, "file_size": 1}
        ok, _ = self._check(
            [other],
            item_extra={
                "upgrade": True,
                "upgrade_old_file_id": "f-old",
                "upgrade_old_file_name": _MOVIE_FILE,
            },
            row_extra={
                "is_upgrade": True,
                "replaced_from": [dict(other)],
            },
        )
        self.assertTrue(ok)
        self.assertEqual(len(self.owner.episode_dedup.movie_calls), 1)

    def test_upgrade_other_new_version_intercepts(self):
        """洗版电影落盘前实查：旧版之外还有另一份新版时拦截。"""
        old = {"file_id": "f-old", "file_name": _MOVIE_FILE, "file_size": 1}
        new = {
            "file_id": "f-new",
            "file_name": "测试电影 (2026) 2160p.mkv",
            "file_size": 6000000000,
        }
        ok, reason = self._check(
            [old, new],
            item_extra={
                "upgrade": True,
                "upgrade_old_file_id": "f-old",
                "upgrade_old_file_name": _MOVIE_FILE,
            },
            row_extra={
                "is_upgrade": True,
                "replaced_from": [dict(old)],
            },
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "已在库（实查）")

    def test_dedup_off_fail_open(self):
        """判重关闭时不实查，直接放行。"""
        dedup = _FakeDedup(active=False)
        other = {"file_id": "f-other", "file_name": _MOVIE_FILE, "file_size": 1}
        ok, _ = self._check([other], episode_dedup=dedup)
        self.assertTrue(ok)

    def test_lookup_failure_fail_open(self):
        """电影目录列取失败时 fail-open，不挡整理。"""
        dedup = _FakeDedup(fail_movie=True)
        ok, _ = self._check([], episode_dedup=dedup)
        self.assertTrue(ok)


class FunnelMoviePrecommitTests(_DbMixin):
    """v1.29.7 阶段 1 修补 1：提交前复查的电影分支。"""

    def setUp(self):
        super().setUp()
        _FAKE_SUBSCRIBES[445] = SimpleNamespace(
            id=445, tmdbid=_MOVIE_TMDB_ID, name=_MOVIE_TITLE, season=1, type="电影",
        )

    def _recheck(self, item, pending_key, media=None, ctx=None):
        return self.service.precommit_recheck(
            item, pending_key, media or _movie_media(), ctx
        )

    def test_library_other_copy_intercepts(self):
        """提交前复查：电影库已有另一份电影文件 → 拦截。"""
        self.service.enqueue_from_pending(
            _movie_item(), "pk-mv-01", _movie_media()
        )
        self.owner.episode_dedup = _FakeDedup(movie_versions=[{
            "file_id": "f-other",
            "file_name": "测试电影 (2026).mkv",
            "file_size": 4000000000,
        }])
        intercepted, reason = self._recheck(_movie_item(), "pk-mv-01")
        self.assertTrue(intercepted)
        self.assertIn("提交前复查发现重复", reason)
        self.assertIn("另一份电影", reason)

    def test_own_landed_file_does_not_intercept(self):
        """提交前复查：库里只有本项刚落盘的文件（同 file_id + 同名）→ 放行。"""
        self.service.enqueue_from_pending(
            _movie_item(), "pk-mv-01", _movie_media()
        )
        self.owner.episode_dedup = _FakeDedup(movie_versions=[{
            "file_id": "f-mv-01",
            "file_name": _MOVIE_FILE,
            "file_size": _MOVIE_SIZE,
        }])
        intercepted, reason = self._recheck(_movie_item(), "pk-mv-01")
        self.assertFalse(intercepted)
        self.assertEqual(reason, "")

    def test_upgrade_old_version_only_passes(self):
        """提交前复查：洗版电影项库里只有旧版时放行（旧版按
        replaced_from 除外；v1.29.10 热修复：洗版不再跳过库检查）。"""
        self.service.enqueue_from_pending(
            _movie_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_MOVIE_FILE,
            ),
            "pk-mv-01", _movie_media(),
        )
        self.owner.episode_dedup = _FakeDedup(movie_versions=[{
            "file_id": "f-old",
            "file_name": _MOVIE_FILE,
            "file_size": 1,
        }])
        intercepted, _ = self._recheck(
            _movie_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_MOVIE_FILE,
            ),
            "pk-mv-01",
        )
        self.assertFalse(intercepted)

    def test_upgrade_other_new_version_intercepts(self):
        """提交前复查：洗版电影项旧版之外还有另一份新版时拦截。"""
        self.service.enqueue_from_pending(
            _movie_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_MOVIE_FILE,
            ),
            "pk-mv-01", _movie_media(),
        )
        self.owner.episode_dedup = _FakeDedup(movie_versions=[
            {"file_id": "f-old", "file_name": _MOVIE_FILE, "file_size": 1},
            {
                "file_id": "f-new",
                "file_name": "测试电影 (2026) 2160p.mkv",
                "file_size": 6000000000,
            },
        ])
        intercepted, reason = self._recheck(
            _movie_item(
                upgrade=True,
                upgrade_old_file_id="f-old",
                upgrade_old_file_name=_MOVIE_FILE,
            ),
            "pk-mv-01",
        )
        self.assertTrue(intercepted)
        self.assertIn("另一份电影", reason)

    def test_subscribe_missing_fail_open(self):
        """提交前复查：电影订阅解析不到时 fail-open。"""
        self.service.enqueue_from_pending(
            _movie_item(), "pk-mv-01", _movie_media()
        )
        self.owner.episode_dedup = _FakeDedup(movie_versions=[{
            "file_id": "f-other",
            "file_name": "测试电影 (2026).mkv",
            "file_size": 4000000000,
        }])
        intercepted, _ = self._recheck(_movie_item(subscribe_id=0), "pk-mv-01")
        self.assertFalse(intercepted)

    def test_lookup_failure_fail_open(self):
        """提交前复查：电影目录列取失败时 fail-open。"""
        self.service.enqueue_from_pending(
            _movie_item(), "pk-mv-01", _movie_media()
        )
        self.owner.episode_dedup = _FakeDedup(fail_movie=True)
        intercepted, _ = self._recheck(_movie_item(), "pk-mv-01")
        self.assertFalse(intercepted)


class FunnelPreStrmRecheckTests(unittest.TestCase):
    """v1.29.7 阶段 1 修补 2：提交前复查提到 STRM 生成之前（落点测试）。

    用 DummyMpWaitService（test_finalize_mp_wait_1_12_6 的替身）驱动真实
    的 _organize_and_finalize_single_item 走到 STRM 步骤；漏斗复查用假替
    身，只验“拦截发生在 _generate_strm 之前”。
    """

    def setUp(self):
        self.service = DummyMpWaitService()
        self.abort_calls = []
        self.held = []
        self.generate_calls = []
        self.finalize_calls = []
        _stub(
            self.service, "_abort_single_item",
            lambda item, key, reason, ctx, notify=True: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason, "notify": notify}
            ),
        )
        _stub(
            self.service, "_generate_strm",
            lambda cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True: self.generate_calls.append(
                (cloud_dir, file_name)
            ) or Path("/tmp/x.strm"),
        )
        _stub(self.service, "_strm_file_ready", lambda path: True)
        _stub(
            self.service, "_finalize_after_metadata",
            lambda item, key, file_name, strm_path, media, media_data, ctx: (
                self.finalize_calls.append((key, strm_path))
            ),
        )
        _stub(
            self.service, "_restore_pending_media_context",
            lambda item, key: (None, {}),
        )
        dest_file = SimpleNamespace(
            name=_MP_FILE_NAME, id="f1", size=123456, sha1="ABCDEF",
        )
        self.service._snapshot_map = {
            _MP_DEST_PARENT: DirectoryFileIndex([dest_file]),
        }

    def _run(self, precommit):
        owner = self.service._owner
        owner.funnel = SimpleNamespace(precommit_recheck=precommit)
        owner.funnel_mark_held_by_pending_key = lambda key, reason: self.held.append(
            (key, reason)
        )
        tracker = FakeTracker(("succeeded", _MP_DEST_PATH, _MP_DEST_PATH))
        _stub(self.service, "_mp_organize_tracker", lambda: tracker)
        item = _mp_item(_MP_NOW - 600)
        pending = {"key1": item}
        ctx = _ctx(pending)
        self.service._organize_and_finalize_single_item(item, "key1", ctx)
        return item, pending, ctx

    def test_intercept_before_strm_generation(self):
        """拦截：STRM 未生成、abort（notify=False）、漏斗行挂起、未终态。"""
        reason = "提交前复查发现重复，已拦截：测试"
        self._run(lambda item, key, media, ctx: (True, reason))
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.abort_calls), 1)
        self.assertEqual(self.abort_calls[0]["key"], "key1")
        self.assertEqual(self.abort_calls[0]["reason"], reason)
        self.assertFalse(self.abort_calls[0]["notify"])
        self.assertEqual(self.held, [("key1", reason)])
        self.assertEqual(self.finalize_calls, [])

    def test_pass_through_generates_strm(self):
        """放行：STRM 生成、走到元数据后终态，不 abort、不挂起。"""
        self._run(lambda item, key, media, ctx: (False, ""))
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(self.held, [])
        self.assertEqual(len(self.finalize_calls), 1)
        self.assertEqual(self.finalize_calls[0][0], "key1")

    def test_recheck_exception_fails_open_to_strm(self):
        """复查异常 fail-open：STRM 正常生成，不拦截。"""
        def _boom(item, key, media, ctx):
            raise RuntimeError("recheck down")
        self._run(_boom)
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.abort_calls, [])


class FunnelPreStrmRealRecheckTests(_DbMixin):
    """v1.29.7 阶段 1 修补 2：真实 precommit_recheck 在 STRM 生成前拦截。

    漏斗行真实入队、判重用 _FakeDedup；_organize_and_finalize_single_item
    用 DummyMpWaitService 驱动到 STRM 步骤，验真实复查的拦截/放行。
    """

    def setUp(self):
        super().setUp()
        _FAKE_SUBSCRIBES[444] = SimpleNamespace(
            id=444, tmdbid=_TV_TMDB_ID, name=_TV_TITLE, season=1, type="电视剧",
        )

    def _drive(self, versions):
        mp = DummyMpWaitService()
        mp._owner.funnel = self.service
        held = []

        def _mark_held(pending_key, reason=""):
            held.append((pending_key, reason))
            return SyncHandler.funnel_mark_held_by_pending_key(
                SimpleNamespace(funnel=self.service), pending_key, reason
            )

        mp._owner.funnel_mark_held_by_pending_key = _mark_held
        self.abort_calls = []
        self.generate_calls = []
        self.finalize_calls = []
        _stub(
            mp, "_abort_single_item",
            lambda item, key, reason, ctx, notify=True: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason, "notify": notify}
            ),
        )
        _stub(
            mp, "_generate_strm",
            lambda cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True: self.generate_calls.append(
                (cloud_dir, file_name)
            ) or Path("/tmp/x.strm"),
        )
        _stub(mp, "_strm_file_ready", lambda path: True)
        _stub(
            mp, "_finalize_after_metadata",
            lambda item, key, file_name, strm_path, media, media_data, ctx: (
                self.finalize_calls.append((key, strm_path))
            ),
        )
        _stub(
            mp, "_restore_pending_media_context",
            lambda item, key: (_tv_media(), {}),
        )
        dest_file = SimpleNamespace(
            name=_TV_FILE, id="f-tv-09", size=_TV_SIZE, sha1="ABCDEF",
        )
        mp._snapshot_map = {
            _TV_CLOUD_DIR: DirectoryFileIndex([dest_file]),
        }
        tracker = FakeTracker(
            ("succeeded", _TV_CLOUD_DIR + "/" + _TV_FILE, _TV_CLOUD_DIR + "/" + _TV_FILE)
        )
        _stub(mp, "_mp_organize_tracker", lambda: tracker)
        self.owner.episode_dedup = _FakeDedup(versions=versions)
        item = _tv_item()
        item["mp_organize"] = {
            "src_path": _TV_CLOUD_DIR + "/" + _TV_STAGING,
            "src_storage": "local",
            "triggered_at": _MP_NOW - 600,
            "target_dir": "/媒体库/剧集",
            "target_storage": "u115cs",
        }
        pending = {"pk-tv-09": item}
        ctx = _ctx(pending)
        mp._organize_and_finalize_single_item(item, "pk-tv-09", ctx)
        return held

    def test_real_recheck_intercepts_second_copy_before_strm(self):
        """真实复查：库里已有另一份同集 → 拦截在 STRM 之前，漏斗行挂起。"""
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        other = {"file_id": "f-other", "file_name": _TV_FILE, "file_size": 1}
        held = self._drive({9: [other]})
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.abort_calls), 1)
        self.assertFalse(self.abort_calls[0]["notify"])
        self.assertIn("另一份同集", self.abort_calls[0]["reason"])
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][0], "pk-tv-09")
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )
        self.assertEqual(self.finalize_calls, [])

    def test_real_recheck_own_landed_file_generates_strm(self):
        """真实复查：库里只有本项刚落盘的文件（同 file_id + 同名）→ 放行生成 STRM。

        锁死修补 2 的关键前提：MP 整理落盘后 item["file_name"] 已同步成
        真实落盘名，115 内移动不改 file_id，本项文件能被正确排除。
        """
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        own = {"file_id": "f-tv-09", "file_name": _TV_FILE, "file_size": _TV_SIZE}
        held = self._drive({9: [own]})
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(held, [])
        self.assertEqual(len(self.finalize_calls), 1)
        self.assertEqual(self.finalize_calls[0][0], "pk-tv-09")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")


class FunnelOfflineMonitorCountTests(unittest.TestCase):
    """v1.29.7 阶段 1 修补 4：离线监控 job 只为非漏斗待终态行服务。"""

    def _handler(self):
        handler = SyncHandler.__new__(SyncHandler)
        handler._funnel_phase1_enabled = True
        store = {}
        handler._get_data = lambda key: store.get(key, {})
        handler._save_data = lambda key, data: store.__setitem__(key, data)
        handler._organize_after_transfer = True
        handler._cloud_drive = None
        handler._upgrade_mode = "largest"
        handler._offline_pending_lock = threading.Lock()
        handler._offline_pending_changed = None
        handler._config = {}
        handler._funnel_target_dir = _TARGET_DIR
        handler._funnel_inflight_lease_seconds = 7200
        handler._funnel_max_attempts = 5
        handler._funnel_dedup_strict = True
        handler._is_cloud_resource_url = lambda url: False
        handler._is_direct_cloud_resource_url = lambda url: False
        materialize_handler_fields(handler)
        migrate_handler_stubs(handler)
        handler._funnel_phase1_enabled = True
        return handler

    def _item(self, key, funnel_route=False):
        # 每项用不同的 file_name/result_key：pending_key 由
        # share_url+cloud_dir+file_name 派生，同名会撞键并继承旧 funnel_route。
        return {
            "pending_key": key,
            "funnel_route": funnel_route,
            "share_url": "https://115.com/s/abcdef",
            "result_key": "f-tv-%s" % key,
            "cloud_dir": _TV_CLOUD_DIR,
            "staging_dir": _TV_CLOUD_DIR,
            "file_name": "多喜一家人 {tmdb-294498} - S01E09 - 第9集-%s.mkv" % key,
            "staging_name": _TV_STAGING,
            "file_size": _TV_SIZE,
            "media_data": {"tmdb_id": _TV_TMDB_ID, "type": "电视剧", "title": _TV_TITLE},
            "mediainfo": None,
            "subscribe_id": 444,
            "media_type": "tv",
            "tmdb_id": _TV_TMDB_ID,
            "season": 1,
            "episode": 9,
            "source": "manual",
        }

    def test_only_funnel_rows_switch_on_counts_zero(self):
        """只有漏斗行待终态且开关开启时，计数为 0（job 不建/停）。"""
        handler = self._handler()
        pending = {"a": {"funnel_route": True}, "b": {"funnel_route": True}}
        self.assertEqual(handler.count_offline_monitor_pending(pending), 0)

    def test_mixed_rows_count_only_non_funnel(self):
        """漏斗行与非漏斗行混待时，只计非漏斗行。"""
        handler = self._handler()
        pending = {
            "a": {"funnel_route": True},
            "b": {"funnel_route": False},
            "c": {},
        }
        self.assertEqual(handler.count_offline_monitor_pending(pending), 2)

    def test_switch_off_counts_funnel_rows(self):
        """开关关闭时漏斗行由旧流水线处理，计入计数（job 正常建）。"""
        handler = self._handler()
        handler._funnel_phase1_enabled = False
        pending = {"a": {"funnel_route": True}, "b": {"funnel_route": True}}
        self.assertEqual(handler.count_offline_monitor_pending(pending), 2)

    def test_list_input_shape(self):
        """get_pending_finalize_tasks 返回的列表形状同样计数。"""
        handler = self._handler()
        pending = [
            {"pending_key": "a", "funnel_route": True},
            {"pending_key": "b", "funnel_route": False},
        ]
        self.assertEqual(handler.count_offline_monitor_pending(pending), 1)

    def test_queue_batch_notifies_filtered_count(self):
        """_queue_file_finalize_batch 通知的是过滤后计数。

        只排漏斗项 → 通知 0；再排非漏斗项 → 通知 1；开关关闭再排漏斗项 →
        三行都计入 → 通知 3。
        """
        handler = self._handler()
        notified = []
        handler._offline_pending_changed = lambda count: notified.append(count)
        handler.enqueue_funnel_from_pending = lambda item, key, media: None
        handler._queue_file_finalize_batch(
            [self._item("k1", funnel_route=True)], None, season=1, sub_key=""
        )
        self.assertEqual(notified, [0])
        handler._queue_file_finalize_batch(
            [self._item("k2", funnel_route=False)], None, season=1, sub_key=""
        )
        self.assertEqual(notified, [0, 1])
        handler._funnel_phase1_enabled = False
        handler._queue_file_finalize_batch(
            [self._item("k3", funnel_route=True)], None, season=1, sub_key=""
        )
        self.assertEqual(notified, [0, 1, 3])

    def test_remove_pending_returns_filtered_count(self):
        """_remove_pending_finalize_tasks 返回的计数按同一口径过滤。

        开关开启时：删掉非漏斗行后只剩漏斗行 → 0；开关关闭时 → 1。
        """
        pending = {
            "k1": {"file_name": "a.mkv", "funnel_route": True},
            "k2": {"file_name": "b.mkv"},
        }

        def _build(phase1):
            store = dict(pending)
            service = PostprocessService.__new__(PostprocessService)
            owner = SimpleNamespace(
                _OFFLINE_PENDING_KEY="pending_offline_strm",
                # 返回副本：生产侧 _get_data 每次取的也是独立字典；
                # 返回本体时 _save_offline_pending 的 clear 会把同一对象清空。
                _get_data=lambda key: dict(store),
                _save_offline_pending=lambda data: (
                    store.clear(), store.update(data)
                ),
                _offline_pending_lock=threading.Lock(),
                _funnel_phase1_enabled=phase1,
            )
            object.__setattr__(service, "_owner", owner)
            return service

        removed, count, _items = _build(True)._remove_pending_finalize_tasks({"k2"})
        self.assertEqual(removed, {"k2"})
        self.assertEqual(count, 0)
        _removed, count, _items = _build(False)._remove_pending_finalize_tasks({"k2"})
        self.assertEqual(count, 1)


class FunnelWorkerRealDriveTests(_DbMixin):
    """v1.29.7 阶段 1 复核修补 1（第二轮）：漏斗 worker 经真实 _process_due_items 驱动。

    回归死锁：_process_due_items 的漏斗跳过只应拦离线监控，不能拦漏斗
    worker 自己的驱动。_FakePostprocess 的替身 _process_due_items 不实现
    跳过逻辑，端到端 round 测试走替身时永远发现不了；这里用真实
    PostprocessService（DummyMpWaitService）+ 生产配置（开关开）锁死。
    """

    def _real_mp(self):
        mp = DummyMpWaitService()
        mp._owner._funnel_phase1_enabled = True
        mp._owner._stop_requested = lambda: False
        return mp

    def _stub_drive(self, mp, pending):
        """把驱动上下文与分发打成可控桩：_prepare 返回给定 ctx，
        _dispatch 模拟终态提交（completed+1、弹出 pending）。"""
        ctx = _ctx(dict(pending))
        dispatch_calls = []

        def _dispatch(item, key, c):
            dispatch_calls.append(key)
            c.completed += 1
            c.pending.pop(key, None)
            c.committed_keys.add(key)

        _stub(
            mp, "_prepare_postprocess_context",
            lambda force=True, pending_keys=None: ctx,
        )
        _stub(mp, "_dispatch_due_item", _dispatch)
        _stub(mp, "_flush_batch_postprocess", lambda c: None)
        _stub(mp, "_reconcile_pending_state", lambda c: None)
        return ctx, dispatch_calls

    def test_drive_postprocess_completes_funnel_row_with_switch_on(self):
        """生产配置（开关开）：漏斗行经 _drive_postprocess 驱动后终态 done。

        修补前复现：_process_due_items 的跳过把行拦住，_drive_postprocess
        返回 "processing"、行仍 queued（死锁）。
        """
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        mp = self._real_mp()
        self.owner.postprocess = mp
        pending = {"pk-tv-09": _tv_item(funnel_route=True)}
        ctx, dispatch_calls = self._stub_drive(mp, pending)
        outcome = self.service._drive_postprocess(
            self.repo, row, "pk-tv-09", pending["pk-tv-09"]
        )
        self.assertEqual(dispatch_calls, ["pk-tv-09"])
        self.assertEqual(ctx.completed, 1)
        self.assertEqual(outcome, "done")
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "done")

    def test_process_due_items_default_still_skips_funnel_row(self):
        """默认口径不变：离线监控驱动时漏斗行仍被跳过（不分发、不结算）。"""
        mp = self._real_mp()
        pending = {"pk-tv-09": _tv_item(funnel_route=True)}
        ctx, dispatch_calls = self._stub_drive(mp, pending)
        mp._process_due_items(ctx)
        self.assertEqual(dispatch_calls, [])
        self.assertEqual(ctx.completed, 0)
        self.assertIn("pk-tv-09", ctx.pending)

    def test_process_due_items_default_dispatches_non_funnel_row(self):
        """默认口径不变：非漏斗行照常分发。"""
        mp = self._real_mp()
        pending = {"pk-tv-09": _tv_item()}
        ctx, dispatch_calls = self._stub_drive(mp, pending)
        mp._process_due_items(ctx)
        self.assertEqual(dispatch_calls, ["pk-tv-09"])
        self.assertEqual(ctx.completed, 1)


class FunnelDestMissedRecheckTests(_DbMixin):
    """v1.29.7 阶段 1 复核修补 2（第二轮）：dest_missed 路径 STRM 前复查。

    _commit_mp_organize_success 也在 _finalize_ready_item 之前生成 STRM，
    前置复查必须盖住这条路；否则第二份只在历史/通知上被拦，STRM 仍会写出。
    """

    def setUp(self):
        super().setUp()
        _FAKE_SUBSCRIBES[444] = SimpleNamespace(
            id=444, tmdbid=_TV_TMDB_ID, name=_TV_TITLE, season=1, type="电视剧",
        )

    def _drive(self, versions):
        mp = DummyMpWaitService()
        mp._owner.funnel = self.service
        held = []

        def _mark_held(pending_key, reason=""):
            held.append((pending_key, reason))
            return SyncHandler.funnel_mark_held_by_pending_key(
                SimpleNamespace(funnel=self.service), pending_key, reason
            )

        mp._owner.funnel_mark_held_by_pending_key = _mark_held
        self.abort_calls = []
        self.generate_calls = []
        self.finalize_calls = []
        _stub(
            mp, "_abort_single_item",
            lambda item, key, reason, ctx, notify=True: self.abort_calls.append(
                {"item": item, "key": key, "reason": reason, "notify": notify}
            ),
        )
        _stub(
            mp, "_generate_strm",
            lambda cloud_dir, file_name, target_file=None,
            lookup_target=True, log_success=True: self.generate_calls.append(
                (cloud_dir, file_name)
            ) or Path("/tmp/x.strm"),
        )
        _stub(
            mp, "_finalize_ready_item",
            lambda item, key, strm_path, media, media_data, ctx: (
                self.finalize_calls.append((key, strm_path))
            ),
        )
        _stub(
            mp, "_restore_pending_media_context",
            lambda item, key: (_tv_media(), {}),
        )
        self.owner.episode_dedup = _FakeDedup(versions=versions)
        item = _tv_item()
        pending = {"pk-tv-09": item}
        ctx = _ctx(pending)
        mp._commit_mp_organize_success(
            item, "pk-tv-09",
            _TV_CLOUD_DIR + "/" + _TV_FILE, _TV_CLOUD_DIR, ctx,
        )
        return held

    def test_dest_missed_recheck_intercepts_second_copy_before_strm(self):
        """dest_missed：库里已有另一份同集 → 拦截在 STRM 之前，漏斗行挂起。"""
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        other = {"file_id": "f-other", "file_name": _TV_FILE, "file_size": 1}
        held = self._drive({9: [other]})
        self.assertEqual(self.generate_calls, [])
        self.assertEqual(len(self.abort_calls), 1)
        self.assertFalse(self.abort_calls[0]["notify"])
        self.assertIn("另一份同集", self.abort_calls[0]["reason"])
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0][0], "pk-tv-09")
        self.assertEqual(
            self.repo.get_by_id(row["id"])["status"], "held"
        )
        self.assertEqual(self.finalize_calls, [])

    def test_dest_missed_recheck_own_landed_file_generates_strm(self):
        """dest_missed：库里只有本项刚落盘的文件（同 file_id + 同名）→ 放行生成 STRM。"""
        row, _ = self.service.enqueue_from_pending(
            _tv_item(), "pk-tv-09", _tv_media()
        )
        own = {"file_id": "f-tv-09", "file_name": _TV_FILE, "file_size": _TV_SIZE}
        held = self._drive({9: [own]})
        self.assertEqual(len(self.generate_calls), 1)
        self.assertEqual(self.abort_calls, [])
        self.assertEqual(held, [])
        self.assertEqual(len(self.finalize_calls), 1)
        self.assertEqual(self.finalize_calls[0][1], Path("/tmp/x.strm"))
        self.assertEqual(self.repo.get_by_id(row["id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
