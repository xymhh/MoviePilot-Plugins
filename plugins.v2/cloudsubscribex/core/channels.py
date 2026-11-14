"""渠道数据模型与 JSON 持久化（网盘订阅助手）。

渠道是插件的一等对象：16 个搜索源与 TG 监控频道都通过统一的
``channels.json`` 增删改启停。模块级只依赖标准库和宿主日志；
搜索源定义与 TG 规则归一化在函数内延迟导入，避免导入环。
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.log import logger

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
