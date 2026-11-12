"""环境摘要渲染：合并他人事件为一条紧凑摘要，分层预算 + 踢旧淘汰。

设计要点：
- 输出格式：用户{QQ}({昵称})，昵称缺失回退纯 QQ。
- 分层预算：先按类型配额渲染（每类型 ≤ N 条），总超限从最旧事件整条删除（不截断字符串）。
- 时间前缀：[群动态·刚刚(≤60s)] / [群动态·过去N分钟] / [群动态·今天] / [群动态·昨天 HH:MM 起]。
- 无 emoji 堆砌、无 Markdown；条目编号 ①②③ 便于模型引用。
- 性能：O(N log N) 排序 + O(N) 渲染（N ≤ 20）。
"""

from __future__ import annotations

import time
from typing import Optional

from .event_normalizer import QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_TITLE, E_CARD, E_ADMIN, E_KICKED_SELF, E_TITLE_SELF, E_EMOJI_LIKE_SELF, E_PROFILE_LIKE_SELF


def _fmt_user(qq: str, name: str) -> str:
    """格式化用户：用户{QQ}({昵称})，昵称缺失回退纯 QQ。"""
    if qq and name:
        return f"用户{qq}({name})"
    elif qq:
        return f"用户{qq}"
    elif name:
        return f"用户({name})"
    else:
        return "用户(未知)"


def _time_prefix(events: list[QQEvent]) -> str:
    """时间前缀：[群动态·刚刚(≤60s)] / [群动态·过去N分钟] / [群动态·今天] / [群动态·昨天 HH:MM 起]。"""
    if not events:
        return ""
    now = time.time()
    oldest = min(ev.ts for ev in events)
    age = now - oldest
    if age <= 60:
        return "[群动态·刚刚]"
    elif age <= 3600:
        mins = int(age // 60)
        return f"[群动态·过去{mins}分钟]"
    elif age <= 86400:
        return "[群动态·今天]"
    else:
        from datetime import datetime
        dt = datetime.fromtimestamp(oldest)
        return f"[群动态·{dt.strftime('%m-%d %H:%M')} 起]"


def render_env_summary(
    events: list[QQEvent],
    *,
    max_per_type: int = 6,
    max_chars: int = 800,
) -> str:
    """渲染环境摘要（分层预算 + 踢旧淘汰）。

    Args:
        events: 事件列表（按时间升序）。
        max_per_type: 每类型最多渲染条数。
        max_chars: 摘要字符上限（超限从最旧事件整条删除，不截断字符串）。

    Returns:
        渲染后的摘要文本（空则返回空串）。
    """
    if not events:
        return ""

    # 拼接候选文本（类型条目用 ①②③ 编号，分隔符用 ；）
    header = _time_prefix(events)

    def _assemble(evts: list[QQEvent]) -> str:
        """按当前事件集合渲染并编号拼接。"""
        by_t: dict[str, list[QQEvent]] = {}
        for ev in evts:
            by_t.setdefault(ev.etype, []).append(ev)
        for et in by_t:
            by_t[et].sort(key=lambda e: e.ts, reverse=True)
        order = sorted(
            by_t.keys(),
            key=lambda et: by_t[et][0].ts if by_t[et] else 0,
            reverse=True,
        )
        parts = []
        for et in order:
            rendered = _render_type(et, by_t[et][:max_per_type])
            if rendered:
                parts.append(rendered)
        if not parts:
            return ""
        circled = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
        numbered = []
        for i, p in enumerate(parts):
            mark = circled[i] if i < len(circled) else f"({i + 1})"
            numbered.append(f"{mark}{p}")
        body = "；".join(numbered)
        return f"{header} {body}" if header else body

    candidate = _assemble(events)

    # 踢旧淘汰：超限从最旧事件整条删除（不是截断字符串）
    while len(candidate) > max_chars and len(events) > 1:
        events = events[1:]
        candidate = _assemble(events)

    return candidate if len(candidate) <= max_chars or len(events) == 1 else candidate


def _render_type(etype: str, events: list[QQEvent]) -> str:
    """渲染单个类型的事件（已排序，新事件在前）。"""
    if etype == E_POKE:
        # 合并：同 actor → 同 target 的 N 次 poke 合并为 ×N
        pairs = {}
        for ev in events:
            key = (ev.actor, ev.target)
            pairs[key] = pairs.get(key, 0) + 1
        parts = []
        for (actor, target), count in pairs.items():
            ev = next((e for e in events if e.actor == actor and e.target == target), None)
            aname = ev.actor_name if ev else ""
            tname = ev.target_name if ev else ""
            part = f"{_fmt_user(actor, aname)} 戳了戳 {_fmt_user(target, tname)}"
            if count > 1:
                part += f"×{count}"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_EMOJI_LIKE:
        # 合并：按 (actor, target_message_author) 归并
        # 注意：target_message_author 需 message_locator 反查（此处 events 已带 target/target_name）
        from .emoji_display import format_emoji_list
        parts = []
        for ev in events:
            emoji_ids = ev.extra.get("emoji_ids", [])
            count = ev.extra.get("count", 1)
            msg_id = ev.extra.get("message_id", "")
            emoji_str = format_emoji_list(emoji_ids, count)
            part = f"{_fmt_user(ev.actor, ev.actor_name)} 给 {_fmt_user(ev.target, ev.target_name)} 的消息贴了 {emoji_str}"
            if msg_id:
                part += f"(msg:{msg_id[:8]}...)"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_BAN:
        # 合并：同 target 的 N 次 ban 合并
        parts = []
        for ev in events:
            duration = ev.extra.get("duration", 0)
            part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 禁言 {duration}秒"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_UNBAN:
        parts = []
        for ev in events:
            part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 解除禁言"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_MEMBER_DECREASE:
        parts = []
        for ev in events:
            sub = ev.extra.get("sub_type", "")
            if sub == "kick":
                action = "被移出群聊"
            elif sub == "leave":
                action = "退群"
            else:
                action = "离开群聊"
            part = f"{_fmt_user(ev.target, ev.target_name)} {action}"
            if ev.actor:
                part += f"(操作者:{_fmt_user(ev.actor, ev.actor_name)})"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_RECALL:
        parts = []
        for ev in events:
            part = f"{_fmt_user(ev.actor, ev.actor_name)} 撤回了一条消息"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_TITLE:
        parts = []
        for ev in events:
            title = ev.extra.get("title", "")
            part = f"{_fmt_user(ev.target, ev.target_name)} 被授予头衔\"{title}\""
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_CARD:
        parts = []
        for ev in events:
            card_new = ev.extra.get("card_new", "")
            card_old = ev.extra.get("card_old", "")
            if card_new and card_old:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片变更: {card_old} → {card_new}"
            elif card_new:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片更新为: {card_new}"
            else:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片更新"
            parts.append(part)
        return "; ".join(parts)

    elif etype == E_ADMIN:
        parts = []
        for ev in events:
            sub = ev.extra.get("sub_type", "")
            action = "被设为管理员" if sub == "set" else "被取消管理员"
            part = f"{_fmt_user(ev.target, ev.target_name)} {action}"
            parts.append(part)
        return "; ".join(parts)

    # 唤醒级事件（E_KICKED_SELF / E_TITLE_SELF / E_EMOJI_LIKE_SELF / E_PROFILE_LIKE_SELF）不在环境摘要渲染
    return ""