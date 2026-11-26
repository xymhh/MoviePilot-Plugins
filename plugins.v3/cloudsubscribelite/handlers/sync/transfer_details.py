"""后处理完成通知的详情拼装（Phase 2 v1.13.3）。

从 handlers/sync/history.py 拆出：history 表删除后，通知详情不再读
插件 history——体积只取本次转存结果的实际值（§3.3），来源盘名由
分享链接推导后显式传入，媒体信息缺失时从重试索引行恢复。
"""

import copy
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from app.core.context import MediaInfo
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.models.downloadhistory import DownloadHistory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import CloudFile, OwnerDelegator
from ...core.media import download_history_identity_payload, media_identity
from .utils import normalize_season


class TransferDetailsService(OwnerDelegator):
    """拼转存/洗版完成通知的详情 dict（不写任何账本）。"""

    def _transfer_drive_names_for_share(
            self, share_url: str
    ) -> Tuple[str, str]:
        """按分享链接推导（源盘名，目标盘名）。

        源盘名来自分享平台的 provider；推导失败时源盘留空，目标盘用
        当前挂载的网盘名兜底（与 _detail_source_text 的兜底同一口径）。
        """
        source_name = ""
        try:
            provider = self._owner.resource_transfer._resource_provider_for_url(share_url)
            if provider is not None:
                source_name = str(getattr(provider, "name", "") or "").strip()
        except Exception as error:  # noqa: BLE001 - 推导失败时按同盘处理
            logger.debug(f"推导分享来源盘名失败：{error}", exc_info=True)
        target_name = str(
            getattr(getattr(self, "_cloud_drive", None), "name", "") or ""
        ).strip()
        return source_name, target_name

    def _is_cross_share(self, share_url: str) -> bool:
        """该分享是不是跨盘来源（源盘 key 与目标盘不同）。"""
        try:
            provider = self._owner.resource_transfer._resource_provider_for_url(share_url)
            target = getattr(self, "_cloud_drive", None)
            return bool(
                provider is not None
                and target is not None
                and getattr(provider, "key", None)
                and getattr(target, "key", None)
                and provider.key != target.key
            )
        except Exception as error:  # noqa: BLE001 - 推导失败时按同盘处理
            logger.debug(f"推导跨盘来源失败：{error}", exc_info=True)
            return False

    def _detail_source_text(
            self,
            history_item: Optional[Dict[str, Any]] = None,
            *,
            source_drive_name: str = "",
            target_drive_name: str = "",
    ) -> str:
        """通知里展示的网盘来源：跨盘写「源 → 目标」，同盘只写目标盘。

        显式传入的盘名优先（创建时就带真实来源）；未传时从记录里取；
        目标盘仍为空时用当前挂载的网盘名兜底，避免来源整段消失。
        """
        item = history_item or {}
        source = str(source_drive_name or "").strip()
        target = str(target_drive_name or "").strip()
        if not source or not target:
            item_source, item_target = self._detail_drive_names(item)
            source = source or item_source
            target = target or item_target
        if not target:
            # 部分后处理链路（文件落盘时才发通知）没有历史记录，
            # 直接用当前挂载的网盘名兜底，避免来源整段消失
            target = str(
                getattr(getattr(self, "_cloud_drive", None), "name", "") or ""
            ).strip()
        # 跨盘才写「源 → 目标」；同盘只写目标盘名
        if source and target and source != target:
            return f"{source} → {target}"
        return target or source or ""

    @staticmethod
    def _detail_drive_names(
            item: Dict[str, Any]
    ) -> Tuple[str, str]:
        """从一条记录里取（源盘名，目标盘名）。"""
        target = str(
            item.get("target_drive_name") or item.get("cloud_drive_name") or ""
        ).strip()
        source = str(item.get("source_drive_name") or "").strip()
        return source, target

    @staticmethod
    def _detail_size(
            history_item: Optional[Dict[str, Any]] = None,
            file_info: Any = None,
    ) -> int:
        """通知里展示的文件大小：只取本次转存结果的实际字节数（§3.3）。

        file_info（后处理项/转存结果）优先；拿不到时才回落到记录里的
        file_size。旧的“历史记录首个正值候选”是体积错数的来源之一，
        已随 history 删除；两边都拿不到时返回 0，由调用方打 size_unknown。
        """
        candidates: List[Any] = []
        if isinstance(file_info, dict):
            candidates.append(file_info.get("file_size"))
            candidates.append(file_info.get("size"))
            inner = file_info.get("file")
            if isinstance(inner, dict):
                candidates.append(inner.get("size"))
        if isinstance(history_item, dict):
            candidates.append(history_item.get("file_size"))
            candidates.append(history_item.get("size"))
        for value in candidates:
            try:
                size = int(value or 0)
            except (TypeError, ValueError):
                size = 0
            if size > 0:
                return size
        return 0

    @staticmethod
    def _append_tv_transfer_detail(
            transfer_details: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            season: int,
            episodes: List[int],
            notification_kind: str = "transfer",
            size_bytes: int = 0,
            source_text: str = "",
            file_names: Optional[List[str]] = None,
            episode_sizes: Optional[Dict[int, int]] = None,
            organized_via_mp: bool = False,
            organized_via_mp_episodes: Optional[Dict[int, bool]] = None,
    ) -> None:
        """按媒体季批量合并通知详情，避免逐集重复扫描列表。

        size_bytes 是本批集数的实际总字节（从转存结果累加）；传了
        episode_sizes 时以它为准重新计算，并按集对齐 file_names；
        任一集大小未知（<=0）时记 size_unknown=True，卡片不显示数字。

        v1.26.4：``organized_via_mp`` / ``organized_via_mp_episodes``
        是终态分支标记——MP 整理触发且被接受的集为真，MP 自己会发
        入库通知，插件的完成通知要跳过这些集。同一批里可能混着两
        个分支（某集触发 MP 整理成功、另一集因本地挂载未见文件而
        没触发），所以标记按集存（与 episode_sizes 同套路），
        ``organized_via_mp`` 只是“本批全是 MP 分支”的派生值。
        类别与 detail_link 随详情走，供完成通知模板渲染用。
        """
        normalized = sorted({
            int(episode) for episode in episodes if int(episode) > 0
        })
        if not normalized:
            return
        sizes = {
            int(episode): max(0, int(size or 0))
            for episode, size in (episode_sizes or {}).items()
            if int(episode) > 0
        }
        batch_size = (
            sum(sizes.get(episode, 0) for episode in normalized)
            if sizes else max(0, int(size_bytes or 0))
        )
        batch_unknown = (
            any(sizes.get(episode, 0) <= 0 for episode in normalized)
            if sizes else batch_size <= 0
        )
        # v1.26.4：按集的分支标记（没给逐集表时整批同值）。
        organized_map = {
            int(episode): bool(
                (organized_via_mp_episodes or {}).get(
                    int(episode), organized_via_mp
                )
            )
            for episode in normalized
        }
        names = [str(name).strip() for name in (file_names or []) if str(name).strip()]
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
            merged_names = {}
            for episode, name in zip(
                    detail.get("episodes") or [], detail.get("file_names") or []
            ):
                if name:
                    merged_names[int(episode)] = name
            for episode, name in zip(normalized, names):
                if name:
                    merged_names[int(episode)] = name
            detail["episodes"] = sorted(
                set(detail.get("episodes") or []) | set(normalized)
            )
            detail["file_names"] = [
                merged_names[episode]
                for episode in detail["episodes"]
                if episode in merged_names
            ]
            if sizes or detail.get("episode_sizes"):
                merged_sizes = dict(detail.get("episode_sizes") or {})
                merged_sizes.update(sizes)
                for episode in normalized:
                    merged_sizes.setdefault(episode, 0)
                detail["episode_sizes"] = merged_sizes
                detail["size"] = sum(
                    merged_sizes.get(episode, 0)
                    for episode in detail["episodes"]
                )
                detail["size_unknown"] = any(
                    merged_sizes.get(episode, 0) <= 0
                    for episode in detail["episodes"]
                )
            else:
                detail["size"] = int(detail.get("size") or 0) + batch_size
                detail["size_unknown"] = bool(
                    detail.get("size_unknown") or batch_unknown
                )
            if source_text and not detail.get("source"):
                detail["source"] = source_text
            merged_organized = {
                int(episode): bool(value)
                for episode, value in (
                    detail.get("organized_via_mp_episodes") or {}
                ).items()
                if int(episode or 0) > 0
            }
            merged_organized.update(organized_map)
            detail["organized_via_mp_episodes"] = merged_organized
            detail["organized_via_mp"] = bool(
                detail.get("episodes")
                and all(
                    merged_organized.get(int(episode), False)
                    for episode in detail["episodes"]
                )
            )
            return
        transfer_details.append({
            "type": MediaType.TV.value,
            "title": mediainfo.title,
            "year": mediainfo.year,
            "season": season,
            "episodes": normalized,
            "image": mediainfo.get_poster_image(),
            "notification_kind": notification_kind,
            "size": batch_size,
            "size_unknown": batch_unknown,
            "source": source_text,
            "file_names": names,
            "episode_sizes": dict(sizes),
            # v1.26.4：终态分支标记（按集）+ 完成通知模板要的类别 /
            # 详情链接；organized_via_mp 是“本批全是 MP 分支”派生值。
            "organized_via_mp": bool(
                organized_map and all(organized_map.values())
            ),
            "organized_via_mp_episodes": dict(organized_map),
            "category": str(getattr(mediainfo, "category", "") or ""),
            "detail_link": str(getattr(mediainfo, "detail_link", "") or ""),
        })

    @staticmethod
    def _is_upgrade_history(record: Dict[str, Any]) -> bool:
        value = record.get("upgrade")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    def _derive_notification_kind(
            self,
            *,
            is_upgrade: bool = False,
            replaced_from: Optional[List[Dict[str, Any]]] = None,
            index_record: Optional[Dict[str, Any]] = None,
            cross: bool = False,
    ) -> str:
        """通知类型唯一派生点（v1.29.1）：已替换（``replaced_from`` 非空）
        或处置是 replace（``is_upgrade`` / 历史 upgrade 标记）→
        ``"upgrade"``，否则按原有规则派生 ``cross_transfer`` /
        ``transfer``。此前五处各写一遍三元表达式（television / movie /
        本文件两处详情构造 + upgrade.py 的字面量），现全部走这里。
        """
        if replaced_from or is_upgrade:
            return "upgrade"
        if index_record is not None and self._is_upgrade_history(index_record):
            return "upgrade"
        return "cross_transfer" if cross else "transfer"

    def _pending_index_record(self, pending_key: str) -> Optional[Dict[str, Any]]:
        """后处理项在重试索引里的对应行（按 finalize_key 查），拼成 history
        形状的 dict；没有行时返回 None（同盘成功行本来就不进索引）。"""
        if not pending_key:
            return None
        try:
            get_store = getattr(self, "_get_data_store", None)
            if not callable(get_store):
                return None
            row = get_store().repositories.retry_index.get_by_finalize_key(
                pending_key
            )
        except Exception as error:  # noqa: BLE001 - 索引不可用时按无记录处理
            logger.debug(f"按 finalize_key 查重试索引失败：{error}", exc_info=True)
            return None
        if not row:
            return None
        try:
            from ...core.retry_index import index_row_to_history_dict
        except Exception as error:  # noqa: BLE001 - 索引模块不可用时按无记录处理
            logger.debug(f"重试索引转换失败：{error}", exc_info=True)
            return None
        return index_row_to_history_dict(row)

    def _restore_pending_media_context(
            self, item: Dict[str, Any], pending_key: str
    ) -> Tuple[Optional[MediaInfo], Dict[str, Any]]:
        media_data = item.get("mediainfo") or {}
        if media_data:
            try:
                return self._deserialize_mediainfo(media_data), media_data
            except Exception as error:
                logger.warning(f"网盘文件后处理媒体信息已失效，将重新识别：{error}")

        record = self._pending_index_record(pending_key)
        if not record:
            logger.warning(f"网盘文件后处理缺少对应重试索引行：{pending_key}")
            return None, {}
        try:
            source_name = str(
                record.get("source_file_name") or record.get("file_name") or ""
            )
            context = self._owner.history_retry._resolve_history_retry_context(record, source_name)
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
                f"已从重试索引行恢复网盘文件后处理媒体信息：{mediainfo.title_year}"
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
            notify_path = self._owner.naming._resolve_resource_season_dir(
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
            # 媒体信息恢复失败也不丢通知：文件已经就绪，用后处理项与重试索引行
            # 现有字段拼一份同口径详情（无海报），走同一条完成通知。
            # 手动/MP 搜索转存的提交时通知本就被跳过（pending_key 导致详情为空），
            # 这里再返回 None，该文件就永远没有任何完成通知。
            logger.warning("文件已完成但缺少媒体信息，改用后处理记录发送完成通知")
            return self._fallback_finalized_detail(item, pending_key)
        index_record = self._pending_index_record(pending_key) or {}
        display_title = str(
            getattr(subscribe, "name", None)
            or (target_subscribe and getattr(target_subscribe, "name", None))
            or item.get("name")
            or item.get("title")
            or mediainfo.title
        ).strip()
        share_url = str(item.get("share_url") or "")
        source_name, target_name = self._transfer_drive_names_for_share(share_url)
        detail = {
            "type": "电视剧" if mediainfo.type == MediaType.TV else "电影",
            "title": display_title,
            "year": getattr(target_subscribe, "year", None) or mediainfo.year,
            "image": getattr(mediainfo, "get_poster_image", lambda: None)(),
            "file_name": item.get("file_name"),
            "size": self._detail_size(index_record, item),
            "source": self._detail_source_text(
                index_record or item,
                source_drive_name=source_name,
                target_drive_name=target_name,
            ),
            "notification_kind": self._derive_notification_kind(
                is_upgrade=bool(item.get("upgrade")),
                replaced_from=item.get("replaced_from"),
                index_record=index_record,
                cross=bool(
                    index_record.get("transfer_mode") == "cross"
                    or self._is_cross_share(share_url)
                ),
            ),
            # v1.29.1：实际删除的旧版本（主旧版 + 额外旧版），提交后才有；
            # 空列表表示本次没有删旧版（普通转存、或旧文件已不在）。
            "replaced_from": list(item.get("replaced_from") or []),
            # v1.26.4：终态分支标记（MP 整理身份在 pending 项上时为真，
            # MP 自己会发入库通知，插件的完成通知要跳过这一支）+ 完成
            # 通知模板要的类别 / 详情链接。
            # v1.28.2：触发来源（三态：True=插件这次触发、MP 会发；
            # False=外部提交、MP 不发、插件补发；None=未知、保守不补发）。
            # 只在 organized_via_mp 为真时有意义。
            "organized_via_mp": bool(item.get("mp_organize")),
            "organized_via_mp_triggered": (
                item.get("mp_organize_success_from_plugin_trigger")
                if item.get("mp_organize") else None
            ),
            "category": str(getattr(mediainfo, "category", "") or ""),
            "detail_link": str(getattr(mediainfo, "detail_link", "") or ""),
        }
        episodes = []
        episode_values = (
                item.get("notification_episodes")
                or item.get("success_episodes")
                or (
                    [index_record.get("episode")]
                    if index_record.get("episode")
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
                    or index_record.get("season")
                    or getattr(mediainfo, "season", 0)
                    or 1
                ),
            )
            detail["episodes"] = episodes
            # v1.26.4：按集的分支标记（本项各集同属一个分支）。
            detail["organized_via_mp_episodes"] = {
                int(episode): bool(item.get("mp_organize"))
                for episode in episodes
            }
            # v1.28.2：按集的触发来源标记（本项各集同属一个触发来源；
            # 三态不坍缩，None=未知）。
            detail["organized_via_mp_triggered_episodes"] = {
                int(episode): (
                    item.get("mp_organize_success_from_plugin_trigger")
                    if item.get("mp_organize") else None
                )
                for episode in episodes
            }
        return detail

    def _fallback_finalized_detail(
            self, item: Dict[str, Any], pending_key: str
    ) -> Optional[Dict[str, Any]]:
        """媒体信息缺失时的完成通知详情：只用后处理项与重试索引行现有字段。

        与主路径同一份详情结构（type/title/year/image/file_name/size/source/
        notification_kind/season/episodes），只是没有海报；渲染文案与
        ``_send_transfer_notification_now`` 完全一致。
        """
        index_record = self._pending_index_record(pending_key) or {}
        title = str(
            item.get("name")
            or item.get("title")
            or index_record.get("title")
            or ""
        ).strip()
        if not title:
            title = str(item.get("file_name") or pending_key).strip()
        record_type = str(index_record.get("type") or "").strip()
        is_tv = (
            record_type == MediaType.TV.value
            or bool(item.get("season"))
            or bool(item.get("notification_episodes"))
            or bool(item.get("success_episodes"))
            or bool(index_record.get("episode"))
        )
        share_url = str(item.get("share_url") or "")
        source_name, target_name = self._transfer_drive_names_for_share(share_url)
        detail: Dict[str, Any] = {
            "type": MediaType.TV.value if is_tv else MediaType.MOVIE.value,
            "title": title,
            "year": index_record.get("year") or item.get("year") or "",
            "image": None,
            "file_name": item.get("file_name"),
            "size": self._detail_size(index_record, item),
            "source": self._detail_source_text(
                index_record or item,
                source_drive_name=source_name,
                target_drive_name=target_name,
            ),
            "notification_kind": self._derive_notification_kind(
                is_upgrade=bool(item.get("upgrade")),
                replaced_from=item.get("replaced_from"),
                index_record=index_record,
                cross=bool(
                    index_record.get("transfer_mode") == "cross"
                    or self._is_cross_share(share_url)
                ),
            ),
            # v1.29.1：同主路径（见上），媒体信息缺失时同样带上。
            "replaced_from": list(item.get("replaced_from") or []),
            # v1.26.4：与主路径同一份详情结构（分支标记 + 类别 /
            # 详情链接；媒体信息缺失时类别与链接为空）。
            # v1.28.2：触发来源与主路径同口径（三态，见上）。
            "organized_via_mp": bool(item.get("mp_organize")),
            "organized_via_mp_triggered": (
                item.get("mp_organize_success_from_plugin_trigger")
                if item.get("mp_organize") else None
            ),
            "category": "",
            "detail_link": "",
        }
        if is_tv:
            episodes = []
            episode_values = (
                item.get("notification_episodes")
                or item.get("success_episodes")
                or (
                    [index_record.get("episode")]
                    if index_record.get("episode")
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
            try:
                season = int(
                    item.get("season") or index_record.get("season") or 1
                )
            except (TypeError, ValueError):
                season = 1
            detail["season"] = max(1, season)
            detail["episodes"] = sorted(set(episodes))
            # v1.26.4：按集的分支标记（本项各集同属一个分支）。
            detail["organized_via_mp_episodes"] = {
                int(episode): bool(item.get("mp_organize"))
                for episode in detail["episodes"]
            }
            # v1.28.2：按集的触发来源标记（与主路径同口径）。
            detail["organized_via_mp_triggered_episodes"] = {
                int(episode): (
                    item.get("mp_organize_success_from_plugin_trigger")
                    if item.get("mp_organize") else None
                )
                for episode in detail["episodes"]
            }
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
        # 逐集体积是否完整：只有合并进来的明细全都带 episode_sizes，
        # 汇总体积才能按逐集之和校验；finalize 路径的明细不带逐集体积，
        # 合并后只保留按集未知标记，不修正汇总。
        episode_sizes_complete: Dict[Tuple[str, str, int, str], bool] = {}
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
                file_names = list(detail.get("file_names") or [])
                file_name = str(detail.get("file_name") or "").strip()
                if file_name and file_name not in file_names:
                    file_names.append(file_name)
                if file_names:
                    detail["file_names"] = file_names
                    file_name_sets[key] = set(file_names)
                episode_sets[key] = set(detail["episodes"])
                episode_sizes_complete[key] = bool(detail.get("episode_sizes"))
                tv_index[key] = detail
                aggregated.append(detail)
                continue
            episode_sets[key].update(detail["episodes"])
            episode_sizes_complete[key] = bool(
                episode_sizes_complete.get(key)
                and detail.get("episode_sizes")
            )
            incoming_names = list(detail.get("file_names") or [])
            file_name = str(detail.get("file_name") or "").strip()
            if file_name and file_name not in incoming_names:
                incoming_names.append(file_name)
            if incoming_names:
                file_names = current.setdefault("file_names", [])
                known_names = file_name_sets.setdefault(key, set(file_names))
                for name in incoming_names:
                    if name not in known_names:
                        known_names.add(name)
                        file_names.append(name)
            if detail.get("episode_sizes"):
                merged_sizes = dict(current.get("episode_sizes") or {})
                merged_sizes.update({
                    int(episode): max(0, int(size or 0))
                    for episode, size in detail["episode_sizes"].items()
                    if int(episode) > 0
                })
                current["episode_sizes"] = merged_sizes
            # v1.26.4：按集的分支标记随聚合合并；organized_via_mp
            # 重算为“合并后全部集都是 MP 分支”。
            if (
                    detail.get("organized_via_mp_episodes")
                    or current.get("organized_via_mp_episodes")
            ):
                merged_organized = {
                    int(episode): bool(value)
                    for episode, value in (
                        current.get("organized_via_mp_episodes") or {}
                    ).items()
                    if int(episode or 0) > 0
                }
                merged_organized.update({
                    int(episode): bool(value)
                    for episode, value in (
                        detail.get("organized_via_mp_episodes") or {}
                    ).items()
                    if int(episode or 0) > 0
                })
                current["organized_via_mp_episodes"] = merged_organized
                current["organized_via_mp"] = bool(
                    episode_sets[key]
                    and all(
                        merged_organized.get(int(episode), False)
                        for episode in episode_sets[key]
                    )
                )
            # v1.28.2：触发来源标记随聚合合并（三态不坍缩：None=未知，
            # 不能 bool() 成 False，否则未知会被误判成“外部提交”而
            # 补发）。后到的详情覆盖先到的；标量按合并后的集重算：
            # 合并后全部集都确认是插件触发才算 True，任一集确认是
            # 外部提交即 False，其余（缺标记或全未知）为 None。
            if (
                    detail.get("organized_via_mp_triggered_episodes")
                    or current.get("organized_via_mp_triggered_episodes")
            ):
                merged_triggered = {
                    int(episode): value
                    for episode, value in (
                        current.get("organized_via_mp_triggered_episodes")
                        or {}
                    ).items()
                    if int(episode or 0) > 0
                }
                merged_triggered.update({
                    int(episode): value
                    for episode, value in (
                        detail.get("organized_via_mp_triggered_episodes")
                        or {}
                    ).items()
                    if int(episode or 0) > 0
                })
                current["organized_via_mp_triggered_episodes"] = merged_triggered
                if episode_sets[key] and all(
                        merged_triggered.get(int(episode)) is True
                        for episode in episode_sets[key]
                ):
                    current["organized_via_mp_triggered"] = True
                elif any(
                        merged_triggered.get(int(episode)) is False
                        for episode in episode_sets[key]
                ):
                    current["organized_via_mp_triggered"] = False
                else:
                    current["organized_via_mp_triggered"] = None
            if not current.get("image") and detail.get("image"):
                current["image"] = detail["image"]
            current["size"] = int(current.get("size") or 0) + int(
                detail.get("size") or 0
            )
            current["size_unknown"] = bool(
                current.get("size_unknown") or detail.get("size_unknown")
            )
            if not current.get("source") and detail.get("source"):
                current["source"] = detail["source"]
        for key, detail in tv_index.items():
            detail["episodes"] = sorted(episode_sets[key])
            sizes = detail.get("episode_sizes")
            if sizes:
                if episode_sizes_complete.get(key):
                    expected = sum(
                        max(0, int(size or 0)) for size in sizes.values()
                    )
                    if int(detail.get("size") or 0) != expected:
                        logger.warning(
                            f"转存通知体积校验：{detail.get('title')} "
                            f"S{detail.get('season')} 汇总 {detail.get('size')} "
                            f"与逐集体积之和 {expected} 不一致，已按逐集修正"
                        )
                        detail["size"] = expected
                detail["size_unknown"] = any(
                    int(sizes.get(episode, 0) or 0) <= 0
                    for episode in detail["episodes"]
                )
        return aggregated

    def _send_finalized_batch(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        if not transfer_details:
            return
        aggregated = self._aggregate_transfer_details(transfer_details)
        total_count = len(transfer_details)
        if self._notify:
            # v1.13.3 第 6 步：转存完成通知只留洗版；普通转存不再发完成通知
            # （MP 的入库通知仍在）。没有洗版详情时直接跳过，不发空通知。
            # v1.26.4：普通转存的完成通知在终态门恢复——“MP 的入库通知
            # 仍在”只在 MP 整理分支成立，strm 直出分支没有 MP 事件；
            # 与同步完成块共用 send_transfer_completed_notification，
            # 发送器内部按分支分流（MP 整理分支跳过、strm 分支发）。
            # v1.28.2：MP 整理分支里能证明是外部提交的（MP 不会发）
            # 由插件补发一条，见 notify.py 的 _mp_backstop_detail。
            upgrade_details = [
                detail for detail in aggregated
                if str(detail.get("notification_kind") or "") == "upgrade"
            ]
            if upgrade_details:
                try:
                    self.send_transfer_notification(upgrade_details, total_count)
                except Exception as error:
                    logger.warning(f"文件完成通知入队失败：{error}")
            ordinary_details = [
                detail for detail in aggregated
                if str(detail.get("notification_kind") or "")
                in {"transfer", "cross_transfer"}
            ]
            if ordinary_details:
                try:
                    self.send_transfer_completed_notification(
                        ordinary_details, total_count
                    )
                except Exception as error:
                    logger.warning(f"转存完成通知发送失败：{error}")
        if self._file_finalized:
            try:
                self._file_finalized(aggregated, total_count)
            except Exception as error:
                logger.warning(f"文件完成汇总 Webhook 发送失败：{error}")
        logger.debug(
            f"网盘文件后处理完成通知已入队：{total_count} 个文件，"
            f"{len(aggregated)} 个媒体项"
        )

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
        """集中生成本轮转存记录（供重试索引落盘与通知详情用）。

    v1.13.3：不再写插件 history——记录只在内存里流转，终态由
    _write_terminal_state / persist_sync_results 写进重试索引。"""
        media_data = self._serialize_mediainfo(mediainfo)
        media_source, media_id = media_identity(mediainfo)
        effective_media = self._owner.naming._effective_mediainfo(subscribe, mediainfo)
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
        item.update(self._owner.resource_transfer._resource_history_meta(resource or {}, share_url))
        source_provider = self._owner.resource_transfer._resource_provider_for_url(share_url)
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
                    target_name=file_name,
                ))
        if not item.get("discovered_via"):
            fallback_via = str(getattr(subscribe, "_discovered_via", "") or "").strip()
            if fallback_via:
                # 频道推送来的候选带 tg:<频道>/<消息id>，搜索来的留空
                item["discovered_via"] = fallback_via
        item["task_types"] = self._history_task_types(item)
        return item

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
        """通过下载历史能力登记一次网盘转存（MP 的 DownloadHistory 是
        唯一载体，插件 history 表在 v1.13.3 删除后仍走这条路）。"""
        effective_media = self._owner.naming._effective_mediainfo(subscribe, mediainfo)
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

    def is_offline_task_finalized(self, info_hash: str) -> bool:
        """MP 下载历史里存在该磁力任务的记录时视为已入库。

        v1.13.3：旧实现查插件 history 的成功记录（share_url 含该 hash）；
        history 表删除后改查 MP 自己的 DownloadHistory（download_hash
        对磁力任务就是 magnet 链接，含 info_hash）。115 秒传命中时文件
        立即可用，但 115 云下载任务条目的状态可能长时间停留在「等待下载」，
        故此判定仅用于展示层标注，不参与任何完成/失败判定。
        """
        normalized = re.sub(r"[^0-9A-Fa-f]", "", str(info_hash or "")).upper()
        if not normalized:
            return False
        try:
            from app.db import SessionFactory
            from app.db.models.downloadhistory import DownloadHistory
            with SessionFactory() as db:
                row = db.query(DownloadHistory).filter(
                    DownloadHistory.download_hash.like(f"%{normalized}%")
                ).first()
                return row is not None
        except Exception as error:
            logger.debug(f"查询离线任务入库状态失败：{error}")
            return False

