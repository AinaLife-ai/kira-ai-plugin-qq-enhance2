import sys
import time
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from qq_core.emoji_rewriter import archive_emoji_like, rewrite_emoji_like, clear_emoji_archive, _EMOJI_STORE
from qq_core.emoji_dict import get_emoji_name
# 使用框架真实 Prompt（conftest 装的 stub 提供），
# 确保 rewrite 内的 isinstance(p, Prompt) 成立
from core.prompt_manager import Prompt as MockPrompt


class MockReq:
    def __init__(self, prompts):
        self.user_prompt = prompts


def _run(coro):
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
    _EMOJI_STORE.clear()
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
    _EMOJI_STORE.clear()
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

    _run(rewrite_emoji_like(sid="test:sid2", req=req, enabled=True))

    # 第一条消息应追加标注
    assert "↑该条消息被贴了" in req.user_prompt[0].content and "张三" in req.user_prompt[0].content
    # 第二条消息不应有标注
    assert "↑该条消息被贴了" not in req.user_prompt[1].content


def test_idempotent():
    """幂等测试：同一归档不重复追加。"""
    _EMOJI_STORE.clear()
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

    _run(rewrite_emoji_like(sid="test:sid3", req=req, enabled=True))
    first_content = req.user_prompt[0].content

    # 再次改写（归档已清空，不应变化）
    _run(rewrite_emoji_like(sid="test:sid3", req=req, enabled=True))
    assert req.user_prompt[0].content == first_content, "幂等失败"


def test_emoji_dict():
    """表情字典测试。"""
    name = get_emoji_name("128077")
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

def test_notice_line_never_annotated():
    """Notice 行是事件描述，不能被追加 `↑该条消息被贴了…`（用户实机反馈的重复）。"""
    from qq_core.emoji_rewriter import EmojiRewriter
    from qq_core.fmt import is_notice_line

    rw = EmojiRewriter()
    sid = "test:notice"
    original = "这儿的规矩，就是个旁人管0_o"
    rw.archive(sid=sid, message_id="900001", message_text=original,
               emoji_ids=["66"], count=1, operator="769690776",
               operator_name="周武", ts=int(time.time()))

    notice_line = ("[Nov 13 2026 01:31 Fri] Notice [group_id: 188395693, user_id: 769690776] | "
                   "[System 用户769690776(周武)给用户3967118487(C7)的消息贴了爱心 (ID: 66)"
                   "（\"这儿的规矩，就是个旁人管0_o...\"）]")
    message_line = ("[Nov 13 2026 01:28 Fri] [message_id: 900001] [group_name: 绿岛酒吧 "
                    "group_id: 188395693 user_nickname: C7, user_id: 3967118487] | " + original)

    assert is_notice_line(notice_line) is True
    assert is_notice_line(message_line) is False

    class Req:
        def __init__(self):
            self.user_prompt = [MockPrompt(notice_line, name="message", source="system"),
                                MockPrompt(message_line, name="message", source="system")]
            self.messages = []

    req = Req()
    _run(rw.rewrite(sid=sid, req=req))

    assert "↑该条消息被贴了" not in req.user_prompt[0].content, "Notice 行不应被标注"
    assert "↑该条消息被贴了" in req.user_prompt[1].content, "原消息应被标注"
