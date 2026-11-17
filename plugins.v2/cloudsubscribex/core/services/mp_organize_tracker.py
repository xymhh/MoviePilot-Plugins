"""MP 整理中登记表与整理轮询器（v1.12.6 B/C）。

跨组件交接用的整理中登记表：transfer_ops 在 MP 整理触发成功后写入，
下载页读取它展示“整理中”态，finalize 在任务终态时清除。

MpOrganizeTracker 轮询 MP 侧的整理状态（v1.12.6 C）：先查 MP 整理历史
（成功记录、失败记录），再查 durable 待整理登记（人工复核态），最后查
内存整理队列。成功只认历史记录 status 为 True，不用任何目标存在性判断
（目标可能只是同名小文件）；失败原因原样透传 MP 的 errmsg/last_error。
"""

import time
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Tuple

from app.log import logger

#: MP 整理最长等待：上传 1.5 GB 到 115 约 20–45 分钟，还要排队，取 90 分钟。
MP_ORGANIZE_WAIT_TIMEOUT_SECONDS = 90 * 60
#: MP 转移队列连续无进展即判停滞：15 分钟。
MP_ORGANIZE_QUEUE_STALL_SECONDS = 15 * 60
#: 触发后先给 MP 一点启动时间，10 分钟内无队列/历史也不算异常。
MP_ORGANIZE_GRACE_SECONDS = 10 * 60

#: 失败原因截断长度（约 200 字，原样呈现不改写）。
_MP_ORGANIZE_DETAIL_LIMIT = 200

#: parent_task_id -> 登记项；登记项结构见 mark_mp_organizing。
_MP_ORGANIZING: Dict[str, Dict[str, object]] = {}


def mark_mp_organizing(
        parent_task_id: str,
        mp_organize: Dict[str, object],
        status_text: str = "",
) -> None:
    """登记一项目前正在 MP 整理（触发成功后由 transfer_ops 调用）。

    :param parent_task_id: 父任务 id（如订阅任务 id）
    :param mp_organize: trigger_mp_organize 成功时返回的 info
        （含 src_path/target_dir/target_storage/target_name 等）
    :param status_text: 展示用的当前状态文案（可空）
    """
    key = str(parent_task_id or "").strip()
    if not key:
        return
    _MP_ORGANIZING[key] = {
        "mp_organize": dict(mp_organize or {}),
        "status_text": str(status_text or ""),
        "updated_at": time.time(),
    }


def clear_mp_organizing(parent_task_id: str) -> None:
    """清除一项目的整理中登记（finalize 终态时调用）。"""
    _MP_ORGANIZING.pop(str(parent_task_id or "").strip(), None)


def list_mp_organizing() -> Dict[str, Dict[str, object]]:
    """返回全部整理中登记项的浅拷贝（调用方不要直接改原表）。"""
    return dict(_MP_ORGANIZING)


def _matching_mp_organizing_keys(src_path: str) -> list:
    """找出登记表中 src_path 相同的全部登记键。"""
    src = str(src_path or "").strip()
    if not src:
        return []
    return [
        key
        for key, entry in _MP_ORGANIZING.items()
        if str(
            (entry.get("mp_organize") or {}).get("src_path") or ""
        ) == src
    ]


def clear_mp_organizing_by_src(src_path: str) -> None:
    """按源路径清除整理中登记（finalize 侧不知道 parent_task_id 时用）。"""
    for key in _matching_mp_organizing_keys(src_path):
        _MP_ORGANIZING.pop(key, None)


def update_mp_organizing_by_src(src_path: str, status_text: str) -> None:
    """按源路径更新整理中登记的状态文案（如队列停滞提示）。"""
    for key in _matching_mp_organizing_keys(src_path):
        entry = _MP_ORGANIZING.get(key)
        if entry is None:
            continue
        entry["status_text"] = str(status_text or "")
        entry["updated_at"] = time.time()


def _truncate_detail(text: str) -> str:
    """失败原因原文截断到约 200 字，不解析、不改写。"""
    text = str(text or "").strip()
    if len(text) <= _MP_ORGANIZE_DETAIL_LIMIT:
        return text
    return text[:_MP_ORGANIZE_DETAIL_LIMIT] + "…"


class MpOrganizeTracker:
    """轮询 MP 整理链，判断一次 MP 整理当前处于什么状态。

    顺序：MP 整理历史 → durable 待整理登记（人工复核）→ 内存整理队列。
    成功只认历史记录 status 为 True；人工复核（某步骤执行异常后外部结果
    unknown、禁止自动重放）按失败处理，原因取 last_error 原文。

    :param history_oper: 可注入的整理历史操作对象（测试用 fake）；
        不传时每次 poll 用 SessionFactory 新开会话构造真实 Oper。
    :param pending_oper: 可注入的 durable 待整理登记操作对象；
        不传时每次 poll 用 SessionFactory 新开会话构造真实 Oper。
    :param jobview: 可注入的 MP 整理队列视图（JobManager 实例）；
        不传时每次 poll 用 TransferChain().jobview 懒构造。
    """

    def __init__(
            self,
            history_oper: Optional[object] = None,
            pending_oper: Optional[object] = None,
            jobview: Optional[object] = None,
    ) -> None:
        self._history_oper = history_oper
        self._pending_oper = pending_oper
        self._jobview = jobview

    @contextmanager
    def _history_oper_context(self) -> Iterator[object]:
        """取整理历史 Oper；未注入时用新会话构造，用完即关。"""
        if self._history_oper is not None:
            yield self._history_oper
            return
        from app.db import SessionFactory
        from app.db.transferhistory_oper import TransferHistoryOper

        with SessionFactory() as db:
            yield TransferHistoryOper(db=db)

    @contextmanager
    def _pending_oper_context(self) -> Iterator[object]:
        """取 durable 待整理登记 Oper；未注入时用新会话构造，用完即关。"""
        if self._pending_oper is not None:
            yield self._pending_oper
            return
        from app.db import SessionFactory
        from app.db.oper.transferpending import TransferPendingOper

        with SessionFactory() as db:
            yield TransferPendingOper(db=db)

    def _jobview_instance(self) -> object:
        """取 MP 整理队列视图；未注入时用 TransferChain 懒构造。"""
        if self._jobview is not None:
            return self._jobview
        from app.chain.transfer import TransferChain

        return TransferChain().jobview

    def poll(
            self, mp_organize: Dict[str, object]
    ) -> Tuple[str, str, Optional[str]]:
        """轮询一次 MP 整理的状态。

        :param mp_organize: trigger_mp_organize 成功时返回的 info
            （至少含 src_path；src_storage 缺省为 local）
        :return: (state, detail, dest_path|None)；state 为 queued /
            running / succeeded / failed / missing / unavailable 之一；
            succeeded 时 detail 与 dest_path 均为历史记录的 dest；failed
            时 detail 为 MP 原因原文（截断到约 200 字）；missing 是三处
            后端都健康但查无记录（MP 未受理）；unavailable 是 MP 侧不可用
            （后端抛异常）——两者不能混同：missing 过宽限期可判“未受理”
            而 abort，unavailable 只能继续等（受 90 分钟总超时约束）。
        """
        src_path = str((mp_organize or {}).get("src_path") or "").strip()
        src_storage = (
            str((mp_organize or {}).get("src_storage") or "local").strip()
            or "local"
        )
        if not src_path:
            return ("missing", "MP 整理身份缺少源路径", None)
        try:
            return self._poll_backends(src_path, src_storage)
        except Exception as error:
            logger.debug(f"MP 整理轮询失败：{error}", exc_info=True)
            return ("unavailable", "MP 整理链不可用", None)

    def _poll_backends(
            self, src_path: str, src_storage: str
    ) -> Tuple[str, str, Optional[str]]:
        """按历史 → 人工复核登记 → 队列的顺序查 MP 侧。"""
        with self._history_oper_context() as history_oper:
            success = history_oper.get_success_by_src(src_path, src_storage)
            if success is not None:
                dest = str(getattr(success, "dest", "") or "")
                return ("succeeded", dest, dest or None)
            record = history_oper.get_by_src(src_path, src_storage)
            if record is not None:
                status = getattr(record, "status", None)
                dest = str(getattr(record, "dest", "") or "")
                if status is False:
                    errmsg = str(getattr(record, "errmsg", "") or "")
                    detail = (
                        _truncate_detail(errmsg)
                        if errmsg.strip()
                        else "MP 整理失败（未记录原因）"
                    )
                    return ("failed", detail, dest or None)
                if status is True:
                    return ("succeeded", dest, dest or None)
        with self._pending_oper_context() as pending_oper:
            pending = pending_oper.get_by_identity(
                storage=src_storage, src_path=src_path
            )
            if pending is not None:
                execution_state = str(
                    getattr(pending, "execution_state", "") or ""
                ).strip()
                if execution_state in {"manual_review", "failed"}:
                    reason = str(getattr(pending, "last_error", "") or "")
                    if execution_state == "manual_review":
                        detail = (
                            _truncate_detail(reason)
                            if reason.strip()
                            else "MP 整理待人工复核"
                        )
                    else:
                        detail = (
                            _truncate_detail(reason)
                            if reason.strip()
                            else "MP 整理失败（未记录原因）"
                        )
                    return ("failed", detail, None)
        jobs = self._jobview_instance().list_jobs() or []
        for job in jobs:
            for task in getattr(job, "tasks", None) or []:
                fileitem = getattr(task, "fileitem", None)
                if fileitem is None:
                    continue
                if str(getattr(fileitem, "path", "") or "") != src_path:
                    continue
                storage = str(getattr(fileitem, "storage", "") or "")
                if storage and storage != src_storage:
                    continue
                state = str(getattr(task, "state", "") or "").strip()
                if state == "waiting":
                    return ("queued", "MP 整理排队中", None)
                if state == "failed":
                    return ("failed", "MP 整理失败（队列任务已失败）", None)
                if state == "manual_review":
                    return ("failed", "MP 整理待人工复核", None)
                # running / completed / 未知态：MP 手里还有这个任务，
                # 历史尚未结算，按进行中处理，等下一轮再看历史。
                return ("running", "MP 整理进行中", None)
        return ("missing", "MP 整理队列与历史中均无记录", None)
