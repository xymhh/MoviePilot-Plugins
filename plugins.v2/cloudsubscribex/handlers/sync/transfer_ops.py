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
        """在同一任务内拆批完成全部剧集转存。"""
        items = list(matched_items or [])
        if not items:
            return []
        batch_size = self._transfer_task_batch_size
        batch_count = (len(items) + batch_size - 1) // batch_size
        if batch_count > 1:
            logger.info(
                f"匹配 {len(items)} 个文件，将按每批最多 {batch_size} 个"
                f"分 {batch_count} 批在当前任务内处理"
            )
        results = []
        for batch_index, offset in enumerate(range(0, len(items), batch_size), 1):
            if self._stop_requested():
                break
            batch_items = items[offset:offset + batch_size]
            if batch_count > 1:
                logger.debug(
                    f"开始处理转存批次 {batch_index}/{batch_count}："
                    f"文件={len(batch_items)}"
                )
                self._set_task_phase(
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
            )
            results.extend(batch_results)
            if self._stop_requested():
                break
            if batch_index < batch_count and not self._wait_transfer_batch_interval():
                break
        return results

    def _wait_transfer_batch_interval(self) -> bool:
        """在任务内批次之间等待，并允许停止请求及时中断。"""
        deadline = time.monotonic() + self._batch_interval
        while time.monotonic() < deadline:
            if self._stop_requested():
                return False
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return True

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
    ) -> List[Dict[str, Any]]:
        """执行一个剧集转存批次及对应后处理。"""
        selected_items = list(matched_items or [])
        if not selected_items:
            return []
        if self._stop_requested():
            return []

        file_ids = [str(item["file"]["id"]) for item in selected_items]
        cloud_resource = self._is_cloud_resource_url(share_url)
        direct_cloud_resource = (
                cloud_resource and self._is_direct_cloud_resource_url(share_url)
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
        try:
            source_provider = self._resource_provider_for_url(share_url)
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
                        item_staging_dir = self._resource_staging_dir(
                            str(item["file"].get("url") or share_url), item["file"]
                        )
                        save_target_name = (
                            item["target_name"] if organize_enabled else item["file"].get("name")
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
                            return self._resource_staging_dir(str(file_dict.get("url") or share_url), file_dict)
                        return self._cloud_transfer_path

                    # 本地快照缓存，避免多集同目录时重复列目录
                    staging_snapshots: Dict[str, Tuple[bool, Dict[str, Any]]] = {}

                    def _get_snapshot(dir_path: str) -> Tuple[bool, Dict[str, Any]]:
                        if dir_path not in staging_snapshots:
                            staging_snapshots[dir_path] = self._cloud_directory_snapshot(dir_path)
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
                                _, check_index = self._cloud_directory_snapshot(check_path)
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

        success_id_set = {str(file_id) for file_id in (success_ids or [])}
        transferred_subtitle_ids = set()
        for item in processed_items:
            if str(item["file"]["id"]) not in success_id_set:
                continue
            subtitle_files = item.get("subtitle_files") or []
            item["subtitles"] = self._transfer_companion_subtitles(
                share_url=str(item["file"].get("url") or share_url),
                files=[item["file"], *subtitle_files],
                video_file=item["file"],
                target_video_name=item["target_name"],
                media_type="tv",
                season=season,
                episode=item.get("episode"),
                transferred_ids=transferred_subtitle_ids,
            ) if subtitle_files else []
        batch_strm_results = self._generate_or_queue_strm_batch(
            [
                {
                    "result_key": str(item["file"]["id"]),
                    "share_url": str(item["file"].get("url") or share_url),
                    "cloud_dir": item["target_dir"],
                    "file_name": item["target_name"],
                    "staging_dir": self._resource_staging_dir(
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
                    "subtitles": item.get("subtitles") or [],
                    "skip_history": bool(
                        (item.get("resource") or {}).get("skip_history")
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
                "failure_reason": (
                    "" if success else self._transfer_failure_reason(file_id)
                ),
            })
        return results

    def _transfer_file(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
    ) -> bool:
        """转存入口：可选地先走同步作业账本，再执行原转存实现。

        开关关闭时行为与旧版完全一致。打开后，同一内容写同一目标位置只会
        有一个 worker 真正执行；已完成的作业直接复用。
        """
        share_url = canonicalize_share_url(share_url)
        if not bool(getattr(self, "_sync_job_ledger_enabled", False)):
            return self._transfer_file_impl(
                share_url, file_item, save_path, target_name, source_sha1,
                parent_task_id, stop_requested, media_type,
            )

        if self._sync_paused():
            logger.warning(
                f"转存已暂停，跳过本次转存：{save_path}/{target_name}"
            )
            return False

        should_stop = stop_requested or self._stop_requested
        source = self._resource_provider_for_url(share_url)
        source_provider = str(getattr(source, "key", "") or "share")
        target_provider = str(getattr(self._cloud_drive, "key", "") or "")
        effective_name = str(target_name or file_item.get("name") or "").strip()
        source_ref = {
            "sha1": source_sha1 or file_item.get("sha1") or "",
            "md5": file_item.get("md5") or "",
            "file_id": file_item.get("id") or "",
            "size": file_item.get("size") or 0,
        }
        try:
            ledger = self._get_sync_job_ledger()
            lease, reason = ledger.begin(
                target_provider=target_provider,
                target_path=save_path,
                target_name=effective_name,
                source=source_ref,
                source_provider=source_provider,
                source_name=str(file_item.get("name") or ""),
                subscribe_id=_safe_optional_int(file_item.get("subscribe_id")),
                history_id=str(file_item.get("history_id") or "") or None,
                payload={"entry": "sync_transfer"},
                verify_done=self._verify_sync_job_result,
            )
        except Exception as error:
            logger.error(f"同步作业账本不可用，已阻止本次转存：{error}")
            return False
        if lease is None:
            logger.warning(
                f"同步作业未认领，跳过本次转存：{save_path}/{effective_name}，"
                f"原因={reason or '未知'}"
            )
            return False
        if lease.reused:
            logger.info(
                f"同步作业已完成，复用结果不重复转存：{save_path}/{effective_name}"
            )
            return True

        heartbeat_stop = threading.Event()

        def _heartbeat_loop() -> None:
            while not heartbeat_stop.wait(self._SYNC_JOB_HEARTBEAT_SECONDS):
                try:
                    if not ledger.heartbeat(lease):
                        logger.warning(
                            f"同步作业续租失败，停止心跳：{lease.job_key}"
                        )
                        return
                except Exception as error:
                    logger.warning(f"同步作业续租异常，停止心跳：{error}")
                    return

        heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            name="sync-job-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            success = self._transfer_file_impl(
                share_url, file_item, save_path, target_name, source_sha1,
                parent_task_id, stop_requested, media_type,
            )
        except Exception as error:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            try:
                ledger.failure(
                    lease,
                    error=f"{type(error).__name__}: {error}",
                    retryable=True,
                )
            except Exception as ledger_error:
                logger.error(f"同步作业失败记账异常：{ledger_error}")
            raise
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)

        if success:
            try:
                recorded = ledger.success(
                    lease,
                    result_name=effective_name,
                    result_size=file_item.get("size") or 0,
                    result_method="legacy_transfer",
                )
            except Exception as ledger_error:
                logger.error(f"同步作业完成记账异常：{ledger_error}")
                recorded = False
            if not recorded:
                logger.warning(
                    f"同步作业完成记账失败（文件可能已写入）：{save_path}/{effective_name}"
                )
            return True

        canceled = bool(should_stop())
        try:
            ledger.failure(
                lease,
                error="用户停止" if canceled else "转存实现返回失败",
                retryable=not canceled,
                canceled=canceled,
            )
        except Exception as ledger_error:
            logger.error(f"同步作业失败记账异常：{ledger_error}")
        return False

    def _transfer_file_impl(
            self, share_url: str, file_item: Dict[str, Any], save_path: str,
            target_name: str, source_sha1: str = "",
            parent_task_id: str = "",
            stop_requested: Optional[Callable[[], bool]] = None,
            media_type: str = "",
    ) -> bool:
        should_stop = stop_requested or self._stop_requested
        cloud_resource = self._is_cloud_resource_url(share_url)
        source = self._resource_provider_for_url(share_url)
        cross_provider = bool(
            source and self._cloud_drive and source.key != self._cloud_drive.key
        )
        if cross_provider:
            item_media_type = self._normalize_cross_transfer_media_type(
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
                    return False
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
                logger.warning(
                    f"无法跨盘转存单个文件：{source.name} -> "
                    f"{self._cloud_drive.name}，请检查跨盘开关和网盘能力"
                )
                return False
            if cloud_resource:
                item = self._cloud_file_from_dict(file_item)
                if not item.id:
                    logger.warning(f"无法跨盘整理文件：{source.name} 文件 ID 为空")
                    return False
            else:
                source_file_id = str(file_item.get("id") or "").strip()
                if not source_file_id:
                    logger.warning(f"无法跨盘转存单个文件：{source.name} 文件 ID 为空")
                    return False
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
                    return False
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
                    return False
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
                task = self._cross_transfer_manager.create_from_cloud_file(
                    source.key, item, self._cloud_drive.key, save_path, target_name,
                    fallback=True,
                    parent_task_id=parent_task_id,
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
                if not success:
                    failure_reason = str(
                        completed_task.get("error")
                        or completed_task.get("message")
                        or ""
                    ).strip()
                    if str(completed_task.get("abort_reason") or "") == "rapid_miss":
                        failure_reason = failure_reason or "未命中秒传"
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
            t_valid, t_index = self._cloud_directory_snapshot(save_path)
            if t_valid and (
                    target_check_name in t_index
                    or (target_name and target_name in t_index)
                    or (file_item.get("name") and file_item.get("name") in t_index)
            ):
                logger.debug(f"单文件转存返回失败但转存目录复核已存在，自愈复用：{save_path}/{target_check_name}")
                return True
        if not success:
            self._remember_transfer_failure(file_item, "分享转存失败")
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

    def _remember_transfer_failure(self, file_item: Dict[str, Any], reason: str) -> None:
        """记录单个文件的转存失败原因，供历史记录区分「秒传失败」等类型。"""
        text = str(reason or "").strip()
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

    def _verify_sync_job_result(self, result) -> str:
        """回查 done 作业的目标文件，区分存在/删除/冲突/查不清。

        只读网盘，不写任何状态；由 ``SyncJobLedger.begin`` 决定复用还是重开。
        目录/列表查询失败一律返回 unknown，避免把网络错误当成文件丢失后重传。
        """
        path = str(getattr(result, "target_path", "") or "")
        name = str(
            getattr(result, "result_name", "")
            or getattr(result, "target_name", "")
            or ""
        )
        if not path or not name:
            return self._SYNC_DONE_CHECK_UNKNOWN

        directories = getattr(self, "_cloud_directories", None)
        if directories is None:
            return self._SYNC_DONE_CHECK_UNKNOWN
        try:
            lookup = directories.resolve_directory(path)
        except Exception as error:
            logger.debug(f"同步作业 done 回查目录失败：{path}，{error}")
            return self._SYNC_DONE_CHECK_UNKNOWN
        if not bool(getattr(lookup, "checked", False)):
            return self._SYNC_DONE_CHECK_UNKNOWN
        directory_id = getattr(lookup, "directory_id", None)
        if directory_id is None:
            return self._SYNC_DONE_CHECK_MISSING
        try:
            listing = directories.list_directory(directory_id)
        except Exception as error:
            logger.debug(f"同步作业 done 回查列表失败：{path}，{error}")
            return self._SYNC_DONE_CHECK_UNKNOWN
        if not bool(getattr(listing, "checked", False)):
            return self._SYNC_DONE_CHECK_UNKNOWN

        found = next(
            (
                item for item in (getattr(listing, "files", ()) or ())
                if not bool(getattr(item, "is_directory", False))
                and str(getattr(item, "name", "") or "") == name
            ),
            None,
        )
        if found is None:
            return self._SYNC_DONE_CHECK_MISSING

        expected_size = int(getattr(result, "result_size", 0) or 0)
        actual_size = int(getattr(found, "size", 0) or 0)
        if expected_size and actual_size and expected_size != actual_size:
            return self._SYNC_DONE_CHECK_MISMATCH
        expected_id = str(getattr(result, "result_file_id", "") or "")
        actual_id = str(getattr(found, "id", "") or "")
        if expected_id and actual_id and expected_id != actual_id:
            return self._SYNC_DONE_CHECK_MISMATCH
        return self._SYNC_DONE_CHECK_EXISTS

    def _get_sync_job_ledger(self):
        """惰性获取账本业务入口，避免未开启时加载数据库相关模块。"""
        ledger = getattr(self, "_sync_job_ledger_instance", None)
        if ledger is not None:
            return ledger
        plugin = self._plugin
        store_getter = getattr(plugin, "_get_data_store", None)
        if store_getter is None:
            raise RuntimeError("宿主插件未提供数据存储")
        store = store_getter()
        if hasattr(store, "initialize"):
            store.initialize()
        from ...core.database.sync_job_ledger import SyncJobLedger
        ledger = SyncJobLedger(store.repositories.sync_jobs)
        self._sync_job_ledger_instance = ledger
        return ledger

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
