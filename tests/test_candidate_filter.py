"""候选资源预筛回归测试（1.7.28）。

回归背景：线上日志 10 小时里 118 条「分享根目录与订阅标题不符」，每条都发生在
``list_share_files`` 之后——标题守卫只能拦下转存，拦不住那次分享目录读取；其中
还有「任素汐 找名字的人 电影 神探之痕迹 主题曲 (FLAC 24bit 48khz)」这类音乐分享
被当成影视资源校验。本用例锁住两件事：
1. 音频/电子书标题在列目录之前就被判定为非影视资源；
2. 同一份 (分享链接, 标题) 被判「标题不符」后 24h 内不再重复列目录。
"""

import time
import unittest

from plugin_env import load_module

_filter = load_module(
    "cloudsubscribex.handlers.sync.candidate_filter",
    "plugins.v2/cloudsubscribex/handlers/sync/candidate_filter.py",
)


class NonVideoReasonTest(unittest.TestCase):
    def test_audio_title_is_rejected(self):
        reason = _filter.non_video_reason(
            "任素汐 找名字的人 电影 神探之痕迹 主题曲 (2026) (FLAC 24bit 48khz)"
        )
        self.assertIn("FLAC", reason)

    def test_ebook_and_audiobook_titles_are_rejected(self):
        for title in ("某剧 有声书 全 320 集", "某某 电子书 EPUB 合集", "某某 原声带"):
            with self.subTest(title=title):
                self.assertIsNotNone(_filter.non_video_reason(title))

    def test_video_titles_are_kept(self):
        for title in (
            "小城良方 (2026) 4K 全 40 集",
            "伟大的长征 The Great Long March 2160p",
            "玉簟秋 S01E01-E24 1080p",
        ):
            with self.subTest(title=title):
                self.assertIsNone(_filter.non_video_reason(title))

    def test_ape_marker_needs_word_boundary(self):
        # ESCAPE 里的 APE 不该被当成音频标记
        self.assertIsNone(_filter.non_video_reason("ESCAPE PLAN 2013 1080p"))


class MismatchMemoTest(unittest.TestCase):
    def setUp(self):
        _filter._mismatch_records.clear()

    def test_remember_then_seen(self):
        url = "https://pan.quark.cn/s/abc123"
        self.assertFalse(_filter.mismatch_seen(url, "小城良方"))
        _filter.remember_mismatch(url, "小城良方")
        self.assertTrue(_filter.mismatch_seen(url, "小城良方"))
        # 同一链接换个标题仍要重新校验，避免误杀别的订阅
        self.assertFalse(_filter.mismatch_seen(url, "嫁金钗"))

    def test_expired_record_is_treated_as_miss(self):
        url = "https://pan.quark.cn/s/expired"
        _filter.remember_mismatch(url, "玉簟秋")
        key = (url, "玉簟秋")
        _filter._mismatch_records[key] = time.time() - 1
        self.assertFalse(_filter.mismatch_seen(url, "玉簟秋"))
        self.assertNotIn(key, _filter._mismatch_records)

    def test_records_are_bounded(self):
        for index in range(_filter._MISMATCH_MAXSIZE + 10):
            _filter.remember_mismatch(f"https://pan.quark.cn/s/{index}", "伟大的长征")
        self.assertLessEqual(len(_filter._mismatch_records), _filter._MISMATCH_MAXSIZE)


if __name__ == "__main__":
    unittest.main()
