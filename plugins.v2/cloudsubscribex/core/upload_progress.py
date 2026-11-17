"""115 上传实时进度登记表（进程内）。

MP 整理队列的进度是按文件数算的，单文件整理全程 0%；而上传本身
永远走插件的 ``U115CSStorage.upload``，回调把字节进度记到这里，
下载管理页的「网盘订阅助手」标签再把它投影成任务卡，就能看到
真实百分比、速度与剩余时间。

纯标准库，不 import 插件其他模块，避免循环导入。所有函数永不抛
异常到调用方：内部 try/except 保护，失败时静默——进度展示绝不能
影响上传本身。

登记表 key 是上传的本地文件名（storage.upload 传 local_path.name）。
速度是“自首次记录以来的平均速度”；uploaded 比上次小（整文件重传）
时重置速度基线。
"""

import threading
import time
from typing import Any, Dict, List

_LOCK = threading.Lock()
_ENTRIES: Dict[str, Dict[str, Any]] = {}
# key -> {"first_uploaded": int, "first_at": float}，速度基线。
_BASELINES: Dict[str, Dict[str, float]] = {}


def report_upload_progress(
    key: Any, file_name: Any, uploaded: Any, total: Any
) -> None:
    """记录一次上传进度；任何异常都静默吞掉。"""
    try:
        name = str(key or "").strip()
        if not name:
            return
        try:
            uploaded_value = max(0, int(uploaded or 0))
        except (TypeError, ValueError):
            uploaded_value = 0
        try:
            total_value = max(0, int(total or 0))
        except (TypeError, ValueError):
            total_value = 0
        now = time.monotonic()
        with _LOCK:
            previous = _ENTRIES.get(name)
            baseline = _BASELINES.get(name)
            if (
                previous is None
                or baseline is None
                or uploaded_value < int(previous.get("uploaded") or 0)
            ):
                # 首次记录，或整文件重传（uploaded 回落）：重置速度基线。
                baseline = {
                    "first_uploaded": float(uploaded_value),
                    "first_at": now,
                }
                _BASELINES[name] = baseline
            elapsed = now - baseline["first_at"]
            if elapsed > 0:
                speed = max(
                    0.0,
                    (uploaded_value - baseline["first_uploaded"]) / elapsed,
                )
            else:
                speed = 0.0
            _ENTRIES[name] = {
                "key": name,
                "file_name": str(file_name or ""),
                "uploaded": uploaded_value,
                "total": total_value,
                "speed": speed,
                "updated_at": now,
            }
    except Exception:
        return


def remove_upload_progress(key: Any) -> None:
    """删除一条上传进度记录（上传完成时调用，卡片随之消失）。"""
    try:
        name = str(key or "").strip()
        if not name:
            return
        with _LOCK:
            _ENTRIES.pop(name, None)
            _BASELINES.pop(name, None)
    except Exception:
        return


def snapshot_upload_progress(max_age_seconds: float = 30.0) -> List[Dict[str, Any]]:
    """返回未过期的进度条目列表，并清理过期条目。

    过期按 updated_at 与 time.monotonic() 的差算；max_age_seconds<=0
    时只保留“刚刚”记录的条目（基本等于全清，仅测试用）。
    """
    try:
        try:
            max_age = float(max_age_seconds)
        except (TypeError, ValueError):
            max_age = 30.0
        now = time.monotonic()
        with _LOCK:
            expired = [
                name
                for name, entry in _ENTRIES.items()
                if now - float(entry.get("updated_at") or 0) > max_age
            ]
            for name in expired:
                _ENTRIES.pop(name, None)
                _BASELINES.pop(name, None)
            return [dict(entry) for entry in _ENTRIES.values()]
    except Exception:
        return []


def clear_upload_progress() -> None:
    """清空登记表（测试与插件重载用）。"""
    try:
        with _LOCK:
            _ENTRIES.clear()
            _BASELINES.clear()
    except Exception:
        return
