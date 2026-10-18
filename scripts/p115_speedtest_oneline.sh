P=""; for c in python3 python3.12 python3.13 python /opt/venv/bin/python /usr/local/bin/python3; do command -v "$c" >/dev/null 2>&1 && "$c" -c "import p115client" 2>/dev/null && P="$c" && break; done; [ -z "$P" ] && echo "✗ 这台机器没有带 p115client 的 python（p115client 要 python>=3.12，装不了就换 MP 容器终端跑）" || "$P" -c '
import hashlib,json,os,re,sqlite3,time
LIM=float(os.environ.get("MAX_SECONDS","300")); SZ=int(os.environ.get("SIZE_MB","100"))
C=re.compile(r"UID=\w+;\s*CID=\w+")
def g(v): return v.strip() if isinstance(v,str) and C.search(v) and len(v)<2000 else ""
def w(o):
    st=[o]
    while st:
        c=st.pop()
        if isinstance(c,dict): st+=list(c.values())
        elif isinstance(c,list): st+=c
        else:
            h=g(c)
            if h: return h
    return ""
def scan(root):
    for d,_,fs in os.walk(root):
        for n in sorted(fs):
            p=os.path.join(d,n)
            try:
                if n.endswith(".json"): hit=w(json.load(open(p,encoding="utf-8",errors="ignore")))
                elif n.endswith(".db"):
                    hit=""
                    con=sqlite3.connect("file:%s?mode=ro"%p,uri=True)
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type=\x27table\x27"):
                        for r in con.execute("SELECT * FROM \"%s\""%t):
                            hit=w(list(r))
                            if hit: break
                        if hit: break
                    con.close()
                else: hit=""
            except Exception: hit=""
            if hit: return hit
    return ""
root=os.environ.get("MPCFG","/config")
ck=g(os.environ.get("COOKIE","")) or scan(root)
if not ck:
    print("✗ 没在 %s 里找到 115 cookie。改用：COOKIE=\"UID=..; CID=..; SEID=..\" 再跑一次"%root); raise SystemExit(1)
print("✓ 凭据找到：%d 字符（内容不显示）"%len(ck))
from p115client import P115Client
cl=P115Client(ck)
f="/tmp/.115speed.bin"; fh=open(f,"wb")
for i in range(SZ): fh.write(os.urandom(1048576))
fh.close()
h=hashlib.sha1(open(f,"rb").read()).hexdigest().upper(); size=os.path.getsize(f)
pid=0
try:
    r=cl.fs_mkdir("/临时测速"); pid=int((r or {}).get("cid") or 0)
except Exception: pass
print("→ 上传 %d MB 随机数据到 115（不命中秒传，真跑网络）…"%SZ)
class R:
    def __init__(s,fp,dl): s.f=fp; s.dl=dl; s.n=0
    def read(s,z=-1):
        if time.time()>s.dl: raise TimeoutError("超过 %g 秒上限，已中止"%LIM)
        b=s.f.read(z); s.n+=len(b); return b
    def seek(s,*a): return s.f.seek(*a)
    def tell(s): return s.f.tell()
t=time.time(); rd=R(open(f,"rb"),t+LIM); res=None
try:
    res=cl.upload_file(rd,pid=pid,filename="speedtest_cloudsubscribe.bin",filesha1=h,filesize=size)
except TypeError:
    try: res=cl.upload_file(rd,pid=pid,filename="speedtest_cloudsubscribe.bin")
    except Exception as e: print("上传失败：%s"%e)
except Exception as e: print("上传中止：%s"%e)
el=max(time.time()-t,0.001); up=rd.n or (size if res else 0)
m=up/el/1048576
print(""); print("======== 结果 ========")
print("上传 %.1f MB / %.1f 秒 = %.2f MB/s (%.1f Mbps)"%(up/1048576,el,m,m*8))
print("判读：%s"%("很快：机器→115 通道没问题" if m>=5 else "正常：接近这台机器上行带宽" if m>=1 else "偏慢：115 对这条链路不给力" if m>=0.1 else "很慢：115 明显限速（插件日志里几十 KB/s 就是这档）"))
fid=""
if isinstance(res,dict):
    for k in ("file_id","fid","id","cid"):
        if res.get(k): fid=str(res[k]); break
if fid:
    try: cl.fs_delete([fid]); print("✓ 已删除 115 上的测试文件")
    except Exception as e: print("! 115 上测试文件没删掉（%s），请手动删"%e)
try: os.remove(f)
except Exception: pass
print("✓ 本地临时文件已删")
'
