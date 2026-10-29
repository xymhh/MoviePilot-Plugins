"""转存失败原因分类。

状态枚举（成功 / 失败 / 跳过 …）保持不变，失败再按「原因」这一正交维度
打标：``failure_kind`` 说明为什么失败，``failure_retryable`` 说明重试还能
不能救。分享链接失效、分享内找不到目标文件属于永久性失败——同样的链接
再点一百次重试也不会成功，必须换源；网络超时、风控限流、目标空间不足等
属于暂时性失败，重试有意义。

老记录只有自由文本 ``failure_reason``，读的时候按文本回溯推断，不需要
数据迁移。
"""

from typing import Any, Dict, Iterable, Mapping, Tuple

#: 分享链接失效 / 已取消 / 已删除 / 提取码错误。
KIND_SHARE_INVALID = "share_invalid"
#: 分享有效，但里面找不到历史记录对应的源文件。
KIND_FILE_MISSING = "file_missing"
#: 115 秒传未命中，且当前策略不允许兜底上传。
KIND_CHECKSUM_MISS = "checksum_miss"
#: 账号风控 / 请求过于频繁。
KIND_RISK_CONTROL = "risk_control"
#: 目标网盘空间不足。
KIND_TARGET_SPACE = "target_space"
#: 网络超时、连接失败。
KIND_NETWORK = "network"
#: 提交离线下载失败。
KIND_OFFLINE_SUBMIT = "offline_submit"
#: 后处理（STRM / 整理 / 登记）失败。
KIND_POSTPROCESS = "postprocess"
#: 上传到目标网盘失败。
KIND_UPLOAD = "upload"
#: 未能归类。
KIND_UNKNOWN = "unknown"

#: 永久性失败：同一条链接重试不可能成功，只能换源或删除记录。
PERMANENT_KINDS = frozenset({KIND_SHARE_INVALID, KIND_FILE_MISSING})

#: 类型 -> 中文展示名（历史页 tooltip、日志统一口径）。
KIND_LABELS: Dict[str, str] = {
    KIND_SHARE_INVALID: "分享链接失效",
    KIND_FILE_MISSING: "分享内无目标文件",
    KIND_CHECKSUM_MISS: "秒传未命中",
    KIND_RISK_CONTROL: "网盘风控",
    KIND_TARGET_SPACE: "目标网盘空间不足",
    KIND_NETWORK: "网络异常",
    KIND_OFFLINE_SUBMIT: "离线下载失败",
    KIND_POSTPROCESS: "后处理失败",
    KIND_UPLOAD: "上传失败",
    KIND_UNKNOWN: "未分类失败",
}

#: 文本 -> 类型的关键词表。**顺序即优先级**：越靠前越具体，
#: 例如「分享链接无效：已过期」必须先命中 share_invalid，而不是 network。
_KEYWORD_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (KIND_SHARE_INVALID, (
        "分享链接无效", "分享链接失效", "分享已失效", "分享失效",
        "分享已取消", "分享取消", "已取消分享", "取消分享",
        "分享已删除", "分享被删除", "分享不存在", "分享不可用",
        "分享已过期", "链接已过期", "提取码错误", "提取码不正确",
        "文件已删除", "已失效", "已过期", "share not found",
    )),
    (KIND_FILE_MISSING, (
        "未找到历史记录对应的源文件", "未找到对应的源文件",
        "分享中未找到", "分享内未找到", "分享文件不存在",
    )),
    (KIND_CHECKSUM_MISS, (
        "秒传未命中", "快速上传未命中", "秒传失败", "未命中",
    )),
    (KIND_RISK_CONTROL, (
        "风控", "请求过于频繁", "操作太频繁", "频率限制", "限流",
        "账号异常",
    )),
    (KIND_TARGET_SPACE, (
        "空间不足", "容量不足", "空间已满", "存储空间",
    )),
    (KIND_OFFLINE_SUBMIT, (
        "提交离线下载失败", "离线下载失败", "离线任务",
    )),
    (KIND_POSTPROCESS, (
        "后处理", "登记失败", "strm", "整理失败",
    )),
    (KIND_UPLOAD, (
        "上传失败", "恢复上传", "上传中断",
    )),
    (KIND_NETWORK, (
        "超时", "timeout", "连接失败", "connection", "网络", "ssl",
    )),
)


def classify_failure(reason: Any) -> str:
    """把失败原因文本归纳成类型；无法归类返回 ``unknown``。"""
    text = str(reason or "").strip()
    if not text:
        return KIND_UNKNOWN
    lowered = text.casefold()
    for kind, keywords in _KEYWORD_RULES:
        for keyword in keywords:
            if keyword.casefold() in lowered:
                return kind
    return KIND_UNKNOWN


def is_permanent(kind: Any) -> bool:
    """该类型失败是否永久性（重试不可能成功）。"""
    return str(kind or "").strip() in PERMANENT_KINDS


def failure_label(kind: Any) -> str:
    """类型的中文展示名，未知类型返回「未分类失败」。"""
    key = str(kind or "").strip()
    return KIND_LABELS.get(key) or KIND_LABELS[KIND_UNKNOWN]


def failure_kind_of(record: Mapping[str, Any]) -> str:
    """读取记录的分类；老记录缺字段时按 ``failure_reason`` 回溯推断。"""
    if not isinstance(record, Mapping):
        return KIND_UNKNOWN
    explicit = str(record.get("failure_kind") or "").strip()
    if explicit:
        return explicit
    return classify_failure(record.get("failure_reason") or record.get("error"))


def failure_retryable_of(record: Mapping[str, Any]) -> bool:
    """读取记录的重试可行性；显式字段优先，缺省按分类推断。"""
    if not isinstance(record, Mapping):
        return True
    explicit = record.get("failure_retryable")
    if isinstance(explicit, bool):
        return explicit
    return not is_permanent(failure_kind_of(record))


def apply_failure(
        record: Dict[str, Any],
        reason: Any,
        kind: str = "",
) -> Dict[str, Any]:
    """按统一口径把失败写进历史记录。

    ``status`` 固定为「失败」（调用方可覆盖），同时写 ``failure_reason``、
    ``failure_kind``、``failure_retryable``。``kind`` 为空时按原因文本自动
    分类，调用方明确知道原因时（例如分享校验直接返回失效）建议显式传入。
    """
    if not isinstance(record, dict):
        return record
    text = str(reason or "").strip()
    resolved = str(kind or "").strip() or classify_failure(text)
    record["status"] = "失败"
    record["failure_reason"] = text or failure_label(resolved)
    record["failure_kind"] = resolved
    record["failure_retryable"] = not is_permanent(resolved)
    return record


def clear_failure(record: Dict[str, Any]) -> Dict[str, Any]:
    """清空失败痕迹（重试提交、转成功时调用），避免旧分类残留。"""
    if not isinstance(record, dict):
        return record
    for key in ("failure_reason", "failure_kind", "failure_retryable"):
        record.pop(key, None)
    return record


def kinds_of(records: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    """统计一组记录的失败类型分布（供调试与后续看板使用）。"""
    counts: Dict[str, int] = {}
    for record in records or ():
        if str(record.get("status") or "") != "失败":
            continue
        kind = failure_kind_of(record)
        counts[kind] = counts.get(kind, 0) + 1
    return counts
