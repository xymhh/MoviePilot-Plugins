"""规则评分。"""

import hashlib
import json
from pathlib import Path
from typing import Optional

from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType
from app.utils.string import StringUtils

from ...core import OwnerDelegator
from ...core.media import apply_media_identity, tmdb_id_of
from ...drive.common import format_size


class UpgradeRuleScoringService(OwnerDelegator):
    @staticmethod
    def _resource_size_bytes(value: object) -> int:
        """将搜索卡片的大小展示值转换为字节。"""
        return max(0, int(StringUtils.num_filesize(value) or 0))

    def _should_upgrade_candidate(
            self,
            old_score: int,
            new_score: int,
            old_size: int = 0,
            new_size: int = 0,
            has_existing: bool = True,
            old_file_name: str = "",
            subscribe=None,
            mediainfo: Optional[MediaInfo] = None,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> tuple[bool, str]:
        """按评分和洗版模式判断候选是否应进入原有转存流程。"""
        if not has_existing:
            return True, "无现有版本"
        old_file_name = str(old_file_name or "").strip()
        # 「已符合重命名模板」只用于挡掉无收益的重复转存，不能一票否决体积/评分升级：
        # 插件自己转存过的文件必然符合模板，若在此处直接 return，洗版会永远失效。
        template_conformant = bool(
            old_file_name and self._version_matches_rename_template(
                subscribe, mediainfo, old_file_name, season, episode)
        )
        old_score = int(old_score or 0)
        new_score = int(new_score or 0)
        old_size = max(0, int(old_size or 0))
        new_size = max(0, int(new_size or 0))
        if new_score > old_score:
            return True, f"评分提升 {old_score}→{new_score}"
        if new_score < old_score:
            return False, f"评分降低 {old_score}→{new_score}"

        mode = self._upgrade_mode
        if mode == "largest":
            if old_size and new_size and new_size > old_size:
                return True, (
                    f"同评分保留更大文件 {format_size(old_size)}→{format_size(new_size)}"
                )
            if template_conformant:
                return False, f"现有版本已符合当前重命名模板，保留 {old_file_name}"
            return False, "同评分且候选文件未更大"
        if mode == "smallest":
            if old_size and new_size and new_size < old_size:
                return True, (
                    f"同评分保留更小文件 {format_size(old_size)}→{format_size(new_size)}"
                )
            if template_conformant:
                return False, f"现有版本已符合当前重命名模板，保留 {old_file_name}"
            return False, "同评分且候选文件未更小"
        if template_conformant:
            return False, f"现有版本已符合当前重命名模板，保留 {old_file_name}"
        return False, "同评分不执行替换"

    def _version_matches_rename_template(
            self,
            subscribe,
            mediainfo: Optional[MediaInfo],
            file_name: str,
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> bool:
        """现有文件名是否已由当前重命名模板生成（渲染幂等即视为符合）。

        渲染方式与转存落位完全一致（``_platform_rename_path``），用文件名自身的
        元数据回填模板；结果与原名一致说明它已符合「现有重命名规则」，应当保留。
        渲染失败（宿主接口异常、无模板）时返回 False，回退评分判定。
        """
        if not bool(getattr(self, "_prefer_template_named_version", True)):
            return False
        name = str(file_name or "").strip()
        if not name or mediainfo is None:
            return False
        try:
            expected = self._platform_rename_path(
                getattr(self, "_CLOUD_MEDIA_ROOT", "/"),
                subscribe,
                mediainfo,
                name,
                season,
                episode,
            )
        except Exception as error:
            logger.debug(f"重命名模板比对失败，回退评分判定：{name}，{error}")
            return False
        if not expected:
            return False
        expected_stem = Path(str(getattr(expected, "name", "") or "")).stem.strip()
        return bool(expected_stem) and expected_stem == Path(name).stem.strip()

    def _template_conformant_version(
            self,
            versions,
            subscribe,
            mediainfo: Optional[MediaInfo],
            season: Optional[int] = None,
            episode: Optional[int] = None,
    ) -> Optional[dict]:
        """挑出第一个已符合当前重命名模板的现有版本。"""
        for version in versions or []:
            if not isinstance(version, dict):
                continue
            file_name = str(
                version.get("file_name") or version.get("target_file_name") or ""
            ).strip()
            if self._version_matches_rename_template(
                    subscribe, mediainfo, file_name, season, episode):
                return version
        return None

    def _upgrade_replaces_existing(self) -> bool:
        return self._upgrade_mode != "coexist"

    @staticmethod
    def _coexist_target_name(
            target_name: str,
            source_name: str,
            file_size: int = 0,
            source_sha1: str = "",
    ) -> str:
        """共存模式生成稳定的独立目标名，避免与现有版本同名冲突。"""
        target = Path(str(target_name or source_name))
        identity = str(source_sha1 or "").strip().upper() or hashlib.sha1(
            f"{source_name}|{int(file_size or 0)}".encode("utf-8")
        ).hexdigest()
        return f"{target.stem} [共存-{identity[:8]}]{target.suffix}"

    def _get_mp_rule_score(
            self,
            filename: str,
            filesize: int,
            subscribe,
            season: int,
            mediainfo: Optional[MediaInfo] = None,
    ) -> int:
        """返回规则组的 ``pri_order``，未匹配时为 0。"""
        try:
            rule_mediainfo = mediainfo or MediaInfo(
                type=MediaType.TV,
                title=getattr(subscribe, "name", None),
                year=getattr(subscribe, "year", None),
                tmdb_id=tmdb_id_of(subscribe),
            )
            apply_media_identity(
                rule_mediainfo, "themoviedb", tmdb_id_of(subscribe)
            )
            selected, priority = self._search_handler.select_file_candidate(
                [{"name": filename, "size": filesize or 0}],
                rule_mediainfo,
                subscribe,
            )
            return int(priority or 0) if selected else 0
        except Exception as error:
            logger.warning(f"MoviePilot 规则评分失败：{error}")
            return 0

    @staticmethod
    def _read_ep_priority(subscribe) -> dict:
        """读取订阅已保存的逐集 ``pri_order``。"""
        raw = getattr(subscribe, "episode_priority", None) or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                raw = {}
        result = {}
        if not isinstance(raw, dict):
            return result
        for key, value in raw.items():
            try:
                result[key] = int(
                    value.get("score", 0) if isinstance(value, dict) else value or 0
                )
            except (TypeError, ValueError):
                result[key] = 0
        return result
