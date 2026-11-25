"""媒体服务器路径与本地路径之间的映射。

配置项 ``media_server_path_mappings`` 每行是 ``媒体服务器路径=本地路径``，
例如 ``/media/strm=/strm``。Emby 的 ``Items?Path=`` 只认它自己挂载出来的
路径，直接传 MP 侧路径会永远查不到条目（线上实测：入库后连刷 4 次
「仍未找到媒体项，跳过媒体信息提取」）。
"""
from pathlib import Path
from typing import List, Tuple, Union


def parse_path_mappings(mappings: str) -> List[Tuple[str, str]]:
    """解析映射行，返回 ``(本地根, 媒体服务器根)``，长前缀优先。"""
    pairs: List[Tuple[str, str]] = []
    for line in str(mappings or "").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        server_root, _, local_root = raw.partition("=")
        server_root = "/" + server_root.strip().strip("/")
        local_root = "/" + local_root.strip().strip("/")
        if server_root == "/" or local_root == "/":
            continue
        pairs.append((local_root, server_root))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def map_local_to_server_path(
        path: Union[Path, str], mappings: str
) -> str:
    """把本地路径翻译成媒体服务器路径；无匹配时原样返回（补前导斜杠）。"""
    text = "/" + str(path or "").replace("\\", "/").strip("/")
    for local_root, server_root in parse_path_mappings(mappings):
        if text == local_root or text.startswith(local_root.rstrip("/") + "/"):
            return server_root.rstrip("/") + text[len(local_root):]
    return text
