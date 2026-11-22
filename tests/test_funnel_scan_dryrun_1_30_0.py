"""漏斗阶段 4 扫描对账 dry-run（v1.30.0）测试。

覆盖（设计文档 §5.3 分类 + §12 阶段 4）：
- 分类三态：可识别（不在库）/ 不可识别（认不出）/ 已在库（跳过）；
- 查库失败（None）与查库异常按可认领记（fail-open）；
- dry-run 只记日志：不写队列行、不删文件；
- dry-run 关掉也不接管（只记警告、跳过扫描）；
- 漏斗未启用时跳过扫描；
- 识别异常的包记不可识别；
- 扫描轮次记进轮次历史（kind=scan）。

用真实 scan_entries + build_packages（utils/organize_plan.py，纯
函数模块）跑完整扫描；识别与查库用替身注入（run_scan_dry_run 的
organize_service / provider / library_check 参数）。
"""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import install_app_mocks, register_mock  # noqa: E402

# 子集跑时本文件可能在 baseline 之前被收集：自己补齐脚手架链要的
# app.* 桩（test_finalize_mp_wait 加载 service.py 的导入链要这些，
# 全量里由更早收集的文件提供；install_app_mocks 幂等）。settings 的
# DB_TIMEOUT 在 setUpClass 由 _ensure_db_settings 原地补。
install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.core.cache",
    "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.subscribe",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver",
)
_config_module = sys.modules.get("app.core.config")
_settings_obj = getattr(_config_module, "settings", None)
if not isinstance(getattr(_settings_obj, "DB_TIMEOUT", None), (int, float)):
    register_mock(
        "app.core.config",
        settings=SimpleNamespace(
            DB_TIMEOUT=30, TZ="Asia/Shanghai", CONFIG_PATH=Path("/tmp"),
        ),
    )
from test_funnel_stage1_1_29_7 import (  # noqa: E402
    _DbMixin,
    _movie_media,
    _tv_media,
)


def _ensure_db_settings():
    """DB 模块首次加载前，保证 settings.DB_TIMEOUT/TZ 为真值。

    test_downloader_plugin（经其它文件链）在加载真实插件 __init__ 时
    把 app.core.config.settings 换成只有 API_TOKEN 的桩；manager.py 在
    _DbMixin.setUpClass 里 ``from app.core.config import settings`` 绑定
    当时对象。原地补属性（不换对象，已绑定的模块不受影响）。
    """
    module = sys.modules.get("app.core.config")
    settings_obj = getattr(module, "settings", None) if module is not None else None
    if settings_obj is None:
        return
    if not isinstance(getattr(settings_obj, "DB_TIMEOUT", None), (int, float)):
        settings_obj.DB_TIMEOUT = 30
    if not isinstance(getattr(settings_obj, "TZ", None), str):
        settings_obj.TZ = "Asia/Shanghai"


class _ScanDbMixin(_DbMixin):
    @classmethod
    def setUpClass(cls):
        _ensure_db_settings()
        super().setUpClass()


class _FakeScanProvider:
    """scan_entries 要的目录读取能力：resolve_directory + list_directory。"""

    def __init__(self, tree):
        # tree: {cid: [(name, is_dir, file_id, size)]}，根 cid 为 "cid-root"。
        self.tree = tree
        self.resolve_calls = []
        self.list_calls = []

    def resolve_directory(self, path):
        self.resolve_calls.append(path)
        return SimpleNamespace(checked=True, directory_id="cid-root")

    def list_directory(self, cid):
        self.list_calls.append(cid)
        files = [
            SimpleNamespace(name=name, is_directory=is_dir, id=file_id, size=size)
            for name, is_dir, file_id, size in self.tree.get(cid, [])
        ]
        return SimpleNamespace(checked=True, files=files)


class _FakeOrganizeService:
    """_recognize_package 替身：按首个视频文件名匹配媒体，认不出返回 None。"""

    def __init__(self, media_by_name):
        self.media_by_name = media_by_name
        self.calls = []

    def _recognize_package(self, package):
        videos = list(getattr(package, "videos", []) or [])
        name = ""
        if videos:
            entry = getattr(videos[0], "entry", None)
            name = str(getattr(entry, "name", "") or "")
        self.calls.append(name)
        for key, media in self.media_by_name.items():
            if key in name:
                return media
        return None


def _package(is_tv, videos):
    return SimpleNamespace(
        pack_name="测试包",
        is_tv=is_tv,
        videos=[
            SimpleNamespace(
                season=season, episode=episode,
                entry=SimpleNamespace(name=name),
            )
            for season, episode, name in videos
        ],
    )


class ScanClassifyTests(_ScanDbMixin):
    """_scan_classify_package 三态 + fail-open。"""

    def test_classify_recognizable_tv(self):
        """认得出、不在库：可认领入队。"""
        status, detail = self.service._scan_classify_package(
            _package(True, [(1, 9, "多喜一家人 - S01E09 - 第9集.mkv")]),
            _tv_media(),
            lambda media, season, episode, file_name: False,
        )
        self.assertEqual(status, "recognizable")
        self.assertIn("S01E09", detail)

    def test_classify_recognizable_movie(self):
        """认得出电影、不在库：可认领入队（季集 0/0，说明带文件名）。"""
        status, detail = self.service._scan_classify_package(
            _package(False, [(0, 0, "测试电影 (2026).mkv")]),
            _movie_media(),
            lambda media, season, episode, file_name: False,
        )
        self.assertEqual(status, "recognizable")
        self.assertIn("测试电影", detail)

    def test_classify_unrecognizable_when_media_none(self):
        """认不出媒体：待人工认领（不查库）。"""
        calls = []
        status, _detail = self.service._scan_classify_package(
            _package(True, [(1, 9, "看不懂的名字.mkv")]),
            None,
            lambda *a: calls.append(a) or True,
        )
        self.assertEqual(status, "unrecognizable")
        self.assertEqual(calls, [])

    def test_classify_already_in_library_when_check_true(self):
        """认得出、已在库：跳过（skip_reason=“已入库”）。"""
        status, detail = self.service._scan_classify_package(
            _package(True, [(1, 9, "多喜一家人 - S01E09 - 第9集.mkv")]),
            _tv_media(),
            lambda media, season, episode, file_name: True,
        )
        self.assertEqual(status, "already_in_library")
        self.assertIn("S01E09", detail)

    def test_classify_fail_open_when_check_none(self):
        """查库查不出来（None）：按可认领记，绝不丢包。"""
        status, _detail = self.service._scan_classify_package(
            _package(True, [(1, 9, "多喜一家人 - S01E09 - 第9集.mkv")]),
            _tv_media(),
            lambda media, season, episode, file_name: None,
        )
        self.assertEqual(status, "recognizable")

    def test_classify_fail_open_when_check_raises(self):
        """查库抛异常：按可认领记（fail-open）。"""
        def _boom(*args):
            raise RuntimeError("library lookup down")

        status, _detail = self.service._scan_classify_package(
            _package(True, [(1, 9, "多喜一家人 - S01E09 - 第9集.mkv")]),
            _tv_media(),
            _boom,
        )
        self.assertEqual(status, "recognizable")


class ScanDryRunTests(_ScanDbMixin):
    """run_scan_dry_run：真实扫描原语 + 替身识别/查库。"""

    def _tree(self):
        return {
            "cid-root": [
                ("多喜一家人 - S01E09 - 第9集.mkv", False, "f-scan-09", 1500000000),
                ("测试电影 (2026).mkv", False, "f-scan-mv", 5000000000),
                ("看不懂的名字.mkv", False, "f-scan-unknown", 100000000),
            ]
        }

    def _organize_service(self):
        return _FakeOrganizeService({
            "多喜一家人": _tv_media(),
            "测试电影": _movie_media(),
        })

    def _library_check(self, media, season, episode, file_name):
        # 电影已在库（跳过），剧集不在库（可认领）。
        if getattr(media, "tmdb_id", None) == 123456:
            return True
        return False

    def test_dry_run_writes_no_queue_rows(self):
        """dry-run 全流程：三态各一，不写队列行、不删文件。"""
        provider = _FakeScanProvider(self._tree())
        summary = self.service.run_scan_dry_run(
            trigger="手动",
            organize_service=self._organize_service(),
            provider=provider,
            library_check=self._library_check,
        )
        self.assertTrue(summary["success"])
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["scanned"], 3)
        self.assertEqual(summary["recognizable"], 1)
        self.assertEqual(summary["unrecognizable"], 1)
        self.assertEqual(summary["already_in_library"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(self.repo.count_by_status(), {})
        history = self.service.round_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["kind"], "scan")
        self.assertEqual(history[0]["scanned"], 3)

    def test_dry_run_off_skips_without_scanning(self):
        """dry-run 关掉也不接管：只记警告、跳过扫描、不读目录、不记轮次。"""
        self.owner._funnel_scan_dry_run = False
        provider = _FakeScanProvider(self._tree())
        summary = self.service.run_scan_dry_run(
            trigger="手动",
            organize_service=self._organize_service(),
            provider=provider,
            library_check=self._library_check,
        )
        self.assertFalse(summary["success"])
        self.assertFalse(summary["dry_run"])
        self.assertIn("dry-run 已关闭", summary["message"])
        self.assertEqual(provider.resolve_calls, [])
        self.assertEqual(provider.list_calls, [])
        self.assertEqual(self.service.round_history(), [])

    def test_dry_run_phase1_off_skips(self):
        """漏斗未启用：跳过扫描，不读目录。"""
        self.owner._funnel_phase1_enabled = False
        provider = _FakeScanProvider(self._tree())
        summary = self.service.run_scan_dry_run(
            trigger="定时",
            organize_service=self._organize_service(),
            provider=provider,
            library_check=self._library_check,
        )
        self.assertFalse(summary["success"])
        self.assertIn("漏斗未启用", summary["message"])
        self.assertEqual(provider.resolve_calls, [])
        self.assertEqual(self.service.round_history(), [])

    def test_dry_run_recognize_exception_counts_unrecognizable(self):
        """单包识别抛异常：记不可识别，不中断整轮。"""
        provider = _FakeScanProvider(self._tree())
        organize_service = self._organize_service()

        def _boom(package):
            raise RuntimeError("recognize down")

        organize_service._recognize_package = _boom
        summary = self.service.run_scan_dry_run(
            trigger="手动",
            organize_service=organize_service,
            provider=provider,
            library_check=self._library_check,
        )
        self.assertTrue(summary["success"])
        self.assertEqual(summary["scanned"], 3)
        self.assertEqual(summary["unrecognizable"], 3)
        self.assertEqual(summary["recognizable"], 0)
        self.assertEqual(self.repo.count_by_status(), {})

    def test_dry_run_unreadable_target_dir_records_error(self):
        """目标目录解析失败：errors 记 1，扫描 0 包，不抛异常。"""
        provider = _FakeScanProvider(self._tree())
        provider.resolve_directory = lambda path: SimpleNamespace(
            checked=False, directory_id=None
        )
        summary = self.service.run_scan_dry_run(
            trigger="定时",
            organize_service=self._organize_service(),
            provider=provider,
            library_check=self._library_check,
        )
        self.assertTrue(summary["success"])
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["scanned"], 0)
        self.assertEqual(self.repo.count_by_status(), {})
