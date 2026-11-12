"""表情 ID → 中文名映射（对齐框架 qq/emoji.json 的机制）。

加载策略（按可靠性排序）：
1. 从已加载的 QQ 适配器实例拿（最可靠，框架自己加载的）
2. 从框架源码路径推导（相对插件目录无法确定，用 importlib 定位框架包）
3. 都失败 → 空字典（显示原始 ID，功能不受影响）
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

_EMOJI_DICT: dict[str, str] = {}
_LOADED = False


def _load_dict():
    """加载框架的 emoji.json（幂等，只尝试一次）。"""
    global _EMOJI_DICT, _LOADED
    if _LOADED:
        return
    _LOADED = True

    # 路径 1：从框架已 import 的 core.adapter 模块推导
    try:
        import core.adapter as _ca
        base = os.path.dirname(os.path.abspath(_ca.__file__))
        # core/adapter/ → core/adapter/src/qq/emoji.json
        candidates = [
            os.path.join(base, "src", "qq", "emoji.json"),
            os.path.join(base, "qq", "emoji.json"),
            os.path.join(base, "emoji.json"),
        ]
        for c in candidates:
            if os.path.exists(c):
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    _EMOJI_DICT.update({str(k): str(v) for k, v in data.items()})
                    return
    except Exception:
        pass

    # 路径 2：从 core 包位置推导
    try:
        import core as _core
        base = os.path.dirname(os.path.abspath(_core.__file__))
        for c in (
            os.path.join(base, "adapter", "src", "qq", "emoji.json"),
            os.path.join(base, "adapter", "qq", "emoji.json"),
        ):
            if os.path.exists(c):
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data:
                    _EMOJI_DICT.update({str(k): str(v) for k, v in data.items()})
                    return
    except Exception:
        pass


def get_emoji_name(emoji_id: str) -> str:
    """获取表情中文名（找不到时返回 emoji_id 本身）。

    Args:
        emoji_id: 表情 ID（系统表情为短数字串，市场表情为字母数字串）。

    Returns:
        中文名（框架 emoji.json 有映射时），否则原始 ID。
    """
    if not emoji_id:
        return ""
    _load_dict()
    return _EMOJI_DICT.get(str(emoji_id), str(emoji_id))


def get_emoji_count() -> int:
    """已加载的表情映射条目数（用于诊断）。"""
    _load_dict()
    return len(_EMOJI_DICT)