"""候选提交的统一入口（频道监控并入搜索渠道 · P2a）。

背景：频道监控命中订阅后直接调 ``SyncApi.api_vue_start_manual_sync``，搜索链路则
走「候选 → 判定 → 转存」。两条入口各自组装 payload，导致同一件事有两套参数、
两套日志，也无法回答「这个候选是谁发现的、后来落地了没有」。

本模块只做**一件事**：把「推送型渠道发现的候选」收敛到一个函数
``submit_candidate()``——统一清洗链接、统一带 ``via``、统一写候选账本，
底层仍复用现有手动转存链路（行为不变，P2b 才会替换判定）。

架构：``docs/plan/2026-10-28-频道监控并入搜索渠道-架构.md`` 第 4 节 Phase 1。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:  # 单测环境可能不注入宿主日志
    from app.log import logger
except Exception:  # noqa: BLE001
    import logging

    logger = logging.getLogger(__name__)

#: 手动转存接口单次链接上限；超出部分截断（与 tg_monitor 的 BATCH_LINK_LIMIT 一致）。
MAX_LINKS = 50


@dataclass
class Candidate:
    """推送型渠道发现的一条候选（一条频道消息）。"""

    links: List[str] = field(default_factory=list)
    title: str = ""
    subscribe_id: int = 0
    channel: str = ""
    message_id: int = 0
    #: 候选来源批次，用于账本按 run 聚合；留空表示单条提交。
    run_id: str = ""

    def via(self) -> str:
        """人类可读来源标识，如 ``tg:@QukanMovie/12281``。"""
        if not self.channel:
            return "push"
        if not self.message_id:
            return f"tg:{self.channel}"
        return f"tg:{self.channel}/{self.message_id}"

    def clean_links(self) -> List[str]:
        """去空、去重、截断到上限。"""
        seen: List[str] = []
        for link in self.links or []:
            text = str(link or "").strip()
            if text and text not in seen:
                seen.append(text)
            if len(seen) >= MAX_LINKS:
                break
        return seen


@dataclass
class SubmitResult:
    """提交结果：既给账本用，也直接回给频道监控判定成功/失败。"""

    success: bool
    message: str = ""
    via: str = ""
    links: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """保持 ``{"success", "message"}`` 契约，附加来源标识。"""
        payload = dict(self.raw or {})
        payload["success"] = bool(self.success)
        payload["message"] = self.message
        payload["discovered_via"] = self.via
        return payload


def _ledger_store(owner: Any) -> Any:
    """拿候选账本存储；拿不到返回 None（账本不能影响主流程）。"""
    for attr in ("_get_data_store", "_storage"):
        getter = getattr(owner, attr, None)
        if not callable(getter):
            continue
        try:
            return getter().ledger_store()
        except Exception as error:  # noqa: BLE001
            logger.debug(f"获取候选账本存储失败：{error}", exc_info=True)
            return None
    return None


def _record_ledger(owner: Any, candidate: Candidate, result: SubmitResult) -> None:
    """把这次提交写进候选账本（kind=push_submit），失败只记 debug。"""
    store = _ledger_store(owner)
    if store is None:
        return
    rows = [
        {
            "kind": "push_submit",
            "provider": "频道监控",
            "reason": (result.message or "")[:300],
            "reference": result.via,
            "resource_title": candidate.title,
            "subscribe_id": candidate.subscribe_id or None,
        }
    ]
    try:
        try:
            from .candidate_ledger import append_records
        except ImportError:  # 单测精简环境下没有账本模块
            store.append(rows, run_id=candidate.run_id)
        else:
            append_records(store, rows, run_id=candidate.run_id)
    except Exception as error:  # noqa: BLE001
        logger.debug(f"写候选账本失败：{error}", exc_info=True)


def submit_candidate(
        owner: Any,
        candidate: Candidate,
        *,
        wait: bool = True,
        extra_payload: Optional[Dict[str, Any]] = None,
) -> SubmitResult:
    """提交一条推送候选：清洗链接 → 复用手动转存 → 记账本。

    Phase 2a 行为与旧的 ``_auto_transfer_tg_item`` **完全等价**：
    仍然调 ``api_vue_start_manual_sync``，只是多了 via 与账本。
    """
    links = candidate.clean_links()
    via = candidate.via()
    if not links:
        result = SubmitResult(success=False, message="没有可转存的网盘链接", via=via)
        _record_ledger(owner, candidate, result)
        return result

    payload: Dict[str, Any] = {
        "subscribe_id": int(candidate.subscribe_id or 0),
        "resource_links": links,
    }
    title = str(candidate.title or "").strip()
    if title:
        # 频道消息标题比订阅名更贴近真实资源，电影单文件兜底靠它
        payload["resource_titles"] = {link: title for link in links}
    if extra_payload:
        payload.update(extra_payload)

    try:
        from ..api.sync import SyncApi

        # wait=True 才能拿到实际转存文件数；否则「任务已提交」会被误标成已转存
        raw = SyncApi(owner).api_vue_start_manual_sync(payload, wait=wait) or {}
    except Exception as error:  # noqa: BLE001 - 提交异常要作为失败回给监控
        logger.warning(f"提交候选失败（{via}）：{error}")
        result = SubmitResult(success=False, message=str(error), via=via, links=len(links))
        _record_ledger(owner, candidate, result)
        return result

    if not isinstance(raw, dict):
        raw = {"success": bool(raw), "message": ""}
    result = SubmitResult(
        success=bool(raw.get("success")),
        message=str(raw.get("message") or ""),
        via=via,
        links=len(links),
        raw=raw,
    )
    _record_ledger(owner, candidate, result)
    return result
