"""全局转存暂停开关。

场景：更新插件 / 重启 MoviePilot 时，正在跑的转存会被打断，租约过期后
重新认领会消耗一次 ``attempt``。用户希望「先别接新活」，等更新完再继续，
而不是让每个排队作业都被掐一次。

开关落在 ``runtime_records``（``sync_pause`` / ``global``），重启后保持；
只影响**认领新作业**，不打断正在写的文件。

仓储由调用方注入：``DbOper`` 子类的构造需要数据库管理器，本模块不能自己
``new`` 一个（1.8.7 的 ``RuntimeRecordRepository()`` 无参调用会抛
``DbOper.__init__() missing 1 required positional argument``，把任务中心整条
接口带崩）。没注入时读操作按「未暂停」处理，写操作明确报错。
"""

from __future__ import annotations

import time
from typing import Any, Dict

from app.log import logger

NAMESPACE = "sync_pause"
KEY = "global"


def _empty_snapshot() -> Dict[str, Any]:
    return {"paused": False, "updated_at": 0, "actor": "", "reason": ""}


def snapshot(repository: Any = None) -> Dict[str, Any]:
    """返回 ``{"paused": bool, "updated_at": int, "actor": str, "reason": str}``。

    ``repository`` 必须是 ``RuntimeRecordRepository`` 实例（由插件注入）；
    为 None 时按「未暂停」处理，避免暂停开关的故障拖垮调用方。
    """
    if repository is None:
        logger.debug("转存暂停开关：未注入仓储，按未暂停处理")
        return _empty_snapshot()
    try:
        data = repository.load(NAMESPACE, KEY, {}) or {}
    except Exception as error:  # 读失败不能把转存一起停掉
        logger.warning(f"读取转存暂停开关失败：{error}")
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "paused": bool(data.get("paused")),
        "updated_at": int(data.get("updated_at") or 0),
        "actor": str(data.get("actor") or ""),
        "reason": str(data.get("reason") or ""),
    }


def is_paused(repository: Any = None) -> bool:
    return bool(snapshot(repository).get("paused"))


def set_paused(paused: bool, actor: str = "", repository: Any = None) -> Dict[str, Any]:
    """切换暂停状态并落库；返回最新快照。没有仓储时明确报错，不静默吞掉。"""
    if repository is None:
        raise RuntimeError("转存暂停开关不可用：数据库仓储未就绪")
    payload = {
        "paused": bool(paused),
        "updated_at": int(time.time()),
        "actor": str(actor or "")[:64],
        "reason": "" if paused else "用户恢复转存",
    }
    try:
        repository.save(NAMESPACE, KEY, payload)
    except Exception as error:
        logger.error(f"写入转存暂停开关失败：{error}")
        raise
    logger.info(
        f"转存{'已暂停' if payload['paused'] else '已恢复'}"
        f"{'（操作人：' + payload['actor'] + '）' if payload['actor'] else ''}"
    )
    return snapshot(repository)


__all__ = ["KEY", "NAMESPACE", "is_paused", "set_paused", "snapshot"]
