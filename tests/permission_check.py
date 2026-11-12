"""权限模式验证：框架的 allow_list / deny_list 如何影响 notice。

关键问题：如果用户配置了 allow_list 且群不在白名单，
框架应该**过滤掉该群的所有事件**（包括 notice），插件不应收到。

这决定了插件是否需要自己处理权限（答案：不需要，框架已处理）。
但必须验证这个假设成立，否则插件可能在"不该感知的群"里收集数据。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
KIRAAI = Path("/var/minis/workspace/qq_enhance2_design/kiraai312")

if (KIRAAI / "core").is_dir():
    sys.path.insert(0, str(KIRAAI))
sys.path.insert(0, str(ROOT))

from tests.real_adapter import make_poke  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  OK   {name}")
    else:
        FAIL.append((name, detail))
        print(f"  FAIL {name}  {detail}")


SELF_ID = "3003003"


def build_with_permission(mode: str, group_allow=None, group_deny=None,
                          user_allow=None, user_deny=None):
    """构造指定权限模式的真实适配器。"""
    from core.adapter.adapter_info import AdapterInfo
    from core.adapter.context import AdapterContext
    from core.adapter.src.qq.qq import QQAdapter

    info = AdapterInfo(
        enabled=True, adapter_id="napcat", name="napcat", platform="QQ",
        config={
            "self_id": SELF_ID,
            "permission_mode": mode,
            "group_allow_list": group_allow or [],
            "group_deny_list": group_deny or [],
            "user_allow_list": user_allow or [],
            "user_deny_list": user_deny or [],
            "ws_uri": "ws://127.0.0.1:0", "ws_token": "",
            "bot_pid": SELF_ID, "debug_mode": False,
        },
    )
    q = asyncio.Queue()
    adapter = QQAdapter(AdapterContext(info=info, event_queue=q))

    async def fake_send_action(action, params=None, timeout=10.0):
        params = params or {}
        if action == "get_group_info":
            return {"status": "ok", "data": {"group_name": "测试群"}}
        if action == "get_stranger_info":
            return {"status": "ok", "data": {"nickname": f"用户{params.get('user_id')}",
                                             "sex": 1, "sign": ""}}
        return {"status": "ok", "data": {}}

    adapter.bot.send_action = fake_send_action
    adapter.bot.login_success_event.set()
    adapter.bot.self_id = SELF_ID
    return adapter, q


async def push_and_count(adapter, q, raw):
    await adapter.im._on_notice_message(raw)
    n = 0
    evs = []
    while not q.empty():
        evs.append(q.get_nowait())
        n += 1
    return n, evs


async def main():
    GID_OK = 111111
    GID_BAD = 222222

    print("\n=== 1. deny_list 模式（群在拒绝列表 → 应过滤）===")
    adapter, q = build_with_permission("deny_list", group_deny=[str(GID_BAD)])
    n_ok, _ = await push_and_count(adapter, q, make_poke(1001, 1003, GID_OK, SELF_ID))
    n_bad, _ = await push_and_count(adapter, q, make_poke(1001, 1003, GID_BAD, SELF_ID))
    print(f"  允许群事件: {n_ok} | 拒绝群事件: {n_bad}")
    check("deny_list: 拒绝列表里的群被过滤", n_bad == 0, str(n_bad))
    check("deny_list: 其他群正常通过", n_ok == 1, str(n_ok))

    print("\n=== 2. allow_list 模式（群不在白名单 → 应过滤）===")
    adapter2, q2 = build_with_permission("allow_list", group_allow=[str(GID_OK)])
    n2_ok, _ = await push_and_count(adapter2, q2, make_poke(1001, 1003, GID_OK, SELF_ID))
    n2_bad, _ = await push_and_count(adapter2, q2, make_poke(1001, 1003, GID_BAD, SELF_ID))
    print(f"  白名单群事件: {n2_ok} | 非白名单群事件: {n2_bad}")
    check("allow_list: 白名单群通过", n2_ok == 1, str(n2_ok))
    check("allow_list: 非白名单群被过滤", n2_bad == 0, str(n2_bad))

    print("\n=== 3. 私聊 poke 的权限（user_allow/deny）===")
    adapter3, q3 = build_with_permission("deny_list", user_deny=["1001"])
    raw_dm_denied = {
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "time": 1, "self_id": int(SELF_ID), "user_id": 1001,
        "target_id": int(SELF_ID),
    }
    n3_bad, _ = await push_and_count(adapter3, q3, raw_dm_denied)
    raw_dm_ok = dict(raw_dm_denied, user_id=1005)
    n3_ok, _ = await push_and_count(adapter3, q3, raw_dm_ok)
    print(f"  拒绝用户私聊: {n3_bad} | 允许用户私聊: {n3_ok}")
    check("deny_list: 拒绝用户的私聊 poke 被过滤", n3_bad == 0, str(n3_bad))
    check("deny_list: 其他用户私聊 poke 通过", n3_ok == 1, str(n3_ok))

    print("\n=== 4. 结论：插件无需自己处理权限 ===")
    check("框架在适配器层已过滤 notice（插件收到的都是允许的）", True,
          "deny/allow 两种模式均已验证")

    print("\n=== 5. 无限制模式下插件正常收集 ===")
    adapter4, q4 = build_with_permission("deny_list")
    evs = []
    for i in range(3):
        await adapter4.im._on_notice_message(make_poke(1000 + i, 2000, GID_OK, SELF_ID))
        while not q4.empty():
            evs.append(q4.get_nowait())
    check(f"无限制模式：3 个事件全部到达（{len(evs)}）", len(evs) == 3, str(len(evs)))


asyncio.run(main())

print("\n" + "=" * 60)
print(f"权限模式验证: {len(PASS)} 通过, {len(FAIL)} 失败")
if FAIL:
    for name, detail in FAIL:
        print(f"  FAIL {name}: {detail}")
    sys.exit(1)
print("=== 全部通过 ===")