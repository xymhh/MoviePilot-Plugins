"""同步作业的稳定身份与状态迁移规则。

本模块只做纯计算，不依赖数据库、MoviePilot 或网盘客户端，方便在单测里
把「同一个文件为什么会被重复转存」钉成可回归的契约。

设计来源：``docs/plan/2026-10-20-同步作业状态机与幂等改造.md``。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping as MappingABC
from typing import Any, Final

JOB_QUEUED: Final = "queued"
JOB_TRANSFERRING: Final = "transferring"
JOB_VERIFYING: Final = "verifying"
JOB_FINALIZING: Final = "finalizing"
JOB_RETRY_WAIT: Final = "retry_wait"
JOB_DONE: Final = "done"
JOB_FAILED: Final = "failed"
JOB_NEEDS_ATTENTION: Final = "needs_attention"
JOB_CANCELED: Final = "canceled"

JOB_STATES: Final = frozenset({
    JOB_QUEUED,
    JOB_TRANSFERRING,
    JOB_VERIFYING,
    JOB_FINALIZING,
    JOB_RETRY_WAIT,
    JOB_DONE,
    JOB_FAILED,
    JOB_NEEDS_ATTENTION,
    JOB_CANCELED,
})

TERMINAL_STATES: Final = frozenset({
    JOB_DONE,
    JOB_FAILED,
    JOB_NEEDS_ATTENTION,
    JOB_CANCELED,
})

# 只允许这些迁移。新增边必须有明确语义，不能为了「少改代码」随便放行。
ALLOWED_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    JOB_QUEUED: frozenset({JOB_TRANSFERRING, JOB_CANCELED}),
    JOB_TRANSFERRING: frozenset({
        JOB_VERIFYING, JOB_RETRY_WAIT, JOB_FAILED,
        JOB_NEEDS_ATTENTION, JOB_CANCELED,
    }),
    JOB_VERIFYING: frozenset({
        JOB_FINALIZING, JOB_RETRY_WAIT, JOB_NEEDS_ATTENTION, JOB_CANCELED,
    }),
    JOB_FINALIZING: frozenset({
        JOB_DONE, JOB_RETRY_WAIT, JOB_NEEDS_ATTENTION, JOB_CANCELED,
    }),
    JOB_RETRY_WAIT: frozenset({JOB_QUEUED, JOB_FAILED, JOB_CANCELED}),
    # done 不是数学意义上的终态：目标文件可能被人删除/移走，允许回查后
    # 重新入队；目标同名但内容不一致时转待处理，绝不自动覆盖。
    JOB_DONE: frozenset({JOB_QUEUED, JOB_NEEDS_ATTENTION}),
    JOB_FAILED: frozenset(),
    JOB_NEEDS_ATTENTION: frozenset(),
    # canceled 只是「用户停掉了这一次」，不是「这文件永远不要」：
    # 用户之后重新发起同步时，同一 job_key 必须能重新入队，否则该内容会被
    # 永久跳过（1.7.37 修复：金银岛 S01E02 转存被 canceled 记录挡住）。
    JOB_CANCELED: frozenset({JOB_QUEUED}),
}

_HASH_PATTERNS: Final = {
    "sha1": re.compile(r"^[0-9a-f]{40}$"),
    "md5": re.compile(r"^[0-9a-f]{32}$"),
}


def normalize_path(path: Any) -> str:
    """把云盘目录规范成稳定的绝对 POSIX 路径。

    只做身份需要的归一化：NFC、去首尾空白、折叠重复斜杠、去尾部斜杠。
    不解析 ``..``，也不改大小写；真正的路径合法性由网盘适配器负责。
    """
    text = unicodedata.normalize("NFC", str(path or "/")).strip()
    if not text:
        return "/"
    text = re.sub(r"/+", "/", text)
    if not text.startswith("/"):
        text = "/" + text
    if text != "/":
        text = text.rstrip("/")
    return text or "/"


def normalize_name(name: Any) -> str:
    """规范化目标文件名；只接受单层文件名，拒绝路径分隔符和空值。"""
    text = unicodedata.normalize("NFC", str(name or "")).strip()
    if not text:
        raise ValueError("目标文件名不能为空")
    if text in {".", ".."}:
        raise ValueError(f"非法目标文件名：{text!r}")
    if "/" in text or "\\" in text or "\x00" in text:
        raise ValueError(f"目标文件名不能包含路径分隔符：{text!r}")
    return text


def _read(source: Any, key: str, default: Any = "") -> Any:
    if isinstance(source, MappingABC):
        return source.get(key, default)
    return getattr(source, key, default)


def _clean_hash(value: Any, algorithm: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    pattern = _HASH_PATTERNS[algorithm]
    return text if pattern.fullmatch(text) else ""


def content_key(source: Any, provider: str = "") -> str:
    """按「SHA1 → MD5 → 文件 ID+大小」生成内容身份。

    哈希格式不合法时继续降级，避免把短哈希/脏值当成稳定身份。
    """
    sha1 = _clean_hash(_read(source, "sha1"), "sha1")
    if sha1:
        return f"sha1:{sha1}"

    md5 = _clean_hash(_read(source, "md5"), "md5")
    if md5:
        return f"md5:{md5}"

    source_provider = str(
        provider or _read(source, "provider") or _read(source, "source_provider") or ""
    ).strip().lower()
    file_id = str(
        _read(source, "file_id") or _read(source, "id") or ""
    ).strip()
    if not source_provider:
        raise ValueError("缺少源盘标识，无法生成内容身份")
    if not file_id:
        raise ValueError("缺少源文件 ID，且没有可用哈希")

    size = _read(source, "size", 0)
    try:
        normalized_size = max(0, int(size or 0))
    except (TypeError, ValueError):
        normalized_size = 0
    return f"id:{source_provider}:{file_id}:{normalized_size}"


def build_job_key(
        target_provider: Any,
        target_path: Any,
        target_name: Any,
        source: Any,
        source_provider: str = "",
) -> str:
    """生成「同一内容 → 同一目标位置」的幂等键。

    注意：同名不同内容会得到不同键；目标位置的冲突由 TargetWriter 查同名
    文件后决策，不能只靠键去重。
    """
    provider = str(target_provider or "").strip().lower()
    if not provider:
        raise ValueError("缺少目标盘标识")
    fingerprint = "\n".join((
        provider,
        normalize_path(target_path),
        normalize_name(target_name),
        content_key(source, provider=source_provider),
    ))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def can_transition(from_state: str, to_state: str) -> bool:
    """判断状态迁移是否被允许；未知状态一律拒绝。"""
    return to_state in ALLOWED_TRANSITIONS.get(str(from_state or ""), frozenset())


def assert_transition(from_state: str, to_state: str) -> None:
    """迁移非法时抛出 ``ValueError``，避免业务层静默写坏状态。"""
    if from_state not in JOB_STATES:
        raise ValueError(f"未知作业状态：{from_state!r}")
    if to_state not in JOB_STATES:
        raise ValueError(f"未知作业状态：{to_state!r}")
    if not can_transition(from_state, to_state):
        raise ValueError(f"非法作业状态迁移：{from_state} -> {to_state}")


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES
