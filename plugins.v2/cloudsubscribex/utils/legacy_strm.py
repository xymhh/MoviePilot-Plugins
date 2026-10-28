"""把 P115StrmHelper 生成的 STRM 改写成本插件端点。

只换 URL 前缀，查询参数（pickcode / share_code / receive_code / id）
原样保留，因为两边的换链语义一致。
"""
from pathlib import Path
from typing import Iterable, Optional

LEGACY_MARKER = "P115StrmHelper/redirect_url"
TARGET_PATH = "CloudSubscribeX/redirect"


def migrate_strm_content(content: str) -> Optional[str]:
    """返回改写后的内容；不需要改写时返回 None。"""
    text = str(content or "")
    if LEGACY_MARKER not in text:
        return None
    return text.replace(LEGACY_MARKER, TARGET_PATH)


def iter_strm_files(root: str) -> Iterable[Path]:
    base = Path(str(root or ""))
    if not base.exists():
        return []
    return base.rglob("*.strm")
