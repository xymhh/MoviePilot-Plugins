"""搜索渠道元数据过滤：只保留后台启用的渠道。

资源页/抽屉搜索与订阅搜索必须同口径——后台只勾了 PanSou 就不该去问
HDHive/Dian115，否则既慢，候选里又全是「需积分解锁」的无效项。
单独成模块便于单测（避免 import 整个 API 层拉进 MP 运行时依赖）。
"""

from typing import Any, Dict, List


def filter_source_meta_by_enabled(
        available_meta: List[Dict[str, Any]],
        enabled_sources: List[str],
) -> List[Dict[str, Any]]:
    """按启用顺序过滤渠道元数据；未启用的渠道、未知与重复 key 一律丢弃。"""
    if not enabled_sources:
        return []
    by_key = {
        str(meta.get("key") or "").strip().lower(): meta
        for meta in (available_meta or [])
        if isinstance(meta, dict)
    }
    ordered: List[Dict[str, Any]] = []
    seen = set()
    for key in enabled_sources:
        clean = str(key or "").strip().lower()
        if not clean or clean in seen:
            continue
        meta = by_key.get(clean)
        if meta is None:
            continue
        seen.add(clean)
        ordered.append(meta)
    return ordered
