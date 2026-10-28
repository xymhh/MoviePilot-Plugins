"""候选资源账本：逐条记录订阅搜索到的候选资源及其去向。

线上痛点（2026-10-24 复盘）：一轮扫描里 396 次跳过/失败，用户在界面上只能看到
最终「没转存」的结果，看不到「为什么没转存」——是分享失效、标题不符、还是画质
不达标。日志里有原因，但需要翻容器日志。

本模块把每次同步的候选去向写进插件私有库 ``candidate_ledger_entries`` 表，
供 ``/ledger`` 接口与前端页面查询：

* 只追加，不修改，按 ``day`` 分桶，超过保留天数按天清理；
* 存储细节交给 ``core.database.repositories.CandidateLedgerRepository``，
  这里只负责字段白名单与截断，保证每行可控；
* 写入失败只记 debug 日志，绝不影响转存主流程。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable

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
        store: Any,
        records: Iterable[Dict[str, Any]],
        *,
        run_id: str = "",
) -> int:
    """把一批候选记录追加到账本，返回实际写入条数。

    任何异常都被吞掉：账本是辅助观测数据，不能因为它中断同步。
    """
    if store is None:
        return 0
    items = [item for item in (_clean(record) for record in records or []) if item]
    if not items:
        return 0
    try:
        return int(store.append(items, run_id=run_id) or 0)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"写入候选账本失败：{error}", exc_info=True)
        return 0


def read_records(
        store: Any,
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
    if store is None:
        return {"total": 0, "items": [], "days": 0}
    try:
        result = store.read(
            days=max(1, int(days or 1)),
            kind=kind,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )
    except Exception as error:  # noqa: BLE001
        logger.debug(f"读取候选账本失败：{error}", exc_info=True)
        return {"total": 0, "items": [], "days": 0}
    return result if isinstance(result, dict) else {"total": 0, "items": [], "days": 0}


def summarize(store: Any, *, days: int = 7) -> Dict[str, Any]:
    """按类型与来源聚合最近 N 天账本，供页面顶部卡片展示。"""
    if store is None:
        return {"total": 0, "by_kind": {}, "by_provider": {}, "by_day": {}}
    try:
        result = store.summary(days=max(1, int(days or 7)))
    except Exception as error:  # noqa: BLE001
        logger.debug(f"聚合候选账本失败：{error}", exc_info=True)
        return {"total": 0, "by_kind": {}, "by_provider": {}, "by_day": {}}
    return result if isinstance(result, dict) else {
        "total": 0, "by_kind": {}, "by_provider": {}, "by_day": {},
    }


def purge_old(store: Any, *, keep_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """删除超过保留天数的账本行，返回删除条数。"""
    if store is None:
        return 0
    try:
        return int(store.purge(keep_days=max(1, int(keep_days or 1))) or 0)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"清理候选账本失败：{error}", exc_info=True)
        return 0
