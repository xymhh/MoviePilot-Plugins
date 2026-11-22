"""手动添加认不出夸克链接（Bug 1）：TMDB ID 直查短路。

覆盖 core/services/platform_links.py::api_vue_resolve_manual_links：
- 标题搜不到但 tmdb_id 正确时，按 ID 直查仍解析出唯一候选（不调标题搜索）；
- 只给显式标题时走现有标题搜索；
- 坏 tmdb_id 回退标题搜索（不硬报错）；
- 直查到的类型与指定类型不符时回退标题搜索；
- 电视剧直查候选能取到真实季列表；
- 云盘路径流程不变（直查失败时仍走标题搜索，直查成功时同样解析）。

加载方式：setUpClass 里懒加载真实模块（media / search.types /
media_resolve / platform_agent / platform_links），加载前对动过的
``cloudsubscribex.*`` 模块与父包属性做快照，tearDownClass 里恢复
（app.*、core.state、core.failure 是全量套件共享的打桩基础设施，不动）。
被测方法是真实实现；TMDB 搜索、按 ID 直查、分享预览、订阅匹配全部用
实例级替身控制，不碰真实 DB 与网络。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from plugin_env import ensure_package, install_app_mocks, load_module  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MISSING = object()
_MODULES = (
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.search",
    "cloudsubscribex.core.services",
    "cloudsubscribex.core.media",
    "cloudsubscribex.search.types",
    "cloudsubscribex.core.services.media_resolve",
    "cloudsubscribex.core.services.platform_agent",
    "cloudsubscribex.core.services.platform_links",
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


LINK = "https://pan.quark.cn/s/5009594fd63e"
GARBAGE_TITLE = "S-双 I D I 之-战"
CLEAN_TITLE = "斗破苍穹剧场版 双帝之战"
DIRECT_MOVIE = {
    "tmdb_id": 1599184,
    "media_type": "movie",
    "title": CLEAN_TITLE,
    "original_title": "",
    "year": 2026,
}
DIRECT_TV = {
    "tmdb_id": 12345,
    "media_type": "tv",
    "title": "测试剧",
    "original_title": "",
    "year": 2026,
}


class _FakeSearch:
    """可控的 TMDB 候选搜索：记录每次调用，按 handler 返回结果。"""

    def __init__(self):
        self.calls = []
        self.handler = lambda payload: {"success": True, "data": {"items": []}}

    def __call__(self, payload):
        self.calls.append(dict(payload or {}))
        return self.handler(dict(payload or {}))


class _FakeDirect:
    """可控的按 ID 直查：记录每次调用，返回预设候选或 None。

    result_by_type 不为空时按 media_type 分发（模拟同一 ID 在两种
    类型下各有/没有对应媒体）。
    """

    def __init__(self, result=None, result_by_type=None):
        self.calls = []
        self.result = result
        self.result_by_type = result_by_type

    def __call__(self, tmdb_id, media_type, title):
        self.calls.append((tmdb_id, media_type, title))
        if self.result_by_type is not None:
            return self.result_by_type.get(media_type)
        return self.result


class ManualAddResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved_modules, cls._saved_attrs = _snapshot_modules(_MODULES)
        install_app_mocks(
            "app.core",
            "app.core.config",
            "app.core.metainfo",
            "app.db",
            "app.db.models.subscribe",
            "app.db.subscribe_oper",
            "app.utils",
            "app.utils.string",
        )
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
        services = ensure_package("cloudsubscribex.core.services")
        services.__path__ = [
            os.path.join(
                REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "services"
            )
        ]
        load_module(
            "cloudsubscribex.core.media",
            "plugins.v2/cloudsubscribex/core/media.py",
        )
        load_module(
            "cloudsubscribex.search.types",
            "plugins.v2/cloudsubscribex/search/types.py",
        )
        load_module(
            "cloudsubscribex.core.services.media_resolve",
            "plugins.v2/cloudsubscribex/core/services/media_resolve.py",
        )
        load_module(
            "cloudsubscribex.core.services.platform_agent",
            "plugins.v2/cloudsubscribex/core/services/platform_agent.py",
        )
        cls.links_module = load_module(
            "cloudsubscribex.core.services.platform_links",
            "plugins.v2/cloudsubscribex/core/services/platform_links.py",
        )
        cls.PlatformLinkMixin = cls.links_module.PlatformLinkMixin

    @classmethod
    def tearDownClass(cls):
        _restore_modules(cls._saved_modules, cls._saved_attrs)

    def setUp(self):
        self._preview = {}
        self.search = _FakeSearch()
        self.direct = _FakeDirect()

    def _service(self):
        svc = object.__new__(self.PlatformLinkMixin)
        svc.extract_resource_links = lambda value: (
            [str(item).strip() for item in value if str(item or "").strip()]
            if isinstance(value, (list, tuple, set))
            else [
                line.strip()
                for line in str(value or "").splitlines()
                if line.strip()
            ]
        )
        svc._preview_link_media = lambda links: dict(self._preview)
        svc._link_media_title = lambda links: ""
        svc._link_title_seasons = lambda value: []
        svc._find_link_subscribe = lambda *args: None
        svc.api_vue_search_tmdb_candidates = self.search
        svc._candidate_for_tmdb_id = self.direct
        return svc

    def test_tmdb_id_short_circuits_when_title_search_finds_nothing(self):
        """规避名标题搜不到、tmdb_id 正确：按 ID 直查出唯一候选，不调标题搜索。"""
        self._preview = {"title": GARBAGE_TITLE, "media_type": "", "seasons": []}
        self.direct.result = dict(DIRECT_MOVIE)
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": GARBAGE_TITLE,
            "tmdb_id": 1599184,
            "media_type": "movie",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_MOVIE])
        self.assertEqual(self.direct.calls, [(1599184, "movie", GARBAGE_TITLE)])
        self.assertEqual(self.search.calls, [])

    def test_explicit_title_uses_title_search(self):
        """只给显式标题（不给 ID）：走现有标题搜索，不碰按 ID 直查。"""
        self._preview = {"title": GARBAGE_TITLE, "media_type": "", "seasons": []}
        self.search.handler = lambda payload: {
            "success": True, "data": {"items": [dict(DIRECT_MOVIE)]},
        }
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": CLEAN_TITLE,
            "media_type": "movie",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_MOVIE])
        self.assertEqual(len(self.search.calls), 1)
        self.assertEqual(self.search.calls[0]["title"], CLEAN_TITLE)
        self.assertIsNone(self.search.calls[0]["tmdb_id"])
        self.assertEqual(self.direct.calls, [])

    def test_bad_tmdb_id_falls_back_to_title_search(self):
        """坏 tmdb_id：直查不到，回退标题搜索（不硬报错），结果如实为空候选。"""
        self.direct.result = None
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": CLEAN_TITLE,
            "tmdb_id": 99999999,
            "media_type": "movie",
        })
        self.assertEqual(self.direct.calls, [(99999999, "movie", CLEAN_TITLE)])
        self.assertEqual(len(self.search.calls), 1)
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [])

    def test_media_type_mismatch_drops_direct_candidate(self):
        """直查到的类型与指定类型不符：弃用直查结果，回退标题搜索。"""
        self.direct.result = dict(DIRECT_MOVIE)
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": GARBAGE_TITLE,
            "tmdb_id": 1599184,
            "media_type": "tv",
        })
        self.assertEqual(self.direct.calls, [(1599184, "tv", GARBAGE_TITLE)])
        self.assertEqual(len(self.search.calls), 1)
        self.assertEqual(result["data"]["candidates"], [])

    def test_tmdb_id_short_circuit_tv_returns_seasons(self):
        """电视剧按 ID 直查：唯一候选 + 详情调用取到真实季列表。"""
        self._preview = {"title": GARBAGE_TITLE, "media_type": "", "seasons": []}
        self.direct.result = dict(DIRECT_TV)
        self.search.handler = lambda payload: {
            "success": True,
            "data": {"items": [dict(DIRECT_TV)], "seasons": [1, 2]},
        }
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": GARBAGE_TITLE,
            "tmdb_id": 12345,
            "media_type": "tv",
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_TV])
        self.assertEqual(result["data"]["available_seasons"], [1, 2])
        self.assertEqual(len(self.search.calls), 1)
        self.assertEqual(self.search.calls[0]["media_type"], "tv")
        self.assertEqual(self.search.calls[0]["tmdb_id"], 12345)

    def test_tmdb_id_without_type_tries_both_types(self):
        """只给 TMDB ID 不给类型：电影和电视剧两种都试，命中一种即定位。"""
        self.direct.result_by_type = {"movie": dict(DIRECT_MOVIE), "tv": None}
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": GARBAGE_TITLE,
            "tmdb_id": 1599184,
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_MOVIE])
        self.assertEqual(self.direct.calls, [
            (1599184, "movie", GARBAGE_TITLE),
            (1599184, "tv", GARBAGE_TITLE),
        ])
        self.assertEqual(self.search.calls, [])

    def test_tmdb_id_matching_both_types_returns_both_candidates(self):
        """同一 ID 在电影和电视剧都存在：两个候选都返回，让前端点选。"""
        ambiguous_tv = dict(DIRECT_TV, tmdb_id=1599184)
        self.direct.result_by_type = {
            "movie": dict(DIRECT_MOVIE),
            "tv": ambiguous_tv,
        }
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "resource_links": [LINK],
            "title": GARBAGE_TITLE,
            "tmdb_id": 1599184,
        })
        self.assertTrue(result["success"])
        self.assertEqual(
            result["data"]["candidates"], [DIRECT_MOVIE, ambiguous_tv],
        )
        self.assertEqual(self.search.calls, [])

    def test_cloud_path_resolves_via_title_search_when_direct_fails(self):
        """云盘路径流程不变：直查失败时仍走标题搜索，同样解析出媒体。"""
        self.direct.result = None
        self.search.handler = lambda payload: {
            "success": True, "data": {"items": [dict(DIRECT_MOVIE)]},
        }
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "cloud_path": "/115/电影/斗破苍穹剧场版 双帝之战 {tmdb id-1599184}/",
            "cloud_provider": "115",
            "title": CLEAN_TITLE,
            "tmdb_id": 1599184,
            "media_type": None,
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_MOVIE])
        self.assertEqual(len(self.search.calls), 1)

    def test_cloud_path_resolves_via_direct_id(self):
        """云盘路径同样受益于直查：标题搜索不被调用，结果是同一部媒体。"""
        self.direct.result = dict(DIRECT_MOVIE)
        svc = self._service()
        result = svc.api_vue_resolve_manual_links({
            "cloud_path": "/115/电影/斗破苍穹剧场版 双帝之战 {tmdb id-1599184}/",
            "cloud_provider": "115",
            "title": CLEAN_TITLE,
            "tmdb_id": 1599184,
            "media_type": None,
        })
        self.assertTrue(result["success"])
        self.assertEqual(result["data"]["candidates"], [DIRECT_MOVIE])
        self.assertEqual(self.search.calls, [])


if __name__ == "__main__":
    unittest.main()
