"""重复订阅治理接口。

背景：同一部剧常被搜出多张订阅卡片（来源站点不同、季集写法不同），
同步时只处理最早创建的一张，其余静默跳过——用户只能在日志里看到一行警告。

这里把同步时发现的重复组暴露出来，并提供「暂停多余卡片」的动作：

* 只改 ``state``，不删除数据，随时可以在 MP 订阅页恢复；
* 执行前由用户在前端点按钮触发，不做后台静默批量操作；
* 每一步都写候选账本，便于回溯谁被暂停了。
"""

from typing import Any, Dict, List


from ..delegation import OwnerDelegator


class DuplicateApi(OwnerDelegator):
    """列出重复订阅组，并按用户确认暂停多余卡片。"""

    def api_duplicate_list(self) -> dict:
        """返回最近一次同步发现的重复订阅组。"""
        groups = self._duplicate_groups()
        total = sum(len(group.get("duplicates") or []) for group in groups)
        return {"success": True, "data": {"groups": groups, "duplicate_total": total}}


    def _duplicate_groups(self) -> List[Dict[str, Any]]:
        owner = getattr(self, "_owner", None)
        groups = getattr(owner, "_duplicate_subscribe_groups", None)
        return list(groups) if isinstance(groups, list) else []

