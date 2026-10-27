"""同步任务完成通知聚合与推送服务。"""

import copy
import threading
from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import NotificationType

from ...core import OwnerDelegator, render_template, resolve_template
from ...drive.common import format_size


class SyncNotificationService(OwnerDelegator):
    """负责转存与洗版完成后的通知合并、静默窗口等待与聚合发送。"""

    _NOTIFICATION_BATCH_WINDOW_SECONDS = 3.0

    def __init__(self, owner=None):
        super().__init__(owner)
        self._notification_batch_lock = threading.RLock()
        self._notification_batch: List[Dict[str, Any]] = []
        self._notification_batch_timer: Optional[threading.Timer] = None

    def update_notification_config(
            self,
            notify: bool,
            notification_type: NotificationType,
            **kwargs,
    ) -> None:
        """更新通知开关与渠道类型。"""
        if hasattr(self, "_notify"):
            self._notify = bool(notify)
        if hasattr(self, "_notification_type"):
            self._notification_type = notification_type

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

    def _history_record_for_detail(
            self, detail: Dict[str, Any], records: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """按文件名在同一条历史里找记录，用来补全通知里缺失的大小与来源。"""
        file_name = str(detail.get("file_name") or "").strip()
        if not file_name:
            return None
        matched = None
        for record in records or []:
            if str(record.get("status") or "") != "成功":
                continue
            names = {
                str(record.get("file_name") or "").strip(),
                str(record.get("source_file_name") or "").strip(),
            }
            if file_name in names:
                matched = record
        return matched

    def _enrich_transfer_details(
            self, transfer_details: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """发送前补全大小与网盘来源：详情自带值优先，缺失时回查历史记录。

        转存链路有多条，个别链路拿到 detail 时历史尚未落库或文件对象为空，
        导致通知只剩「类型」。这里统一兜底，避免模板正确但字段为空。
        """
        get_data = getattr(self, "_get_data", None)
        records: Optional[List[Dict[str, Any]]] = None
        for detail in transfer_details:
            try:
                size = int(detail.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            source = str(detail.get("source") or "").strip()
            if size > 0 and source:
                continue
            if records is None:
                try:
                    records = list(get_data("history") or []) if get_data else []
                except Exception:
                    records = []
            record = self._history_record_for_detail(detail, records)
            if not record:
                continue
            if size <= 0:
                try:
                    detail["size"] = int(record.get("file_size") or 0)
                except (TypeError, ValueError):
                    detail["size"] = 0
            if not source:
                detail["source"] = self._detail_source_text(record)
        return transfer_details

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
        """按普通转存、跨盘转存和洗版分别发送聚合后的完成通知。"""
        post_message = getattr(self, "_post_message", None)
        if not post_message:
            return
        notification_type = getattr(self, "_notification_type", NotificationType.Plugin)
        transfer_details = self._enrich_transfer_details(transfer_details)

        kind_config = {
            "transfer": ("【网盘订阅助手】转存完成", "转存", "notify_template_transfer"),
            "cross_transfer": ("【网盘订阅助手】跨盘转存完成", "跨盘转存", "notify_template_cross_transfer"),
            "upgrade": ("【网盘洗版】洗版完成", "洗版", "notify_template_upgrade"),
        }
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for detail in transfer_details:
            kind = str(detail.get("notification_kind") or "transfer")
            grouped.setdefault(kind if kind in kind_config else "transfer", []).append(detail)

        kind_prefix = {
            "transfer": "",
            "cross_transfer": "🔀 ",
            "upgrade": "⬆️ ",
        }
        for kind, details in grouped.items():
            text_lines = []
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
                if size > 0:
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
                line = f"{kind_prefix.get(kind, '')}{icon} {headline}"
                if meta:
                    line = f"{line} · {' · '.join(meta)}"
                text_lines.append(line)
                if not first_image and detail.get("image"):
                    first_image = detail.get("image")
            if len(text_lines) > 10:
                text_lines = text_lines[:10]
                text_lines.append(f"... 等共 {len(details)} 项")
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
                    items=list(details),
                ),
                image=first_image,
            )
            logger.info(
                f"{action}完成通知已发送：{file_count} 个文件，"
                f"{len(details)} 个媒体项"
            )
