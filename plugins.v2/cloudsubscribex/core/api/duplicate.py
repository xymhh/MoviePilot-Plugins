"""重复订阅治理接口。

背景：同一部剧常被搜出多张订阅卡片（来源站点不同、季集写法不同），
同步时只处理最早创建的一张，其余静默跳过——用户只能在日志里看到一行警告。

这里把同步时发现的重复组暴露出来，并提供「暂停多余卡片」的动作：

* 只改 ``state``，不删除数据，随时可以在 MP 订阅页恢复；
* 执行前由用户在前端点按钮触发，不做后台静默批量操作；
* 每一步都写候选账本，便于回溯谁被暂停了。
"""

from typing import Any, Dict, List, Optional

from app.log import logger

from .. import OwnerDelegator
from ..services.candidate_ledger import append_records

#: MoviePilot 订阅状态：S = 暂停。
PAUSED_STATE = "S"


class DuplicateApi(OwnerDelegator):
    """列出重复订阅组，并按用户确认暂停多余卡片。"""

    def api_duplicate_list(self) -> dict:
        """返回最近一次同步发现的重复订阅组。"""
        groups = self._duplicate_groups()
        total = sum(len(group.get("duplicates") or []) for group in groups)
        return {"success": True, "data": {"groups": groups, "duplicate_total": total}}

    def api_duplicate_pause(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        """暂停指定订阅卡片（保留最早创建的那张）。

        ``payload`` 形如 ``{"ids": [12, 13]}``；也可传 ``{"all": true}``
        一次暂停最近一次同步发现的所有重复卡片。
        """
        payload = payload or {}
        raw_ids = payload.get("ids") or []
        if payload.get("all"):
            raw_ids = [
                int(item.get("id") or 0)
                for group in self._duplicate_groups()
                for item in (group.get("duplicates") or [])
            ]
        if not isinstance(raw_ids, list):
            return {"success": False, "message": "参数无效：ids 必须是数组"}

        keep_ids = {
            int(group.get("keep_id") or 0)
            for group in self._duplicate_groups()
        }
        targets: List[int] = []
        for value in raw_ids[:200]:
            try:
                subscribe_id = int(value)
            except (TypeError, ValueError):
                continue
            if subscribe_id <= 0 or subscribe_id in keep_ids:
                # 绝不动「保留项」，避免用户误把所有卡片都停掉。
                continue
            if subscribe_id not in targets:
                targets.append(subscribe_id)
        if not targets:
            return {"success": False, "message": "没有可暂停的重复订阅"}

        paused: List[int] = []
        failed: List[Dict[str, Any]] = []
        try:
            # 延迟导入：插件模块加载阶段不触碰 app.db，避免在 MP 启动 /
            # 更新流程里因数据库尚未就绪而阻塞插件加载。
            from app.db.subscribe_oper import SubscribeOper

            oper = SubscribeOper()
            for subscribe_id in targets:
                try:
                    updated = oper.update(subscribe_id, {"state": PAUSED_STATE})
                    if updated:
                        paused.append(subscribe_id)
                    else:
                        failed.append({"id": subscribe_id, "error": "订阅不存在或未更新"})
                except Exception as error:  # noqa: BLE001 - 单条失败不影响其余
                    failed.append({"id": subscribe_id, "error": str(error)})
        except Exception as error:  # noqa: BLE001
            logger.error(f"暂停重复订阅失败：{error}")
            return {"success": False, "message": str(error)}

        self._write_ledger(paused)
        logger.info(
            f"已暂停 {len(paused)} 个重复订阅卡片："
            + ", ".join(f"#{item}" for item in paused[:10])
        )
        return {
            "success": bool(paused) or not failed,
            "data": {"paused": paused, "failed": failed},
            "message": (
                f"已暂停 {len(paused)} 个重复订阅"
                if not failed
                else f"已暂停 {len(paused)} 个，失败 {len(failed)} 个"
            ),
        }

    def _duplicate_groups(self) -> List[Dict[str, Any]]:
        owner = getattr(self, "_owner", None)
        groups = getattr(owner, "_duplicate_subscribe_groups", None)
        return list(groups) if isinstance(groups, list) else []

    def _write_ledger(self, paused: List[int]) -> None:
        """暂停动作也落账本：事后能回答「这张卡片什么时候被停的」。"""
        if not paused:
            return
        owner = getattr(self, "_owner", None)
        getter = getattr(owner, "get_data_path", None)
        data_path = getter() if callable(getter) else None
        if not data_path:
            return
        try:
            names = {
                str(int(item.get("id") or 0)): str(item.get("name") or "")
                for group in self._duplicate_groups()
                for item in (group.get("duplicates") or [])
            }
            append_records(
                data_path,
                [
                    {
                        "kind": "duplicate_paused",
                        "provider": "订阅",
                        "reason": "重复订阅，已暂停多余卡片（保留最早创建的一张）",
                        "reference": f"subscribe#{subscribe_id}",
                        "resource_title": names.get(str(subscribe_id), ""),
                    }
                    for subscribe_id in paused
                ],
            )
        except Exception as error:  # noqa: BLE001
            logger.debug(f"写入重复订阅账本失败：{error}", exc_info=True)
