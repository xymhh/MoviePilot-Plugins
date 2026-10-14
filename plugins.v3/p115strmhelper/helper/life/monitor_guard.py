"""
生活事件监控「静默停摆」看门狗与拉取防护的纯函数集合。

本模块只依赖标准库，原因有两条：

1. ``service`` 与 ``helper.life.client`` 都在运行期引用这里的逻辑，
   必须与 MoviePilot 宿主、p115client 解耦；
2. 自包含测试环境（无 p115client、无宿主）要能按文件路径直接加载本模块做断言。

对应任务书 2.8.78 的 A（心跳判定输入）、B（看门狗）、C（回放起始时间）、
E（单轮全量抽干 + 计数告警）四段实现要求。
"""

import sys
import traceback
from typing import Any, Callable, Iterator, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# 配置默认值与边界
# ---------------------------------------------------------------------------

#: 停摆看门狗阈值默认值（分钟）
DEFAULT_STALL_TIMEOUT_MINUTES = 15
#: 停摆看门狗阈值最小值（分钟）：1~4 视为无效，钳到 5；0 是唯一的关闭开关
MIN_STALL_TIMEOUT_MINUTES = 5
#: 停摆看门狗阈值上限（分钟）：24 小时，再大看门狗就失去意义
MAX_STALL_TIMEOUT_MINUTES = 24 * 60

#: 回放宽限默认值（分钟）
DEFAULT_REPLAY_GRACE_MINUTES = 10
#: 回放宽限上限（分钟）
MAX_REPLAY_GRACE_MINUTES = 24 * 60

#: 整理等待绝对上限默认值（小时）
DEFAULT_TRANSFER_WAIT_MAX_HOURS = 6
#: 整理等待绝对上限上限（小时）
MAX_TRANSFER_WAIT_MAX_HOURS = 7 * 24

#: 单轮拉取条数告警阈值默认值（只告警，不截断）
DEFAULT_LARGE_WINDOW_WARN_EVENTS = 2000
#: 单轮拉取条数告警阈值上限
MAX_LARGE_WINDOW_WARN_EVENTS = 100_000

#: 从日志对象上找可写句柄时按顺序探测的属性名
_LOG_STREAM_ATTRS = (
    "handlers",
    "_handlers",
    "_core",
    "core",
    "sink",
    "stream",
    "_sink",
    "file",
)


def _as_int(raw: Any) -> Optional[int]:
    """
    尝试把配置值转成 int，转不动返回 None

    :param raw (Any): 原始配置值

    :return Optional[int]: 整数值，失败返回 None
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(raw, float) and raw != value:
        # 例如 -0.5 截断成 0 会把看门狗悄悄关掉，按非法处理
        return None
    return value


def _as_number(raw: Any) -> Optional[float]:
    """
    尝试把配置值转成 float，转不动返回 None

    :param raw (Any): 原始配置值

    :return Optional[float]: 数值，失败返回 None
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def normalize_stall_timeout_minutes(raw: Any) -> int:
    """
    归一化停摆看门狗阈值

    规则：0=关闭看门狗；1~4 钳到最小 5；负数/非数字视为非法回落默认 15；
    超过 24 小时钳到 24 小时。

    :param raw (Any): 原始配置值

    :return int: 生效阈值（分钟），0 表示关闭
    """
    value = _as_int(raw)
    if value is None or value < 0:
        return DEFAULT_STALL_TIMEOUT_MINUTES
    if value == 0:
        return 0
    return max(MIN_STALL_TIMEOUT_MINUTES, min(value, MAX_STALL_TIMEOUT_MINUTES))


def normalize_replay_grace_minutes(raw: Any) -> int:
    """
    归一化回放宽限

    规则：0=不回放；负数/非数字回落默认 10；上限 24 小时。

    :param raw (Any): 原始配置值

    :return int: 生效宽限（分钟）
    """
    value = _as_int(raw)
    if value is None or value < 0:
        return DEFAULT_REPLAY_GRACE_MINUTES
    return min(value, MAX_REPLAY_GRACE_MINUTES)


def normalize_transfer_wait_max_hours(raw: Any) -> int:
    """
    归一化整理等待绝对上限

    规则：小于 1（含 0、负数）或非数字视为非法回落默认 6 小时；上限 7 天。

    :param raw (Any): 原始配置值

    :return int: 生效上限（小时）
    """
    value = _as_int(raw)
    if value is None or value < 1:
        return DEFAULT_TRANSFER_WAIT_MAX_HOURS
    return min(value, MAX_TRANSFER_WAIT_MAX_HOURS)


def normalize_large_window_warn_events(raw: Any) -> int:
    """
    归一化「单轮拉取条数告警阈值」

    规则：小于 1（含 0、负数）或非数字视为非法回落默认 2000；上限 100000。
    注意这只是**告警**阈值，不会截断事件（截断会永久丢事件，见 drain_events_batch）。

    :param raw (Any): 原始配置值

    :return int: 生效阈值（条）
    """
    value = _as_int(raw)
    if value is None or value < 1:
        return DEFAULT_LARGE_WINDOW_WARN_EVENTS
    return min(value, MAX_LARGE_WINDOW_WARN_EVENTS)


# ---------------------------------------------------------------------------
# B. 停摆判定与处置
# ---------------------------------------------------------------------------


def evaluate_stall(
    last_progress_ts: Optional[float],
    now: float,
    timeout_minutes: Any,
) -> Optional[float]:
    """
    判断监控线程是否「活着但不干活」

    :param last_progress_ts (Optional[float]): 最近一次心跳时间戳
    :param now (float): 当前时间戳
    :param timeout_minutes (Any): 阈值（分钟），0 或负数语义见 normalize

    :return Optional[float]: 已停摆分钟数；未停摆、看门狗关闭或心跳未知时返回 None
    """
    timeout = normalize_stall_timeout_minutes(timeout_minutes)
    if timeout <= 0:
        return None
    progress_ts = _as_number(last_progress_ts)
    if progress_ts is None or progress_ts <= 0:
        # 心跳未知/非法：没有证据就不判，避免拿垃圾数据杀线程
        return None
    elapsed = float(now) - progress_ts
    if elapsed <= timeout * 60:
        return None
    return elapsed / 60.0


def get_thread_frame(thread: Any) -> Any:
    """
    取线程当前的 Python 栈帧

    :param thread (Any): threading.Thread 实例

    :return Any: 栈帧，取不到返回 None
    """
    ident = getattr(thread, "ident", None)
    if ident is None:
        return None
    try:
        return sys._current_frames().get(ident)
    except Exception:
        return None


def format_thread_stack(thread: Any) -> str:
    """
    把线程当前调用栈格式化成文本

    :param thread (Any): threading.Thread 实例

    :return str: 调用栈文本，取不到时为空串
    """
    frame = get_thread_frame(thread)
    if frame is None:
        return ""
    return "".join(traceback.format_stack(frame))


def _is_text_stream(obj: Any) -> bool:
    """
    判断对象是否像可写文本句柄

    :param obj (Any): 候选对象

    :return bool: 同时具备 write / flush 且不是字符串时返回 True
    """
    if isinstance(obj, (str, bytes, bytearray, int, float, bool)):
        return False
    try:
        return callable(getattr(obj, "write", None)) and callable(
            getattr(obj, "flush", None)
        )
    except Exception:
        return False


def find_log_stream(log: Any, max_depth: int = 4) -> Any:
    """
    尽力从日志对象里挖出可写句柄（拿不到返回 None）

    标准库 logging 走 ``logger.handlers[].stream``，loguru 走
    ``logger._core.handlers[].sink``，这里做有深度上限的广度探测。

    :param log (Any): 日志对象
    :param max_depth (int): 探测深度上限

    :return Any: 可写句柄，找不到返回 None
    """
    if log is None:
        return None
    seen = set()
    queue: List[Tuple[Any, int]] = [(log, 0)]
    while queue:
        obj, depth = queue.pop(0)
        if obj is None or id(obj) in seen or depth > max_depth:
            continue
        seen.add(id(obj))
        if _is_text_stream(obj):
            return obj
        if isinstance(obj, Mapping):
            queue.extend((value, depth + 1) for value in obj.values())
            continue
        if isinstance(obj, (list, tuple, set, frozenset)):
            queue.extend((item, depth + 1) for item in obj)
            continue
        for attr in _LOG_STREAM_ATTRS:
            try:
                sub = getattr(obj, attr, None)
            except Exception:
                sub = None
            if sub is not None:
                queue.append((sub, depth + 1))
    return None


def write_thread_stack(log: Any, thread: Any) -> str:
    """
    把停摆线程的调用栈写进插件日志

    能拿到日志句柄就用 ``traceback.print_stack(frame, file=句柄)``，
    拿不到就退回 ``logger.error``，两种方式都保证栈出现在插件日志里。

    :param log (Any): 日志对象
    :param thread (Any): threading.Thread 实例

    :return str: 写出的调用栈文本（拿不到栈时为空串）
    """
    ident = getattr(thread, "ident", None)
    frame = get_thread_frame(thread)
    if frame is None:
        log.warning(
            f"【监控生活事件】无法获取停摆线程的调用栈（thread ident={ident}）"
        )
        return ""
    header = f"【监控生活事件】停摆线程调用栈（thread ident={ident}）：\n"
    stream = find_log_stream(log)
    if stream is not None:
        try:
            stream.write(header)
            traceback.print_stack(frame, file=stream)
            stream.flush()
            return "".join(traceback.format_stack(frame))
        except Exception:
            pass
    log.error(header + "".join(traceback.format_stack(frame)))
    return "".join(traceback.format_stack(frame))


def detect_and_handle_stall(
    *,
    last_progress_ts: Optional[float],
    now: float,
    timeout_minutes: Any,
    thread: Any,
    log: Any,
    notify: Optional[Callable[[float], None]] = None,
    stop: Optional[Callable[[], None]] = None,
    start: Optional[Callable[[], None]] = None,
) -> Optional[float]:
    """
    判停摆并按任务书顺序处置：WARNING → 打印线程栈 → 通知 → 停止 → 回放重启

    :param last_progress_ts (Optional[float]): 最近一次心跳时间戳
    :param now (float): 当前时间戳
    :param timeout_minutes (Any): 阈值（分钟）
    :param thread (Any): 被守护的线程
    :param log (Any): 日志对象
    :param notify (Optional[Callable]): 通知回调，入参为停摆分钟数
    :param stop (Optional[Callable]): 停止监控线程回调
    :param start (Optional[Callable]): 回放重启监控线程回调

    :return Optional[float]: 判定的停摆分钟数；未停摆返回 None
    """
    stalled_minutes = evaluate_stall(last_progress_ts, now, timeout_minutes)
    if stalled_minutes is None:
        return None

    timeout = normalize_stall_timeout_minutes(timeout_minutes)
    thread_name = getattr(thread, "name", None) or "unknown"
    thread_ident = getattr(thread, "ident", None)
    log.warning(
        f"【监控生活事件】监控线程已 {stalled_minutes:.1f} 分钟无进度"
        f"（阈值 {timeout} 分钟），判定为停摆："
        f"thread={thread_name} ident={thread_ident}，"
        f"正在打印线程调用栈并以回放模式重启"
    )
    write_thread_stack(log, thread)
    if notify is not None:
        notify(stalled_minutes)
    if stop is not None:
        stop()
    if start is not None:
        start()
    return stalled_minutes


# ---------------------------------------------------------------------------
# C. 回放起始游标
# ---------------------------------------------------------------------------


def resolve_monitor_life_start(
    *,
    replay: bool,
    pull_mode: str,
    persisted: Any,
    now: float,
    grace_minutes: Any,
) -> Tuple[float, int]:
    """
    计算生活事件监控线程的起始游标

    回放模式（看门狗重启）：``min(持久化游标, now) - grace``，不丢停摆窗口内的事件；
    非回放模式（用户手工重载 / 改配置）：完全沿用 ``monitor_life_first_pull_mode``
    的 latest / all / last 语义，不改老行为。

    :param replay (bool): 是否为看门狗触发的回放重启
    :param pull_mode (str): 首轮拉取模式 latest / all / last
    :param persisted: 插件数据里持久化的游标 dict
    :param now (float): 当前时间戳
    :param grace_minutes (Any): 回放宽限配置（分钟）

    :return Tuple[float, int]: (from_time, from_id)
    """
    cursor = persisted if isinstance(persisted, Mapping) else None
    cursor_time = _as_number(cursor.get("from_time")) if cursor else None
    cursor_id = _as_int(cursor.get("from_id")) if cursor else None

    if replay:
        grace = normalize_replay_grace_minutes(grace_minutes)
        if cursor_time is not None and cursor_time > 0:
            base = min(cursor_time, float(now))
            from_id = 0 if grace else (cursor_id or 0)
        else:
            # 游标缺失：退回 now，再减 grace，至少把最近一个宽限窗口补齐
            base = float(now)
            from_id = 0
        return (max(0.0, base - grace * 60), int(from_id))

    if pull_mode == "all":
        return (0, 0)
    if pull_mode == "last":
        if cursor_time is not None and cursor_time >= 0:
            return (cursor_time, int(cursor_id or 0))
        # 游标缺失时与旧实现一致：回落到 latest 语义
        return (float(now), 0)
    return (float(now), 0)


# ---------------------------------------------------------------------------
# E. 单轮拉取上限与游标推进
# ---------------------------------------------------------------------------


def drain_events_batch(
    first_event: Any,
    events_iterator: Iterator[Any],
    *,
    on_progress: Optional[Callable[[], None]] = None,
    on_count: Optional[Callable[[int], None]] = None,
    progress_every: int = 0,
) -> List[Any]:
    """
    抽干本轮事件迭代器（逐条迭代，**不截断**）

    为什么不截断：115 生活事件接口的 ``from_time`` 是取值**下界**，迭代器只按
    「新 → 旧」顺序产出，而游标只能记住「本批最新一条」。一旦截断，比截断点更旧、
    又没被取到的事件会落在游标之前，**永远不会再被拉取**（永久丢事件）。
    所以这里只做「全量抽干 + 逐条心跳 + 计数可见」，不做上限截断。

    :param first_event (Any): 调用方已经取出的第一条事件
    :param events_iterator (Iterator): 事件迭代器
    :param on_progress (Optional[Callable]): 每取到一条打一次心跳
    :param on_count (Optional[Callable]): 每累计 progress_every 条回调一次（日志可见性）
    :param progress_every (int): 进度回调间隔条数，<=0 不回调

    :return List: 本批事件（新 → 旧，与迭代器顺序一致）
    """
    events: List[Any] = [first_event]
    if on_progress is not None:
        on_progress()
    for item in events_iterator:
        events.append(item)
        if on_progress is not None:
            on_progress()
        if (
            on_count is not None
            and progress_every > 0
            and len(events) % progress_every == 0
        ):
            on_count(len(events))
    return events


def advance_cursor_from_events(
    from_time: float, from_id: int, events: List[Any]
) -> Tuple[float, int]:
    """
    按本轮已取到的事件推进游标

    顺序与 ``once_pull`` 的逐条处理一致（reversed 后取最后一条），
    因此得到的是本批次最新的 (update_time, id)。

    :param from_time (float): 推进前的起始时间
    :param from_id (int): 起始 ID
    :param events (List): 本批事件

    :return Tuple[float, int]: 推进后的 (from_time, from_id)
    """
    result_time, result_id = from_time, from_id
    for event in reversed(events):
        result_time = int(event["update_time"])
        result_id = int(event["id"])
    return result_time, result_id
