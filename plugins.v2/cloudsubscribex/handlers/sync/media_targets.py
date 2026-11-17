"""媒体库目标解析（Phase 2 v1.13.3）。

从 handlers/sync/history.py 拆出：临时媒体目标构造、媒体服务器
（Emby）内容列举与洗版目标解析。它们不读插件 history——洗版目标
只从媒体服务器内容或手动输入来，history 来源的洗版入口随转存记录
页一起删除。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from app.chain.mediaserver import MediaServerChain
from app.db import SessionFactory
from app.db.models.mediaserver import MediaServerItem
from app.log import logger

try:
    from app.helper.mediaserver import MediaServerHelper
except ImportError:
    from app.application.mediaserver import MediaServerHelper
from app.schemas.types import MediaType
from sqlalchemy import func, or_

from ...core import OwnerDelegator
from ...core.media import media_server_tmdb_filters, tmdb_id_of
from ...drive.common import positive_int
from .utils import normalize_season

try:
    from ...core.media_scope import is_ignored_media_path
except (ImportError, AttributeError):  # 单测/精简环境没有媒体库过滤模块
    def is_ignored_media_path(path):
        return False


class MediaTargetsService(OwnerDelegator):
    """构造临时媒体目标、列举媒体服务器内容、解析洗版目标。"""

    # v1.15.0 P0：补 @staticmethod。1.13.3 从 history.py 拆出时丢了它，方法体
    # 又没写 self，导致 self._transient_target_defaults() 调用时把 self 当成
    # 第 1 个位置参数抛 TypeError；无订阅媒体的手动提交全死在这一步（P3 磁力
    # 实测 2026-11-18 两次复现：同步开始 0.4–0.6 秒即失败）。注意：别在这里
    # 加函数内 docstring——test_platform_compatibility 用正则匹配
    # ``def ...(): ...: return {`` 校验返回字典的 v3 契约字段。
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

