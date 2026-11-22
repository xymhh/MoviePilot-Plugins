"""v1.31.0 重复扫描（同集多份 .strm）。

覆盖：
- .strm 身份解析（redirect pickcode / 分享 id / 未知）；
- 分组归一（三种文件名同一集、电影同组、去标签口径与 naming 一致）；
- pickcode 同/不同判定与留大建议（同视频组与不同视频组）；
- 删除映射（file_id 解析不到拒绝删除、pickcode 核对不上拒绝删除）；
- 根因回归：真实 _generate_strm 同一集两次生成只剩一份 .strm；
- API 层三端点转发到 SyncHandler；v1.31.2 状态保存/读取两个端点同口径。

harness：真实 StrmGenerator + 真实 _generate_strm（复用
test_strm_pickcode_refresh_1_17_0 的假 playback/query 做法）；
集号解析用确定性假 MetaInfo（按 episode_parse 的生产正则解析
SxxExx / 第 N 集），只在需要分组的用例里临时替换
``sys.modules["app.core.metainfo"].MetaInfo``，用完恢复。
"""

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote

from plugin_env import (  # noqa: E402
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.schemas", "app.schemas.types", "app.utils", "app.utils.http",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.utils", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
)
install_core_delegation()

_utils = ensure_package("cloudsubscribex.utils")
_strm = load_module(
    "cloudsubscribex.utils.strm",
    "plugins.v2/cloudsubscribex/utils/strm.py",
)
_utils.StrmTemplateError = _strm.StrmTemplateError
_utils.plan_share_strm_files = _strm.plan_share_strm_files
_utils.rebuild_strm_tree = MagicMock()
_utils.rebuild_strm_by_export_tree = MagicMock()
load_module(
    "cloudsubscribex.utils.legacy_strm",
    "plugins.v2/cloudsubscribex/utils/legacy_strm.py",
)
# CloudFile 只出现在 strm_ops 的注解里，给一个真类型即可。
ensure_package("cloudsubscribex.core").CloudFile = type("CloudFile", (), {})

episode_parse = load_module(
    "cloudsubscribex.core.episode_parse",
    "plugins.v2/cloudsubscribex/core/episode_parse.py",
)
EPISODE_TAG_PATTERN = episode_parse.EPISODE_TAG_PATTERN
CN_EPISODE_PATTERN = episode_parse.CN_EPISODE_PATTERN

naming_module = load_module(
    "cloudsubscribex.handlers.sync.naming",
    "plugins.v2/cloudsubscribex/handlers/sync/naming.py",
)

duplicates_module = load_module(
    "cloudsubscribex.handlers.sync.strm_duplicates",
    "plugins.v2/cloudsubscribex/handlers/sync/strm_duplicates.py",
)
StrmDuplicatesService = duplicates_module.StrmDuplicatesService
parse_strm_identity = duplicates_module.parse_strm_identity
normalize_strm_stem = duplicates_module.normalize_strm_stem
season_from_dirname = duplicates_module.season_from_dirname
strm_group_key = duplicates_module.strm_group_key
nfo_path_for_strm = duplicates_module.nfo_path_for_strm
group_strm_files = duplicates_module.group_strm_files
cleanup_sibling_strms = duplicates_module.cleanup_sibling_strms

_strm_ops = load_module(
    "cloudsubscribex.handlers.sync.strm_ops",
    "plugins.v2/cloudsubscribex/handlers/sync/strm_ops.py",
)
StrmOpsMixin = _strm_ops.StrmOpsMixin
StrmGenerator = _strm.StrmGenerator

api_module = load_module(
    "cloudsubscribex.core.api.strm_duplicates",
    "plugins.v2/cloudsubscribex/core/api/strm_duplicates.py",
)
StrmDuplicatesApi = api_module.StrmDuplicatesApi

BASE_URL = "http://127.0.0.1:9527"
REDIRECT = BASE_URL + "/api/v1/plugin/CloudSubscribeX/redirect"


def _redirect_content(pickcode, video_name):
    return (
        f"{REDIRECT}?pickcode={pickcode}"
        f"&file_name={quote(video_name, safe='')}"
    )


def _share_content(share_id, video_name):
    return (
        f"{REDIRECT}?share_code=SC&receive_code=RC&id={share_id}"
        f"&file_name={quote(video_name, safe='')}"
    )


class _FakeMetaInfo:
    """确定性 MetaInfo 替身：按生产正则解析 SxxExx / 第 N 集。"""

    def __init__(self, text, custom_words=None):
        text = str(text or "")
        match = EPISODE_TAG_PATTERN.search(text)
        if match:
            self.begin_season = int(match.group(1))
            self.episode_list = [int(match.group(2))]
            self.begin_episode = int(match.group(2))
            return
        cn_match = CN_EPISODE_PATTERN.search(text)
        if cn_match:
            self.begin_season = None
            self.episode_list = []
            self.begin_episode = int(cn_match.group(1))
            return
        self.begin_season = None
        self.episode_list = []
        self.begin_episode = None


_MISSING = object()


class _MetaInfoPatch:
    """临时替换 app.core.metainfo.MetaInfo，用完恢复（不污染后续用例）。"""

    def __init__(self):
        self.module = sys.modules["app.core.metainfo"]
        self.saved = self.module.__dict__.get("MetaInfo", _MISSING)

    def __enter__(self):
        self.module.MetaInfo = _FakeMetaInfo
        return self

    def __exit__(self, *exc):
        if self.saved is _MISSING:
            self.module.__dict__.pop("MetaInfo", None)
        else:
            self.module.MetaInfo = self.saved
        return False


class _MetaInfoTestCase(unittest.TestCase):
    """需要分组的用例：整个类期间用假 MetaInfo，结束恢复。"""

    def setUp(self):
        self._patch = _MetaInfoPatch()
        self._patch.__enter__()
        self.addCleanup(self._patch.__exit__, None, None, None)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)


class _FakeCloudFile:
    def __init__(self, file_id, name, size, pickcode):
        self.id = file_id
        self.name = name
        self.size = size
        self.pickcode = pickcode


class _FakeCloudQuery:
    def __init__(self, files):
        self._files = dict(files or {})
        self.find_calls = []
        self.find_strict_calls = []

    def find_file(self, cloud_dir, file_name):
        self.find_calls.append((cloud_dir, file_name))
        return self._files.get((cloud_dir, file_name))

    def find_file_strict(self, cloud_dir, file_name):
        self.find_strict_calls.append((cloud_dir, file_name))
        return self._files.get((cloud_dir, file_name))


class _FakeMutations:
    def __init__(self, succeed=True):
        self.deleted = []
        self._succeed = succeed

    def delete_file(self, file_id):
        self.deleted.append(file_id)
        return self._succeed


class _FakePlaybackReference:
    template_variables = frozenset({"pickcode"})

    @staticmethod
    def reference_values(file_item):
        return {"pickcode": getattr(file_item, "pickcode", "")}


def _duplicates_service(root, query=None, mutations=None, media_root=None, store=None):
    owner = SimpleNamespace(
        _local_resource_path=str(root),
        _cloud_query=query,
        _cloud_mutations=mutations,
        _playback_reference=_FakePlaybackReference(),
    )
    if media_root is not None:
        owner._CLOUD_MEDIA_ROOT = media_root
    if store is not None:
        # 经宿主 _plugin 取不到时，服务回落到宿主直挂的 _get_data_store。
        owner._get_data_store = lambda: store
    return StrmDuplicatesService(owner)


def _entry(path, content):
    path = Path(path)
    return {
        "path": path,
        "content": content,
        "size": len(content.encode("utf-8")),
        "relative": path.name,
    }


class ParseStrmIdentityTests(unittest.TestCase):
    def test_redirect_form(self):
        identity = parse_strm_identity(
            _redirect_content("ay9zcfn2vwfguhhcu", "两京十五日 - S01E06 - 第6集.mkv")
        )
        self.assertEqual(identity["pickcode"], "ay9zcfn2vwfguhhcu")
        self.assertEqual(identity["file_name"], "两京十五日 - S01E06 - 第6集.mkv")
        self.assertEqual(identity["kind"], "redirect")

    def test_share_form(self):
        identity = parse_strm_identity(
            _share_content("987654321", "两京十五日 - S01E06 - 第6集.mkv")
        )
        self.assertEqual(identity["pickcode"], "")
        self.assertEqual(identity["share_id"], "987654321")
        self.assertEqual(identity["file_name"], "两京十五日 - S01E06 - 第6集.mkv")
        self.assertEqual(identity["kind"], "share")

    def test_unknown_content(self):
        self.assertEqual(parse_strm_identity("")["kind"], "unknown")
        self.assertEqual(parse_strm_identity("not a url")["kind"], "unknown")
        self.assertEqual(
            parse_strm_identity(f"{REDIRECT}?file_name=x.mkv")["kind"], "unknown"
        )


class GroupingTests(_MetaInfoTestCase):
    def test_three_filename_forms_are_one_episode(self):
        parent = self.root / "Season 1"
        keys = {
            strm_group_key(parent / "两京十五日 - S01E06 - 第6集.strm"),
            strm_group_key(parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm"),
            strm_group_key(parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.mp4.strm"),
        }
        self.assertEqual(len(keys), 1)
        parent_posix, title, season, episode = keys.pop()
        self.assertEqual(title, "两京十五日")
        self.assertEqual((season, episode), (1, 6))

    def test_different_episodes_are_different_keys(self):
        parent = self.root / "Season 1"
        key5 = strm_group_key(parent / "两京十五日 - S01E05 - 第5集.strm")
        key6 = strm_group_key(parent / "两京十五日 - S01E06 - 第6集.strm")
        self.assertNotEqual(key5, key6)

    def test_movie_forms_are_one_group(self):
        parent = self.root / "斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}"
        keys = {
            strm_group_key(parent / "斗破苍穹剧场版 双帝之战 (2026).strm"),
            strm_group_key(parent / "斗破苍穹剧场版 双帝之战 (2026) {tmdb-1599184}.strm"),
        }
        self.assertEqual(len(keys), 1)
        _, title, season, episode = keys.pop()
        self.assertEqual(title, "斗破苍穹剧场版 双帝之战 (2026)")
        self.assertIsNone(season)
        self.assertIsNone(episode)

    def test_season_from_parent_dir_when_stem_has_none(self):
        parent = self.root / "Season 2"
        key = strm_group_key(parent / "两京十五日 - 第6集.strm")
        self.assertEqual(key[2], 2)
        self.assertEqual(key[3], 6)

    def test_tag_stripping_follows_naming_regex(self):
        """去标签与 naming._TMDB_TAG_RE 同口径（本模块直接复用该正则）。"""
        self.assertEqual(
            normalize_strm_stem("两京十五日 {tmdb-203483} - S01E06 - 第6集"),
            "两京十五日 - S01E06 - 第6集",
        )
        self.assertEqual(
            normalize_strm_stem("凡人修仙传 [tmdbid-106449] - S01E200 - 第200集"),
            "凡人修仙传 - S01E200 - 第200集",
        )
        # 正则只认短横线形；{tmdbid=...}（MP 目录形态）不在口径内，原样保留。
        self.assertEqual(
            normalize_strm_stem("斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}"),
            "斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}",
        )

    def test_season_from_dirname(self):
        self.assertEqual(season_from_dirname("Season 1"), 1)
        self.assertEqual(season_from_dirname("Season 01"), 1)
        self.assertIsNone(season_from_dirname("两京十五日"))
        self.assertIsNone(season_from_dirname(""))

    def test_nfo_path_for_strm(self):
        self.assertEqual(
            nfo_path_for_strm(self.root / "两京十五日 - S01E06 - 第6集.mp4.strm"),
            self.root / "两京十五日 - S01E06 - 第6集.nfo",
        )
        self.assertEqual(
            nfo_path_for_strm(self.root / "两京十五日 - S01E06 - 第6集.strm"),
            self.root / "两京十五日 - S01E06 - 第6集.nfo",
        )


class GroupStrmFilesTests(_MetaInfoTestCase):
    def _tv_entries(self):
        parent = self.root / "Season 1"
        return [
            _entry(
                parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
                _redirect_content("pickcode-aaa", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
            ),
            _entry(
                parent / "两京十五日 - S01E06 - 第6集.strm",
                _redirect_content("pickcode-bbb", "两京十五日 - S01E06 - 第6集.mkv"),
            ),
        ]

    def test_same_pickcode_is_same_video(self):
        parent = self.root / "Season 1"
        entries = [
            _entry(
                parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
                _redirect_content("same-pickcode", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
            ),
            _entry(
                parent / "两京十五日 - S01E06 - 第6集.strm",
                _redirect_content("same-pickcode", "两京十五日 - S01E06 - 第6集.mkv"),
            ),
        ]
        groups = group_strm_files(entries)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "same-video")
        self.assertEqual(groups[0]["suggested_keep"], 0)
        # 同视频组默认保留带标记的规范名（排在第一位）。
        self.assertIn("{tmdb-203483}", groups[0]["items"][0]["file_name"])

    def test_different_pickcode_is_different_video(self):
        groups = group_strm_files(self._tv_entries())
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["kind"], "different-video")
        self.assertIsNone(groups[0]["suggested_keep"])
        self.assertEqual(groups[0]["season_episode"], "S01E06")

    def test_share_ids_decide_kind(self):
        parent = self.root / "分享"
        same = group_strm_files([
            _entry(parent / "剧名 - S01E01 - 第1集.strm", _share_content("111", "剧名 - S01E01 - 第1集.mkv")),
            _entry(parent / "剧名 {tmdb-1} - S01E01 - 第1集.strm", _share_content("111", "剧名 - S01E01 - 第1集.mkv")),
        ])
        self.assertEqual(same[0]["kind"], "same-video")
        different = group_strm_files([
            _entry(parent / "剧名 - S01E01 - 第1集.strm", _share_content("111", "剧名 - S01E01 - 第1集.mkv")),
            _entry(parent / "剧名 {tmdb-1} - S01E01 - 第1集.strm", _share_content("222", "剧名 - S01E01 - 第1集.mkv")),
        ])
        self.assertEqual(different[0]["kind"], "different-video")

    def test_unknown_kind_when_no_identity(self):
        parent = self.root / "Season 1"
        groups = group_strm_files([
            _entry(parent / "两京十五日 - S01E06 - 第6集.strm", "garbage"),
            _entry(parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm", "garbage"),
        ])
        self.assertEqual(groups[0]["kind"], "unknown")
        self.assertIsNone(groups[0]["suggested_keep"])

    def test_singletons_are_dropped(self):
        parent = self.root / "Season 1"
        groups = group_strm_files([
            _entry(
                parent / "两京十五日 - S01E01 - 第1集.strm",
                _redirect_content("only-one", "两京十五日 - S01E01 - 第1集.mkv"),
            ),
        ])
        self.assertEqual(groups, [])


def _write_strm(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class ScanServiceTests(_MetaInfoTestCase):
    def _build_library(self):
        season_dir = (
            self.root / "115网盘" / "剧集" / "国产剧" / "两京十五日" / "Season 1"
        )
        _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E05 - 第5集.strm",
            _redirect_content("pick-e05-tagged", "两京十五日 {tmdb-203483} - S01E05 - 第5集.mkv"),
        )
        _write_strm(
            season_dir / "两京十五日 - S01E05 - 第5集.strm",
            _redirect_content("pick-e05-plain", "两京十五日 - S01E05 - 第5集.mkv"),
        )
        _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content("pick-e06-tagged", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
        )
        _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.mp4.strm",
            _redirect_content("pick-e06-mp4", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mp4"),
        )
        _write_strm(
            season_dir / "两京十五日 - S01E01 - 第1集.strm",
            _redirect_content("pick-e01", "两京十五日 - S01E01 - 第1集.mkv"),
        )
        movie_dir = (
            self.root / "115网盘" / "电影" / "动画电影"
            / "斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}"
        )
        _write_strm(
            movie_dir / "斗破苍穹剧场版 双帝之战 (2026).strm",
            _redirect_content("pick-movie-a", "斗破苍穹剧场版 双帝之战 (2026) 4K.mp4"),
        )
        _write_strm(
            movie_dir / "斗破苍穹剧场版 双帝之战 (2026) {tmdb-1599184}.strm",
            _redirect_content("pick-movie-b", "斗破苍穹剧场版 双帝之战 (2026) 4K AAC.mkv"),
        )
        # 非 .strm 文件不参与扫描。
        (season_dir / "两京十五日 - S01E06 - 第6集.nfo").write_text("<nfo/>", encoding="utf-8")
        (season_dir / "poster.jpg").write_bytes(b"\x00\x01")
        return season_dir, movie_dir

    def test_scan_finds_three_groups(self):
        self._build_library()
        service = _duplicates_service(self.root)
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        data = result["data"]
        self.assertEqual(data["dirs_scanned"], 9)
        self.assertEqual(data["strm_count"], 7)
        self.assertEqual(len(data["groups"]), 3)
        by_title = {group["title"]: group for group in data["groups"]}
        e05 = by_title["两京十五日"]
        self.assertEqual(
            [group["episode"] for group in data["groups"] if group["title"] == "两京十五日"],
            [5, 6],
        )
        self.assertEqual(e05["kind"], "different-video")
        movie = by_title["斗破苍穹剧场版 双帝之战 (2026)"]
        self.assertEqual(movie["media_type"], "movie")
        self.assertEqual(movie["kind"], "different-video")
        self.assertTrue(all(group["sizes_pending"] for group in data["groups"]))

    def test_scan_without_root_fails(self):
        service = _duplicates_service(self.root / "不存在")
        result = service.duplicates_scan()
        self.assertFalse(result["success"])

    def test_scan_zero_cloud_calls(self):
        """扫描只读本地文件：query 为 None 也能跑完（不碰网盘）。"""
        self._build_library()
        service = _duplicates_service(self.root, query=None)
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["data"]["groups"]), 3)


class EnrichServiceTests(_MetaInfoTestCase):
    def _groups(self):
        season_dir, _ = ScanServiceTests._build_library(self)
        service = _duplicates_service(self.root)
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        return result["data"]["groups"]

    def _query_for(self):
        files = {
            ("/115网盘/剧集/国产剧/两京十五日/Season 1", "两京十五日 {tmdb-203483} - S01E05 - 第5集.mkv"):
                _FakeCloudFile("fid-e05-tagged", "x", 1_290_000_000, "pick-e05-tagged"),
            ("/115网盘/剧集/国产剧/两京十五日/Season 1", "两京十五日 - S01E05 - 第5集.mkv"):
                _FakeCloudFile("fid-e05-plain", "x", 1_180_000_000, "pick-e05-plain"),
            ("/115网盘/剧集/国产剧/两京十五日/Season 1", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-e06-tagged", "x", 1_360_000_000, "pick-e06-tagged"),
            ("/115网盘/剧集/国产剧/两京十五日/Season 1", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mp4"):
                _FakeCloudFile("fid-e06-mp4", "x", 1_950_000_000, "pick-e06-mp4"),
            ("/115网盘/电影/动画电影/斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}", "斗破苍穹剧场版 双帝之战 (2026) 4K.mp4"):
                _FakeCloudFile("fid-movie-a", "x", 6_860_000_000, "pick-movie-a"),
            ("/115网盘/电影/动画电影/斗破苍穹剧场版 双帝之战 (2026) {tmdbid=1599184}", "斗破苍穹剧场版 双帝之战 (2026) 4K AAC.mkv"):
                _FakeCloudFile("fid-movie-b", "x", 6_680_000_000, "pick-movie-b"),
        }
        return _FakeCloudQuery(files)

    def test_enrich_suggests_largest(self):
        groups = self._groups()
        query = self._query_for()
        service = _duplicates_service(self.root, query=query)
        result = service.duplicates_enrich(groups)
        self.assertTrue(result["success"], result)
        enriched = result["data"]["groups"]
        by_key = {}
        for group in enriched:
            by_key[(group["title"], group["episode"])] = group
        e05 = by_key[("两京十五日", 5)]
        # E05 带标签份 1.29GB > 没标签份 1.18GB：留带标签份（items[0]）。
        self.assertEqual(e05["suggested_keep"], 0)
        self.assertEqual(e05["items"][0]["video_size"], 1_290_000_000)
        e06 = by_key[("两京十五日", 6)]
        # E06 .mp4 份 1.95GB > 带标签份 1.36GB：留 .mp4 份。
        self.assertEqual(e06["items"][e06["suggested_keep"]]["video_size"], 1_950_000_000)
        movie = by_key[("斗破苍穹剧场版 双帝之战 (2026)", None)]
        self.assertEqual(movie["items"][movie["suggested_keep"]]["video_size"], 6_860_000_000)
        self.assertFalse(any(group["sizes_pending"] for group in enriched))

    def test_enrich_same_video_keeps_tagged(self):
        parent = self.root / "Season 1"
        _write_strm(
            parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content("same-pc", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
        )
        _write_strm(
            parent / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content("same-pc", "两京十五日 - S01E06 - 第6集.mkv"),
        )
        service = _duplicates_service(self.root)
        groups = service.duplicates_scan()["data"]["groups"]
        query = _FakeCloudQuery({
            ("/Season 1", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-a", "x", 1_000, "same-pc"),
            ("/Season 1", "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-b", "x", 1_000, "same-pc"),
        })
        service = _duplicates_service(self.root, query=query)
        enriched = service.duplicates_enrich(groups)["data"]["groups"]
        self.assertEqual(enriched[0]["kind"], "same-video")
        self.assertEqual(enriched[0]["suggested_keep"], 0)
        self.assertIn("{tmdb-203483}", enriched[0]["items"][0]["file_name"])

    def test_enrich_all_sizes_unknown_gives_no_suggestion(self):
        groups = self._groups()
        service = _duplicates_service(self.root, query=_FakeCloudQuery({}))
        enriched = service.duplicates_enrich(groups)["data"]["groups"]
        for group in enriched:
            if group["kind"] == "different-video":
                self.assertIsNone(group["suggested_keep"])
                self.assertIsNone(group["items"][0]["video_size"])

    def test_enrich_skips_share_items(self):
        parent = self.root / "115网盘分享"
        _write_strm(
            parent / "剧名 - S01E01 - 第1集.strm",
            _share_content("111", "剧名 - S01E01 - 第1集.mkv"),
        )
        _write_strm(
            parent / "剧名 {tmdb-1} - S01E01 - 第1集.strm",
            _share_content("222", "剧名 - S01E01 - 第1集.mkv"),
        )
        service = _duplicates_service(self.root)
        groups = service.duplicates_scan()["data"]["groups"]
        query = _FakeCloudQuery({})
        service = _duplicates_service(self.root, query=query)
        enriched = service.duplicates_enrich(groups)["data"]["groups"]
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["kind"], "different-video")
        self.assertEqual(query.find_calls, [])


class DeleteServiceTests(_MetaInfoTestCase):
    def _setup_e06(self):
        season_dir = (
            self.root / "115网盘" / "剧集" / "国产剧" / "两京十五日" / "Season 1"
        )
        tagged = _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content("pick-e06-tagged", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
        )
        plain = _write_strm(
            season_dir / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content("pick-e06-plain", "两京十五日 - S01E06 - 第6集.mkv"),
        )
        (season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.nfo").write_text(
            "<nfo/>", encoding="utf-8",
        )
        cloud_dir = "/115网盘/剧集/国产剧/两京十五日/Season 1"
        query = _FakeCloudQuery({
            (cloud_dir, "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-tagged", "x", 1_360_000_000, "pick-e06-tagged"),
            (cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-plain", "x", 1_950_000_000, "pick-e06-plain"),
        })
        service = _duplicates_service(self.root)
        groups = service.duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        service = _duplicates_service(self.root, query=query)
        enriched = service.duplicates_enrich([group])["data"]["groups"][0]
        # 留大：没标签份 1.95GB > 带标签份 1.36GB，保留 items[1]。
        self.assertEqual(enriched["suggested_keep"], 1)
        return season_dir, tagged, plain, enriched, query

    def test_delete_different_video_removes_video_strm_nfo(self):
        season_dir, tagged, plain, group, query = self._setup_e06()
        mutations = _FakeMutations()
        service = _duplicates_service(self.root, query=query, mutations=mutations)
        result = service.duplicates_delete(group, group["suggested_keep"])
        self.assertTrue(result["success"], result)
        self.assertEqual(mutations.deleted, ["fid-tagged"])
        self.assertFalse(tagged.exists())
        self.assertFalse(
            (season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.nfo").exists()
        )
        self.assertTrue(plain.exists())

    def test_delete_same_video_keeps_cloud_file(self):
        parent = self.root / "Season 1"
        _write_strm(
            parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content("same-pc", "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"),
        )
        _write_strm(
            parent / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content("same-pc", "两京十五日 - S01E06 - 第6集.mkv"),
        )
        (parent / "两京十五日 - S01E06 - 第6集.nfo").write_text("<nfo/>", encoding="utf-8")
        service = _duplicates_service(self.root)
        groups = service.duplicates_scan()["data"]["groups"]
        mutations = _FakeMutations()
        service = _duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=mutations,
        )
        result = service.duplicates_delete(groups[0], 0)
        self.assertTrue(result["success"], result)
        self.assertEqual(mutations.deleted, [])
        self.assertTrue((parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm").exists())
        self.assertFalse((parent / "两京十五日 - S01E06 - 第6集.strm").exists())
        self.assertFalse((parent / "两京十五日 - S01E06 - 第6集.nfo").exists())

    def test_delete_refuses_when_cloud_file_unresolvable(self):
        _, tagged, plain, group, _ = self._setup_e06()
        service = _duplicates_service(
            self.root, query=_FakeCloudQuery({}), mutations=_FakeMutations(),
        )
        result = service.duplicates_delete(group, group["suggested_keep"])
        self.assertFalse(result["success"])
        self.assertTrue(tagged.exists())
        self.assertTrue(plain.exists())
        self.assertIn("找不到该视频", result["data"]["results"][0]["message"])

    def test_delete_refuses_on_pickcode_mismatch(self):
        season_dir, tagged, plain, group, query = self._setup_e06()
        group["items"][0]["pickcode"] = "tampered-pickcode"
        mutations = _FakeMutations()
        service = _duplicates_service(self.root, query=query, mutations=mutations)
        result = service.duplicates_delete(group, group["suggested_keep"])
        self.assertFalse(result["success"])
        self.assertEqual(mutations.deleted, [])
        self.assertTrue(tagged.exists())
        self.assertIn("不一致", result["data"]["results"][0]["message"])

    def test_delete_refuses_unknown_group(self):
        parent = self.root / "Season 1"
        _write_strm(parent / "两京十五日 - S01E06 - 第6集.strm", "garbage")
        _write_strm(parent / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm", "garbage")
        service = _duplicates_service(self.root)
        groups = service.duplicates_scan()["data"]["groups"]
        result = service.duplicates_delete(groups[0], 0)
        self.assertFalse(result["success"])
        self.assertIn("无法确认视频身份", result["message"])

    def test_delete_refuses_bad_keep_index(self):
        _, _, _, group, query = self._setup_e06()
        service = _duplicates_service(self.root, query=query, mutations=_FakeMutations())
        self.assertFalse(service.duplicates_delete(group, 99)["success"])
        self.assertFalse(service.duplicates_delete(group, "x")["success"])


class _FakePlaybackReferenceForOps:
    template_variables = frozenset({"pickcode"})

    @staticmethod
    def reference_values(file_item):
        return dict(file_item.playback_values)


class _OpsFakeCloudQuery:
    def __init__(self, files):
        self._files = dict(files)

    def find_file(self, cloud_dir, file_name):
        return self._files.get((cloud_dir, file_name))


class RegressionGenerateStrmTests(_MetaInfoTestCase):
    """根因回归：真实 _generate_strm，同一集两次生成只剩一份 .strm。"""

    def _ops_service(self, query):
        service = StrmOpsMixin(SimpleNamespace())
        service._strm_generate_enabled = True
        service._strm_generator = StrmGenerator(
            base_url=BASE_URL,
            template=StrmGenerator.REDIRECT_TEMPLATE,
            provider_variables=["pickcode"],
        )
        service._local_resource_path = str(self.root)
        service._CLOUD_MEDIA_ROOT = "/"
        service._playback_reference = _FakePlaybackReferenceForOps()
        service._cloud_query = query
        return service

    def _generate(self, service, cloud_dir, file_name, pickcode):
        target = SimpleNamespace(
            name=file_name, playback_values={"pickcode": pickcode},
        )
        return service._generate_strm(
            cloud_dir, file_name, target_file=target, lookup_target=False,
        )

    def test_same_episode_generated_twice_leaves_one_strm(self):
        cloud_dir = "/115网盘/剧集/国产剧/两京十五日/Season 1"
        service = self._ops_service(_OpsFakeCloudQuery({}))
        first = self._generate(
            service, cloud_dir,
            "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv", "pick-first",
        )
        self.assertIsNotNone(first)
        self.assertTrue(first.exists())
        # 旧 .strm 的同主干 .nfo（刮削给每份视频各写一份）。
        nfo = self.root / "115网盘" / "剧集" / "国产剧" / "两京十五日" / "Season 1" / "两京十五日 {tmdb-203483} - S01E06 - 第6集.nfo"
        nfo.write_text("<nfo/>", encoding="utf-8")
        second = self._generate(
            service, cloud_dir,
            "两京十五日 - S01E06 - 第6集.mkv", "pick-second",
        )
        self.assertIsNotNone(second)
        self.assertTrue(second.exists())
        season_dir = first.parent
        remaining = sorted(path.name for path in season_dir.glob("*.strm"))
        self.assertEqual(remaining, ["两京十五日 - S01E06 - 第6集.strm"])
        self.assertFalse(first.exists())
        self.assertFalse(nfo.exists())
        self.assertIn("pick-second", second.read_text(encoding="utf-8"))

    def test_different_episodes_both_remain(self):
        cloud_dir = "/115网盘/剧集/国产剧/两京十五日/Season 1"
        service = self._ops_service(_OpsFakeCloudQuery({}))
        first = self._generate(
            service, cloud_dir,
            "两京十五日 - S01E05 - 第5集.mkv", "pick-e05",
        )
        second = self._generate(
            service, cloud_dir,
            "两京十五日 - S01E06 - 第6集.mkv", "pick-e06",
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        season_dir = first.parent
        remaining = sorted(path.name for path in season_dir.glob("*.strm"))
        self.assertEqual(remaining, [
            "两京十五日 - S01E05 - 第5集.strm",
            "两京十五日 - S01E06 - 第6集.strm",
        ])

    def test_same_video_regenerated_leaves_one_strm(self):
        cloud_dir = "/115网盘/剧集/国产剧/两京十五日/Season 1"
        service = self._ops_service(_OpsFakeCloudQuery({}))
        first = self._generate(
            service, cloud_dir,
            "两京十五日 - S01E06 - 第6集.mkv", "pick-same",
        )
        second = self._generate(
            service, cloud_dir,
            "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv", "pick-same",
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        season_dir = first.parent
        remaining = sorted(path.name for path in season_dir.glob("*.strm"))
        self.assertEqual(
            remaining, ["两京十五日 {tmdb-203483} - S01E06 - 第6集.strm"],
        )


class ApiDelegationTests(unittest.TestCase):
    def _api(self, handler=None, failure=None):
        owner = SimpleNamespace(
            _require_sync_handler=lambda *names: (handler, failure),
        )
        return StrmDuplicatesApi(owner)

    def test_scan_enrich_delete_delegate(self):
        calls = {}
        handler = SimpleNamespace(
            duplicates_scan=lambda: calls.setdefault("scan", {"success": True}),
            duplicates_enrich=lambda groups: calls.setdefault("enrich", {"success": True}),
            duplicates_delete=lambda group, keep: calls.setdefault("delete", {"success": True}),
        )
        api = self._api(handler=handler)
        self.assertEqual(api.api_vue_duplicates_scan(), {"success": True})
        self.assertEqual(api.api_vue_duplicates_enrich({"groups": []}), {"success": True})
        self.assertEqual(
            api.api_vue_duplicates_delete({"group": {}, "keep_index": 0}),
            {"success": True},
        )
        self.assertEqual(set(calls), {"scan", "enrich", "delete"})

    def test_missing_handler_returns_failure(self):
        failure = {"success": False, "message": "同步服务未就绪"}
        api = self._api(handler=None, failure=failure)
        self.assertEqual(api.api_vue_duplicates_scan(), failure)
        self.assertEqual(api.api_vue_duplicates_enrich({}), failure)
        self.assertEqual(api.api_vue_duplicates_delete({}), failure)

    def test_save_load_state_delegate(self):
        calls = {}
        handler = SimpleNamespace(
            duplicates_save_state=lambda state: calls.setdefault(
                "save", {"success": True},
            ),
            duplicates_load_state=lambda: calls.setdefault(
                "load", {"success": True},
            ),
        )
        api = self._api(handler=handler)
        self.assertEqual(
            api.api_vue_duplicates_save_state({"state": {}}),
            {"success": True},
        )
        self.assertEqual(api.api_vue_duplicates_load_state(), {"success": True})
        self.assertEqual(set(calls), {"save", "load"})

    def test_save_load_missing_handler_returns_failure(self):
        failure = {"success": False, "message": "同步服务未就绪"}
        api = self._api(handler=None, failure=failure)
        self.assertEqual(api.api_vue_duplicates_save_state({"state": {}}), failure)
        self.assertEqual(api.api_vue_duplicates_load_state(), failure)


class MediaRootMappingTests(_MetaInfoTestCase):
    """线上口径：_local_resource_path 就是 .strm 根（/strm/115网盘），
    网盘目录 = _CLOUD_MEDIA_ROOT + 相对目录。初版把相对目录直接当网盘
    目录（缺媒体库根前缀），线上 enrich 查不到大小、delete 永远拒绝；
    这里用线上布局锁住正确映射。"""

    MEDIA_ROOT = "/Alist/影视/moviepilot/媒体库"

    def setUp(self):
        super().setUp()
        self.strm_root = self.root / "115网盘"
        season_dir = (
            self.strm_root / "剧集" / "国产剧" / "两京十五日" / "Season 1"
        )
        self.tagged = _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content(
                "pick-e06-tagged",
                "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
            ),
        )
        self.plain = _write_strm(
            season_dir / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content(
                "pick-e06-plain", "两京十五日 - S01E06 - 第6集.mkv",
            ),
        )
        (season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.nfo").write_text(
            "<nfo/>", encoding="utf-8",
        )
        self.cloud_dir = self.MEDIA_ROOT + "/剧集/国产剧/两京十五日/Season 1"
        self.query = _FakeCloudQuery({
            (self.cloud_dir, "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-tagged", "x", 1_360_000_000, "pick-e06-tagged"),
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"):
                _FakeCloudFile("fid-plain", "x", 1_950_000_000, "pick-e06-plain"),
        })

    def _service(self, query=None, mutations=None):
        return _duplicates_service(
            self.strm_root, query=query, mutations=mutations,
            media_root=self.MEDIA_ROOT,
        )

    def test_cloud_dir_for_local_prefixes_media_root(self):
        service = self._service()
        self.assertEqual(
            service._cloud_dir_for_local(
                "剧集/国产剧/两京十五日/Season 1/两京十五日 - S01E06 - 第6集.strm"
            ),
            self.cloud_dir,
        )
        self.assertEqual(
            service._cloud_dir_for_local("file.strm"), self.MEDIA_ROOT,
        )

    def test_enrich_finds_sizes_with_media_root(self):
        groups = self._service().duplicates_scan()["data"]["groups"]
        self.assertEqual(len(groups), 1)
        enriched = self._service(query=self.query).duplicates_enrich(groups)
        self.assertTrue(enriched["success"], enriched)
        group = enriched["data"]["groups"][0]
        self.assertEqual(group["suggested_keep"], 1)
        self.assertEqual(group["items"][1]["video_size"], 1_950_000_000)
        self.assertEqual(group["items"][1]["video_id"], "fid-plain")
        self.assertIn(
            (self.cloud_dir, "两京十五日 - S01E06 - 第6集.mkv"),
            self.query.find_calls,
        )

    def test_delete_locates_video_with_media_root(self):
        groups = self._service().duplicates_scan()["data"]["groups"]
        enriched = self._service(query=self.query).duplicates_enrich(groups)
        group = enriched["data"]["groups"][0]
        mutations = _FakeMutations()
        service = self._service(query=self.query, mutations=mutations)
        result = service.duplicates_delete(group, group["suggested_keep"])
        self.assertTrue(result["success"], result)
        self.assertEqual(mutations.deleted, ["fid-tagged"])
        self.assertFalse(self.tagged.exists())
        self.assertTrue(self.plain.exists())
        self.assertIn(
            (
                self.cloud_dir,
                "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
            ),
            self.query.find_strict_calls,
        )


class _RaisingCloudQuery(_FakeCloudQuery):
    """指定（目录，文件名）抛错的假查询：锁住单项失败隔离。"""

    def __init__(self, files, raise_for=()):
        super().__init__(files)
        self._raise_for = set(raise_for)

    def find_file(self, cloud_dir, file_name):
        self.find_calls.append((cloud_dir, file_name))
        if (cloud_dir, file_name) in self._raise_for:
            raise RuntimeError("115 查询异常（测试桩）")
        return self._files.get((cloud_dir, file_name))


class EnrichChunkingTests(_MetaInfoTestCase):
    """核对分片契约（v1.31.1）：后端 enrich 本就接受 groups 子集；
    前端分片循环依赖这三条不变——切片只返回切片、两次分片等价全量、
    单项失败不拖垮整组/整轮。"""

    def _five_groups(self):
        ScanServiceTests._build_library(self)
        other_dir = (
            self.root / "115网盘" / "剧集" / "国产剧" / "冰之城墙" / "Season 1"
        )
        _write_strm(
            other_dir / "冰之城墙 {tmdb-999001} - S01E01 - 第1集.strm",
            _redirect_content("pick-bz01-tagged", "冰之城墙 {tmdb-999001} - S01E01 - 第1集.mkv"),
        )
        _write_strm(
            other_dir / "冰之城墙 - S01E01 - 第1集.strm",
            _redirect_content("pick-bz01-plain", "冰之城墙 - S01E01 - 第1集.mkv"),
        )
        _write_strm(
            other_dir / "冰之城墙 {tmdb-999001} - S01E02 - 第2集.strm",
            _redirect_content("pick-bz02-tagged", "冰之城墙 {tmdb-999001} - S01E02 - 第2集.mkv"),
        )
        _write_strm(
            other_dir / "冰之城墙 - S01E02 - 第2集.strm",
            _redirect_content("pick-bz02-plain", "冰之城墙 - S01E02 - 第2集.mkv"),
        )
        service = _duplicates_service(self.root)
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["data"]["groups"]), 5)
        return result["data"]["groups"]

    def _query_for_five(self):
        query = EnrichServiceTests._query_for(self)
        query._files.update({
            ("/115网盘/剧集/国产剧/冰之城墙/Season 1", "冰之城墙 {tmdb-999001} - S01E01 - 第1集.mkv"):
                _FakeCloudFile("fid-bz01-tagged", "x", 2_100_000_000, "pick-bz01-tagged"),
            ("/115网盘/剧集/国产剧/冰之城墙/Season 1", "冰之城墙 - S01E01 - 第1集.mkv"):
                _FakeCloudFile("fid-bz01-plain", "x", 1_800_000_000, "pick-bz01-plain"),
            ("/115网盘/剧集/国产剧/冰之城墙/Season 1", "冰之城墙 {tmdb-999001} - S01E02 - 第2集.mkv"):
                _FakeCloudFile("fid-bz02-tagged", "x", 2_200_000_000, "pick-bz02-tagged"),
            ("/115网盘/剧集/国产剧/冰之城墙/Season 1", "冰之城墙 - S01E02 - 第2集.mkv"):
                _FakeCloudFile("fid-bz02-plain", "x", 1_900_000_000, "pick-bz02-plain"),
        })
        return query

    @staticmethod
    def _snapshot(groups):
        return [
            {
                "key": group["key"],
                "kind": group["kind"],
                "suggested_keep": group["suggested_keep"],
                "sizes_pending": group["sizes_pending"],
                "items": [
                    (item["file_name"], item["video_size"], item["video_id"])
                    for item in group["items"]
                ],
            }
            for group in groups
        ]

    def test_slice_enrich_returns_only_the_slice(self):
        groups = self._five_groups()
        query = self._query_for_five()
        service = _duplicates_service(self.root, query=query)
        result = service.duplicates_enrich(groups[:2])
        self.assertTrue(result["success"], result)
        enriched = result["data"]["groups"]
        self.assertEqual(len(enriched), 2)
        self.assertEqual(
            [group["key"] for group in enriched],
            [group["key"] for group in groups[:2]],
        )
        self.assertFalse(any(group["sizes_pending"] for group in enriched))
        # 未传的组原样不动：大小仍未知、仍待核对。
        for group in groups[2:]:
            self.assertTrue(group["sizes_pending"])
            self.assertTrue(all(item["video_size"] is None for item in group["items"]))

    def test_two_slices_equal_one_full_enrich(self):
        groups = self._five_groups()
        service = _duplicates_service(self.root, query=self._query_for_five())
        full = service.duplicates_enrich(groups)["data"]["groups"]
        first = service.duplicates_enrich(groups[:2])["data"]["groups"]
        second = service.duplicates_enrich(groups[2:])["data"]["groups"]
        self.assertEqual(self._snapshot(first + second), self._snapshot(full))

    def test_failed_group_is_isolated(self):
        groups = self._five_groups()
        failing = {
            (
                "/115网盘/剧集/国产剧/两京十五日/Season 1",
                "两京十五日 {tmdb-203483} - S01E05 - 第5集.mkv",
            ),
            (
                "/115网盘/剧集/国产剧/两京十五日/Season 1",
                "两京十五日 - S01E05 - 第5集.mkv",
            ),
        }
        query = _RaisingCloudQuery(
            self._query_for_five()._files, raise_for=failing,
        )
        service = _duplicates_service(self.root, query=query)
        enriched = service.duplicates_enrich(groups)["data"]["groups"]
        self.assertEqual(len(enriched), 5)
        e05 = next(
            group for group in enriched
            if group["title"] == "两京十五日" and group["episode"] == 5
        )
        self.assertTrue(all(item["video_size"] is None for item in e05["items"]))
        self.assertIsNone(e05["suggested_keep"])
        self.assertFalse(e05["sizes_pending"])
        for group in enriched:
            if group is e05:
                continue
            self.assertTrue(all(
                item["video_size"] is not None
                for item in group["items"]
                if item["kind"] == "redirect"
            ))
            self.assertIsNotNone(group["suggested_keep"])


class _FakeStateStore:
    """runtime_records 命名空间的状态存储替身（dict 内存，深拷贝隔离）。"""

    def __init__(self):
        self.rows = {}

    def load(self, key, default=None):
        return copy.deepcopy(self.rows.get(key, default))

    def save(self, key, value):
        self.rows[key] = copy.deepcopy(value)

    def delete(self, key):
        self.rows.pop(key, None)


class _FakeDataStore:
    """CloudSubscribeDataStore 替身：只提供 runtime_store。"""

    def __init__(self):
        self.stores = {}

    def runtime_store(self, namespace):
        return self.stores.setdefault(namespace, _FakeStateStore())


class DuplicatesStateTests(_MetaInfoTestCase):
    """扫描状态持久化（v1.31.2）：runtime_records 命名空间 strm_duplicates。

    存的内容 = groups 全量（含 video_size/video_id/suggested_keep/
    sizes_pending）+ 扫描元数据 + keep_by_key；saved_at 由服务端补写。
    """

    def _state_service(self, root=None, query=None, mutations=None, media_root=None):
        store = _FakeDataStore()
        service = _duplicates_service(
            root or self.root, query=query, mutations=mutations,
            media_root=media_root, store=store,
        )
        return service, store

    def _three_groups(self):
        """_build_library 的 3 组（两京 E05/E06 + 双帝之战电影组）。"""
        ScanServiceTests._build_library(self)
        service = _duplicates_service(self.root)
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["data"]["groups"]), 3)
        return result["data"]["groups"]

    def test_save_load_round_trip(self):
        groups = self._three_groups()
        query = EnrichServiceTests._query_for(self)
        service, _store = self._state_service(query=query)
        enriched = service.duplicates_enrich(groups)["data"]["groups"]
        keep_by_key = {enriched[0]["key"]: 1, enriched[1]["key"]: 0}
        state = {
            "groups": enriched,
            "strm_root": str(self.root),
            "dirs_scanned": 3,
            "strm_count": 7,
            "unreadable": 0,
            "keep_by_key": keep_by_key,
        }
        result = service.duplicates_save_state(state)
        self.assertTrue(result["success"], result)
        self.assertIsInstance(result["data"]["saved_at"], int)
        self.assertGreater(result["data"]["saved_at"], 0)
        loaded = service.duplicates_load_state()
        self.assertTrue(loaded["success"], loaded)
        saved = loaded["data"]["state"]
        self.assertIsNotNone(saved)
        self.assertEqual(saved["groups"], enriched)
        self.assertEqual(saved["keep_by_key"], keep_by_key)
        self.assertEqual(saved["strm_root"], str(self.root))
        self.assertEqual(saved["dirs_scanned"], 3)
        self.assertEqual(saved["strm_count"], 7)
        self.assertEqual(saved["saved_at"], result["data"]["saved_at"])

    def test_scan_saves_state(self):
        # 线上布局：_local_resource_path 就是 .strm 根，_CLOUD_MEDIA_ROOT 另设。
        strm_root = self.root / "115网盘"
        season_dir = strm_root / "剧集" / "国产剧" / "两京十五日" / "Season 1"
        _write_strm(
            season_dir / "两京十五日 {tmdb-203483} - S01E06 - 第6集.strm",
            _redirect_content(
                "pick-e06-tagged",
                "两京十五日 {tmdb-203483} - S01E06 - 第6集.mkv",
            ),
        )
        _write_strm(
            season_dir / "两京十五日 - S01E06 - 第6集.strm",
            _redirect_content(
                "pick-e06-plain", "两京十五日 - S01E06 - 第6集.mkv",
            ),
        )
        service, _store = self._state_service(
            root=strm_root, media_root=MediaRootMappingTests.MEDIA_ROOT,
        )
        result = service.duplicates_scan()
        self.assertTrue(result["success"], result)
        self.assertEqual(len(result["data"]["groups"]), 1)
        self.assertIn("saved_at", result["data"])
        loaded = service.duplicates_load_state()["data"]["state"]
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["groups"], result["data"]["groups"])
        self.assertEqual(loaded["strm_root"], str(strm_root))
        self.assertEqual(
            loaded["dirs_scanned"], result["data"]["dirs_scanned"],
        )
        self.assertEqual(
            loaded["strm_count"], result["data"]["strm_count"],
        )
        self.assertEqual(loaded["keep_by_key"], {})
        self.assertEqual(loaded["saved_at"], result["data"]["saved_at"])

    def test_sliced_enrich_flow_matches_full_enrich(self):
        groups = self._three_groups()
        query = EnrichServiceTests._query_for(self)
        service, _store = self._state_service(query=query)
        full = service.duplicates_enrich(groups)["data"]["groups"]
        # 模拟前端：扫描后保存 → 分片核对、每批合并后保存 → 读回。
        service.duplicates_save_state({
            "groups": groups, "strm_root": str(self.root),
            "dirs_scanned": 0, "strm_count": 0, "unreadable": 0,
            "keep_by_key": {},
        })
        merged = {group["key"]: group for group in groups}
        keep_by_key = {}
        for start in range(0, len(groups), 2):
            batch = groups[start:start + 2]
            enriched = service.duplicates_enrich(batch)["data"]["groups"]
            for group in enriched:
                merged[group["key"]] = group
                if group["key"] not in keep_by_key:
                    suggested = group["suggested_keep"]
                    keep_by_key[group["key"]] = (
                        suggested if suggested is not None else 0
                    )
            ordered = [merged[group["key"]] for group in groups]
            service.duplicates_save_state({
                "groups": ordered, "strm_root": str(self.root),
                "dirs_scanned": 0, "strm_count": 0, "unreadable": 0,
                "keep_by_key": keep_by_key,
            })
        loaded = service.duplicates_load_state()["data"]["state"]
        self.assertIsNotNone(loaded)
        self.assertEqual(
            EnrichChunkingTests._snapshot(loaded["groups"]),
            EnrichChunkingTests._snapshot(full),
        )
        self.assertEqual(loaded["keep_by_key"], keep_by_key)

    def test_load_without_save_returns_empty(self):
        service, _store = self._state_service()
        loaded = service.duplicates_load_state()
        self.assertTrue(loaded["success"], loaded)
        self.assertIsNone(loaded["data"]["state"])
        # 没有存储可用时也按无保存处理，不报错。
        no_store = _duplicates_service(self.root)
        loaded = no_store.duplicates_load_state()
        self.assertTrue(loaded["success"], loaded)
        self.assertIsNone(loaded["data"]["state"])

    def test_save_without_store_fails_softly_and_scan_still_works(self):
        service = _duplicates_service(self.root)
        result = service.duplicates_save_state({"groups": []})
        self.assertFalse(result["success"])
        # 扫描的自动保存 fail-open，不影响扫描结果。
        scanned = service.duplicates_scan()
        self.assertTrue(scanned["success"], scanned)
        self.assertNotIn("saved_at", scanned["data"])

    def test_save_rejects_bad_state(self):
        service, _store = self._state_service()
        self.assertFalse(service.duplicates_save_state(None)["success"])
        self.assertFalse(service.duplicates_save_state("x")["success"])
        self.assertFalse(
            service.duplicates_save_state({"groups": "not-a-list"})["success"]
        )
        self.assertFalse(
            service.duplicates_save_state(
                {"groups": [], "keep_by_key": [1]},
            )["success"]
        )


if __name__ == "__main__":
    unittest.main()
