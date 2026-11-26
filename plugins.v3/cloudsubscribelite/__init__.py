"""
网盘订阅助手插件
结合订阅功能，自动搜索网盘资源并同步缺失内容
"""
import ast
import copy
import datetime
import json
import re
import traceback
from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent, Lock, RLock, local, Thread
from typing import Optional, Any, List, Dict, Tuple, Callable, Union

import pytz
from app.api.endpoints.plugin import register_plugin_api
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import DownloaderFile, DownloaderTorrent, Notification
from app.schemas.types import (
    ChainEventType,
    EventType,
    NotificationType,
    SystemConfigKey,
    TorrentQueryStatus,
)
from apscheduler.triggers.cron import CronTrigger

from .version import VERSION
from .utils.notify_template import (
    DEFAULT_TEMPLATES,
    apply_runtime_updates,
    upgrade_legacy_templates,
)
from .core import (
    CloudDriveCapability,
    CloudDriveManager,
    CloudDriveProvider,
    CloudDriveRegistry,
    get_component,
    resolve_component,
)
from .core.aria2 import configure_aria2, resolve_data_dir, shutdown_aria2
from .core.config import (
    CONCURRENCY_AND_COOLDOWN_PARAMS,
    PLUGIN_CONFIG_EXTRA_KEYS as PLUGIN_CONFIG_EXTRA_KEYS,
    is_plugin_config_key,
)
from .core.api import (
    AccountApi,
    ChannelsApi,
    ConfigApi,
    CheckinApi,
    DoctorApi,
    MoviePilotRegistration,
    PageApi,
    QRCodeService,
    RedirectApi,
    RuntimeApi,
    SearchApi,
    SyncApi,
    TgMonitorApi,
)
from .core.channels import ChannelStore
from .core.config_normalize import normalize_plugin_config
from .core.scheduler import SchedulerRegistry
from .core.hook import MessageRoutingHook, PluginEventHandler, SubscriptionSearchHook
from .core.legacy_migration import LegacyIdMigrator
from .core.media_scope import configure_media_path_ignores
from .core.services import (
    SubscriptionControlService,
    CheckinService,
    CacheWatchService,
    OfflineTaskService,
    OrganizeService,
    PlatformIntegrationService,
    ResidueService,
    SubscriptionScoringService,
    SyncExecutionService,
    SyncRuntimeService,
    SkipStatsCollector,
)
from .core.storage import CloudSubscribeDataStore
from .core.downloader_view import (
    DOWNLOADER_NAME,
    DOWNLOADER_TYPE,
    ensure_downloader_entry,
    project_tasks,
    remove_downloader_entry,
    resolve_task_id,
    task_to_files,
)
from .core.subscribe import AutoSubscribeService
from .drive.scanner import DriverRegistry
from .handlers import SearchHandler, SyncHandler, SubscribeHandler, WebhookHandler
from .search.scanner import SearchSourceRegistry
from .search.types import ensure_magnet_for_cloud_policy
from . import subscribe  # noqa: F401 - 导入即注册自动订阅渠道
from .utils import FileMatcher, configure_magnet_metadata_url
from .utils.file_parser import MediaFileParser
from .utils.http_client import build_proxy_url, validate_proxy_address
from .core.services.upload_quota import UploadQuota, clamp_limit_gb
from .core.plugin_utils import PluginUtilsMixin
from .core.source_cache import SourceCacheMixin
from .core.scheduled_tasks import ScheduledTasksMixin
from .core.channels import ChannelMixin
from .core.tg_monitor_config import TgMonitorConfigMixin
from .core.plugin_config import PluginConfigMixin
from .core.downloader_view import DownloaderMixin

_COMPONENT_TYPES = (
    PageApi,
    DoctorApi,
    AccountApi,
    ChannelsApi,
    SearchApi,
    ConfigApi,
    CheckinApi,
    SyncApi,
    RuntimeApi,
    QRCodeService,
    RedirectApi,
    TgMonitorApi,
    MessageRoutingHook,
    PluginEventHandler,
    MoviePilotRegistration,
    SyncRuntimeService,
    OfflineTaskService,
    SyncExecutionService,
    SubscriptionScoringService,
    SubscriptionControlService,
    SubscriptionSearchHook,
    PlatformIntegrationService,
    CheckinService,
    OrganizeService,
    ResidueService,
    CacheWatchService,
    AutoSubscribeService,
)


class CloudSubscribeLite(
    PluginConfigMixin,
    TgMonitorConfigMixin,
    ChannelMixin,
    DownloaderMixin,
    ScheduledTasksMixin,
    SourceCacheMixin,
    PluginUtilsMixin,
    _PluginBase,
):
    """网盘订阅助手插件。

    使用独立插件 ID ``CloudSubscribeLite``，与早期同名插件（ID ``CloudSubscribe``）
    彻底解耦：两者可在 MoviePilot 里并存安装、各自独立升级。首次启动时会自动从旧
    ID 迁移配置、持久化数据与私有数据库（见 :meth:`_migrate_from_legacy_plugin`）。
    """

    def post_message(
            self,
            channel=None,
            mtype=None,
            title=None,
            text=None,
            image=None,
            link=None,
            userid=None,
            **kwargs,
    ):
        """发送通知，并关闭 Telegram 的网页预览卡片。

        MoviePilot 默认把插件通知的 link 指向插件页；Telegram 会为这个链接
        生成一张大预览卡，和通知正文叠在一起很占屏。这里保留「查看详情」链接，
        但显式关闭预览。旧版基类不支持该参数时回退原逻辑。
        """
        if not link:
            link = settings.MP_DOMAIN(
                f"#/plugins?tab=installed&id={self.__class__.__name__}"
            )
        kwargs.setdefault("disable_web_page_preview", True)
        try:
            notification = Notification(
                channel=channel,
                mtype=mtype,
                title=title,
                text=text,
                image=image,
                link=link,
                userid=userid,
                **kwargs,
            )
        except TypeError:
            kwargs.pop("disable_web_page_preview", None)
            return super().post_message(
                channel=channel,
                mtype=mtype,
                title=title,
                text=text,
                image=image,
                link=link,
                userid=userid,
                **kwargs,
            )
        return self.chain.post_message(notification)

    # 插件名称
    plugin_name = "网盘订阅 Lite"
    # 插件描述
    plugin_desc = "搜索网盘资源补齐订阅缺失影视，支持跨盘转存、洗版与整理。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/xymhh/MoviePilot-Plugins/main/icons/cloud.png"
    # 插件版本
    plugin_version = VERSION
    # 插件作者
    plugin_author = "xymhh"
    # 作者主页
    author_url = "https://github.com/xymhh"
    # 插件配置项ID前缀（保持 cloudsubscribe_ 不变，旧配置键与旧数据键无需重写）
    plugin_config_prefix = "cloudsubscribe_"
    # 旧插件 ID：早期版本与同名旧插件都用它，首次启动时从它迁移
    legacy_plugin_id = "CloudSubscribe"
    plugin_order = 21
    auth_level = 1

    # 私有变量
    # 插件唯一的定时任务注册表（core.scheduler.SchedulerRegistry）：全部
    # 周期/一次性任务按 id 登记在这里，stop_service 一次停掉，不再各自建调度器。
    _job_registry: Optional[SchedulerRegistry] = None
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
    # 真实上传 115 的代理（空=直连）与每日字节额度（硬上限 900GB）
    _cross_transfer_upload_proxy: str = ""
    _cross_transfer_daily_upload_limit_gb: int = 900
    _upload_quota: Any = None
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
    _cross_transfer_download_threads: int = 16
    _cross_transfer_max_concurrent: int = 2
    _cross_transfer_aria2_enabled: bool = False
    _cross_transfer_aria2_connections: int = 64
    _cross_transfer_aria2_max_concurrent: int = 2
    _cross_transfer_aria2_min_split_size: str = "1M"
    _cross_transfer_aria2_disk_cache: str = "32M"
    _cross_transfer_direct_stream: bool = True
    _cross_transfer_upload_concurrency: int = 2
    # 115 上传熔断器配置（Phase 3b-10）：连续失败几次后打开、冷却多久。
    _cross_transfer_upload_breaker_threshold: int = 5
    _cross_transfer_upload_breaker_cooldown_seconds: float = 300.0
    _cross_transfer_rapid_miss_action: str = "abort"
    # 1.35.0：上传到待整理目录后由 CS 自己整理（默认关）。
    _organize_after_upload: bool = False
    _organize_staging_path: str = ""
    _organize_staging_cleanup: bool = True
    # 1.32.0：真名落盘媒体残留的保留期（小时，0=关闭）。
    _cross_transfer_real_name_retention_hours: int = 24
    # 国内中继上传（实验）：默认关闭，未配置地址时强制视为关闭。
    # TG 频道监控（用户自己的 TG API 会话）：默认关闭，未配置凭据时强制视为关闭。
    _tg_monitor_enabled: bool = False
    _tg_api_id: int = 0
    _tg_api_hash: str = ""
    _tg_session: str = ""
    _tg_channels: list = []
    _tg_channel_rules: list = []
    _tg_monitor_keywords: list = []
    _tg_monitor_exclude_keywords: list = []
    _tg_monitor_scan_limit: int = 20
    _tg_monitor_interval_minutes: int = 10
    _tg_monitor_max_items: int = 500
    #: 渠道库（channels.json）：搜索源与 TG 监控频道的唯一运行来源。
    #: ``None`` 表示尚未按当前数据目录惰性创建。
    _channels: Optional[ChannelStore] = None
    # 跨盘缓存残留清理：启动时一次孤儿清理 + 手动按钮（1.10.0 起任务结束
    # 即删临时文件，不再保留缓存、定时清理与磁盘压力清理）
    _residue_cleanup: bool = True
    #: STRM 全量校准单次时间预算（秒），0 表示不限；到达上限后断点续扫。
    _strm_full_scan_budget: int = 900
    _subscription_concurrency: int = 2
    _batch_size: int = 20
    _batch_interval: float = 3
    _transfer_risk_cooldown: int = 1800
    _skip_other_season_dirs: bool = True
    _cross_season_path_guard: bool = True
    # 「节目名 = 每期集数」多行配置：综艺期号换算成集号（留空=期号即集号）
    _period_episode_rules_text: str = ""
    _video_extensions: List[str] = [".mkv", ".mp4", ".avi", ".iso"]
    _subtitle_extensions: List[str] = [
        ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks"
    ]

    # 1.12.20 Phase 1：补齐只做 setattr 挂载的运行期属性（此前仅在方法内
    # 赋值，类上未声明，配置应用前直接读取会走 OwnerDelegator 委托甚至报错）。
    _cross_transfer_manager: Optional[Any] = None
    _notify_templates: Dict[str, str] = {}
    _organize_source_path: str = ""
    _organize_max_files: int = 500
    _organize_max_depth: int = 8
    _organize_include_subtitles: bool = True
    _organize_conflict_policy: str = "skip"
    _organize_schedule_enabled: bool = False
    _organize_schedule_cron: str = "0 4 * * *"
    _organize_schedule_auto_execute: bool = True
    _organize_schedule_notify: bool = True
    _subscribe_search_recent: Dict[str, Any] = {}

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
    _quality_min_gain_percent: int = 10
    _upgrade_episode_limit: int = 0
    _quality_min_gain_mb: int = 200
    _local_resource_path: str = ""
    # 媒体库一级目录（按媒体类型分层：电影/剧集），仅作用于网盘媒体库与本地 STRM 目录结构
    _type_folder_enabled: bool = False
    _type_folder_movie: str = "电影"
    _type_folder_tv: str = "剧集"
    _strm_generate_enabled: bool = False
    _nfo_scrape_enabled: bool = False
    _image_scrape_enabled: bool = False
    _strm_base_url: str = ""
    _strm_url_template: str = (
        "{base_url}/api/v1/plugin/CloudSubscribeLite/redirect"
        "?pickcode={pickcode}&file_name={file_name}"
    )
    _strm_full_scan_enabled: bool = False
    _strm_full_scan_overwrite: bool = False
    _strm_increment_enabled: bool = False
    _strm_increment_minutes: int = 10
    _strm_increment_budget: int = 180
    _media_server_refresh_enabled: bool = False
    _media_servers: List[str] = []
    _media_server_ignore_paths: str = ""
    _media_server_path_mappings: str = ""
    _media_server_refresh_delay: int = 0
    _emby_mediainfo_enabled: bool = False
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

    def _scheduler_registry(self) -> SchedulerRegistry:
        """插件唯一的定时任务注册表（core.scheduler.SchedulerRegistry）。

        正常由 init_plugin 在 _apply_plugin_config 之前创建；这里兜底懒创建，
        保证配置保存、离线监控等在任何调用顺序下都能登记任务。注册表在
        shutdown 后可重建，所以 stop_service 之后再登记不需要额外处理。
        """
        registry = self.__dict__.get("_job_registry")
        if registry is None:
            registry = SchedulerRegistry()
            self.__dict__["_job_registry"] = registry
        return registry

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

    # 注意：有意不监听 EventType.SubscribeModified——用户手动修改订阅
    # 站点时，不自动覆盖用户操作（1.12.20 随 Phase 1 取消注册，
    # 原 PluginEventHandler.on_subscribe_modified 本就是空操作）。

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

    @staticmethod

    @staticmethod


    def init_plugin(self, config: dict = None):
        """宿主加载或重载插件时初始化完整运行环境。"""
        self._raise_file_limit()
        # 独立 ID 的首次启动：先把旧 ID 的配置/数据/私有数据库搬过来，再建库并应用配置。
        config = self._migrate_from_legacy_plugin(config)
        self._offline_monitor_lock = RLock()
        self._offline_scheduler_lock = RLock()
        # 统一调度器先建好：_apply_plugin_config 末尾的各 _start_* 直接登记任务。
        self.__dict__["_job_registry"] = SchedulerRegistry()
        # 初始化独立数据库并修复历史分组键。
        self._get_data_store().initialize()
        self._apply_plugin_config(config, reset_runtime=True)
        self._ensure_notify_template_defaults(config)
        # 在下载管理页登记独立标签，任务进度改走下载页展示。
        self._ensure_downloader_entry()


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


    def _require_sync_handler(
            self, *method_names: str,
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """取同步服务并确认它有要调用的方法；返回 ``(handler, None)`` 或 ``(None, 失败结果)``。

        v1.27.0：区分两种“用不了”——
        * 同步服务还没初始化（插件启动中 / 未启用）：照旧返回“同步服务未就绪”；
        * 同步服务在、但缺方法：这是插件内部错误（v1.19.0 漏转发那类，会让功能
          悄悄停摆），记 ERROR 并返回带 ``internal_error`` 的明确报错；
          走 MP 调度时由 ``_run_scheduled_task`` 转成异常交给 MP 报出。
        """
        sync_handler = getattr(self, "_sync_handler", None)
        if not sync_handler:
            return None, {"success": False, "message": "同步服务未就绪"}
        missing = [name for name in method_names if not hasattr(sync_handler, name)]
        if missing:
            message = (
                f"同步服务缺少方法：{'、'.join(missing)}（插件内部错误，相关功能已停用，"
                f"请升级或反馈）"
            )
            logger.error(f"{message}——{type(sync_handler).__name__} 上找不到这些方法")
            return None, {"success": False, "message": message, "internal_error": True}
        return sync_handler, None

    def refresh_media_servers(self) -> dict:
        """手动触发媒体库刷新。"""
        sync_handler, unavailable = self._require_sync_handler("refresh_media_servers",)
        if unavailable:
            return unavailable
        try:
            data = sync_handler.refresh_media_servers()
        except Exception as error:  # noqa: BLE001
            return {"success": False, "message": str(error)}
        return {
            "success": True,
            "message": f"已提交媒体库刷新：{data or '无可用媒体服务器'}",
            "data": data,
        }

    # 源文件 SHA1 缓存（秒传未命中后零流量重试）：与 v3 X 一致的取值。
    # 注意：这两个常量曾在精简时误删，get/put_cached_source_sha1 仍引用
    # 它们，缺失会在运行时触发 AttributeError，此处恢复。
    _SOURCE_SHA1_CACHE_TTL_DAYS = 30
    _SOURCE_SHA1_CACHE_LIMIT = 2000


    # 秒传未命中负缓存：24 小时内同一哈希不再下载整份文件重试
    _SOURCE_MISS_CACHE_TTL_HOURS = 24
    _SOURCE_MISS_CACHE_LIMIT = 4000


    @staticmethod

    @staticmethod

    # -------------------------------------------------------------- 渠道库


    @staticmethod


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
        self._sync_upload_breaker_config()


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


    def _run_auto_subscribe_once(self) -> None:
        try:
            self.run_auto_subscribe()
        except Exception as error:
            logger.error(f"榜单自动订阅立即执行失败：{error}")

    # ---------- 下载器投影：任务进度改走 MP 下载管理页 ----------


    # ---------- MP 原生搜索：搜索结果进搜索页，下载走本插件转存 ----------
    # 实现在 core/mp_search.py，这里只做薄委托（与 list_torrents 委托给
    # downloader_view.py 一个写法）。


    def get_module(self) -> dict:
        """按模块合同向宿主声明下载器、搜索与下载能力。

        MP 下载页查任务与搜索页查插件结果都走模块调度器，只认插件
        get_module() 返回的字典：1.9.87 只声明了 5 个下载器方法，
        调度器根本不调本插件的 list_torrents；这次补上
        search_torrents 与 download，搜索页才能搜到订阅助手的结果，
        点下载后才走本插件自己的转存流程。
        """
        return {
            "search_torrents": self.search_torrents,
            "download": self.download,
            "list_torrents": self.list_torrents,
            "torrent_files": self.torrent_files,
            "start_torrents": self.start_torrents,
            "stop_torrents": self.stop_torrents,
            "remove_torrents": self.remove_torrents,
        }

    def stop_service(self, preserve_subscribe_queue: bool = False):
        """停止服务"""
        try:
            shutdown_aria2()
        except Exception as error:  # noqa: BLE001 - 下载引擎停止失败不得阻塞卸载
            logger.warning(f"关闭 aria2 下载引擎失败：{error}")
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
            logger.debug(f"恢复平台消息路由失败：{error}", exc_info=True)
        try:
            components = self.__dict__.get("_plugin_components", {})
            search_api = components.get(SearchApi)
            if search_api:
                search_api.close()
        except Exception as error:
            logger.debug(f"关闭搜索测试连接失败：{error}", exc_info=True)
        try:
            if self._sync_handler:
                self._sync_handler.close()
        except Exception as error:
            logger.debug(f"关闭媒体服务器入库通知器失败：{error}", exc_info=True)
        try:
            if self._search_handler:
                self._search_handler.close(release_cache=True)
        except Exception as error:
            logger.debug(f"关闭搜索客户端失败：{error}", exc_info=True)
        try:
            if self._drive_manager:
                self._drive_manager.close()
                self._drive_manager = None
        except Exception as error:
            logger.debug(f"关闭网盘客户端失败：{error}", exc_info=True)
        try:
            components = self.__dict__.get("_plugin_components", {})
            components.pop(PlatformIntegrationService, None)
        except Exception as error:
            logger.debug(f"关闭平台缓存失败：{error}", exc_info=True)
        # 统一调度器一次停全部：包括以前漏停的历史状态维护与 115 整理两个任务。
        try:
            registry = self.__dict__.get("_job_registry")
            if registry is not None:
                registry.shutdown()
                self.__dict__["_job_registry"] = None
        except Exception:
            logger.debug("stop_service 分支忽略异常", exc_info=True)

        try:
            store = self.__dict__.get("_cloudsubscribe_data_store")
            if store:
                store.close()
        except Exception as error:
            logger.debug(f"关闭插件独立数据库失败：{error}", exc_info=True)

        # 停用/卸载后下载页不再留一个空标签；下次 init_plugin 会重新登记。
        try:
            self._remove_downloader_entry()
        except Exception as error:  # noqa: BLE001 - 移除失败不阻断停机
            logger.debug(f"移除下载器条目失败：{error}", exc_info=True)

    @eventmanager.register(EventType.PluginReload)
    def reload_plugin_api(self, event: Event):
        """热重载完成后同步刷新本插件新增或变更的 API 路由。"""
        event_data = event.event_data if event else None
        if isinstance(event_data, dict) and event_data.get("plugin_id") == self.__class__.__name__:
            register_plugin_api(plugin_id=self.__class__.__name__)
