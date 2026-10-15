"""Gopeed 外部下载器接入（REST API）。

仅替换「夸克 → 本地中继」这一步：取链仍由 Provider 完成，Gopeed 负责实际下载到
本地路径；其余链路（转存、上传 115、重命名、通知、历史、STRM）不变。任何 Gopeed
侧异常都可经 ``fallback_service`` 自动回落内置下载器，主链路不因外部依赖挂掉。

安全约束：接口令牌只放进请求头 ``X-Api-Token``，绝不写日志、历史、通知或异常消息。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import requests

from app.log import logger

from .cloud import CloudFile


class GopeedError(RuntimeError):
    """Gopeed REST 调用失败（异常消息不得包含接口令牌）。"""


class GopeedDownloadStopped(InterruptedError, RuntimeError):
    """调用方请求停止下载（同时兼容内置下载器的 InterruptedError 语义）。"""


class GopeedClient:
    """Gopeed REST API 客户端（v1.9.3 与 2.x 同源）。"""

    def __init__(self, base_url: str, token: str = "", timeout: float = 15.0):
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._token = str(token or "").strip()
        self._timeout = max(1.0, float(timeout or 15.0))

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self._token:
            headers["X-Api-Token"] = self._token
        return headers

    def _request(
            self, method: str, path: str,
            json_body: Optional[dict] = None, params: Optional[dict] = None,
    ) -> dict:
        if not self._base_url:
            raise GopeedError("Gopeed 地址未配置")
        url = f"{self._base_url}{path}"
        try:
            response = requests.request(
                method, url,
                headers=self._headers(),
                json=json_body,
                params=params,
                timeout=self._timeout,
            )
        except requests.RequestException as error:
            raise GopeedError(f"连接 Gopeed 失败：{error}") from error
        if response.status_code == 401:
            raise GopeedError("Gopeed 鉴权失败（HTTP 401），请检查接口令牌")
        if response.status_code >= 400:
            raise GopeedError(f"Gopeed 接口异常：HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as error:
            raise GopeedError("Gopeed 返回了非 JSON 响应") from error
        if isinstance(payload, dict) and payload.get("code") not in (0, None):
            raise GopeedError(f"Gopeed 返回错误码：{payload.get('code')}")
        return payload if isinstance(payload, dict) else {}

    def info(self) -> dict:
        """连通性/版本探测（路由见 Gopeed `pkg/rest/server.go:122`）。"""
        return self._request("GET", "/api/v1/info")

    def create_task(self, url: str, headers: dict, path: str, name: str) -> str:
        body = {
            "req": {
                "url": str(url or ""),
                "labels": {},
                "extra": {"header": dict(headers or {})},
            },
            "opts": {
                "name": str(name or ""),
                "path": str(path or ""),
                "selectFiles": [],
            },
        }
        payload = self._request("POST", "/api/v1/tasks", json_body=body)
        task_id = str(payload.get("data") or "")
        if not task_id:
            raise GopeedError("Gopeed 创建任务未返回任务 ID")
        return task_id

    def get_task(self, task_id: str) -> dict:
        payload = self._request("GET", f"/api/v1/tasks/{task_id}")
        data = payload.get("data")
        return data if isinstance(data, dict) else {}

    def pause_task(self, task_id: str) -> None:
        self._request("PUT", f"/api/v1/tasks/{task_id}/pause")

    def delete_task(self, task_id: str) -> None:
        self._request("DELETE", f"/api/v1/tasks/{task_id}")


def parse_path_prefixes(raw: str) -> List[Tuple[str, str]]:
    """解析 ``gopeed_path_prefix``：``Gopeed侧前缀=本机前缀``，逗号/分号分隔多条。"""
    pairs: List[Tuple[str, str]] = []
    text = str(raw or "").replace("；", ",").replace(";", ",")
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        gopeed_side, local_side = chunk.split("=", 1)
        gopeed_side = gopeed_side.strip().rstrip("/") or "/"
        local_side = local_side.strip().rstrip("/") or "/"
        pairs.append((gopeed_side, local_side))
    return pairs


class GopeedDownloadService:
    """把「下载到本地」交给 Gopeed；签名与 ``HttpFileDownloadService.download_file`` 对齐。

    流程：取链 → 建任务（路径按前缀映射换算）→ 轮询进度回调 → 完成后校验本机文件；
    任务失败则重新取链并重建任务（最多 ``max_attempts`` 次，指数退避）；Gopeed 侧
    异常最终经 ``fallback_service`` 回落内置下载器。``preserve_partial=True`` 时
    不删除 Gopeed 侧任务/文件（与内置断点续传语义对齐）。
    """

    def __init__(
            self,
            resolver: Callable[[CloudFile], Tuple[str, dict]],
            gopeed_url: str,
            gopeed_token: str = "",
            path_prefix: str = "",
            timeout: float = 15.0,
            poll_interval: float = 1.0,
            max_attempts: int = 3,
            backoff_base: float = 1.0,
            stall_timeout: float = 900.0,
            fallback_service=None,
            client=None,
    ):
        self._resolver = resolver
        self._client = client or GopeedClient(gopeed_url, gopeed_token, timeout)
        self._prefix_pairs = parse_path_prefixes(path_prefix)
        self._poll_interval = max(0.01, float(poll_interval or 1.0))
        self._max_attempts = max(1, int(max_attempts or 3))
        self._backoff_base = max(0.0, float(backoff_base or 0.0))
        # 长时间零进度视为卡死（0 = 关闭），避免外部任务卡住中继并发位
        self._stall_timeout = max(0.0, float(stall_timeout or 0.0))
        self._fallback_service = fallback_service

    def _map_dir_for_gopeed(self, local_dir: str) -> str:
        """把本机目录换算成 Gopeed 容器内可见路径（未配置映射时原样返回）。"""
        path = str(local_dir or "").rstrip("/") or "/"
        for gopeed_side, local_side in self._prefix_pairs:
            if path == local_side:
                return gopeed_side
            if local_side != "/" and path.startswith(local_side + "/"):
                return gopeed_side.rstrip("/") + path[len(local_side):]
        return path

    def download_file(
            self, file_item: CloudFile, local_path: str,
            progress_callback=None, stop_requested=None,
            preserve_partial: bool = False,
    ) -> str:
        try:
            return self._download(
                file_item, local_path, progress_callback,
                stop_requested, preserve_partial,
            )
        except (GopeedDownloadStopped, InterruptedError):
            raise
        except Exception as error:  # noqa: BLE001 - Gopeed 侧任何异常都回落内置
            if self._fallback_service is None:
                raise
            logger.warning(
                f"Gopeed 下载失败，自动回落内置下载器：{file_item.name}；"
                f"原因：{type(error).__name__}: {error}"
            )
            return self._fallback_service.download_file(
                file_item, local_path, progress_callback, stop_requested,
                preserve_partial=preserve_partial,
            )

    def _download(
            self, file_item: CloudFile, local_path: str,
            progress_callback, stop_requested, preserve_partial: bool,
    ) -> str:
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        remote_dir = self._map_dir_for_gopeed(str(target.parent))
        last_error: Optional[Exception] = None
        for attempt in range(1, self._max_attempts + 1):
            if stop_requested and stop_requested():
                raise GopeedDownloadStopped("下载已停止")
            url, headers = self._resolver(file_item)
            if not url:
                raise RuntimeError("源网盘未返回下载地址")
            task_id = ""
            try:
                task_id = self._client.create_task(
                    url=url, headers=headers, path=remote_dir, name=target.name,
                )
                return self._wait_task(
                    task_id, file_item, target,
                    progress_callback, stop_requested, preserve_partial,
                )
            except (GopeedDownloadStopped, InterruptedError):
                raise
            except Exception as error:  # noqa: BLE001 - 单次任务失败换新直链重建
                last_error = error
                self._discard_task(task_id, preserve_partial)
                if attempt >= self._max_attempts:
                    break
                logger.warning(
                    f"Gopeed 任务失败（第 {attempt}/{self._max_attempts} 次），"
                    f"重新取链并重建任务：{file_item.name}；"
                    f"task_id={task_id or '-'}；原因：{type(error).__name__}: {error}"
                )
                if self._backoff_base > 0:
                    time.sleep(self._backoff_base * attempt)
        raise RuntimeError(
            f"Gopeed 下载失败（已重试 {self._max_attempts} 次）："
            f"{type(last_error).__name__}: {last_error}"
        )

    def _wait_task(
            self, task_id: str, file_item: CloudFile, target: Path,
            progress_callback, stop_requested, preserve_partial: bool,
    ) -> str:
        reported = 0
        last_progress_at = time.monotonic()
        while True:
            if stop_requested and stop_requested():
                self._stop_task(task_id, preserve_partial)
                raise GopeedDownloadStopped("下载已停止")
            task = self._client.get_task(task_id)
            status = str(task.get("status") or "").strip().lower()
            progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
            # Gopeed 进度字段是 downloaded（pkg/download/downloader.go 的 Progress），
            # 没有 read/total；总量回退到 meta.res.size 与源文件大小。
            done = max(0, int(progress.get("downloaded") or 0))
            meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
            resource = meta.get("res") if isinstance(meta.get("res"), dict) else {}
            total = max(
                0,
                int(
                    resource.get("size")
                    or getattr(file_item, "size", 0)
                    or 0
                ),
            )
            if done > reported:
                reported = done
                last_progress_at = time.monotonic()
            if progress_callback and (reported or total):
                progress_callback(reported, total)
            if status == "done":
                result = self._verify_result(file_item, target)
                # 完成后仅移除 Gopeed 任务记录（不带 force，文件保留在盘上），
                # 避免长期运行后 Gopeed 任务列表无限堆积。
                try:
                    self._client.delete_task(task_id)
                except Exception as error:  # noqa: BLE001 - 清理失败不影响下载结果
                    logger.debug(f"清理已完成 Gopeed 任务失败：task_id={task_id}；{error}")
                return result
            if status in {"error", "pause", "removed"}:
                # pause：任务被外部暂停（Gopeed 面板/其它调用方），不处理会永久空转
                raise RuntimeError(f"Gopeed 任务未完成：status={status}，task_id={task_id}")
            if self._stall_timeout > 0 and time.monotonic() - last_progress_at > self._stall_timeout:
                raise RuntimeError(
                    f"Gopeed 任务 {self._stall_timeout:.0f} 秒无进度，判定卡死：task_id={task_id}"
                )
            time.sleep(self._poll_interval)

    def _verify_result(self, file_item: CloudFile, target: Path) -> str:
        if not target.is_file():
            raise RuntimeError(f"Gopeed 任务完成但本机未见文件：{target}")
        actual = target.stat().st_size
        expected = int(getattr(file_item, "size", 0) or 0)
        if expected > 0 and actual != expected:
            raise RuntimeError(
                f"Gopeed 下载文件大小不符：预期 {expected}，实际 {actual}"
            )
        if expected <= 0 and actual <= 0:
            raise RuntimeError("源网盘下载为空文件")
        return str(target)

    def _stop_task(self, task_id: str, preserve_partial: bool) -> None:
        """停止：暂停任务；未要求保留断点时删除任务（保留文件语义与内置对齐）。"""
        if not task_id:
            return
        try:
            self._client.pause_task(task_id)
        except Exception as error:  # noqa: BLE001 - 停止路径不因清理失败而改道
            logger.debug(f"暂停 Gopeed 任务失败：task_id={task_id}；{error}")
        if not preserve_partial:
            self._discard_task(task_id, preserve_partial)

    def _discard_task(self, task_id: str, preserve_partial: bool) -> None:
        if not task_id or preserve_partial:
            return
        try:
            self._client.delete_task(task_id)
        except Exception as error:  # noqa: BLE001 - 清理失败不影响主流程判定
            logger.debug(f"删除 Gopeed 任务失败：task_id={task_id}；{error}")
