"""一次同步运行内被跳过的候选资源统计（线程安全、可去重）。"""

import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 去重键：同一次运行内 (kind, provider, reference) 只记一条。
SkipRecordKey = Tuple[str, str, str]


def _as_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class SkipStatsCollector:
    """一次同步运行内被跳过的候选资源统计（线程安全、可去重）。"""

    KIND_INVALID = "invalid"   # 分享链接失效
    KIND_EMPTY = "empty"       # 分享链接无可用文件
    KIND_MISMATCH = "mismatch" # 内容与目标季/集不匹配

    #: 类型 -> 中文文案（与候选摘要同一套中文命名）。
    KIND_LABELS: Dict[str, str] = {
        KIND_INVALID: "分享链接失效",
        KIND_EMPTY: "无可用文件",
        KIND_MISMATCH: "内容不匹配",
    }
    #: 汇总文案里的类型顺序：先失效、后无可用文件、再内容不匹配。
    KIND_ORDER: Tuple[str, ...] = (KIND_INVALID, KIND_EMPTY, KIND_MISMATCH)
    #: 识别不出来源网盘时的兜底展示名。
    UNKNOWN_PROVIDER = "未知来源"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: Dict[SkipRecordKey, Dict[str, str]] = {}

    @classmethod
    def kind_label(cls, kind: str) -> str:
        """类型的中文展示名，未知类型原样返回。"""
        key = str(kind or "").strip()
        return cls.KIND_LABELS.get(key) or key

    def record(
            self,
            kind: str,
            provider: str,
            reason: str = "",
            reference: str = "",
    ) -> None:
        """记录一条被跳过的候选资源；同 (kind, provider, reference) 只记一次。"""
        normalized_kind = str(kind or "").strip()
        if not normalized_kind:
            return
        key: SkipRecordKey = (
            normalized_kind,
            str(provider or "").strip() or self.UNKNOWN_PROVIDER,
            str(reference or "").strip(),
        )
        with self._lock:
            if key in self._records:
                return
            self._records[key] = {
                "kind": key[0],
                "provider": key[1],
                "reason": str(reason or "").strip(),
                "reference": key[2],
            }

    def reset(self) -> None:
        """清空统计（一次运行开始时调用，避免两次运行串味）。"""
        with self._lock:
            self._records.clear()

    def take(self) -> Dict[str, Any]:
        """返回本次运行的汇总快照并清零。"""
        with self._lock:
            records = list(self._records.values())
            self._records.clear()
        return self._summarize(records)

    def snapshot(self) -> Dict[str, Any]:
        """返回当前汇总快照（不清零）。"""
        with self._lock:
            records = list(self._records.values())
        return self._summarize(records)

    def is_empty(self) -> bool:
        with self._lock:
            return not self._records

    def summary_text(self) -> str:
        """完成日志的跳过段落；空统计返回 ``""``（日志与现状逐字一致）。"""
        return self.format_summary(self.snapshot())

    def notify_text(self) -> str:
        """完成通知的追加行；空统计返回 ``""``（通知与现状逐字一致）。"""
        return self.format_notify(self.snapshot())

    @staticmethod
    def _summarize(records: Iterable[Dict[str, str]]) -> Dict[str, Any]:
        items = list(records)
        by_kind: Dict[str, int] = {}
        by_provider: Dict[str, int] = {}
        by_kind_provider: Dict[str, Dict[str, int]] = {}
        for record in items:
            kind = record["kind"]
            provider = record["provider"]
            by_kind[kind] = by_kind.get(kind, 0) + 1
            by_provider[provider] = by_provider.get(provider, 0) + 1
            kind_providers = by_kind_provider.setdefault(kind, {})
            kind_providers[provider] = kind_providers.get(provider, 0) + 1
        return {
            "total": len(items),
            "by_kind": by_kind,
            "by_provider": by_provider,
            "by_kind_provider": by_kind_provider,
        }

    @classmethod
    def _ordered_kinds(cls, by_kind: Dict[str, int]) -> List[str]:
        ordered = [kind for kind in cls.KIND_ORDER if kind in by_kind]
        ordered.extend(kind for kind in by_kind if kind not in cls.KIND_ORDER)
        return ordered

    @classmethod
    def format_summary(cls, snapshot: Optional[Dict[str, Any]]) -> str:
        """生成完成日志的跳过汇总段落；空统计返回 ``""``。"""
        data = snapshot or {}
        total = _as_count(data.get("total"))
        by_kind = data.get("by_kind") or {}
        by_provider = data.get("by_provider") or {}
        if total <= 0 or not by_kind:
            return ""
        kind_text = "、".join(
            f"{cls.kind_label(kind)} {_as_count(by_kind.get(kind))}"
            for kind in cls._ordered_kinds(by_kind)
        )
        provider_text = "、".join(
            f"{provider} {_as_count(count)}"
            for provider, count in by_provider.items()
        )
        return f"本次跳过 {total} 个候选资源：{kind_text}（{provider_text}）"

    @classmethod
    def format_notify(cls, snapshot: Optional[Dict[str, Any]]) -> str:
        """生成完成通知的追加行；空统计返回 ``""``。"""
        data = snapshot or {}
        total = _as_count(data.get("total"))
        by_kind = data.get("by_kind") or {}
        by_kind_provider = data.get("by_kind_provider") or {}
        if total <= 0 or not by_kind:
            return ""
        parts = []
        ordered_kinds = cls._ordered_kinds(by_kind)
        for kind in ordered_kinds:
            label = cls.kind_label(kind)
            segment = (
                label
                if len(ordered_kinds) == 1
                else f"{label} {_as_count(by_kind.get(kind))} 个"
            )
            providers = list((by_kind_provider.get(kind) or {}).keys())
            if providers:
                segment = f"{segment}（{'、'.join(providers)}）"
            parts.append(segment)
        return f"跳过 {total} 个候选 · {'；'.join(parts)}"
