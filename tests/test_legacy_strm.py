"""旧 STRM 迁移工具测试（1.7.64）。

把 P115StrmHelper 生成的 STRM 改写成本插件端点：只换 URL 前缀，
pickcode / share_code / receive_code / id 等查询参数必须原样保留。
"""

import sys

from plugin_env import load_module

sys.path.insert(0, "tests")

legacy = load_module(
    "cloudsubscribex.utils.legacy_strm",
    "plugins.v2/cloudsubscribex/utils/legacy_strm.py",
)


def test_migrate_pickcode_url():
    src = "http://mp:3000/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=abc&file_name=x.mkv"
    assert legacy.migrate_strm_content(src) == (
        "http://mp:3000/api/v1/plugin/CloudSubscribeX/redirect?pickcode=abc&file_name=x.mkv"
    )


def test_migrate_share_url():
    src = "http://mp/api/v1/plugin/P115StrmHelper/redirect_url?share_code=s&receive_code=r&id=9"
    out = legacy.migrate_strm_content(src)
    assert "CloudSubscribeX/redirect" in out
    assert "share_code=s" in out and "receive_code=r" in out and "id=9" in out


def test_already_migrated_returns_none():
    assert legacy.migrate_strm_content(
        "http://mp/api/v1/plugin/CloudSubscribeX/redirect?pickcode=a"
    ) is None


def test_empty_and_plain_url_returns_none():
    assert legacy.migrate_strm_content("") is None
    assert legacy.migrate_strm_content("https://example.com/a.mkv") is None


def test_trailing_newline_preserved():
    out = legacy.migrate_strm_content(
        "http://mp/api/v1/plugin/P115StrmHelper/redirect_url?pickcode=a\n"
    )
    assert out.endswith("CloudSubscribeX/redirect?pickcode=a\n")


def test_iter_strm_files_recursive(tmp_path):
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    (sub / "x.strm").write_text("u", encoding="utf-8")
    (sub / "y.mkv").write_text("u", encoding="utf-8")
    assert [p.name for p in legacy.iter_strm_files(str(tmp_path))] == ["x.strm"]


def test_iter_strm_files_missing_root(tmp_path):
    assert list(legacy.iter_strm_files(str(tmp_path / "nope"))) == []
