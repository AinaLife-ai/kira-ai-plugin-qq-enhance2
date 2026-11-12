"""表情 ID → 中文名映射（对齐框架 qq/emoji.json 的机制）。"""

from __future__ import annotations

import json
import os
from typing import Any

_EMOJI_DICT: dict[str, str] = {}
_LOADED = False


def _load_dict():
    """加载框架的 emoji.json（如果存在）。"""
    global _EMOJI_DICT, _LOADED
    if _LOADED:
        return
    _LOADED = True
    # 尝试从框架的 QQ 适配器目录加载
    try:
        from core.adapter.src.qq.emoji import emoji_dict  # type: ignore
        if isinstance(emoji_dict, dict):
            _EMOJI_DICT.update({str(k): str(v) for k, v in emoji_dict.items()})
            return
    except Exception:
        pass
    # 回退：尝试直接读文件
    try:
        import core.adapter as _  # noqa
        base = os.path.dirname(os.path.abspath(__file__))
        # 插件目录 → 框架目录的相对路径不可知，尝试常见位置
        for candidate in (
            os.path.join(base, "..", "emoji.json"),
            os.path.join(base, "emoji.json"),
        ):
            if os.path.exists(candidate):
                with open(candidate, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _EMOJI_DICT.update({str(k): str(v) for k, v in data.items()})
                break
    except Exception:
        pass


def get_emoji_name(emoji_id: str) -> str:
    """获取表情中文名（找不到时返回 emoji_id 本身）。"""
    _load_dict()
    return _EMOJI_DICT.get(str(emoji_id), str(emoji_id))