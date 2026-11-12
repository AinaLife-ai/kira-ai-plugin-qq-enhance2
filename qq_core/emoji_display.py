"""表情显示格式化：对齐框架 message_formatter.py 的 `[Emoji {desc} (ID: {id})]` 格式。

框架源码（core/workflow/src/im/message_formatter.py:70-74）：
    elif isinstance(ele, Emoji):
        if ele.emoji_desc:
            message_str += f"[Emoji {ele.emoji_desc} (ID: {ele.emoji_id})]"
        else:
            message_str += f"[Emoji {ele.emoji_id}]"

为什么必须带 ID：bot 要"回贴/跟贴"同类表情时，必须拿到真实 emoji_id
才能调用 set_msg_emoji_like；只给中文名无法映射回 ID。

本模块的 `format_emoji` 保持同样的信息量（描述 + ID），
但适配"多人多次贴表情合并"的紧凑场景。
"""

from __future__ import annotations

from .emoji_dict import get_emoji_name


def format_emoji(emoji_id: str) -> str:
    """格式化单个表情（对齐框架格式）。

    Args:
        emoji_id: 表情 ID。

    Returns:
        `表情名 (ID: xxx)` 或 `ID: xxx`（无中文名时）。
    """
    if not emoji_id:
        return ""
    name = get_emoji_name(emoji_id)
    if name and name != str(emoji_id):
        return f"{name} (ID: {emoji_id})"
    # 无中文名（市场表情等）：仍保留 ID，保证可回贴
    return f"ID: {emoji_id}"


def format_emoji_list(emoji_ids: list[str], count: int = 1) -> str:
    """格式化表情列表（合并场景，带次数）。

    Args:
        emoji_ids: 表情 ID 列表。
        count: 总次数（>1 时附加 ×N）。

    Returns:
        格式化文本，如 `🔥 (ID: 128077) ×2`。
    """
    if not emoji_ids:
        return "?"
    parts = [format_emoji(e) for e in emoji_ids if e]
    if not parts:
        return "?"
    text = "/".join(parts)
    if count > 1:
        text += f"×{count}"
    return text