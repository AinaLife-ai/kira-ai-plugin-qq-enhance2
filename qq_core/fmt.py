"""通用格式化工具：用户标识、文件 URI 归一化。

框架标准（core/adapter/src/qq/im.py:619）：
    [Poke 用户1593462500(指尖落黑白)戳了戳你]
    [System 用户769690776(周武)禁言了你60秒]

即 `用户{QQ}({昵称})` 格式，昵称缺失时退回纯 QQ 号。
"""

from __future__ import annotations

import re


def fmt_user(qq: str, name: str = "") -> str:
    """格式化用户标识（对齐框架 `用户{QQ}({昵称})` 格式）。

    Args:
        qq: QQ 号。
        name: 昵称（可为空）。

    Returns:
        `用户123(张三)` / `用户123` / `用户(张三)` / `用户(未知)`。
    """
    qq = str(qq or "").strip()
    name = str(name or "").strip()
    if qq and name:
        return f"用户{qq}({name})"
    if qq:
        return f"用户{qq}"
    if name:
        return f"用户({name})"
    return "用户(未知)"


def safe_int(val, default: int) -> int:
    """安全转 int（用户手改配置 / WebUI 存成字符串时不应崩）。

    Args:
        val: 原始值（可能是 int / "20" / None / "abc"）。
        default: 转换失败时的回退值。

    Returns:
        整数值。
    """
    if val is None or val == "":
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        try:
            return int(float(val))  # "20.0" -> 20
        except (TypeError, ValueError):
            return default


def safe_float(val, default: float) -> float:
    """安全转 float。

    Args:
        val: 原始值。
        default: 转换失败时的回退值。

    Returns:
        浮点值。
    """
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# 协议端（NapCat/LLOneBot 的 uriToLocalFile）支持的 file 前缀
_FILE_URI_PREFIXES = ("base64://", "data:", "http://", "https://", "file://")


def normalize_file_uri(value) -> str:
    """归一化 file 参数（协议端只认特定前缀）。

    协议端支持的格式：
      · base64://<data>                OneBot base64
      · data:image/png;base64,<data>   Data URL
      · http:// / https://             远程 URL
      · file:///path                   文件 URL
      · /abs/path 或相对路径            本地文件

    **纯 base64 字符串（无前缀）会被当作文件名 → 失败**，
    这里自动识别并补 `base64://` 前缀（长度 ≥64 且字符集符合 base64）。

    Args:
        value: 用户/LLM 传入的 file 值。

    Returns:
        归一化后的字符串（空输入返回空串）。
    """
    s = str(value or "").strip()
    if not s:
        return ""
    # 已带前缀 → 原样返回
    for prefix in _FILE_URI_PREFIXES:
        if s.startswith(prefix):
            return s
    # 看起来像纯 base64 → 补前缀
    if len(s) >= 64 and re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        compact = re.sub(r"\s+", "", s)
        if len(compact) >= 64:
            return f"base64://{compact}"
    # 其他情况（本地路径等）原样返回
    return s