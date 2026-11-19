"""v1.26.4：转存完成通知在终态门恢复（按架构修，不单点补发）。

转存是一条完整流程：开始（插件发 DownloadAdded）→ 转存 → 终态。
终态只有一处门，两处调用点共用同一条 v1.13.3 规则（只发
notification_kind == "upgrade" 洗版）：同步完成块（sync.py）与
_send_finalized_batch（transfer_details.py:633）。v1.26.4 在门处
恢复普通转存的完成通知：两处调用点共用
send_transfer_completed_notification，发送器内部按分支分流——
MP 整理分支跳过（MP 自己发 OrganizeSuccess），strm 直出分支发
OrganizeSuccess（裸 Message 走用户资源入库模板，模板优先、写死
兜底，与开始通知同套路）。

本文件用假的 MP 模板模块验证，行为贴着 MP v3.2.6 的真实语义，
并用他 MP 里那条真实模板（资源入库 Jinja2 JSON，2026-11-19
从设定 → 通知抄回）跑一遍端到端，断言剧集与电影两种形态。
"""
import ast
import os
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
    register_module,
    set_media_type,
)

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
plugins_v2_path = os.path.join(project_root, "plugins.v2")
if plugins_v2_path not in sys.path:
    sys.path.insert(0, plugins_v2_path)

install_app_mocks(
    "app", "app.log", "app.schemas", "app.schemas.types",
    "app.core", "app.core.config", "app.core.context", "app.core.metainfo",
    "app.utils", "app.utils.string", "app.utils.path",
    "app.modules", "app.modules.filemanager",
    "app.modules.filemanager.transhandler",
    "app.helper", "app.helper.directory",
    "app.application", "app.application.directory",
    "app.sdk", "app.sdk.classification",
    "app.modules.themoviedb", "app.modules.themoviedb.category",
    "app.db", "app.db.models", "app.db.models.downloadhistory",
    "app.db.models.mediaserver", "app.db.downloadhistory_oper",
    "app.db.subscribe_oper", "app.chain", "app.chain.mediaserver",
    "app.helper.mediaserver", "app.application.mediaserver", "sqlalchemy",
    "cloudsubscribex", "cloudsubscribex.core", "cloudsubscribex.core.media",
    "cloudsubscribex.core.history", "cloudsubscribex.utils",
    "cloudsubscribex.utils.file_parser", "cloudsubscribex.handlers",
    "cloudsubscribex.handlers.sync", "cloudsubscribex.handlers.sync.utils",
    "cloudsubscribex.drive", "cloudsubscribex.drive.common",
    "cloudsubscribex.search", "cloudsubscribex.search.types",
    "cloudsubscribex.utils.cache",
)
# 与 test_transfer_started_template_1_26_2.py 同款：types 用显式
# enum 桩，断言才有意义；本版再加 Organize / OrganizeSuccess。
# 直接往占位模块上写属性（不用 register_mock 整个替换），
# MediaType（set_media_type 注入）才不会丢。
set_media_type(movie="电影", tv="电视剧")
_types = sys.modules["app.schemas.types"]
_types.NotificationType = type("NotificationType", (), {"Plugin": "Plugin"})
_types.MessageType = type(
    "MessageType", (), {"Download": "Download", "Organize": "Organize"}
)
_types.ContentType = type(
    "ContentType",
    (),
    {"DownloadAdded": "DownloadAdded", "OrganizeSuccess": "OrganizeSuccess"},
)
install_core_delegation()
ensure_package("cloudsubscribex.core")

sys.modules["app.utils.string"].StringUtils = SimpleNamespace(
    num_filesize=lambda v: int(v) if str(v).isdigit() else 0,
)
register_module("app.schemas", MediaInfo=SimpleNamespace)

_ensure = ensure_package
_ensure("cloudsubscribex.core").CloudFile = SimpleNamespace
_ensure("cloudsubscribex.core").CloudDriveCapability = MagicMock()
_ensure("cloudsubscribex.core.media").tmdb_id_of = MagicMock(return_value=123)
_ensure("cloudsubscribex.core.media").media_identity = MagicMock(
    return_value=("themoviedb", "123")
)
_ensure("cloudsubscribex.core.media").get_transfer_history_by = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.media").download_history_identity_payload = MagicMock(
    return_value={}
)
_ensure("cloudsubscribex.core.media").media_server_tmdb_filters = MagicMock(
    return_value=[]
)
_ensure("cloudsubscribex.core.history").history_group_key = MagicMock(
    return_value=""
)
_ensure("cloudsubscribex.utils.cache").normalize_platform_cache_key = lambda key: key
_ensure("cloudsubscribex.utils.cache").create_platform_ttl_cache = MagicMock()
_ensure("cloudsubscribex.drive.common").positive_int = lambda v: int(v or 0)
_ensure("cloudsubscribex.handlers.sync.utils").normalize_season = lambda s: int(s or 1)
_ensure("cloudsubscribex.search.types").normalize_resource_type = lambda v: str(v or "")
_ensure("cloudsubscribex.search.types").resource_type_from_url = lambda url: "cloud"


class _StubMediaFileParser:
    @staticmethod
    def is_video(name):
        return str(name).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))


_ensure("cloudsubscribex.utils.file_parser").MediaFileParser = _StubMediaFileParser

_tpl = load_module(
    "cloudsubscribex.utils.notify_template",
    "plugins.v2/cloudsubscribex/utils/notify_template.py",
)
_core = sys.modules["cloudsubscribex.core"]
_core.render_template = _tpl.render_template
_core.resolve_template = _tpl.resolve_template


def _format_size(value):
    """确定性大小格式化：mock 环境的 StringUtils.format_size 不可断言。"""
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


sys.modules["cloudsubscribex.drive.common"].format_size = _format_size

notify_module = load_module(
    "cloudsubscribex.handlers.sync.notify",
    "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
)
SyncNotificationService = notify_module.SyncNotificationService
transfer_details_module = load_module(
    "cloudsubscribex.handlers.sync.transfer_details",
    "plugins.v2/cloudsubscribex/handlers/sync/transfer_details.py",
)
TransferDetailsService = transfer_details_module.TransferDetailsService

# 他 MP 里那条真实模板（设定 → 通知 → 通知模板 → “资源入库 Jinja2
# JSON”，2026-11-19 抄回，逐字）。raw 字符串：模板里的 \n 是字面量
# 的转义，ast.literal_eval 后才变真换行，和 MP 存库里的形态一致。
USER_ORGANIZE_TEMPLATE = r"""{
    'title': '📂 已入库{{ file_count }}个文件\n[{{ title_year }}]({{ __mediainfo__.detail_link }})',
    'text': '{% if type == "音乐" and artist %}\n🎤 艺术家：{{ artist }}{% endif %}'
            '{% if type == "音乐" and album %}\n💿 专辑：{{ album }}{% endif %}'
            '{% if type == "音乐" and audio_specs %}\n🔊 音质：{{ audio_specs }}{% endif %}'
            '{% if type != "音乐" and season_episode %}\n📺 集数：{{ season_episode }}{% endif %}'
            '{% if category %}\n🎭 类别：{{ category }}{% endif %}'
            '{% if releaseGroup %}\n👥 小组：{{ releaseGroup }}{% endif %}'
            '{% if resource_term %}\n🌟 质量：{{ webSource }} {{ resource_term }}{% if audioCodec %} {{ audioCodec }}{% endif %}{% endif %}'
            '\n💾 大小：{{ total_size }}'
            '{% if err_msg %}\n⚠️ 失败：{{ err_msg }}{% endif %}'
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
    title/title_year/type/category，原始对象挂 __mediainfo__；调用方
    传的 kwargs（文件数/季集/大小）后合并、同名时覆盖（与 MP 的
    build 先 _add_* 再 update(kwargs) 同序）。type 读 .value，
    没有 .value 时取原值。"""
    context = {}
    if mediainfo is not None:
        title = str(getattr(mediainfo, "title", "") or "")
        year = getattr(mediainfo, "year", "") or ""
        context["title"] = title
        context["title_year"] = f"{title} ({year})" if year else title
        media_type = getattr(mediainfo, "type", "") or ""
        type_value = getattr(media_type, "value", media_type)
        if type_value:
            context["type"] = type_value
        category = getattr(mediainfo, "category", None)
        if category:
            context["category"] = category
        context["__mediainfo__"] = mediainfo
    context.update(kwargs)
    context.pop("mediainfo", None)
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

# 后会友期 E09/E10 的真实体积（2026-11-19 插件日志：7358.56MB / 6892.11MB）。
E09_SIZE = int(7358.56 * 1024 * 1024)
E10_SIZE = int(6892.11 * 1024 * 1024)


def _tv_detail(title="后会友期", year=2026, season=1, episodes=(9, 10),
            organized=(), sizes=None, kind="transfer", category="综艺",
            detail_link="https://www.themoviedb.org/tv/338938",
            image="https://image.tmdb.org/poster.jpg"):
    """终态详情（剧集），形态与 _append_tv_transfer_detail /
    _notify_pending_file_finalized 的产出一致。"""
    eps = sorted(int(e) for e in episodes if int(e or 0) > 0)
    size_map = {int(e): int(s) for e, s in (sizes or {}).items()}
    total = sum(size_map.get(e, 0) for e in eps)
    organized_set = {int(e) for e in organized}
    return {
        "type": "电视剧",
        "title": title,
        "year": year,
        "season": season,
        "episodes": list(eps),
        "image": image,
        "file_name": f"{title} - S{season:02d}E{eps[0]:02d}.mkv" if eps else "",
        "notification_kind": kind,
        "size": total,
        "size_unknown": any(size_map.get(e, 0) <= 0 for e in eps),
        "source": "115网盘",
        "organized_via_mp": bool(eps) and all(e in organized_set for e in eps),
        "organized_via_mp_episodes": {e: e in organized_set for e in eps},
        "episode_sizes": dict(size_map),
        "category": category,
        "detail_link": detail_link,
    }


def _movie_detail(title="深度安静", year=2026, size=15139759718,
            kind="transfer", organized=False, category="华语电影",
            detail_link="https://www.themoviedb.org/movie/1234567",
            image="https://image.tmdb.org/poster.jpg"):
    return {
        "type": "电影",
        "title": title,
        "year": year,
        "image": image,
        "file_name": f"{title} ({year}).mkv",
        "notification_kind": kind,
        "size": size,
        "size_unknown": size <= 0,
        "source": "115网盘",
        "organized_via_mp": bool(organized),
        "category": category,
        "detail_link": detail_link,
    }


def _real_notify_service(notify=True):
    service = object.__new__(SyncNotificationService)
    object.__setattr__(service, "_owner", SimpleNamespace())
    post_message = MagicMock()
    object.__setattr__(service, "_post_message", post_message)
    object.__setattr__(service, "_notify", notify)
    return service, post_message


def _real_transfer_details_service(owner):
    service = object.__new__(TransferDetailsService)
    object.__setattr__(service, "_owner", owner)
    return service


class _FakeTemplateMixin:
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


class TemplateNotificationTests(_FakeTemplateMixin, unittest.TestCase):
    def test_template_renders_tv_batch(self):
        """模板命中：剧集批量（E09+E10）走用户资源入库模板。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(kwargs["mtype"], notify_module.MessageType.Organize)
        self.assertEqual(
            kwargs["ctype"], notify_module.ContentType.OrganizeSuccess
        )
        self.assertEqual(
            kwargs["title"],
            "📂 已入库2个文件\n[后会友期 (2026)]"
            "(https://www.themoviedb.org/tv/338938)",
        )
        self.assertIn("📺 集数：S01E09, E10", kwargs["text"])
        self.assertIn("🎭 类别：综艺", kwargs["text"])
        self.assertIn(
            f"💾 大小：{_format_size(E09_SIZE + E10_SIZE)}", kwargs["text"]
        )
        self.assertNotIn("👥小组", kwargs["text"])
        self.assertNotIn("🌟质量", kwargs["text"])
        self.assertNotIn("⚠️失败", kwargs["text"])
        self.assertEqual(kwargs["image"], "https://image.tmdb.org/poster.jpg")
        self.assertEqual(len(_FakeTemplateHelper.calls), 1)
        call_kwargs = _FakeTemplateHelper.calls[0]
        self.assertEqual(call_kwargs["file_count"], 2)
        self.assertEqual(call_kwargs["season_episode"], "S01E09, E10")
        self.assertEqual(
            call_kwargs["total_size"], _format_size(E09_SIZE + E10_SIZE)
        )
        self.assertEqual(call_kwargs["title_year"], "后会友期 (2026)")

    def test_template_renders_movie(self):
        """模板命中：电影 file_count=1，没有集数行。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([_movie_detail()])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(
            kwargs["title"],
            "📂 已入库1个文件\n[深度安静 (2026)]"
            "(https://www.themoviedb.org/movie/1234567)",
        )
        self.assertNotIn("📺 集数", kwargs["text"])
        self.assertIn("🎭 类别：华语电影", kwargs["text"])
        self.assertIn("💾 大小：14.1 GB", kwargs["text"])
        self.assertEqual(_FakeTemplateHelper.calls[0]["file_count"], 1)
        self.assertEqual(_FakeTemplateHelper.calls[0]["season_episode"], "")

    def test_no_template_falls_back_to_hardcoded(self):
        """没配模板：渲染原样返回，退回写死格式。"""
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(
            kwargs["title"], "📂 已入库2个文件\n后会友期 (2026)"
        )
        self.assertIn("📺 集数：S01E09, E10", kwargs["text"])
        self.assertIn("🎭 类别：综艺", kwargs["text"])
        self.assertIn(
            f"💾 大小：{_format_size(E09_SIZE + E10_SIZE)}", kwargs["text"]
        )
        self.assertEqual(len(_FakeTemplateHelper.calls), 1)

    def test_render_failure_falls_back_to_hardcoded(self):
        """渲染抛错：退回写死格式，不发空通知。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        _FakeTemplateHelper.raise_error = True
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"],
            "📂 已入库2个文件\n后会友期 (2026)",
        )

    def test_empty_render_falls_back_to_hardcoded(self):
        """模板渲出空字典（标题正文都没落下）：退回写死格式。"""
        _FakeTemplateHelper.template = "{}"
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([_movie_detail()])
        self.assertEqual(post_message.call_count, 1)
        self.assertEqual(
            post_message.call_args.kwargs["title"],
            "📂 已入库1个文件\n深度安静 (2026)",
        )

    def test_unknown_size_shows_unknown(self):
        """大小未知：模板的大小行（无条件）给“未知”，不断言空值。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(episodes=(9,), sizes={9: 0}),
        ])
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("💾 大小：未知", post_message.call_args.kwargs["text"])


class BranchRoutingTests(_FakeTemplateMixin, unittest.TestCase):
    def test_mp_organize_branch_sends_nothing(self):
        """MP 整理分支：插件零发送（MP 自己发 OrganizeSuccess）。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(organized=(9, 10), sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 0)
        self.assertEqual(_FakeTemplateHelper.calls, [])

    def test_movie_mp_branch_sends_nothing(self):
        """电影 MP 整理分支：插件零发送。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _movie_detail(organized=True),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_mixed_branch_sends_strm_episodes_only(self):
        """混批：E09 strm 直出、E10 MP 整理——只发 E09 一条，
        不丢 E09、E10 不重复（MP 那侧发）。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(10,),
                sizes={9: E09_SIZE, 10: E10_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertIn("已入库1个文件", kwargs["title"])
        self.assertIn("📺 集数：S01E09", kwargs["text"])
        self.assertNotIn("E10", kwargs["text"])
        self.assertIn(f"💾 大小：{_format_size(E09_SIZE)}", kwargs["text"])
        self.assertEqual(_FakeTemplateHelper.calls[0]["file_count"], 1)

    def test_upgrade_detail_stays_with_wash_notification(self):
        """洗版详情：不走完成通知，不产生 OrganizeSuccess。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(kind="upgrade", sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_same_detail_through_both_gates_sends_once(self):
        """恰好一次：同一转存的详情同时经过两处调用点，总共只发一条。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        detail = _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE})
        service.send_transfer_completed_notification([detail])
        service.send_transfer_completed_notification([detail])
        self.assertEqual(post_message.call_count, 1)

    def test_notify_switch_off_sends_nothing(self):
        """通知开关关了：一条都不发。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service(notify=False)
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_send_failure_does_not_raise(self):
        """发送抛错：不往上抛，不打断转存；失败不占去重位。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        post_message.side_effect = RuntimeError("post boom")
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 1)
        post_message.side_effect = None
        service.send_transfer_completed_notification([
            _tv_detail(sizes={9: E09_SIZE, 10: E10_SIZE}),
        ])
        self.assertEqual(post_message.call_count, 2)


class DetailMarkerTests(unittest.TestCase):
    def _mediainfo(self, title="后会友期", year=2026):
        mediainfo = SimpleNamespace(
            title=title, year=year, category="综艺",
            detail_link="https://www.themoviedb.org/tv/338938",
        )
        mediainfo.get_poster_image = lambda: "https://image.tmdb.org/poster.jpg"
        return mediainfo

    def test_append_keeps_per_episode_branch(self):
        """_append_tv_transfer_detail：混批合并成一条详情，
        按集的分支标记都在，整批不算 MP 分支。"""
        details = []
        transfer_details_module.TransferDetailsService._append_tv_transfer_detail(
            details, self._mediainfo(), 1, [9],
            notification_kind="transfer", size_bytes=E09_SIZE,
            source_text="115网盘", file_names=["后会友期 - S01E09.mkv"],
            episode_sizes={9: E09_SIZE},
            organized_via_mp_episodes={9: False},
        )
        transfer_details_module.TransferDetailsService._append_tv_transfer_detail(
            details, self._mediainfo(), 1, [10],
            notification_kind="transfer", size_bytes=E10_SIZE,
            source_text="115网盘", file_names=["后会友期 - S01E10.mkv"],
            episode_sizes={10: E10_SIZE},
            organized_via_mp_episodes={10: True},
        )
        self.assertEqual(len(details), 1)
        detail = details[0]
        self.assertEqual(detail["episodes"], [9, 10])
        self.assertEqual(
            detail["organized_via_mp_episodes"], {9: False, 10: True}
        )
        self.assertFalse(detail["organized_via_mp"])
        self.assertEqual(detail["category"], "综艺")
        self.assertEqual(
            detail["detail_link"], "https://www.themoviedb.org/tv/338938"
        )

    def test_append_all_mp_branch_marks_whole_detail(self):
        """全是 MP 分支：整条详情标 MP 分支。"""
        details = []
        transfer_details_module.TransferDetailsService._append_tv_transfer_detail(
            details, self._mediainfo(), 1, [9, 10],
            notification_kind="transfer",
            size_bytes=E09_SIZE + E10_SIZE,
            source_text="115网盘",
            episode_sizes={9: E09_SIZE, 10: E10_SIZE},
            organized_via_mp_episodes={9: True, 10: True},
        )
        self.assertEqual(len(details), 1)
        self.assertTrue(details[0]["organized_via_mp"])

    def test_aggregate_merges_branch_maps(self):
        """_aggregate_transfer_details：两条同剧详情合并，
        按集的分支标记合并、整批标记重算。"""
        aggregated = transfer_details_module.TransferDetailsService._aggregate_transfer_details([
            _tv_detail(episodes=(9,), sizes={9: E09_SIZE}),
            _tv_detail(
                episodes=(10,), organized=(10,), sizes={10: E10_SIZE}
            ),
        ])
        self.assertEqual(len(aggregated), 1)
        detail = aggregated[0]
        self.assertEqual(detail["episodes"], [9, 10])
        self.assertEqual(
            detail["organized_via_mp_episodes"], {9: False, 10: True}
        )
        self.assertFalse(detail["organized_via_mp"])


class FinalizedBatchTests(_FakeTemplateMixin, unittest.TestCase):
    def test_finalized_batch_routes_kinds_and_branches(self):
        """门 2（_send_finalized_batch）端到端：洗版走原通知、
        strm 直出走完成通知、MP 整理分支插件零发送；同剧混批
        先聚合再按集切分，只发 strm 那几集。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        wash_mock = MagicMock()
        owner = SimpleNamespace(
            _notify=True,
            _file_finalized=None,
            send_transfer_notification=wash_mock,
            send_transfer_completed_notification=(
                service.send_transfer_completed_notification
            ),
        )
        batch_service = _real_transfer_details_service(owner)
        batch_service._send_finalized_batch([
            _tv_detail(
                kind="upgrade", episodes=(9, 10),
                sizes={9: E09_SIZE, 10: E10_SIZE},
            ),
            _tv_detail(episodes=(9, 10), sizes={9: E09_SIZE, 10: E10_SIZE}),
            _tv_detail(
                episodes=(12, 13), organized=(12, 13),
                sizes={12: 1000000000, 13: 2000000000},
            ),
        ])
        self.assertEqual(wash_mock.call_count, 1)
        wash_details = wash_mock.call_args.args[0]
        self.assertEqual(len(wash_details), 1)
        self.assertEqual(
            wash_details[0].get("notification_kind"), "upgrade"
        )
        # strm E09+E10 与 MP E12+E13 同剧同季同 kind，聚合后一条详情、
        # 按集切分只发 E09+E10。
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(kwargs["ctype"], notify_module.ContentType.OrganizeSuccess)
        self.assertIn("已入库2个文件", kwargs["title"])
        self.assertIn("📺 集数：S01E09, E10", kwargs["text"])
        self.assertNotIn("E12", kwargs["text"])
        self.assertNotIn("E13", kwargs["text"])


if __name__ == "__main__":
    unittest.main()
