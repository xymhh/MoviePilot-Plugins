"""1.32.0：真名缓存残留清理（P0）+ 缓存目录/磁盘水位巡检（P1）单测。

背景：2026-11-24 德国 VPS 上 `/downloads` 堆了 38GB 失败残留，插件侧
没有任何机制清理——残留清理只认 `cloud-transfer-<hash>` 前缀，而 1.12.1
起下载即用目标名落盘；插件也没有任何磁盘监控。

覆盖：
1. `scan_residue(real_name_min_age_seconds=...)`：超期真名媒体才删，
   在途名单 / `.aria2` 伴随文件 / 非视频扩展名一律不动；默认关闭。
2. `ResidueService`：漏斗里仍在自动重试（queued/processing/failed）的行
   指向的文件不删；保留期读了配置就按配置算，0 表示关闭。
3. `CacheWatchService`：超阈值报警一次、同状态按冷却去重、恢复后发恢复
   通知、首次正常不打扰、缓存目录不存在时不抛异常。
"""

import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from plugin_env import ensure_package, install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks()
ensure_package("cloudsubscribex")
ensure_package("cloudsubscribex.core")
ensure_package("cloudsubscribex.core.services")
# 用占位包（保留真实 __path__）而不是 register_mock：services/residue.py 里
# `from ..aria2 import resolve_data_dir` 只需要这个符号，但把 aria2 换成
# `__path__=[]` 的 MagicMock 会让后续测试加载 aria2 子模块时全部 ImportError。
ensure_package("cloudsubscribex.core.aria2")

delegation_module = load_module(
    "cloudsubscribex.core.delegation",
    "plugins.v2/cloudsubscribex/core/delegation.py",
)
residue_module = load_module(
    "cloudsubscribex.core.residue",
    "plugins.v2/cloudsubscribex/core/residue.py",
)
residue_service_module = load_module(
    "cloudsubscribex.core.services.residue",
    "plugins.v2/cloudsubscribex/core/services/residue.py",
)
cache_watch_module = load_module(
    "cloudsubscribex.core.services.cache_watch",
    "plugins.v2/cloudsubscribex/core/services/cache_watch.py",
)


def resolve_data_dir(download_path, plugin_data_path):
    """测试替身：缓存目录就是工作目录，避免依赖真实 aria2 包。"""
    text = str(download_path or "").strip()
    return Path(text) if text else Path("/tmp/cloudsubscribex-aria2")


# services/residue.py 用 from ..aria2 import resolve_data_dir 取了模块级名字，
# 直接替换它持有的那个函数，比 mock 整个包干净。
residue_service_module.resolve_data_dir = resolve_data_dir

REAL_NAME = "挖 {tmdb-290826} - S01E01 - 第1集.mp4"
DAY_HOURS = 24


def _touch(root, name, age_hours, size=10):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    stamp = time.time() - age_hours * 3600
    os.utime(path, (stamp, stamp))
    return path


class RealNameResidueScanTest(unittest.TestCase):
    def test_expired_real_name_media_is_deleted_and_counted(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=500)

            result = residue_module.scan_residue(
                tmp, real_name_min_age_seconds=DAY_HOURS * 3600
            )

            self.assertFalse(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 1)
            self.assertEqual(result["real_name_freed_bytes"], 500)
            self.assertEqual(result["deleted_files"], 1)
            self.assertEqual(result["deleted_real_names"], [REAL_NAME])

    def test_fresh_real_name_media_is_kept(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, 1, size=500)

            result = residue_module.scan_residue(
                tmp, real_name_min_age_seconds=DAY_HOURS * 3600
            )

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)
            self.assertEqual(result["real_name_skipped_recent"], 1)

    def test_active_names_are_never_deleted(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=500)

            result = residue_module.scan_residue(
                tmp,
                active_names=[REAL_NAME],
                real_name_min_age_seconds=DAY_HOURS * 3600,
            )

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)
            self.assertEqual(result["skipped_active"], 1)

    def test_aria2_sidecar_protects_real_name_file(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=500)
            _touch(tmp, f"{REAL_NAME}.aria2", DAY_HOURS + 6, size=5)

            result = residue_module.scan_residue(
                tmp, real_name_min_age_seconds=DAY_HOURS * 3600
            )

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)

    def test_non_media_files_are_untouched(self):
        with TemporaryDirectory() as tmp:
            subtitle = _touch(tmp, "挖 {tmdb-290826} - S01E01.ass", DAY_HOURS + 6)
            text = _touch(tmp, "notes.txt", DAY_HOURS + 6)
            archive = _touch(tmp, "backup.zip", DAY_HOURS + 6)

            result = residue_module.scan_residue(
                tmp, real_name_min_age_seconds=DAY_HOURS * 3600
            )

            self.assertTrue(subtitle.exists())
            self.assertTrue(text.exists())
            self.assertTrue(archive.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)

    def test_disabled_by_default(self):
        """不传参数时行为不变：真名媒体一律不碰（旧版本语义）。"""
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS * 10, size=500)

            result = residue_module.scan_residue(tmp)

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)


class _FakeOwner:
    """最小宿主：只提供组件真正会读的属性与方法。"""

    def __init__(self, download_path, data_path, hours=24, rows=None):
        self._cross_transfer_download_path = str(download_path or "")
        self._data_path = str(data_path or "")
        self._cross_transfer_real_name_retention_hours = hours
        self._rows = list(rows or [])
        self._cross_transfer_manager = None
        self._notification_type = "Plugin"
        self.sent = []

    def get_data_path(self):
        return self._data_path

    def post_message(self, **kwargs):
        self.sent.append(kwargs)
        return True

    def _get_data_store(self):
        def _list_by_status(statuses, limit=200):
            wanted = tuple(statuses or ())
            return [row for row in self._rows if row.get("status") in wanted]

        repository = SimpleNamespace(list_by_status=_list_by_status)
        return SimpleNamespace(
            repositories=SimpleNamespace(funnel_queue=repository)
        )


class ResidueServiceRealNameTest(unittest.TestCase):
    def _service(self, download_path, data_path, hours=24, rows=None):
        owner = _FakeOwner(download_path, data_path, hours=hours, rows=rows)
        return residue_service_module.ResidueService(owner), owner

    def test_service_deletes_expired_real_name_file(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=700)
            service, _owner = self._service(tmp, tmp)

            result = service.cleanup_transfer_residue("测试")

            self.assertFalse(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 1)
            self.assertEqual(result["real_name_freed_bytes"], 700)

    def test_service_keeps_file_referenced_by_retrying_funnel_row(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=700)
            rows = [{"status": "failed", "staging_name": REAL_NAME}]
            service, _owner = self._service(tmp, tmp, rows=rows)

            result = service.cleanup_transfer_residue("测试")

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)

    def test_held_row_does_not_protect_local_file(self):
        """held 表示不再自动重试，本地副本到期就清（否则永久占盘）。"""
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS + 6, size=700)
            rows = [{"status": "held", "staging_name": REAL_NAME}]
            service, _owner = self._service(tmp, tmp, rows=rows)

            result = service.cleanup_transfer_residue("测试")

            self.assertFalse(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 1)

    def test_zero_hours_disables_real_name_cleanup(self):
        with TemporaryDirectory() as tmp:
            path = _touch(tmp, REAL_NAME, DAY_HOURS * 10, size=700)
            service, _owner = self._service(tmp, tmp, hours=0)

            result = service.cleanup_transfer_residue("测试")

            self.assertTrue(path.exists())
            self.assertEqual(result["real_name_deleted_files"], 0)

    def test_invalid_hours_fall_back_to_default(self):
        with TemporaryDirectory() as tmp:
            service, _owner = self._service(tmp, tmp, hours="oops")
            self.assertEqual(
                service._real_name_min_age_seconds(),
                residue_module.DEFAULT_REAL_NAME_MIN_AGE_SECONDS,
            )


class CacheWatchTest(unittest.TestCase):
    def _service(self, cache_dir, data_dir):
        owner = _FakeOwner(cache_dir, data_dir)
        return cache_watch_module.CacheWatchService(owner), owner

    def test_warn_when_cache_dir_exceeds_threshold(self):
        with TemporaryDirectory() as tmp:
            _touch(tmp, REAL_NAME, 1, size=4096)
            service, owner = self._service(tmp, tmp)
            with mock.patch.object(cache_watch_module, "CACHE_DIR_ALERT_BYTES", 1024):
                result = service.check("测试")

            self.assertEqual(result["level"], "warn")
            self.assertTrue(result["notified"])
            self.assertEqual(len(owner.sent), 1)
            self.assertIn("缓存目录", owner.sent[0]["title"])

    def test_cooldown_suppresses_repeat_alert(self):
        with TemporaryDirectory() as tmp:
            _touch(tmp, REAL_NAME, 1, size=4096)
            service, owner = self._service(tmp, tmp)
            with mock.patch.object(cache_watch_module, "CACHE_DIR_ALERT_BYTES", 1024):
                service.check("测试")
                second = service.check("测试")

            self.assertEqual(second["level"], "warn")
            self.assertFalse(second["notified"])
            self.assertEqual(len(owner.sent), 1)

    def test_recovery_sends_recovery_notice(self):
        with TemporaryDirectory() as tmp:
            big = _touch(tmp, REAL_NAME, 1, size=4096)
            service, owner = self._service(tmp, tmp)
            healthy = SimpleNamespace(total=1000, free=800, used=200)
            with mock.patch.object(cache_watch_module, "CACHE_DIR_ALERT_BYTES", 1024), \
                    mock.patch.object(cache_watch_module.shutil, "disk_usage", return_value=healthy):
                service.check("测试")
                big.unlink()
                result = service.check("测试")

            self.assertEqual(result["level"], "ok")
            self.assertTrue(result["notified"])
            self.assertEqual(len(owner.sent), 2)
            self.assertIn("恢复", owner.sent[1]["title"])

    def test_first_healthy_run_does_not_notify(self):
        with TemporaryDirectory() as tmp:
            service, owner = self._service(tmp, tmp)
            healthy = SimpleNamespace(total=1000, free=800, used=200)
            with mock.patch.object(cache_watch_module.shutil, "disk_usage", return_value=healthy):
                result = service.check("测试")

            self.assertEqual(result["level"], "ok")
            self.assertFalse(result["notified"])
            self.assertEqual(owner.sent, [])

    def test_missing_cache_dir_is_survivable(self):
        with TemporaryDirectory() as tmp:
            service, owner = self._service(os.path.join(tmp, "nope"), tmp)
            result = service.check("测试")

            self.assertEqual(result["cache_bytes"], 0)
            self.assertIn("level", result)

    def test_low_disk_ratio_triggers_warn(self):
        with TemporaryDirectory() as tmp:
            service, owner = self._service(tmp, tmp)
            usage = SimpleNamespace(total=1000, free=50, used=950)
            with mock.patch.object(
                cache_watch_module.shutil, "disk_usage", return_value=usage
            ):
                result = service.check("测试")

            self.assertEqual(result["level"], "warn")
            self.assertTrue(result["notified"])


if __name__ == "__main__":
    unittest.main()
