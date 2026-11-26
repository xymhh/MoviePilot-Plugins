"""STRM 生成与重建（旁路能力）。

从 handlers/sync/service.py 拆出，纯搬家：方法体逐字未改。
校验：scripts/verify_split_equivalence.py
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.context import MediaInfo
from app.log import logger

from ...core import CloudFile
from ...core import OwnerDelegator
from ...core.bypass_guard import bypass_guard
from ...utils import StrmTemplateError
from ...utils.strm_deletion_suppression import suppress_deleted_strm_path
from .strm_siblings import cleanup_sibling_strms


class StrmOpsMixin(OwnerDelegator):
    """STRM 文件生成、清理、迁移与全量/增量重建。"""

    def _generate_strm(
            self,
            cloud_dir: str,
            file_name: str,
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
    ) -> Optional[Path]:
        """使用网盘提供方的播放引用值生成 STRM。"""
        if not self._strm_generate_enabled:
            return None
        if not self._strm_generator:
            return None
        if not self._local_resource_path:
            logger.warning("已启用 STRM 直接生成，但未配置本地/挂载媒体根路径")
            return None

        if not target_file and lookup_target:
            target_file = self._cloud_query.find_file(
                cloud_dir, file_name
            )
        if not target_file:
            if log_success:
                logger.debug(
                    f"网盘目标文件尚未就绪，暂不生成 STRM："
                    f"{cloud_dir.rstrip('/')}/{file_name}"
                )
            return None
        template_values = self._playback_reference.reference_values(target_file)
        if not self._strm_template_values_complete(template_values):
            # 磁力路径：matched 文件来自 move 接口返回，playback_values
            # 里没有 pickcode 等模板变量（2026-11-18 秒速5厘米实测，
            # STRM 生成报“缺少模板变量：pickcode”）；按最终文件名重新
            # 定位一次——目录清单里的文件带完整 playback_values。
            # 刷新失败或仍不完整时走原来的路（write 会报模板错并记日志）。
            refreshed = self._refresh_strm_target_file(cloud_dir, file_name)
            if refreshed is not None:
                refreshed_values = self._playback_reference.reference_values(
                    refreshed
                )
                if self._strm_template_values_complete(refreshed_values):
                    target_file = refreshed
                    template_values = refreshed_values
        try:
            strm_path, content = self._strm_generator.write(
                local_root=self._local_resource_path,
                cloud_root=self._CLOUD_MEDIA_ROOT,
                cloud_dir=cloud_dir,
                file_name=file_name,
                template_values=template_values,
            )
            if log_success:
                logger.debug(f"STRM 已生成：{strm_path} -> {content}")
            # v1.31.0 根因修复（两京十五日 S01E05/E06 实测）：写点只按视频
            # 文件名派生 .strm 名，同一集两次入库、文件名一次带 {tmdb-}
            # 标记一次不带，就会各写一份 .strm（两份各指不同视频，不是旧
            # 文件残留）。新生成后按“集”清理同目录旧兄弟 .strm（+ 同主干
            # .nfo），只删本地指针文件，不动网盘视频；清理失败不影响本次
            # 生成（fail-open，记 warning）。漏斗落盘前实查是第一道防线，
            # 这里是最后一道。洗版时旧版 .strm 会比 _delete_upgrade_old_strm
            # 早一步被清掉，终态与洗版成功时一致（见 strm_duplicates 模块注释）。
            try:
                deleted = cleanup_sibling_strms(strm_path.parent, strm_path)
                # v1.31.6：清理掉的旧兄弟 .strm 也会触发 Emby
                # library.deleted，逐条登记屏蔽，防深度删除连锁误删视频。
                for sibling in deleted or []:
                    suppress_deleted_strm_path(self, sibling)
            except Exception as error:  # noqa: BLE001 - 清理失败不影响生成
                logger.warning(f"清理同集旧 STRM 失败：{strm_path}，{error}")
            return strm_path
        except (OSError, StrmTemplateError) as error:
            logger.error(f"生成 STRM 失败：{file_name}，原因：{error}")
            return None

    def _strm_template_values_complete(
            self, template_values: Dict[str, str],
    ) -> bool:
        """模板变量是否齐全：playback_reference 声明的变量都得有非空值。"""
        required = getattr(
            self._playback_reference, "template_variables", None,
        ) or frozenset()
        return all(
            str(template_values.get(name) or "").strip() for name in required
        )

    def _refresh_strm_target_file(
            self, cloud_dir: str, file_name: str,
    ) -> Optional[CloudFile]:
        """按最终文件名重新定位目标文件（取目录清单里的完整 playback_values）。

        刷新失败返回 None，调用方走原来的路，不阻塞 STRM 生成。
        """
        cloud_query = getattr(self, "_cloud_query", None)
        if cloud_query is None:
            return None
        try:
            return cloud_query.find_file(cloud_dir, file_name)
        except Exception as error:  # noqa: BLE001 - 刷新失败不阻塞 STRM 生成
            logger.debug(f"STRM 目标文件刷新失败：{error}", exc_info=True)
            return None

    def _generate_or_queue_strm(
            self,
            share_url: str,
            cloud_dir: str,
            file_name: str,
            mediainfo: MediaInfo,
            source_sha1: str = "",
            file_size: int = 0,
            subscribe_id: Optional[int] = None,
            success_episodes: Optional[List[int]] = None,
            season: Optional[int] = None,
            notification_episodes: Optional[List[int]] = None,
            sub_key: str = "",
            target_file: Optional[CloudFile] = None,
            lookup_target: bool = True,
            log_success: bool = True,
            staging_dir: str = "",
            staging_name: str = "",
            upgrade: bool = False,
            upgrade_mode: str = "",
            upgrade_old_cloud_dir: str = "",
            upgrade_old_file_name: str = "",
            upgrade_old_file_id: str = "",
            upgrade_old_size: int = 0,
            subtitles: Optional[List[Dict[str, Any]]] = None,
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
            skip_history: bool = False,
            mp_organize: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Path], str]:
        strm_path = None
        if not staging_dir:
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=lookup_target,
                log_success=log_success,
            )
            self._scrape_metadata(
                cloud_dir,
                file_name,
                mediainfo,
                season=season,
                episode=next(iter(notification_episodes or success_episodes or []), None),
            )
        if strm_path:
            return strm_path, ""
        pending_key = self._queue_file_finalize(
            share_url=share_url,
            cloud_dir=cloud_dir,
            file_name=file_name,
            mediainfo=mediainfo,
            source_sha1=source_sha1,
            file_size=file_size,
            subscribe_id=subscribe_id,
            success_episodes=success_episodes,
            season=season,
            notification_episodes=notification_episodes,
            sub_key=sub_key,
            staging_dir=staging_dir,
            staging_name=staging_name,
            upgrade=upgrade,
            upgrade_mode=upgrade_mode,
            upgrade_old_cloud_dir=upgrade_old_cloud_dir,
            upgrade_old_file_name=upgrade_old_file_name,
            upgrade_old_file_id=upgrade_old_file_id,
            upgrade_old_size=upgrade_old_size,
            subtitles=subtitles,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
            skip_history=skip_history,
            mp_organize=mp_organize,
        )
        return None, pending_key

    def _generate_or_queue_strm_batch(
            self,
            items: List[Dict[str, Any]],
            mediainfo: MediaInfo,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
            sub_key: str = "",
            transient_target: bool = False,
            target_subscribe: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Tuple[Optional[Path], str]]:
        """复用批量重命名缓存生成 STRM，避免逐文件查询115。"""
        results: Dict[str, Tuple[Optional[Path], str]] = {}
        generated = 0
        queued_items: List[Dict[str, Any]] = []
        ready_items: List[Dict[str, Any]] = []
        organize_enabled = getattr(self, "_organize_after_transfer", True)
        for item in items:
            result_key = str(item["result_key"])
            staging_dir = str(item.get("staging_dir") or "").rstrip("/")
            cloud_dir = str(item.get("cloud_dir") or "").rstrip("/")
            # v1.30.0 阶段 4：漏斗路由项（funnel_route 为真）入队后不再
            # 当场生成 STRM——文件随后由漏斗 worker 经 MP 整理进媒体库，
            # 当场生成的 STRM 会指向被移走的目标目录文件而失效。阶段 2
            # 只排除了 TG 项、阶段 3 只排除了订阅/洗版项，手动同目录仍
            # 当场生成；本版统一：只要 funnel_route 为真就入队（含手动
            # 同目录），funnel_route 由生产者按阶段开关在 _do_sync 里
            # 下沉（总闸关时为假，仍走旧直出路）。
            is_funnel_routed = bool(item.get("funnel_route"))
            if (
                    not organize_enabled
                    or (staging_dir and staging_dir != cloud_dir)
                    or is_funnel_routed
            ):
                queued_items.append(item)
                continue
            cloud_dir = item["cloud_dir"]
            file_name = item["file_name"]
            target_file = self._cloud_query.get_cached_file(
                cloud_dir, file_name
            )
            strm_path = self._generate_strm(
                cloud_dir,
                file_name,
                target_file=target_file,
                lookup_target=False,
                log_success=False,
            )
            ready_items.append(item)
            if strm_path:
                results[result_key] = (strm_path, "")
                generated += 1
            else:
                queued_items.append(item)
        if organize_enabled:
            self._scrape_metadata_batch(ready_items, mediainfo, season=season)
        pending_keys = self._queue_file_finalize_batch(
            queued_items,
            mediainfo,
            subscribe_id=subscribe_id,
            season=season,
            sub_key=sub_key,
            transient_target=transient_target,
            target_subscribe=target_subscribe,
        )
        for item in queued_items:
            result_key = str(item["result_key"])
            results[result_key] = (None, pending_keys.get(result_key, ""))
        if items:
            logger.debug(
                f"批量文件终态检查完成：即时生成 STRM {generated} 个，"
                f"待移动或文件就绪 {len(pending_keys)} 个"
            )
        return results
