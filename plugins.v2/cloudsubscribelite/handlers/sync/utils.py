import copy
from typing import Any, Iterable
from urllib.parse import unquote
from app.log import logger

try:
    from app.domain.episode import format_ranges as platform_format_ranges
except Exception:
    platform_format_ranges = None


def _build_minimal_mediainfo(media: Any) -> Any:
    """用 media 字典拼一个最小 MediaInfo（title/tmdb_id/year/season/
    category 有就带上，type 必须能确定）；拼不出 type 时返回 None。"""
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
    kwargs: dict = {"type": mtype}
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


def resolve_effective_mediainfo(
        mediainfo: Any = None,
        media: Any = None,
        subscribe: Any = None,
        enrich: Any = None,
) -> Any:
    """确定一次整理/命名用的有效 MediaInfo（Phase 3a 第 6 项：唯一实现）。

    - mediainfo 非空直接用它；否则用 media 字典拼最小 MediaInfo
      （拼不出 type 时返回 None——没有类型时 DirectoryHelper.get_dir
      会按 priority 0 回落到“夸克追更”条目，解析出的目标目录是错的，
      不如直接不触发）。
    - subscribe 非空时先深拷贝再调 enrich（订阅标题/年份/分类覆盖），
      不改调用方传入的 mediainfo。
    两处旧实现（mp_organize 的 ``(mediainfo, media)`` 与 naming 的
    ``(subscribe, mediainfo)``）都只是本函数的薄适配，签名保持不变。
    """
    effective = mediainfo
    if effective is None:
        effective = _build_minimal_mediainfo(media)
    if effective is None:
        return None
    if subscribe is None:
        return effective
    effective = copy.deepcopy(effective)
    if enrich is not None:
        try:
            enriched = enrich(subscribe, effective)
            if enriched is not None:
                effective = enriched
        except Exception:
            logger.debug("订阅信息覆盖 MediaInfo 失败", exc_info=True)
    return effective


def extract_ed2k_filename(url: str) -> str:
    """从 ED2K 链接中解析提取原始文件名。"""
    if not url or not str(url).strip().lower().startswith("ed2k://|file|"):
        return ""
    parts = str(url).strip().split("|")
    if len(parts) > 2 and parts[2]:
        return unquote(parts[2]).strip()
    return ""


def format_episode_ranges(episodes: Iterable[Any]) -> str:
    """把集数集合压缩为 E01-E03、E05 形式，优先使用平台原生 format_ranges。"""
    if not episodes:
        return "无"
    numbers = sorted({int(ep) for ep in episodes if str(ep).isdigit() or isinstance(ep, int)})
    if not numbers:
        return "无"
    if platform_format_ranges:
        try:
            result = platform_format_ranges(numbers)
            if result:
                return result
        except Exception:
            logger.debug("format_episode_ranges 分支忽略异常", exc_info=True)
    ranges = []
    start = previous = numbers[0]
    for number in numbers[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
        start = previous = number
    ranges.append(f"E{start:02d}" if start == previous else f"E{start:02d}-E{previous:02d}")
    return "、".join(ranges)


def normalize_season(value: Any, default: int = 1) -> int:
    """规范化季号：None 或空字符串回退默认季号；0 表示特别篇 S00。"""
    try:
        return max(0, int(default if value is None or value == "" else value))
    except (TypeError, ValueError):
        return default
