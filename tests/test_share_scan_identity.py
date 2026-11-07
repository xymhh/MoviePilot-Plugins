"""分享枚举适配 + 身份层（M2）测试。

覆盖：
* 枚举必须「递归到底」——不带 target_season、深度给到上限；
* 枚举异常/空结果不得冒充全量（complete=False）；
* TMDB 只提供集数，拿不到就是 None；
* 无季号时只在「某一季区间完整容纳全部集号」才反推季，否则不猜；
* 三层串起来（扫描结果 → 身份 → 计划）在真实样本上结果不变。
"""

import json
import os
import unittest

from plugin_env import REPO_ROOT, load_module, register_package

PKG = os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")
FIXTURES = os.path.join(REPO_ROOT, "tests", "fixtures", "share_scan")


def _load():
    register_package("cloudsubscribex", [PKG])
    register_package("cloudsubscribex.utils", [os.path.join(PKG, "utils")])
    scan = load_module("cloudsubscribex.utils.share_scan", "plugins.v2/cloudsubscribex/utils/share_scan.py")
    ident = load_module("cloudsubscribex.utils.share_identity",
                        "plugins.v2/cloudsubscribex/utils/share_identity.py")
    recognize = load_module("cloudsubscribex.utils.share_recognize",
                            "plugins.v2/cloudsubscribex/utils/share_recognize.py")
    return scan, ident, recognize


scan_mod, ident_mod, recognize_mod = _load()


class FakeService:
    """记录调用参数的假驱动。"""

    def __init__(self, files=None, error=None, supports_depth=True):
        self.files = files or []
        self.error = error
        self.calls = []
        self.supports_depth = supports_depth

    def list_share_files(self, share_url, **kwargs):
        self.calls.append(dict(kwargs))
        if self.error:
            raise self.error
        if not self.supports_depth and "max_depth" in kwargs:
            raise TypeError("unexpected keyword argument 'max_depth'")
        return list(self.files)


def _entries(*pairs):
    return [{"path": path, "size": size} for path, size in pairs]


class ScanShareTest(unittest.TestCase):
    def test_recurses_to_bottom_without_season_filter(self):
        service = FakeService([{"relative_path": "剧/01.mkv", "size": 10, "id": "1"}])
        result = scan_mod.scan_share(service, "https://pan.quark.cn/s/x", log_prefix="t")
        self.assertTrue(result["complete"])
        self.assertEqual(result["entries"][0]["path"], "剧/01.mkv")
        self.assertEqual(service.calls[0]["max_depth"], scan_mod.MAX_DEPTH)
        self.assertNotIn("target_season", service.calls[0])

    def test_failure_is_not_complete(self):
        service = FakeService(error=RuntimeError("限流"))
        result = scan_mod.scan_share(service, "u")
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "SCAN_FAILED")
        self.assertIn("限流", result["error"])

    def test_empty_share_is_not_complete(self):
        result = scan_mod.scan_share(FakeService([]), "u")
        self.assertFalse(result["complete"])
        self.assertEqual(result["reason"], "EMPTY")

    def test_driver_without_depth_kwarg_falls_back(self):
        service = FakeService([{"name": "a.mkv", "size": 1}], supports_depth=False)
        result = scan_mod.scan_share(service, "u")
        self.assertTrue(result["complete"])
        self.assertEqual(len(service.calls), 2)
        self.assertNotIn("max_depth", service.calls[1])

    def test_entries_without_path_are_dropped(self):
        service = FakeService([{"size": 1}, {"relative_path": "剧/01.mkv", "size": 2}])
        result = scan_mod.scan_share(service, "u")
        self.assertEqual([e["path"] for e in result["entries"]], ["剧/01.mkv"])


class SeasonTableTest(unittest.TestCase):
    def test_episodes_are_deduplicated_and_sorted(self):
        lookup = lambda tmdb_id, season: [
            {"episode_number": 2}, {"episode_number": 1}, {"episode_number": 2},
        ]
        self.assertEqual(ident_mod.season_episodes(1, 1, lookup), [1, 2])
        self.assertEqual(ident_mod.season_total(1, 1, lookup), 2)

    def test_missing_tmdb_data_returns_none(self):
        self.assertIsNone(ident_mod.season_total(1, 1, lambda _a, _b: []))
        self.assertIsNone(ident_mod.season_total(None, 1, lambda _a, _b: []))


class InferSeasonTest(unittest.TestCase):
    def test_unique_covering_season(self):
        totals = {1: 30, 2: 10}
        self.assertEqual(ident_mod.infer_season(range(1, 31), totals), 1)

    def test_picks_smallest_covering_season(self):
        totals = {1: 30, 2: 5}
        self.assertEqual(ident_mod.infer_season(range(1, 6), totals), 2)

    def test_ambiguous_totals_not_guessed(self):
        self.assertIsNone(ident_mod.infer_season(range(1, 6), {1: 5, 2: 5}))

    def test_out_of_range_not_guessed(self):
        self.assertIsNone(ident_mod.infer_season(range(1, 40), {1: 30, 2: 10}))

    def test_explicit_season_wins(self):
        self.assertEqual(ident_mod.infer_season(range(1, 5), {1: 5}, explicit=3), 3)


class PlanForShareTest(unittest.TestCase):
    def _fixture(self, name):
        with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
            return json.load(handle)["entries"]

    def test_real_sample_matches_pure_logic(self):
        entries = self._fixture("jiajinchai.json")
        lookup = lambda tmdb_id, season: [{"episode_number": i} for i in range(1, 31)]
        plan = ident_mod.plan_for_share(entries, work_id=300146, season=1, lookup=lookup)
        self.assertEqual(plan["status"], recognize_mod.READY)
        self.assertEqual(plan["group"], "4K DV杜比视界 高码率")
        self.assertEqual(len([p for p in plan["selected"] if p.endswith(".mkv")]), 30)
        self.assertEqual(plan["missing"], [])

    def test_no_tmdb_falls_back_to_hint(self):
        entries = _entries(("剧/01.mkv", 5 << 30), ("剧/02.mkv", 5 << 30))
        plan = ident_mod.plan_for_share(entries, work_id=9, season=1,
                                        season_total_hint=4, lookup=lambda _a, _b: [])
        self.assertEqual(plan["missing"], [3, 4])

    def test_unknown_identity_stays_review(self):
        entries = _entries(("剧/01.mkv", 5 << 30))
        plan = ident_mod.plan_for_share(entries, work_id=None, season=1)
        self.assertEqual(plan["status"], recognize_mod.REVIEW)
        self.assertEqual(plan["reason"], recognize_mod.NO_IDENTITY)


class RecognitionGateTest(unittest.TestCase):
    """M2-4：识别计划什么时候接管转存（纯函数口径）。"""

    def test_mode_off_for_movie_or_unknown_identity(self):
        self.assertEqual(scan_mod.recognition_mode(None, 1), "off")
        self.assertEqual(scan_mod.recognition_mode(1, None), "off")
        self.assertEqual(scan_mod.recognition_mode(1, 1), "plan")

    def test_manual_selection_wins(self):
        self.assertEqual(scan_mod.recognition_mode(1, 1, explicit_selection=True), "off")

    def test_skip_and_review_block(self):
        for status in ("skip", "review"):
            self.assertEqual(scan_mod.plan_action({"status": status}), "block")
        self.assertEqual(scan_mod.plan_action({"status": "ready"}), "apply")

    def test_filter_keeps_original_entries(self):
        files = [
            {"relative_path": "剧/01.mkv", "size": 1, "id": "a"},
            {"relative_path": "剧/02.mkv", "size": 1, "id": "b"},
        ]
        plan = {"selected": ["剧/01.mkv"]}
        kept = scan_mod.filter_by_plan(files, plan)
        self.assertEqual([item["id"] for item in kept], ["a"])
        # 保留原字典，转存需要的 id/size 字段不能丢
        self.assertIs(kept[0], files[0])


class WiringTest(unittest.TestCase):
    """漏斗确实接上了识别计划（静态接线检查）。"""

    def test_validated_resource_files_applies_plan(self):
        import ast

        path = os.path.join(PKG, "handlers", "sync", "resources.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        called = False
        guarded = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_validated_resource_files":
                continue
            source = ast.dump(node)
            called = "_apply_share_recognition" in source
            # 必须显式判断「识别后为空就不往下走」
            guarded = "if not files" in ast.unparse(node) or "if not files" in source
        self.assertTrue(called, "漏斗没有调用 _apply_share_recognition")
        self.assertTrue(guarded, "识别后返回空列表时缺少提前返回")


if __name__ == "__main__":
    unittest.main()
