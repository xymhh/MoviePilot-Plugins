"""整理台纯函数测试：扫描、季集解析、分组、去重与字幕关联。"""

from dataclasses import dataclass, field
from typing import Dict, List

from tests.plugin_env import load_module

plan = load_module(
    "cloudsubscribex.utils.organize_plan",
    "plugins.v2/cloudsubscribex/utils/organize_plan.py",
)


@dataclass
class FakeFile:
    id: str
    name: str
    size: int = 0
    is_directory: bool = False


@dataclass
class FakeLookup:
    checked: bool = True
    directory_id: str = "root"


@dataclass
class FakeListing:
    checked: bool = True
    files: List[FakeFile] = field(default_factory=list)


class FakeProvider:
    def __init__(self, tree: Dict[str, List[FakeFile]]):
        self.tree = tree

    def resolve_directory(self, path: str) -> FakeLookup:
        return FakeLookup(checked=True, directory_id="root")

    def list_directory(self, cid: str) -> FakeListing:
        return FakeListing(checked=True, files=self.tree.get(cid, []))


def entry(name: str, parent: str, size: int = 1, video: bool = True, subtitles: bool = False):
    return plan.OrganizeEntry(
        file_id=name,
        name=name,
        size=size,
        parent_path=parent,
        is_video=video,
        is_subtitle=subtitles,
    )


def test_scan_nested_tree_depth_and_limits():
    provider = FakeProvider({
        "root": [FakeFile("show", "Show", is_directory=True), FakeFile("movie.mkv", "Movie.mkv", 10)],
        "show": [FakeFile("s1", "Season 1", is_directory=True)],
        "s1": [FakeFile("e1", "Show.S01E01.mkv", 100), FakeFile("e2", "Show.S01E02.mkv", 200)],
    })
    result = plan.scan_entries(provider, "/待整理", max_files=10, max_depth=5)
    assert result.stats.directories == 3
    assert result.stats.videos == 3
    assert {item.name for item in result.entries} == {"Movie.mkv", "Show.S01E01.mkv", "Show.S01E02.mkv"}
    assert all(item.cloud_file is not None for item in result.entries)


def test_scan_respects_max_files_and_marks_truncated():
    provider = FakeProvider({
        "root": [FakeFile("e1", "A.S01E01.mkv", 1), FakeFile("e2", "A.S01E02.mkv", 1)],
    })
    result = plan.scan_entries(provider, "/待整理", max_files=1)
    assert result.stats.videos == 1
    assert result.stats.truncated is True


def test_scan_stops_on_request_and_reports_bad_root():
    provider = FakeProvider({"root": [FakeFile("e1", "A.S01E01.mkv", 1)]})
    result = plan.scan_entries(provider, "/待整理", stop_requested=lambda: True)
    assert result.entries == []
    assert result.stats.truncated is True

    class Broken:
        def resolve_directory(self, path):
            return FakeLookup(checked=False)

    failed = plan.scan_entries(Broken(), "/待整理")
    assert failed.entries == []
    assert failed.errors


def test_parse_episode_variants():
    assert plan.parse_episode("Show.S01E02.1080p.mkv") == (1, 2)
    assert plan.parse_episode("Show.1x03.mkv") == (1, 3)
    assert plan.parse_episode("第 2 季 第 03 集.mkv") == (2, 3)
    assert plan.parse_episode("第 04 集.mkv") == (0, 4)
    assert plan.parse_episode("EP05.mkv") == (0, 5)
    assert plan.parse_episode("Movie.2024.mkv") is None


def test_group_skips_generic_and_season_dirs_and_dedupes():
    entries = [
        entry("Show.S01E01.1080p.mkv", "/待整理/剧集/Show/Season 1", 100),
        entry("Show.S01E01.2160p.mkv", "/待整理/剧集/Show/Season 1", 300),
        entry("Show.S01E02.mkv", "/待整理/剧集/Show/Season 1", 200),
        entry("Show.S01E01.chs.srt", "/待整理/剧集/Show/Season 1", 1, video=False, subtitles=True),
    ]
    packages = plan.build_packages(entries, "/待整理")
    assert len(packages) == 1
    package = packages[0]
    assert package.key == "Show"
    assert package.is_tv is True
    assert [(item.season, item.episode) for item in package.videos] == [(1, 1), (1, 2)]
    # 2160p 更大，保留它；1080p 进入重复项。
    assert package.videos[0].entry.name == "Show.S01E01.2160p.mkv"
    assert [item.name for item in package.duplicates] == ["Show.S01E01.1080p.mkv"]
    assert [item.name for item in package.videos[0].subtitles] == ["Show.S01E01.chs.srt"]


def test_movie_picks_largest_and_ignores_only_extras():
    entries = [
        entry("Movie.2024.1080p.mkv", "/待整理/电影/Movie", 100),
        entry("Movie.2024.2160p.mkv", "/待整理/电影/Movie", 300),
        entry("Movie.2024.chs.srt", "/待整理/电影/Movie", 1, video=False, subtitles=True),
    ]
    package = plan.build_packages(entries, "/待整理")[0]
    assert package.is_tv is False
    assert package.videos[0].entry.name == "Movie.2024.2160p.mkv"
    assert [item.name for item in package.ignored] == ["Movie.2024.1080p.mkv"]
    assert [item.name for item in package.videos[0].subtitles] == ["Movie.2024.chs.srt"]


def test_suffix_name_keeps_extension():
    assert plan.suffix_name("Show - S01E01.mkv", 2) == "Show - S01E01 (2).mkv"
    assert plan.suffix_name("Movie", 3) == "Movie (3)"


def test_same_show_multiple_seasons_dedupe_to_one_package():
    entries = [
        entry("Show.S01E01.mkv", "/待整理/剧集/Show/Season 1", 100),
        entry("Show.S02E01.mkv", "/待整理/剧集/Show/Season 2", 100),
    ]
    packages = plan.build_packages(entries, "/待整理")
    assert len(packages) == 1
    assert packages[0].key == "Show"
    assert [(item.season, item.episode) for item in packages[0].videos] == [(1, 1), (2, 1)]


def test_generic_library_directory_is_not_used_as_title():
    entry_item = entry("Movie.2024.mkv", "/待整理/电影/Movie", 100)
    assert plan.package_key(entry_item, "/待整理") == "Movie"


def test_loose_files_directly_under_source_root_group_by_filename():
    """整个待整理目录作为源时，根下散文件不能被归成同一个「待整理」包。"""
    entries = [
        entry("ShowA.S01E01.mkv", "/待整理", 100),
        entry("ShowA.S01E02.mkv", "/待整理", 100),
        entry("ShowB.S01E01.mkv", "/待整理", 100),
    ]
    packages = plan.build_packages(entries, "/待整理")
    assert {package.key for package in packages} == {"ShowA", "ShowB"}
    show_a = next(package for package in packages if package.key == "ShowA")
    assert [item.episode for item in show_a.videos] == [1, 2]


def test_date_container_directory_is_skipped_for_grouping():
    entries = [
        entry("ShowA.S01E01.mkv", "/待整理/2026-10-18", 100),
        entry("ShowB.S01E01.mkv", "/待整理/2026-10-18", 100),
        entry("ShowA.S01E02.mkv", "/待整理/2026-10-19", 100),
    ]
    packages = plan.build_packages(entries, "/待整理")
    assert {package.key for package in packages} == {"ShowA", "ShowB"}


def test_category_directory_is_not_used_as_show_title():
    """整个 /剧集 作为源时，不能用「国产剧」分类目录当作品名。"""
    entries = [
        entry("ShowA.S01E01.mkv", "/剧集/国产剧/ShowA/Season 1", 100),
        entry("ShowA.S01E02.mkv", "/剧集/国产剧/ShowA/Season 1", 100),
        entry("ShowB.S01E01.mkv", "/剧集/国产剧/ShowB/Season 1", 100),
        entry("ShowC.S01E01.mkv", "/剧集/欧美剧/ShowC/Season 1", 100),
    ]
    packages = plan.build_packages(entries, "/剧集")
    assert {package.key for package in packages} == {"ShowA", "ShowB", "ShowC"}
    show_a = next(package for package in packages if package.key == "ShowA")
    assert [item.episode for item in show_a.videos] == [1, 2]


def test_chinese_category_dir_with_loose_videos_splits_by_filename():
    entries = [
        entry("ShowA.S01E01.mkv", "/剧集/国产剧", 100),
        entry("ShowB.S01E01.mkv", "/剧集/国产剧", 100),
    ]
    packages = plan.build_packages(entries, "/剧集")
    assert {package.key for package in packages} == {"ShowA", "ShowB"}
