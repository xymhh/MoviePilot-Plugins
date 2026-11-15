"""插件配置归一化（纯函数）。

从 ``core/config.py``（Vue 配置页，依赖 app.db 等宿主模块）里拆出来：
归一化只做字典转换，不需要任何宿主依赖，放在这里可供插件入口
（``__init__.py``）直接引用，不把重依赖拖进入口的导入链。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.log import logger


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
