"""CloudSubscribeX 数据库过期数据清理与空间回收管理器。

解决私有库缺少生命周期管理导致 candidate_ledger_entries 与 sync_jobs
无节制膨胀的问题。定期清理过期排障数据并执行 SQLite 增量整理。
注意：这里是清理（purge），不是归档（archive）——过期行直接删除，
不迁移、不保留，1.12.20 起函数名如实改叫 purge（原名 archive_database）。
"""

from typing import Any, Dict

try:
    from app.log import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger(__name__)


def purge_expired_database_data(
    store_or_repos: Any,
    keep_days_ledger: int = 14,
    keep_days_jobs: int = 7,
    vacuum: bool = False,
) -> Dict[str, Any]:
    """统一执行私有数据库过期数据清理与碎片回收。

    :param store_or_repos: 仓储集合对象或包含各 repository 的服务宿主
    :param keep_days_ledger: 候选账本保留天数（默认 14 天）
    :param keep_days_jobs: 调度终态作业保留天数（默认 7 天）
    :param vacuum: 是否执行 VACUUM 回收磁盘空间
    :return: 统计字典，包含各表清理条数
    """
    result = {
        "ledger_purged": 0,
        "jobs_purged": 0,
        "vacuum_executed": False,
        "success": True,
    }

    if not store_or_repos:
        return result

    # 1. 清理候选跳过账本
    ledger_repo = getattr(store_or_repos, "candidate_ledger", None)
    if ledger_repo is None and hasattr(store_or_repos, "get_repository"):
        try:
            ledger_repo = store_or_repos.get_repository("candidate_ledger")
        except Exception as err:
            logger.debug(f"获取 candidate_ledger 仓储异常：{err}", exc_info=True)

    if ledger_repo and hasattr(ledger_repo, "purge"):
        try:
            count = int(ledger_repo.purge(keep_days=keep_days_ledger) or 0)
            result["ledger_purged"] = count
            if count > 0:
                logger.info(f"数据库清理：已自动清理 {count} 条超过 {keep_days_ledger} 天的候选账本记录")
        except Exception as err:
            logger.debug(f"清理候选账本异常：{err}", exc_info=True)
            result["success"] = False

    # 2. 清理调度作业终态记录
    jobs_repo = getattr(store_or_repos, "sync_jobs", None)
    if jobs_repo is None and hasattr(store_or_repos, "get_repository"):
        try:
            jobs_repo = store_or_repos.get_repository("sync_jobs")
        except Exception as err:
            logger.debug(f"获取 sync_jobs 仓储异常：{err}", exc_info=True)

    if jobs_repo and hasattr(jobs_repo, "purge_expired"):
        try:
            count = int(jobs_repo.purge_expired(keep_days=keep_days_jobs) or 0)
            result["jobs_purged"] = count
            if count > 0:
                logger.info(f"数据库清理：已自动清理 {count} 条超过 {keep_days_jobs} 天的终态调度作业")
        except Exception as err:
            logger.debug(f"清理调度作业异常：{err}", exc_info=True)
            result["success"] = False

    # 3. 碎片回收
    if vacuum and (result["ledger_purged"] > 0 or result["jobs_purged"] > 0):
        try:
            manager = getattr(store_or_repos, "manager", None)
            if manager and hasattr(manager, "session"):
                with manager.session(write=True) as db:
                    from sqlalchemy import text
                    db.execute(text("VACUUM"))
                result["vacuum_executed"] = True
                logger.info("数据库清理：SQLite VACUUM 磁盘空间碎片回收完成")
        except Exception as err:
            logger.debug(f"执行数据库 VACUUM 异常：{err}", exc_info=True)

    return result
