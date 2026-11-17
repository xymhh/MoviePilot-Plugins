"""CloudSubscribe 业务数据 Repository。"""

import copy
import hashlib
import time
import json
import re
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .manager import CloudSubscribeDatabaseManager, DbOper, db_query, db_update
from .models import (
    AccountSnapshot,
    AuthSession,
    CheckinHistoryRecord,
    CheckinScheduleState,
    OfflinePendingTask,
    PointBudgetRecord,
    RetryIndex,
    RuntimeRecord,
    SourceSha1Cache,
    SourceMissCache,
)
from ...drive.common import positive_int


#: 重试索引留存（设计 §2）：失败行 90 天、待修复行 30 天；重试完成的行
#: 在转存翻成功时由同步删除，不走过期清理。
RETRY_INDEX_FAILED_RETENTION_SECONDS = 90 * 86400
RETRY_INDEX_REPAIR_RETENTION_SECONDS = 30 * 86400


def _episode_number(record: Dict[str, Any]) -> Optional[int]:
    episode = positive_int(record.get("episode"))
    if episode:
        return episode
    values = record.get("target_episodes")
    candidates = (
        values
        if isinstance(values, (list, tuple, set))
        else re.findall(r"\d+", str(values or ""))
    )
    episodes = [
        number
        for value in candidates
        if (number := positive_int(value)) is not None
    ]
    return max(episodes, default=None)


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


#: 重试索引行字段（与 RetryIndex 模型列一一对应；展示类字段不在此列）。
RETRY_INDEX_FIELDS = tuple(
    column.name for column in RetryIndex.__table__.columns
)


class RetryIndexRepository(DbOper):
    """最小重试索引仓储（Phase 2 v1.13.1）：只存重试必需的字段。

    表里只有三类行：失败（status=="失败"）、跨盘成功待修复
    （status=="成功" 且 landed 为 False）、处理中（重试进行中）。
    成功行只在待修复时才进表，所以 status=="成功" 本身就是待修复标记。
    """

    @db_query
    def list_all(self, db: Session = None) -> List[Dict[str, Any]]:
        return [row.to_dict() for row in RetryIndex.list(db)]

    @db_query
    def get_by_record_id(
            self, record_id: str, db: Session = None
    ) -> Optional[Dict[str, Any]]:
        row = RetryIndex.get(db, str(record_id or "").strip())
        return row.to_dict() if row is not None else None

    @db_query
    def get_by_identity(
            self,
            record_time: str,
            share_url: str,
            file_name: str,
            db: Session = None,
    ) -> Optional[Dict[str, Any]]:
        row = db.scalars(
            select(RetryIndex).where(
                RetryIndex.record_time == str(record_time or ""),
                RetryIndex.share_url == str(share_url or ""),
                RetryIndex.file_name == str(file_name or ""),
            )
        ).first()
        return row.to_dict() if row is not None else None

    @db_query
    def get_by_finalize_key(
            self, finalize_key: str, db: Session = None
    ) -> Optional[Dict[str, Any]]:
        """按 finalize_key 查一行（后处理通知详情的索引回查）。"""
        key = str(finalize_key or "").strip()
        if not key:
            return None
        row = db.scalars(
            select(RetryIndex).where(RetryIndex.finalize_key == key)
        ).first()
        return row.to_dict() if row is not None else None

    @db_query
    def list_retryable(self, db: Session = None) -> List[Dict[str, Any]]:
        """只列失败与待修复行（处理中的重试不列出）。"""
        rows = db.scalars(
            select(RetryIndex)
            .where(or_(RetryIndex.status == "失败", RetryIndex.status == "成功"))
            .order_by(RetryIndex.created_at.asc(), RetryIndex.record_id.asc())
        ).all()
        return [row.to_dict() for row in rows]

    @db_query
    def list_page_rows(self, db: Session = None) -> List[Dict[str, Any]]:
        """失败记录页列表：失败、待修复与处理中行（索引里只存这三类）。

        处理中行也要列出：前端统计卡片可按“处理中”过滤、总览按它分组，
        删除与清空入口又都明确排除处理中行——只计数不列行，卡片点进去
        永远是空的。清空仍走 list_retryable（不删处理中行）。
        """
        rows = db.scalars(
            select(RetryIndex).order_by(
                RetryIndex.created_at.asc(), RetryIndex.record_id.asc()
            )
        ).all()
        return [row.to_dict() for row in rows]

    @db_update
    def upsert_row(self, row: Dict[str, Any], db: Session = None) -> bool:
        """按 record_id 插入或更新一行；无 record_id 时返回 False。"""
        record_id = str((row or {}).get("record_id") or "").strip()
        if not record_id:
            return False
        values = {
            key: (row or {}).get(key)
            for key in RETRY_INDEX_FIELDS
        }
        values["record_id"] = record_id
        statement = sqlite_insert(RetryIndex).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[RetryIndex.record_id],
            set_={
                key: getattr(statement.excluded, key)
                for key in values if key != "record_id"
            },
        ))
        return True

    @db_update
    def delete_by_record_ids(
            self, record_ids: Iterable[str], db: Session = None
    ) -> int:
        ids = {
            str(record_id or "").strip()
            for record_id in (record_ids or [])
            if str(record_id or "").strip()
        }
        if not ids:
            return 0
        result = db.execute(delete(RetryIndex).where(
            RetryIndex.record_id.in_(sorted(ids))
        ))
        return int(result.rowcount or 0)

    @db_update
    def purge_old(
            self, now: Optional[int] = None, db: Session = None
    ) -> int:
        """删过期行：失败行 90 天、待修复行 30 天；处理中行不走过期清理。"""
        current = int(now or time.time())
        failed_cutoff = current - RETRY_INDEX_FAILED_RETENTION_SECONDS
        repair_cutoff = current - RETRY_INDEX_REPAIR_RETENTION_SECONDS
        deleted = 0
        deleted += int(db.execute(delete(RetryIndex).where(
            RetryIndex.status == "失败",
            RetryIndex.created_at < failed_cutoff,
        )).rowcount or 0)
        deleted += int(db.execute(delete(RetryIndex).where(
            RetryIndex.status == "成功",
            RetryIndex.created_at < repair_cutoff,
        )).rowcount or 0)
        return deleted


class OfflinePendingRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, Any]:
        return {
            str(key): copy.deepcopy(value)
            for key, value in (values or {}).items()
            if str(key)
        }

    @db_update
    def replace_all(self, values: Dict[str, Any], db: Session = None) -> int:
        normalized = self.normalize_values(values)
        current = {
            row.pending_key: row
            for row in db.scalars(select(OfflinePendingTask)).all()
        }
        for key in current.keys() - normalized.keys():
            db.delete(current[key])
        for key, payload in normalized.items():
            task_id = str(payload.get("task_id") or "") \
                if isinstance(payload, dict) else ""
            status = str(payload.get("status") or "") \
                if isinstance(payload, dict) else ""
            created_at = int(payload.get("created_at") or 0) \
                if isinstance(payload, dict) else 0
            row = current.get(key)
            if row is None:
                OfflinePendingTask(
                    pending_key=key,
                    task_id=task_id,
                    status=status,
                    created_at=created_at,
                    payload=payload,
                ).create(db)
            else:
                values = {
                    "task_id": task_id,
                    "status": status,
                    "created_at": created_at,
                    "payload": payload,
                }
                changes = {
                    field: copy.deepcopy(value)
                    for field, value in values.items()
                    if getattr(row, field) != value
                }
                if changes:
                    row.update(db, changes)
        return len(normalized)

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Any]:
        rows = db.scalars(select(OfflinePendingTask)).all()
        return {row.pending_key: copy.deepcopy(row.payload) for row in rows}


class CheckinRepository(DbOper):

    @staticmethod
    def _id(provider: str, record: Dict[str, Any], index: int) -> str:
        return str(record.get("id") or "").strip() or hashlib.sha1(
            f"{provider}:{record.get('executed_at')}:{_canonical_digest(record)}:{index}".encode("utf-8")
        ).hexdigest()

    @classmethod
    def normalize_records(
            cls, provider: str, records: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized = []
        for index, item in enumerate(records or []):
            if not isinstance(item, dict):
                continue
            record = copy.deepcopy(item)
            record["id"] = cls._id(provider, record, index)
            normalized.append(record)
        return normalized

    @db_update
    def replace_provider(
            self, provider: str, records: Iterable[Dict[str, Any]],
            db: Session = None,
    ) -> int:
        provider = str(provider or "").strip().lower()
        normalized = self.normalize_records(provider, records)

        current = {
            row.id: row
            for row in db.scalars(select(CheckinHistoryRecord).where(
                CheckinHistoryRecord.provider == provider
            )).all()
        }
        desired_ids = {record["id"] for record in normalized}
        for record_id in current.keys() - desired_ids:
            db.delete(current[record_id])
        for index, record in enumerate(normalized):
            record_id = record["id"]
            values = {
                "provider": provider,
                "sort_index": index,
                "executed_at": str(record.get("executed_at") or ""),
                "success": bool(record.get("success")),
                "payload": record,
            }
            row = current.get(record_id)
            if row is None:
                CheckinHistoryRecord(id=record_id, **values).create(db)
                continue
            changes = {
                key: copy.deepcopy(value)
                for key, value in values.items()
                if getattr(row, key) != value
            }
            if changes:
                row.update(db, changes)
        return len(normalized)

    @db_query
    def list_provider(
            self, provider: str, db: Session = None
    ) -> List[Dict[str, Any]]:
        rows = db.scalars(
            select(CheckinHistoryRecord)
            .where(CheckinHistoryRecord.provider == provider)
            .order_by(CheckinHistoryRecord.sort_index)
        ).all()
        return [copy.deepcopy(row.payload) for row in rows]

    @db_query
    def load_all(
            self, db: Session = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        rows = db.scalars(
            select(CheckinHistoryRecord).order_by(
                CheckinHistoryRecord.provider,
                CheckinHistoryRecord.sort_index,
            )
        ).all()
        for row in rows:
            grouped[row.provider].append(copy.deepcopy(row.payload))
        return dict(grouped)


class CheckinScheduleRepository(DbOper):

    @staticmethod
    def normalize(value: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(value or {})
        return {
            "date": str(data.get("date") or ""),
            "full_completed": bool(data.get("full_completed")),
            "retry_count": int(data.get("retry_count") or 0),
            "pending_providers": [
                str(item) for item in (data.get("pending_providers") or [])
                if str(item)
            ],
            "completed_retry_count": int(
                data.get("completed_retry_count") or 0
            ),
        }

    @db_update
    def save(self, value: Dict[str, Any], db: Session = None) -> None:
        data = self.normalize(value)
        values = {
            "id": 1,
            "schedule_date": str(data.get("date") or ""),
            "full_completed": bool(data.get("full_completed")),
            "retry_count": int(data.get("retry_count") or 0),
            "pending_providers": list(data.get("pending_providers") or []),
            "completed_retry_count": int(
                data.get("completed_retry_count") or 0
            ),
        }
        statement = sqlite_insert(CheckinScheduleState).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[CheckinScheduleState.id],
            set_={
                key: getattr(statement.excluded, key)
                for key in values if key != "id"
            },
        ))

    @db_query
    def load(self, db: Session = None) -> Dict[str, Any]:
        row = CheckinScheduleState.get(db, 1)
        if row is None:
            return {}
        return {
            "date": row.schedule_date,
            "full_completed": bool(row.full_completed),
            "retry_count": int(row.retry_count or 0),
            "pending_providers": list(row.pending_providers or []),
            "completed_retry_count": int(row.completed_retry_count or 0),
        }


class PointBudgetRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, int]:
        normalized = {}
        for key, value in (values or {}).items():
            normalized_key = str(key)
            if not normalized_key:
                continue
            try:
                normalized[normalized_key] = max(0, int(value or 0))
            except (TypeError, ValueError):
                continue
        return normalized

    @db_update
    def replace_provider(
            self, provider: str, values: Dict[str, Any], db: Session = None
    ) -> int:
        provider = str(provider or "").strip().lower()
        normalized = self.normalize_values(values)
        current = {
            row.subscribe_key: row
            for row in db.scalars(select(PointBudgetRecord).where(
                PointBudgetRecord.provider == provider
            )).all()
        }
        for key in current.keys() - normalized.keys():
            db.delete(current[key])
        for key, points in normalized.items():
            row = current.get(key)
            if row is None:
                PointBudgetRecord(
                    provider=provider,
                    subscribe_key=key,
                    points=points,
                ).create(db)
            elif int(row.points or 0) != points:
                row.update(db, {"points": points})
        return len(normalized)

    @db_query
    def load_provider(
            self, provider: str, db: Session = None
    ) -> Dict[str, int]:
        provider = str(provider or "").strip().lower()
        rows = db.scalars(select(PointBudgetRecord).where(
            PointBudgetRecord.provider == provider
        )).all()
        return {row.subscribe_key: int(row.points or 0) for row in rows}

    @db_query
    def load_all(
            self, db: Session = None
    ) -> Dict[str, Dict[str, int]]:
        grouped: Dict[str, Dict[str, int]] = defaultdict(dict)
        rows = db.scalars(select(PointBudgetRecord).order_by(
            PointBudgetRecord.provider,
            PointBudgetRecord.subscribe_key,
        )).all()
        for row in rows:
            grouped[row.provider][row.subscribe_key] = int(row.points or 0)
        return {provider: dict(values) for provider, values in grouped.items()}


class AccountSnapshotRepository(DbOper):

    @staticmethod
    def normalize_values(values: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        return {
            str(key).strip().lower(): copy.deepcopy(value)
            for key, value in (values or {}).items()
            if str(key).strip() and isinstance(value, dict) and value
        }

    @db_update
    def replace_all(self, values: Dict[str, Any], db: Session = None) -> int:
        normalized = self.normalize_values(values)
        current = {
            row.account_key: row
            for row in db.scalars(select(AccountSnapshot)).all()
        }
        for account_key in current.keys() - normalized.keys():
            db.delete(current[account_key])
        for account_key, payload in normalized.items():
            values = {
                "refreshed_at": _timestamp(payload.get("refreshed_at")),
                "payload": payload,
            }
            row = current.get(account_key)
            if row is None:
                AccountSnapshot(
                    account_key=account_key,
                    **values,
                ).create(db)
            else:
                changes = {
                    field: copy.deepcopy(value)
                    for field, value in values.items()
                    if getattr(row, field) != value
                }
                if changes:
                    row.update(db, changes)
        return len(normalized)

    @db_update
    def save_account(
            self, account_key: str, value: Any, db: Session = None
    ) -> None:
        normalized_key = str(account_key or "").strip().lower()
        if not normalized_key:
            raise ValueError("账户快照 account_key 不能为空")
        payload = copy.deepcopy(value) if isinstance(value, dict) and value else {}

        if not payload:
            db.execute(delete(AccountSnapshot).where(
                AccountSnapshot.account_key == normalized_key
            ))
            return
        values = {
            "account_key": normalized_key,
            "refreshed_at": _timestamp(payload.get("refreshed_at")),
            "payload": payload,
        }
        statement = sqlite_insert(AccountSnapshot).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[AccountSnapshot.account_key],
            set_={
                "refreshed_at": statement.excluded.refreshed_at,
                "payload": statement.excluded.payload,
            },
        ))

    @db_query
    def load_account(
            self, account_key: str, db: Session = None
    ) -> Dict[str, Any]:
        normalized_key = str(account_key or "").strip().lower()
        row = AccountSnapshot.get(db, normalized_key)
        return copy.deepcopy(row.payload) if row else {}

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Dict[str, Any]]:
        return {
            row.account_key: copy.deepcopy(row.payload)
            for row in AccountSnapshot.list(db)
        }


class AuthSessionRepository(DbOper):

    @staticmethod
    def normalize_value(value: Any) -> Dict[str, Any]:
        return copy.deepcopy(value) if isinstance(value, dict) and value else {}

    @db_update
    def save_provider(
            self, provider: str, value: Any, db: Session = None
    ) -> None:
        normalized_provider = str(provider or "").strip().lower()
        if not normalized_provider:
            raise ValueError("认证会话 provider 不能为空")
        payload = self.normalize_value(value)

        if not payload:
            db.execute(delete(AuthSession).where(
                AuthSession.provider == normalized_provider
            ))
            return
        values = {
            "provider": normalized_provider,
            "updated_at": _timestamp(payload.get("updated_at")),
            "expires_at": _timestamp(payload.get("expires_at")),
            "payload": payload,
        }
        statement = sqlite_insert(AuthSession).values(**values)
        db.execute(statement.on_conflict_do_update(
            index_elements=[AuthSession.provider],
            set_={
                key: getattr(statement.excluded, key)
                for key in values if key != "provider"
            },
        ))

    @db_update
    def replace_all(
            self, values: Dict[str, Any], db: Session = None
    ) -> int:
        normalized = {}
        for provider, value in (values or {}).items():
            normalized_provider = str(provider).strip().lower()
            normalized_value = self.normalize_value(value)
            if normalized_provider and normalized_value:
                normalized[normalized_provider] = normalized_value

        current = {row.provider: row for row in AuthSession.list(db)}
        for provider in current.keys() - normalized.keys():
            db.delete(current[provider])
        for provider, payload in normalized.items():
            row = current.get(provider)
            if (
                    row is not None
                    and int(row.updated_at or 0)
                    == _timestamp(payload.get("updated_at"))
                    and int(row.expires_at or 0)
                    == _timestamp(payload.get("expires_at"))
                    and row.payload == payload
            ):
                continue
            self.save_provider(provider, payload, db=db)
        return len(normalized)

    @db_query
    def load_provider(
            self, provider: str, db: Session = None
    ) -> Dict[str, Any]:
        normalized_provider = str(provider or "").strip().lower()
        row = AuthSession.get(db, normalized_provider)
        return copy.deepcopy(row.payload) if row else {}

    @db_query
    def load_all(self, db: Session = None) -> Dict[str, Dict[str, Any]]:
        return {
            row.provider: copy.deepcopy(row.payload)
            for row in AuthSession.list(db)
        }


class RuntimeRecordRepository(DbOper):
    """运行期键值对：TG 监控池、扫盘游标、STRM 续扫断点等小状态。"""

    @staticmethod
    def normalize(namespace: Any, key: Any) -> tuple[str, str]:
        return (
            str(namespace or "").strip()[:64],
            str(key or "").strip()[:191],
        )

    @db_query
    def load(
            self,
            namespace: str,
            key: str,
            default: Any = None,
            db: Session = None,
    ) -> Any:
        space, name = self.normalize(namespace, key)
        if not space or not name:
            return default
        row = db.get(RuntimeRecord, (space, name))
        if row is None:
            return default
        return copy.deepcopy(row.payload)

    @db_update
    def save(
            self,
            namespace: str,
            key: str,
            payload: Any,
            db: Session = None,
    ) -> bool:
        space, name = self.normalize(namespace, key)
        if not space or not name:
            return False
        now = int(time.time())
        row = db.get(RuntimeRecord, (space, name))
        data = copy.deepcopy(payload)
        if row is None:
            RuntimeRecord(
                namespace=space,
                record_key=name,
                payload=data,
                updated_at=now,
            ).create(db)
        else:
            row.update(db, {"payload": data, "updated_at": now})
        return True

    @db_update
    def delete(self, namespace: str, key: str, db: Session = None) -> bool:
        space, name = self.normalize(namespace, key)
        if not space or not name:
            return False
        row = db.get(RuntimeRecord, (space, name))
        if row is None:
            return False
        db.delete(row)
        return True

    @db_query
    def keys(self, namespace: str, db: Session = None) -> List[str]:
        space, _ = self.normalize(namespace, "_")
        if not space:
            return []
        return list(
            db.scalars(
                select(RuntimeRecord.record_key).where(
                    RuntimeRecord.namespace == space
                )
            ).all()
        )


class SourceSha1CacheRepository(DbOper):
    """源文件 SHA1 点查/写入，服务于秒传未命中后的零流量重试。"""

    @staticmethod
    def normalize_key(key: Any) -> str:
        return str(key or "").strip()

    @db_query
    def get(
            self, key: str, ttl_seconds: int = 0, db: Session = None
    ) -> Optional[Dict[str, Any]]:
        cache_key = self.normalize_key(key)
        if not cache_key:
            return None
        row = db.get(SourceSha1Cache, cache_key)
        if row is None:
            return None
        if ttl_seconds > 0 and row.updated_at:
            if time.time() - int(row.updated_at) > ttl_seconds:
                return None
        return {
            "sha1": row.sha1,
            "size": int(row.size or 0),
            "name": row.name or "",
            "updated_at": int(row.updated_at or 0),
        }

    @db_update
    def put(
            self,
            key: str,
            sha1: str,
            size: int = 0,
            name: str = "",
            limit: int = 0,
            updated_at: int = 0,
            db: Session = None,
    ) -> bool:
        cache_key = self.normalize_key(key)
        digest = str(sha1 or "").strip().upper()
        if not cache_key or not digest:
            return False
        now = int(updated_at or 0) or int(time.time())
        row = db.get(SourceSha1Cache, cache_key)
        if row is None:
            SourceSha1Cache(
                cache_key=cache_key,
                sha1=digest,
                size=max(0, int(size or 0)),
                name=str(name or ""),
                updated_at=now,
            ).create(db)
        else:
            row.update(db, {
                "sha1": digest,
                "size": max(0, int(size or 0)),
                "name": str(name or ""),
                "updated_at": now,
            })
        if limit > 0:
            self._prune(limit, db=db)
        return True

    @db_update
    def purge_expired(
            self, ttl_seconds: int, db: Session = None
    ) -> int:
        if ttl_seconds <= 0:
            return 0
        deadline = int(time.time()) - int(ttl_seconds)
        rows = db.scalars(
            select(SourceSha1Cache.cache_key).where(
                SourceSha1Cache.updated_at < deadline
            )
        ).all()
        if not rows:
            return 0
        db.execute(
            delete(SourceSha1Cache).where(
                SourceSha1Cache.cache_key.in_(list(rows))
            )
        )
        return len(rows)

    def _prune(self, limit: int, db: Session) -> None:
        # 会话关闭了 autoflush，先落盘新增/更新，否则本次写入不在淘汰候选里。
        db.flush()
        stale = db.scalars(
            select(SourceSha1Cache.cache_key)
            .order_by(
                SourceSha1Cache.updated_at.desc(),
                SourceSha1Cache.cache_key.desc(),
            )
            .offset(max(0, int(limit)))
        ).all()
        if stale:
            db.execute(
                delete(SourceSha1Cache).where(
                    SourceSha1Cache.cache_key.in_(list(stale))
                )
            )


class SourceMissCacheRepository(DbOper):
    """秒传未命中负缓存：TTL 内同一 (目标盘, SHA1) 不再重复下载整份文件。"""

    @staticmethod
    def normalize_key(target_key: Any, sha1: Any) -> str:
        target = str(target_key or "").strip().lower()
        digest = str(sha1 or "").strip().upper()
        if not target or not digest:
            return ""
        return f"{target}|{digest}"

    @db_query
    def get(
            self, target_key: str, sha1: str, ttl_seconds: int = 0,
            db: Session = None,
    ) -> int:
        """返回未命中时间戳；无记录或已过期返回 0。"""
        cache_key = self.normalize_key(target_key, sha1)
        if not cache_key:
            return 0
        row = db.get(SourceMissCache, cache_key)
        if row is None:
            return 0
        missed_at = int(row.missed_at or 0)
        if ttl_seconds > 0 and missed_at and time.time() - missed_at > ttl_seconds:
            return 0
        return missed_at

    @db_update
    def put(
            self,
            target_key: str,
            sha1: str,
            missed_at: int = 0,
            limit: int = 0,
            db: Session = None,
    ) -> bool:
        cache_key = self.normalize_key(target_key, sha1)
        if not cache_key:
            return False
        now = int(missed_at or 0) or int(time.time())
        row = db.get(SourceMissCache, cache_key)
        if row is None:
            SourceMissCache(
                cache_key=cache_key,
                target_key=str(target_key or "").strip().lower(),
                sha1=str(sha1 or "").strip().upper(),
                missed_at=now,
            ).create(db)
        else:
            row.update(db, {"missed_at": now})
        if limit > 0:
            self._prune(limit, db=db)
        return True

    @db_update
    def purge_expired(self, ttl_seconds: int, db: Session = None) -> int:
        if ttl_seconds <= 0:
            return 0
        deadline = int(time.time()) - int(ttl_seconds)
        rows = db.scalars(
            select(SourceMissCache.cache_key).where(
                SourceMissCache.missed_at < deadline
            )
        ).all()
        if not rows:
            return 0
        db.execute(
            delete(SourceMissCache).where(
                SourceMissCache.cache_key.in_(list(rows))
            )
        )
        return len(rows)

    def _prune(self, limit: int, db: Session) -> None:
        db.flush()
        stale = db.scalars(
            select(SourceMissCache.cache_key)
            .order_by(
                SourceMissCache.missed_at.desc(),
                SourceMissCache.cache_key.desc(),
            )
            .offset(max(0, int(limit)))
        ).all()
        if stale:
            db.execute(
                delete(SourceMissCache).where(
                    SourceMissCache.cache_key.in_(list(stale))
                )
            )


class CloudSubscribeRepositories:
    def __init__(
            self,
            manager: CloudSubscribeDatabaseManager,
            db: Session = None,
    ):
        self.retry_index = RetryIndexRepository(manager, db)
        self.offline = OfflinePendingRepository(manager, db)
        self.checkin = CheckinRepository(manager, db)
        self.schedule = CheckinScheduleRepository(manager, db)
        self.budget = PointBudgetRepository(manager, db)
        self.account = AccountSnapshotRepository(manager, db)
        self.auth = AuthSessionRepository(manager, db)
        self.source_sha1 = SourceSha1CacheRepository(manager, db)
        self.source_miss = SourceMissCacheRepository(manager, db)
        self.runtime = RuntimeRecordRepository(manager, db)
