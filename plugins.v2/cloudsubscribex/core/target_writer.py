"""同步链路统一目标写入器：查目标、串行化、写入与写后核验。"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Callable, Mapping, Optional

from app.log import logger

from .cloud import CloudDriveCapability, CloudFile

_TARGET_LOCKS: dict[str, Lock] = {}
_TARGET_LOCKS_GUARD = Lock()


class TargetConflictError(RuntimeError):
    """目标已存在但内容不匹配。"""


class TargetUnknownError(RuntimeError):
    """无法确认目标状态或写入结果。"""


@dataclass(frozen=True)
class TargetPlan:
    provider_key: str
    path: str
    name: str
    size: int = 0
    sha1: str = ""
    md5: str = ""
    conflict_policy: str = "skip"


@dataclass
class TargetResult:
    status: str
    file: Optional[CloudFile] = None
    actual_name: str = ""
    actual_size: int = 0
    reason: str = ""


def _normalize_path(path: str) -> str:
    normalized = str(path or "/").strip().replace("\\", "/").rstrip("/")
    return normalized or "/"


def _normalize_name(name: str) -> str:
    return str(name or "").strip()


def _lock_key(plan: TargetPlan) -> str:
    return "|".join((
        str(plan.provider_key or "").strip().lower(),
        _normalize_path(plan.path),
        _normalize_name(plan.name),
    ))


class TargetWriter:
    """所有同步链路最终目标写入的唯一入口。"""

    def __init__(
            self,
            provider_resolver: Callable[[str], object],
            *,
            lock_registry: Optional[Mapping[str, Lock]] = None,
    ):
        self._provider_resolver = provider_resolver
        self._lock_registry = lock_registry if lock_registry is not None else _TARGET_LOCKS

    def _target_lock(self, key: str) -> Lock:
        with _TARGET_LOCKS_GUARD:
            lock = self._lock_registry.get(key)
            if lock is None:
                lock = Lock()
                self._lock_registry[key] = lock
            return lock

    def _resolve(self, plan: TargetPlan):
        provider = self._provider_resolver(plan.provider_key)
        return provider.require(CloudDriveCapability.FILE_QUERY)

    @staticmethod
    def _file_matches(plan: TargetPlan, file: Optional[CloudFile]) -> bool:
        if file is None or bool(getattr(file, "is_directory", False)):
            return False
        file_size = int(getattr(file, "size", 0) or 0)
        if int(plan.size or 0) > 0 and file_size != int(plan.size):
            return False
        plan_sha1 = str(plan.sha1 or "").strip().lower()
        file_sha1 = str(getattr(file, "sha1", "") or "").strip().lower()
        if plan_sha1 and file_sha1 and plan_sha1 != file_sha1:
            return False
        plan_md5 = str(plan.md5 or "").strip().lower()
        file_md5 = str(getattr(file, "md5", "") or "").strip().lower()
        if plan_md5 and file_md5 and plan_md5 != file_md5:
            return False
        return True

    def _probe_with_service(self, plan: TargetPlan, service) -> TargetResult:
        try:
            found = service.find_file(_normalize_path(plan.path), _normalize_name(plan.name))
        except Exception as error:  # noqa: BLE001 - 查询失败交由调用方决定重试
            return TargetResult(
                "unknown", reason=f"目标查询失败：{type(error).__name__}"
            )
        if found is None:
            return TargetResult("not_written")
        if self._file_matches(plan, found):
            return TargetResult(
                "reused",
                file=found,
                actual_name=str(getattr(found, "name", "") or plan.name),
                actual_size=int(getattr(found, "size", 0) or 0),
                reason="目标已存在且匹配",
            )
        return TargetResult(
            "conflict",
            file=found,
            actual_name=str(getattr(found, "name", "") or plan.name),
            actual_size=int(getattr(found, "size", 0) or 0),
            reason="目标已存在但内容不匹配",
        )

    def probe(self, plan: TargetPlan) -> TargetResult:
        """只查目标，不写；目录查询异常返回 unknown，不抛异常。"""
        try:
            service = self._resolve(plan)
        except Exception as error:  # noqa: BLE001 - 提供方不可用同样视为未知
            return TargetResult(
                "unknown", reason=f"目标查询失败：{type(error).__name__}"
            )
        result = self._probe_with_service(plan, service)
        self._log_result(plan, result)
        return result

    def commit(
            self,
            plan: TargetPlan,
            write: Callable[[], Optional[CloudFile | bool]],
    ) -> TargetResult:
        """持目标锁执行 probe → write → 写后核验。"""
        with self._target_lock(_lock_key(plan)):
            try:
                service = self._resolve(plan)
            except Exception as error:  # noqa: BLE001 - 无法确认目标状态
                result = TargetResult(
                    "unknown", reason=f"目标查询失败：{type(error).__name__}"
                )
                self._log_result(plan, result)
                raise TargetUnknownError(result.reason) from error
            probed = self._probe_with_service(plan, service)
            if probed.status == "reused":
                self._log_result(plan, probed)
                return probed
            if probed.status == "conflict":
                self._log_result(plan, probed)
                raise TargetConflictError(probed.reason)
            if probed.status == "unknown":
                self._log_result(plan, probed)
                raise TargetUnknownError(probed.reason)

            written = write()
            if written is False or written is None:
                result = TargetResult("not_written", reason="写入方未写入")
                self._log_result(plan, result)
                return result

            file = self._verify(plan, service)
            result = TargetResult(
                "written",
                file=file,
                actual_name=str(getattr(file, "name", "") or plan.name),
                actual_size=int(getattr(file, "size", 0) or 0),
                reason="写入后核验通过",
            )
            self._log_result(plan, result)
            return result

    def _verify(self, plan: TargetPlan, service) -> CloudFile:
        path = _normalize_path(plan.path)
        name = _normalize_name(plan.name)
        try:
            found = service.find_file(path, name)
        except Exception as error:  # noqa: BLE001 - 写后核验失败必须显式失败
            raise TargetUnknownError(
                f"写后核验失败：{type(error).__name__}"
            ) from error
        if found is not None:
            if self._file_matches(plan, found):
                return found
            raise TargetUnknownError("写后核验失败：目标文件与期望不匹配")

        lister = getattr(service, "list_files_recursive", None)
        if not callable(lister):
            raise TargetUnknownError("写后核验失败：无法查询目标目录")
        try:
            candidates = lister(path)
        except Exception as error:  # noqa: BLE001 - 查不到真实文件即未知
            raise TargetUnknownError(
                f"写后核验失败：{type(error).__name__}"
            ) from error
        for item in candidates or ():
            if str(getattr(item, "name", "") or "") == name:
                continue
            if self._renamed_match(plan, item):
                return item
        raise TargetUnknownError("写后核验失败：目标目录未找到写入文件")

    @staticmethod
    def _renamed_match(plan: TargetPlan, file: Optional[CloudFile]) -> bool:
        if not (int(plan.size or 0) > 0 or str(plan.sha1 or "").strip()
                or str(plan.md5 or "").strip()):
            return False
        return TargetWriter._file_matches(plan, file)

    @staticmethod
    def _log_result(plan: TargetPlan, result: TargetResult) -> None:
        logger.info(
            "目标写入%s：provider=%s path=%s name=%s actual=%s size=%s reason=%s",
            result.status,
            plan.provider_key,
            _normalize_path(plan.path),
            _normalize_name(plan.name),
            result.actual_name or "-",
            result.actual_size,
            result.reason or "-",
        )
