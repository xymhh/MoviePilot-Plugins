"""
整理跳过（库内更优）语义测试模块

覆盖：_batch_move_or_copy / batch_move_or_copy 的跳过通道分流、
_record_skip 的「不写历史 + 不发失败事件 + 删低质量源文件 + 插件通知」、
_batch_record_skips 的单任务异常隔离与收尾。

风格对齐 tests/test_transfer_classify.py（sys.modules 假宿主 + unittest.TestCase）。
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Optional
from unittest import TestCase
from unittest.mock import MagicMock, patch

plugin_root = Path(__file__).resolve().parents[1]


def _make_module(name: str, **attrs: Any) -> ModuleType:
    """
    创建并注册假模块

    :param name (str): 模块名
    :param attrs (Dict): 模块属性

    :return ModuleType: 假模块
    """
    module = ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _make_pkg(name: str) -> ModuleType:
    """
    创建并注册假包

    :param name (str): 包名

    :return ModuleType: 假包
    """
    return _make_module(name, __path__=[])


class FakeSettings:
    """
    假 MoviePilot 设置
    """

    RMT_SUBEXT = [".srt", ".ass", ".ssa", ".sup"]
    RMT_AUDIOEXT = [".aac", ".ac3", ".m4a", ".flac"]
    RMT_MEDIAEXT = [".mp4", ".mkv", ".ts", ".iso", ".strm"]
    DEFAULT_SUB = "zh-cn"
    AI_AGENT_ENABLE = False
    AI_AGENT_RETRY_TRANSFER = False
    MP_DOMAIN = staticmethod(lambda path: path)


class FakeFileItem:
    """
    假文件项
    """

    def __init__(
        self,
        path: Optional[str] = None,
        name: Optional[str] = None,
        extension: Optional[str] = None,
        type: str = "file",
        fileid: Optional[str] = None,
        size: Optional[int] = None,
        storage: str = "115网盘Plus",
        pickcode: Optional[str] = None,
        modify_time: Optional[int] = None,
    ) -> None:
        """
        初始化假文件项

        :param path (str): 路径
        :param name (str): 名称
        :param extension (str): 扩展名
        :param type (str): 类型
        :param fileid (str): 文件 ID
        :param size (int): 文件大小
        :param storage (str): 存储名称
        :param pickcode (str): pickcode
        :param modify_time (int): 修改时间
        """
        self.path = path
        self.name = name or (Path(path).name if path else None)
        self.extension = extension
        self.type = type
        self.fileid = fileid
        self.size = size
        self.storage = storage
        self.pickcode = pickcode
        self.modify_time = modify_time


def _setup_mock_env() -> None:
    """
    注册全部假依赖模块
    """
    try:
        import app.application.transfer.models  # noqa: F401
    except Exception:  # pragma: no cover - 仅在无桩环境触发
        _make_module("app.application.transfer", TransferTask=object)
        _make_module("app.application.transfer.models", TransferTask=object)
        _make_module(
            "app.application.transfer.workflow",
            JobManager=object,
            job_lock=SimpleNamespace(),
        )

    for pkg in [
        "app",
        "app.sdk",
        "app.schemas",
        "app.chain",
        "app.db",
        "app.db.models",
        "app.db.oper",
        "app.application",
        "app.modules",
        "app.plugins",
        "app.core",
        "app.helper",
        "app.utils",
        "p115client",
        "p115client.tool",
    ]:
        _make_pkg(pkg)

    _log_stub = SimpleNamespace(
        info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
        warn=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )
    _make_module("app.log", logger=_log_stub)
    _make_module("app.sdk.logging", logger=_log_stub)
    _make_module(
        "app.sdk.config",
        settings=FakeSettings(),
        global_vars=SimpleNamespace(),
    )
    _make_module("app.core.config", settings=FakeSettings())
    _event_stub = SimpleNamespace(send_event=lambda *a, **k: None)
    _make_module("app.core.event", eventmanager=_event_stub)
    _make_module("app.sdk.events", eventmanager=_event_stub)
    _make_module(
        "app.application.history",
        add_transfer_success=lambda *a, **k: None,
        add_transfer_fail=lambda *a, **k: None,
    )
    _make_module("app.application.directory", DirectoryHelper=object)
    _make_module("app.schemas.message", Message=object)
    _make_module(
        "app.sdk.media",
        MetaBase=object,
        MetaInfoPath=object,
        MediaInfo=object,
        MediaInfoPath=object,
    )
    _make_module("app.sdk.utilities", StringUtils=object)
    _make_module("app.chain.storage", StorageChain=object)
    _make_module(
        "app.chain.transfer",
        TransferChain=object,
        task_lock=SimpleNamespace(),
    )
    _make_module("app.schemas", FileItem=FakeFileItem, TransferInfo=object)
    _make_module(
        "app.schemas.types",
        EventType=SimpleNamespace(
            TransferComplete="TransferComplete",
            TransferFailed="TransferFailed",
            SubtitleTransferFailed="SubtitleTransferFailed",
            AudioTransferFailed="AudioTransferFailed",
        ),
        MediaType=object,
        MessageType=SimpleNamespace(Plugin="Plugin", Manual="Manual"),
        ChainEventType=SimpleNamespace(TransferOverwriteCheck="TransferOverwriteCheck"),
    )
    _make_module("p115client", P115Client=object, check_response=lambda *a, **k: None)
    _make_module("p115client.tool.edit", update_name=object)

    _make_pkg("app.plugins.p115strmhelper")
    _make_pkg("app.plugins.p115strmhelper.core")
    _make_pkg("app.plugins.p115strmhelper.schemas")
    _make_pkg("app.plugins.p115strmhelper.helper")
    _make_pkg("app.plugins.p115strmhelper.helper.transfer")
    _make_module(
        "app.plugins.p115strmhelper.utils",
        __path__=[str(plugin_root / "utils")],
    )
    _make_module(
        "app.plugins.p115strmhelper.core.config",
        configer=SimpleNamespace(notify=True),
    )
    _make_module(
        "app.plugins.p115strmhelper.core.message",
        post_message=MagicMock(name="post_message"),
    )
    _make_module(
        "app.plugins.p115strmhelper.schemas.transfer",
        TransferTask=object,
        RelatedFile=object,
    )


def _load_module(rel_path: str) -> Any:
    """
    按插件相对路径加载真实模块（绕过插件包 __init__，避免触发运行依赖）

    :param rel_path (str): 插件内相对路径，如 "helper.transfer.handler"

    :return Any: 已加载模块
    """
    module_name = f"app.plugins.p115strmhelper.{rel_path.replace('/', '.')}"
    file_rel = f"{rel_path.replace('.', '/')}.py"
    spec = importlib.util.spec_from_file_location(module_name, plugin_root / file_rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_task(
    overwrite_mode: str = "size",
    source_size: int = 100,
) -> Any:
    """
    构造「库内已有更优文件」场景的整理任务

    :param overwrite_mode (str): 覆盖模式
    :param source_size (int): 源文件大小

    :return Any: 假整理任务
    """
    source_item = FakeFileItem(
        path="/115/待整理/电影/demo.mkv",
        name="demo.mkv",
        extension="mkv",
        fileid="1001",
        size=source_size,
    )
    target_dir = Path("/115/媒体库/电影/demo")
    return SimpleNamespace(
        fileitem=source_item,
        target_dir=target_dir,
        target_name="demo.mkv",
        target_path=target_dir / "demo.mkv",
        transfer_type="copy",
        overwrite_mode=overwrite_mode,
        related_files=[],
        mediainfo=None,
        meta=None,
        downloader=None,
        download_hash=None,
        need_scrape=False,
        need_notify=True,
        username=None,
    )


def _build_existing_item(size: int = 500) -> FakeFileItem:
    """
    构造目标目录中已存在的更优文件

    :param size (int): 目标文件大小

    :return FakeFileItem: 假文件项
    """
    return FakeFileItem(
        path="/115/媒体库/电影/demo/demo.mkv",
        name="demo.mkv",
        extension="mkv",
        fileid="9001",
        size=size,
    )


class TestMoveOrCopySkipRouting(TestCase):
    """
    库内更优时的分流：只进 skipped_tasks，不进 failed_tasks
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载真实模块（先加载 handler 的依赖子模块）
        """
        _setup_mock_env()
        cls.cache_updater = _load_module("helper.transfer.cache_updater")
        cls.linked = _load_module("helper.transfer.linked_subtitle_audio")
        cls.linked_batch = _load_module("helper.transfer.handler_linked_batch")
        cls.handler = _load_module("helper.transfer.handler")

    def _build_handler(self, existing: FakeFileItem) -> Any:
        """
        构造只依赖跳过路径的 TransferHandler 实例

        :param existing (FakeFileItem): 目标目录已存在文件

        :return Any: TransferHandler 实例
        """
        instance = self.handler.TransferHandler.__new__(
            self.handler.TransferHandler
        )
        instance.storage_name = "115网盘Plus"
        instance._get_folder = lambda _path: SimpleNamespace(fileid="42")
        instance.cache_updater = SimpleNamespace(
            _p115_api=SimpleNamespace(list=lambda _folder: [existing])
        )
        return instance

    def test_quality_better_routes_to_skipped_only(self) -> None:
        """
        覆盖模式=size 且目标更大时只进 skipped_tasks
        """
        task = _build_task(overwrite_mode="size", source_size=100)
        handler = self._build_handler(_build_existing_item(size=500))

        failed, skipped, success = handler._batch_move_or_copy([task])

        self.assertEqual(failed, [])
        self.assertEqual(success, [])
        self.assertEqual(len(skipped), 1)
        skipped_task, skip_reason = skipped[0]
        self.assertIs(skipped_task, task)
        self.assertEqual(skip_reason, "媒体库存在同名文件，且质量更好")

    def test_overwrite_never_routes_to_skipped_only(self) -> None:
        """
        覆盖模式=never 时同样只进 skipped_tasks
        """
        task = _build_task(overwrite_mode="never", source_size=100)
        handler = self._build_handler(_build_existing_item(size=10))

        failed, skipped, success = handler._batch_move_or_copy([task])

        self.assertEqual(failed, [])
        self.assertEqual(success, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(
            skipped[0][1], "媒体库存在同名文件，当前覆盖模式为不覆盖"
        )

    def test_linked_batch_routes_to_skipped_only(self) -> None:
        """
        关联批量路径同构分流
        """
        task = _build_task(overwrite_mode="size", source_size=100)
        handler = self._build_handler(_build_existing_item(size=500))
        linked = self.linked_batch.TransferHandlerLinkedBatch(handler)

        failed, skipped, success = linked.batch_move_or_copy([task])

        self.assertEqual(failed, [])
        self.assertEqual(success, [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0][1], "媒体库存在同名文件，且质量更好")


class TestRecordSkip(TestCase):
    """
    _record_skip 语义：不写失败历史、不发失败事件、删源文件、发插件通知
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载真实模块（先加载 handler 的依赖子模块）
        """
        _setup_mock_env()
        cls.cache_updater = _load_module("helper.transfer.cache_updater")
        cls.linked = _load_module("helper.transfer.linked_subtitle_audio")
        cls.linked_batch = _load_module("helper.transfer.handler_linked_batch")
        cls.handler = _load_module("helper.transfer.handler")

    def setUp(self) -> None:
        """
        构造实例并打开通知开关
        """
        self.module = self.handler
        self.module.configer.notify = True
        self.instance = self.handler.TransferHandler.__new__(
            self.handler.TransferHandler
        )
        self.instance.storage_name = "115网盘Plus"
        self.storage_chain = MagicMock(name="storage_chain")
        self.storage_chain.delete_media_file.return_value = True
        self.post_message = sys.modules[
            "app.plugins.p115strmhelper.core.message"
        ].post_message
        self.post_message.reset_mock()

    def _record(self, task: Any, reason: str, logger_mock: Any) -> None:
        """
        在受控 mock 下执行 _record_skip

        :param task (Any): 任务
        :param reason (str): 跳过原因
        :param logger_mock (Any): logger mock
        """
        with (
            patch.object(self.module, "StorageChain", return_value=self.storage_chain),
            patch.object(self.module, "add_transfer_fail") as add_fail,
            patch.object(self.module, "add_transfer_success") as add_success,
            patch.object(self.module, "eventmanager") as event_manager,
            patch.object(self.module, "logger", logger_mock),
        ):
            self.instance._record_skip(task, reason)
            self.add_fail = add_fail
            self.add_success = add_success
            self.event_manager = event_manager

    def test_no_history_no_failure_event(self) -> None:
        """
        不调用 add_transfer_fail / add_transfer_success，也不发 TransferFailed
        """
        task = _build_task()
        logger_mock = MagicMock(name="logger")

        self._record(task, "媒体库存在同名文件，且质量更好", logger_mock)

        self.add_fail.assert_not_called()
        self.add_success.assert_not_called()
        self.event_manager.send_event.assert_not_called()
        logged = " | ".join(
            str(call) for call in logger_mock.info.call_args_list
        )
        self.assertIn("已跳过（不写失败记录）", logged)

    def test_deletes_inferior_source_file(self) -> None:
        """
        调用 StorageChain().delete_media_file 删除低质量源文件
        """
        task = _build_task()

        self._record(task, "媒体库存在同名文件，且质量更好", MagicMock(name="logger"))

        self.storage_chain.delete_media_file.assert_called_once_with(
            fileitem=task.fileitem
        )

    def test_never_mode_keeps_source_file(self) -> None:
        """
        覆盖模式=never 的跳过不删源文件（无法确认库内更优，避免丢失更好版本）
        """
        task = _build_task()

        self._record(
            task, "媒体库存在同名文件，当前覆盖模式为不覆盖", MagicMock(name="logger")
        )

        self.storage_chain.delete_media_file.assert_not_called()
        self.post_message.assert_called_once()
        self.assertIn(
            "源文件已保留", self.post_message.call_args.kwargs["text"]
        )

    def test_notifies_when_config_open(self) -> None:
        """
        configer.notify 为真时发送插件通知
        """
        task = _build_task()

        self._record(task, "媒体库存在同名文件，且质量更好", MagicMock(name="logger"))

        self.assertEqual(self.post_message.call_count, 1)
        kwargs = self.post_message.call_args.kwargs
        self.assertEqual(kwargs["mtype"], self.module.MessageType.Plugin)
        self.assertEqual(
            kwargs["title"], "【整理接管】已跳过（库内已有更优文件）"
        )
        self.assertIn(task.fileitem.path, kwargs["text"])

    def test_no_notify_when_config_closed(self) -> None:
        """
        configer.notify 为假时不发通知
        """
        task = _build_task()
        self.module.configer.notify = False

        self._record(task, "媒体库存在同名文件，且质量更好", MagicMock(name="logger"))

        self.post_message.assert_not_called()

    def test_swallows_exception(self) -> None:
        """
        任何异常只记日志，不向外抛出
        """
        task = _build_task()
        self.storage_chain.delete_media_file.side_effect = RuntimeError("boom")
        self.post_message.side_effect = RuntimeError("notify boom")

        try:
            self._record(
                task, "媒体库存在同名文件，且质量更好", MagicMock(name="logger")
            )
        except Exception as exc:  # pragma: no cover - 仅在实现回归时触发
            self.fail(f"_record_skip 不应向外抛出: {exc}")
        finally:
            self.post_message.side_effect = None


class TestBatchRecordSkips(TestCase):
    """
    _batch_record_skips：单任务异常不外抛 + 收尾调用 _remove_completed_jobs
    """

    @classmethod
    def setUpClass(cls) -> None:
        """
        加载真实模块（先加载 handler 的依赖子模块）
        """
        _setup_mock_env()
        cls.cache_updater = _load_module("helper.transfer.cache_updater")
        cls.linked = _load_module("helper.transfer.linked_subtitle_audio")
        cls.linked_batch = _load_module("helper.transfer.handler_linked_batch")
        cls.handler = _load_module("helper.transfer.handler")

    def test_single_task_exception_does_not_raise(self) -> None:
        """
        单个任务抛错时逐个吞掉，且仍完成任务组收尾
        """
        instance = self.handler.TransferHandler.__new__(
            self.handler.TransferHandler
        )
        skipped_tasks = [(_build_task(), "原因A"), (_build_task(), "原因B")]

        with (
            patch.object(
                instance, "_record_skip", side_effect=RuntimeError("boom")
            ) as record_skip,
            patch.object(
                instance, "_remove_completed_jobs", return_value=0
            ) as remove_jobs,
            patch.object(self.handler, "logger", MagicMock(name="logger")),
        ):
            instance._batch_record_skips(skipped_tasks)

        self.assertEqual(record_skip.call_count, 2)
        remove_jobs.assert_called_once()
        self.assertEqual(remove_jobs.call_args.kwargs.get("task_action"), "fail")
        self.assertEqual(
            remove_jobs.call_args.kwargs.get("check_method"), "is_done"
        )

    def test_empty_list_is_noop(self) -> None:
        """
        空列表直接返回，不触碰任务组收尾
        """
        instance = self.handler.TransferHandler.__new__(
            self.handler.TransferHandler
        )

        with patch.object(
            instance, "_remove_completed_jobs", return_value=0
        ) as remove_jobs:
            instance._batch_record_skips([])

        remove_jobs.assert_not_called()
