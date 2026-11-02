#!/usr/bin/env python3
"""CloudSubscribeX 命令行客户端（零依赖，标准库实现）。

用法示例：
    python3 cli.py doctor
    python3 cli.py history --size 20 --keyword 牧神 --status success
    python3 cli.py progress
    python3 cli.py sync pause on
    python3 cli.py raw GET /runtime --param foo=bar --json

认证（三选一，优先级从上到下）：
    1. --token / MP_TOKEN
    2. --username --password / MP_USER MP_PASSWORD（自动登录并缓存 token）
    3. ~/.config/cloudsubscribex/config.json 里的 url / token

退出码：0 成功 / 1 接口失败 / 2 参数错误 / 3 认证失败 / 4 网络不可达
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_ID = "CloudSubscribeX"
DEFAULT_URL = "http://127.0.0.1:3000"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "cloudsubscribex")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
TOKEN_PATH = os.path.join(CONFIG_DIR, "token.json")

EXIT_OK = 0
EXIT_API = 1
EXIT_ARGS = 2
EXIT_AUTH = 3
EXIT_NETWORK = 4


class CliError(Exception):
    """带退出码的 CLI 异常。"""

    def __init__(self, message: str, code: int = EXIT_API):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 配置与认证


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, payload: Dict[str, Any], private: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    if private:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def resolve_settings(args: argparse.Namespace) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """命令行 > 环境变量 > 配置文件。返回 (base_url, token, username, password)。"""
    config = _read_json(CONFIG_PATH)
    base_url = (
        args.url
        or os.environ.get("MP_URL")
        or config.get("url")
        or DEFAULT_URL
    ).rstrip("/")
    token = args.token or os.environ.get("MP_TOKEN") or config.get("token")
    username = args.username or os.environ.get("MP_USER")
    password = args.password or os.environ.get("MP_PASSWORD")
    return base_url, token, username, password


def _http_json(
    method: str,
    url: str,
    token: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
    timeout: float = 30.0,
    retries: int = 0,
) -> Any:
    """发一个 JSON 请求；GET 可重试，POST 不重试（避免重复转存）。"""
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    attempt = 0
    while True:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001 - 读错误体失败不影响主流程
                detail = ""
            if error.code in (401, 403):
                raise CliError(f"认证失败（HTTP {error.code}）：{detail or 'token 无效或过期'}", EXIT_AUTH)
            raise CliError(f"接口返回 HTTP {error.code}：{detail or error.reason}", EXIT_API)
        except urllib.error.URLError as error:
            if attempt < retries and method == "GET":
                attempt += 1
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            raise CliError(f"无法连接 {url}：{error.reason}", EXIT_NETWORK)
        except (TimeoutError, OSError) as error:
            if attempt < retries and method == "GET":
                attempt += 1
                time.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            raise CliError(f"请求超时或被中断：{error}", EXIT_NETWORK)


def login(base_url: str, username: str, password: str) -> str:
    """用用户名密码换 JWT。"""
    payload = urllib.parse.urlencode(
        {"username": username, "password": password}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/login/access-token",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
    except urllib.error.HTTPError as error:
        raise CliError(f"登录失败（HTTP {error.code}），请检查用户名密码", EXIT_AUTH)
    except urllib.error.URLError as error:
        raise CliError(f"无法连接 {base_url}：{error.reason}", EXIT_NETWORK)
    token = str(data.get("access_token") or "").strip()
    if not token:
        raise CliError("登录接口未返回 access_token", EXIT_AUTH)
    _write_json(
        TOKEN_PATH,
        {"url": base_url, "access_token": token, "saved_at": int(time.time())},
        private=True,
    )
    return token


def ensure_token(args: argparse.Namespace) -> Tuple[str, Optional[str]]:
    """确定 base_url 与可用 token；有用户名密码时优先登录并缓存。"""
    base_url, token, username, password = resolve_settings(args)
    if username and password:
        cached = _read_json(TOKEN_PATH)
        if cached.get("access_token") and cached.get("url") == base_url:
            token = str(cached["access_token"])
        else:
            token = login(base_url, username, password)
    return base_url, token


# ---------------------------------------------------------------- HTTP 封装


def call_plugin(
    args: argparse.Namespace,
    path: str,
    method: str = "GET",
    params: Optional[Dict[str, Any]] = None,
    body: Optional[Dict[str, Any]] = None,
) -> Any:
    base_url, token = ensure_token(args)
    if not token:
        raise CliError(
            "缺少认证信息：设置 MP_TOKEN，或用 --username/--password 登录", EXIT_AUTH
        )
    url = f"{base_url}/api/v1/plugin/{PLUGIN_ID}{path}"
    try:
        return _http_json(
            method, url, token=token, params=params, body=body,
            retries=2 if method == "GET" else 0,
        )
    except CliError as error:
        # token 过期：有账号密码时自动重登一次
        if error.code == EXIT_AUTH:
            _, _, username, password = resolve_settings(args)
            if username and password:
                fresh = login(base_url, username, password)
                return _http_json(
                    method, url, token=fresh, params=params, body=body,
                    retries=2 if method == "GET" else 0,
                )
        raise


# ---------------------------------------------------------------- 渲染工具


def _width(text: str) -> int:
    """按终端显示宽度计算（中日韩字符占 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _truncate(text: str, limit: int) -> str:
    if limit <= 1:
        return text
    if _width(text) <= limit:
        return text
    out, used = "", 0
    for ch in text:
        step = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + step > limit - 1:
            break
        out += ch
        used += step
    return out + "…"


def render_table(headers: List[str], rows: List[List[str]], max_width: Optional[int] = None) -> str:
    """等宽对齐表格；超宽按终端宽度等比截断。"""
    if not rows:
        return "（无数据）"
    terminal = max_width or shutil.get_terminal_size((100, 24)).columns
    columns = len(headers)
    natural = [
        max(2, _width(str(headers[i])), *(_width(str(row[i])) for row in rows))
        for i in range(columns)
    ]
    budget = terminal - (columns - 1) * 2
    if sum(natural) > budget:
        ratio = budget / max(1, sum(natural))
        widths = [max(6, int(value * ratio)) for value in natural]
    else:
        widths = natural
    lines = ["  ".join(_pad(str(h), widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("─" * widths[i] for i in range(columns)))
    for row in rows:
        lines.append("  ".join(
            _pad(_truncate(str(row[i]), widths[i]), widths[i]) for i in range(columns)
        ))
    return "\n".join(lines)


def render_kv(data: Dict[str, Any]) -> str:
    if not data:
        return "（空）"
    key_width = max(_width(str(key)) for key in data)
    lines = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"{_pad(str(key), key_width)}  {value}")
    return "\n".join(lines)


def _emit(payload: Any, args: argparse.Namespace, human: str) -> None:
    """--json 出格式化 JSON，--quiet 出单行 JSON（脚本取值），默认人类可读。"""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif getattr(args, "quiet", False):
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(human)


def _unwrap(payload: Any) -> Any:
    """兼容 {"success": bool, "data": ...} 与裸数据两种返回。"""
    if isinstance(payload, dict) and "data" in payload and set(payload) <= {
        "success", "data", "message", "code", "msg",
    }:
        if payload.get("success") is False:
            raise CliError(str(payload.get("message") or payload.get("msg") or "接口返回失败"))
        return payload.get("data")
    return payload


# ---------------------------------------------------------------- 命令实现


def cmd_doctor(args: argparse.Namespace) -> int:
    raw = call_plugin(args, "/doctor")
    payload = _unwrap(raw)
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        _emit(raw, args, render_kv(payload if isinstance(payload, dict) else {"data": payload}))
        return EXIT_OK

    icons = {"ok": "✔", "pass": "✔", "green": "✔",
             "warn": "⚠", "warning": "⚠", "yellow": "⚠",
             "error": "✖", "fail": "✖", "red": "✖"}
    rows = []
    for item in checks:
        status = str(item.get("status") or item.get("level") or "").lower()
        rows.append([
            icons.get(status, "·"),
            str(item.get("name") or item.get("title") or ""),
            str(item.get("detail") or item.get("message") or ""),
            str(item.get("fix") or item.get("hint") or ""),
        ])
    bad = sum(1 for item in checks if str(item.get("status") or item.get("level") or "").lower()
              in ("error", "fail", "red"))
    warn = sum(1 for item in checks if str(item.get("status") or item.get("level") or "").lower()
               in ("warn", "warning", "yellow"))
    human = render_table(["", "检查项", "详情", "修复指引"], rows)
    human += f"\n\n共 {len(checks)} 项：{len(checks) - bad - warn} 正常 / {warn} 警告 / {bad} 异常"
    _emit(raw, args, human)
    return EXIT_OK if bad == 0 else EXIT_API


def cmd_overview(args: argparse.Namespace) -> int:
    raw = call_plugin(args, "/overview")
    payload = _unwrap(raw)
    if isinstance(payload, dict):
        flat = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if not isinstance(sub_value, (dict, list)):
                        flat[f"{key}.{sub_key}"] = sub_value
            elif not isinstance(value, list):
                flat[key] = value
        human = render_kv(flat)
    else:
        human = str(payload)
    _emit(raw, args, human)
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    params = {
        "page": args.page,
        "page_size": args.size,
        "keyword": args.keyword,
        "statuses": args.status,
        "sources": args.source,
    }
    raw = call_plugin(args, "/page_data", params=params)
    payload = _unwrap(raw)
    records = []
    if isinstance(payload, dict):
        records = payload.get("records") or payload.get("items") or payload.get("list") or []
    rows = []
    for item in records:
        rows.append([
            str(item.get("created_at") or item.get("time") or "")[:19],
            str(item.get("title") or item.get("name") or ""),
            str(item.get("episode") or item.get("episodes") or ""),
            str(item.get("status") or ""),
            str(item.get("source") or ""),
        ])
    total = payload.get("total") if isinstance(payload, dict) else None
    human = render_table(["时间", "标题", "集", "状态", "来源"], rows)
    if total is not None:
        human += f"\n\n第 {args.page} 页，共 {total} 条"
    _emit(raw, args, human)
    return EXIT_OK


def cmd_progress(args: argparse.Namespace) -> int:
    raw = call_plugin(args, "/page_data", params={"page": 1, "page_size": args.size})
    payload = _unwrap(raw)
    cards = []
    if isinstance(payload, dict):
        cards = payload.get("subscribes_progress") or []
    rows = []
    for card in cards:
        missing = card.get("missing_episodes") or card.get("lack_episode") or ""
        if isinstance(missing, list):
            missing = ",".join(str(x) for x in missing)
        rows.append([
            str(card.get("title") or ""),
            f"{card.get('landed_count', card.get('landed', ''))}/{card.get('total_episodes', card.get('total', ''))}",
            str(missing),
            str(card.get("status") or ""),
        ])
    human = render_table(["标题", "已入库/总集", "缺集", "状态"], rows)
    human += f"\n\n共 {len(cards)} 部"
    _emit(raw, args, human)
    return EXIT_OK


def cmd_ledger(args: argparse.Namespace) -> int:
    raw = call_plugin(args, "/ledger/list", params={"limit": args.limit})
    payload = _unwrap(raw)
    items = payload.get("items") if isinstance(payload, dict) else payload
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rows.append([
            str(item.get("created_at") or "")[:19],
            str(item.get("file_name") or item.get("name") or ""),
            str(item.get("state") or item.get("status") or ""),
            str(item.get("attempts") or ""),
            str(item.get("last_error") or "")[:40],
        ])
    human = render_table(["时间", "文件", "状态", "尝试", "最后错误"], rows)
    _emit(raw, args, human)
    return EXIT_OK


def cmd_duplicate(args: argparse.Namespace) -> int:
    raw = call_plugin(args, "/duplicate/list")
    payload = _unwrap(raw)
    groups = payload.get("groups") if isinstance(payload, dict) else payload
    rows = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        ids = group.get("ids") or group.get("subscribe_ids") or []
        rows.append([
            str(group.get("title") or ""),
            str(group.get("tmdb_id") or ""),
            ",".join(str(x) for x in ids) if isinstance(ids, list) else str(ids),
            str(group.get("reason") or ""),
        ])
    human = render_table(["标题", "TMDB", "订阅 ID", "原因"], rows)
    human += f"\n\n共 {len(rows)} 组重复"
    _emit(raw, args, human)
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    action = args.action
    if action == "start":
        body = {"keyword": args.keyword} if args.keyword else {}
        raw = call_plugin(args, "/sync/start", method="POST", body=body)
        payload = _unwrap(raw)
        human = f"已触发同步：{json.dumps(payload, ensure_ascii=False)}"
    elif action == "stop":
        raw = call_plugin(args, "/sync/stop", method="POST", body={})
        payload = _unwrap(raw)
        human = f"已请求停止：{json.dumps(payload, ensure_ascii=False)}"
    elif action == "pause":
        if args.value not in ("on", "off"):
            raise CliError("pause 需要 on 或 off", EXIT_ARGS)
        raw = call_plugin(
            args, "/sync/pause", method="POST", body={"paused": args.value == "on"},
        )
        payload = _unwrap(raw)
        human = f"同步暂停已{'开启' if args.value == 'on' else '关闭'}"
    else:
        raise CliError(f"未知动作：{action}", EXIT_ARGS)
    _emit(raw, args, human)
    return EXIT_OK


def cmd_raw(args: argparse.Namespace) -> int:
    params = {}
    for pair in args.param or []:
        if "=" not in pair:
            raise CliError(f"--param 需要 key=value 形式：{pair}", EXIT_ARGS)
        key, value = pair.split("=", 1)
        params[key] = value
    body = None
    if args.body:
        try:
            body = json.loads(args.body)
        except ValueError as error:
            raise CliError(f"--body 不是合法 JSON：{error}", EXIT_ARGS)
    payload = call_plugin(args, args.path, method=args.method.upper(), params=params, body=body)
    _emit(payload, args, json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------- 参数解析


def _add_common_flags(target: argparse.ArgumentParser, suppress: bool = False) -> None:
    """主命令与每个子命令共享同一批全局参数（子命令后面也能写 --json）。

    子命令侧用 SUPPRESS 默认值，避免覆盖主命令已解析的值。
    """
    def default(value):
        return argparse.SUPPRESS if suppress else value

    target.add_argument("--url", default=default(None),
                        help="MoviePilot 地址（默认 $MP_URL 或 http://127.0.0.1:3000）")
    target.add_argument("--token", default=default(None), help="MoviePilot JWT（默认 $MP_TOKEN）")
    target.add_argument("--username", default=default(None), help="登录用户名（默认 $MP_USER）")
    target.add_argument("--password", default=default(None), help="登录密码（默认 $MP_PASSWORD）")
    target.add_argument("--json", action="store_true", default=default(False), help="输出原始 JSON")
    target.add_argument("--quiet", action="store_true", default=default(False),
                        help="只输出核心值，便于脚本取值")
    target.add_argument("--timeout", type=float, default=default(30.0), help="请求超时秒数（默认 30）")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common_flags(common, suppress=True)

    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="CloudSubscribeX 命令行客户端（零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "认证：MP_TOKEN 直给，或 MP_USER/MP_PASSWORD 自动登录并缓存\n"
            "示例：python3 cli.py doctor\n"
            "      python3 cli.py history --size 20 --keyword 牧神\n"
            "      python3 cli.py sync pause on"
        ),
    )
    _add_common_flags(parser)

    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    subparsers.add_parser("doctor", parents=[common], help="一键体检（9 项红绿灯）").set_defaults(func=cmd_doctor)
    subparsers.add_parser("overview", parents=[common], help="仪表盘总览").set_defaults(func=cmd_overview)

    history = subparsers.add_parser("history", parents=[common], help="历史记录")
    history.add_argument("--page", type=int, default=1, help="页码（默认 1）")
    history.add_argument("--size", type=int, default=20, help="每页条数（默认 20）")
    history.add_argument("--keyword", default="", help="标题关键字")
    history.add_argument("--status", default="", help="状态过滤：success/failed/pending")
    history.add_argument("--source", default="", help="来源过滤")
    history.set_defaults(func=cmd_history)

    progress = subparsers.add_parser("progress", parents=[common], help="追更进度表")
    progress.add_argument("--size", type=int, default=200, help="最多展示多少部（默认 200）")
    progress.set_defaults(func=cmd_progress)

    ledger = subparsers.add_parser("ledger", parents=[common], help="同步作业账本")
    ledger.add_argument("--limit", type=int, default=30, help="条数（默认 30）")
    ledger.set_defaults(func=cmd_ledger)

    subparsers.add_parser("duplicate", parents=[common], help="重复订阅检测").set_defaults(func=cmd_duplicate)

    sync = subparsers.add_parser("sync", parents=[common], help="同步控制")
    sync.add_argument("action", choices=["start", "stop", "pause"], help="动作")
    sync.add_argument("value", nargs="?", help="pause 时填 on/off")
    sync.add_argument("--keyword", default="", help="start 时只同步指定关键字")
    sync.set_defaults(func=cmd_sync)

    raw = subparsers.add_parser("raw", parents=[common], help="逃生口：直接调任意插件接口")
    raw.add_argument("method", choices=["GET", "POST", "get", "post"], help="HTTP 方法")
    raw.add_argument("path", help="插件内路径，如 /runtime")
    raw.add_argument("--param", action="append", help="查询参数 key=value，可重复")
    raw.add_argument("--body", help="POST 的 JSON 体")
    raw.set_defaults(func=cmd_raw)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_ARGS
    try:
        return args.func(args)
    except CliError as error:
        print(f"错误：{error}", file=sys.stderr)
        return error.code
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
