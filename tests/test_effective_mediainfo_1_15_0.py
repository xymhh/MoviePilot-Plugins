"""1.15.0 Phase 3a 第 6 项：_effective_mediainfo 合一。

唯一实现是 handlers/sync/utils.py 的 resolve_effective_mediainfo；
mp_organize 的 (mediainfo, media) 与 naming 的 (subscribe, mediainfo)
两个旧方法只是薄适配，签名不变；trigger_mp_organize 一次整理只
解析一次有效 MediaInfo（目标目录解析复用同一份）。
"""
import re
import unittest
from types import SimpleNamespace

from plugin_env import (
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_module,
    set_media_type,
)

install_app_mocks(
    "app.chain",
    "app.chain.download",
    "app.chain.storage",
    "app.chain.transfer",
    "app.core.metainfo",
    "app.schemas.file",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser",
)
install_core_delegation()
MediaType = set_media_type()


class FakeMediaInfo:
    def __init__(self, **kwargs):
        self.title = None
        self.type = None
        self.tmdb_id = None
        self.year = None
        self.season = None
        self.category = ""
        self.library_category = ""
        self.__dict__.update(kwargs)


class FakeMetaInfo:
    def __init__(self, name, custom_words=None):
        match = re.search(r"[Ss](\d+)[Ee](\d+)", str(name or ""))
        self.begin_season = int(match.group(1)) if match else None
        self.begin_episode = int(match.group(2)) if match else None
        self.episode_list = (
            [self.begin_episode] if self.begin_episode else []
        )


def _install_stubs():
    register_module("app.core.context", MediaInfo=FakeMediaInfo)
    register_module("app.schemas.types", MediaType=MediaType)
    register_module("app.core.metainfo", MetaInfo=FakeMetaInfo)


_install_stubs()

utils_module = load_module(
    "cloudsubscribex.handlers.sync.utils",
    "plugins.v2/cloudsubscribex/handlers/sync/utils.py",
)
resolve_effective_mediainfo = utils_module.resolve_effective_mediainfo


class ResolveEffectiveMediainfoTests(unittest.TestCase):
    def setUp(self):
        _install_stubs()

    def test_mediainfo_passthrough(self):
        """mediainfo 非空直接用它，不拼最小版。"""
        given = FakeMediaInfo(title="玫瑰战争", tmdb_id=299276)
        self.assertIs(
            resolve_effective_mediainfo(mediainfo=given), given
        )

    def test_build_minimal_from_media_dict(self):
        """只有 media 字典时拼最小 MediaInfo。"""
        result = resolve_effective_mediainfo(
            media={
                "tmdb_id": 299276,
                "media_type": "电视剧",
                "season": 1,
                "year": "2026",
                "title": "玫瑰战争",
            }
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.tmdb_id, 299276)
        self.assertEqual(result.type, MediaType.TV)
        self.assertEqual(result.season, 1)

    def test_no_type_returns_none(self):
        """media 里没有 media_type 时拼不出，返回 None。"""
        self.assertIsNone(
            resolve_effective_mediainfo(media={"tmdb_id": 1, "title": "x"})
        )
        self.assertIsNone(resolve_effective_mediainfo())

    def test_subscribe_enrich_does_not_mutate_original(self):
        """subscribe 覆盖时先深拷贝，不改调用方传入的 mediainfo。"""
        given = FakeMediaInfo(title="旧标题", tmdb_id=1)

        def enrich(subscribe, mediainfo):
            mediainfo.title = getattr(subscribe, "name", "")
            return mediainfo

        result = resolve_effective_mediainfo(
            mediainfo=given,
            subscribe=SimpleNamespace(name="新标题"),
            enrich=enrich,
        )
        self.assertEqual(result.title, "新标题")
        self.assertEqual(given.title, "旧标题")

    def test_subscribe_none_skips_enrich(self):
        """无 subscribe 时不深拷贝、直接返回同一对象。"""
        given = FakeMediaInfo(title="玫瑰战争", tmdb_id=299276)
        calls = []

        def enrich(subscribe, mediainfo):
            calls.append(True)
            return mediainfo

        self.assertIs(
            resolve_effective_mediainfo(mediainfo=given, enrich=enrich),
            given,
        )
        self.assertEqual(calls, [])


class FakeFileItem:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageChain:
    next_result = "found"

    def get_item(self, fileitem):
        if FakeStorageChain.next_result == "found":
            return fileitem
        return None


class FakeTransferChain:
    calls = []
    next_result = (True, "")

    def do_transfer(self, fileitem, **kwargs):
        FakeTransferChain.calls.append((fileitem, kwargs))
        return FakeTransferChain.next_result


class FakeDownloadChain:
    """MP 下载链替身：E24 不在库（缺集只有 E24），整理放行。"""

    def get_no_exists_info(self, meta, mediainfo):
        return False, {"tmdb-299276": {1: SimpleNamespace(episodes=[24])}}


class FakeDirectoryConf:
    def __init__(self, **kwargs):
        self.library_path = "/Alist/影视/moviepilot/媒体库/剧集"
        self.library_storage = "u115cs"
        self.name = "剧集"
        self.__dict__.update(kwargs)

    def model_copy(self, deep=False, update=None):
        clone = FakeDirectoryConf(**self.__dict__)
        if update:
            clone.__dict__.update(update)
        return clone

    def copy(self, deep=False, update=None):
        return self.model_copy(deep=deep, update=update)


class FakeDirectoryHelper:
    def get_dir(self, media=None, include_unsorted=False, **kwargs):
        return FakeDirectoryConf()


class _FakeDirItem:
    def __init__(self, name, is_directory=False):
        self.name = name
        self.is_directory = is_directory
        self.id = f"id-{name}"
        self.size = 1000


class _FakeLookup:
    def __init__(self, checked=True, directory_id="dir-1"):
        self.checked = checked
        self.directory_id = directory_id


class _FakeListing:
    def __init__(self, files, checked=True):
        self.files = list(files)
        self.checked = checked


class FakeCloudDirectories:
    rows = []

    def resolve_directory(self, path):
        return _FakeLookup(checked=True, directory_id="dir-1")

    def list_directory(self, directory_id):
        return _FakeListing(FakeCloudDirectories.rows, checked=True)


class FakeMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name or "").lower().endswith(
            (".mkv", ".mp4", ".avi", ".iso")
        )


def _install_mp_stubs():
    _install_stubs()
    register_module("app.chain.storage", StorageChain=FakeStorageChain)
    register_module("app.chain.transfer", TransferChain=FakeTransferChain)
    register_module("app.chain.download", DownloadChain=FakeDownloadChain)
    register_module("app.schemas.file", FileItem=FakeFileItem)
    register_module("app.helper.directory", DirectoryHelper=FakeDirectoryHelper)
    register_module(
        "app.application.directory", DirectoryHelper=FakeDirectoryHelper
    )
    register_module(
        "cloudsubscribex.utils.file_parser",
        MediaFileParser=FakeMediaFileParser,
    )


_install_mp_stubs()

mp_organize = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize.MpOrganizeMixin


class _Service(MpOrganizeMixin):
    def __init__(self, directories=None):
        object.__setattr__(self, "_mp_organize_after_share_save", True)
        object.__setattr__(
            self, "_video_extensions", [".mkv", ".mp4", ".avi", ".iso"]
        )
        object.__setattr__(
            self, "_cloud_transfer_path", "/Alist/影视/moviepilot/转存"
        )
        object.__setattr__(self, "_cloud_directories", directories)


class _CountingService(_Service):
    calls = 0

    def _effective_mediainfo(self, mediainfo=None, media=None):
        _CountingService.calls += 1
        return super()._effective_mediainfo(mediainfo, media)


class TriggerResolvesOnceTests(unittest.TestCase):
    def setUp(self):
        # set_media_type 每次调用都新建一个 MediaType 枚举类：
        # 先绑定再装桩，否则模块级 MediaType 与 sys.modules 里那份是两个类。
        global MediaType
        MediaType = set_media_type(movie="电影", tv="电视剧")
        _install_mp_stubs()
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        FakeStorageChain.next_result = "found"
        FakeCloudDirectories.rows = []

    def test_trigger_resolves_effective_mediainfo_once(self):
        """一次整理只解析一次有效 MediaInfo。"""
        _CountingService.calls = 0
        service = _CountingService(FakeCloudDirectories())
        ok, _info = service.trigger_mp_organize(
            "/downloads",
            "玫瑰战争 - S01E24 - 第24集.mp4",
            media={
                "tmdb_id": 299276,
                "media_type": "电视剧",
                "season": 1,
                "year": "2026",
                "title": "玫瑰战争",
            },
        )
        self.assertTrue(ok)
        self.assertEqual(_CountingService.calls, 1)
        self.assertEqual(len(FakeTransferChain.calls), 1)

    def test_mp_adapter_passthrough_and_minimal(self):
        """mp 侧薄适配：直传 mediainfo、media 字典拼最小版。"""
        service = _Service(FakeCloudDirectories())
        given = FakeMediaInfo(title="玫瑰战争", tmdb_id=299276)
        self.assertIs(service._effective_mediainfo(given, None), given)
        built = service._effective_mediainfo(
            None,
            {
                "tmdb_id": 299276,
                "media_type": "电视剧",
                "season": 1,
                "title": "玫瑰战争",
            },
        )
        self.assertIsNotNone(built)
        self.assertEqual(built.type, MediaType.TV)


if __name__ == "__main__":
    unittest.main()
