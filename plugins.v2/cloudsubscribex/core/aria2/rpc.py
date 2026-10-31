"""极简 aria2 JSON-RPC 客户端（仅标准库 urllib）。

只覆盖下载腿需要的方法；所有请求都带 ``token:<secret>`` 作为首项参数。
aria2 返回 ``error`` 时抛 :class:`Aria2Error`，网络/HTTP 异常不吞掉。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

_RPC_ID = "cloudsubscribex"


class Aria2Error(RuntimeError):
    """aria2 RPC 调用失败；``code == -1`` 表示传输层错误。"""

    def __init__(self, code: int, message: str):
        self.code = int(code)
        self.message = str(message)
        super().__init__(self.message)


class Aria2Rpc:
    """面向单个 aria2 daemon 的 JSON-RPC 客户端。"""

    def __init__(
            self,
            port: int,
            secret: str,
            timeout: float = 5.0,
            host: str = "127.0.0.1",
    ):
        self._host = str(host)
        self._port = int(port)
        self._secret = str(secret or "")
        self._timeout = float(timeout)
        self._url = f"http://{self._host}:{self._port}/jsonrpc"

    def call(
            self,
            method: str,
            params: list | None = None,
            timeout: float | None = None,
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": _RPC_ID,
            "method": str(method),
            "params": [f"token:{self._secret}", *(params or [])],
        }
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                    request,
                    timeout=self._timeout if timeout is None else float(timeout),
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise Aria2Error(-1, str(error)) from error

        error = body.get("error") if isinstance(body, dict) else None
        if error:
            raise Aria2Error(
                int(error.get("code", -1)),
                str(error.get("message", "")),
            )
        return body.get("result") if isinstance(body, dict) else None

    def get_version(self) -> dict:
        return self.call("aria2.getVersion") or {}

    def add_uri(self, uris: list[str], options: dict) -> str:
        return str(self.call("aria2.addUri", [list(uris), dict(options or {})]))

    def tell_status(self, gid: str, keys: list[str] | None = None) -> dict:
        params: list = [str(gid)]
        if keys:
            params.append(list(keys))
        return self.call("aria2.tellStatus", params) or {}

    def change_uri(self, gid: str, uris: list[str]) -> list:
        return self.call(
            "aria2.changeUri", [str(gid), 1, [], list(uris)]
        ) or []

    def force_pause(self, gid: str) -> str:
        return str(self.call("aria2.forcePause", [str(gid)]))

    def remove(self, gid: str) -> str:
        return str(self.call("aria2.remove", [str(gid)]))

    def remove_download_result(self, gid: str) -> str:
        return str(self.call("aria2.removeDownloadResult", [str(gid)]))

    def shutdown(self) -> str:
        return str(self.call("aria2.shutdown"))
