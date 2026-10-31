"""CloudSubscribe 插件业务数据与可恢复运行状态的 ORM 入口。"""

from __future__ import annotations

import copy
import datetime
import json
from threading import RLock
from typing import Any, Dict, List, Optional

from app.log import logger

from .database import CloudSubscribeDatabaseManager, CloudSubscribeRepositories


class RuntimeStateStore:
    """运行期小状态的门面：TG 监控池、扫盘游标、STRM 续扫断点。

    统一落 ``runtime_records`` 表（namespace + key），调用方只看到
    ``load`` / ``save`` / ``delete`` 三个方法。
    """

    def __init__(self, store: "CloudSubscribeDataStore", namespace: str):
        self._store = store
        self._namespace = namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def load(self, key: str, default: Any = None) -> Any:
        return self._store.repositories.runtime.load(
            self._namespace, key, default
        )

    def save(self, key: str, value: Any) -> None:
        self._store.repositories.runtime.save(self._namespace, key, value)

    def delete(self, key: str) -> None:
        self._store.repositories.runtime.delete(self._namespace, key)


class LedgerStore:
    """候选账本门面：追加、按天读、聚合、按天清理。"""

    def __init__(self, store: "CloudSubscribeDataStore"):
        self._store = store

    def append(
            self,
            records: Any,
            *,
            run_id: str = "",
            day: str = "",
    ) -> int:
        return self._store.repositories.ledger.append(
            records, day=day, run_id=run_id
        )

    def read(
            self,
            *,
            days: int = 1,
            kind: str = "",
            keyword: str = "",
            limit: int = 200,
            offset: int = 0,
    ) -> Dict[str, Any]:
        return self._store.repositories.ledger.read(
            ledger_days(days),
            kind=kind,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )

    def summary(self, *, days: int = 7) -> Dict[str, Any]:
        return self._store.repositories.ledger.summary(ledger_days(days))

    def purge(self, *, keep_days: int = 14) -> int:
        keep = set(ledger_days(keep_days))
        cutoff = min(keep) if keep else ""
        return self._store.repositories.ledger.purge_before(cutoff)


def ledger_days(days: int) -> List[str]:
    """最近 N 天的日期串（含今天），用于账本分桶查询。"""
    total = max(1, int(days or 1))
    today = datetime.date.today()
    return [
        (today - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(total)
    ]


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
            repaired_kinds = self.repositories.history.repair_failure_kinds()
            self._migrate_legacy_sha1_cache()
            self._migrate_legacy_json_state()
            if repaired:
                logger.info(f"CloudSubscribe 历史媒体分组已修复：{repaired} 条")
            if repaired_kinds:
                logger.info(
                    f"CloudSubscribe 历史失败类型已回填：{repaired_kinds} 条"
                )
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

    def get_source_miss(
            self, target_key: str, sha1: str, ttl_seconds: int = 0
    ) -> int:
        """取秒传未命中时间戳（0=没有记录或已过期）。"""
        self.initialize()
        return self.repositories.source_miss.get(
            target_key, sha1, ttl_seconds=ttl_seconds
        )

    def put_source_miss(
            self, target_key: str, sha1: str, limit: int = 0
    ) -> bool:
        """记录一次秒传未命中，超出上限按时间淘汰最旧。"""
        self.initialize()
        return self.repositories.source_miss.put(
            target_key, sha1, limit=limit
        )

    def runtime_store(self, namespace: str) -> RuntimeStateStore:
        """按命名空间取运行期状态存储。"""
        self.initialize()
        return RuntimeStateStore(self, namespace)

    def ledger_store(self) -> LedgerStore:
        """候选账本存储。"""
        self.initialize()
        return LedgerStore(self)

    def load_scan_state(self) -> Optional[Dict[str, Any]]:
        self.initialize()
        return self.repositories.runtime.load("strm_scan", "state")

    def save_scan_state(self, payload: Dict[str, Any]) -> bool:
        self.initialize()
        return self.repositories.runtime.save("strm_scan", "state", payload)

    def clear_scan_state(self) -> None:
        self.initialize()
        self.repositories.runtime.delete("strm_scan", "state")

    def _migrate_legacy_json_state(self) -> None:
        """把散落的 JSON 状态文件一次性并入数据库，原文件改名留档。"""
        self._migrate_legacy_tg_monitor()
        self._migrate_legacy_scan_state()
        self._migrate_legacy_ledger()

    def _rename_legacy(self, path) -> None:
        try:
            path.replace(path.with_name(path.name + ".migrated"))
        except OSError as error:
            logger.debug(f"旧状态文件改名失败：{error}", exc_info=True)

    def _migrate_legacy_tg_monitor(self) -> None:
        directory = self.manager.db_path.parent / "tg_monitor"
        if not directory.is_dir():
            return
        migrated = 0
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception as error:  # noqa: BLE001 - 坏文件不阻塞启动
                logger.debug(f"TG 监控旧文件读取失败（{path.name}）：{error}")
                self._rename_legacy(path)
                continue
            if self.repositories.runtime.save(
                    "tg_monitor", path.stem, payload
            ):
                migrated += 1
            self._rename_legacy(path)
        if migrated:
            logger.info(f"TG 监控状态已并入数据库：{migrated} 个文件")

    def _migrate_legacy_scan_state(self) -> None:
        legacy = self.manager.db_path.parent / "strm_scan_state.json"
        if not legacy.is_file():
            return
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
        except Exception as error:  # noqa: BLE001 - 坏文件按空处理
            logger.debug(f"STRM 续扫旧状态读取失败：{error}")
            payload = None
        if isinstance(payload, dict) and payload:
            self.repositories.runtime.save("strm_scan", "state", payload)
            logger.info("STRM 续扫状态已并入数据库")
        self._rename_legacy(legacy)

    def _migrate_legacy_ledger(self) -> None:
        directory = self.manager.db_path.parent / "ledger"
        if not directory.is_dir():
            return
        migrated = 0
        for path in sorted(directory.glob("candidate-ledger-*.jsonl")):
            day = path.stem[len("candidate-ledger-"):]
            records: List[Dict[str, Any]] = []
            try:
                with path.open("r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(item, dict):
                            records.append(item)
            except OSError as error:
                logger.debug(f"候选账本旧文件读取失败（{path.name}）：{error}")
                continue
            if records:
                migrated += self.repositories.ledger.append(records, day=day)
            self._rename_legacy(path)
        if migrated:
            logger.info(f"候选账本已并入数据库：{migrated} 条")

    def close(self) -> None:
        with self._lock:
            self._history_cache = None
        self.manager.close()
