"""插件配置归一化（纯函数）。

从 ``core/config.py``（Vue 配置页，依赖 app.db 等宿主模块）里拆出来：
归一化只做字典转换，不需要任何宿主依赖，放在这里可供插件入口
（``__init__.py``）直接引用，不把重依赖拖进入口的导入链。

分工（v1.24.0 收敛后）：
* ``normalize_plugin_config``：插件配置整体迁移——丢弃已移除渠道的历史
  字段、旧下载开关迁移为 platform_download_policy、未知策略回退；
* ``clamp_auto_subscribe_dates``：自动追更各榜单的起算年月钳制——唯一实现，
  ``UIConfig.normalize_config`` 与
  ``UIConfig.normalize_auto_subscribe_dates`` 都调它；
* ``normalize_channel_rules``：渠道规则校验/归一化，住在
  ``core/tg_monitor.py``（与 TG 监控状态耦合，不适合搬到这里）。
"""

from __future__ import annotations

import datetime
from typing import Any, Dict, Optional

from app.log import logger

#: 自动追更起算年月的键：7 个榜单来源各一对（mikan 多一个当年 year 键）。
_AUTO_SUBSCRIBE_YEAR_KEYS = (
    "auto_subscribe_douban_min_year",
    "auto_subscribe_tmdb_min_year",
    "auto_subscribe_bangumi_min_year",
    "auto_subscribe_anilist_min_year",
    "auto_subscribe_maoyan_min_year",
    "auto_subscribe_netflix_min_year",
    "auto_subscribe_mikan_year",
    "auto_subscribe_mikan_min_year",
)
_AUTO_SUBSCRIBE_MONTH_PROVIDERS = (
    "douban", "tmdb", "bangumi", "anilist", "maoyan", "netflix", "mikan",
)


def clamp_auto_subscribe_dates(config: Dict[str, Any]) -> Dict[str, Any]:
    """把自动追更各榜单的起算年月钳制到合法范围（原地改、返回同一字典）。

    年份：<= 0、缺失或非数字 → 当前年；正数原样保留（不过问未来年份）。
    月份：不在 1–12、缺失或非数字 → 当前月。
    旧的两处实现（``UIConfig.normalize_config`` 与
    ``UIConfig.normalize_auto_subscribe_dates``）规则并不一致：前者漏了
    ``auto_subscribe_anilist_min_year``，后者年份只处理 ``== 0``（负数会
    漏过去）；这里以“全 8 个年份键 + ``<= 0``”为准收敛。
    """
    current_year = datetime.datetime.now().year
    current_month = datetime.datetime.now().month
    for key in _AUTO_SUBSCRIBE_YEAR_KEYS:
        try:
            value = int(config.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value <= 0:
            config[key] = current_year
    for provider_id in _AUTO_SUBSCRIBE_MONTH_PROVIDERS:
        key = f"auto_subscribe_{provider_id}_min_month"
        try:
            month = int(config.get(key) or 0)
        except (TypeError, ValueError):
            month = 0
        config[key] = month if 1 <= month <= 12 else current_month
    return config


def normalize_plugin_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """清理并迁移插件配置，返回新副本（不写回）。

    只做纯转换，写回由调用方决定：

    * 丢弃已移除搜索渠道的历史配置（``butailing_`` 前缀的旧字段），避免
      旧字段继续进入运行态或保存结果；
    * 旧版两个布尔开关（block_platform_downloads / takeover_platform_downloads）
      迁移为 platform_download_policy（allow / block / cloud）后删除；
    * 未知的 platform_download_policy 回退为 block。
    """
    config = dict(config or {})
    # 丢弃已移除搜索渠道的历史配置，避免旧字段继续进入运行态或保存结果。
    removed_config_prefixes = ("but" + "ailing_",)
    config = {
        key: value
        for key, value in config.items()
        if not str(key).lower().startswith(removed_config_prefixes)
    }
    legacy_download_keys = (
        "block_platform_downloads", "takeover_platform_downloads"
    )
    if (
            "platform_download_policy" not in config
            and any(key in config for key in legacy_download_keys)
    ):
        config["platform_download_policy"] = (
            "cloud"
            if bool(config.get("takeover_platform_downloads", False))
            else "block"
            if bool(config.get("block_platform_downloads", True))
            else "allow"
        )
    for deprecated_key in (
            *legacy_download_keys, "unblock_start_time", "unblock_end_time"
    ):
        config.pop(deprecated_key, None)
    if "platform_download_policy" in config:
        policy = str(config.get("platform_download_policy") or "block").strip().lower()
        if policy not in {"allow", "block", "cloud"}:
            logger.warning(f"未知平台下载策略：{policy}，已回退为阻止平台下载")
            policy = "block"
        config["platform_download_policy"] = policy
    return config
