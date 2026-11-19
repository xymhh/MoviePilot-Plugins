"""v1.25.0 Issue A：开始下载通知三路补发。

三条路（订阅自动跑 / TG 监控 / 手动转存）都汇到同一个入库管线
（``TransferOpsMixin._run_ingest_pipeline``），磁力/ED2K 走离线提交
（``SyncHandler._submit_offline_packages``），两处各发一条：

* 管线：判重门已过、确有文件要转时发一次；判重全拦下不发。
* 离线提交：网盘接受离线下载（``add_offline_download`` 返回真值）时
  发一次；提交失败/回滚不发。
* 通知本身：``SyncNotificationService.send_transfer_started_notification``
  走 ``post_message(mtype=MessageType.Download,
  ctype=ContentType.DownloadAdded)``——与 MP 原生 DownloadAdded 同一
  mtype/ctype、同一路由开关；标题/正文由插件直接给出（不走用户模板
  渲染，见 notify.py 里的说明）。
* 来源随订阅对象下沉（``_do_sync`` 挂 ``_transfer_source``），
  管线与离线提交统一读取，不穿参。
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from plugin_env import (
    ensure_package,
    install_app_mocks,
    install_core_delegation,
    load_module,
    register_mock,
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
# 与 test_notify_transfer_rich.py 同款：types 用显式 enum 桩，断言才有意义。
register_mock(
    "app.schemas.types",
    NotificationType=type("NotificationType", (), {"Plugin": "Plugin"}),
    MessageType=type("MessageType", (), {"Download": "Download"}),
    ContentType=type("ContentType", (), {"DownloadAdded": "DownloadAdded"}),
)
install_core_delegation()
ensure_package("cloudsubscribex.core")

transfer_ops = sys.modules.get("cloudsubscribex.handlers.sync.transfer_ops")


def _format_size(value):
    """确定性大小格式化：mock 环境的 StringUtils.format_size 不可断言。

    必须在加载 notify.py / transfer_ops.py 之前覆盖模块属性，否则两模块
    ``from ...drive.common import format_size`` 绑走的是 MagicMock，
    ``" · ".join(meta)`` 会抛 TypeError。
    """
    amount = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return "0 B"


sys.modules["cloudsubscribex.drive.common"].format_size = _format_size

if transfer_ops is None:
    transfer_ops = load_module(
        "cloudsubscribex.handlers.sync.transfer_ops",
        "plugins.v2/cloudsubscribex/handlers/sync/transfer_ops.py",
    )
TransferOpsMixin = transfer_ops.TransferOpsMixin

notify_module = sys.modules.get("cloudsubscribex.handlers.sync.notify")
if notify_module is None:
    notify_module = load_module(
        "cloudsubscribex.handlers.sync.notify",
        "plugins.v2/cloudsubscribex/handlers/sync/notify.py",
    )
SyncNotificationService = notify_module.SyncNotificationService


def _item(file_id, episode, name, size=1000):
    return {
        "file": {"id": file_id, "name": name, "size": size},
        "episode": episode,
        "score": 50,
        "target_name": name,
        "target_dir": "/Alist/影视/moviepilot/媒体库/剧集",
    }


def _mediainfo(title="玫瑰战争", year=2025):
    mediainfo = SimpleNamespace(title=title, year=year, tmdb_id=299276)
    mediainfo.get_poster_image = lambda: "https://image.tmdb.org/poster.jpg"
    return mediainfo


def _subscribe(source=None, via=None):
    subscribe = SimpleNamespace(name="玫瑰战争", id=1)
    if source is not None:
        subscribe._transfer_source = source
    if via is not None:
        subscribe._discovered_via = via
    return subscribe


def _real_notify_service(notify=True):
    """真实通知服务 + 桩 post_message：测的是服务方法的真实组装。"""
    service = object.__new__(SyncNotificationService)
    object.__setattr__(service, "_owner", SimpleNamespace())
    post_message = MagicMock()
    object.__setattr__(service, "_post_message", post_message)
    object.__setattr__(service, "_notify", notify)
    return service, post_message


def _attach_fake_owner(service, mapping):
    owner = service.__dict__.get("_owner")
    if owner is None or not hasattr(owner, "__dict__"):
        owner = SimpleNamespace()
    for field_name, method_names in mapping.items():
        namespace = SimpleNamespace()
        for method_name in method_names:
            setattr(namespace, method_name, getattr(service, method_name))
        setattr(owner, field_name, namespace)
    object.__setattr__(service, "_owner", owner)
    return owner


class _PipelineService(TransferOpsMixin):
    """只测管线编排 + 开始通知：三阶段全部桩掉。"""

    def __init__(self, batch_size=50, gate=None, notify_service=None):
        self._transfer_task_batch_size = batch_size
        self._organize_after_transfer = True
        self._gate = gate
        self._stop_after = None
        self._stop_calls = 0
        self.transfer_calls = []
        self.organize_calls = []
        self.confirm_calls = []
        self.verify_calls = []
        owner = _attach_fake_owner(self, {
            "resource_transfer": [
                "_is_cloud_resource_url",
                "_is_direct_cloud_resource_url",
            ],
            "metadata": ["_set_task_phase"],
            "landed_verify": ["verify_landed_items"],
        })
        if notify_service is not None:
            owner.notifications = notify_service

    def _filter_entry_dedup_items(self, items, mediainfo, subscribe, season):
        if self._gate is not None:
            return self._gate(items)
        return list(items), {}

    def _ingest_stage_transfer(
            self, selected, share_url, mediainfo, season,
            organize_enabled, rename_items, cloud_resource,
            direct_cloud_resource, file_ids,
    ):
        self.transfer_calls.append(list(selected))
        return (
            list(selected),
            [str(item["file"]["id"]) for item in selected],
            False,
        )

    def _ingest_stage_organize(
            self, processed, success_id_set, mediainfo, season,
            share_url, cross_batch, direct_cloud_resource,
            organize_enabled, media_type="tv",
    ):
        self.organize_calls.append({
            "season": season, "media_type": media_type,
            "count": len(processed),
        })
        return set()

    def _ingest_stage_confirm(
            self, processed, success_id_set, mp_organized_ids,
            share_url, mediainfo, subscribe, season, sub_key,
            track_subscription=True, transient_target=False,
    ):
        self.confirm_calls.append({"season": season, "count": len(processed)})
        return [
            {
                "item": item,
                "file_id": str(item["file"]["id"]),
                "success": True,
                "pending_key": "",
                "failure_reason": "",
                "strm_path": "/strm/x.strm",
                "transferred": True,
            }
            for item in processed
        ]

    def verify_landed_items(self, subscribe, mediainfo, season, checks):
        self.verify_calls.append(list(checks))
        return {
            check["key"]: {"landed": True, "path": f"/lib/{check['key']}"}
            for check in checks
        }

    def _is_cloud_resource_url(self, url):
        return False

    def _is_direct_cloud_resource_url(self, url):
        return False

    def _is_offline_url(self, url):
        return False

    def _stop_requested(self):
        self.__dict__["_stop_calls"] = self.__dict__.get("_stop_calls", 0) + 1
        return False

    def _set_task_phase(self, *args, **kwargs):
        return None

    def _wait_transfer_batch_interval(self):
        return True


def _run_tv(service, source=None, via=None):
    items = [
        _item("f24", 24, "玫瑰战争 - S01E24 - 第24集.mp4"),
        _item("f25", 25, "玫瑰战争 - S01E25 - 第25集.mp4"),
    ]
    return service._transfer_episode_items(
        items, "https://115.com/s/test", _mediainfo(), _subscribe(source, via), 1, "k",
    )


class TransferStartedNotificationTests(unittest.TestCase):
    def test_subscribe_source_notified_once(self):
        """订阅自动跑：TV 管线只发一条，来源标 订阅，集数与大小都在文案里。"""
        service, post_message = _real_notify_service()
        pipeline = _PipelineService(notify_service=service)
        results = _run_tv(pipeline, source="subscribe")
        self.assertEqual(len(results), 2)
        self.assertEqual(post_message.call_count, 1)
        kwargs = post_message.call_args.kwargs
        # 以 notify 模块加载时绑定的对象为准：别的测试文件可能先加载了
        # 它（当时 types 还是旧 mock），断言必须跟模块内用的是同一对象。
        self.assertEqual(kwargs["mtype"], notify_module.MessageType.Download)
        self.assertEqual(kwargs["ctype"], notify_module.ContentType.DownloadAdded)
        self.assertEqual(kwargs["title"], "【网盘订阅助手】开始下载")
        self.assertIn("玫瑰战争 S01 E24, E25", kwargs["text"])
        self.assertIn("订阅", kwargs["text"])
        self.assertIn("2.0 KB", kwargs["text"])
        self.assertEqual(kwargs["image"], "https://image.tmdb.org/poster.jpg")

    def test_tg_source_labels_channel(self):
        """TG 监控：来源标 TG 频道 + 频道名（via 里的 tg: 前缀与消息 id 去掉）。"""
        service, post_message = _real_notify_service()
        pipeline = _PipelineService(notify_service=service)
        _run_tv(pipeline, source="tg", via="tg:@Channel/12345")
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("TG 频道 @Channel", post_message.call_args.kwargs["text"])

    def test_manual_source_labels_manual(self):
        """手动转存：来源标 手动。"""
        service, post_message = _real_notify_service()
        pipeline = _PipelineService(notify_service=service)
        _run_tv(pipeline, source="manual")
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("手动", post_message.call_args.kwargs["text"])

    def test_movie_headline_uses_year(self):
        """电影：标题用 电影名 (年份)，不带季集。"""
        service, post_message = _real_notify_service()
        pipeline = _PipelineService(notify_service=service)
        results = pipeline._run_ingest_pipeline(
            [_item("m1", 1, "秒速5厘米 (2025).mkv")],
            mediainfo=_mediainfo(title="秒速5厘米", year=2025),
            subscribe=_subscribe(source="subscribe"),
            season=1,
            sub_key="k",
            share_url="https://115.com/s/test",
            media_type="movie",
            stop_on_first_success=True,
            verify_landed=False,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("秒速5厘米 (2025)", post_message.call_args.kwargs["text"])

    def test_many_episodes_collapse(self):
        """超过 5 集：文案收成 E01-E08 共8集，不把集数全列出来。"""
        service, post_message = _real_notify_service()
        pipeline = _PipelineService(notify_service=service)
        items = [_item(f"f{i}", i, f"玫瑰战争 - S01E{i:02d}.mp4") for i in range(1, 9)]
        pipeline._transfer_episode_items(
            items, "https://115.com/s/test", _mediainfo(), _subscribe("subscribe"), 1, "k",
        )
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("E01-E08 共8集", post_message.call_args.kwargs["text"])

    def test_dedup_gate_blocks_all_no_notification(self):
        """判重门把全部文件拦下：管线提前返回，一条通知都不发。"""
        service, post_message = _real_notify_service()
        gate = lambda items: ([], {0: {"success": False, "skipped": True}})
        pipeline = _PipelineService(gate=gate, notify_service=service)
        results = _run_tv(pipeline, source="subscribe")
        self.assertEqual(len(results), 1)
        self.assertEqual(post_message.call_count, 0)

    def test_notify_switch_off_no_notification(self):
        """通知总开关关了：转存照常跑，通知不发。"""
        service, post_message = _real_notify_service(notify=False)
        pipeline = _PipelineService(notify_service=service)
        results = _run_tv(pipeline, source="subscribe")
        self.assertEqual(len(results), 2)
        self.assertEqual(post_message.call_count, 0)

    def test_post_message_failure_does_not_break_transfer(self):
        """通知发送本身抛错：只记日志，转存结果不受影响。"""
        service, post_message = _real_notify_service()
        post_message.side_effect = RuntimeError("telegram down")
        pipeline = _PipelineService(notify_service=service)
        results = _run_tv(pipeline, source="subscribe")
        self.assertEqual(len(results), 2)


# ---- 离线提交（磁力/ED2K）：SyncHandler._submit_offline_packages ----
# service.py 很重，沿用 test_component_wiring.py 的 mock 清单加载。
# 注意：这里只补 standalone 加载时真正缺的 app.* 占位，且已在
# sys.modules 里的名字一律不碰——别的测试文件（如
# test_bangumi_episode_total.py）用 register_mock 配了用例内才生效的桩，
# ensure_package 会把它们的 MagicMock 换成空 AutoMockModule，桩就丢了。
from plugin_env import ensure_package, install_real_ttl_cache  # noqa: E402

install_app_mocks(
    "app.db", "app.db.subscribe_oper", "app.modules",
    "app.modules.filemanager", "app.modules.filemanager.transhandler",
    "app.helper", "app.application", "app.adapters",
    "cloudsubscribex.core.media",
    "cloudsubscribex.handlers.notification", "cloudsubscribex.handlers.search",
    "cloudsubscribex.handlers.subscription", "cloudsubscribex.search.types",
    *[
        f"cloudsubscribex.handlers.sync.{sibling}"
        for sibling in (
            "baseline", "cleanup", "episode_dedup", "history", "landed",
            "matching", "metadata", "movie", "naming", "notify",
            "platform_history", "postprocess", "pt_upgrade", "resources",
            "retry", "rule_scoring", "subtitles", "television", "upgrade", "utils",
        )
    ],
)
for _name in (
    # service.py 真实加载路径上用到的 app.*（transfer_details /
    # media_targets / mp_organize / subscribe_flow 等实模块的 import），
    # 见本文件顶部注释：已在 sys.modules 里的名字一律不碰。
    "app.adapters", "app.adapters.network", "app.adapters.network.http",
    "app.application.directory", "app.application.mediaserver",
    "app.application.torrent", "app.application.torrent.download",
    "app.chain", "app.chain.download", "app.chain.mediaserver",
    "app.chain.storage", "app.chain.subscribe", "app.chain.subscribe.query",
    "app.chain.transfer",
    "app.db.downloadhistory_oper", "app.db.models",
    "app.db.models.downloadhistory", "app.db.models.mediaserver",
    "app.db.subscribe_oper", "app.db.systemconfig_oper",
    "app.domain", "app.domain.episode",
    "app.helper.directory", "app.helper.mediaserver", "app.helper.message",
    "app.helper.torrent",
    "app.modules.filter",
    "app.schemas.file",
    "app.utils.http",
):
    if _name not in sys.modules:
        ensure_package(_name)
install_real_ttl_cache()

service_module = sys.modules.get("cloudsubscribex.handlers.sync.service")
if service_module is None:
    service_module = load_module(
        "cloudsubscribex.handlers.sync.service",
        "plugins.v2/cloudsubscribex/handlers/sync/service.py",
    )
SyncHandler = service_module.SyncHandler


def _bare_handler(notify_service=None, offline_result="task-123"):
    """不走 __init__ 的裸 SyncHandler：只配离线提交用到的属性。"""
    handler = object.__new__(SyncHandler)
    offline = MagicMock()
    offline.add_offline_download.return_value = offline_result
    object.__setattr__(handler, "_offline_download", offline)
    completed = []
    rolled_back = []
    object.__setattr__(
        handler, "_complete_offline_submission", lambda ctx: completed.append(ctx),
    )
    object.__setattr__(
        handler, "_rollback_offline_submission", lambda ctx: rolled_back.append(ctx),
    )
    if notify_service is not None:
        object.__setattr__(handler, "notifications", notify_service)
    return handler, offline, completed, rolled_back


def _offline_context(key, season=1, episodes=(24, 25)):
    return {
        "pending_key": key,
        "share_url": "magnet:?xt=urn:btih:abc",
        "staging_dir": "/downloads",
        "subscribe": _subscribe(source="subscribe"),
        "mediainfo": _mediainfo(),
        "season": season,
        "target_episodes": list(episodes),
    }


class OfflineSubmitNotificationTests(unittest.TestCase):
    def test_offline_accepted_notifies_once(self):
        """网盘接受离线下载：每项成功发一条开始通知，成功键也收进集合。"""
        service, post_message = _real_notify_service()
        handler, _, completed, rolled_back = _bare_handler(service)
        keys = handler._submit_offline_packages([_offline_context("k1")])
        self.assertEqual(keys, {"k1"})
        self.assertEqual(len(completed), 1)
        self.assertEqual(rolled_back, [])
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("玫瑰战争 S01 E24, E25", post_message.call_args.kwargs["text"])

    def test_offline_rejected_no_notification(self):
        """网盘拒绝（返回空值）：回滚，不发通知。"""
        service, post_message = _real_notify_service()
        handler, _, _, rolled_back = _bare_handler(service, offline_result="")
        keys = handler._submit_offline_packages([_offline_context("k1")])
        self.assertEqual(keys, set())
        self.assertEqual(len(rolled_back), 1)
        self.assertEqual(post_message.call_count, 0)

    def test_offline_error_no_notification(self):
        """提交接口抛错：回滚，不发通知。"""
        service, post_message = _real_notify_service()
        handler, offline, _, rolled_back = _bare_handler(service)
        offline.add_offline_download.side_effect = RuntimeError("115 down")
        keys = handler._submit_offline_packages([_offline_context("k1")])
        self.assertEqual(keys, set())
        self.assertEqual(len(rolled_back), 1)
        self.assertEqual(post_message.call_count, 0)

    def test_offline_movie_headline(self):
        """离线电影（season 为 None）：标题用 电影名 (年份)。"""
        service, post_message = _real_notify_service()
        handler, _, _, _ = _bare_handler(service)
        context = _offline_context("k1", season=None, episodes=())
        context["mediainfo"] = _mediainfo(title="秒速5厘米", year=2025)
        handler._submit_offline_packages([context])
        self.assertEqual(post_message.call_count, 1)
        self.assertIn("秒速5厘米 (2025)", post_message.call_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
