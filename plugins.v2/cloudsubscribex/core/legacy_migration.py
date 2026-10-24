"""旧插件 ID（``CloudSubscribe``）→ 独立 ID（``CloudSubscribeX``）的一次性迁移。

1.6.49 起本插件改用独立 ID，与同名旧插件彻底解耦。老用户升级后不该手工搬运配置，
于是首次启动时由本模块把旧 ID 下的三样东西搬到新 ID：

* 插件配置（宿主库里按实例 ID 存的那一份）；
* SDK 持久化数据键（历史、签到、预算、待转存队列等）；
* 私有 SQLite 库（订阅登记、转存历史）。

两条硬约束：

1. 只在「本插件尚无配置、且没迁移过」时执行；此后再出现空配置属于用户主动重置，
   不回灌旧插件的陈旧配置；
2. 迁移出来的配置里 ``enabled`` 一律置 ``False``：旧插件此刻多半还在运行，两个插件
   同时转存会互相踩（同一集重复转存、互删中继文件），启用与否留给用户手动决定。
"""

from __future__ import annotations

import datetime
import shutil
from typing import Any, List, Optional, Tuple

from app.log import logger


class LegacyIdMigrator:
    """把旧插件 ID 下的配置、持久化数据与私有数据库搬到当前插件 ID。"""

    # 迁移留痕键：既是「已迁移过」的判据，也是事后排查用得到的事实记录
    MARKER_KEY = "legacy_id_migration"

    def __init__(
            self,
            owner,
            legacy_plugin_id: str = "CloudSubscribe",
            database_name: str = "cloudsubscribe.db",
    ):
        self._owner = owner
        self.legacy_plugin_id = legacy_plugin_id
        self.database_name = database_name

    def migrate(self, config: Optional[dict]) -> Optional[dict]:
        """执行迁移。

        :param config: 宿主传入的当前插件配置
        :return: 迁移后的配置；没有可迁移内容时原样返回入参
        """
        if config:
            return config
        if self._owner.get_data(self.MARKER_KEY):
            return config
        legacy_id = self.legacy_plugin_id
        try:
            legacy_config = self._owner.get_config(plugin_id=legacy_id)
        except Exception as error:  # noqa: BLE001 - 旧插件不存在或宿主接口异常都不该拦住启动
            logger.warning(f"读取旧插件 {legacy_id} 配置失败，跳过迁移：{error}")
            return config
        if not legacy_config:
            return config
        migrated = dict(legacy_config)
        migrated["enabled"] = False
        if self._owner.update_config(migrated) is False:
            logger.error(
                f"旧插件 {legacy_id} 配置迁移写入失败，本次仍以空配置启动"
                f"（旧插件未受影响，稍后重载本插件可重试）"
            )
            return config
        migrated_keys = self._migrate_data(legacy_id)
        database_copied = self._copy_database(legacy_id)
        self._owner.save_data(self.MARKER_KEY, {
            "from_plugin_id": legacy_id,
            "migrated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config_keys": len(migrated),
            "data_keys": len(migrated_keys),
            "database_copied": database_copied,
        })
        logger.info(
            f"已从旧插件 {legacy_id} 迁移到独立 ID {self._owner.__class__.__name__}"
            f"（{self._owner.plugin_version}）：配置 {len(migrated)} 项、"
            f"持久化数据 {len(migrated_keys)} 项、"
            f"私有数据库 {'已复制' if database_copied else '无需复制'}；"
            "为避免新旧插件重复转存，本插件已保持停用，请先停用或卸载旧插件，再手动启用本插件"
        )
        return migrated

    def _migrate_data(self, legacy_id: str) -> List[str]:
        """复制旧插件 ID 下的 SDK 持久化数据键。"""
        migrated: List[str] = []
        try:
            legacy_data = self._owner.get_data(plugin_id=legacy_id)
        except Exception as error:  # noqa: BLE001 - 数据迁移失败不影响「配置已迁移」这个事实
            logger.warning(f"读取旧插件 {legacy_id} 持久化数据失败：{error}")
            return migrated
        for key, value in self._data_items(legacy_data):
            try:
                self._owner.save_data(key, value)
                migrated.append(key)
            except Exception as error:  # noqa: BLE001 - 单个键失败不牵连其余键
                logger.warning(f"迁移旧插件数据键 {key} 失败：{error}")
        return migrated

    @staticmethod
    def _data_items(legacy_data: Any) -> List[Tuple[str, Any]]:
        """归一化旧插件全量数据：宿主新版返回 ORM 行列表，旧版返回字典。"""
        if isinstance(legacy_data, dict):
            return list(legacy_data.items())
        if isinstance(legacy_data, (list, tuple)):
            return [
                (row.key, row.value)
                for row in legacy_data
                if getattr(row, "key", None)
            ]
        return []

    def _copy_database(self, legacy_id: str) -> bool:
        """复制旧插件的私有 SQLite 文件（订阅登记、转存历史、缓存都在里面）。

        目标文件已存在且有内容时不动它：本插件的库一旦建好就以它为准，覆盖等于丢数据。
        只有「宿主提前建过一个 0 字节空文件」这种情形才允许用旧库顶上。

        :param legacy_id: 旧插件 ID
        :return: 是否真的复制了旧库
        """
        target = self._owner.get_data_path() / self.database_name
        try:
            if target.is_file() and target.stat().st_size > 0:
                return False
            source = self._owner.get_data_path(plugin_id=legacy_id) / self.database_name
            if not source.is_file():
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            # WAL/SHM 与主库是同一份快照，缺了会把最后一段事务留在 -wal 里
            for suffix in ("-wal", "-shm"):
                extra = source.with_name(source.name + suffix)
                if extra.is_file():
                    shutil.copy2(extra, target.with_name(target.name + suffix))
        except Exception as error:  # noqa: BLE001 - 复制失败不阻塞启动，插件会另建空库
            logger.error(f"复制旧插件 {legacy_id} 私有数据库失败：{error}")
            return False
        return True
