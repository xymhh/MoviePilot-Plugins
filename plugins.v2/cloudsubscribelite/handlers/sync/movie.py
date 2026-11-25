"""电影订阅搜索、匹配与转存流程。"""

import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.failure import apply_failure
from ...core.media import tmdb_id_of
from ...core.services.skip_stats import SkipStatsCollector
from ...core.state import (
    STAGE_TRANSFERRING,
    STATUS_DOWNLOADING,
    STATUS_PROCESSING,
    transition,
)
from ...drive.common import format_size


class MovieSyncProcessor(OwnerDelegator):
    """处理电影订阅同步。"""

    def _subscribe_has_active_funnel_rows(self, subscribe) -> bool:
        """v1.29.9 阶段 3：订阅是否还有漏斗在途行（queued/processing/
        failed）。与 TelevisionSyncProcessor 同一守卫：轮次结束结算前
        调用，在途行未终态时绝不结算，终态成功时由
        _finish_pending_subscription 回写。

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

    def process_movie_subscribe(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
            manual_upgrade: bool = False,
            transient_target: bool = False,
    ) -> int:
        """
        处理单个电影订阅

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :return: 更新后的转存数量
        """
        # 本轮没转存时的真实原因，供频道监控展示。
        # Phase 3a 第 3 项：开场（停止检查 + 预算 + mediainfo）与资源搜索
        # 收进 SubscribeFlowService，TV/电影共用。
        try:
            if self._owner.subscribe_flow.check_subscribe_stopped():
                return transferred_count
            logger.debug(f"处理电影订阅：{subscribe.name} ({subscribe.year})")

            # 加载该订阅的历史积分花费（用 tmdb_id 作为唯一标识）；
            # Phase 3a 第 3 项：开场准备收进 SubscribeFlowService
            # （mediainfo 也在这里解析，后面的洗版基线直接用它）。
            prepared = self._owner.subscribe_flow.prepare_subscribe_entry(
                subscribe, MediaType.MOVIE, manual_resources,
            )
            if prepared is None:
                return transferred_count
            track_points, sub_key, mediainfo = prepared

            # v1.13.3：洗版基线不再从插件 history 取（history 表已删）；
            # 现有版本基线只认媒体服务器文件、手动基线与网盘回退三处，
            # 下面三处按优先级依次合并进 movie_history_score/size。
            movie_history_score = -1  # -1 表示未转存过
            movie_history_size = 0

            # 原生 best_version 决定是否为洗版订阅，插件范围进一步限制处理对象。
            is_best_version = manual_upgrade or self._is_cloud_upgrade_subscribe(subscribe)

            # 洗版电影需要先建立现有版本基线，否则同名文件只能被当作普通订阅完成。
            existing_movie = None
            upgrade_target_exists = False
            if is_best_version:
                media_server_has_size = False
                manual_movie = (
                        (getattr(subscribe, "_manual_media_baseline", {}) or {}).get("movie")
                        or {}
                )
                if manual_movie:
                    manual_name = str(manual_movie.get("file_name") or "").strip()
                    manual_size = self._owner.upgrade_rule_scoring._resource_size_bytes(manual_movie.get("file_size"))
                    manual_score = self._owner.upgrade_rule_scoring._get_mp_rule_score(
                        manual_name, manual_size, subscribe, 0, mediainfo
                    )
                    movie_history_score = max(movie_history_score, manual_score)
                    if manual_size:
                        movie_history_size = manual_size
                    logger.info(
                        f"电影 {subscribe.name} 洗版基线采用所选媒体库内容："
                        f"{manual_name}，评分 {manual_score}，{format_size(manual_size)}"
                    )
                for media_item in self._media_server_resolver.movie_media(
                        chain=self._chain, mediainfo=mediainfo
                ):
                    media_file = Path(str(media_item.get("path") or ""))
                    media_size = self._owner.upgrade_rule_scoring._resource_size_bytes(media_item.get("size"))
                    rule_title = str(
                        media_item.get("rule_title") or media_file.name
                    ).strip()
                    media_server_has_size = media_server_has_size or media_size > 0
                    media_server_score = self._owner.upgrade_rule_scoring._get_mp_rule_score(
                        rule_title, media_size, subscribe, 0, mediainfo
                    )
                    if media_size:
                        movie_history_size = media_size
                    if media_server_score > movie_history_score:
                        movie_history_score = media_server_score
                    if media_size or media_server_score > 0:
                        logger.info(
                            f"电影 {subscribe.name} 洗版基线采用媒体服务器内容："
                            f"{media_file.name}，媒体详情 {rule_title}，"
                            f"评分 {media_server_score}，{format_size(media_size)}"
                        )
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._owner.naming._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    upgrade_target_exists = True
                    existing_dir, existing_name, existing_file = existing_movie
                    if self._strm_generate_enabled:
                        existing_strm = self._owner.strm_ops._generate_strm(
                            existing_dir,
                            existing_name,
                            target_file=existing_file,
                        )
                        if not existing_strm:
                            logger.debug(
                                f"电影 {subscribe.name} 真实网盘文件已存在，"
                                "但 STRM 修复尚未完成"
                            )
                    if not media_server_has_size:
                        file_name, target_file = existing_name, existing_file
                        existing_size = int(getattr(target_file, "size", 0) or 0)
                        existing_score = self._owner.upgrade_rule_scoring._get_mp_rule_score(
                            file_name, existing_size, subscribe, 0, mediainfo
                        )
                        if existing_score >= movie_history_score:
                            movie_history_score = existing_score
                            movie_history_size = existing_size
                        logger.info(
                            f"电影 {subscribe.name} 媒体服务器未提供有效大小，"
                            f"网盘回退基线：{movie_history_score} "
                            f"（{file_name}，{format_size(existing_size)}）"
                        )
                    logger.info(
                        f"电影 {subscribe.name} 洗版中，"
                        f"现有平台优先级 {movie_history_score}"
                    )
                else:
                    movie_history_score = -1
                    movie_history_size = 0
                    logger.info(
                        f"电影 {subscribe.name} 未找到真实网盘旧文件，"
                        "本轮无法建立洗版基线"
                    )
                    if manual_upgrade:
                        return transferred_count

            if not manual_resources:
                release_date = self._owner.metadata._calendar_date(mediainfo.release_date)
                if release_date and release_date > datetime.date.today():
                    self._owner.metadata.defer_subscribe_until(
                        subscribe,
                        release_date,
                        f"电影上映日期为 {release_date.isoformat()}",
                    )
                    logger.debug(
                        f"电影 {mediainfo.title_year} 尚未上映，"
                        f"延期至 {release_date.isoformat()} 后检查"
                    )
                    self._owner._last_skip_reason = f"电影尚未上映（{release_date.isoformat()}）"
                    return transferred_count
            self._owner.metadata._set_task_phase(subscribe, "检查网盘内容", 25)

            if not is_best_version:
                existing_movie = self._timed_sync_call(
                    "cloud_scan",
                    self._owner.naming._find_cloud_movie_file,
                    subscribe,
                    mediainfo,
                )
                if existing_movie:
                    cloud_dir, file_name, _ = existing_movie
                    logger.info(
                        f"目标电影已存在，结束订阅：{cloud_dir.rstrip('/')}/{file_name}"
                    )
                    self._owner.strm_ops._generate_strm(cloud_dir, file_name)
                    self._scrape_metadata(cloud_dir, file_name, mediainfo)
                    if not transient_target:
                        # v1.29.10 热修复（评审 B4）：快路径结算前同样守卫
                        # （与剧集 :429 一致）——漏斗还有未终态行时不结算，
                        # 等行终态成功后的回写再结。
                        if self._subscribe_has_active_funnel_rows(subscribe):
                            logger.info(
                                f"电影 {mediainfo.title_year} 已在库，"
                                "但漏斗有未终态行，暂缓结算"
                            )
                        else:
                            self._subscribe_handler.check_and_finish_subscribe(
                                subscribe=subscribe,
                                mediainfo=mediainfo,
                                success_episodes=[1],
                            )
                    if track_points and self._search_handler:
                        self._search_handler.clear_subscription_budgets(sub_key)
                    self._owner._last_skip_reason = "库中已存在该电影，无需转存"
                    return transferred_count

            # 手动资源直接进入现有匹配转存链，否则查询搜索源；
            # Phase 3a 第 3 项：资源搜索收进 SubscribeFlowService。
            source_order, source_results = self._owner.subscribe_flow.collect_subscribe_resources(
                subscribe, mediainfo, MediaType.MOVIE,
                manual_resources=manual_resources,
                phase_label="搜索候选资源",
                phase_progress=45,
            )
            if manual_resources:
                logger.info(
                    f"手动处理电影 {mediainfo.title}："
                    f"收到 {len(source_results['manual'])} 个资源链接"
                )
            resource_batches = self._owner.resource_transfer._build_transfer_resource_batches(
                source_order, source_results
            )
            candidate_resources = [
                resource
                for _, resources, _ in resource_batches
                for resource in resources
            ]

            if not candidate_resources:
                if self._stop_requested():
                    return transferred_count
                logger.info(f"未找到电影 {mediainfo.title} 的可处理资源")
                self._owner._last_skip_reason = "未找到可用资源"
                return transferred_count

            self._owner.metadata._set_task_phase(subscribe, "筛选候选资源", 60)
            search_label = self._search_handler._search_label(
                mediainfo, MediaType.MOVIE
            )
            result_sources = "/".join(dict.fromkeys(
                str(resource.get("source") or "unknown").upper()
                for resource in candidate_resources
            ))
            logger.debug(
                f"[{search_label}][{result_sources}] 找到候选资源："
                f"{self._owner.resource_transfer._format_resource_summary(candidate_resources)}"
            )

            # 遍历搜索结果，尝试找到并转存电影
            movie_transferred = False
            for resource_index, resource in enumerate(candidate_resources):
                if movie_transferred or self._stop_requested():
                    break
                self._owner.metadata._set_task_phase(
                    subscribe,
                    f"校验候选资源 ({resource_index + 1}/{len(candidate_resources)})",
                    60 + int((resource_index + 1) / len(candidate_resources) * 22),
                )

                share_url = resource.get("url", "")
                resource_title = resource.get("title", "")

                # 检查是否是刚搜索出尚未真正解锁的延期解锁资源
                if (resource.get("need_unlock") or resource.get("need_access")) and not share_url:
                    resource_ref = resource.get("resource_ref")
                    if resource_ref and upgrade_target_exists and movie_history_score >= 0:
                        preview_name = str(
                            getattr(self._search_handler, "_resource_filter_title", lambda value: "")(
                                resource
                            ) or resource_title
                        ).strip()
                        preview_matched, preview_score = self._search_handler.select_file_candidate(
                            [{
                                "name": preview_name,
                                "size": self._owner.upgrade_rule_scoring._resource_size_bytes(resource.get("size")),
                            }],
                            mediainfo,
                            subscribe,
                        )
                        preview_size = self._owner.upgrade_rule_scoring._resource_size_bytes(resource.get("size"))
                        preview_decision = self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                            movie_history_score,
                            preview_score,
                            movie_history_size,
                            preview_size,
                            old_file_name=(
                                existing_movie[1] if existing_movie else ""
                            ),
                            subscribe=subscribe,
                            mediainfo=mediainfo,
                        )
                        if preview_matched and preview_size and not preview_decision[0]:
                            logger.info(
                                f"电影 {mediainfo.title} 候选解锁前跳过："
                                f"{preview_decision[1]}"
                            )
                            continue
                share_url = self._owner.resource_transfer._resolve_candidate_resource_url(
                    candidate_resources,
                    resource_index,
                    resource,
                    search_label,
                    log_prefix=f"[{search_label}][HDHIVE]",
                )
                if self._stop_requested():
                    break

                if not share_url:
                    continue

                if not self._owner.resource_transfer._is_supported_resource(resource, share_url):
                    logger.debug(
                        f"跳过当前同步链不支持的资源类型 "
                        f"{self._owner.resource_transfer._supported_resource_type(resource, share_url)}：{resource_title}"
                    )
                    continue

                cloud_resource = self._owner.resource_transfer._is_cloud_resource_url(share_url)
                direct_cloud_resource = (
                        cloud_resource and self._owner.resource_transfer._is_direct_cloud_resource_url(share_url)
                )
                logger.info(
                    f"检查{self._owner.resource_transfer._resource_input_label(share_url)}：{resource_title} - "
                    f"{self._resource_log_reference(share_url)}"
                )

                try:
                    is_offline_resource = self._is_offline_url(share_url) or self._is_magnet_url(share_url)
                    if is_offline_resource:
                        if not manual_resources and self._is_offline_blacklisted(resource, share_url):
                            logger.debug(
                                f"离线资源命中黑名单跳过：{resource_title}"
                            )
                            continue

                    if is_offline_resource:
                        provider_name = self._owner.resource_transfer._prepare_magnet_resource(
                            resource, share_url
                        )
                        if not self._owner.resource_transfer._validate_resource_url(
                                share_url,
                                resource_label="Magnet 链接",
                                resource_title=resource_title,
                        ):
                            continue
                        provider_name = provider_name or resource_title
                        title_seasons = self._owner.resource_transfer._magnet_title_seasons(resource)
                        if title_seasons:
                            logger.debug(
                                f"电影离线资源包含剧集季数，已跳过：{provider_name}"
                            )
                            continue
                        matched, current_score = self._search_handler.select_file_candidate(
                            [{"name": provider_name, "size": resource.get("size") or 0}],
                            mediainfo,
                            subscribe,
                        )
                        if not matched:
                            logger.debug(f"Magnet 元数据未通过平台优先级规则：{provider_name}")
                            self._owner.resource_transfer._record_skip_candidate(
                                SkipStatsCollector.KIND_MISMATCH,
                                share_url,
                                reason="未通过平台优先级规则",
                                resource_title=resource_title,
                            )
                            continue
                        magnet_size = self._owner.upgrade_rule_scoring._resource_size_bytes(
                            resource.get("size")
                            or (resource.get("magnet_metadata") or {}).get("size")
                        )
                        magnet_upgrade = False
                        if upgrade_target_exists and movie_history_score >= 0:
                            magnet_upgrade, magnet_reason = self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                magnet_size,
                                old_file_name=(
                                    existing_movie[1] if existing_movie else ""
                                ),
                                subscribe=subscribe,
                                mediainfo=mediainfo,
                            )
                            if not magnet_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} Magnet 洗版候选跳过：{magnet_reason}"
                                )
                                continue
                        self._owner.metadata._set_task_phase(subscribe, "提交离线下载", 90, clear_search=True)
                        pending_key = self._queue_magnet_package(
                            resource, share_url, subscribe, mediainfo,
                            sub_key=sub_key if track_points else "",
                            upgrade=magnet_upgrade,
                            upgrade_mode=self._upgrade_mode,
                            upgrade_baseline={
                                "movie": {
                                    "score": movie_history_score,
                                    "size": movie_history_size,
                                }
                            } if magnet_upgrade else {},
                            transient_target=transient_target,
                        )
                        if not pending_key:
                            continue
                        self._owner.resource_transfer._append_magnet_pending_history(
                            history=history,
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            share_url=share_url,
                            cloud_dir=self._cloud_transfer_path.rstrip('/') or "/",
                            resource=resource,
                            rule_score=current_score,
                            upgrade=magnet_upgrade,
                            finalize_key=pending_key,
                        )
                        movie_transferred = True
                        transferred_count += 1
                        logger.info(f"Magnet 已进入下载后真实文件匹配：{provider_name}")
                        continue

                    selected_file_ids, selected_file_names = (
                        self._owner.resource_transfer._explicit_selection_values(resource)
                    )
                    share_files = self._owner.resource_transfer._validated_resource_files(
                        share_url,
                        resource_title=resource_title,
                        target_file_ids=selected_file_ids,
                        target_file_names=selected_file_names,
                        recognition_work_id=tmdb_id_of(mediainfo) or None,
                    )
                    if not share_files:
                        continue

                    require_media_match = self._owner.resource_transfer._is_cross_drive_resource(
                        resource, share_url
                    )
                    matched_file, current_score = self._owner.file_matching._match_movie_file(
                        share_files,
                        mediainfo,
                        subscribe,
                        resource_title,
                        require_media_match=require_media_match,
                        explicit_selection=bool(selected_file_ids or selected_file_names),
                    )

                    if not matched_file:
                        if require_media_match:
                            logger.debug(
                                f"跨盘分享内容未被平台识别为目标电影："
                                f"{mediainfo.title_year}，已跳过该资源"
                            )
                            reason = "跨盘分享内容未被平台识别为目标电影"
                        else:
                            logger.debug(
                                f"目标网盘分享未找到符合规则的电影文件："
                                f"{mediainfo.title_year}，已跳过该资源"
                            )
                            reason = "目标网盘分享未找到符合规则的电影文件"
                        self._owner.resource_transfer._record_skip_candidate(
                            SkipStatsCollector.KIND_MISMATCH,
                            share_url,
                            reason=reason,
                            resource_title=resource_title,
                        )
                        continue

                    if matched_file:
                        file_name = matched_file.get('name', '')
                        logger.debug(f"找到匹配文件：{file_name}")

                        is_upgrade = False
                        # 洗版模式下检查是否需要升级资源
                        upgrade_old_size = movie_history_size
                        dedup_movie_versions: list = []
                        if upgrade_target_exists and movie_history_score >= 0:
                            candidate_size = self._owner.upgrade_rule_scoring._resource_size_bytes(
                                matched_file.get("size")
                            )
                            should_upgrade, reason = self._owner.upgrade_rule_scoring._should_upgrade_candidate(
                                movie_history_score,
                                current_score,
                                movie_history_size,
                                candidate_size,
                                old_file_name=(
                                    existing_movie[1] if existing_movie else ""
                                ),
                                subscribe=subscribe,
                                mediainfo=mediainfo,
                            )
                            if not should_upgrade:
                                logger.info(
                                    f"电影 {mediainfo.title} 洗版候选跳过：{reason}"
                                )
                                continue
                            is_upgrade = True
                            logger.info(
                                f"电影 {mediainfo.title} 洗版：{reason}"
                            )
                            # 1.6.48 判重：收集目录内全部旧版本，主旧版本走 postprocess，其余成功后批量删除
                            if self._owner.episode_dedup._episode_dedup_active():
                                try:
                                    dedup_movie_versions = self._timed_sync_call(
                                        "cloud_scan",
                                        self._owner.episode_dedup._find_cloud_movie_versions,
                                        subscribe,
                                        mediainfo,
                                    ) or []
                                except Exception as dedup_err:
                                    logger.debug(
                                        f"电影 {mediainfo.title} 判重版本收集失败：{dedup_err}"
                                    )
                                    dedup_movie_versions = []

                        organize_enabled = getattr(self, "_organize_after_transfer", True)
                        staging_dir = self._owner.resource_transfer._resource_staging_dir(
                            share_url, matched_file
                        )
                        if organize_enabled:
                            save_dir, target_name = self._owner.naming._platform_target(
                                self._CLOUD_MEDIA_ROOT, subscribe, mediainfo, file_name
                            )
                            if is_upgrade and self._upgrade_mode == "coexist":
                                target_name = self._owner.upgrade_rule_scoring._coexist_target_name(
                                    target_name,
                                    file_name,
                                    self._owner.upgrade_rule_scoring._resource_size_bytes(matched_file.get("size")),
                                    matched_file.get("sha1") or "",
                                )
                        else:
                            save_dir = staging_dir
                            target_name = file_name
                        logger.info(
                            f"网盘源文件: {staging_dir}/{file_name}，"
                            f"整理到: {save_dir}/{target_name}"
                            if direct_cloud_resource
                            else f"跨盘源文件: {self._owner.resource_transfer._cloud_resource_path(share_url)}/{file_name}，"
                                 f"转存后整理到: {save_dir}/{target_name}"
                            if cloud_resource
                            else f"网盘转存暂存: {staging_dir}/{file_name}，"
                                 f"完成后移动到: {save_dir}/{target_name}"
                        )

                        if self._stop_requested():
                            break
                        self._owner.metadata._set_task_phase(
                            subscribe,
                            "登记网盘文件整理" if direct_cloud_resource else "转存匹配文件",
                            90,
                            clear_search=True,
                        )
                        # 1.6.48 判重：主旧版本走 postprocess 备份删除，其余成功后批量删除
                        # （管线 STRM 登记要 upgrade_old_*，先算好放进 item）。
                        primary_old = dedup_movie_versions[0] if dedup_movie_versions else None
                        extra_old_versions = dedup_movie_versions[1:] if len(dedup_movie_versions) > 1 else []
                        if primary_old:
                            upgrade_old_cloud_dir = str(primary_old.get("cloud_dir") or "")
                            upgrade_old_file_name = str(primary_old.get("file_name") or "")
                            upgrade_old_file_id = str(primary_old.get("file_id") or "")
                            if int(primary_old.get("file_size") or 0):
                                upgrade_old_size = int(primary_old.get("file_size") or 0)
                        else:
                            upgrade_old_cloud_dir = existing_movie[0] if existing_movie else ""
                            upgrade_old_file_name = existing_movie[1] if existing_movie else ""
                            upgrade_old_file_id = (
                                getattr(existing_movie[2], "id", "")
                                if existing_movie else ""
                            )
                        # Phase 3a 第 1 项：电影走同一条入库管线（单文件、
                        # 首个成功即停、不做落盘自检）。转存 + 字幕 + STRM
                        # 登记都在管线里；MP 整理由管线 organize 阶段触发
                        # （与旧 _transfer_file 尾巴同口径）。
                        pipeline_results = self._owner.transfer_ops._run_ingest_pipeline(
                            [{
                                "file": matched_file,
                                "target_name": target_name,
                                "target_dir": save_dir,
                                "episode": 1,
                                "score": current_score,
                                "is_upgrade": is_upgrade,
                                "resource": resource,
                                "subtitle_files": list(share_files or []),
                                "success_episodes": (
                                    [] if transient_target else [1]
                                ),
                                "notification_episodes": None,
                                "upgrade_old_cloud_dir": upgrade_old_cloud_dir,
                                "upgrade_old_file_name": upgrade_old_file_name,
                                "upgrade_old_file_id": upgrade_old_file_id,
                                "upgrade_old_size": upgrade_old_size,
                                # v1.29.1：额外旧版本随管线项进 pending 记录，
                                # 删除在后处理提交后（见 postprocess
                                # _apply_replacement_deletion）。
                                "dedup_extra_old_versions": extra_old_versions,
                                "dedup_new_score": current_score,
                                "dedup_new_size": self._owner.upgrade_rule_scoring._resource_size_bytes(matched_file.get("size")),
                            }],
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            season=None,
                            sub_key=sub_key,
                            share_url=share_url,
                            media_type="movie",
                            track_subscription=not transient_target,
                            transient_target=transient_target,
                            stop_on_first_success=True,
                            verify_landed=False,
                        )
                        pipeline_result = (
                            pipeline_results[0] if pipeline_results else {}
                        )
                        success = bool(pipeline_result.get("success"))
                        if not success:
                            if self._stop_requested():
                                logger.info(
                                    f"用户已停止转存：{mediainfo.title}"
                                )
                                break

                        # 记录历史
                        history_item = self._owner.transfer_details._build_transfer_history_item(
                            mediainfo=mediainfo,
                            subscribe=subscribe,
                            status=self._owner.resource_transfer._transfer_history_status(success, share_url),
                            share_url=share_url,
                            file_name=target_name,
                            source_file_name=file_name,
                            cloud_dir=save_dir,
                            resource=resource,
                            file_size=self._owner.upgrade_rule_scoring._resource_size_bytes(matched_file.get("size")),
                            source_sha1=matched_file.get("sha1") or "",
                            source_md5=matched_file.get("md5") or "",
                            rule_score=current_score,
                            upgrade=is_upgrade,
                        )
                        if history_item.get("status") == "失败":
                            failure_reason = str(
                                pipeline_result.get("failure_reason") or ""
                            ).strip()
                            if (
                                    not failure_reason
                                    and pipeline_result.get("transferred")
                                    and not pipeline_result.get("strm_path")
                                    and not pipeline_result.get("pending_key")
                            ):
                                # 转存成功但 STRM 登记失败（管线已把 success
                                # 置 False）：沿用旧文案，别让记录空着原因。
                                failure_reason = "文件已转存但后处理任务登记失败"
                            if failure_reason:
                                apply_failure(history_item, failure_reason)
                        history.append(history_item)

                        if success:
                            transferred_count += 1
                            movie_transferred = True
                            movie_history_score = current_score
                            self._owner.metadata._set_task_phase(subscribe, "登记文件后处理", 95)
                            # STRM 登记已在管线 confirm 阶段完成；转存成功但
                            # 登记失败时管线已把 success 置 False，不会走到这里。
                            pending_key = pipeline_result.get("pending_key") or ""
                            if pending_key:
                                history_item["finalize_key"] = pending_key
                                transition(
                                    history_item,
                                    STATUS_DOWNLOADING
                                    if self._is_offline_url(share_url)
                                    else STATUS_PROCESSING,
                                    stage=STAGE_TRANSFERRING,
                                    reason="movie_pending_finalize",
                                    task_id=history_item.get("record_id") or "",
                                )
                            # v1.29.1：额外旧版本不再这里删——转存成功只代表
                            # 新文件已落盘，确认入库在后处理提交
                            # （_commit_single_item）之后；删除走那里的替换
                            # 正式步骤（_apply_replacement_deletion），失败/
                            # 超时都保留旧版。额外旧版本随管线项进 pending
                            # 记录（dedup_extra_old_versions）。
                            # 媒体库通知已在管线 confirm 阶段发出（strm_path
                            # 生成即通知），这里不再重复通知。
                            logger.info(
                                f"{'已登记网盘电影整理' if direct_cloud_resource else '成功转存电影'}："
                                f"{mediainfo.title} (平台优先级:{current_score})"
                            )

                            # 收集转存详情用于通知：体积直接取转存结果，
                            # 不再从 history 里补（history 表在 v1.13.3 删除）。
                            if not pending_key:
                                movie_size = self._owner.upgrade_rule_scoring._resource_size_bytes(
                                    matched_file.get("size")
                                )
                                transfer_details.append({
                                    "type": "电影",
                                    "title": mediainfo.title,
                                    "year": mediainfo.year,
                                    "image": mediainfo.get_poster_image(),
                                    "file_name": target_name,
                                    "notification_kind": self._owner.transfer_details._derive_notification_kind(
                                        is_upgrade=is_upgrade,
                                        cross=history_item.get("transfer_mode") == "cross",
                                    ),
                                    "size": movie_size,
                                    "size_unknown": movie_size <= 0,
                                    "source": self._owner.transfer_details._detail_source_text(history_item),
                                    # v1.26.4：终态分支标记（MP 整理身份只在
                                    # 触发成功时才挂在处理项上）+ 完成通知
                                    # 模板要的类别 / 详情链接。
                                    "organized_via_mp": bool(
                                        (pipeline_result.get("item") or {}).get("mp_organize")
                                        or ((pipeline_result.get("item") or {}).get("file") or {}).get("mp_organize")
                                    ),
                                    "category": str(getattr(mediainfo, "category", "") or ""),
                                    "detail_link": str(getattr(mediainfo, "detail_link", "") or ""),
                                })

                            self._owner.transfer_details._record_download_history(
                                mediainfo=mediainfo,
                                subscribe=subscribe,
                                path=save_dir,
                                download_hash=matched_file.get("id"),
                                torrent_name=resource_title,
                                share_url=share_url,
                                torrent_description=file_name,
                            )

                            if not pending_key and not transient_target:
                                # v1.29.9 阶段 3：订阅仍有漏斗在途行时不结算
                                # （与电视流水线同一守卫），终态成功时由
                                # _finish_pending_subscription 回写。
                                if self._subscribe_has_active_funnel_rows(subscribe):
                                    logger.info(
                                        f"{mediainfo.title_year} 仍有漏斗在途行，"
                                        "本轮不结算订阅，终态成功后回写"
                                    )
                                else:
                                    self._subscribe_handler.check_and_finish_subscribe(
                                        subscribe=subscribe,
                                        mediainfo=mediainfo,
                                        success_episodes=[1],
                                    )
                                    if track_points and self._search_handler:
                                        self._search_handler.clear_subscription_budgets(sub_key)
                        else:
                            logger.error(f"转存失败：{mediainfo.title}")

                except Exception as e:
                    logger.error(
                        f"处理分享链接出错：{self._resource_log_reference(share_url)}，"
                        f"错误：{str(e)}"
                    )
                    continue

        except Exception as e:
            logger.error(f"处理电影订阅 {subscribe.name} 出错：{str(e)}")
        return transferred_count
