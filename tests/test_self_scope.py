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
