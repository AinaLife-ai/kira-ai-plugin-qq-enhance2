"""表情显示格式测试：确保 bot 能拿到 ID 回贴/跟贴。

对齐框架 message_formatter.py:70-74：
    [Emoji {desc} (ID: {emoji_id})]  /  [Emoji {emoji_id}]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_system_emoji_has_id():
    """系统表情：中文名 + ID 都要有。"""
    from qq_core.emoji_display import format_emoji
    r = format_emoji("4")
    assert "4" in r, f"必须含 ID（bot 靠它回贴）: {r}"
    assert "ID:" in r, f"必须标注 ID: {r}"


def test_market_emoji_keeps_id():
    """市场表情（无中文名）：ID 必须保留。"""
    from qq_core.emoji_display import format_emoji
    r = format_emoji("128077")
    assert "128077" in r, f"市场表情必须保留 ID: {r}"


def test_empty_emoji():
    """空 ID 返回空串（不崩）。"""
    from qq_core.emoji_display import format_emoji
    assert format_emoji("") == ""


def test_format_emoji_list_with_count():
    """列表 + 次数。"""
    from qq_core.emoji_display import format_emoji_list
    r = format_emoji_list(["4", "9"], 2)
    assert "4" in r and "9" in r and "×2" in r, f"列表应含所有 ID 和次数: {r}"


def test_annotation_carries_id():
    """贴表情标注必须携带 ID（否则 bot 无法回贴）。"""
    import time
    from qq_core.emoji_rewriter import archive_emoji_like, _EMOJI_STORE
    _EMOJI_STORE.clear()
    archive_emoji_like(
        sid="s", message_id="m", message_text="x", emoji_ids=["4"],
        count=1, operator="1", operator_name="张三", ts=int(time.time()),
    )
    ann = _EMOJI_STORE["s"][0]["annotation"]
    assert "ID: 4" in ann or "ID:4" in ann, f"标注必须带 ID: {ann}"
    print(f"  标注: {ann!r}")


def test_wake_text_carries_id():
    """唤醒文本必须携带 ID。"""
    from qq_core.emoji_display import format_emoji_list
    text = format_emoji_list(["4"], 1)
    assert "ID:" in text


if __name__ == "__main__":
    test_system_emoji_has_id()
    test_market_emoji_keeps_id()
    test_empty_emoji()
    test_format_emoji_list_with_count()
    test_annotation_carries_id()
    test_wake_text_carries_id()
    print("=== 表情 ID 可见性测试全部通过 ===")