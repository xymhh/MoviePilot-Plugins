"""洗版基线、评分与自动升级。"""

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from app.db import SessionFactory
from app.log import logger
from app.schemas import MediaInfo
from app.schemas.types import MediaType

from ...core import OwnerDelegator


#: 质量上限（MP 过滤规则的评分标尺）：MP v3.2.6
#: ``app/modules/filter/__init__.py`` 的 ``FilterModule.__get_order`` 规定
#: “首层优先级为 100，后续每层减 1；数值越大越优先”，规则串按 ``>`` 分层、
#: 首个命中的层决定评分，因此规则评分最高只能到 100（这也是
#: ``rule_scoring`` 评分比较的标尺）。基线里评分已达 100 的集在评分上
#: 不可能被超越，洗版搜索前直接跳过、省请求（同分体积更大的理论替换
#: 也被一并跳过，这是为省请求付出的代价）。
QUALITY_CEILING_SCORE = 100


def plan_upgrade_episode_order(
        local_episodes: Iterable[int],
        all_expected_episodes: Set[int],
        target_episodes: Optional[Set[int]],
        limit: int = 0,
        scores: Optional[Dict[int, int]] = None,
        ceiling: int = 0,
) -> Tuple[List[int], Set[int], List[int], Set[int]]:
    """按「缺集优先」排本轮待搜索集数。

    补集是功能缺失，洗版只是画质优化：更新中的剧（已入库 30+ 集、缺最新几集）
    不能把每集几 GB 的洗版排在补集前面，否则新集要等整轮洗完。

    :param local_episodes: 已入库（有本地基线）的集数
    :param all_expected_episodes: 订阅应覆盖的全部集数；为空表示集数未知
    :param target_episodes: 手动指定的集数（多为补集请求）；非空时全部按优先处理
    :param limit: 每轮最多洗版多少集，0 表示不限；只限制洗版部分，不限制缺集
    :param scores: 基线评分（集号 → 规则评分），供质量上限筛掉不搜
    :param ceiling: 质量上限，大于 0 时启用；已达上限的洗版候选直接跳过
        （不进搜索、不进 postponed——不是“本轮放后”，而是“本轮不搜”）；
        缺集与手动目标集不受上限影响（手动目标集在上面已全部划进 missing）
    :return: (本轮待搜索集数, 缺集集合, 因上限推迟到下一轮的集数,
        已达质量上限本轮不搜的集数)
    """
    searched = {int(episode) for episode in local_episodes}
    missing: Set[int] = set()
    if target_episodes is not None:
        # 手动指定集数：只处理这批，整批按优先处理，也不受每轮洗版上限约束
        searched = {int(episode) for episode in target_episodes}
        missing = set(searched)
    elif all_expected_episodes:
        missing = {int(episode) for episode in all_expected_episodes} - searched
        searched |= missing

    upgrade_only = searched - missing
    # 质量上限筛掉不搜（v1.29.4）：只筛洗版候选，缺集与手动目标集不动。
    ceiling_skipped: Set[int] = set()
    if ceiling and scores:
        ceiling = int(ceiling)
        ceiling_skipped = {
            episode for episode in upgrade_only
            if int(scores.get(episode) or 0) >= ceiling
        }
        upgrade_only -= ceiling_skipped
    postponed: List[int] = []
    if limit and limit > 0 and len(upgrade_only) > limit:
        ordered_upgrade = sorted(upgrade_only)
        postponed = ordered_upgrade[limit:]
        upgrade_only = set(ordered_upgrade[:limit])

    ordered = sorted(missing) + sorted(upgrade_only)
    return ordered, missing, postponed, ceiling_skipped


class UpgradeService(OwnerDelegator):
    def _set_upgrade_phase(
            self, subscribe, phase: str, progress: int, clear_search: bool = False, **extra_kwargs
    ) -> None:
        self._owner.metadata._set_task_phase(subscribe, phase, progress, clear_search=clear_search, **extra_kwargs)

    def _process_tv_subscribe_upgrade(
            self,
            subscribe,
            history: List[dict],
            transfer_details: List[Dict[str, Any]],
            transferred_count: int,
            target_episodes: Optional[Set[int]] = None,
            manual_upgrade: bool = False,
            manual_resources: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """洗版入口：建基线 → 排期（含质量上限）→ 组装洗版计划 → 调统一流水线。

        v1.29.4 第 3 步：流程本身在
        ``TelevisionSyncProcessor.process_tv_subscribe``
        （handlers/sync/television.py），这里只做参数组装，不再自有流程。
        三种处置（缺集→转存、已有+更优→替换、已有+不更优→跳过）与
        洗版结算（不结算订阅、只更新进度与单集评分）都在统一流水线里按
        plan 分支处理。手动洗版三个入口（订阅手动升级、媒体服务器内容
        升级、无绑定手动提交）都经这里进统一流水线，行为不变。

        :param subscribe: 订阅对象
        :param history: 历史记录列表
        :param transfer_details: 转存详情列表
        :param transferred_count: 当前已转存数量
        :return: 更新后的转存数量
        """
        try:
            transient_target = bool(getattr(subscribe, "_transient_target", False))
            season = subscribe.season or 1
            sub_key = self.subscription_budget_key(subscribe, MediaType.TV)
            if self._search_handler:
                self._search_handler.reset_subscription_budgets(sub_key)
            logger.debug(f"开始洗版：{subscribe.name} S{season:02d}")

            mediainfo: MediaInfo = self._subscribe_mediainfo(
                subscribe, MediaType.TV
            )
            if not mediainfo:
                logger.warning(f"【洗版转存】无法识别媒体信息 {subscribe.name}")
                return transferred_count

            # 读取已有评分，并以整季整理历史、插件历史和 Emby 构建统一基线。
            # （基线 + 真实网盘扫描 + 合并三合一，见 _build_episode_baseline；
            # 季目录列不出来时按整理历史/媒体服务器基线继续，判重校验
            # fail-open，不会把已有集误判成缺集。）
            self._set_upgrade_phase(subscribe, "建立洗版基线", 20)
            existing_ep_pri = self._read_ep_priority(subscribe)
            baseline = self._build_episode_baseline(
                subscribe, mediainfo, season, include_saved=True
            )
            if target_episodes is not None:
                normalized_targets = {
                    int(episode) for episode in target_episodes if int(episode) > 0
                }
                baseline = {
                    episode: item for episode, item in baseline.items()
                    if episode in normalized_targets
                }
            local_scores = {
                episode: int(item.get("score") or 0)
                for episode, item in baseline.items()
            }
            upgrade_log_prefix = f"【洗版转存】{mediainfo.title} S{season:02d}"

            if not local_scores:
                if manual_upgrade:
                    logger.warning(
                        f"{upgrade_log_prefix} 未找到所选内容对应的真实网盘旧文件，"
                        "无法执行洗版"
                    )
                    return transferred_count
                logger.info(
                    f"{upgrade_log_prefix} 未找到可用的现有版本基线，"
                    "回退到普通转存逻辑"
                )
                return self.process_tv_subscribe(
                    subscribe=subscribe, history=history,
                    transfer_details=transfer_details,
                    transferred_count=transferred_count,
                    allow_upgrade=False,
                    manual_resources=manual_resources,
                    manual_upgrade=False,
                    target_episodes=target_episodes,
                )

            logger.info(f"{upgrade_log_prefix} 已建立 {len(local_scores)} 集基线")

            # 构造待搜索的集数列表
            total_ep = subscribe.total_episode or 0
            start_ep = subscribe.start_episode or 1
            all_expected_episodes = set(range(start_ep, total_ep + 1)) if total_ep > 0 else set()

            # 质量上限（v1.29.4）：规则评分满分 100（MP FilterModule 首层
            # 优先级，见 QUALITY_CEILING_SCORE），基线评分已达上限的集不
            # 可能被超越，本轮不搜索、省请求。顺序：缺集优先，其次洗版
            #（可按每轮上限截断），见 plan_upgrade_episode_order。
            episodes_to_search, missing_episode_set, postponed_upgrade, ceiling_skipped = (
                plan_upgrade_episode_order(
                    local_episodes=local_scores.keys(),
                    all_expected_episodes=all_expected_episodes,
                    target_episodes=target_episodes,
                    limit=int(getattr(self, "_upgrade_episode_limit", 0) or 0),
                    scores=local_scores,
                    ceiling=QUALITY_CEILING_SCORE,
                )
            )
            if missing_episode_set:
                logger.info(
                    f"{upgrade_log_prefix} 缺集优先：先补 "
                    f"{self._format_episode_ranges(missing_episode_set)}"
                    f"（{len(missing_episode_set)} 集）"
                )
            if postponed_upgrade:
                logger.info(
                    f"{upgrade_log_prefix} 每轮洗版上限 "
                    f"{int(getattr(self, '_upgrade_episode_limit', 0) or 0)} 集，"
                    f"本轮暂不洗版 {len(postponed_upgrade)} 集："
                    f"{postponed_upgrade[:20]}"
                )
            if ceiling_skipped:
                logger.info(
                    f"{upgrade_log_prefix} 已达质量上限（评分≥"
                    f"{QUALITY_CEILING_SCORE}），本轮不搜索 "
                    f"{len(ceiling_skipped)} 集：{sorted(ceiling_skipped)[:20]}"
                )

            if not episodes_to_search:
                logger.info(f"{upgrade_log_prefix} 无可搜索的集数")
                return transferred_count

            logger.debug(f"{upgrade_log_prefix} 待搜索 {len(episodes_to_search)} 集：{episodes_to_search}")

            plan = {
                "mediainfo": mediainfo,
                "season": season,
                "sub_key": sub_key,
                "baseline": baseline,
                "local_scores": local_scores,
                "episodes_to_search": list(episodes_to_search),
                "missing_episode_set": set(missing_episode_set),
                "existing_ep_pri": existing_ep_pri,
                "all_expected_episodes": set(all_expected_episodes),
                # v1.29.9 阶段 3：洗版计划带漏斗归属标记，供统一流水线结算
                # 分支读取。注意计划里没有候选资源（搜索在流水线内），薄
                # 入口无法直接入队——流水线保留搜索与判定，执行侧由漏斗归
                # 属标记决定：finalize 边界入队，不再直出/直整。
                "funnel_route": bool(getattr(subscribe, "_funnel_route", False)),
            }
            return self.process_tv_subscribe(
                subscribe=subscribe, history=history,
                transfer_details=transfer_details,
                transferred_count=transferred_count,
                allow_upgrade=False,
                manual_resources=manual_resources,
                manual_upgrade=manual_upgrade,
                target_episodes=target_episodes,
                transient_target=transient_target,
                _upgrade_plan=plan,
            )
        except Exception as e:
            logger.error(f"洗版失败：{subscribe.name}，{e}")
        return transferred_count


    def auto_upgrade_scan(self):
        """网盘转存后刷新已开启洗版订阅的整季评分。"""
        from app.db.subscribe_oper import SubscribeOper

        with SessionFactory() as db:
            all_subs = SubscribeOper(db=db).list() or []

        targets = [
            subscribe for subscribe in all_subs
            if subscribe.type == MediaType.TV.value
               and self._is_cloud_upgrade_subscribe(subscribe)
        ]
        if not targets:
            logger.debug("网盘洗版没有待刷新订阅")
            return

        updated = 0
        failed = 0
        for subscribe in targets:
            try:
                updated += self._upgrade_scan_single_sub(subscribe)
            except Exception as error:
                failed += 1
                logger.error(
                    f"网盘洗版评分刷新失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )
        logger.info(
            f"网盘洗版评分刷新完成：{len(targets) - failed}/"
            f"{len(targets)} 个订阅，更新 {updated} 集"
        )

    def _upgrade_scan_single_sub(self, subscribe):
        """按统一基线刷新单个订阅的 episode_priority。"""
        from app.db.subscribe_oper import SubscribeOper

        season = int(subscribe.season or 1)
        mediainfo = self._subscribe_mediainfo(subscribe, MediaType.TV)
        if not mediainfo:
            logger.warning(f"洗版评分刷新失败：无法识别 {subscribe.name}")
            return 0

        baseline = self._build_episode_baseline(
            subscribe, mediainfo, season, include_saved=False
        )
        scores = {
            str(episode): int(item.get("score") or 0)
            for episode, item in baseline.items()
            if int(item.get("score") or 0) > 0
        }
        if not scores:
            logger.debug(f"洗版评分无可用基线：{subscribe.name} S{season:02d}")
            return 0
        if scores != self._read_ep_priority(subscribe):
            SubscribeOper().update(subscribe.id, {"episode_priority": scores})
            return len(scores)
        return 0

    def _self_heal_cleanup(self):
        """按真实整理/转存/媒体库基线刷新评分，避免依赖 STRM 文件名误删。"""
        from app.db.subscribe_oper import SubscribeOper

        try:
            with SessionFactory() as db:
                subscribes = SubscribeOper(db=db).list() or []
                subscribes = [
                    item for item in subscribes
                    if getattr(item, "episode_priority", None)
                ]
        except Exception as error:
            logger.warning(f"洗版评分自愈查询失败：{error}")
            return

        updated_count = 0
        cleaned_count = 0
        for subscribe in subscribes:
            try:
                season = int(subscribe.season or 1)
                mediainfo = self._subscribe_mediainfo(
                    subscribe, MediaType.TV
                )
                if not mediainfo:
                    continue
                baseline = self._build_episode_baseline(
                    subscribe, mediainfo, season, include_saved=False
                )
                if not baseline:
                    continue
                scores = {
                    str(episode): int(item.get("score") or 0)
                    for episode, item in baseline.items()
                    if int(item.get("score") or 0) > 0
                }
                old_scores = self._read_ep_priority(subscribe)
                if scores and scores != old_scores:
                    cleaned_count += len(set(old_scores) - set(scores))
                    SubscribeOper().update(
                        subscribe.id, {"episode_priority": scores}
                    )
                    updated_count += len(scores)
            except Exception as error:
                logger.warning(
                    f"洗版评分自愈失败：{subscribe.name} "
                    f"S{subscribe.season or 1:02d}，{error}"
                )

        if updated_count or cleaned_count:
            logger.info(
                f"洗版评分自愈完成：更新 {updated_count} 集，"
                f"清理 {cleaned_count} 条旧评分"
            )
