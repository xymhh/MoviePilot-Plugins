"""同步任务完成通知聚合与推送服务。"""

import copy
import threading
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import ContentType, MessageType, NotificationType

from ...core import OwnerDelegator, render_template, resolve_template
from ...drive.common import format_size


class SyncNotificationService(OwnerDelegator):
    """负责洗版完成后的通知合并、静默窗口等待与聚合发送（v1.13.3：
    转存完成通知已收敛掉，入库通知由 MP 自己发）。"""

    _NOTIFICATION_BATCH_WINDOW_SECONDS = 3.0

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
        """转存真正开始时补一条 MP 原生形态的开始下载通知（v1.25.0 Issue A）。

        MP 自己的开始下载通知是 ``Message(mtype=Download,
        ctype=DownloadAdded)``，由用户配的 DownloadAdded 模板渲染；插件的
        三条路（订阅自动跑 / TG 监控 / 手动转存）走内部云盘直传，从不经过
        MP 下载链，所以一条都不会发。这里在转存入口（入库管线过判重门后、
        离线提交被网盘接受后）补发一条：

        * ``mtype=MessageType.Download``：与原生通知走同一路由开关
          （``get_notification_switch(mtype)``），同一渠道投递；
        * ``ctype=ContentType.DownloadAdded``：消息上带原生内容类型标识；
        * 标题/正文由插件直接给出（不走用户模板渲染——模板渲染要求
          title/text 为空且带齐 meta/mediainfo/torrentinfo 上下文，云盘
          转存入口没有真实 TorrentInfo，渲染失败会留下一条空通知；
          见 ``MessageTemplateHelper.render`` 的 ``is_instance_valid``
          短路）。形态差异：标题固定为「开始下载」，不随用户模板变。

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
            if str(media_type or "").strip().lower() == "movie":
                headline = f"{title} ({year})".replace(" ()", "")
            else:
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
            if str(source or "").strip().lower() == "tg":
                channel = str(via or "").strip()
                if channel.startswith("tg:"):
                    channel = channel[3:]
                channel = channel.split("/")[0].strip()
                if channel:
                    source_label = f"TG 频道 {channel}"
            meta = []
            if size > 0:
                meta.append(format_size(size))
            meta.append(source_label)
            icon = self._media_type_icon(
                "电影" if str(media_type or "").strip().lower() == "movie" else "电视剧"
            )
            text = f"{icon} {headline}" + (f" · {' · '.join(meta)}" if meta else "")
            image = None
            getter = getattr(mediainfo, "get_poster_image", None)
            if callable(getter):
                try:
                    image = getter() or None
                except Exception:  # noqa: BLE001 - 海报取不到不影响通知
                    image = None
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
