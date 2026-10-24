"""候选资源账本查询接口。

对应页面：插件「资源账本」页签。数据来自 ``core/services/candidate_ledger``
落盘的按天 JSONL，不依赖数据库，读失败时返回空结果而不是报错。
"""

import datetime
from typing import Any

from app.log import logger

from .. import OwnerDelegator
from ..services.candidate_ledger import read_records, summarize
from ...utils.cache import cache_stats


class LedgerApi(OwnerDelegator):
    """提供候选资源账本的分页查询与聚合摘要。"""

    def api_ledger_list(
            self,
            days: int = 3,
            kind: str = "",
            keyword: str = "",
            limit: int = 200,
            offset: int = 0,
    ) -> dict:
        """按天倒序分页返回候选资源去向，可按类型与关键词过滤。"""
        data_path = self._ledger_data_path()
        if data_path is None:
            return {"success": True, "data": {"total": 0, "items": [], "days": 0}}
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            result = read_records(
                data_path,
                days=max(1, min(int(days or 1), 30)),
                kind=kind,
                keyword=keyword,
                limit=max(1, min(int(limit or 200), 1000)),
                offset=max(0, int(offset or 0)),
            )
            result["today"] = today
            return {"success": True, "data": result}
        except Exception as error:  # noqa: BLE001 - 查询失败不影响宿主
            logger.error(f"读取候选账本失败：{error}")
            return {"success": False, "message": str(error), "data": {"total": 0, "items": []}}

    def api_ledger_summary(self, days: int = 7) -> dict:
        """最近 N 天的候选去向聚合，用于页面顶部统计卡片。"""
        data_path = self._ledger_data_path()
        if data_path is None:
            return {"success": True, "data": {"total": 0, "by_kind": {}, "by_provider": {}, "by_day": {}}}
        try:
            data: Any = summarize(data_path, days=max(1, min(int(days or 7), 30)))
            return {"success": True, "data": data}
        except Exception as error:  # noqa: BLE001
            logger.error(f"聚合候选账本失败：{error}")
            return {"success": False, "message": str(error), "data": {"total": 0}}

    def api_cache_stats(self) -> dict:
        """缓存健康度：编码失败 / 解码失败 / 命中率，用于判断缓存层是否退化。"""
        try:
            return {"success": True, "data": cache_stats()}
        except Exception as error:  # noqa: BLE001
            logger.error(f"读取缓存健康度失败：{error}")
            return {"success": False, "message": str(error), "data": {}}

    def _ledger_data_path(self):
        """插件数据目录；拿不到时返回 None，由调用方降级为空结果。"""
        owner = getattr(self, "_owner", None)
        getter = getattr(owner, "get_data_path", None)
        if callable(getter):
            try:
                return getter()
            except Exception as error:  # noqa: BLE001
                logger.debug(f"获取插件数据目录失败：{error}")
        return None
