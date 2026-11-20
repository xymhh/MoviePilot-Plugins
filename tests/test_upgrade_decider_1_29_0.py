"""v1.29.0 第 1 步：洗版判定器收敛单测。

覆盖三层：

1. 新判定器本身（``handlers/sync/upgrade_decision.py`` 的
   ``decide_upgrade``，真实实现）：无旧版、评分升/降、评分相等时的
   largest/smallest/其他模式、体积/分辨率优先分支（伪 4K、增益不足、
   分辨率升/降）、模板保留（含“模板符合不否决已判定的升级”、模板开关
   关闭、模板名为空字符串的旧边角）。
2. 委托关系：评分制薄包装 ``_should_upgrade_candidate``、季集判重
   ``_episode_dedup_decision``、体积/分辨率制 ``compare_quality`` ——
   各至少一个用例走原入口，断言动作与原因和直接调判定器逐字一致。
3. 旧行为锁定：原因文案沿用旧措辞（``assertIn`` 锁关键片段，
   ``assertEqual`` 锁模板保留这类整句）。

判定器取自 ``test_episode_dedup`` 的加载链（该文件加载
``episode_dedup`` / ``rule_scoring`` / ``naming`` 时会连带加载
``upgrade_decision``）；``format_size`` 沿用该文件的测试桩
（``lambda n: f"{n}B"``），原因里的体积片段因此是确定性的。
"""

import os
import sys
import unittest
from types import SimpleNamespace

from test_episode_dedup import _MEDIA, _SUB, _make_handler

_decision_module = sys.modules.get(
    "cloudsubscribex.handlers.sync.upgrade_decision"
)
assert _decision_module is not None, (
    "upgrade_decision 未被 test_episode_dedup 的加载链带入，"
    "请检查 episode_dedup / rule_scoring 是否仍委托给判定器"
)
decide_upgrade = _decision_module.decide_upgrade
UpgradeDecisionPolicy = _decision_module.UpgradeDecisionPolicy

MB = 1024 * 1024
GB = 1024 * MB


def _template_aware_rename(root_path, subscribe, mediainfo, source_name,
                            season=None, episode=None):
    """桩：只有已按模板命名的名字（剧集 SxxExx）渲染后才等于自身。"""
    suffix = os.path.splitext(str(source_name))[1]
    if episode:
        canonical = (
            f"{subscribe.name} - S{int(season or 1):02d}"
            f"E{int(episode):02d}{suffix}"
        )
    else:
        canonical = f"{mediainfo.title_year}{suffix}"
    return SimpleNamespace(
        parent=SimpleNamespace(as_posix=lambda: "/媒体/剧集/Show/Season 01"),
        name=canonical,
    )


def _handler(**overrides):
    """沿用 test_episode_dedup 的 handler 构造，补齐洗版阈值属性。"""
    overrides.setdefault("platform_rename_path", _template_aware_rename)
    handler = _make_handler(**overrides)
    handler._quality_prefer_enabled = overrides.get(
        "quality_prefer_enabled", False
    )
    handler._quality_min_gain_percent = overrides.get(
        "quality_min_gain_percent", 10
    )
    handler._quality_min_gain_mb = overrides.get("quality_min_gain_mb", 200)
    return handler


def _policy_for(handler, quality_prefer=False,
                template_fallback_for_any_skip=False):
    """按薄包装的取数口径，从 handler 属性拼出策略参数。"""
    return UpgradeDecisionPolicy(
        mode=str(getattr(handler, "_upgrade_mode", "largest") or "largest"),
        min_gain_percent=int(
            getattr(handler, "_quality_min_gain_percent", 10) or 0
        ),
        min_gain_mb=int(getattr(handler, "_quality_min_gain_mb", 200) or 0),
        prefer_template_named_version=bool(
            getattr(handler, "_prefer_template_named_version", True)
        ),
        quality_prefer=quality_prefer,
        template_fallback_for_any_skip=template_fallback_for_any_skip,
    )


def _versions(name, size, score=0, file_id="f1"):
    return [{
        "file_id": file_id,
        "file_name": name,
        "cloud_dir": "/d",
        "file_size": size,
        "score": score,
    }]


class DecideUpgradeScoreTests(unittest.TestCase):
    """直接判定器：评分分支。"""

    def test_no_existing_replaces(self):
        action, reason = decide_upgrade(
            has_existing=False, old_score=99, new_score=1,
        )
        self.assertEqual((action, reason), ("replace", "无现有版本"))

    def test_score_up_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=3, new_score=8,
        )
        self.assertEqual(action, "replace")
        self.assertEqual(reason, "评分提升 3→8")

    def test_score_down_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=8, new_score=3,
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "评分降低 8→3")

    def test_equal_largest_bigger_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "replace")
        self.assertEqual(reason, "同评分保留更大文件 1000B→1500B")

    def test_equal_largest_not_bigger_skips(self):
        # 旧评分制：大 1 字节就换；这里新文件反而更小 → 不换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1500, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且候选文件未更大")

    def test_equal_largest_same_size_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且候选文件未更大")

    def test_equal_smallest_smaller_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1500, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="smallest"),
        )
        self.assertEqual(action, "replace")
        self.assertEqual(reason, "同评分保留更小文件 1500B→1000B")

    def test_equal_smallest_not_smaller_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="smallest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且候选文件未更小")

    def test_equal_other_mode_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="replace"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分不执行替换")


class DecideUpgradeTemplateTests(unittest.TestCase):
    """直接判定器：模板保留（只挡无收益，不否决已判定的升级）。"""

    _OLD = "正相反的你与我 - S01E01.mkv"

    def test_template_kept_only_without_gain(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1000,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason, f"现有版本已符合当前重命名模板，保留 {self._OLD}"
        )

    def test_template_does_not_veto_score_upgrade(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=3, new_score=8,
            template_conformant_name=self._OLD,
        )
        self.assertEqual(action, "replace")
        self.assertEqual(reason, "评分提升 3→8")
        self.assertNotIn("已符合当前重命名模板", reason)

    def test_template_does_not_veto_size_upgrade(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "replace")
        self.assertIn("同评分保留更大文件", reason)

    def test_template_does_not_veto_score_down_skip_reason(self):
        # 评分降低时模板不改原因：旧评分制在比较出降低后直接返回。
        action, reason = decide_upgrade(
            has_existing=True, old_score=8, new_score=3,
            template_conformant_name=self._OLD,
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "评分降低 8→3")

    def test_template_fallback_rewrites_score_down_reason(self):
        # 旧判重外层的 _keep_template_version 会改写任何不换的原因：评分
        # 降低 + 模板符合时，判重路径的原因是模板保留而不是“评分降低”。
        # 只有判重包装开 template_fallback_for_any_skip 才走这条。
        action, reason = decide_upgrade(
            has_existing=True, old_score=8, new_score=3,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(
                template_fallback_for_any_skip=True
            ),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason, f"现有版本已符合当前重命名模板，保留 {self._OLD}"
        )

    def test_template_switch_off_ignores_conformant_name(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1000,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(
                mode="largest", prefer_template_named_version=False
            ),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且候选文件未更大")

    def test_template_empty_name_still_counts_as_conformant(self):
        # 旧边角：_keep_template_version 里 file_name 为空时原因照样写
        # “保留 ”（空名），判定仍按模板保留；这里锁定该行为不漂移。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1000,
            template_conformant_name="",
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "现有版本已符合当前重命名模板，保留 ")


class DecideUpgradeQualityTests(unittest.TestCase):
    """直接判定器：体积/分辨率优先分支（不看评分）。"""

    _POLICY = UpgradeDecisionPolicy(quality_prefer=True)

    def test_resolution_upgrade_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=99, new_score=0,
            new_file_name="某剧.S01E01.2160p.mkv", new_size=2 * GB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1 * GB),
            policy=self._POLICY,
        )
        # 评分更低也不挡：该分支本来就不看评分。
        self.assertEqual(action, "replace")
        self.assertIn("按体积择优", reason)
        self.assertIn("1080p → 2160p", reason)

    def test_fake_4k_blocked(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.2160p.mkv",
            new_size=500 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1 * GB),
            policy=self._POLICY,
        )
        self.assertEqual(action, "skip")
        self.assertIn("现有版本更优", reason)
        self.assertIn("疑似伪 4K/片段", reason)

    def test_resolution_downgrade_keeps(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.720p.mkv",
            new_size=4 * GB,
            old_versions=_versions("某剧.S01E01.2160p.mkv", 1 * GB),
            policy=self._POLICY,
        )
        self.assertEqual(action, "skip")
        self.assertIn("现有版本更优", reason)
        self.assertIn("低于现有 2160p", reason)

    def test_same_resolution_small_gain_keeps(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.1080p.mkv",
            new_size=1050 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1000 * MB),
            policy=self._POLICY,
        )
        self.assertEqual(action, "skip")
        self.assertIn("体积增益不足", reason)

    def test_same_resolution_big_gain_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.1080p.mkv",
            new_size=1400 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1000 * MB),
            policy=self._POLICY,
        )
        self.assertEqual(action, "replace")
        self.assertIn("按体积择优", reason)
        self.assertIn("+40%", reason)

    def test_template_kept_only_without_gain(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.mkv",
            new_size=1000 * MB,
            old_versions=_versions("某剧.S01E01.mkv", 1000 * MB),
            template_conformant_name="某剧.S01E01.mkv",
            policy=self._POLICY,
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason, "现有版本已符合当前重命名模板，保留 某剧.S01E01.mkv"
        )

    def test_template_does_not_veto_quality_upgrade(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.1080p.mkv",
            new_size=1500 * MB,
            old_versions=_versions("某剧.S01E01.720p.mkv", 600 * MB),
            template_conformant_name="某剧.S01E01.720p.mkv",
            policy=self._POLICY,
        )
        self.assertEqual(action, "replace")
        self.assertNotIn("已符合当前重命名模板", reason)

    def test_unknown_old_size_upgrades_when_new_known(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.mkv",
            new_size=500 * MB, old_versions=[],
            policy=self._POLICY,
        )
        self.assertEqual(action, "replace")
        self.assertIn("旧文件体积未知", reason)


class ScorerDelegationTests(unittest.TestCase):
    """委托关系：评分制薄包装与直接调判定器逐字一致。"""

    _OLD = "正相反的你与我 - S01E01.mkv"

    def _scorer(self, **overrides):
        handler = _handler(
            prefer_template_named_version=True, **overrides
        )
        return handler, handler._owner.upgrade_rule_scoring

    def _expected(self, handler, old_score, new_score, old_size, new_size,
                old_file_name=""):
        scoring = handler._owner.upgrade_rule_scoring
        template_name = None
        if old_file_name and scoring._version_matches_rename_template(
                _SUB, _MEDIA, old_file_name, 1, 1):
            template_name = old_file_name
        return decide_upgrade(
            has_existing=True, old_score=old_score, new_score=new_score,
            old_size=old_size, new_size=new_size,
            old_file_name=old_file_name,
            template_conformant_name=template_name,
            policy=_policy_for(handler),
        )

    def _assert_same(self, handler, scoring, old_score, new_score,
                    old_size, new_size, old_file_name=""):
        expected = self._expected(
            handler, old_score, new_score, old_size, new_size, old_file_name
        )
        got = scoring._should_upgrade_candidate(
            old_score, new_score, old_size, new_size,
            old_file_name=old_file_name,
            subscribe=_SUB, mediainfo=_MEDIA, season=1, episode=1,
        )
        self.assertEqual(got, (expected[0] == "replace", expected[1]))

    def test_score_up(self):
        handler, scoring = self._scorer()
        self._assert_same(handler, scoring, 3, 99, 100, 9999, self._OLD)

    def test_score_down(self):
        handler, scoring = self._scorer()
        self._assert_same(handler, scoring, 99, 3, 100, 9999, self._OLD)

    def test_equal_largest_bigger(self):
        handler, scoring = self._scorer()
        self._assert_same(
            handler, scoring, 99, 99, 1_500_000_000, 4_800_000_000,
            self._OLD,
        )

    def test_equal_largest_not_bigger_template_kept(self):
        handler, scoring = self._scorer()
        self._assert_same(
            handler, scoring, 99, 99, 4_800_000_000, 4_800_000_000,
            self._OLD,
        )

    def test_equal_smallest_smaller(self):
        handler, scoring = self._scorer(upgrade_mode="smallest")
        self._assert_same(
            handler, scoring, 5, 5, 4_800_000_000, 1_500_000_000,
            self._OLD,
        )

    def test_no_existing(self):
        handler, scoring = self._scorer()
        got = scoring._should_upgrade_candidate(
            99, 1, 100, 9999, has_existing=False,
            subscribe=_SUB, mediainfo=_MEDIA, season=1, episode=1,
        )
        self.assertEqual(got, (True, "无现有版本"))


class DedupDelegationTests(unittest.TestCase):
    """委托关系：季集判重与直接调判定器逐字一致。"""

    def _expected(self, handler, versions, new_score, new_size,
                new_file_name):
        quality_active = handler._quality_prefer_active()
        if quality_active:
            best_old_score, best_old_size = 0, 0
        else:
            best_old_score = max(int(v.get("score") or 0) for v in versions)
            best_old_size = max(int(v.get("file_size") or 0) for v in versions)
        template_version = (
            handler._owner.upgrade_rule_scoring._template_conformant_version(
                versions, _SUB, _MEDIA, 1, 1
            )
        )
        template_name = (
            str(template_version.get("file_name") or "").strip()
            if template_version is not None else None
        )
        return decide_upgrade(
            has_existing=True, old_score=best_old_score,
            new_score=new_score, old_size=best_old_size, new_size=new_size,
            new_file_name=new_file_name, old_versions=list(versions),
            template_conformant_name=template_name,
            policy=_policy_for(
                handler, quality_prefer=quality_active,
                template_fallback_for_any_skip=True,
            ),
        )

    def _assert_same(self, handler, versions, new_score, new_size,
                    new_file_name):
        expected = self._expected(
            handler, versions, new_score, new_size, new_file_name
        )
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=new_score, new_size=new_size,
            existing_versions={1: list(versions)},
            new_file_name=new_file_name,
        )
        self.assertEqual(action, expected[0])
        self.assertEqual(reason, expected[1])
        self.assertEqual(len(old), len(versions))

    def test_score_path_replace(self):
        handler = _handler(upgrade_mode="largest")
        versions = _versions("old.mkv", 50, score=3)
        self._assert_same(handler, versions, 8, 100, "new.mkv")

    def test_score_path_skip(self):
        handler = _handler(upgrade_mode="largest")
        versions = _versions("old.mkv", 100, score=10)
        self._assert_same(handler, versions, 5, 50, "new.mkv")

    def test_score_path_down_template_kept(self):
        # 旧判重行为：评分降低且旧版符合模板时，原因写模板保留（外层
        # _keep_template_version 改写），不是“评分降低”。
        handler = _handler(
            upgrade_mode="largest", prefer_template_named_version=True
        )
        versions = _versions(
            "正相反的你与我 - S01E01.mkv", 100, score=10
        )
        self._assert_same(handler, versions, 5, 50, "new.mkv")

    def test_score_path_template_kept(self):
        handler = _handler(
            upgrade_mode="largest", prefer_template_named_version=True
        )
        versions = _versions("正相反的你与我 - S01E01.mkv", 100, score=3)
        self._assert_same(handler, versions, 3, 100, "new.mkv")

    def test_quality_path_replace(self):
        handler = _handler(quality_prefer_enabled=True)
        versions = _versions(
            "正相反的你与我 - S01E01.720p.mkv", 600 * MB, score=0
        )
        self._assert_same(
            handler, versions, 0, 1500 * MB,
            "正相反的你与我.S01E01.1080p.mkv",
        )

    def test_quality_path_fake_4k_skip(self):
        handler = _handler(quality_prefer_enabled=True)
        versions = _versions(
            "正相反的你与我 - S01E01.1080p.mkv", 1000 * MB, score=0
        )
        self._assert_same(
            handler, versions, 0, 500 * MB,
            "正相反的你与我.S01E01.2160p.mkv",
        )

    def test_quality_path_template_kept(self):
        handler = _handler(
            quality_prefer_enabled=True, prefer_template_named_version=True
        )
        versions = _versions(
            "正相反的你与我 - S01E01.mkv", 1000 * MB, score=0
        )
        self._assert_same(
            handler, versions, 0, 1000 * MB,
            "正相反的你与我.S01E01.mkv",
        )

    def test_proceed_when_no_existing(self):
        handler = _handler()
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=100,
            existing_versions={},
        )
        self.assertEqual(action, "proceed")
        self.assertEqual(old, [])
        self.assertEqual(reason, "该集无现有版本")

    def test_proceed_when_switch_off(self):
        handler = _handler(episode_dedup_enabled=False)
        versions = _versions("old.mkv", 100, score=10)
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=50,
            existing_versions={1: versions},
        )
        self.assertEqual(action, "proceed")
        self.assertEqual(reason, "判重开关关闭或共存模式")


class CompareQualityDelegationTests(unittest.TestCase):
    """委托关系：体积/分辨率制仍是判定器内部的比较实现。"""

    def test_quality_branch_matches_compare_quality(self):
        quality = sys.modules["cloudsubscribex.utils.quality_compare"]
        cases = [
            ("某剧.S01E01.2160p.mkv", 2 * GB,
            [("某剧.S01E01.1080p.mkv", 1 * GB)]),
            ("某剧.S01E01.2160p.mkv", 500 * MB,
            [("某剧.S01E01.1080p.mkv", 1 * GB)]),
            ("某剧.S01E01.720p.mkv", 4 * GB,
            [("某剧.S01E01.2160p.mkv", 1 * GB)]),
            ("某剧.S01E01.1080p.mkv", 1050 * MB,
            [("某剧.S01E01.1080p.mkv", 1000 * MB)]),
        ]
        for new_name, new_size, old_versions in cases:
            action, reason = quality.compare_quality(
                new_name, new_size, old_versions
            )
            got_action, got_reason = decide_upgrade(
                has_existing=True, new_file_name=new_name,
                new_size=new_size, old_versions=list(old_versions),
                policy=UpgradeDecisionPolicy(quality_prefer=True),
            )
            self.assertEqual(got_action, "replace" if action == "upgrade" else "skip")
            self.assertIn(reason, got_reason)


if __name__ == "__main__":
    unittest.main()
