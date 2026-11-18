"""v1.18.1：跨盘 MP 整理身份不能在 confirm 阶段丢掉。

v1.17.0 Phase 4 第 6 项修误报时只盖住了同盘批量：同盘批量触发在
mp_organize.py 把身份写在外层 processed item 的 item["mp_organize"]；
跨盘转存（源盘≠目标盘，如夸克→115）与同盘单文件尾巴在
transfer_ops.py _transfer_file_impl 里把身份写在内层
file_item["mp_organize"]。而 _ingest_stage_confirm 组装传给
_generate_or_queue_strm_batch 的 dict 时只读外层
item.get("mp_organize")，跨盘项读到 None，pending 记录就没有身份，
finalize 跳过 _poll_mp_organize_state 保护、按老规则 2 次定位失败即
abort。实测：天才谋杀法 (2026) 手动添加夸克分享，11:24:31 交 MP 整理，
9.4 秒后 11:24:40 两次定位失败 abort，零轮询记录；当时本地下载仅
~7%、MP 整理 0/1 还在跑。

修复：confirm 里外层读不到就读内层
item.get("mp_organize") or (item.get("file") or {}).get("mp_organize")。

覆盖：
- confirm：跨盘项（外层无、内层有）→ 身份带上；同盘项（外层有）→
  行为不变；两层都没有 → None；两层都有 → 外层优先。
- 端到端：跨盘项经 confirm → _queue_file_finalize_batch 落盘后，
  pending 记录带 mp_organize；无身份项落盘后不留该字段。

finalize 侧“带身份的项在 MP 整理中时轮询而不 abort”已由
test_finalize_mp_terminal_wait_1_17_0.py 覆盖，这里不再重复。
"""

import json
import os
import sys
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from test_finalize_mp_wait_1_12_6 import SyncHandler
except ImportError:  # 直接用 unittest 跑单文件时 tests/ 不在 path 上
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from test_finalize_mp_wait_1_12_6 import SyncHandler

from plugin_env import load_module  # noqa: E402

# 与 test_ingest_pipeline_1_15_0.py 同样的理由：已加载时复用，未加载
# 才加载，避免 fork 出第二个模块对象。
transfer_ops = sys.modules.get("cloudsubscribex.handlers.sync.transfer_ops")
if transfer_ops is None:
    transfer_ops = load_module(
        "cloudsubscribex.handlers.sync.transfer_ops",
        "plugins.v2/cloudsubscribex/handlers/sync/transfer_ops.py",
    )
TransferOpsMixin = transfer_ops.TransferOpsMixin

NOW = 1_700_000_000.0

INFO = {
    "src_path": (
        "/downloads/天才谋杀法 (2026) {tmdb-1084409}"
        ".1080p.WEB-DL.H264.DDP 5.1.mkv"
    ),
    "src_storage": "local",
    "target_dir": (
        "/Alist/影视/moviepilot/媒体库/电影/外语电影/"
        "天才谋杀法 (2026) {tmdbid=1084409}"
    ),
    "target_storage": "u115cs",
    "target_name": (
        "天才谋杀法 (2026) {tmdb-1084409}.1080p.WEB-DL.H264.DDP 5.1.mkv"
    ),
    "file_name": (
        "天才谋杀法 (2026) {tmdb-1084409}.1080p.WEB-DL.H264.DDP 5.1.mkv"
    ),
    "triggered_at": NOW,
}

SHARE_URL = "https://pan.quark.cn/s/adda4aa849fd"


def _item(file_id="f1", outer=None, inner=None):
    """processed item：外层/内层按需带 mp_organize 身份。"""
    item = {
        "file": {
            "id": file_id,
            "name": "天才谋杀法 (2026).mkv",
            "size": 7213910000,
            "sha1": "a" * 40,
            "url": SHARE_URL,
        },
        "episode": 1,
        "target_name": INFO["file_name"],
        "target_dir": INFO["target_dir"],
        "success_episodes": [1],
        "notification_episodes": [1],
    }
    if outer is not None:
        item["mp_organize"] = dict(outer)
    if inner is not None:
        item["file"]["mp_organize"] = dict(inner)
    return item


def _mediainfo():
    return SimpleNamespace(title="天才谋杀法", tmdb_id=1084409, year=2026)


def _subscribe():
    return SimpleNamespace(name="天才谋杀法", id=365)


class _ConfirmHost(TransferOpsMixin):
    """只测 confirm：_generate_or_queue_strm_batch 桩掉，只收 item。"""

    def __init__(self, forwarder=None):
        super().__init__(SimpleNamespace())
        self._upgrade_mode = ""
        self.captured = []
        self._forwarder = forwarder

    def _resource_staging_dir(self, url, file_item):
        return "/转存"

    def _generate_or_queue_strm_batch(
            self, items, mediainfo, subscribe_id=None, season=None,
            sub_key="", transient_target=False, target_subscribe=None,
    ):
        self.captured.extend(items)
        if self._forwarder is not None:
            return self._forwarder(
                items, mediainfo, subscribe_id, season, sub_key,
                transient_target, target_subscribe,
            )
        return {
            str(item["result_key"]): (None, "pk-" + str(item["result_key"]))
            for item in items
        }


def _queue_host(store):
    """与 test_queue_file_finalize_persists_mp_organize 同款的 host。"""
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
    return handler


class ConfirmMpOrganizeFallbackTests(unittest.TestCase):
    def _confirm(self, items):
        host = _ConfirmHost()
        results = host._ingest_stage_confirm(
            items, {str(item["file"]["id"]) for item in items}, set(),
            SHARE_URL, _mediainfo(), _subscribe(), None, "movie:1084409",
        )
        self.assertEqual(len(host.captured), len(items))
        return host, results

    def test_cross_drive_inner_identity_reaches_confirm(self):
        """跨盘项：外层无身份、内层有 → confirm 结果带上内层身份。"""
        host, results = self._confirm([_item(inner=INFO)])
        self.assertEqual(host.captured[0]["mp_organize"], INFO)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["pending_key"], "pk-f1")

    def test_same_drive_outer_identity_unchanged(self):
        """同盘项：外层有身份 → 原样带上（防回归）。"""
        host, results = self._confirm([_item(outer=INFO)])
        self.assertEqual(host.captured[0]["mp_organize"], INFO)
        self.assertEqual(results[0]["pending_key"], "pk-f1")

    def test_no_identity_stays_none(self):
        """两层都没有 → mp_organize 为 None，老行为不变。"""
        host, results = self._confirm([_item()])
        self.assertIsNone(host.captured[0]["mp_organize"])
        self.assertEqual(results[0]["pending_key"], "pk-f1")

    def test_outer_identity_wins_over_inner(self):
        """两层都有 → 外层优先（同盘批量写的外层身份为准）。"""
        inner = dict(INFO, triggered_at=NOW - 600)
        host, _results = self._confirm([_item(outer=INFO, inner=inner)])
        self.assertEqual(host.captured[0]["mp_organize"], INFO)
        self.assertEqual(
            host.captured[0]["mp_organize"]["triggered_at"], NOW
        )


class ConfirmToPendingRecordTests(unittest.TestCase):
    """confirm → 真实 _queue_file_finalize_batch → 落盘记录带身份。"""

    def _run(self, item):
        store = {}
        queue = _queue_host(store)

        def forwarder(
                items, mediainfo, subscribe_id, season, sub_key,
                transient_target, target_subscribe,
        ):
            pending_keys = queue._queue_file_finalize_batch(
                items, mediainfo, subscribe_id=subscribe_id,
                season=season, sub_key=sub_key,
                transient_target=transient_target,
                target_subscribe=target_subscribe,
            )
            return {
                str(entry["result_key"]): (
                    None, pending_keys.get(str(entry["result_key"]), "")
                )
                for entry in items
            }

        host = _ConfirmHost(forwarder=forwarder)
        results = host._ingest_stage_confirm(
            [item], {str(item["file"]["id"])}, set(), SHARE_URL,
            _mediainfo(), _subscribe(), None, "movie:1084409",
        )
        pending_key = results[0]["pending_key"]
        self.assertTrue(pending_key)
        record = store[SyncHandler._OFFLINE_PENDING_KEY][pending_key]
        return record

    def test_cross_drive_identity_persists_to_pending_record(self):
        """跨盘项：身份一路进 pending 记录，且 JSON 可序列化。"""
        record = self._run(_item(inner=INFO))
        self.assertEqual(record["mp_organize"], INFO)
        json.dumps(record)

    def test_no_identity_pending_record_has_no_field(self):
        """无身份项：落盘记录不留 mp_organize 字段。"""
        record = self._run(_item())
        self.assertNotIn("mp_organize", record)


if __name__ == "__main__":
    unittest.main()
