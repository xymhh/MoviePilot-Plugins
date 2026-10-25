"""媒体服务器路径映射单测（1.7.67）。

回归背景：入库后触发 Emby「神医媒体信息提取」时，把 MP 侧路径
``/strm/115网盘/...`` 直接丢给 ``Items?Path=``，而 Emby 挂载点是
``/media/strm/...``，每次都在重试 4 次后放弃 —— 提取开关开了等于没开。
"""

import sys

from plugin_env import load_module

sys.path.insert(0, "tests")

path_map = load_module(
    "cloudsubscribex.utils.path_map",
    "plugins.v2/cloudsubscribex/utils/path_map.py",
)


def test_maps_prefix():
    assert path_map.map_local_to_server_path(
        "/strm/115网盘/剧集/a.strm", "/media/strm=/strm"
    ) == "/media/strm/115网盘/剧集/a.strm"


def test_exact_root_maps():
    assert path_map.map_local_to_server_path(
        "/strm", "/media/strm=/strm"
    ) == "/media/strm"


def test_unmatched_keeps_path():
    assert path_map.map_local_to_server_path(
        "/other/a.strm", "/media/strm=/strm"
    ) == "/other/a.strm"


def test_no_boundary_false_positive():
    # /strm2 不能命中 /strm 前缀
    assert path_map.map_local_to_server_path(
        "/strm2/a.strm", "/media/strm=/strm"
    ) == "/strm2/a.strm"


def test_longest_prefix_wins():
    mappings = "/media/strm=/strm\n/media/strm/115网盘=/strm/115网盘"
    assert path_map.map_local_to_server_path(
        "/strm/115网盘/x/a.strm", mappings
    ) == "/media/strm/115网盘/x/a.strm"


def test_empty_mappings_returns_normalized():
    assert path_map.map_local_to_server_path("strm/a.strm", "") == "/strm/a.strm"


def test_ignores_comments_and_bad_lines():
    mappings = "# comment\n/媒体=/m\nnot-a-mapping\n"
    assert path_map.map_local_to_server_path("/m/a.strm", mappings) == "/媒体/a.strm"


def test_windows_backslash_normalized():
    assert path_map.map_local_to_server_path(
        "\\strm\\a.strm", "/media/strm=/strm"
    ) == "/media/strm/a.strm"
