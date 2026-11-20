"""v1.28.1：直出 strm 时补写 MP 整理历史 + 完成通知替身可 pickle。

用户 2026-11-20 定方向 A：转存仍走快的直出 strm，插件在生成 .strm
的同时往 MP 整理历史补写一条记录（“看起来统一”）。写入入口按 MP
v3.2.6 真实源码核实：官方外部登记口径（app/application/history/
registration.py 的 TransferRegistrationWrite → app/db/adapters/
history/registration.py 的 insert_if_absent）把同一组列映射到
TransferHistory（origin="external"、registration 审计、date、
status=True），再经 TransferHistoryOper.stage_insert_if_source_absent
原子落库（(src, src_storage) 唯一索引，冲突不替换、不重复写）。

本文件用真实 HistoryBackfillService + 真实 media_identity +
真实 format_episode_ranges 验证；DB 层用记录型桩 oper/session。
"""

import os
import pickle
import sys
import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
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

# 真实模块：utils（format_episode_ranges）→ core.media
# （media_identity）→ history_backfill → notify（替身）。
# utils 顶层 try 读 MP 的 app.domain.episode.format_ranges：全量套件
# 里其它文件可能已把它注册为占位模块（MagicMock），此时必须钉死为
# None 走本地区间拼装，否则返回值不是字符串。
_utils = load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)
_utils.platform_format_ranges = None
load_module(
    "cloudsubscribex.core.media",
    "plugins.v2/cloudsubscribex/core/media.py",
)
_tpl = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)
_core = sys.modules["cloudsubscribex.core"]
_core.render_template = _tpl.render_template
_core.resolve_template = _tpl.resolve_template
_backfill_module = load_module(
    "cloudsubscribex.handlers.sync.history_backfill",
    "plugins.v2/cloudsubscribex/handlers/sync/history_backfill.py",
)
HistoryBackfillService = _backfill_module.HistoryBackfillService
_notify = load_module(
    "cloudsubscribex.handlers.sync.notify",
    "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
)

_TV_CLOUD_DIR = (
    "/Alist/影视/moviepilot/媒体库/剧集/国产剧/多喜一家人 (2026) {tmdb-294498}/Season 1"
)
_TV_FILE = "多喜一家人 {tmdb-294498} - S01E09 - 第9集.mkv"
_TV_STRM = (
    "/strm/115网盘/剧集/国产剧/多喜一家人 (2026) {tmdb-294498}/Season 1/"
    "多喜一家人 {tmdb-294498} - S01E09 - 第9集.strm"
)
_TV_SIZE = 1585446912


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


class _RecordingOper:
    """stage_insert_if_source_absent 的记录型桩：按 MP 唯一索引语义，
    同一 (src, src_storage) 第二次返回 created=False。"""

    def __init__(self):
        self.calls = []
        self._seen = set()

    def stage_insert_if_source_absent(self, values):
        self.calls.append(dict(values))
        key = (values.get("src"), values.get("src_storage"))
        if key in self._seen:
            return {"id": 1}, False
        self._seen.add(key)
        return {"id": 1}, True


class _FailingOper:
    def __init__(self):
        self.calls = []

    def stage_insert_if_source_absent(self, values):
        self.calls.append(dict(values))
        raise RuntimeError("db down")


class _FakeSession:
    def __init__(self):
        self.committed = False

    def commit(self):
        self.committed = True


class _SessionBackfillService(HistoryBackfillService):
    """走真实 session 路径的服务：验证写成功后显式 commit。"""

    def __init__(self, owner, oper, session):
        super().__init__(owner, history_oper=oper)
        self._session = session

    @contextmanager
    def _history_oper_context(self):
        yield self._history_oper, self._session


class StrmHistoryBackfillTests(unittest.TestCase):
    def _service(self, oper=None):
        return HistoryBackfillService(SimpleNamespace(), history_oper=oper)

    def test_tv_backfill_fields_are_truthful(self):
        """剧集直出：字段全部取真实值，不编造下载器/耗时。"""
        oper = _RecordingOper()
        service = self._service(oper)
        values = service.build_history_values(
            item=_tv_item(),
            strm_path=_TV_STRM,
            media_data={"tmdb_id": 294498},
            detail=_tv_detail(),
        )
        self.assertIsNotNone(values)
        self.assertEqual(values["src"], f"{_TV_CLOUD_DIR}/{_TV_FILE}")
        self.assertEqual(values["src_storage"], "local")
        self.assertEqual(values["dest"], _TV_STRM)
        self.assertEqual(values["dest_storage"], "local")
        self.assertEqual(values["type"], "电视剧")
        self.assertEqual(values["title"], "多喜一家人")
        self.assertEqual(values["year"], "2026")
        self.assertEqual(values["media_source"], "themoviedb")
        self.assertEqual(values["media_id"], "294498")
        self.assertEqual(values["seasons"], "S01")
        self.assertEqual(values["episodes"], "E09")
        self.assertEqual(values["category"], "国产剧")
        self.assertEqual(
            values["image"], "https://image.tmdb.org/t/p/w500/abc.jpg"
        )
        self.assertEqual(values["status"], True)
        self.assertEqual(values["retry_count"], 0)
        self.assertEqual(values["auto_paused"], False)
        # MP 整理链才有的字段一律留空，不编造。
        self.assertIsNone(values["mode"])
        self.assertIsNone(values["downloader"])
        self.assertIsNone(values["download_hash"])
        self.assertIsNone(values["transfer_task_id"])
        # 外部登记标识与审计。
        self.assertEqual(values["origin"], "external")
        self.assertEqual(values["files"], [_TV_STRM])
        self.assertEqual(
            values["registration"]["actor_name"], "网盘订阅助手"
        )
        self.assertEqual(values["src_fileitem"]["path"], values["src"])
        self.assertEqual(values["src_fileitem"]["size"], _TV_SIZE)
        self.assertEqual(values["dest_fileitem"]["path"], _TV_STRM)
        self.assertRegex(
            values["date"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"
        )
        self.assertTrue(service.backfill_strm_history(
            item=_tv_item(),
            strm_path=_TV_STRM,
            media_data={"tmdb_id": 294498},
            detail=_tv_detail(),
        ))
        self.assertEqual(len(oper.calls), 1)

    def test_movie_backfill_has_no_seasons_episodes(self):
        """电影直出：同一 commit 路径，不写季集。"""
        values = self._service().build_history_values(
            item={
                "cloud_dir": "/Alist/影视/moviepilot/媒体库/电影/秒速5厘米 (2025)",
                "file_name": "秒速5厘米 (2025).mkv",
                "size": 7460000000,
            },
            strm_path="/strm/115网盘/电影/秒速5厘米 (2025)/秒速5厘米 (2025).strm",
            media_data={"tmdb_id": 123},
            detail={
                "type": "电影",
                "title": "秒速5厘米",
                "year": "2025",
                "size": 7460000000,
                "category": "",
                "image": None,
            },
        )
        self.assertIsNotNone(values)
        self.assertEqual(values["type"], "电影")
        self.assertIsNone(values["seasons"])
        self.assertIsNone(values["episodes"])
        self.assertEqual(values["media_id"], "123")

    def test_multi_episode_range_format(self):
        """多集聚合一条时按集分别写：本项的集字段用 MP 式区间。"""
        values = self._service().build_history_values(
            item=_tv_item(episode=9),
            strm_path=_TV_STRM,
            detail=_tv_detail(episodes=[9, 10]),
        )
        self.assertIsNotNone(values)
        self.assertEqual(values["episodes"], "E09-E10")

    def test_mp_organize_branch_not_written(self):
        """MP 整理分支不重复写：MP 自己已写记录。"""
        oper = _RecordingOper()
        service = self._service(oper)
        result = service.backfill_strm_history(
            item=_tv_item(mp_organize={"status": "success"}),
            strm_path=_TV_STRM,
            detail=_tv_detail(),
        )
        self.assertFalse(result)
        self.assertEqual(oper.calls, [])

    def test_skip_history_not_written(self):
        """already_in_library 等 skip_history 项不写。"""
        oper = _RecordingOper()
        service = self._service(oper)
        result = service.backfill_strm_history(
            item=_tv_item(skip_history=True),
            strm_path=_TV_STRM,
            detail=_tv_detail(),
        )
        self.assertFalse(result)
        self.assertEqual(oper.calls, [])

    def test_no_strm_not_written(self):
        """没生成 .strm 不写。"""
        oper = _RecordingOper()
        service = self._service(oper)
        result = service.backfill_strm_history(
            item=_tv_item(),
            strm_path=None,
            detail=_tv_detail(),
        )
        self.assertFalse(result)
        self.assertEqual(oper.calls, [])

    def test_missing_title_not_written(self):
        """拼不出真实记录（无标题）时不写。"""
        service = self._service(_RecordingOper())
        values = service.build_history_values(
            item=_tv_item(),
            strm_path=_TV_STRM,
            detail=_tv_detail(title=""),
        )
        self.assertIsNone(values)

    def test_dedup_same_source_not_written_twice(self):
        """去重：同一 (src, src_storage) 第二次返回已存在，不重复写。"""
        oper = _RecordingOper()
        service = self._service(oper)
        kwargs = {
            "item": _tv_item(),
            "strm_path": _TV_STRM,
            "detail": _tv_detail(),
        }
        self.assertTrue(service.backfill_strm_history(**kwargs))
        self.assertFalse(service.backfill_strm_history(**kwargs))
        self.assertEqual(len(oper.calls), 2)
        self.assertEqual(
            oper.calls[0]["src"], oper.calls[1]["src"]
        )

    def test_write_failure_does_not_block_transfer(self):
        """写失败不阻断转存：返回 False，不抛异常。"""
        oper = _FailingOper()
        service = self._service(oper)
        result = service.backfill_strm_history(
            item=_tv_item(),
            strm_path=_TV_STRM,
            detail=_tv_detail(),
        )
        self.assertFalse(result)
        self.assertEqual(len(oper.calls), 1)

    def test_successful_write_commits_session(self):
        """真实 session 路径：stage 写完后显式 commit。"""
        oper = _RecordingOper()
        session = _FakeSession()
        service = _SessionBackfillService(SimpleNamespace(), oper, session)
        result = service.backfill_strm_history(
            item=_tv_item(),
            strm_path=_TV_STRM,
            detail=_tv_detail(),
        )
        self.assertTrue(result)
        self.assertTrue(session.committed)

    def test_commit_single_item_calls_backfill(self):
        """接线：_commit_single_item 在详情建成后调 history_backfill。"""
        postprocess_path = os.path.join(
            project_root,
            "plugins.v2", "cloudsubscribex", "handlers", "sync",
            "postprocess.py",
        )
        with open(postprocess_path, encoding="utf-8") as handle:
            lines = handle.readlines()
        start = next(
            index for index, line in enumerate(lines)
            if "def _commit_single_item" in line
        )
        end = next(
            index for index, line in enumerate(lines)
            if index > start and line.startswith("    def ")
        )
        body = "".join(lines[start:end])
        self.assertIn(
            "self._owner.history_backfill.backfill_strm_history(",
            body,
        )
        self.assertIn("strm_path=strm_path", body)
        self.assertIn("detail=detail", body)

class CompletedMediaInfoStubPickleTests(unittest.TestCase):
    def test_stub_pickle_roundtrip(self):
        """v1.28.1 回归：2026-11-20 00:37:04 redis 报错的根因——
        _completed_mediainfo_stub 里的局部类 _MediaTypeValue 与两个
        lambda 不能 pickle，MP 模板缓存写 redis 时失败。模块级类 +
        真方法后，Message 含该对象时 pickle.dumps 能过。"""
        stub = _notify._completed_mediainfo_stub(_tv_detail())
        self.assertEqual(stub.type.value, "电视剧")
        self.assertEqual(stub.title, "多喜一家人")
        self.assertEqual(stub.title_year, "多喜一家人 (2026)")
        self.assertEqual(stub.season, 1)
        self.assertEqual(
            stub.get_poster_image(),
            "https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        self.assertIsNone(stub.get_backdrop_image())
        # 模拟 MP TemplateHelper.set_cache_context：整个渲染上下文
        #（含 __mediainfo__ 替身）被 pickle 进 redis。
        # 注意：全量套件收集时会先 import 其它测试文件，有的在
        # install_app_mocks 里把 notify 注册为占位模块（AutoMockModule），
        # pickle 按 __module__ + __qualname__ 查类时会取到占位模块上的
        # MagicMock 而报“不是同一对象”。生产只加载一次真实模块，无此
        # 问题；测试里只在 dumps/loads 瞬间把 sys.modules 指回本文件
        # 加载的真实模块，完事立刻恢复，不影响其它测试。
        context = {"__mediainfo__": stub, "title": "多喜一家人"}
        notify_key = "cloudsubscribex.handlers.sync.notify"
        previous_notify = sys.modules.get(notify_key)
        sys.modules[notify_key] = _notify
        try:
            restored = pickle.loads(pickle.dumps(context))
        finally:
            if previous_notify is None:
                sys.modules.pop(notify_key, None)
            else:
                sys.modules[notify_key] = previous_notify
        self.assertEqual(restored["__mediainfo__"].type.value, "电视剧")
        self.assertEqual(restored["__mediainfo__"].title, "多喜一家人")
        self.assertEqual(
            restored["__mediainfo__"].get_poster_image(),
            "https://image.tmdb.org/t/p/w500/abc.jpg",
        )
        self.assertIsNone(restored["__mediainfo__"].get_backdrop_image())


if __name__ == "__main__":
    unittest.main()
