"""配置页选项与详情页数据 API。"""

from __future__ import annotations

import copy
import datetime
import os
import re
import time
from typing import Any, Dict
from urllib.parse import quote, urlencode
import pytz
from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType

from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ..config import UIConfig
from ..episode_parse import parse_episodes_from_names as _parse_episodes_from_names
from ...drive.scanner import DriverRegistry
from ...search.scanner import SearchSourceRegistry
from ...search.types import (
    resource_type_aliases,
    resource_type_catalog,
)
from ...utils.cache import create_platform_ttl_cache


def _display_catalog() -> dict:
    """资源类型与搜索渠道的展示元数据：名称、图标、配色与顺序全部由后端下发。"""
    return {
        "resource_types": resource_type_catalog(),
        "resource_type_aliases": resource_type_aliases(),
        "sources": SearchSourceRegistry.get_source_catalog(),
    }


_UI_OPTIONS_CACHE = create_platform_ttl_cache(
    "ui:options", maxsize=16, ttl=2 * 60
)
# key 带版本号：平台 TTL 缓存跨插件重载存活，换结构时必须换前缀，否则旧条目缺字段
_AIRED_INFO_CACHE = create_platform_ttl_cache(
    "ui:aired:v2", maxsize=512, ttl=6 * 60 * 60
)

# 本地媒体库（STRM 落盘目录）索引：追更进度里的「已入库」必须反映磁盘上真实存在的集，
# 只看订阅 note 会漏掉「订阅开始前就已入库」的集（例如订阅 start_episode=3 时 E1/E2 永远补不上）。
# 追更日历：只展示未来 N 天内的待更新集（TMDB 只有日期，没有钟点）
_CALENDAR_HORIZON_DAYS = 30
_LOCAL_LIBRARY_TTL = 60.0
_LOCAL_LIBRARY_STATE: dict[str, Any] = {"at": 0.0, "root": "", "index": {}}
_SEASON_DIR_PATTERN = re.compile(r"^(?:season[\s._-]*|s)(\d{1,2})$|^第\s*(\d{1,2})\s*季$", re.IGNORECASE)
_TMDB_TAG_PATTERN = re.compile(r"\{tmdb[-_]?(\d+)\}", re.IGNORECASE)
_YEAR_TAG_PATTERN = re.compile(r"[（(](\d{4})[)）]")


def _match_season_dir(dir_name: str) -> int | None:
    """识别 `Season 1` / `S01` / `第1季` 形式的季目录。"""
    match = _SEASON_DIR_PATTERN.match((dir_name or "").strip())
    if not match:
        return None
    number = match.group(1) or match.group(2)
    try:
        season = int(number)
    except (TypeError, ValueError):
        return None
    return season if 0 < season < 100 else None


def _normalize_media_name(value: str) -> str:
    """媒体目录名归一化：去掉 tmdb 标签、年份与标点，便于按片名兜底匹配。"""
    text = _TMDB_TAG_PATTERN.sub(" ", str(value or ""))
    text = _YEAR_TAG_PATTERN.sub(" ", text)
    text = re.sub(r"[\[\]【】{}()（）._·:：!！?？,，、\-—~]+", " ", text)
    text = re.sub(r"\s+", "", text)
    return text.strip().lower()


def _aired_episode_info(tmdb_id: int, season: int) -> dict | None:
    """按 TMDB 本季分集返回已播数量、最新已播日期，以及未来待更新的集（追更日历数据）。

    TMDB 只给日期没有钟点，所以日历只到「哪天播」，不编造时间。
    未来集与已播集来自同一次 season detail 请求，不额外增加 API 调用。
    """
    try:
        from app.modules.themoviedb.tmdbapi import TmdbApi

        detail = TmdbApi().get_tv_season_detail(int(tmdb_id), int(season)) or {}
        today_date = datetime.date.today()
        today = today_date.isoformat()
        horizon = (today_date + datetime.timedelta(days=_CALENDAR_HORIZON_DAYS)).isoformat()
        aired: list[tuple[str, int]] = []
        upcoming: list[dict[str, Any]] = []
        for item in detail.get("episodes") or []:
            if not isinstance(item, dict):
                continue
            air_date = str(item.get("air_date") or "").strip()
            if not air_date:
                continue
            try:
                episode = int(item.get("episode_number") or 0)
            except (TypeError, ValueError):
                episode = 0
            if episode <= 0:
                continue
            if air_date <= today:
                aired.append((air_date, episode))
            elif air_date <= horizon:
                upcoming.append({"episode": episode, "air_date": air_date})
        upcoming.sort(key=lambda item: (item["air_date"], item["episode"]))
        if not aired:
            return {"aired": 0, "episode": 0, "air_date": "", "upcoming": upcoming}
        aired.sort(key=lambda item: (item[0], item[1]))
        latest_date, latest_episode = aired[-1]
        return {
            "aired": len(aired),
            "episode": latest_episode,
            "air_date": latest_date,
            "upcoming": upcoming,
        }
    except Exception as err:  # noqa: BLE001 - TMDB 不可用时只隐藏已播信息
        logger.debug(
            f"查询已播集数失败 tmdb={tmdb_id} season={season}: {err}",
            exc_info=True,
        )
        return None




def _platform_image_url(value: object, size: str = "w500") -> str:
    """使用 MoviePilot V3 官方图片缓存代理生成前端可直接加载的地址。"""
    raw = str(value or "").strip()
    if not raw or raw.startswith(("data:", "blob:")):
        return raw
    if not raw.lower().startswith(("http://", "https://")):
        if raw.startswith("/") and not raw.startswith("//"):
            raw = f"https://image.tmdb.org/t/p/{size}{raw}"
        else:
            builder = getattr(settings, "TMDB_IMAGE_URL", None)
            raw = builder(raw, size) if callable(builder) else raw
    return f"/api/v1/system/cache/image?url={quote(raw, safe='')}" if raw else ""


def _resolve_subscribe_tmdb_id(subscribe: object):
    """解析订阅的 TMDB ID。

    MoviePilot V3 起订阅模型改用 media_source + media_id（字符串）承载外部 ID，
    旧的 tmdbid / tmdb_id 字段在 V3 数据里常为空 —— 只读旧字段会让按 tmdb 合并
    转存历史的键全部失配，表现为「头部说缺 17 集、矩阵却把 93-106 涂成已入库」。
    """
    for attr in ("tmdbid", "tmdb_id"):
        value = getattr(subscribe, attr, None)
        if value not in (None, "", 0, "0"):
            return value

    media_id = getattr(subscribe, "media_id", None)
    if media_id in (None, "", 0, "0"):
        return None

    source = str(getattr(subscribe, "media_source", "") or "").strip().lower()
    # 未标注来源时按 TMDB 处理（V3 剧集订阅绝大多数来自 TMDB）
    if source and "tmdb" not in source and "themoviedb" not in source:
        return None
    return media_id






class PageApi(OwnerDelegator):
    @staticmethod
    def _history_filter_values(value: str) -> set[str]:
        return {
            item.strip().lower()
            for item in str(value or "").split(",")
            if item.strip()
        }

    def api_vue_page_data(
            self,
            page: int = 1,
            page_size: int = 10,
            keyword: str = "",
            resource_types: str = "",
            sources: str = "",
            task_types: str = "",
            statuses: str = "",
    ) -> dict:
        page_result = self._get_data_store().query_history_page(
            page=page,
            page_size=page_size,
            keyword=keyword,
            resource_types=self._history_filter_values(resource_types),
            sources=self._history_filter_values(sources),
            task_types=self._history_filter_values(task_types),
            statuses=self._history_filter_values(statuses),
        )
        page_groups = page_result.pop("groups", [])
        page_history = [
            record for group in page_groups
            for record in group.get("records", [])
        ]
        if self._sync_handler:
            page_history = self._sync_handler.prepare_history_records(
                page_history
            )
            prepared_by_id = {
                str(record.get("record_id") or ""): record
                for record in page_history
            }
            history_groups = []
            for group in page_groups:
                group["records"] = [
                    prepared_by_id.get(
                        str(record.get("record_id") or ""), record
                    )
                    for record in group.get("records", [])
                ]
                prepared_group = self._sync_handler.prepare_history_group(group)
                poster = (
                    prepared_group.get("poster")
                    or prepared_group.get("image")
                    or prepared_group.get("poster_path")
                    or prepared_group.get("poster_url")
                )
                if poster:
                    prepared_group["poster_url"] = _platform_image_url(poster, "w500")
                history_groups.append(prepared_group)
        else:
            page_history = [copy.deepcopy(item) for item in page_history]
            history_groups = [copy.deepcopy(group) for group in page_groups]
        return {
            "success": True,
            "data": {
                **_display_catalog(),
                "history_groups": history_groups,
                "history_page": {
                    "page": page_result["page"],
                    "page_size": page_result["page_size"],
                    "total": page_result["total"],
                    "total_pages": page_result["total_pages"],
                    "filter_options": page_result["filter_options"],
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                },
                "emby_play_items": self._history_emby_play_items(page_history),
                "subscribes_progress": self._build_subscribes_progress(page_history),
            },
        }

    def api_vue_aired_counts(self, payload: dict) -> dict:
        """批量查询订阅的已播集数与最新一集开播日期：前端先渲染列表，再异步补齐。"""
        raw_items = (payload or {}).get("items") if isinstance(payload, dict) else []
        entries: dict[str, tuple[int, int]] = {}
        for item in raw_items or []:
            if not isinstance(item, dict):
                continue
            try:
                tmdb_id = int(item.get("tmdb_id") or 0)
                raw_season = item.get("season")
                season = 1 if raw_season in (None, "") else int(raw_season)
            except (TypeError, ValueError):
                continue
            if tmdb_id <= 0 or season < 0:
                continue
            entries[f"{tmdb_id}:{season}"] = (tmdb_id, season)

        counts: dict[str, int] = {}
        latest: dict[str, dict] = {}
        upcoming: dict[str, list] = {}
        pending: list[tuple[str, int, int]] = []
        for key, (tmdb_id, season) in entries.items():
            cached = _AIRED_INFO_CACHE.get(key)
            # 旧结构（无 upcoming 字段）视为未命中，升级后自动补齐日历数据
            if isinstance(cached, dict) and "upcoming" in cached:
                counts[key] = max(0, int(cached.get("aired") or 0))
                if cached.get("air_date"):
                    latest[key] = {
                        "episode": int(cached.get("episode") or 0),
                        "air_date": str(cached.get("air_date")),
                    }
                if cached.get("upcoming"):
                    upcoming[key] = list(cached.get("upcoming") or [])
            elif cached is not None:
                counts[key] = max(0, int(cached))
            else:
                pending.append((key, tmdb_id, season))

        if pending:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            executor = ThreadPoolExecutor(max_workers=min(6, len(pending)))
            futures = {
                executor.submit(
                    _aired_episode_info, tmdb_id, season
                ): key
                for key, tmdb_id, season in pending
            }
            try:
                for future in as_completed(futures, timeout=8):
                    key = futures[future]
                    try:
                        info = future.result()
                    except Exception:  # noqa: BLE001 - 单条失败不影响其它订阅
                        info = None
                    if not isinstance(info, dict):
                        continue
                    counts[key] = max(0, int(info.get("aired") or 0))
                    if info.get("air_date"):
                        latest[key] = {
                            "episode": int(info.get("episode") or 0),
                            "air_date": str(info.get("air_date")),
                        }
                    if info.get("upcoming"):
                        upcoming[key] = list(info.get("upcoming") or [])
                    _AIRED_INFO_CACHE.set(key, info)
            except Exception as err:  # noqa: BLE001 - 超时后保留已有结果
                logger.debug(f"批量查询已播集数超时：{err}", exc_info=True)
            finally:
                executor.shutdown(wait=False)

        return {"success": True, "data": {"counts": counts, "latest": latest, "upcoming": upcoming}}

    def _local_library_index(self) -> dict[tuple, set[int]]:
        """扫描本地媒体库根目录，建立「tmdb/片名 + 季 → 磁盘上真实存在的集号」索引。"""
        root = str(getattr(self._owner, "_local_resource_path", "") or "").strip()
        if not root or not os.path.isdir(root):
            return {}
        now = time.monotonic()
        state = _LOCAL_LIBRARY_STATE
        if state.get("root") == root and state.get("index") and now - float(state.get("at") or 0) < _LOCAL_LIBRARY_TTL:
            return state["index"]

        index: dict[tuple, set[int]] = {}
        try:
            for dir_path, dir_names, file_names in os.walk(root):
                relative = dir_path[len(root):].strip(os.sep)
                # 媒体库结构最多到 类型/分类/媒体目录/季目录 这一层，再深不再遍历
                if relative and relative.count(os.sep) > 4:
                    dir_names[:] = []
                    continue
                season = _match_season_dir(os.path.basename(dir_path))
                if season is None:
                    continue
                dir_names[:] = []
                episodes = _parse_episodes_from_names(file_names, season)
                if not episodes:
                    continue
                media_dir = os.path.basename(os.path.dirname(dir_path))
                tmdb_match = _TMDB_TAG_PATTERN.search(media_dir)
                if tmdb_match:
                    index.setdefault(("tmdb", tmdb_match.group(1), season), set()).update(episodes)
                media_name = _normalize_media_name(media_dir)
                if media_name:
                    year_match = _YEAR_TAG_PATTERN.search(media_dir)
                    year = int(year_match.group(1)) if year_match else 0
                    index.setdefault(("title", media_name, year, season), set()).update(episodes)
        except OSError as error:
            logger.debug(f"本地媒体库索引构建失败：{error}")
            return {}

        _LOCAL_LIBRARY_STATE.update({"at": now, "root": root, "index": index})
        if index:
            logger.debug(f"本地媒体库索引已刷新：{len(index)} 条季记录，根目录 {root}")
        return index

    def _local_library_episodes(
            self,
            tmdb_id: Any,
            title: Any,
            year: Any,
            season: Any,
    ) -> set[int]:
        """取本地媒体库中该季真实存在的集号；无索引或未命中返回空集。"""
        index = self._local_library_index()
        if not index:
            return set()
        try:
            season_no = int(season or 1)
        except (TypeError, ValueError):
            season_no = 1
        if tmdb_id:
            hit = index.get(("tmdb", str(tmdb_id), season_no))
            if hit:
                return set(hit)
        media_name = _normalize_media_name(title)
        if not media_name:
            return set()
        try:
            year_no = int(year or 0)
        except (TypeError, ValueError):
            year_no = 0
        hit = index.get(("title", media_name, year_no, season_no))
        if hit:
            return set(hit)
        # 年份标签缺失或写法不同（如目录只写片名）时，按片名兜底合并
        merged: set[int] = set()
        for key, episodes in index.items():
            if key[0] == "title" and key[1] == media_name and key[3] == season_no:
                merged.update(episodes)
        return merged

    def _build_subscribes_progress(self, page_history: list[dict[str, Any]] = None) -> list[dict[str, Any]]:
        """构建以 MoviePilot 订阅为核心的追更进度大屏数据。"""
        subscribes = []
        history_success_map: dict[str, set[int]] = {}
        # 电影没有集号，episode 为空时进不了 history_success_map，
        # 单独留一份「成功落地的 tmdb id」用于判定电影是否已入库
        history_success_tmdb: set[str] = set()
        try:
            from app.db import SessionFactory
            from app.db.models.subscribe import Subscribe
            with SessionFactory() as db:
                subscribes = list(db.query(Subscribe).all())
        except Exception as err:
            logger.warning(f"通过 SessionFactory 读取订阅失败: {err}")
            try:
                from app.db.subscribe_oper import SubscribeOper
                subscribes = SubscribeOper().list() or []
            except Exception as err2:
                logger.warning(f"回退读取订阅列表仍失败: {err2}")
                return []

        # 转存历史存在插件自己的 SQLite（history_records 表），
        # 用 MoviePilot 的 SessionFactory 去查只会静默返回空 —— 必须走插件数据仓库。
        try:
            store = self._get_data_store()
            store.initialize()
            history_rows = store.repositories.history.list_all() or []
        except Exception as err:
            logger.warning(f"读取插件转存历史失败，已入库将只统计订阅 note: {err}")
            history_rows = []
        if not history_rows:
            # 空历史会让「已入库」永远等于订阅 note，属于必须能被发现的口径故障
            logger.warning("插件转存历史为空，已入库将只统计订阅 note")

        for rec in history_rows:
            if not isinstance(rec, dict):
                continue
            if str(rec.get("status") or "") not in ("成功", "完成"):
                continue
            hr_tmdb = str(rec.get("tmdb_id") or "").strip()
            if hr_tmdb:
                history_success_tmdb.add(hr_tmdb)
            ep = rec.get("episode")
            try:
                int_ep = int(ep)
            except (TypeError, ValueError):
                continue
            if int_ep <= 0:
                continue
            hr_season = rec.get("season") or 1
            hr_sub = rec.get("subscribe_id")
            hr_title = str(rec.get("title") or "").strip()
            if hr_sub:
                history_success_map.setdefault(f"sub:{hr_sub}", set()).add(int_ep)
            if hr_tmdb:
                history_success_map.setdefault(f"tmdb:{hr_tmdb}:{hr_season}", set()).add(int_ep)
            if hr_title:
                history_success_map.setdefault(f"title:{hr_title}:{hr_season}", set()).add(int_ep)

        active_tasks = []
        owner = getattr(self, "_owner", None)
        if owner and hasattr(owner, "_runtime_service"):
            try:
                active_tasks = owner._runtime_service.get_tasks() or []
            except Exception as error:  # noqa: BLE001 - 任务看板不可用不该拖垮进度表
                logger.debug(f"读取运行任务失败：{error}", exc_info=True)

        history_items = page_history or []
        progress_list = []
        build_errors = 0
        for sub in subscribes:
            try:
                sub_id = int(getattr(sub, "id", 0) or 0)
                name = str(getattr(sub, "name", "") or "未知剧集")
                year = str(getattr(sub, "year", "") or "")
                season = getattr(sub, "season", None)
                if season is not None:
                    try:
                        season = int(season)
                    except (ValueError, TypeError):
                        season = None

                total_episodes = int(getattr(sub, "total_episode", 0) or 0)
                lack_episode = int(getattr(sub, "lack_episode", 0) or 0)
                raw_note = getattr(sub, "note", []) or []
                if isinstance(raw_note, str):
                    try:
                        raw_note = [int(x.strip()) for x in raw_note.split(",") if x.strip().isdigit()]
                    except Exception:
                        raw_note = []
                library_episodes = {int(x) for x in raw_note if str(x).isdigit() and int(x) > 0}

                # 必须先解析 tmdb_id，下面合并转存历史时要用它做键
                tmdb_id = _resolve_subscribe_tmdb_id(sub)
                state = str(getattr(sub, "state", "R") or "R")
                # 电影/剧集判定：优先用订阅自身的 type，缺省时按有无季号推断
                sub_type = str(getattr(sub, "type", "") or "").strip()
                is_movie = sub_type == "电影" or (not sub_type and season is None)

                # 合并转存历史中已成功的集数，确保转存后列表即时更新
                eff_season = season or 1
                history_eps = set(history_success_map.get(f"sub:{sub_id}", set()))
                if tmdb_id:
                    history_eps |= history_success_map.get(
                        f"tmdb:{tmdb_id}:{eff_season}", set()
                    )
                if name:
                    history_eps |= history_success_map.get(
                        f"title:{name}:{eff_season}", set()
                    )
                library_episodes = sorted(library_episodes | history_eps)

                # 媒体库实测优先：磁盘上真实存在的集一定算已入库。
                # 订阅 note 只记录 MoviePilot 下载过的集，订阅从第 N 集开始时早期集永远补不上。
                if not is_movie:
                    local_episodes = self._local_library_episodes(tmdb_id, name, year, eff_season)
                    if local_episodes:
                        library_episodes = sorted(set(library_episodes) | local_episodes)

                # 海报多级回查与增强
                poster = str(getattr(sub, "poster", "") or getattr(sub, "backdrop", "") or "")
                if not poster and history_items:
                    for rec in history_items:
                        rec_tmdb = str(rec.get("tmdb_id") or "")
                        rec_title = str(rec.get("title") or "")
                        if (tmdb_id and rec_tmdb == str(tmdb_id)) or rec_title == name:
                            cand = rec.get("image") or rec.get("poster") or rec.get("poster_path")
                            if cand:
                                poster = str(cand)
                                break
                poster_url = _platform_image_url(poster, "w500") if poster else ""

                transferring = []
                for t in active_tasks:
                    if int(t.get("subscribe_id") or 0) == sub_id:
                        ep = t.get("episode")
                        if ep and int(ep) > 0:
                            transferring.append(int(ep))
                transferring_episodes = sorted(set(transferring))

                # 挂载该订阅对应的真实转存记录（电影判定也要用，故提前计算）
                matched_records = [
                    rec for rec in history_items
                    if int(rec.get("subscribe_id") or 0) == sub_id
                    or (tmdb_id and str(rec.get("tmdb_id") or "") == str(tmdb_id))
                    or str(rec.get("title") or "") == name
                ]

                if total_episodes > 0:
                    missing_episodes = [
                        i for i in range(1, total_episodes + 1)
                        if i not in library_episodes and i not in transferring_episodes
                    ]
                    missing_count = len(missing_episodes)
                elif is_movie:
                    # 电影没有集数概念：已入库 = 有集数记录 ∪ 有成功转存流水。
                    # MoviePilot 的 lack_episode 在电影订阅上常为 None，直接沿用会让
                    # 「一次都没转存成功」的电影被算成「已全部入库」。
                    landed = (
                        bool(library_episodes)
                        or (bool(tmdb_id) and str(tmdb_id) in history_success_tmdb)
                        or any(
                            str(rec.get("status") or "") in ("成功", "完成")
                            for rec in matched_records
                        )
                    )
                    if landed:
                        library_episodes = [1]
                        transferring_episodes = []
                        missing_episodes = []
                        missing_count = 0
                    else:
                        library_episodes = []
                        missing_episodes = [1]
                        missing_count = 1
                else:
                    missing_episodes = []
                    missing_count = lack_episode

                # 挂载该订阅对应的真实转存记录
                matched_records = [
                    rec for rec in history_items
                    if int(rec.get("subscribe_id") or 0) == sub_id
                    or (tmdb_id and str(rec.get("tmdb_id") or "") == str(tmdb_id))
                    or str(rec.get("title") or "") == name
                ]

                progress_list.append({
                    "subscribe_id": sub_id,
                    "name": name,
                    "title": name,
                    "year": year,
                    "season": season,
                    "type": "电影" if is_movie else "电视剧",
                    "media_type": "movie" if is_movie else "tv",
                    "total_episodes": total_episodes,
                    "library_episodes": library_episodes,
                    "library_count": len(library_episodes),
                    "transferring_episodes": transferring_episodes,
                    "missing_episodes": missing_episodes,
                    "missing_count": missing_count,
                    "tmdb_id": tmdb_id,
                    "state": state,
                    "poster": poster,
                    "poster_url": poster_url,
                    "records": matched_records,
                })
            except Exception as err:
                # 单条订阅失败不能静默：整表归零会让前端退化回历史卡片视图
                build_errors += 1
                if build_errors <= 3:
                    logger.warning(f"处理订阅条目追更进度失败(累计 {build_errors}): {err}")
                continue

        if build_errors:
            logger.warning(
                f"追更进度构建部分失败：{build_errors} 条订阅被跳过，"
                f"成功 {len(progress_list)} 条"
            )

        # 按同剧同季自动合并重复订阅，避免数据库中历史重复卡片导致大屏泛滥
        dedup_map = {}
        for item in progress_list:
            dedup_key = (
                str(item["tmdb_id"]) if item.get("tmdb_id") else item["name"].strip().lower(),
                item.get("season"),
            )
            if dedup_key not in dedup_map:
                dedup_map[dedup_key] = item
            else:
                existing = dedup_map[dedup_key]
                merged_lib = sorted(set(existing["library_episodes"]) | set(item["library_episodes"]))
                existing["library_episodes"] = merged_lib
                existing["library_count"] = len(merged_lib)
                if existing["total_episodes"] > 0:
                    existing["missing_episodes"] = [
                        i for i in range(1, existing["total_episodes"] + 1)
                        if i not in merged_lib and i not in existing["transferring_episodes"]
                    ]
                    existing["missing_count"] = len(existing["missing_episodes"])
                dup_ids = existing.setdefault("duplicate_subscribe_ids", [existing["subscribe_id"]])
                if item["subscribe_id"] not in dup_ids:
                    dup_ids.append(item["subscribe_id"])
                existing["records"].extend(item.get("records") or [])
                if not existing.get("poster_url") and item.get("poster_url"):
                    existing["poster_url"] = item["poster_url"]
                    existing["poster"] = item.get("poster") or ""

        final_list = list(dedup_map.values())
        final_list.sort(key=lambda x: (
            0 if (x["missing_count"] > 0 and x["state"] != "S") else 1,
            -x["subscribe_id"],
        ))
        return final_list

    def api_vue_history_summary(self) -> dict:
        today = datetime.datetime.now(
            tz=pytz.timezone(settings.TZ)
        ).strftime("%Y-%m-%d")
        return {
            "success": True,
            "data": self._get_data_store().history_summary(today),
        }

    def _get_plugin_config(self) -> Dict[str, Any]:
        """获取当前插件的完整持久化或运行时配置字典。"""
        owner = getattr(self, "_owner", None)
        if owner:
            if hasattr(owner, "get_config"):
                try:
                    cfg = owner.get_config()
                    if isinstance(cfg, dict) and cfg:
                        return cfg
                except Exception:
                    logger.debug("_get_plugin_config 分支忽略异常", exc_info=True)
            if hasattr(owner, "_applied_config"):
                cfg = getattr(owner, "_applied_config", None)
                if isinstance(cfg, dict) and cfg:
                    return cfg
            if hasattr(owner, "_config"):
                cfg = getattr(owner, "_config", None)
                if isinstance(cfg, dict) and cfg:
                    return cfg
        return getattr(self, "__dict__", {}) or {}

    def api_vue_ui_options(self, scope: str = "base", refresh: bool = False) -> dict:
        normalized_scope = str(scope or "base").strip().lower()
        normalized_scope = {
            "transfer": "subscriptions",
            "upgrade": "subscriptions",
            "manual": "subscriptions",
        }.get(normalized_scope, normalized_scope)
        if normalized_scope not in {
            "base", "subscriptions", "drive", "search", "pansou", "notify", "checkin"
        }:
            return {"success": False, "message": "未知的配置选项范围"}
        cache_key = f"instance:{id(self)}:{normalized_scope}"
        if refresh:
            _UI_OPTIONS_CACHE.pop(cache_key, None)
        cached = _UI_OPTIONS_CACHE.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        if normalized_scope == "checkin":
            from ..checkin_manager import get_checkin_schemas
            result = {
                "success": True,
                "data": {
                    "checkin_schemas": get_checkin_schemas(),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "pansou":
            from ...search.pansou.definition import PanSouSourceDefinition
            plugin_cfg = self._get_plugin_config()
            pansou_options = PanSouSourceDefinition.get_dynamic_options(
                plugin_cfg,
                {"proxy": getattr(self, "_search_proxy", None) or plugin_cfg.get("search_proxy")},
            )
            result = {"success": True, "data": {"pansou": pansou_options}}
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "subscriptions":
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            target_key = str(
                getattr(self._cloud_drive, "key", "")
                or getattr(self, "_cloud_drive_key", "")
            ).strip().lower()
            target_accepts_cross_transfer = bool(
                self._cloud_drive
                and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            cloud_drives = []
            for provider in providers:
                if not provider.supports(CloudDriveCapability.DIRECTORY_READ):
                    continue
                direct = provider.key == target_key
                cross = bool(
                    not direct
                    and bool(getattr(self, "_cross_transfer_enabled", False))
                    and target_accepts_cross_transfer
                    and provider.supports(CloudDriveCapability.FILE_QUERY)
                    and provider.supports(CloudDriveCapability.FILE_DOWNLOAD)
                )
                if not direct and not cross:
                    continue
                cloud_drives.append({
                    "title": provider.name,
                    "value": provider.key,
                    "mode": "direct" if direct else "cross",
                })
            result = {
                "success": True,
                "data": {
                    "subscribes": UIConfig.get_subscribe_options_grouped(),
                    "transfer_cloud_drives": cloud_drives,
                    "cloud_drives": cloud_drives,
                    "target_cloud_drive": target_key,
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                    "cross_transfer_media_types": sorted(
                        str(value) for value in getattr(
                            self, "_cross_transfer_media_types", set()
                        )
                    ),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope in {"base", "drive"}:
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            if normalized_scope == "base":
                from ..checkin_manager import get_checkin_schemas
                provider_map = {p.key: p for p in providers}
                cloud_drives = []
                for def_cls in DriverRegistry.get_definitions():
                    p = provider_map.get(def_cls.id)
                    if p:
                        caps = sorted(capability.value for capability in p.capabilities)
                        rtypes = sorted(p.resource_types)
                        policy = {
                            "pagination_mode": p.policy.pagination_mode,
                            "max_page_size": p.policy.max_page_size,
                            "supports_batch": p.policy.supports_batch,
                            "max_batch_size": p.policy.max_batch_size,
                            "supports_cancel": p.policy.supports_cancel,
                            "max_concurrency": p.policy.max_concurrency,
                            "cache_ttl_seconds": dict(p.policy.cache_ttl_seconds),
                        }
                    else:
                        caps = [c.value for c in getattr(def_cls, "capabilities", ())]
                        rtypes = list(getattr(def_cls, "resource_types", ()))
                        policy = {}
                    cloud_drives.append({
                        "title": def_cls.name,
                        "value": def_cls.id,
                        "capabilities": caps,
                        "resource_types": rtypes,
                        "policy": policy,
                    })
                result = {
                    "success": True,
                    "data": {
                        **_display_catalog(),
                        "defaults": UIConfig.normalize_config(UIConfig.get_default_config()),
                        "mediaservers": UIConfig.get_media_server_options(),
                        "checkin_schemas": get_checkin_schemas(),
                        "cloud_drives": cloud_drives,
                    },
                }
            else:
                accounts = {}
                for provider in providers:
                    if not provider.supports(CloudDriveCapability.ACCOUNT):
                        continue
                    accounts[provider.key] = self._cached_account_info(
                        f"drive:{provider.key}",
                        {
                            "connected": False,
                            "error": "点击刷新按钮读取账户信息",
                        },
                    )
                result = {
                    "success": True,
                    "data": {
                        "account": accounts.get(self._cloud_drive_key, {
                            "connected": False,
                            "error": "请先配置当前网盘账号",
                        }),
                        "accounts": accounts,
                        "driver_schemas": DriverRegistry.get_driver_schemas(),
                    },
                }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "notify":
            mediaservers = UIConfig.get_media_server_options()
            result = {
                "success": True,
                "data": {
                    "mediaservers": mediaservers,
                    "media_library_webhook_urls": {
                        str(item.get("value") or ""): (
                                "/api/v1/webhook/?"
                                + urlencode({
                            "token": settings.API_TOKEN,
                            "source": str(item.get("value") or ""),
                        })
                        )
                        for item in mediaservers
                        if str(item.get("type") or "").strip().lower() == "emby"
                           and str(item.get("value") or "").strip()
                    },
                    "notification_types": [
                        {"title": item.value, "value": item.name}
                        for item in NotificationType
                    ],
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        search_accounts = {}
        for def_cls in SearchSourceRegistry.get_definitions():
            search_accounts[def_cls.id] = self._cached_account_info(
                f"search:{def_cls.id}",
                {
                    "connected": False,
                    "error": f"配置并保存 {def_cls.name} 账户后读取账户信息",
                },
            )
        from ...search.pansou.definition import PanSouSourceDefinition
        plugin_cfg = self._get_plugin_config()
        pansou_options = PanSouSourceDefinition.get_dynamic_options(
            plugin_cfg,
            {"proxy": getattr(self, "_search_proxy", None) or plugin_cfg.get("search_proxy")},
        )
        available_sources = []
        search_handler = getattr(self, "_search_handler", None)
        if search_handler and hasattr(search_handler, "get_available_sources_meta"):
            available_sources = search_handler.get_available_sources_meta()

        result = {
            "success": True,
            "data": {
                **_display_catalog(),
                "defaults": UIConfig.normalize_config(UIConfig.get_default_config()),
                "search_accounts": search_accounts,
                "pansou": pansou_options,
                "available_sources": available_sources,
                "search_schemas": SearchSourceRegistry.get_search_schemas(),
            },
        }
        _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
        return result


    def api_vue_cloud_directories(
            self, path: str = "/", provider: str = "", refresh: bool = False
    ) -> dict:
        """列出指定或当前网盘目录，供配置页选择转存路径。"""
        normalized_path = str(path or "/").strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(
                CloudDriveCapability.DIRECTORY_READ
        ):
            return {"success": False, "message": "当前网盘不支持目录浏览"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            if refresh:
                service.refresh_directories()
            directories = service.list_directories(normalized_path)
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            current_path = ""
            for part in (item for item in normalized_path.split("/") if item):
                current_path = f"{current_path}/{part}"
                breadcrumbs.append({"name": part, "path": current_path})
            return {
                "success": True,
                "data": {
                    "path": normalized_path,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取网盘目录失败：{normalized_path}，{error}")
            return {"success": False, "message": f"读取网盘目录失败：{error}"}

    def api_vue_create_cloud_directory(self, payload: dict) -> dict:
        """在目录选择器当前目录创建子文件夹。"""
        request = payload or {}
        path = request.get("path", "/")
        name = request.get("name", "")
        provider = request.get("provider", "")
        normalized_path = str(path or "/").strip() or "/"
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        folder_name = str(name or "").strip()
        if (
                not folder_name
                or folder_name in {".", ".."}
                or "/" in folder_name
                or "\\" in folder_name
        ):
            return {"success": False, "message": "文件夹名称无效"}
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(CloudDriveCapability.DIRECTORY_READ):
            return {"success": False, "message": "当前网盘不支持目录操作"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            target_path = f"{normalized_path.rstrip('/')}/{folder_name}" if normalized_path != "/" else f"/{folder_name}"
            lookup = service.resolve_directory(target_path, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return {"success": False, "message": "创建文件夹失败"}
            return {"success": True, "data": {"path": target_path}}
        except Exception as error:
            logger.error(f"创建网盘目录失败：{target_path if 'target_path' in locals() else folder_name}，{error}")
            return {"success": False, "message": f"创建文件夹失败：{error}"}

    def api_vue_local_directories(self, path: str = "/") -> dict:
        """列出本地（宿主机/容器）指定目录下的子目录，供配置页选择本地路径。"""
        import os
        from pathlib import Path

        normalized_path = str(path or "/").strip()
        if not normalized_path:
            normalized_path = "/"
        # 兼容 Windows 根目录或 Unix 根目录
        target = Path(normalized_path)
        if not target.is_absolute():
            target = Path("/").resolve()

        if not target.exists():
            # 尝试向上回溯到存在的父目录
            while not target.exists() and target.parent != target:
                target = target.parent

        target_str = str(target.as_posix()) if hasattr(target, "as_posix") else str(target).replace("\\", "/")
        if not target_str.startswith("/"):
            target_str = f"/{target_str}"

        try:
            directories = []
            if target.is_dir():
                try:
                    with os.scandir(target) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    name = entry.name
                                    if name.startswith("."):
                                        continue
                                    full_p = str(Path(entry.path).as_posix())
                                    if not full_p.startswith("/"):
                                        full_p = f"/{full_p}"
                                    directories.append({
                                        "id": full_p,
                                        "name": name,
                                        "path": full_p,
                                    })
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError) as perm_err:
                    logger.warning(f"扫描本地目录权限受限：{target_str}，{perm_err}")

            directories.sort(key=lambda item: str(item.get("name", "")).lower())

            # 生成面包屑导航
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            curr = ""
            for part in [p for p in target_str.split("/") if p]:
                curr = f"{curr}/{part}"
                breadcrumbs.append({"name": part, "path": curr})

            return {
                "success": True,
                "data": {
                    "path": target_str,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取本地目录失败：{target_str}，{error}")
            return {"success": False, "message": f"读取本地目录失败：{error}"}

    def api_vue_create_local_directory(self, payload: dict) -> dict:
        """在本地当前目录创建子文件夹。"""
        from pathlib import Path

        request = payload or {}
        parent_path = str(request.get("path") or "/").strip()
        name = str(request.get("name") or "").strip()

        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return {"success": False, "message": "文件夹名称无效"}

        parent = Path(parent_path)
        target = parent / name
        try:
            target.mkdir(parents=True, exist_ok=False)
            target_str = str(target.as_posix())
            if not target_str.startswith("/"):
                target_str = f"/{target_str}"
            return {"success": True, "data": {"path": target_str}}
        except FileExistsError:
            return {"success": False, "message": "同名文件夹已存在"}
        except (PermissionError, OSError) as error:
            logger.error(f"创建本地目录失败：{target}，{error}")
            return {"success": False, "message": f"创建本地目录失败：{error}"}


def clear_ui_options_cache() -> int:
    caches = (
        _UI_OPTIONS_CACHE,
    )
    count = sum(len(list(cache.items())) for cache in caches)
    for cache in caches:
        cache.clear()
    return count
