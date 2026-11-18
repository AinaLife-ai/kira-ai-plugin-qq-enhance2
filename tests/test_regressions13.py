"""第十五轮回归测试：资料卡点赞的会话挑选。

规则（用户 2026-11-18 明确）：
    取该用户**最后（最新）活跃**的会话（群聊/私聊**一起比较**），
    优先"发过言"的会话；从没发过言才退到"只有通知动作"的会话；都没有才私聊。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from qq_core.session_pick import (  # noqa: E402
    pick_like_sid,
    remember_session,
    LIKE_SESSION_MAX_AGE,
    MAX_SESSIONS_PER_USER,
    MAX_USERS,
)

ADAPTER = "qq"
UID = "769690776"
GM_A = "qq:gm:111"
GM_B = "qq:gm:222"
DM = f"qq:dm:{UID}"


def _store(*items):
    """items: (sid, 距今多少秒) 或 (sid, 距今多少秒, 是否发过言)"""
    store = {}
    now = time.time()
    for item in items:
        sid, ago = item[0], item[1]
        spoke = item[2] if len(item) > 2 else True
        remember_session(store, ADAPTER, UID, sid, spoke=spoke, now=now - ago)
    return store


def test_newer_dm_beats_older_group():
    """私聊 10 秒前发过言、群 1 小时前发过言 → 选**私聊**（最新活跃，群私聊同权）。"""
    store = _store((GM_A, 3600), (DM, 10))
    assert pick_like_sid(store, ADAPTER, UID) == DM


def test_newer_group_beats_older_dm():
    """反过来：群更新 → 选群。"""
    store = _store((GM_A, 10), (DM, 3600))
    assert pick_like_sid(store, ADAPTER, UID) == GM_A


def test_spoke_session_beats_merely_active_one():
    """只有通知动作的会话（没发言）不算"最后活跃"，让位给发过言的会话。"""
    store = _store((GM_A, 5, False), (DM, 600, True))
    assert pick_like_sid(store, ADAPTER, UID) == DM


def test_newest_session_wins_regardless_of_type():
    """多个会话时只看时间：私聊 5 秒前 > 群 60 秒前 > 群 1 小时前。"""
    store = _store((GM_A, 3600), (GM_B, 60), (DM, 5))
    assert pick_like_sid(store, ADAPTER, UID) == DM
    store2 = _store((GM_A, 3600), (GM_B, 60), (DM, 120))
    assert pick_like_sid(store2, ADAPTER, UID) == GM_B


def test_falls_back_to_dm_without_group():
    store = _store((DM, 30))
    assert pick_like_sid(store, ADAPTER, UID) == DM


def test_stale_group_expires():
    """群会话超过 7 天 → 退到私聊。"""
    store = _store((GM_A, LIKE_SESSION_MAX_AGE + 60), (DM, 30))
    assert pick_like_sid(store, ADAPTER, UID) == DM


def test_no_record_falls_back_to_dm():
    assert pick_like_sid({}, ADAPTER, UID) == DM


def test_force_dm_wins():
    """profile_like_dm_only=true → 强制私聊。"""
    store = _store((GM_A, 60))
    assert pick_like_sid(store, ADAPTER, UID, force_dm=True) == DM


def test_none_sid_ignored():
    store = {}
    remember_session(store, ADAPTER, UID, "qq:gm:None")
    remember_session(store, ADAPTER, "None", "qq:gm:1")
    assert store == {}
    assert pick_like_sid(store, ADAPTER, UID) == DM


def test_per_user_session_cap():
    store = {}
    now = time.time()
    for i in range(MAX_SESSIONS_PER_USER + 5):
        remember_session(store, ADAPTER, UID, f"qq:gm:{1000 + i}", now=now + i)
    assert len(store[(ADAPTER, UID)]) == MAX_SESSIONS_PER_USER
    # 最旧的被淘汰，最新的还在
    assert f"qq:gm:{1000 + MAX_SESSIONS_PER_USER + 4}" in store[(ADAPTER, UID)]


def test_global_user_cap_prunes_in_place():
    """全局上限时**就地**裁剪（不能换成新 dict，否则 main.py 的引用失效）。"""
    store = {}
    now = time.time()
    for i in range(MAX_USERS + 10):
        remember_session(store, ADAPTER, f"u{i}", f"qq:dm:u{i}", now=now + i)
    assert len(store) == MAX_USERS
    # 最新的那批用户保留
    assert (ADAPTER, f"u{MAX_USERS + 9}") in store


def test_old_sessions_are_ignored():
    """7 天前的单聊不算；2 天前的群算。"""
    store = _store((GM_A, 2 * 86400), (DM, LIKE_SESSION_MAX_AGE + 60))
    assert pick_like_sid(store, ADAPTER, UID) == GM_A


def test_spoke_flag_is_sticky():
    """发过言的会话后来只来了通知 → 仍然是"发过言"的会话，不会被降级。"""
    store = _store((GM_A, 600, True))
    remember_session(store, ADAPTER, UID, GM_A, spoke=False)
    assert store[(ADAPTER, UID)][GM_A]["spoke"] is True


def test_lookup_is_per_user_not_global():
    """关键隔离：A 和 B 在同一个群，B 最近在群里发言 → 不能把"群活跃"算到 A 头上。

    点赞的是 A，就要找 **A 自己**最后活跃的会话；B 的发言只是 B 的活跃。
    """
    store = {}
    now = time.time()
    remember_session(store, ADAPTER, "userB", GM_A, spoke=True, now=now)        # B 刚在群里发言
    remember_session(store, ADAPTER, "userA", DM, spoke=True, now=now - 300)    # A 5 分钟前私聊发言
    assert pick_like_sid(store, ADAPTER, "userA") == DM

    # 反过来：A 在群里发言、B 在私聊 → A 的点赞要进群
    store2 = {}
    remember_session(store2, ADAPTER, "userB", f"qq:dm:userB", spoke=True, now=now)
    remember_session(store2, ADAPTER, "userA", GM_A, spoke=True, now=now - 300)
    assert pick_like_sid(store2, ADAPTER, "userA") == GM_A


def test_multi_user_multi_group_isolation():
    """A 在群 111，B 在群 222：A 的点赞只能落在 A 的群。"""
    store = {}
    now = time.time()
    remember_session(store, ADAPTER, "userA", GM_A, spoke=True, now=now - 600)
    remember_session(store, ADAPTER, "userB", GM_B, spoke=True, now=now)
    assert pick_like_sid(store, ADAPTER, "userA") == GM_A
    assert pick_like_sid(store, ADAPTER, "userB") == GM_B


def test_different_adapters_do_not_mix():
    """多适配器（多号）部署：同名 QQ 号在别的 adapter 下的会话不能串用。"""
    store = {}
    now = time.time()
    remember_session(store, "qq2", UID, "qq2:gm:333", spoke=True, now=now)
    remember_session(store, "qq", UID, DM, spoke=True, now=now - 60)
    assert pick_like_sid(store, "qq", UID) == DM
    assert pick_like_sid(store, "qq2", UID) == "qq2:gm:333"
