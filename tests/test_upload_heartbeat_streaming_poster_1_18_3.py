"""1.18.3：上传卡闪烁修复（心跳 + 流式进度）+ 电影海报 + oss2 改 https。

覆盖：
1. 心跳（core/upload_progress.start_upload_heartbeat）：分片卡住时
   条目不过期、卡不掉；停掉后条目正常过期；心跳绝不复活已删条目；
   条目长期不出现时线程自停；停滞时速度衰减；
2. 流式进度（drive/p115/upload.py）：_oss2_part_progress_callback
   把分片内 consumed 映射成全局已传字节；_monotonic_progress_callback
   只放行递增值、传完永远放行；顺序上传全程收到片内中间值；
   并发上传上报单调递增、最终到 100%；
3. 电影海报（core/downloader_view.synthetic_upload_tasks）：电影
   文件名进 media 投影（media_type“电影”）、海报走 tmdb_id 解析；
   剧集仍是“电视剧”、未知仍是空；
4. 状态（MP 前端约束）：transferring 经 map_task_state 映成
   downloading——MP 下载页只有“下载中/已暂停”两种文案，没有
   “上传中”状态，改状态值只会变“已暂停”，故保持原样；
5. OSS2_ENDPOINT 已是 https。
"""

import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (
    OwnerDelegator,
    register_module,
)

# 复用 1.12.18 的登记表/投影模块与 1.18.0 的 oss2 上传桩：这些文件
# 在本文件之前被 pytest 导入，模块级打桩已就绪，这里只取名字。
from test_downloader_media_1_9_93 import _PosterResolver, _Torrent
from test_upload_concurrency_1_18_0 import (
    CONTENT,
    PART_SIZE,
    _FakeBucket,
    _init_response,
    _token_response,
    upload_module,
)
from test_upload_progress_1_12_18 import upload_progress, view

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


_register_real_package("cloudsubscribex", _plugin_dir())
_core_pkg = _register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_core_pkg.OwnerDelegator = OwnerDelegator
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.drive.p115", _plugin_dir("drive", "p115"))


MOVIE_FILE = (
    "天才谋杀法 (2026) {tmdb-1084409}.1080p.WEB-DL.H264.DDP 5.1.mkv"
)
TV_FILE = "十万狂花入梦来 {tmdb-261315} - S01E10 - 第10集.mkv"
UNKNOWN_FILE = "random-file-no-meta.mkv"
YEAR_ONLY_FILE = "某某电影 (2025).mkv"


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        upload_progress.clear_upload_progress()
        self.addCleanup(upload_progress.clear_upload_progress)

    def test_heartbeat_keeps_entry_alive_past_expiry(self):
        upload_progress.report_upload_progress("a.mkv", "a.mkv", 10, 100)
        stop = upload_progress.start_upload_heartbeat(
            "a.mkv", interval_seconds=0.05
        )
        try:
            time.sleep(0.35)
            # 没心跳时 0.1 秒就过期；有心跳时条目还在。
            self.assertEqual(
                len(upload_progress.snapshot_upload_progress(0.1)), 1
            )
        finally:
            stop()
        time.sleep(0.25)
        self.assertEqual(
            upload_progress.snapshot_upload_progress(0.1), []
        )

    def test_heartbeat_does_not_resurrect_removed_entry(self):
        upload_progress.report_upload_progress("b.mkv", "b.mkv", 10, 100)
        upload_progress.remove_upload_progress("b.mkv")
        stop = upload_progress.start_upload_heartbeat(
            "b.mkv", interval_seconds=0.05
        )
        try:
            time.sleep(0.2)
            self.assertEqual(
                upload_progress.snapshot_upload_progress(), []
            )
        finally:
            stop()

    def test_heartbeat_stops_itself_when_entry_never_appears(self):
        stop = upload_progress.start_upload_heartbeat(
            "ghost.mkv", interval_seconds=0.05, max_missing_seconds=0.15
        )
        try:
            time.sleep(0.6)
            names = {
                thread.name
                for thread in threading.enumerate()
            }
            self.assertNotIn("upload-heartbeat-ghost.mkv", names)
            self.assertEqual(
                upload_progress.snapshot_upload_progress(), []
            )
        finally:
            stop()

    def test_heartbeat_refresh_decays_speed_while_stalled(self):
        upload_progress.report_upload_progress("c.mkv", "c.mkv", 10, 1000)
        upload_progress.report_upload_progress("c.mkv", "c.mkv", 60, 1000)
        early = upload_progress.snapshot_upload_progress()[0]["speed"]
        self.assertGreater(early, 0.0)
        stop = upload_progress.start_upload_heartbeat(
            "c.mkv", interval_seconds=0.05
        )
        try:
            time.sleep(0.4)
            later = upload_progress.snapshot_upload_progress()[0]["speed"]
        finally:
            stop()
        # 同一 uploaded、elapsed 变长：速度衰减，不会一直谎报旧速度。
        self.assertLess(later, early)

    def test_heartbeat_stop_is_idempotent(self):
        upload_progress.report_upload_progress("d.mkv", "d.mkv", 10, 100)
        stop = upload_progress.start_upload_heartbeat(
            "d.mkv", interval_seconds=0.05
        )
        started = time.monotonic()
        stop()
        stop()
        self.assertLess(time.monotonic() - started, 2.0)


class StreamingHelperTests(unittest.TestCase):
    def test_part_progress_maps_consumed_to_global(self):
        seen = []
        callback = upload_module._oss2_part_progress_callback(
            lambda uploaded, total: seen.append((uploaded, total)),
            part_offset=10 * 1024 * 1024,
            file_size=30 * 1024 * 1024,
        )
        self.assertIsNotNone(callback)
        callback(3 * 1024 * 1024, 10 * 1024 * 1024)
        callback(10 * 1024 * 1024, 10 * 1024 * 1024)
        callback(99 * 1024 * 1024, 10 * 1024 * 1024)
        self.assertEqual(
            seen,
            [
                (13 * 1024 * 1024, 30 * 1024 * 1024),
                (20 * 1024 * 1024, 30 * 1024 * 1024),
                (30 * 1024 * 1024, 30 * 1024 * 1024),
            ],
        )

    def test_part_progress_returns_none_without_callback(self):
        self.assertIsNone(
            upload_module._oss2_part_progress_callback(None, 0, 100)
        )
        self.assertIsNone(
            upload_module._oss2_part_progress_callback(
                lambda a, b: None, 0, 0
            )
        )

    def test_monotonic_suppresses_regression_and_duplicates(self):
        seen = []
        callback = upload_module._monotonic_progress_callback(
            lambda uploaded, total: seen.append((uploaded, total))
        )
        self.assertIsNotNone(callback)
        callback(10, 30)
        callback(5, 30)  # 回落：拦住
        callback(20, 30)
        callback(20, 30)  # 重复：拦住
        callback(30, 30)  # 传完：放行
        callback(30, 30)  # 传完重复：仍放行（完成清理不能被拦）
        self.assertEqual(seen, [(10, 30), (20, 30), (30, 30), (30, 30)])

    def test_monotonic_returns_none_without_callback(self):
        self.assertIsNone(upload_module._monotonic_progress_callback(None))

    def test_oss2_endpoint_is_https(self):
        self.assertTrue(
            upload_module.OSS2_ENDPOINT.startswith("https://"),
            upload_module.OSS2_ENDPOINT,
        )


class _StreamingFakeBucket(_FakeBucket):
    """oss2 Bucket 桩：按 1 字节一块调 progress_callback，模拟
    oss2 适配器边读边报的流式进度。"""

    def upload_part(self, key, upload_id, part_number, data, **kwargs):
        callback = kwargs.get("progress_callback")
        content = data.read() if hasattr(data, "read") else b""
        total = len(content)
        if callback is not None:
            for consumed in range(1, total + 1):
                callback(consumed, total)
        plan = self.fail_plan.get(part_number, [])
        if plan:
            outcome = plan.pop(0)
            if outcome is not None:
                raise outcome
        self.uploaded_parts.append(
            (part_number, content, threading.get_ident())
        )
        return SimpleNamespace(etag=f"etag-{part_number}")


class StreamingUploadTests(unittest.TestCase):
    def setUp(self):
        self._saved = {
            "P115_AVAILABLE": upload_module.P115_AVAILABLE,
            "OSS2_AVAILABLE": upload_module.OSS2_AVAILABLE,
            "check_response": getattr(upload_module, "check_response", None),
            "Oss2Bucket": upload_module.Oss2Bucket,
            "Oss2StsAuth": upload_module.Oss2StsAuth,
            "oss2_determine_part_size": upload_module.oss2_determine_part_size,
        }
        upload_module.P115_AVAILABLE = True
        upload_module.OSS2_AVAILABLE = True
        upload_module.check_response = MagicMock()
        upload_module.oss2_determine_part_size = MagicMock(
            return_value=PART_SIZE
        )
        self._bucket = _StreamingFakeBucket(part_delay=0.0)
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        upload_module.Oss2StsAuth = MagicMock(return_value=MagicMock())
        self.addCleanup(self._restore)

    def _restore(self):
        for key, value in self._saved.items():
            setattr(upload_module, key, value)

    def _make_service(self, concurrency):
        client = MagicMock()
        far_future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        client.upload_file_init.side_effect = (
            lambda *args, **kwargs: _init_response()
        )
        client.upload_gettoken.side_effect = (
            lambda *args, **kwargs: _token_response(far_future)
        )
        owner = OwnerDelegator(SimpleNamespace(
            client=client,
            _cross_transfer_upload_concurrency=concurrency,
            _target_file_cache=MagicMock(),
            _get_component=lambda _component: MagicMock(),
            _rate_limited_call=lambda func, *args, **kwargs: func(*args, **kwargs),
        ))
        directory_reader = upload_module.P115DirectoryReader
        directory_reader.return_value.resolve_directory.return_value = (
            SimpleNamespace(checked=True, directory_id=42)
        )
        return upload_module.P115UploadService(owner)

    def _upload(self, service, concurrency):
        self._bucket = _StreamingFakeBucket(
            part_delay=0.02 if concurrency > 1 else 0.0
        )
        upload_module.Oss2Bucket = MagicMock(return_value=self._bucket)
        with tempfile.TemporaryDirectory() as tempdir:
            source = Path(tempdir) / "05.mkv"
            source.write_bytes(CONTENT)
            progress = []
            with patch.object(upload_module.time, "sleep"):
                self.assertTrue(
                    service.upload_file(
                        str(source), "/",
                        progress_callback=lambda a, b: progress.append((a, b)),
                    )
                )
            return progress

    def test_sequential_upload_reports_streaming_progress(self):
        service = self._make_service(concurrency=0)
        progress = self._upload(service, concurrency=0)
        # 10 字节、每片 4 字节：片内 1 字节一报，全程连续，
        # 不再只在片尾跳 4/8/10。末尾 3 个 (10, 10) 都是传完上报：
        # 流式片尾、顺序循环的片尾、upload_file 的收尾——登记表
        # 里每次都是先记后清，幂等，不会复活条目。
        self.assertEqual(
            progress,
            [(n, 10) for n in range(1, 10)] + [(10, 10)] * 3,
        )
        self.assertEqual(len(self._bucket.completed), 1)

    def test_concurrent_upload_reports_monotonically(self):
        service = self._make_service(concurrency=2)
        progress = self._upload(service, concurrency=2)
        self.assertEqual(progress[-1], (10, 10))
        values = [value for value, _ in progress]
        self.assertEqual(values, sorted(values))
        # 至少收到一个片内中间值（不是只在片尾跳）。
        self.assertTrue(any(value in (1, 2, 3, 5, 6, 7) for value in values))
        self.assertEqual(len(self._bucket.completed), 1)


class MoviePosterTests(unittest.TestCase):
    def setUp(self):
        upload_progress.clear_upload_progress()
        view.clear_poster_cache()
        self.addCleanup(upload_progress.clear_upload_progress)
        self.addCleanup(view.clear_poster_cache)

    def _tasks_for(self, file_name, total=1000):
        upload_progress.report_upload_progress(
            file_name, file_name, 100, total
        )
        return view.synthetic_upload_tasks()

    def test_movie_upload_task_gets_movie_media_type(self):
        tasks = self._tasks_for(MOVIE_FILE)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["media_type"], "电影")
        self.assertEqual(tasks[0]["tmdb_id"], 1084409)

    def test_movie_upload_task_has_poster(self):
        tasks = self._tasks_for(MOVIE_FILE)
        resolver = _PosterResolver()
        torrent = view.task_to_torrent(
            tasks[0], torrent_cls=_Torrent, poster_resolver=resolver
        )
        media = torrent.media
        self.assertEqual(media["type"], "电影")
        self.assertEqual(
            media["poster"],
            "https://image.tmdb.org/t/p/w500/poster1084409.jpg",
        )
        self.assertEqual(resolver.calls, [(1084409, "电影")])

    def test_tv_upload_task_still_tv(self):
        tasks = self._tasks_for(TV_FILE)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["media_type"], "电视剧")
        self.assertEqual(tasks[0]["season"], 1)
        self.assertEqual(tasks[0]["tmdb_id"], 261315)

    def test_unknown_upload_task_has_no_media(self):
        tasks = self._tasks_for(UNKNOWN_FILE)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["media_type"], "")
        torrent = view.task_to_torrent(
            tasks[0], torrent_cls=_Torrent,
            poster_resolver=_PosterResolver(),
        )
        self.assertIsNone(torrent.media)

    def test_year_only_movie_gets_movie_media_type(self):
        tasks = self._tasks_for(YEAR_ONLY_FILE)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["media_type"], "电影")
        torrent = view.task_to_torrent(
            tasks[0], torrent_cls=_Torrent,
            poster_resolver=_PosterResolver(),
        )
        # 没 tmdb_id：media 在（标题/类型），海报为空，不丢字段。
        self.assertEqual(torrent.media["type"], "电影")
        self.assertIsNone(torrent.media["poster"])

    def test_upload_card_state_stays_downloading(self):
        # MP 下载页的状态文案只有两种（MoviePilot-Frontend
        # DownloadingCard.vue 的 stateText：state == downloading
        # 显示“下载中”，其余一律“已暂停”），没有“上传中”状态：
        # transferring 只能映成 downloading，改状态值只会变“已暂停”。
        tasks = self._tasks_for(MOVIE_FILE)
        self.assertEqual(tasks[0]["status"], "transferring")
        self.assertEqual(view.map_task_state("transferring"), "downloading")
        torrent = view.task_to_torrent(
            tasks[0], torrent_cls=_Torrent,
            poster_resolver=_PosterResolver(),
        )
        self.assertEqual(torrent.state, "downloading")


if __name__ == "__main__":
    unittest.main()
