"""渠道管理 API：搜索渠道 + TG 监控频道统一走渠道对象。

列表返回渠道、搜索源目录与表单 schema，前端新增/编辑表单直接按 schema
渲染，不手写死字段。搜索渠道的测试复用 ``SearchApi`` 的隔离单源测试；
TG 频道测试只做格式 + 账号校验（最小实现），扫描走监控的单频道扫描。
"""

from typing import Any, Dict, List, Optional

from app.log import logger
from app.schemas.types import MediaType

from ..channels import TG_CHANNEL_TYPE, ChannelStore
from ..delegation import OwnerDelegator
from ...search.scanner import SearchSourceRegistry


class ChannelsApi(OwnerDelegator):
    #: 渠道探测用的固定媒体：只验证渠道本身可用，不依赖用户输入。
    _TEST_TITLE = "测试"

    # -------------------------------------------------------------- 内部工具
    def _store(self) -> ChannelStore:
        accessor = getattr(self, "_channel_store", None)
        if callable(accessor):
            return accessor()
        return ChannelStore(self.get_data_path())

    def _refresh_runtime(
            self, search_config_updates: Optional[Dict[str, Any]] = None
    ) -> None:
        refresher = getattr(self, "_refresh_channel_runtime", None)
        if not callable(refresher):
            return
        try:
            refresher(search_config_updates=search_config_updates)
        except Exception as error:  # noqa: BLE001 - 刷新失败不拖垮接口
            logger.warning(f"刷新渠道运行态失败：{error}")

    def _persist_search_values(self, values: Dict[str, Any]) -> None:
        """把搜索渠道改动的配置值写回插件配置（运行态的取值来源）。"""
        if not values:
            return
        persist = getattr(self, "_persist_config_values", None)
        if not callable(persist):
            return
        try:
            persist(**values)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"渠道配置值写回插件配置失败：{error}")

    def _plugin_config(self) -> Dict[str, Any]:
        config = getattr(self, "_applied_config", None)
        if isinstance(config, dict) and config:
            return config
        getter = getattr(self, "get_config", None)
        if callable(getter):
            try:
                return dict(getter() or {})
            except Exception as error:  # noqa: BLE001 - 配置读失败时按空配置处理
                logger.debug("读取插件配置失败：%s", error, exc_info=True)
        return {}

    def _display_channel(
            self, channel: Dict[str, Any], plugin_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """搜索渠道的展示形态：插件配置里的现值覆盖渠道库里的旧值。

        每源配置字段仍保留在配置页，配置页改了值之后渠道页表单要显示
        最新值；保存时再把表单值写回两处，始终以插件配置为准。
        """
        if channel.get("type") == TG_CHANNEL_TYPE:
            return channel
        definition = next(
            (
                item for item in SearchSourceRegistry.get_definitions()
                if item.id == channel.get("type")
            ),
            None,
        )
        if definition is None:
            return channel
        merged = dict(channel.get("config") or {})
        for key in definition.get_config_keys():
            if key in plugin_config:
                merged[key] = plugin_config[key]
        return {**channel, "config": merged}

    @staticmethod
    def _config_diff(
            old: Dict[str, Any], new: Dict[str, Any],
    ) -> Dict[str, Any]:
        """返回 new 相对 old 变化的配置键值（只含发生变化的键）。"""
        return {
            key: value for key, value in (new or {}).items()
            if (old or {}).get(key) != value
        }

    # -------------------------------------------------------------- CRUD
    def api_channels(self) -> dict:
        """列出全部渠道（含搜索源目录与表单 schema，供前端渲染）。"""
        store = self._store()
        plugin_config = self._plugin_config()
        items = [
            self._display_channel(item, plugin_config)
            for item in store.list()
        ]
        return {
            "success": True,
            "data": {
                "items": items,
                "catalog": SearchSourceRegistry.get_source_catalog(),
                "schemas": SearchSourceRegistry.get_search_schemas(),
            },
        }

    def api_channel_create(self, payload: Optional[dict] = None) -> dict:
        """新增渠道；搜索渠道每种只允许一条（已存在请编辑）。"""
        payload = dict(payload or {})
        channel_type = str(payload.get("type") or "").strip()
        store = self._store()
        if channel_type and channel_type != TG_CHANNEL_TYPE:
            for item in store.list():
                if item.get("type") == channel_type:
                    catalog = {
                        entry["key"]: entry["name"]
                        for entry in SearchSourceRegistry.get_source_catalog()
                    }
                    name = catalog.get(channel_type, channel_type)
                    return {
                        "success": False,
                        "message": f"{name} 渠道已存在，请编辑已有渠道",
                    }
        try:
            item = store.create(payload)
        except ValueError as error:
            return {"success": False, "message": str(error)}
        changed: Dict[str, Any] = {}
        if item.get("type") != TG_CHANNEL_TYPE:
            changed = dict(item.get("config") or {})
        self._persist_search_values(changed)
        self._refresh_runtime(search_config_updates=changed or None)
        return {"success": True, "data": item}

    def api_channel_update(
            self, channel_id: str = "", payload: Optional[dict] = None,
    ) -> dict:
        """局部更新渠道字段；搜索渠道改了配置值时同步写回插件配置。"""
        store = self._store()
        old = store.get(str(channel_id or ""))
        if not old:
            return {"success": False, "message": "渠道不存在"}
        try:
            item = store.update(
                str(channel_id or ""), dict(payload or {}),
            )
        except ValueError as error:
            return {"success": False, "message": str(error)}
        if not item:
            return {"success": False, "message": "渠道不存在"}
        changed: Dict[str, Any] = {}
        if item.get("type") != TG_CHANNEL_TYPE and isinstance(
                (payload or {}).get("config"), dict,
        ):
            changed = self._config_diff(
                old.get("config") or {}, item.get("config") or {},
            )
        self._persist_search_values(changed)
        self._refresh_runtime(search_config_updates=changed or None)
        return {"success": True, "data": item}

    def api_channel_delete(self, channel_id: str = "") -> dict:
        """删除渠道；删除后立即刷新运行时。"""
        if not self._store().delete(str(channel_id or "")):
            return {"success": False, "message": "渠道不存在"}
        self._refresh_runtime()
        return {"success": True}

    # -------------------------------------------------------------- 测试/扫描
    def _test_search_channel(self, channel: Dict[str, Any]) -> Dict[str, Any]:
        """复用 SearchApi 的隔离单源测试：按渠道配置建临时搜索器跑一次。"""
        from .search import SearchApi

        source = str(channel.get("type") or "").strip().lower()
        search_api = SearchApi(self._owner)
        display = self._display_channel(channel, self._plugin_config())
        config = search_api._test_search_config(
            source, dict(display.get("config") or {}),
        )
        from app.schemas import MediaInfo

        mediainfo = MediaInfo(type=MediaType.TV, title=self._TEST_TITLE)
        handler = None
        try:
            handler = search_api._build_test_search_handler(source, config)
            results = handler.test_source(
                source, mediainfo, MediaType.TV, 1,
            )
        finally:
            if handler is not None:
                try:
                    handler.close(release_cache=False)
                except Exception as error:  # noqa: BLE001
                    logger.debug(f"关闭渠道测试搜索器失败：{error}", exc_info=True)
        count = len(results or [])
        return {
            "ok": True,
            "count": count,
            "message": f"渠道可用，本次探测返回 {count} 条",
        }

    def _test_tg_channel(self, channel: Dict[str, Any]) -> Dict[str, Any]:
        """TG 频道最小测试：校验频道格式 + TG 账号，不发网络请求。"""
        config = channel.get("config") or {}
        target = str(config.get("channel") or "").strip()
        if not target:
            return {"ok": False, "message": "渠道缺少频道标识"}
        try:
            api_id = int(getattr(self, "_tg_api_id", 0) or 0)
        except (TypeError, ValueError):
            api_id = 0
        api_hash = str(getattr(self, "_tg_api_hash", "") or "").strip()
        session = str(getattr(self, "_tg_session", "") or "").strip()
        if not (api_id and api_hash and session):
            return {
                "ok": False,
                "message": "TG 账号未登录：请先在配置页填写 api_id/api_hash，"
                           "再到「频道监控」页登录",
            }
        enabled = bool(getattr(self, "_tg_monitor_enabled", False))
        return {
            "ok": True,
            "count": 0,
            "message": (
                f"频道格式有效：{target}；"
                f"频道监控{'已启用' if enabled else '未启用'}"
            ),
        }

    def api_channel_test(self, channel_id: str = "") -> dict:
        """探测渠道可用性：搜索渠道跑一次隔离搜索，TG 频道校验格式与账号。"""
        store = self._store()
        channel = store.get(str(channel_id or ""))
        if not channel:
            return {"success": False, "message": "渠道不存在"}
        try:
            if channel.get("type") == TG_CHANNEL_TYPE:
                outcome = self._test_tg_channel(channel)
            else:
                outcome = self._test_search_channel(channel)
        except Exception as error:  # noqa: BLE001 - 探活失败只返回结果
            store.record_stat(channel["id"], error=type(error).__name__)
            return {"success": False, "message": f"探测失败：{error}"}
        if not outcome.get("ok"):
            store.record_stat(channel["id"], error=str(outcome.get("message") or ""))
            return {"success": False, "message": str(outcome.get("message") or "渠道不可用")}
        store.record_stat(channel["id"], hits=int(outcome.get("count") or 0))
        return {"success": True, "message": str(outcome.get("message") or "渠道可用")}

    def api_channel_scan(self, channel_id: str = "") -> dict:
        """立即扫描一个 TG 频道一次，返回新增条目数。"""
        store = self._store()
        channel = store.get(str(channel_id or ""))
        if not channel:
            return {"success": False, "message": "渠道不存在"}
        if channel.get("type") != TG_CHANNEL_TYPE:
            return {"success": False, "message": "搜索渠道不支持扫描"}
        config = channel.get("config") or {}
        target = str(config.get("channel") or "").strip()
        if not target:
            return {"success": False, "message": "渠道缺少频道标识"}
        try:
            from ..tg_monitor import get_channel_monitor

            monitor = get_channel_monitor(self._owner)
            result = monitor.scan(trigger="手动", channels=[target])
        except Exception as error:  # noqa: BLE001 - 扫描失败记录到统计
            store.record_stat(channel["id"], error=type(error).__name__)
            return {"success": False, "message": f"扫描失败：{type(error).__name__}"}
        if not result.get("ok"):
            message = str(result.get("message") or "扫描失败")
            store.record_stat(channel["id"], error=message)
            return {"success": False, "message": message}
        added = int(result.get("added") or 0)
        store.record_stat(channel["id"], hits=added)
        return {
            "success": True,
            "message": f"扫描完成，新增 {added} 条",
            "data": {"added": added},
        }

    def api_channels_summary(self) -> dict:
        """返回渠道总数、启用数与按类型拆分，TG 监控开关一并返回。"""
        items = self._store().list()
        search_items = [
            item for item in items if item.get("type") != TG_CHANNEL_TYPE
        ]
        tg_items = [
            item for item in items if item.get("type") == TG_CHANNEL_TYPE
        ]
        return {
            "success": True,
            "data": {
                "total": len(items),
                "enabled": len([item for item in items if item.get("enabled")]),
                "search_total": len(search_items),
                "search_enabled": len(
                    [item for item in search_items if item.get("enabled")]
                ),
                "tg_total": len(tg_items),
                "tg_enabled": len(
                    [item for item in tg_items if item.get("enabled")]
                ),
                "tg_monitor_enabled": bool(
                    getattr(self, "_tg_monitor_enabled", False)
                ),
            },
        }
