"""1.12.8：已交 MP 整理的转存不再另写平台整理历史。

根因：插件 `_record_platform_transfer_histories` 会把每条成功转存直写
进 MP 的 TransferHistory（来源标成 PanSou、目标存成 115网盘、方式复制），
哪怕这条转存已经交给 MP 的 do_transfer（P1/P2）去整理了。MP 那边
自己会写一条真正的整理记录（本地 → 115网盘CS版），结果同一份资源
在媒体整理里看起来整理了两遍。`skip_history` 即「已交 MP 整理」，
写平台历史与对账时都要排除它；对账还会顺带删掉已存在的旧重复记录。

v1.12.11：目标存储改记 MP 存储名「115网盘CS版」（旧值是网盘驱动名
「115网盘」，与 MP 原生 115 存储同名、会误导）；未交 MP 整理的
PanSou 记录现为「PanSou → 115网盘CS版」。

覆盖 core/handlers/sync/platform_history.py 的写入口。
"""

import sys
import unittest
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.core", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.transferhistory", "app.log", "app.schemas.types",
    "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.scanner",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
)
set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()
core_pkg.CloudFile = MagicMock()


class _FakeColumn:
    """TransferHistory 的列占位：in_/like/== 只需返回可忽略的值。"""

    def __init__(self, name):
        self.name = name

    def in_(self, values):
        return ("in", self.name, tuple(values))

    def like(self, pattern):
        return ("like", self.name, pattern)

    def __eq__(self, other):  # noqa: D105 - 列比较只为拼过滤条件
        return ("eq", self.name, other)

    def __hash__(self):
        return hash(("column", self.name))


class _FakeTable:
    def __init__(self, columns):
        self._columns = list(columns)

    @property
    def columns(self):
        table = self

        class _Columns:
            def keys(self):
                return list(table._columns)

        return _Columns()


# 平台历史条目会用到的全部列名。
_COLUMNS = [
    "src", "src_storage", "src_fileitem", "dest", "dest_storage",
    "dest_fileitem", "mode", "type", "title", "year", "tmdbid",
    "imdbid", "tvdbid", "doubanid", "bangumiid", "anilistid",
    "media_source", "media_id", "category", "episode_group", "image",
    "status", "files", "downloader", "date", "seasons", "episodes",
]


class _FakeTransferHistory:
    """MP TransferHistory 桩：按 kwargs 存属性，列支持 in_/like/==。"""

    __table__ = _FakeTable(_COLUMNS)
    src = _FakeColumn("src")
    downloader = _FakeColumn("downloader")

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """SessionFactory 桩会话：记录 add/delete/commit，query 返回预置行。"""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.added = []
        self.deleted = []
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def query(self, *args, **kwargs):
        return _FakeQuery(self.rows)

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.committed = True


class _FakeSessionFactory:
    """每次调用返回同一个桩会话，便于断言。"""

    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


def _positive_int(value):
    """drive.common.positive_int 的等价实现：正整数否则 0。"""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


# 先把 positive_int 钉死再加载被测模块：platform_history 在顶层
# from ...drive.common import positive_int，加载时绑定的就是这里的值；
# 若留给 AutoMockModule，season 会是 MagicMock，f"{season:02d}" 会炸。
sys.modules["cloudsubscribex.drive.common"].positive_int = _positive_int


# 搜索源注册表桩：pansou → PanSou。
class _FakeSourceDef:
    def __init__(self, source_id, name):
        self.id = source_id
        self.name = name


class _FakeRegistry:
    @staticmethod
    def get_definitions():
        return [_FakeSourceDef("pansou", "PanSou")]


register_module(
    "cloudsubscribex.search.scanner",
    SearchSourceRegistry=_FakeRegistry,
)
register_module(
    "cloudsubscribex.search.types",
    resource_type_name=lambda value, fallback="": fallback,
)
sys.modules["app.db.models.transferhistory"].TransferHistory = (
    _FakeTransferHistory
)

_platform_history = load_module(
    "cloudsubscribex.handlers.sync.platform_history",
    "plugins.v2/cloudsubscribex/handlers/sync/platform_history.py",
)
PlatformHistoryService = _platform_history.PlatformHistoryService


def _record(**extra):
    """一条 PanSou 来的 115 分享成功记录（剧集单集）。"""
    record = {
        "status": "成功",
        "type": "电视剧",
        "title": "凡人修仙传",
        "year": "2020",
        "tmdb_id": 106449,
        "season": 1,
        "episode": 200,
        "success_episodes": [200],
        "file_name": "凡人修仙传 {tmdb-106449} - S01E200 - 第200集.mkv",
        "file_size": 2160000000,
        "cloud_dir": "/Alist/影视/moviepilot/媒体库/剧集/国漫/凡人修仙传 (2020)/Season 1",
        "cloud_drive_name": "115网盘",
        "source": "pansou",
        "time": "2026-11-16 17:54:09",
    }
    record.update(extra)
    return record


class _Service(PlatformHistoryService):
    """只测平台历史写入口，不依赖插件宿主。"""

    def __init__(self, session):
        object.__setattr__(self, "_platform_transfer_history_enabled", True)
        object.__setattr__(self, "_platform_history_lock", None)
        object.__setattr__(self, "_is_upgrade_history", None)
        fake_drive = MagicMock()
        fake_drive.name = "115网盘"
        object.__setattr__(self, "_cloud_drive", fake_drive)
        self._session = session

    def _get_data(self, key):
        return None

    def _save_data(self, key, value):
        return None


def _install_session(session):
    # 全量套件收集全部测试文件后才跑用例，别的测试文件可能覆盖了共享
    # 模块；每次用例开始时把关键桩重新钉死，避免跨文件污染。
    sys.modules["app.db"].SessionFactory = _FakeSessionFactory(session)
    # 模块顶层已绑定的 SessionFactory 也要换成桩。
    _platform_history.SessionFactory = _FakeSessionFactory(session)
    sys.modules["app.db.models.transferhistory"].TransferHistory = (
        _FakeTransferHistory
    )
    sys.modules["cloudsubscribex.search.scanner"].SearchSourceRegistry = (
        _FakeRegistry
    )
    # 对账分支里 from sqlalchemy import or_：若别的测试文件装了真
    # sqlalchemy，真 or_ 收到桩列对象会炸；钉成恒返回的 lambda。
    sys.modules["sqlalchemy"].or_ = lambda *args, **kwargs: ("or", args)
    _platform_history.positive_int = _positive_int


class TestSkipOrganizedRecords(unittest.TestCase):
    def test_organized_record_writes_no_platform_history(self):
        """已交 MP 整理的记录不写「PanSou → 115网盘CS版」平台记录。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        organized = _record(skip_history=True)
        result = service._record_platform_transfer_histories([organized])
        self.assertEqual(result, 0)
        self.assertEqual(session.added, [])
        self.assertFalse(session.committed)

    def test_unorganized_record_still_writes_platform_history(self):
        """未交 MP 整理的记录照常写一条平台记录（PanSou → 115网盘CS版）。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        plain = _record()
        result = service._record_platform_transfer_histories([plain])
        self.assertEqual(result, 1)
        self.assertEqual(len(session.added), 1)
        entry = session.added[0]
        self.assertEqual(entry.src_storage, "PanSou")
        self.assertEqual(entry.dest_storage, "115网盘CS版")
        self.assertEqual(entry.mode, "copy")
        self.assertEqual(entry.downloader, "网盘订阅助手")
        self.assertTrue(session.committed)

    def test_mixed_batch_only_writes_unorganized(self):
        """一批里只有未交 MP 整理的那条会写平台记录。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        organized = _record(skip_history=True)
        plain = _record(title="吞噬星空", tmdb_id=101172, episode=232,
                        file_name="吞噬星空 - S01E232 - 第232集.mp4")
        result = service._record_platform_transfer_histories(
            [organized, plain]
        )
        self.assertEqual(result, 1)
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.added[0].title, "吞噬星空")


class TestReconcileCleansStaleDuplicates(unittest.TestCase):
    def test_reconcile_removes_platform_record_for_organized(self):
        """对账时，已交 MP 整理的那条旧平台记录会被删掉。"""
        stale = _FakeTransferHistory(
            src="PanSou · 凡人修仙传 S01E200",
            src_storage="PanSou",
            dest_storage="115网盘",
            mode="copy",
        )
        session = _FakeSession(rows=[stale])
        _install_session(session)
        service = _Service(session)
        organized = _record(skip_history=True)
        result = service._record_platform_transfer_histories(
            [organized], reconcile=True
        )
        self.assertEqual(result, 1)
        self.assertEqual(session.deleted, [stale])
        self.assertEqual(session.added, [])

    def test_reconcile_keeps_unorganized_platform_record(self):
        """对账时，未交 MP 整理的那条平台记录会被保留（不删不增）。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        plain = _record()
        # 用将要写入的同一份条目构造已存在记录，对账时字段全等、无更新。
        entries = service._platform_history_entries(plain)
        self.assertEqual(len(entries), 1)
        existing = _FakeTransferHistory(**entries[0])
        session.rows = [existing]
        result = service._record_platform_transfer_histories(
            [plain], reconcile=True
        )
        self.assertEqual(result, 0)
        self.assertEqual(session.deleted, [])
        self.assertEqual(session.added, [])


if __name__ == "__main__":
    unittest.main()
