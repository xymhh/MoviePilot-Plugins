"""1.12.14：插件不再往 MP 整理历史里写记录，并一次性清理镜像。

固定契约（理清逻辑：MP 的整理历史是唯一账本，只有 MP 能写）：

1. `_record_platform_transfer_histories` 已在 1.12.20 删除：插件不再
   直写「PanSou → 115网盘CS版、复制」这类记录，方法本身连同 5 处
   调用一起删掉，不留 no-op 桩；
2. `restore_history_posters` 不再镜像到 MP 表：只恢复插件
   自身 history 记录缺失的海报，不调用写入口；
3. `remove_platform_transfer_history_mirror` 清理：删掉
   全部插件托管行（src 以 cloudsubscribe:// 开头，或 downloader
   为「网盘订阅助手」），幂等、每次启动都跑，不写数据标记
   （1.12.15：1.12.14 用的 platform_history_mirror_removed 未在
   CloudSubscribeDataStore 声明，写入时抛 ValueError 导致插件
   加载失败、更新回滚到 1.12.11）。

v1.28.1 例外（用户 2026-11-20 定方向 A）：直出 strm 时插件在
handlers/sync/history_backfill.py 经 MP 官方外部登记映射补写一行
（origin="external"、downloader 为空、src 为真实 115 路径）；它是
唯一的写入口，且清理的两个条件匹配不到补写行——本文件末尾的测试
钉死这一点。
"""

import os
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

    def delete(self, synchronize_session=False):
        # 只删插件托管行（与 _delete_platform_transfer_histories
        # 的 or_ 条件一致），其余行不动。
        kept = [
            row for row in self._rows
            if not (
                str(getattr(row, "src", "")).startswith("cloudsubscribe://")
                or getattr(row, "downloader", "") == "网盘订阅助手"
            )
        ]
        count = len(self._rows) - len(kept)
        self._rows[:] = kept
        return count


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
# from ...drive.common import positive_int，加载时绑定的就是这里的值。
sys.modules["cloudsubscribex.drive.common"].positive_int = _positive_int


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
    """只测平台历史写入口与一次性清理，不依赖插件宿主。"""

    def __init__(self, session, data=None):
        object.__setattr__(self, "_platform_history_lock", None)
        object.__setattr__(self, "_is_upgrade_history", None)
        fake_drive = MagicMock()
        fake_drive.name = "115网盘"
        object.__setattr__(self, "_cloud_drive", fake_drive)
        self._session = session
        self._data = dict(data or {})

    def _get_data(self, key):
        return self._data.get(key)

    def _save_data(self, key, value):
        self._data[key] = value
        return None


def _install_session(session):
    sys.modules["app.db"].SessionFactory = _FakeSessionFactory(session)
    _platform_history.SessionFactory = _FakeSessionFactory(session)
    sys.modules["app.db.models.transferhistory"].TransferHistory = (
        _FakeTransferHistory
    )
    sys.modules["cloudsubscribex.search.scanner"].SearchSourceRegistry = (
        _FakeRegistry
    )
    sys.modules["sqlalchemy"].or_ = lambda *args, **kwargs: ("or", args)
    _platform_history.positive_int = _positive_int


class TestPlatformHistoryRemoved(unittest.TestCase):
    def test_record_method_removed(self):
        """写入口已删除：类上无此方法，生产代码无调用点，不留 no-op 桩。"""
        # 注意：PlatformHistoryService 继承 OwnerDelegator（__getattr__ 委托
        # 解析），hasattr(service, ...) 不可信——委托会让任意名字都“存在”。
        self.assertNotIn(
            "_record_platform_transfer_histories",
            PlatformHistoryService.__dict__,
        )
        plugin_root = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugins.v2",
            "cloudsubscribex",
        )
        for root, _dirs, files in os.walk(plugin_root):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    self.assertNotIn(
                        "_record_platform_transfer_histories(",
                        handle.read(),
                        path,
                    )

    def test_mirror_cleanup_deletes_managed_rows_idempotent(self):
        """清理删掉插件托管行且幂等：第二次调用已无托管行可删，返回 0。"""
        managed = [
            _FakeTransferHistory(
                src="cloudsubscribe://abc", downloader="网盘订阅助手"
            ),
            _FakeTransferHistory(
                src="cloudsubscribe://def", downloader="网盘订阅助手"
            ),
            _FakeTransferHistory(src="local", downloader=""),
        ]
        session = _FakeSession(rows=managed)
        _install_session(session)
        service = _Service(session)
        removed = service.remove_platform_transfer_history_mirror()
        self.assertEqual(removed, 2)
        self.assertTrue(session.committed)
        # 第二次调用：托管行已清掉，只剩非托管行，返回 0、不提交。
        remaining = [
            row for row in managed
            if not str(getattr(row, "src", "")).startswith(
                "cloudsubscribe://"
            )
            and getattr(row, "downloader", "") != "网盘订阅助手"
        ]
        session2 = _FakeSession(rows=remaining)
        _install_session(session2)
        self.assertEqual(
            service.remove_platform_transfer_history_mirror(), 0
        )
        self.assertFalse(session2.committed)

    def test_sync_does_not_mirror_to_mp_history(self):
        """v1.13.3：history 表已删，海报恢复（restore_history_posters）
        连同读 history 的路径一起删除；模块里唯一碰 MP 表的方法只剩
        一次性清理（删托管行），不再有任何写记录入口。"""
        self.assertNotIn(
            "restore_history_posters", PlatformHistoryService.__dict__
        )
        self.assertNotIn(
            "_record_platform_transfer_histories",
            PlatformHistoryService.__dict__,
        )
        methods = {
            name for name in PlatformHistoryService.__dict__
            if not name.startswith("__")
        }
        self.assertEqual(
            methods,
            {"_delete_platform_transfer_histories",
             "remove_platform_transfer_history_mirror"},
            "平台历史模块只应剩清理方法，新增写入口须先审",
        )
        # 清理本身不新增行：只删、不 add。
        session = _FakeSession(rows=[
            _FakeTransferHistory(
                src="cloudsubscribe://abc", downloader="网盘订阅助手"
            ),
        ])
        _install_session(session)
        service = _Service(session)
        self.assertEqual(service.remove_platform_transfer_history_mirror(), 1)
        self.assertEqual(session.added, [])

    def test_backfill_rows_survive_cleanup(self):
        """v1.28.1：直出 strm 补写行（真实 115 路径 + downloader 为空）
        不匹配清理的两个条件，启动清理删不到它。

        （_FakeQuery.delete 只改自己的行副本，断言用返回值 + 单独
        只有补写行的会话：删 0 条、不提交。）"""
        backfill = _FakeTransferHistory(
            src="/Alist/影视/moviepilot/媒体库/剧集/国产剧/多喜一家人 (2026)/Season 1/多喜一家人 - S01E09.mkv",
            downloader=None,
        )
        managed = _FakeTransferHistory(
            src="cloudsubscribe://abc", downloader="网盘订阅助手"
        )
        session = _FakeSession(rows=[backfill, managed])
        _install_session(session)
        service = _Service(session)
        # 两行里只删托管行。
        self.assertEqual(service.remove_platform_transfer_history_mirror(), 1)
        # 只有补写行时：删 0 条、不提交。
        session2 = _FakeSession(rows=[backfill])
        _install_session(session2)
        self.assertEqual(service.remove_platform_transfer_history_mirror(), 0)
        self.assertFalse(session2.committed)


if __name__ == "__main__":
    unittest.main()
