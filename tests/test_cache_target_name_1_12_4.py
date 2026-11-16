"""无标记旧目标名缓存迁移（1.12.4）。

背景（2026-11-16）：1.12.4 起下载目标名带 {tmdb-...} 标记，此前版本
下载的文件用的是无标记的旧目标名（如 /downloads/十万狂花入梦来 -
S01E06 - 第6集.mkv、/downloads/凡人修仙传 - S01E200 - 第200集.mkv），
与当前目标名只差一个标记。重试时应按伴随文件核对归属后改名成当前
目标名复用，不重下（S01E200 有 2 GB）。

固定契约：
1. 无标记旧目标名文件有伴随文件能证明归属（meta 里的 cache_key
   对得上，或 parts 总量对得上）时，_resolve_cache_path 把它改名成
   当前目标名（含 meta/parts/aria2 伴随文件），只读查找不改名；
2. 归属对不上（meta 里是别人的 key）或没有任何伴随文件的裸文件时
   不迁移、不复用；
3. 目标名本身无标记时不产生无标记候选，行为与此前一致。
"""

import hashlib
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from plugin_env import load_module, register_mock, register_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)


def _plugin_dir(*parts):
    return os.path.join(plugins_v2_path, "cloudsubscribex", *parts)


def _register_real_package(name, path):
    module = register_module(name, package=True)
    module.__path__ = [path]
    return module


register_module("app", package=True)
register_module("app.core", package=True)
register_mock("app.core.cache", TTLCache=mock.MagicMock())
register_module("app.utils", package=True)
register_mock("app.utils.string", StringUtils=mock.MagicMock())
register_mock("app.log", logger=mock.MagicMock())

_register_real_package("cloudsubscribex", _plugin_dir())
_register_real_package("cloudsubscribex.core", _plugin_dir("core"))
_register_real_package("cloudsubscribex.drive", _plugin_dir("drive"))
_register_real_package("cloudsubscribex.utils", _plugin_dir("utils"))
register_mock(
    "cloudsubscribex.utils.cache",
    create_platform_ttl_cache=mock.MagicMock(return_value=mock.MagicMock()),
)

transfer_module = load_module(
    "cloudsubscribex.core.transfer", "plugins.v2/cloudsubscribex/core/transfer.py"
)
CrossTransferTaskManager = transfer_module.CrossTransferTaskManager
CloudFile = transfer_module.CloudFile

CONTENT = b"cache-bytes" * 100
OTHER = b"other-bytes" * 100

UNTAGGED = "十万狂花入梦来 - S01E06 - 第6集.mkv"
TAGGED = "十万狂花入梦来 {tmdb-261315} - S01E06 - 第6集.mkv"
SOURCE_NAME = "06.mkv"


def _make_source(name, content=CONTENT):
    return CloudFile(
        id="file-1", name=name, is_directory=False, size=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
        md5=hashlib.md5(content).hexdigest(),
    )


def _make_manager(download_path):
    manager = CrossTransferTaskManager(
        lambda key: mock.MagicMock(), download_path=download_path
    )
    manager._notify_change = lambda: None
    return manager


def _seed_untagged(manager, tmp, source, with_aria2=True):
    """模拟 1.12.2/1.12.3 重试留下的无标记旧目标名文件：主文件 +
    已完成 meta + .aria2。"""
    path = Path(tmp) / UNTAGGED
    path.write_bytes(CONTENT)
    manager._stamp_cache_owner(
        path, manager._cache_key("quark", source), source
    )
    manager._mark_complete_cache(
        path, manager._cache_key("quark", source), source
    )
    if with_aria2:
        (Path(tmp) / f"{UNTAGGED}.aria2").write_bytes(b"aria2-control")
    return path


def test_resolve_migrates_untagged_target_name_with_matching_stamp():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        seeded = _seed_untagged(manager, tmp, source)

        resolved = manager._resolve_cache_path(key, source, target_name=TAGGED)

        target = Path(tmp) / TAGGED
        assert resolved == target
        assert target.is_file()
        assert not seeded.exists()
        assert (Path(tmp) / f"{TAGGED}.meta.json").is_file()
        assert (Path(tmp) / f"{TAGGED}.aria2").is_file()
        assert not (Path(tmp) / f"{UNTAGGED}.aria2").exists()
        assert manager._read_cache_owner_key(target) == key


def test_resolve_ignores_untagged_target_name_with_wrong_key():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        other = _make_source(SOURCE_NAME, content=OTHER)
        other_key = manager._cache_key("quark", other)
        assert other_key != key
        path = Path(tmp) / UNTAGGED
        path.write_bytes(OTHER)
        manager._stamp_cache_owner(path, other_key, other)

        resolved = manager._resolve_cache_path(key, source, target_name=TAGGED)

        assert resolved == Path(tmp) / TAGGED
        assert path.is_file()
        assert not (Path(tmp) / TAGGED).exists()


def test_resolve_ignores_bare_untagged_target_name():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        path = Path(tmp) / UNTAGGED
        path.write_bytes(CONTENT)

        resolved = manager._resolve_cache_path(key, source, target_name=TAGGED)

        assert resolved == Path(tmp) / TAGGED
        assert path.is_file()
        assert not (Path(tmp) / TAGGED).exists()


def test_lookup_finds_untagged_target_name_without_renaming():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        seeded = _seed_untagged(manager, tmp, source)

        found = manager._lookup_cache_path(key, source, target_name=TAGGED)

        assert found == seeded
        assert seeded.is_file()
        assert not (Path(tmp) / TAGGED).exists()


def test_untagged_cache_path_returns_none_without_tag():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)

        assert manager._untagged_cache_path(None) is None
        assert manager._untagged_cache_path(UNTAGGED) is None
        assert manager._untagged_cache_path(TAGGED) == Path(tmp) / UNTAGGED
