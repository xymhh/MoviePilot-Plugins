"""订阅与平台搜索钩子。"""

import contextvars
import inspect
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from threading import Thread
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.chain.subscribe import SubscribeChain
from app.db.subscribe_oper import SubscribeOper
from app.log import logger

from .. import OwnerDelegator
from ..pending_titles import get_pending_title_store, media_type_value

_season_expansion_suppressed = contextvars.ContextVar(
    "cloudsubscribe_season_expansion_suppressed", default=0
)


@contextmanager
def suppress_season_expansion():
    token = _season_expansion_suppressed.set(_season_expansion_suppressed.get() + 1)
    try:
        yield
    finally:
        _season_expansion_suppressed.reset(token)


def _is_tv_media_type(value: Any) -> bool:
    """判断媒体类型是否为电视剧（兼容 MediaType 枚举与中英文文本值）。"""
    return str(value or "").strip().lower() in {"tv", "电视剧", "media_type.tv"}


# 平台「用户点选候选 → 搜索资源」的入口类：v3.1.x 起由 InteractionChain 改名为
# MediaInteractionChain（app/chain/interaction.py），两个名字都要认，防止再次改名后
# 静默失效（1.6.28~1.6.33 期间点选接入器一直没装上，就是因为只认旧名字）。
_INTERACTION_CHAIN_NAMES = ("MediaInteractionChain", "InteractionChain")
_INTERACTION_METHOD = "_search_media_resources"


def resolve_interaction_chain_class() -> Tuple[Optional[Any], str]:
    """定位承载「点选候选 → 搜索资源」的平台类。

    返回 ``(类, 说明)``；找不到时类为 None，说明里带上失败原因，便于日志排查。
    """
    try:
        from app.chain import interaction as interaction_module
    except Exception as error:
        return None, f"平台无 app.chain.interaction：{error}"
    for name in _INTERACTION_CHAIN_NAMES:
        candidate = getattr(interaction_module, name, None)
        if callable(getattr(candidate, _INTERACTION_METHOD, None)):
            return candidate, name
    for name, member in vars(interaction_module).items():
        if isinstance(member, type) and callable(
                getattr(member, _INTERACTION_METHOD, None)
        ):
            return member, f"{name}（按方法扫描命中）"
    return None, "app.chain.interaction 里找不到带 %s 的类" % _INTERACTION_METHOD


def _is_media_selection_frame(function_name: str) -> bool:
    """调用栈里是否出现平台「点选候选」的处理函数。"""
    return function_name in {"_search_media_resources", "_handle_media_selection"}


class SubscriptionSearchHook(OwnerDelegator):
    """按接管时段分流订阅搜索，并精确恢复原函数。"""

    _JOB_IDS = ("subscribe_search", "new_subscribe_search", "subscribe_refresh")
    _PLATFORM_SEARCH_METHODS = {
        "search_by_id": "sync",
        "search_by_title": "sync",
        "async_search_by_id": "async",
        "async_search_by_title": "async",
        "async_search_by_title_stream": "stream",
        "async_search_by_id_stream": "stream",
        # 平台「候选选定 → 搜索资源」走的是 process（app/chain/interaction.py 的
        # _search_media_resources），不在上面几个公开入口里，单独按 pending 语义挂接：
        # 只有真的消费到用户点选的那条片名候选才拦截，其余调用原样透传。
        "process": "pending",
        "async_process": "pending_async",
        "async_process_stream": "pending_stream",
    }
    # 「点选候选」那次 process 调用只会带 mediainfo/no_exists；订阅搜索、扫描等还会带
    # 这些过滤/站点参数，用它们把两类调用区分开，避免点选兜底误吃订阅搜索。
    _SELECTION_EXCLUSIVE_KEYS = (
        "keyword",
        "sites",
        "rule_groups",
        "filter_params",
        "custom_words",
        "candidate_filter",
        "meta",
        "metainfo",
        "media_id",
        "media_source",
    )

    def _install_subscribe_search_takeover(self) -> None:
        if not self._enabled:
            return
        self._install_platform_search_block()
        self._install_interaction_media_hook()
        self._install_subscribe_chain_takeover()
        self._install_subscribe_add_expansion()
        try:
            from app.scheduler import Scheduler

            scheduler = (
                Scheduler.get_existing_instance()
                if hasattr(Scheduler, "get_existing_instance")
                else Scheduler()
            )
            if scheduler is None:
                logger.debug("调度器尚未就绪，等待平台注册后安装订阅接管")
                return
            jobs = getattr(scheduler, "_jobs", None) or {}
            newly_installed = []
            for job_id in self._JOB_IDS:
                job = jobs.get(job_id)
                if not job:
                    continue
                current = job.get("func")
                if getattr(current, "__self__", None) is self:
                    continue
                self._subscribe_search_originals.setdefault(job_id, current)
                job["func"] = (
                    self._dispatch_subscribe_refresh
                    if job_id == "subscribe_refresh"
                    else self._dispatch_subscribe_search
                )
                newly_installed.append(job_id)
            if newly_installed:
                logger.info(f"订阅搜索路由已接管：{', '.join(newly_installed)}")
            else:
                logger.debug("订阅搜索路由已保持接管")
        except Exception as error:
            logger.warning(f"安装订阅搜索路由失败：{error}")

    def _restore_subscribe_search_takeover(self) -> None:
        self._restore_platform_search_block()
        self._restore_interaction_media_hook()
        self._restore_subscribe_chain_takeover()
        originals = dict(self._subscribe_search_originals or {})
        if not originals:
            return
        try:
            from app.scheduler import Scheduler

            scheduler = (
                Scheduler.get_existing_instance()
                if hasattr(Scheduler, "get_existing_instance")
                else Scheduler()
            )
            jobs = getattr(scheduler, "_jobs", None) if scheduler else {}
            jobs = jobs or {}
            for job_id, original in originals.items():
                job = jobs.get(job_id)
                current = job.get("func") if job else None
                if job and getattr(current, "__self__", None) is self:
                    job["func"] = original
        except Exception as error:
            logger.warning(f"恢复订阅搜索路由失败：{error}")
        finally:
            self._subscribe_search_originals = {}

    def _install_platform_search_block(self) -> None:
        """挂接平台公开资源搜索入口，在 block 策略生效时提前终止。"""
        try:
            from app.chain.search import SearchChain

            installed = []
            for method_name, method_type in self._PLATFORM_SEARCH_METHODS.items():
                current = getattr(SearchChain, method_name, None)
                if not callable(current):
                    logger.warning(f"搜索入口不存在：SearchChain.{method_name}")
                    continue
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    continue

                original = getattr(
                    current, "__cloudsubscribe_original__", current
                )
                self._platform_search_originals.setdefault(method_name, original)
                wrapper = self._create_platform_search_wrapper(
                    method_name=method_name,
                    method_type=method_type,
                    original=original,
                )
                setattr(SearchChain, method_name, wrapper)
                installed.append(method_name)
            if installed:
                logger.info(
                    "平台搜索阻止器已安装：" + ", ".join(installed)
                )
        except Exception as error:
            logger.warning(f"安装平台搜索阻止器失败：{error}")

    def _restore_platform_search_block(self) -> None:
        """仅恢复当前插件实例安装的平台搜索方法。"""
        originals = dict(self._platform_search_originals or {})
        if not originals:
            return
        try:
            from app.chain.search import SearchChain

            for method_name, original in originals.items():
                current = getattr(SearchChain, method_name, None)
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    setattr(SearchChain, method_name, original)
        except Exception as error:
            logger.warning(f"恢复平台搜索入口失败：{error}")
        finally:
            self._platform_search_originals = {}

    def _create_platform_search_wrapper(
            self,
            method_name: str,
            method_type: str,
            original: Callable,
    ) -> Callable:
        owner = self

        if method_type == "stream":
            @wraps(original)
            async def stream_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    owner._dispatch_pending_title_search(original, (chain, *args), kwargs)
                    yield {
                        "type": "done",
                        "stage": "done",
                        "value": 100,
                        "text": "网盘订阅接管中，已阻止平台搜索",
                        "items": [],
                        "total_items": 0,
                    }
                    return
                async for event in original(chain, *args, **kwargs):
                    yield event

            wrapper = stream_wrapper
        elif method_type == "async":
            @wraps(original)
            async def async_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    owner._dispatch_pending_title_search(original, (chain, *args), kwargs)
                    return []
                return await original(chain, *args, **kwargs)

            wrapper = async_wrapper
        else:
            @wraps(original)
            def sync_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked():
                    owner._dispatch_pending_title_search(original, (chain, *args), kwargs)
                    return []
                return original(chain, *args, **kwargs)

            wrapper = sync_wrapper

        if method_type in {"pending", "pending_async", "pending_stream"}:
            wrapper = self._create_pending_wrapper(original, method_type)

        wrapper.__cloudsubscribe_owner__ = self
        wrapper.__cloudsubscribe_original__ = original
        return wrapper

    def _create_pending_wrapper(self, original: Callable, method_type: str) -> Callable:
        """按「命中片名候选才拦截」的语义包装平台搜索入口（process 系列）。

        与 ``sync``/``async``/``stream`` 的差别：后者在接管态下一律拦截平台搜索，这里
        只在真的消费到用户点选的那条片名候选时才拦截，其余调用原样透传，避免影响订阅
        搜索、扫描等其它同样调用 ``process`` 的路径。
        """
        owner = self

        if method_type == "pending_stream":
            @wraps(original)
            async def pending_stream_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked() \
                        and owner._dispatch_pending_title_search(
                            original, (chain, *args), kwargs
                        ):
                    yield {
                        "type": "done",
                        "stage": "done",
                        "value": 100,
                        "text": "网盘订阅接管中，已阻止平台搜索",
                        "items": [],
                        "total_items": 0,
                    }
                    return
                async for event in original(chain, *args, **kwargs):
                    yield event

            return pending_stream_wrapper

        if method_type == "pending_async":
            @wraps(original)
            async def pending_async_wrapper(chain, *args, **kwargs):
                if owner._is_platform_search_blocked() \
                        and owner._dispatch_pending_title_search(
                            original, (chain, *args), kwargs
                        ):
                    return []
                return await original(chain, *args, **kwargs)

            return pending_async_wrapper

        @wraps(original)
        def pending_wrapper(chain, *args, **kwargs):
            if owner._is_platform_search_blocked() \
                    and owner._dispatch_pending_title_search(
                        original, (chain, *args), kwargs
                    ):
                return []
            return original(chain, *args, **kwargs)

        return pending_wrapper

    # ------------------------------------------------ 媒体点选接入（1.6.28）
    def _install_interaction_media_hook(self) -> None:
        """挂接平台「候选选定 → 搜索资源」入口，点选后不再走平台站点搜索。"""
        target, reason = resolve_interaction_chain_class()
        if target is None:
            logger.warning(f"媒体点选接管器未安装：{reason}")
            return
        method_name = _INTERACTION_METHOD
        current = getattr(target, method_name, None)
        if not callable(current):
            logger.warning(f"媒体点选接管器未安装：{reason} 里没有 {method_name}")
            return
        self._interaction_chain_class = target
        if getattr(current, "__cloudsubscribe_owner__", None) is self:
            logger.debug(f"媒体点选接管器已保持接管：{target.__name__}.{method_name}")
            return
        original = getattr(current, "__cloudsubscribe_original__", current)
        originals = getattr(self, "_interaction_originals", None)
        if originals is not None:
            originals.setdefault(method_name, original)
        setattr(
            target,
            method_name,
            self._create_interaction_media_wrapper(original),
        )
        logger.info(f"媒体点选接管器已安装：{target.__name__}.{method_name}")

    def _restore_interaction_media_hook(self) -> None:
        originals = dict(getattr(self, "_interaction_originals", None) or {})
        if not originals:
            return
        try:
            target = getattr(self, "_interaction_chain_class", None)
            if target is None:
                target, reason = resolve_interaction_chain_class()
                if target is None:
                    logger.warning(f"恢复媒体点选接管器失败：{reason}")
                    return
            for method_name, original in originals.items():
                current = getattr(target, method_name, None)
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    setattr(target, method_name, original)
        except Exception as error:
            logger.warning(f"恢复媒体点选接管器失败：{error}")
        finally:
            if hasattr(self, "_interaction_originals"):
                self._interaction_originals = {}

    def _create_interaction_media_wrapper(self, original: Callable) -> Callable:
        """用户点选候选后的落点：命中片名候选就接入订阅并跳过平台搜索。"""
        owner = self

        @wraps(original)
        def media_resources_wrapper(
                chain_self,
                request=None,
                mediainfo=None,
                *args,
                **kwargs,
        ):
            if owner._is_platform_search_blocked() and owner._consume_pending_identity(
                    owner._identity_from_media_selection(
                        mediainfo,
                        meta=getattr(request, "meta", None),
                        keyword=getattr(request, "keyword", None),
                    ),
                    allow_sole=True,
            ):
                return None
            return original(chain_self, request, mediainfo, *args, **kwargs)

        media_resources_wrapper.__cloudsubscribe_owner__ = self
        media_resources_wrapper.__cloudsubscribe_original__ = original
        return media_resources_wrapper

    def _identity_from_media_selection(
            self,
            mediainfo: Any,
            meta: Any = None,
            season: Any = None,
            keyword: Any = None,
    ) -> Dict[str, Any]:
        """按「用户点选的那一项」构造搜索身份，结构与 ``_search_identity`` 一致。

        点选项自带 TMDB 身份与正式片名，所以这里以它为准；片名只用于匹配登记记录
        （决定回执发给谁、用哪条记录的渠道），其中 ``keyword`` 是用户原始输入，
        与登记记录的片名最可能完全同名，排在最前面。
        """
        titles: List[str] = []
        if isinstance(keyword, str) and keyword.strip():
            titles.append(keyword.strip())
        for candidate in (meta, mediainfo):
            if candidate is None:
                continue
            for attribute in ("name", "title"):
                value = getattr(candidate, attribute, None)
                if isinstance(value, str) and value.strip() and value.strip() not in titles:
                    titles.append(value.strip())
        raw_type = getattr(mediainfo, "type", None) or getattr(meta, "type", None)
        if season is None:
            season = (
                    getattr(mediainfo, "begin_season", None)
                    or getattr(mediainfo, "season", None)
            )
        media_id = getattr(mediainfo, "media_id", None)
        media_source = getattr(mediainfo, "media_source", None)
        tmdbid = self._tmdb_id_of(mediainfo)
        if not tmdbid and media_id and self._is_tmdb_source(media_source):
            tmdbid = media_id
        return {
            "titles": titles,
            "title": titles[0] if titles else None,
            "media_id": media_id,
            "media_source": media_source,
            "mtype": media_type_value(raw_type),
            "mtype_raw": raw_type,
            "season": season,
            "tmdbid": tmdbid,
            "meta": meta,
            "mediainfo": mediainfo,
        }

    def _consume_pending_identity(
            self,
            identity: Optional[Dict[str, Any]],
            *,
            allow_sole: bool = False,
    ) -> bool:
        """按身份消费一条片名候选；命中就异步接入订阅并返回 True。

        ``allow_sole`` 只在「用户点选候选项」这条路径上传 True（点选接入器）：点选是
        用户明确的一次动作，登记表里还有类型相容的记录就直接用最近一条接入，订阅按
        点选项自带的信息建，不再要求片名对得上。
        """
        if not identity:
            return False
        store = get_pending_title_store()
        if not len(store):
            return False
        record = store.consume(
            titles=identity.get("titles") or (),
            tmdbid=identity.get("tmdbid"),
            mtype=identity.get("mtype"),
            allow_sole=allow_sole,
        )
        if not record:
            if allow_sole:
                logger.info(
                    "点选候选未接上片名登记（titles=%s / tmdbid=%s），登记保留待过期"
                    % (identity.get("titles") or [], identity.get("tmdbid"))
                )
            return False
        match = record.get("match")
        ratio = record.get("match_ratio")
        extra = ""
        if match == "sole":
            candidates = int(record.get("match_candidates") or 1)
            extra = f"，登记表共 {candidates} 条未消费"
        logger.info(
            f"片名候选命中（{match}{f' {ratio}' if ratio else ''}{extra}）：{record.get('text')}"
        )
        self._start_selected_title(record, identity)
        return True

    # -------------------------------------------------- 片名候选命中（1.6.27）
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

    @staticmethod
    def _is_tmdb_source(media_source: Any) -> bool:
        """判断媒体身份来源是否为 TMDB。"""
        if not media_source:
            return False
        from ..media import TMDB_SOURCE, normalize_media_source

        try:
            return normalize_media_source(media_source) == TMDB_SOURCE
        except Exception:
            return False

    def _search_identity(
            self,
            original: Callable,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """从平台搜索调用参数里提取被搜索的媒体身份（片名 / TMDB ID / 类型 / 季）。"""
        try:
            signature = inspect.signature(original)
            bound = signature.bind_partial(*args, **kwargs)
        except (TypeError, ValueError):
            return None
        arguments = dict(bound.arguments)
        arguments.pop("self", None)
        arguments.pop("chain", None)
        for name, parameter in signature.parameters.items():
            if parameter.kind is not inspect.Parameter.VAR_KEYWORD:
                continue
            extra = arguments.pop(name, None)
            if isinstance(extra, dict):
                arguments.update(extra)
        meta = arguments.get("meta") or arguments.get("metainfo")
        mediainfo = arguments.get("mediainfo") or arguments.get("media_info")
        media_id = arguments.get("media_id") or arguments.get("mediaid")
        media_source = arguments.get("media_source") or arguments.get("source")
        titles = [arguments.get("title")]
        raw_type = arguments.get("mtype")
        season = arguments.get("season")
        for candidate in (meta, mediainfo):
            if candidate is None:
                continue
            titles.append(getattr(candidate, "name", None))
            titles.append(getattr(candidate, "title", None))
            raw_type = raw_type or getattr(candidate, "type", None)
            season = (
                season
                or getattr(candidate, "begin_season", None)
                or getattr(candidate, "season", None)
            )
        tmdbid = self._tmdb_id_of(mediainfo)
        if not tmdbid and self._is_tmdb_source(media_source):
            tmdbid = media_id
        titles = [str(title).strip() for title in titles if title]
        if not titles and not media_id and meta is None and mediainfo is None:
            return None
        return {
            "titles": titles,
            "title": titles[0] if titles else None,
            "media_id": media_id,
            "media_source": media_source,
            "mtype": media_type_value(raw_type),
            "mtype_raw": raw_type,
            "season": season,
            "tmdbid": tmdbid,
            "meta": meta,
            "mediainfo": mediainfo,
        }

    def _dispatch_pending_title_search(
            self,
            original: Callable,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
    ) -> bool:
        """平台搜索被阻止时，检查是否为「用户刚发过的片名被选定」，命中就接入网盘订阅。

        ``args`` 需带上链实例（``(chain, *args)``），以便按原函数签名解析参数。
        只有存在未过期的片名候选时才做参数解析，平时零开销。
        返回 True 表示已命中并接管（调用方据此阻止平台搜索）。
        """
        store = get_pending_title_store()
        if not len(store):
            return False
        identity = self._search_identity(original, args, kwargs)
        if not identity:
            return False
        selection = self._is_media_selection_call(args, kwargs)
        if self._consume_pending_identity(identity, allow_sole=selection):
            return True
        if not selection and (identity.get("media_id") or identity.get("meta") is not None):
            # 只有媒体身份、没有片名：后台识别出标题再比对，避免阻塞搜索调用
            try:
                Thread(
                    target=self._recognize_selected_title,
                    args=(identity,),
                    daemon=True,
                    name="cloudsubscribe-selected-title",
                ).start()
            except Exception as error:
                logger.debug(f"选定片名识别线程启动失败：{error}", exc_info=True)
        return False

    @classmethod
    def _looks_like_media_selection(cls, kwargs: Dict[str, Any]) -> bool:
        """按参数形状判断：这次调用是不是「点选候选 → 搜索资源」。"""
        arguments = kwargs or {}
        if not arguments.get("no_exists"):
            return False
        return not any(arguments.get(key) for key in cls._SELECTION_EXCLUSIVE_KEYS)

    @classmethod
    def _is_media_selection_call(
            cls,
            args: Tuple[Any, ...],
            kwargs: Dict[str, Any],
    ) -> bool:
        """判断这次平台搜索调用是否来自「用户点选候选」。

        两条判据互为备份：调用栈里出现平台点选处理函数（最准），或参数形状与点选调用一致
        （只有 ``mediainfo``/``no_exists``，没有订阅搜索的过滤参数）。任何一条成立就按
        「点选」处理，这样即使平台再给入口改名，点选接入仍然生效。
        """
        try:
            for frame in inspect.stack():
                if _is_media_selection_frame(frame.function):
                    return True
        except Exception as error:
            logger.debug(f"点选调用栈判定失败：{error}", exc_info=True)
        return cls._looks_like_media_selection(kwargs)

    def _start_selected_title(
            self,
            record: Dict[str, Any],
            identity: Dict[str, Any],
    ) -> None:
        """把命中记录交给宿主创建订阅（异步，避免拖慢搜索调用）。"""
        try:
            Thread(
                target=self._handle_selected_title,
                args=(record, identity),
                daemon=True,
                name="cloudsubscribe-selected-title",
            ).start()
        except Exception as error:
            logger.error(f"选定片名接入线程启动失败：{error}")

    def _recognize_selected_title(self, identity: Dict[str, Any]) -> None:
        """把只有媒体身份的搜索识别成标题，再与候选片名比对。"""
        store = get_pending_title_store()
        if not len(store):
            return
        try:
            mediainfo = self._sync_handler._recognize_media_once(
                (
                    "selected_search",
                    str(identity.get("media_source") or ""),
                    str(identity.get("media_id") or ""),
                    str(identity.get("title") or ""),
                ),
                meta=identity.get("meta"),
                mtype=identity.get("mtype_raw"),
                media_source=identity.get("media_source"),
                media_id=identity.get("media_id"),
                cache=True,
            )
        except Exception as error:
            logger.debug(f"搜索结果识别失败：{error}")
            return
        if not mediainfo:
            return
        enriched = dict(identity)
        enriched["mediainfo"] = mediainfo
        enriched["title"] = getattr(mediainfo, "title", None) or identity.get("title")
        enriched["tmdbid"] = self._tmdb_id_of(mediainfo) or identity.get("tmdbid")
        enriched["mtype"] = (
            media_type_value(getattr(mediainfo, "type", None)) or identity.get("mtype")
        )
        record = store.consume(
            titles=[enriched.get("title")],
            tmdbid=enriched.get("tmdbid"),
            mtype=enriched.get("mtype"),
        )
        if not record:
            return
        self._handle_selected_title(record, enriched)

    def _is_platform_search_blocked(self) -> bool:
        return (
                self._platform_download_policy == "block"
                and self._is_takeover_active()
        )

    def _install_subscribe_chain_takeover(self) -> None:
        """挂接 SubscribeChain 内部的单订阅搜索执行器（适配 MoviePilot v3 持久化队列及单项搜索）。"""
        try:
            from app.chain.subscribe import SubscribeChain

            method_name = "_process_search_subscription"
            current = getattr(SubscribeChain, method_name, None)
            if not callable(current):
                return
            if getattr(current, "__cloudsubscribe_owner__", None) is self:
                return

            original = getattr(
                current, "__cloudsubscribe_original__", current
            )
            originals = getattr(self, "_subscribe_chain_originals", None)
            if originals is not None:
                originals.setdefault(method_name, original)
            wrapper = self._create_subscribe_chain_wrapper(
                method_name=method_name,
                original=original,
            )
            setattr(SubscribeChain, method_name, wrapper)
            logger.debug("已安装订阅执行接管器")
        except Exception as error:
            logger.warning(f"安装订阅执行接管器失败：{error}")

    def _install_subscribe_add_expansion(self) -> None:
        for method_name in ("add", "async_add"):
            current = getattr(SubscribeChain, method_name, None)
            if not callable(current) or getattr(current, "__cloudsubscribe_owner__", None) is self:
                continue
            original = getattr(current, "__cloudsubscribe_original__", current)
            originals = getattr(self, "_subscribe_chain_originals", None)
            if originals is not None:
                originals.setdefault(method_name, original)
            wrapper = self._create_subscribe_add_expansion(original)
            wrapper.__cloudsubscribe_owner__ = self
            wrapper.__cloudsubscribe_original__ = original
            setattr(SubscribeChain, method_name, wrapper)

    def _create_subscribe_add_expansion(self, original: Callable) -> Callable:
        owner = self
        is_async = inspect.iscoroutinefunction(original)

        def expansion_context(args, kwargs):
            """
            解析本次 add 调用的季号与媒体身份。

            MoviePilot 的 ``SubscribeChain.add`` 只接收 media_source / media_id
            （interaction.py 的片名选定路径即如此），因此缺 mediainfo 时靠身份重新识别。
            """
            try:
                values = inspect.signature(original).bind_partial(
                    None, *args, **kwargs
                ).arguments
            except (TypeError, ValueError):
                values = {}
            mediainfo = values.get("mediainfo") or kwargs.get("mediainfo")
            mtype = (
                values.get("mtype")
                or kwargs.get("mtype")
                or getattr(mediainfo, "type", None)
            )
            return {
                "values": values,
                "mediainfo": mediainfo,
                "mtype": mtype,
                "season": values.get("season", kwargs.get("season")),
                "media_source": values.get("media_source") or kwargs.get("media_source"),
                "media_id": values.get("media_id") or kwargs.get("media_id"),
                "is_tv": _is_tv_media_type(getattr(mtype, "value", mtype)),
            }

        def resolve_seasons(context):
            from ..subscribe.seasons import resolve_subscribe_seasons

            return resolve_subscribe_seasons(
                context["mediainfo"],
                media_source=context["media_source"],
                media_id=context["media_id"],
                mtype=context["mtype"],
                skip_season_zero=getattr(owner, "_subscribe_skip_season_zero", True),
                limit=getattr(owner, "_subscribe_season_limit", 0),
            )

        def should_expand(context):
            """只有「电视剧 + 未指定季号 + 开关开启 + 非抑制态」才按季展开。"""
            return bool(
                context["is_tv"]
                and context["season"] is None
                and getattr(owner, "_subscribe_all_missing_seasons", True)
                and not _season_expansion_suppressed.get()
            )

        async def run_async(chain, args, kwargs):
            context = expansion_context(args, kwargs)
            if not should_expand(context):
                return await original(chain, *args, **kwargs)
            seasons = resolve_seasons(context)
            if not seasons:
                return await original(chain, *args, **kwargs)
            return await invoke_seasons(
                chain, args, kwargs, context["values"], seasons, True
            )

        def invoke_seasons(chain, args, kwargs, values, seasons, async_mode):
            results = []
            try:
                parameter_names = list(inspect.signature(original).parameters)
            except (TypeError, ValueError):
                parameter_names = []

            def set_argument(positional, keyword, name, value):
                try:
                    index = parameter_names.index(name) - 1
                except ValueError:
                    index = -1
                if 0 <= index < len(positional):
                    positional[index] = value
                    keyword.pop(name, None)
                else:
                    keyword[name] = value

            for season in seasons:
                call_kwargs = dict(kwargs)
                positional = list(args)
                set_argument(positional, call_kwargs, "season", season)
                if season != seasons[0]:
                    set_argument(positional, call_kwargs, "message", False)
                result = original(chain, *positional, **call_kwargs)
                results.append(result)
            if async_mode:
                async def collect():
                    first = None
                    last = None
                    for season, pending in zip(seasons, results):
                        result = await pending
                        logger.info(f"手动订阅按缺集季展开：{values.get('title', '')} S{season:02d} -> {result}")
                        if first is None and isinstance(result, tuple) and result and result[0]:
                            first = result
                        last = result
                    return first if first is not None else last
                return collect()
            first = None
            for season, result in zip(seasons, results):
                logger.info(f"手动订阅按缺集季展开：{values.get('title', '')} S{season:02d} -> {result}")
                if first is None and isinstance(result, tuple) and result and result[0]:
                    first = result
            return first if first is not None else results[-1]

        if is_async:
            @wraps(original)
            async def async_wrapper(chain, *args, **kwargs):
                return await run_async(chain, args, kwargs)
            return async_wrapper

        @wraps(original)
        def sync_wrapper(chain, *args, **kwargs):
            context = expansion_context(args, kwargs)
            if not should_expand(context):
                return original(chain, *args, **kwargs)
            seasons = resolve_seasons(context)
            if not seasons:
                return original(chain, *args, **kwargs)
            return invoke_seasons(
                chain, args, kwargs, context["values"], seasons, False
            )
        return sync_wrapper

    def _restore_subscribe_chain_takeover(self) -> None:
        """恢复 SubscribeChain 的原始搜索执行方法。"""
        originals = dict(getattr(self, "_subscribe_chain_originals", None) or {})
        if not originals:
            return
        try:
            from app.chain.subscribe import SubscribeChain

            for method_name, original in originals.items():
                current = getattr(SubscribeChain, method_name, None)
                if getattr(current, "__cloudsubscribe_owner__", None) is self:
                    setattr(SubscribeChain, method_name, original)
        except Exception as error:
            logger.warning(f"恢复订阅执行接管器失败：{error}")
        finally:
            if hasattr(self, "_subscribe_chain_originals"):
                self._subscribe_chain_originals = {}

    def _create_subscribe_chain_wrapper(
            self,
            method_name: str,
            original: Callable,
    ) -> Callable:
        owner = self

        @wraps(original)
        def process_search_subscription_wrapper(
                chain_self,
                subscribe,
                searchchain=None,
                execution_context=None,
                *args,
                **kwargs,
        ):
            return owner._dispatch_process_search_subscription(
                original=original,
                chain_self=chain_self,
                subscribe=subscribe,
                searchchain=searchchain,
                execution_context=execution_context,
                *args,
                **kwargs,
            )

        process_search_subscription_wrapper.__cloudsubscribe_owner__ = self
        process_search_subscription_wrapper.__cloudsubscribe_original__ = original
        return process_search_subscription_wrapper

    def _dispatch_process_search_subscription(
            self,
            original: Callable,
            chain_self: Any,
            subscribe: Any,
            searchchain: Any = None,
            execution_context: Any = None,
            *args: Any,
            **kwargs: Any,
    ) -> Any:
        if searchchain is None and "searchchain" in kwargs:
            searchchain = kwargs.pop("searchchain")
        if execution_context is None and "execution_context" in kwargs:
            execution_context = kwargs.pop("execution_context")

        subscribe_id = getattr(subscribe, "id", None)
        subscribe_state = getattr(subscribe, "state", "R")
        use_plugin = self._is_takeover_active()
        if subscribe_state == "N" and self._enabled and self._takeover_new_subscribes:
            use_plugin = True
        if subscribe_id and self._is_subscribe_excluded(subscribe_id):
            use_plugin = False

        if not use_plugin:
            return original(
                chain_self,
                subscribe,
                searchchain=searchchain,
                execution_context=execution_context,
                *args,
                **kwargs,
            )

        # 平台 v3 订阅搜索接管逻辑：
        # 1. 尝试更新订阅 last_search 时间戳，保持平台最近搜索事实同步
        if execution_context is None or not getattr(execution_context, "resuming_sites", False):
            apply_update = getattr(chain_self, "_SubscribeChain__apply_subscribe_update", None)
            if callable(apply_update):
                try:
                    subscribe = apply_update(
                        subscribe,
                        {"last_search": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                        scene="search",
                    )
                except Exception as update_err:
                    logger.debug(f"更新订阅 last_search 失败（可忽略）：{update_err}", exc_info=True)

        # 2. 如果存在 execution_context，通知当前阶段进入 searching
        if execution_context and hasattr(execution_context, "report_phase"):
            try:
                execution_context.report_phase("searching")
            except Exception:
                logger.debug("_dispatch_process_search_subscription 分支忽略异常", exc_info=True)

        # 3. 投递到网盘订阅助手搜索队列
        subscribe_name = getattr(subscribe, "name", "") or getattr(subscribe, "title", "")
        logger.debug(
            f"订阅搜索转入网盘任务：id={subscribe_id or 'ALL'}，标题={subscribe_name}"
        )
        self.queue_subscribe_search(
            subscribe_id=subscribe_id,
            subscribe_state=subscribe_state,
            progress_callback=None,
        )

        # 4. 返回 subscribeSnapshot，供 v3 的 SubscriptionSearchTaskRunner
        # 正常调用 finish_returned_search_task 将原生队列任务标记为 completed 终态。
        return subscribe

    @staticmethod
    def _call_subscribe_chain_search(**kwargs) -> Any:
        """安全调用 SubscribeChain.search，根据支持的参数自动适配，防止 TypeError。同时兼容 v2 和 v3。"""
        chain = SubscribeChain()
        search_func = getattr(chain, "search", None)
        if not callable(search_func):
            return None

        sids = kwargs.get("sids")
        try:
            sig = inspect.signature(search_func)
            has_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            # 如果原生 search 支持 sids 或支持 **kwargs（如 v3）
            if has_var_kwargs or "sids" in sig.parameters:
                filtered = kwargs if has_var_kwargs else {k: v for k, v in kwargs.items() if k in sig.parameters}
                return search_func(**filtered)

            # 原生 search 不支持 sids（如 v2 环境）
            if sids:
                single_kwargs = dict(kwargs)
                single_kwargs.pop("sids", None)
                results = []
                for sub_id in sids:
                    single_kwargs["sid"] = sub_id
                    filtered = {k: v for k, v in single_kwargs.items() if k in sig.parameters}
                    results.append(search_func(**filtered))
                return results[-1] if results else None

            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            return search_func(**filtered)
        except Exception as err:
            logger.debug(f"检查 SubscribeChain.search 签名失败，尝试直接调用：{err}")
            try:
                return search_func(**kwargs)
            except TypeError:
                if sids:
                    base_keys = ("state", "manual", "progress_callback")
                    base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
                    results = []
                    for sub_id in sids:
                        results.append(search_func(sid=sub_id, **base_kwargs))
                    return results[-1] if results else None
                base_keys = ("sid", "state", "manual", "progress_callback")
                base_kwargs = {k: v for k, v in kwargs.items() if k in base_keys}
                return search_func(**base_kwargs)

    @staticmethod
    def _call_subscribe_chain_refresh(**kwargs) -> Any:
        """安全调用 SubscribeChain.refresh，根据支持的参数自动适配。"""
        chain = SubscribeChain()
        refresh_func = getattr(chain, "refresh", None)
        if not callable(refresh_func):
            return None
        try:
            sig = inspect.signature(refresh_func)
            has_var_kwargs = any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in sig.parameters.values()
            )
            if has_var_kwargs:
                return refresh_func(**kwargs)
            filtered = {k: v for k, v in kwargs.items() if k in sig.parameters}
            return refresh_func(**filtered)
        except Exception as err:
            logger.debug(f"检查 SubscribeChain.refresh 签名失败，尝试直接调用：{err}")
            try:
                return refresh_func(**kwargs)
            except TypeError:
                return refresh_func(progress_callback=kwargs.get("progress_callback"))

    def _dispatch_subscribe_search(
            self,
            sid: Optional[int] = None,
            state: Optional[str] = "R",
            manual: Optional[bool] = False,
            progress_callback: Optional[Callable[..., None]] = None,
            sids: Optional[Tuple[int, ...]] = None,
            scheduled_interval: Optional[int] = None,
            **kwargs: Any,
    ):
        use_plugin = self._is_takeover_active()
        if state == "N" and self._enabled and self._takeover_new_subscribes:
            use_plugin = True
        if sid and self._is_subscribe_excluded(sid):
            use_plugin = False
        if not use_plugin:
            return self._call_subscribe_chain_search(
                sid=sid,
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                sids=sids,
                scheduled_interval=scheduled_interval,
                **kwargs,
            )

        # 自身会每 5 分钟触发新增订阅搜索；接管状态下仅消费
        # 平台自动任务，统一由插件配置的 Cron 执行自动同步。
        if not bool(manual):
            return True

        if sids:
            logger.info(
                f"订阅搜索转入网盘任务：subscribe_ids={sids}，"
                f"manual={bool(manual)}"
            )
            for subscribe_id in sids:
                if self._is_subscribe_excluded(subscribe_id):
                    self._call_subscribe_chain_search(
                        sid=subscribe_id,
                        state=state,
                        manual=manual,
                        progress_callback=progress_callback,
                        scheduled_interval=scheduled_interval,
                        **kwargs,
                    )
                else:
                    self.queue_subscribe_search(
                        subscribe_id=subscribe_id,
                        subscribe_state=state,
                        progress_callback=progress_callback,
                    )
            return True

        if sid is None:
            return self._dispatch_all_subscribe_search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                scheduled_interval=scheduled_interval,
                **kwargs,
            )

        logger.info(
            f"订阅搜索转入网盘任务：subscribe_id={sid or 'ALL'}，"
            f"manual={bool(manual)}"
        )
        return self.queue_subscribe_search(
            subscribe_id=sid,
            subscribe_state=state,
            progress_callback=progress_callback,
        )

    def _dispatch_subscribe_refresh(
            self,
            progress_callback: Optional[Callable[..., None]] = None,
            mtype: Optional[str] = None,
            **kwargs: Any,
    ):
        """按平台下载策略决定接管态是否继续RSS/PT 刷新。"""
        if not self._is_takeover_active():
            return self._call_subscribe_chain_refresh(
                progress_callback=progress_callback,
                mtype=mtype,
                **kwargs,
            )

        if progress_callback:
            progress_callback(
                value=100,
                text="订阅已由网盘订阅助手接管，跳过原生资源刷新",
            )
        logger.debug("接管态已跳过原生订阅资源刷新")
        return True

    def _dispatch_all_subscribe_search(
            self,
            state: Optional[str],
            manual: Optional[bool],
            progress_callback: Optional[Callable[..., None]],
            scheduled_interval: Optional[int] = None,
            **kwargs: Any,
    ) -> bool:
        """将全量平台任务拆成插件接管与原生保留两部分。"""
        try:
            subscribes = SubscribeOper().list(state or "N,R") or []
        except Exception as error:
            logger.warning(f"读取订阅接管范围失败，已回退原生搜索：{error}")
            return self._call_subscribe_chain_search(
                state=state,
                manual=manual,
                progress_callback=progress_callback,
                scheduled_interval=scheduled_interval,
                **kwargs,
            )

        managed_ids, native_ids = self._partition_subscribe_ids(subscribes)
        logger.info(
            f"订阅搜索分流：状态={state or 'N,R'}，"
            f"插件处理 {len(managed_ids)} 个，原生处理 {len(native_ids)} 个"
        )
        for index, subscribe_id in enumerate(managed_ids):
            self.queue_subscribe_search(
                subscribe_id=subscribe_id,
                subscribe_state=state,
                progress_callback=progress_callback if index == 0 else None,
            )
        for index, subscribe_id in enumerate(native_ids):
            self._call_subscribe_chain_search(
                sid=subscribe_id,
                state=None,
                manual=manual,
                progress_callback=(
                    progress_callback
                    if not managed_ids and index == 0
                    else None
                ),
                scheduled_interval=scheduled_interval,
                **kwargs,
            )
        return True

    def _partition_subscribe_ids(self, subscribes) -> Tuple[List[int], List[int]]:
        """按过滤规则划分插件与原生搜索的订阅。"""
        managed_ids = []
        native_ids = []
        for subscribe in subscribes:
            subscribe_id = getattr(subscribe, "id", None)
            if not subscribe_id:
                continue
            target = native_ids if self._is_subscribe_excluded(subscribe_id) else managed_ids
            target.append(subscribe_id)
        return managed_ids, native_ids
