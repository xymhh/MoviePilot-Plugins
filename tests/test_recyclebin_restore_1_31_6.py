"""v1.31.6 115 回收站一键恢复。

覆盖：
- 窗口过滤（窗口内保留、窗口外丢弃、dtime 缺失丢弃）；
- 分页与倒序提前停（整页早于窗口下限且倒序时不再翻页）；
- 只还原确认过的 rid（多传的 rid 不进 revert 调用）；
- 还原后核对（重查回收站：rid 消失算成功、仍在算失败；原路径复查；
  还原接口异常也不等于没还原，核对说了算）；
- 恢复路径绝不做永久删除/清空（源码级断言：新服务与 drive 层不得出现
  永久删除调用）；
- API 层两端点转发到 SyncHandler。

harness：假 drive（fake list_recyclebin / revert / 重查回收站 /
recyclebin_info / find_file），不加载真实 DB；本文件只新增两个模块名
进 sys.modules（服务与 API），在 tearDownClass 里恢复；不改共享 mock
包上的父属性（无 monkeypatch），与 test_strm_duplicates_1_31_0 写法一致。
"""

import os
import sys
import unittest
from types import SimpleNamespace

from plugin_env import (  # noqa: E402
    install_app_mocks,
    install_core_delegation,
    load_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

# 只恢复本文件新增的两个模块名；共享 mock 包沿用已有写法，不动。
_ADDED_MODULES = (
    "cloudsubscribex.handlers.sync.recyclebin_restore",
    "cloudsubscribex.core.api.recyclebin_restore",
)
_SAVED_ADDED = {name: sys.modules.get(name) for name in _ADDED_MODULES}

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.schemas", "app.schemas.types", "app.utils", "app.utils.http",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.utils", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
)
install_core_delegation()

restore_module = load_module(
    "cloudsubscribex.handlers.sync.recyclebin_restore",
    "plugins.v2/cloudsubscribex/handlers/sync/recyclebin_restore.py",
)
RecyclebinRestoreService = restore_module.RecyclebinRestoreService

api_module = load_module(
    "cloudsubscribex.core.api.recyclebin_restore",
    "plugins.v2/cloudsubscribex/core/api/recyclebin_restore.py",
)
RecyclebinRestoreApi = api_module.RecyclebinRestoreApi

# 默认窗口 2026-11-23 16:25–16:55（+08:00）；事发 16:27:32–16:49:27。
WINDOW_START = 1795422300
WINDOW_END = 1795424100
IN_INCIDENT_1 = 1795422452  # 16:27:32
IN_INCIDENT_2 = 1795423767  # 16:49:27
BEFORE_WINDOW = 1795422000
AFTER_WINDOW = 1795425000


class _RestoreBase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for name, module in _SAVED_ADDED.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _raw(rid, dtime, name="视频.mkv", size=1024, is_dir=False):
    item = {"rid": rid, "fn": name, "s": size, "dtime": dtime}
    if is_dir:
        item["is_dir"] = True
    return item


class _FakeQuery:
    """假网盘查询：按顺序吐出预设的回收站页；记录全部调用。"""

    def __init__(self, pages, info=None, found=None, raise_on_call=None):
        self.pages = [list(page) for page in pages]
        self.info = dict(info or {})
        self.found = dict(found or {})
        self.raise_on_call = raise_on_call
        self.calls = []

    def list_recyclebin(self, limit=100, offset=0):
        self.calls.append(("list_recyclebin", limit, offset))
        if self.raise_on_call is not None and len(self.calls) == self.raise_on_call:
            raise RuntimeError("115 列表异常")
        if not self.pages:
            return []
        return self.pages.pop(0)

    def recyclebin_info(self, rid):
        self.calls.append(("recyclebin_info", str(rid)))
        return dict(self.info.get(str(rid)) or {})

    def find_file(self, path, file_name):
        self.calls.append(("find_file", path, file_name))
        return self.found.get((path, file_name))


class _FakeMutations:
    """假网盘变更：只记录 revert 调用，不做真还原。"""

    def __init__(self, fail=False):
        self.calls = []
        self.reverted = []
        self._fail = fail

    def revert_recyclebin(self, rids):
        self.calls.append(("revert_recyclebin", list(rids)))
        if self._fail:
            raise RuntimeError("115 还原接口异常")
        self.reverted.append(list(rids))
        return True


def _restore_service(query=None, mutations=None):
    owner = SimpleNamespace(_cloud_query=query, _cloud_mutations=mutations)
    return RecyclebinRestoreService(owner)


class ListCandidatesTests(_RestoreBase):
    def test_window_filter_keeps_only_in_window(self):
        query = _FakeQuery([[
            _raw("in1", IN_INCIDENT_1, "痴迷 (2026) - 2160p.mkv", 5_000_000_000),
            _raw("in2", IN_INCIDENT_2, "玉簟秋 - S01E01 - 第1集.mkv", 1_000_000_000),
            _raw("before", BEFORE_WINDOW, "旧文件.mkv"),
            _raw("after", AFTER_WINDOW, "新文件.mkv"),
            {"rid": "no-dtime", "fn": "没时间.mkv", "s": 10},
            {"fn": "没rid.mkv", "s": 10, "dtime": IN_INCIDENT_1},
        ]])
        service = _restore_service(query=query)
        result = service.restore_list_candidates(WINDOW_START, WINDOW_END)
        self.assertTrue(result["success"])
        data = result["data"]
        self.assertEqual(
            [item["rid"] for item in data["candidates"]], ["in2", "in1"],
        )
        self.assertEqual(data["window_start"], WINDOW_START)
        self.assertEqual(data["window_end"], WINDOW_END)
        self.assertEqual(data["scanned"], 6)

    def test_default_window_is_incident_window(self):
        query = _FakeQuery([[_raw("in1", IN_INCIDENT_1)]])
        service = _restore_service(query=query)
        result = service.restore_list_candidates()
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["window_start"], WINDOW_START)
        self.assertEqual(result["data"]["window_end"], WINDOW_END)
        self.assertEqual(len(result["data"]["candidates"]), 1)

    def test_early_stop_when_descending_past_window(self):
        page1 = [
            _raw(f"new{i}", AFTER_WINDOW + 100 - i, f"新{i}.mkv")
            for i in range(100)
        ]
        page2 = [
            _raw(f"old{i}", BEFORE_WINDOW - i, f"旧{i}.mkv")
            for i in range(100)
        ]
        page3 = [_raw("never", BEFORE_WINDOW - 500, "不应被读到.mkv")]
        query = _FakeQuery([page1, page2, page3])
        service = _restore_service(query=query)
        result = service.restore_list_candidates(WINDOW_START, WINDOW_END)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [])
        list_calls = [
            call for call in query.calls if call[0] == "list_recyclebin"
        ]
        self.assertEqual(len(list_calls), 2)

    def test_list_failure_returns_failure_not_partial(self):
        query = _FakeQuery([[_raw("in1", IN_INCIDENT_1)]], raise_on_call=1)
        service = _restore_service(query=query)
        result = service.restore_list_candidates(WINDOW_START, WINDOW_END)
        self.assertFalse(result["success"])
        self.assertIn("读取回收站失败", result["message"])

    def test_list_without_query_returns_failure(self):
        service = _restore_service(query=None)
        result = service.restore_list_candidates(WINDOW_START, WINDOW_END)
        self.assertFalse(result["success"])


class RevertTests(_RestoreBase):
    def _service_with_verify(self, verify_page, mutations=None, **query_kwargs):
        # restore_revert 不先列候选：核对重查就是第一次 list 调用，
        # 所以 pages 里只放核对页。
        query = _FakeQuery([verify_page], **query_kwargs)
        return _restore_service(query=query, mutations=mutations or _FakeMutations()), query

    def test_revert_only_confirmed_rids(self):
        mutations = _FakeMutations()
        query = _FakeQuery([
            [_raw("a", IN_INCIDENT_1, "A.mkv"), _raw("b", IN_INCIDENT_2, "B.mkv")],
            [],
        ])
        service = _restore_service(query=query, mutations=mutations)
        listed = service.restore_list_candidates(WINDOW_START, WINDOW_END)
        candidates = listed["data"]["candidates"]
        result = service.restore_revert(
            [candidates[0]], WINDOW_START, WINDOW_END,
        )
        self.assertTrue(result["success"])
        self.assertEqual(mutations.reverted, [["b"]])
        self.assertEqual(
            [item["rid"] for item in result["data"]["restored"]], ["b"],
        )
        self.assertEqual(result["data"]["failed"], [])

    def test_revert_still_present_is_failed(self):
        service, _query = self._service_with_verify(
            [_raw("a", IN_INCIDENT_1, "A.mkv")],
        )
        result = service.restore_revert(
            [{"rid": "a", "name": "A.mkv"}, {"rid": "b", "name": "B.mkv"}],
            WINDOW_START, WINDOW_END,
        )
        self.assertFalse(result["success"])
        self.assertEqual(
            [item["rid"] for item in result["data"]["restored"]], ["b"],
        )
        self.assertEqual(len(result["data"]["failed"]), 1)
        self.assertEqual(result["data"]["failed"][0]["rid"], "a")
        self.assertIn("仍在回收站", result["data"]["failed"][0]["reason"])

    def test_revert_verifies_original_path_when_known(self):
        full_path = (
            "/Alist/影视/moviepilot/媒体库/电影/外语电影/痴迷 (2026) {tmdb-1339713}/"
            "痴迷 (2026) - 2160p.mkv"
        )
        parent = "/Alist/影视/moviepilot/媒体库/电影/外语电影/痴迷 (2026) {tmdb-1339713}"
        name = "痴迷 (2026) - 2160p.mkv"
        query = _FakeQuery(
            [[]],
            found={(parent, name): object()},
        )
        service = _restore_service(query=query, mutations=_FakeMutations())
        result = service.restore_revert(
            [{"rid": "a", "name": name, "original_path": full_path}],
            WINDOW_START, WINDOW_END,
        )
        self.assertTrue(result["success"])
        restored = result["data"]["restored"]
        self.assertEqual(len(restored), 1)
        self.assertIs(restored[0]["path_verified"], True)
        self.assertIn(("find_file", parent, name), query.calls)

    def test_revert_path_missing_still_restored(self):
        full_path = "/Alist/影视/moviepilot/媒体库/电影/痴迷 (2026)/痴迷.mkv"
        parent = "/Alist/影视/moviepilot/媒体库/电影/痴迷 (2026)"
        query = _FakeQuery(
            [[]],
            found={(parent, "痴迷.mkv"): None},
        )
        service = _restore_service(query=query, mutations=_FakeMutations())
        result = service.restore_revert(
            [{"rid": "a", "name": "痴迷.mkv", "original_path": full_path}],
            WINDOW_START, WINDOW_END,
        )
        self.assertTrue(result["success"])
        self.assertIs(result["data"]["restored"][0]["path_verified"], False)

    def test_revert_resolves_path_via_recyclebin_info(self):
        full_path = "/Alist/影视/moviepilot/媒体库/剧集/国产剧/玉簟秋/Season 1/玉簟秋 - S01E01 - 第1集.mkv"
        parent = "/Alist/影视/moviepilot/媒体库/剧集/国产剧/玉簟秋/Season 1"
        name = "玉簟秋 - S01E01 - 第1集.mkv"
        query = _FakeQuery(
            [[]],
            info={"a": {"path": full_path}},
            found={(parent, name): object()},
        )
        service = _restore_service(query=query, mutations=_FakeMutations())
        result = service.restore_revert(
            [{"rid": "a", "name": name}], WINDOW_START, WINDOW_END,
        )
        self.assertTrue(result["success"])
        self.assertIs(result["data"]["restored"][0]["path_verified"], True)
        self.assertIn(("recyclebin_info", "a"), query.calls)

    def test_revert_empty_rids_returns_failure(self):
        service = _restore_service(
            query=_FakeQuery([[]]), mutations=_FakeMutations(),
        )
        result = service.restore_revert([], WINDOW_START, WINDOW_END)
        self.assertFalse(result["success"])
        self.assertIn("未指定", result["message"])

    def test_revert_call_error_still_verifies(self):
        query = _FakeQuery([[]])
        service = _restore_service(
            query=query, mutations=_FakeMutations(fail=True),
        )
        result = service.restore_revert(
            [{"rid": "a", "name": "A.mkv"}], WINDOW_START, WINDOW_END,
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            [item["rid"] for item in result["data"]["restored"]], ["a"],
        )
        self.assertIn("还原接口异常", result["message"])

    def test_revert_verify_list_failure_fails_all(self):
        # 核对重查是第 2 次 query 调用（第 1 次是还原前的 recyclebin_info）。
        query = _FakeQuery([[]], raise_on_call=2)
        service = _restore_service(query=query, mutations=_FakeMutations())
        result = service.restore_revert(
            [{"rid": "a", "name": "A.mkv"}], WINDOW_START, WINDOW_END,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["data"]["restored"], [])
        self.assertEqual(len(result["data"]["failed"]), 1)


class NoPermanentDeleteTests(_RestoreBase):
    def test_restore_path_never_calls_permanent_delete(self):
        """恢复路径只做还原：新服务与 drive 层源码里不得出现永久删除调用。"""
        for relative in (
            "plugins.v2/cloudsubscribex/handlers/sync/recyclebin_restore.py",
            "plugins.v2/cloudsubscribex/drive/p115/files.py",
        ):
            path = os.path.join(project_root, relative)
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            self.assertNotIn("recyclebin_clean", source, relative)


class ApiDelegationTests(_RestoreBase):
    def _api(self, handler=None, failure=None):
        owner = SimpleNamespace(
            _require_sync_handler=lambda *names: (handler, failure),
        )
        return RecyclebinRestoreApi(owner)

    def test_list_revert_delegate(self):
        calls = {}
        handler = SimpleNamespace(
            restore_list_candidates=lambda start, end: calls.setdefault(
                "list", {"success": True},
            ),
            restore_revert=lambda items, start, end: calls.setdefault(
                "revert", {"success": True},
            ),
        )
        api = self._api(handler=handler)
        self.assertEqual(
            api.api_vue_recyclebin_list(
                {"window_start": WINDOW_START, "window_end": WINDOW_END},
            ),
            {"success": True},
        )
        self.assertEqual(
            api.api_vue_recyclebin_revert(
                {"items": [{"rid": "a"}],
                "window_start": WINDOW_START, "window_end": WINDOW_END},
            ),
            {"success": True},
        )
        self.assertEqual(set(calls), {"list", "revert"})

    def test_missing_handler_returns_failure(self):
        failure = {"success": False, "message": "同步服务未就绪"}
        api = self._api(handler=None, failure=failure)
        self.assertEqual(api.api_vue_recyclebin_list({}), failure)
        self.assertEqual(api.api_vue_recyclebin_revert({"items": [{"rid": "a"}]}), failure)

    def test_revert_without_items_returns_failure(self):
        handler = SimpleNamespace(
            restore_revert=lambda items, start, end: {"success": True},
        )
        api = self._api(handler=handler)
        result = api.api_vue_recyclebin_revert({})
        self.assertFalse(result["success"])
        self.assertIn("未指定", result["message"])


if __name__ == "__main__":
    unittest.main()
