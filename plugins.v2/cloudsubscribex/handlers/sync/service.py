"""
同步处理模块
负责核心的同步逻辑：处理电影订阅、处理电视剧订阅
"""
import copy
import hashlib
import re
import threading
import time
import traceback
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from typing import List, Dict, Any, Set, Optional, Callable, Tuple, Mapping, Iterable

from app.core.context import MediaInfo
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper

try:
    from app.helper.directory import DirectoryHelper
except Exception:
    try:
        from app.application.directory import DirectoryHelper
    except Exception:
        DirectoryHelper = None

from app.log import logger
from app.schemas.types import MediaType, NotificationType

try:
    from app.utils.http import RequestUtils
except Exception:
    try:
        from app.adapters.network.http import RequestUtils
    except Exception:
        RequestUtils = None

from .baseline import UpgradeBaselineService
from .cleanup import HistoryCleanupService
from .episode_dedup import EpisodeDedupService
from .landed import LandedVerifyService
from .matching import FileMatchingService
from .media_refresh import MediaRefreshMixin
from .media_targets import MediaTargetsService
from .mp_organize import MpOrganizeMixin
from .strm_ops import StrmOpsMixin
from .transfer_ops import TransferOpsMixin

# 向后兼容：跨盘暂存引用计数与路径工具已搬到 transfer_ops，旧调用方（含单测）
# 仍从本模块取用，这里按引用再导出，保证取到的是同一份状态对象。
from .transfer_ops import (  # noqa: F401  (兼容再导出)
    _STAGING_REFS,
    _STAGING_REFS_LOCK,
    _acquire_staging_ref,
    _release_staging_ref,
    _safe_optional_int,
    _staging_ref_key,
)
from .metadata import SyncMetadataService
from .movie import MovieSyncProcessor
from .naming import SyncNamingService
from .notify import SyncNotificationService
from .platform_history import PlatformHistoryService
from .postprocess import PostprocessService
from .pt_upgrade import PtUpgradeService
from .resources import ResourceTransferService
from .retry import HistoryRetryService
from .rule_scoring import UpgradeRuleScoringService
from .subscribe_flow import SubscribeFlowService
from .subtitles import SubtitleService
from .television import TelevisionSyncProcessor
from .transfer_details import TransferDetailsService
from .upgrade import UpgradeService
from .utils import extract_ed2k_filename, format_episode_ranges
from ...search.magnet import extract_magnet_hash
from ..notification import MediaServerNotifier, MediaServerResolver
from ..search import SearchHandler
from ..subscription import SubscribeHandler
from ...core import (
    CloudDriveCapability,
    CloudDriveProvider,
    get_component,
    MediaScraper,
)
from ...core.media import (
    legacy_media_ids,
    list_subscribes_by_tmdb_id,
    tmdb_id_of,
)
from ...utils import (
    StrmGenerator,
    StrmTemplateError,
)
from ...utils.cache import create_platform_ttl_cache

# Phase 5（v1.19.0）：显式组合——25 个职责组件以具名服务字段持有。
# 构造顺序沿用旧 _COMPONENT_TYPES（顺序本身不再有解析语义，删掉
# SyncHandler.__getattr__ + resolve_component 的隐式解析后，调用关系
# 在代码里可见）。每个字段与 _handler_components 缓存指向同一实例
# （构造时复用 get_component），测试打桩工具 stub_component_method
# 仍可沿用。
COMPONENT_FIELDS = (
    ("movie", MovieSyncProcessor),
    ("television", TelevisionSyncProcessor),
    ("history_cleanup", HistoryCleanupService),
    ("history_retry", HistoryRetryService),
    ("transfer_details", TransferDetailsService),
    ("media_targets", MediaTargetsService),
    ("file_matching", FileMatchingService),
    ("postprocess", PostprocessService),
    ("upgrade_baseline", UpgradeBaselineService),
    ("upgrade_rule_scoring", UpgradeRuleScoringService),
    ("episode_dedup", EpisodeDedupService),
    ("landed_verify", LandedVerifyService),
    ("resource_transfer", ResourceTransferService),
    ("subtitles", SubtitleService),
    ("upgrade", UpgradeService),
    ("pt_upgrade", PtUpgradeService),
    ("metadata", SyncMetadataService),
    ("naming", SyncNamingService),
    ("notifications", SyncNotificationService),
    ("subscribe_flow", SubscribeFlowService),
    ("platform_history", PlatformHistoryService),
    ("media_refresh", MediaRefreshMixin),
    ("mp_organize", MpOrganizeMixin),
    ("strm_ops", StrmOpsMixin),
    ("transfer_ops", TransferOpsMixin),
)


class SyncHandler:
    """同步处理器"""

    _OFFLINE_PENDING_KEY = "pending_offline_strm"
    _OFFLINE_CHECK_DELAYS = (10, 20, 40, 60, 120, 300)
    _OFFLINE_TIMEOUT = 30 * 60
    # Phase 4：finalize 阈值与超时只在这里定义一处，postprocess.py 只
    # 引用、不再自立类常量。离线任务（ed2k/magnet/offline）连续失败
    # _FINALIZE_MAX_FAILURES 次判死；其余任务（插件自己的定位/移动）
    # _FINALIZE_MAX_FAILURES_LOCAL 次或 _FINALIZE_LOCATE_TIMEOUT_SECONDS
    # 秒未定位到判死。MP 整理等待不受这两个约束（走
    # _poll_mp_organize_state 的 90 分钟总超时）。
    _FINALIZE_MAX_FAILURES = 5
    _FINALIZE_MAX_FAILURES_LOCAL = 2
    _FINALIZE_LOCATE_TIMEOUT_SECONDS = 120.0
    _FILE_FINALIZE_TIMEOUT = 30 * 60
    _OFFLINE_MONITOR_LEASE_SECONDS = 15 * 60
    # v1.26.0：待后处理记录的绝对过期（自首次出现起算）。created_at 会被
    # 重登记重置（_build_pending_record 的 fresh_attempt），不能当作时钟；
    # first_seen_at 在重登记时保留（见 _build_pending_record），只增不减。
    _PENDING_ABSOLUTE_TIMEOUT_SECONDS = 6 * 60 * 60
    _MEDIA_RECOGNITION_CACHE_LIMIT = 256
    _PLATFORM_ROOT_CACHE_LIMIT = 256
    _RESOURCE_SEASON_DIR_CACHE_LIMIT = 256
    _SUBSCRIBE_DEFER_CACHE_LIMIT = 512
    _SUBSCRIBE_CALENDAR_CACHE_LIMIT = 512
    _RUNTIME_CACHE_TTL = 6 * 60 * 60
    _SUBSCRIBE_DEFER_CACHE_TTL = 32 * 24 * 60 * 60
    _SUBSCRIBE_CALENDAR_CACHE_TTL = 26 * 60 * 60
    _NOTIFICATION_BATCH_WINDOW_SECONDS = 2
    _CLOUD_MEDIA_ROOT = "/"
    _OFFLINE_RESOURCE_URL_RE = re.compile(
        r"ed2k://\|file\|[^|\r\n]+\|\d+\|[0-9A-Fa-f]{32}"
        r"(?:\|(?:h|p)=[^|\r\n]+)*\|/|magnet:\?[^\s\r\n]+",
        re.IGNORECASE,
    )

    def __init__(
            self,
            plugin: Optional[Any] = None,
            cloud_drive: Optional[CloudDriveProvider] = None,
            search_handler: Optional[SearchHandler] = None,
            subscribe_handler: Optional[SubscribeHandler] = None,
            chain: Any = None,
            cloud_transfer_path: Optional[str] = None,
            cloud_media_root: Optional[str] = None,
            cloud_transfer_paths: Optional[Mapping[str, str]] = None,
            cloud_media_paths: Optional[Mapping[str, str]] = None,
            **kwargs,
    ):
        """
        初始化同步处理器。

        :param plugin: 宿主插件实例（如 CloudSubscribe），提供全局配置与平台能力回调
        :param cloud_drive: 当前网盘提供方（可选，默认从 plugin 获取）
        :param search_handler: 搜索处理器（可选，默认从 plugin 获取）
        :param subscribe_handler: 订阅处理器（可选，默认从 plugin 获取）
        :param chain: MediaChain 实例（可选，默认从 plugin 获取）
        :param cloud_transfer_path: 当前网盘转存暂存路径（可选）
        :param cloud_media_root: 当前网盘媒体库分类根目录（可选）
        :param cloud_transfer_paths: 各网盘提供方的转存暂存路径映射（可选）
        :param cloud_media_paths: 各网盘提供方的媒体库路径映射（可选）
        """
        self._plugin = plugin

        # Phase 5（v1.19.0）：显式组合——具名服务字段放在 __init__ 最前面；
        # 组件只存 _owner、构造无副作用；__init__ 后面会直接调组件方法
        # （如 _normalize_cross_transfer_media_type），字段必须先就绪。
        # 字段与 _handler_components 缓存指向同一实例。
        for _field_name, _component_type in COMPONENT_FIELDS:
            setattr(self, _field_name, get_component(self, _component_type, "_handler_components"))

        # v1.27.1：单订阅「一条都没转存」的真实原因，唯一归属在 SyncHandler
        # （见下方 _last_skip_reason 属性）。按线程隔离：订阅组在线程池里并发跑，
        # 共用这一个 SyncHandler，一组的原因不能被另一组覆盖或清空。
        self._skip_reason_local = threading.local()

        def _get_val(name: str, default: Any = None) -> Any:
            if name in kwargs and kwargs[name] is not None:
                return kwargs[name]
            if plugin is not None:
                if hasattr(plugin, f"_{name}"):
                    val = getattr(plugin, f"_{name}")
                    if val is not None:
                        return val
                if hasattr(plugin, name):
                    val = getattr(plugin, name)
                    if val is not None:
                        return val
            return default

        self._init_host_handles(cloud_drive, search_handler, subscribe_handler, chain)
        self._init_organize_flags(_get_val)
        self._init_cloud_services()
        self._init_batch_limits(_get_val)
        self._init_plugin_callbacks(_get_val)
        self._init_upgrade_rules(_get_val)
        self._init_cloud_paths(cloud_transfer_path, cloud_media_root, cloud_transfer_paths, cloud_media_paths)
        self._init_strm_media_server(_get_val)
        self._init_locks_caches()

    def _init_host_handles(self, cloud_drive, search_handler, subscribe_handler, chain):
        """Phase 5 第 3 项（v1.20.0）：宿主句柄：网盘/搜索/订阅处理器与 MediaChain，缺省时从插件上取。原 __init__ 第 213-233 行原样搬移，行为不变。"""
        self._cloud_drive = (
            cloud_drive
            if cloud_drive is not None
            else getattr(self._plugin, "_cloud_drive", None) if self._plugin else None
        )
        self._search_handler = (
            search_handler
            if search_handler is not None
            else getattr(self._plugin, "_search_handler", None) if self._plugin else None
        )
        self._subscribe_handler = (
            subscribe_handler
            if subscribe_handler is not None
            else getattr(self._plugin, "_subscribe_handler", None) if self._plugin else None
        )
        self._chain = (
            chain
            if chain is not None
            else getattr(self._plugin, "chain", None) if self._plugin else None
        )


    def _init_organize_flags(self, get_val):
        """Phase 5 第 3 项（v1.20.0）：整理与转存开关：整理后整理、离线超时、跨盘开关与媒体类型、网盘注册表。原 __init__ 第 234-259 行原样搬移，行为不变。"""
        self._organize_after_transfer = bool(get_val("organize_after_transfer", True))
        self._mp_organize_after_share_save = bool(
            get_val("mp_organize_after_share_save", True)
        )
        self._organize_subtitles = bool(get_val("organize_subtitles", True))
        self._subtitle_traditional_to_simplified = bool(
            get_val("subtitle_traditional_to_simplified", False)
        )
        offline_timeout = get_val("offline_timeout", 30)
        self._offline_timeout = max(10, min(int(offline_timeout or 30), 1440)) * 60
        self._OFFLINE_TIMEOUT = self._offline_timeout
        self._cross_transfer_enabled = bool(get_val("cross_transfer_enabled", False))
        cross_media_types = get_val("cross_transfer_media_types", ("movie", "tv"))
        self._cross_transfer_media_types = {
            self.resource_transfer._normalize_cross_transfer_media_type(value)
            for value in (cross_media_types or ("movie", "tv"))
        }
        self._cloud_drive_registry = get_val(
            "cloud_drive_registry",
            getattr(self._plugin, "_cloud_drive_registry", None) if self._plugin else None
        )
        self._cross_transfer_manager = get_val(
            "cross_transfer_manager",
            getattr(self._plugin, "_cross_transfer_manager", None) if self._plugin else None
        )


    def _init_cloud_services(self):
        """Phase 5 第 3 项（v1.20.0）：网盘能力服务：按 CloudDriveCapability 向当前网盘取 12 个可选服务。原 __init__ 第 260-296 行原样搬移，行为不变。"""
        self._cloud_auth = self._optional_cloud_service(
            CloudDriveCapability.AUTHENTICATION
        )
        self._cloud_account = self._optional_cloud_service(
            CloudDriveCapability.ACCOUNT
        )
        self._share_transfer = self._optional_cloud_service(
            CloudDriveCapability.SHARE_TRANSFER
        )
        self._offline_download = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_DOWNLOAD
        )
        self._cloud_directories = self._optional_cloud_service(
            CloudDriveCapability.DIRECTORY_READ
        )
        self._cloud_query = self._optional_cloud_service(
            CloudDriveCapability.FILE_QUERY
        )
        self._cloud_mutations = self._optional_cloud_service(
            CloudDriveCapability.FILE_MUTATION
        )
        self._checksum_rename = self._optional_cloud_service(
            CloudDriveCapability.CHECKSUM_RENAME
        )
        self._cloud_batch_mutations = self._optional_cloud_service(
            CloudDriveCapability.BATCH_FILE_MUTATION
        )
        self._playback_reference = self._optional_cloud_service(
            CloudDriveCapability.PLAYBACK_REFERENCE
        )
        self._offline_tasks = self._optional_cloud_service(
            CloudDriveCapability.OFFLINE_TASKS
        )
        self._cloud_upload = self._optional_cloud_service(
            CloudDriveCapability.LOCAL_UPLOAD
        )


    def _init_batch_limits(self, get_val):
        """Phase 5 第 3 项（v1.20.0）：批量与风控：每批数量/间隔、风控冷却、离线黑名单。原 __init__ 第 297-319 行原样搬移，行为不变。"""
        transfer_task_batch_size = get_val("transfer_task_batch_size", 50)
        self._transfer_task_batch_size = max(
            1, min(int(transfer_task_batch_size or 50), 1000)
        )
        policy = self._cloud_drive.policy if self._cloud_drive else None
        configured_batch_size = max(1, int(get_val("batch_size", 20) or 1))
        self._batch_size = min(
            configured_batch_size,
            policy.max_batch_size if policy and policy.supports_batch else configured_batch_size,
        )
        self._batch_interval = max(0.0, min(float(get_val("batch_interval", 3) or 0), 60.0))
        transfer_risk_cooldown = get_val("transfer_risk_cooldown", 1800)
        self._transfer_risk_cooldown = max(
            60, min(int(transfer_risk_cooldown or 1800), 86400)
        )
        self._share_transfer_risk_lock = threading.Lock()
        self._share_transfer_blocked_until: Dict[str, float] = {}
        self._offline_blacklist = create_platform_ttl_cache(
            "offline_blacklist",
            ttl=86400,
            maxsize=10000,
        )
        self._skip_other_season_dirs = bool(get_val("skip_other_season_dirs", True))


    def _init_plugin_callbacks(self, get_val):
        """Phase 5 第 3 项（v1.20.0）：插件回调：通知、取数/存数、停止信号、任务更新等宿主回调。原 __init__ 第 320-363 行原样搬移，行为不变。"""
        self._notify = bool(get_val("notify", False))
        self._notification_type = get_val("notification_type", NotificationType.Plugin)

        self._post_message = get_val(
            "post_message_func",
            getattr(self._plugin, "post_message", None) if self._plugin else None
        )
        self._get_data = get_val(
            "get_data_func",
            getattr(self._plugin, "get_data", None) if self._plugin else None
        )
        self._save_data = get_val(
            "save_data_func",
            getattr(self._plugin, "save_data", None) if self._plugin else None
        )
        self._should_stop = get_val(
            "should_stop",
            getattr(self._plugin, "_stop_requested", None) if self._plugin else None
        )
        self._offline_pending_changed = get_val(
            "offline_pending_changed",
            getattr(self._plugin, "_update_offline_monitor", None) if self._plugin else None
        )
        self._history_changed = get_val(
            "history_changed",
            getattr(self._plugin, "_mark_history_changed", None) if self._plugin else None
        )
        self._file_finalized = get_val(
            "file_finalized",
            getattr(self._plugin, "_on_file_finalized", None) if self._plugin else None
        )
        self._task_update = get_val(
            "task_update",
            getattr(self._plugin, "_update_sync_task", None) if self._plugin else None
        )
        self._postprocess_task_update = get_val(
            "postprocess_task_update",
            getattr(self._plugin, "_update_postprocess_task", None) if self._plugin else None
        )
        self._task_context = get_val(
            "task_context",
            getattr(self._plugin, "_current_task_context", None) if self._plugin else None
        )


    def _init_upgrade_rules(self, get_val):
        """Phase 5 第 3 项（v1.20.0）：洗版与判重规则：自愈间隔、洗版开关/订阅/模式、集数判重、画质偏好、本地路径与分类目录。原 __init__ 第 364-395 行原样搬移，行为不变。"""
        self._self_heal_interval = get_val("self_heal_interval", 10)
        self._enable_cloud_upgrade = bool(get_val("enable_cloud_upgrade", False))
        self._enable_pt_upgrade = bool(get_val("enable_pt_upgrade", False))
        if self._enable_pt_upgrade and not self._cloud_upload:
            logger.warning("PT洗版已启用，但当前网盘不支持本地文件上传")
        upgrade_sub_ids = get_val("upgrade_subscribe_ids", None)
        self._upgrade_subscribe_ids = list(upgrade_sub_ids or [])
        self._upgrade_subscribe_id_set = {
            str(value) for value in self._upgrade_subscribe_ids
        }
        raw_mode = str(get_val("upgrade_mode", "largest") or "largest").strip().lower()
        self._upgrade_mode = (
            raw_mode if raw_mode in {"coexist", "replace", "largest", "smallest"} else "largest"
        )
        self._episode_dedup_enabled = bool(get_val("episode_dedup_enabled", True))
        self._verify_landed_enabled = bool(get_val("verify_landed_enabled", True))
        self._prefer_template_named_version = bool(
            get_val("prefer_template_named_version", True)
        )
        self._quality_prefer_enabled = bool(get_val("quality_prefer_enabled", False))
        self._quality_min_gain_percent = int(get_val("quality_min_gain_percent", 10) or 0)
        self._quality_min_gain_mb = int(get_val("quality_min_gain_mb", 200) or 0)
        self._local_resource_path = str(get_val("local_resource_path", "") or "").strip()
        # 媒体库一级目录：在网盘媒体库目录/本地媒体根路径下先建 电影/剧集 目录，再放二级分类
        self._type_folder_enabled = bool(get_val("type_folder_enabled", False))
        self._type_folder_movie = (
                str(get_val("type_folder_movie", "电影") or "").strip().strip("/") or "电影"
        )
        self._type_folder_tv = (
                str(get_val("type_folder_tv", "剧集") or "").strip().strip("/") or "剧集"
        )


    def _init_cloud_paths(self, cloud_transfer_path, cloud_media_root, cloud_transfer_paths, cloud_media_paths):
        """Phase 5 第 3 项（v1.20.0）：网盘路径：各盘转存/媒体库路径映射、当前盘路径与默认值回填。原 __init__ 第 396-434 行原样搬移，行为不变。"""
        paths = cloud_transfer_paths if cloud_transfer_paths is not None else self.transfer_ops._configured_transfer_paths(self._plugin)
        self._cloud_transfer_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(paths or {}).items()
            if str(key).strip()
        }

        media_paths = cloud_media_paths if cloud_media_paths is not None else self.transfer_ops._configured_media_paths(self._plugin)
        self._cloud_media_paths = {
            str(key).strip().lower(): self._normalize_cloud_path(value)
            for key, value in dict(media_paths or {}).items()
            if str(key).strip()
        }

        # 优先从各网盘专属路径映射中取当前网盘路径，兼容显式参数传入
        drive_key = getattr(self._cloud_drive, "key", "") if self._cloud_drive else ""
        raw_transfer_path = (
                cloud_transfer_path
                or (self._cloud_transfer_paths.get(drive_key) if drive_key else None)
                or "/"
        )
        self._cloud_transfer_path = (
                str(raw_transfer_path or "/").strip().rstrip("/") or "/"
        )
        raw_media_root = (
                cloud_media_root
                or (self._cloud_media_paths.get(drive_key) if drive_key else None)
                or "/"
        )
        self._CLOUD_MEDIA_ROOT = self._normalize_cloud_path(raw_media_root)

        if self._cloud_drive:
            self._cloud_transfer_paths.setdefault(
                self._cloud_drive.key, self._cloud_transfer_path
            )
            self._cloud_media_paths.setdefault(
                self._cloud_drive.key, self._CLOUD_MEDIA_ROOT
            )


    def _init_strm_media_server(self, get_val):
        """Phase 5 第 3 项（v1.20.0）：STRM 与媒体服务器：STRM 生成器、媒体服务器通知器与解析器。原 __init__ 第 435-518 行原样搬移，行为不变。"""
        self._strm_generate_enabled = bool(get_val("strm_generate_enabled", True))
        self._video_extensions = list(get_val("video_extensions", [".mkv", ".mp4", ".avi", ".iso"]))
        self._nfo_scrape_enabled = bool(get_val("nfo_scrape_enabled", False))
        self._image_scrape_enabled = bool(get_val("image_scrape_enabled", False))
        self._metadata_scraper = (
            MediaScraper(
                nfo_enabled=self._nfo_scrape_enabled,
                image_enabled=self._image_scrape_enabled,
            )
            if self._nfo_scrape_enabled or self._image_scrape_enabled
            else None
        )
        if self._metadata_scraper:
            enabled_types = "、".join(
                name for enabled, name in (
                    (self._nfo_scrape_enabled, "NFO"),
                    (self._image_scrape_enabled, "图片"),
                ) if enabled
            )
            if self._local_resource_path:
                logger.info(
                    f"元数据刮削已启用：{enabled_types}，"
                    f"本地资源目录={self._local_resource_path}"
                )
            else:
                logger.warning(
                    f"元数据刮削已启用：{enabled_types}，但未配置本地资源目录，"
                    "无法生成 NFO 或图片"
                )
        self._path_mapper = StrmGenerator(
            StrmGenerator.DEFAULT_BASE_URL, StrmGenerator.DEFAULT_TEMPLATE
        )
        self._strm_generator = None
        if self._strm_generate_enabled:
            if not self._playback_reference:
                logger.error(
                    "当前网盘不支持播放引用，已停止直接生成 STRM"
                )
                self._strm_generate_enabled = False
            else:
                s_base_url = str(
                    get_val("strm_base_url", StrmGenerator.DEFAULT_BASE_URL) or ""
                ).strip()
                s_template = str(
                    get_val("strm_url_template", StrmGenerator.DEFAULT_TEMPLATE) or ""
                ).strip()
                if not s_base_url:
                    logger.warning(
                        "已开启「转存后生成 STRM」，但未填写播放地址前缀（STRM base_url），"
                        "本次不生成 STRM；请到「STRM 与播放」填 http://<主机>:<端口>"
                    )
                    self._strm_generate_enabled = False
                else:
                    try:
                        self._strm_generator = StrmGenerator(
                            s_base_url,
                            s_template,
                            provider_variables=self._playback_reference.template_variables,
                        )
                    except StrmTemplateError as error:
                        logger.error(f"STRM 生成配置无效，已停止直接生成：{error}")

        media_server_refresh_enabled = bool(get_val("media_server_refresh_enabled", False))
        media_servers = get_val("media_servers", None)
        media_server_path_mappings = get_val("media_server_path_mappings", "")
        media_server_refresh_delay = get_val("media_server_refresh_delay", 0)
        emby_mediainfo_enabled = bool(get_val("emby_mediainfo_enabled", False))

        self._media_server_path_mappings = str(media_server_path_mappings or "")
        self._media_server_notifier = MediaServerNotifier(
            enabled=media_server_refresh_enabled,
            mediaservers=media_servers,
            path_mappings=media_server_path_mappings,
            delay_seconds=media_server_refresh_delay,
            emby_mediainfo_enabled=emby_mediainfo_enabled,
        )
        self._notification_delay_seconds = max(
            0, int(media_server_refresh_delay or 0)
        )
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None
        self._media_server_resolver = MediaServerResolver()
        MediaServerResolver.configure(media_servers)


    def _init_locks_caches(self):
        """Phase 5 第 3 项（v1.20.0）：锁与缓存：各并发锁与平台 TTL 缓存（识别/季目录/根目录/延期/基线等）。原 __init__ 第 519-570 行原样搬移，行为不变。"""
        self._offline_pending_lock = threading.RLock()
        self._pt_upgrade_lock = threading.RLock()
        self._pt_upgrade_active = set()
        self._platform_history_lock = threading.RLock()
        self._sync_metrics_lock = threading.RLock()
        self._sync_metrics: Dict[str, Dict[str, int]] = {}
        self._media_recognition_lock = threading.RLock()
        self._platform_media_recognition_lock = threading.Lock()
        self._media_recognition_cache = create_platform_ttl_cache(
            "sync:media_recognition", self,
            maxsize=self._MEDIA_RECOGNITION_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._media_recognition_inflight: Dict[Tuple[Any, ...], Future] = {}
        self._resource_season_dir_lock = threading.RLock()
        self._resource_season_dir_cache = create_platform_ttl_cache(
            "sync:resource_season_dirs", self,
            maxsize=self._RESOURCE_SEASON_DIR_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._platform_root_lock = threading.RLock()
        self._platform_root_cache = create_platform_ttl_cache(
            "sync:platform_roots", self,
            maxsize=self._PLATFORM_ROOT_CACHE_LIMIT,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._subscribe_defer_lock = threading.RLock()
        self._subscribe_defer_cache = create_platform_ttl_cache(
            "sync:subscribe_defer", self,
            maxsize=self._SUBSCRIBE_DEFER_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_DEFER_CACHE_TTL,
        )
        self._subscribe_calendar_cache = create_platform_ttl_cache(
            "sync:subscribe_calendar", self,
            maxsize=self._SUBSCRIBE_CALENDAR_CACHE_LIMIT,
            ttl=self._SUBSCRIBE_CALENDAR_CACHE_TTL,
        )
        self._baseline_cache_lock = threading.RLock()
        self._baseline_transfer_cache = create_platform_ttl_cache(
            "sync:baseline_transfer", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        self._baseline_media_server_cache = create_platform_ttl_cache(
            "sync:baseline_media_server", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )
        # 目标季目录列目录结果：判重与「历史记录存在性校验」共用（1.6.51）
        self._season_listing_cache = create_platform_ttl_cache(
            "sync:season_listing", self, maxsize=256,
            ttl=self._RUNTIME_CACHE_TTL,
        )


    # -- 对外 API：显式转发（Phase 5，v1.19.0）--
    # 插件宿主（__init__.py）与 core/api、core/hook 经 self._sync_handler
    # 调用的 35 个组件方法：过去靠 SyncHandler.__getattr__ 隐式解析，
    # 现在是 SyncHandler 上的真实方法，内部转发到具名服务字段；
    # 调用方与测试一律不用改。签名与组件方法一致。
    # v1.26.1 教训：v1.19.0 只补了 28 个，漏了 get_due_offline_task_groups
    # 与 6 个 STRM/媒体方法——前者无守卫，每轮监控直接崩（下载页 95% 卡
    # 自此攒起）；后者有 hasattr 守卫，悄悄返回“同步服务未就绪”（STRM
    # 增量/全量补齐自此停摆）。新增转发务必同步补测试
    # test_sync_handler_forwarder_coverage_1_26_1.py 的调用点扫描。

    # -- 单订阅未转存原因（v1.27.1，唯一来源）--
    # 写入方：本处理器的组件经 ``self._owner._last_skip_reason = ...`` 写
    # （television / movie / subscribe_flow），落到这个属性；读取方：运行态
    # 服务 ``_run_subscription_group`` 经 ``last_skip_reason()`` 读，随组结果
    # 交给同步执行层。v1.27.0 及以前读的是插件实例上的同名属性（每组开头
    # 清空、组件从不写那里），读出来恒为空，频道监控只能退回「本轮无新集」。

    def _skip_reason_store(self) -> threading.local:
        store = self.__dict__.get("_skip_reason_local")
        if store is None:
            # 测试里经 __new__ 构造、未跑 __init__ 时按需补上。
            store = threading.local()
            self.__dict__["_skip_reason_local"] = store
        return store

    @property
    def _last_skip_reason(self) -> str:
        return str(getattr(self._skip_reason_store(), "value", "") or "")

    @_last_skip_reason.setter
    def _last_skip_reason(self, value: Any) -> None:
        self._skip_reason_store().value = str(value or "")

    def last_skip_reason(self) -> str:
        """当前线程最近一次单订阅处理「未转存」的真实原因。"""
        return self._last_skip_reason.strip()

    def _build_episode_baseline(self, subscribe, mediainfo: MediaInfo, season: int, include_saved: bool=True, score_cache: Optional[Dict[tuple, int]]=None) -> Dict[int, Dict[str, Any]]:
        return self.upgrade_baseline._build_episode_baseline(subscribe, mediainfo, season, include_saved, score_cache)

    def _read_ep_priority(self, subscribe) -> dict:
        return self.upgrade_rule_scoring._read_ep_priority(subscribe)

    def _recognize_media_once(self, key: Tuple[Any, ...], **kwargs: Any):
        return self.metadata._recognize_media_once(key, **kwargs)

    def _self_heal_cleanup(self):
        return self.upgrade._self_heal_cleanup()

    def _subscribe_mediainfo(self, subscribe: Any, media_type: MediaType, *, cache: bool=True) -> Optional[MediaInfo]:
        return self.metadata._subscribe_mediainfo(subscribe, media_type, cache=cache)

    def auto_upgrade_scan(self):
        return self.upgrade.auto_upgrade_scan()

    def begin_notification_batch(self) -> bool:
        return self.media_refresh.begin_notification_batch()

    def build_transient_media_target(self, media: Dict[str, Any], target_id: int=-1, episodes: Optional[Set[int]]=None, manual_upgrade: bool=False, manual_baseline: Optional[Dict[str, Any]]=None) -> Any:
        return self.media_targets.build_transient_media_target(media, target_id, episodes, manual_upgrade, manual_baseline)

    def clean_share_strm(self, share_url: str, save_path: str = "") -> Dict[str, Any]:
        return self.strm_ops.clean_share_strm(share_url, save_path=save_path)

    def delete_by_media_server_paths(self, paths: List[str]) -> Dict[str, int]:
        return self.history_cleanup.delete_by_media_server_paths(paths)

    def delete_pending_finalize_tasks(self, pending_keys: Set[str], task_ids: Optional[Set[str]]=None) -> int:
        return self.postprocess.delete_pending_finalize_tasks(pending_keys, task_ids)

    def finish_notification_batch(self) -> bool:
        return self.media_refresh.finish_notification_batch()

    def generate_share_strm(self, share_url: str, save_path: str = "", overwrite: bool = False) -> Dict[str, Any]:
        return self.strm_ops.generate_share_strm(share_url, save_path=save_path, overwrite=overwrite)

    def get_due_offline_task_groups(self, force: bool = False, pending_keys: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
        return self.postprocess.get_due_offline_task_groups(force=force, pending_keys=pending_keys)

    def get_pending_finalize_tasks(self) -> List[Dict[str, Any]]:
        return self.postprocess.get_pending_finalize_tasks()

    def get_subscribe_defer(self, subscribe: Any) -> Optional[Dict[str, str]]:
        return self.metadata.get_subscribe_defer(subscribe)

    def is_offline_task_finalized(self, info_hash: str) -> bool:
        return self.transfer_details.is_offline_task_finalized(info_hash)

    def list_media_server_content(self, server: str='', keyword: str='', tmdb_id: Any=None, media_type: str='', limit: int=500) -> Dict[str, Any]:
        return self.media_targets.list_media_server_content(server, keyword, tmdb_id, media_type, limit)

    def migrate_legacy_strm(self, root: str = "", dry_run: bool = False) -> Dict[str, Any]:
        return self.strm_ops.migrate_legacy_strm(root=root, dry_run=dry_run)

    def monitor_offline_strm_tasks(self, force: bool=False, pending_keys: Optional[Set[str]]=None, offline_tasks: Optional[List[Dict[str, Any]]]=None, offline_tasks_valid: Optional[bool]=None) -> Dict[str, int]:
        return self.postprocess.monitor_offline_strm_tasks(force, pending_keys, offline_tasks, offline_tasks_valid)

    def preview_resource_files(self, share_url: str) -> List[Dict[str, Any]]:
        return self.resource_transfer.preview_resource_files(share_url)

    def process_movie_subscribe(self, subscribe, history: List[dict], transfer_details: List[Dict[str, Any]], transferred_count: int, manual_resources: Optional[List[Dict[str, Any]]]=None, manual_upgrade: bool=False, transient_target: bool=False) -> int:
        return self.movie.process_movie_subscribe(subscribe, history, transfer_details, transferred_count, manual_resources, manual_upgrade, transient_target)

    def process_pt_upgrade(self, event_data: Dict[str, Any]) -> bool:
        return self.pt_upgrade.process_pt_upgrade(event_data)

    def process_tv_subscribe(self, subscribe, history: List[dict], transfer_details: List[Dict[str, Any]], transferred_count: int, manual_resources: Optional[List[Dict[str, Any]]]=None, allow_upgrade: bool=True, manual_upgrade: bool=False, target_episodes: Optional[Set[int]]=None, transient_target: bool=False) -> int:
        return self.television.process_tv_subscribe(subscribe, history, transfer_details, transferred_count, manual_resources, allow_upgrade, manual_upgrade, target_episodes, transient_target)

    def rebuild_strm_files(self, overwrite: bool = False, time_budget_seconds: float = 0.0, state_store: Any = None) -> Dict[str, int]:
        return self.strm_ops.rebuild_strm_files(overwrite=overwrite, time_budget_seconds=time_budget_seconds, state_store=state_store)

    def rebuild_strm_increment_by_tree(self, timeout_seconds: float = 600.0) -> Dict[str, int]:
        return self.strm_ops.rebuild_strm_increment_by_tree(timeout_seconds=timeout_seconds)

    def refresh_media_servers(self) -> Dict[str, Any]:
        return self.media_refresh.refresh_media_servers()

    def remove_platform_transfer_history_mirror(self) -> int:
        return self.platform_history.remove_platform_transfer_history_mirror()

    def repair_subscribe_tmdb_id(self, subscribe: Any) -> bool:
        return self.metadata.repair_subscribe_tmdb_id(subscribe)

    def resolve_media_server_upgrade_targets(self, selections: List[Dict[str, Any]]) -> List[Any]:
        return self.media_targets.resolve_media_server_upgrade_targets(selections)

    def send_transfer_notification(self, transfer_details: List[Dict[str, Any]], total_count: int) -> None:
        return self.notifications.send_transfer_notification(transfer_details, total_count)

    def send_transfer_completed_notification(self, transfer_details: List[Dict[str, Any]], total_count: int = 0) -> None:
        return self.notifications.send_transfer_completed_notification(transfer_details, total_count)

    def stop_pending_finalize_tasks(self, pending_keys: Set[str]) -> int:
        return self.postprocess.stop_pending_finalize_tasks(pending_keys)

    def submit_history_retry(self, record_time: str, share_url: str, file_name: str) -> Dict[str, Any]:
        return self.history_retry.submit_history_retry(record_time, share_url, file_name)

    def submit_history_retry_batch(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.history_retry.submit_history_retry_batch(records)

    def update_notification_config(self, notify: bool, notification_type: NotificationType, media_server_refresh_enabled: bool, media_servers: List[str], media_server_path_mappings: str, media_server_refresh_delay: int, emby_mediainfo_enabled: bool) -> None:
        return self.media_refresh.update_notification_config(notify, notification_type, media_server_refresh_enabled, media_servers, media_server_path_mappings, media_server_refresh_delay, emby_mediainfo_enabled)

    def persist_sync_results(
            self,
            records: List[Dict[str, Any]],
    ) -> int:
        """本轮同步结果落盘（v1.13.3 §1.4）：只把失败（可重试）与跨盘成功
        待修复行写进重试索引；同盘成功行不写（MP 整理历史是唯一载体）。
        返回写入行数，写入后通知前端刷新。

        Phase 4 第 1 项：本方法是重试索引终态写入的三处之一（另两处是
        finalize 终态 _write_terminal_state 与重试执行 retry.py）。
        """
        count = 0
        get_store = getattr(self, "_get_data_store", None)
        if not callable(get_store):
            return 0
        try:
            store = get_store()
        except Exception as error:  # noqa: BLE001 - 存储不可用时跳过落盘
            logger.debug(f"重试索引存储不可用，同步结果未落盘：{error}", exc_info=True)
            return 0
        try:
            from ...core.retry_index import (
                record_to_index_row,
                retry_index_status,
            )
        except Exception as error:  # noqa: BLE001 - 索引模块不可用时跳过落盘
            logger.debug(f"重试索引模块不可用，同步结果未落盘：{error}", exc_info=True)
            return 0
        for record in records or []:
            if not isinstance(record, dict):
                continue
            try:
                status = retry_index_status(record)
            except Exception:
                continue
            if status is None:
                continue
            try:
                row = record_to_index_row(record, status)
            except Exception:
                continue
            if row is None:
                continue
            try:
                if store.repositories.retry_index.upsert_row(row):
                    count += 1
            except Exception as error:  # noqa: BLE001 - 单行写失败不阻塞整批
                logger.debug(f"同步结果写重试索引失败：{error}", exc_info=True)
        if count and self._history_changed:
            self._history_changed()
        return count

    def _is_cloud_upgrade_subscribe(self, subscribe: Any) -> bool:
        """判断订阅是否属于插件网盘洗版范围（委托服务层唯一实现）。"""
        # 懒加载：顶层 import 会把 core.services 包的 __init__ 拉进来，
        # 单测里 core.services 常是只有部分属性的桩，import 即炸；
        # 方法调用时 sys.modules 里已有真实模块。
        from ...core.services.subscription import is_cloud_upgrade_subscribe

        return is_cloud_upgrade_subscribe(
            self._enable_cloud_upgrade,
            self._upgrade_subscribe_id_set,
            subscribe,
        )

    @staticmethod
    def subscription_budget_key(
            subscribe: Any, media_type: Optional[MediaType] = None
    ) -> str:
        """生成普通转存和洗版共用的订阅积分键。"""
        resolved_type = media_type or {
            MediaType.MOVIE.value: MediaType.MOVIE,
            MediaType.TV.value: MediaType.TV,
        }.get(str(getattr(subscribe, "type", "") or ""))
        tmdb_id = str(tmdb_id_of(subscribe) or "")
        identity = (
            f"tmdb_{tmdb_id}"
            if tmdb_id
            else str(getattr(subscribe, "name", "") or "").strip()
        )
        if resolved_type == MediaType.MOVIE:
            return f"{identity}_movie"
        season = max(1, int(getattr(subscribe, "season", 1) or 1))
        return f"{identity}_S{season}"

    def _optional_cloud_service(self, capability: CloudDriveCapability):
        if not self._cloud_drive or not self._cloud_drive.supports(capability):
            return None
        return self._cloud_drive.require(capability)

    def clear_runtime_cache(self) -> Dict[str, int]:
        """清空同步过程中可重建的计算缓存。"""
        with self._media_recognition_lock:
            media_recognition = len(self._media_recognition_cache)
            self._media_recognition_cache.clear()
        with self._resource_season_dir_lock:
            resource_season_dirs = len(self._resource_season_dir_cache)
            self._resource_season_dir_cache.clear()
        with self._platform_root_lock:
            platform_roots = len(self._platform_root_cache)
            self._platform_root_cache.clear()
        with self._subscribe_defer_lock:
            subscribe_defer = len(self._subscribe_defer_cache)
            subscribe_calendar = len(self._subscribe_calendar_cache)
            self._subscribe_defer_cache.clear()
            self._subscribe_calendar_cache.clear()
        with self._baseline_cache_lock:
            baseline_transfer = len(self._baseline_transfer_cache)
            baseline_media_server = len(self._baseline_media_server_cache)
            season_listing = len(getattr(self, "_season_listing_cache", {}) or {})
            self._baseline_transfer_cache.clear()
            self._baseline_media_server_cache.clear()
            season_listing_cache = getattr(self, "_season_listing_cache", None)
            if season_listing_cache is not None:
                season_listing_cache.clear()
        return {
            "media_recognition": media_recognition,
            "resource_season_dirs": resource_season_dirs,
            "platform_roots": platform_roots,
            "subscribe_defer": subscribe_defer,
            "subscribe_calendar": subscribe_calendar,
            "baseline_transfer": baseline_transfer,
            "baseline_media_server": baseline_media_server,
            "season_listing": season_listing,
        }

    def _sync_paused(self) -> bool:
        """全局暂停开关（3 秒缓存）：暂停期间不认领新作业、不发起新转存。"""
        now = time.time()
        cached = getattr(self, "_sync_pause_cache", None)
        try:
            if cached and (now - float(cached[0])) < 3:
                return bool(cached[1])
        except (TypeError, ValueError, IndexError):
            cached = None
        try:
            from ...core import sync_pause  # 延迟导入，避免拉起仓储层依赖

            repository = None
            provider = getattr(self._plugin, "_sync_pause_repository", None)
            if callable(provider):
                repository = provider()
            paused = sync_pause.is_paused(repository) is True
        except Exception as error:
            logger.warning(f"读取转存暂停开关失败，按未暂停处理：{error}")
            paused = False
        self._sync_pause_cache = (now, paused)
        return paused

    def reset_sync_metrics(self) -> None:
        with self._sync_metrics_lock:
            self._sync_metrics = {}
        self.clear_runtime_cache()

    def _record_sync_metric(self, name: str, elapsed_ms: int) -> None:
        with self._sync_metrics_lock:
            metric = self._sync_metrics.setdefault(
                name, {"calls": 0, "elapsed_ms": 0}
            )
            metric["calls"] += 1
            metric["elapsed_ms"] += max(0, int(elapsed_ms or 0))

    def _timed_sync_call(self, name: str, func: Callable, *args, **kwargs):
        started = time.monotonic()
        try:
            return func(*args, **kwargs)
        finally:
            self._record_sync_metric(
                name, int((time.monotonic() - started) * 1000)
            )

    def get_sync_metrics(self) -> Dict[str, Dict[str, int]]:
        with self._sync_metrics_lock:
            return copy.deepcopy(self._sync_metrics)

    def _is_offline_url(self, url: str) -> bool:
        return bool(
            self._offline_download
            and self._offline_download.is_offline_url(url)
        )

    def _is_ed2k_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_ed2k_url(url)
        )

    def _is_magnet_url(self, url: str) -> bool:
        return bool(
            self._offline_download and self._offline_download.is_magnet_url(url)
        )

    def close(self) -> None:
        """提交尚未发送的完成通知并释放通知定时器。"""
        self.notifications._flush_transfer_notifications()
        self._media_server_notifier.close(flush=True)


    def _stop_requested(self) -> bool:
        if hasattr(self, "_active_task_context") and self._active_task_context:
            if getattr(self._active_task_context, "is_cancelled", False):
                return True
        try:
            return bool(self._should_stop and self._should_stop())
        except Exception as err:
            logger.debug(f"读取停止状态失败：{err}")
            return False

    def check_task_alive(self) -> None:
        """检查任务存活状态：若已取消或超时立即退出。"""
        if hasattr(self, "_active_task_context") and self._active_task_context:
            if hasattr(self._active_task_context, "check_alive"):
                self._active_task_context.check_alive()
        if self._stop_requested():
            from ...core.exceptions import TaskAbortedError
            raise TaskAbortedError("任务已停止")

    def _current_task_context(self) -> Tuple[str, Any]:
        if not self._task_context:
            return "", None
        try:
            task_id, stop_event = self._task_context()
            return str(task_id or ""), stop_event
        except Exception as error:
            logger.debug(f"读取当前订阅任务上下文失败：{error}")
            return "", None


    def _scrape_metadata(
            self,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[Path]:
        """在最终分类路径补齐元数据；失败不影响网盘文件处理。"""
        if not self._metadata_scraper or not self._local_resource_path:
            return None
        try:
            mapped_path = self._path_mapper.local_path(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
            )
            media_path = mapped_path.with_suffix(Path(file_name).suffix)
            scrape_items = self._metadata_scraper.filter_missing_items([{
                "media_path": media_path,
                "season": season,
                "episode": episode,
            }], mediainfo)
            if not scrape_items:
                logger.debug(f"跳过元数据刮削：{media_path.parent}，NFO 和图片均已存在")
                return media_path
            logger.info(
                f"开始元数据刮削：{media_path}，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
            if created:
                logger.info(f"元数据刮削完成：{media_path.parent}，新增 {created} 个文件")
            else:
                logger.info(
                    f"元数据刮削完成但无新增文件：{media_path.parent}，"
                    f"目标文件可能已存在或 TMDB 未返回内容"
                )
            return media_path
        except Exception as error:
            logger.warning(f"元数据刮削失败：{file_name}，{error}")
            return None

    def _scrape_metadata_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: Optional[int] = None,
    ) -> None:
        """按一次转存批次刮削，避免重复请求剧根与季元数据。"""
        if not self._metadata_scraper or not self._local_resource_path or not items:
            return
        scrape_items = []
        try:
            for item in items:
                mapped_path = self._path_mapper.local_path(
                    local_root=self._local_resource_path,
                    cloud_root=self._CLOUD_MEDIA_ROOT,
                    cloud_dir=item["cloud_dir"],
                    file_name=item["file_name"],
                )
                episode = next(iter(
                    item.get("notification_episodes")
                    or item.get("success_episodes")
                    or []
                ), None)
                scrape_items.append({
                    "media_path": mapped_path.with_suffix(Path(item["file_name"]).suffix),
                    "season": season,
                    "episode": episode,
                })
            scrape_items = self._metadata_scraper.filter_missing_items(
                scrape_items, mediainfo
            )
            if not scrape_items:
                logger.debug(
                    f"跳过批量元数据刮削：{mediainfo.title_year}，"
                    "NFO 和图片均已存在"
                )
                return
            logger.debug(
                f"开始批量元数据刮削：{mediainfo.title_year}，"
                f"{len(scrape_items)} 个媒体文件，"
                f"NFO={'是' if self._nfo_scrape_enabled else '否'}，"
                f"图片={'是' if self._image_scrape_enabled else '否'}"
            )
            created = self._metadata_scraper.scrape_batch(scrape_items, mediainfo)
            if created:
                logger.debug(
                    f"批量元数据刮削完成：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，新增 {created} 个文件"
                )
            else:
                logger.debug(
                    f"批量元数据刮削完成但无新增文件：{mediainfo.title_year}，"
                    f"{len(scrape_items)} 个媒体文件，"
                    "目标文件可能已存在或 TMDB 未返回内容"
                )
        except Exception as error:
            logger.warning(f"批量元数据刮削失败：{error}")

    @staticmethod
    def _normalize_offline_key(key_or_url: str) -> str:
        """将离线任务/资源链接规范化为全局唯一指纹键，确保黑名单与排重的准确性和唯一性。"""
        text = str(key_or_url or "").strip()
        if not text:
            return ""
        # 1. 磁力链接：提取 xt 中的 info_hash，剔除动态 tracker/dn 参数变体，生成规范化唯一键
        if text.lower().startswith("magnet:?"):
            match = re.search(r"xt=urn:btih:([0-9a-fA-F]{40}|[2-7a-zA-Z]{32})", text, re.I)
            if match:
                return f"magnet:{match.group(1).upper()}"
        # 2. ED2K 链接：提取 32 位文件哈希
        match_ed2k = re.search(r"ed2k://\|file\|[^|]+\|(\d+)\|([0-9a-fA-F]{32})", text, re.I)
        if match_ed2k:
            return f"ed2k:{match_ed2k.group(2).upper()}"
        # 3. 普通 URL：去除 query 参数与 fragment
        if text.lower().startswith(("http://", "https://", "ftp://")):
            return text.split("#")[0].strip().lower()
        # 4. 其他原生任务 ID 或文本
        return text.upper()

    def _resource_log_reference(self, share_url: str) -> str:
        """格式化资源引用日志，输出规范化唯一键，避免输出过长 Tracker 参数。"""
        url = str(share_url or "").strip()
        norm = self._normalize_offline_key(url)
        return norm or (url.split("&")[0] if self._is_magnet_url(url) else url)

    def _add_offline_blacklist(self, key_or_url: str, reason: str = "") -> None:
        """将失败或超时的离线任务/资源以规范化唯一键加入黑名单（1天TTL）。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        norm_key = self._normalize_offline_key(key_or_url)
        if not norm_key:
            return
        raw_key = str(key_or_url).strip()
        now = time.time()
        for k in {norm_key, raw_key}:
            if k:
                self._offline_blacklist[k] = {"reason": reason, "time": now}
        logger.info(f"🚫 离线资源已加入黑名单（1天过期）：{norm_key}，原因：{reason}")

    def _remove_offline_blacklist(self, key_or_url: str) -> None:
        """从黑名单中移除指定的离线任务。"""
        if not key_or_url or not hasattr(self, "_offline_blacklist"):
            return
        norm_key = self._normalize_offline_key(key_or_url)
        raw_key = str(key_or_url).strip()
        for k in (norm_key, raw_key):
            if k:
                self._offline_blacklist.pop(k, None)

    def _is_offline_blacklisted(self, resource: Optional[Dict[str, Any]] = None, share_url: str = "") -> bool:
        """按规范化唯一键检查离线资源是否在黑名单中，彻底杜绝参数变体漏判。"""
        if not hasattr(self, "_offline_blacklist"):
            return False
        # 1. 检查 share_url
        if share_url:
            norm_key = self._normalize_offline_key(share_url)
            if norm_key in self._offline_blacklist or str(share_url).strip() in self._offline_blacklist:
                return True
        # 2. 检查 resource 中的链接与任务 ID
        if resource:
            for k in ("url", "link", "share_url"):
                val = str(resource.get(k) or "").strip()
                if val:
                    norm_key = self._normalize_offline_key(val)
                    if norm_key in self._offline_blacklist or val in self._offline_blacklist:
                        return True
            task_id = str(resource.get("task_id") or "").strip()
            if task_id and (task_id in self._offline_blacklist or task_id.upper() in self._offline_blacklist):
                return True
        return False

    @classmethod
    def _is_same_media_target(
            cls,
            item: Dict[str, Any],
            mediainfo: Optional[Any] = None,
            subscribe: Optional[Any] = None,
            subscribe_id: Optional[Any] = None,
    ) -> bool:
        """多维度判断 pending 中的任务与当前候选是否属于同一媒体。"""
        # 1. 优先比对权威 TMDB ID（若双方都有，不一致则绝对不是同一媒体，一致则判定为同一媒体）
        item_tmdb_id = (
                (item.get("mediainfo") or {}).get("tmdb_id")
                or (item.get("target_subscribe") or {}).get("tmdbid")
                or tmdb_id_of(item.get("target_subscribe"))
        )
        target_tmdb_id = (
                getattr(mediainfo, "tmdb_id", None)
                or (tmdb_id_of(subscribe) if subscribe else None)
                or (mediainfo.get("tmdb_id") if isinstance(mediainfo, dict) else None)
        )
        try:
            if item_tmdb_id and target_tmdb_id and int(item_tmdb_id) > 0 and int(target_tmdb_id) > 0:
                if int(item_tmdb_id) != int(target_tmdb_id):
                    return False
                return True
        except (ValueError, TypeError):
            logger.debug("_is_same_media_target 分支忽略异常", exc_info=True)

        # 2. 比对豆瓣 ID
        item_douban_id = (
                (item.get("mediainfo") or {}).get("douban_id")
                or (item.get("target_subscribe") or {}).get("doubanid")
        )
        target_douban_id = (
                getattr(mediainfo, "douban_id", None)
                or (legacy_media_ids(subscribe).get("doubanid") if subscribe else None)
                or (mediainfo.get("douban_id") if isinstance(mediainfo, dict) else None)
        )
        if item_douban_id and target_douban_id:
            s_item_douban = str(item_douban_id).strip()
            s_target_douban = str(target_douban_id).strip()
            if s_item_douban and s_target_douban:
                if s_item_douban != s_target_douban:
                    return False
                return True

        # 3. 比对 subscribe_id
        target_sid = str(subscribe_id or (getattr(subscribe, "id", None) if subscribe else "") or "").strip()
        item_sid = str(item.get("subscribe_id") or "").strip()
        if target_sid and target_sid != "0" and item_sid and item_sid != "0":
            if target_sid == item_sid:
                return True

        # 4. 提取并比对规范化标题 + 年份
        item_media = item.get("mediainfo") or {}
        item_title = str(
            item_media.get("title") or (item.get("target_subscribe") or {}).get("name") or "").strip().lower()
        target_title = str(getattr(mediainfo, "title", None) or getattr(subscribe, "name", None) or (
            mediainfo.get("title") if isinstance(mediainfo, dict) else "") or "").strip().lower()
        if item_title and target_title and item_title == target_title:
            item_year = str(item_media.get("year") or (item.get("target_subscribe") or {}).get("year") or "").strip()
            target_year = str(getattr(mediainfo, "year", None) or getattr(subscribe, "year", None) or (
                mediainfo.get("year") if isinstance(mediainfo, dict) else "") or "").strip()
            if not item_year or not target_year or item_year == target_year:
                return True

        return False

    @classmethod
    def _unreserved_episodes(
            cls,
            pending: Dict[str, Any],
            subscribe_id: Any = None,
            season: Any = None,
            targets: Iterable[int] = (),
            mediainfo: Optional[Any] = None,
            subscribe: Optional[Any] = None,
    ) -> List[int]:
        """过滤掉当前已有未完成离线任务的目标集数，防止并发重复提交下载。"""
        reserved: Set[int] = set()
        target_season = str(season or "").strip()
        for item in (pending or {}).values():
            item_season = str(item.get("season") or "").strip()
            if item_season == target_season and not item.get("upgrade"):
                if cls._is_same_media_target(item, mediainfo=mediainfo, subscribe=subscribe, subscribe_id=subscribe_id):
                    for value in item.get("target_episodes") or []:
                        try:
                            num = int(value)
                            if num > 0:
                                reserved.add(num)
                        except (ValueError, TypeError):
                            continue
        return sorted(set(targets or []) - reserved)

    def _queue_magnet_package(
            self,
            resource: Dict[str, Any],
            share_url: str,
            subscribe: Any,
            mediainfo: MediaInfo,
            season: Optional[int] = None,
            target_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_baseline: Optional[Dict[str, Any]] = None,
            transient_target: bool = False,
            submit_queue: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        """提交离线下载（Magnet/ED2K/直链离线等）到隔离目录；下载完成后再按真实文件树匹配。"""
        magnet_title = self.resource_transfer._prepare_magnet_resource(resource, share_url)
        metadata = resource.get("magnet_metadata") or {}
        title_seasons = self.resource_transfer._magnet_title_seasons(resource)
        if season is not None and title_seasons and int(season) not in title_seasons:
            logger.debug(
                "离线资源标题预过滤排除，未请求远端内容元数据："
                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                f"目标季数=S{int(season):02d}，标题={magnet_title or share_url}"
            )
            return ""
        if season is None and title_seasons:
            logger.debug(
                "电影离线资源包含剧集季数，预过滤排除："
                f"标题季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                f"标题={magnet_title or share_url}"
            )
            return ""
        title_episodes = self.resource_transfer._magnet_title_episodes(
            resource, int(season or 1)
        )
        if season is None and title_episodes:
            logger.debug(
                "电影离线资源包含剧集集数，预过滤排除："
                f"标题集数={self._format_episode_ranges(title_episodes)}，"
                f"标题={magnet_title or share_url}"
            )
            return ""
        preview_episodes = (
                title_episodes
                or self.resource_transfer._resource_preview_episodes(resource, int(season or 1))
        )
        target_episode_set = {
            int(value) for value in (target_episodes or []) if int(value) > 0
        }
        if season is not None and target_episode_set and title_episodes:
            confirmed_targets = target_episode_set & title_episodes
            if not confirmed_targets:
                logger.debug(
                    "离线资源标题预过滤排除，未请求远端内容元数据："
                    f"标题集数={self._format_episode_ranges(title_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or share_url}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
            logger.debug(
                "离线资源标题预过滤命中，跳过远端内容元数据获取："
                f"标题集数={self._format_episode_ranges(title_episodes)}，"
                f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                f"标题={magnet_title or share_url}"
            )
        if (
                self._is_magnet_url(share_url)
                and not title_episodes
                and not preview_episodes
                and not metadata.get("torrent_files")
        ):
            if season is not None:
                logger.debug(
                    "Magnet 标题未识别明确集数，开始获取远端内容元数据："
                    f"{magnet_title or share_url}"
                )
            else:
                logger.debug(
                    "Magnet 开始获取远端内容元数据："
                    f"{magnet_title or share_url}"
                )
            magnet_info = self._offline_download.parse_magnet_link(
                share_url, fetch_metadata=True
            )
            fetched_metadata = (magnet_info or {}).get("metadata") or {}
            if fetched_metadata:
                metadata = {
                    **metadata,
                    **{
                        key: value for key, value in fetched_metadata.items()
                        if value not in (None, "", [], {})
                    },
                }
                resource["magnet_metadata"] = metadata
                magnet_title = self.resource_transfer._prepare_magnet_resource(resource, share_url)
                title_seasons = self.resource_transfer._magnet_title_seasons(resource)
                if (
                        season is not None
                        and title_seasons
                        and int(season) not in title_seasons
                ):
                    logger.debug(
                        "Magnet 远端内容元数据季数不匹配，已跳过："
                        f"内容季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                        f"目标季数=S{int(season):02d}，标题={magnet_title or share_url}"
                    )
                    return ""
                if season is None and title_seasons:
                    logger.debug(
                        "Magnet 远端内容元数据包含剧集季数，与电影订阅不匹配，已跳过："
                        f"内容季数={','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                        f"标题={magnet_title or share_url}"
                    )
                    return ""
                title_episodes = self.resource_transfer._magnet_title_episodes(
                    resource, int(season or 1)
                )
                metadata_preview = metadata.get("preview_episodes") or {}
                if metadata_preview:
                    resource["preview_episodes"] = metadata_preview
                preview_episodes = (
                        title_episodes
                        or self.resource_transfer._resource_preview_episodes(
                    resource, int(season or 1)
                )
                )
        if (
                not share_url
                or not self._get_data
                or (
                self._is_magnet_url(share_url)
                and (
                        (season is not None and not bool(metadata.get("metadata_available")) and not bool(
                            title_episodes) and not bool(preview_episodes))
                        or (season is None and not bool(metadata.get("metadata_available")) and not bool(magnet_title))
                )
        )
        ):
            logger.debug(
                "离线资源标题和元数据均未提供可确认内容，已跳过："
                f"{magnet_title or share_url}"
            )
            return ""
        if season is not None and target_episodes and preview_episodes:
            target_episode_set = {
                int(value) for value in target_episodes if int(value) > 0
            }
            confirmed_targets = target_episode_set & preview_episodes
            if not confirmed_targets:
                logger.debug(
                    "离线资源内容确认未覆盖目标集数，已跳过网盘离线下载候选："
                    f"内容集数={self._format_episode_ranges(preview_episodes)}，"
                    f"目标集数={self._format_episode_ranges(target_episode_set)}，"
                    f"标题={magnet_title or share_url}"
                )
                return ""
            target_episodes[:] = sorted(confirmed_targets)
        subscribe_id = int(getattr(subscribe, "id", 0) or 0)
        prefix = "magnet" if self._is_magnet_url(share_url) else "ed2k" if self._is_ed2k_url(share_url) else "offline"
        token = extract_magnet_hash(share_url) or hashlib.sha1(share_url.encode("utf-8")).hexdigest()[:16].upper()
        pending_key = f"{prefix}:{token}:{subscribe_id}"
        staging_dir = f"{self._cloud_transfer_path.rstrip('/')}"

        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            existing = pending.get(pending_key)
            if existing and existing.get("status") != "submitting":
                return pending_key
            if season and target_episodes and not upgrade:
                target_episodes[:] = self._unreserved_episodes(
                    pending,
                    subscribe_id=subscribe_id,
                    season=season,
                    targets=target_episodes,
                    mediainfo=mediainfo,
                    subscribe=subscribe,
                )
                if not target_episodes:
                    logger.debug(f"跳过同集重复离线：S{int(season):02d}")
                    return ""

            # 写入占位（In-Flight 状态，防止并发穿透同集提交）
            pending[pending_key] = {
                "pending_key": pending_key,
                "task_type": prefix,
                "status": "submitting",
                "subscribe_id": subscribe_id,
                "season": season,
                "target_episodes": sorted({int(value) for value in (target_episodes or []) if int(value) > 0}),
                "mediainfo": self._serialize_mediainfo(mediainfo),
                "target_subscribe": {
                    "tmdbid": tmdb_id_of(subscribe) if subscribe else None,
                    "name": str(getattr(subscribe, "name", "") or ""),
                } if subscribe else {},
                "created_at": time.time(),
            }
            self._save_offline_pending(pending)

        submit_context = {
            "pending_key": pending_key,
            "prefix": prefix,
            "share_url": share_url,
            "staging_dir": staging_dir,
            "resource": resource,
            "subscribe": subscribe,
            "mediainfo": mediainfo,
            "subscribe_id": subscribe_id,
            "season": season,
            "target_episodes": list(target_episodes or []),
            "sub_key": sub_key,
            "upgrade": upgrade,
            "upgrade_mode": upgrade_mode,
            "upgrade_baseline": upgrade_baseline,
            "transient_target": transient_target,
        }
        if submit_queue is not None:
            submit_queue.append(submit_context)
            return pending_key
        successful = self._submit_offline_packages([submit_context])
        return pending_key if pending_key in successful else ""

    def _complete_offline_submission(self, context: Dict[str, Any]) -> None:
        """将已由网盘接受的离线任务从提交占位转为正式待下载记录。"""
        pending_key = str(context["pending_key"])
        prefix = str(context["prefix"])
        share_url = str(context["share_url"])
        staging_dir = str(context["staging_dir"])
        resource = context["resource"]
        subscribe = context["subscribe"]
        mediainfo = context["mediainfo"]
        subscribe_id = int(context["subscribe_id"] or 0)
        season = context.get("season")
        target_episodes = list(context.get("target_episodes") or [])
        sub_key = str(context.get("sub_key") or "")
        upgrade = bool(context.get("upgrade"))
        upgrade_mode = str(context.get("upgrade_mode") or "")
        upgrade_baseline = context.get("upgrade_baseline") or {}
        transient_target = bool(context.get("transient_target"))
        now = time.time()
        ed2k_file_name = extract_ed2k_filename(share_url)
        if not ed2k_file_name and isinstance(resource.get("file_list"), list) and resource["file_list"]:
            ed2k_file_name = str(resource["file_list"][0]).strip()
        display_name = str(
            ed2k_file_name
            or (resource.get("magnet_metadata") or {}).get("display_name")
            or resource.get("title") or Path(share_url).name or pending_key
        )
        target_dir = ""
        target_name = ""
        if getattr(self, "_organize_after_transfer", True) and prefix != "magnet" and mediainfo:
            try:
                if mediainfo.type == MediaType.TV:
                    target_ep = target_episodes[0] if target_episodes and len(target_episodes) == 1 else None
                    if target_ep is not None and season is not None:
                        target_dir, target_name = self.naming._platform_target(
                            self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                            display_name, int(season), int(target_ep)
                        )
                else:
                    target_dir, target_name = self.naming._platform_target(
                        self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                        display_name
                    )
            except Exception as target_err:
                logger.debug(f"计算离线任务目标路径失败，将保留原始目录：{target_err}", exc_info=True)
        if target_name:
            source_suffix = Path(display_name).suffix
            if source_suffix and not target_name.endswith(source_suffix):
                target_name = f"{Path(target_name).stem}{source_suffix}"

        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            pending[pending_key] = {
                "pending_key": pending_key,
                "task_type": prefix,
                "task_id": str(context.get("task_id") or "").strip() or pending_key,
                "share_url": share_url,
                "staging_dir": staging_dir,
                "cloud_dir": target_dir or staging_dir,
                "file_name": target_name or display_name,
                "staging_name": display_name,
                "episode": target_episodes[0] if target_episodes and len(target_episodes) == 1 else None,
                "created_at": now,
                # v1.26.0：首次出现时间；重复提交同一 pending_key 时保留旧值，
                # 不跟着整条记录被覆盖而重置（绝对过期的时钟，见 _build_pending_record）。
                "first_seen_at": float(
                    (pending.get(pending_key) or {}).get("first_seen_at") or now
                ),
                "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
                "check_index": 0,
                "history_ready": True,
                "mediainfo": self._serialize_mediainfo(mediainfo),
                "subscribe_id": subscribe_id,
                "season": season,
                "target_episodes": sorted({
                    int(value) for value in (target_episodes or []) if int(value) > 0
                }),
                "notification_episodes": sorted({
                    int(value) for value in (target_episodes or []) if int(value) > 0
                }),
                "resource": dict(resource),
                "sub_key": str(sub_key or ""),
                "upgrade": bool(upgrade),
                "upgrade_mode": str(upgrade_mode or self._upgrade_mode),
                "upgrade_baseline": dict(upgrade_baseline or {}),
                "transient_target": bool(transient_target),
                "target_subscribe": (
                    self._serialize_pending_target_subscribe(subscribe)
                    if transient_target else {}
                ),
            }
            self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        logger.info(
            f"离线任务已提交：{pending[pending_key].get('staging_name') or pending[pending_key]['file_name']}"
        )

    def _rollback_offline_submission(self, context: Dict[str, Any]) -> None:
        pending_key = str(context.get("pending_key") or "")
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            if pending_key in pending and pending[pending_key].get("status") == "submitting":
                pending.pop(pending_key, None)
                self._save_offline_pending(pending)
        self._add_offline_blacklist(
            str(context.get("share_url") or ""), "提交离线下载失败"
        )

    def _submit_offline_packages(
            self, contexts: List[Dict[str, Any]]
    ) -> set[str]:
        """统一提交网盘离线任务，逐项完成或回滚占位记录。"""
        contexts = [item for item in contexts if item.get("pending_key")]
        if not contexts:
            return set()
        successful_keys: set[str] = set()

        for index, item in enumerate(contexts):
            if index > 0:
                time.sleep(1.5)
            try:
                res = self._offline_download.add_offline_download(
                    item["share_url"],
                    item["staging_dir"],
                    target_name=item.get("file_name"),
                )
                if res:
                    if isinstance(res, str) and res.lower() != "true":
                        item["task_id"] = res
                    self._complete_offline_submission(item)
                    successful_keys.add(str(item["pending_key"]))
                    # v1.25.0 Issue A：网盘已接受离线下载，转存真正开始，
                    # 补一条开始下载通知（磁力/ED2K 不走入库管线，这里是
                    # 唯一入口；提交失败或回滚的不发）。
                    self._notify_offline_transfer_started(item)
                else:
                    self._rollback_offline_submission(item)
            except Exception as error:
                logger.error(f"提交网盘离线下载接口异常：{error}")
                self._rollback_offline_submission(item)

        return successful_keys

    def _notify_offline_transfer_started(self, item: Dict[str, Any]) -> None:
        """离线任务被网盘接受后补开始下载通知（v1.25.0 Issue A）。

        磁力/ED2K 不走入库管线，``_submit_offline_packages`` 是唯一入口。
        来源随订阅对象下沉（``_do_sync`` 挂的 ``_transfer_source``，
        PT 洗版等非 _do_sync 流程默认按订阅算）。
        """
        notifications = getattr(self, "notifications", None)
        send_started = getattr(
            notifications, "send_transfer_started_notification", None
        )
        if not callable(send_started):
            return
        subscribe = item.get("subscribe")
        season = item.get("season")
        send_started(
            mediainfo=item.get("mediainfo"),
            subscribe=subscribe,
            source=str(getattr(subscribe, "_transfer_source", "") or "subscribe"),
            via=str(getattr(subscribe, "_discovered_via", "") or ""),
            media_type="tv" if season is not None else "movie",
            season=season,
            episodes=list(item.get("target_episodes") or []),
        )

    @staticmethod
    def _serialize_mediainfo(mediainfo: MediaInfo) -> Dict[str, Any]:
        if not mediainfo:
            return {}
        try:
            if hasattr(mediainfo, "to_dict"):
                return mediainfo.to_dict()
            if hasattr(mediainfo, "model_dump"):
                return mediainfo.model_dump(mode="json")
            if hasattr(mediainfo, "dict"):
                return mediainfo.dict()
        except Exception as error:
            logger.debug(f"序列化媒体信息失败，将仅生成 STRM：{error}", exc_info=True)
        return {}

    @staticmethod
    def _deserialize_mediainfo(media_data: Dict[str, Any]) -> Optional[MediaInfo]:
        if not media_data:
            return None
        mediainfo = MediaInfo()
        if hasattr(mediainfo, "from_dict"):
            mediainfo.from_dict(dict(media_data))
            return mediainfo
        return MediaInfo(**media_data)

    def _save_offline_pending(self, pending: Dict[str, Dict[str, Any]]) -> None:
        if self._save_data:
            self._save_data(self._OFFLINE_PENDING_KEY, pending)

    def _notify_offline_pending_changed(self, pending_count: int) -> None:
        try:
            if self._offline_pending_changed:
                self._offline_pending_changed(max(0, int(pending_count or 0)))
        except Exception as error:
            logger.debug(f"更新网盘文件后处理监控状态失败：{error}", exc_info=True)

    @staticmethod
    def _finalize_source_identity(
            source_sha1: str, staging_dir: str, staging_name: str,
            file_size: int,
    ) -> Tuple[str, str]:
        source_hash = re.sub(
            r"[^0-9A-Fa-f]", "", str(source_sha1 or "")
        ).upper()
        if len(source_hash) != 40:
            source_hash = ""
        identity = source_hash or hashlib.sha1(
            "\0".join((
                str(staging_dir or "/").rstrip("/") or "/",
                str(staging_name or ""),
                str(max(0, int(file_size or 0))),
            )).encode("utf-8")
        ).hexdigest().upper()
        return source_hash, identity

    @staticmethod
    def _offline_hash(share_url: str) -> str:
        """从磁力或 ed2k 链接中提取特征 hash，非离线链接返回空字符串。"""
        if not share_url:
            return ""
        url = str(share_url).strip()
        lower = url.lower()
        if lower.startswith("magnet:") or "xt=urn:btih:" in lower:
            return (extract_magnet_hash(url) or "").upper()
        if lower.startswith("ed2k://"):
            parts = url.split("|")
            if len(parts) >= 5 and parts[1].lower() == "file" and parts[4]:
                return parts[4].upper()
            return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16].upper()
        return ""

    def _pending_identity(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            source_identity: str,
    ) -> Tuple[str, str, str]:
        """统一计算后处理键，避免单项和批量流程产生不同规则。"""
        info_hash = self._offline_hash(share_url)
        if info_hash:
            return (
                info_hash,
                "magnet" if self._is_magnet_url(share_url) else "ed2k",
                info_hash,
            )
        path_digest = hashlib.sha1(
            f"{str(cloud_dir or '').rstrip('/')}/{file_name}".encode("utf-8")
        ).hexdigest()[:12]
        provider_key = str(
            getattr(self._cloud_drive, "key", "cloud") or "cloud"
        )
        cloud_resource = self.resource_transfer._is_cloud_resource_url(share_url)
        task_type = (
            "cloud"
            if cloud_resource and self.resource_transfer._is_direct_cloud_resource_url(share_url)
            else "cross_cloud"
            if cloud_resource
            else "share"
        )
        return (
            f"{provider_key}:{source_identity}:{path_digest}",
            task_type,
            "",
        )

    @staticmethod
    def _serialize_pending_target_subscribe(subscribe: Any) -> Dict[str, Any]:
        return {
            "name": str(getattr(subscribe, "name", "") or ""),
            "year": getattr(subscribe, "year", None),
            "type": str(getattr(subscribe, "type", "") or ""),
            "tmdbid": tmdb_id_of(subscribe),
            "doubanid": legacy_media_ids(subscribe).get("doubanid"),
            "season": getattr(subscribe, "season", None),
            "start_episode": getattr(subscribe, "start_episode", None),
            "total_episode": getattr(subscribe, "total_episode", None),
            "media_category": getattr(subscribe, "media_category", None),
            "episode_group": getattr(subscribe, "episode_group", None),
            "filter_groups": getattr(subscribe, "filter_groups", None),
            "include": getattr(subscribe, "include", None),
            "exclude": getattr(subscribe, "exclude", None),
            "quality": getattr(subscribe, "quality", None),
            "resolution": getattr(subscribe, "resolution", None),
            "effect": getattr(subscribe, "effect", None),
            "audio_quality": getattr(subscribe, "audio_quality", None),
            "audio_format": getattr(subscribe, "audio_format", None),
            "min_bitrate": getattr(subscribe, "min_bitrate", None),
            "min_bit_depth": getattr(subscribe, "min_bit_depth", None),
            "min_sample_rate": getattr(subscribe, "min_sample_rate", None),
            "custom_words": getattr(subscribe, "custom_words", None),
            "sites": getattr(subscribe, "sites", None),
            "best_version": bool(getattr(subscribe, "best_version", False)),
            "_manual_upgrade": bool(getattr(subscribe, "_manual_upgrade", False)),
        }

    def _build_pending_record(
            self,
            *,
            current: Dict[str, Any],
            pending_key: str,
            task_type: str,
            info_hash: str,
            source_hash: str,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            staging_dir: str,
            staging_name: str,
            file_size: int,
            now: float,
            mediainfo: MediaInfo,
            media_data: Optional[Dict[str, Any]] = None,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            notification_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            skip_history: bool = False,
            mp_organize: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """构造单个后处理记录；单项和批量入口共用同一字段规则。"""
        current = current or {}
        # 已有同名后处理记录时，本次登记即一次全新尝试：清掉上一次的失败计数、
        # 死任务标记与进度时间戳。否则用户手动重试会直接继承 finalize_dead
        # 或已耗尽的定位窗口（普通转存仅 120 秒 / 2 次），一进队列就被判失败。
        fresh_attempt = bool(current)
        is_transient_target = bool(
            transient_target or current.get("transient_target")
        )
        success_values = success_episodes or current.get("success_episodes") or []
        notification_values = (
                notification_episodes
                or current.get("notification_episodes")
                or success_values
                or current.get("success_episodes")
                or []
        )
        pending_task_id = str(
            info_hash
            or current.get("task_id")
            or (
                f"subscribe:{int(subscribe_id)}"
                if subscribe_id
                else f"media:{sub_key}" if sub_key else ""
            )
        )
        record = {
            **current,
            "pending_key": pending_key,
            "task_type": task_type,
            "task_id": pending_task_id,
            "source_sha1": source_hash,
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "file_size": int(file_size or current.get("file_size") or 0),
            "upgrade": bool(upgrade or current.get("upgrade")),
            "upgrade_mode": str(
                upgrade_mode or current.get("upgrade_mode") or self._upgrade_mode
            ),
            "upgrade_old_cloud_dir": str(
                upgrade_old_cloud_dir
                or current.get("upgrade_old_cloud_dir")
                or ""
            ),
            "upgrade_old_file_name": str(
                upgrade_old_file_name
                or current.get("upgrade_old_file_name")
                or ""
            ),
            "upgrade_old_file_id": str(
                upgrade_old_file_id or current.get("upgrade_old_file_id") or ""
            ),
            "upgrade_old_size": int(
                upgrade_old_size or current.get("upgrade_old_size") or 0
            ),
            "created_at": (
                now if fresh_attempt else float(current.get("created_at") or now)
            ),
            # v1.26.0：首次出现时间，重登记时保留旧值、绝不跟着 fresh_attempt
            # 重置；绝对过期的唯一时钟（见 postprocess._expire_overdue_pending_item）。
            # 老记录没有该字段时取 now（本轮首次出现即为 first_seen）。
            "first_seen_at": float(current.get("first_seen_at") or now),
            "download_completed_at": (
                0.0 if fresh_attempt
                else float(current.get("download_completed_at") or 0.0)
            ),
            "moved_at": (
                0.0 if fresh_attempt else float(current.get("moved_at") or 0.0)
            ),
            "next_check_at": now + self._OFFLINE_CHECK_DELAYS[0],
            "check_index": 0,
            "fail_count": 0,
            "finalize_dead": False,
            # v1.13.3 §4.4：后处理登记时就绪标志默认就是 True（终态不再
            # 等历史记录落盘才置位）；宽限期只为照顾更新前已登记的旧项。
            "history_ready": True,
            "skip_history": bool(skip_history or current.get("skip_history")),
            "mediainfo": (
                media_data
                if media_data is not None
                else self._serialize_mediainfo(mediainfo)
            ),
            "subscribe_id": subscribe_id or current.get("subscribe_id"),
            "success_episodes": sorted(
                {
                    int(episode)
                    for episode in success_values
                    if int(episode) > 0
                }
            ),
            "season": (
                max(1, int(season or current.get("season") or 1))
                if getattr(mediainfo, "type", None) == MediaType.TV
                else None
            ),
            "episode": next(iter(notification_episodes or success_episodes or []), None),
            "notification_episodes": sorted(
                {
                    int(episode)
                    for episode in notification_values
                    if int(episode) > 0
                }
            ),
            "sub_key": str(sub_key or current.get("sub_key") or ""),
            "transient_target": is_transient_target,
            "target_subscribe": (
                copy.deepcopy(target_subscribe or current.get("target_subscribe") or {})
                if is_transient_target else {}
            ),
            "subtitles": copy.deepcopy(
                subtitles if subtitles is not None else current.get("subtitles") or []
            ),
        }
        # v1.12.6 C：MP 整理身份跟 pending 记录走，供 finalize 轮询 MP
        # 链；重登记时新身份优先、无新身份时沿用旧身份、无身份时不留字段，
        # 老路径行为一字不改。
        record.pop("mp_organize", None)
        mp_organize_value = mp_organize or current.get("mp_organize")
        if isinstance(mp_organize_value, dict) and mp_organize_value:
            record["mp_organize"] = copy.deepcopy(mp_organize_value)
        return record

    def _queue_file_finalize(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
            mp_organize: Optional[Dict[str, Any]] = None,
    ) -> str:
        item = {
            "result_key": "single",
            "share_url": share_url,
            "cloud_dir": cloud_dir,
            "file_name": file_name,
            "source_sha1": source_sha1,
            "file_size": file_size,
            "staging_dir": staging_dir,
            "staging_name": staging_name,
            "success_episodes": success_episodes,
            "notification_episodes": notification_episodes,
            "upgrade": upgrade,
            "upgrade_mode": upgrade_mode,
            "upgrade_old_cloud_dir": upgrade_old_cloud_dir,
            "upgrade_old_file_name": upgrade_old_file_name,
            "upgrade_old_file_id": upgrade_old_file_id,
            "upgrade_old_size": upgrade_old_size,
            "subtitles": subtitles or [],
            "skip_history": skip_history,
            "mp_organize": mp_organize,
        }
        result = self._queue_file_finalize_batch(
            items=[item],
            mediainfo=mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
        )
        pending_key = result.get("single", "")
        if pending_key:
            info_hash = self._offline_hash(share_url)
            if info_hash:
                logger.debug(f"⏳ 已登记离线完成监控：{file_name}")
            else:
                logger.debug(f"⏳ 文件仍在115系统处理中，已登记重命名与STRM后处理：{file_name}")
        return pending_key


    def _queue_file_finalize_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """一次持久化整批未就绪文件，避免逐项读写插件数据。"""
        if not items or not self._get_data:
            return {}
        now = time.time()
        media_data = self._serialize_mediainfo(mediainfo)
        result: Dict[str, str] = {}
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            organize_enabled = getattr(self, "_organize_after_transfer", True)
            for item in items:
                share_url = item["share_url"]
                staging_dir = str(
                    item.get("staging_dir") or item.get("cloud_dir") or "/"
                ).rstrip("/") or "/"
                staging_name = str(item.get("staging_name") or item.get("file_name") or "")
                if not organize_enabled:
                    cloud_dir = staging_dir
                    file_name = staging_name
                else:
                    cloud_dir = item["cloud_dir"]
                    file_name = item["file_name"]
                source_hash, source_identity = self._finalize_source_identity(
                    item.get("source_sha1") or "",
                    staging_dir,
                    staging_name,
                    item.get("file_size") or 0,
                )
                pending_key, task_type, info_hash = self._pending_identity(
                    share_url, cloud_dir, file_name, source_identity
                )
                current = pending.get(pending_key) or {}
                pending[pending_key] = self._build_pending_record(
                    current=current,
                    pending_key=pending_key,
                    task_type=task_type,
                    info_hash=info_hash,
                    source_hash=source_hash,
                    share_url=share_url,
                    cloud_dir=cloud_dir,
                    file_name=file_name,
                    staging_dir=staging_dir,
                    staging_name=staging_name,
                    file_size=item.get("file_size") or 0,
                    now=now,
                    mediainfo=mediainfo,
                    media_data=media_data,
                    subscribe_id=subscribe_id,
                    success_episodes=item.get("success_episodes"),
                    notification_episodes=item.get("notification_episodes"),
                    season=season,
                    sub_key=sub_key,
                    upgrade=bool(item.get("upgrade")),
                    upgrade_mode=item.get("upgrade_mode") or "",
                    upgrade_old_cloud_dir=item.get("upgrade_old_cloud_dir") or "",
                    upgrade_old_file_name=item.get("upgrade_old_file_name") or "",
                    upgrade_old_file_id=item.get("upgrade_old_file_id") or "",
                    upgrade_old_size=item.get("upgrade_old_size") or 0,
                    subtitles=item.get("subtitles") or [],
                    transient_target=transient_target,
                    target_subscribe=target_subscribe,
                    skip_history=bool(item.get("skip_history")),
                    mp_organize=item.get("mp_organize"),
                )
                result[str(item["result_key"])] = pending_key
            if result:
                self._save_offline_pending(pending)
            pending_count = len(pending)
        self._notify_offline_pending_changed(pending_count)
        return result


    def _finish_pending_subscription(
            self,
            item: Dict[str, Any],
            media_data: Dict[str, Any],
            mediainfo: Optional[MediaInfo] = None,
    ) -> None:
        """文件最终就绪后再更新订阅进度并执行完结。"""
        if item.get("transient_target"):
            return
        task_type = str(item.get("task_type") or "share").strip().lower()
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        completion_source = {
            "share": f"{provider_name}分享转存",
            "cloud": f"{provider_name}路径整理",
            "cross_cloud": f"跨盘转存到{provider_name}后整理",
            "ed2k": "ED2K离线下载",
            "magnet": "Magnet离线下载",
        }.get(task_type, "文件后处理")
        subscribe_id = int(item.get("subscribe_id") or 0)
        episode_values = (
                item.get("success_episodes")
                or item.get("notification_episodes")
                or ([item.get("episode")] if item.get("episode") else [])
        )
        success_episodes = [
            int(episode)
            for episode in episode_values
            if int(episode) > 0
        ]
        if mediainfo is None and media_data:
            try:
                mediainfo = self._deserialize_mediainfo(media_data)
            except Exception as error:
                logger.debug(f"后处理订阅进度媒体信息恢复失败：{error}", exc_info=True)
        if not mediainfo or not success_episodes:
            logger.debug(
                f"跳过后处理订阅进度更新：媒体信息={'有' if mediainfo else '无'}，"
                f"完成集数={success_episodes or '无'}"
            )
            return
        try:
            subscribe = None
            if subscribe_id:
                with SessionFactory() as db:
                    subscribe = SubscribeOper(db=db).get(subscribe_id)
            if not subscribe and mediainfo.tmdb_id:
                season = (
                    max(1, int(item.get("season") or 1))
                    if mediainfo.type == MediaType.TV else None
                )
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), mediainfo.tmdb_id, season
                )
                subscribe = next(
                    (
                        candidate for candidate in candidates
                        if str(getattr(candidate, "type", "")) == mediainfo.type.value
                    ),
                    None,
                )
                if subscribe:
                    subscribe_id = int(subscribe.id)
                    item["subscribe_id"] = subscribe_id
                    logger.info(
                        f"后处理任务已重新关联订阅：{subscribe.name}，"
                        f"订阅ID={subscribe_id}"
                    )
            if not subscribe:
                logger.warning(
                    f"后处理完成时未找到对应订阅：订阅ID={subscribe_id or '无'}，"
                    f"TMDB={mediainfo.tmdb_id}，季={item.get('season') or '-'}"
                )
                return
            self._subscribe_handler.check_and_finish_subscribe(
                subscribe=subscribe,
                mediainfo=mediainfo,
                success_episodes=success_episodes,
            )
            with SessionFactory() as db:
                remaining_subscribe = SubscribeOper(db=db).get(subscribe_id)
            if remaining_subscribe:
                downloaded = {
                    int(episode)
                    for episode in (getattr(remaining_subscribe, "note", None) or [])
                    if str(episode).isdigit()
                }
                total_ep = int(
                    getattr(remaining_subscribe, "total_episode", 0) or 0
                )
                start_ep = int(
                    getattr(remaining_subscribe, "start_episode", 1) or 1
                )
                expected_count = max(0, total_ep - start_ep + 1)
                completed_count = len({
                    episode for episode in downloaded
                    if start_ep <= episode <= total_ep
                }) if expected_count else len(downloaded)
                progress = (
                    int(completed_count * 100 / expected_count)
                    if expected_count else 0
                )
                self.metadata._set_task_phase(
                    remaining_subscribe,
                    f"{completion_source}完成，订阅进度 "
                    f"{completed_count}/{expected_count or '-'}",
                    progress,
                )
                logger.debug(
                    f"{completion_source}完成后订阅进度已更新："
                    f"{remaining_subscribe.name}，"
                    f"已完成 {completed_count}/{expected_count or '-'}，"
                    f"缺失 {int(getattr(remaining_subscribe, 'lack_episode', 0) or 0)} 集"
                )
            else:
                self.metadata._set_task_phase(subscribe, "订阅已完成并移至历史", 100)
                logger.debug(
                    f"{completion_source}完成后订阅已完结并移至历史："
                    f"{subscribe.name}"
                )
            sub_key = str(item.get("sub_key") or "")
            should_clear_points = mediainfo.type == MediaType.MOVIE
            if mediainfo.type == MediaType.TV:
                total_ep = int(getattr(subscribe, "total_episode", 0) or 0)
                start_ep = int(getattr(subscribe, "start_episode", 1) or 1)
                if total_ep >= start_ep:
                    expected = set(range(start_ep, total_ep + 1))
                    downloaded = set(getattr(subscribe, "note", None) or [])
                    downloaded.update(success_episodes)
                    should_clear_points = not (expected - downloaded)
            if (
                    should_clear_points
                    and sub_key
                    and hasattr(self._search_handler, "clear_subscription_budgets")
            ):
                self._search_handler.clear_subscription_budgets(sub_key)
        except Exception as error:
            logger.error(f"文件后处理完成后更新订阅失败：{subscribe_id}，{error}")

    @staticmethod
    def _format_episode_ranges(episodes: Set[int]) -> str:
        return format_episode_ranges(episodes)

    @staticmethod
    def _normalize_cloud_path(path: str) -> str:
        return str(PurePosixPath("/" + str(path or "/").strip().lstrip("/")))


    @staticmethod
    def _reconcile_subscribe_physical_episodes(
            subscribe,
            episodes: Set[int],
            start_episode: int,
            total_episode: int,
    ) -> Dict[str, Any]:
        """以 Emby 与115实际数据纠正订阅进度，包括移除误标集数。"""
        expected = set(range(start_episode, total_episode + 1))
        verified = {int(episode) for episode in episodes} & expected
        current = {
            int(episode) for episode in (subscribe.note or [])
            if str(episode).isdigit()
        }
        new_note = sorted(verified)
        new_lack = len(expected - verified)
        update_data = {}
        if current != verified:
            update_data["note"] = new_note
        if int(subscribe.lack_episode or 0) != new_lack:
            update_data["lack_episode"] = new_lack
        if update_data:
            SubscribeOper().update(subscribe.id, update_data)
            subscribe.note = new_note
            subscribe.lack_episode = new_lack
        return {
            "added": sorted(verified - current),
            "removed": sorted(current - verified),
            "missing": sorted(expected - verified),
            "updated": bool(update_data),
        }

    def guardian_check(self, all_subs) -> int:
        """
        集数守护 & 日历修复：扫描媒体库 strm 文件，同步订阅 note/lack_episode。

        修复场景：
        - PT bypass、115直搜、洗版模式等非标准路径下载后 note 未更新
        - 日历显示"未入库"但文件实际已在媒体库中
        - 订阅进度与实际文件不一致

        :param all_subs: 所有订阅列表（SubscribeOper().list() 结果）
        :return: 本次完成的订阅数（新增的 lack_episode=0 的个数）
        """
        completed_count = 0

        for subscribe in all_subs:
            try:
                # 只处理活跃的电视剧订阅
                if getattr(subscribe, 'state', None) == 'D':
                    continue
                sub_type = getattr(subscribe, 'type', None)
                if sub_type != MediaType.TV.value:
                    continue

                season = subscribe.season or 1
                total_ep = subscribe.total_episode or 0
                start_ep = subscribe.start_episode or 1

                if total_ep <= 0:
                    continue

                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    continue

                found_episodes = self.naming._scan_local_resource_episodes(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=start_ep,
                    total_episode=total_ep,
                )
                if not found_episodes:
                    continue

                remaining_lack = self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=sorted(found_episodes),
                )
                if remaining_lack == 0:
                    completed_count += 1

            except Exception as e:
                logger.debug(f"订阅完结检查异常 {getattr(subscribe, 'name', '?')}：{e}", exc_info=True)
                logger.debug(traceback.format_exc(), exc_info=True)

        return completed_count
