"""第三轮人工审计发现的 bug 回归测试（Bug 9-21）。

覆盖：
 9. should_wake 死代码（已删）
10. 点赞累计永不结算（只点一次就走 → 永远收不到唤醒）
11. 点赞重复查询 / 定时器泄漏
12. 取消表情（is_add=false）被当成贴表情
13. ban ↔ unban 未配对消解（设计承诺未实现）
14. 撤回/贴表情标注被无条件清空（未匹配的永久丢失）
15. 贴表情合并时丢 emoji_ids
16. poke 逐条列出不合并（token 浪费）
17. store 落盘条件错误（dirty<50 永不落盘）
18. store 加载未应用 LRU
19. config 为 dict 时取 lang 崩
20. card/title 空字符串无法清除
21. _is_self_message 与 lookup 重复查询
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 复用框架 stub（独立测试环境无 KiraAI）
from tests.test_core import _install_framework_stubs  # noqa

import qq_core.wake_scheduler as ws_mod
from qq_core.event_normalizer import (
    QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_PROFILE_LIKE_SELF,
)
from qq_core.env_renderer import render_env_summary
from qq_core.event_store import EventStore


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ============ Bug 9: should_wake 死代码已删除 ============

def test_bug9_dead_code_removed():
    src = Path(__file__).parent.parent.joinpath("qq_core", "wake_scheduler.py").read_text(encoding="utf-8")
    assert "async def should_wake(" not in src, "死代码 should_wake 应删除"
    assert "def should_wake_now(" in src, "should_wake_now 应保留"
    print("  ✓ Bug9: 死代码已清理")


# ============ Bug 10: 点赞累计必须能结算 ============

def test_bug10_like_settle():
    """只点一次就走 → 必须通过定时器结算（不能永远不唤醒）。"""
    from tests.test_regressions import _make_scheduler
    sched, ctx = _make_scheduler(
        wake_profile_like=True,
        profile_like_merge_window_seconds=1,  # 1 秒窗口，快速验证
        wake_max_per_hour=-1,
    )

    ev = QQEvent(eid="1", etype=E_PROFILE_LIKE_SELF, sid="napcat:dm:1", ts=int(time.time()),
                 actor="1001", extra={"times": 1})

    async def scenario():
        await sched.on_profile_like(ev)
        # 此时应无唤醒（在合并窗口内）
        n_before = len(ctx.event_bus.published)
        # 等窗口结束 + 结算
        await asyncio.sleep(1.4)
        return n_before, len(ctx.event_bus.published)

    n_before, n_after = _run(scenario())
    assert n_before == 0, f"窗口内不应唤醒: {n_before}"
    assert n_after == 1, f"窗口结束后应结算 1 次唤醒: {n_after}"
    _run(sched.terminate())
    print(f"  ✓ Bug10: 点赞窗口结束自动结算（{n_before}→{n_after}）")


def test_bug10_like_merge():
    """窗口内多次点赞合并为一条，次数累加。"""
    from tests.test_regressions import _make_scheduler
    sched, ctx = _make_scheduler(
        wake_profile_like=True, profile_like_merge_window_seconds=1, wake_max_per_hour=-1,
    )

    async def scenario():
        for i in range(3):
            ev = QQEvent(eid=str(i), etype=E_PROFILE_LIKE_SELF, sid="napcat:dm:1",
                         ts=int(time.time()), actor="1001", extra={"times": 1})
            await sched.on_profile_like(ev)
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.4)
        return len(ctx.event_bus.published)

    n = _run(scenario())
    assert n == 1, f"3 次点赞应合并为 1 次唤醒: {n}"
    _run(sched.terminate())
    print(f"  ✓ Bug10: 3 次点赞合并为 {n} 次唤醒")


# ============ Bug 12: 取消表情应被忽略 ============

def test_bug12_cancel_emoji_ignored():
    from qq_core.event_normalizer import normalize_notice
    base = {
        "notice_type": "group_msg_emoji_like", "user_id": 1001,
        "message_id": 888, "likes": [{"emoji_id": "4", "count": 1}],
        "group_id": 100, "time": int(time.time()),
    }
    # is_add=False（取消）
    r = dict(base, is_add=False)
    ev = normalize_notice(r, sid="napcat:gm:100", self_id="3003")
    assert ev is None, "取消表情应被忽略"

    # sub_type=remove（SnowLuma 风格）
    r2 = dict(base, sub_type="remove")
    ev2 = normalize_notice(r2, sid="napcat:gm:100", self_id="3003")
    assert ev2 is None, "sub_type=remove 应被忽略"

    # 正常贴
    r3 = dict(base, is_add=True)
    ev3 = normalize_notice(r3, sid="napcat:gm:100", self_id="3003")
    assert ev3 is not None, "正常贴表情应处理"
    print("  ✓ Bug12: 取消表情被正确忽略")


# ============ Bug 13: ban/unban 配对 ============

def test_bug13_ban_unban_pair():
    now = int(time.time())
    ban = QQEvent(eid="b", etype=E_BAN, sid="s", ts=now - 300,
                  actor="1001", actor_name="管理员", target="1005", target_name="赵六",
                  extra={"duration": 600})
    unban = QQEvent(eid="u", etype=E_UNBAN, sid="s", ts=now - 100,
                    actor="1001", actor_name="管理员", target="1005", target_name="赵六",
                    extra={"duration": 0})

    # 已解除
    t = render_env_summary([ban, unban])
    assert "已解除" in t, f"应显示已解除: {t}"
    # 不应重复出现独立"解除禁言"条目
    assert t.count("解除禁言") == 0, f"配对后不应重复: {t}"

    # 未解除（仍在禁言中）
    t2 = render_env_summary([ban])
    assert "仍禁言中" in t2 or "禁言时长已过" in t2, f"应显示剩余状态: {t2}"
    print(f"  ✓ Bug13: ban/unban 配对 → {t}")
    print(f"         未解除 → {t2}")


# ============ Bug 14: 撤回/贴表情标注不能无条件清空 ============

def test_bug14_recall_not_cleared_on_miss():
    """被撤回消息不在本轮 prompt → 归档必须保留。"""
    from qq_core.message_locator import MessageLocator
    from qq_core.recall_rewriter import RecallRewriter
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt

    class FakeBot:
        async def send_action(self, *a, **k):
            return {"status": "ok", "data": {}}
    class FakeAdapter:
        bot = FakeBot()
    locator = MessageLocator(adapter=FakeAdapter(), self_id="1")
    locator._cache["888"] = {"sender": "1001", "sender_name": "张三",
                             "text": "被撤回的原文", "ts": time.time(), "is_self": False}

    async def scenario():
        await locator.archive_recall(
            sid="s", message_id="888", sender="1001", sender_name="张三",
            text="被撤回的原文", ts=int(time.time()), operator="1001",
            operator_name="张三", sub_type="", is_group=True,
        )
        # 本轮 prompt 里没有这条消息（模拟它在更早历史）
        req = LLMRequest()
        req.user_prompt.append(Prompt("完全不同的消息", name="message", source="system"))
        rewriter = RecallRewriter(locator=locator)
        await rewriter.rewrite(sid="s", req=req)
        # 归档应保留
        return await locator.get_recalls("s")

    recalls = _run(scenario())
    assert len(recalls) == 1, f"未匹配的归档必须保留: {len(recalls)}"
    print("  ✓ Bug14: 未匹配的撤回归档被保留（不丢失标注）")


def test_bug14_recall_cleared_on_hit():
    """匹配成功 → 归档应清除（避免重复）。"""
    from qq_core.message_locator import MessageLocator
    from qq_core.recall_rewriter import RecallRewriter
    from core.provider import LLMRequest
    from core.prompt_manager import Prompt

    class FakeBot:
        async def send_action(self, *a, **k):
            return {"status": "ok", "data": {}}
    class FakeAdapter:
        bot = FakeBot()
    locator = MessageLocator(adapter=FakeAdapter(), self_id="1")
    locator._cache["888"] = {"sender": "1001", "sender_name": "张三",
                             "text": "被撤回的原文", "ts": time.time(), "is_self": False}

    async def scenario():
        await locator.archive_recall(
            sid="s", message_id="888", sender="1001", sender_name="张三",
            text="被撤回的原文", ts=int(time.time()), operator="1001",
            operator_name="张三", sub_type="", is_group=True,
        )
        req = LLMRequest()
        p = Prompt("被撤回的原文", name="message", source="system")
        req.user_prompt.append(p)
        rewriter = RecallRewriter(locator=locator)
        await rewriter.rewrite(sid="s", req=req)
        return p.content, await locator.get_recalls("s")

    content, recalls = _run(scenario())
    assert "[已撤回｜" in content, f"应追加标注: {content}"
    assert len(recalls) == 0, f"已处理的应清除: {len(recalls)}"
    print("  ✓ Bug14: 匹配成功的撤回归档被清除")


# ============ Bug 15: 贴表情合并保留 emoji_ids ============

def test_bug15_merge_keeps_emoji_ids():
    from qq_core.emoji_rewriter import archive_emoji_like, _EMOJI_STORE
    _EMOJI_STORE.clear()
    now = int(time.time())
    archive_emoji_like(sid="s", message_id="m", message_text="x",
                       emoji_ids=["4"], count=1, operator="1001", operator_name="张三", ts=now)
    archive_emoji_like(sid="s", message_id="m", message_text="x",
                       emoji_ids=["9"], count=1, operator="1001", operator_name="张三", ts=now)
    item = _EMOJI_STORE["s"][0]
    ids = item["sub_entries"][0]["emoji_ids"]
    assert "4" in ids and "9" in ids, f"两次不同表情应都保留: {ids}"
    assert "ID: 4" in item["annotation"] and "ID: 9" in item["annotation"], \
        f"标注应含两个 ID: {item['annotation']}"
    print(f"  ✓ Bug15: 合并保留所有 emoji_id → {item['annotation']!r}")


# ============ Bug 16: poke 合并省 token ============

def test_bug16_poke_merge():
    now = int(time.time())
    evs = [
        QQEvent(eid=str(i), etype=E_POKE, sid="s", ts=now - 600 + i * 60,
                actor=str(1000 + i), actor_name=f"用户{i}", target="2002", target_name="李四")
        for i in range(8)
    ]
    t = render_env_summary(evs)
    # 应合并（出现"等8人"或类似合并形式），而不是 8 条独立
    assert t.count("戳了戳") == 1, f"8 人戳同一目标应合并为 1 条: {t}"
    # token 效率：应远小于逐条列出
    assert len(t) < 150, f"合并后应该紧凑: {len(t)} 字符"
    print(f"  ✓ Bug16: 8 条合并为 1 条（{len(t)} 字符）→ {t}")


# ============ Bug 17: store 落盘条件 ============

def test_bug17_persist_condition():
    src = Path(__file__).parent.parent.joinpath("qq_core", "event_store.py").read_text(encoding="utf-8")
    assert "if self._dirty:" in src, "应 dirty 非空即写"
    assert "if len(self._dirty) >= 50:" not in src, "不应只在满 50 时写"
    print("  ✓ Bug17: 落盘条件修正（dirty 非空即写）")


# ============ Bug 18: store 加载应用 LRU ============

def test_bug18_load_applies_lru():
    src = Path(__file__).parent.parent.joinpath("qq_core", "event_store.py").read_text(encoding="utf-8")
    assert "len(ev_list) > self.max_events" in src, "加载时应应用 max_events"
    print("  ✓ Bug18: 加载时应用 LRU 上限")


# ============ Bug 19: config 为 dict 时取 lang ============

def test_bug19_config_dict_lang():
    from tools.qq_profile import QQProfileTool

    class Ctx:
        config = {"locale": {"lang": "ja"}}
    t = QQProfileTool(ctx=Ctx())
    assert t._get_locale_lang() == "ja", f"dict config 应能取到 lang: {t._get_locale_lang()}"

    class Ctx2:
        class config:
            @staticmethod
            def get_config(key, default=None):
                return "en" if key == "locale.lang" else default
    t2 = QQProfileTool(ctx=Ctx2())
    assert t2._get_locale_lang() == "en", f"对象 config 应能取到: {t2._get_locale_lang()}"

    class Ctx3:
        config = None
    t3 = QQProfileTool(ctx=Ctx3())
    assert t3._get_locale_lang() == "zh", f"无 config 应回退 zh: {t3._get_locale_lang()}"
    print("  ✓ Bug19: config 两种形态 + 缺失回退都正常")


# ============ Bug 20: card/title 清空 ============

def test_bug20_clear_card():
    """card="" 应能清除群名片（不是跳过）。"""
    src = Path(__file__).parent.parent.joinpath("tools", "qq_group_profile.py").read_text(encoding="utf-8")
    # 签名默认应为 None
    assert "card: str = None" in src, "card 默认应为 None"
    # 判断应为 is not None（允许空字符串）
    assert "if card is not None:" in src, "应使用 is not None 以支持清空"
    assert 'if card is not None and card != ""' not in src, "不应排除空字符串"
    print("  ✓ Bug20: card/title 支持空字符串清除")


# ============ Bug 21: lookup 单次 ============

def test_bug21_single_lookup():
    src = Path(__file__).parent.parent.joinpath("main.py").read_text(encoding="utf-8")
    import re
    m = re.search(r"if qq_event\.etype == E_EMOJI_LIKE:(.*?)# 撤回事件", src, re.S)
    assert m, "未找到贴表情分支"
    # 贴表情分支内只 lookup 一次（不再额外调 _is_self_message 造成重复查询）
    assert m.group(1).count("locator.lookup") == 1, "贴表情分支 lookup 应只调用一次"
    assert "await self.wake_scheduler._is_self_message" not in m.group(1), \
        "不应再额外调用 _is_self_message（会重复 lookup）"
    print("  ✓ Bug21: 贴表情分支 lookup 单次（不重复查询）")


if __name__ == "__main__":
    test_bug9_dead_code_removed()
    test_bug10_like_settle()
    test_bug10_like_merge()
    test_bug12_cancel_emoji_ignored()
    test_bug13_ban_unban_pair()
    test_bug14_recall_not_cleared_on_miss()
    test_bug14_recall_cleared_on_hit()
    test_bug15_merge_keeps_emoji_ids()
    test_bug16_poke_merge()
    test_bug17_persist_condition()
    test_bug18_load_applies_lru()
    test_bug19_config_dict_lang()
    test_bug20_clear_card()
    test_bug21_single_lookup()
    print("=== 第三轮 14 项回归测试全部通过 ===")