"""第十五轮回归测试：资料卡点赞的会话挑选（先群后私聊）。

用户实机反馈：
    用户"刚私聊过、也在群里活跃"时，资料卡点赞被通知到了私聊。
根因：
    `_recent_sessions[(adapter, uid)]` 只存**最后一条**消息的会话，
    私聊后接着来点赞 → 命中私聊；群会话被覆盖掉了。
修复：
    每个用户记多个会话（群+私聊），`pick_like_sid` 明确 **先最近的群，再私聊**。
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
    """items: (sid, 距今多少秒)"""
    store = {}
    now = time.time()
    for sid, ago in items:
        remember_session(store, ADAPTER, UID, sid, now=now - ago)
    return store


def test_prefers_group_over_newer_dm():
    """群 1 小时前说过话、私聊 10 秒前 → 仍选群（本次修复的核心）。"""
    store = _store((GM_A, 3600), (DM, 10))
    assert pick_like_sid(store, ADAPTER, UID) == GM_A


def test_newest_group_wins():
    store = _store((GM_A, 3600), (GM_B, 60), (DM, 5))
    assert pick_like_sid(store, ADAPTER, UID) == GM_B


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


def test_old_sessions_do_not_leak_into_group_choice():
    """7 天前的群不算，但同一用户 2 天前的群要算。"""
    store = _store((GM_A, 2 * 86400), (DM, 1))
    assert pick_like_sid(store, ADAPTER, UID) == GM_A
