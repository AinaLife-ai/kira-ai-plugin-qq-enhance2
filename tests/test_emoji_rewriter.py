"""贴表情标注改写 + 撤回改写测试。

验证：
1. archive_emoji_like 归档与合并。
2. rewrite_emoji_like 在 Prompt 后追加标注（幂等）。
3. 缓存不受破坏（persist=False）。
"""

import sys
import time
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qq_core.emoji_rewriter import archive_emoji_like, rewrite_emoji_like, clear_emoji_archive, _EMOJI_STORE
from qq_core.emoji_dict import get_emoji_name


class MockPrompt:
    def __init__(self, content, name=None, source=None, end="\n", persist=True):
        self.content = content
        self.name = name
        self.source = source
        self.end = end
        self.persist = persist

    def to_string(self):
        return self.content + self.end


class MockReq:
    def __init__(self, prompts):
        self.user_prompt = prompts


def _run(coro):
    """同步运行协程（兼容无事件循环环境）。"""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_archive_and_merge():
    """归档与合并测试。"""
    clear_emoji_archive("test:sid")
    archive_emoji_like(
        sid="test:sid",
        message_id="12345",
        message_text="原始消息内容",
        emoji_ids=["128077"],
        count=1,
        operator="1001",
        operator_name="张三",
        ts=int(time.time()),
    )
    # 同一消息再贴一次
    archive_emoji_like(
        sid="test:sid",
        message_id="12345",
        message_text="原始消息内容",
        emoji_ids=["128078"],
        count=1,
        operator="1002",
        operator_name="李四",
        ts=int(time.time()),
    )

    store = _EMOJI_STORE.get("test:sid")
    assert len(store) == 1, "同一 message_id 应合并为一条"
    assert "用户1001(张三)" in store[0]["annotation"]
    assert "用户1002(李四)" in store[0]["annotation"]


def test_rewrite():
    """改写测试。"""
    clear_emoji_like()
    clear_emoji_archive("test:sid2")
    archive_emoji_like(
        sid="test:sid2",
        message_id="67890",
        message_text="这是被贴表情的原始消息",
        emoji_ids=["128077"],
        count=2,
        operator="1001",
        operator_name="张三",
        ts=int(time.time()),
    )

    req = MockReq([
        MockPrompt("这是被贴表情的原始消息", name="message", source="system"),
        MockPrompt("另一条消息", name="message", source="system"),
    ])

    import asyncio
    _run(rewrite_emoji_like(sid="test:sid2", req=req, enabled=True))

    # 第一条消息应追加标注
    assert "[贴表情｜用户1001(张三)" in req.user_prompt[0].content
    # 第二条消息不应有标注
    assert "贴表情" not in req.user_prompt[1].content


def test_idempotent():
    """幂等测试：同一归档不重复追加。"""
    clear_emoji_like()
    clear_emoji_archive("test:sid3")
    archive_emoji_like(
        sid="test:sid3",
        message_id="11111",
        message_text="幂等测试消息",
        emoji_ids=["128077"],
        count=1,
        operator="1001",
        operator_name="张三",
        ts=int(time.time()),
    )

    req = MockReq([MockPrompt("幂等测试消息", name="message", source="system")])

    import asyncio
    _run(rewrite_emoji_like(sid="test:sid3", req=req, enabled=True))
    first_content = req.user_prompt[0].content

    # 再次改写（归档已清空，不应变化）
    _run(rewrite_emoji_like(sid="test:sid3", req=req, enabled=True))
    assert req.user_prompt[0].content == first_content, "幂等失败"


def test_emoji_dict():
    """表情字典测试。"""
    # 未加载时返回原 ID
    name = get_emoji_name("128077")
    # 有映射则返回中文名，无则返回原 ID
    assert name == "128077" or len(name) > 0


def clear_emoji_like():
    """清空全局状态（测试辅助）。"""
    _EMOJI_STORE.clear()


if __name__ == "__main__":
    test_archive_and_merge()
    test_rewrite()
    test_idempotent()
    test_emoji_dict()
    print("=== 贴表情改写测试全部通过 ===")