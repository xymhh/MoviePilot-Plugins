"""订阅搜索接管时段与洗版开关管理。"""

import datetime
from typing import Any

import pytz
from app.core.config import settings

from ...core import OwnerDelegator


def is_cloud_upgrade_subscribe(
        enable_cloud_upgrade: bool,
        upgrade_subscribe_ids: Any,
        subscribe: Any,
        respect_manual_upgrade: bool = True,
) -> bool:
    """判断订阅是否属于插件网盘洗版范围（服务层唯一实现）。

    三处旧定义已收敛到这里（handlers/search/service.py、
    handlers/sync/service.py、SubscriptionControlService 各自只留一行委托）：
    搜索/同步处理器认手动洗版快捷方式（subscribe._manual_upgrade 为真时
    直接返回 True）；订阅控制服务不认——其调用方 core/api/sync.py 的
    _manual_upgrade_for_subscribe 明确要求 best_version，调用时传
    respect_manual_upgrade=False 保留该语义。逐一核对过全部调用方：
    api/sync.py:44 的订阅现取自数据库、从不带 _manual_upgrade，
    services/runtime.py:685 的调用方自己先 or 了 manual_upgrade，
    其余调用方本来就走带快捷方式的处理器版本，所以两种语义都与合一前一致。
    """
    if (
            enable_cloud_upgrade
            and respect_manual_upgrade
            and bool(getattr(subscribe, "_manual_upgrade", False))
    ):
        return True
    if (
            not enable_cloud_upgrade
            or not subscribe
            or not bool(getattr(subscribe, "best_version", False))
    ):
        return False
    selected_ids = {str(value) for value in (upgrade_subscribe_ids or [])}
    return not selected_ids or str(getattr(subscribe, "id", "")) in selected_ids


class SubscriptionControlService(OwnerDelegator):
    """管理订阅搜索接管时段与洗版状态。"""

    def _is_subscribe_excluded(self, subscribe_id: int) -> bool:
        """
        按订阅过滤模式判断订阅是否不归本插件处理

        - exclude 排除模式：勾选的订阅被排除，其余全部处理
        - include 指定模式：仅处理勾选的订阅，其余全部排除
        """
        if self._subscribe_filter_mode == "include":
            return subscribe_id not in set(self._include_subscribes or [])
        return subscribe_id in set(self._exclude_subscribes or [])

    def _is_time_in_block(self, time_str: str = None) -> bool:
        """
        判断指定时间（或当前时间）是否在屏蔽时间段内。
        仅在 block_system_subscribe=OFF 时生效。
        支持跨天时段（如 22:00 ~ 06:00）。
        """
        if self._block_system_subscribe:
            return False
        if not self._block_start_time or not self._block_end_time:
            return False

        tz = pytz.timezone(settings.TZ)
        now = datetime.datetime.now(tz=tz).strftime("%H:%M")
        check = time_str or now

        b_start = self._block_start_time.strip()
        b_end = self._block_end_time.strip()

        if b_start < b_end:
            return b_start <= check <= b_end
        else:
            return check >= b_start or check <= b_end

    def _is_takeover_active(self) -> bool:
        if not self._enabled:
            return False
        if self._block_system_subscribe:
            return True
        return self._is_time_in_block()

    def _is_cloud_upgrade_subscribe(self, subscribe) -> bool:
        """判断订阅是否属于插件网盘洗版范围（委托服务层唯一实现）。

        本服务不认手动洗版快捷方式：调用方 _manual_upgrade_for_subscribe
        （core/api/sync.py）要求订阅启用 best_version，见
        is_cloud_upgrade_subscribe 的 respect_manual_upgrade 参数。
        """
        return is_cloud_upgrade_subscribe(
            self._enable_cloud_upgrade,
            self._upgrade_subscribe_ids,
            subscribe,
            respect_manual_upgrade=False,
        )
