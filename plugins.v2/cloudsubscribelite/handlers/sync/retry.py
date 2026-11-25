"""
历史记录重试与现场补偿执行服务。
"""
import re
import time
from copy import deepcopy
from threading import Lock, Thread
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import CloudDriveCapability, CloudFile, OwnerDelegator
from ...core.failure import (
    KIND_SHARE_INVALID,
    apply_failure,
    clear_failure,
)
from ...core.media import (
    list_subscribes_by_tmdb_id,
    recognize_media,
    tmdb_id_of,
)
from ...core.state import (
    STAGE_DONE,
    STAGE_TRANSFERRING,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_PENDING_CONFIRM,
    STATUS_PROCESSING,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    transition,
)


_retry_inflight_lock = Lock()
_retry_inflight_keys: set = set()

# 批量重试护栏：单次上限、串行提交间隔、整批最大在飞数量。
_RETRY_BATCH_MAX_RECORDS = 20
_RETRY_BATCH_MIN_INTERVAL_SECONDS = 1.0
_RETRY_BATCH_MAX_INFLIGHT = 2


class HistoryRetryService(OwnerDelegator):
    """负责对历史失败或中断的记录进行就地重试与上下文还原。"""

    def _find_history_record(
            self, record_time: str, share_url: str, file_name: str
    ) -> Optional[Dict[str, Any]]:
        """重试入口只查重试索引（v1.13.3：history 表已删，无回落）。"""
        return self._find_retry_index_record(record_time, share_url, file_name)

    def _find_retry_index_record(
            self, record_time: str, share_url: str, file_name: str
    ) -> Optional[Dict[str, Any]]:
        """查重试索引：有行时用它拼一条可重试的记录 dict。

        索引不可用（测试桩/异常）时返回 None。
        """
        try:
            get_store = getattr(self, "_get_data_store", None)
            if not callable(get_store):
                return None
            row = get_store().repositories.retry_index.get_by_identity(
                record_time, share_url, file_name
            )
        except Exception as error:  # noqa: BLE001 - 索引不可用时返回 None
            logger.debug(f"重试索引查询失败：{error}", exc_info=True)
            return None
        if not row:
            return None
        try:
            from ...core.retry_index import index_row_to_history_dict
        except Exception as error:  # noqa: BLE001 - 索引模块不可用时返回 None
            logger.debug(f"重试索引转换失败：{error}", exc_info=True)
            return None
        return index_row_to_history_dict(row)

    @staticmethod
    def _history_retry_key(record_time: str, share_url: str, file_name: str) -> str:
        return "|".join(
            (str(record_time or ""), str(share_url or ""), str(file_name or ""))
        )

    def _history_retry_is_inflight(self, record: Dict[str, Any]) -> bool:
        """这条记录是否正有后台重试线程在跑（清扫器据此别碰它）。

        键在提交时加入、线程结束时 finally 丢弃；插件重载后集合是空的，
        所以更新杀掉线程后留下的孤儿记录在这里一定返回 False。
        """
        try:
            key = self._history_retry_key(
                str(record.get("time") or ""),
                str(record.get("share_url") or ""),
                str(record.get("file_name") or ""),
            )
        except Exception:
            return False
        with _retry_inflight_lock:
            return key in _retry_inflight_keys

    def submit_history_retry(
            self, record_time: str, share_url: str, file_name: str
    ) -> Dict[str, Any]:
        """校验后立即返回，把耗时的重试放到后台线程执行。

        前台请求不再阻塞在跨盘转存与后处理上（此前会长达数十分钟，
        界面只能停在旧状态等刷新）；提交时先把记录置为处理中并刷新历史版本，
        结束后再刷新一次，界面通过运行态 SSE 自动更新，无需手动刷新。
        """
        record = self._find_history_record(record_time, share_url, file_name)
        if not record:
            raise ValueError("未找到对应的转存历史记录")
        can_retry, retry_title = self._history_retry_state(record)
        if not can_retry:
            raise ValueError(retry_title)

        key = self._history_retry_key(record_time, share_url, file_name)
        with _retry_inflight_lock:
            if key in _retry_inflight_keys:
                raise ValueError("该记录正在重试中，请稍候")
            _retry_inflight_keys.add(key)

        try:
            processing = deepcopy(record)
            target_status = (
                STATUS_DOWNLOADING
                if self._is_ed2k_url(str(processing.get("share_url") or ""))
                else STATUS_PROCESSING
            )
            if processing.get("status") == STATUS_SUCCESS:
                transition(
                    processing, STATUS_PENDING_CONFIRM,
                    stage=STAGE_TRANSFERRING, reason="history_retry_reopen",
                    task_id=processing.get("record_id") or "",
                )
            elif processing.get("status") in {STATUS_FAILED, STATUS_SKIPPED}:
                transition(
                    processing, STATUS_PROCESSING,
                    stage=STAGE_TRANSFERRING, reason="history_retry_reopen",
                    task_id=processing.get("record_id") or "",
                )
            transition(
                processing, target_status,
                stage=STAGE_TRANSFERRING, reason="history_retry_submit",
                task_id=processing.get("record_id") or "",
            )
            clear_failure(processing)
            self._upsert_retry_index_row(processing, "处理中")
            if self._history_changed:
                self._history_changed()
        except Exception:
            with _retry_inflight_lock:
                _retry_inflight_keys.discard(key)
            raise

        Thread(
            target=self._run_history_retry,
            args=(key, str(record_time or ""), str(share_url or ""), str(file_name or "")),
            daemon=True,
            name="cloudsubscribe-history-retry",
        ).start()
        return {"status": "处理中"}

    def submit_history_retry_batch(
            self, records: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """批量重试失败记录：同步完成逐条分类，限流提交放到后台线程执行。

        入参 ``records`` 为 ``[{time, share_url, file_name}, ...]``；
        单次最多 20 条，超出直接报错不静默截断。提交复用
        :meth:`submit_history_retry` 的校验/去重/入队逻辑，不复制一份。
        """
        items = [record for record in (records or []) if isinstance(record, dict)]
        if len(items) > _RETRY_BATCH_MAX_RECORDS:
            raise ValueError(
                f"单次最多重试 {_RETRY_BATCH_MAX_RECORDS} 条记录，"
                f"当前 {len(items)} 条"
            )
        candidates: List[Dict[str, str]] = []
        details: List[Dict[str, Any]] = []
        skipped = 0
        for record in items:
            record_time = str(record.get("time") or "")
            share_url = str(record.get("share_url") or "")
            file_name = str(record.get("file_name") or "")
            found = self._find_history_record(record_time, share_url, file_name)
            if not found:
                skipped += 1
                details.append({
                    "file_name": file_name, "ok": False, "message": "记录不存在",
                })
                continue
            can_retry, retry_title = self._history_retry_state(found)
            if not can_retry:
                skipped += 1
                details.append({
                    "file_name": file_name, "ok": False, "message": retry_title,
                })
                continue
            key = self._history_retry_key(record_time, share_url, file_name)
            with _retry_inflight_lock:
                if key in _retry_inflight_keys:
                    skipped += 1
                    details.append({
                        "file_name": file_name, "ok": False, "message": "正在重试中",
                    })
                    continue
            candidates.append({
                "time": record_time, "share_url": share_url, "file_name": file_name,
            })
            details.append({
                "file_name": file_name, "ok": True, "message": "已加入重试队列",
            })
        if candidates:
            Thread(
                target=self._run_history_retry_batch,
                args=(candidates,),
                daemon=True,
                name="cloudsubscribe-history-retry-batch",
            ).start()
        return {"submitted": len(candidates), "skipped": skipped, "details": details}

    def _run_history_retry_batch(self, candidates: List[Dict[str, str]]) -> None:
        """串行提交批量重试：每条间隔限流，整批在飞数量不超过上限。"""
        attempted_keys: List[str] = []
        for index, candidate in enumerate(candidates):
            if index:
                time.sleep(_RETRY_BATCH_MIN_INTERVAL_SECONDS)
            record_time = str(candidate.get("time") or "")
            share_url = str(candidate.get("share_url") or "")
            file_name = str(candidate.get("file_name") or "")
            key = self._history_retry_key(record_time, share_url, file_name)
            while True:
                with _retry_inflight_lock:
                    inflight = sum(
                        1 for item in attempted_keys if item in _retry_inflight_keys
                    )
                if inflight < _RETRY_BATCH_MAX_INFLIGHT:
                    break
                time.sleep(_RETRY_BATCH_MIN_INTERVAL_SECONDS)
            try:
                self.submit_history_retry(record_time, share_url, file_name)
                attempted_keys.append(key)
            except Exception as error:
                logger.debug(f"批量重试提交失败（{file_name}）：{error}", exc_info=True)
        if self._history_changed:
            self._history_changed()

    def _run_history_retry(
            self, key: str, record_time: str, share_url: str, file_name: str
    ) -> None:
        try:
            self.retry_history_record(
                record_time, share_url, file_name, force=True
            )
        except Exception as error:
            logger.error(f"后台重试历史记录失败：{error}")
            self._mark_history_retry_failed(
                record_time, share_url, file_name, str(error) or "重试失败"
            )
        finally:
            with _retry_inflight_lock:
                _retry_inflight_keys.discard(key)
            if self._history_changed:
                self._history_changed()

    def _mark_history_retry_failed(
            self, record_time: str, share_url: str, file_name: str, reason: str
    ) -> None:
        """后台重试异常时把记录落回失败态，避免界面一直停在「处理中」。"""
        record = self._find_history_record(record_time, share_url, file_name)
        if not record or str(record.get("status") or "") == "成功":
            return
        apply_failure(record, reason)
        record.pop("retrying", None)
        # v1.30.0 阶段 4：漏斗行还在时保留 finalize_key——它是下一次
        # 重试找到漏斗行的唯一线索；旧路的 pending 记录已不在，stale
        # key 才要清（否则下次重试走旧路重传，有重复入库风险）。
        if not self._has_funnel_row(str(record.get("finalize_key") or "")):
            record.pop("finalize_key", None)
        self._upsert_retry_index_row(record, "失败")

    def _has_funnel_row(self, finalize_key: str) -> bool:
        """指定终态键是否还有漏斗行（任意状态，含 held/done）。"""
        if not str(finalize_key or "").strip():
            return False
        funnel = getattr(self._owner, "funnel", None)
        find_row = getattr(funnel, "find_row_by_pending_key", None)
        if not callable(find_row):
            return False
        try:
            return find_row(str(finalize_key or "").strip()) is not None
        except Exception as error:  # noqa: BLE001 - 查不到按无行处理
            logger.debug(f"重试查漏斗行失败：{error}", exc_info=True)
            return False

    def _retry_funnel_row(self, record: Dict[str, Any]) -> bool:
        """漏斗失败行的重试：重新入队（attempts 清零）。

        按记录的 finalize_key 查漏斗行：无行→返回 False（调用方走
        旧重试路）；行在 queued/processing→拒绝重试（已在处理中）；
        行在 held/failed 且漏斗开→重建待终态记录（缺失时）后放回
        队列，返回 True；漏斗关→返回 False（走旧重试路）。
        """
        finalize_key = str(record.get("finalize_key") or "").strip()
        if not finalize_key:
            return False
        funnel = getattr(self._owner, "funnel", None)
        find_row = getattr(funnel, "find_row_by_pending_key", None)
        if funnel is None or not callable(find_row):
            return False
        try:
            row = find_row(finalize_key)
        except Exception as error:  # noqa: BLE001 - 查不到按无行处理
            logger.debug(f"重试查漏斗行失败：{error}", exc_info=True)
            return False
        if not row:
            return False
        status = str(row.get("status") or "")
        if status in {"queued", "processing"}:
            raise ValueError("该记录已在整理漏斗处理中，无需重试")
        if status not in {"held", "failed"}:
            return False
        if not bool(getattr(self._owner, "_funnel_phase1_enabled", False)):
            return False
        row_id = int(row.get("id") or 0)
        ensure = getattr(funnel, "ensure_pending_record", None)
        if callable(ensure):
            try:
                if not ensure(row):
                    logger.warning(
                        f"漏斗行 #{row_id} 重试重建待终态记录失败"
                    )
            except Exception as error:  # noqa: BLE001 - 重建失败仍尝试放回
                logger.debug(f"漏斗行 #{row_id} 重试重建记录异常：{error}", exc_info=True)
        requeue = getattr(funnel, "requeue_rows", None)
        if not callable(requeue):
            raise ValueError("整理漏斗未就绪，无法重新入队")
        try:
            requeued = int(requeue([row_id]) or 0)
        except Exception as error:  # noqa: BLE001 - 放回异常按重试失败
            raise ValueError(f"重新入队失败：{error}") from error
        if requeued <= 0:
            raise ValueError("重新入队失败（行状态已变）")
        ensure_worker = getattr(funnel, "ensure_worker", None)
        if callable(ensure_worker):
            try:
                ensure_worker()
            except Exception as error:  # noqa: BLE001 - 起轮失败不挡入队结论
                logger.debug(f"漏斗重试起轮失败：{error}", exc_info=True)
        logger.info(f"历史重试已重新入队漏斗行 #{row_id}（{finalize_key}）")
        return True

    def _upsert_retry_index_row(self, record: Dict[str, Any], status: str) -> bool:
        """把一条 history 形状记录写进重试索引（失败/处理中/待修复成功行）。

        Phase 4 第 1 项：本方法是重试索引终态写入的三处之一（另两处是
        finalize 终态 _write_terminal_state 与同步结果落盘
        persist_sync_results）。
        """
        try:
            get_store = getattr(self, "_get_data_store", None)
            if not callable(get_store):
                return False
            from ...core.retry_index import record_to_index_row
            row = record_to_index_row(record, status)
            return bool(get_store().repositories.retry_index.upsert_row(row))
        except Exception as error:  # noqa: BLE001 - 索引写入失败不中断重试主流程
            logger.warning(f"重试索引写入失败：{error}")
            return False

    def _delete_retry_index_row(self, record: Dict[str, Any]) -> None:
        """重试终态成功时删掉索引行（无事可重试、无待修复项）。"""
        try:
            get_store = getattr(self, "_get_data_store", None)
            if not callable(get_store):
                return
            from ...core.retry_index import retry_record_id
            record_id = retry_record_id(record)
            if record_id:
                get_store().repositories.retry_index.delete_by_record_ids([record_id])
        except Exception as error:  # noqa: BLE001 - 索引删除失败不中断重试主流程
            logger.warning(f"重试索引删除失败：{error}")

    @staticmethod
    def _history_retry_state(record: Dict[str, Any]) -> Tuple[bool, str]:
        """一条索引行是否可重试（v1.13.3：规则已移到 core.retry_index
        的 retry_state_of，这里只做转发，失败页展示与重试入口共用）。"""
        from ...core.retry_index import retry_state_of

        return retry_state_of(record)

    @staticmethod
    def _retry_episode_number(file_name: str, season: int) -> Optional[int]:
        """从目标文件名解析季集号，用于识别同集不同名/不同扩展名的文件。"""
        stem = str(file_name or "").strip()
        if not stem:
            return None
        stem = re.sub(r"\.[^.]+$", "", stem)
        match = re.search(
            r"(?i)(?:^|[^A-Z0-9])S(\d{1,2})[ ._-]*E(?:P)?(\d{1,4})(?:[^0-9]|$)",
            stem,
        )
        if match:
            file_season = int(match.group(1))
            if season and file_season != season:
                return None
            return int(match.group(2))
        match = re.search(
            r"(?i)(?:^|[ ._-])E(?:P)?(\d{1,4})(?:[ ._-]|$)", stem
        )
        if match:
            return int(match.group(1))
        match = re.search(r"第\s*(\d{1,4})\s*[集话]", stem)
        if match:
            return int(match.group(1))
        return None

    def _find_existing_retry_target(
            self,
            cloud_dir: str,
            target_name: str,
            record: Dict[str, Any],
    ) -> tuple[Optional[CloudFile], bool]:
        """查找目标目录中已存在的目标文件。

        先按当前规则生成的精确文件名查找；未命中时，对剧集按季集号回退匹配，
        避免历史记录因命名模板变化（如 ``.mkv`` -> ``.2160p.mkv``/``.iso``）
        被误判为缺失，从而反复去校验早已失效的分享链接。
        """
        exact = self._cloud_query.find_file(cloud_dir, target_name, attempts=1)
        if exact:
            return exact, False

        record_type = str(record.get("type") or "")
        if record_type and record_type.lower() not in ("电视剧", "tv", "episode"):
            return None, False
        episode = int(record.get("episode") or 0)
        season = int(record.get("season") or 1)
        if not episode:
            return None, False

        try:
            files = self._cloud_query.list_files_recursive(
                cloud_dir, max_depth=1
            ) or []
        except Exception as error:
            logger.debug(f"历史重试扫描目标季目录失败：{cloud_dir}，{error}")
            return None, False

        expected_size = int(record.get("file_size") or 0)
        min_size = max(50 * 1024 * 1024, int(expected_size * 0.2))
        best_file = None
        best_size = -1
        for item in files:
            if getattr(item, "is_directory", False):
                continue
            name = str(getattr(item, "name", "") or "")
            if self._retry_episode_number(name, season) != episode:
                continue
            size = int(getattr(item, "size", 0) or 0)
            # 过滤样片/预告等明显不是正片的同集文件。
            if expected_size and size and size < min_size:
                continue
            if size > best_size:
                best_file = item
                best_size = size
        return best_file, bool(best_file)

    @staticmethod
    def _find_share_file_for_history(files: List[dict], source_sha1: str, source_name: str) -> Optional[dict]:
        source_hash = re.sub(r"[^0-9A-Fa-f]", "", str(source_sha1 or "")).upper()
        source_name = str(source_name or "").strip()
        leaf_files = []

        def collect(items: List[dict]) -> None:
            for item in items or []:
                if item.get("is_dir"):
                    collect(item.get("children") or [])
                else:
                    leaf_files.append(item)

        collect(files)
        for item in leaf_files:
            item_hash = re.sub(
                r"[^0-9A-Fa-f]", "", str(item.get("sha1") or "")
            ).upper()
            if source_hash and item_hash == source_hash:
                return item
        if source_name:
            matched = next(
                (
                    item
                    for item in leaf_files
                    if str(item.get("name") or "").strip() == source_name
                ),
                None,
            )
            if matched:
                return matched
        return leaf_files[0] if len(leaf_files) == 1 else None

    def retry_history_record(
            self,
            record_time: str,
            share_url: str,
            file_name: str,
            force: bool = False,
    ) -> Dict[str, Any]:
        """按持久化历史中的精确记录重新执行平台命名和完整后处理。

        force=True 供后台线程调用：提交阶段已校验过可重试性，此时记录已被
        置为「处理中」，需要跳过状态校验继续执行。
        """
        record = self._find_history_record(record_time, share_url, file_name)
        if not record:
            raise ValueError("未找到对应的转存历史记录")
        if not force:
            can_retry, retry_title = self._history_retry_state(record)
            if not can_retry:
                raise ValueError(retry_title)
        else:
            record.pop("retrying", None)

        # v1.30.0 阶段 4：漏斗失败行改走重新入队（attempts 清零），
        # 不再直接重跑整理；非漏斗行行为不变。True=已重新入队，旧路
        # 停在这里（提交阶段已把记录置为“处理中”，终态由漏斗 worker
        # 的 _commit_single_item 写回同一条索引行）。
        if self._retry_funnel_row(record):
            return {"success": True, "message": "已重新入队，整理漏斗将继续处理"}

        source_sha1 = str(record.get("source_sha1") or "").strip()
        source_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        ).strip()
        canonical_url = str(record.get("share_url") or "").strip()
        if not source_name or not canonical_url:
            raise ValueError("历史记录缺少文件名或资源链接，无法重试")

        retry_context = self._resolve_history_retry_context(record, source_name)
        subscribe = retry_context["subscribe"]
        mediainfo = retry_context["mediainfo"]
        season = retry_context["season"]
        episode = retry_context["episode"]
        cloud_dir = retry_context["cloud_dir"]
        target_name = retry_context["target_name"]

        existing_target, episode_match = self._find_existing_retry_target(
            cloud_dir, target_name, record
        )
        final_file_exists = bool(existing_target)
        success = final_file_exists
        retry_staging_name = source_name
        if existing_target:
            actual_name = str(getattr(existing_target, "name", "") or target_name)
            if actual_name != target_name:
                logger.info(
                    f"历史重试目标目录已有同集文件，改用实际文件名："
                    f"{cloud_dir.rstrip('/')}/{actual_name}"
                )
                target_name = actual_name
            if episode_match:
                record["landed"] = True
                record["landed_reason"] = "episode_match"
                record["verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                record["retry_reused_existing"] = True
        if not success:
            expected_size = int(record.get("file_size") or 0)
            expected_sha1 = source_sha1.upper()
            for candidate_name in dict.fromkeys((source_name, target_name)):
                staging_file = self._cloud_query.find_file(
                    self._cloud_transfer_path, candidate_name, attempts=1
                )
                if not staging_file:
                    continue
                actual_size = int(getattr(staging_file, "size", 0) or 0)
                actual_sha1 = str(
                    getattr(staging_file, "sha1", "") or ""
                ).upper()
                if expected_sha1 and actual_sha1 != expected_sha1:
                    continue
                if expected_size > 0 and actual_size != expected_size:
                    continue
                retry_staging_name = str(
                    getattr(staging_file, "name", "") or candidate_name
                )
                if not source_sha1 and actual_sha1:
                    source_sha1 = actual_sha1
                    record["source_sha1"] = actual_sha1
                success = True
                logger.info(
                    f"历史恢复复用目标盘暂存文件："
                    f"{self._cloud_transfer_path.rstrip('/')}/{retry_staging_name}"
                )
                break
        cross_source = None
        cached_source = None
        # v1.12.6 C：转存入参字典要留一个名字——_transfer_file_impl 会在跨盘
        # 分支就地写 file_item["mp_organize"]，重试后 _generate_or_queue_strm
        # 要从同一对象上读它传给 finalize；原来是内联字面量，调用后读不到。
        transfer_file_item: Dict[str, Any] = {}
        if record.get("transfer_mode") == "cross":
            source_key = str(record.get("source_drive_key") or "").strip()
            target_key = str(record.get("target_drive_key") or "").strip()
            if not source_key or not target_key:
                raise ValueError("跨盘历史缺少源网盘或目标网盘信息")
            if not self._cloud_drive or target_key != self._cloud_drive.key:
                raise ValueError("跨盘历史的目标网盘与当前转存网盘不一致")
            cached_source = CloudFile(
                id="",
                name=source_name,
                is_directory=False,
                size=int(record.get("file_size") or 0),
                sha1=source_sha1,
                md5=str(record.get("source_md5") or ""),
            )
            cache_info = self._cross_transfer_manager.cache_info(
                source_key, cached_source, verify_checksum=True,
                target_name=target_name,
            ) if self._cross_transfer_manager else {}
            record.update(cache_info)
            # 跨盘重试刷新了源文件缓存信息（id/sha1/md5），同步回索引行，
            # 状态保持原样（失败仍失败、待修复成功仍成功）。
            self._upsert_retry_index_row(
                record, str(record.get("status") or "失败")
            )
            # 1.10.0 起任务结束即删临时文件，不再保留缓存供恢复；
            # 重试一律完整重跑（下面直接走分享重新取源文件）。
            if not success:
                try:
                    cross_source = self._cloud_drive_registry.get(source_key)
                except KeyError as error:
                    raise ValueError(
                        f"跨盘历史源网盘未就绪：{source_key}"
                    ) from error

        if not success:
            file_id = ""
            if not self._is_ed2k_url(canonical_url):
                share_transfer = (
                    cross_source.require(CloudDriveCapability.SHARE_TRANSFER)
                    if cross_source else self._share_transfer
                )
                status = share_transfer.check_share_status(canonical_url)
                if not status.is_valid:
                    provider_name = cross_source.name if cross_source else "网盘"
                    reason = f"{provider_name}分享链接无效：{status.status_text}"
                    apply_failure(record, reason, KIND_SHARE_INVALID)
                    self._upsert_retry_index_row(record, "失败")
                    raise ValueError(reason)
                share_files = share_transfer.list_share_files(canonical_url)
                source_file = self._find_share_file_for_history(
                    share_files, source_sha1, source_name
                )
                if not source_file:
                    raise ValueError("原分享中未找到历史记录对应的源文件")
                file_id = str(source_file.get("id") or "")
                if not file_id:
                    raise ValueError("原分享文件缺少文件ID")
                if not source_sha1:
                    source_sha1 = str(source_file.get("sha1") or "")
                source_md5 = str(
                    source_file.get("md5") or record.get("source_md5") or ""
                )
                source_name = str(source_file.get("name") or source_name)
                record["source_file_name"] = source_name
                record["source_sha1"] = source_sha1
                record["source_md5"] = source_md5
                record["file_size"] = int(source_file.get("size") or 0)
                if cached_source:
                    cached_source = CloudFile(
                        id=file_id,
                        name=source_name,
                        is_directory=False,
                        size=int(record.get("file_size") or 0),
                        sha1=source_sha1,
                        md5=source_md5,
                    )
            transfer_file_item = {
                "id": file_id, "name": source_name,
                "size": record.get("file_size") or 0,
                "sha1": source_sha1,
                "md5": record.get("source_md5") or "",
            }
            success = self._owner.transfer_ops._transfer_file(
                canonical_url,
                transfer_file_item,
                self._cloud_transfer_path, target_name, source_sha1,
                # 1.12.2：重试必须把上面算好的目标名（整理后的规范名，如
                # “十万狂花入梦来 - S01E06 - 第6集.mkv”）传给转存，不能传
                # None。此前这里传 None，create() 的 title 退回源文件名，
                # 1.12.1 的“下载时用目标名落盘”在重试上根本没生效：文件以
                # 06.mkv 之类的源文件名落到 /downloads，MP 整理认不出剧名，
                # 5 条重试（13:17–13:21）全以 Downloads 命名失败。同盘分支
                # 不受影响（SHARE_TRANSFER 仍优先用源文件名保存）。
                media={
                    # mediainfo 是本次重试现认的，认不到时 tmdb_id 为空；
                    # 记录本身存的 tmdb_id 是权威的（历史页媒体详情就
                    # 链到它，如 LOST10 的 tmdb:329490），拿来兜底。
                    "tmdb_id": getattr(mediainfo, "tmdb_id", None) or record.get("tmdb_id"),
                    "media_type": getattr(getattr(mediainfo, "type", None), "name", ""),
                    "season": season,
                    "year": getattr(mediainfo, "year", None),
                    "media_title": getattr(mediainfo, "title", ""),
                    # 记录建时存的海报地址：下载页实时查 TMDB 取不到
                    # 海报时用它兜底（MP 自身都识别不到该媒体时也一样）。
                    "poster": str(record.get("image") or ""),
                    # 重试知道确切集号：裸文件名（如 18.mp4）解析不出集号，
                    # 靠这里直传，否则下载页只剩 S01、没有 E18。
                    "target_episodes": (
                        [episode]
                        if mediainfo.type == MediaType.TV and episode
                        else []
                    ),
                },
            )

        if not success:
            if cached_source and self._cross_transfer_manager:
                record.update(self._cross_transfer_manager.cache_info(
                    str(record.get("source_drive_key") or ""),
                    cached_source,
                    verify_checksum=False,
                    target_name=target_name,
                ))
            # 本次重试的失败原因优先用任务刚记下来的那条（_transfer_file
            # 已把任务 error 写进 _transfer_failure_reasons）；记录上残留
            # 的旧原因（上一次失败的）只能作备选。以前这里只读记录，旧
            # 原因一空就只剩一句通用的「重试转存失败」，查不到这次到底
            # 错在哪——LOST10 S01E05 那条就是这么来的。
            reason = (
                self._owner.transfer_ops._transfer_failure_reason(file_id)
                or str(record.get("failure_reason") or "").strip()
                or "重试转存失败"
            )
            apply_failure(record, reason)
            self._upsert_retry_index_row(record, "失败")
            raise RuntimeError(reason)

        strm_path, pending_key = self._owner.strm_ops._generate_or_queue_strm(
            canonical_url,
            cloud_dir,
            target_name,
            mediainfo,
            source_sha1=source_sha1,
            file_size=int(record.get("file_size") or 0),
            subscribe_id=getattr(subscribe, "id", None),
            success_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else [1]
            ),
            season=season if mediainfo.type == MediaType.TV else None,
            notification_episodes=(
                [episode] if mediainfo.type == MediaType.TV and episode else None
            ),
            staging_dir="" if final_file_exists else self._cloud_transfer_path,
            staging_name=retry_staging_name,
            # v1.12.6 C：跨盘重试转存成功时 _transfer_file_impl 已把 MP 整理
            # 身份就地写进 transfer_file_item；同盘重试或复用既有文件时没有
            # 身份，传 None 走老路径。
            mp_organize=transfer_file_item.get("mp_organize"),
        )
        if not strm_path and not pending_key:
            apply_failure(record, "文件已转存但后处理任务登记失败")
            self._upsert_retry_index_row(record, "失败")
            raise RuntimeError("文件已转存，但无法登记后处理任务")
        record["file_name"] = target_name
        record["cloud_dir"] = cloud_dir
        record["source_file_name"] = source_name
        record["source_sha1"] = source_sha1
        record["tmdb_id"] = mediainfo.tmdb_id
        record.pop("failure_reason", None)
        if cached_source and self._cross_transfer_manager:
            record.update(self._cross_transfer_manager.cache_info(
                str(record.get("source_drive_key") or ""),
                cached_source,
                verify_checksum=False,
                target_name=target_name,
            ))
        # 终态关联先落定，再做标题/年份/封面等装饰性更新：
        # 装饰性字段异常时记录仍带 finalize_key，后处理可把终态写回，不会卡在旧状态。
        if record.get("status") in {STATUS_FAILED, STATUS_SKIPPED}:
            transition(
                record, STATUS_PROCESSING,
                stage=STAGE_TRANSFERRING, reason="history_retry_reopen",
                task_id=record.get("record_id") or "",
            )
        if pending_key:
            record["finalize_key"] = pending_key
            transition(
                record,
                STATUS_DOWNLOADING if self._is_ed2k_url(canonical_url) else STATUS_PROCESSING,
                stage=STAGE_TRANSFERRING, reason="history_retry_pending",
                task_id=record.get("record_id") or "",
            )
        else:
            record.pop("finalize_key", None)
            transition(
                record, STATUS_SUCCESS,
                stage=STAGE_DONE, reason="history_retry_success",
                task_id=record.get("record_id") or "",
            )
            self._media_server_notifier.notify(
                path=strm_path,
                mediainfo=mediainfo,
                file_name=target_name,
            )
        try:
            target_subscribe = subscribe or SimpleNamespace(
                name=str(record.get("title") or getattr(mediainfo, "title", "") or ""),
                year=str(record.get("year") or getattr(mediainfo, "year", "") or ""),
            )
            effective_title = str(
                getattr(subscribe, "name", None)
                or getattr(target_subscribe, "name", None)
                or getattr(mediainfo, "title", None)
                or record.get("title")
                or ""
            ).strip()
            if effective_title:
                record["title"] = effective_title
            if getattr(target_subscribe, "year", None) or getattr(mediainfo, "year", None):
                record["year"] = str(
                    getattr(target_subscribe, "year", None) or mediainfo.year
                )
            if getattr(mediainfo, "get_poster_image", None) and mediainfo.get_poster_image():
                record["image"] = mediainfo.get_poster_image()
        except Exception as error:
            logger.warning(f"历史记录重试元数据更新失败（不影响终态写入）：{error}")

        if subscribe and not pending_key:
            success_episodes = [1]
            if mediainfo.type == MediaType.TV:
                success_episodes = [episode] if episode else []
            if success_episodes:
                self._subscribe_handler.check_and_finish_subscribe(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    success_episodes=success_episodes,
                )
        if pending_key:
            # 重试已交后处理跟进：索引行 stays 处理中，finalize 终态由
            # _write_terminal_state 结算（成功删行、失败写失败行）。
            self._upsert_retry_index_row(record, "处理中")
        else:
            # 重试已在线完成且成功：索引行不再有重试/修复价值，直接删行。
            self._delete_retry_index_row(record)
        logger.info(
            f"历史记录后处理完成：{cloud_dir.rstrip('/')}/{target_name}，"
            f"状态：{record['status']}"
        )
        return {
            "status": record["status"],
            "pending_key": pending_key,
            "strm_path": str(strm_path or ""),
            "cloud_dir": cloud_dir,
            "file_name": target_name,
        }

    def _resolve_history_retry_context(
            self, record: Dict[str, Any], source_name: str
    ) -> Dict[str, Any]:
        """按当前订阅和规则还原历史记录的最终处理上下文。"""
        title = str(record.get("title") or "").strip()
        if not title or not source_name:
            raise ValueError("历史记录缺少媒体名称或源文件名")

        media_type = (
            MediaType.TV
            if str(record.get("type") or "") == "电视剧"
            else MediaType.MOVIE
        )
        season = int(record.get("season") or 1) if media_type == MediaType.TV else None
        episode = (
            int(record.get("episode") or 0) or None
            if media_type == MediaType.TV
            else None
        )
        meta = MetaInfo(title)
        meta.type = media_type
        meta.year = record.get("year")
        if season is not None:
            meta.begin_season = season
        if episode is not None:
            meta.begin_episode = episode
        mediainfo = recognize_media(
            self._chain,
            meta=meta,
            mtype=media_type,
            tmdb_id=record.get("tmdb_id"),
            cache=True,
        )
        if not mediainfo:
            raise ValueError(f"无法识别历史记录媒体：{title}")

        subscribe = None
        tmdb_id = mediainfo.tmdb_id or record.get("tmdb_id")
        if tmdb_id:
            try:
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdb_id, season
                )
                if not candidates and media_type == MediaType.MOVIE:
                    candidates = [
                        item
                        for item in (SubscribeOper().list() or [])
                        if tmdb_id_of(item) == int(tmdb_id)
                    ]
                subscribe = next(
                    (
                        item
                        for item in candidates
                        if str(getattr(item, "type", "")) == media_type.value
                           and (
                                   media_type != MediaType.TV
                                   or int(getattr(item, "season", 1) or 1) == season
                           )
                    ),
                    None,
                )
            except Exception as error:
                logger.warning(f"查询历史记录对应订阅失败：{title}，{error}")

        target_subscribe = subscribe or SimpleNamespace(
            name=title or (mediainfo and mediainfo.title),
            year=record.get("year") or (mediainfo and mediainfo.year),
            media_category=None,
            media_category_id=None,
            include=None,
            exclude=None,
            quality=None,
            resolution=None,
            effect=None,
            audio_quality=None,
            audio_format=None,
            min_bitrate=None,
            min_bit_depth=None,
            min_sample_rate=None,
            filter=None,
            filter_groups=None,
            sites=None,
            custom_words=None,
        )
        cloud_dir, target_name = self._owner.naming._platform_target(
            root_path=self._CLOUD_MEDIA_ROOT,
            subscribe=target_subscribe,
            mediainfo=mediainfo,
            source_name=source_name,
            season=season,
            episode=episode,
        )
        return {
            "subscribe": subscribe,
            "target_subscribe": target_subscribe,
            "mediainfo": mediainfo,
            "season": season,
            "episode": episode,
            "cloud_dir": cloud_dir,
            "target_name": target_name,
        }
