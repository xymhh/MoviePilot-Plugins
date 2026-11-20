"""v1.28.2：MP 整理分支的补发通知（终态门兜底）。

背景（2026-11-20《罪兽之法》E01–E04）：四集全部入库成功，只有 E04
收到入库通知——E04 走 MP 后台路径（MP 聚合器 30 秒防抖后发），E01/E02/E03
是 MP 前台手动整理（manual=True 且 background=False），MP 按设计静默、
通知根本没生成。插件自己触发的整理一定传 background=True，MP 一定会发；
只有外部提交的 MP 整理成功才需要插件补发。

判定信号（已在 MP v3.2.6 源码核实）：插件触发时给 do_transfer 传固定
downloader 标记 ``CloudSubscribeX``；MP 结算时把 task.downloader 原样
写进整理历史行（success_fields 无条件写 download_hash 与 downloader）。
终态轮询到成功时读回该行的 downloader：等于标记 → 插件这次触发，
MP 会发，插件跳过；不等于（None 或其他下载器名）→ 外部提交，MP 不会
发，插件补发一条；读不到 / 读失败 → 未知，保守跳过（宁可漏发，不可
重复）。补发与 strm 分支同一发送器、同一文案、同一 600 秒去重。

本文件用真实类验证三层：判定信号（tracker 读行 + trigger 传标记）、
详情字段与聚合（transfer_details）、发送分流（notify 发送器）。
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
tracker_module = load_module(
    "cloudsubscribex.core.services.mp_organize_tracker",
    "plugins.v2/cloudsubscribex/core/services/mp_organize_tracker.py",
)
MpOrganizeTracker = tracker_module.MpOrganizeTracker

# 他 MP 里那条真实模板（设定 → 通知 → 通知模板 → “资源入库 Jinja2
# JSON”，2026-11-19 从设定 → 通知抄回，逐字）。raw 字符串：模板里的 \n 是字面量
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

# 罪兽之法四集的真实体积（2026-11-20 插件日志：E01 5.50GB / E02 6.53GB /
# E03 6.45GB / E04 7.98GB，取整到字节）。
E01_SIZE = int(5.50 * 1024 * 1024 * 1024)
E02_SIZE = int(6.53 * 1024 * 1024 * 1024)
E03_SIZE = int(6.45 * 1024 * 1024 * 1024)
E04_SIZE = int(7.98 * 1024 * 1024 * 1024)


def _triggered_map(eps, organized, triggered):
    """按集的触发来源映射（三态）：MP 分支的集取 triggered（字典按集、
    单值全集同值、None=未知），strm 分支的集为 None（无意义）。"""
    organized_set = {int(e) for e in organized}
    result = {}
    for episode in eps:
        if episode not in organized_set:
            result[episode] = None
            continue
        if isinstance(triggered, dict):
            result[episode] = triggered.get(episode)
        else:
            result[episode] = triggered
    return result


def _triggered_scalar(eps, organized, triggered):
    """整条详情的触发来源标量：MP 分支的集全 True 才 True，任一 False
    即 False，其余（缺标记或全未知）为 None；没有 MP 分支的集为 None。"""
    organized_set = {int(e) for e in organized}
    if not organized_set:
        return None
    values = [_triggered_map(eps, organized, triggered)[e] for e in organized_set]
    if all(value is True for value in values):
        return True
    if any(value is False for value in values):
        return False
    return None


def _tv_detail(title="罪兽之法", year=2026, season=1, episodes=(1, 2, 3, 4),
            organized=(), triggered=None, sizes=None, kind="transfer",
            category="国产剧",
            detail_link="https://www.themoviedb.org/tv/332839",
            image="https://image.tmdb.org/poster.jpg"):
    """终态详情（剧集），形态与 _notify_pending_file_finalized 的产出一致，
    另带 v1.28.2 的触发来源字段。"""
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
        "organized_via_mp_triggered": _triggered_scalar(eps, organized, triggered),
        "organized_via_mp_triggered_episodes": _triggered_map(
            eps, organized, triggered
        ),
        "episode_sizes": dict(size_map),
        "category": category,
        "detail_link": detail_link,
    }


def _movie_detail(title="深度安静", year=2026, size=15139759718,
            kind="transfer", organized=False, triggered=None,
            category="华语电影",
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
        "organized_via_mp_triggered": triggered if organized else None,
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


class BackstopRoutingTests(_FakeTemplateMixin, unittest.TestCase):
    def test_mp_branch_external_sends_one(self):
        """MP 分支且外部提交（触发来源 False）：补发一条，4 集一并。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(1, 2, 3, 4), triggered=False,
                sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertEqual(kwargs["mtype"], notify_module.MessageType.Organize)
        self.assertEqual(
            kwargs["ctype"], notify_module.ContentType.OrganizeSuccess
        )
        self.assertIn("已入库4个文件", kwargs["title"])
        self.assertIn("📺 集数：S01E01, E02, E03, E04", kwargs["text"])
        self.assertIn(
            f"💾 大小：{_format_size(E01_SIZE + E02_SIZE + E03_SIZE + E04_SIZE)}",
            kwargs["text"],
        )
        self.assertEqual(_FakeTemplateHelper.calls[0]["file_count"], 4)

    def test_mp_branch_own_trigger_sends_nothing(self):
        """MP 分支且插件这次触发（触发来源 True）：MP 会发，插件零发送。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(1, 2, 3, 4), triggered=True,
                sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 0)
        self.assertEqual(_FakeTemplateHelper.calls, [])

    def test_mp_branch_unknown_trigger_sends_nothing(self):
        """MP 分支且触发来源未知（None）：保守跳过，宁可漏发，不可重复。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(1, 2, 3, 4), triggered=None,
                sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_mp_branch_missing_trigger_field_sends_nothing(self):
        """旧口径详情（根本没有触发来源字段）：按未知处理，零发送。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        detail = _tv_detail(
            organized=(1, 2, 3, 4),
            sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
        )
        detail.pop("organized_via_mp_triggered", None)
        detail.pop("organized_via_mp_triggered_episodes", None)
        service.send_transfer_completed_notification([detail])
        self.assertEqual(post_message.call_count, 0)

    def test_mixed_three_way_splits(self):
        """混批四路切分：E01 strm 直出、E02 MP 分支插件触发、E03 MP 分支
        外部提交、E04 MP 分支来源未知——只发两条（E01 一条、E03 补发
        一条），E02 不重复（MP 那侧发）、E04 保守跳过。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(2, 3, 4),
                triggered={2: True, 3: False, 4: None},
                sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 2)
        first = post_message.call_args_list[0].kwargs
        second = post_message.call_args_list[1].kwargs
        self.assertIn("已入库1个文件", first["title"])
        self.assertIn("📺 集数：S01E01", first["text"])
        self.assertNotIn("E02", first["text"])
        self.assertIn(f"💾 大小：{_format_size(E01_SIZE)}", first["text"])
        self.assertIn("已入库1个文件", second["title"])
        self.assertIn("📺 集数：S01E03", second["text"])
        self.assertNotIn("E02", second["text"])
        self.assertNotIn("E04", second["text"])
        self.assertIn(f"💾 大小：{_format_size(E03_SIZE)}", second["text"])
        self.assertEqual(
            [_FakeTemplateHelper.calls[0]["file_count"],
            _FakeTemplateHelper.calls[1]["file_count"]],
            [1, 1],
        )

    def test_movie_mp_branch_external_sends_one(self):
        """电影 MP 分支且外部提交：补发一条，file_count=1，没有集数行。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _movie_detail(organized=True, triggered=False),
        ])
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        self.assertIn("已入库1个文件", kwargs["title"])
        self.assertIn("[深度安静 (2026)]", kwargs["title"])
        self.assertNotIn("📺 集数", kwargs["text"])
        self.assertIn("💾 大小：14.1 GB", kwargs["text"])
        self.assertEqual(_FakeTemplateHelper.calls[0]["file_count"], 1)

    def test_movie_mp_branch_own_trigger_sends_nothing(self):
        """电影 MP 分支且插件触发：零发送。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _movie_detail(organized=True, triggered=True),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_movie_mp_branch_unknown_sends_nothing(self):
        """电影 MP 分支且来源未知：零发送。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        service.send_transfer_completed_notification([
            _movie_detail(organized=True, triggered=None),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_notify_switch_off_sends_nothing(self):
        """通知开关关了：补发也不发。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service(notify=False)
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(1, 2, 3, 4), triggered=False,
                sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 0)

    def test_backstop_dedup_across_gates(self):
        """恰好一次：同一外部 MP 详情同时经过两处调用点，总共只发一条。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        detail = _tv_detail(
            organized=(1, 2, 3, 4), triggered=False,
            sizes={1: E01_SIZE, 2: E02_SIZE, 3: E03_SIZE, 4: E04_SIZE},
        )
        service.send_transfer_completed_notification([detail])
        service.send_transfer_completed_notification([detail])
        self.assertEqual(post_message.call_count, 1)

    def test_strm_part_failure_does_not_block_backstop(self):
        """一部分发送失败不拦另一部分：strm 部分抛错，补发部分照常发出；
        失败的部分不占去重位。"""
        _FakeTemplateHelper.template = USER_ORGANIZE_TEMPLATE
        service, post_message = _real_notify_service()
        post_message.side_effect = [RuntimeError("post boom"), None]
        service.send_transfer_completed_notification([
            _tv_detail(
                organized=(2,),
                triggered={2: False},
                sizes={1: E01_SIZE, 2: E02_SIZE},
            ),
        ])
        self.assertEqual(post_message.call_count, 2)
        second = post_message.call_args_list[1].kwargs
        self.assertIn("📺 集数：S01E02", second["text"])


class TriggeredMergeTests(unittest.TestCase):
    def test_aggregate_merges_triggered_maps(self):
        """_aggregate_transfer_details：两条同剧详情合并，触发来源映射
        合并、标量重算（任一集外部提交即 False）。"""
        aggregated = TransferDetailsService._aggregate_transfer_details([
            _tv_detail(episodes=(1,), sizes={1: E01_SIZE}),
            _tv_detail(
                episodes=(2,), organized=(2,), triggered=False,
                sizes={2: E02_SIZE},
            ),
        ])
        self.assertEqual(len(aggregated), 1)
        detail = aggregated[0]
        self.assertEqual(detail["episodes"], [1, 2])
        self.assertEqual(
            detail["organized_via_mp_triggered_episodes"],
            {1: None, 2: False},
        )
        self.assertIs(detail["organized_via_mp_triggered"], False)
        self.assertFalse(detail["organized_via_mp"])

    def test_aggregate_all_own_trigger_scalar_true(self):
        """全是插件触发：标量 True。"""
        aggregated = TransferDetailsService._aggregate_transfer_details([
            _tv_detail(
                episodes=(1,), organized=(1,), triggered=True,
                sizes={1: E01_SIZE},
            ),
            _tv_detail(
                episodes=(2,), organized=(2,), triggered=True,
                sizes={2: E02_SIZE},
            ),
        ])
        self.assertEqual(len(aggregated), 1)
        detail = aggregated[0]
        self.assertEqual(
            detail["organized_via_mp_triggered_episodes"],
            {1: True, 2: True},
        )
        self.assertIs(detail["organized_via_mp_triggered"], True)
        self.assertTrue(detail["organized_via_mp"])

    def test_aggregate_unknown_scalar_none(self):
        """触发来源全未知：标量 None（三态不坍缩成 False）。"""
        aggregated = TransferDetailsService._aggregate_transfer_details([
            _tv_detail(
                episodes=(1, 2), organized=(1, 2), triggered=None,
                sizes={1: E01_SIZE, 2: E02_SIZE},
            ),
        ])
        self.assertEqual(len(aggregated), 1)
        detail = aggregated[0]
        self.assertEqual(
            detail["organized_via_mp_triggered_episodes"],
            {1: None, 2: None},
        )
        self.assertIs(detail["organized_via_mp_triggered"], None)

    def test_aggregate_later_detail_wins_per_episode(self):
        """同一集在两条详情里触发来源不同：后到的覆盖先到的。"""
        aggregated = TransferDetailsService._aggregate_transfer_details([
            _tv_detail(
                episodes=(2,), organized=(2,), triggered=False,
                sizes={2: E02_SIZE},
            ),
            _tv_detail(
                episodes=(2,), organized=(2,), triggered=True,
                sizes={2: E02_SIZE},
            ),
        ])
        self.assertEqual(len(aggregated), 1)
        detail = aggregated[0]
        self.assertEqual(
            detail["organized_via_mp_triggered_episodes"], {2: True}
        )
        self.assertIs(detail["organized_via_mp_triggered"], True)


class FakeHistoryOper:
    """桩整理历史 Oper：按预设返回成功行，并记录查询参数。"""

    def __init__(self, success=None):
        self.success = success
        self.calls = []

    def get_success_by_src(self, src, storage=None):
        self.calls.append(("get_success_by_src", src, storage))
        return self.success


class FakePendingOper:
    """桩 durable 待整理登记 Oper（get_success_downloader 不用）。"""

    def get_by_identity(self, *, storage, src_path):
        return None


class FakeJobview:
    """桩 MP 整理队列视图（get_success_downloader 不用）。"""

    def list_jobs(self):
        return []


_SRC = "/Alist/影视/moviepilot/转存/罪兽之法 - S01E01.mkv"


def _mp_organize(src_path=_SRC):
    return {
        "src_path": src_path,
        "src_storage": "local",
        "triggered_at": 1000000.0,
        "target_dir": "/媒体库/剧集",
        "target_storage": "u115cs",
        "target_name": "剧集",
        "file_name": "罪兽之法 - S01E01.mkv",
    }


class SuccessDownloaderTests(unittest.TestCase):
    def _tracker(self, success):
        return MpOrganizeTracker(
            history_oper=FakeHistoryOper(success=success),
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )

    def test_marker_downloader_means_plugin_trigger(self):
        """成功行 downloader 是触发标记 → (True, 标记)。"""
        tracker = self._tracker(
            SimpleNamespace(dest="/媒体库/剧集/罪兽之法/Season 1/x.mkv",
                            downloader="CloudSubscribeX")
        )
        found, downloader = tracker.get_success_downloader(_mp_organize())
        self.assertTrue(found)
        self.assertEqual(downloader, "CloudSubscribeX")

    def test_none_downloader_means_external(self):
        """成功行 downloader 为空（MP 前台手动整理）→ (True, None)。"""
        tracker = self._tracker(
            SimpleNamespace(dest="/媒体库/剧集/罪兽之法/Season 1/x.mkv",
                            downloader=None)
        )
        found, downloader = tracker.get_success_downloader(_mp_organize())
        self.assertTrue(found)
        self.assertIsNone(downloader)

    def test_other_downloader_means_external(self):
        """成功行 downloader 是别的下载器名 → (True, 原值)。"""
        tracker = self._tracker(
            SimpleNamespace(dest="/媒体库/剧集/罪兽之法/Season 1/x.mkv",
                            downloader="aria2")
        )
        found, downloader = tracker.get_success_downloader(_mp_organize())
        self.assertTrue(found)
        self.assertEqual(downloader, "aria2")

    def test_no_success_row_means_unknown(self):
        """没有成功行 → (False, None)，调用方按未知处理、绝不补发。"""
        tracker = self._tracker(None)
        found, downloader = tracker.get_success_downloader(_mp_organize())
        self.assertFalse(found)
        self.assertIsNone(downloader)

    def test_oper_error_means_unknown(self):
        """读历史抛错 → (False, None)，不往上抛。"""
        history = FakeHistoryOper(success=None)

        def _boom(src, storage=None):
            raise RuntimeError("db boom")

        history.get_success_by_src = _boom
        tracker = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        found, downloader = tracker.get_success_downloader(_mp_organize())
        self.assertFalse(found)
        self.assertIsNone(downloader)

    def test_missing_src_path_means_unknown(self):
        """MP 整理身份缺少源路径 → (False, None)，不查库。"""
        history = FakeHistoryOper(
            success=SimpleNamespace(dest="/x.mkv", downloader=None)
        )
        tracker = MpOrganizeTracker(
            history_oper=history,
            pending_oper=FakePendingOper(),
            jobview=FakeJobview(),
        )
        found, downloader = tracker.get_success_downloader({"src_path": ""})
        self.assertFalse(found)
        self.assertIsNone(downloader)
        self.assertEqual(history.calls, [])


class FakeFileItem:
    """只记构造参数的 FileItem 替身。"""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeStorageChain:
    """get_item 可控：found 返回原项，missing 返回 None。"""

    next_result = "found"
    last_fileitem = None

    def get_item(self, fileitem):
        FakeStorageChain.last_fileitem = fileitem
        if FakeStorageChain.next_result == "found":
            return fileitem
        return None


class FakeTransferChain:
    """do_transfer 可控，记下每一次调用。"""

    calls = []
    next_result = (True, "")

    def do_transfer(self, fileitem, **kwargs):
        FakeTransferChain.calls.append((fileitem, kwargs))
        return FakeTransferChain.next_result


class FakeDownloadChain:
    """get_no_exists_info 替身：默认“不在库”，整理放行。"""

    def get_no_exists_info(self, meta, mediainfo):
        return False, {}


class FakeMediaInfo:
    """app.core.context.MediaInfo 替身：只存构造参数。"""

    title = None
    type = None
    tmdb_id = None
    year = None
    season = None
    category = ""
    library_category = ""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeDirectoryConf:
    """DirectoryHelper.get_dir 返回的目录条目替身（剧集条目）。"""

    def __init__(self, **kwargs):
        self.library_path = "/媒体库/剧集"
        self.library_storage = "u115cs"
        self.name = "剧集"
        self.library_type_folder = False
        self.library_category_folder = False
        self.__dict__.update(kwargs)

    def model_copy(self, deep=False, update=None):
        clone = FakeDirectoryConf(**self.__dict__)
        if update:
            clone.__dict__.update(update)
        return clone

    def copy(self, deep=False, update=None):
        return self.model_copy(deep=deep, update=update)


class FakeDirectoryHelper:
    """get_dir 记下调用参数，默认返回剧集条目。"""

    calls = []
    next_result = "found"
    last_conf = None

    def get_dir(self, media=None, include_unsorted=False, **kwargs):
        FakeDirectoryHelper.calls.append(
            {
                "media": media,
                "include_unsorted": include_unsorted,
                "kwargs": dict(kwargs),
            }
        )
        if FakeDirectoryHelper.next_result == "found":
            FakeDirectoryHelper.last_conf = FakeDirectoryConf()
            return FakeDirectoryHelper.last_conf
        FakeDirectoryHelper.last_conf = None
        return None


def _install_trigger_stubs():
    """重注 trigger 测试用的全部宿主桩（每个用例执行前调一次：
    全量收集时排在本文件之后的文件会用 register_mock 覆盖这些模块，
    模块级那次注册到执行时已被抹掉）。"""
    register_module("app.chain.storage", StorageChain=FakeStorageChain)
    register_module("app.chain.transfer", TransferChain=FakeTransferChain)
    register_module("app.chain.download", DownloadChain=FakeDownloadChain)
    register_module("app.schemas.file", FileItem=FakeFileItem)
    register_module("app.core.context", MediaInfo=FakeMediaInfo)
    register_module("app.helper.directory", DirectoryHelper=FakeDirectoryHelper)
    register_module("app.application.directory", DirectoryHelper=FakeDirectoryHelper)


_install_trigger_stubs()

mp_organize_module = load_module(
    "cloudsubscribex.handlers.sync.mp_organize",
    "plugins.v2/cloudsubscribex/handlers/sync/mp_organize.py",
)
MpOrganizeMixin = mp_organize_module.MpOrganizeMixin
PLUGIN_TRIGGER_DOWNLOADER = mp_organize_module.PLUGIN_TRIGGER_DOWNLOADER


def _trigger_owner(**overrides):
    values = {
        "_mp_organize_after_share_save": True,
        "_video_extensions": [".mkv", ".mp4", ".avi", ".iso"],
        "_cloud_transfer_path": "/Alist/影视/moviepilot/转存",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TriggerMarkerTests(unittest.TestCase):
    def setUp(self):
        global MediaType
        MediaType = set_media_type()
        _install_trigger_stubs()
        FakeStorageChain.next_result = "found"
        FakeStorageChain.last_fileitem = None
        FakeTransferChain.calls = []
        FakeTransferChain.next_result = (True, "")
        FakeDirectoryHelper.calls = []
        FakeDirectoryHelper.next_result = "found"
        FakeDirectoryHelper.last_conf = None
        self.mediainfo = MagicMock()
        self.mediainfo.type = MediaType.TV
        self.mediainfo.category = "国产剧"
        self.mixin = MpOrganizeMixin(_trigger_owner())

    def test_do_transfer_receives_trigger_marker(self):
        """触发成功：do_transfer 收到固定 downloader 标记，mp_info 里
        也记一份；manual/background 仍为 True（MP 会发通知的前提）。"""
        self.assertEqual(PLUGIN_TRIGGER_DOWNLOADER, "CloudSubscribeX")
        ok, info = self.mixin.trigger_mp_organize(
            "/Alist/影视/moviepilot/转存", "罪兽之法 - S01E01.mkv",
            mediainfo=self.mediainfo, season=1,
        )
        self.assertTrue(ok)
        self.assertEqual(info["trigger_downloader"], "CloudSubscribeX")
        fileitem, kwargs = FakeTransferChain.calls[0]
        self.assertEqual(kwargs["downloader"], "CloudSubscribeX")
        self.assertTrue(kwargs["manual"])
        self.assertTrue(kwargs["background"])
