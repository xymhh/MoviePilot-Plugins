"""115 同盘直存后触发 MP 自带整理（P1）。

重构方向：插件不再自己写转存记录，115 分享直存成功后立即调 MP 的
TransferChain.do_transfer（后台、手动整理），文件进 MP 媒体库，记录
落在 MP 的媒体整理历史里。触发失败时返回 (False, info)，info 里带
reason 码与中文 message，调用方按 reason 区分记录失败原因，绝不能
丢记录、绝不能打断同步。

v1.12.6：触发时显式传目标目录——先用不带 src_path 的
DirectoryHelper.get_dir 按媒体类型解析目录条目，再把该条目作为
target_directory / target_storage 传给 do_transfer；否则 MP 会按源
路径前缀匹配到“夸克追更”条目（download_path=/downloads），把文件
整理进 /转存/ 而不是媒体库。

v1.12.7：不再无条件把拼好的最小 MediaInfo 传给 do_transfer。
最小 MediaInfo 没有 category，而 MP 的剧集目录条目开了
library_category_folder（媒体库下有 国漫/、国产剧/ 子目录）且无
固定分类时，MP 的分类校验要求 mediainfo 必须带 category，否则
在动文件前就拒绝（“TMDB 信息未匹配到媒体分类”）。所以只在
MediaInfo 自带非空 category 时才传；没有时不传，让 MP 按文件名
识别（文件名带 {tmdb-<id>} 标记，MP 能认到正确的 id）并自己分类。
目标目录/存储的显式传参不变，/转存/ 落点修复不受影响。

STRM 不受影响：STRM 内容用的是 115 文件的 pickcode（与路径无关），
整理把文件从 /转存 移进媒体库后，旧 STRM 照样能播。
"""

import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.log import logger

from ...core import OwnerDelegator


class MpOrganizeMixin(OwnerDelegator):
    """同盘直存后交 MP 整理的触发器。"""

    def _mp_organize_enabled(self) -> bool:
        """是否允许直存后触发 MP 整理（配置开关，默认开）。"""
        return bool(getattr(self, "_mp_organize_after_share_save", True))

    def _mp_organize_is_video(self, file_name: Any) -> bool:
        """只整理视频文件：字幕等 companion 文件不触发。"""
        extensions = getattr(self, "_video_extensions", None) or [
            ".mkv", ".mp4", ".avi", ".iso",
        ]
        allowed = {str(ext).strip().lower() for ext in extensions if str(ext).strip()}
        return Path(str(file_name or "")).suffix.lower() in allowed

    def _effective_mediainfo(
            self,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """确定一次整理用的有效 MediaInfo。

        mediainfo 非空就直接用它；否则用 media 字典拼一个最小
        MediaInfo（title/tmdb_id/year/season/category 有就带上，
        type 必须能确定）。拼不出 type 时返回 None——没有类型时
        DirectoryHelper.get_dir 会按 priority 0 回落到“夸克追更”
        条目，解析出的目标目录是错的，不如直接不触发。
        """
        if mediainfo is not None:
            return mediainfo
        if not isinstance(media, dict):
            return None
        try:
            from app.core.context import MediaInfo
            from app.schemas.types import MediaType
        except Exception as error:
            logger.debug(
                f"MP 整理触发不可用（宿主未提供 MediaInfo）：{error}",
                exc_info=True,
            )
            return None
        raw_type = media.get("media_type")
        if raw_type is None:
            return None
        if isinstance(raw_type, str):
            text = raw_type.strip().lower()
            if text in ("tv", "电视剧", "电视剧集"):
                mtype = MediaType.TV
            elif text in ("movie", "电影"):
                mtype = MediaType.MOVIE
            else:
                return None
        else:
            mtype = raw_type
        kwargs: Dict[str, Any] = {"type": mtype}
        title = media.get("media_title") or media.get("title")
        if title:
            kwargs["title"] = str(title)
        for key in ("tmdb_id", "year", "season", "category"):
            if media.get(key) not in (None, ""):
                kwargs[key] = media[key]
        try:
            return MediaInfo(**kwargs)
        except Exception as error:
            logger.debug(f"拼最小 MediaInfo 失败：{error}", exc_info=True)
            return None

    def _resolve_mp_target_directory(
            self,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
    ) -> Optional[object]:
        """用不带 src_path 的 get_dir 解析 MP 整理目标目录条目。

        只传 media（不带 src_path/storage/target_storage），让 MP 按
        媒体类型匹配目录条目；带 src_path 时 MP 会按源路径前缀匹配，
        本地 /downloads 的文件会被匹配到“夸克追更”条目，library_path
        是 /转存/，文件就进不了媒体库。解析不到或宿主未提供
        DirectoryHelper 时返回 None。
        """
        effective = self._effective_mediainfo(mediainfo, media)
        if effective is None:
            return None
        try:
            from app.helper.directory import DirectoryHelper
        except Exception:
            try:
                from app.application.directory import DirectoryHelper
            except Exception:
                return None
        if DirectoryHelper is None:
            return None
        try:
            return DirectoryHelper().get_dir(
                media=effective, include_unsorted=False
            )
        except Exception as error:
            logger.debug(f"解析 MP 整理目标目录失败：{error}", exc_info=True)
            return None

    def _library_episode_exists(
            self,
            library_path: str,
            file_name: str,
            season: Any = None,
    ) -> Optional[str]:
        """查目标媒体库目录里是否已有同集文件，有则返回已存在的文件名。

        按 MetaInfo 口径比对季集（不是按文件名精确比对）：库里那份
        可能是带 {tmdb-} 标记的、也可能是不带标记的 mp4/mkv，只要
        季集相同就算重复。查不到、解析不出集号、目录不存在、列目录
        失败时都返回 None（放行整理）：一次网络抖动不能拦住正常入库，
        且目录不存在时该集一定不在库里。
        """
        try:
            from pathlib import Path as _Path
        except Exception:
            return None
        stem = str(_Path(str(file_name or "")).stem or "").strip()
        if not stem:
            return None
        try:
            dedup_meta = getattr(self, "_dedup_meta_info", None)
            if callable(dedup_meta):
                meta = dedup_meta(stem, None)
            else:
                from app.core.metainfo import MetaInfo
                meta = MetaInfo(stem)
        except Exception:
            return None
        episodes = list(getattr(meta, "episode_list", None) or [])
        if not episodes and getattr(meta, "begin_episode", None):
            episodes = [meta.begin_episode]
        if not episodes:
            return None
        try:
            target_episodes = {int(episode) for episode in episodes}
        except (TypeError, ValueError):
            return None
        file_season = getattr(meta, "begin_season", None)
        cloud_directories = getattr(self, "_cloud_directories", None)
        if cloud_directories is None:
            return None
        target_dir = str(library_path or "").strip().rstrip("/") or "/"
        try:
            lookup = cloud_directories.resolve_directory(target_dir)
        except Exception:
            return None
        if not getattr(lookup, "checked", False):
            return None
        if getattr(lookup, "directory_id", None) is None:
            return None
        try:
            listing = cloud_directories.list_directory(lookup.directory_id)
        except Exception:
            return None
        if not getattr(listing, "checked", False):
            return None
        try:
            from ...utils.file_parser import MediaFileParser
        except Exception:
            return None
        for item in getattr(listing, "files", []) or []:
            if getattr(item, "is_directory", False):
                continue
            name = str(getattr(item, "name", "") or "")
            if not MediaFileParser.is_video(name):
                continue
            try:
                if callable(dedup_meta):
                    item_meta = dedup_meta(_Path(name).stem, None)
                else:
                    from app.core.metainfo import MetaInfo
                    item_meta = MetaInfo(_Path(name).stem)
            except Exception:
                continue
            item_season = getattr(item_meta, "begin_season", None)
            if item_season is None:
                item_season = season
            if (
                    file_season is not None and item_season is not None
                    and int(file_season) != int(item_season)
            ):
                continue
            item_episodes = list(
                getattr(item_meta, "episode_list", None) or []
            )
            if not item_episodes and getattr(
                    item_meta, "begin_episode", None
            ):
                item_episodes = [item_meta.begin_episode]
            try:
                if any(
                        int(episode) in target_episodes
                        for episode in item_episodes
                ):
                    return name
            except (TypeError, ValueError):
                continue
        return None

    def trigger_mp_organize(
            self,
            save_dir: str,
            file_name: str,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
            season: Optional[int] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """把已落盘的同盘直存文件交给 MP 整理。

        :param save_dir: 文件所在的云盘目录（如 /Alist/影视/moviepilot/转存）
        :param file_name: 保存后的文件名
        :param mediainfo: 同步已认到的 MediaInfo（自带分类时直传，MP 不用
            重认；没有分类时不传，让 MP 按文件名识别并自己分类）
        :param media: 只有媒体字典时的备用身份（tmdb_id/media_type/season）
        :param season: 季号（media 字典里没有时可单传）
        :return: (True, info) 表示 MP 已接受整理，info 里有触发时传给
            do_transfer 的源路径与解析到的目标目录；(False, info) 表示
            未触发，info 一定带 reason 码（disabled/no_source/not_video/
            unavailable/no_source_file/no_target_dir/not_accepted）与中文
            message，调用方按 reason 区分记录失败原因
        """
        if not self._mp_organize_enabled():
            return False, {
                "reason": "disabled",
                "message": "MP 整理已关闭：文件未进媒体库，下一轮重试",
            }
        save_dir = str(save_dir or "").strip().rstrip("/")
        file_name = str(file_name or "").strip()
        if not save_dir or not file_name:
            return False, {
                "reason": "no_source",
                "message": "MP 整理未触发：源目录或文件名为空，文件未进媒体库",
            }
        if not self._mp_organize_is_video(file_name):
            return False, {
                "reason": "not_video",
                "message": "MP 整理未触发：非视频文件，文件未进媒体库",
            }
        try:
            from app.chain.storage import StorageChain
            from app.chain.transfer import TransferChain
            from app.schemas.file import FileItem
            from app.schemas.types import MediaType
        except Exception as error:
            logger.debug(
                f"MP 整理触发不可用（宿主未提供整理链）：{error}", exc_info=True
            )
            return False, {
                "reason": "unavailable",
                "message": "MP 整理未触发：宿主未提供整理链，文件未进媒体库",
            }

        local_path = f"{save_dir}/{file_name}"
        try:
            fileitem = FileItem(
                storage="local",
                type="file",
                path=local_path,
                name=file_name,
                extension=Path(file_name).suffix.lstrip("."),
            )
            resolved = StorageChain().get_item(fileitem)
            if not resolved:
                logger.debug(
                    f"MP 整理暂不触发：本地挂载未见刚直存的文件：{local_path}"
                )
                return False, {
                    "reason": "no_source_file",
                    "message": "MP 整理未触发：本地挂载未见刚保存的文件，文件未进媒体库",
                }
            effective = self._effective_mediainfo(mediainfo, media)
            conf = self._resolve_mp_target_directory(mediainfo, media)
            if conf is None:
                logger.debug(
                    f"MP 整理暂不触发：未解析到目标目录：{local_path}"
                )
                return False, {
                    "reason": "no_target_dir",
                    "message": "MP 整理未触发：未解析到目标目录，文件未进媒体库，下一轮重试",
                }
            # v1.12.16：入库前先查库里有没有这集。同一集从两个来源各下
            # 一份时，两边都会触发 MP 整理，文件名一不一样（带 {tmdb-}
            # 标记/不带、mp4/mkv）就互相覆盖不了，全进库了（玫瑰战争
            # E24/E25）。查到已存在就跳过，不触发、不记失败、不重试。
            existing = self._library_episode_exists(
                str(getattr(conf, "library_path", "") or ""),
                file_name,
                season if season is not None else (
                    media.get("season") if isinstance(media, dict) else None
                ),
            )
            if existing:
                logger.info(
                    f"该集已在媒体库，跳过 MP 整理：{local_path}，"
                    f"库里已有 {existing}"
                )
                return False, {
                    "reason": "already_in_library",
                    "message": f"该集已在媒体库，跳过整理：库里已有 {existing}",
                    "existing_file": existing,
                }
            mtype = None
            if effective is not None:
                mtype = getattr(effective, "type", None)
            if mtype is None and isinstance(media, dict):
                raw = media.get("media_type")
                if raw is not None and not isinstance(raw, str):
                    mtype = raw
                else:
                    text = str(raw or "").strip().lower()
                    if text in ("tv", "电视剧", "电视剧集"):
                        mtype = MediaType.TV
                    elif text in ("movie", "电影"):
                        mtype = MediaType.MOVIE
            kwargs: Dict[str, Any] = {
                "mtype": mtype,
                "manual": True,
                "background": True,
                "target_directory": conf,
                "target_storage": getattr(conf, "library_storage", None),
            }
            # v1.12.7：只在 MediaInfo 自带非空 category 时才传给 MP。
            # 最小 MediaInfo 没有 category，传了会被 MP 的分类校验直接
            # 拒绝（“TMDB 信息未匹配到媒体分类”）；不传时 MP 按文件名
            # 识别（文件名带 {tmdb-<id>} 标记）并自己分类，校验能过。
            if effective is not None:
                category = str(getattr(effective, "category", "") or "").strip()
                if category:
                    kwargs["mediainfo"] = effective
            if season is None and isinstance(media, dict):
                season = media.get("season")
            if season is not None:
                season_text = str(season).strip()
                if season_text.lstrip("-").isdigit():
                    kwargs["season"] = int(season_text)
            ok, _message = TransferChain().do_transfer(resolved, **kwargs)
            if not ok:
                logger.warning(f"MP 整理未接受：{local_path}，{_message or ''}")
                return False, {
                    "reason": "not_accepted",
                    "message": (
                        "MP 整理未接受：文件未进媒体库，下一轮重试"
                        + (f"：{_message}" if _message else "")
                    ),
                }
            logger.info(f"已交 MP 整理：{local_path}")
            return True, {
                "src_path": local_path,
                "src_storage": "local",
                "triggered_at": time.time(),
                "target_dir": str(getattr(conf, "library_path", "") or ""),
                "target_storage": str(getattr(conf, "library_storage", "") or ""),
                "target_name": str(getattr(conf, "name", "") or ""),
                "file_name": file_name,
            }
        except Exception as error:
            logger.warning(f"触发 MP 整理失败：{local_path}，{error}")
            return False, {
                "reason": "error",
                "message": f"触发 MP 整理失败：{error}，文件未进媒体库",
            }

    def _trigger_mp_organize_for_batch(
            self,
            processed_items: Any,
            success_id_set: Any,
            mediainfo: Any = None,
            season: Optional[int] = None,
    ) -> set:
        """批量直存成功后逐项触发 MP 整理，返回已接手的 file_id 集合。

        只对整理开启且同盘直存的批次调用；跨盘、直链、整理关闭时调用方
        不应走到这里。单项触发失败只记日志，不影响其他项与本批结果。
        """
        accepted = set()
        if not self._mp_organize_enabled():
            return accepted
        for item in processed_items or []:
            file_id = str((item or {}).get("file", {}).get("id") or "")
            if not file_id or file_id not in (success_id_set or set()):
                continue
            target_name = str((item or {}).get("target_name") or "")
            if not target_name:
                continue
            try:
                ok, _info = self.trigger_mp_organize(
                        self._cloud_transfer_path,
                        target_name,
                        mediainfo=mediainfo,
                        season=season,
                )
                # v1.12.16：该集已在库时 trigger 返回 already_in_library
                #（ok=False），同样算“已处理”——文件已在媒体库，不用再
                # 触发整理，也不写平台记录（skip_history 靠 accepted 集）。
                if not ok and isinstance(_info, dict):
                    if _info.get("reason") == "already_in_library":
                        ok = True
                if ok:
                    accepted.add(file_id)
            except Exception as error:
                logger.debug(
                    f"触发 MP 整理异常，已跳过该项：{target_name}，{error}",
                    exc_info=True,
                )
        return accepted
