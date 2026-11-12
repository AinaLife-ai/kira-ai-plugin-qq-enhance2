"""第十五轮回归：群动态边界（只记"其他用户之间"的事件）。

用户实机反馈：
    别人戳 bot → 群动态里出现
    `用户769690776(周武) 戳了戳 用户3991867505(紫小贱)`
    —— "这个也不应该有，我们动态只记其他用户对其他用户的"。

本文件验证 **每一种事件类型** 在 actor/target 涉及 bot 时都不进群动态，
同时确认唤醒通道没有被误伤（该唤醒的仍然唤醒）。
"""

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from qq_core.event_normalizer import (
    normalize_notice, involves_self, QQEvent,
    E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL,
    E_TITLE, E_CARD, E_ADMIN, E_KICKED_SELF, E_TITLE_SELF,
)

SELF = "3991867505"
GID = 188395693
SID = f"napcat:gm:{GID}"


def _norm(raw, self_id=SELF):
    return normalize_notice(raw, sid=SID, self_id=self_id, group_id=str(GID))


def _poke(actor, target):
    return {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "time": int(time.time()), "self_id": SELF,
        "user_id": actor, "target_id": target, "group_id": GID,
        "raw_info": [{}, {}, {"txt": "戳了戳"}, {}, {"txt": ""}],
    }


def _ban(target, operator, sub="ban", duration=60):
    return {
        "post_type": "notice", "notice_type": "group_ban", "sub_type": sub,
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "operator_id": operator, "duration": duration, "group_id": GID,
    }


def _decrease(target, sub, operator=None):
    d = {
        "post_type": "notice", "notice_type": "group_decrease", "sub_type": sub,
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "group_id": GID,
    }
    if operator:
        d["operator_id"] = operator
    return d


def _recall(target, operator, mid=123456):
    return {
        "post_type": "notice", "notice_type": "group_recall",
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "operator_id": operator, "message_id": mid, "group_id": GID,
    }


def _title(target, title="活跃分子"):
    return {
        "post_type": "notice", "notice_type": "notify", "sub_type": "title",
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "title": title, "group_id": GID,
    }


def _card(target):
    return {
        "post_type": "notice", "notice_type": "group_card",
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "card_new": "新名片", "card_old": "旧名片", "group_id": GID,
    }


def _admin(target, sub="set"):
    return {
        "post_type": "notice", "notice_type": "group_admin", "sub_type": sub,
        "time": int(time.time()), "self_id": SELF, "user_id": target,
        "group_id": GID,
    }


def _emoji(actor, mid=998877):
    return {
        "post_type": "notice", "notice_type": "group_msg_emoji_like",
        "time": int(time.time()), "self_id": SELF, "user_id": actor,
        "message_id": mid, "group_id": GID,
        "likes": [{"emoji_id": "4", "count": 1}], "is_add": True,
    }


# ==================== 1. 归一化层：涉及 bot 的直接不产生事件 ====================

def test_poke_self_is_dropped():
    """别人戳 bot → 不产生事件（框架自己会发 [Poke ...戳了戳你]）。"""
    assert _norm(_poke(1001, SELF)) is None


def test_poke_by_self_is_dropped():
    """bot 戳别人 → 不产生事件。"""
    assert _norm(_poke(SELF, 1001)) is None


def test_poke_third_party_kept():
    """其他用户 → 其他用户：保留。"""
    ev = _norm(_poke(1001, 1002))
    assert ev is not None and ev.etype == E_POKE
    assert (ev.actor, ev.target) == ("1001", "1002")


def test_ban_self_target_dropped():
    """bot 被禁言 → QQ增强1 负责，我们不产生事件。"""
    assert _norm(_ban(SELF, 8888)) is None


def test_ban_by_self_dropped():
    """bot 禁言别人 → 自己的动作，不进群动态。"""
    assert _norm(_ban(1001, SELF)) is None


def test_ban_third_party_kept():
    ev = _norm(_ban(1001, 8888))
    assert ev is not None and ev.etype == E_BAN


def test_decrease_self_kicked_is_wake_event():
    """bot 被踢 → 唤醒级事件（不是群动态）。"""
    for sub in ("kick_me", "kick"):
        ev = _norm(_decrease(SELF, sub, operator=8888))
        assert ev is not None and ev.etype == E_KICKED_SELF, sub


def test_decrease_self_left_dropped():
    """bot 自己退群 → 自己的动作，不记录。"""
    assert _norm(_decrease(SELF, "leave")) is None


def test_decrease_third_party_kept():
    ev = _norm(_decrease(1001, "leave"))
    assert ev is not None and ev.etype == E_MEMBER_DECREASE


def test_recall_by_self_dropped():
    """bot 撤回消息 → 不记录（assistant 消息本来也不会被标注）。"""
    assert _norm(_recall(1001, SELF)) is None


def test_recall_third_party_kept():
    ev = _norm(_recall(1001, 8888))
    assert ev is not None and ev.etype == E_RECALL


def test_title_to_self_is_wake_event():
    """bot 的头衔被改 → E_TITLE_SELF（此前该常量永远不产生，wake_title 是死开关）。"""
    ev = _norm(_title(SELF))
    assert ev is not None and ev.etype == E_TITLE_SELF
    assert ev.extra["title"] == "活跃分子"


def test_title_third_party_kept():
    ev = _norm(_title(1001))
    assert ev is not None and ev.etype == E_TITLE


def test_card_self_dropped():
    assert _norm(_card(SELF)) is None


def test_card_third_party_kept():
    ev = _norm(_card(1001))
    assert ev is not None and ev.etype == E_CARD


def test_admin_self_dropped():
    for sub in ("set", "unset"):
        assert _norm(_admin(SELF, sub)) is None, sub


def test_admin_third_party_kept():
    ev = _norm(_admin(1001))
    assert ev is not None and ev.etype == E_ADMIN


def test_emoji_like_by_self_dropped():
    """bot 给别人贴表情 → 自己的动作，不记录。"""
    assert _norm(_emoji(SELF)) is None


def test_emoji_like_third_party_kept():
    ev = _norm(_emoji(1001))
    assert ev is not None and ev.etype == E_EMOJI_LIKE


def test_involves_self_helper():
    ev = QQEvent(eid="x", etype=E_POKE, sid=SID, ts=0, actor="1001", target=SELF)
    assert involves_self(ev, SELF) is True
    assert involves_self(ev, "9999") is False
    assert involves_self(ev, "") is False  # 未知 self_id → 保守放行


# ==================== 3. 环境摘要：时间 / 新鲜期 / 禁言状态机 ====================

def _ev(eid, etype, ts, **kw):
    return QQEvent(eid=eid, etype=etype, sid=SID, ts=ts, **kw)


def test_env_fresh_seconds_and_slow_seconds():
    """env_fresh_seconds / env_slow_seconds：戳一戳 5 分钟、名片 15 分钟。"""
    from qq_core.env_renderer import filter_env_events

    now = 1_000_000
    events = [
        _ev("p1", E_POKE, now - 60, actor="1", actor_name="A", target="2", target_name="B"),
        _ev("p2", E_POKE, now - 400, actor="3", actor_name="C", target="4", target_name="D"),
        _ev("c1", E_CARD, now - 400, target="5", target_name="E",
            extra={"card_new": "新", "card_old": "旧"}),
    ]
    kept = filter_env_events(events, now=now, fresh_seconds=300, slow_seconds=900)
    ids = [e.eid for e in kept]
    assert "p1" in ids, "5 分钟内的戳一戳应保留"
    assert "p2" not in ids, "超过 fresh 的戳一戳应消失"
    assert "c1" in ids, "名片走 slow，15 分钟内应保留"


def test_ban_state_machine():
    """禁言：进行中一直保留；到期后只补报一次（env_show_time 场景下显示"已于"）。"""
    from qq_core.env_renderer import filter_env_events, render_env_summary

    now = 1_000_000
    ban = _ev("b1", E_BAN, now - 700, actor="8888", actor_name="管理",
              target="1005", target_name="小A", extra={"duration": 600})
    # 600 秒禁言，已经过了 700 秒 → 到期
    announced = {}
    kept = filter_env_events([ban], now=now, fresh_seconds=300, slow_seconds=900,
                             expiry_announced=announced)
    assert kept, "到期但没补报过 → 应保留一轮"
    text = render_env_summary(kept, now=now, show_time=True, expiry_announced=announced)
    assert "到期" in text, text
    assert announced, "应记录已补报，下一轮不再出现"
    kept2 = filter_env_events([ban], now=now + 1, fresh_seconds=300, slow_seconds=900,
                              expiry_announced=announced)
    assert kept2 == [], "补报过以后应该消失"

    # 仍在禁言中：即使超过 slow 也要保留
    active = _ev("b2", E_BAN, now - 5000, actor="8888", actor_name="管理",
                 target="1006", target_name="小B", extra={"duration": 86400})
    kept3 = filter_env_events([active], now=now, fresh_seconds=300, slow_seconds=900)
    assert kept3, "还在禁言中的事件必须保留（状态，不是时长）"


def test_env_show_time_toggle():
    """env_show_time：开=每条带时间，关=不带。"""
    import re
    from qq_core.env_renderer import render_env_summary

    now = 1_000_000
    ev = _ev("p1", E_POKE, now - 10, actor="769690776", actor_name="周武",
             target="3991867505", target_name="紫小贱")
    on = render_env_summary([ev], now=now, show_time=True)
    off = render_env_summary([ev], now=now, show_time=False)
    assert "截至" in on and "用户769690776(周武)" in on, on
    assert re.search(r"·\s\d{2}:\d{2}\s", on), on
    assert "用户769690776(周武)" in off, off
    # 关掉时间后不应出现 "· HH:MM "
    assert not re.search(r"·\s\d{2}:\d{2}\s", off), off


def test_renderer_never_renders_member_decrease():
    """退群/被踢不再进摘要（改走 notice）。"""
    from qq_core.env_renderer import render_env_summary

    now = 1_000_000
    ev = QQEvent(eid="m1", etype=E_MEMBER_DECREASE, sid=SID, ts=now - 10,
                 target="1009", target_name="退群人", extra={"sub_type": "leave"})
    text = render_env_summary([ev], now=now)
    assert text == "", text


def test_resolve_cache_seconds():
    """resolve_cache_seconds：默认 10 秒；0 = 不缓存（仍保留并发合并）。"""
    import asyncio
    from qq_core.name_resolver import NameResolver

    class _Bot:
        def __init__(self):
            self.calls = 0

        async def send_action(self, action, params, timeout=10.0):
            self.calls += 1
            return {"status": "ok", "data": {"nickname": "张三", "card": "老张", "role": "member"}}

    class _A:
        def __init__(self):
            self.bot = _Bot()

    async def run(ttl):
        a = _A()
        r = NameResolver(ttl=ttl)
        await r.resolve(a, "napcat", "188395693", "1001")
        await r.resolve(a, "napcat", "188395693", "1001")
        return a.bot.calls

    assert asyncio.run(run(10)) == 1, "10 秒内第二次应命中缓存"
    assert asyncio.run(run(0)) == 2, "0 = 不缓存，每次都查"


def test_prune_ban_expiry_bounded():
    """禁言补报集合不能无限增长（超过 512 条要裁，且丢弃 1 小时前的记录）。"""
    import time as _t
    from qq_core.env_renderer import prune_expiry_announced

    now = _t.time()
    announced = {f"e{i}": now - (i % 7200) for i in range(2000)}
    kept = prune_expiry_announced(announced, now)
    assert len(kept) <= 512, len(kept)
    assert all(v >= now - 3600 for v in kept.values())
    # 小规模不动
    small = {"a": now}
    assert prune_expiry_announced(small, now) is small


def test_profile_like_actor_fallback():
    """点赞者字段：优先 operator_id；部分实现放 user_id → 兜底。"""
    from qq_core.event_normalizer import normalize_notice, E_PROFILE_LIKE_SELF

    # NapCat / SnowLuma
    ev = normalize_notice(
        {"notice_type": "notify", "sub_type": "profile_like",
         "operator_id": 769690776, "operator_nick": "周武", "times": 10},
        sid="s", self_id="3991867505")
    assert ev.etype == E_PROFILE_LIKE_SELF and ev.actor == "769690776"

    # 变体：只有 user_id
    ev2 = normalize_notice(
        {"notice_type": "notify", "sub_type": "profile_like",
         "user_id": 1001, "times": 2},
        sid="s", self_id="3991867505")
    assert ev2.actor == "1001", ev2.actor

    # 两者都有 → operator_id 优先
    ev3 = normalize_notice(
        {"notice_type": "notify", "sub_type": "profile_like",
         "user_id": 111, "operator_id": 222, "times": 1},
        sid="s", self_id="3991867505")
    assert ev3.actor == "222", ev3.actor
