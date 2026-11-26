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



def _refresh_upload_entry(name: str) -> bool:
    """只刷新已存在条目的 updated_at（绝不创建条目）。

    速度按 report_upload_progress 的口径重算：uploaded 不变、
    elapsed 增长，上传停滞时速度自然衰减到 0。返回是否刷新过。
    """
    try:
        now = time.monotonic()
        with _LOCK:
            entry = _ENTRIES.get(name)
            if entry is None:
                return False
            entry["updated_at"] = now
            baseline = _BASELINES.get(name)
            if baseline is not None:
                elapsed = now - baseline["first_at"]
                if elapsed > 0:
                    uploaded_now = float(entry.get("uploaded") or 0)
                    entry["speed"] = max(
                        0.0,
                        (uploaded_now - baseline["first_uploaded"]) / elapsed,
                    )
                else:
                    entry["speed"] = 0.0
            return True
    except Exception:
        return False
