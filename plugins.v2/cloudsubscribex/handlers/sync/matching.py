"""网盘分享文件的电影/剧集匹配。"""

import re
from pathlib import Path, PurePosixPath
from typing import Dict, List

from app.log import logger
from app.schemas import MediaInfo

from ...core import OwnerDelegator
from ...search.matching import resource_title_matches
from ...utils import FileMatcher


class FileMatchingService(OwnerDelegator):
    """只负责从已读取的文件树中选择媒体文件。"""

    def _match_episode_files(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            season: int,
            episodes: List[int],
            require_media_match: bool = True,
    ) -> Dict[int, tuple]:
        """按结构收集剧集候选，再使用规则组选择文件。"""
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        candidates = FileMatcher.episode_candidates(
            files,
            season,
            episode_list,
            mediainfo=mediainfo if require_media_match else None,
        )
        results = {}
        for episode in episode_list:
            episode_candidates = candidates.get(episode) or []
            selected, priority = self._search_handler.select_file_candidate(
                episode_candidates, mediainfo, subscribe
            )
            results[episode] = selected, priority
            if selected and len(episode_candidates) > 1:
                selected_path = str(
                    selected.get("_relative_path")
                    or selected.get("name")
                    or ""
                ).strip()
                logger.debug(
                    f"{mediainfo.title_year} S{season:02d}E{episode:02d} "
                    f"存在 {len(episode_candidates)} 个版本，平台规则选择："
                    f"{selected_path}（优先级 {priority}）"
                )
        return results

    def _match_movie_file(
            self,
            files: list,
            mediainfo: MediaInfo,
            subscribe,
            resource_title: str = "",
            require_media_match: bool = True,
            explicit_selection: bool = False,
    ) -> tuple:
        """按媒体文件结构收集电影候选，再使用规则组选择。"""
        matched = self._search_handler.select_file_candidate(
            FileMatcher.movie_candidates(
                files,
                mediainfo=mediainfo if require_media_match else None,
            ),
            mediainfo,
            subscribe,
        )
        if matched[0] or not require_media_match:
            return matched if matched[0] else (None, 0)

        # 用户在预览面板显式勾选过文件：候选已被收敛为该选择，直接按体积取最大视频。
        fallback = FileMatcher.movie_candidates(files)
        if not fallback:
            return None, 0

        if explicit_selection:
            actual = max(fallback, key=lambda item: int(item.get("size") or 0))
            scoring_item = dict(actual)
            scoring_item["name"] = (
                f"{str(resource_title).strip()}{Path(str(actual.get('name') or '')).suffix}"
            )
            _, score = self._search_handler.select_file_candidate(
                [scoring_item], mediainfo, subscribe
            )
            logger.info(
                f"按用户显式选择转存电影文件：{actual.get('name')}（评分 {score}）"
            )
            return actual, score

        # 跨盘文件名被网盘混淆时的多维兜底：
        # 1. 标题匹配证据：支持 resource_title 或 视频父目录名（支持去括号年份、连字符与书名号）
        expected_titles = [
            getattr(mediainfo, key, None)
            for key in ("title", "en_title", "original_title", "original_name")
        ]
        names = getattr(mediainfo, "names", None)
        if isinstance(names, (list, tuple, set)):
            expected_titles.extend(names)
        expected_titles = [str(t).strip() for t in expected_titles if str(t or "").strip()]
        expected_year = getattr(mediainfo, "year", None)

        def _title_evidence_matches(evidence_text: str) -> bool:
            if not evidence_text:
                return False
            if FileMatcher.media_name_matches(evidence_text, mediainfo):
                return True
            if resource_title_matches(evidence_text, expected_titles, expected_year=expected_year, strict_year=False):
                return True
            # 去除连字符/书名号/特殊标点（如《你-能-看-到-一-切》）后重试
            cleaned_spaced = re.sub(r"[\-——《》【】\[\]（）\(\)·#_]", " ", evidence_text)
            cleaned_spaced = re.sub(r"\s+", " ", cleaned_spaced).strip()
            if resource_title_matches(cleaned_spaced, expected_titles, expected_year=expected_year, strict_year=False):
                return True
            # 中文字串包含兜底：去标点去空格后直接子串匹配（解决紧邻类型词如“2026犯罪《你-能-看-到-一-切》”）
            cleaned_dense = re.sub(r"[\-——《》【】\[\]（）\(\)·#_\s]", "", evidence_text).casefold()
            for exp in expected_titles:
                exp_clean = re.sub(r"[\-——《》【】\[\]（）\(\)·#_\s]", "", str(exp)).casefold()
                if len(exp_clean) >= 2 and exp_clean in cleaned_dense:
                    return True
            return False

        has_title_match = _title_evidence_matches(resource_title)
        if not has_title_match:
            for item in fallback:
                rel = str(item.get("_relative_path") or item.get("relative_path") or item.get("parent_path") or "")
                parts = PurePosixPath(rel).parts
                for dir_part in parts[:-1]:
                    if _title_evidence_matches(dir_part):
                        has_title_match = True
                        break
                if has_title_match:
                    break

        if not has_title_match:
            return None, 0

        # 2. 视频候选选择：单视频直接采用；多视频若最大视频占主导地位（体积主导且非广告），采用最大视频
        if len(fallback) == 1:
            actual = fallback[0]
        else:
            sorted_by_size = sorted(fallback, key=lambda item: int(item.get("size") or 0), reverse=True)
            biggest = sorted_by_size[0]
            second = sorted_by_size[1] if len(sorted_by_size) > 1 else None
            big_size = int(biggest.get("size") or 0)
            sec_size = int(second.get("size") or 0) if second else 0
            if big_size >= 300 * 1024 * 1024 and (sec_size == 0 or big_size >= sec_size * 1.5):
                actual = biggest
            else:
                return None, 0

        scoring_item = dict(actual)
        scoring_item["name"] = (
            f"{str(resource_title).strip()}{Path(str(actual.get('name') or '')).suffix}"
        )
        selected, score = self._search_handler.select_file_candidate(
            [scoring_item], mediainfo, subscribe
        )
        return (actual, score) if selected else (None, 0)
