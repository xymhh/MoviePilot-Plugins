"""现有媒体版本基线。"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.db import SessionFactory
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import (
    get_transfer_history_by,
    media_identity,
    tmdb_id_of,
)
from ...utils.cache import normalize_platform_cache_key
from app.log import logger


class UpgradeBaselineService(OwnerDelegator):
    @staticmethod
    def _copy_episode_items(
            value: Dict[int, List[Dict[str, Any]]]
    ) -> Dict[int, List[Dict[str, Any]]]:
        return {
            int(episode): [dict(item) for item in items]
            for episode, items in value.items()
        }

    @staticmethod
    def _baseline_key(subscribe, season: int) -> tuple:
        return (
            int(tmdb_id_of(subscribe) or 0),
            str(getattr(subscribe, "name", "") or "").casefold(),
            str(getattr(subscribe, "year", "") or ""),
            int(season or 1),
        )

    @staticmethod
    def _record_value(record: Any, key: str, default: Any = None) -> Any:
        if isinstance(record, dict):
            return record.get(key, default)
        return getattr(record, key, default)

    @staticmethod
    def _decode_file_item(value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                return decoded if isinstance(decoded, dict) else {}
            except (TypeError, ValueError):
                logger.debug("_decode_file_item 分支忽略异常", exc_info=True)
        return {}

    @staticmethod
    def _int_or_zero(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _episode_numbers(value: Any) -> List[int]:
        """解析的 E01、E01-E03、列表等剧集表达。"""
        if isinstance(value, (list, tuple, set)):
            return sorted({int(item) for item in value if str(item).isdigit()})
        text = str(value or "").strip()
        marked = [int(item) for item in re.findall(r"E(\d+)", text, re.I)]
        numbers = marked or [int(item) for item in re.findall(r"\d+", text)]
        if len(numbers) >= 2 and "-" in text:
            return list(range(numbers[0], numbers[-1] + 1))
        return sorted(set(numbers))

    def _transfer_history_episode_files(
            self, subscribe, season: int
    ) -> Dict[int, List[Dict[str, Any]]]:
        """一次查询整季整理历史，按集建立文件候选索引。"""
        from app.db.transferhistory_oper import TransferHistoryOper

        cache_key = normalize_platform_cache_key(
            self._baseline_key(subscribe, season)
        )
        with self._baseline_cache_lock:
            cached = self._baseline_transfer_cache.get(cache_key)
            if cached is not None:
                return self._copy_episode_items(cached)
        media_source, media_id = media_identity(subscribe)
        tmdbid = tmdb_id_of(subscribe)
        with SessionFactory() as db:
            oper = TransferHistoryOper(db=db)
            records = get_transfer_history_by(
                oper,
                mtype=MediaType.TV.value,
                tmdb_id=tmdbid,
                media_source=media_source,
                media_id=media_id,
                season=f"S{season:02d}",
            ) if media_id or tmdbid else []
            if not records:
                records = oper.get_by(
                    mtype=MediaType.TV.value,
                    title=getattr(subscribe, "name", None),
                    year=str(getattr(subscribe, "year", None) or "") or None,
                    season=f"S{season:02d}",
                ) or []

        episode_files: Dict[int, List[Dict[str, Any]]] = {}
        for record in records or []:
            file_item = self._decode_file_item(
                self._record_value(record, "src_fileitem")
                or self._record_value(record, "dest_fileitem")
            )
            file_name = str(
                file_item.get("name") or file_item.get("basename")
                or Path(str(self._record_value(record, "src", "") or "")).name
            ).strip()
            if not file_name:
                continue
            item = {
                "file_name": file_name,
                "file_size": self._int_or_zero(file_item.get("size")),
                "source": "MoviePilot整理记录",
            }
            for episode in self._episode_numbers(
                    self._record_value(record, "episodes")):
                episode_files.setdefault(episode, []).append(item)
        with self._baseline_cache_lock:
            self._baseline_transfer_cache.set(
                cache_key, self._copy_episode_items(episode_files)
            )
        return episode_files

    def _gather_episode_version_candidates(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            score_cache: Optional[Dict[tuple, int]] = None,
    ) -> Dict[int, List[Dict[str, Any]]]:
        """收集逐集现有版本的全部候选（整理历史 + 手动基线 + 媒体服务器）。

        Phase 3a 第 4 项：洗版三处合一的共享候选层。``_build_episode_baseline``
        与 ``_collect_episode_versions``（经 ``_baseline_episode_versions``）
        都走这里，不再各写一份候选收集。评分结果写进 ``score_cache``（同一
        查询内同名同体积只评一次）；不传时用一次性的本地缓存。
        """
        cache = score_cache if score_cache is not None else {}

        def _score(rule_title: str, file_size: int) -> int:
            score_key = (str(rule_title or "").strip(), int(file_size or 0))
            score = cache.get(score_key)
            if score is None:
                score = self._get_mp_rule_score(
                    rule_title, file_size, subscribe, season, mediainfo
                )
                cache[score_key] = score
            return int(score or 0)

        candidates = self._transfer_history_episode_files(subscribe, season)

        manual_baseline = getattr(subscribe, "_manual_media_baseline", {}) or {}
        for episode, item in (manual_baseline.get("episodes") or {}).items():
            try:
                episode_number = int(episode)
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                candidates.setdefault(episode_number, []).append(dict(item))

        media_server_key = normalize_platform_cache_key((
            *self._baseline_key(subscribe, season),
            int(getattr(mediainfo, "tmdb_id", 0) or 0),
        ))
        with self._baseline_cache_lock:
            cached_media_server = self._baseline_media_server_cache.get(
                media_server_key
            )
        if cached_media_server is None:
            _, media_server_items = self._media_server_resolver.episode_media(
                chain=self._chain,
                mediainfo=mediainfo,
                season=season,
            )
            with self._baseline_cache_lock:
                self._baseline_media_server_cache.set(media_server_key, {
                    int(episode): dict(item)
                    for episode, item in media_server_items.items()
                })
        else:
            media_server_items = {
                int(episode): dict(item)
                for episode, item in cached_media_server.items()
            }
        for episode, media_item in media_server_items.items():
            media_file = Path(str(media_item.get("path") or ""))
            file_size = self._int_or_zero(media_item.get("size"))
            if not file_size and media_file.suffix.lower() != ".strm":
                try:
                    file_size = media_file.stat().st_size
                except OSError:
                    logger.debug("_build_episode_baseline 分支忽略异常", exc_info=True)
            candidates.setdefault(int(episode), []).append({
                "file_name": media_file.name,
                "rule_title": str(
                    media_item.get("rule_title") or media_file.name
                ).strip(),
                "file_size": file_size,
                "source": "媒体服务器",
            })

        scored: Dict[int, List[Dict[str, Any]]] = {}
        for episode, items in candidates.items():
            scored_items = []
            for item in items:
                rule_title = str(
                    item.get("rule_title") or item["file_name"]
                ).strip()
                score = _score(rule_title, int(item["file_size"] or 0))
                scored_items.append(
                    {**item, "score": score, "rule_score": score}
                )
            if scored_items:
                scored[int(episode)] = scored_items
        return scored

    def _build_episode_baseline(
            self,
            subscribe,
            mediainfo: MediaInfo,
            season: int,
            include_saved: bool = True,
            score_cache: Optional[Dict[tuple, int]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """每集现有最佳版本（Phase 3a 第 4 项：洗版三处合一）。

        候选来自 ``_gather_episode_version_candidates``（整理历史、手动基线、
        媒体服务器），真实网盘文件来自 ``_scan_cloud_resource_episode_files``
        （与 ``_collect_episode_versions`` 共用 ``_season_dir_files`` 的缓存
        列目录结果，同一季只列一次目录）；评分结果在 ``score_cache`` 内共享，
        同一查询内同名同体积只评一次。STRM 不提供文件大小。
        """
        cache = score_cache if score_cache is not None else {}
        candidates = self._gather_episode_version_candidates(
            subscribe, mediainfo, season, score_cache=cache
        )
        # 真实网盘文件覆盖：旧版这段在 upgrade.py 里（基线 + 扫描两次查询再手
        # 动合并），现收进基线内部。扫描失败时只丢覆盖信息，不丢基线本身。
        try:
            cloud_valid, cloud_episode_files, cloud_dir = (
                self._scan_cloud_resource_episode_files(
                    subscribe=subscribe,
                    mediainfo=mediainfo,
                    season=season,
                    start_episode=max(
                        1, int(getattr(subscribe, "start_episode", 1) or 1)
                    ),
                    total_episode=max(
                        0, int(getattr(subscribe, "total_episode", 0) or 0)
                    ),
                )
            )
        except Exception as error:
            logger.debug(
                f"洗版基线网盘扫描失败：{mediainfo.title_year}，{error}"
            )
            cloud_valid, cloud_episode_files, cloud_dir = False, {}, ""

        baseline: Dict[int, Dict[str, Any]] = {}
        for episode, items in candidates.items():
            best = dict(max(
                items,
                key=lambda item: (
                    int(item.get("score") or 0),
                    (
                        int(int(item.get("file_size") or 0) > 0)
                        if self._upgrade_mode == "smallest" else 0
                    ),
                    (
                        -int(item.get("file_size") or 0)
                        if self._upgrade_mode == "smallest"
                        else int(item.get("file_size") or 0)
                    ),
                ),
            ))
            media_server_size = max(
                (
                    self._int_or_zero(item.get("file_size"))
                    for item in items
                    if str(item.get("source") or "") == "媒体服务器"
                ),
                default=0,
            )
            if media_server_size and not int(best.get("file_size") or 0):
                best["file_size"] = media_server_size
                best["size_source"] = "媒体服务器"
            if cloud_valid:
                cloud_file = cloud_episode_files.get(int(episode))
                if cloud_file:
                    cloud_name = str(getattr(cloud_file, "name", "") or "")
                    cloud_size = int(getattr(cloud_file, "size", 0) or 0)
                    if not int(best.get("file_size") or 0):
                        best["file_size"] = cloud_size
                        best["size_source"] = "真实网盘文件"
                    best.update({
                        "cloud_dir": cloud_dir,
                        "target_file_name": cloud_name,
                        "cloud_file_id": str(
                            getattr(cloud_file, "id", "") or ""
                        ),
                    })
            baseline[int(episode)] = best

        if cloud_valid:
            for episode, cloud_file in cloud_episode_files.items():
                episode_number = int(episode)
                if episode_number in baseline:
                    continue
                cloud_name = str(getattr(cloud_file, "name", "") or "")
                cloud_size = int(getattr(cloud_file, "size", 0) or 0)
                score_key = (cloud_name, cloud_size)
                score = cache.get(score_key)
                if score is None:
                    score = self._get_mp_rule_score(
                        cloud_name, cloud_size, subscribe, season, mediainfo
                    )
                    cache[score_key] = score
                baseline[episode_number] = {
                    "file_name": cloud_name,
                    "file_size": cloud_size,
                    "source": "真实网盘文件",
                    "score": int(score or 0),
                    "rule_score": int(score or 0),
                    "cloud_dir": cloud_dir,
                    "target_file_name": cloud_name,
                    "cloud_file_id": str(
                        getattr(cloud_file, "id", "") or ""
                    ),
                }

        if include_saved:
            for episode, score in self._read_ep_priority(subscribe).items():
                try:
                    episode_number = int(episode)
                    score_number = int(score or 0)
                except (TypeError, ValueError):
                    continue
                baseline.setdefault(episode_number, {
                    "file_name": "",
                    "file_size": 0,
                    "source": "订阅历史评分",
                    "score": score_number,
                    "rule_score": score_number,
                })
        return baseline
