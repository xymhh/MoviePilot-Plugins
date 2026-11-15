"""1.10.5：总览失败记录走服务端状态查询，含秒传未命中。

根因：总览（PluginWorkbench mode="overview"）只拉历史第 1 页（最新
10 个分组）再在前端按 record.status 过滤，失败分组不在这 10 个里
就永远显示不出——23:34 实测主页“失败记录（可重试）”是空的，而
统计卡写着失败 2 条。

修法：
- 后端：``_filters`` 里「失败」与伪状态「秒传失败」同时勾选时按并集
  查；以前两个条件是 AND 拼在一起，结果恒为空。单选任一时行为不变。
- 前端：总览首屏与点统计卡片时把状态过滤推给服务端（``page_data``
  的 statuses 参数），“失败”同时查「失败」与「秒传失败」（统计卡
  的失败数是两类之和）；总览里 MediaHub 那排订阅口径 chips 与追更
  日历不适用，隐藏；空状态文案按总览过滤走，不再说“请先添加订阅”。

运行：``cd <repo> && python -m pytest tests/test_overview_server_filter_1_10_5.py -q``
"""

import operator
import os
import re
import sys
import types
import unittest
from pathlib import Path

from plugin_env import install_app_mocks, load_module, register_package

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
plugin_dir = os.path.join(plugins_v2_path, "cloudsubscribex")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core",
    "app.core.cache",
    "app.core.config",
    "app.core.metainfo",
    "app.modules",
    "app.modules.themoviedb",
    "app.modules.themoviedb.scraper",
    "app.schemas.types",
    "app.utils",
    "app.utils.string",
)

# MoviePilot 配置替身：真实数值，避免 MagicMock 参与算术。
sys.modules["app.core.config"].settings = types.SimpleNamespace(
    DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp")
)

# 与其它用例共用 ``cloudsubscribex`` 包名，避免同一批 ORM 文件被加载两次
# （两套映射类会触发 SQLAlchemy 的 InternalTraversal 冲突）。
PKG = "cloudsubscribex"
register_package(PKG, paths=[plugin_dir])
register_package(f"{PKG}.core", paths=[os.path.join(plugin_dir, "core")])
register_package(
    f"{PKG}.core.database", paths=[os.path.join(plugin_dir, "core", "database")]
)
register_package(f"{PKG}.drive", paths=[os.path.join(plugin_dir, "drive")])
register_package(f"{PKG}.search", paths=[os.path.join(plugin_dir, "search")])
register_package(f"{PKG}.utils", paths=[os.path.join(plugin_dir, "utils")])


def _restore_real_sqlalchemy() -> None:
    """清理先跑用例塞进 sys.modules 的 sqlalchemy 替身，否则 alembic 导入失败。
    与 tests/test_source_sha1_cache_db.py 同款做法。"""
    stale = sys.modules.get("sqlalchemy")
    if stale is None or getattr(stale, "__spec__", None):
        return
    for name in [n for n in list(sys.modules) if n == "sqlalchemy" or n.startswith("sqlalchemy.")]:
        sys.modules.pop(name, None)


def _load_once(module_name: str, relative_path: str):
    """优先复用已被其它用例加载的同名模块，避免重复映射 ORM 类。"""
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


_db_pkg = sys.modules[f"{PKG}.core.database"]


def _load_history_repository():
    """在用例运行时（非模块导入时）加载数据库层。

    模块导入时加载会在全量跑时造出第二份 sqlalchemy 实例：先跑用例
    的替身被恢复后重导入，之后 ``from sqlalchemy import ...`` 拿到的
    Connection 类与已加载模块的不一致（“got Connection, expected
    Connection”）。运行时加载时优先复用其它用例已加载的同一份模块；
    只有没人加载过时才恢复真实 sqlalchemy 并立即重导入，让数据库层
    与后续导入绑定同一个实例。
    """
    existing = sys.modules.get(f"{PKG}.core.database.repositories")
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing.HistoryRepository
    _restore_real_sqlalchemy()
    import sqlalchemy  # noqa: F401 -- 先占住 sys.modules，再让数据库层绑定它
    manager_module = _load_once(
        f"{PKG}.core.database.manager",
        os.path.join(plugin_dir, "core", "database", "manager.py"),
    )
    _db_pkg.CloudSubscribeDatabaseManager = manager_module.CloudSubscribeDatabaseManager
    repositories_module = _load_once(
        f"{PKG}.core.database.repositories",
        os.path.join(plugin_dir, "core", "database", "repositories.py"),
    )
    _db_pkg.CloudSubscribeRepositories = repositories_module.CloudSubscribeRepositories
    return repositories_module.HistoryRepository


def _params(clause) -> dict:
    """默认方言编译后的绑定参数（不用 sqlalchemy.dialects：全量跑时
    它可能已被其它用例的占位模块盖住，见本文件头注释）。"""
    return dict(clause.compile().params)


def _param_values(clause):
    """把绑定参数展平成字符串集合，便于断言 failure_kind 取值。"""
    values = set()
    for value in _params(clause).values():
        if isinstance(value, (list, tuple, set)):
            values.update(str(v) for v in value)
        else:
            values.add(str(value))
    return values


def _read(rel_path: str) -> str:
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


class TestFailureStatusFiltersUnion(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.HistoryRepository = _load_history_repository()

    def test_failed_only_excludes_checksum_miss(self):
        filters = self.HistoryRepository._filters(statuses=["失败"])
        self.assertEqual(len(filters), 1)
        clause = filters[0]
        # 「失败」单选：status == 失败 AND failure_kind 不在伪状态里。
        self.assertIs(clause.operator, operator.and_)
        values = _param_values(clause)
        self.assertIn("$.failure_kind", values)
        self.assertIn("checksum_miss", values)
        self.assertIn("NOT IN", str(clause))

    def test_checksum_miss_only(self):
        filters = self.HistoryRepository._filters(statuses=["秒传失败"])
        self.assertEqual(len(filters), 1)
        clause = filters[0]
        self.assertIs(clause.operator, operator.and_)
        values = _param_values(clause)
        self.assertIn("checksum_miss", values)
        self.assertIn("$.failure_kind", values)
        self.assertNotIn("NOT IN", str(clause))

    def test_failed_and_checksum_miss_union(self):
        # 1.10.5：同时勾选按并集查；以前是 AND，结果恒为空。
        filters = self.HistoryRepository._filters(statuses=["失败", "秒传失败"])
        self.assertEqual(len(filters), 1)
        clause = filters[0]
        self.assertIs(clause.operator, operator.or_)
        self.assertEqual(len(clause.clauses), 2)
        params = _params(clause)
        flat = _param_values(clause)
        self.assertIn("checksum_miss", flat)
        self.assertIn(["checksum_miss"], list(params.values()))
        self.assertIn("NOT IN", str(clause))

    def test_success_only_unchanged(self):
        filters = self.HistoryRepository._filters(statuses=["成功"])
        self.assertEqual(len(filters), 1)
        values = _param_values(filters[0])
        self.assertIn("成功", values)
        self.assertNotIn("checksum_miss", values)

    def test_no_statuses_no_filter(self):
        self.assertEqual(self.HistoryRepository._filters(statuses=[]), [])


class TestOverviewServerSideFilter(unittest.TestCase):
    def test_stat_select_pushes_filter_to_server(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        handle = re.search(
            r"function handleStatSelect\(stat\) \{(.*?)\n\}",
            source,
            re.S,
        )
        self.assertIsNotNone(handle)
        body = handle.group(1)
        self.assertIn("overviewFilter.value = filter", body)
        self.assertIn("updateHistoryQuery", body)
        self.assertIn("overviewStatusesFor(filter)", body)
        # “失败”要含秒传未命中（统计卡的失败数是两类之和）。
        mapping = re.search(
            r"function overviewStatusesFor\(filter\) \{(.*?)\n\}",
            source,
            re.S,
        )
        self.assertIsNotNone(mapping)
        self.assertIn('if (filter === "失败") return ["失败", "秒传失败"]', mapping.group(1))

    def test_overview_mount_queries_failed_on_server(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        mounted = re.search(
            r"onMounted\(\(\) => \{(.*?)\n\}\)",
            source,
            re.S,
        )
        self.assertIsNotNone(mounted)
        body = mounted.group(1)
        self.assertIn('props.mode === "overview"', body)
        self.assertIn('updateHistoryQuery({statuses: ["失败", "秒传失败"], page: 1})', body)
        self.assertIn("loadPage(false)", body)

    def test_overview_media_hub_hides_subscribe_chips(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/PluginWorkbench.vue"
        )
        mount = re.search(
            r"<MediaHub(.*?) />",
            source,
            re.S,
        )
        self.assertIsNotNone(mount)
        attrs = mount.group(1)
        self.assertIn(':show-filter-chips="false"', attrs)
        self.assertIn(':empty-title="overviewEmptyTitle"', attrs)
        self.assertIn(':empty-hint="overviewEmptyHint"', attrs)
        self.assertIn('暂无失败记录', source)
        self.assertIn('失败的转存会出现在这里，可直接重试', source)


class TestMediaHubOverviewProps(unittest.TestCase):
    def test_filter_chips_gated_by_prop(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn(
            'showFilterChips: { type: Boolean, default: true }', source
        )
        self.assertIn('v-if="showFilterChips"', source)

    def test_release_strip_only_in_subscribe_mode(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn('v-if="isSubscribeMode && hasTvGroups"', source)

    def test_empty_state_uses_props(self):
        source = _read(
            "frontend/cloudsubscribex/src/components/dashboard/MediaHub.vue"
        )
        self.assertIn(
            'emptyTitle: { type: String, default: "暂无匹配的追更订阅" }',
            source,
        )
        self.assertIn("emptyHint", source)
        self.assertIn(
            '{{ searchKeyword.trim() ? "没有匹配的记录" : emptyTitle }}',
            source,
        )


if __name__ == "__main__":
    unittest.main()
