"""v1.12.6 C：MpOrganizeTracker 轮询 MP 整理历史 / 人工复核登记 / 队列。

约定（MP v3 源码已核对）：
- 成功只认 TransferHistory.status 为 True（get_success_by_src 精确匹配，
  兜底 get_by_src）；不用任何目标存在性判断——目标可能只是同名小文件。
- 失败原因原样透传 errmsg/last_error（截断到约 200 字），不解析、不改写；
  MP 给的经常是通用句“整理操作多次失败，请稍后重试”，目标路径尚未生成。
- 人工复核是 TransferPending.execution_state == "manual_review"（某步骤
  materialize_target 执行异常后外部结果 unknown、禁止自动重放，原因在
  last_error，如“上传 u115cs 失败”），按失败处理，不当 queued/running，
  不自动重试。
- 队列任务 state 取值：waiting（排队）/ running / completed / failed；
  completed 但历史尚未结算时仍按进行中，等下一轮历史。
"""

import unittest
from types import SimpleNamespace

from plugin_env import install_app_mocks, load_module

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.utils", "app.utils.http", "app.helper", "app.application", "app.adapters",
)

tracker_module = load_module(
    "cloudsubscribex.core.services.mp_organize_tracker",
    "plugins.v2/cloudsubscribex/core/services/mp_organize_tracker.py",
)
MpOrganizeTracker = tracker_module.MpOrganizeTracker

SRC = "/downloads/吞噬星空 {tmdb-101172} - S01E234 - 第234集.mkv"
DEST = "/转存/吞噬星空 (2020) {tmdb-101172}/Season 1/吞噬星空 {tmdb-101172} - S01E234 - 第234集.mkv"


def _mp_organize(src_path=SRC, triggered_at=1000000.0):
    return {
        "src_path": src_path,
        "src_storage": "local",
        "triggered_at": triggered_at,
        "target_dir": "/媒体库/剧集",
        "target_storage": "u115cs",
        "target_name": "剧集",
        "file_name": "吞噬星空 {tmdb-101172} - S01E234 - 第234集.mkv",
    }


class FakeHistoryOper:
    """桩整理历史 Oper：按预设返回成功/失败记录，并记录查询参数。"""

    def __init__(self, success=None, by_src=None):
        self.success = success
        self.by_src = by_src
        self.calls = []

    def get_success_by_src(self, src, storage=None):
        self.calls.append(("get_success_by_src", src, storage))
        return self.success

    def get_by_src(self, src, storage=None):
        self.calls.append(("get_by_src", src, storage))
        return self.by_src


class FakePendingOper:
    """桩 durable 待整理登记 Oper。"""

    def __init__(self, record=None):
        self.record = record
        self.calls = []

    def get_by_identity(self, *, storage, src_path):
        self.calls.append(("get_by_identity", storage, src_path))
        return self.record


class FakeJobview:
    """桩 MP 整理队列视图。"""

    def __init__(self, jobs=None):
        self.jobs = jobs or []

    def list_jobs(self):
        return self.jobs


def _job(src_path, state, storage="local"):
    return SimpleNamespace(tasks=[
        SimpleNamespace(
            fileitem=SimpleNamespace(path=src_path, storage=storage),
            state=state,
        )
    ])


class TestMpOrganizeTrackerHistory(unittest.TestCase):
    def setUp(self):
        tracker_module.reset_mp_organizing()

    def test_history_success_returns_succeeded_with_exact_dest(self):
        """历史有成功记录 → succeeded，detail 与 dest 均为精确 dest。"""
        history = FakeHistoryOper(
            success=SimpleNamespace(dest=DEST, status=True, errmsg="")
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, dest = poller.poll(_mp_organize())
        self.assertEqual(state, "succeeded")
        self.assertEqual(detail, DEST)
        self.assertEqual(dest, DEST)
        self.assertEqual(history.calls[0], ("get_success_by_src", SRC, "local"))

    def test_history_success_by_src_missed_but_status_true(self):
        """成功查询未命中、但 get_by_src 的记录 status 为 True → succeeded。"""
        history = FakeHistoryOper(
            success=None,
            by_src=SimpleNamespace(dest=DEST, status=True, errmsg=""),
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "succeeded")

    def test_history_failure_uses_errmsg_verbatim(self):
        """历史失败记录 → failed，detail 为 errmsg 原文（通用句也不改写）。"""
        errmsg = "⚠ 整理操作多次失败，请稍后重试"
        history = FakeHistoryOper(
            success=None,
            by_src=SimpleNamespace(dest="", status=False, errmsg=errmsg),
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, dest = poller.poll(_mp_organize())
        self.assertEqual(state, "failed")
        self.assertEqual(detail, errmsg)
        self.assertIsNone(dest)

    def test_history_failure_errmsg_truncated_to_200(self):
        """超长 errmsg 截断到约 200 字，不丢原文开头。"""
        errmsg = "失" + "败原因很长" * 60
        self.assertGreater(len(errmsg), 200)
        history = FakeHistoryOper(
            success=None,
            by_src=SimpleNamespace(dest="", status=False, errmsg=errmsg),
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, _dest = poller.poll(_mp_organize())
        self.assertEqual(state, "failed")
        self.assertEqual(detail, errmsg[:200] + "…")
        self.assertLessEqual(len(detail), 201)

    def test_history_failure_without_errmsg_has_fallback(self):
        """失败记录无 errmsg 时给一句兜底，不返回空 detail。"""
        history = FakeHistoryOper(
            success=None,
            by_src=SimpleNamespace(dest="", status=False, errmsg=""),
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, _dest = poller.poll(_mp_organize())
        self.assertEqual(state, "failed")
        self.assertTrue(detail)


class TestMpOrganizeTrackerPendingAndQueue(unittest.TestCase):
    def setUp(self):
        tracker_module.reset_mp_organizing()

    def test_manual_review_pending_is_failed_with_last_error(self):
        """人工复核登记 → failed，detail 为 last_error 原文（如上传失败）。"""
        pending = FakePendingOper(
            record=SimpleNamespace(
                execution_state="manual_review",
                last_error="上传 u115cs 失败",
            )
        )
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=pending,
            jobview=FakeJobview(),
        )
        state, detail, dest = poller.poll(_mp_organize())
        self.assertEqual(state, "failed")
        self.assertEqual(detail, "上传 u115cs 失败")
        self.assertIsNone(dest)
        self.assertEqual(
            pending.calls, [("get_by_identity", "local", SRC)]
        )

    def test_failed_pending_execution_state_is_failed(self):
        """durable 登记已终态失败 → failed，detail 为 last_error 原文。"""
        pending = FakePendingOper(
            record=SimpleNamespace(
                execution_state="failed",
                last_error="目标存储不可用",
            )
        )
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=pending,
            jobview=FakeJobview(),
        )
        state, detail, _dest = poller.poll(_mp_organize())
        self.assertEqual(state, "failed")
        self.assertEqual(detail, "目标存储不可用")

    def test_running_pending_is_not_terminal(self):
        """durable 登记还在跑 → 不判失败，继续看队列/缺失。"""
        pending = FakePendingOper(
            record=SimpleNamespace(execution_state="running", last_error="")
        )
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=pending,
            jobview=FakeJobview(),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "missing")

    def test_queue_waiting_is_queued(self):
        """无历史记录、队列里有同 src 的 waiting 任务 → queued。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "waiting")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "queued")

    def test_queue_running_is_running(self):
        """无历史记录、队列里有同 src 的 running 任务 → running。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "running")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "running")

    def test_queue_completed_without_history_is_running(self):
        """队列已 completed 但历史尚未结算 → 仍按进行中，等下一轮历史。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "completed")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "running")

    def test_queue_failed_is_failed(self):
        """队列任务已 failed → failed。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "failed")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "failed")

    def test_queue_manual_review_is_failed(self):
        """队列任务是 manual_review 态 → failed，不当 queued/running。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "manual_review")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "failed")

    def test_queue_storage_mismatch_is_missing(self):
        """队列里同路径但 storage 对不上 → 不认，missing。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "waiting", storage="u115cs")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "missing")

    def test_nothing_anywhere_is_missing(self):
        """历史、登记、队列皆无 → missing。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, dest = poller.poll(_mp_organize())
        self.assertEqual(state, "missing")
        self.assertIsNone(dest)
        self.assertTrue(detail)

    def test_history_success_beats_queue(self):
        """历史已成功时不再看队列（队列里可能还残留旧任务）。"""
        history = FakeHistoryOper(
            success=SimpleNamespace(dest=DEST, status=True, errmsg="")
        )
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview([_job(SRC, "waiting")]),
        )
        self.assertEqual(poller.poll(_mp_organize())[0], "succeeded")

    def test_mp_backend_unavailable_returns_missing_without_raise(self):
        """MP 侧 import/查询抛异常 → missing，不抛异常。"""
        history = FakeHistoryOper()
        history.get_success_by_src = _raising
        poller = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        state, detail, dest = poller.poll(_mp_organize())
        self.assertEqual(state, "missing")
        self.assertEqual(detail, "MP 整理链不可用")
        self.assertIsNone(dest)

    def test_missing_src_path_is_missing(self):
        """身份里没有 src_path → missing。"""
        poller = MpOrganizeTracker(
            history_oper=FakeHistoryOper(),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        self.assertEqual(poller.poll({})[0], "missing")


def _raising(*args, **kwargs):
    raise ImportError("app.db.transferhistory_oper 不可用")


class TestMpOrganizingRegistryBySrc(unittest.TestCase):
    def setUp(self):
        tracker_module.reset_mp_organizing()

    def test_clear_by_src_removes_all_entries_with_same_src(self):
        """按源路径清除：同一 src 的多条登记（不同 parent）一次清掉。"""
        info = _mp_organize()
        tracker_module.mark_mp_organizing("parent-1", info)
        tracker_module.mark_mp_organizing("parent-2", info)
        tracker_module.mark_mp_organizing(
            "parent-3", _mp_organize(src_path="/downloads/其它.mkv")
        )
        tracker_module.clear_mp_organizing_by_src(SRC)
        remaining = tracker_module.list_mp_organizing()
        self.assertNotIn("parent-1", remaining)
        self.assertNotIn("parent-2", remaining)
        self.assertIn("parent-3", remaining)

    def test_update_by_src_sets_status_text(self):
        """按源路径更新文案：同一 src 的多条登记都更新，只改文案与时间。"""
        info = _mp_organize()
        tracker_module.mark_mp_organizing("parent-1", info)
        tracker_module.mark_mp_organizing("parent-2", info)
        tracker_module.update_mp_organizing_by_src(
            SRC, "整理停滞（队头阻塞，后续任务排队中）"
        )
        for key in ("parent-1", "parent-2"):
            entry = tracker_module.get_mp_organizing(key)
            self.assertEqual(
                entry["status_text"], "整理停滞（队头阻塞，后续任务排队中）"
            )
            self.assertEqual(entry["mp_organize"]["src_path"], SRC)

    def test_clear_by_src_with_unknown_src_is_noop(self):
        """按不存在的源路径清除是空操作，不抛异常。"""
        tracker_module.mark_mp_organizing("parent-1", _mp_organize())
        tracker_module.clear_mp_organizing_by_src("/downloads/不存在.mkv")
        self.assertIn("parent-1", tracker_module.list_mp_organizing())
