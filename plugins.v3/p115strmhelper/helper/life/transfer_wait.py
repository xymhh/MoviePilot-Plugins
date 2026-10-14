from threading import Event
from time import sleep, time
from typing import Any, Callable, List, Mapping, Optional, Tuple


def _get_queue_snapshot(queue_tasks: Any) -> Tuple[str, ...]:
    if isinstance(queue_tasks, Mapping):
        return tuple(sorted(f"{key!r}:{value!r}" for key, value in queue_tasks.items()))
    return tuple(sorted(repr(task) for task in queue_tasks))


def wait_for_transfer_complete(
    get_queue_tasks: Callable[[], List[Any]],
    stall_timeout_minutes: int,
    stop_event: Optional[Event],
    log: Any,
    max_wait_hours: Optional[int] = None,
    on_progress: Optional[Callable[[], None]] = None,
) -> bool:
    """
    等待 MoviePilot 整理任务完成并限制队列无进展时间

    :param get_queue_tasks (Callable): 整理队列读取函数
    :param stall_timeout_minutes (int): 队列无进展超时分钟数
    :param stop_event (Event): 可选的停止事件
    :param log (Any): 日志记录器
    :param max_wait_hours (Optional[int]): 等待绝对上限（小时），None/0 表示不设上限
    :param on_progress (Optional[Callable]): 每轮等待打一次心跳（正常等待＝进度）

    :return bool: 是否收到停止信号
    """
    queue_snapshot = None
    unchanged_since = None
    last_info_time = None
    stall_timeout_seconds = stall_timeout_minutes * 60
    wait_started_at: Optional[float] = None
    max_wait_seconds = None
    if max_wait_hours:
        try:
            hours = int(max_wait_hours)
        except (TypeError, ValueError):
            hours = 0
        if hours > 0:
            max_wait_seconds = hours * 3600

    while True:
        queue_tasks = get_queue_tasks()
        if not queue_tasks:
            return False

        current_time = time()
        if wait_started_at is None:
            wait_started_at = current_time
        # 等待期间线程一直在轮询队列，这就是进度：每轮都要喂给看门狗
        if on_progress is not None:
            on_progress()

        if max_wait_seconds is not None and current_time - wait_started_at >= (
            max_wait_seconds
        ):
            waited_minutes = int((current_time - wait_started_at) // 60)
            log.warning(
                "【监控生活事件】等待 MoviePilot 整理已达绝对上限 %s 小时"
                "（已等待 %s 分钟），仍有 %s 个任务未结束，恢复生活事件监控: %s",
                max_wait_seconds // 3600,
                waited_minutes,
                len(queue_tasks),
                queue_tasks,
            )
            return False

        current_snapshot = _get_queue_snapshot(queue_tasks)
        if current_snapshot != queue_snapshot:
            queue_snapshot = current_snapshot
            unchanged_since = current_time
            last_info_time = current_time

        wait_duration = current_time - unchanged_since
        wait_duration_minutes = int(wait_duration // 60)

        if wait_duration >= stall_timeout_seconds:
            log.warning(
                "【监控生活事件】MoviePilot 整理队列已连续 %s 分钟无变化，"
                "仍有 %s 个任务未结束，将恢复生活事件监控: %s",
                stall_timeout_minutes,
                len(queue_tasks),
                queue_tasks,
            )
            return False

        if wait_duration >= 15 * 60:
            time_since_last_info = current_time - last_info_time
            if time_since_last_info >= 60:
                log.info(
                    "【监控生活事件】MoviePilot 整理队列已连续 %s 分钟无变化，"
                    "等待整理完成后继续监控生活事件...",
                    wait_duration_minutes,
                )
                last_info_time = current_time
        else:
            log.debug(
                "【监控生活事件】MoviePilot 整理运行中，等待整理完成后继续监控生活事件..."
            )

        wait_seconds = min(20, stall_timeout_seconds - wait_duration)
        if stop_event:
            if stop_event.wait(timeout=wait_seconds):
                return True
        else:
            sleep(wait_seconds)
