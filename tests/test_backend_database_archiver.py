"""私有数据库生命周期归档与清理单元测试。"""

import unittest
from unittest.mock import MagicMock

from plugin_env import load_module

_archiver = load_module(
    "cloudsubscribex.core.database.archiver",
    "plugins.v2/cloudsubscribex/core/database/archiver.py",
)


class DatabaseArchiverTest(unittest.TestCase):
    def test_archive_empty_store_returns_safe_dict(self):
        res = _archiver.archive_database(None)
        self.assertEqual(res["ledger_purged"], 0)
        self.assertEqual(res["jobs_purged"], 0)
        self.assertTrue(res["success"])

    def test_archive_calls_purge_on_repos(self):
        store = MagicMock()
        store.candidate_ledger.purge.return_value = 150
        store.sync_jobs.purge_expired.return_value = 45

        res = _archiver.archive_database(store, keep_days_ledger=14, keep_days_jobs=7)
        self.assertEqual(res["ledger_purged"], 150)
        self.assertEqual(res["jobs_purged"], 45)
        self.assertTrue(res["success"])
        store.candidate_ledger.purge.assert_called_once_with(keep_days=14)
        store.sync_jobs.purge_expired.assert_called_once_with(keep_days=7)

    def test_archive_handles_repo_exceptions_gracefully(self):
        store = MagicMock()
        store.candidate_ledger.purge.side_effect = RuntimeError("database locked")
        store.sync_jobs.purge_expired.return_value = 0

        res = _archiver.archive_database(store)
        self.assertFalse(res["success"])
        self.assertEqual(res["ledger_purged"], 0)


if __name__ == "__main__":
    unittest.main()
