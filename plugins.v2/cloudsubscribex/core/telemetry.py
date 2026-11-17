"""115 API 调用计量（1.12.17 Phase 0-9，风控面验收的观测手段）。

只做计数，不做限流、不改任何行为。计数器是进程内全局单例，线程安全；
插件重载后归零（接受：计量只用于“最近一段时间的调用量”判断，长期
趋势靠日志里的累计快照）。

键名约定（扁平字符串，日志里直接可读）：
- ``control:<op>``：经 ``P115ClientManager._rate_limited_call`` 的
  115 控制面调用，op 是被调函数名（如 upload_file_init、fs_file）。
- ``oss2:part``：oss2 自管分片每次 ``upload_part`` 成功，附字节数。
- ``p115oss:file``：p115oss 回落每次整文件上传，附字节数（其内部分片
  在外部库里，看不到）。
- ``rapid:hit`` / ``rapid:miss``：秒传探测命中 / 未命中（正式上传前
  的 try_rapid_upload 探针与上传内 init 的 reuse 都记在这里）。
- ``upload:success`` / ``upload:failure``：upload_file 终态，附字节数。

注意：MP 整理触发的上传发生在 MP 链里、插件 finalize 窗口之外，
这里记的是插件进程内的累计值；按集归因靠第 10 项的阶段耗时日志里
带的累计快照差分（顺序处理时相邻两集的快照差就是该集的成本）。
"""
from __future__ import annotations

import threading
from typing import Dict


class ApiCallMeter:
    """线程安全的扁平计数器：key -> {"calls": n, "bytes": n}。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Dict[str, Dict[str, int]] = {}

    def record(self, key: str, *, calls: int = 1, bytes_: int = 0) -> None:
        with self._lock:
            entry = self._counters.setdefault(key, {"calls": 0, "bytes": 0})
            entry["calls"] += calls
            entry["bytes"] += int(bytes_ or 0)

    def snapshot(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {key: dict(value) for key, value in self._counters.items()}

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()


def format_snapshot(snapshot: Dict[str, Dict[str, int]]) -> str:
    """累计快照压成一行日志可读的文本：``oss2:part=48次/512.0MB``。"""
    parts = []
    for key in sorted(snapshot):
        entry = snapshot[key]
        text = f"{key}={entry['calls']}次"
        if entry["bytes"]:
            text += f"/{entry['bytes'] / 1048576:.1f}MB"
        parts.append(text)
    return ",".join(parts) if parts else "无"


api_meter = ApiCallMeter()
