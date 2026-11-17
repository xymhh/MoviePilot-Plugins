"""115 网盘存储适配器：把插件现有的 115 实现包装成 MP 存储。

MP 存储标识 ``u115cs``、显示名“115网盘CS版”。MP 的 ``StorageSchema``
是固定枚举、没有这个成员，所以本类的 ``schema`` 用鸭子类型对象
（只需 ``.value == "u115cs"``；``__get_storage_oper`` 与
``_support_storages`` 都只读 ``.value``）。

注册不靠 MP 更新时重建：插件每次加载都调
:func:`register_u115cs_storage`，把本类写进文件管理模块的
``_storage_schemas`` / ``_support_storages``，并用 ``StorageHelper``
补一份存储配置；注册后自检，失败记大声日志。写 ``_storage_schemas``
时是**替换**不是追加：插件每次重载都会产生一个新的本类对象，而 MP
的 ``__get_storage_oper`` 只取第一个 schema.value 匹配的类，且插件
卸载/重载时 MP 不清理 ``_storage_schemas``——只追加会让旧类一直排
在前面、盖住新类（2026-11-16 实测：更新到 1.12.3 后整理仍报“存储
U115CS 未实现严格查询”，就是 1.12.2 的旧类还在生效）。

注意两点 MP 内部约定：
- ``FileManagerModule.init_module`` 会把 ``_storage_schemas`` 换成
  新列表，所以注册必须写到**运行实例**的列表上，只写类属性不够；
  配置重载会再次跑 ``init_module``，插件要在配置保存后重新注册。
- 适配器不持有 115 客户端：实例由 MP 无参构造，客户端经
  :func:`bind_u115cs_provider` 以模块级绑定注入（插件 ``_init_clients``
  之后调一次）。未绑定时所有方法返回失败值，绝不抛错打断 MP 流程。
  唯一例外是 ``get_item_strict``：MP 的覆盖保护要求它区分“不存在”
  和“查不出来”，状态无法确认时必须抛 ``StorageQueryError``（MP 会
  安全跳过本次整理），返回 None 会被当成“确认不存在”去覆盖。
"""

import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Tuple

from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem, StorageUsage

from ...core.cloud import CloudDriveCapability, CloudFile

U115CS_STORAGE_ID = "u115cs"
U115CS_STORAGE_NAME = "115网盘CS版"

#: 存储配置（StorageHelper.add_storage 用）；适配器不需要配置项，
#: 客户端由插件绑定注入，这里只留一个来源标记。
U115CS_STORAGE_CONFIG: Dict[str, Any] = {"provider": "cloudsubscribex"}

_BOUND_PROVIDER: Optional[Any] = None

#: 日志前缀。
_LOG_PREFIX = "115网盘CS版"

#: 整理进度上报节流：两次更新之间最少间隔（秒）。oss2 每传完一个
#: 10MB 分片就回调一次，1.3GB 文件约 134 次；不节流会刷爆 MP 的
#: 进度 SSE（前端每 0.5 秒才拉一次）。
_PROGRESS_MIN_INTERVAL = 1.0


def _mp_filetransfer_progress():
    """取 MP 的文件整理进度器；拿不到（测试/独立环境）返回 None。

    惰性导入：MP 的 ``app.runtime.progress`` 只在 MP 进程里存在，
    存储模块被单测加载时不存在，绝不能在模块顶层 import。
    """
    try:
        from app.runtime.progress import ProgressHelper
    except Exception:
        return None
    try:
        from app.schemas.types import ProgressKey
        key = ProgressKey.FileTransfer
    except Exception:
        key = "filetransfer"
    try:
        return ProgressHelper(key)
    except Exception:
        return None


def _report_upload_registry(key: str, file_name: str, uploaded: int, total: int) -> None:
    """把字节进度记进插件进程内登记表（下载页任务卡的实时进度来源）。

    惰性导入：登记表模块只在 MP 进程里被单测重载，顶层 import 会绑
    到旧表，每次调用都重新取当前模块。登记表调用绝不能影响上传，
    任何异常都静默吞掉。
    """
    try:
        from ...core.upload_progress import report_upload_progress
    except Exception:
        return
    try:
        report_upload_progress(key, file_name, uploaded, total)
    except Exception as error:
        logger.debug(
            f"{_LOG_PREFIX}上传进度登记失败：{file_name}，{error}",
            exc_info=True,
        )


def _remove_upload_registry(key: str) -> None:
    """上传完成时清掉登记表条目（下载页的上传卡随之消失）。"""
    try:
        from ...core.upload_progress import remove_upload_progress
    except Exception:
        return
    try:
        remove_upload_progress(key)
    except Exception as error:
        logger.debug(
            f"{_LOG_PREFIX}上传进度清理失败：{key}，{error}",
            exc_info=True,
        )


def _make_upload_progress_callback(file_name: str, total_size: int, key: str = None):
    """造一个上传进度回调：把字节进度写进 MP 的整理进度。

    MP 整理队列的进度是按**文件数**算的（已处理/总数），单文件整理
    全程 0%、做完才跳 100%，所以整理大文件时页面一直 0%——不是卡住。
    这里把当前文件的字节百分比写进同一个 ProgressHelper，单文件时
    就是整体进度；多文件时至少能看到当前文件在走。MP 没在整理时
    ProgressHelper 未启用，update 是空操作，不会误报。

    同时每次调用都把进度记进插件进程内登记表（不经过 1 秒节流，
    速度靠登记表内部算），下载管理页据此投影实时上传任务卡；
    传完（100%）时清掉登记表条目，卡片随之消失。key 默认是目标
    文件名，storage.upload 传本地文件名，便于与整理任务对应。
    """
    registry_key = str(key or file_name or "").strip() or file_name
    last_reported = [0.0]

    def _report(uploaded: int, total: int) -> None:
        if total <= 0:
            return
        now = time.monotonic()
        finished = uploaded >= total
        # 登记表每次调用都记（不节流）；MP 进度器仍按 1 秒节流。
        _report_upload_registry(registry_key, file_name, uploaded, total)
        if not finished and now - last_reported[0] < _PROGRESS_MIN_INTERVAL:
            return
        last_reported[0] = now
        pct = max(0.0, min(100.0, uploaded / total * 100))
        helper = _mp_filetransfer_progress()
        if helper is not None:
            try:
                helper.update(
                    value=round(pct, 1),
                    text=(
                        f"正在上传 {file_name}：{pct:.0f}%"
                        f"（{uploaded / 1048576:.0f}MB/{total / 1048576:.0f}MB）"
                    ),
                )
            except Exception as error:
                logger.debug(
                    f"{_LOG_PREFIX}上传进度上报失败：{file_name}，{error}",
                    exc_info=True,
                )
        if finished:
            _remove_upload_registry(registry_key)

    return _report


class _U115CSSchema:
    """鸭子类型存储 schema：MP 只读 ``.value`` 做匹配。"""

    value = U115CS_STORAGE_ID
    name = "U115CS"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "U115CS"


def bind_u115cs_provider(provider: Any) -> bool:
    """绑定插件的 115 提供方，供无参构造的适配器实例取用。"""
    global _BOUND_PROVIDER
    if provider is None or not callable(getattr(provider, "require", None)):
        logger.error(f"{_LOG_PREFIX}绑定提供方失败：缺少 require 方法")
        return False
    _BOUND_PROVIDER = provider
    return True


def _bound_provider() -> Optional[Any]:
    return _BOUND_PROVIDER


def _normalize_115_path(path: Any) -> str:
    """归一化 115 路径：前导斜杠、单斜杠分隔、根为 ``/``。"""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw:
        return "/"
    parts = [part for part in raw.split("/") if part]
    return "/" + "/".join(parts) if parts else "/"


def _split_parent(path: str) -> Tuple[str, str]:
    """拆 115 路径为（父目录，文件名）；根返回（``/``，``""``）。"""
    normalized = _normalize_115_path(path)
    if normalized == "/":
        return "/", ""
    parent, _, name = normalized.rpartition("/")
    return parent or "/", name


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _call_via_rate_limiter(
        manager: Any, func: Any, *args: Any, **kwargs: Any
) -> Any:
    """经 115 管理器的限流器调客户端方法；没有限流器时直调。"""
    limiter = getattr(manager, "rate_limiter", None)
    call = getattr(limiter, "call", None)
    if callable(call):
        return call(func, *args, **kwargs)
    return func(*args, **kwargs)


def _to_file_item(cloud: CloudFile, parent_path: str) -> FileItem:
    """CloudFile → MP FileItem；``storage`` 一律填 ``u115cs``。"""
    parent = _normalize_115_path(parent_path)
    name = str(cloud.name or "").strip()
    suffix = PurePosixPath(name).suffix
    path = f"{parent.rstrip('/')}/{name}"
    if cloud.is_directory:
        path = f"{path}/"
    native = cloud.native if isinstance(cloud.native, Mapping) else {}
    parent_fileid = (
        native.get("_parent_cid") or native.get("pid") or None
    )
    modify_time = _to_int(native.get("upt") or native.get("utime"))
    return FileItem(
        storage=U115CS_STORAGE_ID,
        path=path,
        type="dir" if cloud.is_directory else "file",
        name=name,
        basename=PurePosixPath(name).stem,
        extension=suffix[1:] if suffix and not cloud.is_directory else None,
        size=None if cloud.is_directory else int(cloud.size or 0),
        fileid=str(cloud.id or ""),
        parent_fileid=(
            str(parent_fileid) if parent_fileid not in (None, "") else None
        ),
        pickcode=str(
            (cloud.playback_values or {}).get("pickcode") or ""
        ) or None,
        modify_time=float(modify_time) if modify_time is not None else None,
    )


class U115CSStorage(StorageBase):
    """MP 存储“115网盘CS版”：插件 115 实现的 StorageBase 包装。"""

    schema = _U115CSSchema()
    transtype = {"move": "移动", "copy": "复制"}

    def __init__(self) -> None:
        super().__init__()

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        # 115 子项变化不保证更新祖先目录时间，快照必须继续遍历子目录
        #（与 MP 自带 u115 相同）。
        return False

    def init_storage(self) -> None:
        # 客户端由插件绑定注入，本后端无需额外初始化。
        pass

    def close(self) -> None:
        return None

    def _require(self, capability: CloudDriveCapability) -> Optional[Any]:
        provider = _bound_provider()
        if provider is None:
            return None
        try:
            return provider.require(capability)
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}提供方缺少能力 {capability}：{error}"
            )
            return None

    def _manager(self) -> Optional[Any]:
        return self._require(CloudDriveCapability.ACCOUNT)

    def _resolve_cloud(self, fileitem: Any) -> Optional[CloudFile]:
        """按 FileItem 路径查 CloudFile；查不到返回 None。"""
        path = _normalize_115_path(getattr(fileitem, "path", "") or "")
        if not path or path == "/":
            return None
        parent, name = _split_parent(path)
        query = self._require(CloudDriveCapability.FILE_QUERY)
        if query is None:
            return None
        try:
            return query.find_file(parent, name)
        except Exception as error:
            logger.warning(f"{_LOG_PREFIX}查询文件失败：{path}，{error}")
            return None

    def check(self) -> bool:
        try:
            manager = self._manager()
            if manager is None:
                return False
            return bool(manager.check_login())
        except Exception as error:
            logger.warning(f"{_LOG_PREFIX}存储检查失败：{error}")
            return False

    def list(self, fileitem: FileItem) -> List[FileItem]:
        try:
            path = _normalize_115_path(getattr(fileitem, "path", "") or "")
            if not path:
                return []
            if getattr(fileitem, "type", None) == "file":
                detail = self.detail(fileitem)
                return [detail] if detail is not None else []
            reader = self._require(CloudDriveCapability.DIRECTORY_READ)
            if reader is None:
                return []
            directory_id = getattr(fileitem, "fileid", None) or None
            if not directory_id:
                lookup = reader.resolve_directory(path)
                if not lookup.checked or lookup.directory_id is None:
                    return []
                directory_id = lookup.directory_id
            listing = reader.list_directory(str(directory_id))
            if not listing.checked:
                return []
            return [
                _to_file_item(item, path) for item in (listing.files or [])
            ]
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}列目录失败："
                f"{getattr(fileitem, 'path', '')}，{error}"
            )
            return []

    def create_folder(
            self, fileitem: FileItem, name: str
    ) -> Optional[FileItem]:
        try:
            parent = _normalize_115_path(getattr(fileitem, "path", "") or "")
            target_name = str(name or "").strip()
            if not parent or not target_name:
                return None
            reader = self._require(CloudDriveCapability.DIRECTORY_READ)
            if reader is None:
                return None
            target = (
                f"{parent.rstrip('/')}/{target_name}"
                if parent != "/" else f"/{target_name}"
            )
            lookup = reader.resolve_directory(target, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return None
            return self.get_item(Path(target))
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}创建目录失败："
                f"{getattr(fileitem, 'path', '')}/{name}，{error}"
            )
            return None

    def get_folder(self, path: Path) -> Optional[FileItem]:
        try:
            normalized = _normalize_115_path(str(path))
            item = self.get_item(Path(normalized))
            if item is not None:
                return item
            reader = self._require(CloudDriveCapability.DIRECTORY_READ)
            if reader is None:
                return None
            lookup = reader.resolve_directory(normalized, create=True)
            if not lookup.checked or lookup.directory_id is None:
                return None
            return self.get_item(Path(normalized))
        except Exception as error:
            logger.warning(f"{_LOG_PREFIX}获取目录失败：{path}，{error}")
            return None

    def get_item(self, path: Path) -> Optional[FileItem]:
        try:
            normalized = _normalize_115_path(str(path))
            if normalized == "/":
                return FileItem(
                    storage=U115CS_STORAGE_ID,
                    path="/",
                    type="dir",
                    name="/",
                    basename="/",
                    fileid="0",
                )
            parent, name = _split_parent(normalized)
            query = self._require(CloudDriveCapability.FILE_QUERY)
            if query is None:
                return None
            cloud = query.find_file(parent, name)
            if cloud is None:
                return None
            return _to_file_item(cloud, parent)
        except Exception as error:
            logger.warning(f"{_LOG_PREFIX}查询路径失败：{path}，{error}")
            return None

    def get_item_strict(self, path: Path) -> Optional[FileItem]:
        """严格查询路径：MP 覆盖保护用的查询，必须区分“不存在”和“查不出来”。

        MP 的整理在写目标文件前调这个方法（转存处理器的覆盖检查）：
        返回 None 表示已确认目标不存在，可以写；返回 FileItem 表示目标已
        在，按覆盖策略处理；状态无法确认时必须抛 StorageQueryError，
        MP 会跳过本次整理而不是把故障误判成不存在去覆盖。

        与宽松版 ``get_item`` 的区别：宽松版把一切异常都吞成 None；
        这里只有“确认不存在”才返回 None——父目录确认不存在（目标文件
        不可能存在）也算确认不存在；提供方未绑定、父目录解析失败、目录
        列举失败一律抛 StorageQueryError。

        父目录先用只读解析确认：整理目标的父目录常常还不存在（如刚按
        剧名新建的季目录），这时直接调 ``find_file_strict`` 会因父目录
        解析不到而抛错，导致整理被跳过；先确认父目录不存在就直接返回
        None，整理才能继续。根目录直接返回根目录项。
        """
        normalized = _normalize_115_path(str(path))
        if normalized == "/":
            return FileItem(
                storage=U115CS_STORAGE_ID,
                path="/",
                type="dir",
                name="/",
                basename="/",
                fileid="0",
            )
        parent, name = _split_parent(normalized)
        reader = self._require(CloudDriveCapability.DIRECTORY_READ)
        if reader is None:
            raise StorageQueryError(
                f"{_LOG_PREFIX}提供方未就绪，无法确认目标状态：{path}"
            )
        try:
            lookup = reader.resolve_directory(parent)
        except Exception as error:
            raise StorageQueryError(
                f"{_LOG_PREFIX}父目录解析失败，无法确认目标状态：{path}（{error}）"
            ) from error
        if not lookup.checked:
            raise StorageQueryError(
                f"{_LOG_PREFIX}无法确认父目录状态，无法确认目标状态：{path}"
            )
        if lookup.directory_id is None:
            # 父目录确认不存在，目标文件不可能存在。
            return None
        query = self._require(CloudDriveCapability.FILE_QUERY)
        if query is None:
            raise StorageQueryError(
                f"{_LOG_PREFIX}提供方未就绪，无法确认目标状态：{path}"
            )
        try:
            cloud = query.find_file_strict(parent, name)
        except StorageQueryError:
            raise
        except Exception as error:
            raise StorageQueryError(
                f"{_LOG_PREFIX}查询失败，无法确认目标状态：{path}（{error}）"
            ) from error
        if cloud is None:
            return None
        return _to_file_item(cloud, parent)

    def detail(self, fileitem: FileItem) -> Optional[FileItem]:
        try:
            path = getattr(fileitem, "path", None)
            if not path:
                return None
            return self.get_item(Path(str(path)))
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}查询详情失败："
                f"{getattr(fileitem, 'path', '')}，{error}"
            )
            return None

    def download(
            self, fileitem: FileItem, path: Optional[Path] = None
    ) -> Optional[Path]:
        try:
            cloud = self._resolve_cloud(fileitem)
            if cloud is None or cloud.is_directory:
                return None
            query = self._require(CloudDriveCapability.FILE_QUERY)
            if query is None:
                return None
            target_dir = (
                Path(tempfile.gettempdir()) / "u115cs-download"
                if path is None else Path(str(path))
            )
            target_dir.mkdir(parents=True, exist_ok=True)
            local_path = target_dir / str(cloud.name or "download")
            result = query.download_file(cloud, str(local_path))
            result_path = Path(str(result or ""))
            if result_path.is_file():
                return result_path
            return None
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}下载失败："
                f"{getattr(fileitem, 'path', '')}，{error}"
            )
            return None

    def upload(
            self, fileitem: FileItem, path: Path,
            new_name: Optional[str] = None,
    ) -> Optional[FileItem]:
        """把本地文件传进 115 目标目录；失败返回 None。"""
        try:
            dir_path = _normalize_115_path(
                getattr(fileitem, "path", "") or ""
            )
            local_path = Path(str(path or ""))
            if not dir_path or not local_path.is_file():
                return None
            reader = self._require(CloudDriveCapability.DIRECTORY_READ)
            if reader is None:
                return None
            lookup = reader.resolve_directory(dir_path, create=True)
            if not lookup.checked or lookup.directory_id is None:
                logger.warning(f"{_LOG_PREFIX}上传目录不可用：{dir_path}")
                return None
            final_name = str(new_name or local_path.name).strip()
            if not final_name:
                final_name = local_path.name
            upload = self._require(CloudDriveCapability.LOCAL_UPLOAD)
            if upload is None:
                return None
            total_size = int(local_path.stat().st_size)
            if not upload.upload_file(
                    str(local_path), dir_path, final_name,
                    progress_callback=_make_upload_progress_callback(
                        final_name, total_size, key=local_path.name
                    ),
            ):
                logger.warning(
                    f"{_LOG_PREFIX}上传失败："
                    f"{local_path.name} -> {dir_path}/{final_name}，"
                    f"{getattr(upload, 'last_error', '') or ''}"
                )
                return None
            query = self._require(CloudDriveCapability.FILE_QUERY)
            found = None
            if query is not None:
                try:
                    found = query.find_file(dir_path, final_name)
                except Exception as error:
                    logger.warning(
                        f"{_LOG_PREFIX}上传后查询失败："
                        f"{dir_path}/{final_name}，{error}"
                    )
            if found is not None:
                return _to_file_item(found, dir_path)
            # 索引尚不可见时按目标路径合成文件项（与 MP 自带 u115 同款处理）。
            return FileItem(
                storage=U115CS_STORAGE_ID,
                path=f"{dir_path.rstrip('/')}/{final_name}",
                type="file",
                name=final_name,
                basename=local_path.stem,
                extension=local_path.suffix[1:] or None,
                size=int(local_path.stat().st_size),
            )
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}上传异常：{path}，{error}"
            )
            return None

    def delete(self, fileitem: FileItem) -> bool:
        try:
            file_id = getattr(fileitem, "fileid", None) or None
            if not file_id:
                cloud = self._resolve_cloud(fileitem)
                file_id = str(cloud.id) if cloud is not None else None
            if not file_id:
                return False
            mutation = self._require(CloudDriveCapability.FILE_MUTATION)
            if mutation is None:
                return False
            return bool(mutation.delete_file(str(file_id)))
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}删除失败："
                f"{getattr(fileitem, 'path', '')}，{error}"
            )
            return False

    def rename(self, fileitem: FileItem, name: str) -> bool:
        try:
            target_name = str(name or "").strip()
            if not target_name:
                return False
            cloud = self._resolve_cloud(fileitem)
            if cloud is None:
                return False
            parent, _ = _split_parent(
                _normalize_115_path(getattr(fileitem, "path", "") or "")
            )
            mutation = self._require(CloudDriveCapability.FILE_MUTATION)
            if mutation is None:
                return False
            return bool(mutation.rename_file(parent, cloud, target_name))
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}重命名失败："
                f"{getattr(fileitem, 'path', '')} -> {name}，{error}"
            )
            return False

    def copy(
            self, fileitem: FileItem, path: Path, new_name: str,
            **kwargs: Any,
    ) -> bool:
        """115 内复制：调开放接口 ``/open/ufile/copy`` 再核验目标。

        插件的 115 服务里没有 copy 能力，所以直接走 p115client 的
        ``fs_copy_open``（即 MP 自带 u115 用的同一个开放接口，
        ``file_id`` + 目标目录 ``pid``）。复制前拒绝目标已存在同名文件，
        避免 115 在目标目录生成编号副本；复制后按名改名并复核。
        """
        try:
            target_dir = _normalize_115_path(str(path))
            final_name = str(new_name or "").strip()
            if not target_dir or not final_name:
                return False
            source = self._resolve_cloud(fileitem)
            if source is None:
                return False
            destination = self.get_item(Path(target_dir))
            if destination is None or destination.type != "dir":
                logger.warning(f"{_LOG_PREFIX}复制目标目录不存在：{target_dir}")
                return False
            query = self._require(CloudDriveCapability.FILE_QUERY)
            if query is None:
                return False
            if query.find_file(target_dir, final_name) is not None:
                logger.warning(
                    f"{_LOG_PREFIX}复制目标已存在同名文件，拒绝操作："
                    f"{target_dir}/{final_name}"
                )
                return False
            manager = self._manager()
            client = getattr(manager, "client", None) if manager else None
            copy_open = getattr(client, "fs_copy_open", None) if client else None
            if not callable(copy_open):
                logger.warning(f"{_LOG_PREFIX}115 客户端不支持复制接口")
                return False
            source_id = _to_int(source.id)
            target_pid = _to_int(destination.fileid)
            if source_id is None or target_pid is None:
                return False
            response = _call_via_rate_limiter(
                manager, copy_open, source_id, pid=target_pid
            )
            if not isinstance(response, dict) or not response.get("state"):
                logger.warning(
                    f"{_LOG_PREFIX}复制接口未返回成功："
                    f"{getattr(fileitem, 'path', '')} -> {target_dir}，"
                    f"{response if isinstance(response, dict) else ''}"
                )
                return False
            copied = query.find_file(target_dir, str(source.name))
            if copied is None:
                logger.warning(
                    f"{_LOG_PREFIX}复制后未在目标目录找到文件："
                    f"{target_dir}/{source.name}"
                )
                return False
            if str(copied.name) != final_name:
                mutation = self._require(CloudDriveCapability.FILE_MUTATION)
                if mutation is None:
                    return False
                if not mutation.rename_file(target_dir, copied, final_name):
                    logger.warning(
                        f"{_LOG_PREFIX}复制后改名失败："
                        f"{target_dir}/{copied.name} -> {final_name}"
                    )
                    return False
                copied = query.find_file(target_dir, final_name)
                if copied is None:
                    return False
            return True
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}复制失败："
                f"{getattr(fileitem, 'path', '')} -> {path}，{error}"
            )
            return False

    def move(self, fileitem: FileItem, path: Path, new_name: str) -> bool:
        try:
            target_dir = _normalize_115_path(str(path))
            final_name = str(new_name or "").strip()
            if not target_dir or not final_name:
                return False
            source = self._resolve_cloud(fileitem)
            if source is None:
                return False
            mutation = self._require(CloudDriveCapability.FILE_MUTATION)
            if mutation is None:
                return False
            return mutation.move_file(source, target_dir, final_name) is not None
        except Exception as error:
            logger.warning(
                f"{_LOG_PREFIX}移动失败："
                f"{getattr(fileitem, 'path', '')} -> {path}，{error}"
            )
            return False

    def link(self, fileitem: FileItem, target_file: Path) -> bool:
        return False

    def softlink(self, fileitem: FileItem, target_file: Path) -> bool:
        return False

    def reflink(self, fileitem: FileItem, target_file: Path) -> bool:
        return False

    def usage(self) -> Optional[StorageUsage]:
        try:
            manager = self._manager()
            client = getattr(manager, "client", None) if manager else None
            if client is None or not hasattr(client, "fs_index_info"):
                return None
            response = _call_via_rate_limiter(
                manager, client.fs_index_info, payload=0
            )
            if not isinstance(response, dict) or not response.get("state"):
                return None
            space = (response.get("data") or {}).get("space_info") or {}
            total = _to_int((space.get("all_total") or {}).get("size"))
            remain = _to_int((space.get("all_remain") or {}).get("size"))
            if total is None or remain is None:
                return None
            return StorageUsage(total=float(total), available=float(remain))
        except Exception as error:
            logger.warning(f"{_LOG_PREFIX}查询容量失败：{error}")
            return None


def register_u115cs_storage() -> bool:
    """把“115网盘CS版”注册进 MP 的文件管理模块与存储配置。

    关键点：``FileManagerModule.init_module`` 会把 ``_storage_schemas``
    换成新列表，所以必须写到**运行实例**的列表上（经模块管理器取），
    只写类属性不够；类属性也一并追加，作为模块尚未初始化时的兜底。
    MP 配置重载会再次跑 ``init_module`` 抹掉实例列表，插件要在配置
    保存后重新调用本函数。注册后自检，任一环节失败都记 error 并返回
    False（MP 内部属性名若被改名，注册会静默失效——自检就是防这个）。
    """
    try:
        from app.application.module import get_module_manager
        from app.application.storage import StorageHelper
        from app.modules.filemanager.module import FileManagerModule
        from app.schemas.types import OtherModulesType
    except Exception as error:
        logger.error(
            f"{_LOG_PREFIX}注册存储失败：无法导入 MP 模块：{error}"
        )
        return False

    instance = None
    try:
        manager = get_module_manager()
        for candidate in manager.get_running_subtype_module(
                OtherModulesType.FileManager
        ):
            instance = candidate
            break
        if instance is None:
            for candidate in manager.get_running_modules("storage_manage"):
                if isinstance(candidate, FileManagerModule):
                    instance = candidate
                    break
    except Exception as error:
        logger.warning(f"{_LOG_PREFIX}未取到文件管理模块实例：{error}")

    targets: List[Any] = []
    if instance is not None:
        targets.append(instance)
    targets.append(FileManagerModule)

    registered = True
    for target in targets:
        try:
            schemas = getattr(target, "_storage_schemas", None)
            supports = getattr(target, "_support_storages", None)
            if not isinstance(schemas, list) or not isinstance(supports, list):
                raise AttributeError("MP 存储注册表结构已变化")
            # 替换而非追加：插件每次重载都会产生一个新的本类对象，MP 的
            # __get_storage_oper 只取第一个 schema.value 匹配的类，且
            # 插件卸载/重载时 MP 不清理 _storage_schemas——只追加会让
            # 旧类一直排在前面、盖住新类（2026-11-16 实测：更新到 1.12.3
            # 后整理仍报“存储 U115CS 未实现严格查询”，就是 1.12.2 的
            # 旧类还在生效）。先清掉所有同标识的旧类，再挂当前类。
            schemas[:] = [
                schema for schema in schemas
                if getattr(getattr(schema, "schema", None), "value", None)
                != U115CS_STORAGE_ID
            ]
            schemas.append(U115CSStorage)
            if U115CS_STORAGE_ID not in supports:
                supports.append(U115CS_STORAGE_ID)
        except Exception as error:
            logger.error(
                f"{_LOG_PREFIX}注册存储失败：写注册表异常：{error}"
            )
            registered = False

    conf_ok = False
    try:
        helper = StorageHelper()
        if helper.get_storage(U115CS_STORAGE_ID) is None:
            helper.add_storage(
                U115CS_STORAGE_ID, U115CS_STORAGE_NAME,
                dict(U115CS_STORAGE_CONFIG),
            )
        conf_ok = helper.get_storage(U115CS_STORAGE_ID) is not None
    except Exception as error:
        logger.error(
            f"{_LOG_PREFIX}注册存储失败：写存储配置异常：{error}"
        )

    if instance is None:
        logger.error(
            f"{_LOG_PREFIX}注册未完成：未取到文件管理模块运行实例，"
            "MP 整理无法解析 u115cs 存储（插件加载时序问题，配置保存后"
            "会重新注册）"
        )
        return False
    live_ok = False
    try:
        # 自检不能只看“当前类在不在列表里”：旧类排在前面时 MP 取到的
        # 仍是旧类（1.12.3 的教训）。必须确认第一个匹配项就是当前类。
        first_match = next(
            (
                schema
                for schema in (getattr(instance, "_storage_schemas", None) or [])
                if getattr(getattr(schema, "schema", None), "value", None)
                == U115CS_STORAGE_ID
            ),
            None,
        )
        live_ok = (
            U115CS_STORAGE_ID
            in (getattr(instance, "_support_storages", None) or [])
            and first_match is U115CSStorage
        )
    except Exception as error:
        logger.error(f"{_LOG_PREFIX}注册自检异常：{error}")
    if not registered or not conf_ok or not live_ok:
        logger.error(
            f"{_LOG_PREFIX}注册自检未通过："
            f"registered={registered} conf_ok={conf_ok} live_ok={live_ok}，"
            "MP 整理无法使用 u115cs 存储"
        )
        return False
    logger.info(
        f"{_LOG_PREFIX}存储已注册：{U115CS_STORAGE_NAME}（{U115CS_STORAGE_ID}）"
    )
    return True
