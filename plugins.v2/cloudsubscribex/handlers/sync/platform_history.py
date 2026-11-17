"""MP 整理历史镜像清理（Phase 2 v1.13.3）。

1.12.14 起插件不再向 MP 的 TransferHistory 表写任何记录（MP 历史是唯一
账本）；本模块只剩一件事：启动时删除 1.12.14 之前插件写进 MP 历史的
托管行（幂等）。海报恢复（restore_history_posters）随插件 history 表
一起删除——转存记录页已不存在，没有页面再需要它。
"""

from app.db import SessionFactory
from app.log import logger

from ...core import OwnerDelegator


class PlatformHistoryService(OwnerDelegator):
    """清理旧的 MP 历史镜像行。"""

    def _delete_platform_transfer_histories(self) -> int:
        """删除全部插件托管的整理历史行，不触碰关联媒体文件。"""
        try:
            from app.db.models.transferhistory import TransferHistory
            from sqlalchemy import or_

            lock = getattr(self, "_platform_history_lock", None)
            if lock and not lock.acquire(timeout=1.0):
                logger.debug("MoviePilot 整理历史删除仍在执行，本批次已跳过")
                return 0
            try:
                with SessionFactory() as db:
                    query = db.query(TransferHistory).filter(or_(
                        TransferHistory.src.like("cloudsubscribe://%"),
                        TransferHistory.downloader == "网盘订阅助手",
                    ))
                    deleted = query.delete(synchronize_session=False)
                    if deleted:
                        db.commit()
                    return int(deleted or 0)
            finally:
                if lock:
                    lock.release()
        except Exception as error:
            logger.error(f"清理整理历史失败：{error}")
            return 0

    def remove_platform_transfer_history_mirror(self) -> int:
        """清理插件已写进 MP 整理历史的镜像记录，幂等、每次启动都跑。

        删掉全部插件托管行（src 以 cloudsubscribe:// 开头，或
        downloader 为「网盘订阅助手」）。不写任何数据标记——插件
        的数据键必须事先声明（CloudSubscribeDataStore.handles），
        1.12.14 用的 platform_history_mirror_removed 未声明，写入
        时抛 ValueError 导致插件加载失败、更新回滚到 1.12.11。
        这里不需要标记：清理是幂等的，第一次删掉旧记录，之后每次
        启动查到 0 条直接返回；且只删插件自己写的行，MP 自己的
        记录永远不受影响。
        """
        removed = self._delete_platform_transfer_histories()
        if removed:
            logger.info(f"已清理插件写入 MP 整理历史的镜像记录 {removed} 条")
        return int(removed or 0)
