"""Vue 页面需要的配置默认值和选项查询。"""

import datetime
from typing import Any, Dict, List

from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:
    from app.application.mediaserver import MediaServerHelper
from app.log import logger
from app.schemas.types import MediaType

from .config_normalize import clamp_auto_subscribe_dates
from .media import tmdb_id_of
from ..utils.notify_template import DEFAULT_TEMPLATES

DEFAULT_AUTO_SUBSCRIBE_USERNAME = "网盘订阅助手"


#: 并发与冷却集中参数表（1.12.20 Phase 1）：七个值是唯一事实来源。
#: UIConfig.get_default_config() 与各 _apply_*_config 的兜底默认值都从这里取；
#: 数值一个都不许擅自改（改值要同步改这里与线上配置说明）。
#: kind: "risk" = 风控约束（防网盘封控/限流）, "speed" = 速度目标（提速）。
CONCURRENCY_AND_COOLDOWN_PARAMS: Dict[str, Dict[str, Any]] = {
    "cross_transfer_upload_concurrency": {
        "default": 2,
        "kind": "speed",
        "note": (
            "115 上传分片并发度（直传与本地上传共用）；"
            "0=单流顺序（最稳），>0 并发分片，上限 16；"
            "计划 §14：先从 2 起步实测，不要直接拉满"
        ),
    },
    "cross_transfer_upload_breaker_threshold": {
        "default": 5,
        "kind": "risk",
        "note": "115 上传熔断阈值：连续失败几次后打开熔断器",
    },
    "cross_transfer_upload_breaker_cooldown_seconds": {
        "default": 300,
        "kind": "risk",
        "note": "115 上传熔断冷却秒数：打开后多久内新上传直接跳过",
    },
    "cross_transfer_max_concurrent": {
        "default": 2,
        "kind": "speed",
        "note": "同时转存任务数（并行度）；也约束同一时刻对 115 的并发压力",
    },
    "subscription_concurrency": {
        "default": 2,
        "kind": "speed",
        "note": "订阅并发处理数",
    },
    "batch_interval": {
        "default": 3,
        "kind": "risk",
        "note": "批次间隔（秒）：批次之间停顿节流，避免连续请求打满 115",
    },
    "transfer_risk_cooldown": {
        "default": 1800,
        "kind": "risk",
        "note": "风控冷却（秒）：触发风控信号后静默时长",
    },
    "transfer_task_batch_size": {
        "default": 50,
        "kind": "speed",
        "note": "每批处理文件数（吞吐）；过大则单批 API 量大",
    },
    "cross_transfer_daily_upload_limit_gb": {
        "default": 900,
        "kind": "risk",
        "note": "每日真实上传上限（GB）：防 115 限流/封控；秒传命中 0 字节不计数",
    },
}

#: _apply_plugin_config 的 setattr 白名单（1.12.20 Phase 1）：非默认键部分。
#: 完整白名单见文件末尾 PLUGIN_CONFIG_KEYS = 默认键 + 本集合 + 签到后缀规则。
#: 本集合的来源：config.get 读过但不在默认值里的键（各网盘/搜索源凭据键、
#: 搜索源配置键）、normalize 迁移掉的历史键（block/takeover_platform_
#: downloads、unblock_*，normalize 会先删掉它们，进白名单只是兜底）、
#: 签到契约的 credential_keys（只出现在 definition 里，无 config.get 字面量）。
#: 带 "_" 前缀的键来自驱动 definition 对内部属性名的读取，正常配置里不会
#: 出现，列入仅作兜底。
PLUGIN_CONFIG_EXTRA_KEYS: frozenset[str] = frozenset({
    "_alipan_access_token",
    "_alipan_refresh_token",
    "_alipan_request_timeout",
    "_cookies",
    "_cross_transfer_upload_proxy",
    "_guangya_access_token",
    "_guangya_client_id",
    "_guangya_device_id",
    "_guangya_refresh_token",
    "_guangya_request_timeout",
    "_p123_request_timeout",
    "_p123_token",
    "_quark_checkin_url",
    "_quark_cookie",
    "_quark_request_timeout",
    "_search_cache_ttl_minutes",
    "_tianyi_access_token",
    "_tianyi_cookie",
    "_tianyi_refresh_token",
    "_tianyi_request_timeout",
    "_tianyi_session_key",
    "_transfer_risk_cooldown",
    "_yun139_authorization",
    "_yun139_request_timeout",
    "alipan_access_token",
    "alipan_refresh_token",
    "alipan_request_timeout",
    "animegarden_exclude_re",
    "apikey",
    "auto_subscribe_media_type",
    "block_platform_downloads",
    "channel",
    "checkin_auto_retry",
    "checkin_cron",
    "checkin_retry_count",
    "cookies",
    "dian115_email",
    "dian115_password",
    "exclude_keywords",
    "guangya_access_token",
    "guangya_client_id",
    "guangya_device_id",
    "guangya_refresh_token",
    "guangya_request_timeout",
    "hdhaven_password",
    "hdhaven_username",
    "hdhive_access_token",
    "hdhive_api_key",
    "hdhive_client_id",
    "hdhive_password",
    "hdhive_query_mode",
    "hdhive_refresh_token",
    "hdhive_request_interval",
    "hdhive_token_expires_at",
    "hdhive_username",
    "host",
    "juying_password",
    "juying_username",
    "keywords",
    "magnet_metadata_url_template",
    "p123_request_timeout",
    "p123_token",
    "pansou_cloud_types",
    "pinglian_password",
    "pinglian_username",
    "quark_checkin_url",
    "quark_cookie",
    "quark_request_timeout",
    "resource_type_order",
    "scan_limit",
    "takeover_platform_downloads",
    "tianyi_access_token",
    "tianyi_cookie",
    "tianyi_refresh_token",
    "tianyi_request_timeout",
    "tianyi_session_key",
    "unblock_end_time",
    "unblock_start_time",
    "woniu_password",
    "woniu_username",
    "yun139_authorization",
    "yun139_request_timeout",
})

#: 签到动态键后缀：各渠道的 {provider}_checkin_enabled / {provider}_checkin_mode
#: 由签到契约在运行期按渠道生成（见 core/checkin_manager.py），无法静态枚举。
PLUGIN_CONFIG_KEY_SUFFIXES: tuple[str, ...] = ("_checkin_enabled", "_checkin_mode")


class UIConfig:
    """提供 Vue 配置页所需的数据，不再保留旧 iframe/Vuetify 表单。"""

    @classmethod
    def get_default_config(cls) -> Dict[str, Any]:
        current_year = datetime.datetime.now().year
        current_month = datetime.datetime.now().month
        config: Dict[str, Any] = {
            "enabled": False,
            "show_sidebar_nav": True,
            "agent_enabled": True,
            "direct_transfer_enabled": True,
            "title_subscribe_enabled": True,
            "subscribe_all_missing_seasons": True,
            "subscribe_skip_season_zero": True,
            "subscribe_season_limit": 0,
            "notify": True,
            "notification_type": "Plugin",
            **DEFAULT_TEMPLATES,
            "webhook_enabled": False,
            "webhook_url": "",
            "webhook_method": "POST",
            "webhook_timeout": 10,
            "cron": "30 2,10,18 * * *",
            "auto_subscribe_enabled": False,
            "auto_subscribe_onlyonce": False,
            "auto_subscribe_cron": "0 8 * * *",
            "auto_subscribe_username": DEFAULT_AUTO_SUBSCRIBE_USERNAME,
            "auto_subscribe_notify": False,
            "auto_subscribe_skip_subscribed": True,
            "auto_subscribe_skip_history": True,
            "auto_subscribe_skip_library": True,
            "auto_subscribe_skip_season_zero": True,
            "auto_subscribe_proxy": "",
            "auto_subscribe_proxy_username": "",
            "auto_subscribe_proxy_password": "",
            "auto_subscribe_media_types": ["all"],
            "auto_subscribe_douban_enabled": False,
            "auto_subscribe_douban_ranks": ["movie-showing", "movie-hot"],
            "auto_subscribe_douban_proxy": False,
            "auto_subscribe_douban_limit": 30,
            "auto_subscribe_douban_min_vote": 6,
            "auto_subscribe_douban_min_year": current_year,
            "auto_subscribe_douban_min_month": current_month,
            "auto_subscribe_tmdb_enabled": False,
            "auto_subscribe_tmdb_ranks": ["trending", "movies", "tvs"],
            "auto_subscribe_tmdb_proxy": False,
            "auto_subscribe_tmdb_limit": 20,
            "auto_subscribe_tmdb_min_vote": 6,
            "auto_subscribe_tmdb_min_year": current_year,
            "auto_subscribe_bangumi_enabled": False,
            "auto_subscribe_bangumi_proxy": False,
            "auto_subscribe_bangumi_limit": 50,
            "auto_subscribe_bangumi_min_vote": 6,
            "auto_subscribe_bangumi_min_year": current_year,
            "auto_subscribe_bangumi_min_month": current_month,
            "auto_subscribe_anilist_enabled": False,
            "auto_subscribe_anilist_ranks": ["popular_this_season", "trending"],
            "auto_subscribe_anilist_proxy": False,
            "auto_subscribe_anilist_limit": 30,
            "auto_subscribe_anilist_min_vote": 6,
            "auto_subscribe_anilist_min_year": current_year,
            "auto_subscribe_anilist_min_month": current_month,
            "auto_subscribe_maoyan_enabled": False,
            "auto_subscribe_maoyan_base_url": "https://piaofang.maoyan.com",
            "auto_subscribe_maoyan_movie_box": True,
            "auto_subscribe_maoyan_web_platform_map": {"all": ["tv"]},
            "auto_subscribe_maoyan_platforms": ["all"],
            "auto_subscribe_maoyan_limit": 10,
            "auto_subscribe_maoyan_proxy": False,
            "auto_subscribe_maoyan_min_vote": 6,
            "auto_subscribe_maoyan_min_month": current_month,
            "auto_subscribe_netflix_enabled": False,
            "auto_subscribe_netflix_base_url": "https://www.netflix.com",
            "auto_subscribe_netflix_global": True,
            "auto_subscribe_netflix_global_dataset": "weekly",
            "auto_subscribe_netflix_global_media_types": [
                "Films (English)", "Films (Non-English)",
                "TV (English)", "TV (Non-English)",
            ],
            "auto_subscribe_netflix_country_selections": {},
            "auto_subscribe_netflix_limit": 10,
            "auto_subscribe_netflix_proxy": False,
            "auto_subscribe_netflix_min_vote": 6,
            "auto_subscribe_netflix_min_year": current_year,
            "auto_subscribe_netflix_min_month": current_month,
            "auto_subscribe_netflix_rich_metadata": False,
            "auto_subscribe_netflix_max_workers": 4,
            "auto_subscribe_netflix_use_cache": True,
            "auto_subscribe_mikan_enabled": False,
            "auto_subscribe_mikan_year": current_year,
            "auto_subscribe_mikan_season": "当前",
            "auto_subscribe_mikan_resolve_bangumi_id": True,
            "auto_subscribe_mikan_proxy": False,
            "auto_subscribe_mikan_limit": 100,
            "auto_subscribe_mikan_min_vote": 6,
            "auto_subscribe_mikan_min_year": current_year,
            "auto_subscribe_mikan_min_month": current_month,
            "auto_subscribe_mikan_base_urls": [
                "https://mikanani.me", "https://mikanime.tv"
            ],
            "cloud_drive": "115",
            # v1.30.0 阶段 4：organize_after_transfer 已退役（转存后整理恒开），
            # 不再是配置键；旧值由 normalize_plugin_config 静默丢弃。
            "mp_organize_after_share_save": True,
            "organize_subtitles": True,
            "subtitle_traditional_to_simplified": False,
            "video_extensions": [".mkv", ".mp4", ".avi", ".iso"],
            "subtitle_extensions": [
                ".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks",
            ],
            # 写盘类副作用一律默认关：避免新装即与 P115StrmHelper 等外部工具双写，
            # 也避免未填播放地址前缀时写出坏链接。
            "strm_generate_enabled": False,
            "strm_full_scan_enabled": False,
            "strm_full_scan_overwrite": False,
            "strm_increment_enabled": False,
            "strm_increment_minutes": 10,
            "strm_increment_budget": 180,
            # v1.30.0 阶段 4：organize_source_path 已退役（整理台源目录改
            # 为手动现选），不再是配置键；旧值由 normalize_plugin_config 静默丢弃。
            "organize_max_files": 500,
            "organize_max_depth": 8,
            "organize_include_subtitles": True,
            "organize_conflict_policy": "skip",
            "organize_schedule_enabled": False,
            "organize_schedule_cron": "0 4 * * *",
            "organize_schedule_auto_execute": True,
            "organize_schedule_notify": True,
            "nfo_scrape_enabled": False,
            "image_scrape_enabled": False,
            # 只填「主机:端口」；端点由 TEMPLATE 补齐（内置 302 反代）。
            # 默认留空 = 未配置，开启生成时会明确报错而不是写出坏链接。
            "strm_base_url": "",
            "strm_url_template": (
                "{base_url}/api/v1/plugin/CloudSubscribeX/redirect"
                "?pickcode={pickcode}&file_name={file_name}"
            ),
            "media_server_refresh_enabled": False,
            "media_servers": [],
            "media_server_ignore_paths": "",
            "media_server_path_mappings": "",
            "media_server_refresh_delay": 0,
            # 内置 Emby 302 反代：独立端口，播放请求命中 STRM 时 302 到网盘 CDN。
            "emby_proxy_enabled": False,
            "emby_proxy_port": 3010,
            "emby_proxy_upstream": "",
            "emby_proxy_allowed_clients": "",
            "emby_mediainfo_enabled": False,
            "platform_media_sync_enabled": False,
            "platform_deep_delete_enabled": False,
            "platform_deep_delete_title_keywords": ["深度删除", "神医"],
            "timeout_enabled": True,
            "timeout_default_connect": 30,
            "timeout_default_pool": 15,
            "timeout_default_read": 60,
            "timeout_default_write": 60,
            "timeout_slow_connect": 30,
            "timeout_slow_pool": 15,
            "timeout_slow_read": 300,
            "timeout_slow_write": 300,
            "search_source_order": ["pansou"],
            "search_proxy": "",
            "search_proxy_username": "",
            "search_proxy_password": "",
            # MP 自己拉合成种子用的内部地址：与 MP 同进程时默认 localhost 即可。
            "mp_base_url": "http://127.0.0.1:3000",
            "search_cache_enabled": True,
            "search_cache_ttl_minutes": 30,
            "search_concurrency": 2,
            "search_source_timeout": 60,
            "search_circuit_breaker_enabled": True,
            "search_circuit_breaker_threshold": 3,
            "search_circuit_breaker_cooldown": 60,
            "subscribe_filter_mode": "exclude",
            "exclude_subscribes": [],
            "include_subscribes": [],
            "block_system_subscribe": False,
            "takeover_new_subscribes": False,
            "platform_download_policy": "block",
            "block_start_time": "18:00",
            "block_end_time": "23:59",
            "transfer_task_batch_size": CONCURRENCY_AND_COOLDOWN_PARAMS["transfer_task_batch_size"]["default"],
            "cross_transfer_enabled": False,
            "cross_transfer_media_types": ["movie", "tv"],
            "cross_transfer_download_path": "/tmp",
            "cross_transfer_download_threads": 16,
            "cross_transfer_max_concurrent": CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_max_concurrent"]["default"],
            "cross_transfer_aria2_enabled": False,
            "cross_transfer_aria2_connections": 64,
            "cross_transfer_aria2_max_concurrent": 2,
            "cross_transfer_aria2_min_split_size": "1M",
            "cross_transfer_aria2_disk_cache": "32M",
            "cross_transfer_direct_stream": True,
            "cross_transfer_upload_concurrency": CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_concurrency"]["default"],
            "cross_transfer_upload_breaker_threshold": CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_threshold"]["default"],
            "cross_transfer_upload_breaker_cooldown_seconds": CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_upload_breaker_cooldown_seconds"]["default"],
            "cross_transfer_rapid_miss_action": "abort",
            # 真实上传 115 的代理（空=直连，只作用于 115 上传/API）
            "cross_transfer_upload_proxy": "",
            "cross_transfer_upload_proxy_username": "",
            "cross_transfer_upload_proxy_password": "",
            # 每日真实上传字节上限（GB），硬 clamp 1..900；秒传命中 0 字节不计数
            "cross_transfer_daily_upload_limit_gb": CONCURRENCY_AND_COOLDOWN_PARAMS["cross_transfer_daily_upload_limit_gb"]["default"],
            "cross_transfer_residue_cleanup": True,
            # 1.32.0：真名落盘媒体残留的保留期（小时）。1.12.1 起下载即用目标名
            # 落盘、文件名没有 hash，旧清理规则匹配不到；这里给独立保留期，
            # 0 = 关闭这条清理路径。合法区间 0~168。
            "cross_transfer_real_name_retention_hours": 24,
            "subscription_concurrency": CONCURRENCY_AND_COOLDOWN_PARAMS["subscription_concurrency"]["default"],
            "batch_size": 20,
            "batch_interval": CONCURRENCY_AND_COOLDOWN_PARAMS["batch_interval"]["default"],
            "transfer_risk_cooldown": CONCURRENCY_AND_COOLDOWN_PARAMS["transfer_risk_cooldown"]["default"],
            "skip_other_season_dirs": True,
            "cross_season_path_guard": True,
            "enable_cloud_upgrade": False,
            "enable_pt_upgrade": False,
            # v1.30.0 阶段 4：整理漏斗总闸（kill-switch，默认开）。
            # 关 = 全部生产者停止入队、worker 不起轮，旧流水线接手在途
            # 记录；队列行保留在 DB，开关再开时继续处理，不丢。
            # 开 = 行为与此前一致（阶段 1 为总闸，阶段 2/3 各自生效）。
            "funnel_enabled": True,
            # 1.33.0：同集去重时间窗（小时）。终态 done 会释放入队去重键，
            # 同一集换个来源就能再入队并真实转存（实测一小时内传了三次）；
            # 这里把「最近 N 小时已入库」也算成去重条件，0 = 关闭。
            "funnel_recent_done_window_hours": 12,
            # v1.29.7 阶段 1：网盘整理漏斗（手动提交入队 + worker 逐行整理）。
            # 关 = 手动提交回旧流水线，队列里未处理行保留、开关再开时继续处理。
            "funnel_phase1_enabled": False,
            # v1.29.8 阶段 2：TG 进漏斗（TG 自动转存 + 频道手动转存入队）。
            # 关 = TG 回旧流水线与直出 strm 路；开 = TG 走漏斗。只在阶段 1
            # 开启时生效（阶段 1 是漏斗总闸，worker 驱动条件不变）。
            "funnel_phase2_enabled": False,
            # v1.29.9 阶段 3：订阅与洗版进漏斗（订阅轮次 + 洗版替换入队）。
            # 关 = 订阅与洗版回旧流水线；开 = 订阅与洗版走漏斗。只在阶段 1
            # 开启时生效（阶段 1 是漏斗总闸，worker 驱动条件不变）。
            "funnel_phase3_enabled": False,
            # 漏斗目标目录：默认即现有资源目录，零迁移（设计 §14 推荐值）。
            "funnel_target_dir": "/Alist/影视/moviepilot/转存/",
            # 触发方式：immediate=入队后 10 秒内起轮（阶段 1 只支持 immediate）。
            "funnel_trigger": "immediate",
            # 在途登记租约（秒）：worker 认领后 lease_until=now+该值，过期回收重跑。
            "funnel_inflight_lease_seconds": 7200,
            # 单行最多尝试次数，耗尽后挂起待人工复核。
            "funnel_max_attempts": 5,
            # True=落盘前实查双名（带/不带 {tmdb-} 都算）；False=只查规范名，仅排障用。
            "funnel_dedup_strict": True,
            # v1.30.0 阶段 4：04:00 扫描对账默认 dry-run——只分类记日志，
            # 不入队、不删文件。接管（真正入队）留给以后一版，本版关掉
            # 也不接管（只记一条警告），绝不静默接管。
            "funnel_scan_dry_run": True,
            "upgrade_mode": "largest",
            "episode_dedup_enabled": True,
            "verify_landed_enabled": True,
            "prefer_template_named_version": True,
            "quality_min_gain_percent": 10,
            # 每轮订阅任务最多洗版多少集（0=不限）；缺集永远排在这些之前
            "upgrade_episode_limit": 0,
            "quality_min_gain_mb": 200,
            "upgrade_subscribe_ids": [],
            "local_resource_path": "",
            "type_folder_enabled": False,
            "type_folder_movie": "电影",
            "type_folder_tv": "剧集",
            "self_heal_interval": 10,
            "anime_pack_preferred": True,
            "offline_timeout": 30,
            # TG 频道监控：用用户自己的 TG API（api_id/api_hash + 登录会话）读频道消息，
            # 抽取网盘分享链接后进入「频道监控」页，人工或自动转存。
            "tg_monitor_enabled": False,
            "tg_api_id": 0,
            "tg_api_hash": "",
            "tg_session": "",
            "tg_channels": "",
            "tg_channel_rules": [],
            "tg_monitor_keywords": "",
            "tg_monitor_exclude_keywords": "",
            "tg_monitor_scan_limit": 20,
            "tg_monitor_interval_minutes": 10,
            "tg_monitor_max_items": 500,
            "period_episode_rules": "",
            # 白名单模式下，与本频道关注订阅无关的消息是否仍进池子。
            # 关闭（默认）时只保留最近 50 条供回补匹配，其余不入池 ——
            # 否则频道里大量无关资源会把 500 条配额占满。
            # 频道监控全局画质偏好（所有频道共用）：只做软偏好，同集先转
            # 最优画质，低画质保留兜底；识别不到画质默认放行。
            "tg_quality_preference_enabled": True,
            "tg_quality_resolution_order": "4K,1080P,720P",
            "tg_quality_hdr_order": "DOLBY VISION,HDR10+,HDR10,HDR,SDR",
            "tg_quality_codec_order": "HEVC,AVC,AV1",
            "tg_quality_preferred_groups": "",
        }
        try:
            dynamic_defaults = cls.collect_definitions_defaults()
            for key, val in dynamic_defaults.items():
                config.setdefault(key, val)
        except Exception as error:
            logger.debug(f"合并自描述渠道默认配置异常: {error}", exc_info=True)
        return config

    @classmethod
    def collect_definitions_defaults(cls) -> Dict[str, Any]:
        """根据已注册的网盘驱动、搜索渠道和签到契约定义，自动收集所有字段的默认值配置。"""
        defaults: Dict[str, Any] = {}

        # 1. 扫描网盘驱动声明的字段默认值
        try:
            from ..drive.scanner import DriverRegistry
            for driver_cls in DriverRegistry.get_definitions():
                for group in driver_cls.get_config_groups():
                    for field in group.fields:
                        cls._extract_field_default(defaults, field)
        except Exception as error:
            logger.debug(f"从网盘驱动提取默认配置异常: {error}", exc_info=True)

        # 2. 扫描搜索渠道声明的字段默认值
        try:
            from ..search.scanner import SearchSourceRegistry
            for source_cls in SearchSourceRegistry.get_definitions():
                for group in source_cls.get_config_groups():
                    for field in group.fields:
                        cls._extract_field_default(defaults, field)
        except Exception as error:
            logger.debug(f"从搜索渠道提取默认配置异常: {error}", exc_info=True)

        # 3. 扫描签到提供方声明的契约与字段默认值
        try:
            from .checkin_manager import get_checkin_definitions
            for defn in get_checkin_definitions().values():
                if defn.key:
                    defaults.setdefault(f"{defn.key}_checkin_enabled", False)
                    if getattr(defn, "modes", None):
                        defaults.setdefault(f"{defn.key}_checkin_mode", defn.modes[0] if defn.modes else "normal")
                if defn.group and defn.group.fields:
                    for field in defn.group.fields:
                        cls._extract_field_default(defaults, field)
        except Exception as error:
            logger.debug(f"从签到契约提取默认配置异常: {error}", exc_info=True)

        return defaults

    @staticmethod
    def _extract_field_default(defaults: Dict[str, Any], field: Any) -> None:
        key = getattr(field, "key", "")
        if not key or key in defaults:
            return
        ftype = getattr(field, "type", "text")
        if ftype in ("account", "test-source", "hdhive-oauth"):
            return

        if getattr(field, "default", None) is not None:
            defaults[key] = field.default
            return

        if ftype == "switch":
            defaults[key] = False
        elif ftype == "cloud-directory":
            defaults[key] = "/"
        elif ftype in ("priority-order", "tags", "channels", "plugins"):
            defaults[key] = []
        elif ftype == "online-documents":
            defaults[key] = [{"url": "", "resource_types": []}]
        elif ftype == "select":
            options = getattr(field, "options", None)
            if options and isinstance(options, list) and options[0].get("value") is not None:
                defaults[key] = options[0]["value"]
            else:
                defaults[key] = ""
        elif ftype == "number":
            defaults[key] = getattr(field, "min", 0) or 0
        else:
            defaults[key] = ""
    @staticmethod
    def normalize_auto_subscribe_dates(config: Dict[str, Any]) -> None:
        # 唯一实现在 core/config_normalize.py：clamp_auto_subscribe_dates
        #（年份 <= 0、月份不在 1–12 时钳制到当前年月；这里只保留入口名，
        # 供 core/api/config.py 的校验端点调用）。
        clamp_auto_subscribe_dates(config)

    @staticmethod
    def _subscribes() -> list:
        try:
            with SessionFactory() as db:
                return SubscribeOper(db=db).list("N,R") or []
        except Exception as error:
            logger.error(f"获取订阅列表失败: {error}")
            return []

    @staticmethod
    def get_subscribe_options_grouped() -> List[Dict[str, Any]]:
        items = []
        for subscribe in UIConfig._subscribes():
            is_movie = subscribe.type == MediaType.MOVIE.value
            group = "电影订阅" if is_movie else "电视剧订阅"
            prefix = "[电影]" if is_movie else "[电视剧]"
            suffix = f" ({subscribe.year})" if subscribe.year else ""
            season = f" S{subscribe.season or 1}" if subscribe.type == MediaType.TV.value else ""
            items.append(
                {
                    "title": f"{prefix} {subscribe.name}{suffix}{season}",
                    "value": subscribe.id,
                    "group": group,
                    "name": subscribe.name,
                    "year": subscribe.year,
                    "media_type": "movie" if is_movie else "tv",
                    "tmdb_id": tmdb_id_of(subscribe),
                    "season": subscribe.season if not is_movie else None,
                }
            )
        return items

    @staticmethod
    def get_media_server_options() -> List[Dict[str, Any]]:
        try:
            return [
                {"title": config.name, "value": config.name, "type": config.type}
                for config in MediaServerHelper().get_configs().values()
            ]
        except Exception as error:
            logger.error(f"获取媒体服务器列表失败: {error}")
            return []

    @staticmethod
    def normalize_config(target: Dict[str, Any]) -> Dict[str, Any]:
        """集中处理配置清洗、赋初值、字符串与数组拆分转换，解耦前端。"""
        if not isinstance(target, dict):
            return {}
        import re

        if not str(target.get("auto_subscribe_username") or "").strip():
            target["auto_subscribe_username"] = DEFAULT_AUTO_SUBSCRIBE_USERNAME

        # 自动追更起算年月钳制：唯一实现在 core/config_normalize.py
        #（clamp_auto_subscribe_dates），这里只调它。
        clamp_auto_subscribe_dates(target)

        mikan_urls = target.get("auto_subscribe_mikan_base_urls")
        if isinstance(mikan_urls, str):
            target["auto_subscribe_mikan_base_urls"] = [
                u.strip() for u in re.split(r"[\n,，]+", mikan_urls) if u.strip()
            ]
        elif not isinstance(mikan_urls, list) or not mikan_urls:
            target["auto_subscribe_mikan_base_urls"] = ["https://mikanani.me", "https://mikanime.tv"]

        default_fansub_order = ["LoliHouse", "VCB-Studio", "喵萌奶茶|Nekomoe", "Nix-Raws", r"\bANI\b|ANi"]
        if not isinstance(target.get("mikan_fansub_order"), list) or not target["mikan_fansub_order"]:
            target["mikan_fansub_order"] = list(default_fansub_order)
        if not isinstance(target.get("animegarden_fansub_order"), list) or not target["animegarden_fansub_order"]:
            target["animegarden_fansub_order"] = list(default_fansub_order)

        default_no_subs_re = r"无字幕|無字幕|无字版|偏字版|生肉|\b(?:unsubbed|no[ ._-]*subs?|subtitle[ ._-]*free)\b"
        default_chinese_re = r"简[体體繁中]|簡[体體繁中]|繁[体體简簡中]|中[日英双雙文]|[简簡繁]日|\b(?:CHS|CHT|BIG5|GB|SC|TC|ZH|CHI|ZHO)(?:\b|_)"
        default_exclude_re = r"720[pP]|480[pP]|特别篇|特別篇|\b(?:SP|OVA|OAD)\d*|\b\d+\s*-\s*\d+\b"

        if not target.get("mikan_no_subs_re"):
            target["mikan_no_subs_re"] = default_no_subs_re
        if not target.get("mikan_chinese_re"):
            target["mikan_chinese_re"] = default_chinese_re
        if not target.get("mikan_exclude_re"):
            target["mikan_exclude_re"] = default_exclude_re

        if not target.get("animegarden_no_subs_re"):
            target["animegarden_no_subs_re"] = default_no_subs_re
        if not target.get("animegarden_chinese_re"):
            target["animegarden_chinese_re"] = default_chinese_re
        if not target.get("animegarden_exclude_re"):
            target["animegarden_exclude_re"] = default_exclude_re

        if not str(target.get("cross_transfer_download_path") or "").strip():
            target["cross_transfer_download_path"] = "/tmp"

        # 1.33.0：同集去重时间窗，非法值回落 12 小时，clamp 0~168（0=关）。
        try:
            recent_done_hours = int(
                target.get("funnel_recent_done_window_hours", 12)
            )
        except (TypeError, ValueError):
            recent_done_hours = 12
        target["funnel_recent_done_window_hours"] = max(0, min(recent_done_hours, 168))

        # 1.32.0：真名媒体残留保留期，非法值回落默认 24 小时，clamp 0~168（0=关）。
        try:
            real_name_hours = int(target.get("cross_transfer_real_name_retention_hours", 24))
        except (TypeError, ValueError):
            real_name_hours = 24
        target["cross_transfer_real_name_retention_hours"] = max(0, min(real_name_hours, 168))

        online_docs = target.get("online_docs")
        if not isinstance(online_docs, list) or not online_docs:
            legacy_urls = target.get("online_docs_urls") or []
            if isinstance(legacy_urls, str):
                legacy_urls = [u.strip() for u in re.split(r"[,，\n]+", legacy_urls) if u.strip()]
            legacy_types = target.get("online_docs_resource_types") or []
            if not isinstance(legacy_types, list):
                legacy_types = []
            target["online_docs"] = [
                {"url": url, "resource_types": list(legacy_types)}
                for url in legacy_urls if url
            ]
        if not target["online_docs"]:
            target["online_docs"].append({"url": "", "resource_types": []})
        target["online_docs_urls"] = []
        target["online_docs_resource_types"] = []

        for key in list(target.keys()):
            if key == "search_source_order" or key.endswith(
                    ("_channels", "_plugins", "_filter_include", "_filter_exclude")):
                val = target.get(key)
                if isinstance(val, str):
                    target[key] = [v.strip() for v in re.split(r"[,，\n]+", val) if v.strip()]
                elif not isinstance(val, list):
                    target[key] = []

        def _clean_ext_list(raw_val, defaults):
            if isinstance(raw_val, str):
                items = re.split(r"[,，\n\s]+", raw_val)
            elif isinstance(raw_val, (list, set, tuple)):
                items = list(raw_val)
            else:
                return list(defaults)
            result = []
            seen = set()
            for item in items:
                ext = str(item or "").strip().lower()
                if not ext:
                    continue
                if not ext.startswith("."):
                    ext = f".{ext}"
                if ext not in seen:
                    seen.add(ext)
                    result.append(ext)
            return result if result else list(defaults)

        target["video_extensions"] = _clean_ext_list(
            target.get("video_extensions"), [".mkv", ".mp4", ".avi", ".iso"]
        )
        target["subtitle_extensions"] = _clean_ext_list(
            target.get("subtitle_extensions"),
            [".srt", ".ass", ".ssa", ".vtt", ".sub", ".sup", ".idx", ".smi", ".mks"],
        )

        return target


#: _apply_plugin_config 的 setattr 白名单（1.12.20 Phase 1）：只允许已知
#: 的插件配置键落到实例属性上。= UIConfig.get_default_config() 的全部顶层键
#: + PLUGIN_CONFIG_EXTRA_KEYS。未知键由循环直接忽略并记 warning，绝不抛错。
PLUGIN_CONFIG_KEYS: frozenset[str] = (
    frozenset(UIConfig.get_default_config()) | PLUGIN_CONFIG_EXTRA_KEYS
)


def is_plugin_config_key(key: str) -> bool:
    """判定配置键是否在白名单内（含签到动态键后缀规则）。"""
    name = str(key or "")
    if not name:
        return False
    if name in PLUGIN_CONFIG_KEYS:
        return True
    return name.endswith(PLUGIN_CONFIG_KEY_SUFFIXES)
