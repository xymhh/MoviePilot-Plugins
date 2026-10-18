#!/bin/sh
# ============================================================================
#  115 上传测速一键脚本  v2
#  用途：测「这台机器 → 115 网盘」的真实上传速度（p115client 单连接分片，
#        也就是 CloudSubscribeX 插件「本地文件 → 115」用的那条通道）。
#  特点：自己找 python 和依赖（临时目录，不动系统）、自己找 115 凭据、
#        测完自己清理（本地文件 + 临时依赖 + 115 上的测试文件）。
#  用法：sh p115_speedtest.sh [测试大小MB，默认100]
#  环境：SKIP_INSTALL=1 跳过装依赖 | COOKIE='UID=..; CID=..' 直接给凭据
#        MAX_SECONDS=300 超时上限 | PIP_INDEX=https://... 指定 pip 源
#  提示：在 MoviePilot 容器里跑最省事（容器自带 p115client，不用装）。
# ============================================================================
set -u

SIZE_MB="${1:-${SIZE_MB:-100}}"
MAX_SECONDS="${MAX_SECONDS:-300}"
WORK="/tmp/.p115speedtest.$$"
LIBS="$WORK/lib"
PYFILE="$WORK/test.py"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*"; exit 1; }

cleanup() { rm -rf "$WORK" 2>/dev/null; }
trap cleanup EXIT INT TERM

mkdir -p "$WORK" || die "创建临时目录失败：$WORK"

# ---------- 0. 找一个能用的 python（3.8+，优先已经装好 p115client 的） ----------
PY=""
HAVE_LIB=0
for CAND in python3.13 python3.12 python3.11 python3.10 python3.9 python3.8 python3; do
  command -v "$CAND" >/dev/null 2>&1 || continue
  VER="$("$CAND" -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>/dev/null || echo 0)"
  case "$VER" in ''|*[!0-9]*) continue ;; esac
  [ "$VER" -ge 308 ] || { say "   · $CAND 版本太低（$("$CAND" -V 2>&1)），跳过"; continue; }
  if "$CAND" -c 'import p115client' >/dev/null 2>&1; then PY="$CAND"; HAVE_LIB=1; break; fi
  [ -z "$PY" ] && PY="$CAND"
done

if [ -z "$PY" ]; then
  say "✗ 没找到 python 3.12+（p115client 现在要求 >=3.12）。本机情况："
  say "   $(command -v python3 >/dev/null 2>&1 && python3 -V 2>&1 || echo 'python3 都没有')"
  say "   两条路：① 在 MoviePilot 容器终端里跑本脚本（容器自带 python3.11 + p115client）"
  say "           ② 装个新 python：Debian/Ubuntu 用 apt-get install -y python3.11 python3-pip"
  exit 1
fi
say "→ python: $($PY -V 2>&1)  （$PY）"

# ---------- 1. 依赖（能省则省，装也只装到临时目录） ----------
if [ "$HAVE_LIB" = "1" ]; then
  say "   ✓ 本机已有 p115client，跳过安装"
elif [ "${SKIP_INSTALL:-0}" = "1" ]; then
  die "SKIP_INSTALL=1，但 $PY 里没有 p115client"
else
  say "→ 装 p115client（临时目录，不动系统环境）…"
  if ! "$PY" -m pip --version >/dev/null 2>&1; then
    die "$PY 没有 pip。装一下：apt-get install -y python3-pip（或 yum install -y python3-pip）"
  fi
  ERR=""
  OK=0
  for IDX in "${PIP_INDEX:-}" "" "https://pypi.tuna.tsinghua.edu.cn/simple" "https://mirrors.aliyun.com/pypi/simple" "https://mirrors.cloud.tencent.com/pypi/simple"; do
    if [ -n "$IDX" ]; then
      ERR="$("$PY" -m pip install -q --disable-pip-version-check --target "$LIBS" -i "$IDX" --timeout 20 --retries 1 p115client 2>&1 | tail -8)"
    else
      ERR="$("$PY" -m pip install -q --disable-pip-version-check --target "$LIBS" --timeout 20 --retries 1 p115client 2>&1 | tail -8)"
    fi
    if [ -d "$LIBS/p115client" ] || [ -f "$LIBS/p115client.py" ]; then OK=1; break; fi
  done
  if [ "$OK" != "1" ]; then
    say "✗ p115client 装不上，pip 最后报的是："
    [ -n "$ERR" ] && printf '%s\n' "$ERR" | sed 's/^/   | /'
    say ""
    say "   常见原因：本机 python 低于 3.12（p115client 现在所有版本都要求 >=3.12，3.8~3.11 装上也跑不了）"
    say "             ／pip 太老／这台机器连不上 PyPI 和国内镜像。"
    say "   最省事的替代：在 MoviePilot 容器终端里跑 scripts/p115_speedtest_oneline.sh（容器里 p115client 是现成的）。"
    exit 1
  fi
  say "   ✓ 依赖就绪（临时目录，测完删除）"
fi

# ---------- 2. 生成并运行测速程序 ----------
cat > "$PYFILE" <<'PY'
import hashlib, json, os, re, sqlite3, sys, time, urllib.request

SIZE_MB = int(os.environ.get("SIZE_MB", "100"))
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "300"))
WORK = os.environ.get("WORK_DIR", "/tmp")
NAME = "speedtest_cloudsubscribe.bin"
TMP = os.path.join(WORK, NAME)
COOKIE_RE = re.compile(r"UID=\w+;\s*CID=\w+")
CONFIG_DIRS = ["/config", "/app/config",
               "/opt/moviepilot/config", "/opt/moviepilot_v3/config",
               "/root/moviepilot/config", "/opt/1panel/apps/moviepilot/config",
               "/volume1/docker/moviepilot/config",
               "/www/dk_project/dk_app/moviepilot/config"]


def say(text):
    print(text, flush=True)


def pick(value):
    if isinstance(value, str) and COOKIE_RE.search(value) and len(value) < 2000:
        return value.strip()
    return ""


def walk_values(obj):
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        else:
            hit = pick(cur)
            if hit:
                return hit
    return ""


def from_api():
    port = (os.environ.get("MP_PORT") or "3000").strip()
    token = (os.environ.get("API_TOKEN") or "").strip()
    if not token:
        for path in [os.environ.get("MP_ENV", ""), "/config/app.env", "/app/app.env"]:
            if not path:
                continue
            try:
                with open(path, encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        if line.startswith("API_TOKEN="):
                            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            except OSError:
                pass
            if token:
                break
    if not token:
        return ""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/v1/plugin/CloudSubscribeX",
                                     headers={"X-API-KEY": token, "User-Agent": "Mozilla/5.0"})
    try:
        return walk_values(json.loads(urllib.request.urlopen(request, timeout=15).read().decode()))
    except Exception:
        return ""


def from_files():
    dirs = [os.environ.get("MP_CONFIG_DIR", "")] + CONFIG_DIRS
    for root in dirs:
        if not root or not os.path.isdir(root):
            continue
        files = []
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if name.lower().endswith((".json", ".db")):
                    files.append(("cloudsubscribe" not in dirpath.lower(), os.path.join(dirpath, name)))
        for _pri, path in sorted(files):
            try:
                if path.endswith(".json"):
                    hit = walk_values(json.load(open(path, encoding="utf-8", errors="ignore")))
                    if hit:
                        return hit
                    continue
                con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                    try:
                        rows = con.execute(f'SELECT * FROM "{table}"')
                    except sqlite3.Error:
                        continue
                    for row in rows:
                        hit = walk_values(list(row))
                        if hit:
                            return hit
                con.close()
            except Exception:
                continue
    return ""


def find_cookie():
    hit = pick(os.environ.get("COOKIE") or os.environ.get("P115_COOKIE") or "")
    if hit:
        say("凭据来源：环境变量")
        return hit
    hit = from_api()
    if hit:
        say("凭据来源：MP 插件配置接口")
        return hit
    hit = from_files()
    if hit:
        say("凭据来源：MP 配置文件")
        return hit
    if sys.stdin.isatty():
        import getpass
        try:
            hit = pick(getpass.getpass("自动找不到 115 凭据，请粘贴 cookie（不回显，不会上传到任何地方）：") or "")
        except Exception:
            hit = ""
        if hit:
            say("凭据来源：手动输入")
            return hit
    return ""


class Capped:
    """读文件时计时，超过上限就中断上传（避免慢链路上一等等半小时）。"""

    def __init__(self, fh, deadline):
        self.fh, self.deadline, self.read_bytes = fh, deadline, 0

    def read(self, size=-1):
        if time.time() > self.deadline:
            raise TimeoutError(f"超过 {MAX_SECONDS:g} 秒上限，已中止")
        data = self.fh.read(size)
        self.read_bytes += len(data)
        return data

    def seek(self, *args):
        return self.fh.seek(*args)

    def tell(self):
        return self.fh.tell()


def verdict(mbps):
    if mbps >= 5:
        return "很快：机器→115 通道没问题"
    if mbps >= 1:
        return "正常：大致就是这台机器的上行带宽水平"
    if mbps >= 0.1:
        return "偏慢：115 对这条链路的上传不太给力"
    return "很慢：115 对这个连接明显限速（插件日志里的几十 KB/s 就是这一档）"


def main():
    cookie = find_cookie()
    if not cookie:
        say("✗ 没找到 115 凭据。三种给法：")
        say("   1) COOKIE='UID=...; CID=...; SEID=...' sh p115_speedtest.sh")
        say("   2) 在 MP 容器里跑本脚本（能直接读 /config）")
        say("   3) API_TOKEN=xxx sh p115_speedtest.sh（在 MP 所在机器上）")
        return 1
    say(f"凭据长度 {len(cookie)} 字符（内容不显示）")

    from p115client import P115Client
    client = P115Client(cookie)

    say(f"→ 造 {SIZE_MB}MB 随机测试文件（随机内容 → 不会命中秒传）…")
    with open(TMP, "wb") as fh:
        block = os.urandom(1024 * 1024)
        for _ in range(SIZE_MB):
            fh.write(block)
    size = os.path.getsize(TMP)
    digest = hashlib.sha1()
    with open(TMP, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(chunk)

    pid = 0
    try:
        created = client.fs_mkdir("/临时测速")
        pid = int(created.get("cid") or created.get("id") or 0) if isinstance(created, dict) else 0
        say("→ 上传到 115 的 /临时测速 …")
    except Exception as exc:
        say(f"→ 建目录失败（{exc}），改传根目录 …")

    reader = None
    result, note = None, ""
    started = time.time()
    calls = [
        dict(pid=pid, filename=NAME, filesha1=digest.hexdigest().upper(), filesize=size,
             partsize=16 * 1024 * 1024, max_retries=0),
        dict(pid=pid, filename=NAME, filesha1=digest.hexdigest().upper(), filesize=size),
        dict(pid=pid, filename=NAME),
    ]
    for kwargs in calls:
        try:
            reader = Capped(open(TMP, "rb"), started + MAX_SECONDS)
            result = client.upload_file(reader, **kwargs)
            note = "完整上传"
            break
        except TypeError as exc:      # 不同版本 p115client 参数名不同，降级重试
            note = f"参数不匹配（{exc}）"
            if reader:
                try:
                    reader.fh.close()
                except Exception:
                    pass
            continue
        except Exception as exc:      # 网络/超时/115 报错
            note = f"已中止（{exc}）"
            break
    elapsed = max(time.time() - started, 0.001)
    uploaded = getattr(reader, "read_bytes", 0) or (size if result else 0)
    if reader:
        try:
            reader.fh.close()
        except Exception:
            pass
    if not uploaded:
        uploaded = size

    mbps = uploaded / elapsed / 1048576
    say("")
    say("================ 结果 ================")
    say(f"上传字节 : {uploaded / 1048576:.1f} MB / 用时 {elapsed:.1f} s（{note}）")
    say(f"上传速度 : {mbps:.2f} MB/s（{mbps * 8:.1f} Mbps）")
    say(f"判读     : {verdict(mbps)}")
    say("======================================")

    file_id = ""
    if isinstance(result, dict):
        for key in ("file_id", "fid", "id", "cid"):
            if result.get(key):
                file_id = str(result[key])
                break
    if file_id:
        try:
            client.fs_delete([file_id])
            say("✓ 已删除 115 上的测试文件")
        except Exception as exc:
            say(f"! 115 上的测试文件没删掉（{exc}），请手动删 {NAME}")
    elif note.startswith("已中止"):
        say("! 上传被中止，115 上可能残留半截临时分片（115 一般会自动丢弃，可在 /临时测速 里看一眼）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

say ""
PYTHONPATH="$LIBS${PYTHONPATH:+:$PYTHONPATH}" SIZE_MB="$SIZE_MB" MAX_SECONDS="$MAX_SECONDS" \
  WORK_DIR="$WORK" "$PY" "$PYFILE"
CODE=$?
if [ "$CODE" != "0" ]; then
  die "测速没跑完（退出码 $CODE）。若是凭据问题，按上面提示换一种给法再试。"
fi

# 3. 收尾（临时目录由 trap 清理）
say ""
say "✓ 本脚本已清理：本地测试文件、临时依赖目录（$WORK）"
say "  说明：脚本本身没写系统目录，没装任何系统包，可随时删除。"
