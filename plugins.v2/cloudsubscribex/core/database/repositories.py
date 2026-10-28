"""CloudSubscribe 业务数据 Repository。"""

import copy
import hashlib
import time
import json
import os
import re
from collections import defaultdict
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import Integer, case, cast, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .manager import CloudSubscribeDatabaseManager, DbOper, db_query, db_update
from .models import (
    AccountSnapshot,
    AuthSession,
    CheckinHistoryRecord,
    CheckinScheduleState,
    HistoryRecord,
    OfflinePendingTask,
    CandidateLedgerEntry,
    PointBudgetRecord,
    RuntimeRecord,
    SourceSha1Cache,
    SourceMissCache,
)
from .sync_jobs import SyncJobRepository
from ..history import history_group_key
from ...drive.common import positive_int
from ...search.types import normalize_resource_type, resource_type_from_url


#: 历史台账默认保留条数（超过后按记录时间淘汰最旧）。可用环境变量覆盖。
DEFAULT_HISTORY_RETENTION = 5000


def history_retention_limit() -> int:
    """读取历史台账保留上限；0 或负数表示不做上限。"""
    raw = str(os.environ.get("CLOUDSUBSCRIBE_HISTORY_LIMIT", "") or "").strip()
    if not raw:
        return DEFAULT_HISTORY_RETENTION
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_RETENTION


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


class HistoryRepository(DbOper):
    def __init__(self, manager: CloudSubscribeDatabaseManager, db: Session = None):
        super().__init__(manager, db)
        self._filter_options_lock = RLock()
        self._filter_options: Optional[Dict[str, List[str]]] = None

    def _load_filter_options(self, db: Session) -> Dict[str, List[str]]:
        with self._filter_options_lock:
            if self._filter_options is None:
                rows = db.execute(select(
                    HistoryRecord.resource_type,
                    HistoryRecord.source,
                ).distinct()).all()
                self._filter_options = {
                    "resource_types": sorted({
                        row.resource_type for row in rows if row.resource_type
                    }),
                    "sources": sorted({
                        row.source for row in rows if row.source
                    }),
                }
            return copy.deepcopy(self._filter_options)

    def _invalidate_filter_options(self) -> None:
        with self._filter_options_lock:
            self._filter_options = None

    @staticmethod
    def _record_id(record: Dict[str, Any], index: int) -> str:
        record_id = str(record.get("record_id") or "").strip()
        if record_id:
            return record_id
        identity = "\0".join(str(record.get(key) or "") for key in (
            "share_url", "file_name", "tmdb_id", "season", "episode",
        ))
        if identity.strip("\0"):
            return hashlib.sha1(identity.encode("utf-8")).hexdigest()
        return hashlib.sha1(
            f"{_canonical_digest(record)}:{index}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _source(record: Dict[str, Any]) -> str:
        source = str(record.get("source") or "unknown").strip().lower()
        return "manual" if source in {"manual", "手动添加", "手动资源"} else source

    @staticmethod
    def _resource_type(record: Dict[str, Any]) -> str:
        return (
                normalize_resource_type(
                    record.get("resource_type") or record.get("pan_type") or ""
                )
                or resource_type_from_url(record.get("share_url"))
                or "unknown"
        )

    @staticmethod
    def _task_types(record: Dict[str, Any]) -> List[str]:
        values = {
            str(value or "").strip().lower()
            for value in (record.get("task_types") or [])
            if str(value or "").strip()
        }
        if str(record.get("transfer_mode") or "").strip().lower() == "cross":
            values.add("cross_transfer")
        upgrade = record.get("upgrade")
        if upgrade is True or str(upgrade or "").strip().lower() in {
            "1", "true", "yes", "on",
        }:
            values.add("upgrade")
        return sorted(values)

    @classmethod
    def normalize(cls, record: Dict[str, Any], index: int) -> Dict[str, Any]:
        payload = copy.deepcopy(record)
        record_id = cls._record_id(payload, index)
        payload["record_id"] = record_id
        group_key = history_group_key(payload)
        task_types = cls._task_types(payload)
        return {
            "record_id": record_id,
            "sort_index": index,
            "group_key": group_key,
            "record_time": str(payload.get("time") or ""),
            "status": str(payload.get("status") or ""),
            "resource_type": cls._resource_type(payload),
            "source": cls._source(payload),
            "task_types": "|" + "|".join(task_types) + "|",
            "title": str(payload.get("title") or ""),
            "file_name": str(payload.get("file_name") or ""),
            "media_type": str(payload.get("type") or ""),
            "tmdb_id": str(payload.get("tmdb_id") or ""),
            "season": positive_int(payload.get("season")),
            "episode": _episode_number(payload),
            "subscribe_id": positive_int(payload.get("subscribe_id")),
            "payload": payload,
        }

    @classmethod
    def normalize_records(
            cls, records: Iterable[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        normalized = []
        used_ids = set()
        for index, value in enumerate(records or []):
            if not isinstance(value, dict):
                continue
            item = cls.normalize(value, index)
            if item["record_id"] in used_ids:
                item["record_id"] = hashlib.sha1(
                    f"{item['record_id']}:{index}".encode("utf-8")
                ).hexdigest()
                item["payload"]["record_id"] = item["record_id"]
            used_ids.add(item["record_id"])
            normalized.append(item)
        return normalized

    @db_update
    def replace_all(
            self, records: Iterable[Dict[str, Any]], db: Session = None
    ) -> int:
        normalized = self.normalize_records(records)
        current = {
            row.record_id: row
            for row in db.scalars(select(HistoryRecord)).all()
        }
        desired_ids = {item["record_id"] for item in normalized}
        for record_id in current.keys() - desired_ids:
            db.delete(current[record_id])
        for item in normalized:
            row = current.get(item["record_id"])
            if row is None:
                HistoryRecord(**item).create(db)
                continue
            changes = {
                key: copy.deepcopy(value)
                for key, value in item.items()
                if getattr(row, key) != value
            }
            if changes:
                row.update(db, changes)
        self._invalidate_filter_options()
        self.prune(db=db)
        return len(normalized)

    @db_update
    def repair_group_keys(self, db: Session = None) -> int:
        """幂等回填旧记录的媒体分组键。"""
        repaired = 0
        for row in db.scalars(select(HistoryRecord)).all():
            expected = history_group_key(row.payload or {})
            if row.group_key == expected:
                continue
            row.update(db, {"group_key": expected})
            repaired += 1
        return repaired

    @db_query
    def list_all(self, db: Session = None) -> List[Dict[str, Any]]:
        rows = db.scalars(
            select(HistoryRecord).order_by(HistoryRecord.sort_index)
        ).all()
        return [copy.deepcopy(row.payload) for row in rows]

    @db_update
    def prune(self, limit: Optional[int] = None, db: Session = None) -> int:
        """按记录时间只保留最近 ``limit`` 条，返回删除条数。"""
        cap = history_retention_limit() if limit is None else int(limit)
        if cap <= 0:
            return 0
        total = db.scalar(select(func.count()).select_from(HistoryRecord)) or 0
        if total <= cap:
            return 0
        keep_ids = db.scalars(
            select(HistoryRecord.id)
            .order_by(HistoryRecord.record_time.desc(), HistoryRecord.id.desc())
            .limit(cap)
        ).all()
        if not keep_ids:
            return 0
        deleted = db.execute(
            delete(HistoryRecord).where(HistoryRecord.id.not_in(keep_ids))
        ).rowcount or 0
        return int(deleted)

    @staticmethod
    def _filters(
            keyword: str = "",
            resource_types: Iterable[str] = (),
            sources: Iterable[str] = (),
            task_types: Iterable[str] = (),
            statuses: Iterable[str] = (),
    ) -> list:
        filters = []
        normalized_resource_types = {
            str(value).strip().lower() for value in resource_types if str(value).strip()
        }
        normalized_sources = {
            str(value).strip().lower() for value in sources if str(value).strip()
        }
        normalized_statuses = {
            str(value).strip().lower() for value in statuses if str(value).strip()
        }
        normalized_task_types = {
            str(value).strip().lower() for value in task_types if str(value).strip()
        }
        if normalized_resource_types:
            filters.append(HistoryRecord.resource_type.in_(normalized_resource_types))
        if normalized_sources:
            filters.append(HistoryRecord.source.in_(normalized_sources))
        if normalized_statuses:
            filters.append(func.lower(HistoryRecord.status).in_(normalized_statuses))
        if normalized_task_types:
            filters.append(or_(*[
                HistoryRecord.task_types.like(f"%|{value}|%")
                for value in normalized_task_types
            ]))
        normalized_keyword = str(keyword or "").strip().lower()
        if normalized_keyword:
            pattern = f"%{normalized_keyword}%"
            filters.append(or_(
                func.lower(HistoryRecord.title).like(pattern),
                func.lower(HistoryRecord.file_name).like(pattern),
                func.lower(HistoryRecord.media_type).like(pattern),
                func.lower(HistoryRecord.tmdb_id).like(pattern),
                func.lower(HistoryRecord.resource_type).like(pattern),
                func.lower(HistoryRecord.source).like(pattern),
            ))
        return filters

    @db_query
    def query_group_page(
            self,
            *,
            page: int,
            page_size: int,
            keyword: str = "",
            resource_types: Iterable[str] = (),
            sources: Iterable[str] = (),
            task_types: Iterable[str] = (),
            statuses: Iterable[str] = (),
            db: Session = None,
    ) -> Dict[str, Any]:
        filters = self._filters(
            keyword, resource_types, sources, task_types, statuses
        )
        normalized_page_size = min(50, max(1, int(page_size or 10)))
        filter_options = self._load_filter_options(db)
        latest_time = func.max(HistoryRecord.record_time).label("latest_time")
        success_count = func.sum(case(
            (HistoryRecord.status == "成功", 1), else_=0
        )).label("success_count")
        pending_count = func.sum(case(
            (HistoryRecord.status.in_(["处理中", "下载中"]), 1), else_=0
        )).label("pending_count")
        failed_count = func.sum(case(
            (HistoryRecord.status == "成功", 0),
            (HistoryRecord.status.in_(["处理中", "下载中"]), 0),
            else_=1,
        )).label("failed_count")
        total_size = func.coalesce(func.sum(cast(
            func.json_extract(HistoryRecord.payload, "$.file_size"), Integer
        )), 0).label("total_size")
        total_groups = func.count().over().label("total_groups")
        grouped = select(
            HistoryRecord.group_key,
            latest_time,
            success_count,
            pending_count,
            failed_count,
            total_size,
            total_groups,
        ).where(*filters).group_by(HistoryRecord.group_key)

        requested_page = max(1, int(page or 1))

        def load_groups(page_number: int):
            return db.execute(
                grouped.order_by(latest_time.desc(), HistoryRecord.group_key)
                .offset((page_number - 1) * normalized_page_size)
                .limit(normalized_page_size)
            ).all()

        group_rows = load_groups(requested_page)
        if group_rows:
            total = int(group_rows[0].total_groups or 0)
        elif requested_page > 1:
            total = int(db.scalar(
                select(func.count()).select_from(grouped.subquery())
            ) or 0)
        else:
            total = 0
        total_pages = max(
            1, (total + normalized_page_size - 1) // normalized_page_size
        )
        normalized_page = min(requested_page, total_pages)
        if normalized_page != requested_page:
            group_rows = load_groups(normalized_page)
        group_keys = [row.group_key for row in group_rows]
        records_by_group = defaultdict(list)
        if group_keys:
            rows = db.scalars(
                select(HistoryRecord)
                .where(HistoryRecord.group_key.in_(group_keys), *filters)
                .order_by(
                    HistoryRecord.season.desc(),
                    HistoryRecord.episode.desc(),
                    HistoryRecord.record_time.desc(),
                    HistoryRecord.sort_index.desc(),
                )
            ).all()
            for row in rows:
                payload = copy.deepcopy(row.payload)
                payload["record_id"] = row.record_id
                records_by_group[row.group_key].append(payload)
        groups = [{
            "group_key": row.group_key,
            "latest_time": str(row.latest_time or ""),
            "success_count": int(row.success_count or 0),
            "pending_count": int(row.pending_count or 0),
            "failed_count": int(row.failed_count or 0),
            "total_size": int(row.total_size or 0),
            "records": records_by_group.get(row.group_key, []),
        } for row in group_rows]
        return {
            "groups": groups,
            "page": normalized_page,
            "page_size": normalized_page_size,
            "total": total,
            "total_pages": total_pages,
            "filter_options": {
                "resource_types": filter_options["resource_types"],
                "sources": filter_options["sources"],
            },
        }

    @staticmethod
    def _summary(db: Session, today: str) -> Dict[str, int]:
        stats = db.execute(select(
            func.count(HistoryRecord.id),
            func.sum(case(
                (HistoryRecord.record_time.like(f"{today}%"), 1), else_=0
            )),
            func.sum(case((HistoryRecord.status == "成功", 1), else_=0)),
            func.sum(case((HistoryRecord.status == "失败", 1), else_=0)),
        )).one()
        return {
            "total": int(stats[0] or 0),
            "today": int(stats[1] or 0),
            "success": int(stats[2] or 0),
            "failed": int(stats[3] or 0),
        }

    @db_query
    def summary(
            self, today: str, db: Session = None
    ) -> Dict[str, int]:
        return self._summary(db, today)

    @db_query
    def overview(
            self, today: str, recent_limit: int = 20,
            db: Session = None,
    ) -> Dict[str, Any]:
        summary = self._summary(db, today)
        recent = db.scalars(
            select(HistoryRecord)
            .order_by(HistoryRecord.record_time.desc(), HistoryRecord.id.desc())
            .limit(max(0, min(int(recent_limit or 0), 20)))
        ).all()
        return {
            **summary,
            "recent": [copy.deepcopy(row.payload) for row in recent],
        }


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


class CandidateLedgerRepository(DbOper):
    """候选账本：追加写入、按天读、按天清理。"""

    @db_update
    def append(
            self,
            records: Iterable[Dict[str, Any]],
            *,
            day: str = "",
            ts: str = "",
            run_id: str = "",
            db: Session = None,
    ) -> int:
        day_text = str(day or "") or time.strftime("%Y-%m-%d")
        stamp = str(ts or "") or time.strftime("%Y-%m-%dT%H:%M:%S")
        written = 0
        for item in records or []:
            if not isinstance(item, dict) or not item:
                continue
            payload = copy.deepcopy(item)
            payload.setdefault("ts", stamp)
            if run_id:
                payload.setdefault("run_id", run_id)
            CandidateLedgerEntry(
                day=day_text,
                ts=str(payload.get("ts") or stamp)[:19],
                run_id=str(payload.get("run_id") or "")[:32],
                kind=str(payload.get("kind") or "")[:32],
                provider=str(payload.get("provider") or "")[:64],
                payload=payload,
            ).create(db)
            written += 1
        return written

    @db_query
    def read(
            self,
            days: List[str],
            *,
            kind: str = "",
            keyword: str = "",
            limit: int = 200,
            offset: int = 0,
            db: Session = None,
    ) -> Dict[str, Any]:
        wanted = [str(day) for day in days or [] if str(day)]
        if not wanted:
            return {"total": 0, "items": [], "days": 0}
        query = select(CandidateLedgerEntry).where(
            CandidateLedgerEntry.day.in_(wanted)
        )
        wanted_kind = str(kind or "").strip()
        if wanted_kind:
            query = query.where(CandidateLedgerEntry.kind == wanted_kind)
        rows = list(db.scalars(
            query.order_by(
                CandidateLedgerEntry.day.desc(), CandidateLedgerEntry.id.desc()
            )
        ).all())
        needle = str(keyword or "").strip().lower()
        items: List[Dict[str, Any]] = []
        for row in rows:
            payload = copy.deepcopy(row.payload) or {}
            if needle:
                haystack = " ".join(
                    str(payload.get(name) or "")
                    for name in ("reason", "resource_title", "reference", "provider")
                ).lower()
                if needle not in haystack:
                    continue
            items.append(payload)
        total = len(items)
        start = max(0, int(offset or 0))
        size = max(1, int(limit or 200))
        present = {
            row for row in db.scalars(
                select(CandidateLedgerEntry.day)
                .where(CandidateLedgerEntry.day.in_(wanted))
                .distinct()
            ).all()
        }
        return {
            "total": total,
            "items": items[start:start + size],
            "days": len(present),
        }

    @db_query
    def summary(self, days: List[str], db: Session = None) -> Dict[str, Any]:
        wanted = [str(day) for day in days or [] if str(day)]
        if not wanted:
            return {"total": 0, "by_kind": {}, "by_provider": {}, "by_day": {}}
        by_kind: Dict[str, int] = {}
        by_provider: Dict[str, int] = {}
        by_day: Dict[str, int] = {}
        total = 0
        rows = db.execute(
            select(
                CandidateLedgerEntry.day,
                CandidateLedgerEntry.kind,
                CandidateLedgerEntry.provider,
                func.count().label("count"),
            )
            .where(CandidateLedgerEntry.day.in_(wanted))
            .group_by(
                CandidateLedgerEntry.day,
                CandidateLedgerEntry.kind,
                CandidateLedgerEntry.provider,
            )
        ).all()
        for day, kind, provider, count in rows:
            count = int(count or 0)
            total += count
            by_day[str(day)] = by_day.get(str(day), 0) + count
            kind_key = str(kind or "unknown")
            by_kind[kind_key] = by_kind.get(kind_key, 0) + count
            provider_key = str(provider or "未知来源")
            by_provider[provider_key] = by_provider.get(provider_key, 0) + count
        return {
            "total": total,
            "by_kind": by_kind,
            "by_provider": by_provider,
            "by_day": by_day,
        }

    @db_update
    def purge_before(self, day: str, db: Session = None) -> int:
        cutoff = str(day or "").strip()
        if not cutoff:
            return 0
        rows = db.scalars(
            select(CandidateLedgerEntry.id).where(
                CandidateLedgerEntry.day < cutoff
            )
        ).all()
        if not rows:
            return 0
        db.execute(
            delete(CandidateLedgerEntry).where(
                CandidateLedgerEntry.day < cutoff
            )
        )
        return len(rows)


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
        self.history = HistoryRepository(manager, db)
        self.offline = OfflinePendingRepository(manager, db)
        self.sync_jobs = SyncJobRepository(manager, db)
        self.checkin = CheckinRepository(manager, db)
        self.schedule = CheckinScheduleRepository(manager, db)
        self.budget = PointBudgetRepository(manager, db)
        self.account = AccountSnapshotRepository(manager, db)
        self.auth = AuthSessionRepository(manager, db)
        self.source_sha1 = SourceSha1CacheRepository(manager, db)
        self.source_miss = SourceMissCacheRepository(manager, db)
        self.runtime = RuntimeRecordRepository(manager, db)
        self.ledger = CandidateLedgerRepository(manager, db)
