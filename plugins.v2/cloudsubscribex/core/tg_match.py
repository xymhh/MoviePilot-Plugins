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
_EPISODE_EN_RE = re.compile(
    r"(?<![A-Za-z0-9])E(?:P)?\s*0*(\d{1,4})(?![0-9])", re.IGNORECASE
)
_SEASON_EPISODE_RE = re.compile(
    r"(?<![A-Za-z0-9])S0*(\d{1,3})\s*E(?:P)?0*(\d{1,4})(?![0-9])",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    r"更(?:新)?至\s*[0-9零〇一二两三四五六七八九十百./月日第集期话\s]+"
)
_QUALITY_RE = re.compile(
    r"(?:4K|8K|2160P|1080P|720P|WEB[- ]?DL|WEBRip|BluRay|BDRip|"
    r"HDR10\+?|HDR|杜比视界|杜比全景声|Dolby\s*Vision|Dolby\s*Atmos|"
    r"H\.?264|H\.?265|HEVC|AVC|AAC|DTS(?:-HD)?|TrueHD|ATMOS|REMUX|"
    r"简繁|简体|繁体|中字|国语|粤语|英语|字幕|内嵌|外挂)",
    re.IGNORECASE,
)
_TRAILING_RE = re.compile(r"[\s\-_.·|/:：,，;；]+$")

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
        match = _EPISODE_CN_RE.search(text) or _EPISODE_EN_RE.search(text)
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
    cleaned = _YEAR_RE.sub(" ", cleaned)
    cleaned = _QUALITY_RE.sub(" ", cleaned)
    cleaned = _SEASON_EPISODE_RE.sub(" ", cleaned)
    cleaned = _SEASON_CN_RE.sub(" ", cleaned)
    cleaned = _SEASON_EN_RE.sub(" ", cleaned)
    cleaned = _EPISODE_CN_RE.sub(" ", cleaned)
    cleaned = _EPISODE_EN_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\[\]【】()（）<>《》]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_·|/:：,，;；")
    cleaned = _TRAILING_RE.sub("", cleaned).strip()

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


def score_subscription(
        parsed: Dict[str, Any], sub: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """给单个订阅打分；标题不匹配、季号冲突或类型冲突返回 None。"""
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
            # 前缀只作为候选提示，不达到自动转存阈值：
            # “Doctor” 不应该自动吞掉 “Doctor Who”。
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
    if item_season is not None:
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
        return {
            "parsed": parsed,
            "matched": None,
            "candidates": [],
            "reason": "白名单内没有标题匹配的订阅",
        }
    top = candidates[0]
    close = [row for row in candidates if top["score"] - row["score"] <= 5]
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
