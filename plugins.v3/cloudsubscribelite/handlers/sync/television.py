"""电视剧订阅搜索、匹配与转存流程。"""

from datetime import date
from typing import Any, Dict, List, Optional, Set

from app.log import logger
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from .candidate_filter import (
    episode_miss_seen,
    remember_episode_miss,
    remember_mismatch,
)
from .utils import normalize_season
from ..notification import MediaServerResolver
from ...core import OwnerDelegator
from ...core.failure import apply_failure
from ...core.services.skip_stats import SkipStatsCollector
from ...core.state import (
    STAGE_TRANSFERRING,
    STAGE_VERIFYING,
    STATUS_DOWNLOADING,
    STATUS_PENDING_CONFIRM,
    STATUS_PROCESSING,
    transition,
)


class TelevisionSyncProcessor(OwnerDelegator):
    """处理电视剧订阅同步。"""

    @staticmethod
    def _share_title_mismatch(
            share_files: Any,
            mediainfo: Any,
            resource_title: str,
            season: Any = None,
    ) -> bool:
        """分享根目录标题守卫（延迟导入，避免测试环境缺少 utils 包）。"""
        from ...utils import FileMatcher
        return FileMatcher.share_title_mismatch(
            share_files, mediainfo, resource_title, season
        )

    @staticmethod
    def _season_episode_air_dates(mediainfo: Any, season: int) -> Dict[int, str]:
        """获取目标季每集的播出日期，优先使用 TMDB 分集数据。"""
        result: Dict[int, str] = {}
        tmdb_id = getattr(mediainfo, "tmdb_id", None)
        if tmdb_id is None and isinstance(mediainfo, dict):
            tmdb_id = mediainfo.get("tmdb_id")
        if tmdb_id:
            try:
                from app.chain.tmdb import TmdbChain
                episodes = TmdbChain().tmdb_episodes(
                    tmdbid=int(tmdb_id),
                    season=int(season),
                    episode_group=getattr(mediainfo, "episode_group", None),
                ) or []
            except Exception as error:  # noqa: BLE001
                logger.debug(f"读取 TMDB 分集播出日期失败：{tmdb_id} S{season}：{error}")
                episodes = []
            for episode in episodes:
                if isinstance(episode, dict):
                    number = episode.get("episode_number")
                    air_date = episode.get("air_date")
                else:
                    number = getattr(episode, "episode_number", None)
                    air_date = getattr(episode, "air_date", None)
                try:
                    number = int(number)
                except (TypeError, ValueError):
                    continue
                text = str(air_date or "").strip()[:10]
                if number > 0 and text:
                    result[number] = text
            if result:
                return result

        # 兼容测试桩或宿主已把分集数据塞进 season_info 的情况。
        season_info = getattr(mediainfo, "season_info", None)
        if season_info is None and isinstance(mediainfo, dict):
            season_info = mediainfo.get("season_info")
        for info in season_info or []:
            if isinstance(info, dict):
                season_number = info.get("season_number")
                episodes = info.get("episodes") or []
            else:
                season_number = getattr(info, "season_number", None)
                episodes = getattr(info, "episodes", None) or []
            try:
                if int(season_number) != int(season):
                    continue
            except (TypeError, ValueError):
                continue
            for episode in episodes:
                if isinstance(episode, dict):
                    number = episode.get("episode_number")
                    air_date = episode.get("air_date")
                else:
                    number = getattr(episode, "episode_number", None)
                    air_date = getattr(episode, "air_date", None)
                try:
                    number = int(number)
                except (TypeError, ValueError):
                    continue
                text = str(air_date or "").strip()[:10]
                if number > 0 and text:
                    result[number] = text
        return result

    def _subscribe_has_active_funnel_rows(self, subscribe) -> bool:
        """v1.29.9 阶段 3：订阅是否还有漏斗在途行（queued/processing/
        failed）。轮次结束结算前调用：在途行未终态时绝不结算——开场扫
        的是目标网盘目录，在途文件会被算作“已存在”，不守卫就会把订阅
        提前结算；终态成功时由 _finish_pending_subscription 回写。

        v1.29.10 热修复（评审 B4）：守卫改查未终态行（多一个 held——
        held 行的集没入库，订阅绝不能因此结算）；漏斗组件不可用或
        查询异常时 fail-closed（返回 True，暂缓结算）。"""
        try:
            subscribe_id = int(getattr(subscribe, "id", 0) or 0)
            if subscribe_id <= 0:
                return False
            has_unsettled = getattr(
                self._owner, "funnel_has_unsettled_rows", None
            )
            if not callable(has_unsettled):
                logger.warning(
                    "漏斗组件缺 funnel_has_unsettled_rows，订阅结算暂缓："
                    f"subscribe_id={subscribe_id}"
                )
                return True
            return bool(has_unsettled(subscribe_id))
        except Exception as error:  # noqa: BLE001 - 查询异常暂缓结算
            logger.warning(
                "查询订阅漏斗未终态行失败，订阅结算暂缓："
                f"subscribe_id={getattr(subscribe, 'id', '')}，{error}"
            )
            return True

    def process_tv_subscribe(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            allow_upgrade: bool = True,
            manual_upgrade: bool = False,
            target_episodes: Optional[Set[int]] = None,
            transient_target: bool = False,
            _upgrade_plan: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        处理单个电视剧订阅——普通转存与网盘洗版的唯一流水线（v1.29.4 第 3 步）。

        三种处置：
        - 缺集 → 转存；
        - 已有 + 更优 → 替换（判重开时按季集判重决定，判重未开且在洗版
          范围内时按基线评分判定；替换的旧版删除只在后处理提交成功后
          由替换正式步骤做）；
        - 已有 + 不更优 → 跳过。

        洗版特有的东西是参数，不是另一条流程：搜索范围（含已入库集数）、
        排序（缺集在前、洗版在后、每轮上限、已达质量上限的集已筛掉）、
        不结算订阅——都由 ``_upgrade_plan``（洗版入口组装，见
        ``UpgradeService._process_tv_subscribe_upgrade``）带进来；普通
        订阅（``_upgrade_plan`` 为空）走完整核对开场与订阅结算，行为与
        合并前一致。

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :param _upgrade_plan: 洗版计划（内部参数，只有洗版入口传）：
            mediainfo / season / sub_key / baseline / local_scores /
            episodes_to_search / missing_episode_set / existing_ep_pri /
            all_expected_episodes
        :return: 更新后的转存数量
        """
        # 本轮没转存时的真实原因，供频道监控展示（不再是笼统的「未发现新资源」）。
        # Phase 3a 第 3 项：开场（停止检查 + 预算 + mediainfo）与资源搜索
        # 收进 SubscribeFlowService，TV/电影共用。
        try:
            if self._owner.subscribe_flow.check_subscribe_stopped():
                return transferred_count
            # 原生 best_version 决定洗版订阅，插件范围进一步限制处理对象。
            # 无订阅绑定的手动提交（如 Emby 电影院卡片转存）没有洗版策略可继承：
            # 只要插件启用了网盘洗版，就先建立真实网盘基线，按洗版规则比较后替换或跳过，
            # 否则会把整包分享重复转存成“(1)”文件。
            manual_cloud_upgrade = bool(
                manual_resources
                and transient_target
                and self._enable_cloud_upgrade
            )
            if (
                    allow_upgrade
                    and (
                    manual_upgrade
                    or manual_cloud_upgrade
                    or self._is_cloud_upgrade_subscribe(subscribe)
            )
            ):
                return self._owner.upgrade._process_tv_subscribe_upgrade(
                    subscribe=subscribe,
                    history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    target_episodes=target_episodes,
                    manual_upgrade=manual_upgrade,
                    manual_resources=manual_resources,
                )

            # --- 开场（二选一，v1.29.4 第 3 步）---
            # 普通模式：完整核对（媒体服务器/网盘扫描、播出日期、订阅对账）。
            # 洗版模式（_upgrade_plan 非空）：基线/评分/季号/媒体信息/预算键
            # 都已在洗版入口（UpgradeService._process_tv_subscribe_upgrade）
            # 算好，跳过扫描与对账；本轮待处理集数由 plan 的
            # episodes_to_search 定（缺集在前、洗版在后、已达质量上限的
            # 集已筛掉）。
            upgrade_scope = _upgrade_plan is not None
            baseline: Dict[int, Dict[str, Any]] = {}
            local_scores: Dict[int, int] = {}
            existing_ep_pri: Dict[str, int] = {}
            new_priority: Dict[str, int] = {}
            upgrade_episodes: Set[int] = set()
            upgrade_downloaded = 0
            # v1.29.9 阶段 3：漏斗归属的待终态洗版集（已入队、未确认入库）。
            # 本轮不计入 upgrade_episodes/new_priority，终态成功时由
            # _finish_pending_subscription 回写进度与评分。
            funnel_pending_upgrade_episodes: Set[int] = set()
            if upgrade_scope:
                mediainfo = _upgrade_plan["mediainfo"]
                season = _upgrade_plan["season"]
                sub_key = _upgrade_plan["sub_key"]
                track_points = False
                baseline = _upgrade_plan["baseline"]
                local_scores = _upgrade_plan["local_scores"]
                existing_ep_pri = _upgrade_plan["existing_ep_pri"]
                new_priority = dict(existing_ep_pri)
                # 本轮待处理集数：缺集 + 待洗版集（变量名沿用“缺集”含义）。
                missing_episodes = list(_upgrade_plan["episodes_to_search"])
                existing_episodes_in_resources = set(baseline.keys())
                expected_episodes = set(_upgrade_plan.get("all_expected_episodes") or [])
                target_episode_air_dates: Dict[int, str] = {}
                unaired_episodes: Set[int] = set()
                undated_episodes: Set[int] = set()
                discover_manual_episodes = False
                discovered_manual_episodes: Set[int] = set()
                existing_episodes_for_manual: Set[int] = set()
                cloud_drive_name = self._owner.naming._cloud_drive_name()
                logger.debug(
                    f"📺 洗版处理订阅：{subscribe.name} S{season:02d}，"
                    f"待处理 {self._format_episode_ranges(set(missing_episodes))}"
                )
            else:

                logger.debug(
                    f"📺 处理订阅：{subscribe.name} S{normalize_season(subscribe.season):02d}，"
                    f"范围 E{subscribe.start_episode or 1:02d}-E{subscribe.total_episode or 0:02d}，"
                    f"订阅记录缺失 {subscribe.lack_episode} 集"
                )

                # 加载该订阅的历史积分花费（用 tmdb_id + 季数作为唯一标识）；
                # Phase 3a 第 3 项：开场准备收进 SubscribeFlowService。
                prepared = self._owner.subscribe_flow.prepare_subscribe_entry(
                    subscribe, MediaType.TV, manual_resources,
                    mediainfo_kwargs={"cache": False},
                )
                if prepared is None:
                    return transferred_count
                track_points, sub_key, mediainfo = prepared

                self._owner.metadata._set_task_phase(subscribe, "核对播出范围", 20)
                if not transient_target:
                    correct_count = getattr(self._owner.metadata, "_correct_bangumi_episode_total", None)
                    if callable(correct_count):
                        correct_count(subscribe, mediainfo)
                season = normalize_season(subscribe.season)
                cloud_drive_name = self._owner.naming._cloud_drive_name()
                total_ep = subscribe.total_episode or 0
                start_ep = subscribe.start_episode or 1
                if total_ep <= 0 and mediainfo:
                    season_episodes = (getattr(mediainfo, "seasons", None) or {}).get(season)
                    if season_episodes and isinstance(season_episodes, (list, tuple, set)):
                        total_ep = max([int(x) for x in season_episodes if str(x).isdigit()] or [0])
                    if total_ep <= 0 and hasattr(mediainfo, "season_info"):
                        for s_info in (mediainfo.season_info or []):
                            s_num = getattr(s_info, "season_number", None) if not isinstance(s_info, dict) else s_info.get(
                                "season_number")
                            if s_num == season:
                                ep_count = getattr(s_info, "episode_count", None) if not isinstance(s_info,
                                                                                                    dict) else s_info.get(
                                    "episode_count")
                                if ep_count and int(ep_count) > 0:
                                    total_ep = int(ep_count)
                                    break
                expected_episodes = set(range(start_ep, total_ep + 1)) if total_ep >= start_ep else set()
                discover_manual_episodes = bool(
                    manual_resources and transient_target and not expected_episodes
                )
                missing_episodes: List[int] = []
                discovered_manual_episodes: Set[int] = set()
                target_episode_air_dates: Dict[int, str] = self._season_episode_air_dates(
                    mediainfo, season
                )
                today = date.today().isoformat()
                # v1.27.1 新口径（用户 2026-11-19 定：按分享里实际有的集来算，不推算日期）：
                # - TMDB 日期确定在今天之后的集 → 未播出，照旧挡住（不计入已有、不进缺集）。
                # - TMDB 没给日期的集 → 不再算未播出：留在缺集里，转不转存只由分享里
                #   实际解析出的视频文件集号决定（下面「分享文件集号 ∩ 缺集」匹配）。
                #   国产日更剧 TMDB 常晚几天才补日期（多喜一家人 E09 起无日期，
                #   旧口径让已出的 E09/E10 每轮被当成「未播出」跳过）。
                # 2026-11-08 谷雨事故（标称「完结」的假资源塞进 22 集碎片）不再靠日期守卫兜，
                # 由标题守卫、分享根目录守卫与逐文件集号匹配挡；日期守卫只挡确定未来的集。
                # 整季一条日期都没有时（TMDB 无分集数据）同样不挡，与旧口径一致。
                unaired_episodes: Set[int] = {
                    episode for episode in expected_episodes
                    if target_episode_air_dates.get(episode)
                    and target_episode_air_dates[episode] > today
                }
                undated_episodes: Set[int] = (
                    {
                        episode for episode in expected_episodes
                        if not target_episode_air_dates.get(episode)
                    }
                    if target_episode_air_dates else set()
                )
                # 1. 先读取媒体服务器实际剧集，不混入订阅 note。
                self._owner.metadata._set_task_phase(subscribe, "检查媒体库内容", 30)
                media_server_valid, media_server_episodes = self._timed_sync_call(
                    "media_server_scan",
                    MediaServerResolver.episode_numbers,
                    self._chain,
                    mediainfo,
                    season,
                )
                existing_episodes_in_resources: Set[int] = set()
                if not media_server_valid:
                    if transient_target:
                        media_server_episodes = set()
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} 未读取到媒体服务器数据，"
                            "临时媒体目标继续按网盘实际内容检查"
                        )
                    else:
                        logger.warning(
                            f"{mediainfo.title_year} S{season:02d} 无法读取媒体服务器实际数据，"
                            f"本轮跳过且不访问{cloud_drive_name}，不修改订阅进度"
                        )
                        self._owner._last_skip_reason = "无法读取媒体服务器数据"
                        return transferred_count
                logger.debug(
                    f"媒体服务器实际存在剧集："
                    f"{self._format_episode_ranges(media_server_episodes & expected_episodes)}"
                )
                # 2026-11-13：物理事实优先。TMDB 分集日期可能错位或缺失（例如
                # 玫瑰战争 E05 已在库里却被无 air_date 判为「未播出」，订阅 note 被剔除），
                # 媒体库已存在的集显然已播出，不应被未播出守卫挡掉。
                # v1.27.1 起无日期的集本就不算未播出；这里只剩「日期在未来但库里已有」的
                # 情况（TMDB 日期错位），同样以物理事实为准。
                if unaired_episodes and media_server_valid and media_server_episodes:
                    library_episodes = set(media_server_episodes) & expected_episodes
                    if library_episodes:
                        unaired_episodes -= library_episodes
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"媒体库已存在的 {self._format_episode_ranges(library_episodes)} "
                            f"按已播出处理（TMDB 日期错位不覆盖物理事实）"
                        )

                # 2. 再读取目标网盘目录；不扫描本地 STRM 路径。
                self._owner.metadata._set_task_phase(subscribe, "检查网盘内容", 40)
                cloud_valid, cloud_episodes, cloud_label = self._timed_sync_call(
                    "cloud_scan",
                    self._owner.naming._scan_cloud_resource_episodes,
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=start_ep,
                    total_episode=total_ep,
                )
                if not cloud_valid:
                    logger.warning(
                        f"{mediainfo.title_year} S{season:02d} 无法读取{cloud_drive_name}实际数据，"
                        "本轮跳过，不修改订阅进度"
                    )
                    self._owner._last_skip_reason = "无法读取网盘数据"
                    return transferred_count
                cloud_matched = set(cloud_episodes & expected_episodes)
                if unaired_episodes:
                    # 日期确定在未来的集即使网盘里已有（泄露/错包），也不计入“已入库”；
                    # 无日期的集按物理事实计入已有（v1.27.1，与 2026-11-13 口径一致）。
                    cloud_matched -= unaired_episodes
                # 以当前目标网盘实际存在的剧集为准计算缺集，彻底解耦外部媒体库可能残留的旧源索引
                existing_episodes_in_resources = set(cloud_matched)
                # 1.34.0（下载前挡）：去重窗口内已 done 的集也算已有——云端目录
                # 快照可能还没刷新（上传后要等下一轮导出树），不并入就会把同一集
                # 再判为缺集、重新下载（实测 S01E07 一小时内下载/上传三次）。
                recent_done: Set[int] = set()
                try:
                    raw_recent = self._owner.funnel.recent_done_episodes(
                        mediainfo, season,
                    )
                    # 只认真实集合：测试替身/宿主缺组件时拿到的是 Mock，
                    # 直接参与集合运算会把 existing 污染成 Mock。
                    if isinstance(raw_recent, (set, frozenset, list, tuple)):
                        for value in raw_recent:
                            try:
                                episode_value = int(value)
                            except (TypeError, ValueError):
                                continue
                            if episode_value > 0:
                                recent_done.add(episode_value)
                except Exception as error:  # noqa: BLE001 - 取不到按原逻辑
                    logger.debug(f"读取去重窗口集失败（忽略）：{error}", exc_info=True)
                if recent_done:
                    window_added = (
                        recent_done & expected_episodes
                    ) - existing_episodes_in_resources
                    if window_added:
                        existing_episodes_in_resources |= window_added
                        logger.info(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"{self._format_episode_ranges(window_added)} "
                            "在去重窗口内已入库，本轮按已有处理（不再重复下载）"
                        )
                existing_episodes_for_manual = set(cloud_episodes) if discover_manual_episodes else set()
                logger.debug(
                    f"{cloud_label} 实际存在剧集："
                    f"{self._format_episode_ranges(cloud_matched)}"
                )

                if expected_episodes:
                    current_note = {
                                       int(episode) for episode in (subscribe.note or [])
                                       if str(episode).isdigit()
                                   } & expected_episodes
                    missing_episodes = sorted(expected_episodes - existing_episodes_in_resources)
                    restored_missing = set(missing_episodes) & current_note
                    if not transient_target:
                        self._reconcile_subscribe_physical_episodes(
                            subscribe=subscribe,
                            episodes=existing_episodes_in_resources,
                            start_episode=start_ep,
                            total_episode=total_ep,
                        )
                    logger.debug(
                        f"{cloud_drive_name}已存在 "
                        f"{self._format_episode_ranges(existing_episodes_in_resources)}，缺失 "
                        f"{self._format_episode_ranges(set(missing_episodes))}"
                    )
                    if restored_missing:
                        logger.warning(
                            f"媒体库与{cloud_drive_name}均不存在，已删除订阅误标并恢复缺集："
                            f"{self._format_episode_ranges(restored_missing)}"
                        )

                if expected_episodes and not missing_episodes and not discover_manual_episodes:
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} "
                        f"{cloud_drive_name}已完整存在"
                    )
                    self._owner._last_skip_reason = "库中已完整存在，无需转存"
                    if not transient_target:
                        # v1.29.9 阶段 3：在途文件也在目标网盘目录里，会被
                        # 算作“已存在”——订阅仍有漏斗在途行时不结算，终态
                        # 成功后由 _finish_pending_subscription 回写。
                        if self._subscribe_has_active_funnel_rows(subscribe):
                            logger.info(
                                f"{mediainfo.title_year} S{season:02d} 仍有漏斗在途行，"
                                "本轮不结算订阅，终态成功后回写"
                            )
                        else:
                            self._subscribe_handler.check_and_finish_subscribe(
                                subscribe=subscribe,
                                mediainfo=mediainfo,
                                success_episodes=sorted(existing_episodes_in_resources),
                            )
                    if track_points and self._search_handler:
                        self._search_handler.clear_subscription_budgets(sub_key)
                    return transferred_count

                # 过滤掉小于开始集数的剧集。
                if subscribe.start_episode:
                    missing_episodes = [
                        episode for episode in missing_episodes
                        if episode >= subscribe.start_episode
                    ]

                # 未播出集数不参与转存：部分资源站会提前放出“全集/泄露版”，
                # 或把别的剧打包冒充；TMDB 日期确定在未来的集按日期挡住。
                if unaired_episodes:
                    skipped = set(missing_episodes) & unaired_episodes
                    if skipped:
                        logger.info(
                            f"{mediainfo.title_year} S{season:02d} 跳过未播出剧集"
                            f"（TMDB 日期在未来）：{self._format_episode_ranges(skipped)}"
                        )
                        missing_episodes = [
                            episode for episode in missing_episodes
                            if episode not in skipped
                        ]
                    if not missing_episodes and not discover_manual_episodes:
                        logger.info(
                            f"{mediainfo.title_year} S{season:02d} 当前缺集均未播出，等待更新"
                        )
                        self._owner._last_skip_reason = "缺集尚未播出，等待更新"
                        return transferred_count
                undated_missing = set(missing_episodes) & undated_episodes
                if undated_missing:
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} 无播出日期的 "
                        f"{len(undated_missing)} 集按分享实际文件判定："
                        f"{self._format_episode_ranges(undated_missing)}"
                    )

                logger.debug(
                    f"{mediainfo.title_year} S{season:02d} 待转存剧集："
                    f"{self._format_episode_ranges(set(missing_episodes))}"
                )

            # 成功转存的集数列表
            success_episodes = []

            # 手动资源作为单一来源进入现有匹配转存链；
            # Phase 3a 第 3 项：资源搜索收进 SubscribeFlowService。
            # 洗版范围同样走这里（搜索范围由 plan 的待处理集数定，
            # 不再像旧洗版流那样直接调 search_sources）。
            enabled_sources, prefetched_results = self._owner.subscribe_flow.collect_subscribe_resources(
                subscribe, mediainfo, MediaType.TV,
                manual_resources=manual_resources,
                season=season,
                target_episodes=missing_episodes,
                target_episode_air_dates=target_episode_air_dates,
                phase_label="搜索候选资源" if upgrade_scope else "搜索缺失剧集",
                phase_progress=40 if upgrade_scope else 55,
            )

            if not enabled_sources:
                logger.debug(f"没有可用的搜索源，跳过 {mediainfo.title} S{season} 的搜索")
                self._owner._last_skip_reason = "未启用可用的搜索源"
                return transferred_count

            resource_batches = self._owner.resource_transfer._build_transfer_resource_batches(
                enabled_sources, prefetched_results
            )
            seen_share_urls = set()
            search_label = self._search_handler._search_label(
                mediainfo, MediaType.TV, season
            )
            for source_index, (source, candidate_resources, is_cross_batch) in enumerate(
                    resource_batches
            ):
                offline_submit_queue: List[Dict[str, Any]] = []
                search_prefix = f"[{search_label}][{source.upper()}]"
                if self._stop_requested():
                    break
                if not missing_episodes and not discover_manual_episodes:
                    logger.debug(f"{mediainfo.title_year} S{season} 所有缺失剧集已转存完成，不再查询后续源")
                    break

                logger.debug(
                    f"🔎 {search_prefix} 开始处理"
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选"
                    f"（当前缺失: {len(missing_episodes)} 集）"
                )
                self._owner.metadata._set_task_phase(
                    subscribe,
                    f"处理 {source.upper()} "
                    f"{'跨盘' if is_cross_batch else '目标网盘'}候选",
                    55 + int((source_index + 1) / len(resource_batches) * 15),
                )

                logger.info(
                    f"{search_prefix} 找到候选资源："
                    f"{self._owner.resource_transfer._format_resource_summary(candidate_resources)}"
                )

                # 遍历搜索结果
                for resource_index, resource in enumerate(candidate_resources):
                    if self._stop_requested():
                        break
                    self._owner.metadata._set_task_phase(
                        subscribe,
                        f"校验候选资源 ({resource_index + 1}/{len(candidate_resources)})",
                        72 + int((resource_index + 1) / len(candidate_resources) * 16),
                    )

                    share_url = resource.get("url", "")
                    resource_title = resource.get("title", "")

                    share_url = self._owner.resource_transfer._resolve_candidate_resource_url(
                        candidate_resources,
                        resource_index,
                        resource,
                        search_label,
                        log_prefix=search_prefix,
                    )
                    if self._stop_requested():
                        break

                    # 洗版范围：需解锁的资源在解锁前先看预览——预览里的目标集
                    # 均不满足洗版条件就跳过，不浪费一次解锁（旧洗版流行为；
                    # 普通流程不做这步）。
                    if (
                            upgrade_scope
                            and (resource.get("need_unlock") or resource.get("need_access"))
                            and not share_url
                    ):
                        resource_ref = resource.get("resource_ref")
                        if resource_ref:
                            preview_files = resource.get("preview_files") or []
                            preview_matches = self._owner.file_matching._match_episode_files(
                                preview_files,
                                mediainfo,
                                subscribe,
                                season,
                                tuple(missing_episodes),
                                require_media_match=is_cross_batch,
                            ) if preview_files else {}
                            preview_candidates = [
                                (episode, file_item, score)
                                for episode, (file_item, score) in preview_matches.items()
                                if file_item
                            ]
                            if preview_candidates:
                                preview_worthwhile = any(
                                    self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                                        local_scores.get(episode, 0),
                                        score,
                                        int(baseline.get(episode, {}).get("file_size") or 0),
                                        self._owner.upgrade_rule_scoring._resource_size_bytes(file_item.get("size")),
                                        has_existing=episode in baseline,
                                        old_file_name=str(
                                            baseline.get(episode, {}).get("target_file_name")
                                            or baseline.get(episode, {}).get("file_name") or ""
                                        ),
                                        subscribe=subscribe,
                                        mediainfo=mediainfo,
                                        season=season,
                                        episode=episode,
                                    )[0]
                                    for episode, file_item, score in preview_candidates
                                )
                                if not preview_worthwhile:
                                    logger.info(
                                        f"{search_prefix} 预览中的目标集均不满足"
                                        f"{self._upgrade_mode}洗版条件，解锁前跳过：{resource_title}"
                                    )
                                    continue

                    if not share_url:
                        continue

                    share_url = share_url.strip()
                    if not self._owner.resource_transfer._is_supported_resource(resource, share_url):
                        logger.debug(
                            f"跳过当前同步链不支持的资源类型 "
                            f"{self._owner.resource_transfer._supported_resource_type(resource, share_url)}：{resource_title}"
                        )
                        continue
                    if share_url in seen_share_urls:
                        logger.debug(f"跳过重复分享链接：{resource_title}")
                        continue
                    seen_share_urls.add(share_url)
                    resource_by_url = {
                        str(item.get("url") or "").strip(): item
                        for item in candidate_resources
                        if str(item.get("url") or "").strip()
                    }
                    resource_input_label = self._owner.resource_transfer._resource_input_label(share_url)
                    logger.debug(
                        f"检查{resource_input_label}：{resource_title} - "
                        f"{self._resource_log_reference(share_url)}"
                    )

                    try:
                        missing_episode_set = set(missing_episodes)
                        is_offline_resource = self._is_offline_url(share_url) or self._is_magnet_url(share_url)
                        if is_offline_resource:
                            # 洗版范围的手动资源也查黑名单（与旧洗版流一致；
                            # 普通流程的手动资源保留人工判断，不在这里拦）。
                            if (upgrade_scope or not manual_resources) and self._is_offline_blacklisted(resource, share_url):
                                logger.debug(
                                    f"离线资源命中黑名单跳过：{resource_title}"
                                )
                                continue

                        if is_offline_resource:
                            magnet_title = self._owner.resource_transfer._prepare_magnet_resource(
                                resource, share_url
                            )
                            title_seasons = self._owner.resource_transfer._magnet_title_seasons(resource)
                            if title_seasons and season not in title_seasons:
                                logger.debug(
                                    f"离线资源标题预过滤排除：标题季数="
                                    f"{','.join(f'S{value:02d}' for value in sorted(title_seasons))}，"
                                    f"目标季数=S{season:02d}，"
                                    f"标题={magnet_title or resource_title}"
                                )
                                continue
                            title_episodes = self._owner.resource_transfer._magnet_title_episodes(
                                resource, season
                            )
                            if title_episodes:
                                target_episode_set = (
                                    title_episodes - discovered_manual_episodes - existing_episodes_for_manual
                                    if discover_manual_episodes
                                    else missing_episode_set & title_episodes
                                )
                                if not target_episode_set:
                                    logger.debug(
                                        f"离线资源标题预过滤排除：标题集数="
                                        f"{self._format_episode_ranges(title_episodes)}，"
                                        f"当前缺集={self._format_episode_ranges(missing_episode_set)}，"
                                        f"标题={magnet_title or resource_title}"
                                    )
                                    continue
                                target_episodes = sorted(target_episode_set)
                            else:
                                preview_episodes = self._owner.resource_transfer._resource_preview_episodes(
                                    resource, season
                                )
                                target_episode_set = (
                                    preview_episodes - discovered_manual_episodes - existing_episodes_for_manual
                                    if discover_manual_episodes
                                    else missing_episode_set & preview_episodes
                                    if preview_episodes
                                    else missing_episode_set
                                )
                                target_episodes = sorted(target_episode_set)

                            if not self._owner.resource_transfer._validate_resource_url(
                                    share_url,
                                    resource_label="Magnet 链接" if self._is_magnet_url(share_url) else "离线下载链接",
                                    log_prefix=search_prefix,
                                    resource_title=resource_title,
                            ):
                                continue
                            if not target_episodes:
                                logger.debug(
                                    f"离线资源预览集数未覆盖当前缺集：预览="
                                    f"{self._format_episode_ranges(preview_episodes)}，"
                                    f"当前缺集={self._format_episode_ranges(missing_episode_set)}，"
                                    f"标题={magnet_title or resource_title}"
                                )
                                continue
                            # 洗版范围：Magnet 元数据通常只有整包评分，先按
                            # 提供方评分粗筛（逐集大小留到下载完成后判断）。
                            # 普通流程不做这步（缺集照单全收）。
                            upgrade_baseline: Dict[str, Any] = {}
                            if upgrade_scope:
                                _, provider_score = self._search_handler.select_file_candidate(
                                    [{"name": (magnet_title or resource_title), "size": 0}],
                                    mediainfo,
                                    subscribe,
                                )
                                worthwhile = False
                                for episode in target_episodes:
                                    baseline_item = baseline.get(episode, {})
                                    old_score = local_scores.get(episode, 0)
                                    episode_worthwhile = False
                                    if episode not in baseline:
                                        episode_worthwhile = True
                                    elif provider_score > old_score:
                                        episode_worthwhile = True
                                    elif provider_score == old_score and self._upgrade_mode in {"largest", "smallest"}:
                                        episode_worthwhile = True
                                    worthwhile = worthwhile or episode_worthwhile
                                    if episode_worthwhile and episode in baseline:
                                        upgrade_baseline[str(episode)] = {
                                            "score": old_score,
                                            "size": int(baseline_item.get("file_size") or 0),
                                            "cloud_dir": baseline_item.get("cloud_dir") or "",
                                            "file_name": (
                                                baseline_item.get("target_file_name")
                                                or baseline_item.get("file_name") or ""
                                            ),
                                            "file_id": baseline_item.get("cloud_file_id") or "",
                                        }
                                if not worthwhile:
                                    logger.debug(
                                        f"{search_prefix} Magnet 提供方评分未超基线，跳过："
                                        f"{magnet_title or resource_title}"
                                    )
                                    continue
                            pending_key = self._queue_magnet_package(
                                resource,
                                share_url,
                                subscribe,
                                mediainfo,
                                season=season,
                                target_episodes=target_episodes,
                                sub_key=sub_key if (track_points or upgrade_scope) else "",
                                transient_target=transient_target,
                                # 洗版立即提交（旧洗版流行为）；普通流程随源
                                # 末尾批量提交。
                                submit_queue=None if upgrade_scope else offline_submit_queue,
                                upgrade=bool(upgrade_baseline),
                                upgrade_mode=self._upgrade_mode if upgrade_scope else "",
                                upgrade_baseline=upgrade_baseline or None,
                            )
                            if not pending_key:
                                continue
                            if discover_manual_episodes:
                                discovered_manual_episodes.update(target_episodes)
                            target_ep_set = set(target_episodes)
                            missing_episodes = [
                                ep for ep in missing_episodes
                                if ep not in target_ep_set
                            ]
                            missing_episode_set = set(missing_episodes)
                            file_list_name = str((resource.get("file_list") or [""])[0]).strip() if isinstance(
                                resource.get("file_list"), list) and resource.get("file_list") else ""
                            provider_name = str(
                                file_list_name
                                or (resource.get("magnet_metadata") or {}).get("display_name")
                                or resource_title
                            ).strip()
                            self._owner.resource_transfer._append_magnet_pending_history(
                                history=history,
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                share_url=share_url,
                                cloud_dir=self._cloud_transfer_path.rstrip('/') or "/",
                                resource=resource,
                                season=season,
                                target_episodes=target_episodes,
                                upgrade=bool(upgrade_baseline),
                                finalize_key=pending_key,
                            )
                            logger.info(
                                f"离线任务已进入下载后真实文件匹配：{provider_name}，"
                                f"目标 {self._format_episode_ranges(target_ep_set)}"
                            )
                            if not discover_manual_episodes and not missing_episodes:
                                logger.debug(
                                    f"{mediainfo.title_year} S{season:02d} 缺失剧集已齐，提前结束候选遍历"
                                )
                                break
                            continue
                        share_files = []
                        selected_file_ids, selected_file_names = (
                            self._owner.resource_transfer._explicit_selection_values(resource)
                        )
                        # 6h 负缓存跳过：如果该资源此前已记录过可用集数，且与当前缺失集完全无交集，免重复列目录
                        # 洗版范围不读这个缓存（待处理集含已入库集，交集口径不同）。
                        if not upgrade_scope and source != "manual" and episode_miss_seen(
                            share_url, resource_title, season, missing_episode_set
                        ):
                            logger.debug(
                                f"跳过候选：6h 内已判定集号不包含当前缺集 - "
                                f"{resource_title or self._resource_log_reference(share_url)}"
                            )
                            continue

                        # Phase 3a 第 3 项：resource_urls 恒为单元素
                        # [share_url]，循环是死结构，直接调一次。
                        share_files.extend(self._owner.resource_transfer._validated_resource_files(
                            share_url,
                            resource_title=resource_title,
                            target_season=(season if self._skip_other_season_dirs else None),
                            log_prefix=search_prefix,
                            target_file_ids=selected_file_ids,
                            target_file_names=selected_file_names,
                            recognition_work_id=getattr(mediainfo, "tmdb_id", None),
                            recognition_season=season,
                        ))
                        if not share_files:
                            continue
                        # 自动搜索源常见“标题写 A、分享里是 B”的挂羊头资源；
                        # 手动资源保留人工判断，不在这里拦。洗版范围也不在这里
                        # 拦（基线比较本身会挡掉不相关的分享）。
                        if not upgrade_scope and source != "manual" and self._share_title_mismatch(
                                share_files, mediainfo, resource_title, season
                        ):
                            reason = "剧名不符"
                            # 只打印「真正导致拦截」的根目录：原先打印
                            # sorted(roots)[:3] 会把 06.mkv / 基德盘搜安卓app.apk
                            # 这类无关名字排在前面，凶手常被截掉，排障只能靠猜。
                            try:
                                from ...utils import FileMatcher as _FM
                                _roots = _FM.mismatch_evidence_roots(
                                    share_files, mediainfo, resource_title
                                )
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "解析拦截根目录失败，回退为全部根目录",
                                    exc_info=True,
                                )
                                _roots = []
                            if _roots:
                                # 短句优先：定性词 + 一个可核对的证据，完整根目录留给日志。
                                extra = " 等 %d 个" % len(_roots) if len(_roots) > 3 else ""
                                reason = (
                                    "剧名不符 · 分享是《"
                                    + _roots[0][:20]
                                    + "》"
                                    + extra
                                )
                            # 记下 (链接, 标题)，24h 内不再为它重复读分享目录。
                            remember_mismatch(share_url, resource_title)
                            logger.info(
                                f"{reason}，已跳过：{resource_title} - "
                                f"{self._resource_log_reference(share_url)}"
                            )
                            self._owner.resource_transfer._record_skip_candidate(
                                SkipStatsCollector.KIND_MISMATCH,
                                share_url,
                                reason=reason,
                                resource_title=resource_title,
                            )
                            continue

                        video_count, share_episodes = self._owner.naming._summarize_share_episodes(
                            share_files,
                            season,
                            mediainfo if is_cross_batch else None,
                        )
                        matched_episode_numbers = (
                            share_episodes - discovered_manual_episodes - existing_episodes_for_manual
                            if discover_manual_episodes
                            else missing_episode_set & share_episodes
                        )
                        absent_episode_numbers = (
                            set()
                            if discover_manual_episodes
                            else missing_episode_set - share_episodes
                        )
                        logger.debug(
                            f"{resource_input_label}实际包含 {video_count} 个视频，"
                            f"S{season:02d} 可识别集数："
                            f"{self._format_episode_ranges(share_episodes)}；"
                            f"当前缺失中可用：{self._format_episode_ranges(matched_episode_numbers)}"
                        )
                        if video_count and not share_episodes:
                            reason = f"认不出集号 · {video_count} 个视频无 S{season:02d}"
                            logger.debug(
                                f"{reason}：{mediainfo.title_year} "
                                f"S{season:02d}，已跳过该资源"
                            )
                            self._owner.resource_transfer._record_skip_candidate(
                                SkipStatsCollector.KIND_UNPARSABLE,
                                share_url,
                                reason=reason,
                                resource_title=resource_title,
                            )
                            continue
                        if absent_episode_numbers:
                            logger.debug(
                                f"{resource_input_label}未包含当前缺失集数："
                                f"{self._format_episode_ranges(absent_episode_numbers)}"
                            )

                        # 收集该分享中所有匹配的文件
                        matched_items = []
                        # 洗版范围的跳过原因（判重/评分未达条件），只在洗版
                        # 结算时汇总记一条；普通流程不产生。
                        skip_reasons: List[str] = []
                        if discover_manual_episodes:
                            episodes_to_match = sorted(matched_episode_numbers)
                        elif upgrade_scope and manual_resources:
                            # 洗版手动提交：分享里实际有的集都算待处理——
                            # 已有集按洗版规则比较，新集正常转存（旧洗版流
                            # 的 pending |= share_episodes）。
                            episodes_to_match = sorted(
                                set(missing_episodes) | share_episodes
                            )
                        else:
                            episodes_to_match = [
                                episode for episode in missing_episodes
                                if not share_episodes or episode in share_episodes
                            ]
                        matched_files = self._owner.file_matching._match_episode_files(
                            share_files,
                            mediainfo,
                            subscribe,
                            season,
                            episodes_to_match,
                            require_media_match=is_cross_batch,
                        )

                        # 1.6.48 按季集判重：懒加载现有版本（目录扫描 + 基线并集）
                        dedup_versions = None
                        if self._owner.episode_dedup._episode_dedup_active() and episodes_to_match:
                            dedup_versions = self._owner.episode_dedup._collect_episode_versions(
                                subscribe, mediainfo, season
                            )

                        for episode in episodes_to_match:
                            matched_file, current_score = matched_files.get(
                                episode, (None, 0)
                            )

                            if matched_file:
                                file_name = matched_file.get('name', '')
                                logger.debug(f"找到匹配文件：{file_name} -> E{episode:02d}")

                                is_upgrade = False
                                candidate_size = self._owner.upgrade_rule_scoring._resource_size_bytes(
                                    matched_file.get("size")
                                )
                                dedup_old_versions: list = []
                                if dedup_versions is not None:
                                    dedup_action, dedup_old, dedup_reason = (
                                        self._owner.episode_dedup._episode_dedup_decision(
                                            subscribe,
                                            mediainfo,
                                            season,
                                            episode,
                                            current_score,
                                            candidate_size,
                                            dedup_versions,
                                            new_file_name=file_name,
                                        )
                                    )
                                    if dedup_action == "skip":
                                        logger.info(
                                            f"{mediainfo.title} S{season:02d}E{episode:02d} "
                                            f"按季集判重跳过：{dedup_reason}"
                                        )
                                        self._owner.resource_transfer._record_skip_candidate(
                                            SkipStatsCollector.KIND_DEDUP_SKIP,
                                            share_url,
                                            reason=f"同集已有更优版本：{dedup_reason}",
                                            resource_title=mediainfo.title,
                                            season=season,
                                            episode=episode,
                                        )
                                        continue
                                    if dedup_action == "replace":
                                        is_upgrade = True
                                        dedup_old_versions = dedup_old
                                elif upgrade_scope:
                                    # 洗版范围且判重未开：按基线评分判定（统一判
                                    # 定器；判重开时上面已用判重决定——判重取数
                                    # 是基线并集，比这里的单基线更全）。
                                    should_upgrade, upgrade_reason = self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                                        local_scores.get(episode, 0),
                                        current_score,
                                        int(baseline.get(episode, {}).get("file_size") or 0),
                                        candidate_size,
                                        has_existing=episode in baseline,
                                        old_file_name=str(
                                            baseline.get(episode, {}).get("target_file_name")
                                            or baseline.get(episode, {}).get("file_name") or ""
                                        ),
                                        subscribe=subscribe,
                                        mediainfo=mediainfo,
                                        season=season,
                                        episode=episode,
                                    )
                                    if not should_upgrade:
                                        logger.debug(
                                            f"{mediainfo.title} S{season:02d}E{episode:02d} "
                                            f"洗版候选跳过：{upgrade_reason}"
                                        )
                                        skip_reasons.append(f"E{episode:02d}:{upgrade_reason}")
                                        continue
                                    is_upgrade = bool(episode in baseline)

                                organize_enabled = getattr(self, "_organize_after_transfer", True)
                                if organize_enabled:
                                    target_dir, target_name = self._owner.naming._platform_target(
                                        self._CLOUD_MEDIA_ROOT, subscribe, mediainfo,
                                        file_name, season, episode
                                    )
                                else:
                                    target_dir = self._owner.resource_transfer._resource_staging_dir(
                                        str(matched_file.get("url") or share_url),
                                        matched_file,
                                    )
                                    target_name = file_name
                                if upgrade_scope and episode in baseline and self._upgrade_mode == "coexist":
                                    # 共存模式：新旧同名会互相覆盖，改名区分
                                    #（旧洗版流行为；普通流程不进这里——共存
                                    # 模式下判重整体关闭，is_upgrade 恒为 False）。
                                    target_name = self._owner.upgrade_rule_scoring._coexist_target_name(
                                        target_name,
                                        file_name,
                                        candidate_size,
                                        matched_file.get("sha1") or "",
                                    )
                                item_entry = {
                                    "file": matched_file,
                                    "resource": resource_by_url.get(
                                        str(matched_file.get("url") or "").strip(), resource
                                    ),
                                    "episode": episode,
                                    "score": current_score,
                                    "new_score": current_score,
                                    "old_score": local_scores.get(episode, 0),
                                    "candidate_size": candidate_size,
                                    "file_name": file_name,
                                    "is_upgrade": is_upgrade,
                                    "target_dir": target_dir,
                                    "target_name": target_name,
                                    "subtitle_files": self._owner.subtitles._companion_subtitle_files(
                                        share_files,
                                        matched_file,
                                        season=season,
                                        episode=episode,
                                    ),
                                }
                                if is_upgrade and dedup_old_versions:
                                    primary_old = dedup_old_versions[0]
                                    item_entry["upgrade_old_cloud_dir"] = str(
                                        primary_old.get("cloud_dir") or ""
                                    )
                                    item_entry["upgrade_old_file_name"] = str(
                                        primary_old.get("file_name") or ""
                                    )
                                    item_entry["upgrade_old_file_id"] = str(
                                        primary_old.get("file_id") or ""
                                    )
                                    item_entry["upgrade_old_size"] = int(
                                        primary_old.get("file_size") or 0
                                    )
                                    # 额外旧版本（>1）在转存成功后批量删除
                                    if len(dedup_old_versions) > 1:
                                        item_entry["dedup_extra_old_versions"] = (
                                            dedup_old_versions[1:]
                                        )
                                        item_entry["dedup_new_score"] = current_score
                                        item_entry["dedup_new_size"] = candidate_size
                                elif upgrade_scope and is_upgrade:
                                    # 洗版范围且判重未开：旧版信息取自基线
                                    #（+ 云端查询补 file_id，供提交后替换步骤用）。
                                    baseline_item = baseline.get(episode, {})
                                    old_dir = str(baseline_item.get("cloud_dir") or "").strip()
                                    old_name = str(
                                        baseline_item.get("target_file_name")
                                        or baseline_item.get("file_name") or ""
                                    ).strip()
                                    old_file_id = str(baseline_item.get("cloud_file_id") or "")
                                    if self._upgrade_mode != "coexist" and not old_file_id:
                                        if not old_dir or not old_name:
                                            old_dir, old_name = self._owner.naming._platform_target(
                                                self._CLOUD_MEDIA_ROOT,
                                                subscribe,
                                                mediainfo,
                                                old_name or file_name,
                                                season,
                                                episode,
                                            )
                                        old_file = self._cloud_query.get_cached_file(old_dir, old_name)
                                        if old_file:
                                            old_file_id = str(getattr(old_file, "id", "") or "")
                                            old_name = str(getattr(old_file, "name", "") or old_name)
                                    item_entry["upgrade_old_cloud_dir"] = old_dir
                                    item_entry["upgrade_old_file_name"] = old_name
                                    item_entry["upgrade_old_file_id"] = old_file_id
                                    item_entry["upgrade_old_size"] = int(
                                        baseline_item.get("file_size") or 0
                                    )
                                matched_items.append(item_entry)

                        if not matched_items:
                            if upgrade_scope:
                                # 洗版范围：判重/评分跳过的集只记原因，不写
                                # 6h 负缓存（洗版流不读它）；无匹配文件时记一条。
                                if skip_reasons:
                                    logger.info(
                                        f"{search_prefix} 候选匹配 {len(matched_files)} 集"
                                        f"但均未达升级条件：{'; '.join(skip_reasons[:12])}，"
                                        f"资源={resource_title or self._resource_log_reference(share_url)}"
                                    )
                                elif not matched_files:
                                    logger.info(
                                        f"{search_prefix} 候选未匹配到目标集数："
                                        f"分享文件 {len(share_files)} 个，目标集数 "
                                        f"{self._format_episode_ranges(set(missing_episodes))}，"
                                        f"资源={resource_title or self._resource_log_reference(share_url)}"
                                    )
                                continue
                            # 记入 6h 负缓存，避免同一未更新资源每轮重复列目录
                            remember_episode_miss(
                                share_url, resource_title, season, share_episodes
                            )
                            # 到这里说明集号解析成功、只是与缺失集没有交集，
                            # 不是「内容不匹配」——账本要能直接看出差在哪。
                            reason = (
                                f"集号不符 · 有 "
                                f"{self._format_episode_ranges(share_episodes) or '无'}，"
                                f"缺 {self._format_episode_ranges(missing_episode_set) or '无'}"
                            )[:100]
                            logger.debug(
                                f"该{resource_input_label}未匹配到 S{season} 的任何缺失剧集："
                                f"{reason}"
                            )
                            self._owner.resource_transfer._record_skip_candidate(
                                SkipStatsCollector.KIND_EPISODE_MISS,
                                share_url,
                                reason=reason,
                                resource_title=resource_title,
                            )
                            continue

                        cloud_resource = self._owner.resource_transfer._is_cloud_resource_url(share_url)
                        direct_cloud_resource = (
                                cloud_resource
                                and self._owner.resource_transfer._is_direct_cloud_resource_url(share_url)
                        )
                        self._owner.metadata._set_task_phase(
                            subscribe,
                            "登记网盘剧集整理"
                            if direct_cloud_resource else "转存匹配剧集",
                            92,
                            clear_search=True,
                        )
                        logger.debug(
                            f"准备批量整理：{mediainfo.title_year} S{season:02d}，"
                            f"{len(matched_items)} 个网盘内文件直接进入目标目录"
                            if direct_cloud_resource
                            else f"准备跨盘转存：{mediainfo.title_year} S{season:02d}，"
                                 f"{len(matched_items)} 个文件进入目标网盘后整理"
                            if cloud_resource
                            else f"准备批量转存：{mediainfo.title_year} S{season:02d}，"
                                 f"{len(matched_items)} 个文件到 {self._cloud_transfer_path}"
                        )
                        transfer_results = self._owner.transfer_ops._transfer_episode_items(
                            matched_items,
                            share_url,
                            mediainfo,
                            subscribe,
                            season,
                            sub_key,
                            track_subscription=not transient_target,
                            transient_target=transient_target,
                        )
                        if not transfer_results:
                            break
                        self._owner.metadata._set_task_phase(subscribe, "登记文件后处理", 95)
                        batch_success_episodes = []
                        batch_detail_episodes = {}
                        batch_detail_size: Dict[str, int] = {}
                        batch_detail_source: Dict[str, str] = {}
                        batch_detail_file_names: Dict[str, Dict[int, str]] = {}
                        batch_detail_episode_sizes: Dict[str, Dict[int, int]] = {}
                        # v1.26.4：按集的终态分支标记（与上面几组按集字典对齐）。
                        batch_detail_organized: Dict[str, Dict[int, bool]] = {}
                        completed_missing_episodes = set()

                        # 1.6.52 落盘自检：Phase 3a 第 1 项已收进管线 confirm
                        # 阶段（_run_ingest_pipeline 批后统一做，一轮一次
                        # 季目录列目录），结果挂在每条 transfer_result 的
                        # "landed" 上；这里只读，不再另调一次自检。
                        # （入口判重跳过的项本轮没转存，管线不给它做自检，
                        # landed 为 None；库里已有版本的名字未必等于
                        # target_name，自检会误判。）

                        # 处理结果
                        for index, transfer_result in enumerate(transfer_results):
                            item = transfer_result["item"]
                            episode = item["episode"]
                            file_name = item["file"]["name"]
                            current_score = item["score"]
                            is_upgrade = item["is_upgrade"]
                            success = transfer_result["success"]
                            pending_key = transfer_result["pending_key"]
                            item_share_url = item["file"].get("url") or share_url

                            history_item = self._owner.transfer_details._build_transfer_history_item(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                status=self._owner.resource_transfer._transfer_history_status(success, item_share_url),
                                share_url=item_share_url,
                                file_name=item["target_name"],
                                source_file_name=file_name,
                                cloud_dir=item["target_dir"],
                                resource=item["resource"],
                                season=season,
                                episode=episode,
                                file_size=int(item["file"].get("size") or 0),
                                source_sha1=item["file"].get("sha1") or "",
                                source_md5=item["file"].get("md5") or "",
                                rule_score=current_score,
                                upgrade=is_upgrade,
                            )
                            if history_item.get("status") == "失败":
                                failure_reason = str(
                                    transfer_result.get("failure_reason") or ""
                                ).strip()
                                if failure_reason:
                                    apply_failure(history_item, failure_reason)
                            history.append(history_item)

                            # 落盘自检只在普通流程做：洗版范围沿用旧洗版流
                            #（不读 landed），避免改动洗版的成功口径。
                            landed_info = transfer_result.get("landed") if not upgrade_scope else None
                            landed_blocked = (
                                    success
                                    and not pending_key
                                    and landed_info is not None
                                    and not self._owner.landed_verify._landed_confirms_success(landed_info)
                            )
                            if success and not pending_key and landed_info is not None:
                                history_item.update(
                                    self._owner.landed_verify._landed_verification_fields(landed_info)
                                )
                                if landed_blocked:
                                    transition(
                                        history_item, STATUS_PENDING_CONFIRM,
                                        stage=STAGE_VERIFYING,
                                        reason="tv_landed_verify",
                                        task_id=history_item.get("record_id") or "",
                                    )
                                    logger.warning(
                                        f"落盘自检未通过：{mediainfo.title} "
                                        f"S{int(season):02d}E{int(episode):02d} "
                                        f"{item['target_name']} 不在目标季目录"
                                        f"（{landed_info.get('reason') or 'missing_in_dir'}），"
                                        f"记「待确认」"
                                    )
                            if success:
                                if transfer_result.get("skip_history"):
                                    # Phase 3a 第 2 项：入口判重已跳过——云盘
                                    # 已有更优版本，不下载不转存。只记一条
                                    # 成功历史、不重试本轮；不计入成功集数
                                    # 与通知（Emby 里还没有它，订阅进度仍
                                    # 以实际入库为准），下一资源仍可尝试更
                                    # 好的版本。
                                    continue
                                if pending_key:
                                    history_item["finalize_key"] = pending_key
                                    transition(
                                        history_item,
                                        STATUS_DOWNLOADING
                                        if self._is_ed2k_url(item_share_url)
                                        else STATUS_PROCESSING,
                                        stage=STAGE_TRANSFERRING,
                                        reason="tv_pending_finalize",
                                        task_id=history_item.get("record_id") or "",
                                    )
                                if landed_blocked:
                                    # 「待确认」不是失败也不是成功：不补齐、不通知、
                                    # 不写整理历史，也不删旧版本（新文件尚未确认存在）。
                                    continue
                                transferred_count += 1
                                completed_missing_episodes.add(episode)

                                if not is_upgrade and not pending_key:
                                    success_episodes.append(episode)

                                if upgrade_scope:
                                    # v1.29.9 阶段 3：漏斗归属的待终态洗版项
                                    # 不计入本轮结算——“已转存”按终态成功计数
                                    # （设计 §6），终态成功时由
                                    # _finish_pending_subscription 回写进度与
                                    # 评分；否则同一集会在确认入库前就被记进
                                    # note/评分。判定：本项待终态且本订阅/计划
                                    # 走漏斗（计划标记优先，订阅标记兜底）。
                                    funnel_routed = bool(
                                        (_upgrade_plan or {}).get("funnel_route")
                                        or getattr(subscribe, "_funnel_route", False)
                                    )
                                    if pending_key and funnel_routed:
                                        funnel_pending_upgrade_episodes.add(episode)
                                    else:
                                        # 洗版结算用：已转存集（缺集 + 替换）与
                                        # 新评分，供末尾更新订阅进度与 episode_priority。
                                        upgrade_episodes.add(episode)
                                        upgrade_downloaded += 1
                                        new_priority[str(episode)] = current_score

                                score_info = f"(平台优先级:{current_score})"
                                upgrade_info = " [洗版升级]" if is_upgrade else ""
                                logger.debug(
                                    f"成功转存：{mediainfo.title} S{season:02d}E{episode:02d} {score_info}{upgrade_info}")

                                # v1.29.1：额外旧版本不再这里删——转存成功只
                                # 代表新文件已落盘，确认入库在后处理提交
                                # （_commit_single_item）之后；删除走那里的
                                # 替换正式步骤（_apply_replacement_deletion），
                                # 失败/超时都保留旧版。

                                if not pending_key:
                                    # v1.29.1：通知类型走统一派生（处置是
                                    # replace 即 upgrade；replaced_from 在
                                    # 提交后才有，终态详情里补）。
                                    notification_kind = self._owner.transfer_details._derive_notification_kind(
                                        is_upgrade=is_upgrade,
                                        cross=history_item.get("transfer_mode") == "cross",
                                    )
                                    batch_detail_episodes.setdefault(
                                        notification_kind, []
                                    ).append(episode)
                                    # 体积与文件名直接取转存结果，不从 history 里补。
                                    batch_detail_episode_sizes.setdefault(
                                        notification_kind, {}
                                    )[episode] = int(item["file"].get("size") or 0)
                                    batch_detail_file_names.setdefault(
                                        notification_kind, {}
                                    )[episode] = str(
                                        item.get("target_name") or file_name or ""
                                    ).strip()
                                    batch_detail_size[notification_kind] = (
                                        batch_detail_size.get(notification_kind, 0)
                                        + int(item["file"].get("size") or 0)
                                    )
                                    if not batch_detail_source.get(notification_kind):
                                        batch_detail_source[notification_kind] = (
                                            self._owner.transfer_details._detail_source_text(history_item)
                                        )
                                    # v1.26.4：按集的终态分支标记——MP 整理
                                    # 身份只在触发成功时才挂在处理项上。
                                    batch_detail_organized.setdefault(
                                        notification_kind, {}
                                    )[episode] = bool(
                                        item.get("mp_organize")
                                        or (item.get("file") or {}).get("mp_organize")
                                    )

                                batch_success_episodes.append(episode)
                            else:
                                logger.error(f"转存失败：{mediainfo.title} S{season:02d}E{episode:02d}")

                        if completed_missing_episodes:
                            if discover_manual_episodes:
                                discovered_manual_episodes.update(
                                    completed_missing_episodes
                                )
                            missing_episodes = [
                                episode for episode in missing_episodes
                                if episode not in completed_missing_episodes
                            ]
                        for notification_kind, episodes in batch_detail_episodes.items():
                            try:
                                names_by_episode = batch_detail_file_names.get(
                                    notification_kind, {}
                                )
                                ordered_episodes = sorted({
                                    int(episode) for episode in episodes
                                    if int(episode) > 0
                                })
                                self._owner.transfer_details._append_tv_transfer_detail(
                                    transfer_details, mediainfo, season, ordered_episodes,
                                    notification_kind=notification_kind,
                                    size_bytes=batch_detail_size.get(notification_kind, 0),
                                    source_text=batch_detail_source.get(notification_kind, ""),
                                    file_names=[
                                        names_by_episode.get(episode, "")
                                        for episode in ordered_episodes
                                    ],
                                    episode_sizes=batch_detail_episode_sizes.get(
                                        notification_kind
                                    ),
                                    organized_via_mp_episodes=batch_detail_organized.get(
                                        notification_kind
                                    ),
                                )
                            except TypeError as error:
                                # 同上：模块新旧混用时签名可能不一致，通知失败不中断转存。
                                logger.debug(f"转存通知详情写入失败（已忽略）：{error}", exc_info=True)

                        # 记录下载历史
                        if batch_success_episodes:
                            episodes_str = StringUtils.format_ep(batch_success_episodes)
                            self._owner.transfer_details._record_download_history(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                path=matched_items[0]["target_dir"],
                                download_hash=share_url,
                                torrent_name=resource_title,
                                share_url=share_url,
                                seasons=f"S{season:02d}",
                                episodes=episodes_str,
                            )

                        if (
                                self._stop_requested()
                                or not discover_manual_episodes and not missing_episodes
                        ):
                            break

                    except Exception as e:
                        logger.error(
                            f"处理分享链接出错：{self._resource_log_reference(share_url)}，"
                            f"错误：{str(e)}"
                        )
                        continue

                if offline_submit_queue:
                    self._owner.metadata._clear_task_search_state(subscribe)
                    successful_pending_keys = self._submit_offline_packages(
                        offline_submit_queue
                    )
                    successful_contexts = [
                        item for item in offline_submit_queue
                        if str(item.get("pending_key") or "") in successful_pending_keys
                    ]
                    submitted_count = sum(
                        len(item.get("target_episodes") or [1])
                        for item in successful_contexts
                    )
                    transferred_count += submitted_count
                    failed_contexts = [
                        item for item in offline_submit_queue
                        if str(item.get("pending_key") or "")
                           not in successful_pending_keys
                    ]
                    if failed_contexts:
                        failed_keys = {
                            str(item.get("pending_key") or "")
                            for item in failed_contexts
                        }
                        for item in history:
                            if str(item.get("finalize_key") or "") in failed_keys:
                                apply_failure(item, "提交离线下载失败")
                                item["error"] = "提交离线下载失败"
                                item.pop("finalize_key", None)
                        restored_episodes = {
                            int(episode)
                            for item in failed_contexts
                            for episode in item.get("target_episodes") or []
                            if int(episode) > 0
                        }
                        if discover_manual_episodes:
                            discovered_manual_episodes.difference_update(restored_episodes)
                        else:
                            missing_episodes = sorted(
                                set(missing_episodes) | restored_episodes
                            )

                # 当前源处理完成
                if missing_episodes:
                    remaining_batches = resource_batches[source_index + 1:]
                    remaining_label = "仍待处理" if upgrade_scope else "仍缺失"
                    if remaining_batches:
                        next_source, _, next_is_cross = remaining_batches[0]
                        logger.debug(
                            f"{source.upper()} 处理完成，{remaining_label} {len(missing_episodes)} 集，"
                            f"继续处理 {next_source.upper()} "
                            f"{'跨盘' if next_is_cross else '目标网盘'}候选"
                        )
                    else:
                        logger.debug(
                            f"{source.upper()} 处理完成，{remaining_label} {len(missing_episodes)} 集，"
                            "候选资源已用尽"
                        )

            # 更新订阅状态
            self._owner.metadata._set_task_phase(subscribe, "更新订阅进度", 96)
            if upgrade_scope:
                # 洗版结算：不调 check_and_finish_subscribe，订阅保持激活
                # 以持续搜索更优资源；只更新进度与单集评分。
                if upgrade_episodes and not manual_upgrade and not transient_target:
                    remaining_lack = self._subscribe_handler.update_subscribe_progress(
                        subscribe=subscribe,
                        mediainfo=mediainfo,
                        success_episodes=sorted(upgrade_episodes),
                    )
                    if remaining_lack is not None:
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"已更新订阅进度（{len(upgrade_episodes)} 集）"
                        )
                    else:
                        logger.warning(
                            f"{mediainfo.title_year} S{season:02d} 更新订阅进度失败"
                        )
                if (
                        new_priority != existing_ep_pri
                        and not manual_upgrade
                        and not transient_target
                ):
                    try:
                        from app.db.subscribe_oper import SubscribeOper
                        SubscribeOper().update(subscribe.id, {"episode_priority": new_priority})
                        logger.debug(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"已更新 {len(new_priority)} 集评分"
                        )
                    except Exception as e:
                        logger.warning(
                            f"{mediainfo.title_year} S{season:02d} "
                            f"更新 episode_priority 失败：{e}"
                        )
                if upgrade_downloaded:
                    self._owner.metadata._set_task_phase(subscribe, "洗版完成", 100)
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} "
                        f"洗版转存完成，共升级 {upgrade_downloaded} 集"
                    )
                else:
                    self._owner.metadata._set_task_phase(subscribe, "无需替换", 100)
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} "
                        "洗版转存完成，未发现可升级资源"
                    )
                # v1.29.9 阶段 3：本轮入队的洗版集尚未确认入库，只记一条
                # 入队日志；进度与评分在终态成功时由
                # _finish_pending_subscription 回写。
                if funnel_pending_upgrade_episodes:
                    logger.info(
                        f"{mediainfo.title_year} S{season:02d} 洗版 "
                        f"{len(funnel_pending_upgrade_episodes)} 集已入队整理队列，"
                        "终态成功后回写订阅进度与评分"
                    )
            else:
                # 将媒体路径已存在的集数和本次成功转存的集数合并
                all_success_episodes = list(set(success_episodes) | existing_episodes_in_resources)
                if all_success_episodes and not transient_target:
                    # v1.29.9 阶段 3：订阅仍有漏斗在途行时不结算——开场扫
                    # 的目标网盘目录含在途文件，会被算作“已存在”，不守卫
                    # 就会把订阅提前结算；终态成功时由
                    # _finish_pending_subscription 回写。
                    if self._subscribe_has_active_funnel_rows(subscribe):
                        logger.info(
                            f"{mediainfo.title_year} S{season:02d} 仍有漏斗在途行，"
                            "本轮不结算订阅，终态成功后回写"
                        )
                    else:
                        remaining_lack = self._subscribe_handler.check_and_finish_subscribe(
                            subscribe=subscribe,
                            mediainfo=mediainfo,
                            success_episodes=all_success_episodes
                        )
                        if track_points and remaining_lack == 0 and hasattr(
                                self._search_handler, "clear_subscription_budgets"
                        ):
                            self._search_handler.clear_subscription_budgets(sub_key)

        except Exception as e:
            logger.error(f"处理订阅 {subscribe.name} 出错：{str(e)}")
        return transferred_count
