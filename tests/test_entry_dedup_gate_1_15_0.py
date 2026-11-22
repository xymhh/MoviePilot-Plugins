"""1.15.0 Phase 3a 第 2 项：转存入口预检（判重两层中的入口层）。

_transfer_episode_items 入口用 _collect_episode_versions（季目录
列目录 + 基线并集，走 _season_listing_cache）+ _episode_dedup_decision
先筛一遍：决策 skip 的项不下载不转存，返回的 results 里 success=True、
skip_history=True，调用方按已处理计入（不记失败、不重试、不进落盘
自检、不发转存通知）；proceed/replace 的项原样放行。判重关闭、
查不到版本、查库异常时全部放行（fail-open），最终兜底仍是
trigger_mp_organize 内的 MP 查库（第 12 项）。
"""
import unittest
from types import SimpleNamespace

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
)

install_app_mocks(
    "app.core",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
    "app.log",
    "app.schemas",
    "app.schemas.types",
    "app.utils",
    "app.utils.string",
    "app.utils.path",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.drive",
    "cloudsubscribex.drive.scanner",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache",
)
install_core_delegation()
ensure_package("cloudsubscribex.core")

# 注意：不要无条件 load_module transfer_ops——service.py 再导出它的
# _STAGING_REFS，重复加载会 fork 出第二个模块对象，test_component_wiring
# 的同一对象断言就断了。已加载时直接复用，未加载（单跑本文件）时才加载。
import sys

transfer_ops = sys.modules.get("cloudsubscribex.handlers.sync.transfer_ops")
if transfer_ops is None:
    transfer_ops = load_module(
        "cloudsubscribex.handlers.sync.transfer_ops",
        "plugins.v2/cloudsubscribex/handlers/sync/transfer_ops.py",
    )
TransferOpsMixin = transfer_ops.TransferOpsMixin


def _item(file_id, episode, name, score=50, size=1000):
    return {
        "file": {"id": file_id, "name": name, "size": size},
        "episode": episode,
        "score": score,
        "target_name": name,
        "target_dir": "/Alist/影视/moviepilot/媒体库/剧集",
    }


def _attach_fake_owner(service, mapping):
    """Phase 5：裸组件实例配假 owner——字段命名空间把桩方法指回 service 自身的桩。沿用已有的 _owner 命名空间（若有）：__init__ 里普通属性写已被 OwnerDelegator 转发到它身上，换掉会丢属性。"""
    owner = service.__dict__.get("_owner")
    if owner is None or not hasattr(owner, "__dict__"):
        owner = SimpleNamespace()
    for field_name, method_names in mapping.items():
        namespace = SimpleNamespace()
        for method_name in method_names:
            setattr(namespace, method_name, getattr(service, method_name))
        setattr(owner, field_name, namespace)
    object.__setattr__(service, "_owner", owner)
    return owner


class _Service(TransferOpsMixin):
    """只测入口预检与结果拼回，不跑真实转存。"""

    def __init__(self, versions=None, decisions=None, dedup_active=True):
        self._transfer_task_batch_size = 50
        self._batch_interval = 0
        self._versions = versions or {}
        self._decisions = decisions or {}
        self._dedup_active = dedup_active
        self._collect_calls = []
        self._batch_calls = []
        self._verify_calls = []
        self._collect_error = None
        _attach_fake_owner(self, {
            "episode_dedup": [
                "_episode_dedup_active",
                "_collect_episode_versions",
                "_episode_dedup_decision",
            ],
            "metadata": ["_set_task_phase"],
            "landed_verify": ["verify_landed_items"],
        })

    def _episode_dedup_active(self):
        return self._dedup_active

    def _collect_episode_versions(self, subscribe, mediainfo, season, *, force_refresh=False):
        self._collect_calls.append((subscribe, mediainfo, season, force_refresh))
        if self._collect_error is not None:
            raise self._collect_error
        return dict(self._versions)

    def _episode_dedup_decision(
            self, subscribe, mediainfo, season, episode, new_score,
            new_size, existing_versions=None, new_file_name="",
    ):
        return self._decisions.get(
            int(episode), ("proceed", [], "该集无现有版本")
        )

    def _stop_requested(self):
        return False

    def _set_task_phase(self, *args, **kwargs):
        return None

    def _wait_transfer_batch_interval(self):
        return True

    def _transfer_episode_batch(
            self, matched_items, share_url, mediainfo, subscribe,
            season, sub_key, track_subscription=True,
            transient_target=False, media_type="tv",
    ):
        self._batch_calls.append(list(matched_items))
        return [
            {
                "item": item,
                "file_id": str(item["file"]["id"]),
                "success": True,
                "pending_key": "",
                "failure_reason": "",
            }
            for item in matched_items
        ]

    def verify_landed_items(
            self, subscribe, mediainfo, season, checks,
    ):
        # 管线 confirm 阶段的落盘自检：本文件只测入口预检，不跑
        # 真实自检，返回空映射（即全部 landed=None）。
        self._verify_calls.append(checks)
        return {}


def _mediainfo():
    return SimpleNamespace(title="玫瑰战争", tmdb_id=299276)


def _subscribe():
    return SimpleNamespace(name="玫瑰战争", id=1)


class EntryDedupGateTests(unittest.TestCase):
    def test_skip_returns_success_with_skip_history_in_order(self):
        """E24 已有更优版本：跳过（不进批次），E25 正常转存，结果按原序拼回。"""
        service = _Service(
            versions={24: [{"file_id": "old-1", "file_name": "旧版.mp4"}]},
            decisions={24: ("skip", [{"file_id": "old-1"}], "同集已有更优版本")},
        )
        items = [
            _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
        ]
        results = service._transfer_episode_items(
            items, "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 2)
        skipped, transferred = results
        self.assertEqual(skipped["file_id"], "f24")
        self.assertTrue(skipped["success"])
        self.assertTrue(skipped["skip_history"])
        self.assertEqual(skipped["dedup_reason"], "同集已有更优版本")
        self.assertEqual(transferred["file_id"], "f25")
        self.assertTrue(transferred["success"])
        self.assertNotIn("skip_history", transferred)
        # 只 E25 进了批次，E24 没下载没转存。
        self.assertEqual(len(service._batch_calls), 1)
        self.assertEqual(
            [i["file"]["id"] for i in service._batch_calls[0]], ["f25"]
        )
        # 版本只收集一次（整批共用，不按项重复列目录）。
        self.assertEqual(len(service._collect_calls), 1)

    def test_replace_is_kept(self):
        """决策 replace 的项原样放行（旧版删除走 finalize/洗版路径）。"""
        service = _Service(
            versions={24: [{"file_id": "old-1", "file_name": "旧版.mp4"}]},
            decisions={24: ("replace", [{"file_id": "old-1"}], "新版更优")},
        )
        results = service._transfer_episode_items(
            [_item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4")],
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_id"], "f24")
        self.assertNotIn("skip_history", results[0])
        self.assertEqual(len(service._batch_calls), 1)

    def test_all_skipped_never_calls_batch(self):
        """整批都被跳过时不调 _transfer_episode_batch。"""
        service = _Service(
            versions={24: [{"file_id": "old-1"}]},
            decisions={24: ("skip", [{"file_id": "old-1"}], "同集已有更优版本")},
        )
        results = service._transfer_episode_items(
            [_item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4")],
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertTrue(results[0]["skip_history"])
        self.assertEqual(service._batch_calls, [])

    def test_dedup_inactive_passes_all_through(self):
        """判重关闭时不收集版本、全部放行。"""
        service = _Service(
            versions={24: [{"file_id": "old-1"}]},
            decisions={24: ("skip", [{"file_id": "old-1"}], "同集已有更优版本")},
            dedup_active=False,
        )
        results = service._transfer_episode_items(
            [_item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4")],
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("skip_history", results[0])
        self.assertEqual(service._collect_calls, [])
        self.assertEqual(len(service._batch_calls), 1)

    def test_collect_error_fails_open(self):
        """查库异常时全部放行（fail-open），不拦正常转存。"""
        service = _Service(
            versions={24: [{"file_id": "old-1"}]},
            decisions={24: ("skip", [{"file_id": "old-1"}], "同集已有更优版本")},
        )
        # 挂上 _owner 后普通属性写会被 OwnerDelegator 转发给 owner，
        # 必须用 object.__setattr__ 才能写到实例自己身上。
        object.__setattr__(service, "_collect_error", RuntimeError("网盘列目录失败"))
        results = service._transfer_episode_items(
            [_item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4")],
            "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("skip_history", results[0])
        self.assertEqual(len(service._batch_calls), 1)

    def test_item_without_episode_is_kept(self):
        """没有集号的项无法判重，原样放行。"""
        service = _Service(
            versions={24: [{"file_id": "old-1"}]},
            decisions={24: ("skip", [{"file_id": "old-1"}], "同集已有更优版本")},
        )
        item = _item("f99", 24, "玫瑰战争 - S01E24 - 第24集.mp4")
        del item["episode"]
        results = service._transfer_episode_items(
            [item], "https://115.com/s/test", _mediainfo(), _subscribe(), 1, "k",
        )
        self.assertEqual(len(results), 1)
        self.assertNotIn("skip_history", results[0])


if __name__ == "__main__":
    unittest.main()
