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

from plugin_env import (  # noqa: E402
    load_module, materialize_handler_fields, migrate_handler_stubs,
)

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


def _attach_fake_owner(service, mapping):
    """Phase 5：裸组件实例配假 owner——字段命名空间把桩方法指回 service 自身的桩。

    沿用已有的 _owner 命名空间（_ConfirmHost 经 super().__init__ 挂的那个）：
    __init__ 里普通属性写已被 OwnerDelegator 转发到它身上，换掉会丢属性。
    """
    owner = service.__dict__.get("_owner")
    if owner is None or not hasattr(owner, "__dict__"):
        owner = SimpleNamespace()
    for field_name, method_names in mapping.items():
        namespace = SimpleNamespace()
        for method_name in method_names:
            setattr(namespace, method_name, getattr(service, method_name))
        setattr(owner, field_name, namespace)
    object.__setattr__(service, "_owner", owner)
    return owner


class _ConfirmHost(TransferOpsMixin):
    """只测 confirm：_generate_or_queue_strm_batch 桩掉，只收 item。"""

    def __init__(self, forwarder=None):
        super().__init__(SimpleNamespace())
        self._upgrade_mode = ""
        self.captured = []
        self._forwarder = forwarder
        _attach_fake_owner(self, {
            "resource_transfer": ["_resource_staging_dir"],
            "strm_ops": ["_generate_or_queue_strm_batch"],
        })

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
    materialize_handler_fields(handler, "resource_transfer")
    migrate_handler_stubs(handler)
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


# ----------------------------------------------------------------------
# 1.35.0：上传待整理目录 + 本插件整理（organize_after_upload）
# ----------------------------------------------------------------------
class _OpsHarness(TransferOpsMixin):
    """只用来调辅助方法的最小实例（不跑 __init__）。"""

    def __init__(self, **attrs):
        for key, value in attrs.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "_owner", SimpleNamespace(_plugin=None))


class _FakeUpload:
    def __init__(self, ok=True, error=""):
        self.ok = ok
        self.last_error = error
        self.calls = []

    def upload_file(self, local_path, save_path, target_name):
        self.calls.append((local_path, save_path, target_name))
        return self.ok


class _FakeDrive:
    def __init__(self, upload):
        self.upload = upload

    def require(self, capability):
        return self.upload


class _FakeOrganize:
    def __init__(self, result=None):
        self.result = result or {"success": True, "target": "/lib/S01/x.mkv"}
        self.calls = []
        self.cleanups = []

    def organize_one(self, source_dir, source_name, target_dir, target_name, **kwargs):
        self.calls.append((source_dir, source_name, target_dir, target_name, kwargs))
        return dict(self.result)

    def cleanup_empty_dirs(self, root):
        self.cleanups.append(root)
        return {"success": True}


class OrganizeAfterUploadHelperTest(unittest.TestCase):
    def test_missing_staging_root_fails_soft(self):
        ops = _OpsHarness(_organize_staging_path="", _p115_transfer_path="")

        ok, info = ops._organize_uploaded_file(
            "/downloads/a.mkv", "/lib/S01", "a.mkv",
        )

        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_staging")

    def test_missing_target_dir_fails_soft(self):
        ops = _OpsHarness(
            _organize_staging_path="/转存", _p115_transfer_path="",
            _cloud_drive=_FakeDrive(_FakeUpload()),
            _organize_staging_cleanup=False,
        )

        ok, info = ops._organize_uploaded_file("/downloads/a.mkv", "", "a.mkv")

        self.assertFalse(ok)
        self.assertEqual(info["reason"], "no_target_dir")

    def test_upload_failure_reports_reason(self):
        upload = _FakeUpload(ok=False, error="115 上传熔断器开启")
        ops = _OpsHarness(
            _organize_staging_path="/转存", _p115_transfer_path="",
            _cloud_drive=_FakeDrive(upload),
            _organize_staging_cleanup=False,
        )

        ok, info = ops._organize_uploaded_file(
            "/downloads/a.mkv", "/lib/S01", "a.mkv",
        )

        self.assertFalse(ok)
        self.assertEqual(info["reason"], "upload_failed")
        self.assertIn("熔断", info["message"])
        # 没有 task_key 时用本地文件名主干当子目录，避免与别的任务撞名
        self.assertEqual(upload.calls[0][1], "/转存/a")

    def test_success_uploads_then_organizes_and_cleans(self):
        upload = _FakeUpload()
        organize = _FakeOrganize()
        ops = _OpsHarness(
            _organize_staging_path="/转存/", _p115_transfer_path="",
            _cloud_drive=_FakeDrive(upload),
            _organize_staging_cleanup=True,
        )
        object.__setattr__(ops, "_organize_service", lambda: organize)

        ok, info = ops._organize_uploaded_file(
            "/downloads/a.mkv", "/lib/S01", "a.mkv",
            episode=7, task_key="cross:abc",
        )

        self.assertTrue(ok)
        self.assertEqual(info["reason"], "organized")
        self.assertEqual(upload.calls[0][1], "/转存/cross-abc")
        self.assertEqual(
            organize.calls[0],
            ("/转存/cross-abc", "a.mkv", "/lib/S01", "a.mkv", {"episode": 7}),
        )
        self.assertEqual(organize.cleanups, ["/转存"])

    def test_organize_failure_propagates_message(self):
        organize = _FakeOrganize({"success": False, "message": "源文件已不存在"})
        ops = _OpsHarness(
            _organize_staging_path="/转存", _p115_transfer_path="",
            _cloud_drive=_FakeDrive(_FakeUpload()),
            _organize_staging_cleanup=True,
        )
        object.__setattr__(ops, "_organize_service", lambda: organize)

        ok, info = ops._organize_uploaded_file(
            "/downloads/a.mkv", "/lib/S01", "a.mkv",
        )

        self.assertFalse(ok)
        self.assertEqual(info["reason"], "organize_failed")
        self.assertIn("源文件已不存在", info["message"])
        self.assertEqual(organize.cleanups, [])

    def test_default_staging_falls_back_to_p115_transfer_path(self):
        upload = _FakeUpload()
        ops = _OpsHarness(
            _organize_staging_path="", _p115_transfer_path="/Alist/影视/moviepilot/转存/",
            _cloud_drive=_FakeDrive(upload),
            _organize_staging_cleanup=False,
        )
        object.__setattr__(ops, "_organize_service", lambda: _FakeOrganize())

        ok, _info = ops._organize_uploaded_file(
            "/downloads/a.mkv", "/lib/S01", "a.mkv", task_key="t1",
        )

        self.assertTrue(ok)
        self.assertEqual(upload.calls[0][1], "/Alist/影视/moviepilot/转存/t1")


class OrganizeTargetResolveTest(unittest.TestCase):
    """1.35.0 修：历史重试路径没有预算目标时，_resolve_organize_target 兜底算。"""

    def _ops(self, enabled=True, naming=None, media_path="/Alist/媒体库"):
        owner = SimpleNamespace(
            _plugin=None,
            naming=naming or SimpleNamespace(),
        )
        ops = _OpsHarness(
            _organize_after_upload=enabled,
            _p115_media_path=media_path,
        )
        # _OpsHarness.__init__ 会写一个默认 _owner，这里覆盖成带 naming 的宿主。
        object.__setattr__(ops, "_owner", owner)
        return ops

    def test_switch_off_returns_empty(self):
        ops = self._ops(enabled=False)
        self.assertEqual(ops._resolve_organize_target({}, {}, "a.mkv"), ("", ""))

    def test_stashed_target_wins(self):
        ops = self._ops()
        item = {"_organize_target": "/lib/S01", "_organize_name": "a - S01E01.mkv"}
        self.assertEqual(
            ops._resolve_organize_target(item, {}, "raw.mkv"),
            ("/lib/S01", "a - S01E01.mkv"),
        )

    def test_fallback_uses_naming_service(self):
        import sys

        utils_module = sys.modules.get("cloudsubscribex.handlers.sync.utils")
        if utils_module is None:
            self.skipTest("utils 模块未加载（全量套件里才可用）")
        calls = {}

        def _platform_target(root, subscribe, mediainfo, source_name, season, episode):
            calls.update(root=root, source=source_name, season=season, episode=episode)
            return "/lib/S02", "剧 - S02E05.mkv"

        original = utils_module.resolve_effective_mediainfo
        utils_module.resolve_effective_mediainfo = (
            lambda mediainfo=None, media=None, subscribe=None, enrich=None: SimpleNamespace(
                title="剧", type="tv", tmdb_id=1, year=2026,
            )
        )
        try:
            ops = self._ops(naming=SimpleNamespace(_platform_target=_platform_target))
            media = {"media_type": "tv", "media_title": "剧", "season": 2, "tmdb_id": 1}
            target_dir, target_name = ops._resolve_organize_target(
                {"episode": 5}, media, "raw.mkv",
            )
        finally:
            utils_module.resolve_effective_mediainfo = original

        self.assertEqual((target_dir, target_name), ("/lib/S02", "剧 - S02E05.mkv"))
        self.assertEqual(calls["root"], "/Alist/媒体库")
        self.assertEqual(calls["season"], 2)
        self.assertEqual(calls["episode"], 5)

    def test_unresolvable_media_falls_back(self):
        ops = self._ops()
        self.assertEqual(
            ops._resolve_organize_target({}, {"media_title": "无类型"}, "a.mkv"),
            ("", ""),
        )


class SyncApiSubmitDelegationTest(unittest.TestCase):
    """1.35.2：SyncApi 必须自己持有 _submit_sync_operation（类级查找）。"""

    def test_class_exposes_submit_helper(self):
        sync_api = sys.modules.get("cloudsubscribex.core.api.sync")
        if sync_api is None:
            self.skipTest("api.sync 模块未加载（全量套件里才可用）")
        self.assertTrue(hasattr(sync_api.SyncApi, "_submit_sync_operation"))
