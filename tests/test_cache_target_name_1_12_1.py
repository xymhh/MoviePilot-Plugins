"""跨盘缓存目标名落盘（1.12.1）：下载时直接用目标名命名。

固定契约：
1. 有目标名时下载直接落盘为清洗后的目标名（如
   ``十万狂花入梦来 - S01E06 - 第6集.mkv``），交给 MP 整理时文件名本身
   就能被识别；目标名不可用（空、无扩展名）时退回旧命名
   ``cloud-transfer-<hash>-<源文件名>`` / ``.bin``；
2. 旧命名缓存命中时在 cache_key 锁内改名成目标名迁移（含伴随文件），
   改名后 ``_validate_complete_cache`` 仍通过，已下好的文件不用重下；
   只读查找（cache_info）不改名；
3. 目标名已被别源占用时加 `` (8 位 hash)`` 区分；同一源复用目标名、
   断点续传（parts 总量对得上）时不改名；
4. 缓存身份记在进程内登记表里：``delete_cache`` 按登记表里的 cache_key
   删目标名文件；residue 只按 cloud-transfer-* 命名分组，旧版
   ``.meta.json`` / ``.parts.json`` 见到直接删，目录里其他文件一律不碰。
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


def _write_complete_cache(manager, path, cache_key, source):
    path.write_bytes(
        CONTENT if source.sha1 == hashlib.sha1(CONTENT).hexdigest() else OTHER
    )
    manager._mark_complete_cache(path, cache_key, source)
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


# ---------------------------------------------------------------- 目标名落盘


def test_temporary_path_uses_target_name():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        task = {
            "source_provider": "quark", "source_file": source,
            "title": TARGET,
        }
        assert manager._temporary_path(task) == str(Path(tmp) / TARGET)


def test_temporary_path_falls_back_without_usable_target_name():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("源文件 S01E01.mkv")
        key = manager._cache_key("quark", source)
        no_ext = {
            "source_provider": "quark", "source_file": source,
            "title": "没有扩展名的目标名",
        }
        assert manager._temporary_path(no_ext) == str(
            manager._cache_path(key, source)
        )
        empty = {
            "source_provider": "quark", "source_file": source, "title": "",
        }
        assert manager._temporary_path(empty) == str(
            manager._cache_path(key, source)
        )


def test_target_name_sanitize_cleans_illegal_chars():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        path = manager._target_cache_path("剧名：S01/E06?.mkv")
        assert path is not None
        assert path.name == "剧名：S01_E06_.mkv"
        assert manager._target_cache_path("README") is None
        assert manager._target_cache_path("") is None


# ---------------------------------------------------------------- 旧名迁移


def test_legacy_cache_migrated_to_target_name_on_resolve():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        legacy = manager._cache_path(key, source)
        _write_complete_cache(manager, legacy, key, source)

        resolved = manager._resolve_cache_path(key, source, target_name=TARGET)

        target = Path(tmp) / TARGET
        assert resolved == target
        assert target.is_file()
        assert not legacy.exists()
        # 登记表条目随主文件改名：新路径认归属、校验仍通过。
        assert manager._read_cache_owner_key(target) == key
        assert manager._validate_complete_cache(target, key, source)
        assert not [p for p in Path(tmp).iterdir() if p.name.endswith(".json")]


def test_legacy_bin_migrated_to_target_name():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        _write_complete_cache(manager, legacy, key, source)

        resolved = manager._resolve_cache_path(key, source, target_name=TARGET)

        assert resolved == Path(tmp) / TARGET
        assert resolved.is_file()
        assert not legacy.exists()
        assert manager._validate_complete_cache(resolved, key, source)


def test_lookup_with_target_name_does_not_rename():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        legacy = manager._cache_path(key, source)
        _write_complete_cache(manager, legacy, key, source)

        assert manager._lookup_cache_path(key, source, target_name=TARGET) == legacy
        assert legacy.is_file()
        assert not (Path(tmp) / TARGET).exists()


def test_cache_info_finds_target_named_cache():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        _write_complete_cache(manager, target, key, source)

        info = manager.cache_info("quark", source, target_name=TARGET)
        assert info["cache_key"] == key
        assert info["cache_status"] == "complete"

        # 不传目标名时只找得到旧命名，目标名文件不在查找范围。
        assert manager.cache_info("quark", source)["cache_status"] == "missing"


# ---------------------------------------------------------------- 占用与复用


def test_collision_disambiguates_with_short_hash():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source_a = _make_source("06.mkv", CONTENT)
        source_b = _make_source("06.mkv", OTHER)
        key_a = manager._cache_key("quark", source_a)
        key_b = manager._cache_key("quark", source_b)
        target = Path(tmp) / TARGET
        _write_complete_cache(manager, target, key_b, source_b)

        resolved = manager._resolve_cache_path(key_a, source_a, target_name=TARGET)

        assert resolved.name == f"十万狂花入梦来 - S01E06 - 第6集 ({key_a[:8]}).mkv"
        assert resolved != target
        assert target.is_file()


def test_same_source_reuses_target_name():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        _write_complete_cache(manager, target, key, source)

        assert manager._resolve_cache_path(key, source, target_name=TARGET) == target
        assert manager._validate_complete_cache(target, key, source)


def test_partial_same_source_resumes_target_name():
    # 同一源的半截文件：登记表里有归属标记就复用目标名，不改名。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        target.write_bytes(CONTENT)
        manager._stamp_cache_owner(target, key, source)

        assert manager._resolve_cache_path(key, source, target_name=TARGET) == target
        assert manager._is_our_cache_file(target, key, source)


def test_partial_cache_size_counts_memory_parts():
    # 登记表里的分片进度按 part_size/total 校验后算已下字节；
    # 没有分片进度时返回 0（串行半截文件也一样）。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        target.write_bytes(CONTENT)
        manager._stamp_cache_owner(target, key, source)

        assert manager._partial_cache_size(target, source) == 0

        part_size = 100
        with manager._lock:
            entry = manager._cache_registry[str(target)]
            entry["parts"] = {0, 100, 200}
            entry["part_size"] = part_size
            entry["total"] = len(CONTENT)
        assert manager._partial_cache_size(target, source) == 300


# ---------------------------------------------------------------- 按 key 删除


def test_delete_cache_removes_target_named_by_meta():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source_a = _make_source("06.mkv", CONTENT)
        key_a = manager._cache_key("quark", source_a)
        target_a = Path(tmp) / TARGET
        _write_complete_cache(manager, target_a, key_a, source_a)
        source_b = _make_source("07.mkv", OTHER)
        key_b = manager._cache_key("quark", source_b)
        target_b = Path(tmp) / "另一部剧 S01E01.mkv"
        _write_complete_cache(manager, target_b, key_b, source_b)

        assert manager.delete_cache(key_a) == 1
        assert not target_a.exists()
        assert manager._read_cache_owner_key(target_a) == ""
        assert target_b.is_file()
        assert manager.delete_cache(key_a) == 0


def test_stamp_marks_partial_owner_but_not_complete():
    # 下载开始前的归属标记：断点续传文件也能按 key 认出，但不算完整缓存。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        target = Path(tmp) / TARGET
        target.write_bytes(CONTENT)
        manager._stamp_cache_owner(target, key, source)

        assert manager._read_cache_owner_key(target) == key
        assert not manager._validate_complete_cache(target, key, source)
        assert manager._is_our_cache_file(target, key, source)
        assert manager.delete_cache(key) == 1
        assert not target.exists()
        assert manager._read_cache_owner_key(target) == ""


# ---------------------------------------------------------------- residue


def test_residue_deletes_legacy_meta_but_keeps_target_named_main():
    # 1.12.19：目标名文件不再分组（文件名没有 hash，residue 认不出归属，
    # 一律不碰）；它旁边的旧版 .meta.json 是 litter，直接删。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        root = Path(tmp)
        main = root / TARGET
        _write_complete_cache(manager, main, key, source)
        _age(main, 3)
        legacy_meta = root / f"{TARGET}.meta.json"
        legacy_meta.write_text('{"cache_key": "%s"}' % key, encoding="utf-8")
        _age(legacy_meta, 3)

        result = scan_residue(root, now=time.time())

        assert result["deleted_files"] == 1
        assert main.exists()
        assert not legacy_meta.exists()
        assert result["errors"] == []


def test_residue_target_named_main_kept_regardless_of_active_key():
    # 目标名文件本来就不进分组，active_keys 与它无关；旧 meta 照样删。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        key = manager._cache_key("quark", source)
        root = Path(tmp)
        main = root / TARGET
        _write_complete_cache(manager, main, key, source)
        _age(main, 3)
        legacy_meta = root / f"{TARGET}.meta.json"
        legacy_meta.write_text('{"cache_key": "%s"}' % key, encoding="utf-8")
        _age(legacy_meta, 3)

        result = scan_residue(
            root, active_keys={f"cloud-transfer-{key}"}, now=time.time()
        )

        assert result["deleted_files"] == 1
        assert result["skipped_active"] == 0
        assert main.exists()
        assert not legacy_meta.exists()


def test_residue_deletes_legacy_parts_but_keeps_main():
    # 1.12.19：旧版 .parts.json 是 litter，直接删；主文件不进分组、不动。
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _touch(root, TARGET, 3, 100)
        parts = root / f"{TARGET}.parts.json"
        parts.write_text(
            '{"version": 1, "total": 100, "part_size": 50, "completed": []}',
            encoding="utf-8",
        )
        _age(parts, 3)

        result = scan_residue(root, now=time.time())

        assert result["deleted_files"] == 1
        assert main.exists()
        assert not parts.exists()


def test_residue_legacy_parts_deleted_even_when_name_active():
    # 旧 parts 文件直接删，不受 active_names 保护（保护只针对分组内的文件）。
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _touch(root, TARGET, 3, 100)
        parts = root / f"{TARGET}.parts.json"
        parts.write_text(
            '{"version": 1, "total": 100, "part_size": 50, "completed": []}',
            encoding="utf-8",
        )
        _age(parts, 3)

        result = scan_residue(root, active_names={TARGET}, now=time.time())

        assert result["deleted_files"] == 1
        assert result["skipped_active"] == 0
        assert main.exists()
        assert not parts.exists()


def test_residue_ignores_unrelated_target_named_file():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        keep = _touch(root, "movie.mkv", 5, 20)

        result = scan_residue(root, now=time.time())

        assert result["deleted_files"] == 0
        assert keep.exists()
        assert result["errors"] == []


# ---------------------------------------------------------------- 在途保护


def test_active_cache_names_returns_target_names():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("06.mkv")
        manager._tasks["cross:fake"] = {
            "id": "cross:fake", "status": "running",
            "source_provider": "quark", "source_file": source,
            "title": TARGET,
        }

        assert manager.active_cache_names() == {TARGET}
