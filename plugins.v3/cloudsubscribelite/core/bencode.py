"""最小 bencode 编码器（零依赖）。

来源：本用户另一仓库 MoviePilot-QuarkChannel（网盘直链频道）的同名模块
``plugins.v3/quarkchannel/bencode.py``，逻辑原样复制，只改了种子
``created by`` 字段与模块说明；字符串统一按 UTF-8 编码，字典键按字节序排序。

只用于把分享文件清单编码成 MoviePilot 可解析的合成种子，不实现解码。
"""

from __future__ import annotations

from typing import Any


def encode(value: Any) -> bytes:
    """把 Python 值编码为 bencode 字节串。

    :param value: 支持 int / bytes / str / list / tuple / dict（键为 str 或 bytes）
    :return: bencode 字节串
    :raises TypeError: 传入不支持的类型或 bool 时抛出
    """
    if isinstance(value, bool):
        raise TypeError("bencode 不支持 bool，请显式转换为 int")
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return encode(value.encode("utf-8"))
    if isinstance(value, (list, tuple)):
        return b"l" + b"".join(encode(item) for item in value) + b"e"
    if isinstance(value, dict):
        pairs: list[tuple[bytes, Any]] = []
        for key, item in value.items():
            raw_key = key if isinstance(key, bytes) else str(key).encode("utf-8")
            pairs.append((raw_key, item))
        pairs.sort(key=lambda pair: pair[0])
        return b"d" + b"".join(encode(k) + encode(v) for k, v in pairs) + b"e"
    raise TypeError(f"bencode 不支持的类型：{type(value).__name__}")


def info_dict(name: str, files: list[dict], piece_length: int = 262144,
              max_pieces_bytes: int = 4 * 1024 * 1024) -> dict:
    """构造种子 info 字典。

    pieces 长度必须等于 ceil(总大小 / piece length) * 20，
    否则生成的种子是非法的（部分客户端会直接判定无法下载）。
    文件很大时自动放大 piece length，保证 pieces 不超上限。

    :param name: 种子名（MoviePilot 会把它当作下载目录名）
    :param files: 文件清单，每项包含 length 与 path（相对路径字符串列表）
    :param piece_length: 初始分片长度
    :param max_pieces_bytes: pieces 字段体积上限
    :return: info 字典
    """
    total = sum(int(item["length"]) for item in files) or 1
    length = max(16384, int(piece_length))
    while (total + length - 1) // length * 20 > max_pieces_bytes:
        length *= 2
    piece_count = max(1, (total + length - 1) // length)
    return {
        "name": name,
        "piece length": length,
        "pieces": b"\x00" * (piece_count * 20),
        "files": [
            {"length": int(item["length"]), "path": list(item["path"])}
            for item in files
        ],
    }


def build_torrent(name: str, files: list[dict], comment: str) -> bytes:
    """生成合成种子字节串。

    :param name: 种子名
    :param files: 文件清单
    :param comment: 种子注释，插件用它识别自己的任务
    :return: 完整种子的 bencode 字节串
    """
    payload = {
        "comment": comment,
        "created by": "MoviePilot CloudSubscribeX",
        "encoding": "UTF-8",
        "info": info_dict(name, files),
    }
    return encode(payload)


def _skip_value(data: bytes, index: int) -> int:
    """跳过一段 bencode 值，返回下一个值的位置。

    :param data: bencode 字节串
    :param index: 当前值的起始下标
    :return: 下一个值的起始下标
    """
    head = data[index:index + 1]
    if head == b"i":
        return data.index(b"e", index) + 1
    if head in (b"l", b"d"):
        cursor = index + 1
        while data[cursor:cursor + 1] != b"e":
            cursor = _skip_value(data, cursor)
        return cursor + 1
    colon = data.index(b":", index)
    length = int(data[index:colon])
    return colon + 1 + length


def info_hash(torrent_bytes: bytes) -> str:
    """计算种子 info 段的 sha1。

    :param torrent_bytes: 完整种子字节串
    :return: 40 位十六进制 hash；解析失败时返回空字符串
    """
    import hashlib

    key = b"4:info"
    position = torrent_bytes.find(key)
    if position < 0:
        return ""
    start = position + len(key)
    try:
        end = _skip_value(torrent_bytes, start)
    except (ValueError, IndexError):
        return ""
    return hashlib.sha1(torrent_bytes[start:end]).hexdigest()
