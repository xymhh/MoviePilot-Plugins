"""同步记录与 MoviePilot 平台整理历史表 (TransferHistory) 的双向同步。"""

import re
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.db import SessionFactory
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import get_download_history_last_by
from ...drive.common import positive_int


class PlatformHistoryService(OwnerDelegator):
    """负责将插件的转存成功记录镜像到 MoviePilot 平台的 TransferHistory 表。"""

    @staticmethod
    def _u115cs_storage_name() -> str:
        """MP 存储名「115网盘CS版」；拿不到常量时用字面值兜底。"""
        try:
            from ...drive.p115.storage import U115CS_STORAGE_NAME
        except Exception:
            return "115网盘CS版"
        name = U115CS_STORAGE_NAME
        if isinstance(name, str) and name.strip():
            return name.strip()
        return "115网盘CS版"

    @staticmethod
    def _platform_episode_numbers(record: Dict[str, Any]) -> List[int]:
        values = (
                record.get("success_episodes")
                or record.get("episode")
                or record.get("episodes")
                or record.get("target_episodes")
        )
        candidates = values if isinstance(values, (list, tuple, set)) else [values]
        episodes = {
            int(number)
            for value in candidates
            for number in re.findall(r"\d+", str(value or ""))
            if int(number) > 0
        }
        if (
                not isinstance(values, (list, tuple, set))
                and "-" in str(values or "")
                and len(episodes) >= 2
        ):
            episodes.update(range(min(episodes), max(episodes) + 1))
        return sorted(episodes)

    @staticmethod
    def _platform_source_label(record: Dict[str, Any]) -> str:
        source = str(record.get("source") or "").strip().lower()
        if source == "manual":
            # 手动添加的跨盘转存（夸克 → 本地 → 115）：文件进 115 前
            # 在本地 /downloads 中转，MP 整理历史里应记成「本地」，
            # 与 MP 自己写的「本地 → 115网盘CS版」一致；记「手动添加」
            # 会让同一集多出一条来源不同的记录。
            return "本地"
        if source:
            from ...search.scanner import SearchSourceRegistry
            defs = {d.id: d.name for d in SearchSourceRegistry.get_definitions()}
            if source in defs:
                return defs[source]

        resource_type = str(record.get("resource_type") or "").strip().lower()
        if resource_type == "cloud":
            return "网盘路径"
        from ...search.types import resource_type_name
        return resource_type_name(resource_type, fallback="网盘订阅助手")


    @classmethod
    def _platform_source_path(
            cls,
            record: Dict[str, Any],
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> str:
        """生成可读且稳定的整理来源标识，避免界面显示内部协议路径。"""
        title = re.sub(r"[\\/]+", "-", str(record.get("title") or "").strip())
        scope = ""
        if season:
            scope = f" S{season:02d}"
            if episode:
                scope += f"E{episode:02d}"
        record_id = record.get("id") or ""
        suffix = f" #{record_id[:10]}" if record_id else ""
        return f"{cls._platform_source_label(record)} · {title}{scope}{suffix}"

    def _platform_history_entries(
            self, record: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        is_upgrade_history = getattr(self, "_is_upgrade_history", None)
        if (
                str(record.get("status") or "") != "成功"
                or record.get("landed") is False
                or (is_upgrade_history and is_upgrade_history(record))
        ):
            return []
        media_type = str(record.get("type") or "")
        if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
            return []
        title = str(record.get("title") or "").strip()
        if not title:
            return []

        tmdb_id = positive_int(record.get("tmdb_id"))

        file_name = str(record.get("file_name") or "").strip()
        source_name = str(record.get("source_file_name") or file_name).strip()
        cloud_dir = str(record.get("cloud_dir") or "/").strip() or "/"
        destination = (
            str(PurePosixPath(cloud_dir) / file_name) if file_name else cloud_dir
        )
        try:
            file_size = max(0, int(record.get("file_size") or 0))
        except (TypeError, ValueError):
            file_size = 0
        source_storage = self._platform_source_label(record)
        # 目标存 MP 存储名「115网盘CS版」，不用网盘驱动名「115网盘」：
        # TransferHistory 的 dest_storage 指 MP 存储；驱动名与 MP 原生
        # 115 存储同名，会让人以为文件进了原生 115 而不是 CS 版。
        dest_storage = self._u115cs_storage_name()
        common = {
            "src_storage": source_storage,
            "src_fileitem": {
                "name": source_name,
                "path": source_name,
                "size": file_size,
                "storage": source_storage,
            },
            "dest": destination,
            "dest_storage": dest_storage,
            "dest_fileitem": {
                "name": file_name,
                "path": destination,
                "size": file_size,
                "storage": dest_storage,
            },
            "mode": "copy",
            "type": media_type,
            "title": title,
            "year": str(record.get("year") or "") or None,
            "tmdbid": tmdb_id,
            "imdbid": str(record.get("imdb_id") or "").strip() or None,
            "tvdbid": positive_int(record.get("tvdb_id")),
            "doubanid": str(record.get("douban_id") or "").strip() or None,
            "bangumiid": positive_int(record.get("bangumi_id")),
            "anilistid": positive_int(record.get("anilist_id")),
            "media_source": str(record.get("media_source") or "").strip() or None,
            "media_id": str(record.get("media_id") or "").strip() or None,
            "category": str(record.get("category") or "").strip() or None,
            "episode_group": str(record.get("episode_group") or "").strip() or None,
            "image": str(record.get("image") or "").strip() or None,
            "status": True,
            "files": [destination] if destination else [],
            "downloader": "网盘订阅助手",
            "date": str(record.get("time") or "").strip() or None,
        }
        if media_type == MediaType.MOVIE.value:
            return [{
                **common,
                "src": self._platform_source_path(record),
            }]

        season = positive_int(record.get("season")) or 1
        episodes = self._platform_episode_numbers(record)
        if not episodes:
            return [{
                **common,
                "src": self._platform_source_path(record, season=season),
                "seasons": f"S{season:02d}",
            }]
        return [
            {
                **common,
                "src": self._platform_source_path(
                    record, season=season, episode=episode
                ),
                "seasons": f"S{season:02d}",
                "episodes": f"E{episode:02d}",
            }
            for episode in episodes
        ]

    def _record_platform_transfer_histories(
            self,
            records: List[Dict[str, Any]],
            reconcile: bool = False,
    ) -> int:
        # 1.12.14：插件不再往 MP 的 TransferHistory 里写记录。
        # MP 的整理历史是唯一账本，只有 MP 自己能写（do_transfer /
        # 整理）；插件直写平台记录（PanSou → 115网盘CS版、复制）会
        # 和 MP 自己的「本地 → 115网盘CS版、移动」成对出现，同一集
        # 看起来整理了两遍。8 处写入点与启动对账都因此停用；已有
        # 的插件记录由 remove_platform_transfer_history_mirror_once
        # 一次性清理。
        return 0

    @staticmethod
    def _history_image_from_download(record: Dict[str, Any]) -> Optional[str]:
        """从下载历史恢复旧版插件记录缺失的海报。"""
        media_type = str(record.get("type") or "").strip()
        tmdb_id = positive_int(record.get("tmdb_id"))
        title = str(record.get("title") or "").strip()
        year = str(record.get("year") or "").strip() or None
        if media_type not in {MediaType.MOVIE.value, MediaType.TV.value}:
            return None
        if not tmdb_id and not title:
            return None
        try:
            histories = get_download_history_last_by(
                DownloadHistoryOper(),
                mtype=media_type,
                title=title or None,
                year=year,
                tmdb_id=tmdb_id,
            )
        except Exception as error:
            logger.debug(f"从下载历史恢复整理海报失败：{title}，{error}")
            return None
        return next(
            (
                str(getattr(item, "image", "") or "").strip()
                for item in (histories or [])
                if str(getattr(item, "image", "") or "").strip()
            ),
            None,
        )

    def _delete_platform_transfer_histories(
            self,
            records: Optional[List[Dict[str, Any]]] = None,
            all_managed: bool = False,
    ) -> int:
        """增量删除插件托管的整理历史，不触碰关联媒体文件。"""
        sources = {
            entry["src"]
            for record in (records or [])
            if isinstance(record, dict)
            for entry in self._platform_history_entries(record)
        }
        if not all_managed and not sources:
            return 0
        try:
            from app.db.models.transferhistory import TransferHistory
            from sqlalchemy import or_

            lock = getattr(self, "_platform_history_lock", None)
            if lock and not lock.acquire(timeout=1.0):
                logger.debug("MoviePilot 整理历史删除仍在执行，本批次已跳过")
                return 0
            try:
                with SessionFactory() as db:
                    query = db.query(TransferHistory).filter(or_(
                        TransferHistory.src.like("cloudsubscribe://%"),
                        TransferHistory.downloader == "网盘订阅助手",
                    ))
                    if not all_managed:
                        query = query.filter(TransferHistory.src.in_(sources))
                    deleted = query.delete(synchronize_session=False)
                    if deleted:
                        db.commit()
                    return int(deleted or 0)
            finally:
                if lock:
                    lock.release()
        except Exception as error:
            logger.error(f"清理整理历史失败：{error}")
            return 0

    def sync_platform_transfer_history(self) -> int:
        """只恢复插件自身 history 记录缺失的海报，不再镜像到 MP 整理历史。

        1.12.14：MP 的整理历史是唯一账本，插件不再直写平台记录，
        启动对账（镜像）随之取消。这里只保留海报恢复（插件自己
        的转存记录页用），返回值是恢复的海报条数。
        """
        get_data = getattr(self, "_get_data", None)
        save_data = getattr(self, "_save_data", None)
        if not get_data:
            return 0
        history = get_data("history") or []
        image_cache: Dict[Tuple[Any, ...], Optional[str]] = {}
        restored = 0
        for record in history:
            if (
                    not isinstance(record, dict)
                    or str(record.get("status") or "") != "成功"
                    or str(record.get("image") or "").strip()
            ):
                continue
            cache_key = (
                record.get("type"),
                record.get("tmdb_id"),
                record.get("title"),
                record.get("year"),
            )
            if cache_key not in image_cache:
                image_cache[cache_key] = self._history_image_from_download(record)
            image = image_cache[cache_key]
            if image:
                record["image"] = image
                restored += 1
        if restored and save_data:
            save_data("history", history)
            logger.info(f"已从下载历史恢复 {restored} 条插件整理记录海报")
        return restored

    def remove_platform_transfer_history_mirror_once(self) -> int:
        """一次性清理插件已写进 MP 整理历史的镜像记录，只跑一次。

        删掉全部插件托管行（src 以 cloudsubscribe:// 开头，或
        downloader 为「网盘订阅助手」），用数据标记
        platform_history_mirror_removed 保证只清理一次；之后插件
        不再写这类记录，MP 的整理历史只剩 MP 自己写的账。
        """
        get_data = getattr(self, "_get_data", None)
        save_data = getattr(self, "_save_data", None)
        if get_data and get_data("platform_history_mirror_removed"):
            return 0
        removed = self._delete_platform_transfer_histories(all_managed=True)
        if save_data:
            save_data("platform_history_mirror_removed", True)
        if removed:
            logger.info(f"已清理插件写入 MP 整理历史的镜像记录 {removed} 条")
        return int(removed or 0)
