"""v1.29.0 第 1 步：洗版判定器收敛单测；v1.29.4 统一为一条规则。

覆盖三层：

1. 新判定器本身（``handlers/sync/upgrade_decision.py`` 的
   ``decide_upgrade``，真实实现）：无旧版、评分升/降、评分相等时的
   阈值体积比较（largest/replace 委托 ``compare_quality``、smallest 镜像
   阈值、体积未知不换）、模板保留（含“模板符合不否决已判定的升级”、
   模板开关关闭、模板名为空字符串的旧边角）。v1.29.4 统一规则：评分
   优先（升→换、降→不换），评分相等时走阈值；体积/分辨率优先分支已删，
   评分更低但分辨率更高的候选不再换（``test_score_down_beats_resolution_upgrade``
   锁定）。
2. 委托关系：评分制薄包装 ``_should_upgrade_candidate``、季集判重
   ``_episode_dedup_decision``、评分相等时的 ``compare_quality`` 委托——
   各至少一个用例走原入口，断言动作与原因和直接调判定器逐字一致。
3. 旧行为锁定：原因文案沿用旧措辞（``assertIn`` 锁关键片段，
   ``assertEqual`` 锁模板保留这类整句）；用户 2026-11-20 定下的行为
   变化（largest 下 +5% 增益从换变不换）由
   ``test_equal_largest_small_gain_skips`` 锁定。

判定器取自 ``test_episode_dedup`` 的加载链（该文件加载
``episode_dedup`` / ``rule_scoring`` / ``naming`` 时会连带加载
``upgrade_decision``）；``format_size`` 沿用该文件的测试桩
（``lambda n: f\"{n}B\"``），原因里的体积片段因此是确定性的。
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
    handler._quality_min_gain_percent = overrides.get(
        "quality_min_gain_percent", 10
    )
    handler._quality_min_gain_mb = overrides.get("quality_min_gain_mb", 200)
    return handler


def _policy_for(handler, template_fallback_for_any_skip=False):
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
    """直接判定器：评分分支（评分优先，升换降不换）。"""

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


class DecideUpgradeTieTests(unittest.TestCase):
    """直接判定器：评分相等时的阈值体积比较（v1.29.4 统一）。

    旧评分制“大 1 字节就换”作废：largest 下 +5%（+50MB）增益从换变
    不换（用户 2026-11-20 定下的行为变化）；同分辨率须同时达到 +10%
    且 +200MB 才换；smallest 为镜像阈值（同时小 10% 且小 200MB）。
    """

    def test_equal_largest_small_gain_skips(self):
        # 用户定下的行为变化：+5%（+50MB）增益以前换，现在不换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000 * MB, new_size=1050 * MB,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1000 MB → 1.03 GB，需 ≥ 1.17 GB）",
        )

    def test_equal_largest_tiny_gain_skips(self):
        # 旧规则：1000→1500（+50% 但只 +500B）会换；统一后 200MB 绝对
        # 阈值不过，不换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1000 B → 1.46 KB，需 ≥ 200 MB）",
        )

    def test_equal_largest_not_bigger_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1500, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1.46 KB → 1000 B，需 ≥ 200 MB）",
        )

    def test_equal_largest_same_size_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1000 B → 1000 B，需 ≥ 200 MB）",
        )

    def test_equal_largest_big_gain_replaces(self):
        # +40% 且 +400MB，同时过两个阈值 → 换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000 * MB, new_size=1400 * MB,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "replace")
        self.assertEqual(
            reason,
            "同评分按体积择优：体积 1000 MB → 1.37 GB（+40%）",
        )

    def test_equal_smallest_big_reduction_replaces(self):
        # 镜像阈值：-50% 且 -500MB，同时过两个阈值 → 换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000 * MB, new_size=500 * MB,
            policy=UpgradeDecisionPolicy(mode="smallest"),
        )
        self.assertEqual(action, "replace")
        self.assertEqual(
            reason,
            "同评分按体积择优：体积 1048576000B→524288000B（-50%）",
        )

    def test_equal_smallest_small_reduction_skips(self):
        # 旧规则：1500→1000（-33% 但只 -500B）会换；统一后 200MB 绝对
        # 阈值不过，不换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1500, new_size=1000,
            policy=UpgradeDecisionPolicy(mode="smallest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积缩减不足"
            "（1500B→1000B，需 ≤ 0B）",
        )

    def test_equal_smallest_not_smaller_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="smallest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积缩减不足"
            "（1000B→1500B，需 ≤ 0B）",
        )

    def test_equal_other_mode_uses_threshold(self):
        # 旧规则：mode="replace" 在评分相等时直接不换；统一后与其他模式
        # 一样走阈值（这里 +50% 但 +500B 不过 200MB 阈值 → 不换）。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            policy=UpgradeDecisionPolicy(mode="replace"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1000 B → 1.46 KB，需 ≥ 200 MB）",
        )

    def test_equal_unknown_old_size_skips(self):
        # v1.29.4 行为变化：旧体积/分辨率优先分支在旧体积未知时按“更大”
        # 换；统一后体积未知不换（旧评分制同样要求体积已知才比，且未知
        # 旧体积时按“更大”换会误伤 STRM 等未记录体积的旧版）。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            new_size=500 * MB, old_versions=[],
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且体积未知，保留现有版本")

    def test_equal_unknown_new_size_skips(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000 * MB, new_size=0,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "同评分且体积未知，保留现有版本")


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

    def test_template_kept_when_size_gain_below_threshold(self):
        # +50% 但 +500B 不过 200MB 阈值 → 判为无收益，模板保留生效。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000, new_size=1500,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(
            reason, f"现有版本已符合当前重命名模板，保留 {self._OLD}"
        )

    def test_template_does_not_veto_threshold_size_upgrade(self):
        # +40% 且 +400MB 过阈值 → 已判定升级，模板不否决。
        action, reason = decide_upgrade(
            has_existing=True, old_score=5, new_score=5,
            old_size=1000 * MB, new_size=1400 * MB,
            template_conformant_name=self._OLD,
            policy=UpgradeDecisionPolicy(mode="largest"),
        )
        self.assertEqual(action, "replace")
        self.assertIn("同评分按体积择优", reason)
        self.assertNotIn("已符合当前重命名模板", reason)

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
        self.assertEqual(
            reason,
            "同评分现有版本更优：体积增益不足"
            "（1000 B → 1000 B，需 ≥ 200 MB）",
        )

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


class DecideUpgradeTieQualityTests(unittest.TestCase):
    """直接判定器：评分相等时的分辨率因素（委托 compare_quality）。

    v1.29.4 统一后：评分是第一道门——评分更低但分辨率更高的候选不再
    换；评分相等时才看分辨率/体积（伪 4K、增益不足、分辨率升/降）。
    """

    def test_score_down_beats_resolution_upgrade(self):
        # 统一后的行为变化：旧体积/分辨率优先分支不看评分，2160p 候选
        # 会换；现在评分 99→0 直接不换。
        action, reason = decide_upgrade(
            has_existing=True, old_score=99, new_score=0,
            new_file_name="某剧.S01E01.2160p.mkv", new_size=2 * GB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1 * GB),
        )
        self.assertEqual(action, "skip")
        self.assertEqual(reason, "评分降低 99→0")

    def test_resolution_upgrade_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, old_score=0, new_score=0,
            new_file_name="某剧.S01E01.2160p.mkv", new_size=2 * GB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1 * GB),
        )
        self.assertEqual(action, "replace")
        self.assertIn("同评分按体积择优", reason)
        self.assertIn("1080p → 2160p", reason)

    def test_fake_4k_blocked(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.2160p.mkv",
            new_size=500 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1 * GB),
        )
        self.assertEqual(action, "skip")
        self.assertIn("同评分现有版本更优", reason)
        self.assertIn("疑似伪 4K/片段", reason)

    def test_resolution_downgrade_keeps(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.720p.mkv",
            new_size=4 * GB,
            old_versions=_versions("某剧.S01E01.2160p.mkv", 1 * GB),
        )
        self.assertEqual(action, "skip")
        self.assertIn("同评分现有版本更优", reason)
        self.assertIn("低于现有 2160p", reason)

    def test_same_resolution_small_gain_keeps(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.1080p.mkv",
            new_size=1050 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1000 * MB),
        )
        self.assertEqual(action, "skip")
        self.assertIn("同评分现有版本更优", reason)
        self.assertIn("体积增益不足", reason)

    def test_same_resolution_big_gain_replaces(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.1080p.mkv",
            new_size=1400 * MB,
            old_versions=_versions("某剧.S01E01.1080p.mkv", 1000 * MB),
        )
        self.assertEqual(action, "replace")
        self.assertIn("同评分按体积择优", reason)
        self.assertIn("+40%", reason)

    def test_template_kept_only_without_gain(self):
        action, reason = decide_upgrade(
            has_existing=True, new_file_name="某剧.S01E01.mkv",
            new_size=1000 * MB,
            old_versions=_versions("某剧.S01E01.mkv", 1000 * MB),
            template_conformant_name="某剧.S01E01.mkv",
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
        )
        self.assertEqual(action, "replace")
        self.assertNotIn("已符合当前重命名模板", reason)


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
                handler, template_fallback_for_any_skip=True,
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

    def test_tie_path_replace(self):
        handler = _handler(upgrade_mode="largest")
        versions = _versions(
            "正相反的你与我 - S01E01.720p.mkv", 600 * MB, score=0
        )
        self._assert_same(
            handler, versions, 0, 1500 * MB,
            "正相反的你与我.S01E01.1080p.mkv",
        )

    def test_tie_path_fake_4k_skip(self):
        handler = _handler(upgrade_mode="largest")
        versions = _versions(
            "正相反的你与我 - S01E01.1080p.mkv", 1000 * MB, score=0
        )
        self._assert_same(
            handler, versions, 0, 500 * MB,
            "正相反的你与我.S01E01.2160p.mkv",
        )

    def test_tie_path_template_kept(self):
        handler = _handler(
            upgrade_mode="largest", prefer_template_named_version=True
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
    """委托关系：评分相等时的体积/分辨率比较仍委托 compare_quality。"""

    def test_tie_branch_matches_compare_quality(self):
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
                has_existing=True, old_score=0, new_score=0,
                old_size=old_versions[0][1],
                new_file_name=new_name,
                new_size=new_size, old_versions=list(old_versions),
                policy=UpgradeDecisionPolicy(),
            )
            self.assertEqual(got_action, "replace" if action == "upgrade" else "skip")
            self.assertIn(reason, got_reason)


if __name__ == "__main__":
    unittest.main()
