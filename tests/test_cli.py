"""CLI 回归：stub 服务器模拟 MoviePilot 路由，响应结构取自 2026-11-02 真机抓包。

教训（v1.9.23 → v1.9.24）：v1.9.23 的 fixture 是照着「以为的字段名」写的，
上线后 doctor 渲染成 JSON 原文、progress 显示 `/0`。本文件所有 payload 均按真机
字段录入，改动前请先回抓一次真实响应。
"""

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

# ---------------------------------------------------------------- 真机 fixture（脱敏）

DOCTOR_PAYLOAD = {
    "checked_at": "2026-11-02 10:29:22",
    "overall": "ok",
    "counts": {"ok": 8, "warn": 1, "error": 0, "skip": 1},
    "elapsed_ms": 223,
    "items": [
        {"key": "plugin_db", "name": "插件私有库", "status": "ok",
         "detail": "转存流水 2394 条，库文件 8.8 MB", "hint": None, "ms": 102},
        {"key": "search", "name": "搜索渠道", "status": "warn",
         "detail": "可用渠道 3 个", "hint": "建议补一条渠道", "ms": 0},
        {"key": "media_dir", "name": "整理目录", "status": "skip",
         "detail": "未配置整理源目录", "hint": "不用整理功能可忽略", "ms": 0},
    ],
}

PROGRESS_CARDS = [
    {"subscribe_id": 340, "title": "小丑回魂：德里故事", "year": "2026", "season": None,
     "type": "电影", "media_type": "movie", "total_episodes": 0, "library_episodes": [],
     "library_count": 0, "missing_episodes": [1], "missing_count": 1, "state": "R"},
    {"subscribe_id": 12, "title": "牧神记", "year": "2024", "season": 1, "type": "电视剧",
     "media_type": "tv", "total_episodes": 110, "library_episodes": list(range(1, 107)),
     "library_count": 106, "missing_episodes": [107, 108, 109, 110], "missing_count": 4,
     "state": "R"},
    {"subscribe_id": 13, "title": "谎言", "year": "2026", "season": 1, "type": "电视剧",
     "media_type": "tv", "total_episodes": 16, "library_episodes": [1, 2, 3],
     "library_count": 3, "missing_episodes": [4, 5, 6, 8, 9, 10],
     "missing_count": 6, "state": "R"},
]

HISTORY_RECORD = {
    "title": "登登登邓氏三宝", "year": "2026", "type": "电视剧", "status": "成功",
    "status_label": "成功", "time": "2026-11-02 08:21:33", "season": 1, "episode": 8,
    "file_name": "登登登邓氏三宝 - S01E08 - 第8集.mkv", "file_size": 2583332643,
    "source": "115", "cloud_drive_name": "115网盘",
}

PAGE_DATA_PAYLOAD = {
    "history_page": {"page": 1, "page_size": 2, "total": 2394, "total_pages": 1197},
    "history_groups": [
        {"group_key": "tmdb:314478:1", "title": "登登登邓氏三宝", "year": "2026",
         "type": "电视剧", "latest_time": "2026-11-02 08:21:33", "success_count": 8,
         "pending_count": 0, "failed_count": 1, "total_size": 19472704367,
         "seasons": [1], "records": [HISTORY_RECORD]},
        {"group_key": "tmdb:292741:1", "title": "夜曲", "year": "2025", "type": "电视剧",
         "latest_time": "2026-11-01 22:10:00", "success_count": 3, "pending_count": 1,
         "failed_count": 0, "total_size": 1073741824, "seasons": [1, 2], "records": []},
    ],
    "subscribes_progress": PROGRESS_CARDS,
}

LEDGER_PAYLOAD = {
    "total": 1588, "days": 3, "today": "2026-11-02",
    "items": [
        {"kind": "push_submit", "provider": "频道监控",
         "reason": "订阅搜索完成，未发现需要转存的新资源",
         "reference": "tg:https://t.me/+xxx/108079",
         "resource_title": "名称：嫁金钗 (2026) 4K 高码率 更25集",
         "subscribe_id": 158, "ts": "2026-11-02T10:21:21"},
    ],
}

DUPLICATE_PAYLOAD = {
    "duplicate_total": 1,
    "groups": [
        {"name": "戴帽子的猫", "type": "电影", "season": None, "keep_id": 298,
         "keep_state": "R", "duplicates": [{"id": 299, "state": "R", "season": None,
                                            "date": "2026-10-31 23:38:21"}]},
    ],
}

OVERVIEW_PAYLOAD = {
    "stats": [
        {"title": "总转存", "value": 2394, "color": "primary"},
        {"title": "今日转存", "value": 34, "color": "info"},
        {"title": "成功", "value": 2377, "color": "success"},
        {"title": "失败", "value": 9, "color": "error"},
    ],
    "history_count": 2394,
    "cache": {"hit_rate": 32.5},
    "runtime": {"syncing": False},
}


class _StubState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.token_valid = "good-token"
        self.doctor_counts = {"ok": 8, "warn": 1, "error": 0, "skip": 1}
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
            "method": method, "path": self.path,
            "auth": self.headers.get("Authorization"), **extra,
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
        if (self.headers.get("Authorization") or "") != f"Bearer {STATE.token_valid}":
            self._json({"detail": "Not authenticated"}, 401)
            return
        if path.endswith("/doctor"):
            payload = dict(DOCTOR_PAYLOAD)
            payload["counts"] = STATE.doctor_counts
            self._json({"success": True, "data": payload})
            return
        if path.endswith("/page_data"):
            self._json({"success": True, "data": PAGE_DATA_PAYLOAD})
            return
        if path.endswith("/overview"):
            self._json({"success": True, "data": OVERVIEW_PAYLOAD})
            return
        if path.endswith("/ledger/list"):
            self._json({"success": True, "data": LEDGER_PAYLOAD})
            return
        if path.endswith("/duplicate/list"):
            self._json({"success": True, "data": DUPLICATE_PAYLOAD})
            return
        self._json({"success": True, "data": {}})


class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        STATE.requests.clear()
        STATE.token_valid = "good-token"
        STATE.doctor_counts = {"ok": 8, "warn": 1, "error": 0, "skip": 1}
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
        self.assertIn("插件私有库", out)
        self.assertEqual(STATE.requests[0]["auth"], "Bearer good-token")

    def test_env_token_is_used(self):
        os.environ["MP_TOKEN"] = "good-token"
        code, _, _ = self._run("overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(STATE.requests[0]["auth"], "Bearer good-token")

    def test_wrong_token_exits_3(self):
        code, _, err = self._run("--token", "bad-token", "overview")
        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("认证失败", err)

    def test_username_password_logs_in_and_caches(self):
        code, out, _ = self._run("--username", "u", "--password", "p", "overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("总转存 2394", out)
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

    # ---------------------------------------------------------------- doctor

    def test_doctor_renders_items_and_counts(self):
        code, out, _ = self._run("--token", "good-token", "doctor")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("✔", out)
        self.assertIn("⚠", out)
        self.assertIn("—", out)
        self.assertIn("插件私有库", out)
        self.assertIn("2394 条", out)
        self.assertIn("8 正常 / 1 警告 / 0 异常 / 1 跳过", out)
        self.assertNotIn('"checked_at"', out)  # 不许把 JSON 原样倒出来

    def test_doctor_red_exit_1(self):
        STATE.doctor_counts = {"ok": 7, "warn": 1, "error": 1, "skip": 0}
        code, out, _ = self._run("--token", "good-token", "doctor")
        self.assertEqual(code, cli.EXIT_API)
        self.assertIn("1 异常", out)

    # ---------------------------------------------------------------- progress

    def test_progress_uses_real_fields(self):
        code, out, _ = self._run("--token", "good-token", "progress")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("106/110", out)
        self.assertIn("E107-E110", out)
        self.assertIn("E4-E6,E8-E10", out)
        self.assertNotIn("/0 ", out)  # 电影不再显示 /0

    def test_progress_movie_shows_dash(self):
        code, out, _ = self._run("--token", "good-token", "progress")
        self.assertEqual(code, cli.EXIT_OK)
        movie_line = next(line for line in out.splitlines() if "小丑回魂" in line)
        self.assertIn("未入库", movie_line)
        self.assertIn("—", movie_line)

    def test_progress_only_missing_and_sort(self):
        code, out, _ = self._run("--token", "good-token", "progress", "--only-missing")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertNotIn("小丑回魂", out)
        self.assertIn("牧神记", out)
        code, out, _ = self._run("--token", "good-token", "progress", "--sort", "missing")
        self.assertEqual(code, cli.EXIT_OK)
        idx_lie = out.index("谎言")
        idx_mu = out.index("牧神记")
        self.assertLess(idx_lie, idx_mu)  # 谎言缺 6 集 > 牧神记缺 4 集

    def test_progress_limit(self):
        code, out, _ = self._run("--token", "good-token", "progress", "--limit", "1")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("展示 1 部 / 共 3 部", out)

    # ---------------------------------------------------------------- history

    def test_history_groups_view(self):
        code, out, _ = self._run("--token", "good-token", "history")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("登登登邓氏三宝", out)
        self.assertIn("18.1GB", out)
        self.assertIn("S1,S2", out)
        self.assertIn("共 2 组（共 1 条流水）", out)
        self.assertIn("共 2394 条", out)

    def test_history_records_view(self):
        code, out, _ = self._run("--token", "good-token", "history", "--records")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("S1E8", out)
        self.assertIn("2.4GB", out)

    def test_history_filters_forwarded(self):
        self._run("--token", "good-token", "history", "--size", "5", "--keyword", "牧神",
                  "--status", "成功")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(STATE.requests[0]["path"]).query)
        self.assertEqual(query["page_size"], ["5"])
        self.assertEqual(query["keyword"], ["牧神"])
        self.assertEqual(query["statuses"], ["成功"])

    # ---------------------------------------------------------------- 其他命令

    def test_overview_renders_stats(self):
        code, out, _ = self._run("--token", "good-token", "overview")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("总转存 2394", out)
        self.assertIn("失败 9", out)

    def test_ledger_real_fields(self):
        code, out, _ = self._run("--token", "good-token", "ledger")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("频道监控", out)
        self.assertIn("订阅搜索完成", out)
        self.assertIn("共 1588 条", out)

    def test_duplicate_real_fields(self):
        code, out, _ = self._run("--token", "good-token", "duplicate")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("戴帽子的猫", out)
        self.assertIn("298", out)
        self.assertIn("299", out)

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
        code, out, _ = self._run("--token", "good-token", "raw", "GET", "/runtime",
                                 "--param", "a=1", "--json")
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
        self.assertEqual(payload["data"]["stats"][0]["value"], 2394)

    def test_quiet_is_single_line(self):
        code, out, _ = self._run("--token", "good-token", "overview", "--quiet")
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertEqual(json.loads(out)["data"]["history_count"], 2394)

    # ---------------------------------------------------------------- 渲染工具

    def test_merge_episode_ranges(self):
        self.assertEqual(cli.merge_episode_ranges([1, 2, 3, 7, 9, 10]), "E1-E3,E7,E9-E10")
        self.assertEqual(cli.merge_episode_ranges([5]), "E5")
        self.assertEqual(cli.merge_episode_ranges([]), "")

    def test_human_size(self):
        self.assertEqual(cli.human_size(2583332643), "2.4GB")
        self.assertEqual(cli.human_size(19472704367), "18.1GB")
        self.assertEqual(cli.human_size(512), "512B")
        self.assertEqual(cli.human_size(None), "0B")

    def test_table_aligns_cjk_width(self):
        table = cli.render_table(["名字", "值"], [["牧神记", "1"], ["ab", "2"]], max_width=40)
        rows = table.splitlines()
        self.assertEqual(cli._width(rows[0]), cli._width(rows[2]))
        self.assertEqual(cli._width(rows[0]), cli._width(rows[3]))

    def test_max_cols_caps_long_column(self):
        table = cli.render_table(["a", "b"], [["x", "y" * 100]], max_cols={1: 10})
        self.assertLessEqual(max(cli._width(line) for line in table.splitlines()), 40)

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
        for name in ("doctor", "overview", "history", "progress", "ledger",
                     "duplicate", "sync", "raw"):
            self.assertIn(name, text)


if __name__ == "__main__":
    unittest.main()
