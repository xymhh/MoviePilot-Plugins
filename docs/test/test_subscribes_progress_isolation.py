"""隔离单测：直接跑真实的 `_build_subscribes_progress` 函数体。

做法：用 ast 抽出 page.py 里的函数源码，注入假的 app.* 模块（假 SessionFactory /
假 Subscribe）与假的插件数据仓库（假 history.list_all()），再喂数据跑断言。

覆盖过的真实事故：
1. v1.9.12 `tmdb_id` 在赋值前被引用 → UnboundLocalError 被逐条吞掉 → 整表归零
2. v1.9.13 只读 `tmdbid` 字段，V3 实际存在 `media_id` → 历史合并键 `tmdb:None:1` 全失配
3. v1.9.16 之前用 MoviePilot 的 SessionFactory 查插件私有 SQLite 的 history_records
   → 静默返回空，已入库永远只等于订阅 note
4. 电影订阅 lack_episode 为空 → 从未转存的电影被算成「已全部入库」
"""
import ast
import logging
import os
import sys
import textwrap
import types

SRC_PATH = os.environ.get("CS_PAGE_PY", "plugins.v2/cloudsubscribex/core/api/page.py")


class FakeSubscribe:
    def __init__(self, **kw):
        self.id = kw.get("id")
        self.name = kw.get("name")
        self.year = kw.get("year")
        self.season = kw.get("season")
        self.total_episode = kw.get("total_episode", 0)
        self.lack_episode = kw.get("lack_episode", 0)
        self.note = kw.get("note", [])
        self.tmdbid = kw.get("tmdbid")
        self.state = kw.get("state", "R")
        self.poster = kw.get("poster", "")
        # MoviePilot V3 字段：外部 ID 由 media_source + media_id 承载
        self.media_source = kw.get("media_source")
        self.media_id = kw.get("media_id")
        self.type = kw.get("type")


def hist(episode, tmdb_id, season=1, title="", subscribe_id=None, status="成功"):
    return {
        "episode": episode,
        "tmdb_id": str(tmdb_id) if tmdb_id is not None else "",
        "season": season,
        "title": title,
        "subscribe_id": subscribe_id,
        "status": status,
    }


SUBS = [
    # 牧神记：note 1-10，历史成功补 11-13（历史行 subscribe_id 为空、title 是剧名）
    FakeSubscribe(id=1, name="牧神记", year="2024", season=1, total_episode=13,
                  note=list(range(1, 11)), tmdbid=292741, poster="/a.jpg"),
    # 尚公主：note 1-20，总 24 → 缺 21-24
    FakeSubscribe(id=2, name="尚公主", year="2026", season=1, total_episode=24,
                  note=list(range(1, 21)), tmdbid=307478, poster="/b.jpg"),
    # 电影：从未落地
    FakeSubscribe(id=3, name="某电影", year="2025", season=None, total_episode=None,
                  note=[], media_id="999001", media_source="themoviedb",
                  type="电影", poster=""),
    # 电影：已落地（流水 episode 为空，只能靠 tmdb + status 判定）
    FakeSubscribe(id=5, name="已落地电影", year="2024", season=None, total_episode=None,
                  note=[], media_id="999002", media_source="themoviedb",
                  type="电影", poster=""),
    # V3 真实形态：tmdbid 为空，只有 media_id；note=1..92+97，历史成功 93..106
    FakeSubscribe(id=4, name="沧元图", year="2024", season=1, total_episode=110,
                  note=list(range(1, 93)) + [97],
                  media_source="themoviedb", media_id="236534", poster="/c.jpg"),
]

HISTORY_ROWS = [
    hist(11, 292741, 1, "牧神记"),
    hist(12, 292741, 1, "牧神记"),
    hist(13, 292741, 1, "牧神记"),
    # V3：subscribe_id 为空、title 是文件名，只能靠 tmdb 键命中
    *[hist(ep, 236534, 1, f"沧元图 - S01E{ep} - 第{ep}集.mp4") for ep in range(93, 107)],
    # 失败记录不得计入
    hist(5, 236534, 1, "沧元图 - S01E05.mp4", status="失败"),
    # 电影：无集号
    hist(None, 999002, None, "已落地电影.2024.2160p.mkv"),
]


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class FakeSession:
    def query(self, model):
        return FakeQuery(SUBS)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeHistoryRepository:
    def list_all(self):
        return [dict(row) for row in HISTORY_ROWS]


class FakeStore:
    repositories = types.SimpleNamespace(history=FakeHistoryRepository())

    def initialize(self):
        return None


def install_stubs():
    mods = {
        "app": types.ModuleType("app"),
        "app.db": types.ModuleType("app.db"),
        "app.db.models": types.ModuleType("app.db.models"),
        "app.db.models.subscribe": types.ModuleType("app.db.models.subscribe"),
        "app.db.subscribe_oper": types.ModuleType("app.db.subscribe_oper"),
    }
    mods["app.db"].SessionFactory = FakeSession
    mods["app.db.models.subscribe"].Subscribe = FakeSubscribe
    mods["app.db.subscribe_oper"].SubscribeOper = lambda: types.SimpleNamespace(list=lambda: [])
    for name, mod in mods.items():
        sys.modules[name] = mod


def load_functions(names):
    src = open(SRC_PATH, encoding="utf-8").read()
    tree = ast.parse(src)
    out, found = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in names and node.name not in found:
            out.append(textwrap.dedent(ast.get_source_segment(src, node)))
            found.add(node.name)
    if "_build_subscribes_progress" not in found:
        raise SystemExit("_build_subscribes_progress 未找到")
    return "\n\n".join(out)


def main():
    install_stubs()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    ns = {
        "logger": logging.getLogger("test"),
        "_platform_image_url": lambda p, s="w500": f"img://{p}",
        "Any": object,
    }
    exec(load_functions(("_resolve_subscribe_tmdb_id", "_build_subscribes_progress")), ns)

    holder = types.SimpleNamespace(
        _owner=None,
        _get_data_store=lambda: FakeStore(),
    )
    # SimpleNamespace 不做描述符绑定，必须手动绑 self
    holder._build_subscribes_progress = types.MethodType(
        ns["_build_subscribes_progress"], holder
    )
    result = holder._build_subscribes_progress([])
    assert result, "FAIL: 返回空列表（说明订阅条目全部异常退出）"

    by_name = {r["name"]: r for r in result}
    print(f"返回 {len(result)} 条")
    for r in result:
        print(
            f"  {r['name']}: 总={r['total_episodes']} 已入库={r['library_count']}"
            f" 缺={r['missing_count']} {r['missing_episodes'][:8]}"
        )

    mu = by_name["牧神记"]
    assert mu["library_count"] == 13, f"牧神记应并入历史成功集=13，实得 {mu['library_count']}"
    assert mu["missing_count"] == 0

    sg = by_name["尚公主"]
    assert sg["missing_episodes"] == [21, 22, 23, 24], sg["missing_episodes"]

    mv = by_name["某电影"]
    assert mv["total_episodes"] == 0 and mv["missing_count"] == 1, "未落地电影必须算待补"
    assert mv["media_type"] == "movie" and mv["library_count"] == 0

    landed = by_name["已落地电影"]
    assert landed["missing_count"] == 0 and landed["library_count"] == 1

    cy = by_name["沧元图"]
    assert cy["tmdb_id"] == "236534", f"应从 media_id 解析 tmdb，实得 {cy['tmdb_id']}"
    assert cy["library_count"] == 106, f"应并入历史 93-106 → 106，实得 {cy['library_count']}"
    assert cy["missing_episodes"] == [107, 108, 109, 110], cy["missing_episodes"]

    print("PASS: 全部断言通过（tmdb 顺序 / V3 media_id / 插件私有库历史 / 电影落地判定）")


if __name__ == "__main__":
    main()
