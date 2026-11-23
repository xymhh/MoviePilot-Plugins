"""网盘整理漏斗：队列 + worker + 提交前复查（v1.29.7 阶段 1）。

设计见 ``cloudsubscribex-organize-funnel-design.md`` §12 阶段 1。

阶段 1 只接手动提交（手动转存入口 + 媒体服务器洗版入口）：转存管线在
finalize 边界（``SyncHandler._queue_file_finalize_batch``）写
``funnel_queue`` 行并把 pending 记录标记为漏斗归属，离线监控跳过这类
记录；漏斗 worker 逐行认领——整理前改名、落盘前实查、触发 MP 整理——
再驱动已有的单项后处理（``_organize_and_finalize_single_item``）走
同一条终态提交（``_commit_single_item``），不另写 STRM/通知/历史。

不在阶段 1 范围内的事：TG 监控、订阅轮次、洗版薄入口仍走旧流水线；
扫描对账、轮次历史、孤儿认领是阶段 4（v1.30.0）的事，见本文件末尾。
"""

from __future__ import annotations

import datetime
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.interval import IntervalTrigger
from app.core.config import settings
from app.db import SessionFactory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.schemas.types import MediaType

from ...core import OwnerDelegator
from ...core.media import list_subscribes_by_tmdb_id, tmdb_id_of

#: 漏斗 worker 的调度任务 id（复用离线监控的“有待处理才建 job、
#: 队列空即停 job”模式，见 core/services/offline.py）。
FUNNEL_WORKER_JOB_ID = "CloudSubscribe_FunnelWorker"
#: worker 轮询间隔（秒）。
FUNNEL_WORKER_INTERVAL_SECONDS = 20
#: 失败退避（秒）：第 1..4 次失败分别等 10 秒、60 秒、300 秒、900 秒；
#: 第 5 次失败后挂起待人工复核（不再自动重试）。
#:
#: v1.29.10 热修复（评审 B3）：旧表 (10, 60, 300, 900, 3600) 的 3600
#: 档永远取不到——第 5 次失败时 attempts+1 已到上限 5，先挂起、再
#: 也轮不到查表（_fail_row 在 attempts+1 >= max_attempts 时直接 held）；
#: 实际总退避只有 10+60+300+900 = 1270 秒（约 21 分钟），不是文档
#: §8 写的 81 分钟。表与文档同步改成四档。
FUNNEL_RETRY_DELAYS = (10, 60, 300, 900)
#: 每轮最多处理的行数。
FUNNEL_MAX_ROWS_PER_ROUND = 50
#: 未结算行下次重试的最小间隔（秒）：与离线监控首档检查间隔一致。
FUNNEL_UNSETTLED_RETRY_SECONDS = 20
#: 轮次历史只保留最近这么多条（v1.30.0 阶段 4；形态复用
#: OrganizeService 的 history，只保留最近 20 条）。
FUNNEL_ROUND_HISTORY_LIMIT = 20
#: held 行原因里孤儿文件位置的标记前缀（v1.30.0 阶段 4；队列页
#: “删除孤儿文件”解析用，位置形如 ``云盘目录/文件名``）。
FUNNEL_ORPHAN_LOCATION_MARKER = "orphan_location="


class FunnelService(OwnerDelegator):
    """漏斗队列服务：入队、worker 轮次、提交前复查。

    挂在 SyncHandler 的 COMPONENT_FIELDS 里（字段名 ``funnel``），
    宿主是 SyncHandler；配置与存储都经宿主读插件侧（配置在
    ``SyncHandler._init_funnel_flags`` 里从插件配置读入，存储走
    插件的 ``_get_data_store()``——注意 SyncHandler 本身没有
    ``_get_data_store`` 属性，直接 getattr 会拿到 None，必须经
    ``_plugin`` 取）。
    """

    # 单轮互斥（形态复用插件的 _offline_monitor_lock：类属性 RLock，
    # 同一时间只跑一轮）。
    _funnel_worker_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 配置（经宿主读插件配置；未初始化时取设计默认值）
    # ------------------------------------------------------------------
    @property
    def _phase1_enabled(self) -> bool:
        return bool(getattr(self, "_funnel_phase1_enabled", False))

    @property
    def _target_dir(self) -> str:
        return str(getattr(self, "_funnel_target_dir", "") or "/Alist/影视/moviepilot/转存/")

    @property
    def _trigger(self) -> str:
        return str(getattr(self, "_funnel_trigger", "") or "immediate").strip().lower()

    @property
    def _lease_seconds(self) -> int:
        try:
            value = int(getattr(self, "_funnel_inflight_lease_seconds", 7200) or 7200)
        except (TypeError, ValueError):
            value = 7200
        return max(60, min(value, 86400))

    @property
    def _max_attempts(self) -> int:
        try:
            value = int(getattr(self, "_funnel_max_attempts", 5) or 5)
        except (TypeError, ValueError):
            value = 5
        return max(1, min(value, 20))

    @property
    def _dedup_strict(self) -> bool:
        return bool(getattr(self, "_funnel_dedup_strict", True))

    @property
    def _scan_dry_run(self) -> bool:
        # v1.30.0 阶段 4：04:00 扫描对账默认 dry-run（只分类记日志，
        # 不入队、不删文件）；关掉也不接管（接管留给以后一版）。
        return bool(getattr(self, "_funnel_scan_dry_run", True))

    # ------------------------------------------------------------------
    # 存储
    # ------------------------------------------------------------------
    def _repository(self):
        """取 funnel_queue 仓储；存储不可用时返回 None（调用方 fail-open）。"""
        plugin = getattr(self._owner, "_plugin", None)
        get_store = getattr(plugin, "_get_data_store", None) if plugin is not None else None
        if not callable(get_store):
            # 测试替身可能直接挂在 SyncHandler 上。
            get_store = getattr(self._owner, "_get_data_store", None)
        if not callable(get_store):
            return None
        try:
            store = get_store()
            store.initialize()
            return store.repositories.funnel_queue
        except Exception as error:  # noqa: BLE001 - 存储不可用时调用方按旧路走
            logger.debug(f"漏斗队列存储不可用：{error}", exc_info=True)
            return None

    def _scheduler_registry(self):
        """插件唯一的定时任务注册表；拿不到时返回 None（不建 job）。"""
        plugin = getattr(self._owner, "_plugin", None)
        getter = getattr(plugin, "_scheduler_registry", None) if plugin is not None else None
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception as error:  # noqa: BLE001 - 注册表不可用时不建 job
            logger.debug(f"漏斗取调度注册表失败：{error}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # 去重键
    # ------------------------------------------------------------------
    @staticmethod
    def _item_episode(item: Dict[str, Any]) -> int:
        """从 pending 项取集号：episode 字段优先，其次成功/通知集数列表首项。"""
        try:
            episode = int(item.get("episode") or 0)
        except (TypeError, ValueError):
            episode = 0
        if episode > 0:
            return episode
        for key in ("success_episodes", "notification_episodes"):
            for value in (item.get(key) or []):
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    return number
        return 0

    def _item_dedup_key(
            self, item: Dict[str, Any], media: Any,
    ) -> Optional[Tuple[str, Optional[int], int, int]]:
        """从 pending 项 + MediaInfo 取去重键；身份缺失时返回 None（fail-open）。

        剧集 (media_type, tmdb_id, season, episode)，电影
        (media_type, tmdb_id, 0, 0)；剧集取不到集号时返回 None。
        """
        media_type = ""
        tmdb_id = None
        if media is not None:
            media_type = "tv" if getattr(media, "type", None) == MediaType.TV else "movie"
            tmdb_id = tmdb_id_of(media)
        if not tmdb_id:
            media_data = item.get("mediainfo") if isinstance(item, dict) else None
            if isinstance(media_data, dict):
                tmdb_id = tmdb_id_of(media_data)
                raw_type = str(
                    media_data.get("type") or media_data.get("media_type") or ""
                ).strip().lower()
                if "tv" in raw_type or raw_type in {"电视剧", "电视剧集"}:
                    media_type = "tv"
                elif raw_type in {"movie", "电影"}:
                    media_type = "movie"
        if not tmdb_id:
            return None
        if media_type == "tv":
            season = int(item.get("season") or 1)
            episode = self._item_episode(item)
            if episode <= 0:
                return None
            return ("tv", tmdb_id, season, episode)
        return ("movie", tmdb_id, 0, 0)

    # ------------------------------------------------------------------
    # 入队（finalize 边界）
    # ------------------------------------------------------------------
    def _build_enqueue_payload(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
    ) -> Dict[str, Any]:
        """入队载荷（v1.29.10 热修复，B2 替换路复用）：把 pending 项转成
        funnel_queue 行字段；与 enqueue_from_pending 的旧内联逻辑一致。
        """
        media_type = "movie"
        tmdb_id = None
        if mediainfo is not None:
            media_type = "tv" if getattr(mediainfo, "type", None) == MediaType.TV else "movie"
            tmdb_id = tmdb_id_of(mediainfo)
        if media_type == "tv":
            season_value = int(season or item.get("season") or 1)
            episode_value = self._item_episode(item)
        else:
            season_value = 0
            episode_value = 0
        title = str(
            getattr(mediainfo, "title", "") or item.get("file_name") or ""
        ).strip()
        is_upgrade = bool(item.get("upgrade"))
        replaced_from: List[Dict[str, Any]] = []
        if is_upgrade:
            if item.get("upgrade_old_file_id"):
                replaced_from.append({
                    "file_id": str(item.get("upgrade_old_file_id") or ""),
                    "cloud_dir": str(item.get("upgrade_old_cloud_dir") or ""),
                    "file_name": str(item.get("upgrade_old_file_name") or ""),
                    "file_size": int(item.get("upgrade_old_size") or 0),
                })
            replaced_from.extend(
                dict(version)
                for version in (item.get("dedup_extra_old_versions") or [])
                if isinstance(version, dict)
            )
        task_type = str(item.get("task_type") or "").strip().lower()
        # v1.29.8 阶段 2：通知类型走唯一派生点（_derive_notification_kind），
        # 不由生产者手写；组件不可用时（测试替身）回退到等价内联公式。
        derive_kind = getattr(
            getattr(self._owner, "transfer_details", None),
            "_derive_notification_kind", None,
        )
        if callable(derive_kind):
            notification_kind = derive_kind(
                is_upgrade=is_upgrade,
                replaced_from=replaced_from or None,
                cross=task_type in {"cross_cloud", "cross"},
            )
        else:
            notification_kind = (
                "upgrade" if (is_upgrade or replaced_from)
                else "cross_transfer" if task_type in {"cross_cloud", "cross"}
                else "transfer"
            )
        # v1.29.8 阶段 2：TG 行的 source 即 Candidate.via()
        # （tg:{channel}/{message_id} 或 push），原样带入，列表里显示频道名。
        # v1.29.9 阶段 3：订阅轮次行的 source 为 subscribe:<id>（设计 §5.1；
        # 生产者标记 funnel_producer 由 _do_sync 随订阅对象下沉，经
        # _ingest_stage_confirm 带进 finalize 边界的项）；洗版项 source
        # 仍为 upgrade，手动项为 manual。
        via = str(item.get("discovered_via") or "").strip()
        producer = str(item.get("funnel_producer") or "").strip().lower()
        if via:
            source = via
        elif producer == "subscribe" and subscribe_id:
            source = f"subscribe:{int(subscribe_id)}"
        elif producer == "subscribe":
            source = "subscribe"
        else:
            source = "upgrade" if is_upgrade else "manual"
        media_data = None
        serialize = getattr(self, "_serialize_mediainfo", None)
        if callable(serialize) and mediainfo is not None:
            try:
                media_data = serialize(mediainfo)
            except Exception as error:  # noqa: BLE001 - 序列化失败不挡入队
                logger.debug(f"漏斗入队序列化媒体信息失败：{error}", exc_info=True)
                media_data = None
        return {
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "season": season_value,
            "episode": episode_value,
            "title": title,
            "file_id": str(item.get("result_key") or ""),
            "file_name": str(item.get("file_name") or ""),
            "file_size": int(item.get("file_size") or 0),
            "sha1": str(item.get("source_sha1") or ""),
            "cloud_dir": str(item.get("cloud_dir") or ""),
            "staging_dir": str(item.get("staging_dir") or item.get("cloud_dir") or ""),
            "staging_name": str(item.get("staging_name") or item.get("file_name") or ""),
            "source": source,
            "subscribe_id": subscribe_id,
            "score": int(item.get("dedup_new_score") or 0),
            "quality": str(item.get("quality") or ""),
            "is_upgrade": is_upgrade,
            "replaced_from": replaced_from or None,
            "notification_kind": notification_kind,
            "pending_key": str(pending_key or ""),
            "media_data": media_data,
        }

    def enqueue_from_pending(
            self,
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        """手动/TG 提交在 finalize 边界入队。

        只由 ``SyncHandler._queue_file_finalize_batch`` 在
        ``item["funnel_route"]`` 为真时调用。返回 ``(行字典, 是否新建)``；
        存储不可用时返回 ``(None, False)``（调用方按旧流水线继续）。
        """
        repo = self._repository()
        if repo is None:
            logger.debug("漏斗队列存储不可用，本项不入队，走旧流水线")
            return None, False
        payload = self._build_enqueue_payload(
            item, pending_key, mediainfo,
            subscribe_id=subscribe_id, season=season,
        )
        row, created = repo.enqueue(payload)
        if created:
            via = str(item.get("discovered_via") or "").strip()
            producer = str(item.get("funnel_producer") or "").strip().lower()
            if via:
                producer_label = "TG 转存"
            elif producer == "subscribe":
                producer_label = "订阅轮次"
            else:
                producer_label = "手动提交"
            logger.info(
                f"{producer_label}已入队整理队列：{payload['title'] or payload['file_name']} "
                f"（行 #{row.get('id')}，{pending_key}）"
            )
            self.ensure_worker()
        return row, created

    def replace_row_with_pending(
            self,
            row: Dict[str, Any],
            item: Dict[str, Any],
            pending_key: str,
            mediainfo: Any,
            subscribe_id: Optional[int] = None,
            season: Optional[int] = None,
    ) -> bool:
        """v1.29.10 热修复（B2）：洗版高分候选替换已入队行的文件身份。

        只有 status 仍为 queued 的行能被替换（repo 层按 status='queued'
        守卫；processing 行正在被 worker 驱动，身份换了会打乱在途处理）。
        替换后行仍为 queued、attempts 不变、去重键不变、pending_key
        换成新项的（worker 按新 pending_key 驱动）。"""
        repo = self._repository()
        if repo is None:
            return False
        row_id = int((row or {}).get("id") or 0)
        if row_id <= 0:
            return False
        payload = self._build_enqueue_payload(
            item, pending_key, mediainfo,
            subscribe_id=subscribe_id, season=season,
        )
        replaced = repo.replace_row_file_identity(row_id, payload)
        if replaced:
            logger.info(
                "漏斗洗版替换已入队行："
                f"行 #{row_id} 换成 {payload['file_name']} "
                f"（{pending_key}）"
            )
        return bool(replaced)

    def has_active_rows(self, subscribe_id) -> bool:
        """v1.29.9 阶段 3：指定订阅是否还有漏斗在途行（queued/processing/
        failed）。轮次结束结算前调用：在途行未终态时绝不结算，终态成功
        时由 _finish_pending_subscription 回写。

        v1.29.10 热修复（评审 B4）：存储不可用 / 查询异常时 fail-closed
        （返回 True，暂缓结算），绝不能因“查不到”就把没入库的集结算掉。
        """
        try:
            if int(subscribe_id or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        repo = self._repository()
        if repo is None:
            logger.warning(
                "漏斗仓储不可用，订阅结算暂缓："
                f"subscribe_id={subscribe_id}"
            )
            return True
        has_active = getattr(repo, "has_active_for_subscribe", None)
        if not callable(has_active):
            logger.warning(
                "漏斗仓储缺 has_active_for_subscribe，订阅结算暂缓："
                f"subscribe_id={subscribe_id}"
            )
            return True
        try:
            return bool(has_active(int(subscribe_id)))
        except Exception as error:  # noqa: BLE001 - 查询异常暂缓结算
            logger.warning(
                "查询订阅漏斗在途行失败，订阅结算暂缓："
                f"subscribe_id={subscribe_id}，{error}"
            )
            return True

    def has_unsettled_rows(self, subscribe_id) -> bool:
        """v1.29.10 热修复（评审 B4）：指定订阅是否还有漏斗未终态行
        （queued/processing/failed/**held**）。结算守卫用：held 行的集
        没入库——只有人工放回（终态成功）或删除后，订阅才可能结算。
        查不到/异常时 fail-closed（返回 True，暂缓结算）。"""
        try:
            if int(subscribe_id or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        repo = self._repository()
        if repo is None:
            logger.warning(
                "漏斗仓储不可用，订阅结算暂缓："
                f"subscribe_id={subscribe_id}"
            )
            return True
        has_unsettled = getattr(repo, "has_unsettled_for_subscribe", None)
        if not callable(has_unsettled):
            logger.warning(
                "漏斗仓储缺 has_unsettled_for_subscribe，订阅结算暂缓："
                f"subscribe_id={subscribe_id}"
            )
            return True
        try:
            return bool(has_unsettled(int(subscribe_id)))
        except Exception as error:  # noqa: BLE001 - 查询异常暂缓结算
            logger.warning(
                "查询订阅漏斗未终态行失败，订阅结算暂缓："
                f"subscribe_id={subscribe_id}，{error}"
            )
            return True

    def delete_old_version(self, row_ids) -> Dict[str, Any]:
        """人工删旧版（v1.29.10 热修复，评审 B5）：held/failed 行的
        replaced_from 旧文件由人在队列页点“删旧版”删除。

        只认 held/failed 行；逐个删 ``replaced_from`` 里带 file_id 的
        旧文件；全部删完才清 replaced_from（按钮消失）。删失败的留在
        原地，按钮还在、可重试。返回每行的删成功/删失败数。"""
        repo = self._repository()
        if repo is None:
            return {"success": False, "message": "漏斗仓储不可用"}
        results = []
        for raw_id in row_ids or []:
            try:
                row_id = int(raw_id or 0)
            except (TypeError, ValueError):
                continue
            if row_id <= 0:
                continue
            row = None
            try:
                row = repo.get_by_id(row_id)
            except Exception as error:  # noqa: BLE001 - 读不到按行不存在
                logger.debug(f"删旧版读行 #{row_id} 失败：{error}", exc_info=True)
            if not row:
                results.append({
                    "id": row_id, "success": False, "message": "行不存在",
                })
                continue
            if str(row.get("status") or "") not in {"held", "failed"}:
                results.append({
                    "id": row_id, "success": False,
                    "message": f"只有挂起/失败行能删旧版，当前状态 {row.get('status')}",
                })
                continue
            replaced = row.get("replaced_from") or []
            targets = [
                entry for entry in replaced
                if isinstance(entry, dict)
                and str(entry.get("file_id") or "").strip()
            ]
            if not targets:
                results.append({
                    "id": row_id, "success": False, "message": "该行没有可删的旧版",
                })
                continue
            mutations = getattr(self, "_cloud_mutations", None)
            if mutations is None:
                results.append({
                    "id": row_id, "success": False, "message": "网盘未就绪",
                })
                continue
            deleted = 0
            failed_names = []
            for entry in targets:
                file_id = str(entry.get("file_id") or "").strip()
                try:
                    ok = mutations.delete_file(file_id)
                except Exception as error:  # noqa: BLE001 - 删失败记名
                    logger.debug(f"删旧版 {file_id} 异常：{error}", exc_info=True)
                    ok = False
                if ok:
                    deleted += 1
                else:
                    failed_names.append(
                        str(entry.get("file_name") or file_id)
                    )
            if failed_names:
                results.append({
                    "id": row_id, "success": False, "deleted": deleted,
                    "failed": failed_names,
                    "message": f"已删 {deleted} 个，失败 {len(failed_names)} 个",
                })
                continue
            try:
                repo.clear_replaced_from(row_id)
            except Exception as error:  # noqa: BLE001 - 清字段失败不改删的结论
                logger.debug(f"删旧版清行 #{row_id} 字段失败：{error}", exc_info=True)
            logger.info(f"漏斗行 #{row_id} 人工删旧版 {deleted} 个")
            results.append({
                "id": row_id, "success": True, "deleted": deleted,
            })
        return {"success": True, "results": results}

    # ------------------------------------------------------------------
    # worker 轮次
    # ------------------------------------------------------------------
    def run_round(self) -> Dict[str, int]:
        """worker 一轮：逐行认领处理；同一时间只跑一轮。

        由调度器每 20 秒调一次（队列空时 job 会被停掉）。返回本轮统计。
        """
        stats = {"processed": 0, "done": 0, "failed": 0, "held": 0, "skipped": 0}
        started = int(time.time())
        if not self._funnel_worker_lock.acquire(blocking=False):
            logger.debug("漏斗 worker 已有一轮在跑，本轮跳过")
            return stats
        try:
            if self._stop_requested():
                self._clear_stop()
                self._update_funnel_worker(0)
                return stats
            repo = self._repository()
            if repo is None:
                return stats
            try:
                # v1.29.10 热修复（评审 B3）：租约回收走服务层严格实查
                # （_reclaim_lease_expired）——MP 整理可能仍在跑，盲目
                # 回收会让同一行被两轮并发驱动；查不清时转 held。
                self._reclaim_lease_expired(now=int(time.time()))
            except Exception as error:  # noqa: BLE001 - 回收失败不挡本轮
                logger.debug(f"漏斗租约回收失败：{error}", exc_info=True)
            rows = repo.list_due(limit=FUNNEL_MAX_ROWS_PER_ROUND)
            seen_keys = set()
            for row in rows:
                if self._stop_requested():
                    break
                key = (
                    row.get("media_type"), row.get("tmdb_id"),
                    row.get("season"), row.get("episode"),
                )
                if key in seen_keys:
                    # 唯一约束本应拦住同键两行，这里是断点续跑的兜底。
                    logger.debug(f"漏斗本轮已处理同键，跳过行 #{row.get('id')}")
                    stats["skipped"] += 1
                    continue
                seen_keys.add(key)
                outcome = self._process_row(row)
                stats["processed"] += 1
                if outcome in stats:
                    stats[outcome] += 1
            self._update_funnel_worker_from_repo(repo)
            # v1.30.0 阶段 4：轮次历史（最近 20 轮，队列页展示）。
            self._record_round({
                "kind": "worker",
                "trigger": "worker",
                "started_at": started,
                "finished_at": int(time.time()),
                **stats,
            })
            return stats
        finally:
            try:
                self._funnel_worker_lock.release()
            except RuntimeError:
                logger.debug("漏斗 worker 锁释放时未持有，忽略", exc_info=True)

    def _reclaim_lease_expired(self, now: int) -> Dict[str, int]:
        """租约过期回收（v1.29.10 热修复，评审 B3）：逐行严格实查，
        绝不盲目回收。

        - 行的待终态记录已不在 → held（需人工核对）；
        - 行有 MP 整理身份 → 轮询 tracker：succeeded → held（“MP 整理
          已成功，需人工确认”，绝不重跑）；failed/queued/running/
          missing → 重回队列；unavailable → held；
        - 无身份 → 严格落盘实查（前置条件不全 → held）：已落盘 → held
          （需人工确认，绝不重跑）；未落盘 → 重回队列。
        返回 {"requeued": n, "held": n}。"""
        repo = self._repository()
        if repo is None:
            return {"requeued": 0, "held": 0}
        expired = repo.list_lease_expired(now=now)
        stats = {"requeued": 0, "held": 0}
        for row in expired:
            row_id = int(row.get("id") or 0)
            pending_key = str(row.get("pending_key") or "")
            item = self._load_pending_item(pending_key)
            if item is None:
                repo.mark_held(row_id, reason="租约过期：待终态记录已不在，需人工核对")
                logger.warning(f"漏斗行 #{row_id} 租约过期回收转挂起：待终态记录已不在")
                stats["held"] += 1
                continue
            mp_organize = item.get("mp_organize")
            if isinstance(mp_organize, dict) and mp_organize:
                tracker = None
                try:
                    postprocess = self._owner.postprocess
                    tracker = postprocess._mp_organize_tracker()
                except Exception as error:  # noqa: BLE001 - 取不到轮询器按不可用
                    logger.debug(f"漏斗行 #{row_id} 取 MP 整理轮询器失败：{error}", exc_info=True)
                state = "unavailable"
                if tracker is not None:
                    try:
                        state, _detail, _dest = tracker.poll(mp_organize)
                    except Exception as error:  # noqa: BLE001 - 轮询异常按不可用
                        logger.debug(f"漏斗行 #{row_id} MP 整理轮询异常：{error}", exc_info=True)
                        state = "unavailable"
                if state == "succeeded":
                    repo.mark_held(row_id, reason="租约过期：MP 整理已成功，需人工确认")
                    logger.warning(f"漏斗行 #{row_id} 租约过期回收转挂起：MP 整理已成功")
                    # v1.31.3：同 _fail_row 挂起分支——行已终态，顺手弹出
                    # 待终态记录，否则成孤儿（任务卡永久 95%）。
                    self._pop_pending_record(pending_key)
                    stats["held"] += 1
                elif state in {"failed", "queued", "running", "missing"}:
                    repo.requeue_lease_expired(now=now)
                    logger.info(f"漏斗行 #{row_id} 租约过期回收重回队列（MP 整理 {state}）")
                    stats["requeued"] += 1
                else:
                    repo.mark_held(row_id, reason="租约过期：MP 整理状态查不到，需人工确认")
                    logger.warning(f"漏斗行 #{row_id} 租约过期回收转挂起：MP 整理状态不可用")
                    self._pop_pending_record(pending_key)
                    stats["held"] += 1
                continue
            # 无 MP 整理身份：严格落盘实查。
            media = self._restore_media(row, item)
            landed = self._strict_landed_check(row, item, media)
            if landed is None:
                repo.mark_held(row_id, reason="租约过期：落盘实查不可用，需人工确认")
                logger.warning(f"漏斗行 #{row_id} 租约过期回收转挂起：落盘实查不可用")
                # v1.31.3：同上，挂起即终态，弹出待终态记录防孤儿。
                self._pop_pending_record(pending_key)
                stats["held"] += 1
            elif landed:
                repo.mark_held(row_id, reason="租约过期：文件已落盘，需人工确认")
                logger.warning(f"漏斗行 #{row_id} 租约过期回收转挂起：文件已落盘")
                self._pop_pending_record(pending_key)
                stats["held"] += 1
            else:
                repo.requeue_lease_expired(now=now)
                logger.info(f"漏斗行 #{row_id} 租约过期回收重回队列（文件未落盘）")
                stats["requeued"] += 1
        if stats["held"]:
            self._notify_pending_changed()
        return stats

    def _strict_landed_check(
            self, row: Dict[str, Any], item: Dict[str, Any], media: Any,
    ) -> Optional[bool]:
        """严格落盘实查：本项新文件是否已在目标季目录/电影目录里。

        返回 True（已落盘）/ False（未落盘）/ None（查不到——前置条件
        不全或网盘不可用，调用方按 held 处理，绝不重跑）。"""
        if media is None:
            return None
        dedup = getattr(self._owner, "episode_dedup", None)
        if dedup is None:
            return None
        try:
            if not dedup._episode_dedup_active():
                return None
        except Exception:  # noqa: BLE001 - 判重状态读不到按查不到处理
            return None
        subscribe = self._resolve_subscribe(item)
        if subscribe is None:
            return None
        media_type = getattr(media, "type", None)
        try:
            if media_type == MediaType.TV:
                season = int(item.get("season") or 1)
                versions = dedup._collect_episode_versions(
                    subscribe, media, season, force_refresh=True
                ) or {}
                episode = self._item_episode(item)
                if episode <= 0:
                    return None
                entries = versions.get(episode) or []
            elif media_type == MediaType.MOVIE:
                entries = dedup._find_cloud_movie_versions(subscribe, media) or []
            else:
                return None
        except Exception as error:  # noqa: BLE001 - 列目录异常按查不到处理
            logger.debug(f"漏斗严格落盘实查异常：{error}", exc_info=True)
            return None
        own_file_id = str(row.get("file_id") or item.get("file_id") or "").strip()
        own_file_name = str(item.get("file_name") or "").strip()
        # 与 _other_versions 同口径的 own 判定：file_id 相同，或同名且
        # 至少一边缺 file_id（MP 整理后改名不换 file_id，仍按 id 认）。
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("file_id") or "").strip()
            entry_name = str(entry.get("file_name") or "").strip()
            if own_file_id and entry_id and entry_id == own_file_id:
                return True
            if entry_name and entry_name == own_file_name:
                if not own_file_id or not entry_id:
                    return True
        return False

    def run_now(self) -> Dict[str, Any]:
        """手动触发一轮（整理队列页“立即整理一轮”按钮）。"""
        self._clear_stop()
        stats = self.run_round()
        return {"success": True, "data": stats}

    def request_stop(self) -> Dict[str, Any]:
        """请求停止（整理队列页停止按钮）：本轮跑完即停，不再起新轮。"""
        setattr(self._owner, "_funnel_stop_requested", True)
        self._update_funnel_worker(0)
        return {"success": True, "message": "已请求停止整理队列"}

    def _stop_requested(self) -> bool:
        # 用 is True 而不用 bool()：测试替身的 MagicMock 属性恒真，
        # 只有显式置 True 才算请求停止。
        return getattr(self._owner, "_funnel_stop_requested", False) is True

    def _clear_stop(self) -> None:
        setattr(self._owner, "_funnel_stop_requested", False)

    # ------------------------------------------------------------------
    # 单行处理链
    # ------------------------------------------------------------------
    def _process_row(self, row: Dict[str, Any]) -> str:
        """处理一行；返回 done/failed/held/skipped/processing。"""
        repo = self._repository()
        if repo is None:
            return "skipped"
        row_id = int(row.get("id") or 0)
        now = int(time.time())
        if not repo.claim(row_id, now + self._lease_seconds, now=now):
            return "skipped"
        pending_key = str(row.get("pending_key") or "")
        item = self._load_pending_item(pending_key)
        if item is None:
            # v1.29.10 热修复（评审 B3）：待终态记录已不在，绝不能
            # mark_done——行终态会把“没入库的集”当成已完成结算掉。
            # 转 held 待人工核对（旧流水线已结算、还是记录被删、还是
            # 存储丢了，都由人定）。租约已认领，held 时释放租约。
            repo.mark_held(row_id, reason="待终态记录已不在，需人工核对")
            logger.warning(
                f"漏斗行 #{row_id} 的待终态记录已不在，转挂起待人工核对"
            )
            return "held"
        media = self._restore_media(row, item)
        ok, reason = self._prepare_row(row, item)
        if not ok:
            return self._fail_row(repo, row, reason, item)
        ok, reason = self._pre_organize_check(row, item, media)
        if not ok:
            # 库里已有另一份：不写第二份 STRM/历史/通知；弹出 pending
            # 记录（旧流水线不再碰它），行终态并记短原因，绝不删文件。
            self._pop_pending_record(pending_key)
            repo.mark_done(row_id, reason=reason)
            # v1.29.8 阶段 2 复核修补：拦截路不经过 _write_terminal_state，
            # 须把“已在库”终态同步给频道监控（否则 TG 项一直停在入队时标
            # 的 transferred、缺终态戳）。
            self._sync_row_outcome_to_monitor(item, True, reason)
            logger.info(f"漏斗行 #{row_id} 落盘前实查拦截：{reason}")
            return "done"
        ok, reason, skip = self._trigger_organize(row, item, media)
        if skip:
            self._pop_pending_record(pending_key)
            repo.mark_done(row_id, reason=reason)
            self._sync_row_outcome_to_monitor(item, True, reason)
            logger.info(f"漏斗行 #{row_id} 跳过整理：{reason}")
            return "done"
        if not ok:
            return self._fail_row(repo, row, reason, item)
        return self._drive_postprocess(repo, row, pending_key, item)

    def _load_pending_item(self, pending_key: str) -> Optional[Dict[str, Any]]:
        get_data = getattr(self, "_get_data", None)
        if not callable(get_data):
            return None
        try:
            pending = get_data(self._OFFLINE_PENDING_KEY) or {}
        except Exception as error:  # noqa: BLE001 - 取不到按记录缺失处理
            logger.debug(f"漏斗读取待终态记录失败：{error}", exc_info=True)
            return None
        item = pending.get(pending_key)
        return dict(item) if isinstance(item, dict) else None

    def _pop_pending_record(self, pending_key: str) -> None:
        try:
            get_data = getattr(self, "_get_data", None)
            if not callable(get_data):
                return
            pending = get_data(self._OFFLINE_PENDING_KEY) or {}
            if pending_key not in pending:
                return
            pending.pop(pending_key, None)
            save = getattr(self, "_save_offline_pending", None)
            if callable(save):
                save(pending)
        except Exception as error:  # noqa: BLE001 - 弹出失败只记日志
            logger.debug(f"漏斗弹出待终态记录失败：{error}", exc_info=True)

    def _notify_pending_changed(self) -> None:
        """待终态记录增减后通知宿主刷新任务卡与监控（fail-open）。

        宿主按 count_offline_monitor_pending 口径（漏斗行不计入）重算
        并调 _update_offline_monitor：无待终态项的订阅任务随即翻成
        已完成，下载页卡片 10 分钟保留期后消失。宿主方法缺失时静默
        跳过——下一次同步轮次结束或监控更新时同样会刷新。
        """
        try:
            owner = getattr(self, "_owner", None)
            notify = getattr(owner, "_notify_offline_pending_changed", None)
            counter = getattr(owner, "count_offline_monitor_pending", None)
            get_data = getattr(self, "_get_data", None)
            if not callable(notify) or not callable(counter) or not callable(get_data):
                return
            pending = get_data(self._OFFLINE_PENDING_KEY) or {}
            notify(int(counter(pending)))
        except Exception as error:  # noqa: BLE001 - 通知失败不影响记账
            logger.debug(f"漏斗通知待终态变化失败：{error}", exc_info=True)

    def _restore_media(self, row: Dict[str, Any], item: Dict[str, Any]):
        media_data = row.get("media_data") or item.get("mediainfo")
        if not isinstance(media_data, dict):
            return None
        deserialize = getattr(self, "_deserialize_mediainfo", None)
        if not callable(deserialize):
            return None
        try:
            return deserialize(media_data)
        except Exception as error:  # noqa: BLE001 - 恢复失败按无媒体信息处理
            logger.debug(f"漏斗恢复媒体信息失败：{error}", exc_info=True)
            return None

    # -- 准备：整理前改名 ----------------------------------------------
    def _prepare_row(self, row: Dict[str, Any], item: Dict[str, Any]) -> Tuple[bool, str]:
        """整理前改名：在 115 目标目录内把文件重命名为规范名。

        已有 MP 整理身份（MP 已接手）时不改名；文件已在规范名下时不改名；
        只有“文件还在暂存名下、目标规范名下没有”时才改名。改名失败记失败
        不进整理（重试），绝不带着错名进 MP 整理。
        """
        if item.get("mp_organize"):
            return True, ""
        cloud_dir = str(item.get("cloud_dir") or "").rstrip("/") or "/"
        file_name = str(item.get("file_name") or "").strip()
        staging_dir = str(item.get("staging_dir") or cloud_dir).rstrip("/") or "/"
        staging_name = str(item.get("staging_name") or file_name).strip()
        if not file_name:
            return False, "缺少目标文件名，无法整理"
        try:
            postprocess = self._owner.postprocess
            valid, index = postprocess._cloud_directory_snapshot(cloud_dir, {})
            if valid and file_name in index:
                return True, ""
            rename_dir = None
            rename_file = None
            if staging_name != file_name:
                if valid and staging_name in index:
                    rename_dir, rename_file = cloud_dir, index[staging_name]
                elif staging_dir != cloud_dir:
                    valid_staging, staging_index = (
                        postprocess._cloud_directory_snapshot(staging_dir, {})
                    )
                    if valid_staging and staging_name in staging_index:
                        rename_dir = staging_dir
                        rename_file = staging_index[staging_name]
            if rename_dir is not None:
                drive = getattr(self, "_cloud_drive", None)
                if drive is None:
                    return False, "网盘未就绪，无法改名"
                renamed = drive.rename_file(rename_dir, rename_file, file_name)
                if renamed:
                    logger.info(
                        f"漏斗整理前改名：{rename_dir}/{staging_name} → {file_name}"
                    )
                    return True, ""
                return False, f"改名失败：{rename_dir}/{staging_name} → {file_name}"
            if not valid:
                return False, f"目标目录暂不可读：{cloud_dir}"
            return False, f"目标目录未找到文件：{cloud_dir}/{file_name}"
        except Exception as error:  # noqa: BLE001 - 检查异常按失败重试
            logger.debug(f"漏斗整理前改名检查异常：{error}", exc_info=True)
            return False, f"整理前检查异常：{error}"

    # -- 落盘前实查 ------------------------------------------------------
    def _pre_organize_check(
            self, row: Dict[str, Any], item: Dict[str, Any], media: Any,
    ) -> Tuple[bool, str]:
        """落盘前实查：同一集/同一部电影已有另一份文件时跳过本行。

        返回 (是否继续, 原因)。查不到（身份缺失/订阅缺失/判重关闭/
        查询异常）一律 fail-open 继续；只有实测到另一份文件才拦截。
        洗版项跳过库检查（旧版在提交后才删，库里有旧版是正常的）。
        剧集走季目录实查（双名都算）；电影走目标目录实查
        （``_find_cloud_movie_versions`` 列目录内全部视频文件，
        v1.29.7 阶段 1 修补 1）。
        """
        if media is None:
            return True, ""
        key = self._item_dedup_key(item, media)
        if key is None:
            return True, ""
        media_type = getattr(media, "type", None)
        if media_type not in {MediaType.TV, MediaType.MOVIE}:
            return True, ""
        dedup = getattr(self._owner, "episode_dedup", None)
        if dedup is None:
            return True, ""
        try:
            if not dedup._episode_dedup_active():
                return True, ""
        except Exception:  # noqa: BLE001 - 判重状态读不到按关闭处理
            return True, ""
        subscribe = self._resolve_subscribe(item)
        if subscribe is None:
            return True, ""
        if media_type == MediaType.MOVIE:
            return self._pre_organize_check_movie(row, item, media, dedup, subscribe)
        _, _, season, episode = key
        if episode <= 0:
            return True, ""
        try:
            # v1.29.10 热修复（评审 B1）：落盘前实查强制刷新，绝不读
            # 6 小时 TTL 的季目录快照。
            versions = dedup._collect_episode_versions(
                subscribe, media, season, force_refresh=True
            ) or {}
        except Exception as error:  # noqa: BLE001 - 查不到 fail-open
            logger.debug(f"漏斗落盘前实查异常（fail-open）：{error}", exc_info=True)
            return True, ""
        # v1.29.10 热修复（评审 B5）：洗版不再直通——旧版本除外后仍有
        # 另一份新版时拦截（旧版本按 replaced_from / upgrade_old_* 排除）。
        if bool(item.get("upgrade")):
            others = self._upgrade_other_new_versions(
                versions.get(episode) or [], item, row
            )
        else:
            others = self._other_versions(versions.get(episode) or [], item, row)
        if not others:
            return True, ""
        reason = "已在库（实查）"
        try:
            _, _, decider_reason = dedup._episode_dedup_decision(
                subscribe, media, season, episode,
                int(row.get("score") or 0), int(row.get("file_size") or 0),
                existing_versions=versions,
                new_file_name=str(item.get("file_name") or ""),
            )
            if decider_reason:
                reason = f"已在库（实查）：{decider_reason}"
        except Exception:  # noqa: BLE001 - 判定器异常不改拦截结论
            logger.debug("漏斗预查：判定器异常，仍按拦截处理", exc_info=True)
        return False, reason

    def _pre_organize_check_movie(
            self, row: Dict[str, Any], item: Dict[str, Any], media: Any,
            dedup: Any, subscribe: Any,
    ) -> Tuple[bool, str]:
        """电影落盘前实查：电影目标目录内已有另一份视频文件时拦截。

        电影没有剧集判定器（``_episode_dedup_decision`` 只认季集），
        原因只写“已在库（实查）”，不硬套剧集判定器文案。订阅解析
        不到、目录列取失败、异常一律 fail-open 继续（与剧集口径一致）。
        """
        try:
            versions = dedup._find_cloud_movie_versions(subscribe, media) or []
        except Exception as error:  # noqa: BLE001 - 查不到 fail-open
            logger.debug(f"漏斗电影落盘前实查异常（fail-open）：{error}", exc_info=True)
            return True, ""
        # v1.29.10 热修复（评审 B5）：洗版电影同样走旧版本除外后的
        # 新版检查，不再直通。
        if bool(item.get("upgrade")):
            others = self._upgrade_other_new_versions(versions, item, row)
        else:
            others = self._other_versions(versions, item, row)
        if not others:
            return True, ""
        return False, "已在库（实查）"

    def _other_versions(
            self,
            versions: List[Dict[str, Any]],
            item: Dict[str, Any],
            row: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """同一集里“另一份文件”的版本：不是本项自己的文件。

        own 的判定：file_id 相同，或 file_name 相同且至少一边缺 file_id
        （fail-open：MP 整理精确路径去重是后盾）。
        strict=False（排障）时只认规范名相同的另一份（同名不同 file_id）。
        """
        own_file_id = str(row.get("file_id") or item.get("file_id") or "").strip()
        own_file_name = str(item.get("file_name") or "").strip()
        strict = self._dedup_strict
        others = []
        for version in versions or []:
            if not isinstance(version, dict):
                continue
            version_file_id = str(version.get("file_id") or "").strip()
            version_file_name = str(version.get("file_name") or "").strip()
            if own_file_id and version_file_id and version_file_id == own_file_id:
                continue
            if version_file_name and version_file_name == own_file_name:
                if not own_file_id or not version_file_id:
                    continue
                # 同名不同 file_id：另一份。
            if not strict and version_file_name != own_file_name:
                continue
            others.append(version)
        return others

    def _upgrade_other_new_versions(
            self,
            versions: List[Dict[str, Any]],
            item: Dict[str, Any],
            row: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """洗版实查：同一集里除本项新文件与旧版本之外的另一份新版。

        旧版本来源：行 ``replaced_from``（主旧版 + 额外旧版）、项的
        ``dedup_extra_old_versions``、``upgrade_old_file_id/name``。
        115 改名不换 file_id，备份改名后的旧文件仍按 file_id 排除；
        旧文件已删时不在清单里，自然不计。
        """
        old_ids = set()
        old_names = set()
        for source in (
                (row.get("replaced_from") or [])
                + (item.get("dedup_extra_old_versions") or [])
        ):
            if not isinstance(source, dict):
                continue
            old_id = str(source.get("file_id") or "").strip()
            if old_id:
                old_ids.add(old_id)
            old_name = str(source.get("file_name") or "").strip()
            if old_name:
                old_names.add(old_name)
        old_id = str(item.get("upgrade_old_file_id") or "").strip()
        if old_id:
            old_ids.add(old_id)
        old_name = str(item.get("upgrade_old_file_name") or "").strip()
        if old_name:
            old_names.add(old_name)
        return [
            version
            for version in self._other_versions(versions, item, row)
            if str(version.get("file_id") or "").strip() not in old_ids
            and str(version.get("file_name") or "").strip() not in old_names
        ]

    def _resolve_subscribe(self, item: Dict[str, Any]):
        """按 subscribe_id 或 tmdb_id 解析订阅；解析不到返回 None。"""
        subscribe_id = int(item.get("subscribe_id") or 0)
        try:
            if subscribe_id > 0:
                with SessionFactory() as db:
                    subscribe = SubscribeOper(db=db).get(subscribe_id)
                if subscribe is not None:
                    return subscribe
            media_data = item.get("mediainfo")
            tmdb_id = tmdb_id_of(media_data) if isinstance(media_data, dict) else None
            if tmdb_id:
                season = int(item.get("season") or 1)
                candidates = list_subscribes_by_tmdb_id(
                    SubscribeOper(), tmdb_id, season
                )
                if candidates:
                    return candidates[0]
        except Exception as error:  # noqa: BLE001 - 解析失败 fail-open
            logger.debug(f"漏斗解析订阅异常（fail-open）：{error}", exc_info=True)
        return None

    # -- MP 整理触发 -----------------------------------------------------
    def _trigger_organize(
            self, row: Dict[str, Any], item: Dict[str, Any], media: Any,
    ) -> Tuple[bool, str, bool]:
        """触发 MP 整理。返回 (是否继续, 原因, 是否整行跳过)。

        已有 mp_organize 身份时不重触发；already_in_library 时整行跳过
        （done）；disabled/not_video 时直接进驱动（驱动有直出路径）；
        其余瞬时失败记失败重试。
        """
        if item.get("mp_organize"):
            return True, "", False
        mp_organize = getattr(self._owner, "mp_organize", None)
        if mp_organize is None:
            return True, "", False
        season = int(item.get("season") or 1)
        try:
            accepted, info = mp_organize.trigger_mp_organize(
                item.get("cloud_dir") or "",
                item.get("file_name") or "",
                mediainfo=media,
                season=season,
            )
        except Exception as error:  # noqa: BLE001 - 触发异常按失败重试
            return False, f"触发 MP 整理异常：{error}", False
        info = info or {}
        reason = str(info.get("reason") or "")
        if accepted:
            # 身份写回 pending 记录，驱动轮询用它。
            try:
                get_data = getattr(self, "_get_data", None)
                if callable(get_data):
                    pending = get_data(self._OFFLINE_PENDING_KEY) or {}
                    pending_key = str(row.get("pending_key") or "")
                    if pending_key in pending and isinstance(pending[pending_key], dict):
                        pending[pending_key]["mp_organize"] = info
                        save = getattr(self, "_save_offline_pending", None)
                        if callable(save):
                            save(pending)
                        item["mp_organize"] = info
            except Exception as error:  # noqa: BLE001 - 回写失败不挡整理
                logger.debug(f"漏斗回写 MP 整理身份失败：{error}", exc_info=True)
            return True, "", False
        if reason == "already_in_library":
            return False, "已在库（MP 整理）", True
        if reason in {"disabled", "not_video"}:
            return True, "", False
        return False, str(info.get("message") or f"触发 MP 整理失败：{reason}"), False

    # -- 驱动已有单项后处理 ----------------------------------------------
    def _drive_postprocess(
            self, repo, row: Dict[str, Any], pending_key: str, item: Dict[str, Any],
    ) -> str:
        """驱动已有的单项后处理；按 ctx 的终态计数映射行终态。"""
        postprocess = getattr(self._owner, "postprocess", None)
        if postprocess is None:
            return self._fail_row(repo, row, "后处理服务未就绪", item)
        try:
            ctx = postprocess._prepare_postprocess_context(
                force=True, pending_keys={pending_key}
            )
        except Exception as error:  # noqa: BLE001 - 上下文异常按失败重试
            return self._fail_row(repo, row, f"准备后处理上下文异常：{error}", item)
        if ctx is None or not ctx.due_keys:
            return self._fail_row(repo, row, "后处理上下文为空（记录可能已被结算）", item)
        completed_before = ctx.completed
        failed_before = ctx.failed
        try:
            postprocess._process_due_items(ctx, funnel_drive=True)
            postprocess._flush_batch_postprocess(ctx)
            postprocess._reconcile_pending_state(ctx)
        except Exception as error:  # noqa: BLE001 - 驱动异常按失败重试
            logger.error(f"漏斗驱动后处理异常：{pending_key}，{error}", exc_info=True)
            return self._fail_row(repo, row, f"后处理异常：{error}", item)
        if ctx.completed > completed_before:
            repo.mark_done(int(row.get("id") or 0))
            return "done"
        if ctx.failed > failed_before:
            reason = self._terminal_failure_reason(pending_key)
            return self._fail_row(repo, row, reason, item)
        # 未结算（仍在重试等待）：行放回 queued，按 pending 记录的
        # next_check_at 重试，不耗 attempts。
        try:
            pending_item = self._load_pending_item(pending_key)
            next_check = int((pending_item or {}).get("next_check_at") or 0)
            next_retry = max(
                int(time.time()) + FUNNEL_UNSETTLED_RETRY_SECONDS, next_check
            )
            repo.release_to_queued(int(row.get("id") or 0), next_retry)
        except Exception as error:  # noqa: BLE001 - 放回失败时靠租约过期回收
            logger.debug(f"漏斗未结算行放回队列失败：{error}", exc_info=True)
        return "processing"

    def _terminal_failure_reason(self, pending_key: str) -> str:
        """驱动 abort 后从重试索引取失败原因；取不到给通用文案。"""
        try:
            plugin = getattr(self._owner, "_plugin", None)
            get_store = getattr(plugin, "_get_data_store", None) if plugin is not None else None
            if callable(get_store):
                store = get_store()
                index_row = store.repositories.retry_index.get_by_finalize_key(
                    str(pending_key or "")
                )
                if index_row and index_row.get("failure_reason"):
                    return str(index_row.get("failure_reason"))
        except Exception:  # noqa: BLE001 - 取不到不挡失败记账
            logger.debug("漏斗：取终态失败原因异常，用默认文案", exc_info=True)
        return "后处理失败（见失败记录）"

    def _fail_row(self, repo, row: Dict[str, Any], reason: str,
                    item: Optional[Dict[str, Any]] = None) -> str:
        """失败记账：次数未满按退避重试，满了挂起待人工复核。

        挂起时把终态同步给频道监控（v1.29.8 阶段 2 复核修补）：挂起路
        不经过 _write_terminal_state，不同步 TG 项会一直停在入队时标的
        transferred、而文件根本没入库。驱动 abort 的失败已由终态回写
        同步过一次，apply_finalize_outcome 对同原因的重复回写只更新
        原因与时间，是幂等的。
        """
        row_id = int(row.get("id") or 0)
        attempts = int(row.get("attempts") or 0)
        max_attempts = self._max_attempts
        if attempts + 1 >= max_attempts:
            repo.mark_held(row_id, reason)
            logger.warning(f"漏斗行 #{row_id} 失败 {attempts + 1} 次已挂起：{reason}")
            # v1.31.3：挂起即终态——顺手弹出该行对应的待终态记录。
            # 不弹它就成孤儿：离线监控跳过漏斗记录、worker 不管挂起行、
            # 6 小时绝对过期也到不了它，所属订阅的任务卡永久停在 95%。
            # 只在转 held 的分支清；重试分支（attempts 未满）绝不能清，
            # 行还要靠这条记录重跑。
            pending_key = str(row.get("pending_key") or "")
            if pending_key:
                self._pop_pending_record(pending_key)
                self._notify_pending_changed()
            if item is not None:
                self._sync_row_outcome_to_monitor(item, False, reason)
            return "held"
        delay = FUNNEL_RETRY_DELAYS[min(attempts, len(FUNNEL_RETRY_DELAYS) - 1)]
        # v1.29.10 热修复（评审 I5）：记失败被拒（行已被拦截路置 held，
        # 驱动层拿过期快照又调 _fail_row）时以库里实际状态为准，绝不把
        # held 翻回 failed。
        if not repo.mark_failed(row_id, int(time.time()) + delay, reason):
            current = None
            try:
                current = repo.get_by_id(row_id)
            except Exception as error:  # noqa: BLE001 - 读不到按 held 处理
                logger.debug(f"漏斗行 #{row_id} 读当前状态失败：{error}", exc_info=True)
            status = str((current or {}).get("status") or "held")
            logger.warning(
                f"漏斗行 #{row_id} 记失败被拒，当前状态 {status}：{reason}"
            )
            return status if status in ("held", "done") else "failed"
        logger.warning(
            f"漏斗行 #{row_id} 失败（{attempts + 1}/{max_attempts} 次），"
            f"{delay} 秒后重试：{reason}"
        )
        return "failed"

    def _sync_row_outcome_to_monitor(
            self, item: Dict[str, Any], success: bool, reason: str = "",
    ) -> None:
        """漏斗行终态回写频道监控（拦截/挂起路径专用）。

        后处理驱动的终态已由 _write_terminal_state 回写；但落盘前实查
        拦截、MP 整理跳过与挂起三条路不经过它。按 pending 项的 share_url
        找监控条目；监控不可用（无插件宿主、监控未初始化、回写抛异常）
        时静默跳过，绝不阻塞。share_url 为空的项（手动提交无链接等）
        没有监控条目可写，直接返回。
        """
        share_url = str(item.get("share_url") or "").strip()
        if not share_url:
            return
        plugin = getattr(self._owner, "_plugin", None)
        if plugin is None:
            return
        try:
            from ...core.tg_monitor import get_channel_monitor

            monitor = get_channel_monitor(plugin)
            monitor.apply_finalize_outcome(
                share_url,
                int(item.get("subscribe_id") or 0),
                success=success,
                reason=reason,
                file_name=str(item.get("file_name") or ""),
            )
        except Exception as error:  # noqa: BLE001 - 监控回写失败不阻塞
            logger.debug(f"漏斗行终态回写频道监控失败：{error}", exc_info=True)

    # ------------------------------------------------------------------
    # 提交前复查（_commit_single_item 入口调用）
    # ------------------------------------------------------------------
    def precommit_recheck(
            self,
            item: Dict[str, Any],
            pending_key: str,
            media: Any,
            ctx: Any = None,
    ) -> Tuple[bool, str]:
        """提交前复查：MP 整理成功后、写 STRM/历史/通知之前，再查一次。

        返回 (是否拦截, 原因)。两层：
        1. 在途登记校验：另一行在途同键（不同 pending_key）；
        2. 落盘前实查（判重激活）：剧集查季目录里已有另一份同集文件，
           电影查电影目标目录里已有另一份电影文件。洗版同样查——旧版
           按行 replaced_from 与项 upgrade_old_* 排除，只有“另一份新版”
           才拦截（v1.29.10 热修复，评审 B5：三条件之一）。
        查不到/异常一律 fail-open（不拦截）。绝不删除任何文件。
        """
        key = self._item_dedup_key(item, media)
        if key is None:
            return False, ""
        media_type, tmdb_id, season, episode = key
        repo = self._repository()
        if repo is not None:
            try:
                other = repo.find_active_by_key(
                    media_type, tmdb_id, season, episode,
                    exclude_pending_key=str(pending_key or ""),
                )
            except Exception:  # noqa: BLE001 - 在途校验异常 fail-open
                other = None
            if other:
                return True, (
                    "提交前复查发现重复，已拦截：整理队列已有同集在途行 "
                    f"#{other.get('id')}（{other.get('title') or ''}）"
                )
        if media_type == "tv" and episode <= 0:
            return False, ""
        if media_type not in {"tv", "movie"}:
            return False, ""
        dedup = getattr(self._owner, "episode_dedup", None)
        if dedup is None:
            return False, ""
        try:
            if not dedup._episode_dedup_active():
                return False, ""
        except Exception:  # noqa: BLE001 - 判重状态读不到按关闭处理
            return False, ""
        subscribe = None
        subscribe_cache = getattr(ctx, "subscribe_cache", None) if ctx is not None else None
        subscribe_id = int(item.get("subscribe_id") or 0)
        if isinstance(subscribe_cache, dict) and subscribe_id in subscribe_cache:
            subscribe = subscribe_cache.get(subscribe_id)
        if subscribe is None:
            subscribe = self._resolve_subscribe(item)
        if subscribe is None:
            return False, ""
        if media is None:
            media = self._restore_media({}, item)
        if media is None:
            return False, ""
        # 本项自己的刚落盘文件不算：同 file_id，或同 file_name。
        own_file_id = ""
        own_row: Dict[str, Any] = {}
        if repo is not None:
            try:
                own_row = repo.get_by_pending_key(str(pending_key or "")) or {}
                if own_row:
                    own_file_id = str(own_row.get("file_id") or "")
            except Exception:  # noqa: BLE001 - 行查不到按无 file_id 处理
                own_row = {}
        probe_item = dict(item)
        if own_file_id:
            probe_item["file_id"] = own_file_id
        if media_type == "tv":
            try:
                # v1.29.10 热修复（评审 B1）：提交前复查强制刷新。
                versions = dedup._collect_episode_versions(
                    subscribe, media, season, force_refresh=True
                ) or {}
            except Exception as error:  # noqa: BLE001 - 查不到 fail-open
                logger.debug(f"提交前复查异常（fail-open）：{error}", exc_info=True)
                return False, ""
            # v1.29.10 热修复（评审 B5）：洗版按旧版本除外后的新版检查。
            if bool(item.get("upgrade")):
                others = self._upgrade_other_new_versions(
                    versions.get(episode) or [], probe_item, own_row
                )
            else:
                others = self._other_versions(
                    versions.get(episode) or [], probe_item, {"file_id": own_file_id}
                )
            if not others:
                return False, ""
            return True, "提交前复查发现重复，已拦截：媒体库已有另一份同集文件"
        # 电影：_item_dedup_key 把非 TV 一律标成 "movie"，恢复后的媒体信息
        # 不是电影时不拦截（音乐/合集等不走电影实查）；电影没有剧集判定器，
        # 原因只写“已在库”，不硬套剧集判定器文案。
        if getattr(media, "type", None) != MediaType.MOVIE:
            return False, ""
        try:
            versions = dedup._find_cloud_movie_versions(subscribe, media) or []
        except Exception as error:  # noqa: BLE001 - 查不到 fail-open
            logger.debug(f"提交前复查异常（fail-open）：{error}", exc_info=True)
            return False, ""
        # v1.29.10 热修复（评审 B5）：洗版电影按旧版本除外后的新版检查。
        if bool(item.get("upgrade")):
            others = self._upgrade_other_new_versions(
                versions, probe_item, own_row
            )
        else:
            others = self._other_versions(
                versions, probe_item, {"file_id": own_file_id}
            )
        if not others:
            return False, ""
        return True, "提交前复查发现重复，已拦截：媒体库已有另一份电影文件"

    # ------------------------------------------------------------------
    # worker 调度（复用离线监控模式：有待处理才建 job、队列空即停 job）
    # ------------------------------------------------------------------
    def ensure_worker(self) -> None:
        """有待处理行时建 worker job，没有时停 job。"""
        repo = self._repository()
        if repo is None:
            return
        try:
            counts = repo.count_by_status()
        except Exception:  # noqa: BLE001 - 计数失败不建 job
            return
        active = int(counts.get("queued", 0)) + int(counts.get("failed", 0))
        self._update_funnel_worker(active)

    def _update_funnel_worker_from_repo(self, repo) -> None:
        try:
            counts = repo.count_by_status()
        except Exception:  # noqa: BLE001 - 计数失败不改 job
            return
        active = int(counts.get("queued", 0)) + int(counts.get("failed", 0))
        self._update_funnel_worker(active)

    def _update_funnel_worker(self, active_count: int) -> None:
        # 开关关闭时不建 job：队列里未处理行保留、开关再开时继续处理。
        if not self._phase1_enabled:
            active_count = 0
        with self._funnel_worker_lock:
            registry = self._scheduler_registry()
            if registry is None:
                return
            now = datetime.datetime.now(tz=pytz.timezone(settings.TZ))
            if active_count > 0:
                if registry.get_job(FUNNEL_WORKER_JOB_ID) is not None:
                    registry.modify_job_next_run(
                        FUNNEL_WORKER_JOB_ID,
                        now + datetime.timedelta(seconds=1),
                    )
                    return
                registry.ensure_job(
                    FUNNEL_WORKER_JOB_ID,
                    func=self.run_round,
                    trigger=IntervalTrigger(seconds=FUNNEL_WORKER_INTERVAL_SECONDS),
                    next_run_time=now + datetime.timedelta(seconds=3),
                    max_instances=1,
                    coalesce=True,
                )
                logger.info(f"整理队列 worker 已启动（待处理 {active_count} 行）")
                return
            if registry.remove_job(FUNNEL_WORKER_JOB_ID):
                logger.info("整理队列已空，漏斗 worker 已停止")

    # ------------------------------------------------------------------
    # 队列页 / API 用
    # ------------------------------------------------------------------
    def list_rows(
            self, statuses: List[str], limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """按状态列行（整理队列页三组：待处理/处理中/已挂起）。"""
        repo = self._repository()
        if repo is None:
            return []
        try:
            return repo.list_by_status(statuses, limit=limit)
        except Exception as error:  # noqa: BLE001 - 列表失败返回空
            logger.debug(f"漏斗队列列表失败：{error}", exc_info=True)
            return []

    def queue_counts(self) -> Dict[str, int]:
        """各状态行数。"""
        repo = self._repository()
        if repo is None:
            return {}
        try:
            return repo.count_by_status()
        except Exception as error:  # noqa: BLE001 - 计数失败返回空
            logger.debug(f"漏斗队列计数失败：{error}", exc_info=True)
            return {}

    def requeue_rows(self, row_ids: List[int]) -> int:
        """把 held/failed 行放回 queued（attempts 清零，立即到期）。

        v1.30.0 阶段 4：放回前先确保待终态记录存在——终态失败的旧
        行可能已被 _reconcile_pending_state 弹出记录，直接放回会
        在 worker 里立刻转回 held（“待终态记录已不在”）。缺失时按
        行重建一条（6 小时新窗口，task_type 恒为 share：文件已落盘，
        worker 不再重传，只走 MP 整理 + 终态提交）。
        """
        repo = self._repository()
        if repo is None:
            return 0
        try:
            for raw_id in row_ids or []:
                try:
                    row_id = int(raw_id or 0)
                except (TypeError, ValueError):
                    continue
                if row_id <= 0:
                    continue
                row = None
                try:
                    row = repo.get_by_id(row_id)
                except Exception as error:  # noqa: BLE001 - 读不到按行不存在
                    logger.debug(f"漏斗行 #{row_id} 放回前读行失败：{error}", exc_info=True)
                if not row:
                    continue
                if str(row.get("status") or "") not in {"held", "failed"}:
                    continue
                if not self.ensure_pending_record(row):
                    logger.warning(
                        f"漏斗行 #{row_id} 待终态记录重建失败，放回后"
                        " worker 可能再次挂起"
                    )
            return repo.requeue_by_ids(row_ids)
        except Exception as error:  # noqa: BLE001 - 放回失败返回 0
            logger.debug(f"漏斗行放回队列失败：{error}", exc_info=True)
            return 0

    def delete_rows(self, row_ids: List[int]) -> int:
        """删队列行（只删行本身，不动 pending 记录与网盘文件）。"""
        repo = self._repository()
        if repo is None:
            return 0
        try:
            return repo.delete_by_ids(row_ids)
        except Exception as error:  # noqa: BLE001 - 删除失败返回 0
            logger.debug(f"漏斗行删除失败：{error}", exc_info=True)
            return 0

    def find_row_by_pending_key(self, pending_key: str) -> Optional[Dict[str, Any]]:
        """按终态键查漏斗行（任意状态，含 held/done；失败重试路径用）。"""
        repo = self._repository()
        if repo is None:
            return None
        try:
            return repo.find_row_by_pending_key(str(pending_key or ""))
        except Exception as error:  # noqa: BLE001 - 查不到按无行处理
            logger.debug(f"漏斗按终态键查行失败：{error}", exc_info=True)
            return None

    def repair_orphan_pending_records(self) -> int:
        """启动修复（v1.31.3）：清掉漏斗孤儿待终态记录。

        孤儿 = 待终态记录带 funnel_route、但对应的漏斗行已挂起/已完成/
        不存在：离线监控跳过漏斗记录、worker 不管挂起行、6 小时绝对过期
        也到不了它们，记录永不结算，所属订阅的任务卡永久停在 95%。
        只动孤儿：queued/processing/failed 行的记录不动（worker 还在
        驱动），非漏斗记录不动（归离线监控）。阶段 1 关闭时不扫（那时
        漏斗记录归监控驱动，不是孤儿）。逐键 fail-closed（查行异常
        时跳过该键、不弹），整体 fail-open；幂等，返回清掉的条数。
        """
        if not self._phase1_enabled:
            return 0
        try:
            repo = self._repository()
            if repo is None:
                return 0
            get_data = getattr(self, "_get_data", None)
            if not callable(get_data):
                return 0
            pending = get_data(self._OFFLINE_PENDING_KEY) or {}
            if not isinstance(pending, dict) or not pending:
                return 0
            orphan_keys = []
            for key, item in pending.items():
                if not isinstance(item, dict) or not item.get("funnel_route"):
                    continue
                # 直接调仓储方法（不用 FunnelService.find_row_by_pending_key：
                # 它把异常吞成 None，会把“查不到”等同于“行不存在”而误删；
                # 这里异常要跳过该键，fail-closed）。
                try:
                    row = repo.find_row_by_pending_key(str(key))
                except Exception as error:  # noqa: BLE001 - 查行异常跳过该键
                    logger.debug(f"孤儿扫描查行失败，跳过 {key}：{error}", exc_info=True)
                    continue
                if row is None or str(row.get("status") or "") in {"held", "done"}:
                    orphan_keys.append(str(key))
            if not orphan_keys:
                return 0
            for key in orphan_keys:
                pending.pop(key, None)
            save = getattr(self, "_save_offline_pending", None)
            if callable(save):
                save(pending)
            logger.info(
                f"启动修复：清掉 {len(orphan_keys)} 条孤儿待终态记录"
                "（漏斗行已挂起/已完成/不存在）"
            )
            return len(orphan_keys)
        except Exception as error:  # noqa: BLE001 - 修复失败不挡启动
            logger.warning(f"清孤儿待终态记录失败：{error}", exc_info=True)
            return 0

    # ------------------------------------------------------------------
    # 阶段 4（v1.30.0）：扫描对账 dry-run、轮次历史、held 认领、孤儿清理
    # ------------------------------------------------------------------
    def _round_history_list(self) -> List[Dict[str, Any]]:
        """轮次历史（存宿主身上，形态复用 _funnel_stop_requested）。"""
        history = getattr(self._owner, "_funnel_round_history", None)
        if not isinstance(history, list):
            history = []
            setattr(self._owner, "_funnel_round_history", history)
        return history

    def _record_round(self, entry: Dict[str, Any]) -> None:
        """记一轮摘要，只保留最近 FUNNEL_ROUND_HISTORY_LIMIT 条。"""
        try:
            history = self._round_history_list()
            history.append(dict(entry or {}))
            while len(history) > FUNNEL_ROUND_HISTORY_LIMIT:
                history.pop(0)
        except Exception as error:  # noqa: BLE001 - 历史记不上不挡轮次
            logger.debug(f"漏斗轮次历史记录失败：{error}", exc_info=True)

    def round_history(self) -> List[Dict[str, Any]]:
        """最近 20 轮摘要（新在前，整理队列页展示）。"""
        try:
            return [dict(entry) for entry in reversed(self._round_history_list())]
        except Exception as error:  # noqa: BLE001 - 读不到返回空
            logger.debug(f"漏斗轮次历史读取失败：{error}", exc_info=True)
            return []

    # -- 扫描对账 dry-run ----------------------------------------------
    def run_scan_dry_run(
            self,
            trigger: str = "手动",
            organize_service: Any = None,
            provider: Any = None,
            library_check: Any = None,
    ) -> Dict[str, Any]:
        """04:00 扫描对账（阶段 4：默认 dry-run）。

        扫描 ``funnel_target_dir``，用 OrganizeService 的识别能力按
        §5.3 分类并只记日志：可识别的孤儿记“可认领入队（dry-run，未
        入队）”、不可识别的记“待人工认领（dry-run）”、可识别但已在库
        的记跳过（skip_reason=“已入库”）。dry-run 阶段不写队列行、
        不删任何文件。``funnel_scan_dry_run`` 关掉也不接管（接管留给
        以后一版），只记一条警告并返回。

        ``organize_service`` / ``provider`` / ``library_check`` 为
        测试注入点（默认从插件侧解析）；``library_check`` 签名
        ``(media, season, episode, file_name) -> True/False/None``，
        None=查不出来（fail-open，按可认领记）。
        """
        started = int(time.time())
        summary: Dict[str, Any] = {
            "success": True,
            "trigger": trigger,
            "started_at": started,
            "finished_at": started,
            "scanned": 0,
            "recognizable": 0,
            "unrecognizable": 0,
            "already_in_library": 0,
            "errors": 0,
            "dry_run": True,
        }
        if not self._phase1_enabled:
            summary["success"] = False
            summary["message"] = "漏斗未启用，跳过扫描对账"
            return summary
        if not self._scan_dry_run:
            logger.warning(
                "漏斗扫描对账：dry-run 已关闭，接管未实现，本版不接管，跳过扫描"
            )
            summary["success"] = False
            summary["message"] = "dry-run 已关闭，接管未实现，跳过扫描"
            summary["dry_run"] = False
            return summary
        if organize_service is None:
            organize_service = self._scan_organize_service()
        if organize_service is None:
            summary["success"] = False
            summary["message"] = "整理台服务未就绪，跳过扫描对账"
            return summary
        if provider is None:
            provider = self._scan_provider()
        if provider is None:
            summary["success"] = False
            summary["message"] = "网盘目录读取能力未就绪，跳过扫描对账"
            return summary
        try:
            # 懒加载：顶层 import 在假包名单测环境下走不通（见
            # OrganizeService._recognize_package 的同款做法）；且
            # organize_plan 是纯函数模块，不依赖 MP 运行时。
            from ...utils.organize_plan import build_packages, scan_entries
        except Exception as error:  # noqa: BLE001 - 加载失败按扫描不可用
            logger.warning(f"漏斗扫描对账加载扫描原语失败：{error}")
            summary["success"] = False
            summary["message"] = "扫描原语加载失败，跳过扫描对账"
            return summary
        target_dir = self._target_dir
        try:
            scanned = scan_entries(provider, target_dir)
        except Exception as error:  # noqa: BLE001 - 扫描异常记轮次后返回
            logger.warning(f"漏斗扫描对账读取目标目录失败：{target_dir}：{error}")
            summary["success"] = False
            summary["message"] = f"读取目标目录失败：{error}"
            summary["errors"] = 1
            summary["finished_at"] = int(time.time())
            self._record_round({**summary, "kind": "scan"})
            return summary
        if getattr(scanned, "errors", None):
            summary["errors"] = len(scanned.errors)
            for message in list(scanned.errors)[:5]:
                logger.warning(f"漏斗扫描对账：{message}")
        try:
            packages = build_packages(getattr(scanned, "entries", []) or [], target_dir)
        except Exception as error:  # noqa: BLE001 - 分组异常记轮次后返回
            logger.warning(f"漏斗扫描对账分组失败：{error}")
            summary["success"] = False
            summary["message"] = f"分组失败：{error}"
            summary["errors"] = summary.get("errors", 0) + 1
            summary["finished_at"] = int(time.time())
            self._record_round({**summary, "kind": "scan"})
            return summary
        recognize = getattr(organize_service, "_recognize_package", None)
        if not callable(recognize):
            summary["success"] = False
            summary["message"] = "整理台识别能力未就绪，跳过扫描对账"
            return summary
        for package in packages:
            try:
                media = recognize(package)
            except Exception as error:  # noqa: BLE001 - 单包识别失败记不可识别
                logger.debug(f"漏斗扫描对账识别失败：{package.pack_name}：{error}", exc_info=True)
                media = None
            status, detail = self._scan_classify_package(
                package, media, library_check
            )
            summary["scanned"] += 1
            if status == "already_in_library":
                summary["already_in_library"] += 1
                logger.info(
                    f"漏斗扫描对账 dry-run：跳过（已入库）{detail}"
                )
            elif status == "recognizable":
                summary["recognizable"] += 1
                logger.info(
                    f"漏斗扫描对账 dry-run：可认领入队（dry-run，未入队）{detail}"
                )
            else:
                summary["unrecognizable"] += 1
                logger.info(
                    f"漏斗扫描对账 dry-run：待人工认领（dry-run）{detail}"
                )
        summary["finished_at"] = int(time.time())
        self._record_round({**summary, "kind": "scan"})
        logger.info(
            "漏斗扫描对账 dry-run 完成：扫描 "
            f"{summary['scanned']} 包，可认领 {summary['recognizable']}，"
            f"待人工认领 {summary['unrecognizable']}，"
            f"已入库跳过 {summary['already_in_library']}"
        )
        return summary

    def _scan_organize_service(self) -> Any:
        """取整理台服务（只借识别能力，不走它的移动/生成 STRM 执行路）。"""
        plugin = getattr(self._owner, "_plugin", None)
        if plugin is None:
            return None
        get_component = getattr(plugin, "_get_component", None)
        if not callable(get_component):
            return None
        try:
            from ...core.services.organize import OrganizeService
        except Exception as error:  # noqa: BLE001 - 懒加载失败按无服务
            logger.debug(f"漏斗扫描对账加载整理台失败：{error}", exc_info=True)
            return None
        try:
            return get_component(OrganizeService)
        except Exception as error:  # noqa: BLE001 - 取不到按无服务
            logger.debug(f"漏斗扫描对账取整理台失败：{error}", exc_info=True)
            return None

    def _scan_provider(self) -> Any:
        """取网盘目录读取能力（DIRECTORY_READ）。"""
        drive = getattr(self, "_cloud_drive", None)
        if drive is None:
            return None
        require = getattr(drive, "require", None)
        if not callable(require):
            return None
        try:
            from ...core import CloudDriveCapability
        except Exception as error:  # noqa: BLE001 - 能力枚举取不到按无能力
            logger.debug(f"漏斗扫描对账取目录读取能力失败：{error}", exc_info=True)
            return None
        try:
            return require(CloudDriveCapability.DIRECTORY_READ)
        except Exception as error:  # noqa: BLE001 - 能力未就绪按无能力
            logger.debug(f"漏斗扫描对账取目录读取能力失败：{error}", exc_info=True)
            return None

    def _scan_classify_package(
            self, package: Any, media: Any, library_check: Any,
    ) -> Tuple[str, str]:
        """按 §5.3 分类一包：返回 (状态, 说明)。

        状态：recognizable（可认领入队）、unrecognizable（待人工认领）、
        already_in_library（已入库，跳过）。媒体认不出即不可识别；认
        出但查库失败（library_check 返回 None）按可认领记（fail-open）。
        """
        pack_name = str(getattr(package, "pack_name", "") or "").strip()
        if media is None:
            return "unrecognizable", pack_name or "未知包"
        title = str(getattr(media, "title", "") or pack_name).strip()
        videos = list(getattr(package, "videos", []) or [])
        first_video = videos[0] if videos else None
        if bool(getattr(package, "is_tv", False)):
            season = max(1, int(getattr(first_video, "season", 1) or 1)) if first_video else 1
            episode = max(1, int(getattr(first_video, "episode", 1) or 1)) if first_video else 1
        else:
            season, episode = 0, 0
        file_name = ""
        if first_video is not None:
            entry = getattr(first_video, "entry", None)
            file_name = str(getattr(entry, "name", "") or "").strip()
        detail = title
        if season and episode:
            detail = f"{title} S{season:02d}E{episode:02d}"
        elif file_name:
            detail = f"{title}（{file_name}）"
        check = library_check
        if check is None:
            check = self._scan_library_check
        try:
            in_library = check(media, season, episode, file_name)
        except Exception as error:  # noqa: BLE001 - 查库异常按可认领记
            logger.debug(f"漏斗扫描对账查库异常：{detail}：{error}", exc_info=True)
            in_library = None
        if in_library is True:
            return "already_in_library", detail
        return "recognizable", detail

    def _scan_library_check(
            self, media: Any, season: int, episode: int, file_name: str,
    ) -> Optional[bool]:
        """扫描用的入库查核：MP 自己的 get_no_exists_info（Emby 口径）。

        True=已在库，False=不在库，None=查不出来（fail-open）。
        """
        mp_organize = getattr(self._owner, "mp_organize", None)
        check = getattr(mp_organize, "_episode_exists_in_library_via_mp", None)
        if not callable(check):
            return None
        try:
            return check(
                mediainfo=media, season=season, file_name=file_name,
            )
        except Exception as error:  # noqa: BLE001 - 查库异常按查不出来
            logger.debug(f"漏斗扫描对账查库失败：{error}", exc_info=True)
            return None

    # -- 待终态记录重建 ----------------------------------------------
    def ensure_pending_record(self, row: Dict[str, Any]) -> bool:
        """行重回队列前确保待终态记录存在；缺失时按行重建。

        终态失败的旧行可能已被 _reconcile_pending_state 弹出记录，
        直接放回会在 worker 里立刻转回 held。重建用行里已有的字段：
        云盘位置/文件名/体积/media_data/订阅/季集/洗版旧版/评分，
        share_url 与 source_sha1 行里没有——文件已落盘，worker 不再
        重传（只走 MP 整理 + 终态提交），share_url 置空、source_sha1
        取行里的 sha1；task_type 恒为 share。first_seen_at 取现在，
        给重建的记录一个全新的 6 小时绝对窗口。
        """
        pending_key = str((row or {}).get("pending_key") or "")
        if not pending_key:
            return False
        if self._load_pending_item(pending_key) is not None:
            return True
        build = getattr(self._owner, "_build_pending_record", None)
        save = getattr(self, "_save_offline_pending", None)
        get_data = getattr(self, "_get_data", None)
        if not callable(build) or not callable(save) or not callable(get_data):
            return False
        mediainfo = self._restore_media(row, {})
        media_data = (row or {}).get("media_data")
        if not isinstance(media_data, dict):
            media_data = None
        now = time.time()
        episode = int((row or {}).get("episode") or 0)
        is_tv = str((row or {}).get("media_type") or "").strip().lower() == "tv"
        replaced = (row or {}).get("replaced_from") or []
        if not isinstance(replaced, list):
            replaced = []
        first_old = replaced[0] if replaced and isinstance(replaced[0], dict) else {}
        try:
            record = build(
                current={},
                pending_key=pending_key,
                task_type="share",
                info_hash="",
                source_hash=str((row or {}).get("sha1") or ""),
                share_url="",
                cloud_dir=str((row or {}).get("cloud_dir") or ""),
                file_name=str((row or {}).get("file_name") or ""),
                staging_dir=str(
                    (row or {}).get("staging_dir") or (row or {}).get("cloud_dir") or ""
                ),
                staging_name=str(
                    (row or {}).get("staging_name") or (row or {}).get("file_name") or ""
                ),
                file_size=int((row or {}).get("file_size") or 0),
                now=now,
                mediainfo=mediainfo,
                media_data=media_data,
                subscribe_id=(row or {}).get("subscribe_id"),
                success_episodes=[episode] if is_tv and episode > 0 else [],
                notification_episodes=[episode] if is_tv and episode > 0 else [],
                season=int((row or {}).get("season") or 1) if is_tv else None,
                upgrade=bool((row or {}).get("is_upgrade")),
                upgrade_old_cloud_dir=str(first_old.get("cloud_dir") or ""),
                upgrade_old_file_name=str(first_old.get("file_name") or ""),
                upgrade_old_file_id=str(first_old.get("file_id") or ""),
                upgrade_old_size=int(first_old.get("file_size") or 0),
                dedup_extra_old_versions=[
                    dict(entry) for entry in replaced[1:]
                    if isinstance(entry, dict)
                ],
                dedup_new_score=int((row or {}).get("score") or 0),
                funnel_route=True,
            )
        except Exception as error:  # noqa: BLE001 - 重建失败按无记录
            logger.warning(
                f"漏斗行 #{(row or {}).get('id')} 待终态记录重建失败：{error}"
            )
            return False
        record["result_key"] = str((row or {}).get("file_id") or "")
        try:
            # 注意：不能用 `get_data(...) or {}`——空字典是 falsy，会被
            # 换成新对象，写进新对象后内存里的待终态映射还是空的（生产
            # 靠 save 兜底才没爆，save 是 no-op 的环境直接丢记录）。
            # 只有取不到/非字典时才新建，取到字典（哪怕空）就地写。
            pending = get_data(self._OFFLINE_PENDING_KEY)
            if not isinstance(pending, dict):
                pending = {}
            pending[pending_key] = record
            save(pending)
        except Exception as error:  # noqa: BLE001 - 写盘失败按无记录
            logger.warning(
                f"漏斗行 #{(row or {}).get('id')} 待终态记录写盘失败：{error}"
            )
            return False
        logger.info(
            f"漏斗行 #{(row or {}).get('id')} 待终态记录已重建"
            f"（{pending_key}，6 小时新窗口）"
        )
        return True

    # -- held 行人工认领 ----------------------------------------------
    def _resolve_claim_media(
            self, tmdb_id: int, media_type: str, title: str,
    ) -> Any:
        """认领时按 TMDB ID 直查媒体（复用手动添加的直查路）。

        识别失败或返回的 TMDB ID 不符时返回 None（调用方拒绝认领，
        绝不带着错身份入队）。测试可直接猴补本方法。
        """
        try:
            from app.core.metainfo import MetaInfo
        except Exception as error:  # noqa: BLE001 - MP 未提供时按识别失败
            logger.debug(f"认领查媒体：MetaInfo 不可用：{error}", exc_info=True)
            return None
        try:
            from ...core.media import recognize_media
        except Exception as error:  # noqa: BLE001 - 识别入口不可用
            logger.debug(f"认领查媒体：recognize_media 不可用：{error}", exc_info=True)
            return None
        plugin = getattr(self._owner, "_plugin", None)
        chain = getattr(plugin, "chain", None) if plugin is not None else None
        if chain is None:
            chain = getattr(self._owner, "chain", None)
        if chain is None:
            return None
        mtype = MediaType.MOVIE if str(media_type or "").strip().lower() == "movie" else MediaType.TV
        try:
            mediainfo = recognize_media(
                chain,
                meta=MetaInfo(str(title or tmdb_id)),
                mtype=mtype,
                tmdb_id=int(tmdb_id),
                cache=True,
            )
        except Exception as error:  # noqa: BLE001 - 识别失败按无媒体
            logger.debug(f"认领查媒体失败（tmdb={tmdb_id}）：{error}", exc_info=True)
            return None
        if not mediainfo:
            return None
        try:
            resolved_id = int(getattr(mediainfo, "tmdb_id", 0) or 0)
        except (TypeError, ValueError):
            resolved_id = 0
        if resolved_id != int(tmdb_id):
            return None
        return mediainfo

    def claim_row(
            self, row_id: int, tmdb_id: int, media_type: str,
            season: Any = None, episode: Any = None,
    ) -> Dict[str, Any]:
        """held 行人工认领：指定剧集/TMDB 后重新入队。

        只认 held/failed 行；先按 TMDB ID 直查媒体（查不到或 ID 不符
        拒绝认领），再把身份写进行（media_type/tmdb_id/season/episode/
        title/media_data），重建待终态记录后放回队列（attempts 清零）。
        """
        repo = self._repository()
        if repo is None:
            return {"success": False, "message": "漏斗仓储不可用"}
        try:
            row_id = int(row_id or 0)
            tmdb_id = int(tmdb_id or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "行 ID 或 TMDB ID 非法"}
        if row_id <= 0 or tmdb_id <= 0:
            return {"success": False, "message": "行 ID 或 TMDB ID 非法"}
        media_type = str(media_type or "").strip().lower()
        if media_type not in {"movie", "tv"}:
            return {"success": False, "message": "媒体类型只能是 movie 或 tv"}
        try:
            season_value = int(season or 0) if season not in (None, "") else 0
            episode_value = int(episode or 0) if episode not in (None, "") else 0
        except (TypeError, ValueError):
            return {"success": False, "message": "季/集号非法"}
        if media_type == "tv" and episode_value <= 0:
            return {"success": False, "message": "剧集认领必须指定集号"}
        if media_type == "tv" and season_value <= 0:
            season_value = 1
        row = None
        try:
            row = repo.get_by_id(row_id)
        except Exception as error:  # noqa: BLE001 - 读不到按行不存在
            logger.debug(f"认领读行 #{row_id} 失败：{error}", exc_info=True)
        if not row:
            return {"success": False, "message": "行不存在"}
        if str(row.get("status") or "") not in {"held", "failed"}:
            return {
                "success": False,
                "message": f"只有挂起/失败行能认领，当前状态 {row.get('status')}",
            }
        mediainfo = self._resolve_claim_media(
            tmdb_id, media_type, str(row.get("title") or ""),
        )
        if mediainfo is None:
            return {
                "success": False,
                "message": f"按 TMDB ID {tmdb_id} 查不到媒体，认领失败",
            }
        title = str(getattr(mediainfo, "title", "") or row.get("title") or "").strip()
        media_data = None
        serialize = getattr(self, "_serialize_mediainfo", None)
        if callable(serialize):
            try:
                media_data = serialize(mediainfo)
            except Exception as error:  # noqa: BLE001 - 序列化失败按无媒体数据
                logger.debug(f"认领序列化媒体信息失败：{error}", exc_info=True)
                media_data = None
        try:
            updated = repo.claim_row(
                row_id, media_type, tmdb_id, season_value, episode_value,
                title, media_data,
            )
        except Exception as error:  # noqa: BLE001 - 写行失败按认领失败
            logger.debug(f"认领写行 #{row_id} 失败：{error}", exc_info=True)
            return {"success": False, "message": f"认领写行失败：{error}"}
        if not updated:
            return {"success": False, "message": "认领写行失败（行状态已变）"}
        fresh_row = row
        try:
            fresh_row = repo.get_by_id(row_id) or row
        except Exception as error:  # noqa: BLE001 - 重读失败用旧行重建
            logger.debug(f"认领重读行 #{row_id} 失败：{error}", exc_info=True)
        if not self.ensure_pending_record(fresh_row):
            logger.warning(
                f"漏斗行 #{row_id} 认领后待终态记录重建失败，放回后"
                " worker 可能再次挂起"
            )
        try:
            repo.requeue_by_ids([row_id])
        except Exception as error:  # noqa: BLE001 - 放回失败按认领失败
            logger.debug(f"认领放回行 #{row_id} 失败：{error}", exc_info=True)
            return {"success": False, "message": f"认领放回失败：{error}"}
        self.ensure_worker()
        logger.info(
            f"漏斗行 #{row_id} 已认领：{title}（tmdb={tmdb_id}，"
            f"{media_type}，S{season_value:02d}E{episode_value:02d}），已重新入队"
        )
        return {"success": True, "message": "已认领并重新入队", "id": row_id}

    # -- 孤儿文件人工清理 ----------------------------------------------
    @staticmethod
    def _orphan_location_from_reason(reason: str) -> str:
        """从 held 行原因里解析孤儿文件位置（``orphan_location=`` 标记）。

        位置取到第一个分隔符为止；原因串是中英混排，分隔符可能是
        ASCII 分号（_held_reason_with_orphan 写的格式）或中文分号。"""
        text = str(reason or "")
        marker = FUNNEL_ORPHAN_LOCATION_MARKER
        index = text.find(marker)
        if index < 0:
            return ""
        rest = text[index + len(marker):].strip()
        ends = [
            end for end in (rest.find(";"), rest.find("；")) if end >= 0
        ]
        if ends:
            rest = rest[:min(ends)].strip()
        return rest

    def _delete_strm_for_file(self, cloud_dir: str, file_name: str) -> bool:
        """删指定云盘文件匹配的本地 .strm（两京十五日清理规则：删
        .mkv 时连同匹配的 .strm 一起删）。没有 .strm 也算成功。"""
        generator = getattr(self, "_strm_generator", None)
        if generator is None:
            # 没有 STRM 生成器 = 本插件没给该文件生成过 .strm（STRM
            # 生成关着时不会有孤儿 .strm），无物可删，算成功。
            return True
        try:
            from pathlib import Path
            local_path = generator.local_path("", "", cloud_dir, file_name)
        except Exception as error:  # noqa: BLE001 - 算不出路径按未删
            logger.debug(f"删孤儿 .strm 算路径失败：{error}", exc_info=True)
            return False
        if not local_path:
            return True
        try:
            path = Path(str(local_path))
        except Exception as error:  # noqa: BLE001 - 路径非法按未删
            logger.debug(f"删孤儿 .strm 路径非法：{error}", exc_info=True)
            return False
        try:
            if path.is_file():
                path.unlink()
                logger.info(f"孤儿清理已删 .strm：{path}")
            return True
        except Exception as error:  # noqa: BLE001 - 删失败按未删
            logger.debug(f"删孤儿 .strm 失败：{path}：{error}", exc_info=True)
            return False

    def delete_orphan_files(self, row_ids) -> Dict[str, Any]:
        """held 行人工删孤儿文件（v1.30.0 阶段 4，评审 B5-2/B5-3 落地）。

        默认不自动删（删文件是人工决策）；只认带孤儿位置的 held 行，
        删 .mkv（按行 file_id）时连同匹配的 .strm 一起删，两个都删完
        才清行里的孤儿标记。删失败的留在原地、可重试。
        """
        repo = self._repository()
        if repo is None:
            return {"success": False, "message": "漏斗仓储不可用"}
        results = []
        for raw_id in row_ids or []:
            try:
                row_id = int(raw_id or 0)
            except (TypeError, ValueError):
                continue
            if row_id <= 0:
                continue
            row = None
            try:
                row = repo.get_by_id(row_id)
            except Exception as error:  # noqa: BLE001 - 读不到按行不存在
                logger.debug(f"删孤儿读行 #{row_id} 失败：{error}", exc_info=True)
            if not row:
                results.append({
                    "id": row_id, "success": False, "message": "行不存在",
                })
                continue
            if str(row.get("status") or "") != "held":
                results.append({
                    "id": row_id, "success": False,
                    "message": f"只有挂起行能删孤儿文件，当前状态 {row.get('status')}",
                })
                continue
            orphan_location = self._orphan_location_from_reason(
                str(row.get("skip_reason") or "")
            )
            if not orphan_location:
                results.append({
                    "id": row_id, "success": False, "message": "该行没有孤儿文件位置",
                })
                continue
            file_id = str(row.get("file_id") or "").strip()
            if not file_id:
                results.append({
                    "id": row_id, "success": False, "message": "该行没有孤儿文件 ID",
                })
                continue
            mutations = getattr(self, "_cloud_mutations", None)
            if mutations is None:
                results.append({
                    "id": row_id, "success": False, "message": "网盘未就绪",
                })
                continue
            try:
                ok = mutations.delete_file(file_id)
            except Exception as error:  # noqa: BLE001 - 删失败记名
                logger.debug(f"删孤儿文件 {file_id} 异常：{error}", exc_info=True)
                ok = False
            if not ok:
                results.append({
                    "id": row_id, "success": False,
                    "message": f"删除孤儿文件失败：{orphan_location}",
                })
                continue
            cloud_dir = str(row.get("cloud_dir") or "").strip()
            file_name = str(row.get("file_name") or "").strip()
            if cloud_dir and file_name:
                if not self._delete_strm_for_file(cloud_dir, file_name):
                    logger.warning(
                        f"漏斗行 #{row_id} 孤儿 .mkv 已删，匹配 .strm 未删："
                        f"{orphan_location}"
                    )
                    # .strm 没删掉时不清理标记（否则行丢了孤儿位置，
                    # 残留 .strm 再也删不掉）；本行记失败，可重试。
                    results.append({
                        "id": row_id, "success": False,
                        "message": f"孤儿 .mkv 已删，匹配 .strm 未删：{orphan_location}",
                    })
                    continue
            try:
                repo.clear_orphan_location(row_id)
            except Exception as error:  # noqa: BLE001 - 清标记失败不改删的结论
                logger.debug(f"删孤儿清行 #{row_id} 标记失败：{error}", exc_info=True)
            logger.info(f"漏斗行 #{row_id} 人工删孤儿文件：{orphan_location}")
            results.append({
                "id": row_id, "success": True, "deleted": orphan_location,
            })
        return {"success": True, "results": results}
