"""aria2 JSON-RPC 客户端契约：返回解析、错误映射、token 参数、连接失败。"""

import json
import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from plugin_env import load_module

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

rpc_module = load_module(
    "aria2_rpc_under_test",
    "plugins.v2/cloudsubscribex/core/aria2/rpc.py",
)
Aria2Error = rpc_module.Aria2Error
Aria2Rpc = rpc_module.Aria2Rpc

SECRET = "test-secret"


class RpcStub:
    """本地 JSON-RPC 桩，记录请求体并返回固定 result/error。"""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.requests.append(body)
                if outer.error is not None:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "error": outer.error,
                    }
                else:
                    payload = {
                        "jsonrpc": "2.0",
                        "id": body.get("id"),
                        "result": outer.result,
                    }
                data = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )
        self._thread.start()

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Aria2RpcTest(unittest.TestCase):
    def test_call_returns_result(self):
        stub = RpcStub(result={"version": "1.37.0"})
        self.addCleanup(stub.close)
        rpc = Aria2Rpc(stub.port, SECRET)
        self.assertEqual(rpc.get_version(), {"version": "1.37.0"})

    def test_error_response_raises_with_code_and_message(self):
        stub = RpcStub(error={"code": 24, "message": "authorization failed"})
        self.addCleanup(stub.close)
        rpc = Aria2Rpc(stub.port, SECRET)
        with self.assertRaises(Aria2Error) as context:
            rpc.add_uri(["http://example/a.mkv"], {})
        self.assertEqual(context.exception.code, 24)
        self.assertEqual(context.exception.message, "authorization failed")

    def test_connection_failure_maps_to_minus_one(self):
        port = _free_port()
        rpc = Aria2Rpc(port, SECRET, timeout=0.2)
        with self.assertRaises(Aria2Error) as context:
            rpc.get_version()
        self.assertEqual(context.exception.code, -1)

    def test_request_body_params_start_with_token(self):
        stub = RpcStub(result="gid-1")
        self.addCleanup(stub.close)
        rpc = Aria2Rpc(stub.port, SECRET)
        rpc.call("aria2.addUri", [["http://example/a.mkv"], {"split": "16"}])
        body = stub.requests[0]
        self.assertEqual(body["method"], "aria2.addUri")
        self.assertEqual(body["params"][0], f"token:{SECRET}")
        self.assertEqual(body["params"][1], ["http://example/a.mkv"])
        self.assertEqual(body["params"][2], {"split": "16"})


if __name__ == "__main__":
    unittest.main()
