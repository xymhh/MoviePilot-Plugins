"""1.6.49 插件 ID 独立化：从旧 ID（CloudSubscribe）迁移的单测。

固定契约：

组1 触发条件：
  1. 本插件无配置 + 旧插件有配置 → 执行迁移，配置写入本插件且 ``enabled`` 强制为 False；
  2. 本插件已有配置 → 直接返回，连旧插件配置都不读（不覆盖用户当前配置）；
  3. 本插件无配置 + 旧插件也无配置 → 不写任何东西（首次安装的干净路径）；
  4. 已迁移过（留痕键存在）→ 不再回灌旧插件配置（用户此后清空配置属主动重置）；
  5. 读旧配置抛异常 → 只告警、不中断启动。

组2 数据搬运：
  6. 旧插件持久化数据键全量复制（历史/签到/预算等）；
  7. 宿主返回 ORM 行列表（``.key``/``.value``）与旧版字典两种形态都能搬；
  8. 单个键写入失败只告警，其余键继续搬。

组3 私有 SQLite 库：
  9. 目标库不存在 → 复制旧库（含 ``-wal``/``-shm`` 同快照）；
  10. 目标库已有内容 → 不覆盖（本插件的库为准，覆盖等于丢数据）；
  11. 目标库存在但为 0 字节（宿主提前建的空文件）→ 用旧库顶上；
  12. 旧库不存在 → 不报错，标记为未复制。

组4 留痕：
  13. 写入留痕键 ``legacy_id_migration``，含来源 ID、配置项数、数据键数、是否复制了库。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from plugin_env import install_app_mocks, load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks("cloudsubscribex", "cloudsubscribex.core")
logger_ref = sys.modules["app.log"].logger

migration_module = load_module(
    "cloudsubscribex.core.legacy_migration",
    "plugins.v2/cloudsubscribex/core/legacy_migration.py",
)
LegacyIdMigrator = migration_module.LegacyIdMigrator


class _Row:
    """宿主新版 ``get_data(plugin_id=...)`` 返回的 ORM 行形态。"""

    def __init__(self, key, value):
        self.key = key
        self.value = value


class _FakeOwner:
    """按 SDK 契约实现的宿主桩：只记录调用，不碰真实库。"""

    plugin_version = "1.6.49"

    def __init__(self, data_root: Path, legacy_config=None, legacy_data=None,
                 legacy_db: bytes = None, target_db: bytes = None,
                 legacy_config_error: Exception = None,
                 fail_keys: tuple = ()):
        self._data_root = data_root
        self.legacy_config = legacy_config
        self.legacy_data = legacy_data
        self.legacy_config_error = legacy_config_error
        self.fail_keys = fail_keys
        self.config = None
        self.saved = {}
        self.calls = []
        self.update_config_result = None
        legacy_dir = data_root / "CloudSubscribe"
        target_dir = data_root / "CloudSubscribeX"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        target_dir.mkdir(parents=True, exist_ok=True)
        if legacy_db is not None:
            (legacy_dir / "cloudsubscribe.db").write_bytes(legacy_db)
        if target_db is not None:
            (target_dir / "cloudsubscribe.db").write_bytes(target_db)

    # ---- SDK 端口 ----
    def get_data(self, key=None, plugin_id=None):
        self.calls.append(("get_data", key, plugin_id))
        if key is not None:
            return self.saved.get(key)
        if plugin_id == "CloudSubscribe":
            return self.legacy_data
        return None

    def save_data(self, key, value, plugin_id=None):
        self.calls.append(("save_data", key, plugin_id))
        if key in self.fail_keys:
            raise RuntimeError(f"写入失败：{key}")
        self.saved[key] = value

    def get_config(self, plugin_id=None):
        self.calls.append(("get_config", plugin_id))
        if self.legacy_config_error is not None:
            raise self.legacy_config_error
        if plugin_id == "CloudSubscribe":
            return self.legacy_config
        return self.config

    def update_config(self, config, plugin_id=None):
        self.calls.append(("update_config", dict(config), plugin_id))
        if self.update_config_result is False:
            return False
        self.config = dict(config)
        return True

    def get_data_path(self, plugin_id=None):
        # 与运行期一致：不带 plugin_id 时按插件类名取目录，即独立 ID
        name = plugin_id or "CloudSubscribeX"
        path = self._data_root / name
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestLegacyIdMigration(unittest.TestCase):
    def setUp(self):
        logger_ref.reset_mock()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data_root = Path(self._tmp.name)

    def _owner(self, **kwargs):
        return _FakeOwner(self.data_root, **kwargs)

    # ---- 组1 触发条件 ----
    def test_migrates_config_and_forces_disabled(self):
        owner = self._owner(legacy_config={"enabled": True, "notify": True, "cron": "0 8 * * *"})
        result = LegacyIdMigrator(owner).migrate(None)
        self.assertEqual(result["enabled"], False)
        self.assertEqual(result["notify"], True)
        self.assertEqual(owner.config["enabled"], False)
        self.assertEqual(owner.config["cron"], "0 8 * * *")

    def test_skips_when_current_config_present(self):
        owner = self._owner(legacy_config={"enabled": True})
        current = {"enabled": False, "cron": "0 9 * * *"}
        result = LegacyIdMigrator(owner).migrate(current)
        self.assertEqual(result, current)
        self.assertEqual([call for call in owner.calls if call[0] == "get_config"], [])
        self.assertEqual([call for call in owner.calls if call[0] == "update_config"], [])

    def test_no_legacy_config_means_no_write(self):
        owner = self._owner(legacy_config=None)
        result = LegacyIdMigrator(owner).migrate(None)
        self.assertIsNone(result)
        self.assertEqual([call for call in owner.calls if call[0] == "update_config"], [])
        self.assertEqual(owner.saved, {})

    def test_marker_prevents_second_migration(self):
        owner = self._owner(legacy_config={"enabled": True})
        owner.saved[LegacyIdMigrator.MARKER_KEY] = {"from_plugin_id": "CloudSubscribe"}
        result = LegacyIdMigrator(owner).migrate(None)
        self.assertIsNone(result)
        self.assertEqual([call for call in owner.calls if call[0] == "get_config"], [])
        self.assertIsNone(owner.config)

    def test_legacy_config_read_error_is_swallowed(self):
        owner = self._owner(legacy_config_error=RuntimeError("宿主接口异常"))
        result = LegacyIdMigrator(owner).migrate(None)
        self.assertIsNone(result)
        self.assertTrue(logger_ref.warning.called)

    # ---- 组2 数据搬运 ----
    def test_migrates_all_data_keys_from_dict(self):
        owner = self._owner(
            legacy_config={"enabled": True},
            legacy_data={"history": [1, 2], "p115_checkin_history": {"days": 3}},
        )
        LegacyIdMigrator(owner).migrate(None)
        self.assertEqual(owner.saved["history"], [1, 2])
        self.assertEqual(owner.saved["p115_checkin_history"], {"days": 3})

    def test_migrates_orm_rows_and_skips_keyless(self):
        owner = self._owner(
            legacy_config={"enabled": True},
            legacy_data=[_Row("history", [1]), _Row(None, "坏行"), _Row("checkin", {"a": 1})],
        )
        LegacyIdMigrator(owner).migrate(None)
        self.assertEqual(owner.saved["history"], [1])
        self.assertEqual(owner.saved["checkin"], {"a": 1})
        self.assertNotIn(None, owner.saved)

    def test_single_key_failure_does_not_stop_others(self):
        owner = self._owner(
            legacy_config={"enabled": True},
            legacy_data={"history": [1], "checkin": {"a": 1}},
            fail_keys=("history",),
        )
        LegacyIdMigrator(owner).migrate(None)
        self.assertEqual(owner.saved["checkin"], {"a": 1})
        self.assertNotIn("history", owner.saved)
        self.assertTrue(logger_ref.warning.called)

    # ---- 组3 私有 SQLite 库 ----
    def test_copies_legacy_database_with_wal_and_shm(self):
        owner = self._owner(legacy_config={"enabled": True}, legacy_db=b"sqlite-main")
        (self.data_root / "CloudSubscribe" / "cloudsubscribe.db-wal").write_bytes(b"wal")
        (self.data_root / "CloudSubscribe" / "cloudsubscribe.db-shm").write_bytes(b"shm")
        copied = LegacyIdMigrator(owner)._copy_database("CloudSubscribe")
        self.assertTrue(copied)
        target_dir = self.data_root / "CloudSubscribeX"
        self.assertEqual((target_dir / "cloudsubscribe.db").read_bytes(), b"sqlite-main")
        self.assertEqual((target_dir / "cloudsubscribe.db-wal").read_bytes(), b"wal")
        self.assertEqual((target_dir / "cloudsubscribe.db-shm").read_bytes(), b"shm")

    def test_does_not_overwrite_existing_target_database(self):
        owner = self._owner(
            legacy_config={"enabled": True}, legacy_db=b"sqlite-old", target_db=b"sqlite-new"
        )
        self.assertFalse(LegacyIdMigrator(owner)._copy_database("CloudSubscribe"))
        self.assertEqual(
            (self.data_root / "CloudSubscribeX" / "cloudsubscribe.db").read_bytes(), b"sqlite-new"
        )

    def test_empty_target_database_is_replaced(self):
        owner = self._owner(
            legacy_config={"enabled": True}, legacy_db=b"sqlite-old", target_db=b""
        )
        self.assertTrue(LegacyIdMigrator(owner)._copy_database("CloudSubscribe"))
        self.assertEqual(
            (self.data_root / "CloudSubscribeX" / "cloudsubscribe.db").read_bytes(), b"sqlite-old"
        )

    def test_missing_legacy_database_is_not_an_error(self):
        owner = self._owner(legacy_config={"enabled": True})
        self.assertFalse(LegacyIdMigrator(owner)._copy_database("CloudSubscribe"))
        self.assertTrue(logger_ref.error.called is False)

    # ---- 组4 留痕 ----
    def test_marker_records_migration_facts(self):
        owner = self._owner(
            legacy_config={"enabled": True, "notify": False},
            legacy_data={"history": [1]},
            legacy_db=b"sqlite-old",
        )
        LegacyIdMigrator(owner).migrate(None)
        marker = owner.saved[LegacyIdMigrator.MARKER_KEY]
        self.assertEqual(marker["from_plugin_id"], "CloudSubscribe")
        self.assertEqual(marker["config_keys"], 2)
        self.assertEqual(marker["data_keys"], 1)
        self.assertTrue(marker["database_copied"])
        self.assertTrue(marker["migrated_at"])


if __name__ == "__main__":
    unittest.main()
