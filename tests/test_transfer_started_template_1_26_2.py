"""v1.26.2：开始下载通知走用户配的 DownloadAdded 模板。

v1.25.0 故意写死标题正文（怕模板渲染失败留空通知），结果通知形态
永远是「【网盘订阅助手】开始下载」，跟用户在 MP 里设的模板不一样
（2026-11-19 他截图指出）。v1.26.2 改成模板优先、写死兜底：先建
一条不带标题正文的裸 Message，带 mediainfo 与季集/大小等上下文调
MP 自己的 MessageTemplateHelper.render 渲用户模板，只有渲出非空
标题正文才用模板结果，否则退回写死格式。

本文件用假的 MP 模板模块验证，行为贴着 MP v3.2.6 的真实语义：
标题正文已给出就短路、没配模板原样返回、模板用 Jinja2 逐字段渲染。
并用他 MP 里那条真实模板（资源下载 Jinja2 JSON，2026-11-19 从
设定 → 通知抄回）跑一遍端到端，断言剧集与电影两种形态。

v1.26.3 补充：TG 路站点名不再解析频道名，直接写死“TG频道”
（私密频道存的是邀请链接 https://t.me/+...，旧解析按 "/" 切出
"https:"）；渲染补 pubdate（转存开始时间），模板里的时间行才出得来。
"""
_PUBDATE_RE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
import ast
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import jinja2

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_mock,
    register_module,
)

install_app_mocks(
    "app", "app.log", "app.schemas", "app.schemas.types",
    "app.core",
    "app.core.config",
    "app.core.context",
    "app.core.metainfo",
    "app.utils",
    "app.utils.string",
    "app.utils.path",
    "cloudsubscribex",
    "cloudsubscribex.core",
    "cloudsubscribex.drive",
    "cloudsubscribex.drive.common",
    "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync",
    "cloudsubscribex.utils",
    "cloudsubscribex.utils.cache",
)
# 与 test_transfer_started_notification_1_25_0.py 同款：types 用显式
# enum 桩，断言才有意义。
register_mock(
    "app.schemas.types",
    NotificationType=type("NotificationType", (), {"Plugin": "Plugin"}),
    MessageType=type("MessageType", (), {"Download": "Download"}),
    ContentType=type("ContentType", (), {"DownloadAdded": "DownloadAdded"}),
)
install_core_delegation()
ensure_package("cloudsubscribex.core")


def _format_size(value):
    """确定性大小格式化：mock 环境的 StringUtils.format_size 不可断言。"""
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


sys.modules["cloudsubscribex.drive.common"].format_size = _format_size

notify_module = sys.modules.get("cloudsubscribex.handlers.sync.notify")
if notify_module is None:
    notify_module = load_module(
        "cloudsubscribex.handlers.sync.notify",
        "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
    )
SyncNotificationService = notify_module.SyncNotificationService

# 他 MP 里那条真实模板（设定 → 通知 → 通知模板 → “资源下载 Jinja2
# JSON”，2026-11-19 抄回，逐字）。raw 字符串：模板里的 \n 是字面量
# 的转义，ast.literal_eval 后才变真换行，和 MP 存库里的形态一致。
USER_DOWNLOAD_TEMPLATE = r"""{
    'title': '📥 开始下载\n[{{ title_year }}]({{ __mediainfo__.detail_link }})',
    'text': '{% if download_episodes %}\n📦季集：{{ season_fmt }} {{ download_episodes }}{% elif season_episode %}\n📦季集：{{ season_episode }}{% endif %}'
    '{% if type == "音乐" and artist %}\n🎤艺术家：{{ artist }}{% endif %}'
    '{% if type == "音乐" and album %}\n💿专辑：{{ album }}{% endif %}'
    '{% if type == "音乐" and audio_specs %}\n🔊音质：{{ audio_specs }}{% endif %}'
    '{% if pubdate %}\n🕒时间：{{ pubdate }}{% endif %}'
    '{% if category %}\n🎭类别：{{ category }}{% endif %}'
    '{% if site_name %}\n🌐站点：{{ site_name }}{% endif %}'
    '{% if resource_term %}\n🌟质量：{{ webSource }} {{ resource_term }}{% endif %}'
    '{% if size %}\n💾大小：{{ size }}{% endif %}'
    '{% if seeders %}\n🌱做种：{{ seeders }}{% endif %}'
    '{% if labels %}\n🏷标签：{{ labels }}{% endif %}'
    '{% if original_name %}\n📛名称：[{{ original_name }}]({{ __torrentinfo__.page_url }}){% endif %}'
    '{% if description %}\n\n📝描述：{{ description }}{% endif %}'
}"""


class _FakeMessage:
    """MP Message 的最小替身：只留渲染会读写的字段。"""

    def __init__(self, mtype=None, ctype=None, title=None, text=None,
                image=None, poster=None, **kwargs):
        self.mtype = mtype
        self.ctype = ctype
        self.title = title
        self.text = text
        self.image = image
        self.poster = poster


def _build_template_context(*, mediainfo=None, **kwargs):
    """贴着 MP TemplateContextBuilder 的最小上下文：mediainfo 派生
    title/title_year/type/category，原始对象挂 __mediainfo__，其余
    kwargs（季集/大小/站点/时间）原样进上下文。"""
    context = dict(kwargs)
    if mediainfo is not None:
        title = str(getattr(mediainfo, "title", "") or "")
        year = getattr(mediainfo, "year", "") or ""
        context["title"] = title
        context["title_year"] = f"{title} ({year})" if year else title
        media_type = getattr(mediainfo, "type", "") or ""
        if media_type:
            context["type"] = media_type
        category = getattr(mediainfo, "category", None)
        if category:
            context["category"] = category
        context["__mediainfo__"] = mediainfo
    return context


class _FakeTemplateHelper:
    """MP MessageTemplateHelper 的最小替身：标题正文已给出就短路、
    没配模板原样返回、配了模板按字面量解析 + Jinja2 逐字段渲染。"""

    template = None
    raise_error = False
    calls = []

    @staticmethod
    def render(message, *args, **kwargs):
        _FakeTemplateHelper.calls.append(dict(kwargs))
        if _FakeTemplateHelper.raise_error:
            raise RuntimeError("template boom")
        if getattr(message, "title", None) or getattr(message, "text", None):
            return message
        template = _FakeTemplateHelper.template
        if not template:
            return message
        parsed = ast.literal_eval(template)
        context = _build_template_context(**kwargs)
        for key, value in parsed.items():
            if isinstance(value, str):
                rendered = jinja2.Template(value).render(context)
                if hasattr(message, key):
                    setattr(message, key, rendered)
        return message


_MISSING = object()
_FAKE_MODULE_NAMES = (
    "app.application.messaging",
    "app.application.messaging.message",
    "app.schemas.message",
)


def _mediainfo(title="玫瑰战争", year=2025, media_type="电视剧",
                category="国产剧",
                detail_link="https://www.themoviedb.org/tv/299276"):
    mediainfo = SimpleNamespace(
        title=title, year=year, tmdb_id=299276,
        type=media_type, category=category, detail_link=detail_link,
    )
    mediainfo.get_poster_image = lambda: "https://image.tmdb.org/poster.jpg"
    return mediainfo


def _subscribe(source=None, via=None):
    subscribe = SimpleNamespace(name="玫瑰战争", id=1)
    if source is not None:
        subscribe._transfer_source = source
    if via is not None:
        subscribe._discovered_via = via
    return subscribe


def _item(file_id, episode, name, size=1000):
    return {
        "file": {"id": file_id, "name": name, "size": size},
        "episode": episode,
        "score": 50,
        "target_name": name,
        "target_dir": "/Alist/影视/moviepilot/媒体库/剧集",
    }


def _real_notify_service(notify=True):
    service = object.__new__(SyncNotificationService)
    object.__setattr__(service, "_owner", SimpleNamespace())
    post_message = MagicMock()
    object.__setattr__(service, "_post_message", post_message)
    object.__setattr__(service, "_notify", notify)
    return service, post_message


class TemplateNotificationTests(unittest.TestCase):
    def setUp(self):
        self._saved = {}
        for name in _FAKE_MODULE_NAMES:
            self._saved[name] = sys.modules.get(name)
            parent_name, _, child = name.rpartition(".")
            parent = sys.modules.get(parent_name) if parent_name else None
            old = getattr(parent, child, _MISSING) if parent else _MISSING
            self._saved[(name, "parent")] = (parent, child, old)
        register_module("app.application.messaging", package=True)
        register_module(
            "app.application.messaging.message",
            MessageTemplateHelper=_FakeTemplateHelper,
        )
        register_module("app.schemas.message", Message=_FakeMessage)
        _FakeTemplateHelper.template = None
        _FakeTemplateHelper.raise_error = False
        _FakeTemplateHelper.calls = []

    def tearDown(self):
        for name in _FAKE_MODULE_NAMES:
            prev = self._saved[name]
            if prev is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
            parent, child, old = self._saved[(name, "parent")]
            if parent is not None:
                if old is _MISSING:
                    try:
                        delattr(parent, child)
                    except AttributeError:
                        pass
                else:
                    setattr(parent, child, old)

    def _send_tv(self, service, source="subscribe", via=None):
        items = [
            _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
            _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
        ]
        service.send_transfer_started_notification(
            mediainfo=_mediainfo(),
            subscribe=_subscribe(source, via),
            source=source,
            via=via or "",
            items=items,
            media_type="tv",
            season=1,
            episodes=[24, 25],
        )

    def test_template_renders_tv_notification(self):
        """模板命中：剧集通知走用户模板，种子才有的行自动跳过。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        service, post_message = _real_notify_service()
        self._send_tv(service)
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(kwargs["mtype"], notify_module.MessageType.Download)
        self.assertEqual(kwargs["ctype"], notify_module.ContentType.DownloadAdded)
        self.assertEqual(
            kwargs["title"],
            "📥 开始下载\n[玫瑰战争 (2025)]"
            "(https://www.themoviedb.org/tv/299276)",
        )
        self.assertIn("📦季集：S01 E24, E25", kwargs["text"])
        self.assertIn("🎭类别：国产剧", kwargs["text"])
        self.assertIn("💾大小：2.0 KB", kwargs["text"])
        self.assertRegex(kwargs["text"], "🕒时间：" + _PUBDATE_RE)
        self.assertNotIn("🌱做种", kwargs["text"])
        self.assertNotIn("📛名称", kwargs["text"])
        self.assertNotIn("🌐站点", kwargs["text"])
        self.assertEqual(kwargs["image"], "https://image.tmdb.org/poster.jpg")
        self.assertEqual(len(_FakeTemplateHelper.calls), 1)
        call_kwargs = _FakeTemplateHelper.calls[0]
        self.assertEqual(call_kwargs["download_episodes"], "E24, E25")
        self.assertEqual(call_kwargs["season_fmt"], "S01")
        self.assertEqual(call_kwargs["season_episode"], "S01E24")
        self.assertEqual(call_kwargs["size"], "2.0 KB")
        self.assertRegex(call_kwargs["pubdate"], "^" + _PUBDATE_RE + "$")
        self.assertNotIn("site_name", call_kwargs)
        self.assertIs(call_kwargs["mediainfo"].title, "玫瑰战争")

    def test_template_renders_movie_notification(self):
        """模板命中：电影没有季集行，大小行正常出。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_started_notification(
            mediainfo=_mediainfo(
                title="深度安静", year=2026, media_type="电影",
                category="华语电影",
                detail_link="https://www.themoviedb.org/movie/1234567",
            ),
            subscribe=_subscribe("subscribe"),
            source="subscribe",
            items=[_item("m1", 0, "深度安静 (2026).mkv", size=15139759718)],
            media_type="movie",
            season=1,
            episodes=[],
            total_size=15139759718,
        )
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(
            kwargs["title"],
            "📥 开始下载\n[深度安静 (2026)]"
            "(https://www.themoviedb.org/movie/1234567)",
        )
        self.assertNotIn("📦季集", kwargs["text"])
        self.assertIn("🎭类别：华语电影", kwargs["text"])
        self.assertIn("💾大小：14.1 GB", kwargs["text"])
        self.assertRegex(kwargs["text"], "🕒时间：" + _PUBDATE_RE)
        self.assertNotIn("🌱做种", kwargs["text"])

    def test_tg_site_name_passed_to_template(self):
        """v1.26.3：TG 路站点名直接写死“TG频道”，不再解析频道名。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        service, post_message = _real_notify_service()
        self._send_tv(service, source="tg", via="tg:@Channel/12345")
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("🌐站点：TG频道", post_message.call_args.kwargs["text"])
        self.assertEqual(_FakeTemplateHelper.calls[0]["site_name"], "TG频道")

    def test_tg_invite_link_via_shows_tg_channel(self):
        """v1.26.3：私密频道邀请链接 via 不再切出 "https:"。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        service, post_message = _real_notify_service()
        self._send_tv(
            service, source="tg", via="tg:https://t.me/+AbCdEfGh/12345"
        )
        self.assertEqual(post_message.call_count, 1)
        text = post_message.call_args.kwargs["text"]
        self.assertIn("🌐站点：TG频道", text)
        self.assertNotIn("https:", text)
        self.assertEqual(_FakeTemplateHelper.calls[0]["site_name"], "TG频道")

    def test_no_template_falls_back_to_hardcoded(self):
        """没配模板：渲染原样返回，退回写死格式。"""
        service, post_message = _real_notify_service()
        self._send_tv(service)
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(kwargs["title"], "【网盘订阅助手】开始下载")
        self.assertIn("玫瑰战争 S01 E24, E25", kwargs["text"])
        self.assertEqual(len(_FakeTemplateHelper.calls), 1)

    def test_render_failure_falls_back_to_hardcoded(self):
        """渲染抛错：退回写死格式，不发空通知。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        _FakeTemplateHelper.raise_error = True
        service, post_message = _real_notify_service()
        self._send_tv(service)
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"], "【网盘订阅助手】开始下载"
        )

    def test_empty_render_falls_back_to_hardcoded(self):
        """模板渲出空字典（标题正文都没落下）：退回写死格式。"""
        _FakeTemplateHelper.template = "{}"
        service, post_message = _real_notify_service()
        self._send_tv(service)
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"], "【网盘订阅助手】开始下载"
        )

    def test_invalid_template_falls_back_to_hardcoded(self):
        """模板不是合法字面量：解析抛错，退回写死格式。"""
        _FakeTemplateHelper.template = "not a dict literal {{{"
        service, post_message = _real_notify_service()
        self._send_tv(service)
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"], "【网盘订阅助手】开始下载"
        )

    def test_mediainfo_without_title_skips_render(self):
        """mediainfo 没标题：根本不调渲染，直接写死格式。"""
        _FakeTemplateHelper.template = USER_DOWNLOAD_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_started_notification(
            mediainfo=None,
            subscribe=_subscribe("subscribe"),
            source="subscribe",
            items=[_item("f24", 24, "玫瑰战争 - S01E24.mp4")],
            media_type="tv",
            season=1,
            episodes=[24],
        )
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"], "【网盘订阅助手】开始下载"
        )
        self.assertEqual(_FakeTemplateHelper.calls, [])


if __name__ == "__main__":
    unittest.main()
