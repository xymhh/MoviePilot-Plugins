#!/usr/bin/env python3
"""
MoviePilot 补丁：插件依赖安装锁卡死 → 安装请求永久「正在更新」

在容器内执行（宿主 shell 一条命令）：
    docker exec -i moviepilot-v3 python3 - <<'PY'
    ...
    PY

或存盘后：
    python3 /config/mp-patches/apply_uv_lock.py [--dry-run]

它改的是容器内 /app/app/adapters/system/plugin/health.py，三处：
  1) 抢依赖安装锁 `while not _package_install_lock.acquire(blocking=False)` 加 180s 超时，
     超时返回失败而不是无限静默排队；等待期间每 15s 打一条 INFO（原来完全无日志）。
  2) 登记锁持有者（哪个插件的 requirements）+ 看门狗：持有超过 900s 强制释放，
     避免一个卡死的安装线程让后续所有插件安装永久堵死。
  3) `[UV] 尝试使用策略` 由 debug 提到 info，下次能直接看到 uv 是否真的在跑。

幂等；锚点缺失时只打印文件现状、不改文件。改完需 docker restart moviepilot-v3。
"""
import ast
import re
import sys

TARGET = "/app/app/adapters/system/plugin/health.py"
MARK = "MoviePilot 补丁(3/4)"

PROBE_OLD = re.compile(
    r"(?P<ind>[ \t]*)while not cls\._package_install_lock\.acquire\(blocking=False\):\n"
    r"(?P=ind)[ \t]+await asyncio\.sleep\(0\.01\)\n"
    r"(?P=ind)acquired = True\n"
)

PROBE_NEW = """{ind}# MoviePilot 补丁(3/4)：原实现是无超时忙等——持锁线程一旦卡死（uv 子进程
{ind}# 不可中断、线程池被占满等），后续所有插件的依赖安装都会永久静默排队，
{ind}# 前端只显示「正在更新」，日志零输出。这里加超时 + 持有者可见性。
{ind}lock_deadline = asyncio.get_running_loop().time() + 180.0
{ind}while not cls._package_install_lock.acquire(blocking=False):
{ind}    if asyncio.get_running_loop().time() >= lock_deadline:
{ind}        message = (
{ind}            "等待其他插件依赖安装锁超过 180s"
{ind}            f"（{{cls.__lock_holder_desc()}}），本次安装放弃，请稍后重试"
{ind}        )
{ind}        logger.error(f"[UV] {{message}}")
{ind}        return False, message
{ind}    await asyncio.sleep(0.05)
{ind}    waited = getattr(cls, "_package_install_lock_waited", 0.0) + 0.05
{ind}    cls._package_install_lock_waited = waited
{ind}    if int(waited) and int(waited) % 15 == 0 and abs(waited % 15) < 0.051:
{ind}        logger.info(f"[UV] 等待依赖安装锁已 {{int(waited)}}s，{{cls.__lock_holder_desc()}}")
{ind}acquired = True
{ind}cls._package_install_lock_waited = 0.0
{ind}cls.__mark_lock_holder(resolved_dependency_files)
"""

ATTR_OLD = "    _package_install_lock = threading.Lock()\n"
ATTR_NEW = """    _package_install_lock = threading.Lock()
    # MoviePilot 补丁(3/4)：锁持有者登记 + 看门狗，卡死时强制放行
    _package_install_lock_holder: str = ""
    _package_install_lock_since: float = 0.0
    _package_install_lock_waited: float = 0.0
    _package_install_lock_watchdog_seconds: float = 900.0
"""

FINALLY_OLD = """        finally:
            if acquired:
                cls._package_install_lock.release()
"""

FINALLY_NEW = """        finally:
            if acquired:
                cls.__clear_lock_holder()
                cls._package_install_lock.release()
"""

HELPERS = '''
    # MoviePilot 补丁(3/4)：依赖安装锁的持有者登记与看门狗
    @classmethod
    def __mark_lock_holder(cls, dependency_files) -> None:
        try:
            names = "、".join(getattr(item, "name", str(item)) for item in dependency_files)
        except Exception:
            names = "未知"
        cls._package_install_lock_holder = names or "未知"
        cls._package_install_lock_since = time.monotonic()
        threading.Thread(
            target=cls.__lock_watchdog,
            args=(cls._package_install_lock_holder,),
            name="uv-lock-watchdog",
            daemon=True,
        ).start()

    @classmethod
    def __lock_holder_desc(cls) -> str:
        if not cls._package_install_lock_holder:
            return "持有者未知，可能是历史卡死的安装线程"
        held = time.monotonic() - cls._package_install_lock_since
        return f"持有者 {cls._package_install_lock_holder}，已持有 {int(held)}s"

    @classmethod
    def __clear_lock_holder(cls) -> None:
        cls._package_install_lock_holder = ""

    @classmethod
    def __lock_watchdog(cls, holder: str) -> None:
        limit = cls._package_install_lock_watchdog_seconds
        if limit <= 0:
            return
        time.sleep(limit)
        if cls._package_install_lock_holder != holder:
            return
        logger.error(
            f"[UV] 依赖安装锁被 {holder} 持有超过 {int(limit)}s，强制释放以免后续插件安装永久阻塞"
        )
        cls._package_install_lock_holder = ""
        try:
            cls._package_install_lock.release()
        except RuntimeError:
            pass

'''

DEBUG_RE = re.compile(r'logger\.debug\(\n([ \t]+)f"\[UV\] 尝试使用策略')


def patch(path: str, dry_run: bool) -> int:
    src = open(path, encoding="utf-8").read()
    if MARK in src:
        print("补丁已存在，跳过")
        return 0

    report = []
    m = PROBE_OLD.search(src)
    if not m:
        print("!! 未找到抢锁锚点，文件未被修改。实际片段：")
        idx = src.find("_package_install_lock.acquire")
        print(repr(src[max(0, idx - 200): idx + 300]) if idx > 0 else "   (完全找不到 acquire 调用)")
        return 2
    new_lock = PROBE_NEW.format(ind=m.group("ind"))
    src = src.replace(m.group(0), new_lock, 1)
    report.append("抢锁加 180s 超时 + 等待日志")

    if ATTR_OLD in src:
        src = src.replace(ATTR_OLD, ATTR_NEW, 1)
        report.append("新增持有者登记属性")
    else:
        print("!! 未找到 _package_install_lock 声明，跳过属性补丁")

    if FINALLY_OLD in src:
        src = src.replace(FINALLY_OLD, FINALLY_NEW, 1)
        report.append("finally 清空持有者")
    else:
        print("!! 未找到 finally 释放块，跳过")

    anchor_match = re.search(
        r"(?m)^(?P<ind>[ \t]*)async def async_install_packages_with_fallback\(",
        src,
    )
    if not anchor_match:
        print("!! 未找到 async_install_packages_with_fallback 定义，跳过 helper")
        return 2
    ind = anchor_match.group("ind")
    helpers = "\n".join(
        ("" if not line.strip() else ind + (line[4:] if line.startswith("    ") else line))
        for line in HELPERS.split("\n")
    )
    pos = anchor_match.start()
    # 若上面紧邻 @classmethod/@staticmethod 装饰器，插到装饰器之前
    head = src[:pos]
    deco = re.search(r"(?m)^(?P<ind2>[ \t]*)@(classmethod|staticmethod)\n\s*$", head)
    insert_at = deco.start() if deco else pos
    src = src[:insert_at] + helpers + "\n" + src[insert_at:]
    report.append("新增持有者/看门狗方法")

    debug_match = DEBUG_RE.search(src)
    if debug_match:
        src = src[:debug_match.start()] + 'logger.info(\n' + debug_match.group(1) + 'f"[UV] 尝试使用策略' + src[debug_match.end():]
        report.append("UV 策略日志 debug→info")

    if "import time" not in src.split("\n\n")[0] and "\nimport time\n" not in src:
        src = re.sub(r"^import threading$", "import threading\nimport time", src, count=1, flags=re.M)
        report.append("补 import time")

    try:
        ast.parse(src)
    except SyntaxError as err:
        print(f"!! 语法校验失败，未写入：{err}")
        return 3

    if dry_run:
        print("DRY-RUN 通过，将改动：", "；".join(report))
        return 0

    open(path, "w", encoding="utf-8").write(src)
    print("已应用：", "；".join(report))
    print("生效需：docker restart moviepilot-v3")
    return 0


if __name__ == "__main__":
    sys.exit(patch(TARGET, "--dry-run" in sys.argv))
