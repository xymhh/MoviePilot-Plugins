"""v1.29.4 第 3 步：两条转存流程合并成唯一流水线。

普通订阅与洗版订阅都走 ``TelevisionSyncProcessor.process_tv_subscribe``，
洗版只通过 ``_upgrade_plan`` 传入参数（基线、待搜集数、排序、每轮上限、
不结算订阅）。用真实类（TelevisionSyncProcessor / UpgradeService /
SubscribeFlowService / UpgradeRuleScoringService），不用 _Fake 拼逻辑。

锁定的行为（改测试只许收紧断言、不许放松）：
- 普通订阅：只转存缺集，结算走 check_and_finish_subscribe；
- 洗版：缺集在前、洗版在后、每轮上限只卡洗版部分、不调
  check_and_finish_subscribe，改为 update_subscribe_progress；
- 手动洗版入口仍通（直接调 UpgradeService 入口）；
- 基线已达质量上限（100）的集不进搜索。
"""

import unittest

# 先加载 dedup harness（内含真实 rule_scoring），再加载 manual-dedupe
# harness（内含真实 television / upgrade / subscribe_flow）。
from test_episode_dedup import rule_scoring_module  # noqa: E402,F401
from test_manual_share_dedupe import (  # noqa: E402
    FakeOwner,
    TelevisionSyncProcessor,
    UpgradeService,
    _manual_resource,
    _share_files,
    _subscribe,
)

UpgradeRuleScoringService = rule_scoring_module.UpgradeRuleScoringService


def _baseline_entry(episode, score, size=5_000_000_000):
    return {
        "file_name": f"兰香如故.S01E{episode:02d}.mkv",
        "file_size": size,
        "source": "真实网盘文件",
        "score": score,
        "rule_score": score,
        "cloud_dir": "/电视剧/国剧/兰香如故 (2026)/Season 01",
        "target_file_name": f"兰香如故.S01E{episode:02d}.mkv",
        "cloud_file_id": f"old-{episode}",
    }


class MergeOwner(FakeOwner):
    """FakeOwner + 罐装基线 + 真实评分组件 + 可控分享内容。

    判重关闭（_episode_dedup_enabled=False）：统一流水线的评分兜底
    走真实 ``_should_upgrade_candidate``；_match_episode_files 给
    每个匹配集固定 10 分（FakeOwner 的桩）。
    """

    def __init__(self, baseline, *, cloud_upgrade_subscribe, limit=0,
            share_episodes=None, share_files=None,
            enable_cloud_upgrade=True):
        super().__init__(enable_cloud_upgrade=enable_cloud_upgrade,
                        cloud_episodes=set())
        self._baseline = baseline
        self._cloud_upgrade_subscribe = cloud_upgrade_subscribe
        self._upgrade_episode_limit = limit
        self._upgrade_mode = "largest"
        self._quality_min_gain_percent = 10
        self._quality_min_gain_mb = 200
        self._prefer_template_named_version = True
        self._share_files = (
            share_files if share_files is not None else _share_files()
        )
        self._share_episodes = (
            share_episodes if share_episodes is not None else set(range(1, 43))
        )
        # 真实评分组件：判重关闭时统一流水线的评分兜底走它。
        scoring = UpgradeRuleScoringService(self)
        self.upgrade_rule_scoring._should_upgrade_candidate = (
            scoring._should_upgrade_candidate
        )
        self.upgrade_rule_scoring._resource_size_bytes = (
            UpgradeRuleScoringService._resource_size_bytes
        )

    def _is_cloud_upgrade_subscribe(self, subscribe):
        return self._cloud_upgrade_subscribe

    def _read_ep_priority(self, subscribe):
        return {}

    def _build_episode_baseline(self, subscribe, mediainfo, season,
            include_saved=True):
        return dict(self._baseline)

    def _validated_resource_files(self, url, resource_title="",
            target_season=None, log_prefix="",
            target_file_ids=None, target_file_names=None,
            recognition_work_id=None, recognition_season=None):
        return list(self._share_files)

    def _summarize_share_episodes(self, files, season, mediainfo=None):
        return len(files), set(self._share_episodes)

    def _transfer_episode_items(self, items, *args, **kwargs):
        """FakeOwner 的桩只记录不返回；这里返回全成功的结果，让结算跑完。"""
        self.transfer_items.append(list(items))
        return [
            {"item": item, "success": True, "pending_key": None}
            for item in items
        ]


class TvFlowMergeTests(unittest.TestCase):
    def _search_stub(self, owner, resource=None):
        owner._search_handler.search_sources.return_value = {
            "manual": [resource or _manual_resource()],
        }

    def test_normal_subscribe_transfers_only_missing_and_settles(self):
        """普通订阅：云端已有 1-3 集、共 5 集 → 只转 4、5 集，结算走 MP。"""
        owner = MergeOwner({}, cloud_upgrade_subscribe=False)
        owner.cloud_episodes = {1, 2, 3}
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=5)
        result = TelevisionSyncProcessor(owner).process_tv_subscribe(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 2)
        self.assertEqual(len(owner.transfer_items), 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [4, 5])
        self.assertTrue(all(not item["is_upgrade"] for item in items))
        owner._subscribe_handler.check_and_finish_subscribe.assert_called_once()
        call = owner._subscribe_handler.check_and_finish_subscribe.call_args
        self.assertEqual(set(call.kwargs["success_episodes"]), {1, 2, 3, 4, 5})
        owner._subscribe_handler.update_subscribe_progress.assert_not_called()

    def test_upgrade_missing_first_limit_and_no_settlement(self):
        """洗版：基线 1-34 集、共 36 集、上限 1 → 搜 [35,36,1]，不结算订阅。"""
        baseline = {ep: _baseline_entry(ep, 5) for ep in range(1, 35)}
        owner = MergeOwner(baseline, cloud_upgrade_subscribe=True, limit=1)
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=36)
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 3)
        self.assertEqual(len(owner.transfer_items), 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [35, 36, 1])
        self.assertEqual(
            [item["is_upgrade"] for item in items], [False, False, True]
        )
        self.assertEqual(items[2]["upgrade_old_file_id"], "old-1")
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()
        owner._subscribe_handler.update_subscribe_progress.assert_called_once()
        call = owner._subscribe_handler.update_subscribe_progress.call_args
        self.assertEqual(call.kwargs["success_episodes"], [1, 35, 36])

    def test_upgrade_skips_worse_existing_via_manual_entry(self):
        """手动洗版入口：基线 E05=20 分、分享含 E05+E06 → 只转 E06。"""
        baseline = {5: _baseline_entry(5, 20)}
        owner = MergeOwner(
            baseline, cloud_upgrade_subscribe=True,
            share_episodes={5, 6},
            share_files=[
                {"id": "5", "name": "兰香如故.S01E05.mkv", "size": 5_100_000_000},
                {"id": "6", "name": "兰香如故.S01E06.mkv", "size": 5_100_000_000},
            ],
        )
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe=_subscribe(total_episode=0),
            history=[], transfer_details=[], transferred_count=0,
            manual_upgrade=False,
            manual_resources=[_manual_resource()],
        )
        self.assertEqual(result, 1)
        self.assertEqual(len(owner.transfer_items), 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [6])
        self.assertFalse(items[0]["is_upgrade"])
        owner._subscribe_handler.check_and_finish_subscribe.assert_not_called()

    def test_upgrade_replace_marks_is_upgrade_and_old_version(self):
        """洗版替换：基线 E01=5 分、候选 10 分 → is_upgrade 且带旧版 id。"""
        baseline = {1: _baseline_entry(1, 5)}
        owner = MergeOwner(baseline, cloud_upgrade_subscribe=True)
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=1)
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [1])
        self.assertTrue(items[0]["is_upgrade"])
        self.assertEqual(items[0]["upgrade_old_file_id"], "old-1")
        self.assertEqual(
            items[0]["upgrade_old_file_name"], "兰香如故.S01E01.mkv"
        )
        owner._subscribe_handler.update_subscribe_progress.assert_called_once()
        call = owner._subscribe_handler.update_subscribe_progress.call_args
        self.assertEqual(call.kwargs["success_episodes"], [1])

    def test_upgrade_ceiling_episode_not_searched(self):
        """质量上限：基线 E01=100 分（满分）、E02=5 分 → 只搜 E02 且替换。"""
        baseline = {1: _baseline_entry(1, 100), 2: _baseline_entry(2, 5)}
        owner = MergeOwner(baseline, cloud_upgrade_subscribe=True)
        self._search_stub(owner)
        subscribe = _subscribe(total_episode=2)
        result = UpgradeService(owner)._process_tv_subscribe_upgrade(
            subscribe, [], [], 0,
        )
        self.assertEqual(result, 1)
        items = owner.transfer_items[0]
        self.assertEqual([item["episode"] for item in items], [2])
        self.assertTrue(items[0]["is_upgrade"])


if __name__ == "__main__":
    unittest.main()
