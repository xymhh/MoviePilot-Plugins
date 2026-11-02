"""STRM 生成与重建（旁路能力）。

从 handlers/sync/service.py 拆出，纯搬家：方法体逐字未改。
校验：scripts/verify_split_equivalence.py
"""

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.context import MediaInfo
from app.log import logger

from ...core import CloudFile
from ...core.bypass_guard import bypass_guard
from ...utils import (
    StrmTemplateError,
    plan_share_strm_files,
    rebuild_strm_by_export_tree,
    rebuild_strm_tree,
)
from ...utils.legacy_strm import iter_strm_files, migrate_strm_content


class StrmOpsMixin:
    """STRM 文件生成、清理、迁移与全量/增量重建。"""

    _STRM_EMPTY_STATS = {
        "directories": 0, "files": 0, "created": 0,
        "updated": 0, "skipped": 0, "errors": 0,
    }

    def generate_share_strm(
            self,
            share_url: str,
            save_path: str = "",
            overwrite: bool = False,
    ) -> Dict[str, Any]:
        """把 115 分享内的视频直接生成 STRM（不转存，不占网盘空间）。

        STRM 内容指向本插件 ``/redirect`` 端点；播放时用
        ``p115client.share_download_url`` 现场换链，文件始终留在分享方，
        本地只多出几百字节的 .strm。
        """
        share_service = self._share_transfer
        if share_service is None or not hasattr(share_service, "resolve_share_download_link"):
            raise RuntimeError("当前网盘不支持分享 STRM")
        if self._strm_generator is None:
            raise RuntimeError("STRM 生成未启用，请先在设置中开启并配置基础地址")
        share_url = str(share_url or "").strip()
        if not share_url:
            raise ValueError("请填写 115 分享链接（含提取码）")
        info = share_service.extract_share_info(share_url)
        share_code = str(info.get("share_code") or "")
        receive_code = str(info.get("receive_code") or "")
        if not share_code or not receive_code:
            raise ValueError("分享链接解析失败，请确认链接与提取码完整")

        root = str(save_path or self._local_resource_path or "").strip()
        if not root:
            raise ValueError("未配置本地资源目录，请指定 save_path")

        files = share_service.list_share_files(share_url, max_depth=10) or []
        base_url = str(getattr(self._strm_generator, "base_url", "") or "").rstrip("/")
        planned, skipped = plan_share_strm_files(
            root, files, base_url, share_code, receive_code,
        )
        target_root = Path(root)
        created = updated = 0
        sample = []
        for target, content in planned:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                existed = target.exists()
                if existed and not overwrite:
                    skipped += 1
                    continue
                target.write_text(content, encoding="utf-8")
            except OSError as error:
                logger.warning(f"分享 STRM 写入失败：{target}（{error}）")
                skipped += 1
                continue
            if existed:
                updated += 1
            else:
                created += 1
            if len(sample) < 3:
                sample.append({"path": str(target), "content": content})

        refreshed = self.refresh_media_servers() if (created or updated) else {}
        logger.info(
            f"分享 STRM 生成完成：{share_code} 新增 {created} / 更新 {updated} "
            f"/ 跳过 {skipped}，目录={root}，媒体库刷新={refreshed or '未触发'}"
        )
        return {
            "share_code": share_code,
            "receive_code": receive_code,
            "path": str(target_root),
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "sample": sample,
            "media_refresh": refreshed,
        }

    def clean_share_strm(
            self,
            share_url: str,
            save_path: str = "",
    ) -> Dict[str, Any]:
        """删除某个分享链接生成的 STRM（只删 .strm，不碰原片与目录）。"""
        share_service = self._share_transfer
        if share_service is None or not hasattr(share_service, "resolve_share_download_link"):
            raise RuntimeError("当前网盘不支持分享 STRM")
        share_url = str(share_url or "").strip()
        if not share_url:
            raise ValueError("请填写 115 分享链接（含提取码）")
        info = share_service.extract_share_info(share_url)
        share_code = str(info.get("share_code") or "")
        receive_code = str(info.get("receive_code") or "")
        if not share_code or not receive_code:
            raise ValueError("分享链接解析失败，请确认链接与提取码完整")

        root = str(save_path or self._local_resource_path or "").strip()
        if not root:
            raise ValueError("未配置本地资源目录，请指定 save_path")

        files = share_service.list_share_files(share_url, max_depth=10) or []
        planned, _skipped = plan_share_strm_files(
            root, files, "http://placeholder.invalid", share_code, receive_code,
        )
        removed = missing = 0
        for target, _content in planned:
            try:
                if target.exists():
                    target.unlink()
                    removed += 1
                else:
                    missing += 1
            except OSError as error:
                logger.warning(f"分享 STRM 删除失败：{target}（{error}）")

        refreshed = self.refresh_media_servers() if removed else {}
        logger.info(
            f"分享 STRM 清理完成：{share_code} 删除 {removed} / 不存在 {missing}，"
            f"目录={root}，媒体库刷新={refreshed or '未触发'}"
        )
        return {
            "share_code": share_code,
            "path": str(Path(root)),
            "removed": removed,
            "missing": missing,
            "media_refresh": refreshed,
        }

    def migrate_legacy_strm(
            self, root: str = "", dry_run: bool = False,
    ) -> Dict[str, Any]:
        """把 P115StrmHelper 生成的 STRM 改写成本插件端点（保留查询参数）。"""
        target_root = str(root or self._local_resource_path or "").strip()
        if not target_root:
            raise ValueError("未配置本地资源目录，请指定 root")
        scanned = migrated = skipped = failed = 0
        for path in iter_strm_files(target_root):
            scanned += 1
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as error:
                failed += 1
                logger.warning(f"读取 STRM 失败：{path}（{error}）")
                continue
            new_content = migrate_strm_content(content)
            if new_content is None:
                skipped += 1
                continue
            if not dry_run:
                try:
                    path.write_text(new_content, encoding="utf-8")
                except OSError as error:
                    failed += 1
                    logger.warning(f"改写 STRM 失败：{path}（{error}）")
                    continue
            migrated += 1
        refreshed = {} if dry_run else self.refresh_media_servers()
        logger.info(
            f"旧 STRM 迁移完成：{'预演' if dry_run else '执行'} 扫描 {scanned} / "
            f"改写 {migrated} / 跳过 {skipped} / 失败 {failed}，目录={target_root}"
        )
        return {
            "path": str(Path(target_root)),
            "scanned": scanned,
            "migrated": migrated,
            "skipped": skipped,
            "failed": failed,
            "dry_run": dry_run,
            "media_refresh": refreshed,
        }

    def rebuild_strm_files(
            self,
            overwrite: bool = False,
            time_budget_seconds: float = 0.0,
            state_store: Any = None,
    ) -> Dict[str, int]:
        """STRM 重建（旁路：失败或冷却时返回全 0 统计，不影响转存）。"""
        return bypass_guard.call(
            "strm",
            self._rebuild_strm_files_impl,
            overwrite=overwrite,
            time_budget_seconds=time_budget_seconds,
            state_store=state_store,
            fallback=dict(self._STRM_EMPTY_STATS),
        )

    def _rebuild_strm_files_impl(
            self,
            overwrite: bool = False,
            time_budget_seconds: float = 0.0,
            state_store: Any = None,
    ) -> Dict[str, int]:
        """按目录 CID 全量遍历媒体库，补齐（可选校准）STRM 文件。

        ``time_budget_seconds`` 大于 0 时到达上限即保存断点退出，
        下次运行从断点续扫（断点落插件私有库，不再写 strm_scan_state.json）。
        """
        if not (self._strm_generate_enabled and self._strm_generator
                and self._local_resource_path and self._cloud_drive):
            return {
                "directories": 0, "files": 0, "created": 0,
                "updated": 0, "skipped": 0, "errors": 0,
            }
        if state_store is None:
            plugin = getattr(self, "_plugin", None)
            getter = getattr(plugin, "_get_data_store", None)
            if callable(getter):
                try:
                    state_store = getter()
                except Exception:  # noqa: BLE001
                    state_store = None
        return rebuild_strm_tree(
            provider=self._cloud_drive,
            generator=self._strm_generator,
            local_root=self._local_resource_path,
            cloud_root=self._CLOUD_MEDIA_ROOT,
            video_extensions=self._video_extensions,
            reference_values=self._playback_reference.reference_values,
            overwrite=overwrite,
            interval_seconds=1.0,
            time_budget_seconds=time_budget_seconds,
            state_store=state_store,
            progress=lambda stats: logger.info(
                f"STRM 全量校准进度：目录 {stats['directories']}，"
                f"文件 {stats['files']}，新增 {stats['created']}，"
                f"更新 {stats['updated']}"
            ),
        )

    def rebuild_strm_increment_by_tree(
            self, timeout_seconds: float = 600.0,
    ) -> Dict[str, int]:
        """用 115 导出目录树做增量补齐（请求数恒定，不逐目录遍历）。

        不支持导出目录树的网盘或导出失败时返回 ``errors``，由调用方决定是否降级。
        """
        if not (self._strm_generate_enabled and self._strm_generator
                and self._local_resource_path and self._cloud_drive):
            return {
                "directories": 0, "files": 0, "created": 0,
                "updated": 0, "skipped": 0, "errors": 0,
            }
        return rebuild_strm_by_export_tree(
            provider=self._cloud_drive,
            generator=self._strm_generator,
            local_root=self._local_resource_path,
            cloud_root=self._CLOUD_MEDIA_ROOT,
            video_extensions=self._video_extensions,
            reference_values=self._playback_reference.reference_values,
            overwrite=False,
            timeout_seconds=timeout_seconds,
            progress=lambda stats: logger.info(
                f"STRM 增量进度：新增 {stats['created']}，"
                f"已检查 {stats['files']} 个视频"
            ),
        )

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
            return strm_path
        except (OSError, StrmTemplateError) as error:
            logger.error(f"生成 STRM 失败：{file_name}，原因：{error}")
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
            if not organize_enabled or (staging_dir and staging_dir != cloud_dir):
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
