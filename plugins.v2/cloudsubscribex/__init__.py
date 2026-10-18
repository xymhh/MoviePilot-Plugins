"""
网盘订阅助手插件
结合订阅功能，自动搜索网盘资源并同步缺失内容
"""
import ast
import copy
import datetime
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, Lock, RLock, local, Thread
from typing import Optional, Any, List, Dict, Tuple, Callable

import pytz
from app.api.endpoints.plugin import register_plugin_api
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, NotificationType
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .core import (
    CloudDriveManager,
    CloudDriveProvider,
    CloudDriveRegistry,
    get_component,
    resolve_component,
)
from .core.api import (
    AccountApi,
    ConfigApi,
    CheckinApi,
    MoviePilotRegistration,
    HistoryApi,
    MediaLibraryApi,
    PageApi,
    QRCodeService,
    RuntimeApi,
    SearchApi,
    SyncApi,
    TgMonitorApi,
)
from .core.hook import MessageRoutingHook, PluginEventHandler, SubscriptionSearchHook
from .core.relay import probe_health
from .core.legacy_migration import LegacyIdMigrator
from .core.services import (
    SubscriptionControlService,
    CheckinService,
    PlatformIntegrationService,
    SubscriptionScoringService,
    SyncExecutionService,
    SyncRuntimeService,
    SkipStatsCollector,
)
from .core.storage import CloudSubscribeDataStore
from .core.subscribe import AutoSubscribeService
from .drive.scanner import DriverRegistry
from .handlers import SearchHandler, SyncHandler, SubscribeHandler, WebhookHandler
from .search.scanner import SearchSourceRegistry
from .subscribe import (  # noqa: F401 - 导入即注册自动订阅渠道
    create_anilist_provider,
    create_bangumi_provider,
    create_douban_provider,
    create_maoyan_provider,
    create_mikan_provider,
    create_netflix_provider,
    create_tmdb_provider,
)
from .utils import FileMatcher, configure_magnet_metadata_url
from .utils.file_parser import MediaFileParser
from .utils.http_client import build_proxy_url, validate_proxy_address

_COMPONENT_TYPES = (
    PageApi,
    AccountApi,
    SearchApi,
    ConfigApi,
    CheckinApi,
    SyncApi,
    RuntimeApi,
    MediaLibraryApi,
    QRCodeService,
    HistoryApi,
    TgMonitorApi,
    MessageRoutingHook,
    PluginEventHandler,
    MoviePilotRegistration,
    SyncRuntimeService,
    SyncExecutionService,
    SubscriptionScoringService,
    SubscriptionControlService,
    SubscriptionSearchHook,
    PlatformIntegrationService,
    CheckinService,
    AutoSubscribeService,
)


class CloudSubscribeX(_PluginBase):
    """网盘订阅助手（跨盘优化版）插件。

    1.6.49 起使用独立插件 ID ``CloudSubscribeX``，与上游同名插件彻底解耦：两者可在
    MoviePilot 里并存安装、各自独立升级。首次启动时会自动从旧 ID ``CloudSubscribe``
    迁移配置、持久化数据与私有数据库（见 :meth:`_migrate_from_legacy_plugin`）。
    """

    # 插件名称
    plugin_name = "网盘订阅助手（跨盘优化版）"
    # 插件描述
    plugin_desc = "整合网盘能力与多渠道资源搜索，自动查找并补充订阅缺失的影视内容（xymhh fork：跨盘转存优化版）。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xymhh/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = "1.6.64"
    # 插件作者
    plugin_author = "xymhh（基于 odomu）"
    # 作者主页
    author_url = "https://github.com/xymhh"
    # 插件配置项ID前缀（保持 cloudsubscribe_ 不变，旧配置键与旧数据键无需重写）
    plugin_config_prefix = "cloudsubscribe_"
    # 旧插件 ID：1.6.49 之前的本 fork 与上游同名插件都用它，首次启动时从它迁移
    legacy_plugin_id = "CloudSubscribe"
    plugin_order = 21
    auth_level = 1

    # 私有变量
    _scheduler: Optional[BackgroundScheduler] = None
    _offline_scheduler: Optional[BackgroundScheduler] = None
    _offline_scheduler_lock: RLock = RLock()
    _offline_monitor_lock: RLock = RLock()
    # 配置属性
    _enabled: bool = False
    _show_sidebar_nav: bool = True
    _agent_enabled: bool = True
    _direct_transfer_enabled: bool = True
    _title_subscribe_enabled: bool = True
    _subscribe_all_missing_seasons: bool = True
    _subscribe_skip_season_zero: bool = True
    _subscribe_season_limit: int = 0
    _organize_after_transfer: bool = True
    _organize_subtitles: bool = True
    _subtitle_traditional_to_simplified: bool = False
    _anime_pack_preferred: bool = True
    _offline_timeout: int = 30
    _cron: str = "0 18-23 * * *"
    _auto_subscribe_enabled: bool = False
    _auto_subscribe_onlyonce: bool = False
    _auto_subscribe_cron: str = "0 8 * * *"
    _notify: bool = False
    _notification_type: NotificationType = NotificationType.Plugin
    _webhook_enabled: bool = False
    _webhook_url: str = ""
    _webhook_method: str = "POST"
    _webhook_timeout: int = 10
    _cloud_drive_key: str = "115"
    _resource_type_order: List[str] = ["115", "ed2k"]
    # 订阅搜索可落地的资源类型（内部键），由 CloudDriveManager 按驱动能力回写
    _cloud_drive_resource_types: Tuple[str, ...] = ()
    _magnet_metadata_url_template: str = "https://itorrents.org/torrent/{info_hash}.torrent"

    # 订阅过滤模式："exclude" 排除模式（处理除勾选外的全部订阅）/ "include" 指定模式（仅处理勾选的订阅）
    _subscribe_filter_mode: str = "exclude"
    _exclude_subscribes: List[int] = []
    _include_subscribes: List[int] = []
    # 搜索源优先级（按列表顺序），为空时使用已启用来源的默认顺序
    _search_source_order: List[str] = []
    _search_proxy: Any = ""
    _search_proxy_address: str = ""
    _search_proxy_username: str = ""
    _search_proxy_password: str = ""
    _search_cache_enabled: bool = True
    _search_cache_ttl_minutes: int = 30
    _search_concurrency: int = 2
    _search_source_timeout: int = 60
    _search_circuit_breaker_enabled: bool = True
    _search_circuit_breaker_threshold: int = 3
    _search_circuit_breaker_cooldown: int = 60
    _checkin_cron: str = "0 8 * * *"
    _checkin_auto_retry: bool = True
    _checkin_retry_count: int = 2

    # 是否屏蔽系统订阅（True=已屏蔽系统订阅，False=已恢复系统订阅）
    _block_system_subscribe: bool = False
    _takeover_new_subscribes: bool = False
    _platform_download_policy: str = "block"

    _transfer_task_batch_size: int = 50
    _cross_transfer_enabled: bool = False
    _cross_transfer_media_types: list = ["movie", "tv"]
    _cross_transfer_download_path: str = ""
    _cross_transfer_download_threads: int = 5
    _cross_transfer_max_concurrent: int = 2
    _cross_transfer_direct_stream: bool = True
    _cross_transfer_upload_concurrency: int = 4
    # 外部下载器（夸克→本地一步可选用 Gopeed；默认 builtin 与旧版行为一致）
    _cross_transfer_downloader: str = "builtin"
    _cross_transfer_rapid_miss_action: str = "fallback"
    # 国内中继上传（实验）：默认关闭，未配置地址时强制视为关闭。
    _cross_transfer_relay_enabled: bool = False
    _cross_transfer_relay_url: str = ""
    _cross_transfer_relay_token: str = ""
    _cross_transfer_relay_max_file_gb: int = 25
    _cross_transfer_relay_timeout_minutes: int = 180
    _cross_transfer_relay_fallback_local: bool = True
    _cross_transfer_relay_threads: int = 0
    # TG 频道监控（用户自己的 TG API 会话）：默认关闭，未配置凭据时强制视为关闭。
    _tg_monitor_enabled: bool = False
    _tg_api_id: int = 0
    _tg_api_hash: str = ""
    _tg_session: str = ""
    _tg_channels: list = []
    _tg_monitor_keywords: list = []
    _tg_monitor_exclude_keywords: list = []
    _tg_monitor_scan_limit: int = 20
    _tg_monitor_interval_minutes: int = 10
    _tg_monitor_max_items: int = 500
    _gopeed_url: str = "http://gopeed:9999"
    _gopeed_token: str = ""
    _gopeed_path_prefix: str = ""
    _gopeed_residue_cleanup: bool = True
    _residue_scheduler: Optional[BackgroundScheduler] = None
    _gopeed_connections: int = 0  # 0=沿用 Gopeed 全局连接数；夸克建议 256
    _subscription_concurrency: int = 2
    _batch_size: int = 20
    _batch_interval: float = 3
    _transfer_risk_cooldown: int = 1800
    _skip_other_season_dirs: bool = True
    _cross_season_path_guard: bool = True
    _video_extensions: List[str] = [".mkv", ".mp4", ".avi", ".iso"]
    _subtitle_extensions: List[str] = [
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks"
    ]

    # 洗版配置
    _upgrade_subscribe_ids: list = []
    _last_scored_ids_hash: str = ""  # 上次评分过的ids hash值，用于保存配置时防重复触发
    _self_heal_interval: int = 10
    _enable_cloud_upgrade: bool = False
    _enable_pt_upgrade: bool = False
    _upgrade_mode: str = "largest"
    _episode_dedup_enabled: bool = True
    _verify_landed_enabled: bool = True
    _prefer_template_named_version: bool = True
    _local_resource_path: str = ""
    # 媒体库一级目录（按媒体类型分层：电影/剧集），仅作用于网盘媒体库与本地 STRM 目录结构
    _type_folder_enabled: bool = False
    _type_folder_movie: str = "电影"
    _type_folder_tv: str = "剧集"
    _strm_generate_enabled: bool = True
    _nfo_scrape_enabled: bool = False
    _image_scrape_enabled: bool = False
    _strm_base_url: str = "http://172.17.0.1:9527"
    _strm_url_template: str = "{base_url}/d/{pickcode}?/{file_name}"
    _strm_full_scan_enabled: bool = True
    _strm_full_scan_overwrite: bool = False
    _strm_full_scan_scheduler: Optional[BackgroundScheduler] = None
    _media_server_refresh_enabled: bool = False
    _media_servers: List[str] = []
    _media_server_path_mappings: str = ""
    _media_server_refresh_delay: int = 0
    _emby_mediainfo_enabled: bool = False
    _platform_media_sync_enabled: bool = False
    _platform_deep_delete_enabled: bool = False
    _platform_transfer_history_enabled: bool = False
    _timeout_enabled: bool = True
    _timeout_default_connect: float = 30
    _timeout_default_pool: float = 15
    _timeout_default_read: float = 60
    _timeout_default_write: float = 60
    _timeout_slow_connect: float = 30
    _timeout_slow_pool: float = 15
    _timeout_slow_read: float = 300
    _timeout_slow_write: float = 300
    # 屏蔽态时间段（block_system_subscribe=OFF 时生效）
    _block_start_time: str = "18:00"
    _block_end_time: str = "23:59"
    # 全局配置是否已应用（安装成功首次执行时才修改MP系统配置）
    _global_config_applied: bool = False

    # 运行时对象
    _cloud_drive_registry: Optional[CloudDriveRegistry] = None
    _cloud_drive: Optional[CloudDriveProvider] = None
    _drive_manager: Optional[CloudDriveManager] = None

    # 处理器
    _search_handler: Optional[SearchHandler] = None
    _subscribe_handler: Optional[SubscribeHandler] = None
    _sync_handler: Optional[SyncHandler] = None
    _webhook_handler: Optional[WebhookHandler] = None
    _subscribe_search_originals: Dict[str, Callable[..., Any]] = {}
    _subscribe_chain_originals: Dict[str, Callable[..., Any]] = {}
    _platform_search_originals: Dict[str, Callable[..., Any]] = {}
    _interaction_originals: Dict[str, Callable[..., Any]] = {}
    _stop_event: Optional[ThreadEvent] = None
    _sync_running: bool = False
    _sync_status: str = "idle"
    _sync_task_text: str = "当前没有订阅处理任务"
    _sync_progress: int = 0
    _sync_context: Dict[str, Any] = {}
    # 一次同步运行内被跳过的候选资源统计（运行期实例，类属性兜底为 None）
    _skip_stats: Optional[SkipStatsCollector] = None
    _sync_run_started_at: float = 0
    _sync_last_finished_at: float = 0
    _sync_last_elapsed_ms: int = 0
    _sync_tasks: Dict[str, Dict[str, Any]] = {}
    _sync_tasks_lock: Optional[RLock] = None
    _runtime_revision: int = 0
    _history_revision: int = 0
    _runtime_revision_lock: Lock = Lock()
    _task_local: Optional[local] = None
    _pending_config: Optional[Dict[str, Any]] = None
    _applied_config: Dict[str, Any] = {}
    _pending_config_lock: RLock = RLock()
    _subscribe_search_queue_lock: Optional[RLock] = None
    _subscribe_search_pending: Dict[Optional[int], bool] = {}
    _subscribe_search_active: Dict[Optional[int], bool] = {}
    _subscribe_search_queue_shutdown: Optional[ThreadEvent] = None
    _subscribe_search_coordinator_running: bool = False
    _subscribe_search_queue_revision: int = 0
    _sync_operation_executor: Optional[ThreadPoolExecutor] = None

    @property
    def _p115_manager(self) -> Any:
        return self._drive_manager.get_client("115") if self._drive_manager else None

    @_p115_manager.setter
    def _p115_manager(self, value: Any) -> None:
        if self._drive_manager:
            self._drive_manager._clients["115"] = value

    def _get_data_store(self) -> CloudSubscribeDataStore:
        store = self.__dict__.get("_cloudsubscribe_data_store")
        if store is None:
            store = CloudSubscribeDataStore(self)
            self.__dict__["_cloudsubscribe_data_store"] = store
        return store

    def get_data(self, key: Optional[str] = None, plugin_id: Optional[str] = None) -> Any:
        """读取插件业务数据与可恢复运行状态，统一使用私有库。"""
        target_plugin = plugin_id or self.__class__.__name__
        if target_plugin == self.__class__.__name__ and key:
            if CloudSubscribeDataStore.handles(key):
                return self._get_data_store().load(key)
            return None
        return super().get_data(key=key, plugin_id=plugin_id)

    def save_data(
            self, key: str, value: Any, plugin_id: Optional[str] = None
    ) -> None:
        """保存插件数据"""
        target_plugin = plugin_id or self.__class__.__name__
        if target_plugin == self.__class__.__name__:
            if CloudSubscribeDataStore.handles(key):
                self._get_data_store().save(key, value)
                return
            raise ValueError(f"未声明的数据键不能写入 PluginData：{key}")
        super().save_data(key=key, value=value, plugin_id=plugin_id)

    def _get_component(self, component_type):
        return get_component(self, component_type, "_plugin_components")

    def __getattr__(self, name):
        return resolve_component(self, _COMPONENT_TYPES, name, "_plugin_components")

    def get_state(self) -> bool:
        return self._get_component(MoviePilotRegistration).get_state()

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        return MoviePilotRegistration.get_render_mode()

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_form()

    def get_page(self) -> Optional[List[dict]]:
        return self._get_component(MoviePilotRegistration).get_page()

    def get_api(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_api()

    def get_command(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_command()

    def get_service(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_service()

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        return self._get_component(MoviePilotRegistration).get_dashboard_meta()

    def get_dashboard(self, key: str = "overview", **kwargs):
        return self._get_component(MoviePilotRegistration).get_dashboard(key, **kwargs)

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_sidebar_nav()

    def get_actions(self) -> List[Dict[str, Any]]:
        return self._get_component(MoviePilotRegistration).get_actions()

    def get_agent_tools(self) -> List[type]:
        return self._get_component(MoviePilotRegistration).get_agent_tools()

    @eventmanager.register(EventType.SubscribeAdded)
    def on_subscribe_added(self, event: Event):
        return self._get_component(PluginEventHandler).on_subscribe_added(event)

    @eventmanager.register(EventType.SubscribeModified)
    def on_subscribe_modified(self, event: Event):
        return self._get_component(PluginEventHandler).on_subscribe_modified(event)

    @eventmanager.register(ChainEventType.ResourceDownload)
    def on_resource_download(self, event: Event):
        return self._get_component(PluginEventHandler).on_resource_download(event)

    @eventmanager.register(EventType.TransferComplete)
    def on_transfer_complete(self, event: Event):
        return self._get_component(PluginEventHandler).on_transfer_complete(event)

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        return self._get_component(PluginEventHandler).on_plugin_action(event)

    @eventmanager.register(EventType.MessageAction)
    def on_message_action(self, event: Event):
        return self._get_component(PluginEventHandler).on_message_action(event)

    @eventmanager.register(EventType.WebhookMessage)
    def on_media_server_webhook(self, event: Event):
        event_info = getattr(event, "event_data", None) if event else None
        event_name = str(getattr(event_info, "event", None) or (
            event_info.get("event") if isinstance(event_info, dict) else "") or "")
        channel = str(getattr(event_info, "channel", None) or (
            event_info.get("channel") if isinstance(event_info, dict) else "") or "")
        logger.info(
            f"收到平台媒体服务器 WebhookMessage 事件: channel={channel or '未知'}, event={event_name or '未知'}")
        return self._get_component(MediaLibraryApi).handle_platform_media_webhook(
            event_info
        )

    @staticmethod
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

    @staticmethod
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


    def init_plugin(self, config: dict = None):
        """宿主加载或重载插件时初始化完整运行环境。"""
        self._raise_file_limit()
        # 独立 ID 的首次启动：先把旧 ID 的配置/数据/私有数据库搬过来，再建库并应用配置。
        config = self._migrate_from_legacy_plugin(config)
        self._offline_monitor_lock = RLock()
        self._offline_scheduler_lock = RLock()
        # 初始化独立数据库并修复历史分组键。
        self._get_data_store().initialize()
        self._apply_plugin_config(config, reset_runtime=True)

    def _migrate_from_legacy_plugin(self, config: Optional[dict]) -> Optional[dict]:
        """首次启动时把旧插件 ID（CloudSubscribe）的配置/数据/私有库搬到独立 ID。

        明细见 :class:`~core.legacy_migration.LegacyIdMigrator`：只在自身尚无配置且
        未迁移过时执行，迁移后 ``enabled`` 强制置 False（旧插件可能仍在跑，两个插件
        同时转存会互相踩），启用与否留给用户手动决定。
        """
        return LegacyIdMigrator(
            self, legacy_plugin_id=self.legacy_plugin_id
        ).migrate(config)

    @staticmethod
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

    def _apply_plugin_config(
            self, config: Optional[Dict[str, Any]], reset_runtime: bool = False
    ) -> None:
        """应用配置并重建相关服务；普通保存不重置同步任务状态。"""
        config = dict(config or {})
        original_config = dict(config)
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
            "media_server_path_mappings",
            "media_server_refresh_delay",
            "emby_mediainfo_enabled",
            "platform_media_sync_enabled",
            "platform_deep_delete_enabled",
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
                setattr(self, f"_{key}", val)
            self._apply_base_config(config)
            self._apply_notification_config(config)
            self._cloud_drive_key = str(config.get("cloud_drive", "115") or "115").strip().lower()
            self._apply_search_config(config)
            self._apply_sync_config(config)

        # 初始化客户端/handlers
        self._init_clients()
        self._init_handlers()
        if self._sync_handler:
            self._sync_handler.sync_platform_transfer_history()
            self._sync_handler.reconcile_orphan_finalize_records()
        self._update_offline_monitor(
            len(self._sync_handler.get_pending_finalize_tasks())
            if self._sync_handler else 0
        )
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
        }
        try:
            from .core.checkin_manager import get_checkin_definitions
            for defn in get_checkin_definitions().values():
                service_config_keys.add(f"{defn.key}_checkin_enabled")
                service_config_keys.add(f"{defn.key}_checkin_mode")
                if getattr(defn, "credential_keys", None):
                    service_config_keys.update(defn.credential_keys)
        except Exception as error:
            logger.debug(f"动态收集服务签到配置键异常: {error}")
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
        self._log_relay_state()
        self._start_residue_cleanup()
        self._start_strm_full_scan()

        # 调度一次性任务。
        if self._auto_subscribe_onlyonce:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info("榜单自动订阅服务启动，立即运行一次")
            self._scheduler.add_job(
                func=self._run_auto_subscribe_once,
                trigger="date",
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3)
                ),
            )
            self._auto_subscribe_onlyonce = False
            self._persist_config_values(auto_subscribe_onlyonce=False)
            if self._scheduler.get_jobs():
                self._scheduler.start()

    def _log_relay_state(self) -> None:
        """配置应用时把中继实际生效状态与连通性写进日志。

        中继是「秒传未命中」的唯一出路，配置错一项就会静默退回本机链路，
        仅在配置应用时打一行，避免每次任务重复探测。
        """
        if not self._cross_transfer_relay_enabled:
            if self._cross_transfer_relay_url:
                logger.info("跨盘中继未启用（开关关闭），秒传未命中按本机链路处理")
            else:
                logger.info("跨盘中继未启用（开关关闭或中继地址为空），秒传未命中按本机链路处理")
            return
        logger.info(
            f"跨盘中继已启用：地址={self._cross_transfer_relay_url}, "
            f"令牌={'已配置' if self._cross_transfer_relay_token else '空'}, "
            f"单文件上限={self._cross_transfer_relay_max_file_gb}GB, "
            f"单任务超时={self._cross_transfer_relay_timeout_minutes}分钟, "
            f"下载线程={'中继默认' if not self._cross_transfer_relay_threads else self._cross_transfer_relay_threads}, "
            f"失败回落本机={'是' if self._cross_transfer_relay_fallback_local else '否'}")
        logger.info(
            f"跨盘中继连通性：{probe_health(self._cross_transfer_relay_url)}")

    def run_strm_full_scan(self, reason: str = "手动") -> dict:
        """后台按目录 CID 全量遍历媒体库，补齐/校准 STRM。"""
        sync_handler = getattr(self, "_sync_handler", None)
        if not sync_handler or not hasattr(sync_handler, "rebuild_strm_files"):
            return {"success": False, "message": "同步服务未就绪"}
        lock = self.__dict__.get("_strm_scan_lock")
        if lock is None:
            lock = Lock()
            self._strm_scan_lock = lock
        if not lock.acquire(blocking=False):
            return {"success": False, "message": "已有 STRM 全量校准在运行"}

        overwrite = bool(self._strm_full_scan_overwrite)

        def _run():
            try:
                stats = sync_handler.rebuild_strm_files(overwrite=overwrite)
                logger.info(
                    f"STRM 全量校准完成（{reason}）：目录 {stats.get('directories')}，"
                    f"视频 {stats.get('files')}，新增 {stats.get('created')}，"
                    f"更新 {stats.get('updated')}，跳过 {stats.get('skipped')}，"
                    f"失败 {stats.get('errors')}"
                )
            except Exception as error:  # noqa: BLE001
                logger.error(f"STRM 全量校准失败（{reason}）：{error}")
            finally:
                lock.release()

        Thread(target=_run, daemon=True).start()
        return {"success": True, "message": "已开始后台全量校准，完成后写入日志"}

    def _start_strm_full_scan(self) -> None:
        """每天 05:10 全量校准一次（可在设置页关闭）。"""
        if not (self._strm_full_scan_enabled and self._strm_generate_enabled):
            return
        if self.__dict__.get("_strm_full_scan_scheduler") is not None:
            return
        try:
            scheduler = BackgroundScheduler(timezone=settings.TZ)
            scheduler.add_job(
                func=lambda: self.run_strm_full_scan("定时"),
                trigger=CronTrigger(hour=5, minute=10),
                id="cloudsubscribe_strm_full_scan",
                replace_existing=True,
            )
            scheduler.start()
            self._strm_full_scan_scheduler = scheduler
        except Exception as error:  # noqa: BLE001
            logger.warning(f"STRM 全量校准定时任务启动失败：{error}")

    def cleanup_gopeed_residue(self, reason: str = "手动") -> dict:
        """删除跨盘中继残留（cloud-transfer-*），返回统计。

        判定见 :mod:`core.residue`：仅删「超过 24 小时且不在 Gopeed 任务列表」
        的文件；Gopeed 不可达时退化为纯时间判断（运行中的文件 mtime 会被刷新）。
        """
        from .core.gopeed import GopeedClient, GopeedError
        from .core.residue import scan_residue

        root = self._cross_transfer_download_path or "/tmp"
        active_names: List[str] = []
        if self._gopeed_url:
            try:
                tasks = GopeedClient(
                    self._gopeed_url, self._gopeed_token
                ).list_tasks()
                active_names = [
                    str((task.get("meta") or {}).get("opts", {}).get("name") or "")
                    for task in tasks
                ]
            except GopeedError as error:
                logger.warning(f"跨盘中继残留清理：读取 Gopeed 任务列表失败（{error}），仅按时间判断")
        result = scan_residue(root, active_names=active_names)
        freed_mb = int(result.get("freed_bytes") or 0) / (1024 * 1024)
        logger.info(
            f"跨盘中继残留清理（{reason}）：删除 {result.get('deleted_files')} 个文件、"
            f"释放 {freed_mb:.1f} MB；跳过未过期 {result.get('skipped_recent')} 组、"
            f"任务中 {result.get('skipped_active')} 组"
        )
        for error in result.get("errors") or []:
            logger.warning(f"跨盘中继残留清理：{error}")
        return result

    def _start_residue_cleanup(self) -> None:
        """启动时跑一次 + 每天 04:30 跑一次（同一进程只挂一次）。"""
        if not self._gopeed_residue_cleanup:
            return
        if self.__dict__.get("_residue_scheduler") is not None:
            return
        try:
            scheduler = BackgroundScheduler(timezone=settings.TZ)
            scheduler.add_job(
                func=lambda: self.cleanup_gopeed_residue("每日"),
                trigger=CronTrigger(hour=4, minute=30),
                id="cloudsubscribe_residue_cleanup",
                replace_existing=True,
            )
            scheduler.start()
            self._residue_scheduler = scheduler
        except Exception as error:  # noqa: BLE001
            logger.warning(f"跨盘中继残留定时清理启动失败：{error}")

        def _run_once():
            try:
                self.cleanup_gopeed_residue("启动")
            except Exception as error:  # noqa: BLE001
                logger.warning(f"跨盘中继残留清理失败：{error}")

        Thread(target=_run_once, daemon=True).start()

    @staticmethod
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
        if self._tg_monitor_enabled and not (
                self._tg_api_id and self._tg_api_hash and self._tg_session):
            self._tg_monitor_enabled = False
            if config.get("tg_monitor_enabled"):
                logger.info(
                    "TG 频道监控已关闭：缺少 api_id / api_hash / 会话，"
                    "请在插件设置里先登录"
                )
        try:
            from .core.tg_monitor import get_channel_monitor
            monitor = get_channel_monitor(self)
            monitor.configure(
                enabled=self._tg_monitor_enabled,
                api_id=self._tg_api_id,
                api_hash=self._tg_api_hash,
                session=self._tg_session,
                channels=self._tg_channels,
                keywords=self._tg_monitor_keywords,
                exclude_keywords=self._tg_monitor_exclude_keywords,
                scan_limit=self._tg_monitor_scan_limit,
                interval_minutes=self._tg_monitor_interval_minutes,
                max_items=self._tg_monitor_max_items,
            )
        except Exception as error:  # noqa: BLE001 - 监控不可用不应拖垮插件
            logger.warning(f"TG 频道监控初始化失败：{error}")

    def _apply_notification_config(self, config: Dict[str, Any]) -> None:
        self._notify = bool(config.get("notify", False))
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
        self._media_server_path_mappings = str(
            config.get("media_server_path_mappings", "") or ""
        ).strip()
        self._media_server_refresh_delay = max(
            0, int(config.get("media_server_refresh_delay", 0) or 0)
        )
        self._emby_mediainfo_enabled = bool(
            config.get("emby_mediainfo_enabled", False)
        )
        self._platform_media_sync_enabled = bool(
            config.get("platform_media_sync_enabled", False)
        )
        self._platform_deep_delete_enabled = bool(
            config.get("platform_deep_delete_enabled", False)
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

        configured_resource_order = config.get("resource_type_order", ["115", "ed2k"])
        if not isinstance(configured_resource_order, list):
            configured_resource_order = [configured_resource_order]
        supported_resource_types = {d.id for d in DriverRegistry.get_definitions()} | {"ed2k", "magnet"}
        self._resource_type_order = list(dict.fromkeys(
            str(item).strip().lower()
            for item in configured_resource_order
            if str(item).strip().lower() in supported_resource_types
        ))
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
        self._transfer_task_batch_size = int(config.get("transfer_task_batch_size", 50) or 50)
        self._cross_transfer_enabled = bool(config.get("cross_transfer_enabled", False))
        raw_cross_types = config.get("cross_transfer_media_types", ["movie", "tv"])
        if isinstance(raw_cross_types, str):
            raw_cross_types = [value.strip().lower() for value in raw_cross_types.split(",")]
        self._cross_transfer_media_types = [
                                               value for value in (raw_cross_types or []) if value in {"movie", "tv"}
                                           ] or ["movie", "tv"]
        self._cross_transfer_download_path = str(config.get("cross_transfer_download_path", "") or "").strip()
        self._cross_transfer_download_threads = max(1,
                                                    min(int(config.get("cross_transfer_download_threads", 5) or 5), 256))
        self._cross_transfer_max_concurrent = max(1, min(int(config.get("cross_transfer_max_concurrent", 2) or 2), 10))
        self._cross_transfer_direct_stream = bool(config.get("cross_transfer_direct_stream", True))
        try:
            upload_concurrency = int(
                config.get(
                    "cross_transfer_upload_concurrency",
                    self._cross_transfer_upload_concurrency,
                )
            )
        except (TypeError, ValueError):
            upload_concurrency = 4
        # 0 表示关闭 115 分片并发上传（回退顺序上传），上限 16。
        self._cross_transfer_upload_concurrency = max(0, min(upload_concurrency, 16))
        self._sync_cross_transfer_upload_concurrency()

        self._cross_transfer_downloader = str(
            config.get("cross_transfer_downloader", "builtin") or "builtin"
        ).strip().lower()
        if self._cross_transfer_downloader not in {"builtin", "gopeed"}:
            self._cross_transfer_downloader = "builtin"
        rapid_miss_action = str(
            config.get("cross_transfer_rapid_miss_action", "fallback") or "fallback"
        ).strip().lower()
        # 秒传未命中处理：fallback=回落普通上传（默认）；abort=中止且不真实上传。
        # 非法值一律回退 fallback，保证老行为逐字节不变。
        self._cross_transfer_rapid_miss_action = (
            rapid_miss_action if rapid_miss_action in {"fallback", "abort"} else "fallback"
        )
        # 国内中继上传：开关 + 地址缺一不可，否则保持关闭（避免每个文件白等一次超时）。
        self._cross_transfer_relay_enabled = bool(
            config.get("cross_transfer_relay_enabled", False)
        )
        self._cross_transfer_relay_url = str(
            config.get("cross_transfer_relay_url", "") or ""
        ).strip().rstrip("/")
        self._cross_transfer_relay_token = str(
            config.get("cross_transfer_relay_token", "") or ""
        ).strip()
        try:
            relay_max_gb = int(config.get("cross_transfer_relay_max_file_gb", 25) or 0)
        except (TypeError, ValueError):
            relay_max_gb = 25
        self._cross_transfer_relay_max_file_gb = max(0, min(relay_max_gb, 500))
        try:
            relay_timeout = int(config.get("cross_transfer_relay_timeout_minutes", 180) or 180)
        except (TypeError, ValueError):
            relay_timeout = 180
        self._cross_transfer_relay_timeout_minutes = max(5, min(relay_timeout, 1440))
        self._cross_transfer_relay_fallback_local = bool(
            config.get("cross_transfer_relay_fallback_local", True)
        )
        try:
            relay_threads = int(config.get("cross_transfer_relay_threads", 0) or 0)
        except (TypeError, ValueError):
            relay_threads = 0
        self._cross_transfer_relay_threads = max(0, min(relay_threads, 128))
        if self._cross_transfer_relay_enabled and not self._cross_transfer_relay_url:
            self._cross_transfer_relay_enabled = False
        self._apply_tg_monitor_config(config)
        self._gopeed_url = str(config.get("gopeed_url", "http://gopeed:9999") or "").strip()
        self._gopeed_token = str(config.get("gopeed_token", "") or "").strip()
        self._gopeed_path_prefix = str(config.get("gopeed_path_prefix", "") or "").strip()
        try:
            gopeed_connections = int(config.get("gopeed_connections", 0) or 0)
        except (TypeError, ValueError):
            gopeed_connections = 0
        # 0 = 沿用 Gopeed 全局连接数；>0 = 建任务时显式传给 Gopeed（夸克建议 256）
        self._gopeed_connections = max(0, gopeed_connections)
        self._gopeed_residue_cleanup = bool(
            config.get("gopeed_residue_cleanup", True)
        )

        self._subscription_concurrency = max(1, min(int(config.get("subscription_concurrency", 2) or 2), 5))
        self._batch_size = int(config.get("batch_size", 20) or 20)
        self._batch_interval = max(0, min(float(config.get("batch_interval", 3) or 0), 60))
        self._transfer_risk_cooldown = max(60, min(int(config.get("transfer_risk_cooldown", 1800) or 1800), 86400))
        self._skip_other_season_dirs = config.get("skip_other_season_dirs", True)
        # 跨季路径守卫：分享目录季号与目标季冲突时拒绝进候选（file_matcher 类级开关）
        self._cross_season_path_guard = bool(config.get("cross_season_path_guard", True))
        FileMatcher.path_season_guard_enabled = self._cross_season_path_guard
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
        self._local_resource_path = str(config.get("local_resource_path", "") or "").strip()
        self._type_folder_enabled = bool(config.get("type_folder_enabled", False))
        self._type_folder_movie = (
                str(config.get("type_folder_movie", "电影") or "").strip().strip("/") or "电影"
        )
        self._type_folder_tv = (
                str(config.get("type_folder_tv", "剧集") or "").strip().strip("/") or "剧集"
        )

        self._organize_after_transfer = bool(config.get("organize_after_transfer", True))
        self._organize_subtitles = bool(config.get("organize_subtitles", True))
        self._subtitle_traditional_to_simplified = bool(config.get("subtitle_traditional_to_simplified", False))
        self._anime_pack_preferred = bool(config.get("anime_pack_preferred", True))
        self._offline_timeout = max(10, min(int(config.get("offline_timeout", 30) or 30), 1440))
        self._strm_generate_enabled = bool(config.get("strm_generate_enabled", True))
        self._strm_full_scan_enabled = bool(config.get("strm_full_scan_enabled", True))
        self._strm_full_scan_overwrite = bool(config.get("strm_full_scan_overwrite", False))
        self._nfo_scrape_enabled = bool(config.get("nfo_scrape_enabled", False))
        self._image_scrape_enabled = bool(config.get("image_scrape_enabled", False))
        self._strm_base_url = str(config.get("strm_base_url", "http://172.17.0.1:9527")).strip().rstrip("/")
        self._strm_url_template = str(config.get("strm_url_template", "{base_url}/d/{pickcode}?/{file_name}")).strip()

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

    def _init_clients(self):
        """初始化驱动管理器与跨盘任务调度。"""
        self._cloud_drive = None
        self._cloud_drive_registry = CloudDriveRegistry()
        if self._search_proxy:
            logger.info("搜索渠道已启用统一请求代理")

        self._drive_manager = CloudDriveManager(self)
        self._drive_manager.initialize()
        self._cloud_drive_registry = self._drive_manager.registry
        self._cloud_drive = self._drive_manager.active_drive
        self._cross_transfer_manager = self._drive_manager.cross_transfer_manager
        self._sync_cross_transfer_upload_concurrency()

    def _sync_cross_transfer_upload_concurrency(self) -> None:
        """把 115 分片并发度注入 115 客户端管理器。

        上传组件挂在客户端管理器下、靠属性委托读宿主值，构造期拿不到插件配置，
        所以插件在初始化与每次保存配置后显式注入；注入失败时组件用默认值（4）。
        """
        manager = getattr(self, "_drive_manager", None)
        client = manager.get_client("115") if manager else None
        if not client:
            return
        try:
            client._cross_transfer_upload_concurrency = self._cross_transfer_upload_concurrency
        except Exception as error:  # noqa: BLE001 - 注入失败不影响主流程
            logger.debug(f"注入 115 上传并发度失败：{error}")

    def register_provider(self, key: str) -> bool:
        """根据网盘标识动态注册驱动。"""
        if self._drive_manager:
            return self._drive_manager.register_provider(key)
        return False

    def _init_subscribe_handler(self):
        self._subscribe_handler = SubscribeHandler(plugin=self)

    def _init_handlers(self):
        self._init_subscribe_handler()
        self._search_handler = SearchHandler(plugin=self)
        self._sync_handler = SyncHandler(plugin=self)
        self._sync_handler.reconcile_orphaned_history()

        self._webhook_handler = WebhookHandler(
            enabled=self._webhook_enabled,
            url=self._webhook_url,
            method=self._webhook_method,
            timeout=self._webhook_timeout,
        )

    def _on_file_finalized(
            self,
            transfer_details: List[Dict[str, Any]],
            total_count: int,
    ) -> None:
        """离线文件真正就绪后发送一次汇总 Webhook。"""
        if not self._webhook_handler:
            return
        self._webhook_handler.send_transfer_complete(
            transfer_details=transfer_details,
            total_count=total_count,
        )

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

    def _run_auto_subscribe_once(self) -> None:
        try:
            self.run_auto_subscribe()
        except Exception as error:
            logger.error(f"榜单自动订阅立即执行失败：{error}")

    def stop_service(self, preserve_subscribe_queue: bool = False):
        """停止服务"""
        if not preserve_subscribe_queue and self._subscribe_search_queue_lock is not None:
            self.cancel_pending_subscribe_searches(shutdown=True)
            executor = self._sync_operation_executor
            self._sync_operation_executor = None
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
        if self._stop_event:
            self._stop_event.set()
        if self._sync_tasks_lock is not None:
            with self._sync_tasks_lock:
                for task in self._sync_tasks.values():
                    stop_event = task.get("stop_event")
                    if stop_event:
                        stop_event.set()
        self._restore_subscribe_search_takeover()
        try:
            components = self.__dict__.get("_plugin_components", {})
            message_hook = components.pop(MessageRoutingHook, None)
            if message_hook:
                message_hook.close()
        except Exception as error:
            logger.debug(f"恢复平台消息路由失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            search_api = components.get(SearchApi)
            if search_api:
                search_api.close()
        except Exception as error:
            logger.debug(f"关闭搜索测试连接失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            media_library_api = components.get(MediaLibraryApi)
            if media_library_api:
                media_library_api.close()
        except Exception as error:
            logger.debug(f"关闭媒体库 Webhook 同步调度器失败：{error}")
        try:
            if self._sync_handler:
                self._sync_handler.close()
        except Exception as error:
            logger.debug(f"关闭媒体服务器入库通知器失败：{error}")
        try:
            if self._search_handler:
                self._search_handler.close(release_cache=True)
        except Exception as error:
            logger.debug(f"关闭搜索客户端失败：{error}")
        try:
            if self._drive_manager:
                self._drive_manager.close()
                self._drive_manager = None
        except Exception as error:
            logger.debug(f"关闭网盘客户端失败：{error}")
        try:
            components = self.__dict__.get("_plugin_components", {})
            platform_service = components.pop(PlatformIntegrationService, None)
            if platform_service:
                platform_service.close()
        except Exception as error:
            logger.debug(f"关闭平台缓存失败：{error}")
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception:
            pass

        try:
            if self._offline_scheduler:
                self._offline_scheduler.remove_all_jobs()
                if self._offline_scheduler.running:
                    self._offline_scheduler.shutdown()
                self._offline_scheduler = None
        except Exception:
            pass

        try:
            if self._residue_scheduler:
                self._residue_scheduler.remove_all_jobs()
                if self._residue_scheduler.running:
                    self._residue_scheduler.shutdown()
                self._residue_scheduler = None
        except Exception:
            pass

        try:
            if self._strm_full_scan_scheduler:
                self._strm_full_scan_scheduler.remove_all_jobs()
                if self._strm_full_scan_scheduler.running:
                    self._strm_full_scan_scheduler.shutdown()
                self._strm_full_scan_scheduler = None
        except Exception:
            pass

        try:
            store = self.__dict__.get("_cloudsubscribe_data_store")
            if store:
                store.close()
        except Exception as error:
            logger.debug(f"关闭插件独立数据库失败：{error}")

    @eventmanager.register(EventType.PluginReload)
    def reload_plugin_api(self, event: Event):
        """热重载完成后同步刷新本插件新增或变更的 API 路由。"""
        event_data = event.event_data if event else None
        if isinstance(event_data, dict) and event_data.get("plugin_id") == self.__class__.__name__:
            register_plugin_api(plugin_id=self.__class__.__name__)
