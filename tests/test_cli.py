"""CLI 回归：stub 服务器模拟 MoviePilot 路由，验证渲染、认证与退出码。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import cli  # noqa: E402


class _StubState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.token_valid = "good-token"
        self.doctor_status = "ok"
        self.login_ok = True


STATE = _StubState()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - 静默
        return

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, method: str, **extra) -> None:
        STATE.requests.append({
            "method": method,
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            **extra,
        })

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if self.path.startswith("/api/v1/login/access-token"):
            self._record("POST", login=True)
            if not STATE.login_ok:
                self._json({"detail": "bad password"}, 401)
                return
            self._json({"access_token": STATE.token_valid, "token_type": "bearer"})
            return
        self._record("POST", body=raw)
        self._json({"success": True, "data": {"ok": True}})

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET")
        path = urllib.parse.urlparse(self.path).path
        auth = self.headers.get("Authorization") or ""
        if auth != f"Bearer {STATE.token_valid}":
            self._json({"detail": "Not authenticated"}, 401)
            return
        if path.endswith("/doctor"):
            self._json({"success": True, "data": {"checks": [
                {"name": "网盘连接", "status": STATE.doctor_status, "detail": "ok"},
                {"name": "媒体库", "status": "ok", "detail": "已连接"},
            ]}})
            return
        if path.endswith("/page_data"):
            self._json({"success": True, "data": {
                "total": 2,
                "records": [
                    {"created_at": "2026-11-02T10:00:00", "title": "牧神记",
                     "episode": "E107", "status": "success", "source": "115"},
                    {"created_at": "2026-11-02T11:00:00", "title": "谎言",
                     "episode": "E03", "status": "failed", "source": "quark"},
                ],
                "subscribes_progress": [
                    {"title": "牧神记", "landed_count": 106, "total_episodes": 110,
                     "missing_episodes": ["E107", "E108"], "status": "追更中"},
                ],
            }})
            return
        if path.endswith("/overview"):
            self._json({"success": True, "data": {"subscribes": 79, "history": {"total": 2696}}})
            return
        if path.endswith("/ledger/list"):
            self._json({"success": True, "data": {"items": [
                {"created_at": "2026-11-02T09:00:00", "file_name": "a.mkv",
                 "state": "done", "attempts": 1},
            ]}})
            return
        if path.endswith("/duplicate/list"):
            self._json({"success": True, "data": {"groups": [
                {"title": "夜曲", "tmdb_id": 292741, "ids": [290, 291]},
            ]}})
            return
        self._json({"success": True, "data": {}})


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        STATE.requests.clear()
        STATE.token_valid = "good-token"
        STATE.doctor_status = "ok"
        STATE.login_ok = True
        self.tmp = tempfile.mkdtemp()
        self._old_config, self._old_token = cli.CONFIG_PATH, cli.TOKEN_PATH
        cli.CONFIG_PATH = os.path.join(self.tmp, "config.json")
        cli.TOKEN_PATH = os.path.join(self.tmp, "token.json")
        for key in ("MP_URL", "MP_TOKEN", "MP_USER", "MP_PASSWORD"):
            os.environ.pop(key, None)
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        cli.CONFIG_PATH, cli.TOKEN_PATH = self._old_config, self._old_token

    def _run(self, *argv: str):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--url", self.base, *argv])
        return code, out.getvalue(), err.getvalue()

    # ---------------------------------------------------------------- 认证

    def test_missing_token_exits_3(self):
        code, _, err = self._run("doctor")
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("缺少认证信息", err)

    def test_token_flag_is_used(self):
        code, out, _ = self._run("--token", "good-token", "doctor")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("网盘连接", out)
        self.assertEqual(STATE.requests[0]["auth"], "Bearer good-token")

    def test_wrong_token_exits_3(self):
        code, _, err = self._run("--token", "bad-token", "overview")
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("认证失败", err)

    def test_env_token_is_used(self):
        os.environ["MP_TOKEN"] = "good-token"
        code, _, _ = self._run("overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(STATE.requests[0]["auth"], "Bearer good-token")

    def test_username_password_logs_in_and_caches(self):
        code, out, _ = self._run(
            "--username", "u", "--password", "p", "overview",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("subscribes", out)
        self.assertTrue(os.path.exists(cli.TOKEN_PATH))
        cached = json.load(open(cli.TOKEN_PATH, encoding="utf-8"))
        self.assertEqual(cached["access_token"], "good-token")

    def test_cached_token_reused_without_login(self):
        cli._write_json(cli.TOKEN_PATH, {"url": self.base, "access_token": "good-token"}, True)
        code, _, _ = self._run("--username", "u", "--password", "p", "overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(any("login" in req["path"] for req in STATE.requests))

    def test_expired_token_relogins_once(self):
        cli._write_json(cli.TOKEN_PATH, {"url": self.base, "access_token": "stale"}, True)
        STATE.token_valid = "fresh-token"
        code, _, _ = self._run("--username", "u", "--password", "p", "overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertTrue(any("login" in req["path"] for req in STATE.requests))

    def test_bad_password_exits_3(self):
        STATE.login_ok = False
        code, _, err = self._run("--username", "u", "--password", "p", "doctor")
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("登录失败", err)

    # ---------------------------------------------------------------- 命令

    def test_doctor_green_exit_0(self):
        code, out, _ = self._run("--token", "good-token", "doctor")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("✔", out)
        self.assertIn("2 项", out)

    def test_doctor_red_exit_1(self):
        STATE.doctor_status = "error"
        code, out, _ = self._run("--token", "good-token", "doctor")
        self.assertEqual(code, cli.EXIT_API)
        self.assertIn("✖", out)
        self.assertIn("1 异常", out)

    def test_history_filters_are_forwarded(self):
        code, out, _ = self._run(
            "--token", "good-token", "history", "--size", "5", "--keyword", "牧神",
            "--status", "success",
        )
        self.assertEqual(code, cli.EXIT_OK)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(STATE.requests[0]["path"]).query)
        self.assertEqual(query["page_size"], ["5"])
        self.assertEqual(query["keyword"], ["牧神"])
        self.assertEqual(query["statuses"], ["success"])
        self.assertIn("牧神记", out)
        self.assertIn("共 2 条", out)

    def test_progress_shows_landed_over_total(self):
        code, out, _ = self._run("--token", "good-token", "progress")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("106/110", out)
        self.assertIn("E107", out)

    def test_ledger_and_duplicate(self):
        code, out, _ = self._run("--token", "good-token", "ledger")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("a.mkv", out)
        code, out, _ = self._run("--token", "good-token", "duplicate")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("夜曲", out)
        self.assertIn("290,291", out)

    def test_sync_pause_requires_on_off(self):
        code, _, err = self._run("--token", "good-token", "sync", "pause")
        self.assertEqual(code, cli.EXIT_ARGS)
        self.assertIn("on 或 off", err)

    def test_sync_pause_posts_body(self):
        code, out, _ = self._run("--token", "good-token", "sync", "pause", "on")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("开启", out)
        self.assertEqual(json.loads(STATE.requests[0]["body"]), {"paused": True})

    def test_sync_start_with_keyword(self):
        code, _, _ = self._run("--token", "good-token", "sync", "start", "--keyword", "三体")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(json.loads(STATE.requests[0]["body"]), {"keyword": "三体"})

    def test_raw_get_and_post(self):
        code, out, _ = self._run(
            "--token", "good-token", "raw", "GET", "/runtime", "--param", "a=1", "--json",
        )
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("a=1", STATE.requests[0]["path"])
        self.assertIn('"success": true', out)
        code, _, err = self._run("--token", "good-token", "raw", "POST", "/x", "--body", "{bad}")
        self.assertEqual(code, cli.EXIT_ARGS)
        self.assertIn("不是合法 JSON", err)

    def test_json_output_is_raw(self):
        code, out, _ = self._run("--token", "good-token", "overview", "--json")
        self.assertEqual(code, cli.EXIT_OK)
        payload = json.loads(out)
        self.assertEqual(payload["data"]["subscribes"], 79)

    # ---------------------------------------------------------------- 渲染

    def test_table_aligns_cjk_width(self):
        table = cli.render_table(["名字", "值"], [["牧神记", "1"], ["ab", "2"]], max_width=40)
        rows = table.splitlines()
        self.assertEqual(cli._width(rows[0]), cli._width(rows[2]))
        self.assertEqual(cli._width(rows[0]), cli._width(rows[3]))

    def test_truncate_respects_width(self):
        text = cli._truncate("牧神记第二季第十二集", 9)
        self.assertLessEqual(cli._width(text), 9)
        self.assertTrue(text.endswith("…"))

    def test_no_third_party_imports(self):
        source = open(cli.__file__, encoding="utf-8").read()
        for forbidden in ("import requests", "import click", "import typer", "import httpx"):
            self.assertNotIn(forbidden, source)

    def test_help_lists_all_commands(self):
        out = io.StringIO()
        with redirect_stdout(out):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        text = out.getvalue()
        for name in ("doctor", "overview", "history", "progress", "ledger", "duplicate", "sync", "raw"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
