"""候选提交统一入口（P2a）：submit_candidate 的契约与账本落库。"""

import unittest

from plugin_env import install_app_mocks, load_module

install_app_mocks(
    "app.log",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.services",
)

discovery = load_module(
    "cloudsubscribex.core.services.discovery",
    "plugins.v2/cloudsubscribex/core/services/discovery.py",
)

Candidate = discovery.Candidate
submit_candidate = discovery.submit_candidate


class _FakeStore:
    def __init__(self):
        self.rows = []
        self.run_ids = []

    def append(self, items, run_id=""):
        self.rows.extend(items)
        self.run_ids.append(run_id)
        return len(items)


class _FakeDataStore:
    def __init__(self):
        self.store = _FakeStore()

    def ledger_store(self):
        return self.store


class _Owner:
    def __init__(self, result=None):
        self.data_store = _FakeDataStore()
        self.calls = []
        self._result = result if result is not None else {"success": True, "message": "转存 2 个文件"}

    def _get_data_store(self):
        return self.data_store


class _FakeSyncApi:
    """替身：记录 payload，返回预置结果。"""

    instances = []

    def __init__(self, owner):
        self.owner = owner
        _FakeSyncApi.instances.append(self)

    def api_vue_start_manual_sync(self, payload, wait=False):
        self.owner.calls.append({"payload": payload, "wait": wait})
        return self.owner._result


class TestCandidate(unittest.TestCase):

    def test_clean_links_dedupes_and_caps(self):
        cand = Candidate(links=[" a ", "", "a", "b"] + [f"l{i}" for i in range(80)])
        cleaned = cand.clean_links()
        self.assertEqual(cleaned[:3], ["a", "b", "l0"])
        self.assertEqual(len(cleaned), discovery.MAX_LINKS)

    def test_via_formats(self):
        self.assertEqual(Candidate().via(), "push")
        self.assertEqual(Candidate(channel="@QukanMovie").via(), "tg:@QukanMovie")
        self.assertEqual(
            Candidate(channel="@QukanMovie", message_id=12281).via(),
            "tg:@QukanMovie/12281",
        )


class TestSubmitCandidate(unittest.TestCase):

    def setUp(self):
        import sys
        _FakeSyncApi.instances = []
        self._sys = sys
        self._mod_name = "cloudsubscribex.core.api.sync"
        self._orig = sys.modules.get(self._mod_name)
        mod = type(sys)("cloudsubscribex.core.api.sync")
        mod.SyncApi = _FakeSyncApi
        sys.modules[self._mod_name] = mod

    def tearDown(self):
        if self._orig is not None:
            self._sys.modules[self._mod_name] = self._orig

    def _patch_sync(self):
        return None

    def test_success_passes_links_and_titles(self):
        owner = _Owner()
        self._patch_sync()
        result = submit_candidate(
            owner,
            Candidate(links=["https://pan.quark.cn/s/x"], title="尚公主 4K",
                      subscribe_id=249, channel="@QukanMovie", message_id=7),
        )
        self.assertTrue(result.success)
        self.assertEqual(result.via, "tg:@QukanMovie/7")
        payload = owner.calls[0]["payload"]
        self.assertEqual(payload["resource_links"], ["https://pan.quark.cn/s/x"])
        self.assertEqual(payload["subscribe_id"], 249)
        self.assertEqual(payload["resource_titles"]["https://pan.quark.cn/s/x"], "尚公主 4K")
        self.assertTrue(owner.calls[0]["wait"])

    def test_empty_links_fails_without_calling_transfer(self):
        owner = _Owner()
        self._patch_sync()
        result = submit_candidate(owner, Candidate(links=["  "], subscribe_id=1))
        self.assertFalse(result.success)
        self.assertIn("没有可转存", result.message)
        self.assertEqual(owner.calls, [])

    def test_exception_becomes_failure_not_raise(self):
        owner = _Owner()

        class _Boom:
            def __init__(self, owner):
                raise RuntimeError("boom")

        mod = type(self._sys)("cloudsubscribex.core.api.sync")
        mod.SyncApi = _Boom
        self._sys.modules[self._mod_name] = mod
        result = submit_candidate(owner, Candidate(links=["l"], subscribe_id=1))
        self.assertFalse(result.success)
        self.assertIn("boom", result.message)

    def test_ledger_row_written(self):
        owner = _Owner()
        self._patch_sync()
        submit_candidate(
            owner,
            Candidate(links=["l"], title="小城良方", subscribe_id=236,
                      channel="@QukanMovie", message_id=9, run_id="run-1"),
        )
        rows = owner.data_store.store.rows
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "push_submit")
        self.assertEqual(rows[0]["provider"], "频道监控")
        self.assertEqual(rows[0]["reference"], "tg:@QukanMovie/9")
        self.assertEqual(rows[0]["subscribe_id"], 236)
        self.assertEqual(rows[0]["resource_title"], "小城良方")
        self.assertEqual(owner.data_store.store.run_ids, ["run-1"])

    def test_to_payload_keeps_success_message_contract(self):
        owner = _Owner(result={"success": False, "message": "库中已有"})
        self._patch_sync()
        payload = submit_candidate(
            owner, Candidate(links=["l"], channel="@c", message_id=1)
        ).to_payload()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["message"], "库中已有")
        self.assertEqual(payload["discovered_via"], "tg:@c/1")


if __name__ == "__main__":
    unittest.main()
