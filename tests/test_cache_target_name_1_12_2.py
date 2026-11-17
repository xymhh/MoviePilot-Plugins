"""源文件名缓存迁移与 aria2 控制文件随行（1.12.2）。

背景（2026-11-16）：1.12.1 历史重试漏传目标名，文件以源文件名落盘
（如 /downloads/06.mkv，带 06.mkv.aria2 与 113 字节的未完成 meta），
MP 整理认不出剧名。1.12.2 修好重试传目标名后，下次重试要把这些已下
好的源文件名文件改名成目标名复用（不重下），而不是只认旧命名
cloud-transfer-<hash>-…。

固定契约：
1. 源文件名文件在登记表里有归属标记（cache_key 对得上）时，
   _resolve_cache_path 把它改名成目标名（含 .aria2 与登记表条目），
   只读查找不改名；
2. 源文件名文件归属对不上（登记表里是别人的 key）或没有任何登记
   （裸文件，无法证明是本次源的）时不迁移、不复用；
3. _rename_cache_files 连 .aria2 控制文件一起改名（aria2 continue=true
   才能按新名断点续传）；delete_cache 删主文件时连 .aria2 一起删；
4. residue 扫描：.aria2 随主文件进组；主文件已不在的孤 .aria2 只有
   主文件名本身就是 cloud-transfer 旧命名时才清，MP 自身 aria2
   留下的不动；旧版 .meta.json / .parts.json 见到直接删。
"""

import hashlib
import os
import sys
import time
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

residue_module = load_module(
    "cloudsubscribex.core.residue", "plugins.v2/cloudsubscribex/core/residue.py"
)
scan_residue = residue_module.scan_residue

CONTENT = b"cache-bytes" * 100
OTHER = b"other-bytes" * 100

SOURCE_NAME = "06.mkv"
TARGET = "十万狂花入梦来 - S01E06 - 第6集.mkv"


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


def _seed_source_named(manager, tmp, source, with_stamp=True, with_aria2=True):
    """模拟 1.12.1 重试留下的源文件名文件：主文件 + 登记表未完成条目 + .aria2。"""
    path = Path(tmp) / SOURCE_NAME
    path.write_bytes(CONTENT)
    if with_stamp:
        manager._stamp_cache_owner(
            path, manager._cache_key("quark", source), source
        )
    if with_aria2:
        (Path(tmp) / f"{SOURCE_NAME}.aria2").write_bytes(b"aria2-control")
    return path


def _touch(root, name, age_days, size=10):
    path = Path(root) / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _age(path, age_days):
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))


# ------------------------------------------------------- 源文件名迁移


def test_resolve_migrates_source_named_cache_with_matching_stamp():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        seeded = _seed_source_named(manager, tmp, source)

        resolved = manager._resolve_cache_path(key, source, target_name=TARGET)

        target = Path(tmp) / TARGET
        assert resolved == target
        assert target.is_file()
        assert not seeded.exists()
        assert (Path(tmp) / f"{TARGET}.aria2").is_file()
        assert not (Path(tmp) / f"{SOURCE_NAME}.aria2").exists()
        assert manager._read_cache_owner_key(target) == key
        assert not [p for p in Path(tmp).iterdir() if p.name.endswith(".json")]


def test_resolve_ignores_source_named_cache_with_wrong_key():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        # 别人的文件：meta 里是另一个源的 key，同名也不能认。
        other = _make_source(SOURCE_NAME, content=OTHER)
        other_key = manager._cache_key("quark", other)
        assert other_key != key
        path = Path(tmp) / SOURCE_NAME
        path.write_bytes(OTHER)
        manager._stamp_cache_owner(path, other_key, other)

        resolved = manager._resolve_cache_path(key, source, target_name=TARGET)

        assert resolved == Path(tmp) / TARGET
        assert path.is_file()
        assert not (Path(tmp) / TARGET).exists()


def test_resolve_ignores_bare_source_named_file():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        path = Path(tmp) / SOURCE_NAME
        path.write_bytes(CONTENT)

        resolved = manager._resolve_cache_path(key, source, target_name=TARGET)

        assert resolved == Path(tmp) / TARGET
        assert path.is_file()
        assert not (Path(tmp) / TARGET).exists()


def test_lookup_finds_source_named_cache_without_renaming():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        seeded = _seed_source_named(manager, tmp, source)

        found = manager._lookup_cache_path(key, source, target_name=TARGET)

        assert found == seeded
        assert seeded.is_file()
        assert not (Path(tmp) / TARGET).exists()


def test_delete_cache_removes_aria2_sidecar():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        target.write_bytes(CONTENT)
        manager._stamp_cache_owner(target, key, source)
        (Path(tmp) / f"{TARGET}.aria2").write_bytes(b"aria2-control")

        assert manager.delete_cache(key) == 2
        assert not target.exists()
        assert not (Path(tmp) / f"{TARGET}.aria2").exists()
        assert manager._read_cache_owner_key(target) == ""


# ------------------------------------------------------- residue 认 .aria2


def test_residue_groups_aria2_with_old_scheme_main():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        main = _touch(tmp, f"cloud-transfer-{key}-{SOURCE_NAME}", 7, size=len(CONTENT))
        aria2 = _touch(tmp, f"{main.name}.aria2", 7, size=10)

        result = scan_residue(tmp)

        assert result["deleted_files"] == 2
        assert not main.exists()
        assert not aria2.exists()


def test_residue_cleans_legacy_meta_but_keeps_lone_aria2():
    # 1.12.19：旧版 meta 是 litter，直接删；孤 .aria2 的主文件名不是
    # cloud-transfer 旧命名，没有归属证据，不动（MP 自身 aria2 也可能
    # 留下这种文件）。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source(SOURCE_NAME)
        key = manager._cache_key("quark", source)
        meta = Path(tmp) / f"{SOURCE_NAME}.meta.json"
        meta.write_text('{"cache_key": "%s", "complete": false}' % key)
        _age(meta, 7)
        aria2 = _touch(tmp, f"{SOURCE_NAME}.aria2", 7, size=10)

        result = scan_residue(tmp)

        assert result["deleted_files"] == 1
        assert not meta.exists()
        assert aria2.exists()


def test_residue_ignores_lone_aria2_without_plugin_evidence():
    with TemporaryDirectory() as tmp:
        # MP 自身 aria2 留下的孤控制文件：主文件已不在，也没有任何插件
        # 伴随文件证明归属，一律不碰。
        aria2 = _touch(tmp, "movie.mkv.aria2", 7, size=10)

        result = scan_residue(tmp)

        assert result["deleted_files"] == 0
        assert aria2.exists()
