"""网盘整理漏斗：队列 + worker + 提交前复查（v1.29.7 阶段 1）。

设计见 ``cloudsubscribex-organize-funnel-design.md`` §12 阶段 1。

阶段 1 只接手动提交（手动转存入口 + 媒体服务器洗版入口）：转存管线在
finalize 边界（``SyncHandler._queue_file_finalize_batch``）写
``funnel_queue`` 行并把 pending 记录标记为漏斗归属，离线监控跳过这类
记录；漏斗 worker 逐行认领——整理前改名、落盘前实查、触发 MP 整理——
再驱动已有的单项后处理（``_organize_and_finalize_single_item``）走
同一条终态提交（``_commit_single_item``），不另写 STRM/通知/历史。

不在阶段 1 范围内的事：TG 监控、订阅轮次、洗版薄入口仍走旧流水线；
不做扫描对账（阶段 4）、轮次历史（阶段 4）、孤儿认领（阶段 4）。
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
#: 失败退避（秒）：第 1..5 次失败分别等 10 秒、60 秒、300 秒、900 秒、
#: 3600 秒；第 5 次失败后挂起待人工复核（不再自动重试）。
FUNNEL_RETRY_DELAYS = (10, 60, 300, 900, 3600)
#: 每轮最多处理的行数。
FUNNEL_MAX_ROWS_PER_ROUND = 50
#: 未结算行下次重试的最小间隔（秒）：与离线监控首档检查间隔一致。
FUNNEL_UNSETTLED_RETRY_SECONDS = 20


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
        payload = {
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
        row, created = repo.enqueue(payload)
        if created:
            if via:
                producer_label = "TG 转存"
            elif producer == "subscribe":
                producer_label = "订阅轮次"
            else:
                producer_label = "手动提交"
            logger.info(
                f"{producer_label}已入队整理队列：{title or payload['file_name']} "
                f"（行 #{row.get('id')}，{pending_key}）"
            )
            self.ensure_worker()
        return row, created

    def has_active_rows(self, subscribe_id) -> bool:
        """v1.29.9 阶段 3：指定订阅是否还有漏斗在途行（queued/processing/
        failed）。轮次结束结算前调用：在途行未终态时绝不结算，终态成功
        时由 _finish_pending_subscription 回写。存储不可用时 fail-open
        （返回 False，按旧行为结算）。"""
        try:
            if int(subscribe_id or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        repo = self._repository()
        if repo is None:
            return False
        has_active = getattr(repo, "has_active_for_subscribe", None)
        if not callable(has_active):
            return False
        try:
            return bool(has_active(int(subscribe_id)))
        except Exception as error:  # noqa: BLE001 - 查询异常按无在途行处理
            logger.debug(f"查询订阅漏斗在途行失败：{error}", exc_info=True)
            return False

    # ------------------------------------------------------------------
    # worker 轮次
    # ------------------------------------------------------------------
    def run_round(self) -> Dict[str, int]:
        """worker 一轮：逐行认领处理；同一时间只跑一轮。

        由调度器每 20 秒调一次（队列空时 job 会被停掉）。返回本轮统计。
        """
        stats = {"processed": 0, "done": 0, "failed": 0, "held": 0, "skipped": 0}
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
                repo.requeue_lease_expired(now=int(time.time()))
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
            return stats
        finally:
            try:
                self._funnel_worker_lock.release()
            except RuntimeError:
                logger.debug("漏斗 worker 锁释放时未持有，忽略", exc_info=True)

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
            # 待终态记录已不在（旧流水线已结算或被删）：行终态，释放去重键。
            repo.mark_done(row_id, reason="待终态记录已不在")
            logger.info(f"漏斗行 #{row_id} 的待终态记录已不在，直接终态")
            return "done"
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
        if bool(item.get("upgrade")):
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
            versions = dedup._collect_episode_versions(subscribe, media, season) or {}
        except Exception as error:  # noqa: BLE001 - 查不到 fail-open
            logger.debug(f"漏斗落盘前实查异常（fail-open）：{error}", exc_info=True)
            return True, ""
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
            if item is not None:
                self._sync_row_outcome_to_monitor(item, False, reason)
            return "held"
        delay = FUNNEL_RETRY_DELAYS[min(attempts, len(FUNNEL_RETRY_DELAYS) - 1)]
        repo.mark_failed(row_id, int(time.time()) + delay, reason)
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
        2. 落盘前实查（判重激活、非洗版）：剧集查季目录里已有另一份
           同集文件，电影查电影目标目录里已有另一份电影文件。
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
        if bool(item.get("upgrade")):
            # 洗版：旧版在提交后才删，库里有旧版是正常的，跳过库检查。
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
        if repo is not None:
            try:
                own_row = repo.get_by_pending_key(str(pending_key or ""))
                if own_row:
                    own_file_id = str(own_row.get("file_id") or "")
            except Exception:  # noqa: BLE001 - 行查不到按无 file_id 处理
                own_row = None
        probe_item = dict(item)
        if own_file_id:
            probe_item["file_id"] = own_file_id
        if media_type == "tv":
            try:
                versions = dedup._collect_episode_versions(subscribe, media, season) or {}
            except Exception as error:  # noqa: BLE001 - 查不到 fail-open
                logger.debug(f"提交前复查异常（fail-open）：{error}", exc_info=True)
                return False, ""
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
        """把 held/failed 行放回 queued（attempts 清零，立即到期）。"""
        repo = self._repository()
        if repo is None:
            return 0
        try:
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
