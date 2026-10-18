#!/bin/sh
# ============================================================================
#  115 上传测速 —— 任意 VPS 一键版（宿主机 python 老 / 没有 python 都不怕）
#  测的是「这台机器 → 115 网盘」的单连接上传速度，也就是 CloudSubscribeX
#  插件「本地文件 → 115」走的那条通道。
#
#  用法：
#     COOKIE='UID=..; CID=..; SEID=..' sh p115_speedtest_vps.sh [测试大小MB，默认100]
#     不给 COOKIE 会在终端提示粘贴一次（不回显，只在本机用，不落盘）
#
#  为什么哪台 VPS 都能跑：
#     - 本机已有 p115client（比如 MP 容器里）→ 直接用，什么都不装
#     - 否则自动装 uv（无 root，装到 ~/.local/bin），由 uv 拉一份独立 CPython 3.12
#       + p115client 到它自己的缓存里跑，不动系统 python、不动系统包
#     - 先走国内源（清华 PyPI + 南大 GitHub 镜像），失败再回退官方源（国外机器更快）
#  跑完自清理：本地测试文件、115 上的测试文件、uv 缓存（uv 是本次装的连 uv 一起删）
#  环境变量：MAX_SECONDS=300 超时上限 | SIZE_MB 同第 1 个参数 | KEEP_UV=1 保留 uv
#
#  一行版（从 Gist/仓库直接跑）：
#     curl -sSL <gist_raw_url> | COOKIE='UID=..; CID=..; SEID=..' sh -s 100
#     （管道方式没法交互输入 cookie，必须用 COOKIE= 环境变量）
#
#  国内机器下载不了 raw.githubusercontent.com 时，换成任意一个代理前缀（都已实测可取到本脚本）：
#     https://ghproxy.net/https://raw.githubusercontent.com/xymhh/p115-speedtest/main/p115_speedtest_vps.sh
#     https://ghfast.top/https://raw.githubusercontent.com/xymhh/p115-speedtest/main/p115_speedtest_vps.sh
#     https://gh-proxy.com/https://raw.githubusercontent.com/xymhh/p115-speedtest/main/p115_speedtest_vps.sh
#     https://cdn.jsdelivr.net/gh/xymhh/p115-speedtest@main/p115_speedtest_vps.sh
# ============================================================================
set -u

SIZE_MB="${1:-${SIZE_MB:-100}}"
MAX_SECONDS="${MAX_SECONDS:-300}"
WORK="$(mktemp -d /tmp/.p115speed.XXXXXX)" || { echo "✗ 建临时目录失败"; exit 1; }
PYFILE="$WORK/test.py"
TSINGHUA="https://pypi.tuna.tsinghua.edu.cn/simple"
NJU_MIRROR="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"

say() { printf '%s\n' "$*"; }
die() { printf '✗ %s\n' "$*"; exit 1; }

UV_ADDED=0
cleanup() {
  rm -rf "$WORK" 2>/dev/null
  if [ "$UV_ADDED" = "1" ] && [ "${KEEP_UV:-0}" != "1" ]; then
    rm -f "$HOME/.local/bin/uv" 2>/dev/null
    rm -rf "$HOME/.cache/uv" 2>/dev/null
    rm -rf "$HOME/.local/share/uv" 2>/dev/null
    printf '%s\n' "✓ 顺手清理：uv 二进制 + uv 缓存 + uv 下的 python（都是本次装的）"
  fi
}
trap cleanup EXIT INT TERM

# ---------- 1. 115 凭据 ----------
COOKIE="${COOKIE:-${P115_COOKIE:-}}"
if [ -z "$COOKIE" ] && [ -t 0 ]; then
  printf '把 115 cookie 粘进来（UID=..; CID=..; SEID=..，不回显，回车结束）: '
  stty -echo 2>/dev/null
  read -r COOKIE
  stty echo 2>/dev/null
  printf '\n'
fi
[ -n "$COOKIE" ] || die "没给 cookie。用法：COOKIE='UID=..; CID=..; SEID=..' sh $0 [大小MB]"

# ---------- 2. 写测速程序 ----------
cat > "$PYFILE" <<'PY'
import hashlib, json, os, re, sqlite3, socket, sys, time

SIZE_MB = int(os.environ.get("SIZE_MB", "100"))
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "300"))
WORK = os.environ.get("WORK_DIR", "/tmp")
NAME = "speedtest_cloudsubscribe.bin"
TMP = os.path.join(WORK, NAME)
COOKIE_RE = re.compile(r"UID=\w+;\s*CID=\w+")
CONFIG_DIRS = ["/config", "/app/config"]


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


def from_files():
    for root in CONFIG_DIRS + [os.environ.get("MP_CONFIG_DIR", "")]:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                path = os.path.join(dirpath, name)
                try:
                    if name.endswith(".json"):
                        hit = walk_values(json.load(open(path, encoding="utf-8", errors="ignore")))
                        if hit:
                            return hit
                    elif name.endswith(".db"):
                        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                            for row in con.execute(f'SELECT * FROM "{table}"'):
                                hit = walk_values(list(row))
                                if hit:
                                    return hit
                        con.close()
                except Exception:
                    continue
    return ""


class Capped:
    """读文件时计时，超过上限就中断上传（慢链路不必等半小时）。"""

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
        return "偏慢：115 对这条链路不太给力"
    return "很慢：115 对这个连接明显限速（插件日志里几十 KB/s 就是这档）"


def main():
    cookie = pick(os.environ.get("COOKIE") or "") or from_files()
    if not cookie:
        say("✗ 没找到 115 凭据（用 COOKIE='UID=..; CID=..; SEID=..' 再跑一次）")
        return 1
    say(f"✓ 凭据就绪：{len(cookie)} 字符（内容不显示）")

    try:
        from p115client import P115Client
    except Exception as exc:
        say(f"✗ 导入 p115client 失败：{exc}")
        return 1
    try:
        client = P115Client(cookie)
    except Exception as exc:
        say(f"✗ 115 客户端初始化失败（cookie 过期或格式不对？）：{exc}")
        return 1

    say(f"→ 造 {SIZE_MB}MB 随机数据（随机内容 → 不会命中秒传，真跑网络）…")
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
    except Exception as exc:
        say(f"→ 建 /临时测速 失败（{exc}），改传根目录")

    say("→ 上传到 115 …")
    reader, result, note, err = None, None, "", None
    started = time.time()
    for kwargs in (dict(pid=pid, filename=NAME, filesha1=digest.hexdigest().upper(), filesize=size,
                        partsize=16 * 1024 * 1024, max_retries=0),
                   dict(pid=pid, filename=NAME, filesha1=digest.hexdigest().upper(), filesize=size),
                   dict(pid=pid, filename=NAME)):
        try:
            reader = Capped(open(TMP, "rb"), started + MAX_SECONDS)
            result = client.upload_file(reader, **kwargs)
            note = "完整上传"
            break
        except TypeError as exc:          # p115client 版本间参数名有差异，降级重试
            note = f"参数不匹配（{exc}）"
            if reader:
                try:
                    reader.fh.close()
                except Exception:
                    pass
            continue
        except Exception as exc:          # 网络 / 超时 / 115 报错
            note = f"已中止（{exc}）"
            err = exc
            break
    elapsed = max(time.time() - started, 0.001)
    uploaded = getattr(reader, "read_bytes", 0) or (size if result else 0) or size
    if reader:
        try:
            reader.fh.close()
        except Exception:
            pass

    timed_out = isinstance(err, TimeoutError)
    if result is None and not timed_out and uploaded < 4 * 1048576:
        message = str(err) if err is not None else note
        say("")
        say(f"✗ 上传没成功：{message}")
        if "登录" in message or "990001" in message or "未登录" in message:
            say("  → cookie 失效或不对：重新取一份 UID/CID/SEID 再跑")
        try:
            os.remove(TMP)
        except Exception:
            pass
        return 1

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
        say("! 上传被中止，115 上可能残留半截临时分片（115 一般会自动丢弃）")

    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    say(f"RESULT host={host} size={uploaded / 1048576:.1f}MB time={elapsed:.1f}s speed={mbps:.2f}MB/s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY

# ---------- 3. 挑运行方式并执行 ----------
PY=""
for CAND in python3.13 python3.12 python3 python; do
  command -v "$CAND" >/dev/null 2>&1 || continue
  if "$CAND" -c 'import p115client' >/dev/null 2>&1; then PY="$CAND"; break; fi
done

run_local() { SIZE_MB="$SIZE_MB" MAX_SECONDS="$MAX_SECONDS" WORK_DIR="$WORK" COOKIE="$COOKIE" "$@"; }

if [ -n "$PY" ]; then
  say "→ 本机已有 p115client（$PY），直接用，不装任何东西"
  run_local "$PY" -W ignore "$PYFILE"
  CODE=$?
else
  say "→ 本机 python 装不了 p115client（它要求 >=3.12），用 uv 拉一份独立的 3.12"
  UV=""
  if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"
  elif [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"
  else
    say "  · 装 uv（无 root，装到 ~/.local/bin）…"
    INSTALLER="$WORK/uv-install.sh"
    for BASE in "" "https://ghproxy.net/https://github.com"; do
      if curl -LsSf --max-time 90 https://astral.sh/uv/install.sh -o "$INSTALLER" 2>/dev/null && [ -s "$INSTALLER" ]; then
        if env UV_INSTALLER_GITHUB_BASE_URL="$BASE" sh "$INSTALLER" >/dev/null 2>&1 && [ -x "$HOME/.local/bin/uv" ]; then
          UV="$HOME/.local/bin/uv"; UV_ADDED=1; break
        fi
      fi
    done
    if [ -z "$UV" ]; then
      for IDX in "$TSINGHUA" ""; do
        if [ -n "$IDX" ]; then
          python3 -m pip install -q --user -i "$IDX" uv >/dev/null 2>&1
        else
          python3 -m pip install -q --user uv >/dev/null 2>&1
        fi
        if [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; UV_ADDED=1; break; fi
      done
    fi
  fi
  [ -n "$UV" ] || die "uv 装不上（连不上 astral.sh，也没 pip）。可手动装 uv 再重跑本脚本。"
  say "  ✓ uv $("$UV" --version 2>&1 | head -1)"

  OK=0
  for TRY in cn official; do
    if [ "$TRY" = "cn" ]; then
      say "  · 拉 CPython 3.12 + p115client（国内源：清华 PyPI + 南大镜像）…"
      if env UV_PYTHON_PREFERENCE=only-managed UV_DEFAULT_INDEX="$TSINGHUA" UV_PYTHON_INSTALL_MIRROR="$NJU_MIRROR" \
         "$UV" run --no-project --quiet --python 3.12 --with p115client true >/dev/null 2>&1; then OK=1; break; fi
    else
      say "  · 国内源没成，改走官方源（PyPI + GitHub）…"
      if "$UV" run --no-project --quiet --python 3.12 --with p115client true >/dev/null 2>&1; then OK=1; break; fi
    fi
  done
  [ "$OK" = "1" ] || die "uv 拉 python 3.12 / p115client 失败（网络受限？）"

  say "  ✓ 环境就绪，开始测速"
  if [ "$TRY" = "cn" ]; then
    run_local env UV_PYTHON_PREFERENCE=only-managed UV_DEFAULT_INDEX="$TSINGHUA" UV_PYTHON_INSTALL_MIRROR="$NJU_MIRROR" \
      "$UV" run --no-project --quiet --python 3.12 --with p115client python -W ignore "$PYFILE"
  else
    run_local "$UV" run --no-project --quiet --python 3.12 --with p115client python -W ignore "$PYFILE"
  fi
  CODE=$?
fi
[ "$CODE" = "0" ] || die "测速没跑完（退出码 $CODE）"

say ""
say "✓ 清理完成：本地测试文件、115 上的测试文件、临时目录"
