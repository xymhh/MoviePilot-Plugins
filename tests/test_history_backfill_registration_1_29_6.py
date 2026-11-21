"""v1.29.6：补写 registration 按 MP 模型写 + 启动修复坏记录。

根因：v1.28.1 的补写 registration 漏了 MP TransferRegistrationAudit
必填的 actor_id、多写了模型禁用的 note，MP 查历史列表时逐条
model_validate，一条坏记录就让整页抛 ValidationError，#/history
显示“未知错误”、0 条记录（2026-11-21 15:07 首次爆出）。
修复：补写只写 5 键（actor_id 取 MP 超级管理员 id，取不到
fallback 1）；启动时 repair_invalid_registrations 修复已写坏的行
（只动插件补写行，幂等、失败不挡启动）。

MP 模型以 v3.2.6 缓存源码为准（app/schemas/history.py）：
actor_id（int ≥ 1）必填、actor_name、registered_at、
verification ∈ {matching_sizes, native_hardlink, destination_only}、
source_state ∈ {present, absent}，extra="forbid"。
"""

import os
import sys
import unittest
from types import SimpleNamespace

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
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
    "app", "app.log", "app.schemas", "app.schemas.types",
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
    "cloudsubscribex.utils.cache",
)
set_media_type(movie="电影", tv="电视剧")
_types = sys.modules["app.schemas.types"]
_types.NotificationType = type("NotificationType", (), {"Plugin": "Plugin"})
_types.MessageType = type(
    "MessageType", (), {"Download": "Download", "Organize": "Organize"}
)
_types.ContentType = type(
    "ContentType",
    (), {"DownloadAdded": "DownloadAdded", "OrganizeSuccess": "OrganizeSuccess"},
)
install_core_delegation()


def _format_size(value):
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return "未知"
    if size <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f}{unit}" if unit != "B" else f"{size}B"
        size /= 1024
    return "未知"


ensure_package("cloudsubscribex.drive.common").format_size = _format_size

_utils = load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)
_utils.platform_format_ranges = None
load_module(
    "cloudsubscribex.core.media",
    "plugins.v2/cloudsubscribex/core/media.py",
)
_backfill_module = load_module(
    "cloudsubscribex.handlers.sync.history_backfill",
    "plugins.v2/cloudsubscribex/handlers/sync/history_backfill.py",
)
HistoryBackfillService = _backfill_module.HistoryBackfillService
_build_backfill_registration = _backfill_module._build_backfill_registration
_registration_is_valid = _backfill_module._registration_is_valid
_normalize_registration = _backfill_module._normalize_registration

_TV_CLOUD_DIR = (
    "/Alist/影视/moviepilot/媒体库/剧集/国产剧/多喜一家人 (2026) {tmdb-294498}/Season 1"
)
_TV_FILE = "多喜一家人 {tmdb-294498} - S01E09 - 第9集.mkv"
_TV_STRM = (
    "/strm/115网盘/剧集/国产剧/多喜一家人 (2026) {tmdb-294498}/Season 1/"
    "多喜一家人 {tmdb-294498} - S01E09 - 第9集.strm"
)
_TV_SIZE = 1585446912

# v1.28.1 补写过的坏形状：缺 actor_id、多 note。
_BAD_REGISTRATION = {
    "actor_name": "网盘订阅助手",
    "registered_at": "2026-11-20T08:47:00Z",
    "verification": "destination_only",
    "source_state": "present",
    "note": "网盘订阅助手直出 strm 时补写（MP 未走整理链）",
}
# MP 官方口径写的合法形状：5 键齐全。
_VALID_REGISTRATION = {
    "actor_id": 3,
    "actor_name": "admin",
    "registered_at": "2026-11-20T10:00:00Z",
    "verification": "matching_sizes",
    "source_state": "present",
}


def _tv_item(**extra):
    item = {
        "cloud_dir": _TV_CLOUD_DIR,
        "file_name": _TV_FILE,
        "size": _TV_SIZE,
        "season": 1,
        "episode": 9,
    }
    item.update(extra)
    return item


def _tv_detail(**extra):
    detail = {
        "type": "电视剧",
        "title": "多喜一家人",
        "year": "2026",
        "season": 1,
        "episodes": [9],
        "size": _TV_SIZE,
        "category": "国产剧",
        "image": "https://image.tmdb.org/t/p/w500/abc.jpg",
        "detail_link": "https://www.themoviedb.org/tv/294498",
    }
    detail.update(extra)
    return detail


class _DummyStatement:
    """sqlalchemy select 的替身：只拼链式调用，不执行。"""

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self


def _dummy_select(*args, **kwargs):
    return _DummyStatement()


class _FakeUserColumn:
    def is_(self, value):
        return True


class _FakeUser:
    id = 1
    is_superuser = _FakeUserColumn()


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    """DB 会话替身：execute 返回预设行，commit 只记标记。"""

    def __init__(self, rows=None, fail=False):
        self.rows = list(rows or [])
        self.fail = fail
        self.committed = False
        self.executed = []

    def execute(self, statement):
        self.executed.append(statement)
        if self.fail:
            raise RuntimeError("db down")
        return _FakeResult(self.rows)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeTransferHistory:
    origin = "external"


def _row(row_id, origin, registration):
    return SimpleNamespace(id=row_id, origin=origin, registration=registration)


class _StubSqlAlchemyMixin(unittest.TestCase):
    """给需要走 sqlalchemy select 的用例钉死一个替身 select。

    全量套件里 sys.modules["sqlalchemy"] 可能是其它文件留下的真实
    模块（其 select 遇到假模型会抛错），也可能是占位模块；这里在
    setUp 里换成确定行为的替身，tearDown 原样恢复。
    """

    def setUp(self):
        super().setUp()
        self._prev_sqlalchemy = sys.modules.get("sqlalchemy")
        register_module("sqlalchemy", select=_dummy_select)
        self.addCleanup(self._restore_sqlalchemy)

    def _restore_sqlalchemy(self):
        if self._prev_sqlalchemy is None:
            sys.modules.pop("sqlalchemy", None)
        else:
            sys.modules["sqlalchemy"] = self._prev_sqlalchemy


class BackfillRegistrationShapeTests(unittest.TestCase):
    def test_build_registration_has_exactly_five_legal_keys(self):
        """补写 registration：恰好 5 键，无 note。"""
        registration = _build_backfill_registration(7)
        self.assertEqual(
            set(registration),
            {"actor_id", "actor_name", "registered_at", "verification", "source_state"},
        )
        self.assertEqual(registration["actor_id"], 7)
        self.assertEqual(registration["actor_name"], "网盘订阅助手")
        self.assertEqual(registration["verification"], "destination_only")
        self.assertEqual(registration["source_state"], "present")
        self.assertNotIn("note", registration)
        self.assertRegex(
            registration["registered_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )

    def test_build_history_values_registration_is_valid(self):
        """build_history_values 产出的 registration 符合模型。"""
        service = HistoryBackfillService(SimpleNamespace())
        values = service.build_history_values(
            item=_tv_item(),
            strm_path=_TV_STRM,
            media_data={"tmdb_id": 294498},
            detail=_tv_detail(),
            actor_id=7,
        )
        self.assertIsNotNone(values)
        registration = values["registration"]
        self.assertEqual(
            set(registration),
            {"actor_id", "actor_name", "registered_at", "verification", "source_state"},
        )
        self.assertEqual(registration["actor_id"], 7)
        self.assertNotIn("note", registration)
        self.assertTrue(_registration_is_valid(registration))

    def test_build_history_values_without_actor_id_falls_back_to_1(self):
        """不传 actor_id 时解析失败要 fallback 1，不能写出缺 actor_id 的行。

        测试环境没有真实 MP 用户表，解析必然失败——断言兜底值 1，
        不依赖解析走哪条失败分支。
        """
        service = HistoryBackfillService(SimpleNamespace())
        values = service.build_history_values(
            item=_tv_item(),
            strm_path=_TV_STRM,
            media_data={"tmdb_id": 294498},
            detail=_tv_detail(),
        )
        self.assertIsNotNone(values)
        self.assertEqual(values["registration"]["actor_id"], 1)
        self.assertTrue(_registration_is_valid(values["registration"]))


class AdminActorResolutionTests(_StubSqlAlchemyMixin):
    def setUp(self):
        super().setUp()
        self._prev_user_module = sys.modules.get("app.db.models.user")
        parent = sys.modules.get("app.db.models")
        self._prev_parent_attr = getattr(parent, "user", None) if parent else None
        self._had_parent_attr = (
            parent is not None and "user" in vars(parent)
        )
        register_module("app.db.models.user", User=_FakeUser)
        self.addCleanup(self._restore_user_module)

    def _restore_user_module(self):
        if self._prev_user_module is None:
            sys.modules.pop("app.db.models.user", None)
        else:
            sys.modules["app.db.models.user"] = self._prev_user_module
        parent = sys.modules.get("app.db.models")
        if parent is not None:
            if self._had_parent_attr:
                setattr(parent, "user", self._prev_parent_attr)
            else:
                vars(parent).pop("user", None)

    def test_returns_first_superuser_id(self):
        """用户表里有超级管理员时取其 id。"""
        service = HistoryBackfillService(SimpleNamespace())
        session = _FakeSession(rows=[SimpleNamespace(id=5)])
        self.assertEqual(service._resolve_admin_actor_id(session), 5)

    def test_falls_back_to_1_when_no_superuser(self):
        """用户表里没有超级管理员时 fallback 1。"""
        service = HistoryBackfillService(SimpleNamespace())
        session = _FakeSession(rows=[])
        self.assertEqual(service._resolve_admin_actor_id(session), 1)

    def test_falls_back_to_1_when_db_fails(self):
        """用户表查询抛错时 fallback 1，不抛异常。"""
        service = HistoryBackfillService(SimpleNamespace())
        session = _FakeSession(fail=True)
        self.assertEqual(service._resolve_admin_actor_id(session), 1)


class RegistrationNormalizeTests(unittest.TestCase):
    def test_valid_registration_is_unchanged(self):
        """合法的 5 键 registration 返回 None（不动）。"""
        self.assertIsNone(
            _normalize_registration(dict(_VALID_REGISTRATION), 7)
        )
        self.assertTrue(_registration_is_valid(dict(_VALID_REGISTRATION)))

    def test_none_registration_is_valid(self):
        """registration 为空是 MP 旧行的合法状态，不动。"""
        self.assertIsNone(_normalize_registration(None, 7))
        self.assertTrue(_registration_is_valid(None))

    def test_fixes_missing_actor_id_and_note(self):
        """v1.28.1 坏形状：补 actor_id、删 note，其余原值保留。"""
        fixed = _normalize_registration(dict(_BAD_REGISTRATION), 7)
        self.assertIsNotNone(fixed)
        self.assertEqual(
            set(fixed),
            {"actor_id", "actor_name", "registered_at", "verification", "source_state"},
        )
        self.assertEqual(fixed["actor_id"], 7)
        self.assertEqual(fixed["actor_name"], "网盘订阅助手")
        self.assertEqual(fixed["registered_at"], "2026-11-20T08:47:00Z")
        self.assertEqual(fixed["verification"], "destination_only")
        self.assertEqual(fixed["source_state"], "present")
        self.assertNotIn("note", fixed)
        self.assertTrue(_registration_is_valid(fixed))

    def test_keeps_valid_actor_id(self):
        """已有合法 actor_id 时保留，不被解析值覆盖。"""
        registration = dict(_BAD_REGISTRATION)
        registration["actor_id"] = 9
        fixed = _normalize_registration(registration, 7)
        self.assertEqual(fixed["actor_id"], 9)
        self.assertNotIn("note", fixed)

    def test_fixes_invalid_values_with_defaults(self):
        """非法 verification/source_state/空 actor_name 时按默认值补。"""
        registration = {
            "actor_id": 0,
            "actor_name": "   ",
            "registered_at": "x" * 50,
            "verification": "bogus",
            "source_state": "bogus",
        }
        fixed = _normalize_registration(registration, 7)
        self.assertEqual(fixed["actor_id"], 7)
        self.assertEqual(fixed["actor_name"], "网盘订阅助手")
        self.assertEqual(fixed["verification"], "destination_only")
        self.assertEqual(fixed["source_state"], "present")
        self.assertRegex(
            fixed["registered_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertTrue(_registration_is_valid(fixed))


class RepairInvalidRegistrationsTests(_StubSqlAlchemyMixin):
    def _rows(self):
        self.bad_row = _row(101, "external", dict(_BAD_REGISTRATION))
        self.valid_row = _row(102, "external", dict(_VALID_REGISTRATION))
        self.valid_registration = self.valid_row.registration
        self.native_row = _row(103, None, None)
        self.external_none_row = _row(104, "external", None)
        self.bad_verification_row = _row(
            105,
            "external",
            {
                "actor_id": 2,
                "actor_name": "网盘订阅助手",
                "registered_at": "2026-11-20T09:00:00Z",
                "verification": "bogus",
                "source_state": "present",
            },
        )
        return [
            self.bad_row,
            self.valid_row,
            self.native_row,
            self.external_none_row,
            self.bad_verification_row,
        ]

    def _service(self):
        return HistoryBackfillService(SimpleNamespace())

    def test_repair_fixes_only_plugin_rows(self):
        """只修插件补写坏的行：MP 官方行、原生行、registration 为空的行都不动。"""
        rows = self._rows()
        session = _FakeSession(rows=rows)
        fixed = self._service().repair_invalid_registrations(
            session=session, history_model=_FakeTransferHistory, actor_id=7
        )
        self.assertEqual(fixed, 2)
        self.assertTrue(session.committed)
        self.assertEqual(self.bad_row.registration["actor_id"], 7)
        self.assertNotIn("note", self.bad_row.registration)
        self.assertEqual(
            self.bad_row.registration["registered_at"], "2026-11-20T08:47:00Z"
        )
        self.assertEqual(
            self.bad_verification_row.registration["verification"],
            "destination_only",
        )
        self.assertEqual(self.bad_verification_row.registration["actor_id"], 2)
        self.assertIs(self.valid_row.registration, self.valid_registration)
        self.assertIsNone(self.native_row.registration)
        self.assertIsNone(self.external_none_row.registration)

    def test_repair_is_idempotent_second_run_noop(self):
        """第二次启动无坏行时为 no-op，不再提交。"""
        rows = self._rows()
        session = _FakeSession(rows=rows)
        service = self._service()
        self.assertEqual(
            service.repair_invalid_registrations(
                session=session, history_model=_FakeTransferHistory, actor_id=7
            ),
            2,
        )
        session.committed = False
        self.assertEqual(
            service.repair_invalid_registrations(
                session=session, history_model=_FakeTransferHistory, actor_id=7
            ),
            0,
        )
        self.assertFalse(session.committed)

    def test_repair_failure_is_fail_open(self):
        """DB 抛错时修复返回 0，不抛异常（不挡启动）。"""
        session = _FakeSession(fail=True)
        fixed = self._service().repair_invalid_registrations(
            session=session, history_model=_FakeTransferHistory, actor_id=7
        )
        self.assertEqual(fixed, 0)
        self.assertFalse(session.committed)

    def test_repair_with_no_external_rows_is_noop(self):
        """没有 external 行时直接返回 0，不解析管理员、不提交。"""
        session = _FakeSession(rows=[_row(201, None, None)])
        fixed = self._service().repair_invalid_registrations(
            session=session, history_model=_FakeTransferHistory, actor_id=7
        )
        self.assertEqual(fixed, 0)
        self.assertFalse(session.committed)


class StartupWiringTests(unittest.TestCase):
    def test_sync_handler_forwards_repair(self):
        """SyncHandler 有 repair 转发，委托给 history_backfill 组件。"""
        path = os.path.join(
            project_root,
            "plugins.v2", "cloudsubscribex", "handlers", "sync", "service.py",
        )
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("def repair_backfill_registrations(self) -> int:", source)
        self.assertIn(
            "return self.history_backfill.repair_invalid_registrations()",
            source,
        )

    def test_init_plugin_calls_repair_after_mirror_cleanup(self):
        """启动时在镜像清理之后调修复，且包在 try/except 里（失败不挡启动）。"""
        path = os.path.join(
            project_root,
            "plugins.v2", "cloudsubscribex", "__init__.py",
        )
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
        mirror = next(
            index for index, line in enumerate(lines)
            if "self._sync_handler.remove_platform_transfer_history_mirror()" in line
        )
        window = "".join(lines[mirror:mirror + 12])
        self.assertIn(
            "self._sync_handler.repair_backfill_registrations()", window
        )
        self.assertIn("except Exception", window)


if __name__ == "__main__":
    unittest.main()
