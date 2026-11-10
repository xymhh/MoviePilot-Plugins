"""转存阶段的集号统计必须复用识别阶段的兜底（黑色五叶草078.mp4）。"""

import os
import re
import sys
import unittest

from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import install_app_mocks, load_module, register_module, ensure_package

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app.core.metainfo", "app.log", "app.schemas", "app.schemas.types",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.core.context", "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
)
logger_ref = sys.modules["app.log"].logger

# StringUtils 桩：num_filesize 解析数字
sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)

# ---- MetaInfo 桩：支持 SxxExx 与「第 N 集」两种老命名 ----
def _fake_metainfo(name, custom_words=None):
    text = str(name or "").strip()
    season = episode = None
    match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,4})", text)
    if match:
        season = int(match.group(1))
        episode = int(match.group(2))
    else:
        # 「- 第 1 集」老命名：无 SxxExx 时 begin_season=None（由调用方回退目标季）
        m2 = re.search(r"第\s*(\d{1,4})\s*集", text)
        if m2:
            episode = int(m2.group(1))
    return SimpleNamespace(
        begin_season=season,
        begin_episode=episode,
        episode_list=[episode] if episode else [],
        cn_name=text or None,
    )


register_module("app.core.metainfo", MetaInfo=_fake_metainfo)
register_module("app.schemas", MediaInfo=SimpleNamespace)
register_module(
    "app.schemas.types",
    MediaType=SimpleNamespace(TV="tv", MOVIE="movie"),
)

# ---- cloudsubscribex 包层级 ----
install_app_mocks(
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.utils", "cloudsubscribex.utils.file_parser",
    "cloudsubscribex.handlers", "cloudsubscribex.handlers.sync",
    "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
)

# OwnerDelegator 桩：与 plugin_env 一致
from plugin_env import OwnerDelegator as _BaseDelegator  # noqa: E402


class _OwnerStub(_BaseDelegator):
    pass


# 对包用 ensure_package（保留 AutoMockModule），对叶子模块用 register_module
_ensure = ensure_package
_ensure("cloudsubscribex.core").OwnerDelegator = _OwnerStub
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core.media").apply_media_identity = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(return_value=[])
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(return_value=("themoviedb", "123"))
_ensure("cloudsubscribex.utils").FileMatcher = SimpleNamespace(episode_from_file=MagicMock(return_value=None))
_ensure("cloudsubscribex.utils").MediaFileParser = SimpleNamespace(
    is_video=lambda name: str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))
)
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
# 1.7.88 择优比较器：用真实实现（纯函数，无宿主依赖）
load_module(
    "cloudsubscribex.utils.quality_compare",
    "plugins.v2/cloudsubscribex/utils/quality_compare.py",
)
_ensure("cloudsubscribex.drive.common").format_size = lambda n: f"{n}B"
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.handlers.sync.utils").extract_ed2k_filename = MagicMock()
_ensure("cloudsubscribex.handlers.sync.baseline").normalize_platform_cache_key = lambda key: key

# 兜底被测逻辑需要真实的 share_recognize（纯函数，不依赖宿主）
load_module(
    "cloudsubscribex.utils.share_recognize",
    "plugins.v2/cloudsubscribex/utils/share_recognize.py",
)


# 加载被测模块
naming_module = load_module(
    "cloudsubscribex.handlers.sync.naming",
    "plugins.v2/cloudsubscribex/handlers/sync/naming.py",
)
SyncNamingService = naming_module.SyncNamingService


class SummarizeShareEpisodesTest(unittest.TestCase):
    """识别计划 ready 之后，转存阶段不能再因重算集号而整批跳过。"""

    @staticmethod
    def _files(*names):
        return [{"name": n, "is_dir": False, "size": 800 * 1024 * 1024} for n in names]

    def test_glued_numbers_counted(self):
        names = [f"黑色五叶草{i:03d}.mp4" for i in range(1, 21)]
        count, episodes = SyncNamingService._summarize_share_episodes(self._files(*names), 1)
        self.assertEqual(count, 20)
        self.assertEqual(len(episodes), 20)
        self.assertIn(1, episodes)
        self.assertIn(20, episodes)

    def test_nested_directories_counted(self):
        files = [{
            "name": "【全170集】黑色五叶草",
            "is_dir": True,
            "children": self._files(*[f"黑色五叶草{i:03d}.mp4" for i in range(1, 11)]),
        }]
        count, episodes = SyncNamingService._summarize_share_episodes(files, 1)
        self.assertEqual(count, 10)
        self.assertEqual(len(episodes), 10)

    def test_too_few_files_still_unparsed(self):
        count, episodes = SyncNamingService._summarize_share_episodes(
            self._files("黑色五叶草078.mp4", "黑色五叶草079.mp4"), 1
        )
        self.assertEqual(count, 2)
        self.assertEqual(episodes, set())

    def test_year_file_still_unparsed(self):
        count, episodes = SyncNamingService._summarize_share_episodes(
            self._files("黑色五叶草2017.mp4"), 1
        )
        self.assertEqual(count, 1)
        self.assertEqual(episodes, set())


if __name__ == "__main__":
    unittest.main()
