"""v1.29.1：洗版替换正式步骤（先立后破）。

覆盖：
- 提交时才删旧版：_commit_single_item 里终态先写、再删额外旧版本，
  详情建成前 replaced_from 已挂在项上；abort 不删；删失败只 warning、
  不阻塞提交；无 file_id 的旧版不删。
- 主旧版本 + 额外旧版本一起记 replaced_from。
- pending 记录带 dedup_extra_old_versions（批量登记透传、显式 [] 清残留、
  不传沿用旧值）。
- notification_kind 唯一派生（_derive_notification_kind）与终态详情的
  replaced_from 字段；Magnet 详情同样派生（此前缺 kind、默认按普通转存通知）。
- Magnet 单文件收尾：主旧版在 STRM 生成后删（时序不动），删失败则该输出
  不结算；不走 dedup_extra_old_versions。

真实类：PostprocessService（DummyMpWaitService 形态）、EpisodeDedupService
（test_episode_dedup 的 _make_handler）、SyncHandler._build_pending_record /
_queue_file_finalize_batch、TransferDetailsService。删除循环本身的真实行为
由 test_episode_dedup.py 覆盖，这里只锁定顺序与记账。
"""

import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import materialize_handler_fields, migrate_handler_stubs, set_media_type  # noqa: E402

try:
    from test_episode_dedup import _FakeMutations, _make_handler as _make_dedup_handler  # noqa: E402
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_episode_dedup import _FakeMutations, _make_handler as _make_dedup_handler  # noqa: E402

try:
    from test_finalize_mp_wait_1_12_6 import (  # noqa: E402
        DummyMpWaitService,
        _ctx,
        _service_module,
        _stub,
    )
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import (  # noqa: E402
        DummyMpWaitService,
        _ctx,
        _service_module,
        _stub,
    )

try:
    from test_transfer_details_size_1_13_1 import (  # noqa: E402
    HistoryService,
    transfer_details_module,
)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_transfer_details_size_1_13_1 import (  # noqa: E402
    HistoryService,
    transfer_details_module,
)

SyncHandler = _service_module.SyncHandler
MediaType = None

_OLD_EXTRA = [
    {"file_id": "f1", "file_name": "old1.mkv", "cloud_dir": "/d", "file_size": 100, "score": 3},
    {"file_id": "f2", "file_name": "old2.mkv", "cloud_dir": "/d", "file_size": 200, "score": 2},
]


class _RecordingMutations(_FakeMutations):
    """删文件时同时记一条时间线事件，供“终态先写、再删”断言用。"""

    def __init__(self, events, succeed=True):
        super().__init__(succeed=succeed)
        self._events = events

    def delete_file(self, file_id):
        self._events.append(("delete", file_id))
        return super().delete_file(file_id)


def _media(title="正相反的你与我"):
    return SimpleNamespace(
        title=title,
        title_year=f"{title} (2024)",
        year=2024,
        get_poster_image=lambda: None,
        category="国产剧",
        detail_link="",
    )


class ReplaceStepCommitTests(unittest.TestCase):
    """提交步骤：终态先写，再删额外旧版本，详情带 replaced_from。"""

    def _service(self, succeed=True, record_events=False):
        """真实 PostprocessService + 真实 EpisodeDedupService；终态/通知打桩。"""
        self.events = []
        if record_events:
            mutations = _RecordingMutations(self.events, succeed=succeed)
        else:
            mutations = _FakeMutations(succeed=succeed)
        service = DummyMpWaitService()
        dedup = _make_dedup_handler(cloud_mutations=mutations)
        service._owner.episode_dedup = dedup
        _stub(service, "_update_postprocess_progress", lambda *a, **k: None)
        _stub(service, "_delete_upgrade_old_strm", lambda *a, **k: None)
        _stub(
            service, "_write_terminal_state",
            lambda item, key, status, reason="": self.events.append(("terminal",)),
        )
        _stub(service, "_queue_subscription_completion", lambda *a, **k: None)
        _stub(service, "_log_episode_telemetry", lambda *a, **k: None)
        _stub(service, "_postprocess_task_id", lambda item: "")
        self.captured = {}

        def _notify(item, pending_key, strm_path, mediainfo=None,
                    media_data=None, finish_subscription=True,
                    subscribe_cache=None):
            self.captured["item"] = item
            self.captured["pending_key"] = pending_key
            return {"notification_kind": "upgrade"}

        _stub(service, "_notify_pending_file_finalized", _notify)
        return service, mutations

    def _item(self, **overrides):
        item = {
            "task_type": "share",
            "file_name": "正相反的你与我 - S01E01.mkv",
            "cloud_dir": "/d",
            "staging_dir": "/d",
            "staging_name": "正相反的你与我 - S01E01.mkv",
            "season": 1,
            "episode": 1,
            "created_at": 1000.0,
            "download_completed_at": 1000.0,
            "file_size": 900,
        }
        item.update(overrides)
        return item

    def _commit(self, service, item):
        pending = {"key1": item}
        ctx = _ctx(pending)
        service._commit_single_item(item, "key1", None, _media(), {}, ctx)
        return ctx

    def test_commit_deletes_extras_after_terminal_state(self):
        """终态写完才删：时间线上 terminal 在全部 delete 之前。"""
        service, mutations = self._service(record_events=True)
        item = self._item(
            upgrade=True,
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        ctx = self._commit(service, item)
        self.assertEqual(mutations.deleted, ["f1", "f2"])
        self.assertEqual(
            [entry["file_id"] for entry in item["replaced_from"]],
            ["f1", "f2"],
        )
        self.assertEqual(
            self.captured["item"]["replaced_from"], item["replaced_from"]
        )
        self.assertEqual(self.captured["pending_key"], "key1")
        self.assertNotIn("key1", ctx.pending)
        self.assertEqual(ctx.completed, 1)
        terminal_at = self.events.index(("terminal",))
        deletes_at = [
            index for index, event in enumerate(self.events)
            if event[0] == "delete"
        ]
        self.assertTrue(deletes_at)
        self.assertLess(terminal_at, min(deletes_at))

    def test_abort_does_not_delete_extras(self):
        """确认失败（abort）：旧版一个不删。"""
        service, mutations = self._service()
        _stub(service, "_notify_finalize_dead", lambda item, key: None)
        item = self._item(
            upgrade=True,
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        pending = {"key1": item}
        ctx = _ctx(pending)
        service._abort_single_item(item, "key1", "定位失败", ctx)
        self.assertEqual(mutations.deleted, [])
        self.assertNotIn("replaced_from", item)
        self.assertNotIn("key1", pending)
        self.assertEqual(ctx.failed, 1)

    def test_commit_delete_failure_does_not_block(self):
        """删旧失败只 warning：提交照常完成，replaced_from 为空。"""
        service, mutations = self._service(succeed=False)
        item = self._item(
            upgrade=True,
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        ctx = self._commit(service, item)
        self.assertEqual(mutations.deleted, ["f1", "f2"])
        self.assertNotIn("replaced_from", item)
        self.assertEqual(self.captured["pending_key"], "key1")
        self.assertEqual(ctx.completed, 1)

    def test_commit_missing_file_id_kept(self):
        """无 file_id 的旧版不删、只 warning，提交不受影响。"""
        service, mutations = self._service()
        item = self._item(
            upgrade=True,
            dedup_extra_old_versions=[
                {
                    "file_id": "", "file_name": "orphan.mkv",
                    "cloud_dir": "/d", "file_size": 10, "score": 3,
                },
            ],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        ctx = self._commit(service, item)
        self.assertEqual(mutations.deleted, [])
        self.assertNotIn("replaced_from", item)
        self.assertEqual(ctx.completed, 1)

    def test_commit_records_primary_and_extras(self):
        """主旧版（已删）+ 额外旧版一起记进 replaced_from。"""
        service, mutations = self._service()
        item = self._item(
            upgrade=True,
            upgrade_old_deleted=True,
            upgrade_old_cloud_dir="/d",
            upgrade_old_file_name="primary-old.mkv",
            upgrade_old_file_id="p1",
            upgrade_old_size=500,
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        self._commit(service, item)
        self.assertEqual(
            [(entry["file_id"], entry["file_name"]) for entry in item["replaced_from"]],
            [("p1", "primary-old.mkv"), ("f1", "old1.mkv"), ("f2", "old2.mkv")],
        )

    def test_commit_without_extras_does_not_touch_dedup(self):
        """普通转存（无额外旧版）：不调用删除，不留 replaced_from。"""
        service, mutations = self._service()
        item = self._item()
        ctx = self._commit(service, item)
        self.assertEqual(mutations.deleted, [])
        self.assertNotIn("replaced_from", item)
        self.assertEqual(ctx.completed, 1)


class PendingRecordExtrasTests(unittest.TestCase):
    """pending 记录带 dedup_extra_old_versions。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()

    @staticmethod
    def _fake_self():
        return SimpleNamespace(
            _upgrade_mode="replace",
            _OFFLINE_CHECK_DELAYS=(10, 20, 40, 60, 120, 300),
        )

    def _build(self, current=None, **extras):
        return SyncHandler._build_pending_record(
            self._fake_self(),
            current=current or {},
            pending_key="key1",
            task_type="share",
            info_hash="",
            source_hash="",
            share_url="https://115.com/s/test",
            cloud_dir="/d",
            file_name="正相反的你与我 - S01E01.mkv",
            staging_dir="/d",
            staging_name="正相反的你与我 - S01E01.mkv",
            file_size=900,
            now=2000000.0,
            mediainfo=None,
            media_data={},
            **extras,
        )

    def test_build_record_carries_extras(self):
        """带额外旧版时记录原样带上，且 JSON 可序列化。"""
        record = self._build(
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
            dedup_new_score=8,
            dedup_new_size=900,
        )
        self.assertEqual(
            [entry["file_id"] for entry in record["dedup_extra_old_versions"]],
            ["f1", "f2"],
        )
        self.assertEqual(record["dedup_new_score"], 8)
        self.assertEqual(record["dedup_new_size"], 900)
        json.dumps(record)

    def test_build_record_explicit_empty_clears_stale(self):
        """显式 [] 清掉旧记录里的残留额外旧版。"""
        old = {"dedup_extra_old_versions": [dict(v) for v in _OLD_EXTRA]}
        record = self._build(current=old, dedup_extra_old_versions=[])
        self.assertEqual(record["dedup_extra_old_versions"], [])

    def test_build_record_without_extras_keeps_current(self):
        """不传时沿用旧记录（老路径一字不改）。"""
        old = {"dedup_extra_old_versions": [dict(v) for v in _OLD_EXTRA]}
        record = self._build(current=old)
        self.assertEqual(
            [entry["file_id"] for entry in record["dedup_extra_old_versions"]],
            ["f1", "f2"],
        )

    def test_build_record_defaults_empty(self):
        """新记录默认没有额外旧版。"""
        record = self._build()
        self.assertEqual(record["dedup_extra_old_versions"], [])
        self.assertEqual(record["dedup_new_score"], 0)
        self.assertEqual(record["dedup_new_size"], 0)

    def test_queue_batch_persists_extras(self):
        """批量登记落盘的记录带额外旧版。"""
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
        result = handler._queue_file_finalize_batch(
            [{
                "result_key": "single",
                "share_url": "https://115.com/s/test",
                "cloud_dir": "/d",
                "file_name": "正相反的你与我 - S01E01.mkv",
                "staging_dir": "/d",
                "staging_name": "正相反的你与我 - S01E01.mkv",
                "source_sha1": "",
                "file_size": 900,
                "success_episodes": [1],
                "notification_episodes": [1],
                "upgrade": True,
                "upgrade_mode": "replace",
                "upgrade_old_cloud_dir": "/d",
                "upgrade_old_file_name": "primary-old.mkv",
                "upgrade_old_file_id": "p1",
                "upgrade_old_size": 500,
                "dedup_extra_old_versions": [dict(v) for v in _OLD_EXTRA],
                "dedup_new_score": 8,
                "dedup_new_size": 900,
                "subtitles": [],
                "skip_history": False,
                "mp_organize": None,
            }],
            None,
            season=1,
            sub_key="",
        )
        pending_key = result.get("single", "")
        self.assertTrue(pending_key)
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        self.assertEqual(
            [entry["file_id"] for entry in record["dedup_extra_old_versions"]],
            ["f1", "f2"],
        )
        self.assertEqual(record["dedup_new_score"], 8)


class DeriveNotificationKindTests(unittest.TestCase):
    """通知类型唯一派生点。"""

    def _service(self):
        service = HistoryService.__new__(HistoryService)
        return service

    def test_replace_disposition_is_upgrade(self):
        service = self._service()
        self.assertEqual(
            service._derive_notification_kind(is_upgrade=True), "upgrade"
        )

    def test_replaced_from_is_upgrade(self):
        service = self._service()
        self.assertEqual(
            service._derive_notification_kind(
                replaced_from=[{"file_id": "f1"}]
            ),
            "upgrade",
        )

    def test_index_record_upgrade_is_upgrade(self):
        service = self._service()
        self.assertEqual(
            service._derive_notification_kind(
                index_record={"upgrade": True}
            ),
            "upgrade",
        )
        self.assertEqual(
            service._derive_notification_kind(
                index_record={"upgrade": "true"}
            ),
            "upgrade",
        )

    def test_cross_transfer(self):
        service = self._service()
        self.assertEqual(
            service._derive_notification_kind(cross=True), "cross_transfer"
        )

    def test_plain_transfer(self):
        service = self._service()
        self.assertEqual(service._derive_notification_kind(), "transfer")
        self.assertEqual(
            service._derive_notification_kind(
                replaced_from=[], index_record={"upgrade": False}
            ),
            "transfer",
        )


class FinalizedDetailReplacedFromTests(unittest.TestCase):
    """终态详情带 replaced_from，类型按统一派生。"""

    def setUp(self):
        global MediaType
        MediaType = set_media_type()

    def _service(self, index_record=None):
        service = HistoryService.__new__(HistoryService)
        owner = SimpleNamespace(
            resource_transfer=SimpleNamespace(
                _resource_provider_for_url=lambda url: None,
            ),
        )
        object.__setattr__(service, "_owner", owner)
        object.__setattr__(service, "_cloud_drive", SimpleNamespace(name="115网盘", key="115"))
        object.__setattr__(service, "_local_resource_path", "")
        object.__setattr__(
            service, "_pending_index_record",
            lambda pending_key: dict(index_record or {}),
        )
        return service

    def _item(self, **overrides):
        item = {
            "name": "正相反的你与我",
            "title": "正相反的你与我",
            "share_url": "https://115.com/s/test",
            "file_name": "正相反的你与我 - S01E01.mkv",
            "file_size": 900,
            "season": 1,
            "success_episodes": [1],
            "notification_episodes": [1],
            "subscribe_id": 0,
            "year": 2024,
            "cloud_dir": "/d",
        }
        item.update(overrides)
        return item

    def _mediainfo(self):
        # 必须用详情模块自身绑定的 MediaType：测试 mock 的
        # app.schemas.types 可能被后续安装替换，别的 MediaType 对象
        # 与详情模块里的比较会失败（曾导致 type 误判为电影）。
        media = _media()
        media.type = transfer_details_module.MediaType.TV
        return media

    def test_finalized_detail_carries_replaced_from(self):
        """替换项的终态详情：kind=upgrade，replaced_from 原样带上。"""
        service = self._service()
        item = self._item(
            upgrade=True,
            replaced_from=[dict(v) for v in _OLD_EXTRA],
        )
        detail = service._notify_pending_file_finalized(
            item, "key1", None, mediainfo=self._mediainfo(),
            media_data={"title": "正相反的你与我"}, finish_subscription=False,
        )
        self.assertEqual(detail["notification_kind"], "upgrade")
        self.assertEqual(
            [entry["file_id"] for entry in detail["replaced_from"]],
            ["f1", "f2"],
        )
        self.assertEqual(detail["episodes"], [1])

    def test_finalized_detail_plain_transfer(self):
        """普通转存：kind=transfer，replaced_from 为空列表。"""
        service = self._service()
        item = self._item()
        detail = service._notify_pending_file_finalized(
            item, "key1", None, mediainfo=self._mediainfo(),
            media_data={"title": "正相反的你与我"}, finish_subscription=False,
        )
        self.assertEqual(detail["notification_kind"], "transfer")
        self.assertEqual(detail["replaced_from"], [])

    def test_finalized_detail_cross_transfer(self):
        """索引行 transfer_mode=cross 时 kind=cross_transfer。"""
        service = self._service(index_record={"transfer_mode": "cross"})
        item = self._item()
        detail = service._notify_pending_file_finalized(
            item, "key1", None, mediainfo=self._mediainfo(),
            media_data={"title": "正相反的你与我"}, finish_subscription=False,
        )
        self.assertEqual(detail["notification_kind"], "cross_transfer")

    def test_fallback_detail_carries_replaced_from(self):
        """媒体信息缺失的兜底详情同样带 replaced_from 与派生类型。"""
        service = self._service()
        item = self._item(
            upgrade=True,
            replaced_from=[dict(_OLD_EXTRA[0])],
        )
        detail = service._fallback_finalized_detail(item, "key1")
        self.assertEqual(detail["notification_kind"], "upgrade")
        self.assertEqual(
            [entry["file_id"] for entry in detail["replaced_from"]], ["f1"]
        )


class MagnetReplaceStepTests(unittest.TestCase):
    """Magnet 收尾：主旧版在 STRM 生成后删，时序不动；不走额外旧版。"""

    def _service(self, mutations):
        service = DummyMpWaitService()
        service._cloud_mutations = mutations
        service._owner._media_server_notifier = SimpleNamespace(
            notify=lambda **kwargs: True
        )
        _stub(service, "_scrape_metadata", lambda *a, **k: None)
        _stub(service, "_delete_upgrade_old_strm", lambda *a, **k: None)
        _stub(
            service, "_generate_strm",
            lambda *a, **k: Path("/local/media/new.mkv"),
        )
        return service

    def _replace_item(self, **overrides):
        item = {
            "cloud_dir": "/d",
            "file_name": "new.mkv",
            "upgrade_old_cloud_dir": "/d",
            "upgrade_old_file_name": "old.mkv",
            "upgrade_old_file_id": "old1",
            "upgrade_old_backed_up": True,
        }
        item.update(overrides)
        return item

    def _run(self, service, replace_item):
        from test_finalize_mp_wait_1_12_6 import DirectoryFileIndex  # noqa: E402
        return service._finalize_magnet_single_output(
            cloud_dir="/d",
            target_name="new.mkv",
            target_file=SimpleNamespace(name="new.mkv", id="n1", size=1000),
            mediainfo=_media(),
            subscribe=None,
            season=1,
            episode=1,
            is_upgrade=True,
            mode="replace",
            replace_item=replace_item,
            directory_snapshot=lambda d: (True, DirectoryFileIndex([])),
        )

    def test_magnet_deletes_primary_after_strm(self):
        """STRM 生成后删主旧版，upgrade_old_deleted 置位。"""
        mutations = _FakeMutations(succeed=True)
        service = self._service(mutations)
        replace_item = self._replace_item()
        self.assertTrue(self._run(service, replace_item))
        self.assertEqual(mutations.deleted, ["old1"])
        self.assertTrue(replace_item["upgrade_old_deleted"])

    def test_magnet_delete_failure_blocks_output(self):
        """主旧版删失败：该输出不结算（沿用旧行为，旧文件是已备份的避让件）。"""
        mutations = _FakeMutations(succeed=False)
        service = self._service(mutations)
        replace_item = self._replace_item()
        self.assertFalse(self._run(service, replace_item))
        self.assertNotIn("upgrade_old_deleted", replace_item)

    def test_magnet_without_backup_deletes_nothing(self):
        """没备份过（旧文件本来就不在）：不删任何东西，照常成功。"""
        mutations = _FakeMutations(succeed=True)
        service = self._service(mutations)
        replace_item = self._replace_item(upgrade_old_backed_up=False)
        self.assertTrue(self._run(service, replace_item))
        self.assertEqual(mutations.deleted, [])

    def test_magnet_ignores_extra_old_versions(self):
        """Magnet 不走 dedup_extra_old_versions：只删主旧版。"""
        mutations = _FakeMutations(succeed=True)
        service = self._service(mutations)
        replace_item = self._replace_item(
            dedup_extra_old_versions=[dict(v) for v in _OLD_EXTRA],
        )
        self.assertTrue(self._run(service, replace_item))
        self.assertEqual(mutations.deleted, ["old1"])


if __name__ == "__main__":
    unittest.main()
