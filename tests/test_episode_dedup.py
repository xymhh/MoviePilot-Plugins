"""1.6.48 按季集判重替换单测。

固定契约：

组1 判重决策：
  1. 同集已有更优版本 → skip（不写入，历史记「同集已有更优版本」）；
  2. 同集已有更差版本 → replace（删旧写新）；
  3. 无现有版本 → proceed（正常写入）；
  4. 开关关闭 → 全部 proceed（行为与现在一致）；
  5. coexist 模式 → 全部 proceed（共存即有意多版本）。

组2 删除安全：
  6. 无 file_id 的旧版本 → 不删、warning 含 episode_dedup_miss、不阻塞写入；
  7. 有 file_id → 调用 delete_file 并写 info 日志（含路径与旧/新评分）。

组3 数据源：
  8. 目录扫描用 MetaInfo 口径，能解析「- 第 1 集」这类无 SxxExx 老命名；
  9. 两路（目录扫描 + 基线）按 file_id 去重取并集。

组4 电影侧：
  10. tmdbid 目录内任一视频即视为已有版本（不再要求 path.stem == expected_stem）；
  11. 电影多版本：replace 时主旧版本走 upgrade_old_*，其余进 extra 列表。
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_module, ensure_package

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
)
logger_ref = sys.modules["app.log"].logger

# StringUtils 桩：num_filesize 解析数字
sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)

# ---- MetaInfo 桩：支持 SxxExx 与「第 N 集」两种老命名 ----
def _fake_metainfo(name, custom_words=None):
    text = str(name or "").strip()
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
        # 「- 第 1 集」老命名：无 SxxExx 时 begin_season=None（由调用方回退目标季）
        m2 = re.search(r"第\s*(\d{1,4})\s*集", text)
        if m2:
            episode = int(m2.group(1))
    return SimpleNamespace(
        begin_season=season,
        begin_episode=episode,
        episode_list=[episode] if episode else [],
        cn_name=text or None,
    )


register_module("app.core.metainfo", MetaInfo=_fake_metainfo)
register_module("app.schemas", MediaInfo=SimpleNamespace)
register_module(
    "app.schemas.types",
    MediaType=SimpleNamespace(TV="tv", MOVIE="movie"),
)

# ---- cloudsubscribex 包层级 ----
install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.utils", "cloudsubscribex.utils.file_parser",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
)

# OwnerDelegator 桩：与 plugin_env 一致
from plugin_env import OwnerDelegator as _BaseDelegator  # noqa: E402


class _OwnerStub(_BaseDelegator):
    pass


# 对包用 ensure_package（保留 AutoMockModule），对叶子模块用 register_module
_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = _OwnerStub
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core.media").apply_media_identity = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(return_value=[])
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(return_value=("themoviedb", "123"))
_ensure("cloudsubscribex.utils").FileMatcher = SimpleNamespace(episode_from_file=MagicMock(return_value=None))
_ensure("cloudsubscribex.utils").MediaFileParser = SimpleNamespace(
    is_video=lambda name: str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))
)
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.handlers.sync.utils").extract_ed2k_filename = MagicMock()
_ensure("cloudsubscribex.handlers.sync.utils").format_episode_ranges = MagicMock(return_value="")
_ensure("cloudsubscribex.handlers.sync.baseline").normalize_platform_cache_key = lambda key: key

# 加载被测模块
episode_dedup_module = load_module(
    "cloudsubscribex.handlers.sync.episode_dedup",
    "plugins.v2/cloudsubscribex/handlers/sync/episode_dedup.py",
)
EpisodeDedupService = episode_dedup_module.EpisodeDedupService

# 加载 rule_scoring（被 _should_upgrade_candidate 依赖）
rule_scoring_module = load_module(
    "cloudsubscribex.handlers.sync.rule_scoring",
    "plugins.v2/cloudsubscribex/handlers/sync/rule_scoring.py",
)
UpgradeRuleScoringService = rule_scoring_module.UpgradeRuleScoringService

# 加载 naming（被 _find_cloud_movie_file / _platform_target 依赖）
naming_module = load_module(
    "cloudsubscribex.handlers.sync.naming",
    "plugins.v2/cloudsubscribex/handlers/sync/naming.py",
)
SyncNamingService = naming_module.SyncNamingService


class _FakeCloudFile:
    def __init__(self, id, name, size=0, is_directory=False):
        self.id = id
        self.name = name
        self.size = size
        self.is_directory = is_directory


class _FakeListing:
    def __init__(self, files, checked=True):
        self.files = tuple(files)
        self.checked = checked


class _FakeLookup:
    def __init__(self, directory_id="dir1", checked=True):
        self.directory_id = directory_id
        self.checked = checked


class _FakeDirectories:
    def __init__(self, files=None):
        self._files = files or []
        self.resolve_calls = 0
        self.list_calls = 0

    def resolve_directory(self, path, create=False):
        self.resolve_calls += 1
        return _FakeLookup(directory_id="dir1")

    def list_directory(self, directory_id):
        self.list_calls += 1
        return _FakeListing(self._files)


class _FakeMutations:
    def __init__(self, succeed=True):
        self._succeed = succeed
        self.deleted = []

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return self._succeed


def _make_handler(**overrides):
    """构造一个同时具备 Dedup + Scoring + Naming 能力的最小 handler。"""
    handler = EpisodeDedupService.__new__(EpisodeDedupService)
    # _owner 用 SimpleNamespace 承载属性（OwnerDelegator.__setattr__ 转发到 _owner）
    store = SimpleNamespace()
    object.__setattr__(handler, "_owner", store)
    # 手动混入 scoring / naming 能力（与运行期 SyncHandler 组件委托等价）
    handler._should_upgrade_candidate = UpgradeRuleScoringService._should_upgrade_candidate.__get__(handler, EpisodeDedupService)
    handler._get_mp_rule_score = lambda *a, **k: 0
    handler._resource_size_bytes = UpgradeRuleScoringService._resource_size_bytes.__get__(handler, EpisodeDedupService)
    handler._platform_target = SyncNamingService._platform_target.__get__(handler, EpisodeDedupService)
    handler._platform_target_file = SyncNamingService._platform_target_file.__get__(handler, EpisodeDedupService)
    handler._resolve_resource_season_dir = SyncNamingService._resolve_resource_season_dir.__get__(handler, EpisodeDedupService)
    handler._find_cloud_movie_file = SyncNamingService._find_cloud_movie_file.__get__(handler, EpisodeDedupService)
    handler._cloud_drive_name = lambda: "测试网盘"
    handler._upgrade_mode = overrides.get("upgrade_mode", "largest")
    handler._episode_dedup_enabled = overrides.get("episode_dedup_enabled", True)
    # 1.6.50「保留符合重命名格式的版本」：默认关闭（下面这版 _platform_rename_path
    # 桩是恒等的，会让任何文件名都判成「符合模板」），需要时用 overrides 打开并配桩。
    handler._prefer_template_named_version = overrides.get(
        "prefer_template_named_version", False
    )
    handler._version_matches_rename_template = (
        UpgradeRuleScoringService._version_matches_rename_template.__get__(
            handler, EpisodeDedupService
        )
    )
    handler._template_conformant_version = (
        UpgradeRuleScoringService._template_conformant_version.__get__(
            handler, EpisodeDedupService
        )
    )
    handler._CLOUD_MEDIA_ROOT = "/"
    handler._cloud_directories = overrides.get("cloud_directories")
    handler._cloud_mutations = overrides.get("cloud_mutations")
    handler._cloud_transfer_path = "/transfer"
    handler._resource_season_dir_cache = None
    handler._resource_season_dir_lock = None
    handler._build_episode_baseline = overrides.get(
        "build_baseline", lambda *a, **k: {}
    )
    # _platform_rename_path 桩：固定返回 /媒体/剧集/Show/Season 01/name
    handler._platform_rename_path = overrides.get("platform_rename_path") or (
        lambda root_path, subscribe, mediainfo, source_name, season=None, episode=None: (
            SimpleNamespace(
                parent=SimpleNamespace(as_posix=lambda: f"/媒体/剧集/Show/Season {int(season or 1):02d}"),
                name=str(source_name),
            )
        )
    )
    return handler


_SUB = SimpleNamespace(name="正相反的你与我", year=2024, custom_words=None, id=1)
_MEDIA = SimpleNamespace(title_year="正相反的你与我 (2024)", title="正相反的你与我", year=2024, tmdb_id=123)


class EpisodeDedupDecisionTests(unittest.TestCase):
    """组1：判重决策。"""

    def test_skip_when_existing_better(self):
        """1. 同集已有更优版本 → skip。"""
        handler = _make_handler(upgrade_mode="largest")
        existing = {1: [{"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 100, "score": 10}]}
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=50, existing_versions=existing
        )
        self.assertEqual(action, "skip")
        self.assertEqual(len(old), 1)
        self.assertIn("评分降低", reason)

    def test_replace_when_existing_worse(self):
        """2. 同集已有更差版本 → replace。"""
        handler = _make_handler(upgrade_mode="largest")
        existing = {1: [{"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 50, "score": 3}]}
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=8, new_size=100, existing_versions=existing
        )
        self.assertEqual(action, "replace")
        self.assertEqual(old[0]["file_id"], "f1")
        self.assertIn("评分提升", reason)

    def test_proceed_when_no_existing(self):
        """3. 无现有版本 → proceed。"""
        handler = _make_handler()
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=100, existing_versions={}
        )
        self.assertEqual(action, "proceed")
        self.assertEqual(old, [])

    def test_proceed_when_switch_off(self):
        """4. 开关关闭 → 全部 proceed（即使已有更优）。"""
        handler = _make_handler(episode_dedup_enabled=False)
        existing = {1: [{"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 100, "score": 10}]}
        action, _, _ = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=1, new_size=1, existing_versions=existing
        )
        self.assertEqual(action, "proceed")

    def test_proceed_when_coexist(self):
        """5. coexist 模式 → 全部 proceed（共存即有意多版本）。"""
        handler = _make_handler(upgrade_mode="coexist")
        existing = {1: [{"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 100, "score": 10}]}
        action, _, _ = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=1, new_size=1, existing_versions=existing
        )
        self.assertEqual(action, "proceed")

    def test_same_score_largest_requires_bigger(self):
        """同分 + largest：新更大才 replace，否则 skip。"""
        handler = _make_handler(upgrade_mode="largest")
        existing = {1: [{"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 100, "score": 5}]}
        action, _, _ = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=150, existing_versions=existing
        )
        self.assertEqual(action, "replace")
        action2, _, _ = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=5, new_size=80, existing_versions=existing
        )
        self.assertEqual(action2, "skip")


class EpisodeDedupDeleteTests(unittest.TestCase):
    """组2：删除安全。"""

    def test_miss_when_no_file_id(self):
        """6. 无 file_id → 不删、warning 含 episode_dedup_miss、不阻塞。"""
        mutations = _FakeMutations()
        handler = _make_handler(cloud_mutations=mutations)
        old_versions = [
            {"file_id": "", "file_name": "orphan.mkv", "cloud_dir": "/d", "file_size": 10, "score": 3},
        ]
        deleted = handler._apply_episode_dedup(
            old_versions, new_score=8, new_size=100,
            subscribe=_SUB, mediainfo=_MEDIA, season=1, episode=1,
        )
        self.assertEqual(deleted, 0)
        self.assertEqual(mutations.deleted, [])
        # warning 日志应含 episode_dedup_miss
        warnings = [c for c in logger_ref.warning.call_args_list if "episode_dedup_miss" in str(c)]
        self.assertTrue(warnings, "应记录 episode_dedup_miss warning")

    def test_delete_with_file_id(self):
        """7. 有 file_id → 调用 delete_file 并写 info 日志。"""
        mutations = _FakeMutations(succeed=True)
        handler = _make_handler(cloud_mutations=mutations)
        old_versions = [
            {"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 10, "score": 3},
            {"file_id": "f2", "file_name": "old2.mkv", "cloud_dir": "/d", "file_size": 20, "score": 2},
        ]
        deleted = handler._apply_episode_dedup(
            old_versions, new_score=8, new_size=100,
            subscribe=_SUB, mediainfo=_MEDIA, season=1, episode=1,
        )
        self.assertEqual(deleted, 2)
        self.assertEqual(mutations.deleted, ["f1", "f2"])
        infos = [c for c in logger_ref.info.call_args_list if "按季集判重替换" in str(c)]
        self.assertEqual(len(infos), 2)
        self.assertIn("old.mkv", str(infos[0]))

    def test_delete_failure_not_blocking(self):
        """删除失败 → 不抛异常、记 warning。"""
        mutations = _FakeMutations(succeed=False)
        handler = _make_handler(cloud_mutations=mutations)
        old_versions = [
            {"file_id": "f1", "file_name": "old.mkv", "cloud_dir": "/d", "file_size": 10, "score": 3},
        ]
        deleted = handler._apply_episode_dedup(
            old_versions, new_score=8, new_size=100,
            subscribe=_SUB, mediainfo=_MEDIA, season=1, episode=1,
        )
        self.assertEqual(deleted, 0)
        warnings = [c for c in logger_ref.warning.call_args_list if "episode_dedup_miss" in str(c)]
        self.assertTrue(warnings)


class EpisodeDedupDataSourceTests(unittest.TestCase):
    """组3：数据源（目录扫描 + 基线并集）。"""

    def setUp(self):
        # 多测试文件会在 import 期覆盖 app.core.metainfo.MetaInfo，这里重新注入本文件的桩。
        register_module("app.core.metainfo", MetaInfo=_fake_metainfo)

    def test_scan_parses_old_naming(self):
        """8. 目录扫描能解析「- 第 1 集」老命名（无 SxxExx）。"""
        files = [
            _FakeCloudFile(id="f1", name="正相反的你与我 - 第 1 集.mkv", size=100),
            _FakeCloudFile(id="f2", name="正相反的你与我 - S01E02.mkv", size=200),
        ]
        dirs = _FakeDirectories(files=files)
        handler = _make_handler(cloud_directories=dirs)
        result = handler._scan_episode_versions_in_dir(_SUB, _MEDIA, season=1)
        # 「第 1 集」→ E01（begin_season=None 回退目标季 1）
        self.assertIn(1, result, f"应解析出 E01，实际 keys={list(result.keys())}")
        self.assertEqual(result[1][0]["file_id"], "f1")
        # S01E02 → E02
        self.assertIn(2, result)
        self.assertEqual(result[2][0]["file_id"], "f2")

    def test_union_dedup_by_file_id(self):
        """9. 两路按 file_id 去重取并集。"""
        files = [_FakeCloudFile(id="f1", name="Show - S01E01.mkv", size=100)]
        dirs = _FakeDirectories(files=files)

        def fake_baseline(subscribe, mediainfo, season, include_saved=False):
            return {
                1: {"cloud_file_id": "f1", "file_name": "Show - S01E01.mkv", "cloud_dir": "/d", "file_size": 100, "score": 5},
                2: {"cloud_file_id": "f9", "file_name": "Show - old style.mkv", "cloud_dir": "/d", "file_size": 50, "score": 3},
            }

        handler = _make_handler(cloud_directories=dirs, build_baseline=fake_baseline)
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        # E01 只应有一条（file_id=f1 去重）
        self.assertEqual(len(result.get(1, [])), 1)
        # E02 来自基线
        self.assertEqual(len(result.get(2, [])), 1)
        self.assertEqual(result[2][0]["file_id"], "f9")

    def test_scan_skips_other_season(self):
        """扫描时跳过其它季的文件。"""
        files = [
            _FakeCloudFile(id="f1", name="Show - S02E01.mkv", size=100),
            _FakeCloudFile(id="f2", name="Show - S01E01.mkv", size=100),
        ]
        dirs = _FakeDirectories(files=files)
        handler = _make_handler(cloud_directories=dirs)
        result = handler._scan_episode_versions_in_dir(_SUB, _MEDIA, season=1)
        self.assertIn(1, result)
        self.assertEqual(result[1][0]["file_id"], "f2")


class EpisodeDedupScoreTests(unittest.TestCase):
    """组5：目录扫描出的旧版本必须带评分（否则会被无条件替换）。"""

    def setUp(self):
        register_module("app.core.metainfo", MetaInfo=_fake_metainfo)

    @staticmethod
    def _score_by_name(name, filesize, subscribe, season, mediainfo=None):
        """规则评分桩：文件名含 2160p → 20 分，含 1080p → 10 分，其余 0。"""
        text = str(name).lower()
        if "2160p" in text:
            return 20
        if "1080p" in text:
            return 10
        return 0

    def test_scan_entry_carries_score(self):
        """12. 扫描结果必须带 score（不能靠 .get("score") or 0 退化）。"""
        files = [_FakeCloudFile(id="f1", name="Show - S01E01.2160p.mkv", size=900)]
        handler = _make_handler(cloud_directories=_FakeDirectories(files=files))
        handler._get_mp_rule_score = self._score_by_name
        result = handler._scan_episode_versions_in_dir(_SUB, _MEDIA, season=1)
        self.assertEqual(result[1][0]["score"], 20)

    def test_better_existing_version_is_not_replaced(self):
        """13. 已有 2160p 旧版本 + 新 1080p 候选 → skip（不得删掉更好的旧文件）。"""
        files = [_FakeCloudFile(id="f1", name="Show - S01E01.2160p.mkv", size=900)]
        handler = _make_handler(cloud_directories=_FakeDirectories(files=files))
        handler._get_mp_rule_score = self._score_by_name
        versions = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=10, new_size=500, existing_versions=versions
        )
        self.assertEqual(action, "skip", f"不该替换更优旧版本（reason={reason}）")
        self.assertEqual(old[0]["file_id"], "f1")

    def test_worse_existing_version_is_replaced(self):
        """14. 已有 1080p 旧版本 + 新 2160p 候选 → replace。"""
        files = [_FakeCloudFile(id="f1", name="Show - S01E01.1080p.mkv", size=500)]
        handler = _make_handler(cloud_directories=_FakeDirectories(files=files))
        handler._get_mp_rule_score = self._score_by_name
        versions = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        action, old, _ = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=20, new_size=900, existing_versions=versions
        )
        self.assertEqual(action, "replace")
        self.assertEqual([v["file_id"] for v in old], ["f1"])

    def test_extra_clips_ignored(self):
        """15. 样片/预告不计入「已有版本」，避免误判导致电影/剧集不再转存。"""
        files = [
            _FakeCloudFile(id="s1", name="Show - S01E01-sample.mkv", size=10),
            _FakeCloudFile(id="s2", name="Show - S01E01 预告.mkv", size=10),
        ]
        handler = _make_handler(cloud_directories=_FakeDirectories(files=files))
        handler._get_mp_rule_score = self._score_by_name
        self.assertEqual(handler._scan_episode_versions_in_dir(_SUB, _MEDIA, season=1), {})
        # 电影侧同样忽略
        self.assertEqual(handler._find_cloud_movie_versions(_SUB, _MEDIA), [])

    def test_movie_label_without_season(self):
        """16. 电影删除日志不含 S00E01 这类伪季集标识。"""
        mutations = _FakeMutations(succeed=True)
        handler = _make_handler(cloud_mutations=mutations)
        deleted = handler._apply_episode_dedup(
            [{"file_id": "m1", "file_name": "old.mkv", "cloud_dir": "/电影", "file_size": 10, "score": 3}],
            new_score=8, new_size=100, subscribe=_SUB, mediainfo=_MEDIA, season=0, episode=1,
        )
        self.assertEqual(deleted, 1)
        infos = [str(c) for c in logger_ref.info.call_args_list if "按季集判重替换" in str(c)]
        self.assertTrue(infos)
        self.assertNotIn("S00E01", infos[-1])
        self.assertIn("正相反的你与我 (2024)", infos[-1])


class EpisodeDedupSkipStatusContractTests(unittest.TestCase):
    """组6：「跳过」状态必须在各状态白名单里，否则历史红标且无法删除。"""

    @staticmethod
    def _read(relative_path):
        with open(os.path.join(project_root, relative_path), encoding="utf-8") as handle:
            return handle.read()

    def test_baseline_excludes_skipped_records(self):
        """17. 跳过记录不得进入洗版基线（否则被当成已有的幻影版本）。"""
        source = self._read("plugins.v2/cloudsubscribex/handlers/sync/baseline.py")
        match = re.search(r"status.*?in \{([^}]*)\}", source)
        self.assertIsNotNone(match, "未找到 baseline 的状态排除集合")
        self.assertIn("跳过", match.group(1))

    def test_backend_terminal_status_includes_skipped(self):
        """18. 后端可删除判定与合并终态保护都认「跳过」。"""
        source = self._read("plugins.v2/cloudsubscribex/handlers/sync/history.py")
        self.assertIn('"跳过"', source)
        # _history_record_deletable 与 prepare_history_group 的 failed_count 不得把跳过算成失败
        self.assertIn('{"成功", "处理中", "下载中", "跳过"}', source)

    def test_frontend_supports_skipped_status(self):
        """19. 前端状态筛选/配色/可删除判定都认「跳过」。"""
        table = self._read("frontend/cloudsubscribex/src/components/dashboard/HistoryTable.vue")
        self.assertIn('"处理中", "下载中", "成功", "失败", "跳过"', table)
        self.assertIn('status === "跳过"', table)
        self.assertIn('["成功", "失败", "跳过"]', table)
        app_page = self._read("frontend/cloudsubscribex/src/components/AppPage.vue")
        self.assertIn('["成功", "失败", "跳过"]', app_page)


class MovieDedupTests(unittest.TestCase):
    """组4：电影侧。"""

    def _movie_handler(self, files=None, **overrides):
        dirs = _FakeDirectories(files=files or [])
        handler = _make_handler(cloud_directories=dirs, **overrides)
        return handler

    def test_find_movie_any_video(self):
        """10. tmdbid 目录内任一视频即视为已有版本（不再要求 stem 匹配）。"""
        files = [_FakeCloudFile(id="m1", name="完全不同的名字.mkv", size=500)]
        handler = self._movie_handler(files=files)
        result = handler._find_cloud_movie_versions(_SUB, _MEDIA)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["file_name"], "完全不同的名字.mkv")
        self.assertEqual(result[0]["file_id"], "m1")

    def test_movie_replace_sets_primary_and_extra(self):
        """11. 电影多版本：replace 时主旧版本 + extra 列表。"""
        files = [
            _FakeCloudFile(id="m1", name="v1.mkv", size=100),
            _FakeCloudFile(id="m2", name="v2.mkv", size=200),
            _FakeCloudFile(id="m3", name="v3.mkv", size=300),
        ]
        handler = self._movie_handler(files=files)
        versions = handler._find_cloud_movie_versions(_SUB, _MEDIA)
        self.assertEqual(len(versions), 3)
        action, old, reason = handler._movie_dedup_decision(
            _SUB, _MEDIA, new_score=10, new_size=500, existing_versions=versions
        )
        self.assertEqual(action, "replace")
        self.assertEqual(len(old), 3)

    def test_movie_skip_when_existing_better(self):
        """电影：已有更优 → skip。"""
        files = [_FakeCloudFile(id="m1", name="best.mkv", size=1000)]
        handler = self._movie_handler(files=files)
        versions = handler._find_cloud_movie_versions(_SUB, _MEDIA)
        # 注入高分旧版本
        versions[0]["score"] = 20
        action, _, reason = handler._movie_dedup_decision(
            _SUB, _MEDIA, new_score=5, new_size=100, existing_versions=versions
        )
        self.assertEqual(action, "skip")

    def test_movie_proceed_when_switch_off(self):
        """电影：开关关闭 → proceed。"""
        files = [_FakeCloudFile(id="m1", name="old.mkv", size=100)]
        handler = self._movie_handler(files=files, episode_dedup_enabled=False)
        versions = handler._find_cloud_movie_versions(_SUB, _MEDIA)
        versions[0]["score"] = 20
        action, _, _ = handler._movie_dedup_decision(
            _SUB, _MEDIA, new_score=1, new_size=1, existing_versions=versions
        )
        self.assertEqual(action, "proceed")


class FindCloudMovieFileTests(unittest.TestCase):
    """_find_cloud_movie_file 兼容性：优先精确 stem，回退任一视频。"""

    def _handler(self, files):
        dirs = _FakeDirectories(files=files)
        handler = _make_handler(cloud_directories=dirs)
        return handler

    def test_prefers_exact_stem(self):
        """精确 stem 匹配优先返回。"""
        files = [
            _FakeCloudFile(id="m1", name="other.mkv", size=100),
            _FakeCloudFile(id="m2", name="正相反的你与我.mkv", size=200),
        ]
        handler = self._handler(files)
        result = handler._find_cloud_movie_file(_SUB, _MEDIA)
        self.assertIsNotNone(result)
        self.assertEqual(result[1], "正相反的你与我.mkv")

    def test_fallback_any_video(self):
        """无精确匹配时回退任一视频（不再返回 None）。"""
        files = [_FakeCloudFile(id="m1", name="完全不同的名字.mkv", size=100)]
        handler = self._handler(files)
        result = handler._find_cloud_movie_file(_SUB, _MEDIA)
        self.assertIsNotNone(result, "目录内有视频时不应返回 None")
        self.assertEqual(result[1], "完全不同的名字.mkv")

    def test_none_when_no_video(self):
        """目录无视频 → None。"""
        files = [_FakeCloudFile(id="m1", name="notes.txt", size=100)]
        handler = self._handler(files)
        result = handler._find_cloud_movie_file(_SUB, _MEDIA)
        self.assertIsNone(result)


class TemplateNamedVersionTests(unittest.TestCase):
    """组7（1.6.50）：现有版本已符合当前重命名模板 → 保留，不被评分更高的候选替换。"""

    @staticmethod
    def _template_aware_rename(root_path, subscribe, mediainfo, source_name, season=None, episode=None):
        """桩：只有已按模板命名的名字（剧集 SxxExx / 电影 title_year）渲染后才等于自身。"""
        suffix = os.path.splitext(str(source_name))[1]
        if episode:
            canonical = f"{subscribe.name} - S{int(season or 1):02d}E{int(episode):02d}{suffix}"
        else:
            canonical = f"{mediainfo.title_year}{suffix}"
        return SimpleNamespace(
            parent=SimpleNamespace(as_posix=lambda: "/媒体/剧集/Show/Season 01"),
            name=canonical,
        )

    def _handler(self, **overrides):
        overrides.setdefault("platform_rename_path", self._template_aware_rename)
        return _make_handler(**overrides)

    def test_keep_when_existing_matches_template(self):
        """20. 现有版本名 == 模板渲染结果 → skip，即使候选评分更高。"""
        handler = self._handler(prefer_template_named_version=True)
        existing = {1: [{
            "file_id": "f1",
            "file_name": "正相反的你与我 - S01E01.mkv",
            "cloud_dir": "/d",
            "file_size": 100,
            "score": 3,
        }]}
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=99, new_size=9999, existing_versions=existing
        )
        self.assertEqual(action, "skip")
        self.assertEqual(len(old), 1)
        self.assertIn("已符合当前重命名模板", reason)
        self.assertIn("S01E01", reason)

    def test_replace_when_existing_not_template_named(self):
        """21. 现有版本是老命名（渲染结果不同）→ 仍按评分 replace。"""
        handler = self._handler(prefer_template_named_version=True)
        existing = {1: [{
            "file_id": "f1",
            "file_name": "正相反的你与我.第1集.mkv",
            "cloud_dir": "/d",
            "file_size": 50,
            "score": 3,
        }]}
        action, old, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=8, new_size=100, existing_versions=existing
        )
        self.assertEqual(action, "replace")
        self.assertNotIn("已符合当前重命名模板", reason)

    def test_gate_off_falls_back_to_score(self):
        """22. 开关关闭 → 即使名字已符合模板也按评分替换（= 1.6.49 行为）。"""
        handler = self._handler(prefer_template_named_version=False)
        existing = {1: [{
            "file_id": "f1",
            "file_name": "正相反的你与我 - S01E01.mkv",
            "cloud_dir": "/d",
            "file_size": 50,
            "score": 3,
        }]}
        action, _, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=8, new_size=100, existing_versions=existing
        )
        self.assertEqual(action, "replace")
        self.assertIn("评分提升", reason)

    def test_render_failure_falls_back_to_score(self):
        """23. 模板渲染异常 → 不阻断，回退评分判定。"""

        def _boom(*args, **kwargs):
            raise RuntimeError("宿主接口不可用")

        handler = self._handler(
            prefer_template_named_version=True, platform_rename_path=_boom
        )
        existing = {1: [{
            "file_id": "f1",
            "file_name": "正相反的你与我 - S01E01.mkv",
            "cloud_dir": "/d",
            "file_size": 50,
            "score": 3,
        }]}
        action, _, reason = handler._episode_dedup_decision(
            _SUB, _MEDIA, 1, 1, new_score=8, new_size=100, existing_versions=existing
        )
        self.assertEqual(action, "replace")
        self.assertIn("评分提升", reason)

    def test_movie_keep_when_matches_template(self):
        """24. 电影侧同规则：现有网盘电影名已符合模板 → skip。"""
        files = [_FakeCloudFile(id="m1", name="正相反的你与我 (2024).mkv", size=100)]
        dirs = _FakeDirectories(files=files)
        handler = self._handler(
            prefer_template_named_version=True, cloud_directories=dirs
        )
        versions = handler._find_cloud_movie_versions(_SUB, _MEDIA)
        versions[0]["score"] = 3
        action, _, reason = handler._movie_dedup_decision(
            _SUB, _MEDIA, new_score=99, new_size=9999, existing_versions=versions
        )
        self.assertEqual(action, "skip")
        self.assertIn("已符合当前重命名模板", reason)

    def test_should_upgrade_candidate_accepts_existing_name(self):
        """25. _should_upgrade_candidate 传现有文件名即命中模板门槛。"""
        handler = self._handler(prefer_template_named_version=True)
        should_upgrade, reason = handler._should_upgrade_candidate(
            3,
            99,
            100,
            9999,
            old_file_name="正相反的你与我 - S01E01.mkv",
            subscribe=_SUB,
            mediainfo=_MEDIA,
            season=1,
            episode=1,
        )
        self.assertFalse(should_upgrade)
        self.assertIn("已符合当前重命名模板", reason)
        # 未传文件名时行为不变
        should_upgrade2, reason2 = handler._should_upgrade_candidate(3, 99, 100, 9999)
        self.assertTrue(should_upgrade2)
        self.assertIn("评分提升", reason2)

    def test_config_and_frontend_contract(self):
        """26. 开关在默认配置、运行期装配与前端表单里都存在。"""
        with open(
            os.path.join(project_root, "plugins.v2/cloudsubscribex/core/config.py"),
            encoding="utf-8",
        ) as handle:
            self.assertIn('"prefer_template_named_version": True', handle.read())
        with open(
            os.path.join(project_root, "plugins.v2/cloudsubscribex/handlers/sync/service.py"),
            encoding="utf-8",
        ) as handle:
            service_source = handle.read()
        self.assertIn("_prefer_template_named_version", service_source)
        with open(
            os.path.join(project_root, "plugins.v2/cloudsubscribex/__init__.py"),
            encoding="utf-8",
        ) as handle:
            plugin_source = handle.read()
        self.assertIn("_prefer_template_named_version", plugin_source)
        with open(
            os.path.join(
                project_root,
                "frontend/cloudsubscribex/src/config/fields/upgrade.js",
            ),
            encoding="utf-8",
        ) as handle:
            self.assertIn("prefer_template_named_version", handle.read())


class _UncheckedDirectories(_FakeDirectories):
    """列目录失败（checked=False）：判重不得据此判定「该集不存在」。"""

    def list_directory(self, directory_id):
        self.list_calls += 1
        return _FakeListing([], checked=False)


def _history_baseline(episode, **item):
    return {episode: {"source": "MoviePilot整理记录", **item}}


class EpisodeDedupHistoryTrustTests(unittest.TestCase):
    """组8（1.6.51）：判重只信「目标目录里有实物」的历史版本。

    背景：跨盘转存偶发「记录成功但文件没落进季目录」，历史里的幻影版本会让该集
    被永久跳过，缺失集再也补不上（真机案例：季目录只有 E04，E01–E03 被幻影挡住）。
    """

    def setUp(self):
        register_module("app.core.metainfo", MetaInfo=_fake_metainfo)
        logger_ref.reset_mock()

    @staticmethod
    def _dirs(entries):
        return _FakeDirectories(files=[
            _FakeCloudFile(id=file_id, name=name, size=size)
            for file_id, name, size in entries
        ])

    def test_phantom_history_entry_dropped(self):
        """27. 无 cloud_dir 的 MP 整理历史幻影版本 → 不参与判重并记 info 日志。"""
        baseline = _history_baseline(
            1, file_name="01..mp4", file_size=100, score=10,
        )
        handler = _make_handler(
            cloud_directories=self._dirs([("f4", "Show - S01E04.mp4", 400)]),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertNotIn(1, result, "幻影版本不得挡住缺失集补齐")
        self.assertEqual(result[4][0]["file_id"], "f4")
        logged = " ".join(str(call) for call in logger_ref.info.call_args_list)
        self.assertIn("判重忽略历史记录", logged)
        self.assertIn("S01E01", logged)

    def test_history_entry_with_cloud_dir_kept(self):
        """28. 记录带落盘目录且不是本季目录（我们没扫）→ 维持原口径，不越权判定。"""
        baseline = _history_baseline(
            2, file_name="Show - 别处.mkv", file_size=100, score=10,
            cloud_dir="/媒体/其它库/Show/Season 01",
        )
        handler = _make_handler(
            cloud_directories=self._dirs([("f4", "Show - S01E04.mp4", 400)]),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertIn(2, result)
        self.assertEqual(result[2][0]["file_id"], "")
        logger_ref.info.assert_not_called()

    def test_media_server_entry_kept_without_cloud_dir(self):
        """29. 媒体服务器来源（库里真有该集）不参与「目标目录实物」校验。"""
        baseline = {2: {"file_name": "Show - S01E02.mkv", "source": "媒体服务器", "score": 10}}
        handler = _make_handler(
            cloud_directories=self._dirs([("f4", "Show - S01E04.mp4", 400)]),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertIn(2, result)

    def test_unchecked_listing_keeps_history_entries(self):
        """30. 列目录失败（拿不到实测数据）→ 保留历史条目，避免误判成缺失后重复转存。"""
        baseline = _history_baseline(1, file_name="01..mp4", file_size=100, score=10)
        handler = _make_handler(
            cloud_directories=_UncheckedDirectories(),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertIn(1, result)

    def test_unparsable_file_in_dir_protects_entry(self):
        """31. 目录里确有同名文件（解析不出季集）→ 保留条目，不重复转存。"""
        baseline = _history_baseline(1, file_name="01..mp4", file_size=100, score=10)
        handler = _make_handler(
            cloud_directories=self._dirs([("f1", "01..mp4", 100)]),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertIn(1, result)

    def test_scan_hit_keeps_episode(self):
        """32. 目录里真有该集 → 照旧参与判重（不因新校验漏判）。"""
        baseline = _history_baseline(1, file_name="01..mp4", file_size=100, score=1)
        handler = _make_handler(
            cloud_directories=self._dirs([("f1", "Show - S01E01.mkv", 100)]),
            build_baseline=lambda *a, **k: baseline,
        )
        result = handler._collect_episode_versions(_SUB, _MEDIA, season=1)
        self.assertIn(1, result)
        self.assertTrue(any(entry["source"] == "目标目录扫描" for entry in result[1]))

    def test_baseline_entry_carries_origin(self):
        """33. 基线条目必须带来源标记，否则无法区分「历史」与「媒体服务器」。"""
        handler = _make_handler(
            cloud_directories=self._dirs([]),
            build_baseline=lambda *a, **k: {
                1: {"file_name": "Show - S01E01.mkv", "source": "插件转存记录",
                    "cloud_file_id": "f1", "cloud_dir": "/媒体/剧集/Show/Season 01"},
            },
        )
        result = handler._baseline_episode_versions(_SUB, _MEDIA, season=1)
        self.assertEqual(result[1][0]["origin"], "插件转存记录")
        self.assertEqual(result[1][0]["source"], "插件基线")


class SkipMergeDoesNotPoisonHistoryTests(unittest.TestCase):
    """组9（1.6.51）：跳过记录不得污染成功记录的落盘信息，也不得写进 MP 整理历史。"""

    @staticmethod
    def _read(relative_path):
        with open(os.path.join(project_root, relative_path), encoding="utf-8") as handle:
            return handle.read()

    def test_skip_merge_keeps_landing_fields(self):
        """34. 跳过合并进成功记录时保留 cloud_dir / 文件名，防止再造幻影版本。"""
        source = self._read("plugins.v2/cloudsubscribex/handlers/sync/history.py")
        self.assertIn("skip_onto_success", source)
        block = source.split("skip_onto_success = (", 1)[1]
        for key in ("cloud_dir", "cloud_file_id", "file_size", "file_name"):
            self.assertIn(f'"{key}"', block.split("history[index] = merged", 1)[0])
        # 假成功不得进入平台整理历史
        self.assertIn("if not skip_onto_success:", source)

    def test_cross_success_repair_path_exists(self):
        """35. 手动保底链路：查无落盘的跨盘成功记录可人工重新转存。"""
        source = self._read("plugins.v2/cloudsubscribex/handlers/sync/history.py")
        self.assertIn("_cross_success_needs_repair", source)
        self.assertIn('return True, "重新转存此记录"', source)


if __name__ == "__main__":
    unittest.main()
