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
#: 直链缓存 TTL。115 直链实测寿命远短于官方描述：
#: 2026-10-26 实测 19:46 签发的直链 20:56 已返回
#: ``{"message":"request expired","status":403}``（≤70 分钟），
#: 缓存 6h 会让 ffprobe / 定时提取任务拿到失效链接（403 无媒体信息）。
#: 取 45 分钟，低于实测寿命且不会显著抬高换链频次。
_CACHE_TTL_SECONDS = 45 * 60
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
    def _resolve_link(
            self, pickcode: str, file_name: str, user_agent: str = "",
    ) -> Tuple[str, int]:
        """返回 (直链, 错误码)；错误时直链为空。

        User-Agent 参与缓存键：115 的直链签名绑定取链时的 UA（``f=1``），
        同一个 pickcode 在不同播放器上必须各自换链。
        """
        cache_key = f"{pickcode}|{user_agent or ''}"
        cached = self._cache_get(cache_key)
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
            url, _headers = downloader.resolve_download_link(
                item, user_agent=user_agent or "",
            )
        except TypeError:
            # 老能力实现不接受额外参数（理论上已全部支持）
            try:
                url, _headers = downloader.resolve_download_link(item)
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    f"302 跳转：换取直链失败 pickcode={pickcode}（{error}）"
                )
                return "", 502
        except Exception as error:  # noqa: BLE001 - 上游异常统一转 502
            logger.warning(f"302 跳转：换取直链失败 pickcode={pickcode}（{error}）")
            return "", 502
        if not url:
            return "", 502
        self._cache_put(cache_key, str(url))
        return str(url), 0

    def _resolve_share_link(
            self, share_code: str, receive_code: str, file_id: str,
            user_agent: str = "",
    ) -> Tuple[str, int]:
        """分享文件换链（不转存）：返回 (直链, 错误码)。"""
        cache_key = f"share|{share_code}|{receive_code}|{file_id}|{user_agent or ''}"
        cached = self._cache_get(cache_key)
        if cached:
            return cached, 0

        provider = getattr(self._owner, "_cloud_drive", None)
        if provider is None:
            logger.warning("302 跳转：网盘客户端未初始化")
            return "", 503
        try:
            share_service = provider.require(CloudDriveCapability.SHARE_TRANSFER)
        except Exception as error:  # noqa: BLE001
            logger.warning(f"302 跳转：当前网盘不支持分享换链（{error}）")
            return "", 503

        try:
            url = share_service.resolve_share_download_link(
                share_code, receive_code, file_id, user_agent=user_agent or "",
            )
        except Exception as error:  # noqa: BLE001 - 上游异常统一转 502
            logger.warning(
                f"302 跳转：分享换链失败 share_code={share_code} id={file_id}（{error}）"
            )
            return "", 502
        if not url:
            return "", 502
        self._cache_put(cache_key, str(url))
        return str(url), 0

    # ------------------------------------------------------------------ 路由
    async def api_strm_redirect(
            self, request: Request, pickcode: str = "", file_name: str = "",
            share_code: str = "", receive_code: str = "", id: str = "",
    ) -> Response:
        """GET：302 到直链；HEAD：返回元信息供播放器探测。

        ``share_code`` 非空时按「分享文件」换链（不转存），否则走 pickcode。
        """
        pickcode = str(pickcode or "").strip()
        file_name = str(file_name or "").strip()
        share_code = str(share_code or "").strip()
        receive_code = str(receive_code or "").strip()
        share_file_id = str(id or "").strip()

        if share_code:
            try:
                user_agent = str(request.headers.get("user-agent") or "")
            except AttributeError:  # 测试替身可能没有 headers
                user_agent = ""
            url, error_code = self._resolve_share_link(
                share_code, receive_code, share_file_id, user_agent,
            )
            if error_code:
                return JSONResponse(
                    status_code=error_code,
                    content={
                        "success": False,
                        "message": "获取分享直链失败" if error_code != 503
                        else "网盘客户端不可用",
                        "share_code": share_code,
                        "id": share_file_id,
                    },
                )
            if request.method == "HEAD":
                return Response(
                    status_code=200, headers={"Accept-Ranges": "bytes"},
                )
            logger.debug(
                f"302 跳转：share_code={share_code} id={share_file_id}"
            )
            return RedirectResponse(
                url=url, status_code=302,
                headers={"Cache-Control": "no-store"},
            )

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

        try:
            user_agent = str(request.headers.get("user-agent") or "")
        except AttributeError:  # 测试替身可能没有 headers
            user_agent = ""
        url, error_code = self._resolve_link(pickcode, file_name, user_agent)
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
