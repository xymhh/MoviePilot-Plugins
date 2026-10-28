"""候选资源账本：逐条记录订阅搜索到的候选资源及其去向。

线上痛点（2026-10-24 复盘）：一轮扫描里 396 次跳过/失败，用户在界面上只能看到
最终「没转存」的结果，看不到「为什么没转存」——是分享失效、标题不符、还是画质
不达标。日志里有原因，但需要翻容器日志。

本模块把每次同步的候选去向落成按天 JSONL 文件，供 ``/ledger`` 接口与前端页面查询：

* 只追加，不修改，不需要数据库事务；
* 按天分文件，读取时按需拼接，超过保留天数的文件自动删除；
* 写入失败只记 debug 日志，绝不影响转存主流程。
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Iterable, List, Optional

try:  # 单测环境可能不注入宿主日志
    from app.log import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger(__name__)

#: 明细的落库字段白名单，避免把大对象（文件列表、原始响应）写进账本。
_FIELDS = (
    "kind",
    "provider",
    "reason",
    "reference",
    "resource_title",
    "subscribe_id",
    "subscribe_title",
    "season",
    "episode",
)

#: 默认保留天数：账本是排障数据，不需要长期留存。
DEFAULT_RETENTION_DAYS = 14
_FILE_PREFIX = "candidate-ledger-"
_FILE_SUFFIX = ".jsonl"

_write_lock = Lock()


def _ledger_dir(data_path: Any) -> Optional[Path]:
    """账本目录：``<插件数据目录>/ledger``；拿不到数据目录时返回 None。"""
    if not data_path:
        return None
    try:
        directory = Path(data_path) / "ledger"
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    except Exception as error:  # noqa: BLE001
        logger.debug(f"创建账本目录失败：{error}")
        return None


def _day_text(day: Optional[datetime] = None) -> str:
    return (day or datetime.now()).strftime("%Y-%m-%d")


def _file_for(directory: Path, day: str) -> Path:
    return directory / f"{_FILE_PREFIX}{day}{_FILE_SUFFIX}"


def _clean(item: Dict[str, Any]) -> Dict[str, Any]:
    """按白名单挑字段，长文本截断，保证每行可控。"""
    cleaned: Dict[str, Any] = {}
    for name in _FIELDS:
        value = item.get(name)
        if value in (None, "", []):
            continue
        if isinstance(value, str) and len(value) > 300:
            value = value[:300]
        cleaned[name] = value
    return cleaned


def append_records(
        data_path: Any,
        records: Iterable[Dict[str, Any]],
        *,
        run_id: str = "",
) -> int:
    """把一批候选记录追加到当天账本，返回实际写入条数。

    任何异常都被吞掉：账本是辅助观测数据，不能因为它中断同步。
    """
    directory = _ledger_dir(data_path)
    if directory is None:
        return 0
    items = [item for item in (_clean(record) for record in records or []) if item]
    if not items:
        return 0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    lines: List[str] = []
    for item in items:
        item.setdefault("ts", stamp)
        if run_id:
            item.setdefault("run_id", run_id)
        lines.append(json.dumps(item, ensure_ascii=False))
    payload = "\n".join(lines) + "\n"
    target = _file_for(directory, _day_text())
    try:
        with _write_lock:
            with target.open("a", encoding="utf-8") as handle:
                handle.write(payload)
        return len(items)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"写入候选账本失败：{error}")
        return 0


def _iter_days(days: int) -> List[str]:
    today = datetime.now()
    return [_day_text(today - timedelta(days=offset)) for offset in range(max(1, days))]


def read_records(
        data_path: Any,
        *,
        days: int = 1,
        kind: str = "",
        keyword: str = "",
        limit: int = 200,
        offset: int = 0,
) -> Dict[str, Any]:
    """按天倒序读取账本，支持按类型和关键词过滤。

    返回 ``{"total": 命中条数, "items": 明细, "days": 实际扫描的天数}``。
    """
    directory = _ledger_dir(data_path)
    if directory is None:
        return {"total": 0, "items": [], "days": 0}
    wanted_kind = str(kind or "").strip()
    needle = str(keyword or "").strip().lower()
    collected: List[Dict[str, Any]] = []
    scanned = 0
    for day in _iter_days(days):
        path = _file_for(directory, day)
        if not path.exists():
            continue
        scanned += 1
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
                    if wanted_kind and item.get("kind") != wanted_kind:
                        continue
                    if needle:
                        haystack = " ".join(
                            str(item.get(name) or "")
                            for name in ("reason", "resource_title", "reference", "provider")
                        ).lower()
                        if needle not in haystack:
                            continue
                    collected.append(item)
        except Exception as error:  # noqa: BLE001
            logger.debug(f"读取账本 {path.name} 失败：{error}", exc_info=True)
    # 文件内是追加顺序，最新在后；整体按时间倒序展示。
    collected.reverse()
    total = len(collected)
    start = max(0, int(offset or 0))
    size = max(1, int(limit or 200))
    return {"total": total, "items": collected[start:start + size], "days": scanned}


def summarize(data_path: Any, *, days: int = 7) -> Dict[str, Any]:
    """按类型与来源聚合最近 N 天账本，供页面顶部卡片展示。"""
    directory = _ledger_dir(data_path)
    if directory is None:
        return {"total": 0, "by_kind": {}, "by_provider": {}, "by_day": {}}
    by_kind: Dict[str, int] = {}
    by_provider: Dict[str, int] = {}
    by_day: Dict[str, int] = {}
    total = 0
    for day in _iter_days(days):
        path = _file_for(directory, day)
        if not path.exists():
            continue
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
                    total += 1
                    by_day[day] = by_day.get(day, 0) + 1
                    kind = str(item.get("kind") or "unknown")
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                    provider = str(item.get("provider") or "未知来源")
                    by_provider[provider] = by_provider.get(provider, 0) + 1
        except Exception as error:  # noqa: BLE001
            logger.debug(f"聚合账本 {path.name} 失败：{error}", exc_info=True)
    return {
        "total": total,
        "by_kind": by_kind,
        "by_provider": by_provider,
        "by_day": by_day,
    }


def purge_old(data_path: Any, *, keep_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删除超过保留天数的账本文件，返回删除个数。"""
    directory = _ledger_dir(data_path)
    if directory is None:
        return 0
    keep = set(_iter_days(keep_days))
    removed = 0
    try:
        for path in directory.glob(f"{_FILE_PREFIX}*{_FILE_SUFFIX}"):
            day = path.stem[len(_FILE_PREFIX):]
            if day in keep:
                continue
            try:
                os.remove(path)
                removed += 1
            except OSError as error:  # noqa: BLE001
                logger.debug(f"清理账本 {path.name} 失败：{error}", exc_info=True)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"清理账本目录失败：{error}", exc_info=True)
    return removed
