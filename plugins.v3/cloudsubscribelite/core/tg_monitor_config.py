"""TgMonitorConfigMixin：CloudSubscribeLite 入口拆出的混入类。

方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
"""

from __future__ import annotations

from app.log import logger
from typing import Any, Dict, List


class TgMonitorConfigMixin:
    """TgMonitorConfigMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def _apply_tg_monitor_config(self, config: Dict[str, Any]) -> None:
        """应用 TG 频道监控配置并启动/停止扫描线程。"""
        self._tg_monitor_enabled = bool(config.get("tg_monitor_enabled", False))
        try:
            self._tg_api_id = int(config.get("tg_api_id", 0) or 0)
        except (TypeError, ValueError):
            self._tg_api_id = 0
        self._tg_api_hash = str(config.get("tg_api_hash", "") or "").strip()
        self._tg_session = str(config.get("tg_session", "") or "").strip()
        self._tg_channels = self._split_multiline(config.get("tg_channels", ""))
        raw_channel_rules = config.get("tg_channel_rules", [])
        if isinstance(raw_channel_rules, str):
            self._tg_channel_rules = [raw_channel_rules] if raw_channel_rules.strip() else []
        elif isinstance(raw_channel_rules, (list, tuple)):
            self._tg_channel_rules = list(raw_channel_rules)
        else:
            self._tg_channel_rules = []
        self._tg_monitor_keywords = self._split_multiline(
            config.get("tg_monitor_keywords", "")
        )
        self._tg_monitor_exclude_keywords = self._split_multiline(
            config.get("tg_monitor_exclude_keywords", "")
        )
        try:
            scan_limit = int(config.get("tg_monitor_scan_limit", 20) or 20)
        except (TypeError, ValueError):
            scan_limit = 20
        self._tg_monitor_scan_limit = max(1, min(scan_limit, 200))
        # 1.6.64 前的「全局频道 + 全局关键词」：一次性展开成逐频道规则并写回配置，
        # 之后只保留 tg_channel_rules 一个来源（旧键读一次即清空，避免两套配置互相覆盖）。
        if not self._tg_channel_rules and self._tg_channels:
            from .tg_monitor import migrate_legacy_channel_rules
            migrated_rules = migrate_legacy_channel_rules(
                [],
                channels=self._tg_channels,
                keywords=self._tg_monitor_keywords,
                exclude_keywords=self._tg_monitor_exclude_keywords,
                scan_limit=self._tg_monitor_scan_limit,
            )
            self._tg_channel_rules = migrated_rules
            logger.info(
                f"TG 频道规则为空，已把旧版全局配置迁移为逐频道规则："
                f"{len(migrated_rules)} 个频道；旧键 tg_channels / "
                "tg_monitor_keywords / tg_monitor_exclude_keywords 已清空"
            )
            self._tg_channels = []
            self._tg_monitor_keywords = []
            self._tg_monitor_exclude_keywords = []
            self._persist_config_values(
                tg_channel_rules=migrated_rules,
                tg_channels="",
                tg_monitor_keywords="",
                tg_monitor_exclude_keywords="",
            )
        try:
            interval = int(config.get("tg_monitor_interval_minutes", 10) or 10)
        except (TypeError, ValueError):
            interval = 10
        self._tg_monitor_interval_minutes = max(1, min(interval, 1440))
        try:
            max_items = int(config.get("tg_monitor_max_items", 500) or 500)
        except (TypeError, ValueError):
            max_items = 500
        self._tg_monitor_max_items = max(50, min(max_items, 5000))
        # 白名单模式下与本频道关注订阅无关的消息是否仍入池。
        # 默认 False：只保留最近 50 条供回补匹配，其余不进池，避免占满配额。
        quality_preferences = {
            "enabled": config.get("tg_quality_preference_enabled", True),
            "resolution_order": config.get(
                "tg_quality_resolution_order", "4K,1080P,720P"
            ),
            "hdr_order": config.get(
                "tg_quality_hdr_order", "DOLBY VISION,HDR10+,HDR10,HDR,SDR"
            ),
            "codec_order": config.get(
                "tg_quality_codec_order", "HEVC,AVC,AV1"
            ),
            "preferred_groups": config.get("tg_quality_preferred_groups", ""),
        }
        if self._tg_monitor_enabled and not (
                self._tg_api_id and self._tg_api_hash and self._tg_session):
            self._tg_monitor_enabled = False
            if config.get("tg_monitor_enabled"):
                logger.info(
                    "TG 频道监控已关闭：缺少 api_id / api_hash / 会话，"
                    "请在插件设置里先登录"
                )
        # 渠道库是 TG 规则的唯一运行来源：上面仍保留读配置（含旧全局配置的
        # 一次性迁移），只是作为迁移来源；运行态一律以渠道库为准。
        self._tg_channel_rules = self._tg_channel_rules_from_store()
        try:
            from .tg_monitor import get_channel_monitor
            monitor = get_channel_monitor(self)
            monitor.configure(
                enabled=self._tg_monitor_enabled,
                api_id=self._tg_api_id,
                api_hash=self._tg_api_hash,
                session=self._tg_session,
                channels=self._tg_channels,
                channel_rules=self._tg_channel_rules,
                keywords=self._tg_monitor_keywords,
                exclude_keywords=self._tg_monitor_exclude_keywords,
                scan_limit=self._tg_monitor_scan_limit,
                interval_minutes=self._tg_monitor_interval_minutes,
                max_items=self._tg_monitor_max_items,
                quality_preferences=quality_preferences,
                auto_transfer_handler=self._auto_transfer_tg_item,
                auto_transfer_batch_handler=self._auto_transfer_tg_batch,
                subscribe_provider=self._tg_subscribe_rows,
            )
        except Exception as error:  # noqa: BLE001 - 监控不可用不应拖垮插件
            logger.warning(f"TG 频道监控初始化失败：{error}")


    def _tg_subscribe_rows(self) -> List[Dict[str, Any]]:
        """给频道监控提供订阅匹配所需的精简字段。"""
        from app.db.subscribe_oper import SubscribeOper

        rows: List[Dict[str, Any]] = []
        for sub in SubscribeOper().list() or []:
            media_type = getattr(sub, "type", "")
            media_type = getattr(media_type, "value", media_type)
            rows.append({
                "id": int(getattr(sub, "id", 0) or 0),
                "name": str(getattr(sub, "name", "") or ""),
                "season": int(getattr(sub, "season", 0) or 0),
                "type": str(media_type or ""),
                "year": str(getattr(sub, "year", "") or ""),
                # 还缺几集：TG 队列靠它决定「先补集还是先洗版」。
                # 订阅模型字段是 lack_episode；旧代码读 lack 恒为 0，
                # 结果队列 100% 按洗版排（1.7.110 修正）。
                "lack_episode": int(getattr(sub, "lack_episode", 0) or 0),
            })
        return rows


    @staticmethod
    def _tg_target_episodes(items: Any) -> List[int]:
        """合并多条频道消息的目标集；任一条不限集则整体不限（返回空）。"""
        rows = list(items) if isinstance(items, (list, tuple)) else [items]
        merged: set = set()
        for row in rows:
            values = (row or {}).get("target_episodes") or []
            if not values:
                return []
            for value in values:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    merged.add(number)
        return sorted(merged)


    def _auto_transfer_tg_item(self, item: Dict[str, Any], subscribe_id: int) -> Dict[str, Any]:
        """频道规则命中自动转存：统一走 ``submit_candidate`` 入口。"""
        from .services.discovery import Candidate, submit_candidate

        candidate = Candidate(
            links=list((item or {}).get("links") or []),
            title=str((item or {}).get("title") or "").strip(),
            subscribe_id=int(subscribe_id or 0),
            channel=str((item or {}).get("channel") or ""),
            message_id=int((item or {}).get("message_id") or 0),
            run_id=str((item or {}).get("run_id") or ""),
        )
        # 1.7.110：把「这条消息只覆盖哪几集」带到同步链，避免单集消息触发整季洗版
        targets = self._tg_target_episodes(item)
        extra = {"target_episodes": targets} if targets else None
        return submit_candidate(
            self, candidate, wait=True, extra_payload=extra,
        ).to_payload()


    def _auto_transfer_tg_batch(
            self, items: List[Dict[str, Any]], subscribe_id: int,
    ) -> Dict[str, Any]:
        """同一订阅的多条频道资源合并成一次手动转存（同样走 submit_candidate）。"""
        from .services.discovery import MAX_LINKS as MAX_BATCH_LINKS
        from .services.discovery import Candidate, submit_candidate

        links: List[str] = []
        titles: Dict[str, str] = {}
        channel = ""
        for item in items or []:
            title = str((item or {}).get("title") or "").strip()
            channel = channel or str((item or {}).get("channel") or "")
            for link in (item or {}).get("links") or []:
                text = str(link or "").strip()
                if not text or text in titles:
                    continue
                links.append(text)
                titles[text] = title
                if len(links) >= MAX_BATCH_LINKS:
                    break
            if len(links) >= MAX_BATCH_LINKS:
                break
        if not links:
            return {"success": False, "message": "没有可转存的网盘链接"}
        candidate = Candidate(
            links=links,
            title=" / ".join(dict.fromkeys(t for t in titles.values() if t))[:200],
            subscribe_id=int(subscribe_id or 0),
            channel=channel,
        )
        # 批量里每条消息标题不同，逐链接标题由 extra_payload 精确透传；
        # 目标集取并集，让这次同步只覆盖这些消息实际包含的集数。
        extra_payload: Dict[str, Any] = {"resource_titles": titles}
        targets = self._tg_target_episodes(items)
        if targets:
            extra_payload["target_episodes"] = targets
        return submit_candidate(
            self, candidate, wait=True, extra_payload=extra_payload,
        ).to_payload()
