"""v1.13.1 单源基线奇偶校验（Phase 2 设计 §5.3）。

旧基线（四路全开：MP 整理历史 + 插件 history + 手动基线 + 媒体服务器）与
新基线（去掉插件 history 源）对同一组输入，在 `_collect_episode_versions`
层比较每集选出的最佳版本。

- 场景 1–6：旧新输出必须字节级相等（五字段：file_name、file_size、source、
  score、origin——设计 §5.3 只列前四字段；基线版本的 source 统一为“插件基线”，
  原始来源在 origin 里，不加 origin 无法区分 MP 历史与插件 history 来源）。
- 场景 7：只有插件来源的高分版本——较弱标准：新最佳版本必须 ① 在实扫
  结果里（有云盘实物背书）② 评分不低于旧基线落选版本的评分。

冻结副本：`_frozen_plugin_history_episode_files` 与
`_old_build_episode_baseline` 是 v1.13.0 旧基线实现的逐字副本，仅供本文件
的奇偶校验使用，生产代码已删除（baseline.py 的插件 history 源已删）。
本文件不往共享模块上挂任何东西，冻结函数只活在本文件自己的模块全局量里。

设计与实际不符处（已核实，见场景 2 注释）：设计 §5.3 场景 2 说
landed=False 的记录“在 _plugin_history_episode_files 里就把它丢掉”，
实际代码里 `_cross_success_needs_repair` 对 landed=False 返回 True，
所以该记录会通过插件 history 函数，之后在
`_baseline_entry_backed_by_storage` 一层因季目录里找不到实物才被丢弃。
场景 2 的结论（旧新输出相等、该集无最佳版本）不变，变的只是丢弃的位置。
"""
from __future__ import annotations

import os
import re
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock

from plugin_env import (
    OwnerDelegator,
    ensure_package,
    install_app_mocks,
    load_module,
    register_module,
    set_media_type,
)

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
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
)

logger = sys.modules["app.log"].logger
set_media_type(movie="电影", tv="电视剧")

sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)
register_module("app.schemas", MediaInfo=SimpleNamespace)
register_module(
    "app.schemas.types",
    MediaType=SimpleNamespace(TV="tv", MOVIE="movie"),
)


def _fake_metainfo(name, custom_words=None):
    text = str(name or "").strip()
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
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

install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
)

_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = OwnerDelegator
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core").CloudDriveCapability = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(
    return_value=("themoviedb", "123")
)
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.media").download_history_identity_payload = MagicMock(
    return_value={}
)
_ensure("cloudsubscribex.core.media").media_server_tmdb_filters = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.history").history_group_key = MagicMock(
    return_value=""
)
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
_ensure("cloudsubscribex.utils.cache").create_platform_ttl_cache = MagicMock()
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"


class _StubMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))


_ensure("cloudsubscribex.utils.file_parser").MediaFileParser = _StubMediaFileParser

# 加载被测模块（与 test_episode_dedup / test_landed_verify 同款加载方式）
episode_dedup_module = load_module(
    "cloudsubscribex.handlers.sync.episode_dedup",
    "plugins.v2/cloudsubscribex/handlers/sync/episode_dedup.py",
)
EpisodeDedupService = episode_dedup_module.EpisodeDedupService

baseline_module = load_module(
    "cloudsubscribex.handlers.sync.baseline",
    "plugins.v2/cloudsubscribex/handlers/sync/baseline.py",
)
UpgradeBaselineService = baseline_module.UpgradeBaselineService

rule_scoring_module = load_module(
    "cloudsubscribex.handlers.sync.rule_scoring",
    "plugins.v2/cloudsubscribex/handlers/sync/rule_scoring.py",
)
UpgradeRuleScoringService = rule_scoring_module.UpgradeRuleScoringService

naming_module = load_module(
    "cloudsubscribex.handlers.sync.naming",
    "plugins.v2/cloudsubscribex/handlers/sync/naming.py",
)
SyncNamingService = naming_module.SyncNamingService

retry_index_module = load_module(
    "cloudsubscribex.core.retry_index",
    "plugins.v2/cloudsubscribex/core/retry_index.py",
)

# 冻结副本引用的基线模块全局量（本文件内绑定，不动共享模块）
normalize_platform_cache_key = baseline_module.normalize_platform_cache_key
tmdb_id_of = baseline_module.tmdb_id_of


# ----------------------------------------------------------------------
# 冻结副本：v1.13.0 的旧基线实现（逐字，仅把方法名换成 _frozen/_old 前缀）
# ----------------------------------------------------------------------
def _frozen_plugin_history_episode_files(self, subscribe, season: int) -> Dict[int, List[Dict[str, Any]]]:
    """冻结副本：v1.13.0 的 `_plugin_history_episode_files`，仅供奇偶校验。"""
    records = self._get_data("history") or []
    cache_key = self._baseline_key(subscribe, season)
    cached = self._baseline_plugin_cache.get(cache_key)
    if cached is not None:
        return self._copy_episode_items(cached)
    tmdb = tmdb_id_of(subscribe)
    title = getattr(subscribe, "name", "") or ""
    result: Dict[int, List[Dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("tmdb_id") not in (None, tmdb) or record.get("title") != title:
            continue
        if record.get("type") != "tv":
            continue
        try:
            record_season = int(record.get("season") or 1)
        except (TypeError, ValueError):
            continue
        if record_season != season:
            continue
        if record.get("status") == "失败" or record.get("status") == "处理中":
            continue
        if record.get("landed") is False and not self._cross_success_needs_repair(record):
            continue
        episode = record.get("episode")
        if episode is None:
            continue
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        name = record.get("source_file_name") or record.get("file_name") or ""
        if not name:
            continue
        item = {
            "file_name": name,
            "file_size": self._int_or_zero(record.get("file_size")),
            "source": "插件转存记录",
            "cloud_dir": record.get("cloud_dir"),
            "cloud_file_id": record.get("cloud_file_id"),
        }
        result.setdefault(episode_number, []).append(item)
    self._baseline_plugin_cache.set(cache_key, self._copy_episode_items(result))
    return result


def _old_build_episode_baseline(self, subscribe, mediainfo, season, include_saved=True):
    """冻结副本：v1.13.0 的 `_build_episode_baseline`（四路全开），仅供奇偶校验。"""
    candidates = self._transfer_history_episode_files(subscribe, season)
    for episode, items in self._plugin_history_episode_files(subscribe, season).items():
        candidates.setdefault(episode, []).extend(items)
    manual_baseline = getattr(subscribe, "_manual_media_baseline", {}) or {}
    for episode, item in (manual_baseline.get("episodes") or {}).items():
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            candidates.setdefault(episode_number, []).append(dict(item))
    media_server_key = normalize_platform_cache_key(
        self._baseline_key(subscribe, season)
    )
    with self._baseline_cache_lock:
        media_server_items = self._baseline_media_server_cache.get(media_server_key)
    if media_server_items is None:
        _, media_server_items = self._media_server_resolver.episode_media(
            chain=self._chain, mediainfo=mediainfo, season=season
        )
        with self._baseline_cache_lock:
            self._baseline_media_server_cache.set(
                media_server_key, media_server_items or {}
            )
    for episode, media_item in (media_server_items or {}).items():
        if not isinstance(media_item, dict):
            continue
        media_file = Path(str(media_item.get("path") or ""))
        file_size = self._int_or_zero(media_item.get("size"))
        if not file_size and media_file.suffix.lower() != ".strm":
            try:
                file_size = media_file.stat().st_size
            except OSError:
                logger.debug(
                    "媒体服务器基线：无法读取 %s 的体积，记为 0",
                    media_file,
                )
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        candidates.setdefault(episode_number, []).append(
            {
                "file_name": media_file.name,
                "rule_title": str(media_item.get("rule_title") or media_file.name),
                "file_size": file_size,
                "source": "媒体服务器",
            }
        )
    baseline: Dict[int, Dict[str, Any]] = {}
    score_cache: Dict[tuple, int] = {}
    for episode, items in candidates.items():
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        if episode_number <= 0:
            continue
        best = None
        for item in items:
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file_name") or "")
            if not file_name:
                continue
            file_size = self._int_or_zero(item.get("file_size"))
            rule_title = str(item.get("rule_title") or file_name)
            cache_key = (rule_title, file_size)
            if cache_key not in score_cache:
                score_cache[cache_key] = self._get_mp_rule_score(
                    rule_title, file_size, subscribe, season, mediainfo
                )
            score = score_cache[cache_key]
            if best is None or (score, file_size) > (best["score"], best["file_size"]):
                best = {
                    "file_name": file_name,
                    "rule_title": rule_title,
                    "file_size": file_size,
                    "score": score,
                    "source": str(item.get("source") or ""),
                }
        if best:
            baseline[episode_number] = best
    if include_saved:
        saved = self._read_ep_priority(subscribe, season)
        for episode, item in saved.items():
            if not isinstance(item, dict):
                continue
            file_name = str(item.get("file_name") or "")
            if not file_name:
                continue
            episode_number = int(episode)
            file_size = self._int_or_zero(item.get("file_size"))
            rule_title = str(item.get("rule_title") or file_name)
            score = self._get_mp_rule_score(
                rule_title, file_size, subscribe, season, mediainfo
            )
            baseline[episode_number] = {
                "file_name": file_name,
                "rule_title": rule_title,
                "file_size": file_size,
                "score": score,
                "source": str(item.get("source") or ""),
            }
    return baseline


# ----------------------------------------------------------------------
# 桩与 harness
# ----------------------------------------------------------------------
class _FakeCloudFile:
    def __init__(self, file_id, name, size=0, is_directory=False):
        self.id = file_id
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

    def resolve_directory(self, path, create=False):
        return _FakeLookup(directory_id="dir1")

    def list_directory(self, directory_id):
        return _FakeListing(self._files)


class _FakeMutations:
    def __init__(self, succeed=True):
        self._succeed = succeed
        self.deleted = []

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return self._succeed


class _FakeTtlCache:
    def __init__(self):
        self._store = {}

    def get(self, key):
        return self._store.get(key)

    def set(self, key, value):
        self._store[key] = value


# _platform_rename_path 桩固定返回的季目录（与 test_episode_dedup 一致）
SEASON_DIR = "/媒体/剧集/Show/Season 01"


def _sub(manual=None):
    sub = SimpleNamespace(name="Show", year=2024, custom_words=None, id=1, tmdb_id=123)
    sub._manual_media_baseline = manual or {}
    return sub


_MEDIA = SimpleNamespace(
    title_year="Show (2024)", title="Show", year=2024, tmdb_id=123,
    type=SimpleNamespace(value="电视剧"),
)


def _plugin_record(file_name, size, episode=1, status="成功", landed=None,
                    cloud_dir=SEASON_DIR, source_file_name=None):
    record = {
        "tmdb_id": 123, "title": "Show", "type": "tv", "season": 1,
        "episode": episode, "status": status,
        "file_name": file_name,
        "source_file_name": source_file_name if source_file_name is not None else file_name,
        "file_size": size, "cloud_dir": cloud_dir, "cloud_file_id": None,
    }
    if landed is not None:
        record["landed"] = landed
    return record


def _make_parity_handler(*, old, scan_files, mp_candidates, plugin_history,
                        media_items, scores, manual=None):
    """构造旧/新两路的 handler；除基线实现与 `_HISTORY_BASELINE_SOURCES` 外完全相同。"""
    handler = EpisodeDedupService.__new__(EpisodeDedupService)
    store = SimpleNamespace()
    object.__setattr__(handler, "_owner", store)
    handler._should_upgrade_candidate = UpgradeRuleScoringService._should_upgrade_candidate.__get__(handler, EpisodeDedupService)
    # 评分按文件名查表（固定分），避免真实评分规则干扰奇偶比较
    handler._get_mp_rule_score = lambda rule_title, *a, **k: int(scores.get(str(rule_title or ""), 0))
    handler._resource_size_bytes = UpgradeRuleScoringService._resource_size_bytes.__get__(handler, EpisodeDedupService)
    handler._platform_target = SyncNamingService._platform_target.__get__(handler, EpisodeDedupService)
    handler._platform_target_file = SyncNamingService._platform_target_file.__get__(handler, EpisodeDedupService)
    handler._resolve_resource_season_dir = SyncNamingService._resolve_resource_season_dir.__get__(handler, EpisodeDedupService)
    handler._find_cloud_movie_file = SyncNamingService._find_cloud_movie_file.__get__(handler, EpisodeDedupService)
    handler._cloud_drive_name = lambda: "测试网盘"
    handler._upgrade_mode = "largest"
    handler._episode_dedup_enabled = True
    handler._prefer_template_named_version = False
    handler._version_matches_rename_template = UpgradeRuleScoringService._version_matches_rename_template.__get__(handler, EpisodeDedupService)
    handler._template_conformant_version = UpgradeRuleScoringService._template_conformant_version.__get__(handler, EpisodeDedupService)
    handler._CLOUD_MEDIA_ROOT = "/"
    handler._cloud_directories = _FakeDirectories(files=list(scan_files))
    handler._cloud_mutations = _FakeMutations()
    handler._cloud_transfer_path = "/transfer"
    handler._resource_season_dir_cache = None
    handler._resource_season_dir_lock = None
    handler._platform_rename_path = lambda root_path, subscribe, mediainfo, source_name, season=None, episode=None: (
        SimpleNamespace(
            parent=SimpleNamespace(as_posix=lambda: f"/媒体/剧集/Show/Season {int(season or 1):02d}"),
            name=str(source_name),
        )
    )
    # 基线层
    handler._baseline_cache_lock = threading.RLock()
    handler._baseline_transfer_cache = _FakeTtlCache()
    handler._baseline_media_server_cache = _FakeTtlCache()
    handler._baseline_key = UpgradeBaselineService._baseline_key
    handler._int_or_zero = UpgradeBaselineService._int_or_zero
    handler._copy_episode_items = UpgradeBaselineService._copy_episode_items
    handler._read_ep_priority = lambda *a, **k: {}
    handler._chain = None
    handler._media_server_resolver = SimpleNamespace(
        episode_media=lambda **kwargs: (None, dict(media_items or {}))
    )
    # MP 历史桩：返回预设候选（不走真实解析）
    handler._transfer_history_episode_files = lambda subscribe, season: {
        int(ep): [dict(item) for item in items]
        for ep, items in (mp_candidates or {}).items()
    }
    # 插件 history（旧基线才读）
    handler._data = {"history": [dict(record) for record in (plugin_history or [])]}
    handler._get_data = lambda key: handler._data.get(key)
    if old:
        handler._baseline_plugin_cache = _FakeTtlCache()
        # 注意：_HISTORY_BASELINE_SOURCES 是 EpisodeDedupService 的类属性，
        # 普通赋值会进 _owner 而被类属性遮蔽（__getattr__ 不触发），必须用
        # object.__setattr__ 写进实例 __dict__ 才生效。
        object.__setattr__(
            handler, "_HISTORY_BASELINE_SOURCES",
            frozenset({"MoviePilot整理记录", "插件转存记录"}),
        )
        handler._plugin_history_episode_files = _frozen_plugin_history_episode_files.__get__(handler, EpisodeDedupService)
        handler._cross_success_needs_repair = retry_index_module.cross_success_needs_repair
        handler._build_episode_baseline = _old_build_episode_baseline.__get__(handler, EpisodeDedupService)
    else:
        handler._build_episode_baseline = UpgradeBaselineService._build_episode_baseline.__get__(handler, EpisodeDedupService)
    return handler


def _best_per_episode(collected):
    """每集最佳版本（score, size）——与基线一致的 max 语义，平局取先加入者。"""
    best = {}
    for episode, versions in collected.items():
        if not versions:
            continue
        best[int(episode)] = max(
            versions,
            key=lambda item: (int(item.get("score") or 0), int(item.get("file_size") or 0)),
        )
    return best


def _best_fields(best):
    return {
        episode: {
            "file_name": item.get("file_name"),
            "file_size": int(item.get("file_size") or 0),
            "source": item.get("source"),
            "score": int(item.get("score") or 0),
            "origin": item.get("origin"),
        }
        for episode, item in best.items()
    }


def _run_both(*, scan_files=(), mp_candidates=None, plugin_history=None,
                media_items=None, scores=None, manual=None):
    """同一组输入跑旧/新两路，返回（旧最佳，新最佳，旧全量，新全量）。"""
    subscribe = _sub(manual=manual)
    old = _make_parity_handler(
        old=True, scan_files=scan_files, mp_candidates=mp_candidates,
        plugin_history=plugin_history, media_items=media_items,
        scores=scores or {}, manual=manual,
    )
    new = _make_parity_handler(
        old=False, scan_files=scan_files, mp_candidates=mp_candidates,
        plugin_history=plugin_history, media_items=media_items,
        scores=scores or {}, manual=manual,
    )
    old_collected = old._collect_episode_versions(subscribe, _MEDIA, 1)
    new_collected = new._collect_episode_versions(subscribe, _MEDIA, 1)
    return (
        _best_fields(_best_per_episode(old_collected)),
        _best_fields(_best_per_episode(new_collected)),
        old_collected,
        new_collected,
    )


class BaselineSingleSourceTests(unittest.TestCase):
    """Phase 2 设计 §5.3：单源基线奇偶校验（7 个场景）。"""

    def setUp(self):
        # _dedup_meta_info 在调用时才懒导入 app.core.metainfo.MetaInfo；
        # 后收集的文件（如 test_bangumi_episode_total）会把该模块换成
        # MagicMock，导致季目录扫描不到任何一集——每测重装一次本文件的桩。
        register_module("app.core.metainfo", MetaInfo=_fake_metainfo)

    def test_phantom_plugin_record_discarded_by_both(self):
        """场景 1：幻影记录——插件 history 有一集，实扫与 MP 历史里都没有。"""
        old_best, new_best, old_collected, _ = _run_both(
            plugin_history=[_plugin_record("Show - S01E01.mkv", 1000)],
        )
        self.assertEqual(old_best, {})
        self.assertEqual(new_best, {})
        self.assertNotIn(1, old_collected)

    def test_landed_false_plugin_record_discarded_by_both(self):
        """场景 2：landed is False 的插件记录——旧基线不选它，新基线没有它。

        注意：该记录并非在 _plugin_history_episode_files 里被丢掉——
        _cross_success_needs_repair 对 landed=False 返回 True，所以它会通过
        插件 history 函数；真正丢弃它的是 _baseline_entry_backed_by_storage
        （季目录里找不到实物）。设计 §5.3 的机制描述不准确，结论不变。
        """
        old_best, new_best, old_collected, _ = _run_both(
            plugin_history=[_plugin_record("Show - S01E01.mkv", 1000, landed=False)],
        )
        self.assertEqual(old_best, {})
        self.assertEqual(new_best, {})
        self.assertNotIn(1, old_collected)

    def test_three_way_agreement(self):
        """场景 3：三方一致——同一集在插件 history、MP 历史、实扫里同名同大小。"""
        old_best, new_best, _, _ = _run_both(
            scan_files=[_FakeCloudFile("f1", "Show - S01E01.mkv", size=1000)],
            mp_candidates={1: [{"file_name": "Show - S01E01.mkv", "file_size": 1000, "source": "MoviePilot整理记录"}]},
            plugin_history=[_plugin_record("Show - S01E01.mkv", 1000)],
            scores={"Show - S01E01.mkv": 10},
        )
        expected = {1: {"file_name": "Show - S01E01.mkv", "file_size": 1000, "source": "目标目录扫描", "score": 10, "origin": None}}
        self.assertEqual(old_best, expected)
        self.assertEqual(new_best, expected)

    def test_plugin_only_file_found_by_scan_under_other_spelling(self):
        """场景 4：仅插件 history 有、但文件真实存在于季目录（另一种文件名拼写）。"""
        old_best, new_best, _, _ = _run_both(
            scan_files=[_FakeCloudFile("f1", "Show S01E01.mkv", size=1000)],
            plugin_history=[_plugin_record("Show - S01E01.mkv", 1000)],
            scores={"Show S01E01.mkv": 10, "Show - S01E01.mkv": 10},
        )
        # 文件名以实扫为准
        expected = {1: {"file_name": "Show S01E01.mkv", "file_size": 1000, "source": "目标目录扫描", "score": 10, "origin": None}}
        self.assertEqual(old_best, expected)
        self.assertEqual(new_best, expected)

    def test_mp_history_only(self):
        """场景 5：仅 MP 历史有（回归项）。

        注意：实扫里必须有同集文件，否则实物校验会把 MP 版本也丢掉（季目录
        里找不到实物）；MP 候选靠更高评分胜出，证明 MP 来源仍在参与基线。
        """
        old_best, new_best, _, _ = _run_both(
            scan_files=[_FakeCloudFile("f1", "Show - S01E01-720p.mkv", size=2000)],
            mp_candidates={1: [{"file_name": "Show - S01E01.mkv", "file_size": 1000, "source": "MoviePilot整理记录"}]},
            scores={"Show - S01E01.mkv": 90, "Show - S01E01-720p.mkv": 10},
        )
        expected = {1: {"file_name": "Show - S01E01.mkv", "file_size": 1000, "source": "插件基线", "score": 90, "origin": "MoviePilot整理记录"}}
        self.assertEqual(old_best, expected)
        self.assertEqual(new_best, expected)

    def test_manual_baseline_and_media_server_only(self):
        """场景 6：仅手动基线 / 媒体服务器有。"""
        old_best, new_best, _, _ = _run_both(
            manual={"episodes": {1: {"file_name": "Show - S01E01.mkv", "file_size": 1000}}},
            media_items={2: {"path": "/media/Show - S01E02.mkv", "size": 2000}},
            scores={"Show - S01E01.mkv": 10, "Show - S01E02.mkv": 10},
        )
        expected = {
            1: {"file_name": "Show - S01E01.mkv", "file_size": 1000, "source": "插件基线", "score": 10, "origin": ""},
            2: {"file_name": "Show - S01E02.mkv", "file_size": 2000, "source": "插件基线", "score": 10, "origin": "媒体服务器"},
        }
        self.assertEqual(old_best, expected)
        self.assertEqual(new_best, expected)

    def test_plugin_only_high_score_version_weaker_standard(self):
        """场景 7：只有插件来源的高分版本——较弱标准。"""
        old_best, new_best, old_collected, _ = _run_both(
            scan_files=[_FakeCloudFile("f1", "Show - S01E01-1080p.mkv", size=2000)],
            plugin_history=[_plugin_record("Show - S01E01.mkv", 500)],
            scores={"Show - S01E01.mkv": 90, "Show - S01E01-1080p.mkv": 10},
        )
        # 旧基线选插件高分版
        self.assertEqual(
            old_best,
            {1: {"file_name": "Show - S01E01.mkv", "file_size": 500, "source": "插件基线", "score": 90, "origin": "插件转存记录"}},
        )
        # 新基线：① 最佳版本在实扫结果里；② 评分不低于旧基线落选版本
        new_ep1 = new_best[1]
        self.assertEqual(new_ep1["source"], "目标目录扫描")
        self.assertEqual(new_ep1["file_name"], "Show - S01E01-1080p.mkv")
        old_rejected = max(
            (
                item for item in old_collected[1]
                if item.get("file_name") != old_best[1]["file_name"]
            ),
            key=lambda item: (int(item.get("score") or 0), int(item.get("file_size") or 0)),
            default=None,
        )
        self.assertIsNotNone(old_rejected, "旧基线应有落选版本可供比较")
        self.assertGreaterEqual(new_ep1["score"], int(old_rejected.get("score") or 0))


if __name__ == "__main__":
    unittest.main()
