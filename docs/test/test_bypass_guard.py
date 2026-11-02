"""旁路解耦隔离单测：熔断行为 + 真实接入点降级不外抛。

做法：
1. `core/bypass_guard.py` 用 importlib 直接按文件加载（只依赖 app.log，已 stub）；
2. 接入点方法（finish_notification_batch / refresh_media_servers / rebuild_strm_files）
   与 Doctor 的 `_check_bypass` 用 AST 从真实源码抽取后执行，不 import 整个插件包。

运行：python3 docs/test/test_bypass_guard.py
"""

import ast
import importlib.util
import sys
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins.v2" / "cloudsubscribex"
SYNC_PY = PLUGIN / "handlers" / "sync" / "service.py"
MEDIA_REFRESH_PY = PLUGIN / "handlers" / "sync" / "media_refresh.py"
STRM_OPS_PY = PLUGIN / "handlers" / "sync" / "strm_ops.py"
DOCTOR_PY = PLUGIN / "core" / "api" / "doctor.py"
GUARD_PY = PLUGIN / "core" / "bypass_guard.py"

# ------------------------------------------------------------------ stub app.log
app_mod = types.ModuleType("app")
log_mod = types.ModuleType("app.log")
log_mod.logger = types.SimpleNamespace(
    debug=lambda *a, **k: None, info=lambda *a, **k: None,
    warning=lambda *a, **k: None, error=lambda *a, **k: None,
)
app_mod.log = log_mod
sys.modules.setdefault("app", app_mod)
sys.modules.setdefault("app.log", log_mod)

# ------------------------------------------------------------------ 加载 guard
spec = importlib.util.spec_from_file_location("cs_bypass_guard", GUARD_PY)
guard_mod = importlib.util.module_from_spec(spec)
sys.modules["cs_bypass_guard"] = guard_mod
spec.loader.exec_module(guard_mod)
BypassGuard = guard_mod.BypassGuard
bypass_guard = guard_mod.bypass_guard


def extract(*paths: Path, names) -> dict:
    """从若干源码文件抽取类方法/函数，返回 {name: code}（拆分后方法可能已搬家）。"""
    found: dict = {}
    for path in paths:
        for name, code in _extract_one(path, names - set(found)).items():
            found[name] = code
    missing = set(names) - set(found)
    if missing:
        raise AssertionError(f"源码里找不到：{sorted(missing)}")
    return found


def _extract_one(path: Path, names) -> dict:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            found[node.name] = textwrap.dedent(ast.get_source_segment(src, node))
    return found


PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (f"  <{extra}>" if extra and not cond else ""))


# ---------------------------------------------------------------- 1. 熔断阈值
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


clock = Clock()
guard = BypassGuard(threshold=3, base_cooldown=600, max_cooldown=3600, clock=clock)


def boom():
    raise RuntimeError("emby down")


results = [guard.call("emby_test", boom, fallback="x") for _ in range(3)]
check("1.1 三次失败都降级返回 fallback", results == ["x", "x", "x"], str(results))
check("1.2 达到阈值后熔断", guard.allow("emby_test") is False)
check("1.3 熔断后不再执行被包裹函数", guard.call("emby_test", boom, fallback="y") == "y")
check("1.4 快照标记 cooling",
      any(r["state"] == "cooling" for r in guard.snapshot()))

# ---------------------------------------------------------------- 2. 冷却恢复
clock.now += 601
check("2.1 冷却到期自动放行", guard.allow("emby_test") is True)
guard.call("emby_test", lambda: "ok")
check("2.2 成功一次即复位",
      all(r["state"] == "ok" for r in guard.snapshot() if r["key"] == "emby_test"))

# ---------------------------------------------------------------- 3. 指数退避
guard2 = BypassGuard(threshold=3, base_cooldown=600, max_cooldown=3600, clock=clock)
for _ in range(3):
    guard2.call("x", boom, fallback=None)
first = next(r for r in guard2.snapshot() if r["key"] == "x")["remaining_seconds"]
clock.now += first + 1
for _ in range(3):
    guard2.call("x", boom, fallback=None)
second = next(r for r in guard2.snapshot() if r["key"] == "x")["remaining_seconds"]
check("3.1 再次熔断冷却翻倍", second > first, f"{first} -> {second}")

# ---------------------------------------------------------------- 4. 中断透传
try:
    guard.call("interrupt", lambda: (_ for _ in ()).throw(InterruptedError("stop")))
    check("4.1 InterruptedError 透传", False)
except InterruptedError:
    check("4.1 InterruptedError 透传", True)

# ---------------------------------------------------------------- 5. 真实接入点
codes = extract(SYNC_PY, MEDIA_REFRESH_PY, STRM_OPS_PY, names={
    "finish_notification_batch", "refresh_media_servers", "rebuild_strm_files",
})
ns = {
    "bypass_guard": bypass_guard,
    "Dict": dict,
    "Any": object,
    "bool": bool,
}
for name, code in codes.items():
    exec(code, ns)

bypass_guard.reset()


class Notifier:
    def __init__(self):
        self.calls = 0

    def finish_task_batch(self):
        self.calls += 1
        raise RuntimeError("webhook 401")


class FakeHandler:
    _STRM_EMPTY_STATS = {
        "directories": 0, "files": 0, "created": 0,
        "updated": 0, "skipped": 0, "errors": 0,
    }

    def __init__(self):
        self._media_server_notifier = Notifier()
        self.refresh_calls = 0
        self.strm_calls = 0

    def _refresh_media_servers_impl(self):
        self.refresh_calls += 1
        raise RuntimeError("emby host unreachable")

    def _rebuild_strm_files_impl(self, overwrite=False, time_budget_seconds=0.0, state_store=None):
        self.strm_calls += 1
        raise RuntimeError("strm dir not writable")

    finish_notification_batch = ns["finish_notification_batch"]
    refresh_media_servers = ns["refresh_media_servers"]
    rebuild_strm_files = ns["rebuild_strm_files"]


handler = FakeHandler()
notify_result = handler.finish_notification_batch()
refresh_result = handler.refresh_media_servers()
strm_result = handler.rebuild_strm_files()

check("5.1 通知异常降级为 False", notify_result is False, str(notify_result))
check("5.2 媒体库刷新异常降级为 {}", refresh_result == {}, str(refresh_result))
check("5.3 STRM 异常降级为全 0", strm_result == FakeHandler._STRM_EMPTY_STATS, str(strm_result))
check("5.4 三个能力都进入熔断",
      {r["key"] for r in bypass_guard.snapshot()} >= {"notify", "media_server", "strm"})

# 熔断后再打 3 轮：不抛异常、且不再真正执行底层实现
for _ in range(3):
    handler.finish_notification_batch()
    handler.refresh_media_servers()
    handler.rebuild_strm_files()
check("5.5 熔断后底层实现不再被调用（只试满 3 次）",
      handler.refresh_calls == 3 and handler.strm_calls == 3
      and handler._media_server_notifier.calls == 3,
      f"refresh={handler.refresh_calls} strm={handler.strm_calls} notify={handler._media_server_notifier.calls}")
check("5.6 连续 7 轮调用方全程无异常", True)

# ---------------------------------------------------------------- 6. Doctor 项
doctor_ns = {
    "bypass_guard": bypass_guard,
    "STATUS_OK": "ok", "STATUS_WARN": "warn", "STATUS_ERROR": "error",
    "Dict": dict, "Any": object, "int": int, "max": max, "len": len, "next": next,
    "CheckResult": tuple,
}
exec(extract(DOCTOR_PY, names={"_check_bypass"})["_check_bypass"], doctor_ns)
_check_bypass = doctor_ns["_check_bypass"]

status, detail, hint, _ = _check_bypass(None, {})
check("6.0 全部熔断时为红灯", status == "error", f"{status} {detail}")

# 部分熔断（另有能力正常）→ 黄灯
bypass_guard.call("checkin_ok_probe", lambda: "ok")
guard_mod.BYPASS_LABELS.setdefault("checkin_ok_probe", "签到探活")
status, detail, hint, _ = _check_bypass(None, {})
check("6.1 部分熔断时 Doctor 为黄灯", status == "warn", f"{status} {detail}")
check("6.2 详情含能力中文名", "媒体库刷新" in detail or "通知" in detail, detail)

bypass_guard.reset()
status, detail, _, _ = _check_bypass(None, {})
check("6.3 无异常时为绿灯", status == "ok", f"{status} {detail}")

# 全挂 → 红灯
guard3 = BypassGuard(threshold=1, clock=Clock())
for key in ("strm", "notify"):
    guard3.call(key, boom, fallback=None)
doctor_ns["bypass_guard"] = guard3
status, detail, _, _ = _check_bypass(None, {})
check("6.4 全部熔断时为红灯", status == "error", f"{status} {detail}")

# ---------------------------------------------------------------- 7. aria2 记账
bypass_guard.reset()
for _ in range(3):
    bypass_guard.note("aria2", RuntimeError("aria2 rpc 连不上"))
check("7.1 aria2 失败 3 次进入冷却",
      next(r for r in bypass_guard.snapshot() if r["key"] == "aria2")["state"] == "cooling")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
