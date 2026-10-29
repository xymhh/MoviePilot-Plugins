"""转存历史记录生命周期与重试。"""

import copy
import hashlib
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from app.chain.mediaserver import MediaServerChain
from app.core.context import MediaInfo
from app.db import SessionFactory
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.models.mediaserver import MediaServerItem
from app.db.subscribe_oper import SubscribeOper
from app.log import logger

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:
    from app.application.mediaserver import MediaServerHelper
from app.schemas.types import MediaType
from sqlalchemy import func, or_
from ...core import CloudFile, OwnerDelegator
from .utils import normalize_season
from ...core.failure import (
    KIND_FILE_MISSING,
    KIND_SHARE_INVALID,
    clear_failure,
    failure_kind_of,
    failure_label,
    failure_retryable_of,
)
from ...core.history import history_group_key
from ...drive.common import format_size, positive_int
from ...core.media import (
    download_history_identity_payload,
    media_identity,
    media_server_tmdb_filters,
    tmdb_id_of,
)
try:
    from ...core.media_scope import is_ignored_media_path
except (ImportError, AttributeError):  # 单测/精简环境没有媒体库过滤模块
    def is_ignored_media_path(path):
        return False
from ...search.types import normalize_resource_type, resource_type_from_url


class HistoryService(OwnerDelegator):
    """管理转存历史记录、关联文件与重试流程。"""

    _METADATA_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
    _SEASON_DIRECTORY_PATTERN = re.compile(r"^season[ ._-]*\d+$", re.IGNORECASE)
    _PLATFORM_HISTORY_STORAGE = "cloudsubscribe"
    # 允许「批量手动标记成功」的来源状态：落盘自检查无实据的「待确认」，
    # 以及卡在队列里的「处理中」/「下载中」。终态（成功/失败/跳过）不在此列。
    _MANUAL_SUCCESS_STATUSES = frozenset({"待确认", "处理中", "下载中"})

    def _build_transfer_history_item(
            self,
            mediainfo: MediaInfo,
            subscribe,
            status: str,
            share_url: str,
            file_name: str,
            source_file_name: str,
            cloud_dir: str,
            resource: Optional[Dict[str, Any]] = None,
            **fields: Any,
    ) -> Dict[str, Any]:
        """集中生成插件转存历史，确保平台整理历史所需字段完整。"""
        media_data = self._serialize_mediainfo(mediainfo)
        media_source, media_id = media_identity(mediainfo)
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        cloud_drive_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        ).strip()
        item = {
            "title": effective_media.title,
            "year": effective_media.year,
            "tmdb_id": mediainfo.tmdb_id,
            "imdb_id": mediainfo.imdb_id,
            "tvdb_id": mediainfo.tvdb_id,
            "douban_id": mediainfo.douban_id,
            "bangumi_id": getattr(mediainfo, "bangumi_id", None),
            "anilist_id": getattr(mediainfo, "anilist_id", None),
            "media_source": media_source,
            "media_id": media_id or media_data.get("media_id"),
            "category": getattr(mediainfo, "category", None),
            "episode_group": getattr(mediainfo, "episode_group", None),
            "image": mediainfo.get_poster_image(),
            "type": mediainfo.type.value,
            "status": status,
            "share_url": share_url,
            "file_name": file_name,
            "source_file_name": source_file_name,
            "cloud_dir": cloud_dir,
            "cloud_drive_name": cloud_drive_name,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            **fields,
        }
        item.update(self._resource_history_meta(resource or {}, share_url))
        source_provider = self._resource_provider_for_url(share_url)
        if (
                source_provider and self._cloud_drive
                and source_provider.key != self._cloud_drive.key
        ):
            source_file = CloudFile(
                id="",
                name=str(source_file_name or file_name or ""),
                is_directory=False,
                size=int(fields.get("file_size") or 0),
                sha1=str(fields.get("source_sha1") or ""),
                md5=str(fields.get("source_md5") or ""),
            )
            item.update({
                "transfer_mode": "cross",
                "source_drive_key": source_provider.key,
                "source_drive_name": source_provider.name,
                "target_drive_key": self._cloud_drive.key,
                "target_drive_name": self._cloud_drive.name,
            })
            if self._cross_transfer_manager:
                item.update(self._cross_transfer_manager.cache_info(
                    source_provider.key,
                    source_file,
                    verify_checksum=False,
                ))
        if not item.get("discovered_via"):
            fallback_via = str(getattr(subscribe, "_discovered_via", "") or "").strip()
            if fallback_via:
                # 频道推送来的候选带 tg:<频道>/<消息id>，搜索来的留空
                item["discovered_via"] = fallback_via
        item["task_types"] = self._history_task_types(item)
        return item

    def _record_download_history(
            self,
            mediainfo: MediaInfo,
            subscribe,
            path: str,
            download_hash: Any,
            torrent_name: str,
            share_url: str,
            torrent_description: str = "",
            seasons: str = "",
            episodes: str = "",
    ) -> bool:
        """通过下载历史能力登记一次网盘转存。"""
        effective_media = self._effective_mediainfo(subscribe, mediainfo)
        provider_name = str(
            getattr(self._cloud_drive, "name", "网盘") or "网盘"
        )
        payload = {
            "path": path,
            "type": mediainfo.type.value,
            "title": effective_media.title,
            "year": effective_media.year,
            "image": mediainfo.get_poster_image(),
            "downloader": provider_name,
            "download_hash": download_hash,
            "torrent_name": torrent_name,
            "torrent_site": provider_name,
            "username": "CloudSubscribe",
            "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": {
                "source": f"Subscribe|{getattr(subscribe, 'name', '')}",
                "share_url": share_url,
            },
        }
        payload.update(download_history_identity_payload(
            mediainfo, DownloadHistory
        ))
        if torrent_description:
            payload["torrent_description"] = torrent_description
        if seasons:
            payload["seasons"] = seasons
        if episodes:
            payload["episodes"] = episodes
        try:
            DownloadHistoryOper().add(**payload)
            logger.debug(f"已记录 {mediainfo.title_year} 下载历史")
            return True
        except Exception as error:
            logger.warning(f"记录下载历史失败：{error}")
            return False

    def _detail_source_text(self, history_item: Optional[Dict[str, Any]]) -> str:
        """通知里展示的网盘来源：跨盘写「源 → 目标」，同盘只写目标盘。"""
        item = history_item or {}
        target = str(
            item.get("target_drive_name") or item.get("cloud_drive_name") or ""
        ).strip()
        if not target:
            # 部分后处理链路（文件落盘时才发通知）没有历史记录，
            # 直接用当前挂载的网盘名兜底，避免来源整段消失
            target = str(
                getattr(getattr(self, "_cloud_drive", None), "name", "") or ""
            ).strip()
        source = str(item.get("source_drive_name") or "").strip()
        # 跨盘才写「源 → 目标」；同盘只写目标盘名
        if source and target and source != target:
            return f"{source} → {target}"
        return target or source or ""

    @staticmethod
    def _detail_size(
            history_item: Optional[Dict[str, Any]], file_info: Any = None
    ) -> int:
        """通知里展示的文件大小：优先历史记录，其次网盘文件对象。"""
        candidates: List[Any] = []
        if isinstance(history_item, dict):
            candidates.append(history_item.get("file_size"))
            candidates.append(history_item.get("size"))
        if isinstance(file_info, dict):
            candidates.append(file_info.get("size"))
            inner = file_info.get("file")
            if isinstance(inner, dict):
                candidates.append(inner.get("size"))
        for value in candidates:
            try:
                size = int(value or 0)
            except (TypeError, ValueError):
                size = 0
            if size > 0:
                return size
        return 0

    def _append_tv_transfer_detail(
            transfer_details: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: int,
            episodes: List[int],
            notification_kind: str = "transfer",
            size_bytes: int = 0,
            source_text: str = "",
    ) -> None:
        """按媒体季批量合并通知详情，避免逐集重复扫描列表。"""
        normalized = sorted({
            int(episode) for episode in episodes if int(episode) > 0
        })
        if not normalized:
            return
        detail = next(
            (
                item for item in transfer_details
                if item.get("title") == mediainfo.title
                   and int(item.get("season") or 0) == int(season)
                   and item.get("notification_kind", "transfer") == notification_kind
            ),
            None,
        )
        if detail:
            detail["episodes"] = sorted(
                set(detail.get("episodes") or []) | set(normalized)
            )
            detail["size"] = int(detail.get("size") or 0) + max(0, int(size_bytes or 0))
            if source_text and not detail.get("source"):
                detail["source"] = source_text
            return
        transfer_details.append({
            "type": MediaType.TV.value,
            "title": mediainfo.title,
            "year": mediainfo.year,
            "season": season,
            "episodes": normalized,
            "image": mediainfo.get_poster_image(),
            "notification_kind": notification_kind,
            "size": max(0, int(size_bytes or 0)),
            "source": source_text,
        })

    @staticmethod
    def _history_record_identity(
            record: Dict[str, Any]
    ) -> Optional[Tuple[str, ...]]:
        """使用持久字段标识同一条历史，避免旧快照覆盖监控更新。"""
        identity = tuple(
            str(record.get(key) or "")
            for key in (
                "share_url",
                "file_name",
                "tmdb_id",
                "season",
                "episode",
            )
        )
        return identity if identity[0] and identity[1] else None

    @classmethod
    def _upgrade_scope_identity(
            cls, record: Dict[str, Any]
    ) -> Optional[Tuple[str, ...]]:
        """标识同一媒体季集，洗版写入时据此替换旧版本记录。"""
        media_type = str(record.get("type") or "").strip()
        tmdb_id = str(record.get("tmdb_id") or "").strip()
        media_key = (
            f"tmdb:{tmdb_id}"
            if tmdb_id
            else "legacy:"
                 f"{str(record.get('title') or '').strip()}:"
                 f"{str(record.get('year') or '').strip()}"
        )
        if not media_type or media_key == "legacy::":
            return None
        if media_type == MediaType.MOVIE.value:
            return media_type, media_key
        season = positive_int(record.get("season"))
        episode = positive_int(record.get("episode"))
        if not season or not episode:
            return None
        return media_type, media_key, str(season), str(episode)

    @classmethod
    def _ensure_history_record_id(cls, record: Dict[str, Any]) -> str:
        record_id = str(record.get("record_id") or "").strip()
        if record_id:
            return record_id
        identity = cls._history_record_identity(record)
        if not identity:
            return ""
        record_id = hashlib.sha1("\0".join(identity).encode("utf-8")).hexdigest()
        record["record_id"] = record_id
        return record_id

    @staticmethod
    def _is_upgrade_history(record: Dict[str, Any]) -> bool:
        value = record.get("upgrade")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    @classmethod
    def _history_task_types(cls, record: Dict[str, Any]) -> List[str]:
        raw_types = record.get("task_types") or []
        if isinstance(raw_types, str):
            raw_types = [raw_types]
        task_types = {
            str(value or "").strip()
            for value in raw_types
            if str(value or "").strip()
        }
        if str(record.get("transfer_mode") or "") == "cross":
            task_types.add("cross_transfer")
        if cls._is_upgrade_history(record):
            task_types.add("upgrade")
        return sorted(task_types)

    @classmethod
    def _is_workflow_history(cls, record: Dict[str, Any]) -> bool:
        return bool(cls._history_task_types(record))

    def append_history_records(
            self,
            records: List[Dict[str, Any]],
            reopen_terminal: bool = False,
    ) -> int:
        """合并新增历史，并原子激活对应的网盘文件后处理任务。"""
        if not records or not self._get_data or not self._save_data:
            return 0
        activated_pending_count = 0
        platform_records = []
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            for record in history:
                self._ensure_history_record_id(record)
            record_id_index = {
                str(record.get("record_id") or ""): index
                for index, record in enumerate(history)
                if str(record.get("record_id") or "")
            }
            record_index = {
                identity: index
                for index, record in enumerate(history)
                if (identity := self._history_record_identity(record))
            }
            upgrade_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if (scope := self._upgrade_scope_identity(record))
            }
            workflow_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if self._is_workflow_history(record)
                if (scope := self._upgrade_scope_identity(record))
            }
            failed_scope_index = {
                scope: index
                for index, record in enumerate(history)
                if str(record.get("status") or "") == "失败"
                if (scope := self._upgrade_scope_identity(record))
            }
            for record in records:
                incoming = copy.deepcopy(record)
                self._ensure_history_record_id(incoming)
                identity = self._history_record_identity(incoming)
                incoming_id = str(incoming.get("record_id") or "")
                scope = self._upgrade_scope_identity(incoming)
                # 先用 record_id 锁定原记录：重试会把 file_name 从 .mkv 改成
                # 实际存在的 .iso/.2160p.mkv，identity 随之变化；若此时按
                # identity/scope 合并，会错写同一集的另一条记录，原失败记录
                # 仍留在历史里，页面继续显示「失败」。
                index = record_id_index.get(incoming_id) if incoming_id else None
                if index is not None and scope:
                    current_scope = self._upgrade_scope_identity(history[index])
                    if current_scope and current_scope != scope:
                        index = None
                if index is None and identity:
                    index = record_index.get(identity)
                if index is None and scope:
                    index = upgrade_scope_index.get(scope)
                if index is None:
                    history.append(incoming)
                    platform_records.append(copy.deepcopy(incoming))
                    if incoming_id:
                        record_id_index[incoming_id] = len(history) - 1
                    if identity:
                        record_index[identity] = len(history) - 1
                    if scope:
                        upgrade_scope_index[scope] = len(history) - 1
                        if self._is_workflow_history(incoming):
                            workflow_scope_index[scope] = len(history) - 1
                    if str(incoming.get("status") or "") == "失败" and scope:
                        failed_scope_index[scope] = len(history) - 1
                    continue

                current = history[index]
                current_status = str(current.get("status") or "")
                incoming_status = str(incoming.get("status") or "")
                merged = {**current, **incoming}
                incoming_is_cross = (
                    str(incoming.get("transfer_mode") or "") == "cross"
                )
                if not incoming_is_cross:
                    # 同盘转存覆盖跨盘记录时，跨盘标记必须跟着失效，
                    # 否则「115 来源 + 跨盘区」这种自相矛盾的标签会一直粘着。
                    for stale_key in (
                            "transfer_mode",
                            "source_drive_key",
                            "source_drive_name",
                            "target_drive_key",
                            "target_drive_name",
                    ):
                        merged.pop(stale_key, None)
                merged["task_types"] = sorted(
                    (set(self._history_task_types(current))
                     | set(self._history_task_types(incoming)))
                    - {"cross_transfer"}
                )
                merged["task_types"] = self._history_task_types(merged)
                if self._is_upgrade_history(incoming):
                    merged["upgrade"] = True
                    same_file = (
                        str(current.get("file_name") or "").strip()
                        == str(incoming.get("file_name") or "").strip()
                        and (current.get("file_size") or 0)
                        == (incoming.get("file_size") or 0)
                    )
                    if same_file and self._is_upgrade_history(current):
                        # 同一次洗版的后续状态回写（处理中→成功）：沿用原快照，
                        # 否则 previous_file_size 会被写成新版体积、计数也会翻倍。
                        for snapshot_key in (
                                "previous_file_name",
                                "previous_file_size",
                                "previous_rule_score",
                        ):
                            if snapshot_key in current:
                                merged[snapshot_key] = current[snapshot_key]
                        merged["upgrade_count"] = max(
                            1, int(current.get("upgrade_count") or 1)
                        )
                    else:
                        merged["upgrade_count"] = max(
                            1, int(current.get("upgrade_count") or 0) + 1
                        )
                        merged["previous_file_name"] = str(
                            current.get("file_name") or ""
                        )
                        merged["previous_file_size"] = current.get("file_size")
                        merged["previous_rule_score"] = current.get("rule_score")
                if (
                        current_status in {"成功", "失败", "跳过"}
                        and incoming_status not in {"成功", "失败"}
                        and not reopen_terminal
                ):
                    merged["status"] = current_status
                    for state_key in (
                            "finalize_key", "failure_reason",
                            "failure_kind", "failure_retryable",
                    ):
                        if state_key in current:
                            merged[state_key] = current[state_key]
                        else:
                            merged.pop(state_key, None)
                if incoming_status != "失败":
                    clear_failure(merged)
                skip_onto_success = (
                    incoming_status == "跳过" and current_status == "成功"
                )
                if skip_onto_success:
                    # 「跳过」只说明本轮没转存，不代表平台上发生了什么：不能用它覆盖
                    # 成功记录的落盘信息（文件名/目录/大小），否则该记录会变成
                    # 「成功但找不到文件」的幻影版本，下一轮判重把整集当下载好。
                    for keep_key in (
                        "file_name", "target_file_name", "cloud_dir",
                        "cloud_file_id", "cloud_drive_name", "file_size",
                        "landed", "landed_reason", "verified_at",
                    ):
                        if keep_key in current:
                            merged[keep_key] = current[keep_key]
                        else:
                            merged.pop(keep_key, None)
                history[index] = merged
                if incoming_id:
                    record_id_index[incoming_id] = index
                if identity:
                    record_index[identity] = index
                if not skip_onto_success:
                    # 也不要把这种「假成功」写进 MP 整理历史：写进去后，下一轮基线
                    # 会把它当成已有的更优版本，缺失集就再也补不上。
                    platform_records.append(copy.deepcopy(merged))
                if scope:
                    upgrade_scope_index[scope] = index
                    if self._is_workflow_history(merged):
                        workflow_scope_index[scope] = index
                    if incoming_status == "失败":
                        failed_scope_index[scope] = index
                    elif failed_scope_index.get(scope) == index:
                        failed_scope_index.pop(scope, None)
            self._save_data("history", history)
            finalize_keys = {
                str(record.get("finalize_key") or "")
                for record in records
                if str(record.get("finalize_key") or "")
            }
            if finalize_keys:
                pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
                now = time.time()
                activated = False
                for pending_key in finalize_keys:
                    item = pending.get(pending_key)
                    if not item:
                        continue
                    item["history_ready"] = True
                    item["next_check_at"] = min(
                        float(item.get("next_check_at") or now), now
                    )
                    activated = True
                if activated:
                    self._save_offline_pending(pending)
                    activated_pending_count = len(pending)
        if activated_pending_count:
            self._notify_offline_pending_changed(activated_pending_count)
        self._record_platform_transfer_histories(platform_records)
        self.reconcile_orphan_finalize_records()
        return len(records)

    def compact_workflow_history(self) -> List[Dict[str, Any]]:
        """合并同一媒体季集的跨盘、洗版工作流记录并持久化。"""
        if not self._get_data or not self._save_data:
            return []
        with self._offline_pending_lock:
            history = [
                copy.deepcopy(record)
                for record in (self._get_data("history") or [])
                if isinstance(record, dict)
            ]
            compacted: List[Dict[str, Any]] = []
            scope_indexes: Dict[Tuple[str, ...], int] = {}
            upgrade_scopes = {
                scope
                for record in history
                if self._is_upgrade_history(record)
                if (scope := self._upgrade_scope_identity(record))
            }
            for record in history:
                self._ensure_history_record_id(record)
                scope = self._upgrade_scope_identity(record)
                index = scope_indexes.get(scope) if scope else None
                should_merge = bool(
                    index is not None
                    and (
                            scope in upgrade_scopes
                            or (
                                    self._is_workflow_history(record)
                                    and self._is_workflow_history(compacted[index])
                            )
                    )
                )
                if not should_merge:
                    compacted.append(record)
                    if scope:
                        scope_indexes[scope] = len(compacted) - 1
                    continue
                current = compacted[index]
                latest, previous = (
                    (record, current)
                    if str(record.get("time") or "") >= str(current.get("time") or "")
                    else (current, record)
                )
                is_upgrade = (
                        self._is_upgrade_history(previous)
                        or self._is_upgrade_history(latest)
                )
                merged = {**previous, **latest, "upgrade": is_upgrade}
                merged["task_types"] = sorted(
                    set(self._history_task_types(previous))
                    | set(self._history_task_types(latest))
                    | ({"upgrade"} if is_upgrade else set())
                )
                if is_upgrade:
                    merged["upgrade_count"] = max(
                        1,
                        int(current.get("upgrade_count") or int(
                            self._is_upgrade_history(current)
                        )) + int(self._is_upgrade_history(record)),
                    )
                    merged["previous_file_name"] = str(
                        previous.get("file_name") or ""
                    )
                    merged["previous_file_size"] = previous.get("file_size")
                    merged["previous_rule_score"] = previous.get("rule_score")
                compacted[index] = merged
            if compacted != history:
                self._save_data("history", compacted)
                logger.info(
                    f"跨盘/洗版历史已合并：{len(history)} 条 -> {len(compacted)} 条"
                )
            return compacted

    def _history_page_fields(self, record: Dict[str, Any]) -> Dict[str, str]:
        """生成历史页面所需的稳定标识、名称和可点击链接。"""
        media_type = str(record.get("type") or "未知类型")

        file_name = str(
            record.get("file_name") or record.get("source_file_name") or ""
        ).strip() or "-"
        extension = Path(file_name).suffix.removeprefix(".").upper() or "-"
        if media_type == "电影":
            display_name = str(record.get("title") or "").strip()
            if not display_name:
                display_name = Path(file_name).stem or "-"
        else:
            try:
                season = max(0, int(record.get("season") or 0))
            except (TypeError, ValueError):
                season = 0
            target_episodes = record.get("target_episodes")
            values = (
                target_episodes
                if isinstance(target_episodes, (list, tuple, set))
                else re.findall(r"\d+", str(target_episodes or ""))
            )
            episodes = set()
            for value in values:
                try:
                    episode = int(value)
                except (TypeError, ValueError):
                    continue
                if episode > 0:
                    episodes.add(episode)
            ordered = sorted(episodes)
            season_label = f"S{season:02d}"
            if len(ordered) > 1:
                display_name = (
                    f"{season_label}E{ordered[0]:02d}-E{ordered[-1]:02d}"
                )
            else:
                try:
                    episode = ordered[0] if ordered else int(
                        record.get("episode") or 0
                    )
                except (TypeError, ValueError):
                    episode = 0
                display_name = (
                    f"{season_label}E{episode:02d}"
                    if episode > 0 else season_label
                )

        source = str(record.get("source") or "").strip().casefold()
        source_url = str(
            record.get("source_url") or record.get("media_page_url") or ""
        ).strip()
        if source == "hdhive" and source_url:
            parsed = urlsplit(source_url)
            if parsed.scheme and parsed.netloc:
                source_url = urlunsplit(("", "", parsed.path, parsed.query, ""))
            base_url = str(
                getattr(self, "_hdhive_base_url", "https://re0.me")
                or "https://re0.me"
            ).strip().rstrip("/")
            source_url = urljoin(f"{base_url}/", source_url.lstrip("/"))
        has_source_link = (
                source not in {"manual", "手动添加", "手动资源"}
                and bool(re.match(r"^https?://", source_url, re.IGNORECASE))
        )
        source_link = source_url if has_source_link else ""
        share_url = str(record.get("share_url") or "").strip()
        resource_link = (
            share_url
            if re.match(r"^(?:https?|ed2k|magnet):", share_url, re.IGNORECASE)
            else ""
        )
        return {
            "history_group_key": history_group_key(record),
            "display_name": display_name,
            "display_file_name": file_name,
            "file_extension": extension,
            "source_link": source_link,
            "resource_link": resource_link,
        }

    @classmethod
    def _cross_success_needs_repair(cls, record: Dict[str, Any]) -> bool:
        """跨盘「成功」但查无落盘痕迹 → 留一条人工重新转存入口（1.6.51）。

        1.6.52 起优先读显式 ``landed`` 字段；老记录没有该字段时回退原启发式
        （``cloud_dir`` 为空 或 只剩源文件名）。
        """
        landed = record.get("landed")
        if landed is False:
            return True
        if landed is True:
            return False
        if not str(record.get("cloud_dir") or "").strip():
            return True
        file_name = str(record.get("file_name") or "").strip()
        source_name = str(record.get("source_file_name") or "").strip()
        return bool(source_name) and source_name == file_name

    @classmethod
    def _history_retry_state(cls, record: Dict[str, Any]) -> Tuple[bool, str]:
        status = str(record.get("status") or "")
        is_cross = str(record.get("transfer_mode") or "") == "cross"
        if status == "成功" and is_cross:
            if cls._cross_success_needs_repair(record):
                if not str(record.get("share_url") or "").strip():
                    return False, "历史记录缺少资源链接，无法重试"
                return True, "重新转存此记录"
            return False, "跨盘任务已成功完成，无需重试"
        if not str(record.get("share_url") or "").strip():
            return False, "历史记录缺少资源链接，无法重试"
        if status == "失败":
            if not failure_retryable_of(record):
                kind = failure_kind_of(record)
                if kind == KIND_SHARE_INVALID:
                    return False, "原分享已失效，需重新搜源"
                if kind == KIND_FILE_MISSING:
                    return False, "分享内已找不到该文件，需重新搜源"
                return False, f"{failure_label(kind)}，重试无法恢复，需重新搜源"
            if is_cross:
                cache_status = str(record.get("cache_status") or "")
                title = (
                    "恢复跨盘转存"
                    if cache_status in {"complete", "partial"}
                    else "重新执行跨盘转存"
                )
                return True, title
            return True, "重试此记录"
        if (
                status == "成功"
                and str(record.get("source_file_name") or "")
                == str(record.get("file_name") or "")
        ):
            return True, "修复此记录"
        return False, "当前记录无需重试"

    def prepare_history_records(
            self, records: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """为历史页面生成业务展示字段，前端不再重复推导状态。"""
        prepared = []
        for source in records or []:
            record = copy.deepcopy(source)
            record["resource_type"] = (
                    normalize_resource_type(
                        record.get("resource_type") or record.get("pan_type") or ""
                    )
                    or resource_type_from_url(record.get("share_url"))
                    or "unknown"
            )
            record.update(self._history_page_fields(record))
            is_cross = str(record.get("transfer_mode") or "") == "cross"
            record["is_cross_transfer"] = is_cross
            record["task_types"] = self._history_task_types(record)
            if is_cross:
                source_name = str(
                    record.get("source_drive_name")
                    or record.get("source_drive_key")
                    or "源网盘"
                )
                target_name = str(
                    record.get("target_drive_name")
                    or record.get("target_drive_key")
                    or "目标网盘"
                )
                cache_labels = {
                    "complete": "缓存完整",
                    "partial": "可断点续传",
                    "missing": "缓存已清理" if record.get("status") == "成功" else "无可用缓存",
                }
                cache_label = cache_labels.get(
                    str(record.get("cache_status") or ""), cache_labels["missing"]
                )
                record["cross_transfer_title"] = (
                    f"{source_name} → {target_name} · {cache_label}"
                )
            if str(record.get("status") or "") == "失败":
                failure_kind = failure_kind_of(record)
                record["failure_kind"] = failure_kind
                record["failure_retryable"] = failure_retryable_of(record)
                record["failure_label"] = failure_label(failure_kind)
            can_retry, retry_title = self._history_retry_state(record)
            record["can_retry"] = can_retry
            record["retry_title"] = retry_title
            if self._is_upgrade_history(record):
                parts = []
                previous_name = str(record.get("previous_file_name") or "").strip()
                if previous_name:
                    parts.append(previous_name)
                previous_size = format_size(record.get("previous_file_size"), default="-")
                current_size = format_size(record.get("file_size"), default="-")
                if previous_size != "-":
                    parts.append(
                        f"{previous_size} → {current_size}"
                        if current_size != "-" else previous_size
                    )
                if record.get("previous_rule_score") is not None:
                    parts.append(
                        f"评分 {int(record.get('previous_rule_score') or 0)}"
                        f" → {int(record.get('rule_score') or 0)}"
                    )
                count = max(1, int(record.get("upgrade_count") or 1))
                count_label = f"（已洗版 {count} 次）" if count > 1 else ""
                record["upgrade_version_info"] = (
                    f"原版{count_label}：{' · '.join(parts) or '版本信息缺失'}"
                )
            prepared.append(record)
        return prepared

    @staticmethod
    def _aggregate_skip_reasons(records: List[Dict[str, Any]]) -> Dict[str, int]:
        """把跳过原因的明细归一化成「冒号前的原因头 → 条数」分布。

        归一化只取冒号前的原因头（如 ``同集已有更优版本：同评分且候选文件未更大`` →
        ``同集已有更优版本``），保留原文里的关键信息便于定位；无原因记「未标注」。
        """
        reasons: Dict[str, int] = {}
        for record in records or []:
            if str(record.get("status") or "") != "跳过":
                continue
            raw = str(record.get("skip_reason") or "").strip()
            if not raw:
                label = "未标注"
            else:
                head = re.split(r"[:：]", raw, maxsplit=1)[0].strip()
                label = head or raw
            reasons[label] = reasons.get(label, 0) + 1
        return reasons

    @staticmethod
    def _landed_result_fields(info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """把落盘自检结果转成历史字段；拿不到实测数据时不加字段（维持旧行为）。"""
        if not isinstance(info, dict) or info.get("landed") is None:
            return {}
        return {
            "landed": bool(info.get("landed")),
            "landed_reason": str(info.get("reason") or ""),
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def _history_display_priority(record: Dict[str, Any]) -> int:
        """同一集有多条历史时，优先展示已成功/正在处理的记录。"""
        return {
            "成功": 5,
            "处理中": 4,
            "下载中": 4,
            "待确认": 3,
            "失败": 2,
            "跳过": 1,
        }.get(str(record.get("status") or ""), 0)

    @staticmethod
    def prepare_history_group(group: Dict[str, Any]) -> Dict[str, Any]:
        """补充单个数据库媒体组的展示字段，不在应用层重新分组。"""
        prepared = copy.deepcopy(group)
        raw_records = prepared.get("records") or []
        first = raw_records[0] if raw_records else {}
        is_tv = (prepared.get("type") or first.get("type")) != "电影"
        records = []
        episode_positions: Dict[Tuple[int, int], int] = {}
        for record in raw_records:
            if is_tv:
                season = int(record.get("season") or 0)
                episode = int(record.get("episode") or 0)
                ep_key = (season, episode)
                if episode > 0:
                    position = episode_positions.get(ep_key)
                    if position is not None:
                        # 数据库里同集可能保留多条重试记录。旧失败记录若排在
                        # 新成功记录前面，会把整集显示成失败；这里按状态优先级
                        # 选代表记录，避免「实际已成功却一直红着」。
                        if (
                            HistoryService._history_display_priority(record)
                            > HistoryService._history_display_priority(
                                records[position]
                            )
                        ):
                            records[position] = record
                        continue
                    episode_positions[ep_key] = len(records)
            records.append(record)
        prepared["records"] = records
        prepared["success_count"] = sum(1 for r in records if r.get("status") == "成功")
        prepared["pending_count"] = sum(1 for r in records if r.get("status") in {"处理中", "下载中"})
        prepared["failed_count"] = sum(
            1 for r in records
            if r.get("status") == "失败"
            or (
                r.get("status") not in {"成功", "处理中", "下载中", "跳过"}
                and r.get("status") != "待确认"
            )
        )
        prepared["skipped_count"] = sum(1 for r in records if r.get("status") == "跳过")
        prepared["unverified_count"] = sum(
            1 for r in records if r.get("status") == "待确认"
        )
        prepared["skip_reasons"] = HistoryService._aggregate_skip_reasons(records)
        prepared["unverified_records"] = [
            {
                "season": int(r.get("season") or 0),
                "episode": int(r.get("episode") or 0),
                "file_name": str(r.get("file_name") or ""),
                "reason": str(r.get("landed_reason") or ""),
            }
            for r in records
            if r.get("status") == "待确认"
        ][:20]
        prepared["total_size"] = sum(int(r.get("file_size") or 0) for r in records)
        prepared.update({
            "tmdb_id": first.get("tmdb_id"),
            "title": first.get("title") or "未知媒体",
            "year": first.get("year") or "",
            "type": first.get("type") or "未知类型",
            "resource_types": list(dict.fromkeys(
                str(record.get("resource_type") or "unknown")
                for record in records
                if str(record.get("resource_type") or "").strip()
            )),
        })
        prepared["sources"] = list(dict.fromkeys(
            "manual"
            if str(record.get("source") or "").strip().casefold()
               in {"manual", "手动添加", "手动资源"}
            else str(record.get("source") or "unknown").strip().casefold()
            for record in records
        ))
        prepared["source_items"] = [
            {"value": source} for source in prepared["sources"]
        ]
        prepared["seasons"] = sorted({
            int(record.get("season") or 0)
            for record in records
            if str(record.get("season") or "").isdigit()
               and int(record.get("season") or 0) > 0
        })
        prepared["resource_link_count"] = len({
            str(record.get("resource_link") or "").strip()
            for record in records
            if str(record.get("resource_link") or "").strip()
        })
        prepared["notification_record"] = max(
            (
                record for record in records
                if record.get("status") == "成功" and not record.get("finalize_key")
            ),
            key=lambda record: str(record.get("time") or ""),
            default=None,
        )
        prepared["selectable"] = bool(records)
        prepared["deletable"] = bool(records) and all(
            record.get("status") in {"成功", "失败", "跳过"}
            or bool(record.get("finalize_key"))
            for record in records
        )
        return prepared

    def reconcile_orphaned_history(self) -> int:
        """将 pending 已消失但 STRM 已存在的假下载中记录纠正为成功。"""
        if (
                not self._get_data
                or not self._save_data
                or not self._strm_generator
                or not self._local_resource_path
        ):
            return 0
        repaired = 0
        repaired_records = []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            history = self._get_data("history") or []
            for record in history:
                if str(record.get("status") or "") not in {"下载中", "处理中"}:
                    continue
                pending_key = str(record.get("finalize_key") or "")
                if not pending_key:
                    pending_key = self._offline_hash(str(record.get("share_url") or ""))
                if pending_key and pending_key in pending:
                    continue
                cloud_dir = str(record.get("cloud_dir") or "").strip()
                file_name = str(record.get("file_name") or "").strip()
                if not cloud_dir or not file_name:
                    continue
                try:
                    strm_path = self._strm_generator.local_path(
                        local_root=self._local_resource_path,
                        cloud_root=self._CLOUD_MEDIA_ROOT,
                        cloud_dir=cloud_dir,
                        file_name=file_name,
                    )
                except Exception as error:
                    logger.debug(f"检查遗留历史 STRM 失败：{file_name}，{error}")
                    continue
                if not strm_path.is_file():
                    continue
                record["status"] = "成功"
                record.pop("finalize_key", None)
                clear_failure(record)
                repaired_records.append(copy.deepcopy(record))
                repaired += 1
            if repaired:
                self._save_data("history", history)
        if repaired:
            logger.info(f"已自动修复 {repaired} 条 STRM 已存在但状态未完成的历史记录")
            self._record_platform_transfer_histories(repaired_records)
            if self._history_changed:
                self._history_changed()
        return repaired

    def _pending_history_record(self, pending_key: str) -> Optional[Dict[str, Any]]:
        history = (self._get_data("history") or []) if self._get_data else []
        return next(
            (
                record
                for record in history
                if str(record.get("finalize_key") or "") == pending_key
                   or pending_key.upper()
                   in str(record.get("share_url") or "").upper()
            ),
            None,
        )

    def _restore_pending_media_context(
            self, item: Dict[str, Any], pending_key: str
    ) -> Tuple[Optional[MediaInfo], Dict[str, Any]]:
        media_data = item.get("mediainfo") or {}
        if media_data:
            try:
                return self._deserialize_mediainfo(media_data), media_data
            except Exception as error:
                logger.warning(f"网盘文件后处理媒体信息已失效，将重新识别：{error}")

        record = self._pending_history_record(pending_key)
        if not record:
            logger.warning(f"网盘文件后处理缺少对应历史记录：{pending_key}")
            return None, {}
        try:
            source_name = str(
                record.get("source_file_name") or record.get("file_name") or ""
            )
            context = self._resolve_history_retry_context(record, source_name)
            mediainfo = context["mediainfo"]
            subscribe = context.get("subscribe")
            episode = context.get("episode")
            item["subscribe_id"] = getattr(subscribe, "id", None)
            item["success_episodes"] = (
                [episode]
                if mediainfo.type == MediaType.TV and episode
                else [1]
            )
            if mediainfo.type == MediaType.TV:
                item["season"] = normalize_season(context.get("season"))
                item["notification_episodes"] = [episode] if episode else []
            media_data = self._serialize_mediainfo(mediainfo)
            item["mediainfo"] = media_data
            logger.debug(
                f"已从历史记录恢复网盘文件后处理媒体信息：{mediainfo.title_year}"
            )
            return mediainfo, media_data
        except Exception as error:
            logger.warning(f"恢复网盘文件后处理媒体信息失败：{error}")
            return None, {}

    def _notify_pending_file_finalized(
            self,
            item: Dict[str, Any],
            pending_key: str,
            strm_path: Optional[Path],
            mediainfo: Optional[MediaInfo] = None,
            media_data: Optional[Dict[str, Any]] = None,
            finish_subscription: bool = True,
            subscribe_cache: Optional[Dict[int, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        if mediainfo is None:
            mediainfo, restored_data = self._restore_pending_media_context(
                item, pending_key
            )
            media_data = media_data or restored_data
        else:
            media_data = media_data or self._serialize_mediainfo(mediainfo)
        subscribe = None
        subscribe_id = int(item.get("subscribe_id") or 0)
        if subscribe_id:
            if subscribe_cache is not None:
                subscribe = subscribe_cache.get(subscribe_id)
            else:
                try:
                    subscribe = SubscribeOper().get(subscribe_id)
                except Exception as error:
                    logger.debug(f"读取后处理订阅失败：{subscribe_id}，{error}", exc_info=True)
        sub_title = getattr(subscribe, "name", None) or item.get("name") or item.get("title")
        target_subscribe = subscribe or SimpleNamespace(
            name=sub_title or (mediainfo.title if mediainfo else ""),
            year=item.get("year") or (mediainfo.year if mediainfo else ""),
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

        notify_path = strm_path
        if not notify_path and mediainfo and self._local_resource_path:
            notify_path = self._resolve_resource_season_dir(
                self._local_resource_path,
                target_subscribe,
                mediainfo,
                normalize_season(item.get("season")),
            )
        if notify_path and mediainfo:
            scheduled = self._media_server_notifier.notify(
                path=notify_path,
                mediainfo=mediainfo,
                file_name=str(item.get("file_name") or ""),
            )
            if scheduled:
                logger.debug(
                    f"已加入媒体目录通知批次：{item.get('file_name') or Path(notify_path).name}"
                )
        elif strm_path:
            logger.warning(
                f"STRM 已生成但缺少媒体信息，无法发送入库通知：{strm_path}"
            )

        if finish_subscription and not item.get("transient_target"):
            self._finish_pending_subscription(
                item, media_data or {}, mediainfo=mediainfo
            )
        if not mediainfo:
            logger.warning("文件已完成，但缺少媒体信息，无法发送完成通知和Webhook")
            return None
        history_record = self._pending_history_record(pending_key) or {}
        display_title = str(
            getattr(subscribe, "name", None)
            or (target_subscribe and getattr(target_subscribe, "name", None))
            or item.get("name")
            or item.get("title")
            or mediainfo.title
        ).strip()
        detail = {
            "type": "电视剧" if mediainfo.type == MediaType.TV else "电影",
            "title": display_title,
            "year": getattr(target_subscribe, "year", None) or mediainfo.year,
            "image": getattr(mediainfo, "get_poster_image", lambda: None)(),
            "file_name": item.get("file_name"),
            "size": self._detail_size(history_record, item),
            "source": self._detail_source_text(history_record or item),
            "notification_kind": (
                "upgrade"
                if self._is_upgrade_history(history_record)
                else "cross_transfer"
                if history_record.get("transfer_mode") == "cross"
                else "transfer"
            ),
        }
        episodes = []
        episode_values = (
                item.get("notification_episodes")
                or item.get("success_episodes")
                or (
                    [history_record.get("episode")]
                    if history_record.get("episode")
                    else []
                )
        )
        for value in episode_values:
            try:
                episode = int(str(value or "0"))
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.append(episode)
        if mediainfo.type == MediaType.TV:
            detail["season"] = max(
                1,
                int(
                    item.get("season")
                    or history_record.get("season")
                    or getattr(mediainfo, "season", 0)
                    or 1
                ),
            )
            detail["episodes"] = episodes
        return detail

    @staticmethod
    def _aggregate_transfer_details(
            transfer_details: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """按电视剧标题、年份和季聚合延迟完成的文件。"""
        aggregated: List[Dict[str, Any]] = []
        tv_index: Dict[Tuple[str, str, int, str], Dict[str, Any]] = {}
        episode_sets: Dict[Tuple[str, str, int, str], Set[int]] = {}
        file_name_sets: Dict[Tuple[str, str, int, str], Set[str]] = {}
        for raw_detail in transfer_details:
            detail = copy.deepcopy(raw_detail)
            if detail.get("type") != "电视剧":
                aggregated.append(detail)
                continue
            season = normalize_season(detail.get("season"))
            detail["season"] = season
            detail["episodes"] = sorted(
                {
                    int(episode)
                    for episode in (detail.get("episodes") or [])
                    if int(episode) > 0
                }
            )
            key = (
                str(detail.get("title") or ""),
                str(detail.get("year") or ""),
                season,
                str(detail.get("notification_kind") or "transfer"),
            )
            current = tv_index.get(key)
            if current is None:
                file_name = str(detail.get("file_name") or "").strip()
                if file_name:
                    detail["file_names"] = [file_name]
                    file_name_sets[key] = {file_name}
                episode_sets[key] = set(detail["episodes"])
                tv_index[key] = detail
                aggregated.append(detail)
                continue
            episode_sets[key].update(detail["episodes"])
            file_name = str(detail.get("file_name") or "").strip()
            if file_name:
                file_names = current.setdefault("file_names", [])
                known_names = file_name_sets.setdefault(key, set(file_names))
                if file_name not in known_names:
                    known_names.add(file_name)
                    file_names.append(file_name)
            if not current.get("image") and detail.get("image"):
                current["image"] = detail["image"]
            current["size"] = int(current.get("size") or 0) + int(
                detail.get("size") or 0
            )
            if not current.get("source") and detail.get("source"):
                current["source"] = detail["source"]
        for key, detail in tv_index.items():
            detail["episodes"] = sorted(episode_sets[key])
        return aggregated

    def _send_finalized_batch(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        if not transfer_details:
            return
        aggregated = self._aggregate_transfer_details(transfer_details)
        total_count = len(transfer_details)
        if self._notify:
            try:
                self.send_transfer_notification(aggregated, total_count)
            except Exception as error:
                logger.warning(f"文件完成通知入队失败：{error}")
        if self._file_finalized:
            try:
                self._file_finalized(aggregated, total_count)
            except Exception as error:
                logger.warning(f"文件完成汇总 Webhook 发送失败：{error}")
        logger.debug(
            f"网盘文件后处理完成通知已入队：{total_count} 个文件，"
            f"{len(aggregated)} 个媒体项"
        )

    def is_offline_task_finalized(self, info_hash: str) -> bool:
        """历史记录中存在该磁力任务且状态为成功时视为已入库。

        115 秒传命中时文件立即可用，但 115 云下载任务条目的状态可能长时间停留在
        「等待下载」，故此判定仅用于展示层标注，不参与任何完成/失败判定。
        """
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(info_hash or "")).upper()
        if not normalized or not self._get_data:
            return False
        try:
            history = self._get_data("history") or []
            for item in history:
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "") != "成功":
                    continue
                link = str(item.get("share_url") or "").upper()
                if normalized in link:
                    return True
        except Exception as error:
            logger.debug(f"查询离线任务入库状态失败：{error}")
            return False
        return False

    def _mark_offline_history_status(
            self, pending_key: str, status: str, reason: str = "",
            updates: Optional[Dict[str, Any]] = None,
    ) -> int:
        batch_updates = {pending_key: updates} if updates else None
        return self._mark_offline_history_status_batch({pending_key}, status, reason, updates=batch_updates)

    def _mark_offline_history_status_batch(
            self, pending_keys: Set[str], status: str, reason: str = "",
            updates: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> int:
        """一次扫描并持久化多个离线任务对应的历史记录，返回命中的记录数。"""
        if not self._get_data or not self._save_data:
            return 0
        normalized_keys = {
            str(value or "").strip() for value in pending_keys
            if str(value or "").strip()
        }
        if not normalized_keys:
            return 0
        uppercase_keys = {value.upper() for value in normalized_keys}
        platform_records = []
        success_records = []
        matched_records = 0
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            # 文件级兜底匹配：历史记录缺 finalize_key（或值不同）时，
            # 用 (file_name, cloud_dir) 精确相等定位，禁止模糊/包含匹配。
            fallback_pairs: Dict[Tuple[str, str], str] = {}
            # 目录+季集兜底：平台侧历史记录的可能是带集名的正式名
            # （「金银岛 - S01E02 - 第 2 集.mkv」），而 pending 里存的是上游
            # 资源名（「金银岛 - S01E02.mp4」），文件名对不上时按
            # 「同一目标目录 + 同一季集号」定位，避免终态永远写不进去而
            # 每 60 秒重排队（1.7.37 修复）。
            fallback_episodes: Dict[Tuple[str, int, int], str] = {}
            if isinstance(pending, dict):
                for key in normalized_keys:
                    entry = pending.get(key)
                    if not isinstance(entry, dict):
                        continue
                    pair = (
                        str(entry.get("file_name") or "").strip(),
                        str(entry.get("cloud_dir") or "").strip(),
                    )
                    if pair[0] and pair[1]:
                        fallback_pairs.setdefault(pair, key)
                    try:
                        episode_key = (
                            str(entry.get("cloud_dir") or "").strip(),
                            int(entry.get("season") or 0),
                            int(entry.get("episode") or 0),
                        )
                    except (TypeError, ValueError):
                        episode_key = ("", 0, 0)
                    if episode_key[0] and episode_key[1] and episode_key[2]:
                        fallback_episodes.setdefault(episode_key, key)
            history = self._get_data("history") or []
            changed = False
            for item in history:
                link = str(item.get("share_url") or "").upper()
                item_key = str(item.get("finalize_key") or "")
                matched_key = None
                if item_key in normalized_keys:
                    matched_key = item_key
                elif any(value in link for value in uppercase_keys):
                    matched_key = next((k for k in normalized_keys if k.upper() in link), None)
                if not matched_key and fallback_pairs:
                    record_pair = (
                        str(item.get("file_name") or "").strip(),
                        str(item.get("cloud_dir") or "").strip(),
                    )
                    if record_pair[0] and record_pair[1]:
                        matched_key = fallback_pairs.get(record_pair)
                if not matched_key and fallback_episodes:
                    try:
                        record_episode = (
                            str(item.get("cloud_dir") or "").strip(),
                            int(item.get("season") or 0),
                            int(item.get("episode") or 0),
                        )
                    except (TypeError, ValueError):
                        record_episode = ("", 0, 0)
                    if record_episode[0] and record_episode[1] and record_episode[2]:
                        matched_key = fallback_episodes.get(record_episode)
                if not matched_key:
                    continue
                matched_records += 1
                if item.get("manual_success"):
                    # 用户已手动定案为成功：自动终态与落盘自检不得再覆盖，
                    # 但仍计为已匹配，避免调用方误判「历史尚未落盘」而反复重排队。
                    continue
                if status == "失败" and item.get("status") == "成功":
                    continue
                item["status"] = status
                item.pop("finalize_key", None)
                if reason:
                    item["failure_reason"] = reason
                else:
                    item.pop("failure_reason", None)
                if updates and matched_key in updates and updates[matched_key]:
                    for up_key, up_val in updates[matched_key].items():
                        if up_val is not None:
                            item[up_key] = copy.deepcopy(up_val)
                if status == "成功":
                    success_records.append(item)
                changed = True
            # 1.6.52 落盘自检：成功终态先核对目标目录里真有实物，查无实据记「待确认」。
            if success_records:
                verify_records = getattr(self, "verify_landed_records", None)
                landed_infos = (
                    verify_records(success_records)
                    if callable(verify_records) else {}
                )
                for item in success_records:
                    info = landed_infos.get(id(item)) or {}
                    if info.get("landed") is False:
                        item["status"] = "待确认"
                        item.update(HistoryService._landed_result_fields(info))
                        logger.warning(
                            f"落盘自检未通过：{item.get('title') or '未知媒体'} "
                            f"S{int(item.get('season') or 0):02d}"
                            f"E{int(item.get('episode') or 0):02d} "
                            f"{item.get('file_name') or '未记录'} 不在目标季目录"
                            f"（{info.get('reason') or 'missing_in_dir'}），记「待确认」"
                        )
                        continue
                    fields = HistoryService._landed_result_fields(info)
                    if fields:
                        item.update(fields)
                    platform_records.append(copy.deepcopy(item))
            if changed:
                self._save_data("history", history)
        self._record_platform_transfer_histories(platform_records)
        if changed and self._history_changed:
            self._history_changed()
        return matched_records

    @staticmethod
    def _history_has_episode(
            history: List[dict],
            mediainfo: MediaInfo,
            season: int,
            episode: int,
    ) -> bool:
        """判断历史里是否已有「同一媒体 + 同一季集」的记录（失败记录不算）。"""
        present_states = {"成功", "处理中", "下载中", "待确认"}
        tmdb_id = str(getattr(mediainfo, "tmdb_id", "") or "")
        title = str(getattr(mediainfo, "title", "") or "").strip()
        for record in history or []:
            try:
                if int(record.get("season") or 0) != int(season):
                    continue
                if int(record.get("episode") or 0) != int(episode):
                    continue
            except (TypeError, ValueError):
                continue
            if str(record.get("status") or "") not in present_states:
                continue
            record_tmdb = str(record.get("tmdb_id") or "")
            if tmdb_id and record_tmdb:
                if tmdb_id == record_tmdb:
                    return True
                continue
            if title and str(record.get("title") or "").strip() == title:
                return True
        return False

    def _append_backfill_history_record(
            self,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            episode: int,
            file_name: str,
            cloud_dir: str,
            file_size: int = 0,
            share_url: str = "",
            status: str = "成功",
            reason: str = "历史记录缺失补写",
    ) -> Optional[Dict[str, Any]]:
        """按已知的落盘事实补写一条历史记录，返回新记录（重复或数据不足时返回 None）。

        转存链路偶发「文件已落盘、历史记录却从未写入」：1.7.101 及之前后处理遇到
        这种情况只会打「历史记录长时间缺失，放弃写入终态」然后丢弃终态，导致
        历史页缺口、判重基线缺体积、深度联动删除匹配不到来源。补写比丢弃安全。
        """
        if not self._save_data or not mediainfo or not subscribe:
            return None
        file_name = str(file_name or "").strip()
        cloud_dir = str(cloud_dir or "").strip()
        try:
            episode_number = int(episode or 0)
            season_number = max(1, int(season or 1))
        except (TypeError, ValueError):
            return None
        if not file_name or not cloud_dir or episode_number <= 0:
            return None
        now_text = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            record = self._build_transfer_history_item(
                mediainfo=mediainfo,
                subscribe=subscribe,
                status=status,
                share_url=str(share_url or ""),
                file_name=file_name,
                source_file_name=file_name,
                cloud_dir=cloud_dir,
                season=season_number,
                episode=episode_number,
                file_size=int(file_size or 0),
                landed=status == "成功",
                landed_reason=reason,
                verified_at=now_text,
                backfilled=True,
            )
        except Exception as error:
            logger.warning(f"补写历史记录失败：{error}", exc_info=True)
            return None
        # 历史是「读整份 → 改 → 整份写回」的结构，补写必须与其它写回方共用同一把锁，
        # 否则会覆盖掉并发任务刚写进去的记录（1.7.102 曾用任务级 sync_lock 挡，
        # 结果转存任务跑着就补不了历史）。
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            target_key = (cloud_dir, file_name, season_number, episode_number)
            existing = {
                (
                    str(record.get("cloud_dir") or "").strip(),
                    str(record.get("file_name") or "").strip(),
                    int(record.get("season") or 0),
                    int(record.get("episode") or 0),
                )
                for record in history
            }
            if target_key in existing:
                return None
            # 1.7.104：补写只填「这一集完全没记录」的空档。既有记录可能来自别的目录/
            # 别的来源名（例如老的转存记录在媒体库目录、源名带后缀），只比
            # (目录, 文件名) 会漏判，导致同一集被补成两条（1.7.103 实测 191 条里有
            # 158 条是重复）。这里按「同一媒体 + 同一季集」判重。
            if self._history_has_episode(
                history, mediainfo, season_number, episode_number
            ):
                return None
            history.append(record)
            self._save_data("history", history)
        logger.info(
            f"历史记录缺失已补写：{record.get('title') or mediainfo.title} "
            f"S{season_number:02d}E{episode_number:02d} {file_name}（{reason}）"
        )
        if self._history_changed:
            try:
                self._history_changed()
            except Exception as error:
                logger.debug(f"补写历史记录后刷新前端失败：{error}", exc_info=True)
        return record

    def _backfill_history_from_pending(self, item: Dict[str, Any]) -> int:
        """用后处理项自身携带的数据补写缺失的历史记录，返回补写条数。"""
        if not isinstance(item, dict):
            return 0
        media_data = item.get("mediainfo") or {}
        if not media_data:
            return 0
        try:
            mediainfo = self._deserialize_mediainfo(dict(media_data))
        except Exception as error:
            logger.debug(f"补写历史记录时解析媒体信息失败：{error}", exc_info=True)
            return 0
        if not mediainfo:
            return 0
        subscribe = None
        subscribe_id = item.get("subscribe_id")
        if subscribe_id:
            try:
                subscribe = SubscribeOper().get(int(subscribe_id))
            except Exception as error:
                logger.debug(f"补写历史记录时读取订阅失败：{error}", exc_info=True)
        if not subscribe:
            subscribe = SimpleNamespace(
                id=int(subscribe_id or 0),
                name=str(getattr(mediainfo, "title", "") or ""),
                season=int(item.get("season") or 1),
                start_episode=1,
                total_episode=0,
                custom_words=None,
            )
        episodes = []
        for value in (item.get("success_episodes") or []) + [item.get("episode")]:
            try:
                episode = int(value or 0)
            except (TypeError, ValueError):
                continue
            if episode > 0:
                episodes.append(episode)
        if not episodes:
            return 0
        written = 0
        for episode in sorted(set(episodes)):
            record = self._append_backfill_history_record(
                mediainfo=mediainfo,
                subscribe=subscribe,
                season=int(item.get("season") or 1),
                episode=episode,
                file_name=str(item.get("file_name") or ""),
                cloud_dir=str(item.get("cloud_dir") or ""),
                file_size=int(item.get("file_size") or 0),
                share_url=str(item.get("share_url") or ""),
                reason="后处理补写（终态找不到历史记录）",
            )
            if record:
                written += 1
        return written

    def backfill_history_from_cloud(self, keyword: str = "") -> Dict[str, Any]:
        """扫订阅季目录，把「网盘有、历史无」的集补写成成功记录。"""
        from app.schemas.types import MediaType as _MediaType

        if not self._save_data:
            return {"success": False, "message": "数据通道未就绪"}
        try:
            subscribes = SubscribeOper().list("N,R") or []
        except Exception as error:
            return {"success": False, "message": f"读取订阅失败：{error}"}
        keyword = str(keyword or "").strip()
        scanned = 0
        written = 0
        details: List[Dict[str, Any]] = []
        for subscribe in subscribes:
            name = str(getattr(subscribe, "name", "") or "")
            if keyword and keyword not in name:
                continue
            if getattr(subscribe, "type", None) != _MediaType.TV.value:
                continue
            mediainfo = self._subscribe_mediainfo(subscribe, _MediaType.TV)
            if not mediainfo:
                continue
            season = max(1, int(getattr(subscribe, "season", 1) or 1))
            try:
                valid, cloud_files, cloud_dir = self._scan_cloud_resource_episode_files(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=max(1, int(getattr(subscribe, "start_episode", 1) or 1)),
                    total_episode=max(0, int(getattr(subscribe, "total_episode", 0) or 0)),
                )
            except Exception as error:
                logger.debug(f"补写历史记录时列目录失败：{name}，{error}", exc_info=True)
                continue
            if not valid or not cloud_files:
                continue
            scanned += 1
            title_written = 0
            for episode, cloud_file in sorted(cloud_files.items()):
                record = self._append_backfill_history_record(
                    mediainfo=mediainfo,
                    subscribe=subscribe,
                    season=season,
                    episode=int(episode),
                    file_name=str(getattr(cloud_file, "name", "") or ""),
                    cloud_dir=str(cloud_dir or ""),
                    file_size=int(getattr(cloud_file, "size", 0) or 0),
                    reason="季目录实测补写",
                )
                if record:
                    title_written += 1
            if title_written:
                written += title_written
                details.append({
                    "title": getattr(mediainfo, "title", name),
                    "season": season,
                    "written": title_written,
                })
        return {
            "success": True,
            "message": (
                f"扫描 {scanned} 个订阅季目录，补写 {written} 条历史记录"
                if written else
                f"扫描 {scanned} 个订阅季目录，没有需要补写的历史记录"
            ),
            "data": {"scanned": scanned, "written": written, "details": details},
        }

    def get_pending_finalize_tasks(self) -> List[Dict[str, Any]]:
        """返回等待115文件就绪、重命名或生成STRM的持久任务。"""
        if not self._get_data:
            return []
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            return [copy.deepcopy({**item, "pending_key": key}) for key, item in pending.items()]

    def reconcile_orphan_finalize_records(self) -> int:
        """修复「后处理已提交但历史终态没写入」的记录，避免历史长期停在处理中。"""
        if not self._get_data or not self._save_data:
            return 0
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            repaired = 0
            repaired_records = []
            for item in history:
                if str(item.get("status") or "") not in {"处理中", "下载中"}:
                    continue
                key = str(item.get("finalize_key") or "").strip()
                if not key or key in pending:
                    continue
                item["status"] = "成功"
                item.pop("finalize_key", None)
                item.pop("failure_reason", None)
                repaired_records.append(item)
                repaired += 1
            # 1.6.52 落盘自检：兜底修复同样过闸，查无实据记「待确认」而非「成功」。
            if repaired_records:
                verify_records = getattr(self, "verify_landed_records", None)
                landed_infos = (
                    verify_records(repaired_records)
                    if callable(verify_records) else {}
                )
                for item in repaired_records:
                    info = landed_infos.get(id(item)) or {}
                    if info.get("landed") is False:
                        item["status"] = "待确认"
                        item.update(HistoryService._landed_result_fields(info))
                    else:
                        fields = HistoryService._landed_result_fields(info)
                        if fields:
                            item.update(fields)
            if repaired:
                self._save_data("history", history)
        if repaired:
            logger.warning(
                f"已修复 {repaired} 条历史记录状态：文件后处理已完成但终态未写入历史"
            )
            if self._history_changed:
                self._history_changed()
        return repaired

    def delete_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> int:
        """删除用户明确取消的待后处理任务，并结束对应历史状态。"""
        removed_keys, pending_count = self._remove_pending_finalize_tasks(
            pending_keys,
            task_ids=task_ids,
        )
        if not removed_keys:
            return 0
        self._mark_offline_history_status_batch(
            removed_keys, "失败", "后处理任务已由用户删除"
        )
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def stop_pending_finalize_tasks(self, pending_keys: Set[str]) -> int:
        """安全停止后移除尚未提交的后处理任务，并记录准确的停止原因。"""
        removed_keys, pending_count = self._remove_pending_finalize_tasks(
            pending_keys
        )
        if not removed_keys:
            return 0
        self._mark_offline_history_status_batch(
            removed_keys, "失败", "后处理任务已由用户停止"
        )
        self._notify_offline_pending_changed(pending_count)
        return len(removed_keys)

    def _remove_pending_finalize_tasks(
            self,
            pending_keys: Set[str],
            task_ids: Optional[Set[str]] = None,
    ) -> Tuple[Set[str], int]:
        """仅移除待后处理状态；调用方负责处理历史记录和状态通知。"""
        keys = {str(value or "").strip() for value in pending_keys if str(value or "").strip()}
        normalized_task_ids = {
            str(value or "").strip().upper()
            for value in (task_ids or set())
            if str(value or "").strip()
        }
        if (not keys and not normalized_task_ids) or not self._get_data:
            return set(), 0
        with self._offline_pending_lock:
            pending = self._get_data(self._OFFLINE_PENDING_KEY) or {}
            removed_keys = {
                key for key, item in pending.items()
                if key in keys
                   or str((item or {}).get("task_id") or "").strip().upper()
                   in normalized_task_ids
            }
            if not removed_keys:
                return set(), len(pending)
            for key in removed_keys:
                pending.pop(key, None)
            self._save_offline_pending(pending)
            return removed_keys, len(pending)

    @staticmethod
    def _history_record_matches(
            record: Dict[str, Any], identity: Dict[str, Any]
    ) -> bool:
        for key in (
                "time",
                "share_url",
                "file_name",
                "tmdb_id",
                "season",
                "episode",
        ):
            expected = identity.get(key)
            if expected in (None, "") and key not in {"time", "file_name"}:
                continue
            if str(record.get(key) or "") != str(expected or ""):
                return False
        return True

    @staticmethod
    def _history_record_deletable(record: Dict[str, Any]) -> bool:
        return (
                str(record.get("status") or "") in {"成功", "失败", "跳过"}
                or bool(record.get("finalize_key"))
        )

    @staticmethod
    def _transient_target_defaults() -> Dict[str, Any]:
        return {
            "id": 0,
            "doubanid": None,
            "bangumiid": None,
            "anilistid": None,
            "media_source": None,
            "media_id": None,
            "media_category": None,
            "media_category_id": None,
            "episode_group": None,
            "filter_groups": None,
            "sites": None,
            "include": None,
            "exclude": None,
            "quality": None,
            "resolution": None,
            "effect": None,
            "audio_quality": None,
            "audio_format": None,
            "min_bitrate": None,
            "min_bit_depth": None,
            "min_sample_rate": None,
            "filter": None,
            "custom_words": None,
            "save_path": None,
            "downloader": None,
            "keyword": None,
            "note": [],
            "episode_priority": {},
            "best_version": False,
            "best_version_full": None,
            "current_priority": None,
            "search_imdbid": None,
            "search_interval": None,
            "manual_total_episode": None,
            "total_tracks": None,
            "music_type": None,
            "poster": None,
            "date": None,
            "username": None,
            "state": "N",
        }

    def build_transient_media_target(
            self,
            media: Dict[str, Any],
            target_id: int = -1,
            episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_baseline: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """将订阅外媒体转换为现有同步链可消费的临时目标。"""
        media_type = (
            MediaType.TV
            if str(media.get("media_type") or media.get("type") or "").strip().lower()
               in {"tv", MediaType.TV.value.lower()}
            else MediaType.MOVIE
        )
        title = str(media.get("title") or media.get("name") or "").strip()
        tmdb_id = positive_int(media.get("tmdb_id") or media.get("tmdbid"))
        if not title or not tmdb_id:
            raise ValueError("媒体目标缺少标题或 TMDB ID")

        season = normalize_season(media.get("season")) if media_type == MediaType.TV else None
        selected_episodes = sorted({
            int(value)
            for value in (
                episodes if episodes is not None else media.get("episodes") or []
            )
            if int(value) > 0
        })

        target_data = self._transient_target_defaults()
        target_data.update({
            "id": int(target_id),
            "name": title,
            "year": media.get("year"),
            "type": media_type.value,
            "tmdbid": tmdb_id,
            "doubanid": media.get("douban_id") or media.get("doubanid"),
            "bangumiid": media.get("bangumi_id") or media.get("bangumiid"),
            "season": season,
            "start_episode": min(selected_episodes) if selected_episodes else 1,
            "total_episode": max(selected_episodes) if selected_episodes else 0,
            "lack_episode": len(selected_episodes) if media_type == MediaType.TV else 0,
            "episode_group": media.get("episode_group"),
            "_transient_target": True,
            "_manual_upgrade": bool(manual_upgrade),
            "_target_episodes": list(selected_episodes),
            "_manual_media_baseline": dict(manual_baseline or {}),
        })
        target = SimpleNamespace(**target_data)
        target.to_dict = lambda: {
            k: list(v) if isinstance(v, (set, frozenset)) else v
            for k, v in target.__dict__.items()
        }
        return target

    def _history_upgrade_records(
            self, identities: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        if not self._get_data:
            raise RuntimeError("历史记录存储未初始化")
        records = self._get_data("history") or []
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        selected = []
        for record in records:
            record_id = self._ensure_history_record_id(record)
            if record_id in target_ids or any(
                    self._history_record_matches(record, identity)
                    for identity in identities
            ):
                if str(record.get("status") or "") == "成功" and not record.get("finalize_key"):
                    selected.append(copy.deepcopy(record))
        if not selected:
            raise LookupError("所选历史记录不存在或没有可洗版的成功内容")
        return selected

    def resolve_history_upgrade_targets(
            self, identities: List[Dict[str, Any]]
    ) -> List[Any]:
        records = self._history_upgrade_records(identities)
        grouped: Dict[Tuple[str, str, int], List[Dict[str, Any]]] = {}
        for record in records:
            media_type = str(record.get("type") or "")
            if media_type not in {MediaType.TV.value, MediaType.MOVIE.value}:
                continue
            media_key = str(record.get("tmdb_id") or "").strip() or (
                f"{record.get('title') or ''}|{record.get('year') or ''}"
            )
            season = normalize_season(record.get("season")) if media_type == MediaType.TV.value else 0
            grouped.setdefault((media_type, media_key, season), []).append(record)

        targets = []
        for index, ((media_type, _, season), group) in enumerate(grouped.items(), start=1):
            sample = group[0]
            episodes = {
                episode
                for record in group
                for episode in self._platform_episode_numbers(record)
            }
            target = self.build_transient_media_target(
                {
                    "title": sample.get("title"),
                    "year": sample.get("year"),
                    "media_type": media_type,
                    "tmdb_id": sample.get("tmdb_id"),
                    "douban_id": sample.get("douban_id"),
                    "season": season or None,
                    "episode_group": sample.get("episode_group"),
                },
                target_id=-index,
                episodes=episodes,
                manual_upgrade=True,
            )
            targets.append(target)
        if not targets:
            raise ValueError("所选记录缺少可识别的电影或电视剧媒体信息")
        return targets

    def _media_server_services(self) -> Dict[str, Any]:
        services = MediaServerHelper().get_services() or {}
        return {
            name: service for name, service in services.items()
            if service and not service.instance.is_inactive()
        }

    @staticmethod
    def _media_server_storage_name(name: str, service: Any) -> str:
        """返回媒体库同步表使用的服务器类型标识。"""
        return str(getattr(service, "type", "") or name or "").strip().casefold()

    @staticmethod
    def _normalize_media_path(value: Any) -> str:
        """归一化媒体路径：统一分隔符并去掉尾部斜杠。"""
        return str(value or "").strip().replace("\\", "/").rstrip("/")

    def _parsed_media_server_path_mappings(self) -> List[Tuple[str, str]]:
        """解析「媒体服务器路径=本地路径」映射（长前缀优先）。"""
        raw = str(getattr(self, "_media_server_path_mappings", "") or "")
        if not raw:
            notifier = getattr(self, "_media_server_notifier", None)
            raw = str(getattr(notifier, "path_mappings", "") or "")
        mappings: List[Tuple[str, str]] = []
        for line in raw.splitlines():
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            separator = "=" if "=" in text else ("#" if "#" in text else "")
            if not separator:
                continue
            left, _, right = text.partition(separator)
            server_root = self._normalize_media_path(left)
            local_root = self._normalize_media_path(right)
            if not server_root or not local_root:
                continue
            mappings.append((server_root, local_root))
        mappings.sort(key=lambda item: len(item[0]), reverse=True)
        return mappings

    def _align_media_path_by_suffix(self, raw_value: str, root: Path) -> Optional[Path]:
        """尾部对齐兜底：逐级丢弃原路径前缀段，命中 root 下的真实路径。"""
        parts = [part for part in raw_value.replace("\\", "/").split("/") if part]
        for index in range(1, len(parts)):
            candidate = Path(
                "/" + "/".join(parts[index:])
            ).expanduser().resolve(strict=False)
            # 必须严格落在 root 之下：退化成 root 本身不算命中（避免把整库当条目）
            if root not in candidate.parents:
                continue
            if not candidate.exists():
                continue
            logger.info(
                f"媒体服务器路径前缀自动对齐：{raw_value} → {candidate}"
                "（Emby 挂载点与本地不一致，可在插件设置→媒体服务器路径映射中写 "
                "/media/strm=/strm 消除推断）"
            )
            return candidate
        return None

    def _configured_media_path(self, value: Any) -> Tuple[Path, str]:
        """校验媒体服务器实际路径属于插件配置的 STRM 媒体根目录。

        解析顺序：原样命中 → 显式映射（media_server_path_mappings）→ 尾部对齐兜底。
        只做前缀翻译，不放松「必须在 root 之下」与「结果必须存在」两道检查。
        """
        if not self._local_resource_path:
            raise RuntimeError("未配置本地或挂载媒体根路径")
        root = Path(self._local_resource_path).expanduser().resolve(strict=False)
        if not root.is_dir():
            raise RuntimeError("插件配置的媒体根路径不存在或不可访问")
        raw_value = str(value or "").strip()
        if not raw_value:
            raise ValueError(
                "媒体服务器条目路径为空；"
                f"当前媒体根路径={root}，"
                "若 Emby 挂载点与本地不一致，可配置 media_server_path_mappings"
            )
        # 1. 原样命中：字面量已落在 root 之下（保持既有行为，最高优先级）
        path = Path(raw_value).expanduser().resolve(strict=False)
        if path == root or root in path.parents:
            if not path.exists():
                raise LookupError(f"媒体服务器条目路径不存在或不可访问：{path}")
            return path, path.relative_to(root).as_posix()
        # 2. 显式映射：替换媒体服务器前缀为本地前缀后再校验
        for server_root, local_root in self._parsed_media_server_path_mappings():
            if raw_value != server_root and not raw_value.startswith(f"{server_root}/"):
                continue
            relative = raw_value[len(server_root):].lstrip("/")
            translated = f"{local_root}/{relative}" if relative else local_root
            translated_path = Path(translated).expanduser().resolve(strict=False)
            if translated_path != root and root not in translated_path.parents:
                continue
            if not translated_path.exists():
                continue
            return translated_path, translated_path.relative_to(root).as_posix()
        # 3. 尾部对齐兜底：从右往左逐级丢弃原路径前缀段
        aligned = self._align_media_path_by_suffix(raw_value, root)
        if aligned is not None:
            return aligned, aligned.relative_to(root).as_posix()
        raise ValueError(
            f"媒体服务器条目路径不在插件配置的媒体根路径中：{raw_value}，"
            f"当前媒体根路径={root}；"
            "若 Emby 挂载点与本地不一致，可配置 media_server_path_mappings"
            "（每行 媒体服务器路径=本地路径，例如 /media/strm=/strm）"
        )

    @staticmethod
    def _media_server_item_type(value: Any) -> Optional[MediaType]:
        text = str(value or "").strip().lower()
        if text in {"电影", "movie"}:
            return MediaType.MOVIE
        if text in {"电视剧", "tv", "series", "show"}:
            return MediaType.TV
        return None

    def list_media_server_content(
            self,
            server: str = "",
            keyword: str = "",
            tmdb_id: Any = None,
            media_type: str = "",
            limit: int = 500,
    ) -> Dict[str, Any]:
        """在指定媒体服务器内按标题或 TMDB ID 搜索可洗版内容。"""
        services = self._media_server_services()
        server_options = [
            {
                "title": f"{name}（{str(getattr(service, 'type', '') or '媒体服务器').upper()}）",
                "value": name,
            }
            for name, service in sorted(services.items(), key=lambda item: item[0].casefold())
        ]
        selected_server = str(server or "").strip()
        query_text = str(keyword or "").strip()
        target_tmdb_id = positive_int(tmdb_id)
        target_media_type = str(media_type or "").strip().lower()
        if not selected_server:
            return {"servers": server_options, "items": []}
        if selected_server not in services:
            raise ValueError("所选媒体服务器不存在或未启用")
        if not query_text and not target_tmdb_id:
            return {"servers": server_options, "items": []}

        storage_server = self._media_server_storage_name(
            selected_server, services[selected_server]
        )
        row_limit = max(1, min(int(limit or 500), 1000))
        with SessionFactory() as db:
            query = db.query(MediaServerItem).filter(
                func.lower(MediaServerItem.server) == storage_server
            )
            if target_tmdb_id:
                query = query.filter(*media_server_tmdb_filters(
                    MediaServerItem, [target_tmdb_id]
                ))
            else:
                query = query.filter(or_(
                    MediaServerItem.title.ilike(f"%{query_text}%"),
                    MediaServerItem.original_title.ilike(f"%{query_text}%"),
                ))
            rows = query.order_by(
                MediaServerItem.title, MediaServerItem.year
            ).limit(100).all()

            diagnostics = {
                "matched": 0,
                "dropped_no_identity": 0,
                "dropped_path": 0,
                "dropped_path_example": "",
                "dropped_path_reason": "",
                "root": str(self._local_resource_path or ""),
                "mappings": str(getattr(self, "_media_server_path_mappings", "") or ""),
            }
            items = []
            for row in rows:
                if is_ignored_media_path(row.path):
                    continue
                diagnostics["matched"] += 1
                media_type = self._media_server_item_type(row.item_type)
                row_tmdb_id = tmdb_id_of(row)
                if not media_type or not row_tmdb_id or not row.path:
                    diagnostics["dropped_no_identity"] += 1
                    continue
                if target_media_type in {"movie", "tv"} and (
                        (target_media_type == "movie") != (media_type == MediaType.MOVIE)
                ):
                    continue
                try:
                    _, relative = self._configured_media_path(row.path)
                except (ValueError, LookupError, RuntimeError) as error:
                    diagnostics["dropped_path"] += 1
                    if not diagnostics["dropped_path_example"]:
                        diagnostics["dropped_path_example"] = str(row.path)
                        diagnostics["dropped_path_reason"] = str(error)
                    continue
                base = {
                    "server": selected_server,
                    "item_id": str(row.item_id),
                    "title": str(row.title or "未知媒体"),
                    "year": row.year,
                    "tmdb_id": row_tmdb_id,
                    "media_type": "movie" if media_type == MediaType.MOVIE else "tv",
                    "path": relative,
                }
                if media_type == MediaType.MOVIE:
                    items.append({
                        **base,
                        "kind": "movie",
                        "label": f"{base['title']}{f' ({row.year})' if row.year else ''} · 电影 · {relative}",
                    })
                    continue
                seasoninfo = row.seasoninfo or {}
                for season_value, episode_values in seasoninfo.items():
                    try:
                        season = int(season_value)
                    except (TypeError, ValueError):
                        continue
                    episodes = sorted({
                        episode for value in (episode_values or [])
                        if (episode := positive_int(value))
                    })
                    if not episodes:
                        continue
                    items.append({
                        **base,
                        "kind": "season",
                        "season": season,
                        "label": f"{base['title']} S{season:02d} · 整季（{len(episodes)} 集）· {relative}",
                    })
                    for episode in episodes:
                        items.append({
                            **base,
                            "kind": "episode",
                            "season": season,
                            "episode": episode,
                            "label": f"{base['title']} S{season:02d}E{episode:02d} · {relative}",
                        })
                    if len(items) >= row_limit:
                        break
                if len(items) >= row_limit:
                    break
        result: Dict[str, Any] = {
            "servers": server_options,
            "items": items[:row_limit],
            "diagnostics": diagnostics,
        }
        if not items and diagnostics["dropped_path"] > 0:
            result["message"] = (
                f"已读取 0 个媒体库内容：命中标题 {diagnostics['matched']} 条，"
                "均因路径前缀与媒体根不一致被丢弃"
                f"（示例 {diagnostics['dropped_path_example']}）。"
                "请在插件设置→媒体服务器路径映射中填 /media/strm=/strm。"
            )
        return result

    def _media_server_episode_files(
            self, server: str, item_id: str, season: int
    ) -> Dict[int, Dict[str, Any]]:
        chain = MediaServerChain()
        episode_ids = chain.get_season_episode_ids(
            server=server, item_id=item_id, season=season
        )
        result = {}
        for episode, episode_id in (episode_ids or {}).items():
            info = chain.iteminfo(server=server, item_id=episode_id)
            if not info:
                raise LookupError(
                    f"无法读取媒体服务器剧集详情：{server} S{season:02d}E{int(episode):02d}"
                )
            path, relative = self._configured_media_path(getattr(info, "path", ""))
            result[int(episode)] = {
                "file_name": path.name,
                "file_size": path.stat().st_size if path.is_file() and path.suffix.lower() != ".strm" else 0,
                "path": relative,
                "source": f"媒体服务器 {server}",
            }
        return result

    def resolve_media_server_upgrade_targets(
            self, selections: List[Dict[str, Any]]
    ) -> List[Any]:
        """二次校验媒体服务器条目和实际路径，并按媒体、季聚合洗版目标。"""
        services = self._media_server_services()
        grouped: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
        episode_cache: Dict[Tuple[str, str, int], Dict[int, Dict[str, Any]]] = {}
        seen = set()
        with SessionFactory() as db:
            normalized_selections = []
            rows_by_server: Dict[str, set[str]] = {}
            for selection in selections[:200]:
                server = str(selection.get("server") or "").strip()
                item_id = str(selection.get("item_id") or "").strip()
                kind = str(selection.get("kind") or "").strip().lower()
                identity = (server, item_id, kind, selection.get("season"), selection.get("episode"))
                if identity in seen:
                    continue
                seen.add(identity)
                if server not in services or not item_id:
                    raise ValueError("所选媒体服务器不存在、未启用或条目无效")
                storage_server = self._media_server_storage_name(
                    server, services[server]
                )
                normalized_selections.append((
                    selection, server, item_id, kind, storage_server,
                ))
                rows_by_server.setdefault(storage_server, set()).add(item_id)

            rows_by_key = {}
            for storage_server, item_ids in rows_by_server.items():
                rows = db.query(MediaServerItem).filter(
                    func.lower(MediaServerItem.server) == storage_server,
                    MediaServerItem.item_id.in_(item_ids),
                ).all()
                rows_by_key.update({
                    (storage_server, str(row.item_id)): row for row in rows
                })

            for selection, server, item_id, kind, storage_server in normalized_selections:
                row = rows_by_key.get((storage_server, item_id))
                row_tmdb_id = tmdb_id_of(row)
                if not row or not row_tmdb_id:
                    raise LookupError("所选媒体库内容已不存在或缺少 TMDB ID")
                media_type = self._media_server_item_type(row.item_type)
                if not media_type:
                    raise ValueError("所选媒体库内容不是电影或电视剧")
                media_path, relative = self._configured_media_path(row.path)
                if media_type == MediaType.MOVIE:
                    if kind != "movie":
                        raise ValueError("电影媒体库条目类型错误")
                    if not media_path.is_file():
                        raise LookupError("所选电影的媒体服务器路径不是可访问文件")
                    key = (media_type.value, row_tmdb_id, 0)
                    grouped.setdefault(key, {
                        "row": row,
                        "season": 0,
                        "episodes": set(),
                        "baseline": {"movie": {
                            "file_name": media_path.name,
                            "file_size": media_path.stat().st_size if media_path.is_file() and media_path.suffix.lower() != ".strm" else 0,
                            "path": relative,
                            "source": f"媒体服务器 {server}",
                        }},
                    })
                    continue

                try:
                    season = max(1, int(selection.get("season") or 0))
                except (TypeError, ValueError):
                    raise ValueError("电视剧季号无效")
                cache_key = (server, item_id, season)
                if cache_key not in episode_cache:
                    episode_cache[cache_key] = self._media_server_episode_files(
                        server, item_id, season
                    )
                available = episode_cache[cache_key]
                if kind == "season":
                    selected_episodes = set(available)
                elif kind == "episode":
                    episode = int(selection.get("episode") or 0)
                    if episode <= 0 or episode not in available:
                        raise LookupError("所选媒体库剧集已不存在")
                    selected_episodes = {episode}
                else:
                    raise ValueError("电视剧媒体库条目类型错误")
                if not selected_episodes:
                    raise LookupError("所选媒体库季没有可洗版剧集")
                key = (media_type.value, row_tmdb_id, season)
                group = grouped.setdefault(key, {
                    "row": row,
                    "season": season,
                    "episodes": set(),
                    "baseline": {"episodes": {}},
                })
                group["episodes"].update(selected_episodes)
                group["baseline"]["episodes"].update({
                    episode: available[episode] for episode in selected_episodes
                })

        targets = []
        for index, group in enumerate(grouped.values(), start=1):
            row = group["row"]
            media_type = self._media_server_item_type(row.item_type)
            targets.append(self.build_transient_media_target(
                {
                    "title": row.title,
                    "year": row.year,
                    "media_type": media_type.value,
                    "tmdb_id": tmdb_id_of(row),
                    "season": group["season"] or None,
                },
                target_id=-index,
                episodes=group["episodes"],
                manual_upgrade=True,
                manual_baseline=group["baseline"],
            ))
        if not targets:
            raise ValueError("请选择至少一个符合插件媒体路径配置的媒体库内容")
        return targets

    def delete_history_record(
            self, identity: Dict[str, Any], delete_linked_files: bool = False
    ) -> Dict[str, Any]:
        """删除一条终态或后处理历史；显式请求时联动删除网盘文件和STRM。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        if not str(identity.get("time") or "").strip() or not str(
                identity.get("file_name") or ""
        ).strip():
            raise ValueError("历史记录标识不完整")
        pending_count = 0
        pending_removed = False
        deleted = None
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            for index, record in enumerate(history):
                if not self._history_record_matches(record, identity):
                    continue
                if not self._history_record_deletable(record):
                    raise RuntimeError(
                        f"{record.get('status') or '当前'}状态仍在处理，不能删除历史记录"
                    )
                deleted = copy.deepcopy(record)
                finalize_key = str(deleted.get("finalize_key") or "").strip()
                if finalize_key:
                    removed_keys, pending_count = self._remove_pending_finalize_tasks(
                        {finalize_key}
                    )
                    pending_removed = bool(removed_keys)
                linked_result = None
                if delete_linked_files:
                    linked_result = self._delete_history_linked_files(deleted)
                    self._refresh_deleted_media([deleted])
                deleted["cache_deleted"] = self._delete_history_cache(deleted)
                history.pop(index)
                if delete_linked_files:
                    self._refresh_deleted_subscribe_notes([deleted], history)
                self._save_data("history", history)
                if linked_result:
                    deleted["linked_delete"] = linked_result
                break
        if deleted is None:
            raise LookupError("历史记录不存在或状态已发生变化，请刷新后重试")
        if pending_removed:
            self._notify_offline_pending_changed(pending_count)
        self._delete_platform_transfer_histories([deleted])
        return deleted

    def delete_history_records(
            self,
            identities: List[Dict[str, Any]],
            delete_linked_files: bool = False,
    ) -> Dict[str, int]:
        """批量删除终态或后处理历史，仅进行一次读取和持久化。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        target_identities = {
            identity_key
            for identity in identities
            if (identity_key := self._history_record_identity(identity))
        }
        if not target_ids and not target_identities:
            raise ValueError("未提供有效的历史记录标识")

        pending_count = 0
        pending_removed = False
        linked_stats: Dict[str, int] = {}
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            selected_indexes = []
            for index, record in enumerate(history):
                record_id = self._ensure_history_record_id(record)
                identity_key = self._history_record_identity(record)
                if record_id in target_ids or identity_key in target_identities:
                    selected_indexes.append(index)

            if not selected_indexes:
                raise LookupError("所选历史记录不存在或状态已发生变化，请刷新后重试")

            deleted_indexes = set()
            linked_deleted = 0
            skipped = 0
            deletable_records = []
            for index in selected_indexes:
                record = history[index]
                if not self._history_record_deletable(record):
                    skipped += 1
                    continue
                deletable_records.append(record)
                deleted_indexes.add(index)

            finalize_keys = {
                str(record.get("finalize_key") or "").strip()
                for record in deletable_records
                if str(record.get("finalize_key") or "").strip()
            }
            if finalize_keys:
                removed_keys, pending_count = self._remove_pending_finalize_tasks(
                    finalize_keys
                )
                pending_removed = bool(removed_keys)

            if delete_linked_files and deletable_records:
                linked_stats = self._delete_history_linked_files_batch(
                    deletable_records
                )
                self._refresh_deleted_media(deletable_records)
                linked_deleted = len(deletable_records)

            cache_deleted = sum(
                self._delete_history_cache(record)
                for record in deletable_records
            )

            if deleted_indexes:
                history = [
                    record
                    for index, record in enumerate(history)
                    if index not in deleted_indexes
                ]
                if delete_linked_files and deletable_records:
                    self._refresh_deleted_subscribe_notes(
                        deletable_records, history
                    )
                self._save_data("history", history)

        if pending_removed:
            self._notify_offline_pending_changed(pending_count)
        if deleted_indexes:
            self._delete_platform_transfer_histories(deletable_records)

        return {
            "deleted": len(deleted_indexes),
            "linked_deleted": linked_deleted,
            "cache_deleted": cache_deleted,
            "skipped": skipped,
            **linked_stats,
        }

    def mark_history_records_success(
            self, identities: List[Dict[str, Any]]
    ) -> Dict[str, int]:
        """把所选历史记录手动置为「成功」，并阻止后续自动状态回写。

        仅接受 ``待确认``（落盘自检查无实据）、``处理中``/``下载中``（队列卡住）
        作为起点。置成功后写 ``manual_success`` 标记，后处理终态与落盘自检
        都会跳过该记录，避免下一次同步又把它翻回「待确认」或「失败」。
        """
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        target_ids = {
            str(identity.get("record_id") or "").strip()
            for identity in identities
            if str(identity.get("record_id") or "").strip()
        }
        target_identities = {
            identity_key
            for identity in identities
            if (identity_key := self._history_record_identity(identity))
        }
        target_pairs = {
            (str(identity.get("share_url") or ""), str(identity.get("file_name") or ""))
            for identity in identities
            if str(identity.get("share_url") or "").strip()
            and str(identity.get("file_name") or "").strip()
        }
        if not target_ids and not target_identities:
            raise ValueError("未提供有效的历史记录标识")

        updated = 0
        skipped = 0
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            selected_indexes = [
                index for index, record in enumerate(history)
                if (
                        self._ensure_history_record_id(record) in target_ids
                        or self._history_record_identity(record) in target_identities
                        or (
                            str(record.get("share_url") or ""),
                            str(record.get("file_name") or ""),
                        ) in target_pairs
                )
            ]
            if not selected_indexes:
                raise LookupError("所选历史记录不存在或状态已发生变化，请刷新后重试")
            marked_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            for index in selected_indexes:
                record = history[index]
                status = str(record.get("status") or "")
                if status not in self._MANUAL_SUCCESS_STATUSES:
                    skipped += 1
                    continue
                record["status"] = "成功"
                record["manual_success"] = True
                record["manual_success_at"] = marked_at
                record["manual_success_from"] = status
                record.pop("failure_reason", None)
                record.pop("retrying", None)
                updated += 1
            if updated:
                self._save_data("history", history)

        if updated and callable(getattr(self, "_history_changed", None)):
            self._history_changed()
        logger.info(
            f"历史记录手动标记成功：更新 {updated} 条，跳过 {skipped} 条，"
            f"命中 {len(selected_indexes)} 条"
        )
        return {
            "updated": updated,
            "skipped": skipped,
            "selected": len(selected_indexes),
        }

    def notify_history_record(self, identity: Dict[str, Any]) -> Dict[str, Any]:
        """按成功历史补发入库和 Webhook 通知，不重复发送转存完成消息。"""
        if not self._get_data:
            raise RuntimeError("历史记录存储未初始化")
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            record = next(
                (
                    item
                    for item in history
                    if self._history_record_matches(item, identity)
                ),
                None,
            )
            if not record:
                raise LookupError("历史记录不存在或状态已发生变化，请刷新后重试")
            if str(record.get("status") or "") != "成功" or record.get(
                    "finalize_key"
            ):
                raise RuntimeError("只有已成功完成的记录才能手动通知")
            record = copy.deepcopy(record)

        title = str(record.get("title") or "").strip()
        year = str(record.get("year") or "").strip()
        summary_title = f"{title}（{year}）" if title and year else title

        source_name = str(
            record.get("source_file_name") or record.get("file_name") or ""
        )
        context = self._resolve_history_retry_context(record, source_name)
        target_subscribe = context["target_subscribe"]
        mediainfo = context["mediainfo"]
        file_name = str(record.get("file_name") or context["target_name"])
        if not self._local_resource_path:
            raise RuntimeError("未配置本地STRM或媒体根目录，无法确定入库通知路径")
        notify_path = self._resolve_resource_season_dir(
            self._local_resource_path,
            target_subscribe,
            mediainfo,
            normalize_season(record.get("season")),
        )
        if not notify_path:
            raise RuntimeError("无法按媒体分类规则确定入库通知目录")

        notification_name = summary_title or file_name
        scheduled = self._media_server_notifier.notify(
            path=notify_path,
            mediainfo=mediainfo,
            file_name=notification_name,
        )
        if not scheduled:
            raise RuntimeError("入库通知未启用或媒体服务器配置无效")
        detail = {
            "type": "电视剧" if mediainfo.type == MediaType.TV else "电影",
            "title": mediainfo.title,
            "year": mediainfo.year,
            "image": mediainfo.get_poster_image(),
            "file_name": notification_name,
            "size": self._detail_size(record),
            "source": self._detail_source_text(record),
        }
        if mediainfo.type == MediaType.TV:
            detail["season"] = normalize_season(record.get("season"))
            detail["episodes"] = [int(record.get("episode") or 0)]
        if self._file_finalized:
            self._file_finalized([detail], 1)
        logger.info(
            f"已手动补发汇总通知：{notification_name}，入库目录：{notify_path}"
        )
        return {
            "file_name": file_name,
            "summary_title": notification_name,
            "notify_path": str(notify_path),
        }

    def clear_deletable_history(self, force: bool = False) -> Dict[str, int]:
        """默认只清理终态历史；显式强制时清空全部历史展示。"""
        if not self._get_data or not self._save_data:
            raise RuntimeError("历史记录存储未初始化")
        with self._offline_pending_lock:
            history = self._get_data("history") or []
            retained = []
            deleted_records = []
            for record in history:
                if not force and not self._history_record_deletable(record):
                    retained.append(record)
                else:
                    deleted_records.append(record)
            deleted_count = len(deleted_records)
            if deleted_count:
                self._save_data("history", retained)
        if deleted_count:
            for record in deleted_records:
                self._delete_history_cache(record)
            self._delete_platform_transfer_histories(deleted_records)
        return {"deleted": deleted_count, "retained": len(retained)}
