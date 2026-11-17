"""v1.12.18：下载管理页实时显示 115 上传进度。

MP 整理队列的进度是按文件数算的，单文件整理全程 0%；上传本身
永远走插件的 U115CSStorage.upload，回调把字节进度记进进程内
登记表（core/upload_progress.py），下载页把进行中的上传投影成
任务卡（真实百分比 + ↑ 速度 + MB/MB + 剩余时间），整理中的
任务也用实时字节百分比覆盖原来 95% 上限的假进度。

覆盖：
1. 登记表：report/snapshot/过期清理/速度计算/重传重置基线/remove/clear；
2. _make_upload_progress_callback：调一次后登记表里有条目（MP 进度
   器不可用时也要能记），调到 100% 后条目被清掉；
3. synthetic_upload_tasks + project_tasks/task_to_torrent 投影：
   进度是真实字节百分比、state 是 downloading、upspeed 非空、
   title 含 MB/MB、hash 稳定、left_time 正确；
4. organizing 覆盖：有实时条目时 _task_progress 返回实时百分比
   （不再是 95 上限）、_phase_speed 返回速度文案；无条目时保持
   原有行为；
5. list_torrents 合并：_serialize_runtime_tasks 返回 [] 且登记表有
   条目时，下载页包含合成上传卡；stop_torrents 跳过合成上传卡。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import (
    OwnerDelegator,
    load_module,
    register_module,
)

# 复用 1.12.10 的上传桩（storage 加载与 MP 进度器替身）：该文件已在
# 本文件之前被 pytest 导入，模块级打桩已就绪，这里只取名字，不重跑。
from test_u115cs_upload_progress_1_12_10 import (
    FakeProgressHelper,
    UploadProgressTestBase,
    storage,
)

# 复用 1.9.86 的插件本体桩：裸插件实例（_serialize_runtime_tasks 可替）。
from test_downloader_plugin_1_9_86 import _Torrent, _plugin

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

upload_progress = load_module(
    "cloudsubscribex.core.upload_progress",
    "plugins.v2/cloudsubscribex/core/upload_progress.py",
)
view = load_module(
    "cloudsubscribex.core.downloader_view",
    "plugins.v2/cloudsubscribex/core/downloader_view.py",
)

MB = 1024 * 1024
TOTAL = 100 * MB
WENWU = "文武 {tmdb-123456} - S01E01 - 第1集.mp4"


class _FakeClock:
    """可控的 time.monotonic 替身：永不耗尽，测试里手动推进。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class _ClockMixin(unittest.TestCase):
    """用假时钟跑的测试：速度与过期都按推进的时间算，拒绝 flaky。"""

    def setUp(self):
        super().setUp()
        upload_progress.clear_upload_progress()
        self.clock = _FakeClock(1000.0)
        self._clock_patcher = mock.patch("time.monotonic", self.clock)
        self._clock_patcher.start()
        self.addCleanup(self._clock_patcher.stop)
        self.addCleanup(upload_progress.clear_upload_progress)

    def _report_at(self, key, file_name, uploaded, total, advance=0.0):
        """时钟推进 advance 秒后记一次进度（首次调用 advance=0）。"""
        if advance:
            self.clock.advance(advance)
        upload_progress.report_upload_progress(key, file_name, uploaded, total)


class TestUploadProgressRegistry(_ClockMixin):
    def test_report_and_snapshot(self):
        """report 后 snapshot 能拿到条目，字段齐全，首次速度为 0。"""
        upload_progress.report_upload_progress("a.mkv", "a.mkv", 10 * MB, TOTAL)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["key"], "a.mkv")
        self.assertEqual(entry["file_name"], "a.mkv")
        self.assertEqual(entry["uploaded"], 10 * MB)
        self.assertEqual(entry["total"], TOTAL)
        self.assertEqual(entry["speed"], 0.0)
        self.assertEqual(entry["updated_at"], 1000.0)

    def test_speed_is_average_since_first_report(self):
        """速度是自首次记录以来的平均速度。"""
        self._report_at("a.mkv", "a.mkv", 0, TOTAL)
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL, advance=10.0)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0]["speed"], 1.0 * MB)

    def test_retransmit_resets_speed_baseline(self):
        """uploaded 比上次小（整文件重传）时重置速度基线。"""
        self._report_at("a.mkv", "a.mkv", 5 * MB, TOTAL)
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL, advance=10.0)
        # 重传：uploaded 回落到 1MB，速度归零、基线重置到这次。
        self._report_at("a.mkv", "a.mkv", 1 * MB, TOTAL, advance=10.0)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(entries[0]["speed"], 0.0)
        # 再传 2MB：速度按新基线算，(3-1)MB / 10s。
        self._report_at("a.mkv", "a.mkv", 3 * MB, TOTAL, advance=10.0)
        entries = upload_progress.snapshot_upload_progress()
        self.assertAlmostEqual(entries[0]["speed"], 0.2 * MB)

    def test_snapshot_cleans_expired_entries(self):
        """超过 max_age 的条目在 snapshot 时被清理掉。"""
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL)
        self._report_at("b.mkv", "b.mkv", 10 * MB, TOTAL)
        self.clock.advance(31.0)
        entries = upload_progress.snapshot_upload_progress(max_age_seconds=30.0)
        self.assertEqual(entries, [])
        self.assertEqual(upload_progress.snapshot_upload_progress(), [])

    def test_snapshot_keeps_fresh_entries(self):
        """没过期的条目 snapshot 后还在（不误删）。"""
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL)
        self.clock.advance(10.0)
        entries = upload_progress.snapshot_upload_progress(max_age_seconds=30.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], "a.mkv")

    def test_remove_and_clear(self):
        """remove 删一条，clear 全清。"""
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL)
        self._report_at("b.mkv", "b.mkv", 20 * MB, TOTAL)
        upload_progress.remove_upload_progress("a.mkv")
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual([entry["key"] for entry in entries], ["b.mkv"])
        upload_progress.clear_upload_progress()
        self.assertEqual(upload_progress.snapshot_upload_progress(), [])

    def test_report_never_raises(self):
        """脏输入也不抛异常到调用方。"""
        upload_progress.report_upload_progress(None, None, "x", "y")
        upload_progress.report_upload_progress("", "a.mkv", -5, -10)
        upload_progress.report_upload_progress("a.mkv", "a.mkv", None, None)
        upload_progress.remove_upload_progress(None)
        upload_progress.clear_upload_progress()
        self.assertEqual(upload_progress.snapshot_upload_progress(), [])


class TestUploadProgressCallback(UploadProgressTestBase):
    """上传回调与登记表的联动：复用 1.12.10 的上传桩。"""

    def setUp(self):
        super().setUp()
        upload_progress.clear_upload_progress()
        self.addCleanup(upload_progress.clear_upload_progress)

    def test_callback_registers_entry(self):
        """调一次回调，登记表里就有条目。"""
        callback = storage._make_upload_progress_callback(
            "a.mkv", self.total, key="a.mkv"
        )
        callback(50 * MB, self.total)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], "a.mkv")
        self.assertEqual(entries[0]["file_name"], "a.mkv")
        self.assertEqual(entries[0]["uploaded"], 50 * MB)
        self.assertEqual(entries[0]["total"], self.total)

    def test_callback_registers_without_mp_helper(self):
        """MP 进度模块不存在时，登记表照样记（下载页不依赖 MP 进度器）。"""
        sys.modules.pop("app.runtime.progress", None)
        callback = storage._make_upload_progress_callback(
            "a.mkv", self.total, key="a.mkv"
        )
        callback(50 * MB, self.total)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["uploaded"], 50 * MB)

    def test_callback_removes_entry_at_finish(self):
        """调到 100% 后登记表条目被清掉（卡片随之消失）。"""
        callback = storage._make_upload_progress_callback(
            "a.mkv", self.total, key="a.mkv"
        )
        callback(50 * MB, self.total)
        self.assertEqual(len(upload_progress.snapshot_upload_progress()), 1)
        callback(self.total, self.total)
        self.assertEqual(upload_progress.snapshot_upload_progress(), [])

    def test_callback_default_key_is_file_name(self):
        """不传 key 时默认用目标文件名做登记 key。"""
        callback = storage._make_upload_progress_callback("a.mkv", self.total)
        callback(10 * MB, self.total)
        entries = upload_progress.snapshot_upload_progress()
        self.assertEqual(entries[0]["key"], "a.mkv")

    def test_upload_records_registry_and_clears_on_success(self):
        """走 U115CSStorage.upload：传的过程中登记表有条目，传完清掉。"""
        store = self._make_store([0.0, 0.5, 1.0])
        counter = [999.0]

        def _tick():
            counter[0] += 1.5
            return counter[0]

        with mock.patch("time.monotonic", side_effect=_tick):
            result = self._upload(store)
        self.assertIsNotNone(result)
        self.assertEqual(upload_progress.snapshot_upload_progress(), [])
        # MP 进度器那边 100% 照常上报（1.12.10 的行为没变）。
        self.assertEqual(FakeProgressHelper.updates[-1][0], 100.0)


class TestSyntheticUploadTasks(_ClockMixin):
    def _seed_wenwu(self):
        """文武 S01E01：10 秒传了 50MB/100MB，速度 5MB/s，剩 10 秒。"""
        self._report_at("wenwu.mp4", WENWU, 0, TOTAL)
        self._report_at("wenwu.mp4", WENWU, 50 * MB, TOTAL, advance=10.0)

    def test_synthetic_task_fields(self):
        """合成任务字典字段：标题、媒体、季、tmdb、状态、阶段、字节、速度。"""
        self._seed_wenwu()
        tasks = view.synthetic_upload_tasks()
        self.assertEqual(len(tasks), 1)
        task = tasks[0]
        self.assertTrue(task["id"].startswith("upload:"))
        self.assertEqual(task["media_title"], "文武")
        self.assertEqual(task["media_type"], "电视剧")
        self.assertEqual(task["season"], 1)
        self.assertEqual(task["tmdb_id"], 123456)
        self.assertEqual(task["status"], "transferring")
        self.assertEqual(task["transfer_phase"], "uploading")
        self.assertEqual(task["total"], TOTAL)
        self.assertEqual(task["uploaded_bytes"], 50 * MB)
        self.assertAlmostEqual(
            task["upload_speed_bytes_per_second"], 5.0 * MB
        )
        self.assertEqual(task["current_file"], WENWU)
        self.assertEqual(task["task_kind"], "upload")
        self.assertIn("50/100MB", task["title"])
        self.assertEqual(task["left_time"], "剩余约10秒")

    def test_synthetic_task_hash_stable(self):
        """同一条上传两次投影的 id 一致（下载页按 hash 定位任务）。"""
        self._seed_wenwu()
        first = view.synthetic_upload_tasks()
        second = view.synthetic_upload_tasks()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["id"], second[0]["id"])

    def test_synthetic_task_left_time_minutes(self):
        """剩余 >=60 秒时按“剩余约X分Y秒”写。"""
        self._report_at("big.mkv", "big.mkv", 0, 1000 * MB)
        self._report_at("big.mkv", "big.mkv", 100 * MB, 1000 * MB, advance=10.0)
        tasks = view.synthetic_upload_tasks()
        self.assertEqual(len(tasks), 1)
        # 速度 10MB/s，剩 900MB → 90 秒 → 1分30秒。
        self.assertEqual(tasks[0]["left_time"], "剩余约1分30秒")

    def test_synthetic_task_no_left_time_without_speed(self):
        """没速度时不写 left_time（不瞎猜）。"""
        self._report_at("a.mkv", "a.mkv", 10 * MB, TOTAL)
        tasks = view.synthetic_upload_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertNotIn("left_time", tasks[0])

    def test_synthetic_task_movie_has_no_media_type(self):
        """电影（无 SxxExx）不设 media_type，卡片只显示名字、无海报。"""
        self._report_at("movie.mkv", "流浪地球2.mkv", 10 * MB, TOTAL)
        tasks = view.synthetic_upload_tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["media_title"], "流浪地球2")
        self.assertEqual(tasks[0]["media_type"], "")
        self.assertNotIn("season", tasks[0])
        self.assertNotIn("tmdb_id", tasks[0])

    def test_media_title_strips_hash_suffix(self):
        """(hash) 后缀不进媒体标题。"""
        self._report_at("x.mp4", "多喜一家人 (b77a2171) - S01E03 - 第3集.mp4",
                        10 * MB, TOTAL)
        tasks = view.synthetic_upload_tasks()
        self.assertEqual(tasks[0]["media_title"], "多喜一家人")
        self.assertEqual(tasks[0]["season"], 1)

    def test_project_tasks_shows_real_progress(self):
        """投影成 torrent：进度是真实字节百分比，state 是 downloading，
        upspeed 非空，title 含 MB/MB，left_time 透传，name 不带 MB 文本。"""
        self._seed_wenwu()
        tasks = view.synthetic_upload_tasks()
        torrents = view.project_tasks(tasks, torrent_cls=_Torrent)
        self.assertEqual(len(torrents), 1)
        torrent = torrents[0]
        self.assertEqual(torrent.progress, 50.0)
        self.assertEqual(torrent.state, "downloading")
        self.assertEqual(torrent.upspeed, "5.0 MB/s")
        self.assertIn("50/100MB", torrent.title)
        self.assertEqual(torrent.left_time, "剩余约10秒")
        self.assertEqual(torrent.name, "文武 S01E01")

    def test_project_tasks_movie_card_has_no_media(self):
        """电影合成卡：media 为 None，只显示名字。"""
        self._report_at("movie.mkv", "流浪地球2.mkv", 10 * MB, TOTAL)
        tasks = view.synthetic_upload_tasks()
        torrents = view.project_tasks(tasks, torrent_cls=_Torrent)
        self.assertEqual(len(torrents), 1)
        self.assertIsNone(torrents[0].media)
        self.assertEqual(torrents[0].name, "流浪地球2")


class TestOrganizingLiveProgress(_ClockMixin):
    def _organizing_task(self, progress=99):
        return {
            "id": "subscribe:1",
            "title": "文武",
            "media_title": "文武",
            "media_type": "电视剧",
            "season": 1,
            "status": "transferring",
            "transfer_phase": "organizing",
            "progress": progress,
            "current_file": WENWU,
            "total": TOTAL,
        }

    def _seed_live_entry(self, uploaded=50 * MB):
        self._report_at(WENWU, WENWU, 0, TOTAL)
        self._report_at(WENWU, WENWU, uploaded, TOTAL, advance=10.0)

    def test_task_progress_uses_live_percentage(self):
        """整理中 + 有实时条目：返回实时百分比，不再是 95 上限。"""
        self._seed_live_entry()
        self.assertEqual(view._task_progress(self._organizing_task()), 50.0)

    def test_task_progress_caps_live_percentage_at_99(self):
        """实时百分比 99.5% 时返回 99.0，绝不显示 100%。"""
        self._seed_live_entry(uploaded=int(99.5 * MB))
        self.assertEqual(view._task_progress(self._organizing_task()), 99.0)

    def test_task_progress_falls_back_without_live_entry(self):
        """整理中 + 无实时条目：保持原有行为（任务进度，上限 95%）。"""
        self.assertEqual(view._task_progress(self._organizing_task()), 95.0)

    def test_task_progress_falls_back_when_live_is_zero(self):
        """实时条目 0% 时不覆盖，保持原有行为。"""
        self._report_at(WENWU, WENWU, 0, TOTAL)
        self.assertEqual(view._task_progress(self._organizing_task()), 95.0)

    def test_task_progress_matches_by_basename(self):
        """current_file 是完整路径时，按 basename 匹配登记 key。"""
        self._seed_live_entry()
        task = self._organizing_task()
        task["current_file"] = f"/downloads/{WENWU}"
        self.assertEqual(view._task_progress(task), 50.0)

    def test_task_progress_matches_by_file_name(self):
        """key 对不上、file_name 对得上时也能匹配。"""
        self._report_at("local-name.mp4", WENWU, 0, TOTAL)
        self._report_at("local-name.mp4", WENWU, 50 * MB, TOTAL, advance=10.0)
        self.assertEqual(view._task_progress(self._organizing_task()), 50.0)

    def test_phase_speed_uses_live_speed(self):
        """整理中 + 有实时条目：uploading 档返回 ↑ 实时速度。"""
        self._seed_live_entry()
        task = self._organizing_task()
        self.assertEqual(view._phase_speed(task, "uploading"), "5.0 MB/s")
        self.assertIsNone(view._phase_speed(task, "downloading"))

    def test_phase_speed_none_without_live_entry(self):
        """整理中 + 无实时条目：速度返回 None。"""
        task = self._organizing_task()
        self.assertIsNone(view._phase_speed(task, "uploading"))
        self.assertIsNone(view._phase_speed(task, "downloading"))


class TestListTorrentsMergesUploads(unittest.TestCase):
    """list_torrents 合并与 stop_torrents 跳过合成卡。

    假时钟只包住“记进度 + 查下载页”这一段：_plugin() 加载插件
    模块本身不在补丁里，避免加载期用到 monotonic 时被假时钟干扰。
    """

    def setUp(self):
        super().setUp()
        upload_progress.clear_upload_progress()
        self.addCleanup(upload_progress.clear_upload_progress)
        self.clock = _FakeClock(1000.0)

    def _seed_wenwu(self):
        """文武 S01E01：10 秒传了 50MB/100MB（假时钟下记进度）。"""
        self.clock.now = 1000.0
        upload_progress.report_upload_progress("wenwu.mp4", WENWU, 0, TOTAL)
        self.clock.advance(10.0)
        upload_progress.report_upload_progress("wenwu.mp4", WENWU, 50 * MB, TOTAL)

    def test_list_torrents_includes_synthetic_upload(self):
        """运行时任务为空、登记表有条目时，下载页包含合成上传卡。"""
        plugin = _plugin()
        plugin._serialize_runtime_tasks = lambda: []
        with mock.patch("time.monotonic", self.clock):
            self._seed_wenwu()
            torrents = plugin.list_torrents()
        self.assertEqual(len(torrents), 1)
        torrent = torrents[0]
        self.assertEqual(torrent.state, "downloading")
        self.assertEqual(torrent.progress, 50.0)
        self.assertEqual(torrent.upspeed, "5.0 MB/s")
        self.assertIn("50/100MB", torrent.title)
        self.assertEqual(torrent.left_time, "剩余约10秒")

    def test_stop_torrents_skips_upload_tasks(self):
        """stop_torrents 跳过合成上传卡，不调 api_stop_sync_task。"""
        plugin = _plugin()
        plugin._serialize_runtime_tasks = lambda: []
        with mock.patch("time.monotonic", self.clock):
            self._seed_wenwu()
            torrents = plugin.list_torrents()
            self.assertEqual(len(torrents), 1)
            result = plugin.stop_torrents([torrents[0].hash])
        self.assertFalse(result)
        self.assertEqual(plugin._stop_calls, [])


if __name__ == "__main__":
    unittest.main()
