"""CloudSubscribeX 标准结构化异常体系。

将原项目中分散散落的 780 处裸 `except Exception` 收敛为四级分类，
彻底终结“错误被吞掉、外层只能看到空列表”的架构缺陷。
"""

from typing import Optional


class CloudSubscribeError(Exception):
    """CloudSubscribeX 异常基类。"""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = str(message or "")
        self.detail = str(detail or "")

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} ({self.detail})"
        return self.message


class NetworkError(CloudSubscribeError):
    """网络抖动/请求超时（可自动重试类异常）。"""

    def __init__(self, message: str, detail: Optional[str] = None, retry_after: float = 1.0):
        super().__init__(message, detail)
        self.retryable = True
        self.retry_after = float(retry_after)


class AuthExpiredError(CloudSubscribeError):
    """网盘或搜索渠道鉴权失效（Cookie / Token 过期，需触发熔断与告警通知）。"""

    def __init__(self, provider: str, message: str = "鉴权已失效，请重新登录"):
        super().__init__(f"[{provider}] {message}")
        self.provider = str(provider)


class ResourceInvalidError(CloudSubscribeError):
    """分享链接本身失效（已取消/已删除/违规封禁/参数错误，直接准确记入 KIND_INVALID，不可重试）。"""

    def __init__(self, share_url: str, reason: str = "分享链接不可用"):
        super().__init__(reason)
        self.share_url = str(share_url)
        self.reason = str(reason)


class MatchMismatchError(CloudSubscribeError):
    """内容或集号不匹配（季集号差、根目录不匹配，记入负缓存跳过列目录）。"""

    def __init__(self, title: str, reason: str):
        super().__init__(reason)
        self.title = str(title)
        self.reason = str(reason)


class TaskAbortedError(CloudSubscribeError):
    """任务被用户手动停止或系统中断，触发 100ms 快速退出。"""

    def __init__(self, message: str = "任务已被手动中止"):
        super().__init__(message)


class TaskTimeoutError(CloudSubscribeError):
    """任务执行超出单轮时间预算，优雅结算后下一轮续跑。"""

    def __init__(self, elapsed: float, budget: float):
        super().__init__(f"任务执行耗时 {elapsed:.1f}s 超出单轮预算 {budget:.1f}s")
        self.elapsed = elapsed
        self.budget = budget
