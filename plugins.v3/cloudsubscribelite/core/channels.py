"""渠道数据模型与 JSON 持久化（网盘订阅助手）。

渠道是插件的一等对象：16 个搜索源与 TG 监控频道都通过统一的
``channels.json`` 增删改启停。模块级只依赖标准库和宿主日志；
``ChannelMixin``（入口拆出的渠道逻辑）需要在模块级导入搜索源注册表与
搜索处理器——这两者只依赖 ``core.definitions`` / ``core``，不反向依赖
本模块，无导入环。
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from app.log import logger
from typing import Any, Dict, List, Optional
from ..handlers import SearchHandler
from ..search.scanner import SearchSourceRegistry

CHANNELS_FILE = "channels.json"
TG_CHANNEL_TYPE = "tg_channel"
PRIORITY_MIN = -9999
PRIORITY_MAX = 9999
#: 迁移时启用渠道的优先级起点（每档 10，越大越优先）；与 QuarkChannel 的
#: 盘搜 50 / TG 30 同一量级，搜索渠道默认排在 TG 频道之前。
SEARCH_PRIORITY_BASE = 100
TG_PRIORITY_BASE = 50


def _as_text(value: Any) -> str:
    """把任意值转成去空白字符串。"""
    return str(value or "").strip()


def _as_list(value: Any) -> list[str]:
    """把列表、逗号串或多行文本统一成去重字符串列表。"""
    if isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    elif isinstance(value, str):
        items = re.split(r"[,，\n|]+", value)
    else:
        items = []
    return [item.strip() for item in items if item.strip()]


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    """把值转成受限整数，异常时返回默认值。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _search_definition(channel_type: str) -> Optional[Any]:
    """按 id 取搜索源定义；未注册时返回 None。"""
    from ..search.scanner import SearchSourceRegistry

    for definition in SearchSourceRegistry.get_definitions():
        if definition.id == channel_type:
            return definition
    return None


def _normalize_config(
        channel_type: str, raw: Any, definition: Optional[Any] = None,
) -> dict:
    """校验并规范化类型专属配置。

    搜索渠道只收该源 ``get_config_keys()`` 里的键（全局搜索设置——代理、
    并发、超时、缓存、熔断——留在插件配置里，不进渠道）；TG 频道复用
    ``tg_monitor.normalize_channel_rules`` 做校验/归一化。

    :param channel_type: 搜索源 id 或 ``tg_channel``
    :param raw: 原始配置
    :param definition: 已解析的搜索源定义（不传时现查注册表）
    :return: 规范化后的配置副本
    :raises ValueError: 类型未注册或 TG 频道无效
    """
    config = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    if channel_type == TG_CHANNEL_TYPE:
        from .tg_monitor import normalize_channel_rules

        rules = normalize_channel_rules([{
            "channel": config.get("channel", ""),
            "enabled": True,
            "keywords": config.get("keywords", []),
            "exclude_keywords": config.get("exclude_keywords", []),
            "scan_limit": config.get("scan_limit", 20),
        }])
        if not rules:
            raise ValueError("tg_channel 渠道缺少有效频道")
        rule = rules[0]
        return {
            "channel": rule["channel"],
            "keywords": list(rule.get("keywords") or []),
            "exclude_keywords": list(rule.get("exclude_keywords") or []),
            "scan_limit": int(rule.get("scan_limit") or 20),
        }
    resolved = definition if definition is not None else _search_definition(channel_type)
    if resolved is None:
        raise ValueError(f"未知的搜索渠道类型：{channel_type}")
    allowed = resolved.get_config_keys()
    return {key: config[key] for key in allowed if key in config}


def _new_stats() -> dict:
    """返回新渠道的统计结构。"""
    return {"last_scan": 0.0, "last_error": "", "hits": 0}


def _new_id() -> str:
    """生成 12 位渠道 id。"""
    return uuid.uuid4().hex[:12]


class ChannelStore:
    """渠道存储：线程安全 CRUD 与原子 JSON 落盘。"""

    def __init__(self, data_dir: Path, definitions: Optional[Any] = None) -> None:
        """绑定数据目录并加载渠道文件。

        :param data_dir: 插件数据目录
        :param definitions: 可选的搜索源定义映射（id -> 定义），仅供测试注入；
            不传时现查 :class:`SearchSourceRegistry`。
        """
        self._dir = Path(data_dir)
        self._file = self._dir / CHANNELS_FILE
        self._lock = threading.RLock()
        self._items: list[dict] = []
        self._load_error = False
        self._definitions = definitions
        self._load()

    @property
    def needs_migration(self) -> bool:
        """渠道文件缺失或损坏时为 True，调用方应按旧配置重建。"""
        return (not self._file.exists()) or self._load_error

    def _load(self) -> None:
        """从磁盘恢复渠道；损坏文件按空列表处理并标记 ``_load_error``。"""
        if not self._file.exists():
            return
        try:
            payload = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._load_error = True
            return
        rows = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            self._load_error = True
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                item = self._build_item(row, existing=None)
            except (TypeError, ValueError):
                continue
            self._items.append(item)
        self._sort_locked()

    def _save_locked(self) -> None:
        """原子写回渠道文件；调用方必须持锁。"""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            temp = self._file.with_suffix(".json.tmp")
            payload = {"items": self._items}
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self._file)
        except OSError as error:
            logger.warning(f"写入渠道文件失败：{type(error).__name__}")

    def _sort_locked(self) -> None:
        """按 priority 倒序稳定排序。"""
        self._items.sort(key=lambda item: int(item.get("priority") or 0), reverse=True)

    def _all_search_definitions(self) -> list:
        """返回按 order 排序的全部搜索源定义。"""
        if self._definitions is not None:
            values = list(self._definitions.values())
            values.sort(key=lambda d: getattr(d, "order", 100))
            return values
        from ..search.scanner import SearchSourceRegistry

        return SearchSourceRegistry.get_definitions()

    def _build_item(self, payload: dict, existing: Optional[dict] = None) -> dict:
        """合并已有渠道与局部字段，生成规范化的渠道对象。"""
        source = dict(existing or {})
        for key in ("id", "name", "type", "enabled", "priority", "config", "stats"):
            if key in payload:
                source[key] = payload[key]
        channel_type = _as_text(source.get("type"))
        if not channel_type:
            raise ValueError("渠道类型不能为空")
        name = _as_text(source.get("name"))
        if not name:
            raise ValueError("渠道名称不能为空")
        definition = None
        if channel_type != TG_CHANNEL_TYPE:
            if self._definitions is not None:
                definition = self._definitions.get(channel_type)
            if definition is None and self._definitions is None:
                definition = _search_definition(channel_type)
            if definition is None:
                raise ValueError(f"未知的搜索渠道类型：{channel_type}")
        config = _normalize_config(channel_type, source.get("config"), definition)
        priority = _as_int(source.get("priority"), 0, PRIORITY_MIN, PRIORITY_MAX)
        stats = copy.deepcopy(source.get("stats")) if isinstance(source.get("stats"), dict) else {}
        normalized_stats = _new_stats()
        normalized_stats.update({
            "last_scan": float(stats.get("last_scan") or 0.0),
            "last_error": str(stats.get("last_error") or ""),
            "hits": _as_int(stats.get("hits"), 0, 0, 2**31 - 1),
        })
        return {
            "id": _as_text(source.get("id")) or _new_id(),
            "name": name,
            "type": channel_type,
            "enabled": bool(source.get("enabled", True)),
            "priority": priority,
            "config": config,
            "stats": normalized_stats,
        }

    def list(self) -> list[dict]:
        """返回按 priority 倒序排列的渠道副本。"""
        with self._lock:
            self._sort_locked()
            return [copy.deepcopy(item) for item in self._items]

    def get(self, channel_id: str) -> Optional[dict]:
        """按 id 读取渠道副本。"""
        target = _as_text(channel_id)
        with self._lock:
            for item in self._items:
                if item.get("id") == target:
                    return copy.deepcopy(item)
        return None

    def create(self, payload: dict) -> dict:
        """新增渠道并返回完整对象。

        :param payload: 渠道字段
        :return: 新渠道副本
        :raises ValueError: 名称、类型或配置不合法
        """
        item = self._build_item(payload or {}, existing=None)
        with self._lock:
            while any(row.get("id") == item["id"] for row in self._items):
                item["id"] = _new_id()
            item["stats"] = _new_stats()
            self._items.append(item)
            self._sort_locked()
            self._save_locked()
        return copy.deepcopy(item)

    def update(self, channel_id: str, patch: dict) -> Optional[dict]:
        """局部更新渠道并返回新副本；不存在时返回 None。

        :param channel_id: 渠道 id
        :param patch: 需要更新的字段
        :raises ValueError: 更新后的渠道不合法
        """
        target = _as_text(channel_id)
        with self._lock:
            for index, current in enumerate(self._items):
                if current.get("id") != target:
                    continue
                updated = self._build_item(patch or {}, existing=current)
                self._items[index] = updated
                self._sort_locked()
                self._save_locked()
                return copy.deepcopy(updated)
        return None

    def delete(self, channel_id: str) -> bool:
        """删除渠道；成功返回 True。"""
        target = _as_text(channel_id)
        with self._lock:
            before = len(self._items)
            self._items = [item for item in self._items if item.get("id") != target]
            if len(self._items) == before:
                return False
            self._save_locked()
            return True

    def record_stat(self, channel_id: str, hits: int = 0,
                    error: Optional[str] = None) -> Optional[dict]:
        """累加命中数并记录扫描时间/错误；渠道不存在时返回 None。"""
        target = _as_text(channel_id)
        with self._lock:
            for item in self._items:
                if item.get("id") != target:
                    continue
                stats = item.setdefault("stats", _new_stats())
                stats["hits"] = _as_int(stats.get("hits"), 0, 0, 2**31 - 1) + max(0, _as_int(hits, 0, 0, 2**31 - 1))
                stats["last_scan"] = time.time()
                if error is not None:
                    stats["last_error"] = str(error)
                self._save_locked()
                return copy.deepcopy(item)
        return None

    def _initial_search_config(self, config: Dict[str, Any], definition: Any) -> dict:
        """迁移时把插件配置里该源现有的配置值抄进渠道。"""
        keys = definition.get_config_keys()
        return {
            key: copy.deepcopy(config[key]) for key in keys if key in config
        }

    def migrate_from_config(self, config: dict) -> int:
        """渠道文件缺失或损坏时从旧配置生成渠道，返回新增数量。

        已有有效文件时返回 0——用户在渠道页删空也不复活，损坏/丢失才按
        旧配置重建。搜索源按注册表每源一条：``search_source_order`` 里的
        启用、优先级按在 order 里的下标递减；不在 order 里的排在后面且
        停用；配置项缺失时默认只有 ``pansou`` 启用。TG 频道按
        ``tg_channel_rules`` 每条规则建一条 ``tg_channel``。
        """
        with self._lock:
            if self._file.exists() and not self._load_error:
                return 0
            old = dict(config or {})
            raw_order = old.get("search_source_order")
            if raw_order is None:
                order = ["pansou"]
            elif isinstance(raw_order, str):
                order = [
                    item.strip().lower()
                    for item in re.split(r"[,，\n]+", raw_order) if item.strip()
                ]
            else:
                order = [
                    str(item).strip().lower()
                    for item in (raw_order or []) if str(item).strip()
                ]
            order = list(dict.fromkeys(order))
            definitions = self._all_search_definitions()
            by_id = {definition.id: definition for definition in definitions}
            order = [source for source in order if source in by_id]
            in_order = set(order)
            not_in_order = [
                definition for definition in definitions
                if definition.id not in in_order
            ]
            migrated: list[dict] = []
            for index, source_id in enumerate(order):
                definition = by_id[source_id]
                migrated.append(self._build_item({
                    "name": getattr(definition, "name", source_id),
                    "type": source_id,
                    "enabled": True,
                    "priority": SEARCH_PRIORITY_BASE - 10 * index,
                    "config": self._initial_search_config(old, definition),
                }))
            for position, definition in enumerate(not_in_order):
                migrated.append(self._build_item({
                    "name": getattr(definition, "name", definition.id),
                    "type": definition.id,
                    "enabled": False,
                    "priority": -10 * (position + 1),
                    "config": self._initial_search_config(old, definition),
                }))
            from .tg_monitor import normalize_channel_rules

            rules = normalize_channel_rules(old.get("tg_channel_rules", []))
            for index, rule in enumerate(rules):
                migrated.append(self._build_item({
                    "name": rule["channel"],
                    "type": TG_CHANNEL_TYPE,
                    "enabled": bool(rule.get("enabled", True)),
                    "priority": TG_PRIORITY_BASE - 10 * index,
                    "config": {
                        "channel": rule["channel"],
                        "keywords": list(rule.get("keywords") or []),
                        "exclude_keywords": list(rule.get("exclude_keywords") or []),
                        "scan_limit": int(rule.get("scan_limit") or 20),
                    },
                }))
            if not migrated:
                return 0
            self._items.extend(migrated)
            self._sort_locked()
            self._save_locked()
            self._load_error = False
            return len(migrated)


class ChannelMixin:
    """ChannelMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def _channel_store(self) -> ChannelStore:
        """返回渠道存储；未初始化时按当前数据目录惰性创建并迁移旧配置。"""
        if self._channels is None:
            self._channels = ChannelStore(self.get_data_path())
            if self._channels.needs_migration:
                self._migrate_channels_from_config(
                    self._applied_config or self.get_config() or {}
                )
        return self._channels


    def _migrate_channels_from_config(self, config: Dict[str, Any]) -> int:
        """首次加载且无 channels.json 时，从旧配置生成渠道后写盘。

        旧版全局 TG 配置（``tg_channels`` / ``tg_monitor_keywords`` 等）
        在这里先展开成逐频道规则再迁移，避免后一步的
        :meth:`_apply_tg_monitor_config` 转换完却写不进已落盘的渠道库。
        写盘后旧配置保留，之后渠道库是唯一运行来源。
        """
        store = self._channel_store()
        if not store.needs_migration:
            return 0
        cfg = dict(config or {})
        rules = cfg.get("tg_channel_rules", []) or []
        channels = self._split_multiline(cfg.get("tg_channels", ""))
        if not rules and channels:
            from .tg_monitor import migrate_legacy_channel_rules

            try:
                scan_limit = int(cfg.get("tg_monitor_scan_limit", 20) or 20)
            except (TypeError, ValueError):
                scan_limit = 20
            rules = migrate_legacy_channel_rules(
                [],
                channels=channels,
                keywords=self._split_multiline(
                    cfg.get("tg_monitor_keywords", "")
                ),
                exclude_keywords=self._split_multiline(
                    cfg.get("tg_monitor_exclude_keywords", "")
                ),
                scan_limit=max(1, min(scan_limit, 200)),
            )
            cfg["tg_channel_rules"] = rules
        added = store.migrate_from_config(cfg)
        if added:
            logger.info(f"已从旧配置迁移 {added} 条渠道到 channels.json")
        return added


    def get_enabled_search_sources(self) -> List[str]:
        """返回渠道库里已启用的搜索源 id（按 priority 倒序，去重）。"""
        ordered: List[str] = []
        for channel in self._channel_store().list():
            if channel.get("type") == "tg_channel":
                continue
            if not channel.get("enabled"):
                continue
            source = str(channel.get("type") or "").strip().lower()
            if source and source not in ordered:
                ordered.append(source)
        return ordered


    def get_search_channel_order(self) -> List[str]:
        """返回全部搜索渠道的展示顺序（渠道库 priority 倒序，含停用的）。"""
        ordered: List[str] = []
        for channel in self._channel_store().list():
            if channel.get("type") == "tg_channel":
                continue
            source = str(channel.get("type") or "").strip().lower()
            if source and source not in ordered:
                ordered.append(source)
        try:
            registered = [
                str(definition.id)
                for definition in SearchSourceRegistry.get_definitions()
            ]
        except Exception as error:  # noqa: BLE001 - 注册表异常不影响已有顺序
            logger.warning(f"读取搜索渠道注册表失败：{error}")
            registered = []
        for source in registered:
            if source not in ordered:
                ordered.append(source)
        return ordered


    def disable_search_channel(self, source: str) -> bool:
        """封禁保护：停用指定搜索源的全部渠道并清搜索缓存。"""
        source = str(source or "").strip().lower()
        if not source:
            return False
        store = self._channel_store()
        changed = False
        for channel in store.list():
            if channel.get("type") == source and channel.get("enabled"):
                store.update(channel["id"], {"enabled": False})
                changed = True
        if changed:
            self._search_source_order = self.get_enabled_search_sources()
            handler = getattr(self, "_search_handler", None)
            if handler is not None:
                try:
                    handler.clear_search_cache()
                except Exception as error:
                    logger.debug(f"停用渠道后清搜索缓存失败：{error}", exc_info=True)
        return changed


    def _tg_channel_rules_from_store(self) -> List[Dict[str, Any]]:
        """把渠道库里的 tg_channel 渠道转成监控用的规则列表。"""
        rules: List[Dict[str, Any]] = []
        for channel in self._channel_store().list():
            if channel.get("type") != "tg_channel":
                continue
            config = channel.get("config") or {}
            rules.append({
                "channel": str(config.get("channel") or ""),
                "enabled": bool(channel.get("enabled", True)),
                "keywords": list(config.get("keywords") or []),
                "exclude_keywords": list(config.get("exclude_keywords") or []),
                "scan_limit": int(config.get("scan_limit") or 20),
                "auto_transfer": True,
                "monitor_mode": "subscriptions",
            })
        return rules

    def _tg_importable_channels(self, x_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """X 配置里有、Lite 渠道库里没有的 TG 频道规则。

        只用于「从 X 导入」：运行态频道只读渠道库，导入只写配置会让
        页面仍显示 0 个频道；这里只算缺失项——配置里有、库里没有的才
        算，绝不复活用户在渠道页主动删掉的频道。X 用逐频道规则
        （tg_channel_rules）时直接归一化；只有旧全局频道
        （tg_channels）时按 _migrate_channels_from_config 同一套办法
        展开成逐频道规则。
        """
        from .tg_monitor import (
            migrate_legacy_channel_rules,
            normalize_channel_rules,
        )

        cfg = dict(x_cfg or {})
        rules = normalize_channel_rules(cfg.get("tg_channel_rules", []) or [])
        if not rules:
            channels = self._split_multiline(cfg.get("tg_channels", ""))
            if not channels:
                return []
            try:
                scan_limit = int(cfg.get("tg_monitor_scan_limit", 20) or 20)
            except (TypeError, ValueError):
                scan_limit = 20
            rules = migrate_legacy_channel_rules(
                [],
                channels=channels,
                keywords=self._split_multiline(
                    cfg.get("tg_monitor_keywords", "")
                ),
                exclude_keywords=self._split_multiline(
                    cfg.get("tg_monitor_exclude_keywords", "")
                ),
                scan_limit=max(1, min(scan_limit, 200)),
            )
        existing = set()
        for item in self._channel_store().list():
            if item.get("type") != TG_CHANNEL_TYPE:
                continue
            channel = str((item.get("config") or {}).get("channel") or "").strip()
            if channel:
                existing.add(channel.lower().lstrip("@"))
        missing: List[Dict[str, Any]] = []
        for rule in rules:
            channel = str(rule.get("channel") or "").strip()
            if not channel:
                continue
            if channel.lower().lstrip("@") in existing:
                continue
            missing.append({
                "channel": channel,
                "enabled": bool(rule.get("enabled", True)),
                "keywords": list(rule.get("keywords") or []),
                "exclude_keywords": list(rule.get("exclude_keywords") or []),
                "scan_limit": int(rule.get("scan_limit") or 20),
            })
        return missing


    def _refresh_channel_runtime(
            self, search_config_updates: Optional[Dict[str, Any]] = None
    ) -> None:
        """渠道增删改启停后刷新运行态：搜索端重读渠道、TG 监控重读规则。

        搜索结果缓存一并清掉，否则开关渠道后搜同一关键词会直接命中旧结果，
        渠道开关看起来“不生效”。只有搜索渠道的配置值真的变了才重建搜索
        处理器（客户端在注册时按配置值固化）；只改启用/优先级时轻量刷新。
        """
        self._search_source_order = self.get_enabled_search_sources()
        handler = getattr(self, "_search_handler", None)
        if search_config_updates:
            for key, value in search_config_updates.items():
                setattr(self, f"_{key}", value)
            if handler is not None:
                try:
                    handler.close(release_cache=True)
                except Exception as error:  # noqa: BLE001
                    logger.debug(f"关闭旧搜索处理器失败：{error}", exc_info=True)
                self._search_handler = SearchHandler(plugin=self)
        elif handler is not None:
            refresh = getattr(handler, "refresh_enabled_sources", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception as error:  # noqa: BLE001
                    logger.warning(f"刷新搜索渠道运行态失败：{error}")
        try:
            self._apply_tg_monitor_config(
                dict(self._applied_config or self.get_config() or {})
            )
        except Exception as error:  # noqa: BLE001 - 监控刷新失败不拖垮接口
            logger.warning(f"刷新 TG 监控运行态失败：{error}")
