"""1.12.17：数据键治理回归测试（1.12.14 事故的护栏）。

背景：v1.12.14 在 ``init_plugin`` 启动路径上调 ``save_data`` 写了一个
没在 ``CloudSubscribeDataStore`` 声明的键，``save()`` 抛 ValueError、
插件加载失败、MP 回滚更新。同一类错误还藏在 ``legacy_id_migration``
里（已修）。本文件盯住两件事：

1. 源码里每处 ``save_data`` / ``_save_data`` 写操作的第一实参，要么是
   已声明的字面量键，要么是已知能解析成已声明键的动态模式；新键必须
   先在 ``CloudSubscribeDataStore`` 声明（键清单见 storage.py 的
   类注释），否则测试失败。
2. 真实存储上的迁移回归：旧插件数据迁移走真实
   ``CloudSubscribeDataStore`` 不抛错、留痕键可写、第二次迁移靠留痕
   提前返回（而不是像 1.12.14 之前那样 ``get_data`` 永远返回 None）。
"""
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from plugin_env import (
    install_app_mocks,
    load_module,
    register_module,
    register_package,
)

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

sys.modules["app.core.config"].settings = types.SimpleNamespace(
    DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp")
)

PKG = "cloudsubscribex"
register_package(PKG, paths=[plugin_dir])
register_package(f"{PKG}.core", paths=[os.path.join(plugin_dir, "core")])
register_package(
    f"{PKG}.core.database", paths=[os.path.join(plugin_dir, "core", "database")]
)
register_package(f"{PKG}.drive", paths=[os.path.join(plugin_dir, "drive")])
register_package(f"{PKG}.search", paths=[os.path.join(plugin_dir, "search")])
register_package(f"{PKG}.utils", paths=[os.path.join(plugin_dir, "utils")])
register_package(
    f"{PKG}.core.services", paths=[os.path.join(plugin_dir, "core", "services")]
)


def _load_once(module_name: str, relative_path: str):
    existing = sys.modules.get(module_name)
    if isinstance(existing, types.ModuleType) and getattr(existing, "__spec__", None):
        return existing
    return load_module(module_name, relative_path)


# storage.py 只在独立名字下加载一次（_GOVERNANCE_MODULE），不进
# sys.modules["cloudsubscribex.core.storage"]：它的顶层
# ``from .database import ...`` 会拉起 sqlalchemy，在全量套件里和后
# 加载的真实 sqlalchemy 混成两个身份（建表时报 "got Connection,
# expected Connection"）；若用真名加载，绑定了桩仓储的 storage 模块
# 还会被后跑的用例复用（_load_once 按名字返回已加载模块），污染它们
# 的数据库操作。加载期间 database 包临时用桩顶替，加载完立刻恢复
# sys.modules 与 core 包上的 database 属性——桩只在这一瞬间可见。
# handles() 等键路由方法是纯函数，不需要真实仓储；真实存储上的迁移由
# test_state_db_migration.py 的 RuntimeRecordTest 与
# test_legacy_id_migration.py 的迁移测试分别覆盖。
import importlib.util

_GOVERNANCE_MODULE = f"{PKG}.core.storage_governance"
_core_pkg = sys.modules.get(f"{PKG}.core")
_real_db_pkg = sys.modules.get(f"{PKG}.core.database")
_real_db_attr = getattr(_core_pkg, "database", None) if _core_pkg else None
_db_stub = register_module(f"{PKG}.core.database", package=True)
_db_stub.CloudSubscribeDatabaseManager = MagicMock()
_db_stub.CloudSubscribeRepositories = MagicMock()
try:
    _spec = importlib.util.spec_from_file_location(
        _GOVERNANCE_MODULE,
        os.path.join(plugin_dir, "core", "storage.py"),
    )
    storage_module = importlib.util.module_from_spec(_spec)
    storage_module.__package__ = f"{PKG}.core"
    sys.modules[_GOVERNANCE_MODULE] = storage_module
    _spec.loader.exec_module(storage_module)
finally:
    if _real_db_pkg is None:
        sys.modules.pop(f"{PKG}.core.database", None)
    else:
        sys.modules[f"{PKG}.core.database"] = _real_db_pkg
    if _core_pkg is not None:
        if _real_db_attr is None:
            try:
                delattr(_core_pkg, "database")
            except AttributeError:
                pass
        else:
            setattr(_core_pkg, "database", _real_db_attr)

CloudSubscribeDataStore = storage_module.CloudSubscribeDataStore

_LITERAL_RE = re.compile(
    r"""(?:save_data|_save_data)\(\s*(?:"([^"]+)"|'([^']+)')"""
)
# 动态第一实参：标识符/属性链。``(?<!def )`` 排除 def 定义行；
# ``(?![\w.=])`` 既排除 super().save_data(key=key, ...) 的关键字传参，
# 也防止回溯把 "key=" 截成 "ke" 这种假匹配。
_DYNAMIC_RE = re.compile(
    r"""(?<!def )(?:save_data|_save_data)\(\s*([A-Za-z_][\w.]*)(?![\w.=])"""
)

# 已知能解析成已声明键的动态模式 → 代表性实例键（必须能通过 handles）。
DYNAMIC_PATTERNS = {
    # core/services/checkin.py：provider.history_key == f"{key}_checkin_history"
    "provider.history_key": "p115_checkin_history",
    # search/budget.py：PointBudgetLedger.history_key == f"{id}_sub_points_history"
    "self.history_key": "dian115_sub_points_history",
    # core/legacy_migration.py：MARKER_KEY == "legacy_id_migration"
    "self.MARKER_KEY": "legacy_id_migration",
    # handlers/sync/service.py：_OFFLINE_PENDING_KEY == "pending_offline_strm"
    "self._OFFLINE_PENDING_KEY": "pending_offline_strm",
    # core/services/checkin.py：_SCHEDULE_STATE_KEY == "checkin_schedule_state"
    "self._SCHEDULE_STATE_KEY": "checkin_schedule_state",
}
# 唯一允许的裸变量键：旧插件数据迁移逐键搬运（core/legacy_migration.py），
# 每个键单独 try，失败不牵连其余键（test_legacy_id_migration.py 覆盖）。
BARE_KEY_PATTERN = "key"


def _scan_save_calls():
    literals = set()
    dynamics = set()
    for root, _dirs, files in os.walk(plugin_dir):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            text = Path(path).read_text(encoding="utf-8")
            for match in _LITERAL_RE.finditer(text):
                literals.add(match.group(1) or match.group(2))
            for match in _DYNAMIC_RE.finditer(text):
                dynamics.add(match.group(1))
    return literals, dynamics


class DataKeyGovernanceTests(unittest.TestCase):
    def test_literal_save_keys_are_declared(self):
        """源码里字面量写键必须全部已声明；新增字面量键先改
        CloudSubscribeDataStore，否则这里失败。

        v1.13.3：字面量 save_data 调用已全部随 history 写操作删掉
        （失败/待修复改走 retry_index 仓储），字面量集合为空是预期
        状态——本用例改为守“无未声明键”与“无 history 写操作”两条。
        """
        literals, _dynamics = _scan_save_calls()
        self.assertNotIn(
            "history", literals,
            "save_data('history') 必须随 history 表一起删掉，失败/待修复只走重试索引",
        )
        undeclared = sorted(key for key in literals if not CloudSubscribeDataStore.handles(key))
        self.assertEqual(
            undeclared, [],
            f"未声明的数据键被写入（先在 CloudSubscribeDataStore 声明 + 配好分支）：{undeclared}",
        )

    def test_dynamic_save_patterns_resolve_to_declared_keys(self):
        """动态写键必须全部是已知模式，且每个模式都能解析成已声明的键。"""
        _literals, dynamics = _scan_save_calls()
        unknown = sorted(
            pattern
            for pattern in dynamics
            if pattern not in DYNAMIC_PATTERNS and pattern != BARE_KEY_PATTERN
        )
        self.assertEqual(
            unknown, [],
            f"未知的动态写键模式（先确认它解析成已声明键，再登记到 DYNAMIC_PATTERNS）：{unknown}",
        )
        for pattern, example in DYNAMIC_PATTERNS.items():
            self.assertIn(pattern, dynamics, f"已知模式 {pattern} 已不在源码里，登记可以删了")
            self.assertTrue(
                CloudSubscribeDataStore.handles(example),
                f"动态模式 {pattern} 的实例键 {example!r} 未声明",
            )
        self.assertIn(BARE_KEY_PATTERN, dynamics, "旧插件逐键迁移的裸 key 模式丢了？")

    def test_handles_covers_suffix_patterns(self):
        # v1.13.3：history 键已删除（转存历史表随之删掉），不得再声明/读写。
        for key in (
            "pending_offline_strm",
            "checkin_schedule_state",
            "legacy_id_migration",
            "account_info_cache",
            "p115_checkin_history",
            "dian115_sub_points_history",
            "p115_auth_session",
        ):
            self.assertTrue(CloudSubscribeDataStore.handles(key), key)
        for key in ("checkin", "unknown_key", "", "history", "history_backup"):
            self.assertFalse(CloudSubscribeDataStore.handles(key), key)
