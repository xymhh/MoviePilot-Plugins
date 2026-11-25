"""配置页选项与详情页数据 API。"""

from __future__ import annotations

import copy
import os
from typing import Any, Dict
from urllib.parse import quote, urlencode
from app.core.config import settings
from app.log import logger
from app.schemas.types import NotificationType

from ..cloud import CloudDriveCapability
from ..delegation import OwnerDelegator
from ..config import UIConfig
from ...drive.scanner import DriverRegistry
from ...search.scanner import SearchSourceRegistry
from ...search.types import (
    resource_type_aliases,
    resource_type_catalog,
)
from ...utils.cache import create_platform_ttl_cache


def _display_catalog() -> dict:
    """资源类型与搜索渠道的展示元数据：名称、图标、配色与顺序全部由后端下发。"""
    return {
        "resource_types": resource_type_catalog(),
        "resource_type_aliases": resource_type_aliases(),
        "sources": SearchSourceRegistry.get_source_catalog(),
    }


_UI_OPTIONS_CACHE = create_platform_ttl_cache(
    "ui:options", maxsize=16, ttl=2 * 60
)



def _platform_image_url(value: object, size: str = "w500") -> str:
    """使用 MoviePilot V3 官方图片缓存代理生成前端可直接加载的地址。"""
    raw = str(value or "").strip()
    if not raw or raw.startswith(("data:", "blob:")):
        return raw
    if not raw.lower().startswith(("http://", "https://")):
        if raw.startswith("/") and not raw.startswith("//"):
            raw = f"https://image.tmdb.org/t/p/{size}{raw}"
        else:
            builder = getattr(settings, "TMDB_IMAGE_URL", None)
            raw = builder(raw, size) if callable(builder) else raw
    return f"/api/v1/system/cache/image?url={quote(raw, safe='')}" if raw else ""


def _resolve_subscribe_tmdb_id(subscribe: object):
    """解析订阅的 TMDB ID。

    MoviePilot V3 起订阅模型改用 media_source + media_id（字符串）承载外部 ID，
    旧的 tmdbid / tmdb_id 字段在 V3 数据里常为空 —— 只读旧字段会让按 tmdb 合并
    转存历史的键全部失配，表现为「头部说缺 17 集、矩阵却把 93-106 涂成已入库」。
    """
    for attr in ("tmdbid", "tmdb_id"):
        value = getattr(subscribe, attr, None)
        if value not in (None, "", 0, "0"):
            return value

    media_id = getattr(subscribe, "media_id", None)
    if media_id in (None, "", 0, "0"):
        return None

    source = str(getattr(subscribe, "media_source", "") or "").strip().lower()
    # 未标注来源时按 TMDB 处理（V3 剧集订阅绝大多数来自 TMDB）
    if source and "tmdb" not in source and "themoviedb" not in source:
        return None
    return media_id






class PageApi(OwnerDelegator):
    def api_vue_page_data(
            self,
            page: int = 1,
            page_size: int = 10,
            keyword: str = "",
            status: str = "",
    ) -> dict:
        """失败记录页数据（v1.13.3 瘦页）：只列重试索引里的失败、跨盘成功
        待修复与处理中行，不再分页展示全量转存历史；status 可按行状态过滤
        （失败 / 成功（待修复）/ 处理中）。处理中行要列出：前端统计卡片
        可按它过滤，删除与清空入口又明确排除它——只计数不列行会让卡片
        点进去永远是空的。"""
        try:
            page_no = max(1, int(page or 1))
        except (TypeError, ValueError):
            page_no = 1
        try:
            size = max(1, min(100, int(page_size or 10)))
        except (TypeError, ValueError):
            size = 10
        needle = str(keyword or "").strip().casefold()
        status_filter = str(status or "").strip()
        try:
            store = self._get_data_store()
            store.initialize()
            rows = store.repositories.retry_index.list_page_rows() or []
        except Exception as err:
            logger.warning(f"读取重试索引失败：{err}")
            rows = []
        try:
            from ...core.retry_index import index_row_to_history_dict
            failures = [index_row_to_history_dict(row) for row in rows]
        except Exception as err:
            logger.warning(f"重试索引转换失败：{err}")
            failures = []
        # v1.30.0 阶段 4：漏斗失败行在失败记录页标 source“整理漏斗”
        #（按 finalize_key 批量查漏斗行；查不到按原 source，不改行）。
        try:
            store = self._get_data_store()
            funnel_repo = store.repositories.funnel_queue
            keys = {
                str(record.get("finalize_key") or "").strip()
                for record in failures
                if str(record.get("finalize_key") or "").strip()
            }
            if keys and funnel_repo is not None:
                funnel_rows = funnel_repo.pending_keys_with_rows(keys)
                for record in failures:
                    key = str(record.get("finalize_key") or "").strip()
                    if key and key in funnel_rows:
                        record["source"] = "整理漏斗"
        except Exception as err:  # noqa: BLE001 - 标注失败不挡列表
            logger.debug(f"失败记录页漏斗 source 标注失败：{err}", exc_info=True)
        if status_filter:
            failures = [
                record for record in failures
                if str(record.get("status") or "") == status_filter
            ]
        if needle:
            failures = [
                record for record in failures
                if needle in str(record.get("title") or "").casefold()
                or needle in str(record.get("file_name") or "").casefold()
                or needle in str(record.get("share_url") or "").casefold()
            ]
        total = len(failures)
        total_pages = max(1, (total + size - 1) // size)
        page_no = min(page_no, total_pages)
        page_records = failures[(page_no - 1) * size:page_no * size]
        return {
            "success": True,
            "data": {
                **_display_catalog(),
                "failures": page_records,
                "history_page": {
                    "page": page_no,
                    "page_size": size,
                    "total": total,
                    "total_pages": total_pages,
                    "filter_options": {
                        "resource_types": [],
                        "sources": [],
                    },
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                },
            },
        }

    def api_vue_history_summary(self) -> dict:
        """失败记录页统计（v1.13.3）：重试索引里的失败/待修复/处理中行数。"""
        try:
            store = self._get_data_store()
            store.initialize()
            rows = store.repositories.retry_index.list_all() or []
        except Exception as err:
            logger.warning(f"读取重试索引失败：{err}")
            rows = []
        failed = sum(1 for row in rows if str(row.get("status") or "") == "失败")
        repair = sum(
            1 for row in rows
            if str(row.get("status") or "") == "成功"
            and row.get("landed") is False
        )
        processing = sum(1 for row in rows if str(row.get("status") or "") == "处理中")
        return {
            "success": True,
            "data": {
                "failed": failed,
                "repair": repair,
                "processing": processing,
                "total": len(rows),
            },
        }

    def _get_plugin_config(self) -> Dict[str, Any]:
        """获取当前插件的完整持久化或运行时配置字典。"""
        owner = getattr(self, "_owner", None)
        if owner:
            if hasattr(owner, "get_config"):
                try:
                    cfg = owner.get_config()
                    if isinstance(cfg, dict) and cfg:
                        return cfg
                except Exception:
                    logger.debug("_get_plugin_config 分支忽略异常", exc_info=True)
            if hasattr(owner, "_applied_config"):
                cfg = getattr(owner, "_applied_config", None)
                if isinstance(cfg, dict) and cfg:
                    return cfg
            if hasattr(owner, "_config"):
                cfg = getattr(owner, "_config", None)
                if isinstance(cfg, dict) and cfg:
                    return cfg
        return getattr(self, "__dict__", {}) or {}

    def api_vue_ui_options(self, scope: str = "base", refresh: bool = False) -> dict:
        normalized_scope = str(scope or "base").strip().lower()
        normalized_scope = {
            "transfer": "subscriptions",
            "upgrade": "subscriptions",
            "manual": "subscriptions",
        }.get(normalized_scope, normalized_scope)
        if normalized_scope not in {
            "base", "subscriptions", "drive", "search", "pansou", "notify", "checkin"
        }:
            return {"success": False, "message": "未知的配置选项范围"}
        cache_key = f"instance:{id(self)}:{normalized_scope}"
        if refresh:
            _UI_OPTIONS_CACHE.pop(cache_key, None)
        cached = _UI_OPTIONS_CACHE.get(cache_key)
        if isinstance(cached, dict):
            return copy.deepcopy(cached)

        if normalized_scope == "checkin":
            from ..checkin_manager import get_checkin_schemas
            result = {
                "success": True,
                "data": {
                    "checkin_schemas": get_checkin_schemas(),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "pansou":
            from ...search.pansou.definition import PanSouSourceDefinition
            plugin_cfg = self._get_plugin_config()
            pansou_options = PanSouSourceDefinition.get_dynamic_options(
                plugin_cfg,
                {"proxy": getattr(self, "_search_proxy", None) or plugin_cfg.get("search_proxy")},
            )
            result = {"success": True, "data": {"pansou": pansou_options}}
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "subscriptions":
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            target_key = str(
                getattr(self._cloud_drive, "key", "")
                or getattr(self, "_cloud_drive_key", "")
            ).strip().lower()
            target_accepts_cross_transfer = bool(
                self._cloud_drive
                and self._cloud_drive.supports(CloudDriveCapability.LOCAL_UPLOAD)
                and self._cloud_drive.supports(CloudDriveCapability.FILE_QUERY)
            )
            cloud_drives = []
            for provider in providers:
                if not provider.supports(CloudDriveCapability.DIRECTORY_READ):
                    continue
                direct = provider.key == target_key
                cross = bool(
                    not direct
                    and bool(getattr(self, "_cross_transfer_enabled", False))
                    and target_accepts_cross_transfer
                    and provider.supports(CloudDriveCapability.FILE_QUERY)
                    and provider.supports(CloudDriveCapability.FILE_DOWNLOAD)
                )
                if not direct and not cross:
                    continue
                cloud_drives.append({
                    "title": provider.name,
                    "value": provider.key,
                    "mode": "direct" if direct else "cross",
                })
            result = {
                "success": True,
                "data": {
                    "subscribes": UIConfig.get_subscribe_options_grouped(),
                    "transfer_cloud_drives": cloud_drives,
                    "cloud_drives": cloud_drives,
                    "target_cloud_drive": target_key,
                    "enable_cloud_upgrade": bool(
                        getattr(self, "_enable_cloud_upgrade", False)
                    ),
                    "cross_transfer_media_types": sorted(
                        str(value) for value in getattr(
                            self, "_cross_transfer_media_types", set()
                        )
                    ),
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope in {"base", "drive"}:
            providers = (
                self._cloud_drive_registry.available()
                if self._cloud_drive_registry else []
            )
            if normalized_scope == "base":
                from ..checkin_manager import get_checkin_schemas
                provider_map = {p.key: p for p in providers}
                cloud_drives = []
                for def_cls in DriverRegistry.get_definitions():
                    p = provider_map.get(def_cls.id)
                    if p:
                        caps = sorted(capability.value for capability in p.capabilities)
                        rtypes = sorted(p.resource_types)
                        policy = {
                            "pagination_mode": p.policy.pagination_mode,
                            "max_page_size": p.policy.max_page_size,
                            "supports_batch": p.policy.supports_batch,
                            "max_batch_size": p.policy.max_batch_size,
                            "supports_cancel": p.policy.supports_cancel,
                            "max_concurrency": p.policy.max_concurrency,
                            "cache_ttl_seconds": dict(p.policy.cache_ttl_seconds),
                        }
                    else:
                        caps = [c.value for c in getattr(def_cls, "capabilities", ())]
                        rtypes = list(getattr(def_cls, "resource_types", ()))
                        policy = {}
                    cloud_drives.append({
                        "title": def_cls.name,
                        "value": def_cls.id,
                        "capabilities": caps,
                        "resource_types": rtypes,
                        "policy": policy,
                    })
                result = {
                    "success": True,
                    "data": {
                        **_display_catalog(),
                        "defaults": UIConfig.normalize_config(UIConfig.get_default_config()),
                        "mediaservers": UIConfig.get_media_server_options(),
                        "checkin_schemas": get_checkin_schemas(),
                        "cloud_drives": cloud_drives,
                    },
                }
            else:
                accounts = {}
                for provider in providers:
                    if not provider.supports(CloudDriveCapability.ACCOUNT):
                        continue
                    accounts[provider.key] = self._cached_account_info(
                        f"drive:{provider.key}",
                        {
                            "connected": False,
                            "error": "点击刷新按钮读取账户信息",
                        },
                    )
                result = {
                    "success": True,
                    "data": {
                        "account": accounts.get(self._cloud_drive_key, {
                            "connected": False,
                            "error": "请先配置当前网盘账号",
                        }),
                        "accounts": accounts,
                        "driver_schemas": DriverRegistry.get_driver_schemas(),
                    },
                }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        if normalized_scope == "notify":
            mediaservers = UIConfig.get_media_server_options()
            result = {
                "success": True,
                "data": {
                    "mediaservers": mediaservers,
                    "media_library_webhook_urls": {
                        str(item.get("value") or ""): (
                                "/api/v1/webhook/?"
                                + urlencode({
                            "token": settings.API_TOKEN,
                            "source": str(item.get("value") or ""),
                        })
                        )
                        for item in mediaservers
                        if str(item.get("type") or "").strip().lower() == "emby"
                           and str(item.get("value") or "").strip()
                    },
                    "notification_types": [
                        {"title": item.value, "value": item.name}
                        for item in NotificationType
                    ],
                },
            }
            _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
            return result

        search_accounts = {}
        for def_cls in SearchSourceRegistry.get_definitions():
            search_accounts[def_cls.id] = self._cached_account_info(
                f"search:{def_cls.id}",
                {
                    "connected": False,
                    "error": f"配置并保存 {def_cls.name} 账户后读取账户信息",
                },
            )
        from ...search.pansou.definition import PanSouSourceDefinition
        plugin_cfg = self._get_plugin_config()
        pansou_options = PanSouSourceDefinition.get_dynamic_options(
            plugin_cfg,
            {"proxy": getattr(self, "_search_proxy", None) or plugin_cfg.get("search_proxy")},
        )
        available_sources = []
        search_handler = getattr(self, "_search_handler", None)
        if search_handler and hasattr(search_handler, "get_available_sources_meta"):
            available_sources = search_handler.get_available_sources_meta()

        result = {
            "success": True,
            "data": {
                **_display_catalog(),
                "defaults": UIConfig.normalize_config(UIConfig.get_default_config()),
                "search_accounts": search_accounts,
                "pansou": pansou_options,
                "available_sources": available_sources,
                "search_schemas": SearchSourceRegistry.get_search_schemas(),
            },
        }
        _UI_OPTIONS_CACHE.set(cache_key, copy.deepcopy(result))
        return result


    def api_vue_cloud_directories(
            self, path: str = "/", provider: str = "", refresh: bool = False
    ) -> dict:
        """列出指定或当前网盘目录，供配置页选择转存路径。"""
        normalized_path = str(path or "/").strip()
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(
                CloudDriveCapability.DIRECTORY_READ
        ):
            return {"success": False, "message": "当前网盘不支持目录浏览"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            if refresh:
                service.refresh_directories()
            directories = service.list_directories(normalized_path)
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            current_path = ""
            for part in (item for item in normalized_path.split("/") if item):
                current_path = f"{current_path}/{part}"
                breadcrumbs.append({"name": part, "path": current_path})
            return {
                "success": True,
                "data": {
                    "path": normalized_path,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取网盘目录失败：{normalized_path}，{error}")
            return {"success": False, "message": f"读取网盘目录失败：{error}"}

    def api_vue_create_cloud_directory(self, payload: dict) -> dict:
        """在目录选择器当前目录创建子文件夹。"""
        request = payload or {}
        path = request.get("path", "/")
        name = request.get("name", "")
        provider = request.get("provider", "")
        normalized_path = str(path or "/").strip() or "/"
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        normalized_path = normalized_path.rstrip("/") or "/"
        folder_name = str(name or "").strip()
        if (
                not folder_name
                or folder_name in {".", ".."}
                or "/" in folder_name
                or "\\" in folder_name
        ):
            return {"success": False, "message": "文件夹名称无效"}
        drive = self._cloud_drive
        provider_key = str(provider or "").strip().lower()
        if provider_key and self._cloud_drive_registry:
            try:
                drive = self._cloud_drive_registry.get(provider_key)
            except KeyError:
                return {"success": False, "message": "网盘提供方不存在"}
        if not drive or not drive.supports(CloudDriveCapability.DIRECTORY_READ):
            return {"success": False, "message": "当前网盘不支持目录操作"}
        try:
            service = drive.require(CloudDriveCapability.DIRECTORY_READ)
            target_path = f"{normalized_path.rstrip('/')}/{folder_name}" if normalized_path != "/" else f"/{folder_name}"
            lookup = service.resolve_directory(target_path, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return {"success": False, "message": "创建文件夹失败"}
            return {"success": True, "data": {"path": target_path}}
        except Exception as error:
            logger.error(f"创建网盘目录失败：{target_path if 'target_path' in locals() else folder_name}，{error}")
            return {"success": False, "message": f"创建文件夹失败：{error}"}

    def api_vue_local_directories(self, path: str = "/") -> dict:
        """列出本地（宿主机/容器）指定目录下的子目录，供配置页选择本地路径。"""
        from pathlib import Path

        normalized_path = str(path or "/").strip()
        if not normalized_path:
            normalized_path = "/"
        # 兼容 Windows 根目录或 Unix 根目录
        target = Path(normalized_path)
        if not target.is_absolute():
            target = Path("/").resolve()

        if not target.exists():
            # 尝试向上回溯到存在的父目录
            while not target.exists() and target.parent != target:
                target = target.parent

        target_str = str(target.as_posix()) if hasattr(target, "as_posix") else str(target).replace("\\", "/")
        if not target_str.startswith("/"):
            target_str = f"/{target_str}"

        try:
            directories = []
            if target.is_dir():
                try:
                    with os.scandir(target) as entries:
                        for entry in entries:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    name = entry.name
                                    if name.startswith("."):
                                        continue
                                    full_p = str(Path(entry.path).as_posix())
                                    if not full_p.startswith("/"):
                                        full_p = f"/{full_p}"
                                    directories.append({
                                        "id": full_p,
                                        "name": name,
                                        "path": full_p,
                                    })
                            except (PermissionError, OSError):
                                continue
                except (PermissionError, OSError) as perm_err:
                    logger.warning(f"扫描本地目录权限受限：{target_str}，{perm_err}")

            directories.sort(key=lambda item: str(item.get("name", "")).lower())

            # 生成面包屑导航
            breadcrumbs = [{"name": "根目录", "path": "/"}]
            curr = ""
            for part in [p for p in target_str.split("/") if p]:
                curr = f"{curr}/{part}"
                breadcrumbs.append({"name": part, "path": curr})

            return {
                "success": True,
                "data": {
                    "path": target_str,
                    "breadcrumbs": breadcrumbs,
                    "directories": directories,
                },
            }
        except Exception as error:
            logger.error(f"读取本地目录失败：{target_str}，{error}")
            return {"success": False, "message": f"读取本地目录失败：{error}"}

    def api_vue_create_local_directory(self, payload: dict) -> dict:
        """在本地当前目录创建子文件夹。"""
        from pathlib import Path

        request = payload or {}
        parent_path = str(request.get("path") or "/").strip()
        name = str(request.get("name") or "").strip()

        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            return {"success": False, "message": "文件夹名称无效"}

        parent = Path(parent_path)
        target = parent / name
        try:
            target.mkdir(parents=True, exist_ok=False)
            target_str = str(target.as_posix())
            if not target_str.startswith("/"):
                target_str = f"/{target_str}"
            return {"success": True, "data": {"path": target_str}}
        except FileExistsError:
            return {"success": False, "message": "同名文件夹已存在"}
        except (PermissionError, OSError) as error:
            logger.error(f"创建本地目录失败：{target}，{error}")
            return {"success": False, "message": f"创建本地目录失败：{error}"}


def clear_ui_options_cache() -> int:
    caches = (
        _UI_OPTIONS_CACHE,
    )
    count = sum(len(list(cache.items())) for cache in caches)
    for cache in caches:
        cache.clear()
    return count
