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

from .fmt import fmt_user as _fmt_user
from .event_normalizer import QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_MEMBER_DECREASE, E_RECALL, E_TITLE, E_CARD, E_ADMIN, E_KICKED_SELF, E_TITLE_SELF, E_EMOJI_LIKE_SELF, E_PROFILE_LIKE_SELF




def _time_prefix(events: list[QQEvent]) -> str:
    """时间前缀（按日历日期判断，不是按 age 时长）。

    格式：
      [群动态·刚刚]           最新事件 ≤ 60 秒
      [群动态·过去N分钟]      最新事件 ≤ 60 分钟
      [群动态·今天 HH:MM 起]  最新事件在今天
      [群动态·昨天 HH:MM 起]  最新事件在昨天
      [群动态·MM-DD HH:MM 起] 更早

    注意：用"最新事件"而非"最旧事件"判断，因为摘要描述的是"刚刚发生了什么"，
    用最新事件的时间更符合直觉。
    """
    if not events:
        return ""
    try:
        from datetime import datetime, timedelta
        now = time.time()
        newest = max(ev.ts for ev in events)
        age = now - newest

        if age <= 60:
            return "[群动态·刚刚]"
        if age <= 3600:
            return f"[群动态·过去{int(age // 60)}分钟]"

        # 按日历日期判断（不是按 age 时长，避免跨天误判）
        dt = datetime.fromtimestamp(newest)
        today = datetime.fromtimestamp(now).date()
        target_date = dt.date()
        delta_days = (today - target_date).days

        hm = dt.strftime("%H:%M")
        if delta_days == 0:
            return f"[群动态·今天 {hm} 起]"
        elif delta_days == 1:
            return f"[群动态·昨天 {hm} 起]"
        else:
            return f"[群动态·{dt.strftime('%m-%d %H:%M')} 起]"
    except Exception:
        return "[群动态]"


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

    header = _time_prefix(events)

    def _assemble(evts: list[QQEvent]) -> str:
        """按当前事件集合渲染（无编号，用；分隔以省 token）。

        注意：max_per_type 截断对"可合并类型"（poke/emoji_like）无效——
        先合并再截断会丢事件（8 条戳一戳取 6 条合并 = 漏 2 条），
        所以这些类型传入全部事件，由渲染函数内部决定显示粒度。
        """
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
        # 合并型类型：渲染函数内部会归并，传全量避免丢事件
        merge_types = {E_POKE, E_EMOJI_LIKE}
        parts = []
        for et in order:
            subset = by_t[et] if et in merge_types else by_t[et][:max_per_type]
            rendered = _render_type(et, subset, all_events=evts)
            if rendered:
                parts.append(rendered)
        if not parts:
            return ""
        body = "；".join(parts)
        return f"{header} {body}" if header else body

    candidate = _assemble(events)

    # 踢旧淘汰：超限从最旧事件整条删除（不是截断字符串）
    # 至少保留 1 条（即使超限也返回，避免空摘要）
    while len(candidate) > max_chars and len(events) > 1:
        events = events[1:]
        candidate = _assemble(events)

    return candidate


def _render_type(etype: str, events: list[QQEvent], all_events: list[QQEvent] | None = None) -> str:
    """渲染单个类型的事件（已排序，新事件在前）。

    Args:
        etype: 事件类型。
        events: 该类型的事件（已排序、已截断到 max_per_type）。
        all_events: 全量事件（用于跨类型配对，如 ban ↔ unban）。
    """
    all_events = all_events if all_events is not None else events
    if etype == E_POKE:
        # 合并策略（省 token）：
        # 1. 同 actor → 同 target 的 N 次 → ×N
        # 2. 多 actor → 同 target → 合并为 "A、B、C 戳了戳 T"
        pair_count: dict[tuple[str, str], int] = {}
        actor_names: dict[str, str] = {}
        target_names: dict[str, str] = {}
        for ev in events:
            key = (ev.actor, ev.target)
            pair_count[key] = pair_count.get(key, 0) + 1
            if ev.actor:
                actor_names[ev.actor] = ev.actor_name
            if ev.target:
                target_names[ev.target] = ev.target_name

        # 按 target 归并
        by_target: dict[str, list[tuple[str, int]]] = {}
        for (actor, target), cnt in pair_count.items():
            by_target.setdefault(target, []).append((actor, cnt))

        parts = []
        for target, lst in by_target.items():
            tname = target_names.get(target, "")
            total = sum(c for _, c in lst)
            if len(lst) == 1:
                actor, cnt = lst[0]
                part = f"{_fmt_user(actor, actor_names.get(actor, ''))} 戳了戳 {_fmt_user(target, tname)}"
                if cnt > 1:
                    part += f"×{cnt}"
            else:
                # 多人戳同一人：列出前 3 个 + "等N人"
                names = [
                    _fmt_user(a, actor_names.get(a, ""))
                    for a, _ in lst[:3]
                ]
                more = f"等{len(lst)}人" if len(lst) > 3 else ""
                part = f"{'、'.join(names)}{more} 戳了戳 {_fmt_user(target, tname)}"
                if total > len(lst):
                    part += f"（共{total}次）"
            parts.append(part)
        return "；".join(parts)

    elif etype == E_EMOJI_LIKE:
        # 合并：按 (actor, target_message_author) 归并
        # 注意：target_message_author 需 message_locator 反查（此处 events 已带 target/target_name）
        from .emoji_display import format_emoji_list
        groups: dict[tuple[str, str], dict] = {}
        for ev in events:
            key = (ev.actor, ev.target)
            g = groups.get(key)
            if g is None:
                groups[key] = {
                    "actor_name": ev.actor_name,
                    "target_name": ev.target_name,
                    "emoji_ids": list(ev.extra.get("emoji_ids", [])),
                    "count": ev.extra.get("count", 1),
                    "message_ids": [ev.extra.get("message_id", "")] if ev.extra.get("message_id") else [],
                }
            else:
                # 合并表情 ID（去重保序）+ 累加次数
                for e in ev.extra.get("emoji_ids", []):
                    if e and e not in g["emoji_ids"]:
                        g["emoji_ids"].append(e)
                g["count"] += ev.extra.get("count", 1)
                mid = ev.extra.get("message_id", "")
                if mid and mid not in g["message_ids"] and len(g["message_ids"]) < 3:
                    g["message_ids"].append(mid)

        parts = []
        for (actor, target), g in groups.items():
            emoji_str = format_emoji_list(g["emoji_ids"], g["count"])
            part = (
                f"{_fmt_user(actor, g['actor_name'])} 给 "
                f"{_fmt_user(target, g['target_name'])} 的消息贴了 {emoji_str}"
            )
            # 完整 message_id（bot 回贴必需，不截断）
            if g["message_ids"]:
                ids = ",".join(g["message_ids"])
                part += f"(消息 {ids})"
            parts.append(part)
        return "；".join(parts)

    elif etype == E_BAN:
        # 禁言渲染：与同 target 的解除禁言事件配对
        # - 已解除 → 显示时长 + (已解除)
        # - 未解除且未到期 → 显示 (仍禁言中，剩约N分钟)
        # - 已自然到期 → 显示 (禁言时长已过)
        unban_by_target: dict[str, list[QQEvent]] = {}
        for ev in all_events:
            if ev.etype == E_UNBAN and ev.target:
                unban_by_target.setdefault(ev.target, []).append(ev)

        parts = []
        now = time.time()
        for ev in events:
            duration = ev.extra.get("duration", 0) or 0
            part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 禁言"
            if duration > 0:
                part += f" {duration}秒"
            # 配对解除事件（同 target，时间晚于本次 ban）
            unban = next(
                (u for u in unban_by_target.get(ev.target, []) if u.ts >= ev.ts),
                None,
            )
            if unban:
                part += "(已解除)"
            elif duration > 0:
                remain = int(duration - (now - ev.ts))
                if remain > 0:
                    part += f"(仍禁言中，剩约{remain // 60 + 1}分钟)"
                else:
                    part += "(禁言时长已过)"
            parts.append(part)
        return "；".join(parts)

    elif etype == E_UNBAN:
        # 解除禁言：若已作为某个 ban 的配对显示，这里跳过（避免重复）
        ban_targets = {
            ev.target for ev in all_events if ev.etype == E_BAN and ev.target
        }
        parts = []
        for ev in events:
            if ev.target in ban_targets:
                continue  # 已由 E_BAN 分支配对显示
            part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 解除禁言"
            parts.append(part)
        return "；".join(parts)

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
        return "；".join(parts)

    elif etype == E_RECALL:
        # 合并：同一操作者的多次撤回合并为 "X 撤回了 N 条消息"
        by_actor: dict[str, list[QQEvent]] = {}
        for ev in events:
            key = ev.actor or ev.target or "unknown"
            by_actor.setdefault(key, []).append(ev)

        parts = []
        for key, lst in by_actor.items():
            ev0 = lst[0]
            who = _fmt_user(ev0.actor, ev0.actor_name)
            # 区分操作者身份：撤回自己 vs 撤回他人
            if ev0.actor and ev0.target and ev0.actor != ev0.target:
                detail = "撤回了他人消息"
            else:
                detail = "撤回了一条消息"
            if len(lst) > 1:
                parts.append(f"{who} {detail}×{len(lst)}")
            else:
                parts.append(f"{who} {detail}")
        return "；".join(parts)

    elif etype == E_TITLE:
        parts = []
        for ev in events:
            title = ev.extra.get("title", "")
            part = f"{_fmt_user(ev.target, ev.target_name)} 被授予头衔\"{title}\""
            parts.append(part)
        return "；".join(parts)

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
        return "；".join(parts)

    elif etype == E_ADMIN:
        parts = []
        for ev in events:
            sub = ev.extra.get("sub_type", "")
            action = "被设为管理员" if sub == "set" else "被取消管理员"
            part = f"{_fmt_user(ev.target, ev.target_name)} {action}"
            parts.append(part)
        return "；".join(parts)

    # 唤醒级事件（E_KICKED_SELF / E_TITLE_SELF / E_EMOJI_LIKE_SELF / E_PROFILE_LIKE_SELF）不在环境摘要渲染
    return ""