"""跨盘缓存文件命名（1.9.94）：下载时用真实文件名命名、后缀改回媒体扩展名。

固定契约（1.12.1 起目标名落盘，1.9.94 的源文件名命名改为无目标名时的退路）：
1. 有目标名时新名就是清洗后的目标名（如
   ``十万狂花入梦来 - S01E06 - 第6集.mkv``）；目标名不可用（空、无扩展名）
   时退回旧命名 ``cloud-transfer-<hash>-<清洗后的源文件名>``（如
   ``cloud-transfer-f0c9…-律界战争 S01E05.mp4``），源文件名也没有可用
   扩展名时退回旧名 ``cloud-transfer-<hash>.bin``；缓存身份记在 meta
   伴随文件里，不靠文件名；
2. 源文件名没有可用扩展名时退回旧名 ``cloud-transfer-<hash>.bin``；
3. 旧名缓存命中时在 cache_key 锁内改名迁移（含伴随文件），改名后
   ``_validate_complete_cache`` 仍通过；只读查找（cache_info）不改名；
4. ``delete_cache`` 只按 cache_key 删，新名与旧名都要删干净；
5. residue 正则同时匹配新名与旧名（含 `` (n)`` 变体与伴随文件），
   目录里其他文件一律不碰。
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
    path.write_bytes(CONTENT)
    manager._mark_complete_cache(path, cache_key, source)
    return path


# ---------------------------------------------------------------- 清洗规则


def test_sanitize_keeps_real_name_and_extension():
    sanitize = CrossTransferTaskManager._sanitize_cache_name
    assert sanitize("律界战争 S01E05.mp4") == "律界战争 S01E05.mp4"
    assert sanitize("show.S01E01.2160p.WEB-DL.mkv") == "show.S01E01.2160p.WEB-DL.mkv"
    assert sanitize("movie.MP4") == "movie.MP4"


def test_sanitize_replaces_illegal_chars_and_collapses_whitespace():
    sanitize = CrossTransferTaskManager._sanitize_cache_name
    assert sanitize('a/b\\c:d*e?f"g<h>i|j.mp4') == "a_b_c_d_e_f_g_h_i_j.mp4"
    assert sanitize("show   S01E01.mkv") == "show S01E01.mkv"
    assert sanitize("  show S01E01.mkv  ") == "show S01E01.mkv"


def test_sanitize_truncates_long_name_keeps_extension():
    sanitize = CrossTransferTaskManager._sanitize_cache_name
    cleaned = sanitize("x" * 150 + ".mp4")
    assert len(cleaned) == 120
    assert cleaned.endswith(".mp4")


def test_sanitize_truncates_long_cjk_name_within_byte_budget():
    sanitize = CrossTransferTaskManager._sanitize_cache_name
    cleaned = sanitize("律" * 100 + ".mp4")
    assert cleaned.endswith(".mp4")
    assert len(cleaned) <= 120
    full = f"cloud-transfer-{'0' * 32}-{cleaned}"
    assert len(full.encode("utf-8")) <= 200


def test_sanitize_no_extension_falls_back():
    sanitize = CrossTransferTaskManager._sanitize_cache_name
    assert sanitize("README") == ""
    assert sanitize("file.") == ""
    assert sanitize("") == ""
    assert sanitize("   ") == ""
    assert sanitize(".mp4") == ""


# ---------------------------------------------------------------- 命名出处


def test_cache_path_uses_source_name_and_legacy_without_source():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        assert manager._cache_path(key, source).name == (
            f"cloud-transfer-{key}-律界战争 S01E05.mp4"
        )
        assert manager._cache_path(key).name == f"cloud-transfer-{key}.bin"
        no_ext = _make_source("README")
        assert manager._cache_path(key, no_ext).name == f"cloud-transfer-{key}.bin"


def test_temporary_path_uses_target_name_not_source_name():
    # 1.12.1 起设计反转：下载直接用目标名落盘（交给 MP 整理时文件名本身
    # 就能被识别），源文件名命名只作目标名不可用时的退路。
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("源文件 S01E01.mkv")
        task = {
            "source_provider": "quark", "source_file": source,
            "title": "目标剧 S01E01.mkv",
        }
        path = manager._temporary_path(task)
        assert path == str(Path(tmp) / "目标剧 S01E01.mkv")
        assert "源文件 S01E01.mkv" not in path


# ---------------------------------------------------------------- 旧名迁移


def test_legacy_cache_renamed_on_resolve_and_still_valid():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        _write_complete_cache(manager, legacy, key, source)
        parts = manager._cache_parts_path(legacy)
        parts.write_text('{"version": 1}', encoding="utf-8")
        meta_tmp = manager._cache_meta_path(legacy).with_name(
            f"{manager._cache_meta_path(legacy).name}.tmp"
        )
        meta_tmp.write_text("{}", encoding="utf-8")

        resolved = manager._resolve_cache_path(key, source)

        new_path = manager._cache_path(key, source)
        assert resolved == new_path
        assert new_path.is_file()
        assert not legacy.exists()
        assert manager._cache_meta_path(new_path).is_file()
        assert manager._cache_parts_path(new_path).is_file()
        assert manager._cache_meta_path(new_path).with_name(
            f"{manager._cache_meta_path(new_path).name}.tmp"
        ).is_file()
        assert not manager._cache_meta_path(legacy).exists()
        assert not parts.exists()
        assert not meta_tmp.exists()
        assert manager._validate_complete_cache(new_path, key, source)


def test_resolve_prefers_new_name_when_both_exist():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        _write_complete_cache(manager, legacy, key, source)
        new_path = manager._cache_path(key, source)
        new_path.write_bytes(CONTENT)

        assert manager._resolve_cache_path(key, source) == new_path
        assert new_path.is_file()
        assert legacy.exists()


def test_lookup_cache_path_does_not_rename():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        _write_complete_cache(manager, legacy, key, source)

        assert manager._lookup_cache_path(key, source) == legacy
        assert legacy.is_file()
        assert not manager._cache_path(key, source).exists()


def test_cache_info_reports_legacy_complete_cache_without_renaming():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        _write_complete_cache(manager, legacy, key, source)

        info = manager.cache_info("quark", source)

        assert info["cache_key"] == key
        assert info["cache_status"] == "complete"
        assert legacy.is_file()
        assert not manager._cache_path(key, source).exists()


# ---------------------------------------------------------------- 按 key 删除


def test_delete_cache_removes_both_namings():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source_a = _make_source("律界战争 S01E05.mp4")
        key_a = manager._cache_key("quark", source_a)
        legacy_a = manager._legacy_cache_path(key_a)
        _write_complete_cache(manager, legacy_a, key_a, source_a)
        manager._cache_parts_path(legacy_a).write_text("{}", encoding="utf-8")
        new_a = manager._cache_path(key_a, source_a)
        _write_complete_cache(manager, new_a, key_a, source_a)
        source_b = _make_source("另一部剧 S01E01.mkv")
        key_b = manager._cache_key("quark", source_b)
        legacy_b = manager._legacy_cache_path(key_b)
        legacy_b.write_bytes(CONTENT)

        assert manager.delete_cache(key_a) == 5
        assert not legacy_a.exists()
        assert not manager._cache_meta_path(legacy_a).exists()
        assert not manager._cache_parts_path(legacy_a).exists()
        assert not new_a.exists()
        assert not manager._cache_meta_path(new_a).exists()
        assert legacy_b.is_file()
        assert manager.delete_cache(key_a) == 0
        assert manager.delete_cache("not-a-key") == 0
        assert manager.delete_cache("z" * 32) == 0


def test_delete_cache_skips_in_use_cache():
    with TemporaryDirectory() as tmp:
        manager = _make_manager(tmp)
        source = _make_source("律界战争 S01E05.mp4")
        key = manager._cache_key("quark", source)
        legacy = manager._legacy_cache_path(key)
        legacy.write_bytes(CONTENT)

        manager._acquire_cache_ref(key)
        try:
            assert manager.delete_cache(key) == 0
            assert legacy.is_file()
        finally:
            manager._release_cache_ref(key)
        assert manager.delete_cache(key) == 1
        assert not legacy.exists()


# ---------------------------------------------------------------- residue


def _touch(root, name, age_days, size=10):
    path = Path(root) / name
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_residue_matches_new_naming_and_variants():
    key_a = "cloud-transfer-" + "a" * 32
    key_b = "cloud-transfer-" + "b" * 32
    key_c = "cloud-transfer-" + "c" * 32
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_main = _touch(root, f"{key_a}-律界战争 S01E05.mp4", 3, 100)
        old_meta = _touch(root, f"{key_a}-律界战争 S01E05.mp4.meta.json", 3, 5)
        old_parts = _touch(root, f"{key_a}-律界战争 S01E05.mp4.parts.json.tmp", 3, 5)
        old_dup = _touch(root, f"{key_a}-show (1).mp4", 3, 50)
        old_legacy = _touch(root, f"{key_c}.bin", 3, 20)
        fresh = _touch(root, f"{key_b}-新剧 S01E01.mkv", 0.1, 20)
        keep = _touch(root, "movie.mkv", 5, 20)
        no_ext = _touch(root, f"{key_b}-foo", 5, 20)

        result = scan_residue(root, now=time.time())

        assert result["deleted_files"] == 5
        assert result["skipped_recent"] == 1
        assert not old_main.exists()
        assert not old_meta.exists()
        assert not old_parts.exists()
        assert not old_dup.exists()
        assert not old_legacy.exists()
        assert fresh.exists()
        assert keep.exists()
        assert no_ext.exists()
        assert result["errors"] == []


def test_residue_groups_new_main_and_companions_by_key():
    key_a = "cloud-transfer-" + "a" * 32
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        old_main = _touch(root, f"{key_a}-律界战争 S01E05.mp4", 3, 100)
        fresh_meta = _touch(root, f"{key_a}-律界战争 S01E05.mp4.meta.json", 0.1, 5)

        result = scan_residue(root, now=time.time())

        # 同组最新 mtime 未过期，整组跳过
        assert result["deleted_files"] == 0
        assert result["skipped_recent"] == 1
        assert old_main.exists()
        assert fresh_meta.exists()


def test_residue_new_naming_never_matches_unrelated():
    key_a = "cloud-transfer-" + "a" * 32
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        decoy = _touch(root, f"{key_a}.bin.bak", 5)
        short = _touch(root, "cloud-transfer-abc-show.mp4", 5)

        result = scan_residue(root, now=time.time())

        assert result["deleted_files"] == 0
        assert decoy.exists()
        assert short.exists()
