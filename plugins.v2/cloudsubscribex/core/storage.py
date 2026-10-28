"""CloudSubscribe 插件业务数据与可恢复运行状态的 ORM 入口。"""

from __future__ import annotations

import copy
import datetime
import json
from threading import RLock
from typing import Any, Dict, List, Optional

from app.log import logger

from .database import CloudSubscribeDatabaseManager, CloudSubscribeRepositories


class CloudSubscribeDataStore:
    """业务数据和可恢复运行状态按职责写入独立数据库。"""

    HISTORY_KEY = "history"
    OFFLINE_PENDING_KEY = "pending_offline_strm"
    SCHEDULE_KEY = "checkin_schedule_state"
    RUNTIME_KEYS = {
        "account_info_cache",
    }

    def __init__(self, owner):
        self._lock = RLock()
        self._initialized = False
        # 历史台账快照缓存：一次同步会反复取 history，避免每次都全表反序列化。
        self._history_cache: Optional[List[Dict[str, Any]]] = None
        self.manager = CloudSubscribeDatabaseManager(
            owner.get_data_path() / "cloudsubscribe.db"
        )
        self.repositories = CloudSubscribeRepositories(self.manager)

    @staticmethod
    def _checkin_provider(key: str) -> str:
        return key[:-len("_checkin_history")]

    @staticmethod
    def _budget_provider(key: str) -> str:
        return key[:-len("_sub_points_history")]

    @staticmethod
    def _auth_provider(key: str) -> str:
        return key[:-len("_auth_session")]

    @classmethod
    def is_persistent_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return bool(
            normalized in {
                cls.HISTORY_KEY,
                cls.OFFLINE_PENDING_KEY,
                cls.SCHEDULE_KEY,
            }
            or normalized.endswith("_checkin_history")
            or normalized.endswith("_sub_points_history")
        )

    @classmethod
    def is_runtime_key(cls, key: str) -> bool:
        normalized = str(key or "")
        return normalized in cls.RUNTIME_KEYS or normalized.endswith(
            "_auth_session"
        )

    @classmethod
    def handles(cls, key: str) -> bool:
        return cls.is_persistent_key(key) or cls.is_runtime_key(key)

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            self.manager.open()
            self.manager.init_db()
            self.manager.update_db()
            repaired = self.repositories.history.repair_group_keys()
            self._migrate_legacy_sha1_cache()
            if repaired:
                logger.info(f"CloudSubscribe 历史媒体分组已修复：{repaired} 条")
            self._initialized = True

    def _migrate_legacy_sha1_cache(self) -> None:
        """把 1.7.97 的 source_sha1_cache.json 一次性并入数据库表。"""
        legacy = self.manager.db_path.parent / "source_sha1_cache.json"
        if not legacy.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - 旧缓存损坏按空处理
            logger.debug(f"旧源哈希缓存读取失败，跳过迁移：{error}")
            raw = {}
        migrated = 0
        if isinstance(raw, dict):
            for key, entry in raw.items():
                if not isinstance(entry, dict):
                    continue
                sha1 = str(entry.get("sha1") or "").strip()
                if not str(key or "").strip() or not sha1:
                    continue
                ok = self.repositories.source_sha1.put(
                    key,
                    sha1,
                    size=int(entry.get("size") or 0),
                    name=str(entry.get("name") or ""),
                    updated_at=self._legacy_timestamp(entry.get("updated_at")),
                )
                migrated += 1 if ok else 0
        try:
            legacy.replace(legacy.with_suffix(".json.migrated"))
        except OSError as error:
            logger.debug(f"旧源哈希缓存改名失败：{error}", exc_info=True)
        if migrated:
            logger.info(f"源文件哈希缓存已并入数据库：{migrated} 条")

    @staticmethod
    def _legacy_timestamp(value: Any) -> int:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            saved = datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
        try:
            from app.core.config import settings
            from pytz import timezone

            return int(timezone(settings.TZ).localize(saved).timestamp())
        except Exception:  # noqa: BLE001 - 时区不可用时退回本机时区
            return int(saved.timestamp())

    def _load_database(self, key: str) -> Any:
        if key == self.HISTORY_KEY:
            if self._history_cache is None:
                self._history_cache = self.repositories.history.list_all()
            return copy.deepcopy(self._history_cache)
        if key == self.OFFLINE_PENDING_KEY:
            return self.repositories.offline.load_all()
        if key == self.SCHEDULE_KEY:
            return self.repositories.schedule.load()
        if key.endswith("_checkin_history"):
            return self.repositories.checkin.list_provider(
                self._checkin_provider(key)
            )
        if key.endswith("_sub_points_history"):
            return self.repositories.budget.load_provider(
                self._budget_provider(key)
            )
        if key == "account_info_cache":
            return self.repositories.account.load_all()
        if key.endswith("_auth_session"):
            return self.repositories.auth.load_provider(
                self._auth_provider(key)
            )
        return None

    def _save_database(self, key: str, value: Any) -> None:
        if key == self.HISTORY_KEY:
            self.repositories.history.replace_all(value or [])
            self._history_cache = None
        elif key == self.OFFLINE_PENDING_KEY:
            self.repositories.offline.replace_all(value or {})
        elif key == self.SCHEDULE_KEY:
            self.repositories.schedule.save(value or {})
        elif key.endswith("_checkin_history"):
            self.repositories.checkin.replace_provider(
                self._checkin_provider(key), value or []
            )
        elif key.endswith("_sub_points_history"):
            self.repositories.budget.replace_provider(
                self._budget_provider(key), value or {}
            )
        elif key == "account_info_cache":
            self.repositories.account.replace_all(value or {})
        elif key.endswith("_auth_session"):
            self.repositories.auth.save_provider(
                self._auth_provider(key), value or {}
            )

    def load(self, key: str) -> Any:
        normalized = str(key or "")
        with self._lock:
            if not self.handles(normalized):
                return None
            self.initialize()
            return self._load_database(normalized)

    def save(self, key: str, value: Any) -> None:
        normalized = str(key or "")
        with self._lock:
            if not self.handles(normalized):
                raise ValueError(f"未声明的数据键不能持久化：{normalized}")
            self.initialize()
            self._save_database(normalized, value)

    def query_history_page(self, **kwargs) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.history.query_group_page(**kwargs)

    def history_overview(
            self, today: str, recent_limit: int = 20
    ) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.history.overview(today, recent_limit)

    def history_summary(self, today: str) -> Dict[str, int]:
        self.initialize()
        return self.repositories.history.summary(today)

    def load_checkin_snapshot(self) -> Dict[str, Any]:
        """在同一只读会话中加载全部签到历史和调度状态。"""
        self.initialize()
        with self.manager.session() as db:
            return {
                "histories": self.repositories.checkin.load_all(db=db),
                "schedule": self.repositories.schedule.load(db=db),
            }

    def load_account(self, account_key: str) -> Dict[str, Any]:
        self.initialize()
        return self.repositories.account.load_account(account_key)

    def save_account(self, account_key: str, value: Dict[str, Any]) -> None:
        self.initialize()
        self.repositories.account.save_account(account_key, value)

    def get_source_sha1(
            self, key: str, ttl_seconds: int = 0
    ) -> Optional[Dict[str, Any]]:
        """取源文件 SHA1（秒传零流量重试用）。"""
        self.initialize()
        return self.repositories.source_sha1.get(key, ttl_seconds=ttl_seconds)

    def put_source_sha1(
            self,
            key: str,
            sha1: str,
            size: int = 0,
            name: str = "",
            limit: int = 0,
    ) -> bool:
        """记录源文件 SHA1，超出上限按更新时间淘汰最旧。"""
        self.initialize()
        return self.repositories.source_sha1.put(
            key, sha1, size=size, name=name, limit=limit
        )

    def close(self) -> None:
        with self._lock:
            self._history_cache = None
        self.manager.close()
