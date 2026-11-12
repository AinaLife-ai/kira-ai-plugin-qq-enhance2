"""环境摘要渲染：合并他人事件为一条紧凑摘要，分层预算 + 踢旧淘汰。

设计要点（v1.1.2）：
- **每条事件带绝对时间**：`HH:MM` / `昨天 HH:MM` / `MM-DD HH:MM`（块头写"截至 HH:MM"）。
- **分类型新鲜期**：戳一戳/贴表情短（默认 5 分钟），名片/管理员/头衔/撤回落（默认 15 分钟）。
- **禁言是状态**：进行中显示"剩约 N 分钟"；到期后只再出现**一轮**（"已于 HH:MM 到期"），之后消失。
- 退群/被踢**不进摘要**（改走 notice 唤醒，对齐 QQ增强1 的入群感知）。
- 输出格式：`· 01:31 用户{QQ}({昵称}) 戳了戳 用户{QQ}({昵称})`，昵称缺失回退纯 QQ。
- 输出按时间倒序（时间线），超预算从最旧事件整条删除（不截断字符串）。
- 性能：O(N log N) 排序 + O(N) 渲染（N ≤ 20）。
"""

from __future__ import annotations

import time
from typing import Optional

from .fmt import fmt_user as _fmt_user
from .event_normalizer import (
    QQEvent, E_POKE, E_EMOJI_LIKE, E_BAN, E_UNBAN, E_RECALL,
    E_TITLE, E_CARD, E_ADMIN,
)

# 短新鲜期：瞬时互动，过了就没意义（默认 5 分钟）
FRESH_TYPES = frozenset({E_POKE, E_EMOJI_LIKE})
# 长新鲜期：低频状态/结构变化（默认 15 分钟）
SLOW_TYPES = frozenset({E_CARD, E_ADMIN, E_TITLE, E_RECALL})

# 禁言到期后，最多再"补报"这么久（防止重启后把很久以前的禁言翻出来）
BAN_EXPIRY_GRACE_SECONDS = 3600


def fmt_clock(ts: float, now: Optional[float] = None) -> str:
    """把时间戳格式化成绝对时间（当天 HH:MM / 昨天 HH:MM / MM-DD HH:MM）。

    Args:
        ts: 事件时间戳。
        now: 当前时间（默认 time.time()）。

    Returns:
        格式化后的时间字符串。
    """
    from datetime import datetime

    now = time.time() if now is None else now
    try:
        dt = datetime.fromtimestamp(ts)
        today = datetime.fromtimestamp(now).date()
        delta_days = (today - dt.date()).days
        if delta_days == 0:
            return dt.strftime("%H:%M")
        if delta_days == 1:
            return "昨天 " + dt.strftime("%H:%M")
        return dt.strftime("%m-%d %H:%M")
    except Exception:
        return ""


def env_header(now: Optional[float] = None) -> str:
    """摘要块头：`[群动态·截至 HH:MM]`。"""
    now = time.time() if now is None else now
    t = fmt_clock(now, now)
    return f"[群动态·截至 {t}]" if t else "[群动态]"


def event_ttl(etype: str, fresh_seconds: float, slow_seconds: float) -> float:
    """按类型取新鲜期（秒）。"""
    if etype in FRESH_TYPES:
        return fresh_seconds
    return slow_seconds


def _ban_duration(ev: QQEvent) -> int:
    """安全读取禁言时长（落盘/外部构造可能是字符串）。"""
    try:
        return int(float(ev.extra.get("duration", 0) or 0))
    except (TypeError, ValueError):
        return 0


def prune_expiry_announced(announced: dict, now: float, *,
                           limit: int = 512, max_age: float = 3600) -> dict:
    """裁剪"禁言到期已补报"集合（按 eid 记录，只增不减会慢慢涨）。

    Args:
        announced: {eid: 首次补报时间戳}。
        now: 当前时间戳。
        limit: 超过多少条才动手。
        max_age: 丢弃多少秒之前的记录。

    Returns:
        裁剪后的新 dict（不修改入参）。
    """
    if len(announced) <= limit:
        return announced
    cutoff = now - max_age
    kept = {k: v for k, v in announced.items() if v >= cutoff}
    if len(kept) > limit:
        kept = dict(sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:limit])
    return kept


def filter_env_events(
    events: list[QQEvent],
    *,
    now: float,
    fresh_seconds: float = 300,
    slow_seconds: float = 900,
    expiry_announced: Optional[dict] = None,
) -> list[QQEvent]:
    """按"分类型新鲜期 + 禁言状态机"过滤要进摘要的事件。

    Args:
        events: 候选事件（时间升序）。
        now: 当前时间戳。
        fresh_seconds: 瞬时事件新鲜期（戳/贴表情）。
        slow_seconds: 低频事件新鲜期（名片/管理员/头衔/撤回）。
        expiry_announced: 已"补报到期"的禁言 eid 集合（dict eid→ts）。

    Returns:
        过滤后的事件（保持原顺序）。
    """
    # 第一遍：禁言（状态驱动）
    keep_ban: dict[str, bool] = {}
    for ev in events:
        if ev.etype != E_BAN:
            continue
        duration = _ban_duration(ev)
        if duration <= 0:
            keep_ban[ev.eid] = (now - ev.ts) <= slow_seconds
            continue
        expire_ts = ev.ts + duration
        if now < expire_ts:
            keep_ban[ev.eid] = True          # 仍在禁言中
            continue
        # 已到期：没补报过、且在补报宽限期内 → 保留（渲染时会标"已于…到期"）
        announced = bool(expiry_announced) and ev.eid in expiry_announced
        keep_ban[ev.eid] = (not announced) and (now - expire_ts) <= BAN_EXPIRY_GRACE_SECONDS

    # 仍在渲染的禁言，其 target 用于"解禁配对"（否则解禁过期后，禁言会误显示为仍在进行）
    ban_targets_kept = {ev.target for ev in events
                        if ev.etype == E_BAN and keep_ban.get(ev.eid) and ev.target}

    out = []
    for ev in events:
        if ev.etype == E_BAN:
            if keep_ban.get(ev.eid):
                out.append(ev)
            continue
        if ev.etype == E_UNBAN:
            # 与仍在显示的禁言配对 → 无论多久都要保留（否则禁言状态会错）
            if ev.target and ev.target in ban_targets_kept:
                out.append(ev)
            elif now - ev.ts <= slow_seconds:
                out.append(ev)
            continue

        if now - ev.ts <= event_ttl(ev.etype, fresh_seconds, slow_seconds):
            out.append(ev)
    return out


def render_env_summary(
    events: list[QQEvent],
    *,
    max_per_type: int = 6,
    max_chars: int = 800,
    poke_action_text: bool = False,
    show_time: bool = True,
    now: Optional[float] = None,
    expiry_announced: Optional[dict] = None,
) -> str:
    """渲染环境摘要（分层预算 + 踢旧淘汰）。

    Args:
        events: 事件列表（按时间升序，调用方已做新鲜期过滤）。
        max_per_type: 每类型最多渲染条数（合并型类型不受限）。
        max_chars: 摘要字符上限（超限从最旧事件整条删除，不截断字符串）。
        poke_action_text: 是否用协议端真实动作文案（拍了拍等）。
        show_time: 是否给每条事件加绝对时间。
        now: 当前时间戳（默认 time.time()）。
        expiry_announced: 已补报过期的禁言 eid 集合（dict，渲染时会写入）。

    Returns:
        渲染后的摘要文本（空则返回空串）。
    """
    if not events:
        return ""

    now = time.time() if now is None else now
    header = env_header(now)

    def _assemble(evts: list[QQEvent]) -> str:
        by_t: dict[str, list[QQEvent]] = {}
        for ev in evts:
            by_t.setdefault(ev.etype, []).append(ev)
        for et in by_t:
            by_t[et].sort(key=lambda e: e.ts, reverse=True)

        # 合并型类型：渲染函数内部会归并，传全量避免丢事件
        merge_types = {E_POKE, E_EMOJI_LIKE}
        items: list[tuple[int, str]] = []
        for et, subset_all in by_t.items():
            subset = subset_all if et in merge_types else subset_all[:max_per_type]
            items.extend(_render_type(
                et, subset, all_events=evts,
                poke_action_text=poke_action_text,
                now=now, expiry_announced=expiry_announced,
            ))
        if not items:
            return ""
        # 时间线：旧→新（最新的事件排在最后一行，符合"从上往下读"的聊天习惯）
        # 排序键是事件原始时间戳（秒），同一秒内保持插入顺序（稳定排序）
        items.sort(key=lambda x: x[0])
        lines = []
        for ts, text in items:
            if show_time:
                t = fmt_clock(ts, now)
                lines.append(f"· {t} {text}" if t else f"· {text}")
            else:
                lines.append(f"· {text}")
        return header + "\n" + "\n".join(lines)

    candidate = _assemble(events)

    # 踢旧淘汰：超限从最旧事件整条删除（不是截断字符串）
    # 至少保留 1 条（即使超限也返回，避免空摘要）
    while len(candidate) > max_chars and len(events) > 1:
        events = events[1:]
        candidate = _assemble(events)

    return candidate


def _render_type(etype: str, events: list[QQEvent],
                 all_events: list[QQEvent] | None = None,
                 poke_action_text: bool = False,
                 now: Optional[float] = None,
                 expiry_announced: Optional[dict] = None) -> list[tuple[int, str]]:
    """渲染单个类型的事件，返回 [(时间戳, 文本)]。

    Args:
        etype: 事件类型。
        events: 该类型的事件（已排序、已截断到 max_per_type）。
        all_events: 全量事件（用于跨类型配对，如 ban ↔ unban）。
        poke_action_text: 是否用真实动作文案。
        now: 当前时间戳。
        expiry_announced: 禁言"已补报到期"集合。
    """
    all_events = all_events if all_events is not None else events
    now = time.time() if now is None else now
    out: list[tuple[int, str]] = []

    if etype == E_POKE:
        # 合并策略（省 token）：
        # 1. 同 actor → 同 target 的 N 次 → ×N
        # 2. 多 actor → 同 target → 合并为 "A、B、C 戳了戳 T"
        pair_count: dict[tuple[str, str], int] = {}
        pair_ts: dict[tuple[str, str], int] = {}
        actor_names: dict[str, str] = {}
        target_names: dict[str, str] = {}
        pair_action: dict[tuple[str, str], str] = {}
        for ev in events:
            key = (ev.actor, ev.target)
            pair_count[key] = pair_count.get(key, 0) + 1
            pair_ts[key] = max(pair_ts.get(key, 0), ev.ts)
            if ev.actor:
                actor_names[ev.actor] = ev.actor_name
            if ev.target:
                target_names[ev.target] = ev.target_name
            # 默认归一为"戳了戳"，与框架归一解析保持一致；开启后才用协议端真实文案
            if poke_action_text:
                act = str(ev.extra.get("raw_action") or "").strip()
                if act and key not in pair_action:
                    pair_action[key] = act

        # 按 target 归并
        by_target: dict[str, list[tuple[str, int]]] = {}
        for (actor, target), cnt in pair_count.items():
            by_target.setdefault(target, []).append((actor, cnt))

        for target, lst in by_target.items():
            tname = target_names.get(target, "")
            total = sum(c for _, c in lst)
            ts = max((pair_ts.get((a, target), 0) for a, _ in lst), default=0)
            if len(lst) == 1:
                actor, cnt = lst[0]
                act = pair_action.get((actor, target), "") or "戳了戳"
                part = f"{_fmt_user(actor, actor_names.get(actor, ''))} {act} {_fmt_user(target, tname)}"
                if cnt > 1:
                    part += f"×{cnt}"
            else:
                # 多人戳同一人：列出前 3 个 + "等N人"
                names = [
                    _fmt_user(a, actor_names.get(a, ""))
                    for a, _ in lst[:3]
                ]
                more = f"等{len(lst)}人" if len(lst) > 3 else ""
                acts = [pair_action.get((a, target), "") for a, _ in lst]
                acts = [a for a in acts if a]
                act = max(set(acts), key=acts.count) if acts else "戳了戳"
                part = f"{'、'.join(names)}{more} {act} {_fmt_user(target, tname)}"
                if total > len(lst):
                    part += f"（共{total}次）"
            out.append((ts, part))
        return out

    elif etype == E_EMOJI_LIKE:
        # 合并：按 (actor, target_message_author) 归并
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
                    "ts": ev.ts,
                }
            else:
                for e in ev.extra.get("emoji_ids", []):
                    if e and e not in g["emoji_ids"]:
                        g["emoji_ids"].append(e)
                g["count"] += ev.extra.get("count", 1)
                g["ts"] = max(g["ts"], ev.ts)
                mid = ev.extra.get("message_id", "")
                if mid and mid not in g["message_ids"] and len(g["message_ids"]) < 3:
                    g["message_ids"].append(mid)

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
            out.append((g["ts"], part))
        return out

    elif etype == E_BAN:
        # 禁言：状态机
        # - 已解除 → "（已解除）"
        # - 仍禁言中 → "（剩约N分钟）"
        # - 已自然到期 → "（已于 HH:MM 到期）"，只报一次
        unban_by_target: dict[str, list[QQEvent]] = {}
        for ev in all_events:
            if ev.etype == E_UNBAN and ev.target:
                unban_by_target.setdefault(ev.target, []).append(ev)

        for ev in events:
            duration = _ban_duration(ev)
            if ev.actor:
                part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 禁言"
            else:
                part = f"{_fmt_user(ev.target, ev.target_name)} 被禁言"
            if duration > 0:
                part += f" {duration}秒"

            unban = next(
                (u for u in unban_by_target.get(ev.target, []) if u.ts >= ev.ts),
                None,
            )
            if unban:
                part += "（已解除）"
            elif duration > 0:
                remain = int(duration - (now - ev.ts))
                if remain > 0:
                    part += f"（剩约{max(1, remain // 60 + 1)}分钟）"
                else:
                    # 已经到期：只补报一次，之后由 filter_env_events 过滤掉
                    if expiry_announced is not None and ev.eid in expiry_announced:
                        continue
                    if expiry_announced is not None:
                        expiry_announced[ev.eid] = now
                    part += f"（已于 {fmt_clock(ev.ts + duration, now)} 到期）"
            out.append((ev.ts, part))
        return out

    elif etype == E_UNBAN:
        # 解除禁言：若已作为某个 ban 的配对显示，这里跳过（避免重复）
        ban_targets = {
            ev.target for ev in all_events if ev.etype == E_BAN and ev.target
        }
        for ev in events:
            if ev.target in ban_targets:
                continue
            if ev.actor:
                part = f"{_fmt_user(ev.target, ev.target_name)} 被 {_fmt_user(ev.actor, ev.actor_name)} 解除禁言"
            else:
                part = f"{_fmt_user(ev.target, ev.target_name)} 被解除禁言"
            out.append((ev.ts, part))
        return out

    elif etype == E_RECALL:
        # 合并：同一操作者的多次撤回合并为 "X 撤回了 N 条消息"
        by_actor: dict[str, list[QQEvent]] = {}
        for ev in events:
            key = ev.actor or ev.target or "unknown"
            by_actor.setdefault(key, []).append(ev)

        for key, lst in by_actor.items():
            ev0 = lst[0]
            who = _fmt_user(ev0.actor or ev0.target, ev0.actor_name or ev0.target_name)
            if ev0.actor and ev0.target and ev0.actor != ev0.target:
                detail = "撤回了他人消息"
            else:
                detail = "撤回了一条消息"
            ts = max(e.ts for e in lst)
            if len(lst) > 1:
                out.append((ts, f"{who} {detail}×{len(lst)}"))
            else:
                out.append((ts, f"{who} {detail}"))
        return out

    elif etype == E_TITLE:
        for ev in events:
            title = ev.extra.get("title", "")
            out.append((ev.ts, f"{_fmt_user(ev.target, ev.target_name)} 被授予头衔\"{title}\""))
        return out

    elif etype == E_CARD:
        for ev in events:
            card_new = ev.extra.get("card_new", "")
            card_old = ev.extra.get("card_old", "")
            if card_new and card_old:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片变更: {card_old} → {card_new}"
            elif card_new:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片更新为: {card_new}"
            else:
                part = f"{_fmt_user(ev.target, ev.target_name)} 群名片更新"
            out.append((ev.ts, part))
        return out

    elif etype == E_ADMIN:
        for ev in events:
            sub = ev.extra.get("sub_type", "")
            action = "被设为管理员" if sub == "set" else "被取消管理员"
            out.append((ev.ts, f"{_fmt_user(ev.target, ev.target_name)} {action}"))
        return out

    # 唤醒级事件（E_KICKED_SELF / E_TITLE_SELF / E_EMOJI_LIKE_SELF / E_PROFILE_LIKE_SELF）
    # 与退群/被踢（E_MEMBER_DECREASE）都不在环境摘要里
    return out
