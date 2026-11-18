"""插件业务服务。"""

from .checkin import CheckinService
from .discovery import Candidate, SubmitResult, submit_candidate
from .mp_organize_tracker import MpOrganizeTracker
from .offline import OfflineTaskService
from .organize import OrganizeService
from .platform import PlatformIntegrationService
from .residue import ResidueService
from .runtime import SyncRuntimeService, sync_lock
from .scoring import SubscriptionScoringService
from .skip_stats import SkipStatsCollector
from .subscription import SubscriptionControlService
from .sync import SyncExecutionService
from .upload_quota import UploadQuota, clamp_limit_gb

__all__ = [
    "SubscriptionControlService",
    "SubscriptionScoringService",
    "SyncExecutionService",
    "SyncRuntimeService",
    "OfflineTaskService",
    "PlatformIntegrationService",
    "CheckinService",
    "OrganizeService",
    "ResidueService",
    "SkipStatsCollector",
    "sync_lock",
    # 候选提交值类型（discovery）：Candidate 是推送型渠道发现的候选，
    # SubmitResult 是 submit_candidate 的提交结果；两个都是纯数据，
    # 导出是为让调用方不用深挖模块路径就能组装/读取它们。
    "Candidate",
    "SubmitResult",
    "submit_candidate",
    # MP 整理轮询器：按 MP 整理历史判定终态（成功只认 status=True）。
    "MpOrganizeTracker",
    # 跨盘真实上传 115 的每日字节额度（fail-open，只统计真实上传字节）。
    "UploadQuota",
    "clamp_limit_gb",
]
