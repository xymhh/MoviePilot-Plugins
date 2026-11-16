"""v1.12.11：手动添加的平台整理历史改记「本地 → 115网盘CS版」。

用户反馈（2026-11-16 23:11）：媒体整理记录里出现了「手动添加 →
115网盘」的记录。浏览器核查证实这些是插件写的跨盘转存记录（夸克
→ 本地 → 115）：来源标「手动添加」、目标存网盘驱动名「115网盘」，
而 MP 自己写的同一集记录是「本地 → 115网盘CS版」（移动）。两边
看着不一致，且「115网盘」与 MP 原生 115 存储同名、会误导。

改法：平台历史条目的来源标签里 manual 记「本地」（文件进 115 前
在本地 /downloads 中转），目标存储改记 MP 存储名「115网盘CS版」
（不用网盘驱动名「115网盘」）。老记录靠插件启动时的对账自动替换：
对账按 src 匹配，旧「手动添加 · …」不在新期望集合里会被删掉，
新「本地 · …」会被加上；已交 MP 整理的仍跳过（v1.12.8）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_platform_history_skip_organized_1_12_8 import (
    _FakeSession,
    _FakeTransferHistory,
    _Service,
    _install_session,
    _record,
)


class TestManualRecordLabels(unittest.TestCase):
    def test_manual_record_is_local_to_u115cs(self):
        """手动添加的未整理记录记成「本地 → 115网盘CS版」。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        manual = _record(
            source="manual",
            title="玫瑰战争",
            tmdb_id=299276,
            season=1,
            episode=25,
            success_episodes=[25],
            file_name="玫瑰战争 {tmdb-299276} - S01E25 - 第25集.mp4",
        )
        result = service._record_platform_transfer_histories([manual])
        self.assertEqual(result, 1)
        self.assertEqual(len(session.added), 1)
        entry = session.added[0]
        self.assertEqual(entry.src_storage, "本地")
        self.assertEqual(entry.dest_storage, "115网盘CS版")
        self.assertTrue(entry.src.startswith("本地 · 玫瑰战争 S01E25"))
        self.assertEqual(entry.mode, "copy")
        self.assertEqual(entry.downloader, "网盘订阅助手")

    def test_manual_record_src_storage_in_fileitem(self):
        """手动记录的源/目标文件项存储也与展示一致。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        manual = _record(source="manual")
        service._record_platform_transfer_histories([manual])
        entry = session.added[0]
        self.assertEqual(entry.src_fileitem["storage"], "本地")
        self.assertEqual(entry.dest_fileitem["storage"], "115网盘CS版")


class TestReconcileReplacesManualLabels(unittest.TestCase):
    def test_reconcile_replaces_old_manual_record(self):
        """对账时旧「手动添加 → 115网盘」记录被删，新「本地 → 115网盘CS版」被加上。"""
        stale = _FakeTransferHistory(
            src="手动添加 · 玫瑰战争 S01E25",
            src_storage="手动添加",
            dest_storage="115网盘",
            mode="copy",
        )
        session = _FakeSession(rows=[stale])
        _install_session(session)
        service = _Service(session)
        manual = _record(
            source="manual",
            title="玫瑰战争",
            tmdb_id=299276,
            season=1,
            episode=25,
            success_episodes=[25],
            file_name="玫瑰战争 {tmdb-299276} - S01E25 - 第25集.mp4",
        )
        result = service._record_platform_transfer_histories(
            [manual], reconcile=True
        )
        self.assertEqual(result, 2)
        self.assertEqual(session.deleted, [stale])
        self.assertEqual(len(session.added), 1)
        entry = session.added[0]
        self.assertEqual(entry.src_storage, "本地")
        self.assertEqual(entry.dest_storage, "115网盘CS版")
        self.assertTrue(entry.src.startswith("本地 · 玫瑰战争 S01E25"))

    def test_reconcile_keeps_new_manual_record(self):
        """对账时已是新标签的手动记录会被保留（不删不增）。"""
        session = _FakeSession()
        _install_session(session)
        service = _Service(session)
        manual = _record(source="manual")
        entries = service._platform_history_entries(manual)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["src_storage"], "本地")
        self.assertEqual(entries[0]["dest_storage"], "115网盘CS版")
        existing = _FakeTransferHistory(**entries[0])
        session.rows = [existing]
        result = service._record_platform_transfer_histories(
            [manual], reconcile=True
        )
        self.assertEqual(result, 0)
        self.assertEqual(session.deleted, [])
        self.assertEqual(session.added, [])


if __name__ == "__main__":
    unittest.main()
