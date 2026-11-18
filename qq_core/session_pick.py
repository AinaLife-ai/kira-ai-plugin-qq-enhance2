"""会话挑选：给"没有 group_id 的 notice"（资料卡点赞）选一个合适的会话。

为什么单独成模块：
    profile_like 的 notice 不带 group_id，框架按 `user_id` 建会话 → `qq:dm:None`。
    选会话的规则（**最近活跃，群聊/私聊一起比**）是纯逻辑，放在 main.py 里不好测。

数据结构：
    store: dict[(adapter_name, uid) → dict[sid → {"ts": 最后活跃时间, "spoke": 是否发过言}]]
    每个用户记多个会话（群 + 私聊），不是只记最后一条——否则"用户刚私聊完又去群里说话"
    时，点赞会错误地落到私聊。

挑选规则（用户 2026-11-18 明确）：
    取该用户**最后（最新）活跃**的会话（群聊/私聊一起比较）——
    "活跃"优先取"发过言"的会话（真实消息），只有从没发过言时才退到"只有通知动作"的会话。
"""

from __future__ import annotations

import time
from typing import Optional

# 会话新鲜期：超过 7 天没活跃的会话不再用来收点赞通知
LIKE_SESSION_MAX_AGE = 7 * 86400
# 单用户最多记多少个会话（群+私聊）
MAX_SESSIONS_PER_USER = 8
# 全局最多记多少个用户
MAX_USERS = 2000


def remember_session(
    store: dict,
    adapter_name: str,
    uid: str,
    sid: str,
    *,
    spoke: bool = False,
    now: Optional[float] = None,
) -> None:
    """记录"某用户最近活跃的会话"。

    Args:
        store: 全局字典 {(adapter_name, uid): {sid: {"ts": float, "spoke": bool}}}。
        adapter_name: 适配器名。
        uid: 用户 QQ。
        sid: 会话 ID（`adapter:gm:gid` / `adapter:dm:uid`）。
        spoke: 这次是否是该用户**发的消息**（True）还是仅仅某个通知动作（False）。
        now: 当前时间（测试可注入）。
    """
    uid = str(uid or "")
    sid = str(sid or "")
    if not uid or uid == "None" or not sid or sid.endswith(":None"):
        return
    now = time.time() if now is None else now
    key = (adapter_name, uid)
    sessions = store.setdefault(key, {})
    prev = sessions.get(sid) or {}
    sessions[sid] = {
        "ts": now,
        # 只要发过一次言，这个会话就算"发过言"（不会因为后来的通知动作被降级）
        "spoke": bool(prev.get("spoke")) or bool(spoke),
    }

    # 单用户上限：只留最近 N 个会话
    if len(sessions) > MAX_SESSIONS_PER_USER:
        for old_sid, _ in sorted(sessions.items(), key=lambda kv: kv[1]["ts"])[:-MAX_SESSIONS_PER_USER]:
            sessions.pop(old_sid, None)

    # 全局上限：先清 7 天前的，再按"最近活跃"保留 MAX_USERS 个用户（**就地修改**）
    if len(store) > MAX_USERS:
        cutoff = now - LIKE_SESSION_MAX_AGE
        for k in list(store.keys()):
            fresh = {s: v for s, v in store[k].items() if v["ts"] >= cutoff}
            if fresh:
                store[k] = fresh
            else:
                store.pop(k, None)
        if len(store) > MAX_USERS:
            keep = sorted(
                store.items(),
                key=lambda kv: max(v["ts"] for v in kv[1].values()) if kv[1] else 0,
                reverse=True,
            )[:MAX_USERS]
            keep_keys = {k for k, _ in keep}
            for k in list(store.keys()):
                if k not in keep_keys:
                    store.pop(k, None)


def pick_like_sid(
    store: dict,
    adapter_name: str,
    operator: str,
    *,
    force_dm: bool = False,
    now: Optional[float] = None,
    max_age: float = LIKE_SESSION_MAX_AGE,
) -> str:
    """给资料卡点赞挑会话：**该用户最后活跃的那个会话**（群/私聊一起比）。

    规则：
      1. 7 天内、该用户**发过言**的会话里取最新；
      2. 没有发言记录 → 7 天内任何活跃会话（戳过/被贴过表情等）取最新；
      3. 都没有 → `adapter:dm:uid`。

    Args:
        store: 全局字典 {(adapter_name, uid): {sid: {"ts": float, "spoke": bool}}}。
        adapter_name: 适配器名。
        operator: 点赞的人 QQ。
        force_dm: True=强制私聊（配置项 profile_like_dm_only）。
        now: 当前时间（测试可注入）。
        max_age: 会话新鲜期（秒）。

    Returns:
        会话 ID。
    """
    uid = str(operator or "")
    if force_dm or not uid:
        return f"{adapter_name}:dm:{uid}"

    now = time.time() if now is None else now
    sessions = store.get((adapter_name, uid)) or {}
    alive = {
        sid: info for sid, info in sessions.items()
        if info and info.get("ts") and now - info["ts"] <= max_age
        and sid and not sid.endswith(":None")
    }

    # 优先"发过言"的会话：群聊/私聊同等对待，只比时间
    spoke = {sid: info for sid, info in alive.items() if info.get("spoke")}
    pool = spoke or alive
    if pool:
        return max(pool, key=lambda s: pool[s]["ts"])

    return f"{adapter_name}:dm:{uid}"
