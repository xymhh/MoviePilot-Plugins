"""SourceCacheMixin：CloudSubscribeLite 入口拆出的混入类。

方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
"""

from __future__ import annotations

from typing import Optional


class SourceCacheMixin:
    """SourceCacheMixin：从 CloudSubscribeLite 入口拆出的逻辑。

    方法由重构脚本逐字搬移（行为不变），入口类通过多继承混入。
    """

    def get_cached_source_sha1(self, key: str) -> Optional[dict]:
        """按「来源网盘|体积|文件名」取库中记录过的源文件 SHA1。"""
        key = str(key or "").strip()
        if not key:
            return None
        entry = self._get_data_store().get_source_sha1(
            key, ttl_seconds=self._SOURCE_SHA1_CACHE_TTL_DAYS * 86400
        )
        if not entry:
            return None
        return {"sha1": entry.get("sha1") or "", "size": entry.get("size") or 0}


    def put_cached_source_sha1(self, key: str, entry: dict) -> None:
        """记录源文件 SHA1，供秒传未命中后的零流量重试使用。"""
        key = str(key or "").strip()
        payload = entry or {}
        if not key or not str(payload.get("sha1") or "").strip():
            return
        self._get_data_store().put_source_sha1(
            key,
            str(payload.get("sha1") or "").strip().upper(),
            size=int(payload.get("size") or 0),
            name=str(payload.get("name") or ""),
            limit=self._SOURCE_SHA1_CACHE_LIMIT,
        )


    def get_cached_rapid_miss(self, target_key: str, sha1: str) -> int:
        """返回同一 (目标盘, SHA1) 的秒传未命中时间戳，0=没有或已过期。"""
        return self._get_data_store().get_source_miss(
            target_key, sha1,
            ttl_seconds=self._SOURCE_MISS_CACHE_TTL_HOURS * 3600,
        )


    def put_cached_rapid_miss(self, target_key: str, sha1: str) -> None:
        """记录一次秒传未命中，供后续同哈希任务直接跳过下载。"""
        self._get_data_store().put_source_miss(
            target_key, sha1, limit=self._SOURCE_MISS_CACHE_LIMIT
        )
