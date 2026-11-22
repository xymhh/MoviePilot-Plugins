"""频道监控"订阅已接管"条目终态回写（Bug 2）。

覆盖 core/tg_monitor.py 的接管分支：
- _shorten_skip_reason：接管文案压成"订阅已接管 · N 个文件等待"，无"…"；
- _resolve_skip_reason：接管文案返回 kind "subscribe_submitted"，其余为 "run"；
- _apply_group_outcome：接管 skipped 写 kind + 短标签，skip_reason 存完整原文；
- apply_finalize_outcome：接管 skipped 成功翻 transferred、失败翻 failed；
  其他 kind 的 skipped、URL/订阅不匹配时一律不动。

加载方式：setUpClass 里懒加载真实模块，加载前对动过的
``cloudsubscribex.*`` 模块与父包属性做快照，tearDownClass 里恢复
（app.*、core.state、core.failure 是全量套件共享的打桩基础设施，不动）。
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import ensure_package, install_app_mocks, load_module  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MISSING = object()
_MODULES = (
    "cloudsubscribex.core.tg_monitor",
)


def _snapshot_modules(names):
    saved_modules = {}
    saved_attrs = {}
    for name in names:
        saved_modules[name] = sys.modules.get(name, _MISSING)
        parent_name, _, child = name.rpartition(".")
        if parent_name and parent_name in sys.modules:
            parent = sys.modules[parent_name]
            saved_attrs[(parent_name, child)] = getattr(parent, child, _MISSING)
    return saved_modules, saved_attrs


def _restore_modules(saved_modules, saved_attrs):
    for name, saved in saved_modules.items():
        if saved is _MISSING:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    for (parent_name, child), saved in saved_attrs.items():
        parent = sys.modules.get(parent_name)
        if parent is None:
            continue
        if saved is _MISSING:
            try:
                delattr(parent, child)
            except AttributeError:
                pass
        else:
            setattr(parent, child, saved)


URL = "https://pan.quark.cn/s/5009594fd63e"
OTHER_URL = "https://pan.quark.cn/s/7598ee5db013"
TAKEOVER_MESSAGE = (
    "订阅搜索已提交网盘任务，其中 1 个网盘文件等待就绪，"
    "后台完成后将自动整理并通知"
)
TAKEOVER_MESSAGE_2 = (
    "订阅搜索已提交网盘任务，其中 2 个网盘文件等待就绪，"
    "后台完成后将自动整理并通知"
)


class FakeOwner:
    """最小插件替身：提供数据目录和配置持久化。"""

    def __init__(self, root):
        self.root = root
        self.persisted = {}

    def get_data_path(self):
        return self.root

    def _persist_config_values(self, **updates):
        self.persisted.update(updates)


def _item(item_id="item-1", **overrides):
    item = {
        "id": item_id,
        "title": "测试剧 S01E01",
        "links": [URL],
        "subscribe_id": 101,
        "status": "skipped",
        "match_status": "skipped",
        "skip_kind": "subscribe_submitted",
        "skip_reason": TAKEOVER_MESSAGE,
        "auto_transfer_error": "未转存：订阅已接管 · 1 个文件等待",
    }
    item.update(overrides)
    return item


class TakeoverMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_modules, cls._saved_attrs = _snapshot_modules(_MODULES)
        install_app_mocks()
        package = ensure_package("cloudsubscribex")
        package.__path__ = [os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex")]
        core = ensure_package("cloudsubscribex.core")
        core.__path__ = [
            os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex", "core")
        ]
        search = ensure_package("cloudsubscribex.search")
        search.__path__ = [
            os.path.join(REPO_ROOT, "plugins.v2", "cloudsubscribex", "search")
        ]
        cls.tg_module = load_module(
            "cloudsubscribex.core.tg_monitor",
            "plugins.v2/cloudsubscribex/core/tg_monitor.py",
        )
        cls.TgChannelMonitor = cls.tg_module.TgChannelMonitor

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules, cls._saved_attrs)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.owner = FakeOwner(Path(self.tempdir.name))
        self.monitor = self.TgChannelMonitor(self.owner)

    def tearDown(self):
        self.tempdir.cleanup()

    def _seed(self, rows):
        self.monitor._store.save(self.tg_module.ITEMS_KEY, rows)

    def _load(self):
        return self.monitor._store.load(self.tg_module.ITEMS_KEY, [])

    def test_shorten_takeover_reason_has_no_ellipsis(self):
        """接管文案压成短句且不截断：1 个和 2 个文件两种写法。"""
        short = self.TgChannelMonitor._shorten_skip_reason(TAKEOVER_MESSAGE)
        self.assertEqual(short, "订阅已接管 · 1 个文件等待")
        self.assertNotIn("…", short)
        self.assertLessEqual(len(short), 26)
        short2 = self.TgChannelMonitor._shorten_skip_reason(TAKEOVER_MESSAGE_2)
        self.assertEqual(short2, "订阅已接管 · 2 个文件等待")

    def test_resolve_skip_reason_takeover_kind(self):
        """接管文案返回新 kind；其他跳过仍是 run。"""
        kind, text = self.monitor._resolve_skip_reason({}, TAKEOVER_MESSAGE)
        self.assertEqual(kind, "subscribe_submitted")
        self.assertEqual(text, "订阅已接管 · 1 个文件等待")
        kind, text = self.monitor._resolve_skip_reason({}, "未发现需要转存的新资源")
        self.assertEqual(kind, "run")
        self.assertEqual(text, "本轮无新集")

    def test_apply_group_outcome_takeover_writes_kind_and_full_reason(self):
        """接管 skipped：kind 落 subscribe_submitted，短标签不截断，
        skip_reason 存完整原文（详情页排障用）。"""
        self._seed([_item(
            status="new", match_status="new",
            skip_kind="", skip_reason="", auto_transfer_error="",
        )])
        self.monitor._apply_group_outcome(
            ["item-1"], 101, "skipped", TAKEOVER_MESSAGE, [{}],
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["skip_kind"], "subscribe_submitted")
        self.assertEqual(item["auto_transfer_error"], "未转存：订阅已接管 · 1 个文件等待")
        self.assertEqual(item["skip_reason"], TAKEOVER_MESSAGE)

    def test_apply_group_outcome_other_skip_keeps_run_kind(self):
        """非接管跳过：kind 仍是 run，skip_reason 同样存完整原文。"""
        self._seed([_item(
            status="new", match_status="new",
            skip_kind="", skip_reason="", auto_transfer_error="",
        )])
        self.monitor._apply_group_outcome(
            ["item-1"], 101, "skipped", "未发现需要转存的新资源", [{}],
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["skip_kind"], "run")
        self.assertEqual(item["auto_transfer_error"], "未转存：本轮无新集")
        self.assertEqual(item["skip_reason"], "未发现需要转存的新资源")

    def test_finalize_success_flips_takeover_skipped_to_transferred(self):
        """接管 skipped + 入库成功 → transferred，终态字段齐全。"""
        self._seed([_item()])
        touched = self.monitor.apply_finalize_outcome(URL, 101, success=True)
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "transferred")
        self.assertEqual(item["match_status"], "transferred")
        self.assertEqual(item["auto_transfer_error"], "")
        self.assertEqual(item["failure_kind"], "")
        self.assertEqual(item["failure_stage"], "")
        self.assertEqual(item["retry_count"], 0)
        self.assertEqual(item["finalize_status"], "landed")
        self.assertIn("finalize_completed_at", item)

    def test_finalize_failure_flips_takeover_skipped_to_failed(self):
        """接管 skipped + 后处理失败 → failed（postprocess 阶段）。"""
        self._seed([_item()])
        touched = self.monitor.apply_finalize_outcome(
            URL, 101, success=False, reason="网盘文件定位失败",
        )
        self.assertEqual(touched, 1)
        item = self._load()[0]
        self.assertEqual(item["status"], "failed")
        self.assertEqual(item["match_status"], "failed")
        self.assertEqual(item["failure_kind"], "permanent")
        self.assertEqual(item["failure_stage"], "postprocess")
        self.assertEqual(item["auto_transfer_error"], "订阅转存失败：网盘文件定位失败")
        self.assertEqual(item["finalize_status"], "failed")
        self.assertIn("finalize_failed_at", item)

    def test_finalize_ignores_other_kind_skipped(self):
        """其他 kind 的 skipped：finalize 成功失败都不动。"""
        self._seed([_item(skip_kind="run", skip_reason="未发现需要转存的新资源")])
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 101, success=True), 0
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(
                URL, 101, success=False, reason="网盘文件定位失败",
            ),
            0,
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["skip_kind"], "run")

    def test_finalize_takeover_mismatch_untouched(self):
        """接管 skipped 但 URL 或订阅对不上 → 不写。"""
        self._seed([_item()])
        self.assertEqual(
            self.monitor.apply_finalize_outcome(OTHER_URL, 101, success=True), 0
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 999, success=True), 0
        )
        self.assertEqual(
            self.monitor.apply_finalize_outcome(URL, 999, success=False), 0
        )
        item = self._load()[0]
        self.assertEqual(item["status"], "skipped")
        self.assertNotIn("finalize_status", item)


if __name__ == "__main__":
    unittest.main()
