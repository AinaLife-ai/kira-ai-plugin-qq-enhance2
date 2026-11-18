"""第十五轮回归测试：戳一戳归属（私聊 peer≠actor）/ 私聊撤回误归属 / 头像缓存。

覆盖 2026-11-18 实机日志暴露的三类问题：
55. SnowLuma 私聊 poke 的 `user_id` 是**会话对端**，不是发起者 → bot 戳人记成"用户戳自己"
56. 私聊 recall 的 `user_id` 同样是会话对端 → 不点名，交给台账判定
57. 头像描述缓存：L1（URL+TTL）+ L2（框架 ImageDescCache，按内容 md5）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai_latest")
if not (KIRAAI / "core").is_dir():
    KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")
sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

import asyncio

from qq_core.event_normalizer import QQEvent, E_POKE, E_RECALL, normalize_notice
from qq_core.env_renderer import render_env_summary

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


BOT = "583893217"
USER = "769690776"
GROUP = "999888777"


def _poke(**kw):
    raw = {"notice_type": "notify", "sub_type": "poke", "time": 1000}
    raw.update(kw)
    return raw


def test_poke_snowluma_private_bot_pokes_user():
    """实机日志复现：bot 戳周武 → SnowLuma 报 user_id=周武(peer)/sender_id=bot。"""
    ev = normalize_notice(
        _poke(user_id=USER, sender_id=BOT, target_id=USER),
        sid=f"qq:dm:{USER}", self_id=BOT,
    )
    check("SnowLuma 私聊 bot 戳人 → 不记录", ev is None, repr(ev))


def test_poke_snowluma_private_user_pokes_bot():
    ev = normalize_notice(
        _poke(user_id=USER, sender_id=USER, target_id=BOT),
        sid=f"qq:dm:{USER}", self_id=BOT,
    )
    check("SnowLuma 私聊 用户戳 bot → 交给框架（不重复记录）", ev is None, repr(ev))


def test_poke_snowluma_private_user_to_user():
    """sender_id 才是发起者：peer 和 actor 不同时必须取 sender。"""
    other = "10001"
    ev = normalize_notice(
        _poke(user_id=USER, sender_id=other, target_id=USER),
        sid=f"qq:dm:{USER}", self_id=BOT,
    )
    ok = ev is not None and ev.etype == E_POKE and ev.actor == other and ev.target == USER
    check("SnowLuma 私聊 取 sender_id 作发起者", ok, repr(ev))


def test_poke_napcat_private_user_to_self():
    """NapCat 私聊没有 sender_id：user_id 就是发起者。"""
    ev = normalize_notice(
        _poke(user_id=USER, target_id=USER),
        sid=f"qq:dm:{USER}", self_id=BOT,
    )
    ok = ev is not None and ev.actor == USER and ev.target == USER
    check("NapCat 私聊 无 sender_id 时回落 user_id", ok, repr(ev))


def test_poke_group_uses_user_id_as_actor():
    """群 poke 三家都用 user_id=发起者，不能被 sender_id 规则误伤。"""
    raw = _poke(user_id=USER, target_id="10002", group_id=GROUP, sender_id="777")
    ev = normalize_notice(raw, sid=f"qq:gm:{GROUP}", self_id=BOT, group_id=GROUP)
    ok = ev is not None and ev.actor == USER and ev.target == "10002"
    check("群 poke 仍用 user_id 作发起者", ok, repr(ev))


def test_poke_group_self_poke_rendered():
    """真的自己戳自己（群里可以）→ 文案是"戳了戳自己"。"""
    raw = _poke(user_id=USER, target_id=USER, group_id=GROUP)
    ev = normalize_notice(raw, sid=f"qq:gm:{GROUP}", self_id=BOT, group_id=GROUP)
    check("群自戳仍被收集", ev is not None, repr(ev))
    if ev is None:
        return
    ev.actor_name, ev.target_name = "周武", "周武"
    text = render_env_summary([ev], now=1000, max_per_type=10)
    check("自戳渲染为「戳了戳自己」", "戳了戳自己" in text and "戳了戳 用户" not in text, text)


def test_poke_bot_self_poke_dropped():
    """bot 自己戳自己（极端脏数据）→ 丢弃。"""
    ev = normalize_notice(
        _poke(user_id=BOT, sender_id=BOT, target_id=BOT),
        sid=f"qq:dm:{USER}", self_id=BOT,
    )
    check("bot 自戳 → 丢弃", ev is None, repr(ev))


def test_friend_recall_not_attributed_to_peer():
    """私聊撤回不点名（user_id 是会话对端，不是操作者）。"""
    raw = {"notice_type": "friend_recall", "time": 1000, "user_id": USER, "message_id": "m1"}
    ev = normalize_notice(raw, sid=f"qq:dm:{USER}", self_id=BOT)
    ok = (
        ev is not None
        and ev.etype == E_RECALL
        and not ev.actor
        and not ev.target
        and ev.extra.get("peer") == USER
    )
    check("私聊撤回不把对端当作者", ok, repr(ev))
    if ev is None:
        return
    text = render_env_summary([ev], now=1000, max_per_type=10)
    check("未知作者时不点名撤回", "撤回了一条消息" in text and "周武" not in text, text)


def test_group_recall_keeps_operator():
    raw = {
        "notice_type": "group_recall", "time": 1000, "group_id": GROUP,
        "operator_id": "10002", "user_id": "10003", "message_id": "m2",
    }
    ev = normalize_notice(raw, sid=f"qq:gm:{GROUP}", self_id=BOT, group_id=GROUP)
    ok = ev is not None and ev.actor == "10002" and ev.target == "10003"
    check("群撤回保留 operator/author", ok, repr(ev))


# ==================== 头像缓存 ====================

class FakeImageCache:
    """模拟框架 ImageDescCache（md5 → 描述，计数命中）。"""

    def __init__(self):
        self.data = {}
        self.gets = 0
        self.sets = 0

    async def get(self, md5):
        self.gets += 1
        return self.data.get(md5)

    async def set(self, md5, desc):
        self.sets += 1
        if desc:
            self.data[md5] = desc


class FakeCtx:
    def __init__(self, cache=None):
        self.image_desc_cache = cache

        class _PM:
            @staticmethod
            def get_default_vlm():
                return object()

        self.provider_mgr = _PM()
        self.config = {"locale": {"lang": "zh"}}


async def _fake_prepare(url, need_md5=False):
    """替身：不联网，固定内容 md5。"""
    class _Img:
        md5 = "md5-abc"
    return _Img(), ("md5-abc" if need_md5 else "")


def _run_avatar_test(ttl, urls, md5_value="md5-abc"):
    """跑一组 describe_avatar，返回 (VLM 调用次数, 描述列表, fake_cache)。"""
    import qq_core.avatar_view as av

    av.clear_l1_cache()
    cache = FakeImageCache()
    calls = {"n": 0}

    async def fake_desc_img(client=None, image=None, prompt=None, lang=None):
        calls["n"] += 1
        return "一个蓝发女孩头像"

    orig_desc = av._desc_img
    orig_prepare = av._prepare_image

    async def fake_prepare(url, need_md5=False):
        return await _fake_prepare(url, need_md5)

    av._desc_img = fake_desc_img
    av._prepare_image = fake_prepare
    try:
        ctx = FakeCtx(cache)
        results = [asyncio.run(av.describe_avatar(u, ctx=ctx, ttl=ttl)) for u in urls]
    finally:
        av._desc_img = orig_desc
        av._prepare_image = orig_prepare
        av.clear_l1_cache()
    return calls["n"], results, cache


def test_avatar_l1_and_l2():
    n, results, cache = _run_avatar_test(60, ["https://x/a.png", "https://x/a.png"])
    check("L1 命中只调 1 次 VLM", n == 1, n)
    check("两次描述一致", results[0] == results[1] == "一个蓝发女孩头像", results)
    check("L2 已按 md5 写入", cache.data.get("md5-abc") == "一个蓝发女孩头像", cache.data)

    # 清 L1 → 换 URL 但内容相同 → 走 L2，不再调 VLM
    import qq_core.avatar_view as av
    n2, results2, cache2 = _run_avatar_test(60, ["https://x/a.png?v=2"])
    check("L2 命中（同内容不同 URL）不重复识别", n2 == 0 and results2[0] == "一个蓝发女孩头像", (n2, results2))


def test_avatar_ttl_zero_disables_cache():
    n, results, cache = _run_avatar_test(0, ["https://x/b.png", "https://x/b.png"])
    check("ttl=0 每次都识别", n == 2, n)
    check("ttl=0 不写 L2", cache.sets == 0, cache.sets)


def _main():
    test_poke_snowluma_private_bot_pokes_user()
    test_poke_snowluma_private_user_pokes_bot()
    test_poke_snowluma_private_user_to_user()
    test_poke_napcat_private_user_to_self()
    test_poke_group_uses_user_id_as_actor()
    test_poke_group_self_poke_rendered()
    test_poke_bot_self_poke_dropped()
    test_friend_recall_not_attributed_to_peer()
    test_group_recall_keeps_operator()
    test_avatar_l1_and_l2()
    test_avatar_ttl_zero_disables_cache()

    print("\n" + "=" * 60)
    print(f"回归15: {len(PASS)} 通过, {len(FAIL)} 失败")
    if FAIL:
        for name, detail in FAIL:
            print(f"  FAIL {name}: {detail}")
        sys.exit(1)
    print("=== 全部通过 ===")


if __name__ == "__main__":
    _main()
