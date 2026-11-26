"""PluginUtilsMixin：CloudSubscribeLite 入口拆出的混入类。

方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
"""

from __future__ import annotations

from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType
from apscheduler.triggers.cron import CronTrigger
from typing import Any, Dict
import ast
import json
import pytz


class PluginUtilsMixin:
    """PluginUtilsMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def _cron_is_valid(cron_expr: str) -> bool:
        """仅校验 cron 表达式是否合法,不再强制最小间隔"""
        cron_expr = (cron_expr or "").strip()
        if not cron_expr:
            return False
        try:
            tz = pytz.timezone(settings.TZ)
            CronTrigger.from_crontab(cron_expr, timezone=tz)
            return True
        except Exception:
            return False


    def _resolve_notification_type(value: Any) -> NotificationType:
        """将配置值解析为消息类型，无效值回退为插件消息。"""
        if isinstance(value, NotificationType):
            return value
        configured = str(value or NotificationType.Plugin.name).strip()
        if configured in NotificationType.__members__:
            return NotificationType[configured]
        for item in NotificationType:
            if item.value == configured:
                return item
        logger.warning(f"未知消息通知类型：{configured}，已回退为插件")
        return NotificationType.Plugin


    def _raise_file_limit(target: int = 65535) -> None:
        """把进程的软 nofile 上限抬到 ``target``（受核允许的硬上限约束）。

        容器里默认 ``soft=1024``，跨盘并发下载（每个分片一个连接）会把它撞爆，
        表现为 ``Errno 24 Too many open files`` 加掉速。硬上限通常已是 524288，
        所以插件自己就能抬，不必改 docker compose。
        """
        try:
            import resource

            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            if hard == resource.RLIM_INFINITY or int(hard) < 0:
                desired = int(target)
            else:
                desired = min(int(target), int(hard))
            if int(soft) >= desired:
                return
            resource.setrlimit(resource.RLIMIT_NOFILE, (desired, hard))
            logger.info(f"已提升进程文件句柄上限：soft {soft} -> {desired}（hard {hard}）")
        except Exception as error:  # noqa: BLE001 - 抬不动不影响插件可用性
            logger.warning(f"提升进程文件句柄上限失败（不影响运行）：{error}")


    def _parse_period_episode_rules(value: Any) -> Dict[str, int]:
        """解析「节目名 = 每期集数」多行配置。

        例：``现在就出发 = 2`` 表示该节目每期分上下两集，第3期上 → E05。
        只接受 1~10 之间的整数，其余行静默忽略，避免把写错的配置带进匹配。
        """
        rules: Dict[str, int] = {}
        for raw_line in str(value or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            normalized = line.replace("：", "=").replace("＝", "=")
            if "=" not in normalized:
                continue
            key, _, raw_size = normalized.partition("=")
            key = key.strip()
            try:
                size = int(str(raw_size).strip())
            except (TypeError, ValueError):
                continue
            if key and 1 <= size <= 10:
                rules[key] = size
        return rules


    def _split_multiline(value: Any) -> list:
        """把设置页的多行/逗号文本拆成去重列表。

        兼容 MP 把多行字段存成列表、或存成 ``['a', 'b']`` / ``["a"]``
        字符串的情况（前端编辑后原样提交）。
        """
        if isinstance(value, (list, tuple, set)):
            parts = [str(item).strip() for item in value]
        else:
            text = str(value or "").strip()
            if text.startswith("[") and text.endswith("]"):
                parsed = None
                for loader in (json.loads, ast.literal_eval):
                    try:
                        parsed = loader(text)
                        break
                    except (ValueError, SyntaxError, TypeError):
                        continue
                if isinstance(parsed, (list, tuple, set)):
                    text = "\n".join(str(item) for item in parsed)
                else:
                    text = text.strip("[]")
            parts = [
                item.strip().strip("'\"").strip()
                for item in text.replace(",", "\n").replace("，", "\n").splitlines()
            ]
        return [item for item in dict.fromkeys(parts) if item]
