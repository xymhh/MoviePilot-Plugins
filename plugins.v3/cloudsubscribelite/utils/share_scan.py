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



def recognition_mode(
        work_id: Optional[int] = None,
        season: Optional[int] = None,
        explicit_selection: bool = False,
) -> str:
    """要不要用识别计划接管这次转存。

    ``off``：用户手选了文件（人工优先）、没有 TMDB 身份、或不知道季号（电影/无季
    分享）——维持现有管线，识别层不介入；
    ``plan``：剧集且有身份与季号，按计划收敛文件。
    """
    if explicit_selection:
        return "off"
    if not work_id or not season:
        return "off"
    return "plan"


def plan_action(plan: Dict[str, Any]) -> str:
    """计划允许的动作：``block``（不转）/ ``apply``（只转 selected）。"""
    if str((plan or {}).get("status") or "") in {"skip", "review"}:
        return "block"
    return "apply"


def filter_by_plan(
        files: Iterable[Dict[str, Any]],
        plan: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """按计划的 ``selected`` 过滤原始文件列表（保留原条目，供转存用）。"""
    selected = {str(path) for path in (plan or {}).get("selected") or []}
    if not selected:
        return []
    return [item for item in files or [] if entry_path(item) in selected]


def summarize(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """给日志用的轻量统计：文件数、总体积、一级目录数。"""
    files = [e for e in entries or [] if not e.get("is_dir")]
    groups = {e["path"].split("/", 1)[0] for e in files if "/" in e["path"]}
    return {
        "files": len(files),
        "bytes": sum(int(e.get("size") or 0) for e in files),
        "top_dirs": len(groups),
    }
