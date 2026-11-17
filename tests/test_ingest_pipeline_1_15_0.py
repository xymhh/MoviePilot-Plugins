"""1.15.0 Phase 3a 第 1 项：一条入库管线（_run_ingest_pipeline）。

TV（_transfer_episode_items）与电影都走同一管线：
dedup gate（仅 TV）→ stage（落盘/暂存）→ organize（MP 整理 +
伴随字幕）→ confirm（STRM 登记 + 结果拼装；TV 批后做一次落盘自检，
结果挂在每条 result 的 "landed" 上）。差异只用参数表达：批量 vs
单文件、首个成功即停（stop_on_first_success）、是否落盘确认
（verify_landed）、season 归一（电影为 None）。

本文件三层断言：
1. 管线编排（阶段桩）：TV 包装等价、电影首个成功即停 + 不做落盘
   自检、跳过结果按原序拼回且 landed 为 None、中途停止不断批。
2. organize 阶段真实逻辑：字幕调用按 media_type 分支（TV 传季集，
   电影不传季集）、跨盘不触发 MP 整理、失败项不做字幕。
3. confirm 阶段真实逻辑：STRM 成功即通知、转存成功但 STRM 登记
   失败时 success=False 但 transferred=True 且失败原因写明（电影入
   口靠它区分两种失败）、pending 保持成功、电影显式传的
   notification_episodes=None 不被默认值覆盖。
"""
import unittest
from types import SimpleNamespace

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
    "app.log",
    "app.schemas",
    "app.schemas.types",
    "app.utils",
    "app.utils.string",
    "app.utils.path",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.drive",
    "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache",
)
install_core_delegation()
ensure_package("cloudsubscribex.core")

# 与 test_entry_dedup_gate_1_15_0.py 同样的理由：已加载时复用，未加载
# 才加载，避免 fork 出第二个模块对象。
import sys

transfer_ops = sys.modules.get("cloudsubscribex.handlers.sync.transfer_ops")
if transfer_ops is None:
    transfer_ops = load_module(
        "cloudsubscribex.handlers.sync.transfer_ops",
        "plugins.v2/cloudsubscribex/handlers/sync/transfer_ops.py",
    )
TransferOpsMixin = transfer_ops.TransferOpsMixin


def _item(file_id, episode, name, score=50, size=1000, **extra):
    item = {
        "file": {"id": file_id, "name": name, "size": size},
        "episode": episode,
        "score": score,
        "target_name": name,
        "target_dir": "/Alist/影视/moviepilot/媒体库/剧集",
    }
    item.update(extra)
    return item


def _mediainfo():
    return SimpleNamespace(title="玫瑰战争", tmdb_id=299276)


def _subscribe():
    return SimpleNamespace(name="玫瑰战争", id=1)


class _PipelineService(TransferOpsMixin):
    """只测管线编排：三阶段 + 判重门 + 落盘自检全部桩掉。"""

    def __init__(self, batch_size=50, gate=None, stop_after=None):
        self._transfer_task_batch_size = batch_size
        self._organize_after_transfer = True
        self._gate = gate
        self._stop_after = stop_after
        self._stop_calls = 0
        self.transfer_calls = []
        self.organize_calls = []
        self.confirm_calls = []
        self.verify_calls = []
        self.batch_calls = []

    def _filter_entry_dedup_items(self, items, mediainfo, subscribe, season):
        if self._gate is not None:
            return self._gate(items)
        return list(items), {}

    def _ingest_stage_transfer(
            self, selected, share_url, mediainfo, season,
            organize_enabled, rename_items, cloud_resource,
            direct_cloud_resource, file_ids,
    ):
        self.transfer_calls.append(list(selected))
        return (
            list(selected),
            [str(item["file"]["id"]) for item in selected],
            False,
        )

    def _ingest_stage_organize(
            self, processed, success_id_set, mediainfo, season,
            share_url, cross_batch, direct_cloud_resource,
            organize_enabled, media_type="tv",
    ):
        self.organize_calls.append({
            "season": season, "media_type": media_type,
            "count": len(processed),
        })
        return set()

    def _ingest_stage_confirm(
            self, processed, success_id_set, mp_organized_ids,
            share_url, mediainfo, subscribe, season, sub_key,
            track_subscription=True, transient_target=False,
    ):
        self.confirm_calls.append({"season": season, "count": len(processed)})
        return [
            {
                "item": item,
                "file_id": str(item["file"]["id"]),
                "success": True,
                "pending_key": "",
                "failure_reason": "",
                "strm_path": "/strm/x.strm",
                "transferred": True,
            }
            for item in processed
        ]

    def verify_landed_items(self, subscribe, mediainfo, season, checks):
        self.verify_calls.append(list(checks))
        return {
            check["key"]: {"landed": True, "path": f"/lib/{check['key']}"}
            for check in checks
        }

    def _is_cloud_resource_url(self, url):
        return False

    def _is_direct_cloud_resource_url(self, url):
        return False

    def _is_offline_url(self, url):
        return False

    def _stop_requested(self):
        self._stop_calls += 1
        return self._stop_after is not None and self._stop_calls > self._stop_after

    def _set_task_phase(self, *args, **kwargs):
        return None

    def _wait_transfer_batch_interval(self):
        return True


class PipelineOrchestrationTests(unittest.TestCase):
    def test_tv_wrapper_runs_pipeline_with_verify(self):
        """TV 包装：走管线、做落盘自检、landed 挂到每条结果上。"""
        service = _PipelineService()
        items = [
            _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
        ]
        results = service._transfer_episode_items(
            items, "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 2)
        self.assertEqual([r["file_id"] for r in results], ["f24", "f25"])
        self.assertEqual(len(service.transfer_calls), 1)
        self.assertEqual(service.organize_calls[0]["media_type"], "tv")
        self.assertEqual(service.organize_calls[0]["season"], 1)
        self.assertEqual(len(service.confirm_calls), 1)
        # 落盘自检只调一次，key 与结果下标一致。
        self.assertEqual(len(service.verify_calls), 1)
        self.assertEqual(
            [c["key"] for c in service.verify_calls[0]], ["0", "1"]
        )
        self.assertEqual(results[0]["landed"], {"landed": True, "path": "/lib/0"})
        self.assertEqual(results[1]["landed"], {"landed": True, "path": "/lib/1"})

    def test_movie_stops_on_first_success_and_skips_verify(self):
        """电影：单文件、首个成功即停、不做落盘自检、season 归一为 None。"""
        service = _PipelineService(batch_size=1)
        items = [
            _item("m1", 1, "泥面人 (2026).mkv"),
            _item("m2", 1, "泥面人 (2026).mkv"),
        ]
        results = service._run_ingest_pipeline(
            items,
            mediainfo=_mediainfo(),
            subscribe=_subscribe(),
            season=1,
            sub_key="k",
            share_url="magnet:?xt=urn:btih:abc",
            media_type="movie",
            stop_on_first_success=True,
            verify_landed=False,
        )
        # 首个成功即停：只跑了一批（一个文件），第二个没进管线。
        self.assertEqual(len(service.transfer_calls), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_id"], "m1")
        # 电影不做落盘自检，landed 恒为 None。
        self.assertEqual(service.verify_calls, [])
        self.assertIsNone(results[0]["landed"])
        # season 在进批次前已归一为 None。
        self.assertEqual(service.organize_calls[0]["season"], None)
        self.assertEqual(service.confirm_calls[0]["season"], None)

    def test_skipped_results_merged_in_order_with_landed_none(self):
        """判重跳过的项按原序拼回，landed 为 None，不进落盘自检。"""
        skipped = {
            "item": _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            "file_id": "f24",
            "success": True,
            "pending_key": "",
            "failure_reason": "",
            "skip_history": True,
            "dedup_reason": "同集已有更优版本",
            "landed": None,
        }

        def gate(items):
            return [items[1]], {0: skipped}

        service = _PipelineService(gate=gate)
        items = [
            _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
        ]
        results = service._transfer_episode_items(
            items, "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["file_id"], "f24")
        self.assertTrue(results[0]["skip_history"])
        self.assertIsNone(results[0]["landed"])
        self.assertEqual(results[1]["file_id"], "f25")
        # 落盘自检只查转存的那一项。
        self.assertEqual(len(service.verify_calls), 1)
        self.assertEqual(
            [c["file_id"] for c in service.verify_calls[0]], ["f25"]
        )

    def test_stop_between_batches_breaks_pipeline(self):
        """中途停止：跑完一批后停，后面的批不跑，结果不缺位错位。"""
        # stop_after=2：循环顶检查（第 1 次）→ 批次内检查（第 2 次）→
        # 批后检查（第 3 次）才停；stop_after=1 会在批次内检查就停，
        # 第一批根本跑不起来。
        service = _PipelineService(batch_size=1, stop_after=2)
        items = [
            _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
            _item("f26", 26, "玫瑰战争 - S01E26 - 第26集.mp4"),
        ]
        results = service._transfer_episode_items(
            items, "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(service.transfer_calls), 1)
        self.assertEqual([r["file_id"] for r in results], ["f24"])


class _OrganizeService(TransferOpsMixin):
    """只测 organize 阶段真实逻辑：触发桩 + 字幕桩。"""

    def __init__(self):
        self.trigger_calls = []
        self.subtitle_calls = []

    def _trigger_mp_organize_for_batch(
            self, processed_items, success_id_set, mediainfo=None, season=None,
    ):
        self.trigger_calls.append({
            "count": len(processed_items), "season": season,
        })
        return {"f1"}

    def _transfer_companion_subtitles(
            self, share_url="", files=None, video_file=None,
            target_video_name="", media_type="tv", season=None,
            episode=None, transferred_ids=None,
    ):
        self.subtitle_calls.append({
            "files": list(files or []),
            "media_type": media_type,
            "season": season,
            "episode": episode,
        })
        return ["s1.srt"]


class OrganizeStageTests(unittest.TestCase):
    def _processed(self, file_id="f1", episode=24, subtitles=None):
        return [{
            "file": {
                "id": file_id, "name": "玫瑰战争 - S01E24 - 第24集.mp4",
                "url": "https://115.com/s/test",
            },
            "target_name": "玫瑰战争 - S01E24 - 第24集.mp4",
            "episode": episode,
            "subtitle_files": subtitles or [],
        }]

    def test_tv_subtitles_get_season_episode(self):
        """TV：字幕调用传 [视频, 字幕]，季集都传。"""
        service = _OrganizeService()
        sub = {"id": "s1", "name": "玫瑰战争 - S01E24.srt"}
        processed = self._processed(subtitles=[sub])
        organized = service._ingest_stage_organize(
            processed, {"f1"}, _mediainfo(), 1,
            "https://115.com/s/test", False, False, True, media_type="tv",
        )
        self.assertEqual(organized, {"f1"})
        self.assertEqual(len(service.subtitle_calls), 1)
        call = service.subtitle_calls[0]
        self.assertEqual(call["files"], [processed[0]["file"], sub])
        self.assertEqual(call["season"], 1)
        self.assertEqual(call["episode"], 24)
        self.assertEqual(call["media_type"], "tv")
        self.assertEqual(processed[0]["subtitles"], ["s1.srt"])

    def test_movie_subtitles_get_no_season_episode(self):
        """电影：字幕调用原样传整包分享文件列表，季集不传。"""
        service = _OrganizeService()
        video = {"id": "m1", "name": "泥面人 (2026).mkv"}
        share_files = [video, {"id": "s1", "name": "泥面人.srt"}]
        processed = [{
            "file": dict(video, url="magnet:?xt=urn:btih:abc"),
            "target_name": "泥面人 (2026).mkv",
            "episode": 1,
            "subtitle_files": list(share_files),
        }]
        service._ingest_stage_organize(
            processed, {"m1"}, _mediainfo(), None,
            "magnet:?xt=urn:btih:abc", False, False, True,
            media_type="movie",
        )
        self.assertEqual(len(service.subtitle_calls), 1)
        call = service.subtitle_calls[0]
        self.assertEqual(call["files"], share_files)
        self.assertIsNone(call["season"])
        self.assertIsNone(call["episode"])
        self.assertEqual(call["media_type"], "movie")

    def test_cross_batch_skips_mp_organize(self):
        """跨盘不触发 MP 整理（P2 的事），字幕照常做。"""
        service = _OrganizeService()
        processed = self._processed()
        organized = service._ingest_stage_organize(
            processed, {"f1"}, _mediainfo(), 1,
            "https://115.com/s/test", True, False, True, media_type="tv",
        )
        self.assertEqual(organized, set())
        self.assertEqual(service.trigger_calls, [])

    def test_failed_item_gets_no_subtitles(self):
        """转存失败的项不做字幕。"""
        service = _OrganizeService()
        processed = self._processed(subtitles=[{"id": "s1", "name": "x.srt"}])
        service._ingest_stage_organize(
            processed, set(), _mediainfo(), 1,
            "https://115.com/s/test", False, False, True, media_type="tv",
        )
        self.assertEqual(service.subtitle_calls, [])
        self.assertNotIn("subtitles", processed[0])


class _ConfirmService(TransferOpsMixin):
    """只测 confirm 阶段真实逻辑：STRM 桩 + 通知桩 + 失败原因桩。"""

    def __init__(self, strm_map=None):
        self._upgrade_mode = "replace"
        self._strm_map = strm_map or {}
        self.strm_requests = []
        self.notify_calls = []

    def _resource_staging_dir(self, url, file):
        return "/downloads"

    def _generate_or_queue_strm_batch(
            self, requests, mediainfo, subscribe_id=None, season=None,
            sub_key="", transient_target=False, target_subscribe=None,
    ):
        self.strm_requests.extend(requests)
        return {
            request["result_key"]: self._strm_map.get(
                request["result_key"], ("/strm/x.strm", "")
            )
            for request in requests
        }

    def _serialize_pending_target_subscribe(self, subscribe):
        return None

    def _notify(self, **kwargs):
        self.notify_calls.append(kwargs)

    @property
    def _media_server_notifier(self):
        return SimpleNamespace(notify=self._notify)

    def _transfer_failure_reason(self, file_id):
        return "转存失败"


class ConfirmStageTests(unittest.TestCase):
    def _processed(self, file_id="f1", episode=24, **extra):
        item = {
            "file": {
                "id": file_id, "name": "玫瑰战争 - S01E24 - 第24集.mp4",
                "size": 1000,
            },
            "target_name": "玫瑰战争 - S01E24 - 第24集.mp4",
            "target_dir": "/Alist/影视/moviepilot/媒体库/剧集",
            "episode": episode,
        }
        item.update(extra)
        return [item]

    def test_success_with_strm_notifies(self):
        """STRM 生成成功：success、strm_path、媒体库通知都齐。"""
        service = _ConfirmService(strm_map={"f1": ("/strm/玫瑰战争.strm", "")})
        results = service._ingest_stage_confirm(
            self._processed(), {"f1"}, set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertTrue(result["success"])
        self.assertTrue(result["transferred"])
        self.assertEqual(result["strm_path"], "/strm/玫瑰战争.strm")
        self.assertEqual(result["failure_reason"], "")
        self.assertEqual(len(service.notify_calls), 1)
        self.assertEqual(
            service.notify_calls[0]["file_name"],
            "玫瑰战争 - S01E24 - 第24集.mp4",
        )

    def test_transferred_but_strm_failed_has_distinct_reason(self):
        """转存成功但 STRM 登记失败：success=False、transferred=True、
        失败原因写明是后处理登记失败（不是转存失败），不通知。"""
        service = _ConfirmService(strm_map={"f1": (None, "")})
        results = service._ingest_stage_confirm(
            self._processed(), {"f1"}, set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        result = results[0]
        self.assertFalse(result["success"])
        self.assertTrue(result["transferred"])
        self.assertEqual(
            result["failure_reason"], "文件已转存但后处理任务登记失败"
        )
        self.assertEqual(service.notify_calls, [])

    def test_pending_keeps_success_without_strm(self):
        """STRM 走后处理队列（pending_key）：仍算成功，不通知。"""
        service = _ConfirmService(strm_map={"f1": (None, "pk-1")})
        results = service._ingest_stage_confirm(
            self._processed(), {"f1"}, set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        result = results[0]
        self.assertTrue(result["success"])
        self.assertEqual(result["pending_key"], "pk-1")
        self.assertIsNone(result["strm_path"])
        self.assertEqual(service.notify_calls, [])

    def test_movie_explicit_episodes_preserved(self):
        """电影显式传的 success_episodes=[1] 与 notification_episodes=None
        原样进 STRM 登记请求，不被默认值覆盖；升级项默认 success_episodes=[]。"""
        service = _ConfirmService()
        processed = self._processed(
            episode=1,
            success_episodes=[1],
            notification_episodes=None,
            is_upgrade=True,
        )
        service._ingest_stage_confirm(
            processed, {"f1"}, set(),
            "magnet:?xt=urn:btih:abc", _mediainfo(), _subscribe(), None, "k",
        )
        self.assertEqual(len(service.strm_requests), 1)
        request = service.strm_requests[0]
        self.assertEqual(request["success_episodes"], [1])
        self.assertIsNone(request["notification_episodes"])
        # 升级项不带 success_episodes 时默认 []。
        service2 = _ConfirmService()
        processed2 = self._processed(episode=24, is_upgrade=True)
        service2._ingest_stage_confirm(
            processed2, {"f1"}, set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(
            service2.strm_requests[0]["success_episodes"], []
        )
        # 非升级 TV 项默认 success_episodes=[episode]。
        service3 = _ConfirmService()
        processed3 = self._processed(episode=24)
        service3._ingest_stage_confirm(
            processed3, {"f1"}, set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(
            service3.strm_requests[0]["success_episodes"], [24]
        )

    def test_failed_transfer_uses_transfer_reason(self):
        """转存本身失败：success/transferred 都 False，失败原因是转存原因。"""
        service = _ConfirmService()
        results = service._ingest_stage_confirm(
            self._processed(), set(), set(),
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        result = results[0]
        self.assertFalse(result["success"])
        self.assertFalse(result["transferred"])
        self.assertEqual(result["failure_reason"], "转存失败")
        self.assertEqual(service.strm_requests, [])


if __name__ == "__main__":
    unittest.main()
