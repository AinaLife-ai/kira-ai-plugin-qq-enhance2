"""通用格式化工具：用户/时间等。

框架标准（core/adapter/src/qq/im.py:619）：
    [Poke 用户1593462500(指尖落黑白)戳了戳你]
    [System 用户769690776(周武)禁言了你60秒]

即 `用户{QQ}({昵称})` 格式，昵称缺失时退回纯 QQ 号。
"""

from __future__ import annotations


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