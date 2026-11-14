"""1.9.93：手动/MP 搜索转存完成通知。

覆盖：
* 后台收尾 `_notify_pending_file_finalized`：媒体信息恢复失败时也不丢通知，
  改用后处理项与历史记录拼一份同口径详情（无海报），走同一条完成通知；
* `send_transfer_notification`：_notify 开时发、关时不发，文案与订阅同步
  同一份（转存完成标题 + 标题/季集行），跨盘用「跨盘转存完成」标题；
* 后处理失败结算 `_abort_single_item`：直接 abort 也走已有的死任务失败通知
  口径（同一标题/文案、同一 _notify 开关），用户主动停止不发失败通知，
  死任务路径只通知一次。

注意：本文件不写字面全角方括号，用转义拼通知标题（写工具会剥掉它们）。
"""

from __future__ import annotations

import os
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (  # noqa: E402
    OwnerDelegator,
    install_app_mocks,
    install_core_delegation,
    load_module,
    set_media_type,
)

install_app_mocks(
    "app.chain", "app.chain.mediaserver", "app.core", "app.core.context",
    "app.core.metainfo", "app.db", "app.db.downloadhistory_oper",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.models.subscribe", "app.db.subscribe_oper",
    "app.helper", "app.helper.mediaserver", "app.application",
    "app.application.mediaserver", "app.log", "app.schemas.types",
    "app.utils", "app.utils.string", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.history",
    "cloudsubscribex.core.media", "cloudsubscribex.drive",
    "cloudsubscribex.drive.common", "cloudsubscribex.search",
    "cloudsubscribex.search.types", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.search.subs_filter", "cloudsubscribex.utils",
)
install_core_delegation()
_MEDIA_TYPE = set_media_type(movie="电影", tv="电视剧")
core_pkg = sys.modules["cloudsubscribex.core"]
core_pkg.OwnerDelegator = OwnerDelegator
core_pkg.CloudDriveCapability = MagicMock()
core_pkg.CloudFile = MagicMock()

_history = load_module(
    "cloudsubscribex.handlers.sync.history",
    "plugins.v2/cloudsubscribex/handlers/sync/history.py",
)
_notify_mod = load_module(
    "cloudsubscribex.handlers.sync.notify",
    "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
)
_postprocess = load_module(
    "cloudsubscribex.handlers.sync.postprocess",
    "plugins.v2/cloudsubscribex/handlers/sync/postprocess.py",
)

# jinja2 未装时渲染模板不可用：通知文案断言走 render 的 fallback 正文，
# 该正文本身就是生产代码拼出的同一份文案。
_notify_mod.render_template = lambda template, fallback, **kwargs: fallback
_notify_mod.resolve_template = lambda templates, key: ""
_notify_mod.format_size = lambda value, default="": (
    f"{int(value or 0)} B" if int(value or 0) > 0 else ""
)

TITLE_TRANSFER_DONE = "\u3010\u7f51\u76d8\u8ba2\u9605\u52a9\u624b\u3011\u8f6c\u5b58\u5b8c\u6210"
TITLE_CROSS_DONE = "\u3010\u7f51\u76d8\u8ba2\u9605\u52a9\u624b\u3011\u8de8\u76d8\u8f6c\u5b58\u5b8c\u6210"
TITLE_DEAD = "网盘文件后处理失败"

PENDING_KEY = "pending:lvzz35e05"
HISTORY_RECORD = {
    "finalize_key": PENDING_KEY,
    "title": "律界战争",
    "type": "电视剧",
    "year": "2025",
    "season": 1,
    "episode": 5,
    "file_name": "律界战争 S01E05.mkv",
    "file_size": 123456789,
    "status": "成功",
    "transfer_mode": "cross",
    "target_drive_name": "115",
    "source_drive_name": "夸克",
}


class _HistoryService(_history.HistoryService):
    """内存历史存储 + 可控的媒体恢复。"""

    def __init__(self, records, restore=(None, {})):
        super().__init__(None)
        object.__setattr__(self, "_store", {"history": [dict(r) for r in records]})
        object.__setattr__(self, "_offline_pending_lock", threading.RLock())
        object.__setattr__(self, "_restore", restore)
        object.__setattr__(self, "finish_calls", [])

    def _get_data(self, key):
        return self._store.get(key)

    def _save_data(self, key, value):
        self._store[key] = value

    def _restore_pending_media_context(self, item, pending_key):
        return self._restore

    def _finish_pending_subscription(self, item, media_data, mediainfo=None):
        self.finish_calls.append((item, mediainfo))


def _pending_item(**extra):
    item = {
        "pending_key": PENDING_KEY,
        # subscribe_id 置 0：绕开宿主 SubscribeOper 查询，只覆盖详情拼装本身。
        "subscribe_id": 0,
        "file_name": "律界战争 S01E05.mkv",
        "season": 1,
        "notification_episodes": [5],
        "success_episodes": [5],
        "cloud_dir": "/媒体/电视剧/律界战争 (2025)/Season 1",
    }
    item.update(extra)
    return item


class TestFinalizedFallbackDetail(unittest.TestCase):
    def test_mediainfo_missing_still_builds_cross_transfer_detail(self):
        service = _HistoryService([HISTORY_RECORD])
        detail = service._notify_pending_file_finalized(
            _pending_item(), PENDING_KEY, None
        )
        self.assertIsNotNone(detail)
        self.assertEqual(detail["type"], "电视剧")
        self.assertEqual(detail["title"], "律界战争")
        self.assertEqual(detail["season"], 1)
        self.assertEqual(detail["episodes"], [5])
        self.assertEqual(detail["notification_kind"], "cross_transfer")
        self.assertIsNone(detail["image"])
        self.assertEqual(detail["size"], 123456789)
        self.assertEqual(detail["source"], "夸克 → 115")

    def test_mediainfo_missing_movie_has_no_season(self):
        record = {
            **HISTORY_RECORD,
            "type": "电影",
            "title": "天使与恶徒",
            "season": None,
            "episode": None,
            "transfer_mode": "",
        }
        service = _HistoryService([record])
        detail = service._notify_pending_file_finalized(
            _pending_item(season=None, notification_episodes=[], success_episodes=[]),
            PENDING_KEY,
            None,
        )
        self.assertEqual(detail["type"], "电影")
        self.assertNotIn("season", detail)
        self.assertNotIn("episodes", detail)
        self.assertEqual(detail["notification_kind"], "transfer")

    def test_mediainfo_present_keeps_poster(self):
        mediainfo = SimpleNamespace(
            type=_MEDIA_TYPE.TV,
            title="律界战争",
            year="2025",
            season=1,
            get_poster_image=lambda: "https://image.tmdb.org/poster.jpg",
        )
        service = _HistoryService([HISTORY_RECORD])
        detail = service._notify_pending_file_finalized(
            _pending_item(), PENDING_KEY, None,
            mediainfo=mediainfo, media_data={"title": "律界战争"},
        )
        self.assertEqual(detail["image"], "https://image.tmdb.org/poster.jpg")
        self.assertEqual(detail["episodes"], [5])
        self.assertEqual(detail["notification_kind"], "cross_transfer")


class _NotifyService(_notify_mod.SyncNotificationService):
    def __init__(self, notify=True):
        super().__init__(None)
        object.__setattr__(self, "_notify", bool(notify))
        object.__setattr__(self, "_notification_type", "Plugin")
        object.__setattr__(self, "_notification_delay_seconds", 0)
        object.__setattr__(self, "_notify_templates", {})
        object.__setattr__(self, "sent", [])

    def _post_message(self, **kwargs):
        self.sent.append(kwargs)


class TestTransferNotificationGate(unittest.TestCase):
    def _detail(self, kind="transfer"):
        return {
            "type": "电视剧",
            "title": "律界战争",
            "year": "2025",
            "season": 1,
            "episodes": [5],
            "size": 123456789,
            "source": "夸克 → 115",
            "notification_kind": kind,
        }

    def test_notify_on_sends_same_copy_as_scheduled_sync(self):
        service = _NotifyService(notify=True)
        service.send_transfer_notification([self._detail()], 1)
        service._flush_transfer_notifications()
        self.assertEqual(len(service.sent), 1)
        message = service.sent[0]
        self.assertEqual(message["title"], TITLE_TRANSFER_DONE)
        self.assertIn("律界战争 S01 E05", message["text"])
        self.assertIn("本次共转存 1 个文件", message["text"])

    def test_cross_transfer_uses_cross_title(self):
        service = _NotifyService(notify=True)
        service.send_transfer_notification([self._detail("cross_transfer")], 1)
        service._flush_transfer_notifications()
        self.assertEqual(service.sent[0]["title"], TITLE_CROSS_DONE)
        self.assertIn("律界战争 S01 E05", service.sent[0]["text"])

    def test_notify_off_sends_nothing(self):
        service = _NotifyService(notify=False)
        service.send_transfer_notification([self._detail()], 1)
        service._flush_transfer_notifications()
        self.assertEqual(service.sent, [])


class _PostprocessService(_postprocess.PostprocessService):
    def __init__(self, notify=True):
        super().__init__(None)
        object.__setattr__(self, "_notify", bool(notify))
        object.__setattr__(self, "_notification_type", "Plugin")
        object.__setattr__(self, "sent", [])
        object.__setattr__(self, "task_updates", [])

    def _post_message(self, **kwargs):
        self.sent.append(kwargs)

    def _resolve_history_record(self, item, pending_key, status, reason=""):
        return True

    def _postprocess_task_id(self, item):
        return "subscribe:35"

    def _postprocess_task_update(self, task_id, **values):
        self.task_updates.append((task_id, values))


def _ctx(item):
    return SimpleNamespace(
        pending={PENDING_KEY: item},
        committed_keys=set(),
        failed=0,
    )


class TestAbortNotifies(unittest.TestCase):
    def test_direct_abort_sends_dead_copy_once(self):
        service = _PostprocessService(notify=True)
        item = _pending_item()
        ctx = _ctx(item)
        service._abort_single_item(item, PENDING_KEY, "网盘文件定位失败", ctx)
        self.assertEqual(len(service.sent), 1)
        self.assertEqual(service.sent[0]["title"], TITLE_DEAD)
        self.assertIn("律界战争 S01E05.mkv", service.sent[0]["text"])
        self.assertEqual(ctx.failed, 1)
        self.assertEqual(ctx.pending, {})
        self.assertEqual(service.task_updates[0][1]["item_failed"], True)

    def test_dead_path_notifies_only_once(self):
        service = _PostprocessService(notify=True)
        item = _pending_item(fail_count=3)
        service._handle_finalize_dead_item(item, PENDING_KEY, _ctx(item))
        self.assertEqual(len(service.sent), 1)
        self.assertIn("连续 3 次后处理失败", service.sent[0]["text"])

    def test_notify_off_sends_nothing(self):
        service = _PostprocessService(notify=False)
        service._abort_single_item(
            _pending_item(), PENDING_KEY, "网盘文件定位失败", _ctx(_pending_item())
        )
        self.assertEqual(service.sent, [])

    def test_user_stop_does_not_send_failure_notice(self):
        service = _PostprocessService(notify=True)
        item = _pending_item()
        ctx = _ctx(item)
        service._abort_single_item(
            item, PENDING_KEY, "任务已被用户主动停止，终止后处理", ctx,
            notify=False,
        )
        self.assertEqual(service.sent, [])
        self.assertEqual(ctx.failed, 1)


if __name__ == "__main__":
    unittest.main()
