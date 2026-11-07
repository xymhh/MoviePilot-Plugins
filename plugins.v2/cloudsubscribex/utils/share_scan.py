"""分享目录枚举适配（M2）：把各网盘的分享列举结果统一成 ``[{path, size}]``。

方案见 ``docs/plan/2026-11-07-分享识别重构-方案.md``：

* R1 递归到底：**不带** ``target_season``、不截断深度，先拿全量再过滤；
* 枚举失败/被限流不猜，交给识别层判 ``SCAN_FAILED``；
* 本模块只做列举与归一化，不做任何识别、不转存。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

#: 递归深度上限。够深即可，真正的兜底是「列到空为止」。
MAX_DEPTH = 32


def entry_path(item: Dict[str, Any]) -> str:
    """取条目在分享内的相对路径（``dir/sub/file.mkv``）。"""
    if not isinstance(item, dict):
        return ""
    for key in ("relative_path", "_relative_path"):
        value = str(item.get(key) or "").strip().strip("/")
        if value:
            return value
    name = str(item.get("name") or item.get("file_name") or "").strip()
    parent = str(item.get("parent_path") or "").strip().strip("/")
    if not name:
        return ""
    return f"{parent}/{name}" if parent else name


def normalize_entries(files: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把列举结果归一成识别层输入：只留 path / size / id / is_dir。

    路径缺失的条目直接丢弃——没有相对路径就无法分组，也拼不出转存目标。
    """
    entries: List[Dict[str, Any]] = []
    for item in files or []:
        path = entry_path(item)
        if not path:
            continue
        entries.append({
            "path": path,
            "size": int(item.get("size") or 0),
            "id": str(item.get("id") or item.get("file_id") or ""),
            "is_dir": bool(item.get("is_dir")),
        })
    return entries


def _list_once(share_service: Any, share_url: str, log_prefix: str) -> List[Dict[str, Any]]:
    """按「递归到底」的口径调一次列举；不支持深度的驱动会退回默认参数。"""
    kwargs: Dict[str, Any] = {}
    if log_prefix:
        kwargs["log_prefix"] = log_prefix
    try:
        files = share_service.list_share_files(share_url, max_depth=MAX_DEPTH, **kwargs)
    except TypeError:
        # 夸克等驱动签名是 ``list_share_files(share_url, **kwargs)``，
        # 深度参数无意义（它本来就全递归），去掉再试一次。
        files = share_service.list_share_files(share_url, **kwargs)
    return list(files or [])


def scan_share(
        share_service: Any,
        share_url: str,
        log_prefix: str = "",
) -> Dict[str, Any]:
    """枚举一个分享，返回 ``{"entries", "complete", "reason", "error"}``。

    ``complete=False`` 表示没拿到可信的全量（枚举失败或分享为空），
    调用方应据此判 ``SCAN_FAILED`` / ``NO_MEDIA``，不要拿半棵树去算覆盖。
    """
    result: Dict[str, Any] = {
        "entries": [],
        "complete": False,
        "reason": "",
        "error": "",
    }
    if share_service is None:
        result["reason"] = "NO_SERVICE"
        result["error"] = "没有可用的分享驱动"
        return result
    try:
        files = _list_once(share_service, share_url, log_prefix)
    except Exception as error:  # noqa: BLE001 - 任何异常都只能是「没扫成」
        result["reason"] = "SCAN_FAILED"
        result["error"] = f"{type(error).__name__}: {error}"
        return result
    entries = normalize_entries(files)
    result["entries"] = entries
    if not entries:
        result["reason"] = "EMPTY"
        return result
    result["complete"] = True
    return result


def summarize(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """给日志用的轻量统计：文件数、总体积、一级目录数。"""
    files = [e for e in entries or [] if not e.get("is_dir")]
    groups = {e["path"].split("/", 1)[0] for e in files if "/" in e["path"]}
    return {
        "files": len(files),
        "bytes": sum(int(e.get("size") or 0) for e in files),
        "top_dirs": len(groups),
    }


def optional_season(entries: Optional[List[Dict[str, Any]]]) -> Optional[int]:
    """从整棵树里取「唯一显式季号」；多个不同季号时返回 None（不猜）。"""
    from .file_parser import MediaFileParser

    seasons = set()
    for item in entries or []:
        if item.get("is_dir"):
            continue
        season = MediaFileParser.path_season(str(item.get("path") or ""))
        if season:
            seasons.add(int(season))
    return seasons.pop() if len(seasons) == 1 else None
