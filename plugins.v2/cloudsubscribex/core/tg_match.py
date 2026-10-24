"""TG 频道消息 → 订阅白名单匹配。

匹配只使用消息标题/正文里的媒体名、季号、年份与类型，不使用分享内的文件名：
``1.mkv`` 这类文件名只能提供集号，无法判断它属于哪部作品。匹配结果只用于
决定「把这条链接交给哪个订阅」，真正的季集识别仍由现有手动转存链路完成。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

_PREFIX_RE = re.compile(r"^\s*(?:名称|标题|片名|剧名|资源名)\s*[:：]\s*")
_CATEGORY_RE = re.compile(
    r"[\[【(（][^\[\]【】()（）]{0,40}"
    r"(?:综艺|电影|电视剧|剧集|动漫|动画|纪录片|真人秀|短剧|连续剧)"
    r"[^\[\]【】()（）]{0,40}[\]】)）]"
)
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_SEASON_CN_RE = re.compile(r"第\s*([零〇一二两三四五六七八九十百\d]{1,4})\s*季")
_SEASON_EN_RE = re.compile(
    r"(?<![A-Za-z0-9])S(?:eason)?\s*0*(\d{1,3})(?![0-9])", re.IGNORECASE
)
_EPISODE_CN_RE = re.compile(r"第\s*(\d{1,4})\s*[集话話期]")
_EPISODE_RANGE_CN_RE = re.compile(
    r"第\s*(\d{1,4})\s*[-~至]\s*(\d{1,4})\s*[集话話期]"
)
_EPISODE_RANGE_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d{1,4})\s*[-~至]\s*(\d{1,4})\s*[集话話期]"
)
_ALL_EPISODES_RE = re.compile(r"全\s*\d{1,4}\s*[集话話期]?|全集")
_EPISODE_EN_RE = re.compile(
    r"(?<![A-Za-z0-9])E(?:P)?\s*0*(\d{1,4})(?![0-9])", re.IGNORECASE
)
_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])S0*(\d{1,3})\s*E(?:P)?0*(\d{1,4})(?![0-9])",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    r"(?:更(?:新)?至|更新|更)\s*"
    r"(?:EP?\s*\d{1,4}|第\s*\d{1,4}\s*[集话話期]|"
    r"\d{1,4}\s*[集话話期]|"
    r"[0-9零〇一二两三四五六七八九十百./月日第集期话\s]+)",
    re.IGNORECASE,
)
# 音轨编码和声道数要整体吞掉，否则 AAC2.0 / DDP5.1 会残留 “2.0”“5.1”，
# 把精确标题降成 88 分前缀。先于 _QUALITY_RE 执行。
_AUDIO_RE = re.compile(
    r"(?:AAC|EAC3|AC3|DDP|DTS(?:-HD)?|TrueHD|FLAC|OPUS|ATMOS|HIFI(?:声)?)"
    r"(?:\s*\d(?:[ .]\d)?)?",
    re.IGNORECASE,
)
_QUALITY_RE = re.compile(
    r"(?:4K|8K|2160P|1080P|720P|WEB[- ]?DL|WEBRip|BluRay|BDRip|"
    r"HDR10\+?|HDR|杜比视界|杜比全景声|杜比环绕声|Dolby\s*Vision|Dolby\s*Atmos|"
    r"H\.?264|H\.?265|HEVC|AVC|"
    r"REMUX|DoVi|(?<![A-Za-z])DV(?![A-Za-z])|WEB(?:[- ]?DL|Rip)?|HDTV|ACC|CHS|CHT|简英|双语|"
    r"10bit|SDR|简繁|简体|繁体|中字|国语|粤语|英语|字幕|内嵌|外挂|内封|简中|"
    r"高码率?版?|臻彩(?:MAX)?|无损|\d+Audio|Audio|\d+\s*(?:FPS|帧率?)|"
    r"(?<![A-Za-z])IQ(?![A-Za-z])|更新中|连载中|已完结|完结|"
    # 1.7.33：线上真实样本残留（"金银岛AMZN单集4.8GB""Doctor1080NF高码版精修简韩特效sup"）
    r"AMZN|ATVP|DSNP|HMAX|iTunes|"
    r"(?<![A-Za-z])NF(?![A-Za-z])|"
    r"单集\s*\d+(?:\.\d+)?\s*[GgTt][Bb]?|"
    r"特效|精修|简韩|简日|繁日|简繁日|"
    r"sup|ass|srt)",
    re.IGNORECASE,
)
# 标题与演员表之间常用逗号分隔；演员名通常是 2-4 个汉字，用 / 或 、 分隔。
_CREDITS_RE = re.compile(
    r"[，,]\s*[\u4e00-\u9fff·.]{2,20}"
    r"(?:\s*[/、]\s*[\u4e00-\u9fff·.]{2,20})+\s*$"
)
# 频道常把“剧情/悬疑/犯罪”等分类词直接跟在标题后，用空白隔开。
_GENRE_RE = re.compile(
    r"(?<=\s)(?:剧情|悬疑|犯罪|爱情|古装|喜剧|动画|奇幻|科幻|动作|冒险|"
    r"历史|战争|家庭|青春|都市|旅行|真人秀|综艺)(?=\s|$)"
)
# “现在就出发 第四季 现在就出发4”这类频道会重复剧名；折叠重复前缀。
_DUPLICATE_TITLE_RE = re.compile(
    r"^([\u4e00-\u9fffA-Za-z0-9·:：]{2,40}?)\s+\1\s*\d{0,3}\s*$"
)
# 短后缀自动匹配只接受“正片/演员/副标题”这类明确补充；
# 这些词一旦出现在后缀开头，说明可能是不同作品/不同季，继续留待手动。
_TITLE_SUFFIX_FORBIDDEN = (
    "之", "第", "番", "特别", "剧场", "电影", "季", "部",
    "重启", "新篇", "归来", "再起", "预告", "花絮", "特辑",
    "先导", "加更", "纯享",
)
_TRAILING_RE = re.compile(r"[\s\-_.·|/:：,，;；]+$")
# 频道标题里的补充信息（画质/演员/分类）多放在括号内；集号季号已在前面提取，
# 这里整段丢弃，避免“玉簟秋webacc”“嫁金钗dovi”这类残留把精确匹配降成前缀匹配。
_BRACKET_RE = re.compile(r"[\[【(（][^\]】)）]*[\]】)）]")
# 标题后残留的技术词（可被分隔符切开），只在中文/数字之后清理，避免误伤英文片名。
_TECH_RESIDUE_RE = re.compile(
    r"(?<=[\u4e00-\u9fff0-9])[\s\-_.·|/:：,，;；]*"
    r"(?:web|webdl|webrip|hdtv|dovi|dv|hdr\d*|sdr|aac|acc|hevc|avc|"
    r"h26[45]|2160p|1080p|720p|\d+\.\d+|"
    # 1.7.33：点号分割后裸分辨率与平台/字幕缩写残留
    r"2160|1080|720|480|amzn|atvp|dsnp|hmax|nf|sup|ass|srt)"
    r"(?=$|[\s\-_.·|/:：,，;；])",
    re.IGNORECASE,
)

_TV_WORDS = ("电视剧", "剧集", "综艺", "动漫", "动画", "纪录片", "真人秀", "短剧", "连续剧")
_MOVIE_WORDS = ("电影", "影片")

_CN_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _cn_number(value: str) -> Optional[int]:
    """解析 1~999 的简单中文数字（第四季/十二季/二十季）。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    if "百" in text:
        left, _, right = text.partition("百")
        hundreds = _CN_DIGITS.get(left, 1 if left == "" else 0)
        rest = _cn_number(right) if right else 0
        return hundreds * 100 + (rest or 0)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1 if left == "" else 0)
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in text:
        if char not in _CN_DIGITS:
            return None
        total = total * 10 + _CN_DIGITS[char]
    return total


def normalize_title(value: Any) -> str:
    """标题归一化：NFKC、大小写折叠，只保留文字和数字。"""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def parse_message_title(value: Any) -> Dict[str, Any]:
    """从频道消息标题提取媒体名、季、集、年份和类型。

    例：``名称：现在就出发 第四季(2026) 更至10.18期 [旅行/综艺]``
    → ``title=现在就出发, season=4, year=2026, media_type=tv``。
    """
    raw = str(value or "").strip()
    first_line = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    text = _PREFIX_RE.sub("", first_line)
    text = unicodedata.normalize("NFKC", text)
    type_text = text.casefold()
    text = _CATEGORY_RE.sub(" ", text)

    season = None
    episode = None
    season_episode = _SEASON_EPISODE_RE.search(text)
    if season_episode:
        season = int(season_episode.group(1))
        episode = int(season_episode.group(2))
    if season is None:
        match = _SEASON_CN_RE.search(text) or _SEASON_EN_RE.search(text)
        if match:
            season = _cn_number(match.group(1))
    if episode is None:
        match = (
            _EPISODE_RANGE_CN_RE.search(text)
            or _EPISODE_RANGE_RE.search(text)
            or _EPISODE_CN_RE.search(text)
            or _EPISODE_EN_RE.search(text)
        )
        if match:
            episode = int(match.group(1))

    year = None
    match = _YEAR_RE.search(text)
    if match:
        year = int(match.group(1))

    media_type = ""
    if any(word in type_text for word in _MOVIE_WORDS):
        media_type = "movie"
    elif any(word in type_text for word in _TV_WORDS):
        media_type = "tv"
    if episode is not None or season is not None:
        media_type = "tv"

    cleaned = _UPDATE_RE.sub(" ", text)
    cleaned = _BRACKET_RE.sub(" ", cleaned)
    cleaned = _YEAR_RE.sub(" ", cleaned)
    cleaned = _AUDIO_RE.sub(" ", cleaned)
    cleaned = _QUALITY_RE.sub(" ", cleaned)
    cleaned = _GENRE_RE.sub(" ", cleaned)
    cleaned = _CREDITS_RE.sub(" ", cleaned)
    cleaned = _DUPLICATE_TITLE_RE.sub(r"\1", cleaned)
    cleaned = _SEASON_EPISODE_RE.sub(" ", cleaned)
    cleaned = _EPISODE_RANGE_CN_RE.sub(" ", cleaned)
    cleaned = _EPISODE_RANGE_RE.sub(" ", cleaned)
    cleaned = _ALL_EPISODES_RE.sub(" ", cleaned)
    cleaned = _SEASON_CN_RE.sub(" ", cleaned)
    cleaned = _SEASON_EN_RE.sub(" ", cleaned)
    cleaned = _EPISODE_CN_RE.sub(" ", cleaned)
    cleaned = _EPISODE_EN_RE.sub(" ", cleaned)
    # 季/集号去掉后可能才露出重复的剧名（“现在就出发 第四季 现在就出发4”）
    cleaned = _DUPLICATE_TITLE_RE.sub(r"\1", cleaned)
    cleaned = re.sub(r"[\[\]【】()（）<>《》]", " ", cleaned)
    # “嫁金钗 1-8”“嫁金钗1-8”这类不带“集”的集数范围
    cleaned = re.sub(r"\s+\d{1,4}\s*[-~至]\s*\d{1,4}\s*$", " ", cleaned)
    cleaned = re.sub(
        r"(?<=[\u4e00-\u9fff])\d{1,4}\s*[-~至]\s*\d{1,4}\s*$", " ", cleaned,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_·|/:：,，;；")
    # 反复剥离尾部技术词与分隔符（"玉簟秋 web- ACC" → "玉簟秋"）
    for _ in range(3):
        polished = _TECH_RESIDUE_RE.sub(" ", cleaned)
        polished = re.sub(r"[\s\-_.·|/:：,，;；]+$", "", polished).strip()
        if polished == cleaned:
            break
        cleaned = polished
    cleaned = _TRAILING_RE.sub("", cleaned).strip()
    # 去掉标题两端的频道图标/装饰符号，保留中间标点（Doctor X：…）
    cleaned = re.sub(r"^[\W_]+|[\W_]+$", "", cleaned).strip()

    return {
        "title": cleaned,
        "normalized_title": normalize_title(cleaned),
        "season": season,
        "episode": episode,
        "year": year,
        "media_type": media_type,
    }


def _subscription_type(value: Any, title: str = "") -> str:
    text = str(value or "").strip().casefold()
    if text in {"电影", "movie"} or "电影" in text:
        return "movie"
    if text in {"电视剧", "tv", "剧集", "动漫"} or "剧" in text or "综艺" in text:
        return "tv"
    parsed = parse_message_title(title)
    return parsed["media_type"]


def _subscription_titles(sub: Dict[str, Any]) -> List[str]:
    values = [sub.get("name") or sub.get("title") or ""]
    aliases = sub.get("aliases") or sub.get("alias") or []
    if isinstance(aliases, str):
        aliases = re.split(r"[\n,，、;；]+", aliases)
    if isinstance(aliases, (list, tuple, set)):
        values.extend(str(item) for item in aliases if str(item).strip())
    return [value for value in dict.fromkeys(values) if str(value).strip()]


def _short_suffix_score(sub_title: str, item_title: str) -> int:
    """主标题后只跟演员/副标题时给 94 分；歧义后缀保持 88。

    ``Doctor`` 这类纯英文标题不适用，避免把 ``Doctor Who`` 自动吞掉。
    """
    if len(sub_title) < 3:
        return 0
    if not any("\u4e00" <= char <= "\u9fff" for char in sub_title):
        return 0
    suffix = item_title[len(sub_title):]
    if not (2 <= len(suffix) <= 8):
        return 0
    if not suffix.isalnum():
        return 0
    if suffix.isdigit() and len(suffix) > 2:
        return 0
    if suffix.startswith(_TITLE_SUFFIX_FORBIDDEN):
        return 0
    return 94


def _alias_suffix_score(sub_title: str, item_title: str) -> int:
    """主标题后跟别名/原名/台标残留时给 92 分。

    与 ``_short_suffix_score``（2-8 字演员/副标题，94 分）互补：频道常把
    作品原名直接贴在中文名后面，例如

    - ``某暗部的少女共栖 とある暗部の少女共棲``
    - ``FX战士久留美 FX戦士くるみちゃん``

    归一化后原名与中文名连成一串，长度超过 8，旧逻辑只给 88 分前缀分，
    低于 90 的自动转存门槛，于是真实资源被永久挂起（2026-10-24 线上 4 条）。

    保护：``Doctor`` 不应该吞掉 ``Doctor Who`` —— 订阅名全是拉丁字母、
    后缀是独立的拉丁单词时不提分。
    """
    if len(sub_title) < 2 or not item_title.startswith(sub_title):
        return 0
    suffix = item_title[len(sub_title):]
    if len(suffix) < 3:
        return 0
    if suffix.isdigit():
        return 0
    if suffix.startswith(_TITLE_SUFFIX_FORBIDDEN):
        return 0
    if sub_title.isascii():
        if re.fullmatch(r"[a-z]{2,}", suffix):
            return 0
    return 92


_LATIN_ONLY_RE = re.compile(r"[a-z0-9]+")


def dedupe_duplicate_subscriptions(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同名 + 同季 + 同类型的多条订阅只保留一条。

    线上实例：``#68`` 与 ``#207`` 都是「Doctor X：白色黑手党时代」S01 电视剧，
    两条打分完全一致（100 vs 100，差 0），于是每条真实资源都被判
    「匹配到多个相近订阅」永久挂起——5 条 2026 新番全部卡在 pending。

    保留优先级：仍在订阅中（state=R）> id 更大（更新的一条）。
    """
    if len(candidates) <= 1:
        return list(candidates)
    groups: Dict[Tuple[str, int, str], List[Dict[str, Any]]] = {}
    for row in candidates:
        key = (
            normalize_title(row.get("name")),
            int(row.get("season") or 0),
            str(row.get("type") or ""),
        )
        groups.setdefault(key, []).append(row)
    kept: List[Dict[str, Any]] = []
    for rows in groups.values():
        if len(rows) == 1:
            kept.append(rows[0])
            continue
        kept.append(max(rows, key=lambda row: (
            1 if str(row.get("state") or "").upper() == "R" else 0,
            int(row.get("id") or 0),
        )))
    kept.sort(key=lambda row: (-int(row["score"]), int(row["id"])))
    return kept


def score_subscription(
        parsed: Dict[str, Any], sub: Dict[str, Any],
        ignore_season: bool = False,
) -> Optional[Dict[str, Any]]:
    """给单个订阅打分；标题不匹配、季号冲突或类型冲突返回 None。

    ``ignore_season`` 只用于诊断：标题命中但季号不符时，仍返回分数以便
    日志说明真实原因，而不是笼统地写「没有标题匹配的订阅」。
    """
    item_title = parsed["normalized_title"]
    if not item_title:
        return None

    best_score = 0
    best_reason = ""
    for candidate in _subscription_titles(sub):
        sub_parsed = parse_message_title(candidate)
        sub_title = sub_parsed["normalized_title"] or normalize_title(candidate)
        if not sub_title:
            continue
        if item_title == sub_title:
            score, reason = 100, "标题精确"
        elif item_title.startswith(sub_title) and len(sub_title) >= 2:
            # 中文主标题 + 2-8 字短后缀（演员/副标题）给 94 分；
            # 其余前缀只作为候选提示，不达到自动转存阈值：
            # “Doctor” 不应该自动吞掉 “Doctor Who”。
            short_score = _short_suffix_score(sub_title, item_title)
            alias_score = _alias_suffix_score(sub_title, item_title)
            if short_score:
                score, reason = short_score, "标题+短后缀"
            elif alias_score:
                score, reason = alias_score, "标题+原名/别名"
            else:
                score, reason = 88, "标题前缀"
        elif sub_title.startswith(item_title) and len(item_title) >= 2:
            score, reason = 86, "订阅名更完整"
        elif len(sub_title) >= 4 and sub_title in item_title:
            score, reason = 82, "标题包含"
        else:
            continue
        if score > best_score:
            best_score, best_reason = score, reason
    if not best_score:
        return None

    sub_season = sub.get("season")
    try:
        sub_season = int(sub_season or 0)
    except (TypeError, ValueError):
        sub_season = 0
    item_season = parsed.get("season")
    if item_season is not None and not ignore_season:
        if sub_season and int(item_season) != sub_season:
            return None
        if sub_season and best_score >= 90:
            best_score += 6
            best_reason += f" + S{int(item_season):02d}"

    sub_type = _subscription_type(sub.get("type"), str(sub.get("name") or ""))
    item_type = parsed.get("media_type") or ""
    if sub_type and item_type and sub_type != item_type:
        return None
    if sub_type == "movie" and parsed.get("season") is not None:
        return None
    if sub_type == "movie" and parsed.get("episode") is not None:
        return None

    # 年份只用于电影：剧集/综艺的年份常是首播年，与当前季年份不一致很正常。
    sub_year = str(sub.get("year") or "").strip()
    item_year = parsed.get("year")
    if sub_type == "movie" and sub_year.isdigit() and item_year:
        diff = abs(int(sub_year) - int(item_year))
        if diff > 1:
            return None
        if diff == 0:
            best_score += 3
            best_reason += " + 年份"

    return {
        "id": int(sub.get("id") or sub.get("subscribe_id") or 0),
        "name": str(sub.get("name") or sub.get("title") or ""),
        "season": sub_season,
        "type": str(sub.get("type") or ""),
        "state": str(sub.get("state") or ""),
        "score": best_score,
        "reason": best_reason,
    }


def match_item(
        item: Dict[str, Any],
        subscriptions: Iterable[Dict[str, Any]],
        allowed_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    """把一条频道消息匹配到订阅白名单。

    Returns:
        ``parsed``：消息解析结果；
        ``matched``：唯一高置信订阅（没有则为 None）；
        ``candidates``：按分数降序的候选（最多 5 条）；
        ``reason``：自动转存或挂起的原因。
    """
    parsed = parse_message_title(
        (item or {}).get("title") or (item or {}).get("text") or ""
    )
    has_whitelist = allowed_ids is not None
    allowed = {
        int(value) for value in (allowed_ids or [])
        if str(value).strip().lstrip("-").isdigit()
    }
    candidates: List[Dict[str, Any]] = []
    for sub in subscriptions or []:
        try:
            sub_id = int(sub.get("id") or sub.get("subscribe_id") or 0)
        except (TypeError, ValueError):
            continue
        if has_whitelist and sub_id not in allowed:
            continue
        scored = score_subscription(parsed, sub)
        if scored:
            candidates.append(scored)
    candidates.sort(key=lambda row: (-row["score"], row["id"]))

    if not candidates:
        conflicts: List[str] = []
        if parsed.get("season") is not None:
            for sub in subscriptions or []:
                try:
                    sub_id = int(sub.get("id") or sub.get("subscribe_id") or 0)
                except (TypeError, ValueError):
                    continue
                if has_whitelist and sub_id not in allowed:
                    continue
                scored = score_subscription(parsed, sub, ignore_season=True)
                if not scored:
                    continue
                sub_season = int(scored.get("season") or 0)
                if sub_season and sub_season != int(parsed["season"]):
                    conflicts.append(
                        f"#{scored['id']} {scored['name']} S{sub_season:02d}"
                    )
        if conflicts:
            shown = "、".join(conflicts[:3])
            reason = (
                f"标题匹配但季号不符（消息 S{int(parsed['season']):02d} / "
                f"订阅 {shown}），留待手动选择"
            )
        else:
            reason = "白名单内没有标题匹配的订阅"
        return {
            "parsed": parsed,
            "matched": None,
            "candidates": [],
            "reason": reason,
        }
    top = candidates[0]
    close = [row for row in candidates if top["score"] - row["score"] <= 5]
    # 同名重复登记（#68/#207 同时订阅同一部剧）不该算「多候选」：
    # 一个订阅被登记两遍会让每条真实资源永久挂起。
    if len(close) > 1:
        deduped = dedupe_duplicate_subscriptions(close)
        if len(deduped) == 1:
            close = deduped
            top = deduped[0]
    if len(close) > 1:
        names = "、".join(f"#{row['id']} {row['name']}" for row in close[:3])
        reason = f"匹配到多个相近订阅（{names}），留待手动选择"
    elif top["score"] < 90:
        reason = f"匹配度过低（{top['score']}），留待手动选择"
    else:
        reason = f"匹配到 #{top['id']} {top['name']}（{top['score']}，{top['reason']}）"
        return {
            "parsed": parsed,
            "matched": top,
            "candidates": candidates[:5],
            "reason": reason,
        }
    return {
        "parsed": parsed,
        "matched": None,
        "candidates": candidates[:5],
        "reason": reason,
    }
