from threading import Event
from time import time

from ...core.config import configer
from ...core.i18n import i18n
from ...core.message import post_message
from ...helper.life import MonitorLife
from ...helper.life.monitor_guard import resolve_monitor_life_start
from ...utils.exception import NotifyExceptionFormatter
from ...utils.sentry import sentry_manager

from app.sdk.logging import logger
from app.schemas.types import MessageType

#: 生活事件游标持久化用的插件数据 key（沿用旧 key，last 模式与回放共用同一份游标）
MONITOR_LIFE_CURSOR_KEY = "monitor_life_strm_files"


@sentry_manager.capture_plugin_exceptions
def monitor_life_thread_worker(
    monitor_life: MonitorLife,
    stop_event: Event,
    replay: bool = False,
):
    """
    生活事件监控线程工作函数

    :param monitor_life: 已初始化的 MonitorLife 对象
    :param stop_event: threading.Event 对象，用于接收停止信号
    :param replay: 是否为看门狗触发的回放启动（True 时从「持久化游标 - grace」起拉；
        False 时严格按 monitor_life_first_pull_mode 语义，用户手工重载行为不变）
    """
    try:
        monitor_life.stop_event = stop_event
        # 线程刚启动就记一次进度，避免守护线程把「刚拉起」误判成停摆
        monitor_life.touch_progress()

        logger.info("【监控生活事件】生活事件状态检查中...")
        if not monitor_life.check_status():
            logger.error("【监控生活事件】生活事件状态检查失败，无法启动监控")
            return

        logger.info("【监控生活事件】生活事件监控启动中...")

        pull_mode = configer.monitor_life_first_pull_mode
        persisted = configer.get_plugin_data(MONITOR_LIFE_CURSOR_KEY)
        from_time, from_id = resolve_monitor_life_start(
            replay=replay,
            pull_mode=pull_mode,
            persisted=persisted,
            now=time(),
            grace_minutes=getattr(
                configer, "monitor_life_replay_grace_minutes", None
            ),
        )
        if replay:
            logger.info(
                f"【监控生活事件】回放模式启动：持久化游标={persisted}，"
                f"起始游标 from_time={from_time} from_id={from_id}"
            )

        while True:
            if stop_event.is_set():
                logger.info("【监控生活事件】收到停止信号，退出上传事件监控")
                configer.save_plugin_data(
                    MONITOR_LIFE_CURSOR_KEY,
                    {"from_time": from_time, "from_id": from_id},
                )
                break

            try:
                from_time, from_id = monitor_life.once_pull(
                    from_time=from_time, from_id=from_id
                )
            except Exception as e:
                logger.error(f"【监控生活事件】生活事件监控运行失败: {e}")
                if stop_event.is_set():
                    logger.info("【监控生活事件】收到停止信号，退出监控")
                    return
                if configer.notify:
                    _err_text = NotifyExceptionFormatter.format_exception_for_notify(e)
                    post_message(
                        mtype=MessageType.Plugin,
                        title=i18n.translate("monitor_life_error_title"),
                        text=(
                            f"\n{i18n.translate('monitor_life_error_text', error=_err_text)}\n"
                        ),
                    )
                logger.info("【监控生活事件】30s 后尝试重新启动生活事件监控")
                if stop_event.wait(timeout=30):
                    logger.info("【监控生活事件】等待期间收到停止信号，退出监控")
                    return
            else:
                # 每轮结束：记一次心跳 + 持久化游标，让看门狗与回放随时有据可依
                monitor_life.touch_progress()
                configer.save_plugin_data(
                    MONITOR_LIFE_CURSOR_KEY,
                    {"from_time": from_time, "from_id": from_id},
                )

        logger.info("【监控生活事件】已退出生活事件监控")
    except Exception as e:
        logger.error(f"【监控生活事件】线程运行异常: {e}")
        if configer.notify:
            _err_text = NotifyExceptionFormatter.format_exception_for_notify(e)
            post_message(
                mtype=MessageType.Plugin,
                title=i18n.translate("monitor_life_exit_title"),
                text=(
                    f"\n{i18n.translate('monitor_life_exit_text', error=_err_text)}\n"
                ),
            )
        raise
