"""115 同盘直存后触发 MP 自带整理（P1）。

重构方向：插件不再自己写转存记录，115 分享直存成功后立即调 MP 的
TransferChain.do_transfer（后台、手动整理），文件进 MP 媒体库，记录
落在 MP 的媒体整理历史里。触发失败时返回 False，调用方保留插件自己
的历史记录兜底，绝不能丢记录、绝不能打断同步。

STRM 不受影响：STRM 内容用的是 115 文件的 pickcode（与路径无关），
整理把文件从 /转存 移进媒体库后，旧 STRM 照样能播。
"""

from pathlib import Path
from typing import Any, Dict, Optional

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

    def trigger_mp_organize(
            self,
            save_dir: str,
            file_name: str,
            mediainfo: Any = None,
            media: Optional[Dict[str, Any]] = None,
            season: Optional[int] = None,
    ) -> bool:
        """把已落盘的同盘直存文件交给 MP 整理。

        :param save_dir: 文件所在的云盘目录（如 /Alist/影视/moviepilot/转存）
        :param file_name: 保存后的文件名
        :param mediainfo: 同步已认到的 MediaInfo（有就直传，MP 不用重认）
        :param media: 只有媒体字典时的备用身份（tmdb_id/media_type/season）
        :param season: 季号（media 字典里没有时可单传）
        :return: MP 接受整理返回 True；任何一步走不通都返回 False，
            调用方保留插件历史记录，同步流程不受影响
        """
        if not self._mp_organize_enabled():
            return False
        save_dir = str(save_dir or "").strip().rstrip("/")
        file_name = str(file_name or "").strip()
        if not save_dir or not file_name:
            return False
        if not self._mp_organize_is_video(file_name):
            return False
        try:
            from app.chain.storage import StorageChain
            from app.chain.transfer import TransferChain
            from app.schemas.file import FileItem
            from app.schemas.types import MediaType
        except Exception as error:
            logger.debug(
                f"MP 整理触发不可用（宿主未提供整理链）：{error}", exc_info=True
            )
            return False

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
                return False
            mtype = None
            if mediainfo is not None:
                mtype = getattr(mediainfo, "type", None)
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
                "mediainfo": mediainfo,
                "mtype": mtype,
                "manual": True,
                "background": True,
            }
            if season is None and isinstance(media, dict):
                season = media.get("season")
            if season is not None:
                season_text = str(season).strip()
                if season_text.lstrip("-").isdigit():
                    kwargs["season"] = int(season_text)
            ok, _message = TransferChain().do_transfer(resolved, **kwargs)
            if not ok:
                logger.warning(f"MP 整理未接受：{local_path}，{_message or ''}")
                return False
            logger.info(f"已交 MP 整理：{local_path}")
            return True
        except Exception as error:
            logger.warning(f"触发 MP 整理失败：{local_path}，{error}")
            return False

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
                if self.trigger_mp_organize(
                        self._cloud_transfer_path,
                        target_name,
                        mediainfo=mediainfo,
                        season=season,
                ):
                    accepted.add(file_id)
            except Exception as error:
                logger.debug(
                    f"触发 MP 整理异常，已跳过该项：{target_name}，{error}",
                    exc_info=True,
                )
        return accepted
