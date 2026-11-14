"""1.9.91：搜索接入 MoviePilot 原生搜索。

加载真实的 plugins.v2/cloudsubscribex/__init__.py（app.* 与第三方依赖
全部打桩）与真实的 core/mp_search.py，在裸实例上验证：搜索映射、
/torrent 种子接口、download 接管与任务 id 推导、get_module 声明，
以及配置默认值与 API 注册的静态断言。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from plugin_env import ensure_package, load_module, set_media_type  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MediaType = set_media_type(movie="movie", tv="tv")


class _TorrentInfo:
    """桩 TorrentInfo：按传入字段存属性。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MediaInfo:
    """桩 MediaInfo：只存 title / type。"""

    def __init__(self, title=None, type=None, **kwargs):
        self.title = title
        self.type = type
        for key, value in kwargs.items():
            setattr(self, key, value)


app_schemas = ensure_package("app.schemas")
app_schemas.TorrentInfo = _TorrentInfo
app_schemas.MediaInfo = _MediaInfo

from test_downloader_plugin_1_9_86 import _load_plugin_module, _plugin  # noqa: E402


def _ensure_mp_stubs():
    """在取插件/mp_search 之前重新安装本文件的桩。

    仓库里多个测试文件在模块级用 ``register_module("app.schemas")``
    替换整个包（收集顺序上最后写入的胜出），会把本文件在导入时装的
    TorrentInfo / MediaInfo / MediaType 桩抹掉，导致 mp_search 懒加载
    时拿到 MagicMock。这里每次都用同一份桩对象重新装上（不是新建），
    保证 mp_search 绑定的 MediaType 与用例里断言的是同一个类。
    """
    types_mod = ensure_package("app.schemas.types")
    if types_mod.__dict__.get("MediaType") is not MediaType:
        types_mod.MediaType = MediaType
    schemas = ensure_package("app.schemas")
    if schemas.__dict__.get("TorrentInfo") is not _TorrentInfo:
        schemas.TorrentInfo = _TorrentInfo
    if schemas.__dict__.get("MediaInfo") is not _MediaInfo:
        schemas.MediaInfo = _MediaInfo


def _load_mp_search():
    """加载真实的 core/mp_search.py（只加载一次）。"""
    _ensure_mp_stubs()
    module = sys.modules.get("cloudsubscribex.core.mp_search")
    if module is not None and hasattr(module, "search_torrents"):
        return module
    return load_module(
        "cloudsubscribex.core.mp_search",
        "plugins.v2/cloudsubscribex/core/mp_search.py",
    )


class _FakeSearchHandler:
    """桩 SearchHandler：按预设返回渠道与候选，并记录调用。"""

    def __init__(self, grouped, enabled_sources, display_names=None):
        self.grouped = grouped
        self.enabled_sources = list(enabled_sources)
        self.display_names = dict(display_names or {})
        self.calls = []

    def get_enabled_sources(self, media_type=None):
        self.calls.append(("get_enabled_sources", media_type))
        return list(self.enabled_sources)

    def get_source_display_name(self, source):
        return self.display_names.get(source, source)

    def search_sources(self, **kwargs):
        self.calls.append(("search_sources", kwargs))
        return {
            key: [dict(item) for item in values]
            for key, values in self.grouped.items()
        }


class _FakeChannelStore:
    """桩 ChannelStore：只提供 list()。"""

    def __init__(self, channels):
        self._channels = channels

    def list(self):
        return [dict(channel) for channel in self._channels]


def _mp_plugin(handler=None, channels=None, submit_result=None):
    """裸插件实例 + MP 搜索链路所需的运行态打桩。"""
    _ensure_mp_stubs()
    _load_plugin_module()
    _ensure_mp_stubs()
    plugin = _plugin()
    plugin._enabled = True
    plugin._applied_config = {"mp_base_url": "http://127.0.0.1:3000"}
    plugin._update_config_calls = []
    plugin.update_config = lambda merged: (
        plugin._update_config_calls.append(dict(merged)), None
    )[1]
    plugin._search_handler = handler
    plugin._channel_store = lambda: _FakeChannelStore(channels or [])
    plugin._sync_handler = SimpleNamespace(
        preview_resource_files=lambda share_url: [
            {
                "name": "仙逆 S01E01.mkv",
                "size": 1234567,
                "_relative_path": "仙逆/S01E01.mkv",
            },
            {
                "name": "仙逆 S01E02.mkv",
                "size": 2345678,
                "_relative_path": "仙逆/S01E02.mkv",
            },
        ]
    )
    plugin.submit_calls = []
    plugin._submit_result = submit_result

    def _submit(**kwargs):
        plugin.submit_calls.append(kwargs)
        return plugin._submit_result

    plugin.submit_platform_links = _submit
    plugin._serialize_runtime_tasks = lambda: []
    return plugin


def _search_items():
    """两渠道候选：盘搜 2 条、seedhub 1 条与盘搜重复 + 1 条新。"""
    return {
        "pansou": [
            {
                "share_url": "https://pan.quark.cn/s/aaaa",
                "title": "仙逆 更新至第12集 \u3010公众号：追剧资源\u3011",
                "resource_type": "quark",
                "password": "1234",
                "size": 0,
            },
            {
                "share_url": "https://pan.baidu.com/s/bbbb",
                "title": "仙逆 2024 1080p",
                "resource_type": "baidu",
                "size": 5368709120,
            },
        ],
        "seedhub": [
            {
                "share_url": "https://pan.quark.cn/s/aaaa",
                "title": "仙逆 更新至第12集",
                "resource_type": "quark",
            },
            {
                "share_url": "https://www.alipan.com/s/cccc",
                "title": "仙逆全集",
                "resource_type": "alipan",
            },
        ],
    }


def _search_plugin(**kwargs):
    handler = _FakeSearchHandler(
        _search_items(),
        ["pansou", "seedhub"],
        {"pansou": "盘搜", "seedhub": "SeedHub"},
    )
    channels = [
        {"id": "c1", "type": "pansou", "enabled": True, "priority": 50},
        {"id": "c2", "type": "seedhub", "enabled": True, "priority": 40},
    ]
    return _mp_plugin(handler=handler, channels=channels, **kwargs)


class TestSearchMapping(unittest.TestCase):
    def test_maps_candidates_to_torrent_info(self):
        plugin = _search_plugin()
        rows = plugin.search_torrents(keyword="仙逆")
        self.assertEqual(len(rows), 3)
        # 按渠道 priority 合并：盘搜在前，跨渠道重复链接只留一条。
        self.assertEqual(
            [row.page_url for row in rows],
            [
                "https://pan.quark.cn/s/aaaa",
                "https://pan.baidu.com/s/bbbb",
                "https://www.alipan.com/s/cccc",
            ],
        )
        self.assertEqual([row.site_name for row in rows], ["盘搜", "盘搜", "SeedHub"])
        # 标题清洗生效：更新至第12集 / 公众号水印 / 全集都被洗掉，年份与分辨率保留。
        self.assertEqual([row.title for row in rows], ["仙逆", "仙逆 2024 1080p", "仙逆"])
        self.assertEqual(rows[0].description, "网盘订阅助手 · 夸克网盘 · 盘搜")
        self.assertEqual(rows[1].description, "网盘订阅助手 · 百度网盘 · 盘搜")
        self.assertEqual(rows[0].size, 0.0)
        self.assertEqual(rows[1].size, 5368709120.0)
        self.assertEqual(rows[0].seeders, 0)
        self.assertEqual(rows[0].site, None)
        # pri_order 按渠道 priority：盘搜 50 在前，SeedHub 40 在后。
        self.assertEqual([row.pri_order for row in rows], [50, 50, 40])
        # mtype 为 None：category 置 None，让 MP 自己按标题识别。
        self.assertIsNone(rows[0].category)
        # pubdate 是当前时间文本。
        self.assertRegex(rows[0].pubdate, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_enclosure_is_signed_torrent_url(self):
        plugin = _search_plugin()
        mp_search = _load_mp_search()
        rows = plugin.search_torrents(keyword="仙逆")
        parsed = urlparse(rows[0].enclosure)
        self.assertEqual(
            parsed.scheme + "://" + parsed.netloc + parsed.path,
            "http://127.0.0.1:3000/api/v1/plugin/CloudSubscribeX/torrent",
        )
        query = parse_qs(parsed.query)
        sid = query["sid"][0]
        sig = query["sig"][0]
        self.assertTrue(
            mp_search.verify(sid, sig, mp_search.get_sign_secret(plugin))
        )
        self.assertFalse(mp_search.verify(sid, "0" * 32, mp_search.get_sign_secret(plugin)))
        # 快照已登记：enclosure 的 sid 能在登记表里找到。
        snapshot = mp_search._snapshot_registry(plugin).get(sid)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.share_url, "https://pan.quark.cn/s/aaaa")
        self.assertEqual(snapshot.passcode, "1234")
        self.assertEqual(snapshot.resource_type, "quark")
        self.assertEqual(snapshot.media_type, "tv")

    def test_only_enabled_search_sources_are_used(self):
        """停用的渠道与 tg_channel 不参与：只认 get_enabled_sources。"""
        handler = _FakeSearchHandler(
            {"pansou": [dict(_search_items()["pansou"][0])]},
            ["pansou"],
            {"pansou": "盘搜"},
        )
        channels = [
            {"id": "c1", "type": "pansou", "enabled": True, "priority": 50},
            {"id": "c2", "type": "seedhub", "enabled": False, "priority": 40},
            {"id": "c3", "type": "tg_channel", "enabled": True, "priority": 60},
        ]
        plugin = _mp_plugin(handler=handler, channels=channels)
        rows = plugin.search_torrents(keyword="仙逆")
        self.assertEqual(len(rows), 1)
        search_call = next(
            kwargs for name, kwargs in handler.calls if name == "search_sources"
        )
        self.assertEqual(search_call["sources"], ["pansou"])
        self.assertEqual(search_call["result_limit"], 5)
        self.assertFalse(search_call["apply_platform_rules"])
        self.assertFalse(search_call["force_refresh"])
        self.assertIsNone(search_call["season"])

    def test_total_cap_eight(self):
        grouped = {
            source: [
                {
                    "share_url": f"https://pan.quark.cn/s/{source}{index}",
                    "title": f"仙逆 {index}",
                    "resource_type": "quark",
                }
                for index in range(5)
            ]
            for source in ("pansou", "seedhub")
        }
        handler = _FakeSearchHandler(grouped, ["pansou", "seedhub"])
        plugin = _mp_plugin(
            handler=handler,
            channels=[
                {"id": "c1", "type": "pansou", "enabled": True, "priority": 50},
                {"id": "c2", "type": "seedhub", "enabled": True, "priority": 40},
            ],
        )
        rows = plugin.search_torrents(keyword="仙逆")
        self.assertEqual(len(rows), 8)

    def test_page_above_zero_returns_empty(self):
        plugin = _search_plugin()
        self.assertEqual(plugin.search_torrents(keyword="仙逆", page=1), [])
        self.assertEqual(len(plugin.search_torrents(keyword="仙逆", page=0)), 3)

    def test_empty_keyword_or_disabled_returns_empty(self):
        plugin = _search_plugin()
        self.assertEqual(plugin.search_torrents(keyword=""), [])
        self.assertEqual(plugin.search_torrents(keyword="   "), [])
        self.assertEqual(plugin.search_torrents(keyword=None), [])
        plugin._enabled = False
        self.assertEqual(plugin.search_torrents(keyword="仙逆"), [])

    def test_movie_mtype_sets_category_and_media_type(self):
        plugin = _search_plugin()
        rows = plugin.search_torrents(keyword="仙逆", mtype=MediaType.MOVIE)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].category, "movie")
        mp_search = _load_mp_search()
        snapshot = mp_search._snapshot_registry(plugin).get(
            parse_qs(urlparse(rows[0].enclosure).query)["sid"][0]
        )
        self.assertEqual(snapshot.media_type, "movie")
        # mtype 原样传给 handler（MOVIE 时动漫源由 handler 侧剔除）。
        get_call = next(
            mtype for name, mtype in plugin._search_handler.calls
            if name == "get_enabled_sources"
        )
        self.assertIs(get_call, MediaType.MOVIE)

    def test_none_mtype_searches_as_tv(self):
        plugin = _search_plugin()
        plugin.search_torrents(keyword="仙逆")
        get_call = next(
            mtype for name, mtype in plugin._search_handler.calls
            if name == "get_enabled_sources"
        )
        # mtype 为 None 时按 TV 走 handler，源集合最广。
        self.assertIs(get_call, MediaType.TV)


class TestSignAndTorrent(unittest.TestCase):
    def test_sign_secret_generated_and_persisted_once(self):
        plugin = _mp_plugin()
        mp_search = _load_mp_search()
        secret = mp_search.get_sign_secret(plugin)
        self.assertEqual(len(secret), 64)
        self.assertEqual(len(plugin._update_config_calls), 1)
        self.assertEqual(
            plugin._update_config_calls[0]["mp_search_sign_secret"], secret
        )
        self.assertEqual(mp_search.get_sign_secret(plugin), secret)
        self.assertEqual(len(plugin._update_config_calls), 1)

    def test_render_and_parse_marker_roundtrip(self):
        mp_search = _load_mp_search()
        snapshot = mp_search.ShareSnapshot(
            sid="0123456789abcdef",
            share_url="https://pan.quark.cn/s/aaaa",
            resource_type="quark",
            title="仙逆",
        )
        payload = mp_search.render_torrent(
            snapshot,
            [
                {"name": "仙逆 S01E01.mkv", "size": 1000,
                 "_relative_path": "仙逆/S01E01.mkv"},
                {"name": "空文件.mkv", "size": 0},
            ],
        )
        self.assertTrue(payload.startswith(b"d"))
        marker = mp_search.parse_marker(payload)
        self.assertEqual(marker, ("0123456789abcdef", "quark"))
        self.assertIn(b"CLOUDSUBSCRIBEX|0123456789abcdef|quark", payload)
        from cloudsubscribex.core.bencode import info_hash

        self.assertRegex(info_hash(payload), r"^[0-9a-f]{40}$")
        self.assertIsNone(mp_search.parse_marker(b"not a torrent"))
        self.assertIsNone(mp_search.parse_marker(b"d7:comment5:hello e"))

    def test_api_torrent_success(self):
        plugin = _search_plugin()
        mp_search = _load_mp_search()
        rows = plugin.search_torrents(keyword="仙逆")
        query = parse_qs(urlparse(rows[0].enclosure).query)
        response = plugin.api_torrent(sid=query["sid"][0], sig=query["sig"][0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "application/x-bittorrent")
        self.assertIn("attachment; filename=", response.headers["Content-Disposition"])
        self.assertTrue(response.body.startswith(b"d"))
        self.assertEqual(
            mp_search.parse_marker(response.body),
            (query["sid"][0], "quark"),
        )

    def test_api_torrent_bad_signature_is_403(self):
        plugin = _search_plugin()
        rows = plugin.search_torrents(keyword="仙逆")
        sid = parse_qs(urlparse(rows[0].enclosure).query)["sid"][0]
        response = plugin.api_torrent(sid=sid, sig="0" * 32)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(plugin.api_torrent(sid=sid, sig="").status_code, 403)

    def test_api_torrent_unknown_sid_is_404(self):
        plugin = _search_plugin()
        mp_search = _load_mp_search()
        sig = mp_search.sign("deadbeefdeadbeef", mp_search.get_sign_secret(plugin))
        response = plugin.api_torrent(sid="deadbeefdeadbeef", sig=sig)
        self.assertEqual(response.status_code, 404)

    def test_api_torrent_empty_file_list_is_404(self):
        plugin = _search_plugin()
        plugin._sync_handler = SimpleNamespace(preview_resource_files=lambda url: [])
        rows = plugin.search_torrents(keyword="仙逆")
        query = parse_qs(urlparse(rows[0].enclosure).query)
        response = plugin.api_torrent(sid=query["sid"][0], sig=query["sig"][0])
        self.assertEqual(response.status_code, 404)


class TestDownload(unittest.TestCase):
    def _torrent_bytes(self, plugin, snapshot=None):
        mp_search = _load_mp_search()
        if snapshot is None:
            snapshot = mp_search.ShareSnapshot(
                sid=mp_search.build_sid("https://pan.quark.cn/s/aaaa"),
                share_url="https://pan.quark.cn/s/aaaa",
                resource_type="quark",
                title="仙逆",
                media_type="tv",
                season=1,
            )
            mp_search._snapshot_registry(plugin).put(snapshot)
        return mp_search.render_torrent(
            snapshot,
            [{"name": "仙逆 S01E01.mkv", "size": 1000,
              "_relative_path": "仙逆/S01E01.mkv"}],
        ), snapshot

    def test_download_matched_subscribe_returns_task_hash(self):
        from cloudsubscribex.core.downloader_view import task_hash

        plugin = _mp_plugin(submit_result={
            "success": True,
            "data": {"matched_subscribe_id": 123},
        })
        payload, _ = self._torrent_bytes(plugin)
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result, ("网盘订阅助手", task_hash("subscribe:123"), "", "")
        )
        self.assertEqual(len(plugin.submit_calls), 1)
        call = plugin.submit_calls[0]
        self.assertEqual(call["subscribe_id"], 0)
        self.assertEqual(call["resource_links"], ["https://pan.quark.cn/s/aaaa"])
        self.assertFalse(call["wait"])
        self.assertEqual(call["title"], "仙逆")
        self.assertEqual(call["media_type"], "tv")
        self.assertEqual(call["season"], 1)

    def test_download_transient_media_task_id(self):
        from cloudsubscribex.core.downloader_view import task_hash

        plugin = _mp_plugin(submit_result={
            "success": True,
            "data": {"media": {
                "tmdb_id": 999, "media_type": "tv",
                "title": "仙逆", "seasons": [1],
            }},
        })
        payload, _ = self._torrent_bytes(plugin)
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result, ("网盘订阅助手", task_hash("media:tmdb_999_S1"), "", "")
        )

    def test_download_transient_movie_task_id(self):
        from cloudsubscribex.core.downloader_view import task_hash

        plugin = _mp_plugin(submit_result={
            "success": True,
            "data": {"media": {
                "tmdb_id": 888, "media_type": "movie", "title": "仙逆",
            }},
        })
        payload, snapshot = self._torrent_bytes(plugin)
        snapshot.media_type = "movie"
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result, ("网盘订阅助手", task_hash("media:tmdb_888_movie"), "", "")
        )

    def test_download_falls_back_to_polling_runtime_tasks(self):
        from cloudsubscribex.core.downloader_view import task_hash

        plugin = _mp_plugin(submit_result={"success": True, "data": {}})
        calls = {"count": 0}

        def _serialize():
            calls["count"] += 1
            if calls["count"] == 1:
                return []
            return [{"id": "media:tmdb_999_S1", "title": "仙逆", "season": 1}]

        plugin._serialize_runtime_tasks = _serialize
        payload, _ = self._torrent_bytes(plugin)
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result, ("网盘订阅助手", task_hash("media:tmdb_999_S1"), "", "")
        )

    def test_download_submit_failure_returns_reason(self):
        plugin = _mp_plugin(submit_result={
            "success": False,
            "message": "“仙逆”匹配到多个 TMDB 媒体，请先选择",
        })
        payload, _ = self._torrent_bytes(plugin)
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result,
            (None, None, None, "“仙逆”匹配到多个 TMDB 媒体，请先选择"),
        )

    def test_download_unmatched_content_returns_none(self):
        plugin = _mp_plugin(submit_result={"success": True, "data": {}})
        self.assertIsNone(plugin.download(b"not a torrent", "/downloads"))
        self.assertIsNone(plugin.download("not bytes", "/downloads"))
        # 真实 bencode 种子、但不是本插件的标记：交给下一个 provider。
        from cloudsubscribex.core.bencode import build_torrent

        other = build_torrent(
            name="other",
            files=[{"length": 100, "path": ["other.mkv"]}],
            comment="OTHER|deadbeefdeadbeef|quark",
        )
        self.assertIsNone(plugin.download(other, "/downloads"))
        self.assertEqual(plugin.submit_calls, [])

    def test_download_expired_snapshot_returns_failure_tuple(self):
        mp_search = _load_mp_search()
        plugin = _mp_plugin(submit_result={"success": True, "data": {}})
        snapshot = mp_search.ShareSnapshot(
            sid=mp_search.build_sid("https://pan.quark.cn/s/zzzz"),
            share_url="https://pan.quark.cn/s/zzzz",
            resource_type="quark",
            title="仙逆",
        )
        payload = mp_search.render_torrent(
            snapshot,
            [{"name": "仙逆 S01E01.mkv", "size": 1000}],
        )
        result = plugin.download(payload, "/downloads")
        self.assertEqual(
            result, (None, None, None, "分享快照已过期，请重新搜索")
        )
        self.assertEqual(plugin.submit_calls, [])

    def test_download_disabled_plugin_returns_none(self):
        plugin = _mp_plugin(submit_result={"success": True, "data": {}})
        plugin._enabled = False
        payload, _ = self._torrent_bytes(plugin)
        self.assertIsNone(plugin.download(payload, "/downloads"))
        self.assertEqual(plugin.submit_calls, [])


class TestModuleAndConfig(unittest.TestCase):
    def test_get_module_declares_search_and_download(self):
        plugin = _mp_plugin()
        module = plugin.get_module()
        self.assertIn("search_torrents", module)
        self.assertIn("download", module)
        for key in (
            "list_torrents", "torrent_files", "start_torrents",
            "stop_torrents", "remove_torrents",
        ):
            self.assertIn(key, module)
            self.assertTrue(callable(module[key]))

    def test_version_is_1_9_91(self):
        module = _load_plugin_module()
        self.assertEqual(module.CloudSubscribeX.plugin_version, "1.9.91")
        with open(
            os.path.join(REPO_ROOT, "package.v2.json"), encoding="utf-8"
        ) as handle:
            package = json.load(handle)
        plugin = package["CloudSubscribeX"]
        self.assertEqual(plugin["version"], "1.9.91")
        self.assertIn("v1.9.91", plugin["history"])
        self.assertIn("search_torrents", plugin["history"]["v1.9.91"])

    def test_config_default_and_frontend_field(self):
        config_path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "config.py"
        )
        source = open(config_path, encoding="utf-8").read()
        self.assertIn('"mp_base_url": "http://127.0.0.1:3000"', source)
        basic_path = os.path.join(
            REPO_ROOT, "frontend", "cloudsubscribex", "src", "config",
            "fields", "basic.js",
        )
        basic = open(basic_path, encoding="utf-8").read()
        self.assertIn('key: "mp_base_url"', basic)
        self.assertIn('label: "MoviePilot 内部地址"', basic)

    def test_torrent_api_registered_anonymous(self):
        registration_path = os.path.join(
            REPO_ROOT, "plugins.v2", "cloudsubscribex", "core", "api",
            "registration.py",
        )
        source = open(registration_path, encoding="utf-8").read()
        start = source.index('"/torrent"')
        entry = source[start:start + 400]
        self.assertIn("self.api_torrent", entry)
        self.assertIn('"allow_anonymous": True', entry)
        self.assertIn('"methods": ["GET"]', entry)
        self.assertIn("按分享 id 输出合成种子", entry)


if __name__ == "__main__":
    unittest.main()
