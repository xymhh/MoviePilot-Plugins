"""按媒体结构匹配实际文件候选。"""

import re
from typing import Any, Dict, List

from app.core.metainfo import MetaInfo
from app.log import logger

from .file_parser import MediaFileParser
from ..search.matching import normalize_title, title_matches


class FileMatcher:
    """电影或剧集候选"""

    _TITLE_NOISE_RE = re.compile(r"[丨｜|¦]+")
    _COMPACT_YEAR_RE = re.compile(r"(?<!\s)(\((?:19|20)\d{2}\))")
    _TITLELESS_SEASON_EPISODE = re.compile(
        r"^[Ss]0*(\d{1,3})[ ._~-]*[Ee](?:[Pp])?0*(\d{1,4})(?=[ ._~-]|$)",
        re.IGNORECASE,
    )
    _TITLELESS_EPISODE = re.compile(
        r"^(?:第\s*)?(?:[Ee](?:[Pp])?)?0*(\d{1,4})(?:\s*集)?(?=[ ._~-]|$)",
        re.IGNORECASE,
    )
    # 跨季路径守卫：只看文件所在的分享目录/相对路径（文件名自身的季号仍走
    # contains_other_season 原逻辑）。开关由插件配置 cross_season_path_guard 写入。
    path_season_guard_enabled: bool = True
    # 期集换算规则：{节目名: 每期集数}，由插件配置 period_episode_rules 写入。
    # 留空（默认）时期号就是集号，行为与历史版本完全一致。
    period_episode_rules: Dict[str, int] = {}
    # 「第N期」中文期号标记
    _PERIOD_MARK = re.compile(r"第\s*(\d{1,4})\s*期")
    # 文件名里已带明确季集编号时不参与期号换算
    _EXPLICIT_EPISODE_MARK = re.compile(
        r"[Ss]\d{1,2}[ ._~-]*[Ee]\d{1,4}|(?<![A-Za-z])[Ee][Pp]?\d{1,4}(?![0-9])",
    )
    _PATH_SEASON_DIR_FIELDS = ("_relative_path", "relative_path")
    _PATH_SEASON_DIR_AS_IS_FIELDS = ("parent_path", "cloud_path")

    @classmethod
    def period_episode_override(cls, name: str, mediainfo: Any = None) -> int | None:
        """按「每期 N 集」把中文期号换算成集号。

        综艺文件名常只写「第3期上」：平台解析会得到 E03，但实际按上下半场
        各算一集时它对应 E05（第1期上=E01、第1期下=E02、第2期上=E03…）。
        规则由插件配置 ``period_episode_rules`` 提供，配置为空时完全不介入；
        文件名里已有明确 SxxExx 的以明确标记为准。
        """
        rules = cls.period_episode_rules or {}
        if not rules or not name:
            return None
        if cls._EXPLICIT_EPISODE_MARK.search(name):
            return None
        size = cls._period_rule_size(rules, mediainfo)
        if size <= 0:
            return None
        mark = cls._PERIOD_MARK.search(name)
        if not mark:
            return None
        try:
            period = int(mark.group(1))
        except (TypeError, ValueError):
            return None
        if period <= 0:
            return None
        offset = cls._period_segment_offset(name, mark.end(), size)
        episode = (period - 1) * size + offset + 1
        return episode if episode > 0 else None

    @classmethod
    def _period_segment_offset(cls, name: str, mark_end: int, size: int) -> int:
        """期号后紧跟的半场标记换算成 0 基偏移：上/无=0，中=1，下=size-1。"""
        tail = str(name or "")[mark_end:mark_end + 6]
        if "下" in tail:
            return max(0, size - 1)
        if "中" in tail:
            return min(1, max(0, size - 1))
        return 0

    @classmethod
    def _period_rule_size(cls, rules: Dict[str, int], mediainfo: Any = None) -> int:
        """按当前媒体标题查每期集数；未命中返回 0（不换算）。"""
        if mediainfo is None:
            return 0
        titles = []
        for key in ("title", "en_title", "original_title", "original_name"):
            value = getattr(mediainfo, key, None)
            if value:
                titles.append(str(value))
        names = getattr(mediainfo, "names", None)
        if names is None and isinstance(mediainfo, dict):
            names = mediainfo.get("names")
        if isinstance(names, (list, tuple, set)):
            titles.extend(str(value) for value in names if value)
        if not titles:
            return 0
        normalized: Dict[str, int] = {}
        for rule_title, raw_size in (rules or {}).items():
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                continue
            key = cls._normalize_period_rule_title(rule_title)
            if key and size > 0:
                normalized[key] = size
        if not normalized:
            return 0
        for title in titles:
            key = cls._normalize_period_rule_title(title)
            if not key:
                continue
            if key in normalized:
                return normalized[key]
        for title in titles:
            key = cls._normalize_period_rule_title(title)
            if not key:
                continue
            for rule_title, size in normalized.items():
                if rule_title in key or key in rule_title:
                    return size
        return 0

    @staticmethod
    def _normalize_period_rule_title(value: Any) -> str:
        """去掉季号、年份、标点与空白，只留可比对的节目标题。"""
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = re.sub(r"[\(（\[【].*?[\)）\]】]", "", text)
        text = re.sub(r"(?:第\s*[0-9一二三四五六七八九十]+\s*[季部])|(?:s\d{1,2}\b)", "", text)
        text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)
        return text

    @staticmethod
    def _matches_target_media(meta: Any, mediainfo: Any, season: int | None = None) -> bool:
        if mediainfo is None:
            return True
        expected_titles = [
            getattr(mediainfo, key, None)
            for key in ("title", "en_title", "original_title", "original_name")
        ]
        names = getattr(mediainfo, "names", None)
        if names is None and isinstance(mediainfo, dict):
            names = mediainfo.get("names")
        if isinstance(names, (list, tuple, set)):
            expected_titles.extend(names)
        expected_titles = [t for t in expected_titles if t]

        candidate_titles = [
            getattr(meta, key, None)
            for key in ("cn_name", "en_name", "original_name")
        ]
        if not any(
                title_matches(candidate, expected_titles)
                for candidate in candidate_titles
                if candidate
        ):
            return False

        expected_years = {
            str(value).strip()
            for value in (
                getattr(mediainfo, "year", None),
                (
                    (getattr(mediainfo, "season_years", {}) or {}).get(season)
                    if season is not None else None
                ),
            )
            if str(value or "").strip()
        }
        candidate_year = str(getattr(meta, "year", None) or "").strip()
        if candidate_year and expected_years and candidate_year not in expected_years:
            return False
        candidate_type = getattr(meta, "type", None)
        expected_type = getattr(mediainfo, "type", None)
        if candidate_type and expected_type:
            c_val = str(getattr(candidate_type, "value", candidate_type) or "").strip().lower()
            e_val = str(getattr(expected_type, "value", expected_type) or "").strip().lower()
            type_map = {
                "tv": "tv", "电视剧": "tv", "series": "tv", "teleplay": "tv",
                "movie": "movie", "电影": "movie",
            }
            norm_c = type_map.get(c_val, c_val)
            norm_e = type_map.get(e_val, e_val)
            if (
                    norm_c and norm_e
                    and norm_c not in {"未知", "unknown"}
                    and norm_e not in {"未知", "unknown"}
                    and norm_c != norm_e
            ):
                return False
        return True

    @staticmethod
    def _platform_meta(name: str) -> Any:
        try:
            # 部分分享用竖线类字符拆散中文片名，先去除这类装饰噪声再交给平台识别。
            normalized = FileMatcher._TITLE_NOISE_RE.sub("", name)
            normalized = FileMatcher._COMPACT_YEAR_RE.sub(r" \1", normalized)
            return MetaInfo(normalized)
        except Exception as error:
            logger.debug(f"平台媒体解析失败，跳过文件：{name} - {error}")
            return None

    @staticmethod
    def _has_explicit_media_title(meta: Any) -> bool:
        """数字、E01、S01E01 等只有季集标记的名称不算作品标题。"""
        for key in ("cn_name", "en_name", "original_name"):
            normalized = normalize_title(getattr(meta, key, None))
            if not normalized:
                continue
            residue = re.sub(r"(?i)(?:s\d{1,3})?(?:e|ep)?\d{1,4}", "", normalized)
            if residue:
                return True
        return False

    @classmethod
    def _titleless_episode_number(
            cls, name: str, meta: Any, season: int
    ) -> int | None:
        """仅为无作品名前缀的文件补全当前媒体和季号。"""
        if cls._has_explicit_media_title(meta):
            return None
        parsed_season = getattr(meta, "begin_season", None)
        parsed_episode = getattr(meta, "begin_episode", None)
        if parsed_episode is not None:
            try:
                if parsed_season is None or int(parsed_season) == int(season):
                    return int(parsed_episode)
            except (TypeError, ValueError):
                return None
        season_episode = cls._TITLELESS_SEASON_EPISODE.match(name)
        if season_episode:
            if int(season_episode.group(1)) == int(season):
                return int(season_episode.group(2))
            return None
        episode = cls._TITLELESS_EPISODE.match(name)
        return int(episode.group(1)) if episode else None

    @classmethod
    def _meta_with_target_defaults(
            cls, name: str, meta: Any, mediainfo: Any, season: int
    ) -> tuple[Any, int | None]:
        episode = cls._titleless_episode_number(name, meta, season)
        if episode is None or episode <= 0:
            return meta, episode
        if mediainfo is None:
            return cls._platform_meta(f"S{int(season):02d}E{episode:02d}"), episode
        title = str(
            getattr(mediainfo, "title", None)
            or getattr(mediainfo, "en_title", None)
            or ""
        ).strip()
        if not title:
            return meta, None
        season_years = getattr(mediainfo, "season_years", {}) or {}
        year = str(
            season_years.get(season) or getattr(mediainfo, "year", None) or ""
        ).strip()
        synthetic = f"{title} {year} S{int(season):02d}E{episode:02d}".strip()
        return cls._platform_meta(synthetic), episode

    @classmethod
    def media_name_matches(cls, name: str, mediainfo: Any) -> bool:
        """确认文本经识别后属于目标媒体。"""
        normalized = str(name or "").strip()
        if not normalized:
            return False
        meta = cls._platform_meta(normalized)
        return bool(meta and cls._matches_target_media(meta, mediainfo))

    # 分享根目录常见泛称：这些名字不能用来判断资源属于哪部剧。
    _GENERIC_SHARE_ROOTS = frozenset({
        "国剧", "国产剧", "电视剧", "剧集", "合集", "全集", "更新", "最新",
        "电影", "动漫", "纪录片", "综艺", "未分类", "新建文件夹", "下载",
        "网盘", "资源", "国语", "中字", "双语", "无删减", "完整版",
        "4k", "1080p", "2160p", "720p", "hdr", "hdr10", "dv", "webdl",
        "web-dl", "bluray", "remux", "hevc", "h265", "h264", "avc",
        "aac", "dts",
    })
    _SHARE_ROOT_TAG_RE = re.compile(
        r"(?i)(?:[\[\(（【]?\s*)?"
        r"(?:4k|2160p|1080p|720p|web[- .]?dl|hdr10?|dv|hevc|h265|h264|avc|aac|dts|remux|bluray)"
        r"(?![A-Za-z0-9]).*$"
    )
    _SHARE_ROOT_UPDATE_RE = re.compile(
        r"(?:更新至|全|共)\s*\d+\s*集.*$"
    )
    # 只有季集/集数范围、没有作品名的根目录，不能拿来判断“这是另一部剧”。
    _EPISODE_ONLY_ROOT_RE = re.compile(
        r"(?i)^(?:第\s*)?(?:s\d{1,3})?(?:e|ep)?\d{1,4}"
        r"(?:\s*[-~—至]\s*(?:s\d{1,3})?(?:e|ep)?\d{1,4})?\s*(?:集|话)?$"
    )
    # 组名/代号 + 集数（SGZBRMY-07.mkv）：同样没有作品名，不能当剧名证据。
    _CODE_PREFIX_EPISODE_RE = re.compile(
        r"(?i)^[a-z0-9]{2,20}[-_. ]?(?:s\d{1,3})?(?:e|ep)?\d{1,4}$"
    )
    # 非视频文件（广告/说明/字幕）常和影片摊在同一层，
    # 线上实测「基德盘搜安卓app.apk」「汁源分享.jpg」把整包 4K 源判成另一部剧。
    _NON_VIDEO_ROOT_SUFFIXES = frozenset({
        "apk", "ipa", "exe", "msi", "dmg", "bat", "sh",
        "jpg", "jpeg", "png", "gif", "bmp", "webp", "ico",
        "txt", "md", "html", "htm", "url", "nfo", "pdf",
        "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "zip", "rar", "7z", "tar", "gz", "torrent",
        "srt", "ass", "ssa", "vtt", "sub", "sup", "idx", "smi",
    })
    # 反爬/变体写法常用的装饰符号（4K 源几乎都用：S【上@供#住】、S）尚丨G丨玊（2026））。
    _ROOT_DECORATION_RE = re.compile(r"[@#丨｜|¦*·•~＊＃＠]")
    # 软放行只看「包内有没有本季的集数」：用纯文件名解析，不依赖媒体识别，
    # 避免把守卫和元数据匹配耦在一起（守卫是便宜的前置检查）。
    _SXEX_EPISODE_RE = re.compile(
        r"[Ss](\d{1,3})\s*[._\- ]?\s*[Ee](?:[Pp])?(\d{1,4})"
    )
    _CN_EPISODE_RE = re.compile(r"第\s*(\d{1,4})\s*[集话]")

    @classmethod
    def share_root_names(cls, files: Any) -> set[str]:
        """提取分享文件所属的顶层目录名（无目录时取文件名）。"""
        roots: set[str] = set()
        for item in files or []:
            if not isinstance(item, dict):
                continue
            relative = ""
            for key in ("_relative_path", "relative_path"):
                relative = str(item.get(key) or "").strip().strip("/")
                if relative:
                    break
            if relative:
                root = relative.split("/")[0].strip()
            else:
                root = str(item.get("name") or "").strip()
            if root:
                roots.add(root)
        return roots

    @classmethod
    def _share_files_titleless(cls, files: Any) -> bool:
        """分享内是否没有任何文件名携带作品标题（只有 14.mp4 / S01E14 这类）。"""
        saw_video = False
        for item in files or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name or not MediaFileParser.is_video(name):
                continue
            saw_video = True
            stem = name.rsplit("/", 1)[-1]
            stem = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", stem)
            cleaned = cls._SHARE_ROOT_TAG_RE.sub("", stem)
            cleaned = re.sub(
                r"(?i)(?:s\d{1,3})?(?:e|ep)?\d{1,4}", " ", cleaned
            )
            cleaned = re.sub(r"第\s*\d+\s*[集话]?", " ", cleaned)
            fingerprint = normalize_title(cleaned)
            if any(ch.isalpha() or "\u4e00" <= ch <= "\u9fff"
                   for ch in fingerprint):
                return False
        return saw_video

    @classmethod
    def _is_generic_share_root(cls, name: str) -> bool:
        """判断分享根目录是否只是泛称/格式词，无法据此识别剧名。

        分享把文件直接摊在根目录时，``share_root_names`` 取到的是文件名
        （``01-4K.mp4`` / ``S01E01.mkv``）。这类纯集数文件名必须先剥扩展名
        与尾部标点，再判定为泛称，否则会被当成「另一部剧的剧名」整批拦下
        （线上实测：尚公主 E01-E04 的 4 个候选全部因此被跳过）。
        """
        text = str(name or "").strip()
        # 无目录时这里是文件名：先去掉扩展名，避免 "01-4K.mp4" 判不出集数。
        text = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", text)
        if not text:
            return True
        normalized = normalize_title(text)
        if not normalized:
            return True
        if normalized in cls._GENERIC_SHARE_ROOTS:
            return True
        cleaned = cls._SHARE_ROOT_TAG_RE.sub("", text)
        cleaned = cls._SHARE_ROOT_UPDATE_RE.sub("", cleaned).strip()
        cleaned = cleaned.strip(" -_.·[]()（）【】")
        if cls._EPISODE_ONLY_ROOT_RE.match(cleaned):
            return True
        if cls._CODE_PREFIX_EPISODE_RE.match(cleaned):
            return True
        normalized_clean = normalize_title(cleaned)
        return not normalized_clean or normalized_clean in cls._GENERIC_SHARE_ROOTS

    @staticmethod
    def _title_similar_enough(
            candidate: str, titles: Any, threshold: float = 0.75
    ) -> bool:
        """去装饰后仍有足够相似度时放行，兜住「标题被大量 emoji/标签包住」的误杀。

        只在上面的精确包含判断全部失败后作为最后一道放行条件：
        normal 化后长度不足 3 的候选不参与，避免短词误命中；
        相似度阈值 0.75 意味着 4 字中文至少 3 字一致。
        """
        from difflib import SequenceMatcher

        left = normalize_title(candidate)
        if len(left) < 3:
            return False
        for title in titles or ():
            right = normalize_title(title or "")
            if len(right) < 2:
                continue
            if SequenceMatcher(None, left, right).ratio() >= threshold:
                return True
        return False

    @classmethod
    def share_title_mismatch(
            cls,
            files: Any,
            mediainfo: Any,
            resource_title: str = "",
            season: Any = None,
    ) -> bool:
        """分享顶层目录明显是另一部剧时返回 True。

        判定改为「正向证据」：只有根目录能证明是**另一部剧**才拦，不再因为
        「名字里没有本剧标题」就拦。非视频文件（apk/jpg/字幕）与纯集数、
        代号前缀文件名一律不算标题证据。

        ``season`` 传入时补一条软放行：包内视频文件名能解析出目标季的集数、
        且根目录与目标标题有汉字交集或带反爬装饰符（``S【上@供#住】``、
        ``S）尚丨G丨玊（2026）``、``S 尙伀主``）→ 放行，避免 4K 源被整包误杀。
        分享内同时包含多部剧时，只要有一个根目录匹配就放行，后续按文件匹配。
        """
        if mediainfo is None:
            return False
        roots = [
            root for root in cls.share_root_names(files)
            if cls._is_title_evidence_root(root)
        ]
        if not roots:
            return False

        # 夸克等分享常用「【玉簟秋】更新至第15集」「玉簟秋(45)」这类
        # 带后缀的目录名，而文件只有 14.mp4。文件名没有作品名时，
        # 只按“根目录包含目标标题/标题包含根目录”放行；明确不相干的
        # 根目录（如「私.行」「玉-癫-囚」）仍然拦下。
        expected_title = normalize_title(resource_title)
        target_title = normalize_title(getattr(mediainfo, "title", "") or "")
        if cls._share_files_titleless(files):
            for root in roots:
                normalized = normalize_title(root)
                for expected in (target_title, expected_title):
                    if expected and (
                            expected in normalized or normalized in expected
                    ):
                        return False
        elif any(
                cls._root_matches_title(
                    root, mediainfo, target_title, expected_title
                )
                for root in roots
        ):
            return False

        # 软放行：根目录只是反爬/变体写法，内容却明确是本季的集数。
        if season is not None and cls._episode_evidence_passes(
                files,
                season,
                roots,
                (getattr(mediainfo, "title", "") or "", resource_title),
        ):
            return False
        return True

    @classmethod
    def mismatch_evidence_roots(
            cls,
            files: Any,
            mediainfo: Any,
            resource_title: str = "",
    ) -> List[str]:
        """返回真正导致拦截的根目录（排障用，别再打印前 3 个泛称名）。

        与 :meth:`share_title_mismatch` 的「根目录与标题是否匹配」判定保持一致，
        便于日志/账本直接写出凶手。
        """
        if mediainfo is None:
            return []
        expected_title = normalize_title(resource_title)
        target_title = normalize_title(getattr(mediainfo, "title", "") or "")
        roots = [
            root for root in cls.share_root_names(files)
            if cls._is_title_evidence_root(root)
        ]
        offenders = []
        for root in roots:
            normalized = normalize_title(root)
            if any(
                    expected and (expected in normalized or normalized in expected)
                    for expected in (target_title, expected_title)
            ):
                continue
            if cls._root_matches_title(
                    root, mediainfo, target_title, expected_title
            ):
                continue
            offenders.append(root)
        return sorted(offenders)

    @classmethod
    def _root_matches_title(
            cls,
            root: str,
            mediainfo: Any,
            target_title: str,
            expected_title: str,
    ) -> bool:
        """单个根目录是否足以证明「这就是本剧」。"""
        cleaned = cls._SHARE_ROOT_TAG_RE.sub("", root)
        cleaned = cls._SHARE_ROOT_UPDATE_RE.sub("", cleaned)
        for candidate in (root, cleaned):
            if cls.media_name_matches(candidate, mediainfo):
                return True
            normalized = normalize_title(candidate)
            if expected_title and (
                    normalized == expected_title
                    or expected_title in normalized
                    or normalized in expected_title
            ):
                return True
        # 最后一道容错：装饰过多导致精确包含失败时，按相似度放行。
        return cls._title_similar_enough(
            cleaned or root, (target_title, expected_title)
        )

    @classmethod
    def _is_title_evidence_root(cls, name: str) -> bool:
        """该根目录能否当「剧名证据」用：非视频文件与泛称都不算。"""
        text = str(name or "").strip()
        if not text:
            return False
        suffix = text.rsplit(".", 1)[-1].lower() if "." in text else ""
        if suffix in cls._NON_VIDEO_ROOT_SUFFIXES:
            return False
        return not cls._is_generic_share_root(text)

    @classmethod
    def _episode_evidence_passes(
            cls,
            files: Any,
            season: Any,
            roots: List[str],
            titles: Any,
    ) -> bool:
        """包内集数能对上目标季 + 根目录只是变体写法 → 视为本剧。

        两个条件必须同时成立，避免把「另一部剧 + 纯集数文件名」也放进来：
        根目录要么带反爬装饰符，要么与目标标题含相同汉字。
        """
        try:
            target_season = int(season)
        except (TypeError, ValueError):
            return False
        if target_season <= 0:
            return False
        if not any(cls._root_is_variant_name(root, titles) for root in roots):
            return False
        return cls._share_has_season_episode(files, target_season)

    @classmethod
    def _root_is_variant_name(cls, root: str, titles: Any) -> bool:
        """根目录是否像「本剧标题的反爬/异体写法」。"""
        if cls._ROOT_DECORATION_RE.search(str(root or "")):
            return True
        root_chars = {
            ch for ch in str(root or "") if "\u4e00" <= ch <= "\u9fff"
        }
        if not root_chars:
            return False
        for title in titles or ():
            title_chars = {
                ch for ch in str(title or "") if "\u4e00" <= ch <= "\u9fff"
            }
            if title_chars and (title_chars & root_chars):
                return True
        return False

    @classmethod
    def _share_has_season_episode(cls, files: Any, season: int) -> bool:
        """分享内是否有视频文件名能解析成目标季的某一集（纯文件名解析）。"""

        def walk(items: Any) -> bool:
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                if item.get("is_dir"):
                    if walk(item.get("children") or []):
                        return True
                    continue
                name = str(item.get("name") or "")
                if not MediaFileParser.is_video(name):
                    continue
                stem = name.rsplit("/", 1)[-1]
                match = cls._SXEX_EPISODE_RE.search(stem)
                if match:
                    if (
                        int(match.group(1)) == int(season)
                        and int(match.group(2)) > 0
                    ):
                        return True
                    continue
                if cls._CN_EPISODE_RE.search(stem) or cls._EPISODE_ONLY_ROOT_RE.match(
                        re.sub(r"\.[A-Za-z0-9]{2,5}$", "", stem)
                ):
                    return True
            return False

        return walk(files)

    @classmethod
    def media_matches(cls, item: Any, mediainfo: Any) -> bool:
        """确认分享文件经识别后属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        return bool(
            name
            and MediaFileParser.is_video(name)
            and cls.media_name_matches(name, mediainfo)
        )

    @classmethod
    def _path_season_conflict(cls, item: Any, season: Any) -> tuple[bool, str, int]:
        """文件所在分享目录含与目标季冲突的显式季号时返回 (True, 命中目录, 季号)。

        ``_relative_path``/``relative_path`` 去掉文件名后按目录处理；
        ``parent_path``/``cloud_path`` 直接当目录串。无季号信息或开关关闭时不变。
        """
        if not cls.path_season_guard_enabled or season is None:
            return False, "", 0
        try:
            target = int(season)
        except (TypeError, ValueError):
            return False, "", 0
        data = item if isinstance(item, dict) else {}
        dir_texts: List[str] = []
        for field in cls._PATH_SEASON_DIR_FIELDS:
            text = str(data.get(field) or "").strip()
            if not text:
                continue
            parts = [part for part in text.strip("/").split("/") if part]
            if len(parts) < 2:
                continue
            dir_text = "/".join(parts[:-1])
            if dir_text and dir_text not in dir_texts:
                dir_texts.append(dir_text)
        for field in cls._PATH_SEASON_DIR_AS_IS_FIELDS:
            text = str(data.get(field) or "").strip().strip("/")
            if text and text not in dir_texts:
                dir_texts.append(text)
        for dir_text in dir_texts:
            if MediaFileParser.path_conflicts_season(dir_text, target):
                found = MediaFileParser.path_season(dir_text)
                return True, dir_text, int(found) if found is not None else 0
        return False, "", 0

    @classmethod
    def _log_path_season_rejection(
            cls, conflict_path: str, season: Any, found_season: int,
    ) -> None:
        logger.debug(
            f"分享目录季号与目标季冲突，拒绝候选：{conflict_path}；"
            f"目标=S{int(season):02d}，识别到=S{found_season:02d}"
        )

    @classmethod
    def episode_from_file(
            cls, item: Any, season: int, mediainfo: Any = None
    ) -> int | None:
        """使用元数据解析文件，并确认它属于目标媒体。"""
        name = str((item or {}).get("name") or "").strip()
        if not name or not MediaFileParser.is_video(name):
            return None
        if MediaFileParser.contains_other_season(name, season):
            return None
        conflict, conflict_path, found_season = cls._path_season_conflict(item, season)
        if conflict:
            cls._log_path_season_rejection(conflict_path, season, found_season)
            return None
        # 配置了「每期 N 集」的节目，先按中文期号换算（第3期上 -> E05）
        period_episode = cls.period_episode_override(name, mediainfo)
        if period_episode is not None:
            return period_episode
        meta = cls._platform_meta(name)
        if not meta:
            return None

        if not cls._has_explicit_media_title(meta):
            meta, default_episode = cls._meta_with_target_defaults(
                name, meta, mediainfo, season
            )
            if not meta:
                return None
        else:
            default_episode = None

        parsed_season = getattr(meta, "begin_season", None)
        episode = getattr(meta, "begin_episode", None) or default_episode
        if parsed_season is None or episode is None:
            return None
        try:
            parsed_season, episode = int(parsed_season), int(episode)
        except (TypeError, ValueError):
            return None
        if parsed_season != int(season) or episode <= 0:
            return None
        if not cls._matches_target_media(meta, mediainfo, season):
            return None
        return episode

    @staticmethod
    def episode_candidates(
            files: list,
            season: int,
            episodes: List[int],
            mediainfo: Any = None,
    ) -> Dict[int, List[Any]]:
        episode_list = list(dict.fromkeys(int(value) for value in episodes))
        if not episode_list:
            return {}

        target_episodes = set(episode_list)
        strict_matches = {episode: [] for episode in episode_list}
        loose_matches = {episode: [] for episode in episode_list}
        loosest_matches = {episode: [] for episode in episode_list}
        loose_patterns = {
            episode: (
                re.compile(rf"第\s*{episode}\s*集", re.IGNORECASE),
                re.compile(rf"[Ee][Pp]{episode}(?!\d)", re.IGNORECASE),
                re.compile(
                    rf"[\[\(\s\.\-_][Ee]0?{episode}[\]\)\s\.\-_]",
                    re.IGNORECASE,
                ),
            )
            for episode in episode_list
        }
        loosest_patterns = {
            episode: re.compile(
                rf"[\.\s\-_]0?{episode}[\.\s\-_]", re.IGNORECASE
            )
            for episode in episode_list
        }
        total_files = 0

        for item in MediaFileParser.iter_files(files):
            file_name = str(item.get("name") or "")
            total_files += 1
            if not MediaFileParser.is_video(file_name):
                continue
            conflict, conflict_path, found_season = FileMatcher._path_season_conflict(
                item, season
            )
            if conflict:
                # 目录季号冲突的文件不得进入 strict/loose/loosest 任何候选
                FileMatcher._log_path_season_rejection(
                    conflict_path, season, found_season
                )
                continue
            found_episode = FileMatcher.episode_from_file(item, season, mediainfo)
            if found_episode is not None:
                if found_episode in target_episodes:
                    strict_matches[found_episode].append(item)
                continue
            # 有目标媒体时，平台无法识别标题或季集的文件不能进入转存。
            if mediainfo is not None:
                continue
            matches_season = MediaFileParser.matches_target_season(file_name, season)
            has_season_marker = bool(MediaFileParser.ANY_SEASON_PATTERN.search(file_name))
            for episode in episode_list:
                if any(pattern.search(file_name) for pattern in loose_patterns[episode]):
                    if season == 1 or matches_season or not has_season_marker:
                        loose_matches[episode].append(item)
                    continue
                if matches_season and loosest_patterns[episode].search(file_name):
                    loosest_matches[episode].append(item)

        results = {}
        for episode in episode_list:
            results[episode] = (
                    strict_matches[episode]
                    or loose_matches[episode]
                    or loosest_matches[episode]
            )
            if not results[episode] and total_files:
                logger.debug(f"S{season:02d}E{episode:02d} 未匹配到实际媒体文件")
        return results

    @staticmethod
    def movie_candidates(
            files: list, min_size_mb: int = 500, mediainfo: Any = None
    ) -> List[Any]:
        min_size = max(0, int(min_size_mb or 0)) * 1024 * 1024
        candidates = [
            item
            for item in MediaFileParser.iter_files(files)
            if MediaFileParser.is_video(str(item.get("name") or ""))
               and int(item.get("size") or 0) >= min_size
               and (mediainfo is None or FileMatcher.media_matches(item, mediainfo))
        ]
        candidates.sort(key=lambda item: int(item.get("size") or 0), reverse=True)
        return candidates
