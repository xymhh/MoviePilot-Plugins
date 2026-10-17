"""深圳中继客户端：秒传未命中的单个文件交给深圳下载并上传 115。

插件只发文件身份（源盘 file id + 校验和 + 目标路径），不传直链：
中继用本地 cookie 解析直链，避免直链跨 IP / 时效问题。
"""

from __future__ import annotations

from typing import Any, Optional

import requests


class RelayError(RuntimeError):
    """中继不可用或任务失败。"""


class RelayClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 20.0):
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._token = str(token or "").strip()
        self._timeout = max(3.0, float(timeout or 20.0))

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> dict:
        if not self._base_url:
            raise RelayError("中继地址未配置")
        try:
            response = requests.request(
                method, f"{self._base_url}{path}",
                headers={"X-Token": self._token, "Accept": "application/json"},
                json=json_body, timeout=self._timeout,
            )
        except requests.RequestException as error:
            raise RelayError(f"连接中继失败：{error}") from error
        if response.status_code == 401:
            raise RelayError("中继鉴权失败（令牌不匹配）")
        if response.status_code >= 400:
            message = ""
            try:
                message = str((response.json() or {}).get("error") or "")
            except ValueError:
                pass
            raise RelayError(f"中继接口异常：HTTP {response.status_code} {message}".strip())
        try:
            payload = response.json()
        except ValueError as error:
            raise RelayError("中继返回非 JSON") from error
        return payload if isinstance(payload, dict) else {}

    def submit(self, payload: dict[str, Any]) -> dict:
        return self._request("POST", "/jobs", payload)

    def get(self, job_id: str) -> dict:
        return self._request("GET", f"/jobs/{job_id}")

    def cancel(self, job_id: str) -> None:
        try:
            self._request("DELETE", f"/jobs/{job_id}")
        except RelayError:
            return
