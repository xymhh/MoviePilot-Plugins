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

        薄适配：唯一实现见 ``handlers/sync/utils.py`` 的
        ``resolve_effective_mediainfo``（Phase 3a 第 6 项）。
        mediainfo 非空就直接用它；否则用 media 字典拼一个最小
        MediaInfo，拼不出 type 时返回 None。
        """
        from .utils import resolve_effective_mediainfo

        return resolve_effective_mediainfo(mediainfo=mediainfo, media=media)

    def _resolve_mp_target_directory(
            self,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
            effective: Any = None,
    ) -> Optional[object]:
        """用不带 src_path 的 get_dir 解析 MP 整理目标目录条目。

        只传 media（不带 src_path/storage/target_storage），让 MP 按
        媒体类型匹配目录条目；带 src_path 时 MP 会按源路径前缀匹配，
        本地 /downloads 的文件会被匹配到“夸克追更”条目，library_path
        是 /转存/，文件就进不了媒体库。解析不到或宿主未提供
        DirectoryHelper 时返回 None。``effective`` 由调用方传入时不
        再重复解析（Phase 3a 第 6 项：trigger 内只调一次）。
        """
        if effective is None:
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

    def _parse_file_season_episodes(
            self, stem: str, season: Any = None
    ) -> Tuple[Optional[int], list]:
        """解析文件名词干的季集（Phase 3a 第 7 项唯一入口）。

        唯一入口 ``parse_season_episode`` 不可导入时（精简测试环境）
        退回 MetaInfo 直解析；生产环境一定走唯一入口。
        """
        try:
            from ...core.episode_parse import parse_season_episode

            return parse_season_episode(stem, season=season)
        except Exception as error:  # noqa: BLE001 - 唯一入口不可用时退回 MetaInfo 直解析
            logger.debug(f"季集解析唯一入口不可用，退回 MetaInfo：{error}", exc_info=True)
        try:
            from app.core.metainfo import MetaInfo

            meta = MetaInfo(stem)
        except Exception:
            return None, []
        episodes = list(getattr(meta, "episode_list", None) or [])
        if not episodes and getattr(meta, "begin_episode", None):
            episodes = [meta.begin_episode]
        return getattr(meta, "begin_season", None), episodes

    def _episode_exists_in_library_via_mp(
            self,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
            season: Any = None,
            file_name: str = "",
            effective: Any = None,
    ) -> Optional[bool]:
        """入库前用 MP 自己的 get_no_exists_info 问媒体库（Emby 口径）
        这集在不在（Phase 3a 第 12 项）。

        返回 True=已在库（跳过整理），False=不在库（继续整理），
        None=查不出来（身份缺失/季集解析不出/宿主异常——fail-open，
        继续整理，绝不拦正常入库）。

        不自己钻媒体库文件夹列目录：MP 的整理去重只看算出的目标路径
        存不存在，剧集文件夹是按重命名格式算出来的，插件手算会随
        格式改动而错位；MP 下载链的 get_no_exists_info 按 tmdb id +
        季问 Emby，是 MP 自己的按集查库口径。给 mediainfo 拼的季集
        列表就是待查的这几集，MP 返回的缺集里没有它们即已在库。
        电影没有季集：MP 说整部存在即已在库。

        ``effective`` 由调用方传入时不再重复解析（Phase 3a 第 6 项：
        一次整理只解析一次有效 MediaInfo）。
        """
        if effective is None:
            effective = self._effective_mediainfo(mediainfo, media)
        if effective is None:
            return None
        try:
            from app.chain.download import DownloadChain
            from app.core.metainfo import MetaInfo
            from app.schemas.types import MediaType
        except Exception as error:
            logger.debug(
                f"MP 查库不可用（宿主未提供下载链）：{error}", exc_info=True
            )
            return None
        tmdb_id = getattr(effective, "tmdb_id", None)
        if tmdb_id in (None, "") and isinstance(media, dict):
            tmdb_id = media.get("tmdb_id")
        if tmdb_id in (None, ""):
            return None
        mtype = getattr(effective, "type", None)
        if mtype is None:
            return None
        try:
            import copy as _copy

            probe_media = _copy.deepcopy(effective)
            probe_media.media_source = (
                getattr(effective, "media_source", None) or "themoviedb"
            )
            probe_media.media_id = (
                getattr(effective, "media_id", None) or str(tmdb_id)
            )
            meta = MetaInfo(getattr(effective, "title", "") or "")
            meta.year = getattr(effective, "year", None)
            meta.type = mtype
            chain = getattr(self, "download_chain", None) or DownloadChain()
            if mtype == MediaType.MOVIE:
                exists, _no_exists = chain.get_no_exists_info(
                    meta=meta, mediainfo=probe_media
                )
                return bool(exists)
            if mtype != MediaType.TV:
                return None
            file_season, episodes = (None, [])
            stem = str(Path(str(file_name or "")).stem or "").strip()
            if stem:
                file_season, episodes = self._parse_file_season_episodes(
                    stem, season=season
                )
            target_season = file_season
            if target_season is None:
                target_season = season
            if target_season is None and isinstance(media, dict):
                target_season = media.get("season")
            if target_season is None:
                target_season = getattr(effective, "season", None)
            if target_season is None or not episodes:
                return None
            try:
                target_season = int(target_season)
                targets = sorted({int(episode) for episode in episodes})
            except (TypeError, ValueError):
                return None
            if not targets:
                return None
            probe_media.season = target_season
            probe_media.seasons = {target_season: list(targets)}
            meta.begin_season = target_season
            exists, no_exists = chain.get_no_exists_info(
                meta=meta, mediainfo=probe_media
            )
        except Exception as error:
            logger.debug(f"MP 查库失败（放行整理）：{error}", exc_info=True)
            return None
        if exists:
            return True
        # exists=False：按 MP 的 _append_no_exists 语义读 no_exists——
        # 缺集条目 episodes=[] 表示整季缺失（不是“没有缺集”），目标
        # 集全部算缺失；没有目标季的条目时该季齐全；no_exists 为空
        # 则是 MP 没能查出来（识别失败等），fail-open，不拦整理。
        if not no_exists:
            return None
        missing: set = set()
        season_seen = False
        for season_map in (no_exists or {}).values():
            for key, info in (season_map or {}).items():
                try:
                    if int(key) != target_season:
                        continue
                except (TypeError, ValueError):
                    continue
                season_seen = True
                absent = getattr(info, "episodes", None) or []
                if not absent:
                    # 整季缺失：待查的这几集全都不在库里。
                    missing.update(targets)
                    continue
                try:
                    missing.update(int(episode) for episode in absent)
                except (TypeError, ValueError):
                    continue
        if not season_seen:
            return True
        return not any(episode in missing for episode in targets)

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
            unavailable/no_source_file/no_target_dir/already_in_library/
            not_accepted/error）与中文 message，调用方按 reason 区分记录
            失败原因。其中 already_in_library 不是失败（该集已在库，调用
            方当成功处理）；error 是 do_transfer 抛出的异常原文。
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
            # Phase 3a 第 6 项：一次整理只解析一次有效 MediaInfo，
            # 目标目录解析复用同一份，不再重复拼最小 MediaInfo。
            effective = self._effective_mediainfo(mediainfo, media)
            conf = self._resolve_mp_target_directory(
                mediainfo, media, effective=effective
            )
            if conf is None:
                logger.debug(
                    f"MP 整理暂不触发：未解析到目标目录：{local_path}"
                )
                return False, {
                    "reason": "no_target_dir",
                    "message": "MP 整理未触发：未解析到目标目录，文件未进媒体库，下一轮重试",
                }
            # Phase 3a 第 12 项：入库前用 MP 自己的 get_no_exists_info
            # （Emby 口径）问这集在不在。旧 _library_episode_exists
            # 只列媒体库根目录一层，子文件夹里的文件看不见，同一集
            # 两份照样进库（玫瑰战争 E24/E25、多喜一家人 E05/E06）；
            # MP 的整理去重只看算出的目标路径，剧集文件夹是按重命名
            # 格式算出来的，插件不自己钻文件夹。查不出来（None）就
            # 放行整理，绝不拦正常入库。
            exists = self._episode_exists_in_library_via_mp(
                mediainfo, media,
                season=season if season is not None else (
                    media.get("season") if isinstance(media, dict) else None
                ),
                file_name=file_name,
                effective=effective,
            )
            if exists is True:
                logger.info(f"该集已在媒体库，跳过 MP 整理：{local_path}")
                return False, {
                    "reason": "already_in_library",
                    "message": "该集已在媒体库，跳过整理",
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
