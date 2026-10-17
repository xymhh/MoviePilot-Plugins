"""转存落盘自检（1.6.52）。

把「转存成功」的定义从「接口返回成功」收紧成「目标季目录里真有这个文件」：

* 即时转存路径复用判重那一次列目录（``episode_dedup._season_dir_files``，含 TTL
  缓存），**不额外打网盘**；
* finalize/兜底补写路径按记录自身的 ``cloud_dir`` 列一次目录（同批同目录只列一次）；
* 拿不到实测数据（``verified=False``）一律不判「没有」→ ``landed=None``，调用方必须
  维持旧行为（不产生「待确认」）。

新增字段 ``landed`` / ``verified_at`` / ``landed_reason`` 全部可选、向后兼容，老记录
没有这些字段一律按「未知」处理。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from app.log import logger

from ...core import OwnerDelegator
from ...utils.file_parser import MediaFileParser


def landed_unknown(reason: str) -> Dict[str, Any]:
    """拿不到实测数据/自检关闭时的结果（调用方必须维持旧行为）。"""
    return {"landed": None, "reason": reason, "actual_file_id": ""}


def match_landed(
        file_id: str,
        target_name: str,
        present_ids: set,
        ids_by_name: Dict[str, str],
) -> Dict[str, Any]:
    """``file_id`` 精确命中 → 同名命中 → 都没有则 ``missing_in_dir``。"""
    file_id = str(file_id or "").strip()
    target_name = str(target_name or "").strip()
    if file_id and file_id in present_ids:
        return {"landed": True, "reason": "file_id_match", "actual_file_id": file_id}
    if target_name and target_name in ids_by_name:
        return {
            "landed": True,
            "reason": "name_match",
            "actual_file_id": ids_by_name.get(target_name, ""),
        }
    return {"landed": False, "reason": "missing_in_dir", "actual_file_id": ""}


def landed_confirms_success(info: Optional[Dict[str, Any]]) -> bool:
    """``landed is not False`` → 可按成功处置；``None``=拿不到数据，维持旧行为。"""
    return not (isinstance(info, dict) and info.get("landed") is False)


def landed_verification_fields(
        info: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """把自检结果转成要写进历史记录的字段；拿不到数据时不加任何字段。"""
    if not isinstance(info, dict) or info.get("landed") is None:
        return {}
    return {
        "landed": bool(info.get("landed")),
        "landed_reason": str(info.get("reason") or ""),
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


class LandedVerifyService(OwnerDelegator):
    """转存落盘自检：结果只反映「目标目录实测」，拿不准时不改状态。"""

    def _verify_landed_active(self) -> bool:
        """开关关闭时逐字节回退到 1.6.51 行为。"""
        return bool(getattr(self, "_verify_landed_enabled", True))

    # ------------------------------------------------------------------
    # 即时转存路径：复用 _season_dir_files（一次列目录，优先命中 TTL 缓存）
    # ------------------------------------------------------------------
    def verify_landed_items(
            self,
            subscribe,
            mediainfo,
            season: int,
            items: List[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        """批量核对转存结果是否真落在目标季目录。

        ``items``: ``[{"key": str, "file_id": str, "target_name": str}]``

        返回 ``{key: {"landed": Optional[bool], "reason": str, "actual_file_id": str}}``：

        * ``landed=True``  : ``file_id`` 命中，或（``file_id`` 取不到时）同名文件命中；
        * ``landed=False`` : 目录实测成功（``verified=True``）但找不到该文件；
        * ``landed=None``  : 拿不到实测数据（``verified=False``）或自检关闭。
        """
        result: Dict[str, Dict[str, Any]] = {}
        if not items:
            return result
        if not self._verify_landed_active():
            for item in items:
                key = str(item.get("key") or "")
                if key:
                    result[key] = self._landed_unknown("verify_disabled")
            return result

        try:
            verified, _season_dir, files = self._season_dir_files(
                subscribe, mediainfo, season
            )
        except Exception as error:  # pragma: no cover - 防御性兜底
            logger.debug(f"落盘自检列目录异常：{error}")
            verified, files = False, []
        if not verified:
            for item in items:
                key = str(item.get("key") or "")
                if key:
                    result[key] = self._landed_unknown("listing_unavailable")
            return result

        present_ids = {
            str(item.get("file_id") or "")
            for item in (files or [])
            if str(item.get("file_id") or "")
        }
        ids_by_name: Dict[str, str] = {}
        for entry in files or []:
            name = str(entry.get("file_name") or "").strip()
            if name:
                ids_by_name.setdefault(name, str(entry.get("file_id") or ""))
        for item in items:
            key = str(item.get("key") or "")
            if not key:
                continue
            result[key] = self._match_landed(
                file_id=str(item.get("file_id") or ""),
                target_name=str(item.get("target_name") or ""),
                present_ids=present_ids,
                ids_by_name=ids_by_name,
            )
        return result

    # ------------------------------------------------------------------
    # finalize / 兜底补写路径：按记录自身的 cloud_dir 列目录
    # ------------------------------------------------------------------
    def verify_landed_records(
            self,
            records: List[Dict[str, Any]],
    ) -> Dict[int, Dict[str, Any]]:
        """批量核对历史记录是否真落在其 ``cloud_dir``（同批同目录只列一次）。

        返回 ``{id(record): info}``。记录缺 ``cloud_dir`` 或列目录失败 →
        ``landed=None``，维持旧行为。
        """
        result: Dict[int, Dict[str, Any]] = {}
        if not records:
            return result
        active = self._verify_landed_active()
        dir_cache: Dict[str, Tuple[bool, List[Dict[str, Any]]]] = {}
        for record in records:
            if not active:
                result[id(record)] = self._landed_unknown("verify_disabled")
                continue
            cloud_dir = str(record.get("cloud_dir") or "").strip()
            if not cloud_dir:
                result[id(record)] = self._landed_unknown("missing_target_dir")
                continue
            if cloud_dir not in dir_cache:
                dir_cache[cloud_dir] = self._list_dir_files(cloud_dir)
            verified, files = dir_cache[cloud_dir]
            if not verified:
                result[id(record)] = self._landed_unknown("listing_unavailable")
                continue
            present_ids = {
                str(item.get("file_id") or "")
                for item in files
                if str(item.get("file_id") or "")
            }
            ids_by_name: Dict[str, str] = {}
            for entry in files:
                name = str(entry.get("file_name") or "").strip()
                if name:
                    ids_by_name.setdefault(name, str(entry.get("file_id") or ""))
            result[id(record)] = self._match_landed(
                file_id=str(
                    record.get("cloud_file_id") or record.get("file_id") or ""
                ),
                target_name=str(
                    record.get("target_file_name")
                    or record.get("file_name")
                    or ""
                ),
                present_ids=present_ids,
                ids_by_name=ids_by_name,
            )
        return result

    def _list_dir_files(
            self, cloud_dir: str
    ) -> Tuple[bool, List[Dict[str, Any]]]:
        """列出目录里的正片文件，返回 ``(是否拿到实测数据, 文件条目)``。"""
        cloud_directories = getattr(self, "_cloud_directories", None)
        if not cloud_directories or not cloud_dir:
            return False, []
        try:
            lookup = cloud_directories.resolve_directory(cloud_dir)
            if not lookup.checked:
                return False, []
            if lookup.directory_id is None:
                # 目录确实不存在（而不是请求失败）：按实测空目录处理
                return True, []
            listing = cloud_directories.list_directory(lookup.directory_id)
            if not listing.checked:
                return False, []
        except Exception as error:  # pragma: no cover - 防御性兜底
            logger.debug(f"落盘自检列目录异常：{cloud_dir}，{error}")
            return False, []
        is_extra = getattr(self, "_is_extra_clip", None)
        files: List[Dict[str, Any]] = []
        for entry in listing.files:
            if getattr(entry, "is_directory", False):
                continue
            name = str(getattr(entry, "name", "") or "")
            if not MediaFileParser.is_video(name):
                continue
            if callable(is_extra) and is_extra(name):
                continue
            files.append({
                "file_id": str(getattr(entry, "id", "") or ""),
                "file_name": name,
            })
        return True, files

    # ------------------------------------------------------------------
    # 判定与字段
    # ------------------------------------------------------------------
    @staticmethod
    def _landed_unknown(reason: str) -> Dict[str, Any]:
        return landed_unknown(reason)

    @staticmethod
    def _normalize_dir(path: Any) -> str:
        return str(path or "").strip().rstrip("/")

    def _recheck_other_cloud_dirs(
            self,
            subscribe,
            mediainfo,
            season: int,
            keyed: Dict[str, Dict[str, Any]],
            infos: Dict[str, Dict[str, Any]],
    ) -> None:
        """「查无实据」复核：记录自称落在别的目录（媒体库/其它路径）时再核一次。

        与 1.6.51「记录落在别的目录时本轮不越权判定」同口径：季目录里没有、但它自己
        声称的 ``cloud_dir`` 里有实物 → 仍按成功处置，不把落在别处的记录误记「待确认」。
        只在季目录自检已判「没有」时才多列一次目录（同目录只列一次）。
        """
        if not self._verify_landed_active():
            return
        candidates: List[Tuple[str, Dict[str, Any]]] = []
        for key, record in keyed.items():
            if (infos.get(key) or {}).get("landed") is not False:
                continue
            if not self._normalize_dir(record.get("cloud_dir")):
                # 记录没说自己落在哪 → 季目录自检的结论就是最终结论
                continue
            candidates.append((key, record))
        if not candidates:
            return
        try:
            _verified, season_dir, _files = self._season_dir_files(
                subscribe, mediainfo, season
            )
        except Exception:  # pragma: no cover - 防御性兜底
            season_dir = ""
        season_dir = self._normalize_dir(season_dir)
        suspects = [
            (key, record) for key, record in candidates
            if self._normalize_dir(record.get("cloud_dir")) != season_dir
        ]
        if not suspects:
            return
        recheck = self.verify_landed_records([record for _key, record in suspects])
        for key, record in suspects:
            info = recheck.get(id(record)) or {}
            if info.get("landed") is True:
                infos[key] = info

    def apply_landed_verification_to_records(
            self,
            subscribe,
            mediainfo,
            season: int,
            records: List[Dict[str, Any]],
    ) -> None:
        """对「成功」历史记录批量自检并按结果就地回填 / 改「待确认」。

        复用 ``verify_landed_items``（同一次季目录列目录，优先命中缓存）。
        """
        candidates = [
            record for record in (records or [])
            if str(record.get("status") or "") == "成功"
            # 用户手动标记成功的记录不再参与自检，避免隔天又被翻回「待确认」。
            and not record.get("manual_success")
        ]
        if not candidates:
            return
        keyed: Dict[str, Dict[str, Any]] = {}
        items: List[Dict[str, Any]] = []
        for index, record in enumerate(candidates):
            key = str(index)
            keyed[key] = record
            items.append({
                "key": key,
                "file_id": str(
                    record.get("cloud_file_id") or record.get("file_id") or ""
                ),
                "target_name": str(record.get("file_name") or ""),
            })
        infos = self.verify_landed_items(subscribe, mediainfo, season, items)
        self._recheck_other_cloud_dirs(subscribe, mediainfo, season, keyed, infos)
        for key, record in keyed.items():
            info = infos.get(key) or {}
            if not self._landed_confirms_success(info):
                record["status"] = "待确认"
                record.update(self._landed_verification_fields(info))
                logger.warning(
                    f"落盘自检未通过：{record.get('title') or '未知媒体'} "
                    f"S{int(season or 0):02d}"
                    f"E{int(record.get('episode') or 0):02d} "
                    f"{record.get('file_name') or '未记录'} 不在目标季目录"
                    f"（{info.get('reason') or 'missing_in_dir'}），记「待确认」"
                )
                continue
            fields = self._landed_verification_fields(info)
            if fields:
                record.update(fields)

    @staticmethod
    def _match_landed(
            file_id: str,
            target_name: str,
            present_ids: set,
            ids_by_name: Dict[str, str],
    ) -> Dict[str, Any]:
        return match_landed(file_id, target_name, present_ids, ids_by_name)

    @staticmethod
    def _landed_confirms_success(info: Optional[Dict[str, Any]]) -> bool:
        """``landed is not False`` → 可按成功处置；``None``=拿不到数据，维持旧行为。"""
        return landed_confirms_success(info)

    @staticmethod
    def _landed_verification_fields(
            info: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """把自检结果转成要写进历史记录的字段；拿不到数据时不加任何字段。"""
        return landed_verification_fields(info)
