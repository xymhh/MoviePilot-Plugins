"""115 网盘整理台 API。"""

from typing import Any, Dict, Optional

from .. import OwnerDelegator


class OrganizeApi(OwnerDelegator):
    def api_vue_organize_scan(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        request = payload or {}
        return self.start_scan(
            source_path=str(request.get("source_path") or ""),
            max_files=request.get("max_files") or 0,
            include_subtitles=request.get("include_subtitles"),
            auto_execute=bool(request.get("auto_execute")),
            trigger=str(request.get("trigger") or "手动"),
        )

    def api_vue_organize_cleanup(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        request = payload or {}
        return self.cleanup_empty_dirs(source_path=str(request.get("source_path") or ""))

    def api_vue_organize_status(self) -> dict:
        return self.status()

    def api_vue_organize_execute(self, payload: Optional[Dict[str, Any]] = None) -> dict:
        request = payload or {}
        keys = request.get("keys") or []
        if not isinstance(keys, list):
            keys = []
        return self.execute(keys=keys, trigger=str(request.get("trigger") or "手动"))

    def api_vue_organize_stop(self) -> dict:
        return self.stop()

    def api_vue_organize_history(self) -> dict:
        return self.history()
