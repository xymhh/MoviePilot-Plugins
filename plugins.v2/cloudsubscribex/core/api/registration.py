""" 页面模式、API 路由、命令与定时服务注册。"""

import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import pytz
from app.core.config import settings
from app.core.security import verify_resource_token
from app.log import logger
from app.schemas.types import EventType
from app.utils.timer import TimerUtils
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Depends

from ..delegation import OwnerDelegator
from ..agent import (
    CloudSubscribeCacheClearTool,
    CloudSubscribeCheckinTool,
    CloudSubscribeCheckinHistoryTool,
    CloudSubscribeConfigUpdateTool,
    CloudSubscribeLinksTool,
    CloudSubscribePerformanceTool,
    CloudSubscribeResourceSearchTool,
    CloudSubscribeResourceSelectTool,
    CloudSubscribeStatusTool,
    CloudSubscribeSyncTool,
)
from ..config import UIConfig
from .emby_proxy import emby_proxy_status, restart_emby_proxy


class _WorkflowActionHandler(dict):
    """兼顾动作执行与 FastAPI 动作列表序列化。"""

    def __init__(self, func: Callable[..., Tuple[bool, Any]]):
        super().__init__()
        self._func = func

    def __bool__(self) -> bool:
        return True

    def __call__(self, *args, **kwargs) -> Tuple[bool, Any]:
        return self._func(*args, **kwargs)


class MoviePilotRegistration(OwnerDelegator):
    """声明插件对暴露的注册信息。"""

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, str]:
        """使用 Vue 模块联邦渲染配置页与详情页。"""
        return "vue", "dist/assets"

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return None, UIConfig.get_default_config()

    def get_page(self) -> Optional[List[dict]]:
        return None

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/auto_subscribe/run",
                "endpoint": self.api_run_auto_subscribe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行榜单自动订阅",
            },
            {
                "path": "/auto_subscribe/test",
                "endpoint": self.api_test_auto_subscribe,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试榜单来源",
            },
            {
                "path": "/auto_subscribe/proxy/test",
                "endpoint": self.api_test_auto_subscribe_proxy,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "测试榜单代理",
            },
            {
                "path": "/overview",
                "endpoint": self.api_platform_overview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取仪表盘与侧栏页面聚合状态",
            },
            {
                "path": "/page_data",
                "endpoint": self.api_vue_page_data,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取历史记录分页数据",
            },
            {
                "path": "/doctor",
                "endpoint": self.api_vue_doctor,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "一键体检：网盘/搜索/TMDB/私有库/目录健康",
            },
            {
                "path": "/history/summary",
                "endpoint": self.api_vue_history_summary,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取历史记录聚合摘要",
            },
            {
                "path": "/duplicate/list",
                "endpoint": self.api_duplicate_list,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "列出重复订阅组",
            },
            {
                "path": "/ui_options",
                "endpoint": self.api_vue_ui_options,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取指定配置作用域选项",
            },
            {
                "path": "/account/refresh",
                "endpoint": self.api_vue_refresh_account,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动刷新单个账户信息卡片",
            },
            {
                "path": "/hdhive/oauth/start",
                "endpoint": self.api_vue_hdhive_oauth_start,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "生成HDHive OpenAPI用户授权链接",
            },
            {
                "path": "/hdhive/oauth/exchange",
                "endpoint": self.api_vue_hdhive_oauth_exchange,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "校验HDHive OAuth回调并换取用户Token",
            },
            {
                "path": "/checkin/overview",
                "endpoint": self.api_vue_checkin_overview,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取多渠道签到仪表盘",
            },
            {
                "path": "/checkin/history",
                "endpoint": self.api_vue_checkin_histories,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取全部渠道签到历史",
            },
            {
                "path": "/checkin/{provider}",
                "endpoint": self.api_vue_checkin,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即执行提供方签到",
            },
            {
                "path": "/checkin/{provider}/history",
                "endpoint": self.api_vue_checkin_history,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取提供方签到历史",
            },
            {
                "path": "/cloud/directories",
                "endpoint": self.api_vue_cloud_directories,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "浏览当前网盘目录",
            },
            {
                "path": "/cloud/directories/create",
                "endpoint": self.api_vue_create_cloud_directory,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "创建网盘目录",
            },
            {
                "path": "/local/directories",
                "endpoint": self.api_vue_local_directories,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "浏览本地存储目录",
            },
            {
                "path": "/local/directories/create",
                "endpoint": self.api_vue_create_local_directory,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "创建本地存储目录",
            },
            {
                "path": "/search/tmdb",
                "endpoint": self.api_vue_search_tmdb_candidates,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按标题查询 TMDB 媒体候选",
            },
            {
                "path": "/search/test",
                "endpoint": self.api_vue_test_search_source,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "使用页面输入测试单个搜索渠道",
            },
            {
                "path": "/search/proxy/test",
                "endpoint": self.api_vue_test_search_proxy,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "通过Cloudflare Trace测试搜索代理",
            },
            {
                "path": "/search/preview",
                "endpoint": self.api_vue_preview_search_resource,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "只读预览搜索资源文件列表",
            },
            {
                "path": "/search/unlock",
                "endpoint": self.api_vue_unlock_search_resource,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "确认后解锁测试资源",
            },

            {
                "path": "/resource/search_resources",
                "endpoint": self.api_vue_resource_search_resources,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "后台并发搜索指定媒体的网盘及磁力资源",
            },
            {
                "path": "/config/save",
                "endpoint": self.api_vue_save_config,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "保存Vue配置页配置",
            },
            {
                "path": "/sync/start",
                "endpoint": self.api_vue_start_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "启动全部或所选订阅搜索",
            },
            {
                "path": "/sync/manual",
                "endpoint": self.api_vue_start_manual_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按指定订阅手动添加资源链接",
            },
            {
                "path": "/sync/manual/resolve",
                "endpoint": self.api_vue_resolve_manual_links,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "识别手动资源对应的订阅或 TMDB 媒体",
            },
            {
                "path": "/sync/stop",
                "endpoint": self.api_vue_stop_sync,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止订阅追更",
            },
            {
                "path": "/sync/task/stop",
                "endpoint": self.api_vue_stop_sync_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止单个订阅任务",
            },
            {
                "path": "/runtime",
                "endpoint": self.api_vue_runtime_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看运行状态",
            },
            {
                "path": "/tasks",
                "endpoint": self.api_vue_tasks,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看统一任务中心（历史聚合）",
            },
            {
                "path": "/sync/pause",
                "endpoint": self.api_vue_sync_pause,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看转存是否处于全局暂停",
            },
            {
                "path": "/sync/pause",
                "endpoint": self.api_vue_set_sync_pause,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "全局暂停/恢复转存",
            },
            {
                "path": "/channels",
                "endpoint": self.api_channels,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "列出全部渠道（含搜索源目录与表单 schema）",
            },
            {
                "path": "/channels",
                "endpoint": self.api_channel_create,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "新增渠道",
            },
            {
                "path": "/channels/summary",
                "endpoint": self.api_channels_summary,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "渠道汇总统计",
            },
            {
                "path": "/channels/{channel_id}",
                "endpoint": self.api_channel_update,
                "methods": ["PUT"],
                "auth": "bear",
                "summary": "更新渠道",
            },
            {
                "path": "/channels/{channel_id}",
                "endpoint": self.api_channel_delete,
                "methods": ["DELETE"],
                "auth": "bear",
                "summary": "删除渠道",
            },
            {
                "path": "/channels/{channel_id}/test",
                "endpoint": self.api_channel_test,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "探测渠道可用性",
            },
            {
                "path": "/channels/{channel_id}/scan",
                "endpoint": self.api_channel_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描一个 TG 频道",
            },
            {
                "path": "/tg/status",
                "endpoint": self.api_vue_tg_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "TG 频道监控状态",
            },
            {
                "path": "/tg/items",
                "endpoint": self.api_vue_tg_items,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "TG 频道抓取到的资源",
            },
            {
                "path": "/tg/scan",
                "endpoint": self.api_vue_tg_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即扫描 TG 频道",
            },
            {
                "path": "/tg/ignore",
                "endpoint": self.api_vue_tg_ignore,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "忽略 TG 资源",
            },
            {
                "path": "/tg/delete",
                "endpoint": self.api_vue_tg_delete,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除 TG 资源记录",
            },
            {
                "path": "/tg/subscribes",
                "endpoint": self.api_vue_tg_subscribes,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "目标订阅列表",
            },
            # v1.29.7 阶段 1：整理队列（漏斗）接口。
            {
                "path": "/funnel/rows",
                "endpoint": self.api_vue_funnel_rows,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "整理队列列表（待处理/处理中/已挂起）",
            },
            {
                "path": "/funnel/status",
                "endpoint": self.api_vue_funnel_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "整理队列状态与 worker 运行状态",
            },
            {
                "path": "/funnel/run",
                "endpoint": self.api_vue_funnel_run,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "立即整理一轮",
            },
            {
                "path": "/funnel/stop",
                "endpoint": self.api_vue_funnel_stop,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "停止整理队列",
            },
            {
                "path": "/funnel/requeue",
                "endpoint": self.api_vue_funnel_requeue,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "把挂起/失败行放回队列",
            },
            {
                "path": "/funnel/delete",
                "endpoint": self.api_vue_funnel_delete,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除整理队列行（pending 记录与网盘文件均保留）",
            },
            {
                "path": "/funnel/delete-old-version",
                "endpoint": self.api_vue_funnel_delete_old_version,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "人工删旧版：删除挂起/失败行 replaced_from 里的旧文件",
            },
            {
                "path": "/tg/transfer",
                "endpoint": self.api_vue_tg_transfer,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "手动转存频道监控条目（单条或多条，入转存队列）",
            },
            {
                "path": "/tg/retry_failed",
                "endpoint": self.api_vue_tg_retry_failed,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试失败的 TG 转存任务",
            },
            {
                "path": "/tg/login/start",
                "endpoint": self.api_vue_tg_login_start,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "TG 登录-发送验证码",
            },
            {
                "path": "/tg/login/code",
                "endpoint": self.api_vue_tg_login_code,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "TG 登录-验证码/两步密码",
            },
            {
                "path": "/tg/logout",
                "endpoint": self.api_vue_tg_logout,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "退出 TG 登录",
            },
            {
                "path": "/runtime/stream",
                "endpoint": self.api_vue_runtime_stream,
                "methods": ["GET"],
                "allow_anonymous": True,
                "dependencies": [Depends(verify_resource_token)],
                "summary": "实时推送运行状态",
            },
            {
                # MP 搜索页拉合成种子用的匿名端点：MP 不带登录态请求，
                # 安全靠 sid + HMAC 签名（见 core/mp_search.api_torrent）。
                "path": "/torrent",
                "endpoint": self.api_torrent,
                "methods": ["GET"],
                "allow_anonymous": True,
                "summary": "按分享 id 输出合成种子",
            },
            {
                "path": "/offline",
                "endpoint": self.api_vue_offline_tasks,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "查看当前网盘离线任务",
            },
            {
                "path": "/offline/refresh",
                "endpoint": self.api_vue_refresh_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "刷新当前网盘离线任务",
            },
            {
                "path": "/offline/delete",
                "endpoint": self.api_vue_delete_offline_task,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除当前网盘离线任务",
            },
            {
                "path": "/offline/delete_batch",
                "endpoint": self.api_vue_delete_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除当前网盘离线任务",
            },
            {
                "path": "/offline/retry",
                "endpoint": self.api_vue_retry_offline_tasks,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试当前网盘离线下载或文件后处理任务",
            },
            {
                "path": "/history/clear",
                "endpoint": self.api_vue_clear_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空失败记录",
            },
            {
                "path": "/history/delete",
                "endpoint": self.api_vue_delete_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "删除单条失败记录",
            },
            {
                "path": "/history/delete_batch",
                "endpoint": self.api_vue_delete_history_batch,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量删除失败记录",
            },
            {
                "path": "/history/upgrade",
                "endpoint": self.api_vue_upgrade_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "从媒体服务器内容发起洗版",
            },
            {
                "path": "/media/servers",
                "endpoint": self.api_vue_media_servers,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取可用媒体服务器选项",
            },
            {
                "path": "/media/content",
                "endpoint": self.api_vue_media_server_content,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "搜索路径符合插件配置的媒体服务器内容",
            },
            {
                "path": "/history/retry",
                "endpoint": self.api_vue_retry_history,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重试单条失败转存记录",
            },
            {
                "path": "/history/retry_batch",
                "endpoint": self.api_vue_retry_history_batch,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量重试失败转存记录",
            },
            {
                "path": "/history/mark_success",
                "endpoint": self.api_vue_mark_history_success,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "批量把待修复失败记录标记为成功",
            },
            {
                "path": "/cache/clear",
                "endpoint": self.api_vue_clear_cache,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清空搜索和分享缓存",
            },
            {
                "path": "/redirect",
                "endpoint": self.api_strm_redirect,
                "methods": ["GET", "HEAD"],
                "allow_anonymous": True,
                "summary": "STRM 播放直链 302 跳转（pickcode → 网盘直链）",
            },
            {
                "path": "/emby_proxy/status",
                "endpoint": self.api_emby_proxy_status,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "Emby 302 反代运行状态",
            },
            {
                "path": "/emby_proxy/restart",
                "endpoint": self.api_emby_proxy_restart,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "重启 Emby 302 反代",
            },
            {
                "path": "/strm/full_scan",
                "endpoint": self.api_vue_strm_full_scan,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "按目录 CID 全量校准 STRM",
            },
            {
                "path": "/residue/cleanup",
                "endpoint": self.api_vue_cleanup_residue,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "清理跨盘缓存残留文件",
            },
            {
                "path": "/qrcode",
                "endpoint": self.api_vue_get_qrcode,
                "methods": ["GET"],
                "auth": "bear",
                "summary": "获取网盘登录二维码",
            },
            {
                "path": "/qrcode/check",
                "endpoint": self.api_vue_check_qrcode_post,
                "methods": ["POST"],
                "auth": "bear",
                "summary": "检查网盘扫码登录状态（POST）",
            },
        ]

    def api_emby_proxy_status(self) -> Dict[str, Any]:
        """返回内置 Emby 302 反代的开关、运行态与命中统计。"""
        return emby_proxy_status(self._owner)

    def api_emby_proxy_restart(self) -> Dict[str, Any]:
        """停再起自带反代；配置关闭时只停不起。"""
        return restart_emby_proxy(self._owner)

    def get_dashboard_meta(self) -> List[Dict[str, str]]:
        if not self._enabled:
            return []
        return [
            {"key": "overview", "name": "网盘订阅助手"},
            {"key": "checkin", "name": "签到概览"},
        ]

    def get_dashboard(
            self, key: str = "overview", **kwargs
    ) -> Optional[Tuple[Dict[str, Any], Dict[str, Any], None]]:
        if not self._enabled:
            return None
        dashboards = {
            "overview": {
                "title": "网盘订阅助手",
                "subtitle": "订阅任务与转存概览",
                "dashboard": "overview",
            },
            "checkin": {
                "title": "签到概览",
                "subtitle": "多渠道每日签到与积分",
                "dashboard": "checkin",
            },
        }
        attrs = dashboards.get(key)
        if attrs is None:
            return None
        return (
            {"cols": 12, "sm": 6, "md": 4},
            {**attrs, "refresh": 30, "border": True},
            None,
        )

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._show_sidebar_nav:
            return []
        # 1.10.3：插件 UI 按流程复用 MP 界面——追更列表走 MP 订阅页、网盘资源走
        # MP 探索页；历史记录没有 MP 代替，独立成侧栏导航。
        # 1.10.4：网盘整理不新增导航（1.10.3 的 organize 入口已撤），整理设置
        # 只留在配置页（organize 分组，高级设置里），整理台随导航一起删除。
        # 1.13.2：渠道页拆三导航——频道监控（TgMonitor）从渠道页独立出来，
        # 渠道页只留 ChannelManager；签到不新增导航（主页 checkin widget 已有）。
        return [
            {
                "nav_key": "channels",
                "title": "渠道管理",
                "icon": "mdi-hub-outline",
                "section": "system",
                "permission": "manage",
                "order": 30,
            },
            {
                "nav_key": "tg-monitor",
                "title": "频道监控",
                "icon": "mdi-telegram",
                "section": "system",
                "permission": "manage",
                "order": 31,
            },
            {
                "nav_key": "history",
                "title": "失败记录",
                "icon": "mdi-clipboard-text-clock-outline",
                "section": "system",
                "permission": "manage",
                "order": 32,
            },
            # v1.29.7 阶段 1：整理队列（网盘整理漏斗）独立成主侧栏导航。
            {
                "nav_key": "funnel",
                "title": "整理队列",
                "icon": "mdi-filter-check-outline",
                "section": "system",
                "permission": "manage",
                "order": 33,
            },
        ]

    def get_actions(self) -> List[Dict[str, Any]]:
        return [
            {
                "id": "cloudsubscribe_start_sync",
                "action_id": "cloudsubscribe_start_sync",
                "name": "执行网盘订阅搜索",
                "func": _WorkflowActionHandler(self.workflow_start_sync),
                "kwargs": {},
            },
            {
                "id": "cloudsubscribe_process_links",
                "action_id": "cloudsubscribe_process_links",
                "name": "处理网盘资源链接",
                "func": _WorkflowActionHandler(self.workflow_process_links),
                "kwargs": {},
            },
        ]

    def get_agent_tools(self) -> List[Type]:
        if not self._enabled or not self._agent_enabled:
            return []
        return [
            CloudSubscribeStatusTool,
            CloudSubscribeSyncTool,
            CloudSubscribeCheckinTool,
            CloudSubscribeCheckinHistoryTool,
            CloudSubscribeLinksTool,
            CloudSubscribeResourceSearchTool,
            CloudSubscribeResourceSelectTool,
            CloudSubscribeCacheClearTool,
            CloudSubscribeConfigUpdateTool,
            CloudSubscribePerformanceTool,
        ]

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """定义通用远程控制命令。"""
        return [
            {
                "cmd": "/cloud_sync",
                "event": EventType.PluginAction,
                "desc": "执行网盘订阅搜索",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_sync"},
            },
            {
                "cmd": "/cloud_auto_subscribe",
                "event": EventType.PluginAction,
                "desc": "立即执行榜单自动订阅",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_auto_subscribe"},
            },
            {
                "cmd": "/cloud_status",
                "event": EventType.PluginAction,
                "desc": "查看网盘订阅状态",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_status"},
            },
            {
                "cmd": "/cloud_link",
                "event": EventType.PluginAction,
                "desc": "提交网盘链接：订阅ID或媒体名称 链接",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_links"},
            },
            {
                "cmd": "/cloud_checkin",
                "event": EventType.PluginAction,
                "desc": "立即执行搜索渠道签到",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_checkin"},
            },
            {
                "cmd": "/cloud_checkin_history",
                "event": EventType.PluginAction,
                "desc": "列举签到详情：渠道 数量",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_checkin_history"},
            },
            {
                "cmd": "/cloud_cache_clear",
                "event": EventType.PluginAction,
                "desc": "清理网盘订阅缓存",
                "category": "网盘订阅",
                "data": {"action": "cloudsubscribe_cache_clear"},
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []

        services = []

        if (
                bool(getattr(self, "_auto_subscribe_enabled", False))
                and self._cron_is_valid(getattr(self, "_auto_subscribe_cron", ""))
        ):
            services.append({
                "id": "CloudSubscribe_AutoSubscribe",
                "name": "榜单自动订阅服务",
                "trigger": CronTrigger.from_crontab(self._auto_subscribe_cron),
                "func": self.run_auto_subscribe,
                "kwargs": {},
            })

        if self._cron and self._cron_is_valid(self._cron):
            try:
                services.append({
                    "id": "CloudSubscribe",
                    "name": "网盘订阅助手服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.sync_subscribes,
                    "kwargs": {}
                })
            except Exception as e:
                logger.warning(f"Cron 表达式无效：{self._cron}，将回退默认 0 18-23 * * *。错误：{e}")
                services.append({
                    "id": "CloudSubscribe",
                    "name": "网盘订阅助手服务",
                    "trigger": CronTrigger.from_crontab("0 18-23 * * *"),
                    "func": self.sync_subscribes,
                    "kwargs": {}
                })
        else:
            services.append({
                "id": "CloudSubscribe",
                "name": "网盘订阅助手服务",
                "trigger": CronTrigger.from_crontab("0 18-23 * * *"),
                "func": self.sync_subscribes,
                "kwargs": {}
            })

        services.append({
            "id": "CloudSubscribe_TakeoverInstall",
            "name": "网盘订阅接管初始化",
            "trigger": DateTrigger(
                run_date=(
                    datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3)
                )
            ),
            "func": self._install_subscribe_search_takeover,
            "kwargs": {}
        })

        services.extend(self._maintenance_services())

        # 与运行时使用同一套凭据判定，避免注册了必然失败的签到渠道。
        checkin_providers = [
            provider
            for provider in self.get_checkin_providers()
            if bool(getattr(self, provider.enabled_attr, False))
               and self.is_checkin_ready(provider)
        ]
        if checkin_providers and self._cron_is_valid(self._checkin_cron):
            timezone = pytz.timezone(settings.TZ)
            checkin_trigger = CronTrigger.from_crontab(
                self._checkin_cron,
                timezone=timezone,
            )
            if self._checkin_auto_retry:
                retry_times = TimerUtils.random_even_scheduler(
                    num_executions=self._configured_retry_count(),
                    begin_hour=self._RETRY_START_HOUR,
                    end_hour=self._RETRY_END_HOUR,
                )
                checkin_trigger = OrTrigger(
                    [checkin_trigger] + [
                        CronTrigger(
                            minute=retry_time.minute,
                            hour=retry_time.hour,
                            timezone=timezone,
                        )
                        for retry_time in retry_times
                    ]
                )
            services.append({
                "id": "CloudSubscribe_Checkin",
                "name": "签到服务",
                "trigger": checkin_trigger,
                "func": self.run_scheduled_checkins,
                "kwargs": {},
            })

        return services

    def _maintenance_services(self) -> List[Dict[str, Any]]:
        """插件自身的固定周期维护任务，交给 MP 调度（v1.27.0）。

        原来挂在插件自建调度器（core/scheduler.py）上，异常只进 APScheduler
        日志、「定时服务」页也看不到。现在随 get_service 声明，统一从
        ``_run_scheduled_task`` 进入：MP 会记 ERROR、发系统消息并防止同一任务
        重叠执行。触发器、周期与开关和迁移前一致；只在插件启用时声明。
        """
        services: List[Dict[str, Any]] = []
        strm_enabled = bool(getattr(self, "_strm_generate_enabled", False))
        if strm_enabled and bool(getattr(self, "_strm_full_scan_enabled", False)):
            # 每天 04:20 分批校准，避开 04:30 的其它定时任务。
            services.append({
                "id": "CloudSubscribe_StrmFullScan",
                "name": "STRM 全量校准",
                "trigger": CronTrigger(hour=4, minute=20),
                "func": self._run_scheduled_task,
                "func_kwargs": {"task": "strm_full_scan"},
                "kwargs": {},
            })
        if strm_enabled and bool(getattr(self, "_strm_increment_enabled", False)):
            try:
                minutes = max(1, int(getattr(self, "_strm_increment_minutes", 10) or 10))
            except (TypeError, ValueError):
                minutes = 10
            services.append({
                "id": "CloudSubscribe_StrmIncrement",
                "name": "STRM 增量补齐",
                "trigger": IntervalTrigger(minutes=minutes),
                "func": self._run_scheduled_task,
                "func_kwargs": {"task": "strm_increment"},
                "kwargs": {},
            })
        if bool(getattr(self, "_organize_schedule_enabled", False)):
            cron = str(getattr(self, "_organize_schedule_cron", "") or "")
            if not getattr(self, "_organize_source_path", ""):
                logger.warning("整理台定时任务已开启，但未配置 organize_source_path")
            elif not self._cron_is_valid(cron):
                logger.warning(f"整理台定时任务 Cron 无效：{cron}，未注册")
            else:
                services.append({
                    "id": "CloudSubscribe_Organize",
                    "name": "115 网盘整理",
                    "trigger": CronTrigger.from_crontab(cron),
                    "func": self._run_scheduled_task,
                    "func_kwargs": {"task": "organize"},
                    "kwargs": {},
                })
        # 每天清理一次过期的重试索引行（失败 90 天、待修复 30 天）。
        services.append({
            "id": "CloudSubscribe_RetryIndexMaintenance",
            "name": "失败记录过期清理",
            "trigger": IntervalTrigger(hours=24),
            "func": self._run_scheduled_task,
            "func_kwargs": {"task": "retry_index_maintenance"},
            "kwargs": {},
        })
        return services

    def _refresh_platform_services(self) -> None:
        """配置保存后刷新当前插件在中注册的定时服务。"""
        try:
            from app.scheduler import Scheduler

            scheduler = Scheduler.get_existing_instance()
            if scheduler is None:
                return
            scheduler.update_plugin_job(self._owner.__class__.__name__)
        except Exception as error:
            logger.warning(f"刷新插件服务失败：{error}")
