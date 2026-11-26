"""PluginConfigMixin：CloudSubscribeLite 入口拆出的混入类。

方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
"""

from __future__ import annotations

from app.core.config import settings
from app.log import logger
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, RLock, local
from typing import Any, Dict, Optional
from .aria2 import configure_aria2, resolve_data_dir
from .config import CONCURRENCY_AND_COOLDOWN_PARAMS, is_plugin_config_key
from .config_normalize import normalize_plugin_config
from .hook import MessageRoutingHook
from .media_scope import configure_media_path_ignores
from .services import SkipStatsCollector
from .services.upload_quota import UploadQuota, clamp_limit_gb
from ..drive.scanner import DriverRegistry
from ..handlers import WebhookHandler
from ..search.scanner import SearchSourceRegistry
from ..search.types import ensure_magnet_for_cloud_policy
from ..utils import FileMatcher, configure_magnet_metadata_url
from ..utils.file_parser import MediaFileParser
from ..utils.http_client import build_proxy_url, validate_proxy_address
from ..utils.notify_template import DEFAULT_TEMPLATES, apply_runtime_updates, upgrade_legacy_templates
import copy
import datetime
import pytz


class PluginConfigMixin:
    """PluginConfigMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def _apply_plugin_config(
            self, config: Optional[Dict[str, Any]], reset_runtime: bool = False
    ) -> None:
        """应用配置并重建相关服务；普通保存不重置同步任务状态。"""
        config = dict(config or {})
        original_config = dict(config)
        configure_media_path_ignores(config.get("media_server_ignore_paths", ""))
        config = normalize_plugin_config(config)
        if config != original_config:
            # 宿主仅在真正失败时返回 False，None 表示库中已是该配置（无需更新）。
            if self.update_config(config) is False:
                logger.warning("订阅接管配置迁移持久化失败，本次运行仍使用迁移后配置")
            else:
                logger.info("插件配置已清理并迁移到当前格式")
        hot_keys = {
            "show_sidebar_nav",
            "agent_enabled",
            "direct_transfer_enabled",
            "title_subscribe_enabled",
            "subscribe_all_missing_seasons",
            "subscribe_skip_season_zero",
            "subscribe_season_limit",
            "notify",
            "notification_type",
            "webhook_enabled",
            "webhook_url",
            "webhook_method",
            "webhook_timeout",
            "media_server_refresh_enabled",
            "media_servers",
            "media_server_ignore_paths",
            "media_server_path_mappings",
            "media_server_refresh_delay",
            "emby_mediainfo_enabled",
            "video_extensions",
            "subtitle_extensions",
        }
        changed_keys = set()
        if not reset_runtime and self._applied_config:
            changed_keys = {
                key for key in set(self._applied_config) | set(config)
                if self._applied_config.get(key) != config.get(key)
            }
            if not changed_keys:
                # v1.15.0：配置没变也重登一次下载器条目（幂等、条目已在
                # 时不写）；2026-11-18 线上条目丢过一次，init_plugin 外
                # 的热更新路径原来从不重登，运行中丢了无人恢复。
                self._ensure_downloader_entry()
                return
            if changed_keys <= hot_keys:
                self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
                self._agent_enabled = bool(config.get("agent_enabled", True))
                self._direct_transfer_enabled = bool(
                    config.get("direct_transfer_enabled", True)
                )
                self._title_subscribe_enabled = bool(
                    config.get("title_subscribe_enabled", True)
                )
                self._subscribe_all_missing_seasons = bool(config.get("subscribe_all_missing_seasons", True))
                self._subscribe_skip_season_zero = bool(config.get("subscribe_skip_season_zero", True))
                try:
                    self._subscribe_season_limit = max(0, int(config.get("subscribe_season_limit", 0)))
                except (TypeError, ValueError):
                    self._subscribe_season_limit = 0
                if "video_extensions" in changed_keys or "subtitle_extensions" in changed_keys:
                    video_exts, sub_exts = MediaFileParser.configure_extensions(
                        video_extensions=config.get("video_extensions"),
                        subtitle_extensions=config.get("subtitle_extensions"),
                    )
                    self._video_extensions = sorted(video_exts)
                    self._subtitle_extensions = sorted(sub_exts)
                self._apply_notification_config(config)
                self._get_component(MessageRoutingHook).install()
                self._applied_config = copy.deepcopy(config)
                from app.core.plugin import PluginManager
                PluginManager().clear_plugin_agent_tools_cache()
                logger.info("基础开关或扩展名/通知配置已热更新，网盘与搜索客户端保持运行")
                # v1.15.0：热更新出口也重登下载器条目（同上）。
                self._ensure_downloader_entry()
                return
        self.stop_service(preserve_subscribe_queue=not reset_runtime)
        self._stop_event = ThreadEvent()
        if reset_runtime:
            self._sync_running = False
            self._sync_status = "idle"
            self._sync_task_text = "当前没有订阅处理任务"
            self._sync_progress = 0
            self._sync_context = {}
            self._skip_stats = SkipStatsCollector()
            self._sync_run_started_at = 0
            self._sync_last_finished_at = 0
            self._sync_last_elapsed_ms = 0
            self._sync_tasks_lock = RLock()
            self._task_local = local()
            self._sync_tasks = {}
            with self._runtime_revision_lock:
                self._runtime_revision = 0
                self._history_revision = 0
            with self._pending_config_lock:
                self._pending_config = None
            self._subscribe_search_queue_lock = RLock()
            self._subscribe_search_pending = {}
            self._subscribe_search_active = {}
            self._subscribe_search_recent = {}
            self._subscribe_search_queue_shutdown = ThreadEvent()
            self._subscribe_search_coordinator_running = False
            self._subscribe_search_queue_revision = 0
            self._sync_operation_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="cloudsubscribe-sync-operation",
            )
            self._subscribe_search_originals = {}
            self._subscribe_chain_originals = {}
            self._platform_search_originals = {}
            self._interaction_originals = {}
        else:
            if self._sync_tasks_lock is None:
                self._sync_tasks_lock = RLock()
            if self._task_local is None:
                self._task_local = local()
            if self._sync_operation_executor is None:
                self._sync_operation_executor = ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="cloudsubscribe-sync-operation",
                )

        if config:
            for key, val in config.items():
                # 1.12.20 Phase 1：只允许白名单配置键落到实例属性上；
                # 白名单外的新键直接忽略并记 warning，绝不抛错。
                if not is_plugin_config_key(key):
                    logger.warning(f"忽略未知插件配置键：{key}（未进白名单）")
                    continue
                setattr(self, f"_{key}", val)
            # v1.30.0 阶段 4：funnel_enabled 总闸生效（kill-switch）。
            # 插件实例属性是上面白名单循环写的原始值，各处读到的必须
            # 是“总闸 AND 原始值”，否则只改一处关不掉 API 侧/运行侧的
            # 入队路。_init_handlers 在下面建 SyncHandler（它读插件
            # 属性），所以必须在此处、在建 handler 之前算出有效值。
            _funnel_master = bool(getattr(self, "_funnel_enabled", True))
            self._funnel_enabled = _funnel_master
            self._funnel_phase1_enabled = bool(
                _funnel_master and getattr(self, "_funnel_phase1_enabled", False)
            )
            self._funnel_phase2_enabled = bool(
                _funnel_master and getattr(self, "_funnel_phase2_enabled", False)
            )
            self._funnel_phase3_enabled = bool(
                _funnel_master and getattr(self, "_funnel_phase3_enabled", False)
            )
            # 渠道库是搜索源与 TG 频道的唯一运行来源：先迁移旧配置，
            # 再让 _apply_search_config / _apply_tg_monitor_config 读渠道库。
            self._migrate_channels_from_config(config)
            self._apply_base_config(config)
            self._apply_notification_config(config)
            self._cloud_drive_key = str(config.get("cloud_drive", "115") or "115").strip().lower()
            self._apply_search_config(config)
            self._apply_sync_config(config)

        # 初始化客户端/handlers
        self._init_clients()
        self._init_handlers()
        if self._sync_handler:
            # 1.12.14/1.12.15：清理插件以前写进 MP 整理历史的镜像记录
            # （PanSou → 115网盘CS版、复制等），幂等、每次启动都跑。
            # v1.28.1 例外（用户 2026-11-20 定方向 A）：直出 strm 时插件
            # 经 MP 官方外部登记映射补写一行（origin="external"、
            # downloader 为空、src 为真实 115 路径），清理的两个条件都
            # 匹配不到补写行，补写行不受影响。
            self._sync_handler.remove_platform_transfer_history_mirror()
            # v1.29.6：修复 v1.28.1 补写坏掉的 registration（缺 actor_id、
            # 多 note），否则 #/history 整页查不到（model_validate 抛错）。
            # 幂等、只动插件补写行、失败不挡启动。
            try:
                self._sync_handler.repair_backfill_registrations()
            except Exception as error:  # noqa: BLE001 - 修复失败不得影响插件主流程
                logger.warning(f"修复补写 registration 失败（不影响启动）：{error}")
            # v1.31.3：清漏斗孤儿待终态记录（行已挂起/已完成/不存在，
            # 离线监控跳过漏斗记录、worker 不管挂起行，记录成孤儿后
            # 任务卡永久 95%）。幂等、只动孤儿记录、失败不挡启动。
            # 必须在下面 _update_offline_monitor 之前跑，否则任务卡
            # 刷新用的计数还是旧的。
            try:
                self._sync_handler.repair_orphan_pending_records()
            except Exception as error:  # noqa: BLE001 - 修复失败不得影响插件主流程
                logger.warning(f"清孤儿待终态记录失败（不影响启动）：{error}")
        self._update_offline_monitor(
            self._sync_handler.count_offline_monitor_pending(
                self._sync_handler.get_pending_finalize_tasks()
            )
            if self._sync_handler else 0
        )
        if self._sync_handler:
            # v1.29.7 阶段 1：重启后续跑队列里未处理行（断点续跑）；
            # 开关关闭时不建 job，行保留、开关再开时继续处理。
            try:
                self._sync_handler.funnel_ensure_worker()
            except Exception as error:  # noqa: BLE001 - 恢复失败不挡启动
                logger.warning(f"恢复整理队列 worker 失败：{error}")
        service_config_keys = {
            "enabled", "cron", "auto_subscribe_enabled", "auto_subscribe_cron",
            "auto_subscribe_douban_enabled", "auto_subscribe_maoyan_enabled",
            "auto_subscribe_netflix_enabled", "auto_subscribe_mikan_enabled",
            "auto_subscribe_tmdb_enabled", "auto_subscribe_bangumi_enabled", "auto_subscribe_anilist_enabled",
            "checkin_cron", "checkin_auto_retry", "checkin_retry_count",
            "takeover_new_subscribes",
            "block_start_time", "block_end_time", "block_system_subscribe",
            "platform_download_policy", "block_platform_downloads",
            "takeover_platform_downloads",
            # v1.27.0：交给 MP 调度的插件维护任务
            "strm_generate_enabled", "strm_full_scan_enabled",
            "strm_increment_enabled", "strm_increment_minutes",
            "organize_schedule_enabled", "organize_schedule_cron",
            "organize_source_path", "organize_schedule_auto_execute",
            # v1.30.0 阶段 4：漏斗总闸/阶段 1 开关影响 04:00 扫描对账
            # 任务（CloudSubscribe_Organize）的注册，改动须刷新维护任务。
            "funnel_enabled", "funnel_phase1_enabled",
        }
        try:
            from .checkin_manager import get_checkin_definitions
            for defn in get_checkin_definitions().values():
                service_config_keys.add(f"{defn.key}_checkin_enabled")
                service_config_keys.add(f"{defn.key}_checkin_mode")
                if getattr(defn, "credential_keys", None):
                    service_config_keys.update(defn.credential_keys)
        except Exception as error:
            logger.debug(f"动态收集服务签到配置键异常: {error}", exc_info=True)
        if reset_runtime or changed_keys & service_config_keys:
            self._refresh_platform_services()
        self._install_subscribe_search_takeover()
        self._get_component(MessageRoutingHook).install()

        action = "插件初始化" if reset_runtime else "插件配置已应用"
        self._applied_config = copy.deepcopy(config)
        from app.core.plugin import PluginManager
        PluginManager().clear_plugin_agent_tools_cache()
        logger.info(
            f"{action}：代码版本={self.plugin_version}, "
            f"接管时段={self._block_start_time}~{self._block_end_time}, "
            f"平台下载策略={self._platform_download_policy}, "
            f"网盘洗版={'开启' if self._enable_cloud_upgrade else '关闭'}, "
            f"洗版模式={self._upgrade_mode}, "
            f"洗版范围={'指定订阅' if self._upgrade_subscribe_ids else '全部'}, "
            f"当前接管态={self._is_takeover_active()}")
        self._start_residue_cleanup()
        # v1.27.0：重试索引维护等固定周期任务改由 MP 调度
        # （get_service → _run_scheduled_task），不再挂插件自建调度器；
        # 开关/周期变化通过上面的 _refresh_platform_services 让 MP 重建服务。

        # 调度一次性任务。
        if self._auto_subscribe_onlyonce:
            logger.info("榜单自动订阅服务启动，立即运行一次")
            self._scheduler_registry().ensure_job(
                "cloudsubscribe_auto_subscribe_once",
                func=self._run_auto_subscribe_once,
                trigger="date",
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3)
                ),
            )
            self._auto_subscribe_onlyonce = False
            self._persist_config_values(auto_subscribe_onlyonce=False)

        # v1.15.0：配置应用完成重登下载器条目（同上）。
        self._ensure_downloader_entry()


    def _apply_notification_config(self, config: Dict[str, Any]) -> None:
        self._notify = bool(config.get("notify", False))
        self._notify_templates = {
            key: str(config.get(key) or "").strip() or default
            for key, default in DEFAULT_TEMPLATES.items()
        }
        self._notification_type = self._resolve_notification_type(
            config.get("notification_type")
        )
        self._webhook_enabled = bool(config.get("webhook_enabled", False))
        self._webhook_url = str(config.get("webhook_url", "") or "").strip()
        self._webhook_method = str(config.get("webhook_method", "POST") or "POST").upper()
        if self._webhook_method not in {"POST", "GET"}:
            self._webhook_method = "POST"
        self._webhook_timeout = max(
            1, min(int(config.get("webhook_timeout", 10) or 10), 120)
        )
        self._media_server_refresh_enabled = bool(
            config.get("media_server_refresh_enabled", False)
        )
        self._media_servers = list(config.get("media_servers", []) or [])
        self._media_server_ignore_paths = str(
            config.get("media_server_ignore_paths", "") or ""
        ).strip()
        configure_media_path_ignores(self._media_server_ignore_paths)
        self._media_server_path_mappings = str(
            config.get("media_server_path_mappings", "") or ""
        ).strip()
        self._media_server_refresh_delay = max(
            0, int(config.get("media_server_refresh_delay", 0) or 0)
        )
        self._emby_mediainfo_enabled = bool(
            config.get("emby_mediainfo_enabled", False)
        )
        if self._subscribe_handler:
            self._subscribe_handler._notify = self._notify
            self._subscribe_handler._notification_type = self._notification_type
        if self._sync_handler:
            self._sync_handler.update_notification_config(
                notify=self._notify,
                notification_type=self._notification_type,
                media_server_refresh_enabled=self._media_server_refresh_enabled,
                media_servers=self._media_servers,
                media_server_path_mappings=self._media_server_path_mappings,
                media_server_refresh_delay=self._media_server_refresh_delay,
                emby_mediainfo_enabled=self._emby_mediainfo_enabled,
            )
        self._webhook_handler = WebhookHandler(
            enabled=self._webhook_enabled,
            url=self._webhook_url,
            method=self._webhook_method,
            timeout=self._webhook_timeout,
        )


    def _apply_base_config(self, config: Dict[str, Any]) -> None:
        self._enabled = config.get("enabled", False)
        self._show_sidebar_nav = bool(config.get("show_sidebar_nav", True))
        self._agent_enabled = bool(config.get("agent_enabled", True))
        self._direct_transfer_enabled = bool(
            config.get("direct_transfer_enabled", True)
        )
        self._title_subscribe_enabled = bool(
            config.get("title_subscribe_enabled", True)
        )
        self._subscribe_all_missing_seasons = bool(config.get("subscribe_all_missing_seasons", True))
        self._subscribe_skip_season_zero = bool(config.get("subscribe_skip_season_zero", True))
        try:
            self._subscribe_season_limit = max(0, int(config.get("subscribe_season_limit", 0)))
        except (TypeError, ValueError):
            self._subscribe_season_limit = 0

        self._cron = (config.get("cron", self._cron) or "").strip()
        if self._cron and not self._cron_is_valid(self._cron):
            logger.warning(f"Cron 表达式无效：{self._cron}，已回退默认 0 18-23 * * *")
            self._cron = "0 18-23 * * *"

        self._auto_subscribe_enabled = bool(config.get("auto_subscribe_enabled", False))
        self._auto_subscribe_onlyonce = bool(config.get("auto_subscribe_onlyonce", False))
        self._auto_subscribe_cron = str(config.get("auto_subscribe_cron", "0 8 * * *") or "0 8 * * *").strip()

        self._subscribe_filter_mode = config.get("subscribe_filter_mode", "exclude") or "exclude"
        self._exclude_subscribes = config.get("exclude_subscribes", []) or []
        self._include_subscribes = config.get("include_subscribes", []) or []
        if self._subscribe_filter_mode == "include":
            logger.info(f"订阅过滤模式：指定模式，仅处理 {len(self._include_subscribes)} 个勾选订阅")

        self._checkin_cron = str(config.get("checkin_cron") or "0 8 * * *").strip()
        self._checkin_auto_retry = bool(config.get("checkin_auto_retry", True))
        self._checkin_retry_count = max(1, min(10, int(config.get("checkin_retry_count", 2) or 2)))


    def _apply_search_config(self, config: Dict[str, Any]) -> None:
        # 搜索渠道的启用与优先级只读渠道库（channels.json），配置项
        # search_source_order 只保留为迁移来源，不再进入运行态。
        self._search_source_order = self.get_enabled_search_sources()
        if not self._search_source_order:
            registered_sources = {d.id for d in SearchSourceRegistry.get_definitions()}
            raw_order = config.get("search_source_order", []) or []
            if isinstance(raw_order, str):
                raw_order = raw_order.split(",")
            self._search_source_order = list(dict.fromkeys(
                str(value).strip().lower()
                for value in raw_order
                if str(value).strip().lower() in registered_sources
            ))
            if self._search_source_order:
                logger.warning(
                    "渠道库为空，已回退到旧配置 search_source_order"
                )
        if self._search_source_order:
            logger.info(f"搜索资源优先级：{' > '.join(self._search_source_order)}")

        raw_search_proxy = str(config.get("search_proxy", "") or "").strip()
        self._search_proxy_username = str(config.get("search_proxy_username", "") or "").strip()
        self._search_proxy_password = str(config.get("search_proxy_password", "") or "")
        try:
            self._search_proxy_address = validate_proxy_address(raw_search_proxy)
            self._search_proxy = build_proxy_url(
                self._search_proxy_address,
                self._search_proxy_username,
                self._search_proxy_password,
            )
        except ValueError as error:
            logger.error(f"搜索渠道代理配置无效，本次使用直连：{error}")
            self._search_proxy_address = raw_search_proxy
            self._search_proxy = ""

        # 真实上传 115 的代理（只作用于 115 上传/API，源盘下载保持直连）
        raw_upload_proxy = str(
            config.get("cross_transfer_upload_proxy", "") or ""
        ).strip()
        # 插件没填时回落到 MP 自己的「网络设置」（PROXY_HOST），一处配置全站可用
        if not raw_upload_proxy:
            raw_upload_proxy = str(getattr(settings, "PROXY_HOST", "") or "").strip()
        upload_proxy_user = str(
            config.get("cross_transfer_upload_proxy_username", "") or ""
        ).strip()
        upload_proxy_pwd = str(
            config.get("cross_transfer_upload_proxy_password", "") or ""
        )
        try:
            upload_proxy_addr = validate_proxy_address(raw_upload_proxy)
            self._cross_transfer_upload_proxy = build_proxy_url(
                upload_proxy_addr, upload_proxy_user, upload_proxy_pwd,
            )
        except ValueError as error:
            logger.warning(f"115 上传代理配置无效，本次使用直连：{error}")
            self._cross_transfer_upload_proxy = ""
        self._cross_transfer_daily_upload_limit_gb = clamp_limit_gb(
            config.get("cross_transfer_daily_upload_limit_gb", CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_daily_upload_limit_gb"]["default"])
        )
        self._upload_quota = UploadQuota(
            self, self._cross_transfer_daily_upload_limit_gb
        )

        configured_resource_order = config.get("resource_type_order", ["115", "ed2k"])
        if not isinstance(configured_resource_order, list):
            configured_resource_order = [configured_resource_order]
        supported_resource_types = {d.id for d in DriverRegistry.get_definitions()} | {"ed2k", "magnet"}
        self._resource_type_order = list(dict.fromkeys(
            str(item).strip().lower()
            for item in configured_resource_order
            if str(item).strip().lower() in supported_resource_types
        ))
        # 注意：_platform_download_policy 在 _apply_sync_config 里才落到
        # self 上（后于本函数），这里必须从 config 字典里读，不能读 self。
        download_policy = str(config.get("platform_download_policy", "block") or "block")
        self._resource_type_order, magnet_added = ensure_magnet_for_cloud_policy(
            self._resource_type_order, download_policy,
        )
        if magnet_added:
            logger.warning(
                "平台下载策略为 cloud，已自动把 Magnet 加入资源类型顺序"
                "（Magnet 走 115 离线下载接管）"
            )
        configured_metadata_url = str(config.get(
            "magnet_metadata_url_template",
            "https://itorrents.org/torrent/{info_hash}.torrent",
        ) or "").strip()
        self._magnet_metadata_url_template = configure_magnet_metadata_url(configured_metadata_url)
        if self._magnet_metadata_url_template != configured_metadata_url:
            logger.warning("Magnet元数据地址模板无效，已恢复默认iTorrents地址")

        self._search_cache_enabled = bool(config.get("search_cache_enabled", True))
        self._search_cache_ttl_minutes = max(1, min(int(config.get("search_cache_ttl_minutes", 30) or 30), 1440))
        self._search_concurrency = max(1, min(int(config.get("search_concurrency", 2) or 2), 5))
        self._search_source_timeout = max(5, min(int(config.get("search_source_timeout", 60) or 60), 120))
        self._search_circuit_breaker_enabled = bool(config.get("search_circuit_breaker_enabled", True))
        self._search_circuit_breaker_threshold = max(1, min(int(config.get("search_circuit_breaker_threshold", 3) or 3),
                                                            10))
        self._search_circuit_breaker_cooldown = max(10,
                                                    min(int(config.get("search_circuit_breaker_cooldown", 60) or 60),
                                                        600))


    def _apply_sync_config(self, config: Dict[str, Any]) -> None:
        self._transfer_task_batch_size = int(config.get("transfer_task_batch_size", CONCURRENCY_AND_COOLDOWN_PARAMS["transfer_task_batch_size"]["default"]) or 50)
        self._cross_transfer_enabled = bool(config.get("cross_transfer_enabled", False))
        raw_cross_types = config.get("cross_transfer_media_types", ["movie", "tv"])
        if isinstance(raw_cross_types, str):
            raw_cross_types = [value.strip().lower() for value in raw_cross_types.split(",")]
        self._cross_transfer_media_types = [
                                               value for value in (raw_cross_types or []) if value in {"movie", "tv"}
                                           ] or ["movie", "tv"]
        self._cross_transfer_download_path = str(config.get("cross_transfer_download_path", "") or "").strip()
        self._cross_transfer_download_threads = max(1,
                                                    min(int(config.get("cross_transfer_download_threads", 16) or 16), 256))
        self._cross_transfer_max_concurrent = max(1, min(int(config.get("cross_transfer_max_concurrent", CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_max_concurrent"]["default"]) or 2), 10))
        self._cross_transfer_aria2_enabled = bool(
            config.get("cross_transfer_aria2_enabled", False)
        )
        self._cross_transfer_aria2_connections = max(
            1,
            min(int(config.get("cross_transfer_aria2_connections", 64) or 64), 96),
        )
        self._cross_transfer_aria2_max_concurrent = max(
            1,
            min(int(config.get("cross_transfer_aria2_max_concurrent", 2) or 2), 4),
        )
        self._cross_transfer_aria2_min_split_size = str(
            config.get("cross_transfer_aria2_min_split_size", "1M") or "1M"
        ).strip() or "1M"
        self._cross_transfer_aria2_disk_cache = str(
            config.get("cross_transfer_aria2_disk_cache", "32M") or "32M"
        ).strip() or "32M"
        try:
            configure_aria2(
                enabled=self._cross_transfer_aria2_enabled,
                connections=self._cross_transfer_aria2_connections,
                max_concurrent=self._cross_transfer_aria2_max_concurrent,
                min_split_size=self._cross_transfer_aria2_min_split_size,
                disk_cache=self._cross_transfer_aria2_disk_cache,
                data_dir=resolve_data_dir(
                    self._cross_transfer_download_path, self.get_data_path()
                ),
            )
        except Exception as error:  # noqa: BLE001 - 配置失败不得阻塞插件加载
            logger.warning(f"配置 aria2 下载引擎失败：{error}")
        self._cross_transfer_direct_stream = bool(config.get("cross_transfer_direct_stream", True))
        try:
            upload_concurrency = int(
                config.get(
                    "cross_transfer_upload_concurrency",
                    CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_concurrency"]["default"],
                )
            )
        except (TypeError, ValueError):
            upload_concurrency = CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_concurrency"]["default"]
        # 0 表示关闭 115 分片并发上传（回退顺序上传），上限 16。
        self._cross_transfer_upload_concurrency = max(0, min(upload_concurrency, 16))
        self._sync_cross_transfer_upload_concurrency()
        # 115 上传熔断器配置（Phase 3b-10）：阈值/冷却，非法值回落表默认值。
        try:
            breaker_threshold = int(
                config.get(
                    "cross_transfer_upload_breaker_threshold",
                    CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_threshold"]["default"],
                )
            )
        except (TypeError, ValueError):
            breaker_threshold = CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_threshold"]["default"]
        self._cross_transfer_upload_breaker_threshold = max(1, min(breaker_threshold, 100))
        try:
            breaker_cooldown = float(
                config.get(
                    "cross_transfer_upload_breaker_cooldown_seconds",
                    CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_cooldown_seconds"]["default"],
                )
            )
        except (TypeError, ValueError):
            breaker_cooldown = CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_cooldown_seconds"]["default"]
        self._cross_transfer_upload_breaker_cooldown_seconds = max(5.0, min(breaker_cooldown, 3600.0))
        self._sync_upload_breaker_config()

        rapid_miss_action = str(
            config.get("cross_transfer_rapid_miss_action", "abort") or "abort"
        ).strip().lower()
        # 秒传未命中处理：fallback=回落普通上传（默认）；abort=中止且不真实上传。
        # 非法值一律回退 fallback，保证老行为逐字节不变。
        self._cross_transfer_rapid_miss_action = (
            rapid_miss_action if rapid_miss_action in {"fallback", "abort"} else "fallback"
        )
        # 改配置后立即同步到已存在的跨盘任务管理器（旧实现只在 initialize 时传一次，
        # 导致切到「回落普通上传」后运行期仍是 abort，1.9.76 修复）。
        manager = getattr(self, "_cross_transfer_manager", None)
        setter = getattr(manager, "set_rapid_miss_action", None)
        if callable(setter):
            setter(self._cross_transfer_rapid_miss_action)
        self._apply_tg_monitor_config(config)
        self._residue_cleanup = bool(
            config.get("cross_transfer_residue_cleanup", True)
        )
        # 1.32.0：真名媒体残留保留期（小时），0 = 关闭；非法值回落 24。
        try:
            real_name_hours = int(
                config.get("cross_transfer_real_name_retention_hours", 24)
            )
        except (TypeError, ValueError):
            real_name_hours = 24
        self._cross_transfer_real_name_retention_hours = max(0, min(real_name_hours, 168))

        self._subscription_concurrency = max(1, min(int(config.get("subscription_concurrency", CONCURRENCY_AND_COOLDOWN_PARAMS["subscription_concurrency"]["default"]) or 2), 5))
        self._batch_size = int(config.get("batch_size", 20) or 20)
        self._batch_interval = max(0, min(float(config.get("batch_interval", CONCURRENCY_AND_COOLDOWN_PARAMS["batch_interval"]["default"]) or 0), 60))
        self._transfer_risk_cooldown = max(60, min(int(config.get("transfer_risk_cooldown", CONCURRENCY_AND_COOLDOWN_PARAMS["transfer_risk_cooldown"]["default"]) or 1800), 86400))
        self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)
        # 跨季路径守卫：分享目录季号与目标季冲突时拒绝进候选（file_matcher 类级开关）
        self._cross_season_path_guard = bool(config.get("cross_season_path_guard", True))
        FileMatcher.path_season_guard_enabled = self._cross_season_path_guard
        # 期集换算规则（综艺「第N期上/下」→ 集号），空配置保持原行为
        self._period_episode_rules_text = str(
            config.get("period_episode_rules", "") or ""
        )
        FileMatcher.period_episode_rules = self._parse_period_episode_rules(
            self._period_episode_rules_text
        )
        configured_video_exts, configured_sub_exts = MediaFileParser.configure_extensions(
            video_extensions=config.get("video_extensions"),
            subtitle_extensions=config.get("subtitle_extensions"),
        )
        self._video_extensions = sorted(configured_video_exts)
        self._subtitle_extensions = sorted(configured_sub_exts)

        self._upgrade_subscribe_ids = config.get("upgrade_subscribe_ids", []) or []
        self._self_heal_interval = int(config.get("self_heal_interval", 10))
        self._enable_cloud_upgrade = bool(config.get("enable_cloud_upgrade", False))
        self._enable_pt_upgrade = bool(config.get("enable_pt_upgrade", False))
        self._upgrade_mode = str(config.get("upgrade_mode", "largest") or "largest").strip().lower()
        if self._upgrade_mode not in {"coexist", "replace", "largest", "smallest"}:
            self._upgrade_mode = "largest"
        self._episode_dedup_enabled = bool(config.get("episode_dedup_enabled", True))
        self._verify_landed_enabled = bool(config.get("verify_landed_enabled", True))
        self._prefer_template_named_version = bool(
            config.get("prefer_template_named_version", True)
        )
        if self._upgrade_mode == "coexist" and self._episode_dedup_enabled:
            logger.warning(
                "洗版模式为「新旧共存」：按季集判重不生效，"
                "需要替换请改成「保留最大文件 / 保留最小文件 / 直接替换」"
            )
        self._quality_min_gain_percent = int(config.get("quality_min_gain_percent", 10) or 0)
        self._upgrade_episode_limit = max(
            0, int(config.get("upgrade_episode_limit", 0) or 0)
        )
        self._quality_min_gain_mb = int(config.get("quality_min_gain_mb", 200) or 0)
        self._local_resource_path = str(config.get("local_resource_path", "") or "").strip()
        self._type_folder_enabled = bool(config.get("type_folder_enabled", False))
        self._type_folder_movie = (
                str(config.get("type_folder_movie", "电影") or "").strip().strip("/") or "电影"
        )
        self._type_folder_tv = (
                str(config.get("type_folder_tv", "剧集") or "").strip().strip("/") or "剧集"
        )

        # v1.30.0 阶段 4：organize_after_transfer 已退役（转存后整理恒开，
        # 旧值由 normalize 静默丢弃）；类默认值 True 保持行为不变。
        self._organize_subtitles = bool(config.get("organize_subtitles", True))
        self._subtitle_traditional_to_simplified = bool(config.get("subtitle_traditional_to_simplified", False))
        self._anime_pack_preferred = bool(config.get("anime_pack_preferred", True))
        self._offline_timeout = max(10, min(int(config.get("offline_timeout", 30) or 30), 1440))
        self._strm_generate_enabled = bool(config.get("strm_generate_enabled", True))
        self._strm_full_scan_enabled = bool(config.get("strm_full_scan_enabled", True))
        self._strm_full_scan_overwrite = bool(config.get("strm_full_scan_overwrite", False))
        self._strm_increment_enabled = bool(config.get("strm_increment_enabled", True))
        try:
            self._strm_increment_minutes = max(
                1, min(int(config.get("strm_increment_minutes", 10) or 10), 1440)
            )
        except (TypeError, ValueError):
            self._strm_increment_minutes = 10
        try:
            self._strm_increment_budget = max(
                30, min(int(config.get("strm_increment_budget", 180) or 180), 3600)
            )
        except (TypeError, ValueError):
            self._strm_increment_budget = 180
        # v1.30.0 阶段 4：organize_source_path 已退役（整理台源目录改
        # 为手动现选，旧值由 normalize 静默丢弃）；类默认值 "" 保持行为不变。
        try:
            organize_max_files = int(config.get("organize_max_files", 500) or 500)
        except (TypeError, ValueError):
            organize_max_files = 500
        try:
            organize_max_depth = int(config.get("organize_max_depth", 8) or 8)
        except (TypeError, ValueError):
            organize_max_depth = 8
        self._organize_max_files = max(1, min(organize_max_files, 5000))
        self._organize_max_depth = max(1, min(organize_max_depth, 20))
        self._organize_include_subtitles = bool(config.get("organize_include_subtitles", True))
        self._organize_conflict_policy = str(config.get("organize_conflict_policy", "skip") or "skip")
        self._organize_schedule_enabled = bool(config.get("organize_schedule_enabled", False))
        self._organize_schedule_cron = str(config.get("organize_schedule_cron", "0 4 * * *") or "0 4 * * *")
        self._organize_schedule_auto_execute = bool(config.get("organize_schedule_auto_execute", True))
        self._organize_schedule_notify = bool(config.get("organize_schedule_notify", True))
        self._nfo_scrape_enabled = bool(config.get("nfo_scrape_enabled", False))
        self._image_scrape_enabled = bool(config.get("image_scrape_enabled", False))
        self._strm_base_url = str(config.get("strm_base_url", "")).strip().rstrip("/")
        self._strm_url_template = str(config.get(
            "strm_url_template",
            "{base_url}/api/v1/plugin/CloudSubscribeLite/redirect"
            "?pickcode={pickcode}&file_name={file_name}",
        )).strip()
        # 精简前默认值写死 X 端点：已保存的旧模板若仍指向 X，改指本插件自己的
        # /redirect（Lite 保留该端点，X 未安装时旧链接 404）。
        self._strm_url_template = self._strm_url_template.replace(
            "/api/v1/plugin/CloudSubscribeX/redirect",
            "/api/v1/plugin/CloudSubscribeLite/redirect",
        )

        self._timeout_enabled = bool(config.get("timeout_enabled", True))
        for key, default in (
                ("timeout_default_connect", 30),
                ("timeout_default_pool", 15),
                ("timeout_default_read", 60),
                ("timeout_default_write", 60),
                ("timeout_slow_connect", 30),
                ("timeout_slow_pool", 15),
                ("timeout_slow_read", 300),
                ("timeout_slow_write", 300),
        ):
            setattr(self, f"_{key}", max(0, float(config.get(key, default) or 0)))

        self._block_start_time = str(config.get("block_start_time", self._block_start_time) or self._block_start_time)
        self._block_end_time = str(config.get("block_end_time", self._block_end_time) or self._block_end_time)
        self._block_system_subscribe = bool(config.get("block_system_subscribe", False))
        self._takeover_new_subscribes = bool(config.get("takeover_new_subscribes", False))
        self._platform_download_policy = str(config.get("platform_download_policy", "block") or "block")


    def _persist_config_values(self, **updates: Any) -> None:
        """基于当前完整配置更新少量运行时值，避免遗漏其他配置项。"""
        updates = {
            key: value
            for key, value in updates.items()
            if value is not None
        }
        if not updates:
            return
        config = copy.deepcopy(self.get_config() or self._applied_config or {})
        if all(config.get(key) == value for key, value in updates.items()):
            # 与库中配置一致时无需写库：宿主对“值未变化”的写入返回 None。
            self._applied_config = config
            return
        config.update(updates)
        try:
            # 宿主仅在真正失败时返回 False，None 表示值未变化、无需更新。
            if self.update_config(config) is False:
                logger.warning(f"插件运行时配置持久化失败：{', '.join(updates)}")
        except Exception as error:
            logger.warning(
                f"插件运行时配置持久化异常：{', '.join(updates)}：{error}"
            )
        self._applied_config = config


    def _ensure_notify_template_defaults(self, config: Optional[dict]) -> None:
        """把缺失的通知模板默认值写回配置。

        配置页读的是数据库里的原始配置，新版本新增的模板键在用户保存之前并不存在，
        这里在初始化时补全，保证首次打开「消息通知」就能看到可编辑的默认文案。
        """
        if not isinstance(config, dict):
            return
        missing = {
            key: default
            for key, default in DEFAULT_TEMPLATES.items()
            if not str(config.get(key) or "").strip()
        }
        # 仍是旧默认值的模板随版本升级到新排版；用户改过的不动
        upgraded = upgrade_legacy_templates(config)
        if not missing and not upgraded:
            return
        merged = dict(config)
        merged.update(missing)
        merged.update(upgraded)
        updates = {**missing, **upgraded}
        if self.update_config(merged):
            if missing:
                logger.info(f"已为配置补全 {len(missing)} 个通知模板默认值")
            if upgraded:
                logger.info(f"已将 {len(upgraded)} 个旧版通知模板升级为新排版")
        else:
            logger.warning("通知模板默认值写回配置失败，运行时将使用内置默认模板")
        # 本方法在 _apply_plugin_config 之后执行，内存里还是旧模板；
        # 不同步就会「配置已更新但通知照旧」（1.7.75 线上实测）。
        changed = apply_runtime_updates(
            getattr(self, "_notify_templates", None), updates
        )
        if changed:
            logger.info(f"运行期通知模板已同步 {changed} 项，本次进程内立即生效")


    def _p115_timeout_kwargs(self) -> Dict[str, Any]:
        """组装 p115client 的普通与慢操作超时参数，0 表示不限制。"""

        def timeout_values(prefix: str) -> Dict[str, float]:
            values = {}
            for name in ("connect", "pool", "read", "write"):
                value = float(getattr(self, f"_timeout_{prefix}_{name}", 0) or 0)
                if value > 0:
                    values[name] = value
            return values

        return {
            "timeout_enabled": self._timeout_enabled,
            "default_timeout": timeout_values("default"),
            "slow_timeout": timeout_values("slow"),
        }


    def _sync_cross_transfer_upload_concurrency(self) -> None:
        """把 115 分片并发度注入 115 客户端管理器。

        上传组件挂在客户端管理器下、靠属性委托读宿主值，构造期拿不到插件配置，
        所以插件在初始化与每次保存配置后显式注入；注入失败时组件用默认值（2）。
        """
        manager = getattr(self, "_drive_manager", None)
        client = manager.get_client("115") if manager else None
        if not client:
            return
        try:
            client._cross_transfer_upload_concurrency = self._cross_transfer_upload_concurrency
        except Exception as error:  # noqa: BLE001 - 注入失败不影响主流程
            logger.debug(f"注入 115 上传并发度失败：{error}", exc_info=True)


    def _sync_upload_breaker_config(self) -> None:
        """把 115 上传熔断器阈值/冷却注入 115 客户端管理器。

        与并发度注入同一路子：上传组件每次用熔断器时从 manager 属性现读，
        改配置即生效；注入失败时组件用默认值（阈值 5、冷却 300 秒）。
        """
        manager = getattr(self, "_drive_manager", None)
        client = manager.get_client("115") if manager else None
        if not client:
            return
        try:
            client._upload_breaker_threshold = self._cross_transfer_upload_breaker_threshold
            client._upload_breaker_cooldown_seconds = self._cross_transfer_upload_breaker_cooldown_seconds
        except Exception as error:  # noqa: BLE001 - 注入失败不影响主流程
            logger.debug(f"注入 115 上传熔断配置失败：{error}", exc_info=True)
