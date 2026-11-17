"""候选资源账本查询接口。

对应页面：插件「资源账本」页签。数据来自 ``core/services/candidate_ledger``
写入的 ``candidate_ledger_entries`` 表，读失败时返回空结果而不是报错。
"""

import datetime

from app.log import logger

from ..delegation import OwnerDelegator
from ..services.candidate_ledger import read_records


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
        store = self._ledger_store()
        if store is None:
            return {"success": True, "data": {"total": 0, "items": [], "days": 0}}
        try:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            result = read_records(
                store,
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



    def _ledger_store(self):
        """账本存储；插件还没初始化时返回 None，由调用方降级为空结果。"""
        owner = getattr(self, "_owner", None)
        getter = getattr(owner, "_get_data_store", None)
        if callable(getter):
            try:
                return getter().ledger_store()
            except Exception as error:  # noqa: BLE001
                logger.debug(f"获取候选账本存储失败：{error}", exc_info=True)
        return None
