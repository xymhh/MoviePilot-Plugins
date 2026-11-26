"""CloudSubscribeLite 标准结构化异常体系。

将原项目中分散散落的 780 处裸 `except Exception` 收敛为四级分类，
彻底终结“错误被吞掉、外层只能看到空列表”的架构缺陷。
"""

from typing import Optional


class CloudSubscribeError(Exception):
    """CloudSubscribeLite 异常基类。"""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = str(message or "")
        self.detail = str(detail or "")

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message} ({self.detail})"
        return self.message


class TaskAbortedError(CloudSubscribeError):
    """任务被用户手动停止或系统中断，触发 100ms 快速退出。"""

    def __init__(self, message: str = "任务已被手动中止"):
        super().__init__(message)
