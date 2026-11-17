"""订阅入口公共流程（Phase 3a 第 3 项）。

TV（process_tv_subscribe）与电影（process_movie_subscribe）入口原来
各写一遍：停止检查 + 积分预算键 + mediainfo 解析、手动/搜索源查询 +
进度回调 + 资源批次组装。本组件只收这两段公共流程：
``check_subscribe_stopped`` / ``prepare_subscribe_entry``（开场）、
``collect_subscribe_resources``（查资源）。

不收的部分（各留原地，结构不同）：
- TV 的缺集计算、媒体服务器扫描、网盘扫描；
- 电影的洗版基线（媒体服务器 + 手动基线 + 网盘回退）；
- 洗版入口 _process_tv_subscribe_upgrade 的开场：它无条件重置预算
  （不看 manual_resources），与 prepare_subscribe_entry 的 track_points
  口径不同，仍留在 upgrade.py。
- 资源批次组装本身已是公共方法 ``_build_transfer_resource_batches``，
  TV/电影直接调它，不再包一层。

进度回调口径：TV（"搜索缺失剧集"，55）与电影（"搜索候选资源"，45）
只差兜底文案与阶段进度号，用 phase_label / phase_progress 传入。
get_enabled_sources 的调用口径刻意保持两边旧样：TV 不传 media_type，
电影传 media_type=MediaType.MOVIE。
"""

from typing import Any, Dict, List, Optional, Tuple

from app.core.context import MediaInfo
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator


class SubscribeFlowService(OwnerDelegator):
    """订阅入口公共流程：开场准备 + 资源搜索。"""

    def check_subscribe_stopped(self) -> bool:
        """入口停止检查：停了就记原因返回 True（调用方直接 return）。

        两个入口开场都做这件事：先清 _last_skip_reason，再看停止标志。
        """
        self._owner._last_skip_reason = ""
        if self._stop_requested():
            self._owner._last_skip_reason = "已手动停止"
            return True
        return False

    def prepare_subscribe_entry(
            self,
            subscribe,
            media_type: MediaType,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            mediainfo_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Optional[Tuple[bool, str, MediaInfo]]:
        """开场准备：返回 (track_points, sub_key, mediainfo)，进不去返回 None。

        做的事：预算键（tmdb_id + 季数 / tmdb_id 唯一标识）+ 手动资源
        之外重置积分预算；解析 mediainfo（解析不到记原因返回 None）。
        调用方：``prepared is None`` 时直接 ``return transferred_count``。
        mediainfo_kwargs：TV 传 ``{"cache": False}``，电影不传。
        """
        track_points = not manual_resources
        sub_key = self.subscription_budget_key(subscribe, media_type)
        if track_points and self._search_handler:
            self._search_handler.reset_subscription_budgets(sub_key)
        mediainfo: MediaInfo = self._subscribe_mediainfo(
            subscribe, media_type, **(mediainfo_kwargs or {})
        )
        if not mediainfo:
            logger.warning(f"无法识别媒体信息：{subscribe.name}")
            self._owner._last_skip_reason = "无法识别媒体信息"
            return None
        return track_points, sub_key, mediainfo

    def collect_subscribe_resources(
            self,
            subscribe,
            mediainfo: MediaInfo,
            media_type: MediaType,
            *,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            season: Optional[int] = None,
            target_episodes: Optional[Any] = None,
            target_episode_air_dates: Optional[Dict[int, str]] = None,
            phase_label: str = "搜索候选资源",
            phase_progress: int = 45,
    ) -> Tuple[List[str], Dict[str, List[Dict[str, Any]]]]:
        """查资源：返回 (source_order, source_results)，没源时返回 ([], {})。

        手动资源直接作为单一来源（source_order=["manual"]）；否则按启用
        源搜索（搜索源为空时不调 search_sources，直接返回空——调用方按
        “未启用可用的搜索源”记原因返回，TV 这样做；电影没源时本来也
        走到“未找到可用资源”，同一结局）。
        season / target_episodes / target_episode_air_dates 只在 TV 传
        （search_sources 的 TV 专属参数），电影不传。
        """
        if manual_resources:
            self._set_task_phase(
                subscribe, "处理手动网盘资源", phase_progress
            )
            return ["manual"], {
                "manual": [dict(resource) for resource in manual_resources]
            }
        if media_type == MediaType.MOVIE:
            source_order = self._search_handler.get_enabled_sources(
                media_type=MediaType.MOVIE
            )
        else:
            source_order = self._search_handler.get_enabled_sources()
        if not source_order:
            return [], {}
        self._set_task_phase(subscribe, phase_label, phase_progress)

        def on_search_progress(data: Dict[str, Any]) -> None:
            completed = data.get("completed", 0)
            total = max(1, data.get("total", 1))
            current_prog = 45 + int((completed / total) * 20)  # 45% -> 65%
            self._set_task_phase(
                subscribe,
                data.get("summary") or phase_label,
                current_prog,
                search_active=data.get("active", False),
                search_channels=data.get("channels", []),
                search_total_results=data.get("total_results", 0),
            )

        search_kwargs: Dict[str, Any] = {}
        if season is not None:
            search_kwargs["season"] = season
        if target_episodes is not None:
            search_kwargs["target_episodes"] = target_episodes
        if target_episode_air_dates is not None:
            search_kwargs["target_episode_air_dates"] = target_episode_air_dates
        source_results = self._search_handler.search_sources(
            sources=source_order,
            mediainfo=mediainfo,
            media_type=media_type,
            subscribe=subscribe,
            progress_callback=on_search_progress,
            **search_kwargs,
        )
        return source_order, source_results
