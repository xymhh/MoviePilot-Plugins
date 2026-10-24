"""STRM 播放直链：pickcode → 网盘直链的 302 跳转。

STRM 文件里只保存 ``pickcode``（115 直链约 12h 过期，不能落盘），
由本端点实时换取直链并 302。对标 P115StrmHelper 的 ``/redirect_url``，
但直接复用 CS 自己的网盘客户端，无需外部直链服务。
"""

import time
from typing import Any, Dict, Optional, Tuple

from app.log import logger
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from .. import CloudDriveCapability, CloudFile, OwnerDelegator

#: pickcode 固定 17 位字母数字（115）
_PICKCODE_LENGTH = 17
#: 直链缓存 TTL；115 直链约 12h 有效，取半衰期留余量
_CACHE_TTL_SECONDS = 6 * 3600
#: 缓存条目上限
_CACHE_MAX_ENTRIES = 512


class RedirectApi(OwnerDelegator):
    """把 STRM 中的 pickcode 换成直链并 302 到网盘 CDN。"""

    _link_cache: Dict[str, Tuple[str, float]] = {}

    # ------------------------------------------------------------------ 缓存
    @classmethod
    def _cache_get(cls, key: str) -> Optional[str]:
        entry = cls._link_cache.get(key)
        if not entry:
            return None
        url, expires_at = entry
        if expires_at <= time.time():
            cls._link_cache.pop(key, None)
            return None
        return url

    @classmethod
    def _cache_put(cls, key: str, url: str) -> None:
        if len(cls._link_cache) >= _CACHE_MAX_ENTRIES:
            now = time.time()
            expired = [k for k, (_, exp) in cls._link_cache.items() if exp <= now]
            for item in expired[: _CACHE_MAX_ENTRIES // 4]:
                cls._link_cache.pop(item, None)
            while len(cls._link_cache) >= _CACHE_MAX_ENTRIES:
                cls._link_cache.pop(next(iter(cls._link_cache)), None)
        cls._link_cache[key] = (url, time.time() + _CACHE_TTL_SECONDS)

    # ------------------------------------------------------------------ 换链
    def _resolve_link(self, pickcode: str, file_name: str) -> Tuple[str, int]:
        """返回 (直链, 错误码)；错误时直链为空。"""
        cached = self._cache_get(pickcode)
        if cached:
            return cached, 0

        provider = getattr(self._owner, "_cloud_drive", None)
        if provider is None:
            logger.warning("302 跳转：网盘客户端未初始化")
            return "", 503
        try:
            downloader = provider.require(CloudDriveCapability.FILE_DOWNLOAD)
        except Exception as error:  # noqa: BLE001 - 能力缺失按服务不可用处理
            logger.warning(f"302 跳转：当前网盘不支持直链（{error}）")
            return "", 503

        item = CloudFile(
            id="",
            name=file_name or f"{pickcode}.mkv",
            is_directory=False,
            playback_values={"pickcode": pickcode},
        )
        try:
            url, _headers = downloader.resolve_download_link(item)
        except Exception as error:  # noqa: BLE001 - 上游异常统一转 502
            logger.warning(f"302 跳转：换取直链失败 pickcode={pickcode}（{error}）")
            return "", 502
        if not url:
            return "", 502
        self._cache_put(pickcode, str(url))
        return str(url), 0

    # ------------------------------------------------------------------ 路由
    async def api_strm_redirect(
            self, request: Request, pickcode: str = "", file_name: str = "",
    ) -> Response:
        """GET：302 到直链；HEAD：返回元信息供播放器探测。"""
        pickcode = str(pickcode or "").strip()
        file_name = str(file_name or "").strip()

        if not pickcode:
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": "缺少 pickcode 参数"},
            )
        if len(pickcode) != _PICKCODE_LENGTH or not pickcode.isalnum():
            return JSONResponse(
                status_code=400,
                content={"success": False, "message": f"pickcode 非法：{pickcode}"},
            )

        url, error_code = self._resolve_link(pickcode, file_name)
        if error_code:
            return JSONResponse(
                status_code=error_code,
                content={
                    "success": False,
                    "message": "获取网盘直链失败" if error_code != 503
                    else "网盘客户端不可用",
                    "pickcode": pickcode,
                },
            )

        if request.method == "HEAD":
            headers: Dict[str, Any] = {"Accept-Ranges": "bytes"}
            return Response(status_code=200, headers=headers)

        logger.debug(f"302 跳转：pickcode={pickcode} 文件={file_name or '-'}")
        return RedirectResponse(
            url=url, status_code=302, headers={"Cache-Control": "no-store"},
        )
