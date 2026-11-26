"""一键体检：把「哪一环坏了」聚合成红绿灯，替代翻容器日志。

设计见 docs/plan/2026-11-02-Doctor体检-架构.md：
每项检查独立 try/except，单项异常只让该行变红，绝不让整次体检 500。
"""

import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.log import logger

from ..delegation import OwnerDelegator
from ..bypass_guard import bypass_guard
from ...utils.cache import cache_stats

#: 转存历史超过这么多小时没有成功记录 → 黄灯
HISTORY_STALE_HOURS = 72
#: TMDB 探活超时（秒）
TMDB_PROBE_TIMEOUT = 6
TMDB_PROBE_URL = "https://api.themoviedb.org/3/configuration"

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_ERROR = "error"
STATUS_SKIP = "skip"

CheckResult = Tuple[str, str, Optional[str], Optional[str]]


def _parse_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


class DoctorApi(OwnerDelegator):
    """插件体检：网盘 / 搜索 / TMDB / 私有库 / 目录 / 缓存。"""

    def api_vue_doctor(self) -> dict:
        """执行一次体检，返回逐项红绿灯。"""
        started = time.time()
        context: Dict[str, Any] = {}
        items: List[Dict[str, Any]] = []
        for key, name, check in self._checks():
            item_started = time.time()
            try:
                status, detail, hint, fix_tab = check(context)
            except Exception as error:
                logger.warning(f"体检项 {key} 执行失败：{error}", exc_info=True)
                status, detail, hint, fix_tab = (
                    STATUS_ERROR,
                    f"检查本身出错：{type(error).__name__}: {error}",
                    "这是插件缺陷，请把这条日志发给开发者",
                    None,
                )
            items.append({
                "key": key,
                "name": name,
                "status": status,
                "detail": detail,
                "hint": hint,
                "fix_tab": fix_tab,
                "ms": int((time.time() - item_started) * 1000),
            })

        counts = {status: 0 for status in (
            STATUS_OK, STATUS_WARN, STATUS_ERROR, STATUS_SKIP,
        )}
        for item in items:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        if counts[STATUS_ERROR]:
            overall = STATUS_ERROR
        elif counts[STATUS_WARN]:
            overall = STATUS_WARN
        else:
            overall = STATUS_OK

        return {
            "success": True,
            "data": {
                "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "overall": overall,
                "counts": counts,
                "elapsed_ms": int((time.time() - started) * 1000),
                "items": items,
            },
        }

    # ------------------------------------------------------------------ 检查项

    def _checks(self) -> List[Tuple[str, str, Callable[[Dict[str, Any]], CheckResult]]]:
        return [
            ("plugin_db", "插件私有库", self._check_plugin_db),
            ("subscribe", "订阅缺口", self._check_subscribe),
            ("search", "搜索渠道", self._check_search),
            ("drive", "当前网盘", self._check_drive),
            ("tmdb", "TMDB 刮削", self._check_tmdb),
            ("media_dir", "整理目录", self._check_media_dir),
            ("cache", "缓存健康", self._check_cache),
            ("bypass", "旁路能力", self._check_bypass),
        ]

    def _check_bypass(self, context: Dict[str, Any]) -> CheckResult:
        """签到 / STRM / 媒体库刷新 / 通知 / aria2 的熔断状态。"""
        rows = bypass_guard.snapshot()
        if not rows:
            return STATUS_OK, "旁路尚未运行，暂无异常记录", None, None
        cooling = [row for row in rows if row.get("state") == "cooling"]
        if not cooling:
            return STATUS_OK, f"{len(rows)} 项旁路状态正常", None, None
        detail = "冷却中：" + "、".join(
            f"{row['name']}（剩余 {max(1, int(row['remaining_seconds'] // 60))} 分钟）"
            for row in cooling
        )
        last_error = next(
            (row.get("last_error") for row in cooling if row.get("last_error")), ""
        )
        hint = "旁路失败不影响转存主链路；按提示修好对应能力后会自行恢复"
        if last_error:
            hint = f"{hint}。最近错误：{last_error}"
        if len(cooling) == len(rows):
            return STATUS_ERROR, detail, hint, None
        return STATUS_WARN, detail, hint, None

    def _index_rows(self, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """重试索引只在插件私有 SQLite，且一次体检只读一遍。"""
        if "retry_index" not in context:
            store = self._get_data_store()
            store.initialize()
            context["retry_index"] = store.repositories.retry_index.list_all() or []
            context["db_path"] = str(getattr(store.manager, "db_path", "") or "")
        return context["retry_index"]

    def _check_plugin_db(self, context: Dict[str, Any]) -> CheckResult:
        rows = self._index_rows(context)
        db_path = context.get("db_path") or ""
        size_mb = 0.0
        if db_path and os.path.exists(db_path):
            size_mb = round(os.path.getsize(db_path) / 1024 / 1024, 1)
        failed = sum(1 for row in rows if str(row.get("status") or "") == "失败")
        repair = sum(
            1 for row in rows
            if str(row.get("status") or "") == "成功"
            and row.get("landed") is False
        )
        detail = f"重试索引 {len(rows)} 行（失败 {failed}，待修复 {repair}），库文件 {size_mb} MB"
        if not db_path:
            return STATUS_WARN, detail + "（未取到库路径）", "检查插件数据目录挂载", None
        if not os.path.exists(db_path):
            return STATUS_ERROR, f"库文件不存在：{db_path}", "插件数据目录未持久化，重启会丢索引", None
        if failed >= 20:
            return STATUS_WARN, detail, "近期失败偏多，去失败记录页看具体原因", None
        return STATUS_OK, detail, None, None

    def _check_subscribe(self, context: Dict[str, Any]) -> CheckResult:
        subscribes = self._load_subscribes()
        total = len(subscribes)
        if not total:
            return (
                STATUS_WARN,
                "MoviePilot 里没有订阅",
                "先加一条订阅，插件才有活可干",
                None,
            )
        movies = sum(1 for sub in subscribes if self._is_movie(sub))
        tv = total - movies
        pending = 0
        for sub in subscribes:
            if self._is_movie(sub):
                continue
            try:
                lack = int(getattr(sub, "lack_episode", 0) or 0)
            except (TypeError, ValueError):
                lack = 0
            if lack > 0:
                pending += 1
        detail = f"订阅 {total} 条（剧 {tv} / 影 {movies}），其中 {pending} 部剧有缺集"
        if pending:
            return STATUS_OK, detail, None, None
        return STATUS_OK, detail + "，当前没有待补任务", None, None

    def _load_subscribes(self) -> List[Any]:
        try:
            from app.db import SessionFactory
            from app.db.models.subscribe import Subscribe
            with SessionFactory() as db:
                return list(db.query(Subscribe).all())
        except Exception as error:
            logger.warning(f"体检读取订阅失败：{error}")
            try:
                from app.db.subscribe_oper import SubscribeOper
                return list(SubscribeOper().list() or [])
            except Exception as inner:
                logger.warning(f"体检回退读取订阅失败：{inner}")
                return []

    @staticmethod
    def _is_movie(sub: Any) -> bool:
        media_type = str(getattr(sub, "type", "") or "")
        if media_type in ("电影", "movie"):
            return True
        return getattr(sub, "season", None) in (None, "") and not getattr(sub, "total_episode", None)

    def _check_search(self, context: Dict[str, Any]) -> CheckResult:
        registry = getattr(getattr(self, "_search_handler", None), "_search_registry", None)
        if registry is None:
            return (
                STATUS_WARN,
                "搜索注册表未初始化",
                "保存一次配置后再体检",
                "search",
            )
        try:
            providers = list(registry.available())
        except Exception as error:
            return STATUS_ERROR, f"读取搜索渠道失败：{error}", "检查搜索配置", "search"
        if not providers:
            return (
                STATUS_ERROR,
                "没有任何可用搜索渠道",
                "去「搜索」页启用至少一个渠道，否则缺集永远补不上",
                "search",
            )
        names = []
        for provider in providers[:6]:
            names.append(str(getattr(provider, "id", "") or getattr(provider, "name", "") or "?"))
        detail = f"可用渠道 {len(providers)} 个：{'、'.join(names)}"
        if len(providers) <= 1:
            return STATUS_WARN, detail, "只剩一个渠道，单点故障就全灭，建议再启用一个", "search"
        return STATUS_OK, detail, None, None

    def _check_drive(self, context: Dict[str, Any]) -> CheckResult:
        manager = getattr(self, "_drive_manager", None)
        if manager is None:
            return STATUS_ERROR, "网盘管理器未初始化", "重新保存配置触发初始化", "drive"
        active = getattr(manager, "active_drive", None)
        drive_key = str(getattr(self, "_cloud_drive_key", "") or "")
        if active is None:
            return (
                STATUS_ERROR,
                f"当前网盘 [{drive_key or '未设置'}] 未注册成功",
                "去「网盘」页重新扫码/填写凭据，注册成功后本项会转绿",
                "drive",
            )
        return STATUS_OK, f"当前网盘 [{drive_key or '未知'}] 已就绪", None, None

    def _check_tmdb(self, context: Dict[str, Any]) -> CheckResult:
        started = time.time()
        try:
            import requests
            proxies = None
            try:
                from app.core.config import settings
                proxies = getattr(settings, "PROXY", None) or None
            except Exception:
                proxies = None
            response = requests.get(
                TMDB_PROBE_URL,
                timeout=TMDB_PROBE_TIMEOUT,
                proxies=proxies,
                headers={"User-Agent": "MoviePilot-CloudSubscribeLite-Doctor"},
            )
            elapsed = int((time.time() - started) * 1000)
            # 401/403 说明网络通、只是没带密钥，对「能不能连上」是绿灯
            if response.status_code in (200, 401, 403):
                return STATUS_OK, f"TMDB 可达，{elapsed} ms", None, None
            return (
                STATUS_WARN,
                f"TMDB 返回 HTTP {response.status_code}",
                "刮削可能失败，检查代理或 TMDB 域名设置",
                None,
            )
        except Exception as error:
            return (
                STATUS_WARN,
                f"TMDB 不可达：{type(error).__name__}",
                "网络或代理问题，缺集识别与海报会受影响",
                None,
            )

    def _check_media_dir(self, context: Dict[str, Any]) -> CheckResult:
        path = str(getattr(self, "_organize_source_path", "") or "").strip()
        if not path:
            return STATUS_SKIP, "未配置整理源目录", "不用整理功能可忽略", None
        if not os.path.isdir(path):
            return STATUS_ERROR, f"目录不存在：{path}", "改配置里的整理源目录", "drive"
        if not os.access(path, os.W_OK):
            return STATUS_ERROR, f"目录不可写：{path}", "检查容器挂载权限", "drive"
        return STATUS_OK, f"目录可读写：{path}", None, None

    def _check_cache(self, context: Dict[str, Any]) -> CheckResult:
        stats = cache_stats()
        hit_rate = stats.get("hit_rate", 0.0)
        decode_fail = int(stats.get("decode_fail", 0) or 0)
        detail = f"命中率 {hit_rate}%，解码失败 {decode_fail} 次"
        if decode_fail > 0:
            return (
                STATUS_WARN,
                detail,
                "缓存里有旧类对象，重启插件可清掉；持续增长请报给开发者",
                None,
            )
        return STATUS_OK, detail, None, None
