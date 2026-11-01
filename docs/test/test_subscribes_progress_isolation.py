"""隔离单测：验证 _build_subscribes_progress 不再因 tmdb_id 提前引用而整表归零。

做法：用 ast 抽出 page.py 里的函数源码，注入假 app.* 模块（假 SessionFactory /
假 Subscribe / 假 HistoryRecord），直接跑真实函数体。
"""
import ast
import contextlib
import logging
import os
import sys
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


class _FakeCol:
    """模拟 SQLAlchemy 列表达式，filter 里只用到 .in_()。"""

    def in_(self, *a, **k):
        return True


class FakeHistory:
    # 类属性模拟 SQLAlchemy 的列对象（query.filter 会访问它们）
    status = _FakeCol()
    episode = _FakeCol()
    subscribe_id = _FakeCol()
    tmdb_id = _FakeCol()
    season = _FakeCol()
    title = _FakeCol()

    def __init__(self, **kw):
        self.episode = kw.get("episode")
        self.subscribe_id = kw.get("subscribe_id")
        self.tmdb_id = kw.get("tmdb_id")
        self.season = kw.get("season")
        self.title = kw.get("title")
        self.status = kw.get("status", "成功")


SUBS = [
    # 牧神记：note 里只有 1-10，历史成功补 11-13
    FakeSubscribe(id=1, name="牧神记", year="2024", season=1, total_episode=13,
                  note=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10], tmdbid=292741, poster="/a.jpg"),
    # 尚公主：缺 21-24
    FakeSubscribe(id=2, name="尚公主", year="2026", season=1, total_episode=24,
                  note=list(range(1, 21)), tmdbid=307478, poster="/b.jpg"),
    # 电影型：无季、总集数 0
    FakeSubscribe(id=3, name="某电影", year="2025", season=None, total_episode=0,
                  note=[], tmdbid=None, poster=""),
]

HIST = [
    FakeHistory(episode=11, subscribe_id=1, tmdb_id=292741, season=1, title="牧神记"),
    FakeHistory(episode=12, subscribe_id=1, tmdb_id=292741, season=1, title="牧神记"),
    FakeHistory(episode=13, subscribe_id=None, tmdb_id=292741, season=1, title="牧神记"),
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
        return FakeQuery(SUBS if model is FakeSubscribe else HIST)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_stubs():
    app = types.ModuleType("app")
    db = types.ModuleType("app.db")
    db.SessionFactory = FakeSession
    models = types.ModuleType("app.db.models")
    sub_mod = types.ModuleType("app.db.models.subscribe")
    sub_mod.Subscribe = FakeSubscribe
    oper_mod = types.ModuleType("app.db.subscribe_oper")
    oper_mod.SubscribeOper = lambda: types.SimpleNamespace(list=lambda: [])

    pkg_chain = "app.plugins.cloudsubscribex.core.database.models"
    hmod = types.ModuleType(pkg_chain)
    hmod.HistoryRecord = FakeHistory

    mods = {
        "app": app,
        "app.db": db,
        "app.db.models": models,
        "app.db.models.subscribe": sub_mod,
        "app.db.subscribe_oper": oper_mod,
    }
    # 逐级补全父包，否则 import app.plugins.xxx 会失败并回退到空列表
    parts = pkg_chain.split(".")
    for i in range(1, len(parts)):
        pkg_name = ".".join(parts[:i])
        mods.setdefault(pkg_name, types.ModuleType(pkg_name))
    mods[pkg_chain] = hmod

    for name, mod in mods.items():
        sys.modules[name] = mod
    db.models = models
    models.subscribe = sub_mod


def load_function():
    tree = ast.parse(open(SRC_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_build_subscribes_progress":
            fn_src = ast.get_source_segment(open(SRC_PATH, encoding="utf-8").read(), node)
            import textwrap
            return textwrap.dedent(fn_src)
    raise SystemExit("函数未找到")


def main():
    install_stubs()
    logging.basicConfig(level=logging.DEBUG)
    ns = {
        "logger": logging.getLogger("test"),
        "_platform_image_url": lambda p, s="w500": f"img://{p}",
        "Any": object,
    }
    exec(load_function(), ns)
    holder = types.SimpleNamespace(_build_subscribes_progress=ns["_build_subscribes_progress"], _owner=None)
    result = holder._build_subscribes_progress([])

    assert result, "FAIL: 返回空列表（说明全部订阅条目都异常退出了）"
    by_name = {r["name"]: r for r in result}
    print(f"返回 {len(result)} 条")
    for r in result:
        print(
            f"  {r['name']}: 总={r['total_episodes']} 已入库={r['library_count']}"
            f" {r['library_episodes']} 缺={r['missing_count']} {r['missing_episodes']}"
        )

    mu = by_name["牧神记"]
    assert mu["library_count"] == 13, f"牧神记应并入历史成功集=13，实得 {mu['library_count']}"
    assert mu["missing_count"] == 0, f"牧神记应无缺集，实得 {mu['missing_episodes']}"

    sg = by_name["尚公主"]
    assert sg["missing_episodes"] == [21, 22, 23, 24], f"尚公主应缺 21-24，实得 {sg['missing_episodes']}"

    mv = by_name["某电影"]
    assert mv["total_episodes"] == 0 and mv["missing_count"] == 0

    print("PASS: 所有断言通过（tmdb_id 顺序缺陷已修复）")


if __name__ == "__main__":
    main()
