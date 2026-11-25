"""失败原因文本：拆包 + 脱敏（纯标准库，drive 层与 core 层共用）。

背景（2026-11-16 LOST10 S01E05，1.10.7 复核）：失败原因里只剩
一份上传参数字典，看不到 115 的真正报错——

* p115oss 会把所有分片上传失败包成 MultipartUploadAbort，消息体
  就是一份上传参数字典（还含 user_key 登录凭证），真正的报错在
  异常链的 __cause__ 里；
* p115client 的接口报错是 OSError(errno, 响应字典)，str() 是一整
  段字典文本。

这两类文本都不能直接存进历史记录：前者看不到原因还泄凭证，后者
是一大段字典。本模块沿异常链走到根因，再从字典里抽 errno/error，
拼成一句可读的原因；所有文本过一遍脱敏。
"""

from __future__ import annotations

import ast
import re
from typing import Any, Optional

__all__ = ["sanitize_error_text", "describe_upload_error"]

#: 报错文本里必须脱敏的凭证类字段。值替换成 ***，键名保留。
#: 值字符集排除引号/逗号/分号/花括号/空白，避免一次匹配吞掉后面的
#: 字段（如 "token=abc; Authorization: ..." 里的分号后内容）。
_SENSITIVE_RE = re.compile(
    r"(?i)(['\"]?(?:user_key|userkey|access_token|refresh_token|token|authorization|password|passwd|cookie|secret)['\"]?\s*[:=]\s*(?:['\"]?(?:Bearer|Basic)\s+)?['\"]?)[^'\",};\s]+"
)

#: p115 响应字典里的报错文案键（按优先级）。
_ERROR_MESSAGE_KEYS = ("error", "error_msg", "message", "msg")
#: p115 响应字典里的报错码键（按优先级）。
_ERROR_CODE_KEYS = ("errno", "errNo", "errcode", "errCode", "code", "msg_code")


def sanitize_error_text(text: str) -> str:
    """把报错文本里的凭证类字段值替换成 ***（只动值，不动键名）。

    覆盖字典文本（'user_key': '...'）、赋值文本（token=...）与
    Authorization: Bearer <token> 三种写法；无凭证的文本原样返回。
    """
    if not text:
        return ""
    return _SENSITIVE_RE.sub(r"\1***", str(text))


def _extract_error_fields(payload: Any) -> str:
    """从 p115 响应字典里抽一句可读原因；不是响应字典返回空串。"""
    if not isinstance(payload, dict):
        return ""
    message = ""
    for key in _ERROR_MESSAGE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            message = value.strip()
            break
    code = None
    for key in _ERROR_CODE_KEYS:
        value = payload.get(key)
        if value is not None and value != "":
            code = value
            break
    if message and code is not None:
        return f"errno {code}：{message}"
    if message:
        return message
    if code is not None:
        return f"errno {code}"
    return ""


def _parse_dict_text(text: str) -> Optional[dict]:
    """消息本身就是字典文本时解析它；不是则返回 None。"""
    stripped = (text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        parsed = ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _root_cause(error: BaseException) -> BaseException:
    """沿 __cause__/__context__ 走到异常链的根因（防环，上限 8 层）。"""
    seen = {id(error)}
    current = error
    for _ in range(8):
        nxt = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
        if nxt is None or id(nxt) in seen:
            break
        seen.add(id(nxt))
        current = nxt
    return current


def describe_upload_error(error: BaseException) -> str:
    """把上传异常翻译成一句可存进历史记录的原因。

    根因自带响应字典（OSError(errno, resp)）时抽 errno/error；
    根因消息本身是字典文本时解析后再抽；字典里没有 error/errno、
    又含上传参数键（filename/filesha1）时，说明拿到的只是 p115oss
    包的那份参数字典——除文件名外没有有用信息，别把它存进记录。
    """
    root = _root_cause(error)
    for arg in getattr(root, "args", ()):
        text = _extract_error_fields(arg)
        if text:
            return sanitize_error_text(text)
    message = str(root)
    parsed = _parse_dict_text(message)
    if parsed is not None:
        text = _extract_error_fields(parsed)
        if text:
            return sanitize_error_text(text)
        if "filename" in parsed and "filesha1" in parsed:
            return "115 未返回具体报错（分片上传失败）"
        return sanitize_error_text(message)
    cleaned = sanitize_error_text(message)
    return cleaned or type(root).__name__
