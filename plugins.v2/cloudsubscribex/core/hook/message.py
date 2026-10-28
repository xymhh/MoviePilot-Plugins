"""平台消息中的资源链接与片名订阅路由接管。"""

import inspect
import re
from functools import wraps
from threading import Lock, Thread
from typing import Any, Callable, Dict, Optional, Tuple

from app.chain.message import MessageChain
from app.log import logger

from ..delegation import OwnerDelegator
from ..pending_titles import PendingTitleStore, get_pending_title_store, media_type_value
from ...search.types import resource_type_from_url


class MessageRoutingHook(OwnerDelegator):
    """在全局智能体路由前接管资源链接；纯片名只登记候选，等用户选定后接入订阅。"""

    _patch_lock = Lock()
    _original: Optional[Callable] = None
    _wrapped: Optional[Callable] = None
    _signature: Optional[inspect.Signature] = None
    _active: Optional["MessageRoutingHook"] = None

    # 片名文本判定：超长、含路径分隔或换行的一律交回平台处理
    _TITLE_MAX_LENGTH = 40
    _TITLE_REJECT = re.compile(r"[/\\\n\r\t]")
    _TITLE_LETTER = re.compile(r"[A-Za-z\u4e00-\u9fff]")
    _TITLE_QUESTION = ("?", "？")
    _LINK_MARKERS = (
        "http://", "https://", "magnet:", "ed2k://", "thunder://", "www."
    )

    @staticmethod
    def _channel_value(channel: Any) -> str:
        return str(getattr(channel, "value", channel) or "").strip().lower()

    def _message_payload(self, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self._enabled or not self._direct_transfer_enabled:
            return None
        channel = arguments.get("channel")
        text = str(arguments.get("text") or "").strip()
        if not text or text.startswith(("/", "CALLBACK:")):
            return None
        if (
                arguments.get("images")
                or arguments.get("files")
                or arguments.get("audio_refs")
                or arguments.get("has_audio_input")
        ):
            return None
        links = [
            link for link in self.extract_resource_links(text)
            if resource_type_from_url(link)
        ]
        if not links:
            return None
        return {
            "channel": channel,
            "source": arguments.get("source"),
            "user": arguments.get("userid"),
            "userid": arguments.get("userid"),
            "username": arguments.get("username"),
            "message_id": arguments.get("original_message_id"),
            "chat_id": arguments.get("original_chat_id"),
            "reply_to_message_id": arguments.get("reply_to_message_id"),
            "text": text,
            "links": links,
        }

    def _title_payload(self, arguments: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """判断消息是否为「纯片名」，返回异步处理所需的上下文。"""
        if not self._enabled or not getattr(self, "_title_subscribe_enabled", True):
            return None
        if arguments.get("callback_data"):
            return None
        text = str(arguments.get("text") or "").strip()
        if not text or text.startswith(("/", "CALLBACK:")):
            return None
        if (
                arguments.get("images")
                or arguments.get("files")
                or arguments.get("audio_refs")
                or arguments.get("has_audio_input")
        ):
            return None
        if len(text) > self._TITLE_MAX_LENGTH or self._TITLE_REJECT.search(text):
            return None
        if any(question in text for question in self._TITLE_QUESTION):
            return None
        lowered = text.lower()
        if any(marker in lowered for marker in self._LINK_MARKERS):
            return None
        if not self._TITLE_LETTER.search(text):
            return None
        return {
            "channel": arguments.get("channel"),
            "source": arguments.get("source"),
            "user": arguments.get("userid"),
            "userid": arguments.get("userid"),
            "username": arguments.get("username"),
            "message_id": arguments.get("original_message_id"),
            "chat_id": arguments.get("original_chat_id"),
            "reply_to_message_id": arguments.get("reply_to_message_id"),
            "text": text,
        }

    def _try_handle(self, arguments: Dict[str, Any]) -> bool:
        """接管消息中的资源链接；纯片名只登记候选，消息原样交回平台。

        1.6.27 起纯片名不再直接建订阅：先让平台给出候选列表，用户在候选里
        选定后平台会走「候选选定 → 搜索资源」（1.6.28 起该入口被订阅钩子接管），
        由搜索钩子命中登记记录再创建订阅。
        """
        payload = self._message_payload(arguments)
        if not payload:
            title_payload = self._title_payload(arguments)
            if title_payload:
                self._remember_title_candidate(title_payload)
            return False
        try:
            Thread(
                target=self.handle_telegram_links,
                args=(payload,),
                daemon=True,
                name="cloudsubscribe-message-hook",
            ).start()
        except Exception as error:
            logger.error(f"平台消息接管线程启动失败：{error}")
            self._post_command_message(
                payload,
                "【网盘订阅】资源处理失败",
                "无法启动资源识别任务，请稍后重试。",
            )
        return True

    def install(self) -> None:
        """安装消息路由包装，并在插件重载时切换到最新实例。"""
        if not self._enabled or not (
                self._direct_transfer_enabled
                or getattr(self, "_title_subscribe_enabled", True)
        ):
            self.close()
            return
        cls = type(self)
        with cls._patch_lock:
            cls._active = self
            if cls._original is not None:
                return
            original = MessageChain._handle_message_core
            signature = inspect.signature(original)
            cls._original = original
            cls._signature = signature

            @wraps(original)
            def wrapped(chain_self, *args, **kwargs):
                active = cls._active
                if active:
                    try:
                        bound = signature.bind_partial(chain_self, *args, **kwargs)
                        if active._try_handle(dict(bound.arguments)):
                            return False
                    except Exception as error:
                        logger.error(f"平台消息接管失败：{error}")
                return original(chain_self, *args, **kwargs)

            MessageChain._handle_message_core = wrapped
            cls._wrapped = wrapped

    def close(self) -> None:
        """卸载当前实例，并在没有接管方时恢复平台原方法。"""
        cls = type(self)
        with cls._patch_lock:
            if cls._active is self:
                cls._active = None
            original = cls._original
            if original and MessageChain._handle_message_core is cls._wrapped:
                MessageChain._handle_message_core = original
            cls._original = None
            cls._wrapped = None
            cls._signature = None
        try:
            self._pending_store().clear()
        except Exception as error:
            logger.debug(f"清理片名候选登记表失败：{error}", exc_info=True)

    @staticmethod
    def _as_media_type(value: Any) -> Optional[Any]:
        """把任意媒体类型取值规范为 MoviePilot 的 MediaType。"""
        from app.schemas.types import MediaType

        text = str(getattr(value, "value", value) or "").strip().lower()
        if text == MediaType.MOVIE.value.lower():
            return MediaType.MOVIE
        if text == MediaType.TV.value.lower():
            return MediaType.TV
        return None

    def _resolve_title_media(
            self, text: str
    ) -> Optional[Tuple[Any, Any, Optional[int]]]:
        """把纯片名识别为媒体信息；识别不出或非影视时返回 None。"""
        from app.core.metainfo import MetaInfo
        from app.schemas.types import MediaType

        meta = MetaInfo(text)
        meta_type = self._as_media_type(getattr(meta, "type", None))
        mediainfo = self._sync_handler._recognize_media_once(
            ("message_title", text, meta_type.value if meta_type else ""),
            meta=meta,
            mtype=meta_type,
            cache=True,
        )
        media_type = (
            self._as_media_type(getattr(mediainfo, "type", None)) if mediainfo else None
        )
        if not mediainfo or not media_type:
            return None
        season = None
        if media_type == MediaType.TV:
            parsed = getattr(meta, "begin_season", None) or getattr(meta, "season", None)
            try:
                season = int(parsed) if parsed else None
            except (TypeError, ValueError):
                season = None
            if season is not None and season <= 0:
                season = None
        return mediainfo, media_type, season

    @staticmethod
    def _find_title_subscribe(mediainfo: Any, media_type: Any, season: Optional[int]):
        """按 TMDB 身份查询是否已有订阅，避免重复建订。"""
        from app.db.subscribe_oper import SubscribeOper
        from app.schemas.types import MediaType

        from ..media import get_subscribe_by_media

        return get_subscribe_by_media(
            SubscribeOper(),
            media_type=media_type.value,
            season=season if media_type == MediaType.TV else None,
            media=mediainfo,
        )

    @staticmethod
    def _create_title_subscribe(
            mediainfo: Any,
            media_type: Any,
            season: Optional[int],
            title: str,
            payload: Dict[str, Any],
    ) -> Tuple[Optional[int], str]:
        """按识别结果创建一条订阅，成功时返回订阅 ID。"""
        from app.chain.subscribe import SubscribeChain
        from app.schemas.types import MediaType

        from ..media import media_identity

        source, media_id = media_identity(mediainfo)
        subscribe_id, message = SubscribeChain().add(
            title=title,
            year=str(getattr(mediainfo, "year", "") or ""),
            mtype=media_type,
            media_source=source,
            media_id=media_id,
            season=season if media_type == MediaType.TV else None,
            channel=payload.get("channel"),
            source=payload.get("source"),
            userid=str(payload.get("userid") or "") or None,
            username=str(payload.get("username") or "") or None,
            exist_ok=True,
            message=False,
        )
        return (int(subscribe_id) if subscribe_id else None), str(message or "")

    def _dispatch_title_search(self, subscribe_id: int, state: str) -> None:
        """把订阅立刻排进插件自己的网盘搜索队列，无需等平台定时任务。"""
        if not subscribe_id:
            return
        try:
            queue = getattr(self, "queue_subscribe_search", None)
            if not callable(queue):
                return
            queue(subscribe_id=subscribe_id, subscribe_state=state)
        except Exception as error:
            logger.warning(f"订阅搜索排队失败：subscribe_id={subscribe_id}，{error}")

    @staticmethod
    def _pending_store() -> PendingTitleStore:
        """共享的片名登记表。"""
        return get_pending_title_store()

    def _remember_title_candidate(self, payload: Dict[str, Any]) -> None:
        """登记纯片名候选，并在后台补齐 TMDB 身份，等用户选定后的搜索来消费。"""
        text = str(payload.get("text") or "").strip()
        try:
            record = self._pending_store().remember(
                text=text,
                userid=payload.get("userid"),
                username=payload.get("username"),
                channel=payload.get("channel"),
                source=payload.get("source"),
                chat_id=payload.get("chat_id"),
                message_id=payload.get("message_id"),
            )
        except Exception as error:
            logger.warning(f"片名候选登记失败：{text}，{error}")
            return
        logger.info(f"已登记片名候选「{text}」，交回平台给出候选列表，等用户选定后再接入订阅")
        try:
            Thread(
                target=self._enrich_title_candidate,
                args=(record.get("id"), text),
                daemon=True,
                name="cloudsubscribe-title-candidate",
            ).start()
        except Exception as error:
            logger.debug(f"片名候选识别线程启动失败：{text}，{error}", exc_info=True)

    @staticmethod
    def _tmdb_id_of(media: Any) -> Optional[int]:
        """读取媒体对象的 TMDB ID（惰性导入，避免加载期依赖 app 运行时）。"""
        if media is None:
            return None
        from ..media import tmdb_id_of

        try:
            return tmdb_id_of(media)
        except Exception:
            return None

    def _enrich_title_candidate(self, record_id: Optional[str], text: str) -> None:
        """后台识别片名并补齐登记记录的身份，识别不出就保持只按片名比对。"""
        if not record_id:
            return
        try:
            resolution = self._resolve_title_media(text)
        except Exception as error:
            logger.debug(f"片名候选识别失败：{text}，{error}")
            return
        if not resolution:
            return
        mediainfo, media_type, season = resolution
        try:
            self._pending_store().enrich(
                record_id,
                title=str(getattr(mediainfo, "title", "") or text),
                year=str(getattr(mediainfo, "year", "") or ""),
                mtype=media_type_value(media_type),
                season=season,
                tmdbid=self._tmdb_id_of(mediainfo),
            )
        except Exception as error:
            logger.debug(f"片名候选身份补齐失败：{text}，{error}", exc_info=True)

    def _resolve_selected_media(
            self, identity: Dict[str, Any]
    ) -> Optional[Tuple[Any, Any, Optional[int]]]:
        """按平台搜索使用的身份识别媒体（优先复用已知结果/已识别实例）。"""
        from app.schemas.types import MediaType

        season = identity.get("season")
        try:
            season = int(season) if season else None
        except (TypeError, ValueError):
            season = None
        mediainfo = identity.get("mediainfo")
        if mediainfo is not None:
            media_type = self._as_media_type(getattr(mediainfo, "type", None))
            if media_type:
                return mediainfo, media_type, season if media_type == MediaType.TV else None
        media_id = identity.get("media_id")
        meta = identity.get("meta")
        if not media_id and meta is None:
            return None
        mediainfo = self._sync_handler._recognize_media_once(
            (
                "selected_title",
                str(identity.get("media_source") or ""),
                str(media_id or ""),
                str(identity.get("title") or ""),
            ),
            meta=meta,
            mtype=identity.get("mtype_raw"),
            media_source=identity.get("media_source"),
            media_id=media_id,
            cache=True,
        )
        media_type = (
            self._as_media_type(getattr(mediainfo, "type", None)) if mediainfo else None
        )
        if not mediainfo or not media_type:
            return None
        return mediainfo, media_type, season if media_type == MediaType.TV else None

    def _handle_selected_title(
            self,
            record: Dict[str, Any],
            identity: Dict[str, Any],
    ) -> None:
        """用户选定后的真实搜索：按选定媒体建订阅并排队一次网盘搜索。"""
        identity = dict(identity or {})
        payload = {
            "channel": record.get("channel"),
            "source": record.get("source"),
            "user": record.get("userid"),
            "userid": record.get("userid"),
            "username": record.get("username"),
            "message_id": record.get("message_id"),
            "chat_id": record.get("chat_id"),
            "text": record.get("text"),
        }
        text = str(
            identity.get("title") or record.get("title") or record.get("text") or ""
        ).strip()
        try:
            resolution = self._resolve_selected_media(identity)
            if not resolution:
                resolution = self._resolve_title_media(text)
        except Exception as error:
            logger.error(f"选定片名识别异常：{text}，{error}")
            return
        if not resolution:
            logger.info(f"选定片名未识别为影视媒体，跳过订阅创建：{text}")
            return
        mediainfo, media_type, season = resolution
        title = str(getattr(mediainfo, "title", "") or text).strip() or text
        logger.info(f"片名候选已被平台搜索命中，按选定结果接入订阅：{title}")
        self._ensure_title_subscribe(mediainfo, media_type, season, title, payload)

    def _ensure_title_subscribe(
            self,
            mediainfo: Any,
            media_type: Any,
            season: Optional[int],
            title: str,
            payload: Dict[str, Any],
    ) -> None:
        """按识别结果复用或创建订阅，立刻排队网盘搜索并回执。"""
        try:
            subscribe = self._find_title_subscribe(mediainfo, media_type, season)
        except Exception as error:
            logger.warning(f"查询既有订阅失败：{title}，{error}")
            subscribe = None
        if subscribe:
            self._dispatch_title_search(int(getattr(subscribe, "id", 0) or 0), "R")
            self._post_command_message(
                payload,
                "【网盘订阅】已在订阅中",
                f"《{title}》已在订阅列表中，已触发一次网盘资源搜索。",
            )
            return
        try:
            subscribe_id, message = self._create_title_subscribe(
                mediainfo, media_type, season, title, payload
            )
        except Exception as error:
            logger.error(f"创建订阅失败：{title}，{error}", exc_info=True)
            self._post_command_message(
                payload,
                "【网盘订阅】创建订阅失败",
                f"《{title}》创建订阅失败：{error}",
            )
            return
        if not subscribe_id:
            self._post_command_message(
                payload,
                "【网盘订阅】创建订阅失败",
                f"《{title}》创建订阅失败：{message or '未知原因'}",
            )
            return
        self._dispatch_title_search(int(subscribe_id), "N")
        season_text = f" 第 {season} 季" if season else ""
        self._post_command_message(
            payload,
            "【网盘订阅】已创建订阅",
            f"已按你在平台候选中选定的《{title}》{season_text}创建订阅，"
            "正在搜索网盘资源，找到后会自动转存入库。",
        )
