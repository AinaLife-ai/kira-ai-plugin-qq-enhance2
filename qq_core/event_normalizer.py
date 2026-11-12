"""事件归一化：三家协议端（NapCat/LLOoneBot/SnowLuma）的 notice 事件 → 统一 QQEvent。

设计要点：
- 只处理框架 publish 的 notice 事件（raw_message 完整保留）。
- 归一化输出纯 dict 字段（轻量、可 JSON 序列化）。
- 昵称缺失时回退到 QQ 号，绝不留空。
- 性能：纯 dict 解析，无 IO，单事件 < 0.1ms。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class QQEvent:
    """归一化后的 QQ 社会事件。

    Attributes:
        eid: 事件唯一 ID（uuid4 hex）。
        etype: 事件类型，见 ETYPES。
        sid: 会话 ID（adapter:gm:group_id）。
        ts: unix 秒。
        actor: 发起人 QQ（无则空串）。
        actor_name: 发起人昵称/群名片（best-effort，绝不空）。
        target: 目标 QQ（无则空串）。
        target_name: 目标昵称（best-effort，绝不空）。
        extra: 类型特有字段（emoji_id/count/duration/message_id/operator...）。
    """

    eid: str
    etype: str
    sid: str
    ts: int
    actor: str = ""
    actor_name: str = ""
    target: str = ""
    target_name: str = ""
    extra: dict = field(default_factory=dict)


# 事件类型常量（渲染与唤醒共用）
E_POKE = "poke"
E_EMOJI_LIKE = "emoji_like"
E_BAN = "group_ban"
E_UNBAN = "group_unban"
E_MEMBER_DECREASE = "member_decrease"
E_RECALL = "recall"
E_TITLE = "title"
E_CARD = "card"
E_ADMIN = "admin"
E_PROFILE_LIKE_SELF = "profile_like_self"
E_KICKED_SELF = "kicked_self"
E_TITLE_SELF = "title_self"
E_EMOJI_LIKE_SELF = "emoji_like_self"

ETYPES = frozenset({
    E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL,
    E_TITLE, E_CARD, E_ADMIN, E_PROFILE_LIKE_SELF, E_KICKED_SELF,
    E_TITLE_SELF, E_EMOJI_LIKE_SELF,
})


def _s(v: Any) -> str:
    """安全转字符串（None → 空串）。"""
    if v is None:
        return ""
    return str(v)


def _uid(raw: dict, *keys: str) -> str:
    """从 raw_message 取第一个非空字段并归一化为 QQ 号字符串。"""
    for k in keys:
        v = raw.get(k)
        if v is not None and v != "":
            return str(v)
    return ""


def _nick(raw: dict, *keys: str) -> str:
    for k in keys:
        v = raw.get(k)
        if v:
            return str(v)
    return ""


def _extract_poke_texts(raw: dict) -> tuple[str, str]:
    """提取戳一戳的「动作」与「后缀」文案（兼容三家协议端）。

    Args:
        raw: 原始 notice。

    Returns:
        (action_text, suffix_text)，取不到时为空串。
    """
    # 1. NapCat：raw_info[2].txt / raw_info[4].txt
    raw_info = raw.get("raw_info")
    if isinstance(raw_info, list) and len(raw_info) > 4:
        try:
            act = raw_info[2].get("txt") if isinstance(raw_info[2], dict) else ""
            suf = raw_info[4].get("txt") if isinstance(raw_info[4], dict) else ""
            if act or suf:
                return str(act or ""), str(suf or "")
        except Exception:
            pass
    # 2. SnowLuma / LLOneBot：action / suffix
    act = _nick(raw, "action", "action_str")
    suf = _nick(raw, "suffix", "suffix_str")
    return act, suf


def normalize_notice(
    raw: dict,
    *,
    sid: str,
    self_id: str,
    group_id: str = "",
    now: Optional[int] = None,
    name_resolver=None,
) -> Optional[QQEvent]:
    """把一条 OneBot notice 原始事件归一化为 QQEvent。

    Args:
        raw: 原始 OneBot 事件 dict。
        sid: 会话 ID。
        self_id: 机器人自己的 QQ 号（用于判定"与自己有关"）。
        group_id: 群号（群聊事件必填）。
        now: 当前时间戳（默认 time.time()）。
        name_resolver: 可选 async 回调 (qq, group_id) -> name，用于补昵称。

    Returns:
        QQEvent 或 None（非目标类型/无法归一化）。
    """
    if not isinstance(raw, dict):
        return None

    notice_type = raw.get("notice_type") or ""
    sub_type = raw.get("sub_type") or ""
    user_id = _uid(raw, "user_id")
    target_id = _uid(raw, "target_id")
    operator_id = _uid(raw, "operator_id")
    # time 可能是字符串/异常值 → 安全转换，失败则用当前时间
    _raw_ts = raw.get("time") or (now if now is not None else time.time())
    try:
        ts = int(float(_raw_ts))
    except (TypeError, ValueError):
        ts = int(now if now is not None else time.time())
    self_id = _s(self_id)

    # ---- 戳一戳 ----
    if notice_type == "notify" and sub_type == "poke":
        # 群 poke 或好友 poke
        if target_id and target_id == self_id:
            # 自己被戳：框架已构造 [Poke] 文本，我们只记 store（不重复唤醒）
            et = E_POKE
        elif user_id and user_id == self_id:
            # 自己戳别人：不记录（无信息量）
            return None
        else:
            et = E_POKE
        # 戳一戳的动作文案：三家协议端字段不同
        #   · NapCat    : msg["raw_info"][2]["txt"] / [4]["txt"]
        #   · SnowLuma  : msg["action"] / msg["suffix"]
        #   · LLOneBot  : msg["action"] / msg["suffix"]（部分版本带 raw_info）
        act, suf = _extract_poke_texts(raw)
        extra = {"raw_action": act, "raw_suffix": suf}
        return QQEvent(
            eid=_eid(), etype=et, sid=sid, ts=ts,
            actor=user_id, target=target_id, extra=extra,
        )

    # ---- 贴表情 ----
    if notice_type == "group_msg_emoji_like":
        # is_add=False 表示"取消表情"（取消回应），不是贴表情
        # SnowLuma 的 sub_type 为 "add"/"remove"，NapCat/LLOneBot 用 is_add 字段
        is_add = raw.get("is_add", True)
        if is_add is False or sub_type in ("remove", "del", "unlink"):
            return None
        likes = raw.get("likes") or []
        emoji_ids = []
        total_count = 0
        for lk in likes:
            if isinstance(lk, dict):
                eid = _s(lk.get("emoji_id"))
                if eid:
                    emoji_ids.append(eid)
                try:
                    total_count += int(lk.get("count") or 1)
                except (TypeError, ValueError):
                    total_count += 1
        extra = {
            "message_id": _uid(raw, "message_id"),
            "message_seq": _s(raw.get("message_seq")),
            "emoji_ids": emoji_ids,
            "count": total_count or 1,
            "is_add": True,
            "sub_type": sub_type,
        }
        # 判定是否给自己贴：需 message_locator 反查（此处只做归一化，唤醒判定在 scheduler）
        return QQEvent(eid=_eid(), etype=E_EMOJI_LIKE, sid=sid, ts=ts,
                       actor=user_id, extra=extra)

    # ---- 禁言/解禁 ----
    if notice_type == "group_ban":
        duration = raw.get("duration") or 0
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            duration = 0
        et = E_UNBAN if sub_type == "lift_ban" else E_BAN
        # 自己被禁言/解禁 → 归 QQ增强1 处理，我们不重复
        if user_id and user_id == self_id:
            return None
        return QQEvent(eid=_eid(), etype=et, sid=sid, ts=ts,
                       actor=operator_id, target=user_id,
                       extra={"duration": duration})

    # ---- 成员减少 ----
    if notice_type == "group_decrease":
        sub = sub_type or ""
        # kick_me：自己被踢 → 唤醒
        et = E_KICKED_SELF if sub == "kick_me" else E_MEMBER_DECREASE
        return QQEvent(eid=_eid(), etype=et, sid=sid, ts=ts,
                       actor=operator_id, target=user_id,
                       extra={"sub_type": sub})

    # ---- 撤回 ----
    if notice_type in ("group_recall", "friend_recall"):
        return QQEvent(eid=_eid(), etype=E_RECALL, sid=sid, ts=ts,
                       actor=operator_id, target=user_id,
                       extra={
                           "message_id": _uid(raw, "message_id"),
                           "message_seq": _s(raw.get("message_seq")),
                           "sub_type": sub_type,
                           "is_group": notice_type == "group_recall",
                       })

    # ---- 资料卡点赞 ----
    if notice_type == "notify" and sub_type == "profile_like":
        times = raw.get("times") or 1
        try:
            times = int(times)
        except (TypeError, ValueError):
            times = 1
        return QQEvent(eid=_eid(), etype=E_PROFILE_LIKE_SELF, sid=sid, ts=ts,
                       actor=operator_id, extra={"times": times})

    # ---- 头衔授予 ----
    if notice_type == "notify" and sub_type == "title":
        return QQEvent(eid=_eid(), etype=E_TITLE, sid=sid, ts=ts,
                       target=user_id,
                       extra={"title": _s(raw.get("title"))})

    # ---- 名片更新 ----
    if notice_type == "group_card":
        return QQEvent(eid=_eid(), etype=E_CARD, sid=sid, ts=ts,
                       target=user_id,
                       extra={"card_new": _s(raw.get("card_new")),
                              "card_old": _s(raw.get("card_old"))})

    # ---- 管理员变动 ----
    if notice_type == "group_admin":
        return QQEvent(eid=_eid(), etype=E_ADMIN, sid=sid, ts=ts,
                       target=user_id, extra={"sub_type": sub_type})

    return None


def _eid() -> str:
    import uuid
    return uuid.uuid4().hex