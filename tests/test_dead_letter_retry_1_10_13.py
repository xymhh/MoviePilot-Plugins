"""1.10.13：历史重试可重开账本死信（LOST10 S01E05）。

LOST10 的账本作业在 1.10.12 上已是 failed（重试 3 次耗尽），历史重试
经 ``_transfer_file`` 调 ``ledger.begin`` 时直接被
「作业由其他 worker 持有或已终止：failed」挡住，连下载都走不到。
只有用户显式点的历史重试才允许重开死信；自动同步、字幕转存不传
这个开关，死信仍会被账本挡住。
"""

from __future__ import annotations

import os
import unittest

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(CURRENT_DIR)
PLUGIN_ROOT = os.path.join(PROJECT_ROOT, "plugins.v2", "cloudsubscribex")


def _read(*parts):
    with open(os.path.join(PLUGIN_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class DeadLetterRetryWiringTests(unittest.TestCase):
    def test_history_retry_reopens_dead_letter(self):
        retry_source = _read("handlers", "sync", "retry.py")
        self.assertIn(
            "allow_reopen_dead_letter=True", retry_source,
            "历史重试必须把重开死信的开关传给 _transfer_file",
        )

    def test_only_history_retry_passes_true(self):
        hits = []
        for root, _dirs, files in os.walk(PLUGIN_ROOT):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                if "allow_reopen_dead_letter=True" in text:
                    hits.append(os.path.relpath(path, PLUGIN_ROOT))
        self.assertEqual(
            hits, [os.path.join("handlers", "sync", "retry.py")],
            "只有历史重试能传 True；自动同步、字幕转存一旦传了，"
            "死信就会被无限重跑",
        )

    def test_transfer_file_threads_flag_to_ledger(self):
        source = _read("handlers", "sync", "transfer_ops.py")
        self.assertIn("allow_reopen_dead_letter: bool = False", source)
        self.assertIn(
            "allow_reopen_dead_letter=allow_reopen_dead_letter", source,
        )

    def test_ledger_reopen_is_gated_by_dead_letter_check(self):
        source = _read("core", "database", "sync_job_ledger.py")
        self.assertIn("allow_reopen_dead_letter: bool = False", source)
        self.assertIn("is_dead_letter(", source)
        self.assertIn("reopen_dead_letter(", source)


if __name__ == "__main__":
    unittest.main()
