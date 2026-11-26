"""媒体识别收敛（v1.24.0 从 platform.py 与 organize.py 抽出）。

三处原来各写一份的识别逻辑：
* ``PlatformIntegrationService._recognize_subscribe_media``（按订阅对象识别），
* ``PlatformIntegrationService._recognize_agent_media``（按标题识别），
* ``OrganizeService._recognize_package``（按整包识别），

公共部分只有两件事：① 按标题/类型/季集拼 MetaInfo；② 调一次识别器
（有去重缓存的 ``_recognize_media_once``，没有就直调 ``recognize_media``）。
本模块只收敛这两件，各调用方原有的输入输出形状不变：
订阅路径的 media_source/media_id/legacy_ids 透传、agent 路径的类型校验与
季号回填、organize 路径的“识别器不存在时直调”兜底，都还留在调用方。
"""

from typing import Any

from app.core.metainfo import MetaInfo
from app.schemas.types import MediaType

from ..media import recognize_media


#: build_recognition_meta 里“不碰该字段”的哨兵：调用方传了值（含 None）
#: 就原样赋值——订阅路径原来就是 ``meta.year = None`` 显式赋值（会清掉
#: 标题里解析出的年份），不能把它当成“不传”；没传哨兵才保持 MetaInfo 原值。
_UNSET = object()


def build_recognition_meta(
        title: str,
        media_type: Any = _UNSET,
        year: Any = _UNSET,
        season: Any = _UNSET,
        episode: Any = _UNSET,
) -> MetaInfo:
    """拼一次识别用的 MetaInfo：标题 + 可选的类型/年/季/集。

    只做“赋值”本身，不替调用方做 max()/条件判断——原来三处在这上面
    各不相同（订阅路径 year 恒赋值、agent 路径 media_type 为空时不写
    type 且 begin_season 原值直赋、organize 路径季集先 max(1, …)），
    那些判断还留在调用方。``_candidate_for_tmdb_id`` 不用它：那处刻意
    不写 meta.type（只把 mtype 当 recognize_media 的参数传），形状不同、
    保持原样。
    """
    meta = MetaInfo(str(title or ""))
    if media_type is not _UNSET:
        meta.type = (
            media_type
            if isinstance(media_type, MediaType)
            else MediaType.MOVIE if str(media_type or "").strip().lower() == "movie"
            else MediaType.TV
        )
    if year is not _UNSET:
        meta.year = year
    if season is not _UNSET:
        meta.begin_season = season
    if episode is not _UNSET:
        meta.begin_episode = episode
    return meta


def recognize_once(recognizer: Any, chain: Any, cache_key: Any, **kwargs: Any):
    """调一次媒体识别：有去重识别器就走它，没有就直调 recognize_media。

    ``recognizer`` 是 ``_recognize_media_once(key, **kwargs)`` 形态的可调用
    对象（SyncHandler 上那份带缓存 + 并发去重）；传 None 时退回直调，
    与 OrganizeService 原来的 getattr 兜底行为一致。``cache=True`` 由
    调用方放进 kwargs（原来三处都是这么传的）。
    """
    if recognizer is not None:
        return recognizer(cache_key, **kwargs)
    return recognize_media(chain, **kwargs)
