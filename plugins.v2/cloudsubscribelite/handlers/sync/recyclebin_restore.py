"""115 回收站一键恢复（v1.31.6）。

背景：2026-11-23 16:27 批量删除 45 组同视频重复后，Emby library.deleted
事件触发插件深度删除，误删 115 视频 48 个文件 + 32 个目录（32 部电影整个
文件夹 + 15 集剧集单文件；另有两京十五日 (2025) S01E07 在 16:46 单删后
16:48 又触发一次）。115 回收站 30 天内可还原，本服务把还原做进插件：
先列出事发窗口内的回收站记录给用户确认，再只还原确认过的 rid，逐项核对。

只做还原：本模块与 drive 层只调 recyclebin_list / recyclebin_info /
recyclebin_revert，绝不做永久删除/清空操作。

核对规则（接口返回成功 ≠ 文件已回来）：
* 还原后重查回收站：rid 已不在 = 已还原；rid 还在 = 失败。
* 原路径已知时再用 find_file 核对原位置已回来（path_verified True/False；
  路径未知时为 None，不计入失败——rid 消失已是还原生效的直接证据）。

字段口径：p115client 的 recyclebin_list 不在 docstring 里写明返回字段，
这里全部防御性取值（多候选键 + .get），字段缺失时该项降级——dtime 缺失
的条目无法证明落在事发窗口内，直接丢弃，不猜。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger

from ...core import OwnerDelegator

#: 事发窗口（Asia/Shanghai，留余量）：2026-11-23 16:27:32 至 16:49:27。
_CST = timezone(timedelta(hours=8))
_INCIDENT_WINDOW_START = int(
    datetime(2026, 11, 23, 16, 25, tzinfo=_CST).timestamp()
)
_INCIDENT_WINDOW_END = int(
    datetime(2026, 11, 23, 16, 55, tzinfo=_CST).timestamp()
)

#: 回收站分页大小与最多翻页数（防接口异常时无界拉取）。
_LIST_PAGE_SIZE = 100
_LIST_MAX_PAGES = 100


def _to_int(value: Any) -> Optional[int]:
    """宽松转 int：int / float / 数字字符串；转不出返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    return None


def _first_str(raw: Dict[str, Any], keys: Tuple[str, ...]) -> str:
    """按候选键顺序取第一个非空字符串。"""
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _split_original_path(original_path: str, name: str) -> Tuple[str, str]:
    """原路径拆成（父目录，文件名），供 find_file 核对用。

    原路径可能是完整路径（含文件名）或父目录：去掉末尾斜杠后，若主干
    就是文件名，父目录取 dirname；否则把原路径本身当父目录。
    """
    text = str(original_path or "").strip().rstrip("/")
    if not text or not name:
        return "", ""
    if PurePosixPath(text).name == name:
        parent = PurePosixPath(text).parent.as_posix()
        return (parent or "/"), name
    return text, name


def _parse_candidate(raw: Any) -> Optional[Dict[str, Any]]:
    """一条回收站记录 → 候选条目；dtime 读不到时返回 None（直接丢弃）。"""
    if not isinstance(raw, dict):
        return None
    rid = _first_str(raw, ("rid", "id", "tid"))
    if not rid:
        return None
    deleted_at = _to_int(
        raw.get("dtime", raw.get("delete_time", raw.get("deleted_at")))
    )
    if deleted_at is None:
        return None
    name = _first_str(raw, ("fn", "name", "n", "file_name"))
    size = _to_int(raw.get("s", raw.get("size")))
    is_dir = raw.get("is_dir", raw.get("is_directory"))
    if is_dir is None:
        # 115 常规列表里目录的 fid 为 "0"，回收站条目沿用同一口径时兜底。
        is_dir = str(raw.get("fid") or "") == "0"
    original_path = _first_str(
        raw, ("path", "parent_path", "original_path", "dir")
    )
    return {
        "rid": rid,
        "name": name,
        "size": size,
        "is_dir": bool(is_dir),
        "deleted_at": deleted_at,
        "original_path": original_path or None,
    }


class RecyclebinRestoreService(OwnerDelegator):
    """115 回收站恢复服务：按删除时间窗口列候选 → 只还原确认过的 rid。"""

    @staticmethod
    def _resolve_window(
            window_start: Any, window_end: Any,
    ) -> Tuple[int, int]:
        """窗口参数归一化：epoch 秒 / datetime / 数字字符串；非法回落默认窗口。"""
        start = _to_int(window_start)
        if start is None and isinstance(window_start, datetime):
            start = int(window_start.timestamp())
        end = _to_int(window_end)
        if end is None and isinstance(window_end, datetime):
            end = int(window_end.timestamp())
        if start is None:
            start = _INCIDENT_WINDOW_START
        if end is None:
            end = _INCIDENT_WINDOW_END
        if start > end:
            start, end = end, start
        return start, end

    def restore_list_candidates(
            self, window_start: Any = None, window_end: Any = None,
    ) -> Dict[str, Any]:
        """分页拉回收站，按 dtime 落在窗口内过滤，返回候选条目。

        倒序提前停：仅当观测到的 dtime 整体非递增、且本页最晚的删除时间
        已早于窗口下限时才停——顺序拿不准时宁可多翻页，不可漏条目。
        """
        start, end = self._resolve_window(window_start, window_end)
        query = getattr(self, "_cloud_query", None)
        if query is None:
            return {"success": False, "message": "网盘查询服务不可用"}
        list_fn = getattr(query, "list_recyclebin", None)
        if not callable(list_fn):
            return {"success": False, "message": "当前网盘不支持读取回收站"}
        candidates: List[Dict[str, Any]] = []
        scanned = 0
        offset = 0
        descending = True
        prev_page_min: Optional[int] = None
        for _page in range(_LIST_MAX_PAGES):
            try:
                items = list_fn(limit=_LIST_PAGE_SIZE, offset=offset)
            except Exception as error:  # noqa: BLE001 - 读失败整单失败，不给半份列表
                logger.warning(f"读取115回收站失败：{error}")
                return {"success": False, "message": f"读取回收站失败：{error}"}
            if not isinstance(items, list):
                items = []
            if not items:
                break
            scanned += len(items)
            page_dtimes: List[int] = []
            for raw in items:
                candidate = _parse_candidate(raw)
                if candidate is None:
                    continue
                page_dtimes.append(candidate["deleted_at"])
                if start <= candidate["deleted_at"] <= end:
                    candidates.append(candidate)
            if page_dtimes:
                if prev_page_min is not None and max(page_dtimes) > prev_page_min:
                    descending = False
                prev_page_min = min(page_dtimes)
                if descending and max(page_dtimes) < start:
                    break
            if len(items) < _LIST_PAGE_SIZE:
                break
            offset += len(items)
        candidates.sort(key=lambda item: item["deleted_at"], reverse=True)
        logger.info(
            "回收站候选列出：窗口内 %s 条（共扫 %s 条）",
            len(candidates), scanned,
        )
        return {
            "success": True,
            "data": {
                "candidates": candidates,
                "window_start": start,
                "window_end": end,
                "scanned": scanned,
            },
        }

    def _resolve_original_path(
            self, query: Any, rid: str, name: str,
    ) -> Optional[str]:
        """还原前尽量拿到原路径（recyclebin_info 详情接口，尽力而为）。"""
        info_fn = getattr(query, "recyclebin_info", None) if query is not None else None
        if not callable(info_fn):
            return None
        try:
            info = info_fn(rid)
        except Exception as error:  # noqa: BLE001 - 详情取不到只跳过路径核对
            logger.debug(f"回收站记录详情取不到：rid={rid}，{error}")
            return None
        if not isinstance(info, dict):
            return None
        path = _first_str(
            info, ("path", "parent_path", "original_path", "dir"),
        )
        if not path:
            return None
        parent, _name = _split_original_path(path, name)
        if not parent:
            return None
        return path

    def restore_revert(
            self, items: Any, window_start: Any = None, window_end: Any = None,
    ) -> Dict[str, Any]:
        """只还原调用方确认过的 rid，逐项核对（重查回收站 + 原路径复查）。

        items：rid 字符串列表，或 [{rid, name, original_path?}]；不接受
        “全部还原”这种无清单操作。window_* 与列候选时同一窗口，用于
        还原后重查——重查不到该 rid 即已还原。
        """
        normalized: List[Dict[str, Any]] = []
        seen = set()
        for entry in items or []:
            if isinstance(entry, dict):
                rid = str(entry.get("rid") or "").strip()
                name = str(entry.get("name") or "").strip()
                original_path = str(entry.get("original_path") or "").strip() or None
            else:
                rid = str(entry or "").strip()
                name = ""
                original_path = None
            if not rid or rid in seen:
                continue
            seen.add(rid)
            normalized.append({
                "rid": rid, "name": name, "original_path": original_path,
            })
        if not normalized:
            return {"success": False, "message": "未指定要还原的回收站记录"}
        mutations = getattr(self, "_cloud_mutations", None)
        revert_fn = getattr(mutations, "revert_recyclebin", None) if mutations is not None else None
        if not callable(revert_fn):
            return {"success": False, "message": "当前网盘不支持还原回收站"}
        query = getattr(self, "_cloud_query", None)
        for item in normalized:
            if item["original_path"]:
                continue
            item["original_path"] = self._resolve_original_path(
                query, item["rid"], item["name"],
            )
        rids = [item["rid"] for item in normalized]
        call_error = ""
        try:
            # 115 要求还原操作不并发：一次提交全部 rid，顺序执行。
            revert_fn(rids)
        except Exception as error:  # noqa: BLE001 - 接口异常也继续核对（可能部分已还原）
            call_error = str(error)
            logger.warning(f"还原115回收站接口异常：{len(rids)} 条，{error}")
        start, end = self._resolve_window(window_start, window_end)
        verify = self.restore_list_candidates(start, end)
        if not verify.get("success"):
            failed = [
                {
                    "rid": item["rid"], "name": item["name"],
                    "reason": f"还原后无法重查回收站核对：{verify.get('message')}",
                }
                for item in normalized
            ]
            return {
                "success": False,
                "message": (
                    f"还原完成：还原 0 条，{len(failed)} 条失败"
                    f"（{verify.get('message')}）"
                ),
                "data": {"restored": [], "failed": failed},
            }
        still_present = {
            str(candidate.get("rid") or "")
            for candidate in (verify.get("data") or {}).get("candidates") or []
        }
        restored: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        find_fn = getattr(query, "find_file", None) if query is not None else None
        for item in normalized:
            if item["rid"] in still_present:
                failed.append({
                    "rid": item["rid"], "name": item["name"],
                    "reason": "还原后该记录仍在回收站",
                })
                continue
            path_verified: Optional[bool] = None
            if item["original_path"]:
                parent, file_name = _split_original_path(
                    item["original_path"], item["name"],
                )
                if parent and file_name and callable(find_fn):
                    try:
                        path_verified = find_fn(parent, file_name) is not None
                    except Exception as error:  # noqa: BLE001 - 复查异常按未核对计
                        logger.debug(
                            f"还原后原路径复查异常：{parent}/{file_name}，{error}",
                        )
                        path_verified = None
            restored.append({
                "rid": item["rid"], "name": item["name"],
                "path_verified": path_verified,
            })
        message = (
            f"还原完成：还原 {len(restored)} 条"
            + (f"，{len(failed)} 条失败" if failed else "")
            + (f"；还原接口异常：{call_error}" if call_error else "")
        )
        logger.warning(
            "115回收站还原完成：还原 %s 条，失败 %s 条",
            len(restored), len(failed),
        )
        return {
            "success": not failed,
            "message": message,
            "data": {"restored": restored, "failed": failed},
        }
