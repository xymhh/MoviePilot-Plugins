"""同步任务完成通知聚合与推送服务。"""

import copy
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger
from app.schemas.types import ContentType, MessageType, NotificationType

from ...core import OwnerDelegator, render_template, resolve_template
from ...drive.common import format_size


def _render_started_notification_template(
        *,
        mediainfo: Any,
        is_movie: bool,
        season_no: int,
        ep_str: str,
        has_episodes: bool,
        first_episode: Optional[int],
        size_text: str,
        site_name: str,
        image: Optional[str],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """用 MP 自己的模板渲染器渲一条开始下载通知（v1.26.2）。

    MP 只在标题正文都空时才渲染用户配的 DownloadAdded 模板
    （``MessageTemplateHelper.render`` 的 ``is_instance_valid`` 短路），
    所以这里先建一条不带标题正文的裸 Message，带上 mediainfo 与季集/
    大小等上下文去渲；只有渲出非空标题正文才返回，否则返回 None 让
    调用方退回写死格式。种子才有的变量（做种数、原名链接、站点、质量词）
    云盘转存没有，模板里有 ``{% if %}`` 守卫，那几行直接跳过。

    任何一步失败（没配模板、模板改坏了、MP 内部接口变了、测试桩环境）
    都只返回 None，绝不抛、绝不发空通知。

    v1.26.3：补 ``pubdate``（转存开始时间，``YYYY-MM-DD HH:MM:SS``），
    否则用户模板里 ``{% if pubdate %}🕒时间：...`` 那行永远出不来
    （MP 自身通知里的时间就是这么来的：种子发布时间；云盘转存没有
    种子，就填转存开始时间）。
    """
    if not str(getattr(mediainfo, "title", "") or "").strip():
        return None
    try:
        from app.application.messaging.message import MessageTemplateHelper
        from app.schemas.message import Message
    except Exception:  # noqa: BLE001 - MP 模板接口不可用时退回写死格式
        return None
    try:
        message = Message(
            mtype=MessageType.Download,
            ctype=ContentType.DownloadAdded,
            image=image,
        )
        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        render_kwargs: Dict[str, Any] = {
            "mediainfo": mediainfo,
            "current_time": started_at,
            "pubdate": started_at,
        }
        if size_text:
            render_kwargs["size"] = size_text
        if site_name:
            render_kwargs["site_name"] = site_name
        if not is_movie:
            render_kwargs["season_fmt"] = f"S{season_no:02d}"
            if has_episodes:
                render_kwargs["download_episodes"] = ep_str
            if first_episode:
                render_kwargs["season_episode"] = (
                    f"S{season_no:02d}E{first_episode:02d}"
                )
            else:
                render_kwargs["season_episode"] = f"S{season_no:02d}"
        rendered = MessageTemplateHelper.render(message, **render_kwargs)
        if rendered is None:
            return None
        title = getattr(rendered, "title", None)
        text = getattr(rendered, "text", None)
        if not (isinstance(title, str) and title.strip()):
            return None
        if not (isinstance(text, str) and text.strip()):
            return None
        rendered_image = getattr(rendered, "image", None)
        if not (isinstance(rendered_image, str) and rendered_image.strip()):
            rendered_image = image
        return title, text, rendered_image
    except Exception:  # noqa: BLE001 - 渲染失败退回写死格式
        return None


def _format_completed_episodes(season: int, episodes: List[int]) -> str:
    """完成通知的季集文本：≤5 集全列，>5 集只给首尾 + 集数。"""
    eps = sorted({int(episode) for episode in episodes if int(episode or 0) > 0})
    if not eps:
        return f"S{season:02d}"
    if len(eps) <= 5:
        return f"S{season:02d}" + ", ".join(f"E{episode:02d}" for episode in eps)
    return f"S{season:02d}E{eps[0]:02d}-E{eps[-1]:02d} 共{len(eps)}集"


def _completed_notification_context(detail: Dict[str, Any]) -> Dict[str, Any]:
    """从终态详情算完成通知的展示字段（模板变量与写死兜底共用）。

    电影没有集数行；剧集没给集数时只给季号；大小未知时给“未知”
    （资源入库模板的大小行是无条件的，不能空着）。
    """
    title = str(detail.get("title") or "").strip()
    year = detail.get("year") or ""
    title_year = f"{title} ({year})" if title and year else title
    is_movie = str(detail.get("type") or "").strip() == "电影"
    season = max(1, int(detail.get("season") or 1))
    episodes = sorted({
        int(episode) for episode in (detail.get("episodes") or [])
        if int(episode or 0) > 0
    })
    file_count = 1 if (is_movie or not episodes) else len(episodes)
    if is_movie:
        season_episode = ""
    elif episodes:
        season_episode = _format_completed_episodes(season, episodes)
    else:
        season_episode = f"S{season:02d}"
    size = int(detail.get("size") or 0)
    return {
        "title": title,
        "title_year": title_year,
        "is_movie": is_movie,
        "season": season,
        "episodes": episodes,
        "file_count": file_count,
        "season_episode": season_episode,
        "total_size": format_size(size) if size > 0 else "未知",
        "category": str(detail.get("category") or "").strip(),
        "image": detail.get("image") or None,
    }


class _MediaTypeValue(str):
    """str 替身：真实 builder 读 .value，测试替身读原值。

    v1.28.1：移到模块级——此前它是 _completed_mediainfo_stub 里的
    局部类，MP 的通知模板缓存（TTLCache region="notification"，set_cache_context
    会把整个渲染上下文 pickle 进 redis）序列化它时报
    ``Can't pickle local object``，入库完成通知发出瞬间 redis 写缓存失败。
    通知本身已发出，修法只动可 pickle 性，不改通知内容。
    """

    @property
    def value(self):
        return str(self)


class _CompletedMediaInfoStub:
    """给完成通知模板渲染用的最小 MediaInfo 替身（可 pickle 版）。

    v1.28.1：此前用 SimpleNamespace + 局部类 + lambda 拼装——局部类与
    lambda 都不能 pickle，MP 模板缓存写 redis 时失败。改成模块级普通类
    + 真方法，属性与 v1.26.4 的 SimpleNamespace 版逐个一致，模板渲染
    行为不变。
    """

    def __init__(self, detail: Dict[str, Any]) -> None:
        context = _completed_notification_context(detail)
        self.title = context["title"]
        self.en_title = ""
        self.original_title = ""
        self.season = context["season"] if not context["is_movie"] else None
        self.year = detail.get("year") or ""
        self.title_year = context["title_year"]
        self.type = _MediaTypeValue(str(detail.get("type") or "").strip())
        self.category = context["category"]
        self.vote_average = None
        self.season_years = {}
        self.actors = []
        self.overview = ""
        self.tmdb_id = None
        self.imdb_id = None
        self.douban_id = None
        self.bangumi_id = None
        self.anilist_id = None
        self.media_source = ""
        self.media_id = ""
        self.detail_link = str(detail.get("detail_link") or "")
        self._poster_image = context["image"]

    def get_poster_image(self):
        return self._poster_image

    def get_backdrop_image(self):
        return None


def _completed_mediainfo_stub(detail: Dict[str, Any]) -> Any:
    """给完成通知模板渲染用的最小 MediaInfo 替身。

    终态详情流里没有 MediaInfo 对象（只有标题/年份/类别等纯字段），
    但 MP 的 TemplateContextBuilder 只有拿到 mediainfo 才会把
    ``__mediainfo__``（模板标题里的 detail_link 就靠它）放进上下文，
    且 _add_media_info 会读一串属性。按 MP v3.2.6 实际读到的字段
    逐个给安全缺省值：缺了哪个都会在渲染里抛错、整条模板路失效。
    type 用带 .value 的字符串替身：真实 builder 读 .value，测试
    替身读原值，两边都拿到中文类型名。
    """
    return _CompletedMediaInfoStub(detail)


def _render_completed_notification_template(
        *,
        detail: Dict[str, Any],
) -> Optional[Tuple[str, str, Optional[str]]]:
    """用 MP 自己的模板渲染器渲一条转存完成通知（v1.26.4）。

    与开始通知同套路：先建一条不带标题正文的裸 Message
    （mtype=Organize、ctype=OrganizeSuccess），带上下文调
    ``MessageTemplateHelper.render`` 渲用户配的资源入库模板；只有
    渲出非空标题正文才返回，否则返回 None 让调用方退回写死格式。
    模板要的变量：file_count（标题里的文件数）、title_year、
    type、category、season_episode、total_size；detail_link 靠
    mediainfo 替身挂进 ``__mediainfo__``（见
    ``_completed_mediainfo_stub``）。种子才有的变量（小组、质量词）
    云盘转存没有，模板里有 ``{% if %}`` 守卫，那几行直接跳过。

    任何一步失败（没配模板、模板改坏了、MP 内部接口变了、测试桩环境）
    都只返回 None，绝不抛、绝不发空通知。
    """
    context = _completed_notification_context(detail)
    if not context["title"]:
        return None
    try:
        from app.application.messaging.message import MessageTemplateHelper
        from app.schemas.message import Message
    except Exception:  # noqa: BLE001 - MP 模板接口不可用时退回写死格式
        return None
    try:
        message = Message(
            mtype=MessageType.Organize,
            ctype=ContentType.OrganizeSuccess,
            image=context["image"],
        )
        rendered = MessageTemplateHelper.render(
            message,
            mediainfo=_completed_mediainfo_stub(detail),
            file_count=context["file_count"],
            title_year=context["title_year"],
            type=str(detail.get("type") or "").strip(),
            category=context["category"],
            season_episode=context["season_episode"],
            total_size=context["total_size"],
        )
        if rendered is None:
            return None
        title = getattr(rendered, "title", None)
        text = getattr(rendered, "text", None)
        if not (isinstance(title, str) and title.strip()):
            return None
        if not (isinstance(text, str) and text.strip()):
            return None
        rendered_image = getattr(rendered, "image", None)
        if not (isinstance(rendered_image, str) and rendered_image.strip()):
            rendered_image = context["image"]
        return title, text, rendered_image
    except Exception:  # noqa: BLE001 - 渲染失败退回写死格式
        return None


class SyncNotificationService(OwnerDelegator):
    """负责洗版完成后的通知合并、静默窗口等待与聚合发送（v1.13.3：
    转存完成通知已收敛掉，入库通知由 MP 自己发）。

    v1.26.4：普通转存的完成通知在终态门恢复——两处调用点（同步完成块、
    ``_send_finalized_batch``）共用 ``send_transfer_completed_notification``，
    按分支分流：MP 整理分支跳过（MP 已发），strm 直出分支发
    OrganizeSuccess（走用户资源入库模板，模板优先、写死兜底）。
    """

    _NOTIFICATION_BATCH_WINDOW_SECONDS = 3.0

    # 完成通知去重窗口：同一转存的两处门（同步完成块、
    # ``_send_finalized_batch``）按 pending_key 互斥、正常不会重叠；
    # 这里是最后一道保险——窗口内同一详情只发一条，窗口后重传
    # 同一集正常通知（秒数级远大于两处门的间隔）。
    _COMPLETED_NOTIFICATION_DEDUP_TTL_SECONDS = 600.0

    def __init__(self, owner=None):
        super().__init__(owner)
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None

    def send_transfer_notification(
            self, transfer_details: List[Dict[str, Any]], total_count: int
    ) -> None:
        """完成即入队，并按延迟窗口合并相邻任务通知。"""
        post_message = getattr(self, "_post_message", None)
        if not transfer_details or not post_message:
            return
        delay_seconds = getattr(self, "_notification_delay_seconds", 0)
        with self._notification_batch_lock:
            self._notification_batch.extend(copy.deepcopy(transfer_details))
            if self._notification_batch_timer:
                self._notification_batch_timer.cancel()
            wait_seconds = max(
                delay_seconds,
                self._NOTIFICATION_BATCH_WINDOW_SECONDS,
            )
            self._notification_batch_timer = threading.Timer(
                wait_seconds, self._flush_transfer_notifications
            )
            self._notification_batch_timer.daemon = True
            self._notification_batch_timer.start()
        logger.debug(
            f"完成通知已入队：{total_count} 个文件，"
            f"静默 {wait_seconds} 秒后合并发送"
        )

    def send_transfer_started_notification(
            self,
            *,
            mediainfo: Any = None,
            subscribe: Any = None,
            source: str = "subscribe",
            via: str = "",
            items: Optional[List[Dict[str, Any]]] = None,
            media_type: str = "tv",
            season: Optional[int] = None,
            episodes: Optional[List[int]] = None,
            total_size: Optional[int] = None,
    ) -> None:
        """转存真正开始时补一条 MP 原生形态的开始下载通知（v1.25.0 Issue A，v1.26.2 走用户模板）。

        MP 自己的开始下载通知是 ``Message(mtype=Download,
        ctype=DownloadAdded)``，由用户配的 DownloadAdded 模板渲染；插件的
        三条路（订阅自动跑 / TG 监控 / 手动转存）走内部云盘直传，从不经过
        MP 下载链，所以一条都不会发。这里在转存入口（入库管线过判重门后、
        离线提交被网盘接受后）补发一条：

        * ``mtype=MessageType.Download``：与原生通知走同一路由开关
          （``get_notification_switch(mtype)``），同一渠道投递；
        * ``ctype=ContentType.DownloadAdded``：消息上带原生内容类型标识；
        * 标题/正文优先走用户模板：先建一条不带标题正文的裸 Message，
          带 mediainfo 与季集/大小等上下文调
          ``MessageTemplateHelper.render`` 渲用户配的 DownloadAdded 模板
          （见 ``_render_started_notification_template``）；只有渲出非空
          标题正文才用模板结果，否则退回插件写死格式（标题固定
          「【网盘订阅助手】开始下载」）。种子才有的变量（做种数、原名
          链接、站点、质量词）云盘转存没有，模板里有 ``{% if %}`` 守卫，
          那几行直接跳过；站点名只在 TG 路填固定值“TG频道”（v1.26.3，
        不再解析频道名）；pubdate 填转存开始时间，模板里的时间行才出得来。

        只在转存真正开始时由调用方调一次；链接解析失败、查库命中跳过、
        判重拦下的都不进这里。通知开关 ``_notify`` 关了就不发；发送
        本身失败只记日志，绝不能打断转存。
        """
        post_message = getattr(self, "_post_message", None)
        if not getattr(self, "_notify", False) or not post_message:
            return
        try:
            title = str(getattr(mediainfo, "title", "") or "").strip()
            if not title:
                title = str(getattr(subscribe, "name", "") or "").strip() or "未知媒体"
            year = getattr(mediainfo, "year", None) or ""
            is_movie = str(media_type or "").strip().lower() == "movie"
            season_no = max(1, int(season or 1))
            eps = sorted({
                int(value) for value in (
                    list(episodes or [])
                    or [
                        int((item or {}).get("episode") or 0)
                        for item in (items or [])
                    ]
                ) if int(value or 0) > 0
            })
            if eps:
                if len(eps) <= 5:
                    ep_str = ", ".join(f"E{value:02d}" for value in eps)
                else:
                    ep_str = (
                        f"E{eps[0]:02d}-E{eps[-1]:02d} 共{len(eps)}集"
                    )
            else:
                ep_str = ""
            if is_movie:
                headline = f"{title} ({year})".replace(" ()", "")
            elif eps:
                headline = f"{title} S{season_no:02d} {ep_str}".strip()
            else:
                headline = f"{title} S{season_no:02d}".strip()
            size = int(total_size or 0)
            if size <= 0 and items:
                size = sum(
                    max(0, int(((item or {}).get("file") or {}).get("size") or 0))
                    for item in items
                )
            source_label = {
                "tg": "TG 频道",
                "manual": "手动",
                "subscribe": "订阅",
            }.get(str(source or "").strip().lower(), "订阅")
            # v1.26.3：TG 路站点名不再解析频道名，直接写死“TG频道”。
            # 之前按 "/" 切 via 取第一段：私密频道存的是邀请链接
            # https://t.me/+...，切出来是 "https:"，通知里成了“站点：https:”。
            # 写死格式的来源标签本来就是“TG 频道”，这里也不再拼频道名。
            site_name = (
                "TG频道" if str(source or "").strip().lower() == "tg" else ""
            )
            meta = []
            if size > 0:
                meta.append(format_size(size))
            meta.append(source_label)
            icon = self._media_type_icon(
                "电影" if is_movie else "电视剧"
            )
            text = f"{icon} {headline}" + (f" · {' · '.join(meta)}" if meta else "")
            image = None
            getter = getattr(mediainfo, "get_poster_image", None)
            if callable(getter):
                try:
                    image = getter() or None
                except Exception:  # noqa: BLE001 - 海报取不到不影响通知
                    image = None
            rendered = _render_started_notification_template(
                mediainfo=mediainfo,
                is_movie=is_movie,
                season_no=season_no,
                ep_str=ep_str,
                has_episodes=bool(eps),
                first_episode=eps[0] if eps else None,
                size_text=format_size(size) if size > 0 else "",
                site_name=site_name,
                image=image,
            )
            if rendered is not None:
                template_title, template_text, template_image = rendered
                post_message(
                    mtype=MessageType.Download,
                    ctype=ContentType.DownloadAdded,
                    title=template_title,
                    text=template_text,
                    image=template_image,
                )
                logger.info(
                    f"开始下载通知已发送（MP 模板）：{headline}（{source_label}）"
                )
            else:
                post_message(
                    mtype=MessageType.Download,
                    ctype=ContentType.DownloadAdded,
                    title="【网盘订阅助手】开始下载",
                    text=text,
                    image=image,
                )
                logger.info(f"开始下载通知已发送：{headline}（{source_label}）")
        except Exception as error:  # noqa: BLE001 - 通知失败绝不能打断转存
            logger.warning(f"开始下载通知发送失败：{error}")

    @staticmethod
    def _completed_notification_signature(detail: Dict[str, Any]) -> str:
        """完成通知去重签名：同一转存的详情在两处门各出现一次时只发一条。

        剧集按标题 + 年份 + 季 + 集 + kind；电影按标题 + 年份 + 文件名
        + kind。分支不进签名——MP 整理分支在签名之前就被跳过。
        """
        type_text = str(detail.get("type") or "").strip()
        if type_text == "电影":
            episodes_key = str(detail.get("file_name") or "")
        else:
            episodes = sorted({
                int(episode) for episode in (detail.get("episodes") or [])
                if int(episode or 0) > 0
            })
            episodes_key = ",".join(str(episode) for episode in episodes)
        return "|".join([
            type_text,
            str(detail.get("title") or "").strip(),
            str(detail.get("year") or ""),
            str(detail.get("season") or ""),
            episodes_key,
            str(detail.get("notification_kind") or ""),
        ])

    def send_transfer_completed_notification(
            self, transfer_details: List[Dict[str, Any]], total_count: int = 0
    ) -> None:
        """终态门恢复的普通转存完成通知（v1.26.4），两处调用点共用。

        调用方传进来的已是按媒体聚合好的详情（同步完成块与
        ``_send_finalized_batch`` 都先过 ``_aggregate_transfer_details``）。
        只处理普通转存（transfer / cross_transfer）：洗版详情不在
        这里发（仍走原 ``send_transfer_notification``）；MP 整理分支
        跳过——MP 自己的 OrganizeSuccess 已发，插件再发就重复；其余
        strm 直出分支逐条发 OrganizeSuccess（模板优先、写死兜底，
        见 ``_send_single_completed_notification``）。剧集按集切分
        （见 ``_strm_branch_detail``）：同一批里 MP 分支的集跳过、
        strm 分支的集照常发。同一个详情在窗口内重复进门时只发一条
        （去重状态记在服务实例自己的 ``__dict__`` 里，不经过
        OwnerDelegator 转发）。通知开关 ``_notify`` 关了就不发；
        发送本身失败只记日志，绝不能打断转存。
        """
        post_message = getattr(self, "_post_message", None)
        if not transfer_details or not post_message:
            return
        if not getattr(self, "_notify", False):
            return
        now = time.monotonic()
        seen = self.__dict__.get("_completed_notified")
        if seen is None:
            seen = self.__dict__["_completed_notified"] = {}
        expired = [
            key for key, at in seen.items()
            if now - at > self._COMPLETED_NOTIFICATION_DEDUP_TTL_SECONDS
        ]
        for key in expired:
            seen.pop(key, None)
        for detail in transfer_details:
            try:
                kind = str(detail.get("notification_kind") or "")
                if kind not in {"transfer", "cross_transfer"}:
                    logger.warning(
                        f"丢弃非普通转存完成通知详情：kind={kind}，"
                        f"title={detail.get('title')}"
                    )
                    continue
                effective_detail = self._strm_branch_detail(detail)
                if effective_detail is None:
                    logger.debug(
                        f"跳过 MP 整理分支的完成通知（MP 已发）："
                        f"{detail.get('title')}"
                    )
                    continue
                signature = self._completed_notification_signature(
                    effective_detail
                )
                if signature in seen:
                    logger.debug(
                        f"转存完成通知已发过，跳过重复：{detail.get('title')}"
                    )
                    continue
                self._send_single_completed_notification(effective_detail)
                # 发送成功后才记去重：发送失败时不占位，下一轮还能重发。
                seen[signature] = now
            except Exception as error:  # noqa: BLE001 - 通知失败绝不能打断转存
                logger.warning(f"转存完成通知发送失败：{error}")

    @staticmethod
    def _strm_branch_detail(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """取详情里 strm 直出分支的部分（MP 整理分支由 MP 自己通知）。

        电影：整条详情要么是 MP 分支（返回 None）、要么是 strm 分支
        （原样返回）。剧集：按集的分支标记切分——只有 strm 分支的集
        才进通知；全是 MP 分支时返回 None。返回的详情只含 strm 分支
        的集（集数 / 文件数按这部分算；有逐集体积时按这部分重算，
        没有时大小记未知，不拿整批的和冒充）。
        """
        type_text = str(detail.get("type") or "").strip()
        if type_text == "电影":
            if detail.get("organized_via_mp"):
                return None
            return detail
        episodes = sorted({
            int(episode) for episode in (detail.get("episodes") or [])
            if int(episode or 0) > 0
        })
        organized_map = {
            int(episode): bool(value)
            for episode, value in (
                detail.get("organized_via_mp_episodes") or {}
            ).items()
            if int(episode or 0) > 0
        }
        if organized_map:
            strm_episodes = [
                episode for episode in episodes
                if not organized_map.get(episode, False)
            ]
        elif detail.get("organized_via_mp"):
            strm_episodes = []
        else:
            strm_episodes = list(episodes)
        if not strm_episodes:
            return None
        if strm_episodes == episodes:
            return detail
        effective = dict(detail)
        effective["episodes"] = strm_episodes
        sizes = detail.get("episode_sizes") or {}
        if sizes:
            effective["size"] = sum(
                max(0, int(sizes.get(episode, 0) or 0))
                for episode in strm_episodes
            )
            effective["size_unknown"] = any(
                int(sizes.get(episode, 0) or 0) <= 0
                for episode in strm_episodes
            )
            effective["episode_sizes"] = {
                episode: sizes.get(episode, 0) for episode in strm_episodes
            }
        else:
            # 没有逐集体积时整批的和不能按集拆，大小记未知。
            effective["size"] = 0
            effective["size_unknown"] = True
        return effective

    def _send_single_completed_notification(
            self, detail: Dict[str, Any]
    ) -> None:
        """发一条普通转存的完成通知：模板优先、写死兜底。

        模板路：裸 Message（mtype=Organize、ctype=OrganizeSuccess，
        不填标题正文）走用户资源入库模板；兜底路：标题仿照模板形状
        （“📂 已入库N个文件”+ 标题年份），正文给集数 / 类别 / 大小行。
        """
        post_message = getattr(self, "_post_message", None)
        if not post_message:
            return
        context = _completed_notification_context(detail)
        headline = (
            context["title_year"]
            if context["is_movie"]
            else f"{context['title_year']} {context['season_episode']}".strip()
        )
        rendered = _render_completed_notification_template(detail=detail)
        if rendered is not None:
            template_title, template_text, template_image = rendered
            post_message(
                mtype=MessageType.Organize,
                ctype=ContentType.OrganizeSuccess,
                title=template_title,
                text=template_text,
                image=template_image,
            )
            logger.info(f"转存完成通知已发送（MP 模板）：{headline}")
            return
        text_lines = []
        if context["season_episode"]:
            text_lines.append(f"📺 集数：{context['season_episode']}")
        if context["category"]:
            text_lines.append(f"🎭 类别：{context['category']}")
        text_lines.append(f"💾 大小：{context['total_size']}")
        post_message(
            mtype=MessageType.Organize,
            ctype=ContentType.OrganizeSuccess,
            title=(
                f"📂 已入库{context['file_count']}个文件\n"
                f"{context['title_year']}"
            ),
            text="\n".join(text_lines),
            image=context["image"],
        )
        logger.info(f"转存完成通知已发送：{headline}")

    @staticmethod
    def _media_type_icon(type_text: str) -> str:
        """通知行首图标：用类型区分电影/剧集/动漫。"""
        return {
            "电影": "🎬",
            "电视剧": "📺",
            "剧集": "📺",
            "动漫": "🍿",
            "动画": "🍿",
        }.get(str(type_text or "").strip(), "📦")

    @staticmethod
    def _media_type_name(type_text: str) -> str:
        """通知里展示的中文类型名。"""
        return {
            "电影": "电影",
            "电视剧": "剧集",
            "剧集": "剧集",
            "动漫": "动漫",
            "动画": "动漫",
        }.get(str(type_text or "").strip(), "")

    def _flush_transfer_notifications(self) -> None:
        with self._notification_batch_lock:
            timer = self._notification_batch_timer
            self._notification_batch_timer = None
            if timer and timer is not threading.current_thread():
                timer.cancel()
            transfer_details = self._notification_batch
            self._notification_batch = []
        post_message = getattr(self, "_post_message", None)
        notify = getattr(self, "_notify", False)
        if not transfer_details or not post_message or not notify:
            return
        try:
            self._send_transfer_notification_now(transfer_details)
        except Exception as error:
            logger.warning(f"完成通知发送失败：{error}")

    def _send_transfer_notification_now(
            self, transfer_details: List[Dict[str, Any]]
    ) -> None:
        """只发送洗版完成通知（v1.13.3：转存/跨盘转存完成通知已收敛掉，
        非洗版详情到这里直接丢弃并告警）。"""
        post_message = getattr(self, "_post_message", None)
        if not post_message:
            return
        notification_type = getattr(self, "_notification_type", NotificationType.Plugin)

        kind_config = {
            "upgrade": ("【网盘洗版】洗版完成", "洗版", "notify_template_upgrade"),
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for detail in transfer_details:
            kind = str(detail.get("notification_kind") or "upgrade")
            if kind not in kind_config:
                logger.warning(
                    f"丢弃非洗版完成通知详情：kind={kind}，"
                    f"title={detail.get('title')}"
                )
                continue
            grouped.setdefault(kind, []).append(detail)

        kind_prefix = {
            "upgrade": "⬆️ ",
        }
        for kind, details in grouped.items():
            text_lines = []
            text_cards = []
            first_image = None
            file_count = 0
            total_size = 0
            for detail in details:
                type_text = str(detail.get("type") or "").strip()
                icon = self._media_type_icon(type_text)
                meta = []
                if type_text:
                    meta.append(self._media_type_name(type_text))
                size = int(detail.get("size") or 0)
                size_unknown = bool(detail.get("size_unknown"))
                if size > 0 and not size_unknown:
                    meta.append(format_size(size))
                source = str(detail.get("source") or "").strip()
                if source:
                    meta.append(source)
                if type_text == "电影":
                    title = detail.get("title", "未知")
                    year = detail.get("year", "")
                    headline = f"{title} ({year})".replace(" ()", "")
                    file_count += 1
                else:
                    title = detail.get("title", "未知")
                    season = max(1, int(detail.get("season") or 1))
                    episodes = sorted(detail.get("episodes") or [])
                    if episodes:
                        file_count += len(episodes)
                        if len(episodes) <= 5:
                            ep_str = ", ".join(f"E{episode:02d}" for episode in episodes)
                        else:
                            ep_str = (
                                f"E{episodes[0]:02d}-E{episodes[-1]:02d} "
                                f"共{len(episodes)}集"
                            )
                        headline = f"{title} S{season:02d} {ep_str}".strip()
                    else:
                        file_count += 1
                        headline = f"{title} S{season:02d}".strip()
                total_size += max(0, size)
                prefix = kind_prefix.get(kind, "")
                line = f"{prefix}{icon} {headline}"
                if meta:
                    line = f"{line} · {' · '.join(meta)}"
                text_lines.append(line)
                card_lines = [f"{prefix}{icon} {headline}"]
                field_values = (
                    ("类型", self._media_type_name(type_text)),
                    ("大小", format_size(size) if size > 0 and not size_unknown else ""),
                    ("来源", source),
                )
                for field_name, field_value in field_values:
                    if field_value:
                        card_lines.append(f"{field_name}：{field_value}")
                text_cards.append("\n".join(card_lines))
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            # 任一明细体积未知时注明（汇总数只是已知部分的和，不是全量）。
            if any(detail.get("size_unknown") for detail in details):
                text_lines.append("部分集数体积未知")
                text_cards.append("部分集数体积未知")
            if len(text_lines) > 10:
                text_lines = text_lines[:10]
                text_lines.append(f"... 等共 {len(details)} 项")
            if len(text_cards) > 10:
                text_cards = text_cards[:10]
                text_cards.append(f"… 等共 {len(details)} 项")
            notification_title, action, template_key = kind_config[kind]
            total_size_text = format_size(total_size) if total_size > 0 else ""
            fallback_text = (
                f"本次共{action} {file_count} 个文件"
                + (f"，合计 {total_size_text}" if total_size_text else "")
                + "\n\n"
                + "\n".join(text_lines)
            )
            templates = getattr(self, "_notify_templates", None) or {}
            post_message(
                mtype=notification_type,
                title=notification_title,
                text=render_template(
                    resolve_template(templates, template_key),
                    fallback_text,
                    action=action,
                    count=file_count,
                    total_size=total_size_text,
                    labels=list(text_lines),
                    cards=list(text_cards),
                    items=list(details),
                ),
                image=first_image,
            )
            logger.info(
                f"{action}完成通知已发送：{file_count} 个文件，"
                f"{len(details)} 个媒体项"
            )
