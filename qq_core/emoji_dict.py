"""表情 ID → 中文名映射（对齐框架 qq/emoji.json 的机制）。

加载策略（按可靠性排序）：
1. 从框架 core.adapter / core 包位置推导 emoji.json
2. 都失败 → 空字典（显示原始 ID，功能不受影响）

注意：只有成功加载到数据才标记 _LOADED，避免"插件早于框架模块加载"
时永久失去表情映射（框架的 emoji.json 是随 core.adapter 提供的）。
"""

from __future__ import annotations

import json
import os

_EMOJI_DICT: dict[str, str] = {}
_LOADED = False


def _try_load_from(base: str, candidates: tuple[str, ...]) -> bool:
    """尝试从给定基准目录加载 emoji.json。

    Args:
        base: 基准目录。
        candidates: 相对 base 的候选路径。

    Returns:
        True = 加载成功。
    """
    for rel in candidates:
        path = os.path.join(base, rel)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                _EMOJI_DICT.update({str(k): str(v) for k, v in data.items()})
                return True
        except Exception:
            continue
    return False


def _load_dict():
    """加载框架的 emoji.json（幂等，成功后不再重试）。"""
    global _LOADED
    if _LOADED:
        return

    # 路径 1：从已 import 的 core.adapter 模块推导
    try:
        import core.adapter as _ca
        base = os.path.dirname(os.path.abspath(_ca.__file__))
        if _try_load_from(base, (
            os.path.join("src", "qq", "emoji.json"),
            os.path.join("qq", "emoji.json"),
            "emoji.json",
        )):
            _LOADED = True
            return
    except Exception:
        pass

    # 路径 2：从 core 包位置推导
    try:
        import core as _core
        base = os.path.dirname(os.path.abspath(_core.__file__))
        if _try_load_from(base, (
            os.path.join("adapter", "src", "qq", "emoji.json"),
            os.path.join("adapter", "qq", "emoji.json"),
        )):
            _LOADED = True
            return
    except Exception:
        pass

    # 路径 3：按插件目录相对定位（框架与插件同级部署时）
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        # 插件目录 → 上溯找 core/adapter/src/qq/emoji.json
        for depth in range(1, 5):
            base = os.path.abspath(os.path.join(here, *([".."] * depth)))
            if _try_load_from(base, (os.path.join("core", "adapter", "src", "qq", "emoji.json"),)):
                _LOADED = True
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