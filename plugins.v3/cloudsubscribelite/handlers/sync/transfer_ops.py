"""转存执行（主链路核心）。

从 handlers/sync/service.py 拆出，纯搬家：方法体逐字未改。
校验：scripts/verify_split_equivalence.py --before <拆分前> --after HEAD
"""

import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.config import global_vars
from app.core.context import MediaInfo
from app.log import logger

from ...core import (
    CloudDriveCapability,
    CloudDriveProvider,
    CloudFile,
    OwnerDelegator,
)
from ...core.error_text import sanitize_error_text
from ...core.episode_parse import parse_episodes_from_names
from ...core.share_link import canonicalize_share_url
from ...drive.scanner import get_driver_definitions



# 跨盘暂存文件引用计数（1.6.47 同源并发互删兜底）：
# 计数对象为暂存文件的绝对路径字符串，归零才允许删除。
_STAGING_REFS: Dict[str, int] = {}
_STAGING_REFS_LOCK = threading.Lock()

def _safe_optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None

def _staging_ref_key(staged_path: str, item: Any) -> str:
    """暂存文件绝对路径字符串（引用计数键）；缺路径或缺文件名时为空。"""
    name = str(getattr(item, "name", "") or "")
    base = str(staged_path or "").strip().rstrip("/")
    if not name or not base:
        return ""
    return f"{base}/{name}"

def _acquire_staging_ref(key: str) -> None:
    if not key:
        return
    with _STAGING_REFS_LOCK:
        _STAGING_REFS[key] = _STAGING_REFS.get(key, 0) + 1

def _release_staging_ref(key: str) -> int:
    """归零返回 0（可删）；否则返回剩余引用数（必须跳过删除）。"""
    if not key:
        return 0
    with _STAGING_REFS_LOCK:
        remaining = _STAGING_REFS.get(key, 0) - 1
        if remaining > 0:
            _STAGING_REFS[key] = remaining
            return remaining
        _STAGING_REFS.pop(key, None)
        return 0


class TransferOpsMixin(OwnerDelegator):
    """转存执行：秒传/跨盘/失败记录/结果核验/暂存清理。"""


    def _filter_entry_dedup_items(
            self,
            matched_items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe,
            season: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
        """入口预检（Phase 3a 第 2 项）：已在库的项直接跳过。

        复用 ``_collect_episode_versions``（一次季目录列目录 + 基线
        并集，走 ``_season_listing_cache``）+ ``_episode_dedup_decision``；
        决策 skip 的项不下载不转存，返回的 results 里 success=True、
        skip_history=True，调用方按已处理计入（不记失败、不重试、
        不进落盘自检、不发转存通知）。proceed/replace 的项原样放行
        （replace 的旧版删除仍由 finalize/洗版路径按原有流程处理，
        这里只做入口跳过）。
        判重关闭、查不到版本、查库异常时全部放行（fail-open）；
        最终兜底仍是 trigger_mp_organize 内的 MP 查库（第 12 项）。
        返回 (kept_items, {原下标: 跳过结果})。
        """
        items = list(matched_items or [])
        if not items:
            return items, {}
        try:
            if not self._owner.episode_dedup._episode_dedup_active():
                return items, {}
        except Exception as error:
            logger.debug(f"入口判重开关不可用（放行全部）：{error}", exc_info=True)
            return items, {}
        try:
            versions = self._owner.episode_dedup._collect_episode_versions(
                subscribe, mediainfo, season
            )
        except Exception as error:
            logger.debug(f"入口判重查库失败（放行全部）：{error}", exc_info=True)
            return items, {}
        if not versions:
            return items, {}
        kept: List[Dict[str, Any]] = []
        skipped: Dict[int, Dict[str, Any]] = {}
        for index, item in enumerate(items):
            try:
                episode_no = int(item.get("episode"))
            except (TypeError, ValueError):
                kept.append(item)
                continue
            if episode_no not in versions:
                kept.append(item)
                continue
            file_dict = item.get("file") or {}
            try:
                new_size = int(file_dict.get("size") or 0)
            except (TypeError, ValueError):
                new_size = 0
            try:
                new_score = int(
                    item.get("score") or item.get("new_score") or 0
                )
            except (TypeError, ValueError):
                new_score = 0
            action, _old, reason = self._owner.episode_dedup._episode_dedup_decision(
                subscribe,
                mediainfo,
                season,
                episode_no,
                new_score,
                new_size,
                versions,
                new_file_name=str(file_dict.get("name") or ""),
            )
            if action != "skip":
                kept.append(item)
                continue
            logger.info(
                f"{getattr(mediainfo, 'title', '')} "
                f"S{int(season):02d}E{episode_no:02d} 入口判重跳过：{reason}"
            )
            skipped[index] = {
                "item": item,
                "file_id": str(file_dict.get("id") or ""),
                "success": True,
                "pending_key": "",
                "failure_reason": "",
                "skip_history": True,
                "dedup_reason": reason,
                # 管线 confirm 阶段不给跳过项做落盘自检（没转存），landed
                # 恒为 None，与 _run_ingest_pipeline 的结果口径一致。
                "landed": None,
            }
        return kept, skipped

    def _transfer_episode_items(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """在同一任务内拆批完成全部剧集转存（Phase 3a 第 1 项：薄封装，走
        ``_run_ingest_pipeline``，TV 口径：入口判重 + 补满缺集 + 落盘确认）。"""
        return self._run_ingest_pipeline(
            matched_items,
            mediainfo=mediainfo,
            subscribe=subscribe,
            season=season,
            sub_key=sub_key,
            share_url=share_url,
            media_type="tv",
            track_subscription=track_subscription,
            transient_target=transient_target,
            stop_on_first_success=False,
            verify_landed=True,
        )

    def _wait_transfer_batch_interval(self) -> bool:
        """在任务内批次之间等待，并允许停止请求及时中断。"""
        deadline = time.monotonic() + self._batch_interval
        while time.monotonic() < deadline:
            if self._stop_requested():
                return False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

    def _ingest_stage_transfer(
            self,
            selected_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            season: int,
            organize_enabled: bool,
            rename_items: Dict[str, Any],
            cloud_resource: bool,
            direct_cloud_resource: bool,
            file_ids: List[str],
    ) -> Tuple[List[Dict[str, Any]], List[str], bool]:
        """管线 stage（落盘/暂存）：同盘直存 / 跨盘下载 / 批量转存三路。

        Phase 3a 第 1 项：从 ``_transfer_episode_batch`` 原样移出，只改
        签名与返回值。返回 (processed_items, success_ids, cross_batch)。
        """
        try:
            source_provider = self._owner.resource_transfer._resource_provider_for_url(share_url)
            provider_key = getattr(source_provider, "key", "") or getattr(
                self._cloud_drive, "key", "default"
            )
            if not cloud_resource:
                self._ensure_share_transfer_available(provider_key)
            # 手动资源标记保存在匹配项的 resource 元数据中；这里不能引用不存在的 resources。
            is_manual_cross = any(
                bool(
                    (item.get("resource") or {}).get("source") == "manual"
                    or (item.get("resource") or {}).get("is_cross")
                    or (item.get("resource") or {}).get("_manual")
                    or item.get("source") == "manual"
                    or item.get("is_cross")
                    or item.get("_manual")
                )
                for item in selected_items
            )
            cross_batch = bool(
                (self._cross_transfer_enabled or is_manual_cross) and source_provider
                and self._cloud_drive and source_provider.key != self._cloud_drive.key
            )
            if cross_batch:
                parent_task_id, task_stop_event = self._current_task_context()
                source_abort_event = threading.Event()

                def batch_stop_requested() -> bool:
                    return bool(
                        global_vars.is_system_stopped
                        or self._stop_requested()
                        or self._sync_paused()
                        or source_abort_event.is_set()
                        or (task_stop_event and task_stop_event.is_set())
                    )

                def transfer_one(item: Dict[str, Any]) -> Tuple[str, Optional[bool]]:
                    file_id = str(item["file"]["id"])
                    if batch_stop_requested():
                        return file_id, None
                    try:
                        item_staging_dir = self._owner.resource_transfer._resource_staging_dir(
                            str(item["file"].get("url") or share_url), item["file"]
                        )
                        save_target_name = (
                            item["target_name"] if organize_enabled else item["file"].get("name")
                        )
                        # 1.35.0：开关开时先在调用方算好媒体库目标路径（这里
                        # 手上有 mediainfo/subscribe/season，_transfer_file_impl
                        # 里拿不到），交给下面的 download_only 分支自己整理。
                        # 算不出来就留空，_transfer_file_impl 会回落 MP 整理。
                        if organize_enabled and self._organize_after_upload_enabled():
                            try:
                                # _ingest_stage_transfer 没有 subscribe（签名
                                # 只到 mediainfo/season）；naming 对 subscribe=None
                                # 有降级路径（跳过订阅级自定义词与年份覆盖），
                                # 分类仍按 mediainfo 规则算。
                                organize_dir, organize_name = self._owner.naming._platform_target(
                                    str(getattr(self, "_p115_media_path", "") or ""),
                                    None,
                                    mediainfo,
                                    str(item["file"].get("name") or ""),
                                    season,
                                    item.get("episode") or item["file"].get("episode"),
                                )
                            except Exception as error:  # noqa: BLE001 - 算不出按回落
                                logger.debug(
                                    f"计算整理目标路径失败（回落 MP 整理）：{error}",
                                    exc_info=True,
                                )
                                organize_dir, organize_name = "", ""
                            if organize_dir:
                                item["file"]["_organize_target"] = organize_dir
                                item["file"]["_organize_name"] = (
                                    organize_name or save_target_name
                                )
                        success = self._transfer_file(
                            str(item["file"].get("url") or share_url),
                            item["file"],
                            item_staging_dir if not organize_enabled else self._cloud_transfer_path,
                            save_target_name,
                            str(item["file"].get("sha1") or ""),
                            parent_task_id=parent_task_id,
                            stop_requested=batch_stop_requested,
                            media_type=getattr(getattr(mediainfo, "type", None), "name", ""),
                            media={
                                "tmdb_id": getattr(mediainfo, "tmdb_id", None),
                                "media_type": getattr(getattr(mediainfo, "type", None), "name", ""),
                                "season": season,
                                "year": getattr(mediainfo, "year", None),
                                "media_title": getattr(mediainfo, "title", ""),
                                # 同步时 mediainfo 自带的海报地址：下载页
                                # 实时查 TMDB 取不到时用它兜底。
                                "poster": (
                                    mediainfo.get_poster_image()
                                    if getattr(mediainfo, "get_poster_image", None)
                                    else ""
                                ) or "",
                                # 目标文件名里有 SxxExx 时直接取集号；裸文件名
                                # （如 18.mp4）解析不到，靠上层 target_episodes。
                                "target_episodes": sorted(parse_episodes_from_names(
                                    [save_target_name], season
                                )),
                            },
                        )
                    except Exception as error:
                        if batch_stop_requested():
                            return file_id, None
                        error_text = str(error)
                        if any(marker in error_text for marker in (
                                "封禁转存", "风控", "未返回下载地址",
                                "No space left on device", "磁盘可用空间不足",
                        )):
                            source_abort_event.set()
                            logger.error(
                                f"跨盘转存批次已熔断：{item['target_name']}，{error_text}"
                            )
                            return file_id, False
                        logger.error(
                            f"跨盘转存文件失败：{item['target_name']}，{error}"
                        )
                        return file_id, False
                    if not success and batch_stop_requested():
                        return file_id, None
                    return file_id, success

                provider_limits = [3]
                for provider in (source_provider, self._cloud_drive):
                    limit = int(
                        getattr(getattr(provider, "policy", None), "max_concurrency", 1)
                        or 1
                    )
                    provider_limits.append(limit)
                worker_count = min(len(selected_items), *provider_limits)
                outcomes: Dict[str, bool] = {}
                executor = ThreadPoolExecutor(
                    max_workers=max(1, worker_count),
                    thread_name_prefix="cloudsubscribe-file-download",
                )
                futures = {
                    executor.submit(transfer_one, item): str(item["file"]["id"])
                    for item in selected_items
                }
                try:
                    for future in as_completed(futures):
                        try:
                            file_id, success = future.result()
                        except CancelledError:
                            continue
                        if success is not None:
                            outcomes[file_id] = success
                        if batch_stop_requested():
                            for pending in futures:
                                pending.cancel()
                finally:
                    executor.shutdown(wait=True, cancel_futures=True)
                processed_items = [
                    item for item in selected_items
                    if str(item["file"]["id"]) in outcomes
                ]
                success_ids = [
                    file_id for file_id, success in outcomes.items() if success
                ]
            elif direct_cloud_resource:
                processed_items = selected_items
                success_ids = file_ids
            else:
                processed_items = selected_items
                pre_existing_ids = set()
                share_transferred = False
                if not organize_enabled and hasattr(self._share_transfer, "transfer_share"):
                    try:
                        logger.info(
                            f"转存后整理已关闭，执行整包分享转存以保留母文件夹与目录结构：{share_url} -> {self._cloud_transfer_path}"
                        )
                        share_success = bool(self._timed_sync_call(
                            "share_transfer",
                            self._share_transfer.transfer_share,
                            share_url=share_url,
                            save_path=self._cloud_transfer_path,
                        ))
                        if share_success:
                            success_ids = list(file_ids)
                            failed_ids = []
                            share_transferred = True
                    except Exception as share_err:
                        logger.warning(f"整包分享转存未成功，将尝试按文件列表转存：{share_err}")

                if not share_transferred:
                    # 确定每个文件转存的目标路径：关闭整理时按母文件夹保留层级，开启整理时使用统一转存根目录
                    def _item_target_dir(file_dict: Dict[str, Any]) -> str:
                        if not organize_enabled:
                            return self._owner.resource_transfer._resource_staging_dir(str(file_dict.get("url") or share_url), file_dict)
                        return self._cloud_transfer_path

                    # 本地快照缓存，避免多集同目录时重复列目录
                    staging_snapshots: Dict[str, Tuple[bool, Dict[str, Any]]] = {}

                    def _get_snapshot(dir_path: str) -> Tuple[bool, Dict[str, Any]]:
                        if dir_path not in staging_snapshots:
                            staging_snapshots[dir_path] = self._owner.postprocess._cloud_directory_snapshot(dir_path)
                        return staging_snapshots[dir_path]

                    # 转存前预检：若对应目标目录已存在待转存文件，直接复用并跳过重复转存
                    for item in selected_items:
                        file_id = str(item["file"]["id"])
                        target_dir = _item_target_dir(item["file"])
                        staging_valid, staging_index = _get_snapshot(target_dir)
                        if not staging_valid or not staging_index:
                            continue
                        raw_name = str(item["file"].get("name") or "").strip()
                        file_size = int(item["file"].get("size") or 0)
                        file_sha1 = str(item["file"].get("sha1") or "").upper()
                        matched_staging_file = None
                        if raw_name and raw_name in staging_index:
                            matched_staging_file = staging_index[raw_name]
                        elif file_sha1:
                            matched_staging_file = next(
                                (f for f in staging_index.values() if
                                 str(getattr(f, "sha1", "") or "").upper() == file_sha1),
                                None
                            )
                        elif file_size > 0:
                            matched_staging_file = next(
                                (f for f in staging_index.values() if
                                 int(getattr(f, "size", 0) or 0) == file_size and
                                 raw_name and getattr(f, "name", "").startswith(Path(raw_name).stem)),
                                None
                            )
                        if matched_staging_file:
                            pre_existing_ids.add(file_id)
                            item["file"]["staging_name"] = matched_staging_file.name
                            logger.debug(
                                f"转存目录已存在目标资源，复用并跳过重复转存：{target_dir}/{matched_staging_file.name}"
                            )

                    remaining_file_ids = [fid for fid in file_ids if fid not in pre_existing_ids]
                    if remaining_file_ids:
                        # 按目标保存目录分组转存（关闭整理时按母文件夹分目录，开启整理时统一为转存根目录）
                        dir_groups: Dict[str, List[str]] = {}
                        for item in selected_items:
                            fid = str(item["file"]["id"])
                            if fid in pre_existing_ids:
                                continue
                            save_dir = _item_target_dir(item["file"])
                            dir_groups.setdefault(save_dir, []).append(fid)

                        success_ids_set = set()
                        failed_ids_list = []
                        for dir_path, group_ids in dir_groups.items():
                            if not organize_enabled:
                                logger.debug(
                                    f"整理关闭：分目录转存 {len(group_ids)} 个文件 -> {dir_path}"
                                )
                            grp_success, grp_failed = self._timed_sync_call(
                                "share_transfer",
                                self._share_transfer.transfer_files_batch,
                                share_url=share_url,
                                file_ids=group_ids,
                                save_path=dir_path,
                                batch_size=self._batch_size,
                                batch_interval=self._batch_interval,
                                risk_cooldown=self._transfer_risk_cooldown,
                                rename_items=rename_items,
                            )
                            success_ids_set.update(grp_success or [])
                            failed_ids_list.extend(grp_failed or [])
                        success_ids = list(success_ids_set)
                        failed_ids = failed_ids_list
                    else:
                        success_ids, failed_ids = [], []

                    success_ids = list(success_ids or []) + list(pre_existing_ids)

                    # 对网盘转存返回失败的项进行转存目录复核自愈（防止网盘因已存在报错等返回假失败）
                    if failed_ids:
                        recheck_success = []
                        refreshed_snapshots: Dict[str, Dict[str, Any]] = {}
                        for fid in failed_ids:
                            target_item = next((it for it in selected_items if str(it["file"]["id"]) == fid), None)
                            if not target_item:
                                continue
                            check_path = _item_target_dir(target_item["file"])
                            if check_path not in refreshed_snapshots:
                                _, check_index = self._owner.postprocess._cloud_directory_snapshot(check_path)
                                refreshed_snapshots[check_path] = check_index or {}
                            check_index = refreshed_snapshots[check_path]
                            t_name = str(target_item.get("target_name") or "")
                            r_name = str(target_item["file"].get("name") or "")
                            if check_index and (
                                    (t_name and t_name in check_index) or (r_name and r_name in check_index)
                            ):
                                recheck_success.append(fid)
                                logger.debug(f"转存虽返回失败但转存目录已核验到文件，自愈恢复：{t_name or r_name}")
                        if recheck_success:
                            success_ids.extend(recheck_success)
                            recheck_set = set(recheck_success)
                            failed_ids = [fid for fid in failed_ids if fid not in recheck_set]

                if (
                        failed_ids and not success_ids
                        and bool(getattr(
                    self._share_transfer, "transfer_risk_blocked", False
                ))
                ):
                    self._activate_share_transfer_cooldown(provider_key)
        except Exception as error:
            message = str(error)
            if any(marker in message.lower() for marker in (
                    "风控", "封禁", "受限", "频繁", "rate limit", "too many", "429",
            )):
                self._activate_share_transfer_cooldown(locals().get("provider_key", "default"))
            raise

        return processed_items, success_ids, cross_batch

    def _ingest_stage_organize(
            self,
            processed_items: List[Dict[str, Any]],
            success_id_set: set,
            mediainfo: MediaInfo,
            season: int,
            share_url: str,
            cross_batch: bool,
            direct_cloud_resource: bool,
            organize_enabled: bool,
            media_type: str = "tv",
    ) -> set:
        """管线 organize（MP 整理 + 伴随字幕）。

        Phase 3a 第 1 项：从 ``_transfer_episode_batch`` 原样移出。MP 整理
        只在同盘直存且整理开启时触发（跨盘、直链不触发，与旧口径一致；
        电影同样触发——电影今天经 ``_transfer_file_impl`` 尾巴触发 MP 整理，
        管线只是把它收进同一处）。字幕调用按 media_type 分支：TV 传季集，
        电影不传季集（单电影字幕匹配口径不变）。
        返回已接手 MP 整理的 file_id 集合。
        """
        # P1：同盘 115 直存成功后立即交 MP 整理，记录进 MP 媒体整理；
        # 跨盘、直链、整理关闭时不触发（跨盘是 P2 的事）。
        mp_organized_ids = set()
        if not cross_batch and not direct_cloud_resource and organize_enabled:
            mp_organized_ids = self._owner.mp_organize._trigger_mp_organize_for_batch(
                processed_items, success_id_set,
                mediainfo=mediainfo, season=season,
            )
        transferred_subtitle_ids = set()
        for item in processed_items:
            if str(item["file"]["id"]) not in success_id_set:
                continue
            subtitle_files = item.get("subtitle_files") or []
            if media_type == "tv":
                subtitle_input_files = [item["file"], *subtitle_files]
                sub_season, sub_episode = season, item.get("episode")
            else:
                # 电影：subtitle_files 就是整包分享文件列表（含视频本身），
                # 原样传（与 movie.py 旧调用一致）；季集不传，避免单电影
                # 字幕匹配被当成按集匹配。
                subtitle_input_files = list(subtitle_files or [item["file"]])
                sub_season, sub_episode = None, None
            item["subtitles"] = self._owner.subtitles._transfer_companion_subtitles(
                share_url=str(item["file"].get("url") or share_url),
                files=subtitle_input_files,
                video_file=item["file"],
                target_video_name=item["target_name"],
                media_type=media_type,
                season=sub_season,
                episode=sub_episode,
                transferred_ids=transferred_subtitle_ids,
            ) if subtitle_files else []
        return mp_organized_ids

    def _ingest_stage_confirm(
            self,
            processed_items: List[Dict[str, Any]],
            success_id_set: set,
            mp_organized_ids: set,
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
    ) -> List[Dict[str, Any]]:
        """管线 confirm 前半（STRM 登记 + 结果组装）。

        Phase 3a 第 1 项：从 ``_transfer_episode_batch`` 原样移出。结果比
        旧版多两个字段：``strm_path``（即时生成的 STRM 路径，没有为空）与
        ``transferred``（文件本身是否转存成功，区别于“转存成功但 STRM 登记
        失败”——电影入口靠它区分两种失败）。落盘确认（verify_landed）不在
        这里，在 ``_run_ingest_pipeline`` 的批后统一做。
        """
        batch_strm_results = self._owner.strm_ops._generate_or_queue_strm_batch(
            [
                {
                    "result_key": str(item["file"]["id"]),
                    "share_url": str(item["file"].get("url") or share_url),
                    "cloud_dir": item["target_dir"],
                    "file_name": item["target_name"],
                    "staging_dir": self._owner.resource_transfer._resource_staging_dir(
                        str(item["file"].get("url") or share_url), item["file"]
                    ),
                    "staging_name": (
                            item["file"].get("staging_name")
                            or item["file"]["name"]
                    ),
                    "source_sha1": item["file"].get("sha1"),
                    "file_size": item["file"].get("size") or 0,
                    "success_episodes": item.get(
                        "success_episodes",
                        [] if item.get("is_upgrade") or not track_subscription
                        else [item["episode"]],
                    ),
                    "notification_episodes": item.get(
                        "notification_episodes", [item["episode"]]
                    ),
                    "upgrade": item.get("is_upgrade"),
                    "upgrade_mode": self._upgrade_mode,
                    "upgrade_old_cloud_dir": item.get("upgrade_old_cloud_dir"),
                    "upgrade_old_file_name": item.get("upgrade_old_file_name"),
                    "upgrade_old_file_id": item.get("upgrade_old_file_id"),
                    "upgrade_old_size": item.get("upgrade_old_size") or 0,
                    # v1.29.1：额外旧版本随管线项进 pending 记录，删除在
                    # 后处理提交后（postprocess._apply_replacement_deletion）。
                    "dedup_extra_old_versions": item.get("dedup_extra_old_versions") or [],
                    "dedup_new_score": item.get("dedup_new_score") or 0,
                    "dedup_new_size": item.get("dedup_new_size") or 0,
                    "subtitles": item.get("subtitles") or [],
                    # Phase 4 第 6 项：同盘批量触发的 MP 整理身份随项进
                    # pending 记录（_build_pending_record 原样收下），
                    # finalize 改走 MP 轮询、不再 2 次定位失败就判死。
                    # v1.18.1：跨盘尾巴与同盘单文件尾巴把身份写在内层
                    # file_item 上（外层没有），这里读不到外层就读内层；
                    # 否则跨盘项进 pending 时丢身份，finalize 仍按老规则
                    # 2 次定位失败就 abort（天才谋杀法 2026 实测）。
                    "mp_organize": (
                        item.get("mp_organize")
                        or (item.get("file") or {}).get("mp_organize")
                    ),
                    # v1.29.7 阶段 1：手动提交的漏斗归属随订阅对象下沉
                    # （_do_sync 里按 funnel_route 置位），随项进 pending
                    # 记录；finalize 边界按此标记入队整理队列。
                    "funnel_route": bool(getattr(subscribe, "_funnel_route", False)),
                    # v1.29.8 阶段 2：TG 来源标识随项进 pending 记录与漏斗行
                    # （_do_sync 里随订阅对象下沉）；入队时 source 即此 via。
                    "discovered_via": str(getattr(subscribe, "_discovered_via", "") or ""),
                    # v1.29.9 阶段 3：生产者标记随项进漏斗行（subscribe /
                    # upgrade / manual）；入队时 source 取 subscribe:<id>，
                    # 同目录 STRM 分支靠它区分订阅轮次项与手动同目录项。
                    "funnel_producer": str(getattr(subscribe, "_funnel_producer", "") or ""),
                    "skip_history": bool(
                        (item.get("resource") or {}).get("skip_history")
                        or str(item["file"]["id"]) in mp_organized_ids
                        or item["file"].get("_mp_organized")
                    ),
                }
                for item in processed_items
                if str(item["file"]["id"]) in success_id_set
            ],
            mediainfo,
            subscribe_id=(
                getattr(subscribe, "id", None) if track_subscription else None
            ),
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=(
                self._serialize_pending_target_subscribe(subscribe)
                if transient_target else None
            ),
        )
        results = []
        for item in processed_items:
            file_id = str(item["file"]["id"])
            success = file_id in success_id_set
            transferred = file_id in success_id_set
            strm_path, pending_key = batch_strm_results.get(file_id, (None, ""))
            if success and not strm_path and not pending_key:
                logger.error(
                    f"文件已转存但后处理任务登记失败：{item['target_name']}"
                )
                success = False
            if success and strm_path:
                self._media_server_notifier.notify(
                    path=strm_path,
                    mediainfo=mediainfo,
                    file_name=item["target_name"],
                )
            results.append({
                "item": item,
                "file_id": file_id,
                "success": success,
                "pending_key": pending_key,
                "strm_path": strm_path,
                "transferred": transferred,
                "failure_reason": (
                    "" if success else (
                        # 转存成功但 STRM 登记失败：不是转存失败，别让
                        # 调用方按转存失败重试（电影入口靠这句区分）。
                        "文件已转存但后处理任务登记失败"
                        if transferred and not strm_path and not pending_key
                        else self._transfer_failure_reason(file_id)
                    )
                ),
            })
        return results

    def _transfer_episode_batch(
            self,
            matched_items: List[Dict[str, Any]],
            share_url: str,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            track_subscription: bool = True,
            transient_target: bool = False,
            media_type: str = "tv",
    ) -> List[Dict[str, Any]]:
        """执行一个转存批次及对应后处理（Phase 3a 第 1 项：管线编排）。

        本体只剩编排：stage → organize → confirm，三个阶段见
        ``_ingest_stage_*``。media_type="movie" 时走同一条管线（电影
        入口用；字幕与季集按 media_type 分支，见 organize 阶段）。
        """
        selected_items = list(matched_items or [])
        if not selected_items:
            return []
        if self._stop_requested():
            return []

        file_ids = [str(item["file"]["id"]) for item in selected_items]
        cloud_resource = self._owner.resource_transfer._is_cloud_resource_url(share_url)
        direct_cloud_resource = (
                cloud_resource and self._owner.resource_transfer._is_direct_cloud_resource_url(share_url)
        )
        organize_enabled = getattr(self, "_organize_after_transfer", True)
        rename_items = {}
        for item in selected_items:
            file_item = item["file"]
            item_url = str(file_item.get("url") or share_url).strip()
            rename_items[str(file_item["id"])] = {
                "sha1": file_item.get("sha1"),
                "target_name": (
                    None if (not organize_enabled or self._is_offline_url(item_url)) else item["target_name"]
                ),
                "url": item_url,
            }
        processed_items, success_ids, cross_batch = self._ingest_stage_transfer(
            selected_items, share_url, mediainfo, season,
            organize_enabled, rename_items, cloud_resource,
            direct_cloud_resource, file_ids,
        )
        success_id_set = {str(file_id) for file_id in (success_ids or [])}
        mp_organized_ids = self._ingest_stage_organize(
            processed_items, success_id_set, mediainfo, season, share_url,
            cross_batch, direct_cloud_resource, organize_enabled,
            media_type=media_type,
        )
        return self._ingest_stage_confirm(
            processed_items, success_id_set, mp_organized_ids, share_url,
            mediainfo, subscribe, season, sub_key,
            track_subscription=track_subscription,
            transient_target=transient_target,
        )

    def _run_ingest_pipeline(
            self,
            items: List[Dict[str, Any]],
            *,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            sub_key: str,
            share_url: str = "",
            media_type: str = "tv",
            track_subscription: bool = True,
            transient_target: bool = False,
            stop_on_first_success: bool = False,
            verify_landed: bool = True,
    ) -> List[Dict[str, Any]]:
        """一条入库管线（Phase 3a 第 1 项）。

        acquire（调用方已完成：搜索/匹配出 items）→ dedup gate（TV 入口
        预检，电影不做）→ stage（落盘/暂存）→ organize（MP 整理 + 伴随字
        幕）→ confirm（STRM 登记 + 落盘确认）。差异只用参数表达：批量 vs
        单文件（调用方传的 items 数量）、首个成功即停 vs 补满缺集
        （stop_on_first_success）、是否落盘确认（verify_landed）。
        电影走同一条管线：media_type="movie"、season 归一为 None（电影
        无季）、verify_landed=False（电影今天不做落盘自检）。重试不走管
        线（retry.py：记录级 media/target/failure 定制，显式重传单条记录）。

        结果每条都带 ``landed``：verify_landed 时为落盘自检结果（成功且
        非 pending 且非跳过才查；查不到实测数据时为 None），不 verify 时
        为 None。调用方（television.py）直接读它，不再另调一次自检。
        """
        items = list(items or [])
        if not items:
            return []
        if not share_url:
            share_url = str(((items[0] or {}).get("file") or {}).get("url") or "")
        all_items = list(items)
        if media_type != "tv":
            season = None
        skipped_results: Dict[int, Dict[str, Any]] = {}
        if media_type == "tv":
            kept_items, skipped_results = self._filter_entry_dedup_items(
                items, mediainfo, subscribe, season
            )
            items = kept_items
            if not items:
                return [
                    skipped_results[index] for index in sorted(skipped_results)
                ]
        # v1.25.0 Issue A：转存真正开始（判重门已过、确有文件要转）才补一
        # 条开始下载通知。链接解析失败、查库命中跳过、判重拦下的到不了
        # 这里；每调一次管线只发一条（同一批多个文件共一条）。
        notifications = getattr(self._owner, "notifications", None)
        send_started = getattr(
            notifications, "send_transfer_started_notification", None
        )
        if callable(send_started):
            send_started(
                mediainfo=mediainfo,
                subscribe=subscribe,
                source=str(getattr(subscribe, "_transfer_source", "") or "subscribe"),
                via=str(getattr(subscribe, "_discovered_via", "") or ""),
                items=items,
                media_type=media_type,
                season=season,
            )
        batch_size = self._transfer_task_batch_size
        batch_count = (len(items) + batch_size - 1) // batch_size
        if batch_count > 1:
            logger.info(
                f"匹配 {len(items)} 个文件，将按每批最多 {batch_size} 个"
                f"分 {batch_count} 批在当前任务内处理"
            )
        results: List[Dict[str, Any]] = []
        for batch_index, offset in enumerate(range(0, len(items), batch_size), 1):
            if self._stop_requested():
                break
            batch_items = items[offset:offset + batch_size]
            if batch_count > 1:
                logger.debug(
                    f"开始处理转存批次 {batch_index}/{batch_count}："
                    f"文件={len(batch_items)}"
                )
                self._owner.metadata._set_task_phase(
                    subscribe,
                    f"正在转存第 {batch_index}/{batch_count} 批剧集",
                    90 + int((batch_index / batch_count) * 4),
                )
            batch_results = self._transfer_episode_batch(
                batch_items,
                share_url,
                mediainfo,
                subscribe,
                season,
                sub_key,
                track_subscription=track_subscription,
                transient_target=transient_target,
                media_type=media_type,
            )
            results.extend(batch_results)
            if stop_on_first_success and any(
                    result.get("success") for result in batch_results
            ):
                break
            if self._stop_requested():
                break
            if batch_index < batch_count and not self._wait_transfer_batch_interval():
                break
        if verify_landed:
            # 1.6.52 落盘自检：批内非 pending 的成功条目共用一次季目录列目录
            # （复用判重缓存，不额外打网盘），拿到结果后再决定状态。原来在
            # television.py 里每轮调一次，现收进管线 confirm 阶段（同一轮
            # 一次调用，时机与原来一致：全部内批次跑完后）。
            verify_items = [
                {
                    "key": str(index),
                    "file_id": str(result.get("file_id") or ""),
                    "target_name": str(
                        (result.get("item") or {}).get("target_name") or ""
                    ),
                }
                for index, result in enumerate(results)
                # 入口判重跳过的项本轮没转存，不进落盘自检（库里已有版本的
                # 名字未必等于 target_name，自检会误判）。
                if result.get("success")
                and not result.get("pending_key")
                and not result.get("skip_history")
            ]
            verify_map = self._owner.landed_verify.verify_landed_items(
                subscribe, mediainfo, season, verify_items
            )
            for index, result in enumerate(results):
                result["landed"] = verify_map.get(str(index))
        else:
            for result in results:
                result["landed"] = None
        if not skipped_results:
            return results
        # 跳过结果按原下标拼回（中途停止导致批次结果不全时，未回来的
        # 项直接缺席，不错位）。
        by_file_id = {
            str(result.get("file_id") or ""): result for result in results
        }
        merged: List[Dict[str, Any]] = []
        for index, item in enumerate(all_items):
            if index in skipped_results:
                merged.append(skipped_results[index])
                continue
            file_id = str((item.get("file") or {}).get("id") or "")
            if file_id in by_file_id:
                merged.append(by_file_id[file_id])
        return merged

    def _organize_after_upload_enabled(self) -> bool:
        """1.35.0：上传到待整理目录后由本插件整理（默认关）。"""
        return bool(getattr(self, "_organize_after_upload", False))

    def _resolve_organize_target(
            self,
            file_item: Any,
            media: Any,
            target_name: str,
    ) -> Tuple[str, str]:
        """算媒体库目标路径（开关开时用；调用方没预算过就这里兜底）。

        1.35.0：历史重试 / 字幕等路径不经过 ``_ingest_stage_transfer``，
        拿不到预存的 ``_organize_target``，之前就整批回落 MP 整理（线上
        2026-11-25 实测：所有文件都进了转存目录、没进媒体库）。这里用
        ``file_item`` 自带的媒体身份拼最小 MediaInfo 再算一次。
        """
        if not self._organize_after_upload_enabled():
            return "", ""
        stashed_dir = str((file_item or {}).get("_organize_target") or "").strip()
        if stashed_dir:
            return stashed_dir, str(
                (file_item or {}).get("_organize_name") or target_name
            ).strip()
        try:
            from .utils import resolve_effective_mediainfo

            mediainfo = resolve_effective_mediainfo(None, media)
            if mediainfo is None:
                logger.warning(
                    "整理目标未解析：缺少媒体类型/标题，本次回落 MP 整理"
                )
                return "", ""
            season = media.get("season") if isinstance(media, dict) else None
            episode = (file_item or {}).get("episode")
            if episode in (None, "") and isinstance(media, dict):
                episode = media.get("episode")
            target_dir, final_name = self._owner.naming._platform_target(
                str(getattr(self, "_p115_media_path", "") or ""),
                None,
                mediainfo,
                str(target_name or ""),
                season,
                episode,
            )
        except Exception as error:  # noqa: BLE001 - 算不出按回落
            logger.warning(f"整理目标计算失败（回落 MP 整理）：{error}")
            return "", ""
        if not target_dir:
            logger.warning(
                "整理目标未解析：命名服务未给出目录，本次回落 MP 整理"
            )
            return "", ""
        return str(target_dir), str(final_name or target_name).strip()

    def _staging_root(self) -> str:
        """待整理目录：显式配置优先，空则回退 115 转存目录。"""
        text = str(getattr(self, "_organize_staging_path", "") or "").strip()
        if text:
            return text.rstrip("/")
        return str(getattr(self, "_p115_transfer_path", "") or "").strip().rstrip("/")

    def _organize_service(self):
        """取整理组件；宿主没接上时返回 None（调用方 fail-open）。"""
        plugin = getattr(self._owner, "_plugin", None)
        getter = getattr(plugin, "_get_component", None) if plugin is not None else None
        if not callable(getter):
            return None
        try:
            from ...core.services.organize import OrganizeService

            return getter(OrganizeService)
        except Exception as error:  # noqa: BLE001 - 取不到按不可用
            logger.debug(f"取整理组件失败：{error}", exc_info=True)
            return None

    def _organize_after_save(
            self,
            save_dir: str,
            file_name: str,
            media: Any = None,
            season: Any = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """同盘直存落盘后：开关开 → 本插件整理；否则交 MP 整理（1.35.3）。

        1.35.0 只接了跨盘 download_only 分支，同盘直存（115→115）仍走
        ``trigger_mp_organize``，线上表现为「文件都进了转存目录、没进媒体库」。
        这里把直存路径也接上：目标路径由 ``_resolve_organize_target`` 现算，
        算不出或整理失败才回落 MP 整理。
        """
        if self._organize_after_upload_enabled():
            item_hint = {"episode": (media or {}).get("episode")}
            target_dir, target_name = self._resolve_organize_target(
                item_hint, media, file_name,
            )
            service = self._organize_service() if target_dir else None
            if target_dir and service is not None:
                result = service.organize_one(
                    str(save_dir or ""), str(file_name or ""),
                    target_dir, target_name or str(file_name or ""),
                    episode=int((media or {}).get("episode") or 0),
                )
                if result.get("success"):
                    logger.info(
                        f"已由本插件整理入库：{result.get('target') or target_name}"
                    )
                    return True, {
                        "reason": "organized",
                        "target": str(result.get("target") or ""),
                    }
                return False, {
                    "reason": "organize_failed",
                    "message": str(result.get("message") or "整理失败"),
                }
        return self._owner.mp_organize.trigger_mp_organize(
            save_dir, file_name, mediainfo=media, season=season,
        )

    def _organize_uploaded_file(
            self,
            local_path: str,
            target_dir: str,
            target_name: str,
            episode: int = 0,
            task_key: str = "",
    ) -> Tuple[bool, Dict[str, Any]]:
        """上传到待整理目录 → 由本插件整理进媒体库（1.35.0）。

        与 ``trigger_mp_organize`` 同语义返回 ``(是否成功, 说明)``；失败时文件
        留在待整理目录（不再占 /downloads），下一轮重试可复用。
        """
        staging_root = self._staging_root()
        if not staging_root:
            return False, {
                "reason": "no_staging",
                "message": "待整理目录未配置：请填「待整理目录」或 115 转存目录",
            }
        local = PurePosixPath(str(local_path or "").strip())
        if not local.name:
            return False, {
                "reason": "no_source",
                "message": "本地文件路径为空，未上传待整理目录",
            }
        target_dir = str(target_dir or "").strip().rstrip("/")
        target_name = str(target_name or "").strip() or local.name
        if not target_dir:
            return False, {
                "reason": "no_target_dir",
                "message": "未解析到媒体库目标目录，文件未进媒体库",
            }
        drive = getattr(self, "_cloud_drive", None)
        if drive is None:
            return False, {
                "reason": "unavailable",
                "message": "网盘未就绪，文件留在本地待下轮重试",
            }
        staging_key = str(task_key or local.stem or "job")
        staging_key = staging_key.replace(":", "-").replace("/", "-")[:60]
        staging_dir = f"{staging_root}/{staging_key}"
        try:
            upload = drive.require(CloudDriveCapability.LOCAL_UPLOAD)
        except Exception as error:  # noqa: BLE001 - 能力缺失按不可用
            return False, {
                "reason": "unavailable",
                "message": f"目标网盘不支持本地上传：{error}",
            }
        try:
            uploaded = bool(upload.upload_file(local_path, staging_dir, target_name))
        except Exception as error:  # noqa: BLE001 - 上传异常按失败
            logger.debug(f"上传待整理目录异常：{error}", exc_info=True)
            uploaded = False
        if not uploaded:
            detail = str(getattr(upload, "last_error", "") or "").strip()
            return False, {
                "reason": "upload_failed",
                "message": f"上传待整理目录失败：{detail or '目标盘未返回原因'}",
            }
        logger.info(f"已上传待整理目录：{staging_dir}/{target_name}")
        service = self._organize_service()
        if service is None:
            return False, {
                "reason": "unavailable",
                "message": "整理组件未就绪，文件留在待整理目录",
            }
        result = service.organize_one(
            staging_dir, target_name, target_dir, target_name,
            episode=int(episode or 0),
        )
        if not result.get("success"):
            return False, {
                "reason": "organize_failed",
                "message": str(result.get("message") or "整理失败"),
            }
        if bool(getattr(self, "_organize_staging_cleanup", True)):
            try:
                service.cleanup_empty_dirs(staging_root)
            except Exception as error:  # noqa: BLE001 - 清理失败不影响整理结论
                logger.debug(f"清理待整理空目录失败：{error}", exc_info=True)
        return True, {
            "reason": "organized",
            "staging": f"{staging_dir}/{target_name}",
            "target": str(result.get("target") or ""),
        }

    def _transfer_file(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
            media: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """转存入口：直接执行原转存实现。

        v1.13.0 起同步作业账本已删除，不再认领作业/续租/记账；同一内容
        写同一目标的并发防护交给全局同步串行（sync_lock）与 MP 整理
        队列的源路径去重。全局暂停仍在批量入口（batch_stop_requested）
        检查，这里不重复拦截。
        """
        share_url = canonicalize_share_url(share_url)
        return self._transfer_file_impl(
            share_url, file_item, save_path, target_name, source_sha1,
            parent_task_id, stop_requested, media_type, media,
        )

    def _transfer_file_impl(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
            media: Optional[Dict[str, Any]] = None,
    ) -> bool:
        should_stop = stop_requested or self._stop_requested
        cloud_resource = self._owner.resource_transfer._is_cloud_resource_url(share_url)
        source = self._owner.resource_transfer._resource_provider_for_url(share_url)
        cross_provider = bool(
            source and self._cloud_drive and source.key != self._cloud_drive.key
        )
        if cross_provider:
            item_media_type = self._owner.resource_transfer._normalize_cross_transfer_media_type(
                file_item.get("media_type") or media_type
            )
            is_manual_override = bool(
                file_item.get("source") == "manual"
                or file_item.get("is_cross")
                or file_item.get("_manual")
            )
            if not is_manual_override:
                if item_media_type and item_media_type not in self._cross_transfer_media_types:
                    logger.debug(
                        f"跨盘转存跳过：{source.name} -> {self._cloud_drive.name}，"
                        f"媒体类型 {item_media_type} 未启用"
                    )
                    return self._fail_transfer(
                        file_item,
                        f"媒体类型 {item_media_type} 未启用跨盘转存",
                    )
            required = (
                    (self._cross_transfer_enabled or is_manual_override)
                    and self._cross_transfer_manager
                    and (
                            cloud_resource
                            or source.supports(CloudDriveCapability.SHARE_TRANSFER)
                    )
                    and source.supports(CloudDriveCapability.FILE_QUERY)
                    and source.supports(CloudDriveCapability.FILE_DOWNLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                    and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            if not required:
                # 逐项点名缺了哪块能力：以前这里只记一句日志就返回 False，
                # 重试读不到原因，记录只剩通用句。
                missing = []
                if not (self._cross_transfer_enabled or is_manual_override):
                    missing.append("跨盘转存未启用")
                if not self._cross_transfer_manager:
                    missing.append("跨盘任务管理器未就绪")
                if not (
                    cloud_resource
                    or source.supports(CloudDriveCapability.SHARE_TRANSFER)
                ):
                    missing.append(f"{source.name}不支持分享转存")
                if not source.supports(CloudDriveCapability.FILE_QUERY):
                    missing.append(f"{source.name}不支持文件查询")
                if not source.supports(CloudDriveCapability.FILE_DOWNLOAD):
                    missing.append(f"{source.name}不支持文件下载")
                if not self._cloud_drive.supports(
                    CloudDriveCapability.LOCAL_UPLOAD
                ):
                    missing.append(f"{self._cloud_drive.name}不支持本地上传")
                if not self._cloud_drive.supports(
                    CloudDriveCapability.FILE_QUERY
                ):
                    missing.append(f"{self._cloud_drive.name}不支持文件查询")
                detail = "、".join(missing) or "未知原因"
                logger.warning(
                    f"无法跨盘转存单个文件：{source.name} -> "
                    f"{self._cloud_drive.name}，{detail}"
                )
                return self._fail_transfer(
                    file_item, f"无法跨盘转存：{detail}"
                )
            if cloud_resource:
                item = self._owner.resource_transfer._cloud_file_from_dict(file_item)
                if not item.id:
                    logger.warning(f"无法跨盘整理文件：{source.name} 文件 ID 为空")
                    return self._fail_transfer(
                        file_item, "无法跨盘整理文件：源文件 ID 为空"
                    )
            else:
                source_file_id = str(file_item.get("id") or "").strip()
                if not source_file_id:
                    logger.warning(f"无法跨盘转存单个文件：{source.name} 文件 ID 为空")
                    return self._fail_transfer(
                        file_item, "无法跨盘转存单个文件：源文件 ID 为空"
                    )
                staged_path = self._cross_transfer_staging_path(source.key)
                source_share = source.require(CloudDriveCapability.SHARE_TRANSFER)
                try:
                    staged = source_share.transfer_file(
                        share_url=share_url, file_id=source_file_id,
                        save_path=staged_path,
                        target_name=file_item.get("name") or target_name,
                    )
                except Exception:
                    self._cleanup_cross_transfer_staging(source, "")
                    raise
                if not staged:
                    self._cleanup_cross_transfer_staging(source, "")
                    return self._fail_transfer(
                        file_item,
                        f"{source.name}分享转存失败：未返回暂存文件",
                    )
                source_files = source.require(CloudDriveCapability.FILE_QUERY)
                staged_name = file_item.get("name") or target_name
                item = None
                for attempt in range(10):
                    item = source_files.find_file(staged_path, staged_name)
                    if item or should_stop():
                        break
                    time.sleep(min(0.5 + attempt * 0.25, 2.0))
                if not item:
                    logger.warning(
                        f"跨盘临时文件尚未可见：{source.name} "
                        f"{staged_path}/{staged_name}"
                    )
                    self._cleanup_cross_transfer_staging(source, "")
                    return self._fail_transfer(
                        file_item,
                        f"跨盘临时文件尚未可见：{source.name} "
                        f"{staged_path}/{staged_name}",
                    )
                # 暂存文件已可见即登记引用：同路径兄弟任务在飞时 finally 不删（1.6.47）
                _acquire_staging_ref(_staging_ref_key(staged_path, item))
            if source_sha1 and not item.sha1:
                item = CloudFile(
                    item.id,
                    item.name,
                    False,
                    item.size,
                    source_sha1,
                    item.md5,
                    playback_values=item.playback_values,
                    native=item.native,
                )
            try:
                if not parent_task_id:
                    parent_task_id, _ = self._current_task_context()
                # 媒体身份只做展示用（下载页海报/季集）：显式参数优先，
                # file_item 里的同名字段次之；无归属订阅的独立任务靠它
                # 在下载页画出海报，否则只有文件名、没有媒体信息。
                effective_media = dict(media or {})
                for key in ("tmdb_id", "media_type", "season", "year", "media_title"):
                    if effective_media.get(key) in (None, ""):
                        value = file_item.get(key)
                        if value not in (None, ""):
                            effective_media[key] = value
                if not effective_media.get("media_type") and media_type:
                    effective_media["media_type"] = media_type
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source.key, item, self._cloud_drive.key, save_path, target_name,
                    fallback=True,
                    parent_task_id=parent_task_id,
                    media=effective_media or None,
                    # P2：跨盘只下载到本地，不向目标盘上传；下载完成后
                    # 下方触发 MP 整理，由 MP 把本地文件上传进媒体库。
                    download_only=True,
                )
                success = self._cross_transfer_manager.wait(
                    task["id"], cancel_check=should_stop
                )
                completed_task = next(
                    (
                        value for value in self._cross_transfer_manager.list()
                        if value.get("id") == task["id"]
                    ),
                    {},
                )
                if success:
                    result_name = str(
                        completed_task.get("result_file_name")
                        or target_name or item.name
                    ).strip()
                    if result_name:
                        file_item["staging_name"] = result_name
                    result_sha1 = str(
                        completed_task.get("result_sha1") or ""
                    ).strip()
                    result_md5 = str(
                        completed_task.get("result_md5") or ""
                    ).strip()
                    if result_sha1:
                        file_item["sha1"] = result_sha1
                    if result_md5:
                        file_item["md5"] = result_md5
                    result_size = int(
                        completed_task.get("result_file_size") or 0
                    )
                    if result_size > 0:
                        file_item["size"] = result_size
                    # P2：跨盘只下载到本地，上传交给 MP 整理。download_only
                    # 成功时文件在本地 /downloads；秒传命中（rapid/reuse）
                    # 时文件已在 115 /转存；两种都触发 MP 整理进媒体库。
                    # 整理未接受时本项记失败（文件不在媒体库，下一轮同步
                    # 重试；本地缓存复用，不重复下载）。
                    if not (
                            self._owner.mp_organize._mp_organize_enabled()
                            or self._organize_after_upload_enabled()
                    ):
                        logger.info(
                            f"MP 整理已关闭，跨盘文件留在本地待手动整理："
                            f"{result_name}"
                        )
                        return True
                    method = str(completed_task.get("method") or "")
                    if method == "download_only":
                        local_path = str(
                            completed_task.get("result_local_path") or ""
                        ).strip()
                        local_parts = PurePosixPath(local_path) if local_path else None
                        # 1.35.0：开关开且调用方算出了媒体库目标路径时，
                        # 上传到待整理目录并交给本插件整理；算不出目标路径
                        # 时回落 MP 整理（不因为一个新开关把整批任务弄死）。
                        organize_dir, organize_name = self._resolve_organize_target(
                            file_item, effective_media, result_name or target_name,
                        )
                        if organize_dir:
                            ok, mp_info = self._organize_uploaded_file(
                                local_path,
                                organize_dir,
                                organize_name or result_name,
                                episode=int(file_item.get("episode") or 0),
                                task_key=str(completed_task.get("id") or ""),
                            )
                            if ok:
                                file_item["_mp_organized"] = True
                                file_item["_self_organized"] = True
                                logger.info(
                                    f"已由本插件整理入库："
                                    f"{mp_info.get('target') or result_name}"
                                )
                                return True
                        elif local_parts and local_parts.name:
                            ok, mp_info = self._owner.mp_organize.trigger_mp_organize(
                                str(local_parts.parent), local_parts.name,
                                media=effective_media,
                            )
                        else:
                            ok, mp_info = False, {
                                "reason": "no_source",
                                "message": "MP 整理未触发：本地路径为空，文件未进媒体库",
                            }
                    else:
                        # 1.35.3：直存路径同样受开关控制（开关关时行为不变）。
                        ok, mp_info = self._organize_after_save(
                            save_path, result_name, effective_media,
                        )
                    file_item["_mp_organized"] = bool(ok)
                    if ok:
                        file_item["mp_organize"] = mp_info
                    else:
                        info = mp_info if isinstance(mp_info, dict) else {}
                        reason = info.get("reason")
                        if reason == "already_in_library":
                            # v1.12.16：该集已在媒体库，目标已达成，不记
                            # 失败、不重试；本地 /downloads 的这份是多余
                            # 的重复来源，后续由缓存清理处理。
                            # v1.12.17：显式置 _mp_organized，让这条记录
                            # 拿到 skip_history（与同盘批量对齐）；但不
                            # 置 mp_organize 身份——这次根本没触发 MP 整理，
                            # finalize 没有可轮询的对象，置了反而会被判
                            # “MP 整理未受理”而 abort。
                            file_item["_mp_organized"] = True
                            existing = info.get("existing_file")
                            logger.info(
                                f"该集已在媒体库，跳过 MP 整理：{result_name}"
                                + (
                                    f"，库里已有 {existing}"
                                    if existing
                                    else ""
                                )
                            )
                            return True
                        if reason == "no_target_dir":
                            failure = (
                                "MP 整理未触发：未解析到目标目录，"
                                "文件未进媒体库，下一轮重试"
                            )
                        elif reason == "disabled":
                            failure = (
                                "MP 整理已关闭：文件未进媒体库，下一轮重试"
                            )
                        elif reason == "not_accepted":
                            failure = (
                                "MP 整理未接受：文件未进媒体库，下一轮重试"
                            )
                        else:
                            failure = (
                                info.get("message")
                                or "MP 整理未接受：文件未进媒体库，下一轮重试"
                            )
                        self._remember_transfer_failure(file_item, failure)
                        return False
                if not success:
                    failure_reason = str(
                        completed_task.get("error")
                        or completed_task.get("message")
                        or ""
                    ).strip()
                    if str(completed_task.get("abort_reason") or "") == "rapid_miss":
                        failure_reason = failure_reason or "未命中秒传"
                    if not failure_reason:
                        # 任务字典里没留下原因时（比如任务中途丢失）也别让
                        # 记录空着：至少写清失败时卡在哪个阶段，否则历史页
                        # 上只剩一个光秃秃的「未分类失败」，无从查起。
                        phase = str(
                            completed_task.get("phase") or "unknown"
                        ).strip()
                        failure_reason = f"跨盘转存失败（阶段：{phase}）"
                    self._remember_transfer_failure(file_item, failure_reason)
                    if (
                            should_stop()
                            or completed_task.get("status") in {"canceled", "stopping"}
                    ):
                        logger.info(
                            f"跨盘转存已由用户停止：{source.name} -> "
                            f"{self._cloud_drive.name}"
                        )
                    elif str(completed_task.get("abort_reason") or "") == "rapid_miss":
                        logger.warning(
                            f"秒传未命中，已清理中继缓存并跳过该资源：{source.name} -> "
                            f"{self._cloud_drive.name}（中继缓存已删，SHA1 已记录，"
                            f"本次同步继续尝试后面的候选分享）"
                        )
                    else:
                        logger.error(
                            f"跨盘转存失败：{source.name} -> {self._cloud_drive.name}，"
                            f"阶段={completed_task.get('phase') or 'unknown'}，"
                            f"原因={completed_task.get('error') or completed_task.get('message') or '未知错误'}"
                        )
                return success
            finally:
                if not cloud_resource:
                    # 只清理分享转存产生的源盘暂存文件，绝不删除用户选择的网盘文件。
                    # 传入 staged_path 供引用计数归零判定（1.6.47）。
                    self._cleanup_cross_transfer_staging(source, staged_path, item)
        if source and not source.supports(CloudDriveCapability.SHARE_TRANSFER):
            logger.warning(
                f"{source.name}暂不支持分享转存，已跳过该资源：{share_url}"
            )
            self._remember_transfer_failure(file_item, "当前网盘不支持分享转存")
            return False
        service = source.require(CloudDriveCapability.SHARE_TRANSFER) if source else self._share_transfer
        target_check_name = target_name or file_item.get("name")
        success = bool(service.transfer_file(
            share_url=share_url, file_id=file_item.get("id"),
            save_path=save_path, target_name=target_name,
            source_sha1=source_sha1,
        ))
        if not success and target_check_name:
            t_valid, t_index = self._owner.postprocess._cloud_directory_snapshot(save_path)
            if t_valid and (
                    target_check_name in t_index
                    or (target_name and target_name in t_index)
                    or (file_item.get("name") and file_item.get("name") in t_index)
            ):
                logger.debug(f"单文件转存返回失败但转存目录复核已存在，自愈复用：{save_path}/{target_check_name}")
                return True
        if not success:
            self._remember_transfer_failure(file_item, "分享转存失败")
            return False
        # P1：单文件同盘直存（历史重试、手动单传）成功后同样交 MP 整理；
        # 字幕等非视频文件由 trigger 内部的扩展名守卫拦下。
        if self._owner.mp_organize._mp_organize_enabled():
            saved_name = str(target_name or file_item.get("name") or "")
            ok, mp_info = self._owner.mp_organize.trigger_mp_organize(
                save_path, saved_name, media=media,
                season=(
                    media.get("season") if isinstance(media, dict) else None
                ),
            )
            if ok and mp_info:
                # 触发成功才记身份：finalize 改走 MP 跟踪，不再 2 分钟判失败；
                # 触发失败不影响本次直存结果（文件已在 115 /转存/，老定位找得到）。
                file_item["mp_organize"] = mp_info
                file_item["_mp_organized"] = True
        return success

    def _ensure_share_transfer_available(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            remaining = self._share_transfer_blocked_until.get(key, 0.0) - time.monotonic()
        if remaining > 0:
            raise RuntimeError(f"{key} 分享转存处于风控冷却期，剩余 {int(remaining)} 秒")

    def _activate_share_transfer_cooldown(self, provider_key: str) -> None:
        key = str(provider_key or "default").lower()
        with self._share_transfer_risk_lock:
            self._share_transfer_blocked_until[key] = max(
                self._share_transfer_blocked_until.get(key, 0.0),
                time.monotonic() + self._transfer_risk_cooldown,
            )
        logger.warning(
            f"{key} 分享转存检测到风控，冷却 {self._transfer_risk_cooldown} 秒"
        )

    def _fail_transfer(self, file_item: Dict[str, Any], reason: str) -> bool:
        """静默失败点统一出口：先把原因记下来，再返回 False。

        2026-11-16 LOST10 S01E05 的教训：_transfer_file_impl 里多处
        提前 return False 都不留原因，而重试提交时又清掉了旧原因，
        记录就只剩一句通用的「重试转存失败」，连卡在哪一步都查不到。
        以后每个 False 出口都走这里。
        """
        self._remember_transfer_failure(file_item, reason)
        return False

    def _remember_transfer_failure(self, file_item: Dict[str, Any], reason: str) -> None:
        """记录单个文件的转存失败原因，供历史记录区分「秒传失败」等类型。

        记之前先脱敏：这里是失败原因进历史记录的必经口，任何一路带
        凭证字段的原因都不能原样落盘（1.10.8 同款处理）。
        """
        text = sanitize_error_text(str(reason or "").strip())
        file_id = str((file_item or {}).get("id") or "").strip()
        if not text or not file_id:
            return
        cache = getattr(self, "_transfer_failure_reasons", None)
        if not isinstance(cache, dict):
            cache = {}
            self._transfer_failure_reasons = cache
        cache[file_id] = text

    def _transfer_failure_reason(self, file_id: str) -> str:
        cache = getattr(self, "_transfer_failure_reasons", None) or {}
        return str(cache.pop(str(file_id or "").strip(), "") or "")

    @classmethod
    def _cleanup_cross_transfer_staging(
            cls,
            source: CloudDriveProvider,
            staged_path: str = "",
            item: Optional[CloudFile] = None,
    ) -> None:
        """清理跨盘转存生成的临时源盘文件。
        
        同一暂存路径被多个任务共用时按引用计数归零才删（1.6.47 兜底）。
        注意：绝对不能删除 staged_path 目录本身！因为该目录直接复用用户配置的转存路径（如 /整理/待整理）。
        """
        if not source or not source.supports(CloudDriveCapability.FILE_MUTATION):
            return
        if item and getattr(item, "id", None):
            ref_key = _staging_ref_key(staged_path, item)
            remaining = _release_staging_ref(ref_key)
            if remaining > 0:
                logger.debug(
                    f"跨盘暂存文件仍被其它任务使用，跳过清理：{ref_key}"
                    f"（剩余引用 {remaining}）"
                )
                return
            try:
                mutation = source.require(CloudDriveCapability.FILE_MUTATION)
                mutation.delete_file(item.id)
                logger.debug(f"已清理源盘跨盘临时文件：{getattr(item, 'name', '')} ({item.id})")
            except Exception as error:
                logger.debug(f"清理源盘跨盘临时文件失败：{error}", exc_info=True)

    def _cross_transfer_staging_path(self, provider_key: str) -> str:
        base_path = self._cloud_transfer_paths.get(
            str(provider_key or "").strip().lower(), "/"
        )
        # 直接复用已配置的转存目录，不为跨盘任务创建额外目录。
        return str(PurePosixPath(base_path))

    @staticmethod
    def _configured_transfer_paths(plugin: Any) -> Dict[str, str]:
        """按驱动自描述读取各网盘的转存路径，新增网盘无需改动此处。"""
        paths = {}
        for definition_cls in get_driver_definitions():
            key = definition_cls.get_transfer_path_key()
            if not key:
                continue
            val = getattr(plugin, f"_{key}", None) or "/"
            paths[definition_cls.id] = val
        return paths

    @staticmethod
    def _configured_media_paths(plugin: Any) -> Dict[str, str]:
        """按驱动自描述读取各网盘的媒体库目录，新增网盘无需改动此处。"""
        paths = {}
        for definition_cls in get_driver_definitions():
            key = definition_cls.get_media_path_key()
            if not key:
                continue
            val = getattr(plugin, f"_{key}", None) or "/"
            paths[definition_cls.id] = val
        return paths
