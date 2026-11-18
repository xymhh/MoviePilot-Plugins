"""离线任务「已入库」标注的数据源判定测试。

现场（2026-10-10 Onslaught.2026.2160p.iT.WEB-DL...BYNDR，16.4GB）：

* 07:48:35 插件提交 115 离线任务；
* 07:50:11 日志显示「Magnet 下载后文件匹配完成：移动 1 个文件」——已经入库；
* 但 115 云下载任务条目 ``status`` 仍为 0，列表 UI 一直显示「等待下载 0.0%」。

原因是 115 秒传命中时文件立即可用、任务条目状态却可能长期不翻转，而插件的完成判定
只看网盘暂存目录（``handlers/sync/postprocess.py::_poll_offline_ready``），不依赖该状态。

v1.13.3：本判定不再查插件 history（表已删除），改查 MP 自己的
DownloadHistory（download_hash 对磁力任务就是 magnet 链接，含 info_hash）。
本测试锁定新判定：同一 info_hash 在 MP 下载历史里出现才算已入库；
大小写、空值、查询异常都要正确处理。
"""

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    install_app_mocks,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.helper", "app.helper.mediaserver",
    "app.application", "app.application.mediaserver", "app.log",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
)
set_media_type(movie="电影", tv="电视剧")
sys.modules["cloudsubscribex.core"].OwnerDelegator = OwnerDelegator
sys.modules["cloudsubscribex.core"].CloudFile = MagicMock()


class _FakeColumn:
    """模拟 DownloadHistory.download_hash 列：like() 只记录 pattern。"""

    def __init__(self, store):
        self._store = store

    def like(self, pattern):
        self._store["pattern"] = pattern
        return ("like", pattern)


class _FakeQuery:
    def __init__(self, hashes, store):
        self._hashes = list(hashes)
        self._store = store

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        pattern = str(self._store.get("pattern") or "")
        needle = pattern.strip("%").upper()
        if not needle:
            return None
        for value in self._hashes:
            if needle in str(value or "").upper():
                return SimpleNamespace(download_hash=value)
        return None


class _FakeSession:
    def __init__(self, hashes, store):
        self._hashes = hashes
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def query(self, *args, **kwargs):
        return _FakeQuery(self._hashes, self._store)


# transfer_details.py 在 import 时就绑定 SessionFactory / DownloadHistory，
# 所以假实现必须在 load 之前装好，并用可变状态让各用例换数据。
_FAKE_HASHES = []
_FAKE_STORE = {}
_FAKE_RAISE = {"enabled": False}


def _fake_session_factory():
    if _FAKE_RAISE["enabled"]:
        raise RuntimeError("db down")
    return _FakeSession(_FAKE_HASHES, _FAKE_STORE)


sys.modules["app.db"].SessionFactory = _fake_session_factory
sys.modules["app.db.models.downloadhistory"].DownloadHistory = type(
    "DownloadHistory", (), {"download_hash": _FakeColumn(_FAKE_STORE)}
)

_transfer_details = load_module(
    "cloudsubscribex.handlers.sync.transfer_details",
    "plugins.v2/cloudsubscribex/handlers/sync/transfer_details.py",
)
TransferDetailsService = _transfer_details.TransferDetailsService


def _install_fake_download_history():
    """在 sys.modules 里装上内存假实现。

    is_offline_task_finalized 在方法内才 import SessionFactory /
    DownloadHistory，所以必须在每个用例跑之前（setUp）重装一次：
    全量套件里后收集的文件会在收集期重注册 app.db 占位模块，
    只在 import 时装一次会被覆盖（全量曾因此让否定用例全挂）。
    """
    sys.modules["app.db"].SessionFactory = _fake_session_factory
    sys.modules["app.db.models.downloadhistory"].DownloadHistory = type(
        "DownloadHistory", (), {"download_hash": _FakeColumn(_FAKE_STORE)}
    )


class _BaseTest(unittest.TestCase):
    def setUp(self):
        _install_fake_download_history()
        self.service = object.__new__(TransferDetailsService)
        object.__setattr__(self.service, "_owner", types.SimpleNamespace())
        _FAKE_HASHES.clear()
        _FAKE_STORE.clear()
        _FAKE_RAISE["enabled"] = False

    def _with_download_hashes(self, hashes):
        _FAKE_HASHES[:] = list(hashes)


INFO_HASH = "3F1A4C2B5D6E7F80912A3B4C5D6E7F8091A2B3C4"
MAGNET_URL = (
    "magnet:?xt=urn:btih:3f1a4c2b5d6e7f80912a3b4c5d6e7f8091a2b3c4"
    "&dn=Onslaught.2026.2160p.iT.WEB-DL.DDP5.1.Atmos.DV.HDR.H.265-BYNDR"
)


class OfflineFinalizedFlagTest(_BaseTest):
    """MP 下载历史里有该 hash 的磁力记录才算已入库。"""

    def test_matching_download_history_marks_finalized(self):
        self._with_download_hashes([MAGNET_URL])
        self.assertTrue(self.service.is_offline_task_finalized(INFO_HASH))

    def test_unrelated_hash_is_not_finalized(self):
        self._with_download_hashes(
            ["https://pan.quark.cn/s/4df950a3b4b6"]
        )
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))

    def test_lowercase_hash_matches_uppercase_record(self):
        self._with_download_hashes([MAGNET_URL.upper()])
        self.assertTrue(self.service.is_offline_task_finalized(INFO_HASH.lower()))

    def test_empty_hash_is_not_finalized(self):
        self._with_download_hashes([MAGNET_URL])
        self.assertFalse(self.service.is_offline_task_finalized(""))
        self.assertFalse(self.service.is_offline_task_finalized(None))

    def test_empty_download_history_is_not_finalized(self):
        self._with_download_hashes([])
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))

    def test_query_failure_is_not_finalized(self):
        _FAKE_RAISE["enabled"] = True
        self.assertFalse(self.service.is_offline_task_finalized(INFO_HASH))


class OfflineFinalizedApiContractTest(unittest.TestCase):
    """列表接口必须只加展示字段，不得污染完成判定字段。"""

    # v1.24.0：_merge_pending_finalize_tasks（含 finalized 标注）从
    # runtime.py 搬到了 core/services/offline.py，契约跟过去。
    OFFLINE_FILE = "plugins.v2/cloudsubscribex/core/services/offline.py"

    def setUp(self):
        with open(self.OFFLINE_FILE, encoding="utf-8") as handle:
            self.source = handle.read()

    def test_finalized_fields_are_set(self):
        self.assertIn('task["finalized"] = True', self.source)
        self.assertIn('task["finalized_text"] = "已入库"', self.source)

    def test_completion_fields_are_not_touched(self):
        self.assertNotIn('task["completed"] = True', self.source)
        self.assertNotIn('task["state"] = "completed"', self.source)

    def test_pending_tasks_are_skipped(self):
        self.assertIn("if task.get(\"finalize_pending\"):", self.source)


if __name__ == "__main__":
    unittest.main()
