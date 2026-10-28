"""通知体积校正：按历史台账逐集累加真实写入大小。

转存链路有多条，个别链路拿到的 ``size`` 只覆盖其中一集（线上 13 集洗版
通知只报 2.87GB，实为单集体积，真实写入 46.75GB）。历史台账里 ``file_size``
是落盘后写入的大小，按集数累加即可得到这批文件的真实体积。
"""

from typing import Any, Dict, Iterable

SUCCESS_STATUS = "成功"


def _episodes(detail: Dict[str, Any]) -> set:
    episodes = set()
    for episode in detail.get("episodes") or []:
        try:
            value = int(episode)
        except (TypeError, ValueError):
            continue
        if value > 0:
            episodes.add(value)
    return episodes


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def history_episode_size(
        detail: Dict[str, Any], records: Iterable[Dict[str, Any]]
) -> int:
    """按集数累加同季成功记录的 ``file_size``，失败记录不计入。

    每集取最新一条（``time`` 最大），避免洗版前后的旧记录把体积算小或算重。
    """
    episodes = _episodes(detail)
    if not episodes:
        return 0
    title = str(detail.get("title") or "").strip()
    season = _int(detail.get("season"))
    latest: Dict[int, Dict[str, Any]] = {}
    for record in records or []:
        if str(record.get("status") or "") != SUCCESS_STATUS:
            continue
        if str(record.get("title") or "").strip() != title:
            continue
        if _int(record.get("season")) != season:
            continue
        episode = _int(record.get("episode"))
        if episode not in episodes:
            continue
        if _int(record.get("file_size")) <= 0:
            continue
        previous = latest.get(episode)
        if previous is None or str(record.get("time") or "") >= str(
                previous.get("time") or ""
        ):
            latest[episode] = record
    return sum(_int(record.get("file_size")) for record in latest.values())


def apply_history_size(
        detail: Dict[str, Any], records: Iterable[Dict[str, Any]]
) -> int:
    """用历史台账校正详情体积，只在明显偏小时覆盖；返回校正后的大小。"""
    size = _int(detail.get("size"))
    record_size = history_episode_size(detail, list(records or []))
    if record_size > size:
        detail["size"] = record_size
        return record_size
    return size
