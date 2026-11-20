"""直出 STRM 补写 MP 整理历史（v1.28.1，用户 2026-11-20 定方向 A）。

背景：插件 115 云端直出 strm 的转存（文件留在 115，只生成 .strm）不走
MP 下载→整理链，MP 整理历史（#/history，transfer_history）里没有记录；
用户要的只是“看起来统一”——转存仍走快的直出 strm，插件在生成 .strm
的同时往 MP 整理历史补写一条记录。

写入入口（按 MP v3.2.6 真实源码核实，app/application/history/
registration.py + app/db/adapters/history/registration.py + app/db/
oper/transferhistory.py）：
- MP 官方的外部登记口径是 ``TransferRegistrationWrite`` → 仓库
  ``SessionTransferRegistrationRepository.insert_if_absent``，把同一组列
  映射到 TransferHistory（origin="external"、registration 审计 JSON、
  date、status=True、transfer_task_id=None），再经
  ``TransferHistoryOper.stage_insert_if_source_absent`` 原子落库——该方法
  按 (src, src_storage) 唯一索引 insert-or-nothing，冲突时返回已存在行
  不替换、不重复写（去重就靠它，不另做预查）。
- 整条 ``TransferRegistrationCommand`` 不走：它要求本地文件真实存在
  （硬链核验）且 mode 限 copy/link/move，云盘源文件不适用；也不用
  ``add_transfer_success``（要真实 MetaBase + TransferInfo，太重）与
  ``add_force``/``repository.replace``（会替换同 src 行）。
- 本模块只复用其持久层映射与去重语义：直接调
  ``TransferHistoryOper.stage_insert_if_source_absent`` 并显式 commit
  （该方法不提交），不拼 SQL。

补写点：handlers/sync/postprocess.py ``_commit_single_item``，在详情
（detail）建成之后；只在直出 strm 分支写：
- MP 整理分支不写：item 带 ``mp_organize`` 身份时 MP 自己已写；
- ``skip_history`` 项不写（already_in_library 等，MP 库里已有该集）；
- 没有生成 .strm（strm_path 为空）不写；失败不写（失败不进 commit）；
- 写失败绝不阻断转存：全程 try/except，只写日志。

字段全部取真实值，不编造 MP 整理链才有的字段：
- src：115 内实际文件路径（cloud_dir + file_name，/Alist/... 风格，
  与 MP 自己写的同类行一致）；src_storage="local"（MP 对这些 /Alist/...
  路径就这么写；若写 u115cs，MP 日后整理同一文件时会因 (src,
  src_storage) 不同而多写一行）。
- dest：实际生成的 .strm 路径；dest_storage="local"。
- 集数/大小/时间/标题/年份/类别/海报：用详情与后处理项的真实值。
- mode=None（没有 MP 转移模式）、downloader=None（无下载器参与——
  这也让启动清理（platform_history，删 src 以 cloudsubscribe:// 开头
  或 downloader 为「网盘订阅助手」的旧镜像行）永远匹配不到补写行）、
  download_hash=None。
- 电影与剧集走同一 commit 路径；多集聚合一条时按集分别写（与
  v1.26.4 通知的 per-episode 分流口径一致，调用方按集拆分后逐项进
  commit，每项一行）。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from app.log import logger

from ...core import OwnerDelegator
from ...core.media import media_identity
from .utils import format_episode_ranges

# 补写行的存储标识：与 MP 自己对 /Alist/... 路径的写法一致。
_BACKFILL_SRC_STORAGE = "local"
_BACKFILL_DEST_STORAGE = "local"

# 审计字段：按 MP TransferRegistrationAudit 的形状，只记能证实的值。
_BACKFILL_ACTOR = "网盘订阅助手"


class HistoryBackfillService(OwnerDelegator):
    """直出 strm 时把一条 MP 整理历史记录补写进 transfer_history。"""

    def __init__(self, owner: Any, history_oper: Any = None) -> None:
        super().__init__(owner)
        # 测试注入的 oper（记录型桩）；生产为 None，走真实 SessionFactory。
        self._history_oper = history_oper

    @contextmanager
    def _history_oper_context(self) -> Iterator[Tuple[Any, Any]]:
        """产出 (oper, session)；真实路径下 session 需调用方显式提交。"""
        if self._history_oper is not None:
            yield self._history_oper, None
            return
        from app.db import SessionFactory
        from app.db.transferhistory_oper import TransferHistoryOper

        with SessionFactory() as db:
            yield TransferHistoryOper(db=db), db

    def backfill_strm_history(
        self,
        *,
        item: Dict[str, Any],
        strm_path: Any,
        media: Any = None,
        media_data: Optional[Dict[str, Any]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """直出 strm 分支补写一条 MP 整理历史记录。

        返回 True 当且仅当新写了一行；已存在、被守卫拦下、写失败都返回
        False。任何异常都被吞掉（只记日志），绝不阻断转存本身。
        """
        try:
            if item.get("mp_organize"):
                logger.debug("直出 strm 补写历史跳过：MP 整理分支已由 MP 写记录")
                return False
            if item.get("skip_history"):
                logger.debug("直出 strm 补写历史跳过：skip_history 项不写")
                return False
            if not strm_path:
                logger.debug("直出 strm 补写历史跳过：未生成 .strm")
                return False
            values = self.build_history_values(
                item=item,
                strm_path=strm_path,
                media=media,
                media_data=media_data,
                detail=detail,
            )
            if not values:
                return False
            with self._history_oper_context() as (oper, session):
                _record, created = oper.stage_insert_if_source_absent(values)
                if session is not None:
                    session.commit()
            if created:
                logger.info(
                    "已补写 MP 整理历史：%s %s",
                    values.get("title"),
                    values.get("episodes") or "",
                )
            else:
                logger.debug(
                    "MP 整理历史已有同源记录，不重复补写：%s",
                    values.get("src"),
                )
            return bool(created)
        except Exception as error:
            logger.warning(f"补写 MP 整理历史失败（不影响转存）：{error}")
            return False

    def build_history_values(
        self,
        *,
        item: Dict[str, Any],
        strm_path: Any,
        media: Any = None,
        media_data: Optional[Dict[str, Any]] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 MP 外部登记映射拼补写行的列值；拼不出真实记录时返回 None。

        只用 transfer_history 模型真实存在的列（MP v3.2.6
        app/db/models/transferhistory.py），其余一律不写。
        """
        detail = detail or {}
        cloud_dir = str(item.get("cloud_dir") or "").strip()
        file_name = str(item.get("file_name") or "").strip()
        if not cloud_dir or not file_name:
            logger.debug("直出 strm 补写历史跳过：缺 cloud_dir/file_name")
            return None
        src = f"{cloud_dir.rstrip('/')}/{file_name}"
        dest = str(strm_path or "").strip()
        if not dest:
            return None

        title = str(
            detail.get("title")
            or getattr(media, "title", None)
            or (media_data or {}).get("title")
            or ""
        ).strip()
        if not title:
            logger.debug("直出 strm 补写历史跳过：无标题")
            return None
        type_text = str(
            detail.get("type")
            or getattr(media, "type", None) and getattr(media.type, "value", media.type)
            or (media_data or {}).get("type")
            or ""
        ).strip()
        if type_text not in ("电视剧", "电影"):
            logger.debug("直出 strm 补写历史跳过：未知类型 %r", type_text)
            return None
        is_movie = type_text == "电影"

        year_value = (
            detail.get("year")
            or getattr(media, "year", None)
            or (media_data or {}).get("year")
        )
        try:
            year = str(int(year_value)) if year_value else None
        except (TypeError, ValueError):
            year = str(year_value).strip() or None

        size_value = detail.get("size")
        if size_value is None:
            size_value = item.get("size") or item.get("file_size")
        try:
            size = int(size_value) if size_value else 0
        except (TypeError, ValueError):
            size = 0
        if size < 0:
            size = 0

        image = (
            detail.get("image")
            or (
                media.get_poster_image()
                if callable(getattr(media, "get_poster_image", None))
                else None
            )
            or (media_data or {}).get("poster_path")
            or None
        )
        category = (
            str(detail.get("category") or "").strip()
            or str(getattr(media, "category", None) or "").strip()
            or str((media_data or {}).get("category") or "").strip()
            or None
        )
        media_source, media_id = media_identity(media)
        if not media_source or not media_id:
            media_source, media_id = media_identity(media_data)
        media_source = media_source or None
        media_id = str(media_id).strip() if media_id else None
        episode_group = (
            getattr(media, "episode_group", None)
            or (media_data or {}).get("episode_group")
            or None
        )

        seasons = None
        episodes = None
        if not is_movie:
            season_value = (
                detail.get("season")
                or item.get("season")
                or getattr(media, "season", None)
                or 1
            )
            try:
                season = max(1, int(season_value))
            except (TypeError, ValueError):
                season = 1
            seasons = f"S{season:02d}"
            episode_values = detail.get("episodes") or []
            if not episode_values:
                single = item.get("episode")
                if single:
                    episode_values = [single]
            episode_numbers = []
            for value in episode_values:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    continue
                if number > 0:
                    episode_numbers.append(number)
            if episode_numbers:
                episodes = format_episode_ranges(sorted(set(episode_numbers)))

        try:
            dest_size = Path(dest).stat().st_size if dest else None
        except (OSError, ValueError):
            dest_size = None

        src_fileitem = {
            "path": src,
            "storage": _BACKFILL_SRC_STORAGE,
            "type": "file",
            "name": file_name,
            "size": size or None,
        }
        dest_name = Path(dest).name
        dest_fileitem = {
            "path": dest,
            "storage": _BACKFILL_DEST_STORAGE,
            "type": "file",
            "name": dest_name,
            "size": dest_size,
        }
        registration = {
            "actor_name": _BACKFILL_ACTOR,
            "registered_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            ),
            "verification": "destination_only",
            "source_state": "present",
            "note": "网盘订阅助手直出 strm 时补写（MP 未走整理链）",
        }
        return {
            "src": src,
            "src_storage": _BACKFILL_SRC_STORAGE,
            "src_fileitem": src_fileitem,
            "dest": dest,
            "dest_storage": _BACKFILL_DEST_STORAGE,
            "dest_fileitem": dest_fileitem,
            "mode": None,
            "type": type_text,
            "title": title,
            "year": year,
            "media_source": media_source,
            "media_id": media_id,
            "category": category,
            "episode_group": episode_group,
            "image": image,
            "downloader": None,
            "download_hash": None,
            "status": True,
            "retry_count": 0,
            "auto_paused": False,
            "date": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "files": [dest],
            "seasons": seasons,
            "episodes": episodes,
            "origin": "external",
            "registration": registration,
            "transfer_task_id": None,
            "transfer_settlement_revision": None,
        }
